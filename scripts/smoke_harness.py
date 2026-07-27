"""Generic live smoke-test engine for a FastMCP server.

Enumerates every tool the server actually registers and exercises each one
against real data, so a tool that exists but does not work is caught the day
it breaks. Unit tests cannot cover this class of failure: the earnings
calendar tools once returned well-formed *empty* results for every query
because the cache lacked the requested dates, and the suite stayed green.

The engine holds no server-specific knowledge. A companion "probes" module
supplies the per-tool specs (see ``smoke_probes.py`` for this repo's), so the
same engine can smoke-test any other FastMCP server by swapping that module.
Its one deployment-wide assumption is the timezone report timestamps are
rendered in (JST).

Design notes:

* **Registry-driven.** Tools come from ``list_tools()``, never a hand-written
  list, and a registered tool with no probe spec is a FAILURE — adding a tool
  forces a deliberate decision about how to verify it.
* **Non-triviality.** "No exception" is not success. A probe asserts the shape
  it expects (rows present, keys present) and may assert *freshness*, which is
  what distinguishes a working tool from one quietly serving stale data.
* **Operational tolerance.** Plan-restricted endpoints pass when they return
  the explicit restriction error, and destructive tools are skipped by name.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

JST = timezone(timedelta(hours=9))

#: Statuses that mean the run failed.
FAILING = frozenset({"FAIL", "NO_SPEC"})

_DATE_TOKEN_RE = re.compile(r"^\{(today|t)(?:([+-])(\d+))?\}$")

#: A callable that invokes one tool and returns its decoded payload.
Caller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class SkipProbe(Exception):
    """Raised by an ``args_factory`` when the probe cannot be prepared.

    The message is shown in the report as the skip reason, and — unlike every
    other detail the engine prints — it is NOT redacted, because it is written
    by the probe author rather than returned by the server. That is a contract,
    not an enforcement: on a server whose reports must be safe to paste
    anywhere, a reason must describe the *situation* ("no group membership to
    probe with"), never interpolate what was discovered.
    """



@dataclass(frozen=True)
class Probe:
    """How to exercise one tool and what counts as a working answer.

    Args may embed date tokens resolved at run time: ``{t}`` is the reference
    business day, ``{t-30}`` is 30 calendar days earlier, ``{today}`` is the
    current date. Tokens keep specs stable — a spec must never hardcode a date,
    or it silently rots into "tested nothing" once that date ages out of cache.
    """

    args: dict[str, Any] = field(default_factory=dict)
    #: Builds args from a live call, for tools whose input comes from another
    #: tool's output (e.g. a download key listed by a companion tool). What it
    #: returns is merged over ``args``, so a probe can pin the bounds it cares
    #: about and discover only the identifier. Raising ``SkipProbe`` reports a
    #: SKIP.
    args_factory: Callable[[Caller], Awaitable[dict[str, Any]]] | None = None
    #: Dotted path to the list of rows; when None the longest list is used.
    rows_key: str | None = None
    #: Minimum rows for the probe to count as returning real data.
    min_rows: int = 1
    #: Top-level keys that must be present in the payload.
    require_keys: tuple[str, ...] = ()
    #: Dotted path -> minimum numeric value. Catches summary-shaped answers that
    #: are technically well-formed but computed over an empty universe.
    min_values: dict[str, float] = field(default_factory=dict)
    #: Regexes the rendered payload must contain. The way to assert a tool that
    #: answers with formatted text rather than rows — common outside this repo.
    must_match: tuple[str, ...] = ()
    #: Regexes the payload must NOT contain, for tools whose failure mode is a
    #: polite "no data" sentence rather than an error.
    must_not_match: tuple[str, ...] = ()
    #: Minimum rendered length; a text tool degrading to a header line or an
    #: empty string is otherwise indistinguishable from a working one.
    min_chars: int = 0
    #: Field holding a date; combined with fresh_within_days for staleness.
    date_field: str | None = None
    #: Newest date_field value must be >= today - N days (0 = today or later).
    fresh_within_days: int | None = None
    #: Treat an explicit plan-restriction error as a pass (the tool works; the
    #: subscription does not cover it). Data still passes when cache serves it.
    allow_plan_restriction: bool = False
    #: Zero rows is an acceptable answer (a detector that found nothing today).
    #: It does NOT waive structural checks: the payload must still be a
    #: container, and such a probe is expected to assert something concrete via
    #: ``require_keys`` / ``min_values`` — otherwise a tool returning ``{}``
    #: would sail through, which is the blind spot this harness exists to close.
    allow_empty: bool = False
    #: Non-None skips the tool entirely; the string is the reason shown.
    skip: str | None = None
    timeout: float = 90.0


@dataclass
class Result:
    tool: str
    status: str
    detail: str = ""
    elapsed: float = 0.0
    rows: int | None = None


# ---------------------------------------------------------------------------
# Date tokens


def resolve_tokens(value: Any, reference: date, today: date) -> Any:
    """Recursively replace ``{t}`` / ``{t-N}`` / ``{today}`` date tokens."""
    if isinstance(value, str):
        m = _DATE_TOKEN_RE.match(value)
        if not m:
            return value
        base = reference if m.group(1) == "t" else today
        if m.group(2):
            delta = timedelta(days=int(m.group(3)))
            base = base - delta if m.group(2) == "-" else base + delta
        return base.isoformat()
    if isinstance(value, dict):
        return {k: resolve_tokens(v, reference, today) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_tokens(v, reference, today) for v in value]
    return value


def previous_weekday(today: date) -> date:
    """Latest weekday strictly before ``today`` (holiday-blind fallback).

    Strictly before, because a probe must not depend on whether the current
    trading day has been ingested yet — that varies with the time of day and
    would make the smoke test flap.
    """
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Payload inspection


def _dig(payload: Any, path: str) -> Any:
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def count_rows(payload: Any, rows_key: str | None) -> int | None:
    """Row count for the payload, or None when no list-shaped data is found."""
    if rows_key is not None:
        target = _dig(payload, rows_key)
        return len(target) if isinstance(target, list) else None
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    best: int | None = None
    for value in payload.values():
        if isinstance(value, list):
            best = len(value) if best is None else max(best, len(value))
        elif isinstance(value, dict):
            nested = count_rows(value, None)
            if nested is not None:
                best = nested if best is None else max(best, nested)
    return best


def _iter_dates(payload: Any, field_name: str):
    """Yield every ``field_name`` value found anywhere in the payload."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == field_name and isinstance(value, str):
                yield value
            else:
                yield from _iter_dates(value, field_name)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_dates(item, field_name)


def max_date(payload: Any, field_name: str) -> date | None:
    newest: date | None = None
    for raw in _iter_dates(payload, field_name):
        text = raw[:10]
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


# ---------------------------------------------------------------------------
# Evaluation


def _describe(exc: BaseException, redact: bool) -> str:
    """Render an exception for the report, optionally without its message."""
    if redact:
        return f"{type(exc).__name__} (message redacted)"
    return f"{type(exc).__name__}: {exc}"


def evaluate(
    tool: str, probe: Probe, payload: Any, today: date, redact_details: bool = False
) -> Result:
    """Decide whether one payload proves the tool works.

    ``redact_details`` suppresses server-authored message text in the result,
    for deployments whose errors quote the data they were asked about.
    """
    if isinstance(payload, dict) and payload.get("error"):
        kind = payload.get("error_type", "?")
        if probe.allow_plan_restriction and kind == "PlanRestrictionError":
            return Result(tool, "RESTRICTED", "plan does not cover this endpoint")
        if redact_details:
            return Result(tool, "FAIL", f"{kind} (message redacted)")
        message = str(payload.get("message", ""))[:120]
        return Result(tool, "FAIL", f"{kind}: {message}")

    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    if len(text) < probe.min_chars:
        return Result(tool, "FAIL", f"{len(text)} chars (want >= {probe.min_chars})")
    for pattern in probe.must_match:
        if not re.search(pattern, text, re.MULTILINE):
            return Result(tool, "FAIL", f"missing expected pattern: {pattern}")
    for pattern in probe.must_not_match:
        if re.search(pattern, text, re.MULTILINE):
            return Result(tool, "FAIL", f"matched forbidden pattern: {pattern}")

    if isinstance(payload, str):
        # A text-answering tool: must_match / min_chars above are the contract,
        # since there are no rows or keys to inspect. Refuse to pass one that
        # asserts neither — an empty string would otherwise read as success.
        if not probe.must_match and not probe.min_chars:
            return Result(
                tool,
                "FAIL",
                "text payload with nothing asserted — set must_match or min_chars",
            )
        return Result(tool, "OK")

    if not isinstance(payload, (dict, list)):
        return Result(tool, "FAIL", f"payload is not a container: {type(payload).__name__}")

    missing = [k for k in probe.require_keys if _dig(payload, k) is None]
    if missing:
        return Result(tool, "FAIL", f"missing keys: {', '.join(missing)}")

    for path, minimum in probe.min_values.items():
        actual = _dig(payload, path)
        if not isinstance(actual, (int, float)):
            # The value comes from the payload, so it falls under redaction too.
            shown = "redacted" if redact_details else repr(actual)
            return Result(tool, "FAIL", f"{path} missing or not numeric: {shown}")
        if actual < minimum:
            # The bound is ours and stays; the observed value came from the
            # payload — a holding, a headcount — so it is redacted with
            # everything else the server said.
            if redact_details:
                return Result(tool, "FAIL", f"{path} below the required {minimum:g}")
            return Result(tool, "FAIL", f"{path}={actual:g} (want >= {minimum:g})")

    # A named rows_key must resolve to a list even when the probe accepts an
    # empty one: allow_empty waives the *count*, not the shape. Without this a
    # tool answering {"events": "broken"} passes every check a probe can make.
    if probe.rows_key is not None:
        target = _dig(payload, probe.rows_key)
        if not isinstance(target, list):
            kind = type(target).__name__ if target is not None else "missing"
            return Result(tool, "FAIL", f"{probe.rows_key} is {kind}, not a list")

    rows = count_rows(payload, probe.rows_key)
    if not probe.allow_empty:
        if rows is None:
            return Result(tool, "FAIL", "no list-shaped data in the payload", rows=rows)
        if rows < probe.min_rows:
            return Result(tool, "FAIL", f"{rows} rows (want >= {probe.min_rows})", rows=rows)

    if probe.fresh_within_days is not None and probe.date_field:
        newest = max_date(payload, probe.date_field)
        if newest is None:
            return Result(
                tool, "FAIL", f"no {probe.date_field} value to check freshness", rows=rows
            )
        floor = today - timedelta(days=probe.fresh_within_days)
        if newest < floor:
            # The newest date is payload content — a user's last login on one
            # server, a trade date on another — so redaction covers it. The
            # floor is computed here, not read from the payload, and stays.
            observed = "redacted" if redact_details else newest.isoformat()
            return Result(
                tool,
                "FAIL",
                f"stale: newest {probe.date_field}={observed} < {floor.isoformat()}",
                rows=rows,
            )
    # Last check, deliberately: a payload that is broken in its own right
    # should be reported as broken, not as a complaint about the spec. What is
    # left here is a probe that waived the row count and then asserted nothing
    # else — which would report every empty or malformed answer as OK. The
    # allow_empty docstring has always said so; only the text path enforced it.
    # Freshness counts: it is an assertion the engine actually ran a few lines
    # above, so a probe configured with it has not "asserted nothing" and must
    # not be told that it did. (It is a weak choice on its own — an empty
    # payload has no date to check and fails the freshness branch instead — but
    # a probe that returns fresh rows would otherwise be failed for asserting
    # something the check itself just verified.)
    if probe.allow_empty and not (
        probe.require_keys
        or probe.min_values
        or probe.must_match
        or probe.min_chars
        or probe.rows_key
        or (probe.fresh_within_days is not None and probe.date_field)
    ):
        return Result(
            tool,
            "FAIL",
            "allow_empty with nothing asserted — an empty answer would pass unread",
            rows=rows,
        )

    return Result(tool, "OK", rows=rows)


# ---------------------------------------------------------------------------
# Runner


async def run_probes(
    tool_names: list[str],
    probes: dict[str, Probe],
    call: Caller,
    reference: date,
    today: date,
    concurrency: int = 4,
    show_traceback: bool = False,
    redact_details: bool = False,
) -> list[Result]:
    """Run every registered tool through its probe and collect results.

    ``show_traceback`` prints the full exception chain for failures. Server
    frameworks typically re-raise tool errors as a flat message, so the
    original stack is the only way to see where a live failure came from.

    ``redact_details`` keeps server-authored text out of the report. Error
    messages routinely quote the argument that failed ("User 'alice' not
    found"), which is fine for a market-data server and unacceptable for one
    serving personal data — the report is meant to be safe to paste anywhere.
    """
    if concurrency < 1:
        # Semaphore(0) parks every probe forever, and the per-probe timeout
        # cannot save the run because it only starts once the slot is held —
        # a bounded smoke test that hangs is the one failure mode it must not
        # have. Negative values raise from the Semaphore itself, after the
        # caller has already paid for setup.
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")

    semaphore = asyncio.Semaphore(concurrency)

    async def one(name: str) -> Result:
        probe = probes.get(name)
        if probe is None:
            return Result(
                name,
                "NO_SPEC",
                "registered tool has no probe spec — add one (or an explicit skip)",
            )
        if probe.skip:
            return Result(name, "SKIP", probe.skip)
        async with semaphore:
            # Timed from here, not from the call to this function: queue wait is
            # an artefact of the runner and would make every reported duration a
            # function of how many probes ran before it. Discovery IS counted —
            # it is work this probe caused, and a factory that polls a job for a
            # minute should not be reported as an instant probe.
            started = time.monotonic()
            # Inside the semaphore: a factory makes its own server calls, so
            # running it outside would let every probe's discovery fire at once
            # and blow straight through the configured concurrency bound.
            if probe.args_factory is not None:
                try:
                    discovered = await asyncio.wait_for(
                        probe.args_factory(call), timeout=probe.timeout
                    )
                except SkipProbe as exc:
                    return Result(name, "SKIP", str(exc), time.monotonic() - started)
                except Exception as exc:  # noqa: BLE001 - a broken prerequisite is a finding
                    # Same traceback handling as the tool call below: on
                    # servers where nearly every probe discovers its arguments,
                    # a failure here is the most common one there is, and
                    # --traceback promising a stack everywhere except the
                    # common case is worse than not promising one.
                    if show_traceback:
                        print(f"--- traceback: {name} (args_factory) ---", file=sys.stderr)
                        traceback.print_exception(exc, file=sys.stderr)
                    detail = f"args_factory failed: {_describe(exc, redact_details)}"
                    return Result(name, "FAIL", detail[:160], time.monotonic() - started)
                # Merged, not replaced: a probe that sets both means "these are
                # the fixed arguments, and this one is discovered". Dropping
                # ``args`` here would silently lose a bound the author wrote.
                args = {**probe.args, **discovered}
            else:
                args = probe.args
            args = resolve_tokens(args, reference, today)
            try:
                payload = await asyncio.wait_for(call(name, args), timeout=probe.timeout)
            # Both spellings: on Python 3.10 asyncio.TimeoutError is
            # concurrent.futures.TimeoutError, NOT the builtin, so catching the
            # builtin alone silently reports timeouts as generic failures there.
            except (asyncio.TimeoutError, TimeoutError):
                return Result(
                    name, "FAIL", f"timed out after {probe.timeout:g}s", time.monotonic() - started
                )
            except Exception as exc:  # noqa: BLE001 - any failure is a smoke-test finding
                if show_traceback:
                    print(f"--- traceback: {name} ---", file=sys.stderr)
                    traceback.print_exception(exc, file=sys.stderr)
                return Result(
                    name,
                    "FAIL",
                    _describe(exc, redact_details)[:160],
                    time.monotonic() - started,
                )
        result = evaluate(name, probe, payload, today, redact_details=redact_details)
        result.elapsed = time.monotonic() - started
        return result

    results = await asyncio.gather(*(one(n) for n in tool_names))
    return sorted(results, key=lambda r: (r.status not in FAILING, r.tool))


# ---------------------------------------------------------------------------
# Reporting


def render_markdown(results: list[Result], reference: date, mode: str) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    order = ["OK", "RESTRICTED", "SKIP", "FAIL", "NO_SPEC"]
    summary = " / ".join(f"{s}: {counts[s]}" for s in order if s in counts)

    lines = [
        "# Tool smoke test",
        "",
        f"mode: {mode} | reference business day: {reference.isoformat()} | "
        f"run at {datetime.now(JST):%Y-%m-%d %H:%M} JST",
        "",
        f"**{summary}**",
        "",
    ]
    problems = [r for r in results if r.status in FAILING]
    if problems:
        lines += ["## Problems", "", "| tool | status | detail |", "|---|---|---|"]
        lines += [f"| `{r.tool}` | {r.status} | {r.detail} |" for r in problems]
        lines.append("")
    lines += [
        "## All tools",
        "",
        "| tool | status | rows | sec | detail |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: x.tool):
        rows = "-" if r.rows is None else str(r.rows)
        lines.append(f"| `{r.tool}` | {r.status} | {rows} | {r.elapsed:.1f} | {r.detail} |")
    return "\n".join(lines) + "\n"


def render_json(results: list[Result], reference: date, mode: str) -> str:
    return json.dumps(
        {
            "mode": mode,
            "reference_business_day": reference.isoformat(),
            "results": [
                {
                    "tool": r.tool,
                    "status": r.status,
                    "detail": r.detail,
                    "rows": r.rows,
                    "elapsed_sec": round(r.elapsed, 3),
                }
                for r in sorted(results, key=lambda x: x.tool)
            ],
        },
        ensure_ascii=False,
        indent=2,
    )

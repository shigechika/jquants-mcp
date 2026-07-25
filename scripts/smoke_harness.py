"""Generic live smoke-test engine for a FastMCP server.

Enumerates every tool the server actually registers and exercises each one
against real data, so a tool that exists but does not work is caught the day
it breaks. Unit tests cannot cover this class of failure: the earnings
calendar tools once returned well-formed *empty* results for every query
because the cache lacked the requested dates, and the suite stayed green.

The engine holds no server-specific knowledge. A companion "probes" module
supplies the per-tool specs (see ``smoke_probes.py`` for this repo's), so the
same engine can smoke-test any other FastMCP server by swapping that module.

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
    """Raised by an ``args_factory`` when the probe cannot be prepared."""


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
    #: tool's output (e.g. a download key listed by a companion tool). Takes
    #: precedence over ``args``; raising ``SkipProbe`` reports a SKIP.
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


def evaluate(tool: str, probe: Probe, payload: Any, today: date) -> Result:
    """Decide whether one payload proves the tool works."""
    if isinstance(payload, dict) and payload.get("error"):
        kind = payload.get("error_type", "?")
        message = str(payload.get("message", ""))[:120]
        if probe.allow_plan_restriction and kind == "PlanRestrictionError":
            return Result(tool, "RESTRICTED", "plan does not cover this endpoint")
        return Result(tool, "FAIL", f"{kind}: {message}")

    if not isinstance(payload, (dict, list)):
        return Result(tool, "FAIL", f"payload is not a container: {type(payload).__name__}")

    missing = [k for k in probe.require_keys if _dig(payload, k) is None]
    if missing:
        return Result(tool, "FAIL", f"missing keys: {', '.join(missing)}")

    for path, minimum in probe.min_values.items():
        actual = _dig(payload, path)
        if not isinstance(actual, (int, float)):
            return Result(tool, "FAIL", f"{path} missing or not numeric: {actual!r}")
        if actual < minimum:
            return Result(tool, "FAIL", f"{path}={actual:g} (want >= {minimum:g})")

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
            return Result(
                tool,
                "FAIL",
                f"stale: newest {probe.date_field}={newest.isoformat()} < {floor.isoformat()}",
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
) -> list[Result]:
    """Run every registered tool through its probe and collect results.

    ``show_traceback`` prints the full exception chain for failures. Server
    frameworks typically re-raise tool errors as a flat message, so the
    original stack is the only way to see where a live failure came from.
    """
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
        # Timed from here, but reset once the semaphore is held — queue wait is
        # an artefact of the runner and would make every reported duration a
        # function of how many probes ran before it.
        started = time.monotonic()
        if probe.args_factory is not None:
            try:
                args = await asyncio.wait_for(probe.args_factory(call), timeout=probe.timeout)
            except SkipProbe as exc:
                return Result(name, "SKIP", str(exc), time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 - a broken prerequisite is a finding
                detail = f"args_factory failed: {type(exc).__name__}: {exc}"
                return Result(name, "FAIL", detail[:160], time.monotonic() - started)
        else:
            args = probe.args
        args = resolve_tokens(args, reference, today)
        async with semaphore:
            started = time.monotonic()
            try:
                payload = await asyncio.wait_for(call(name, args), timeout=probe.timeout)
            except TimeoutError:
                return Result(
                    name, "FAIL", f"timed out after {probe.timeout:g}s", time.monotonic() - started
                )
            except Exception as exc:  # noqa: BLE001 - any failure is a smoke-test finding
                if show_traceback:
                    print(f"--- traceback: {name} ---", file=sys.stderr)
                    traceback.print_exception(exc, file=sys.stderr)
                detail = f"{type(exc).__name__}: {exc}"
                return Result(name, "FAIL", detail[:160], time.monotonic() - started)
        result = evaluate(name, probe, payload, today)
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

"""Tests for the smoke-test engine (scripts/smoke_harness.py).

The engine decides whether a live answer proves a tool works, so its own
verdicts need to be pinned: a harness that silently passes everything would
recreate exactly the blind spot it exists to remove.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import smoke_harness as sh  # noqa: E402 - needs the sys.path line above

TODAY = date(2026, 7, 25)
REFERENCE = date(2026, 7, 24)


class TestDateTokens:
    def test_reference_and_offsets(self):
        args = {"date": "{t}", "date_from": "{t-30}", "as_of": "{today}", "end": "{today+1}"}
        out = sh.resolve_tokens(args, REFERENCE, TODAY)
        assert out == {
            "date": "2026-07-24",
            "date_from": "2026-06-24",
            "as_of": "2026-07-25",
            "end": "2026-07-26",
        }

    def test_leaves_other_values_alone(self):
        args = {"code": "72030", "n": 10, "codes": ["72030", "{t}"], "flag": True}
        out = sh.resolve_tokens(args, REFERENCE, TODAY)
        assert out == {"code": "72030", "n": 10, "codes": ["72030", "2026-07-24"], "flag": True}

    def test_previous_weekday_skips_the_weekend(self):
        # Monday's previous weekday is the Friday before, not Sunday.
        assert sh.previous_weekday(date(2026, 7, 27)) == date(2026, 7, 24)
        assert sh.previous_weekday(date(2026, 7, 24)) == date(2026, 7, 23)


class TestRowCounting:
    def test_finds_the_longest_list_when_no_key_given(self):
        payload = {"count": 2, "data": [{"a": 1}, {"a": 2}], "meta": ["x"]}
        assert sh.count_rows(payload, None) == 2

    def test_explicit_key_wins(self):
        payload = {"data": [1, 2, 3], "extras": [1, 2, 3, 4, 5]}
        assert sh.count_rows(payload, "extras") == 5

    def test_searches_one_level_deep(self):
        assert sh.count_rows({"result": {"rows": [1, 2, 3]}}, None) == 3

    def test_none_when_no_list_present(self):
        assert sh.count_rows({"advances": 0, "declines": 1}, None) is None


class TestMaxDate:
    def test_picks_the_newest_value_anywhere_in_the_payload(self):
        payload = {"days": [{"Date": "2026-07-20"}, {"Date": "2026-07-27T00:00:00"}]}
        assert sh.max_date(payload, "Date") == date(2026, 7, 27)

    def test_ignores_unparseable_values(self):
        assert sh.max_date({"Date": "not-a-date"}, "Date") is None


class TestEvaluate:
    def test_rows_present_passes(self):
        probe = sh.Probe()
        result = sh.evaluate("t", probe, {"data": [{"x": 1}]}, TODAY)
        assert result.status == "OK"
        assert result.rows == 1

    def test_empty_fails_unless_allowed(self):
        probe = sh.Probe()
        assert sh.evaluate("t", probe, {"data": []}, TODAY).status == "FAIL"
        assert sh.evaluate("t", sh.Probe(allow_empty=True), {"data": []}, TODAY).status == "OK"

    def test_allow_empty_still_enforces_required_keys(self):
        """Waiving the row count must not waive the shape."""
        probe = sh.Probe(allow_empty=True, require_keys=("count", "new_high"))
        assert sh.evaluate("t", probe, {}, TODAY).status == "FAIL"
        good = {"count": 0, "new_high": [], "new_low": []}
        assert sh.evaluate("t", probe, good, TODAY).status == "OK"

    def test_non_container_payload_fails_even_when_empty_is_allowed(self):
        probe = sh.Probe(allow_empty=True)
        for payload in (None, 42):
            result = sh.evaluate("t", probe, payload, TODAY)
            assert result.status == "FAIL", f"{payload!r} should not pass"
            assert "not a container" in result.detail
        # A string takes the text path: still a failure, different reason.
        assert sh.evaluate("t", probe, "plain text", TODAY).status == "FAIL"

    def test_min_rows_guards_a_thin_universe(self):
        """A cross-sectional tool returning one row is broken, not quiet."""
        probe = sh.Probe(min_rows=100)
        result = sh.evaluate("t", probe, {"data": [{"x": 1}]}, TODAY)
        assert result.status == "FAIL"
        assert "want >= 100" in result.detail

    def test_missing_required_key_fails(self):
        probe = sh.Probe(require_keys=("plan",), allow_empty=True)
        assert sh.evaluate("t", probe, {"other": 1}, TODAY).status == "FAIL"

    def test_min_values_catch_a_summary_over_an_empty_universe(self):
        probe = sh.Probe(allow_empty=True, min_values={"total": 100})
        ok = sh.evaluate("t", probe, {"total": 3800}, TODAY)
        thin = sh.evaluate("t", probe, {"total": 1}, TODAY)
        assert ok.status == "OK"
        assert thin.status == "FAIL"
        assert "total=1" in thin.detail

    def test_text_payload_needs_an_assertion(self):
        """Text-answering tools are common outside this repo; silence is not OK."""
        result = sh.evaluate("t", sh.Probe(), "some output", TODAY)
        assert result.status == "FAIL"
        assert "nothing asserted" in result.detail

    def test_text_payload_passes_when_pattern_matches(self):
        probe = sh.Probe(must_match=(r"^Users: \d+",), min_chars=5)
        assert sh.evaluate("t", probe, "Users: 4213\nActive: 88", TODAY).status == "OK"

    def test_text_payload_fails_on_missing_pattern(self):
        probe = sh.Probe(must_match=(r"^Users: \d+",))
        result = sh.evaluate("t", probe, "no data available", TODAY)
        assert result.status == "FAIL"
        assert "missing expected pattern" in result.detail

    def test_forbidden_pattern_catches_a_polite_no_data_answer(self):
        probe = sh.Probe(min_chars=1, must_not_match=(r"(?i)no (data|results)",))
        result = sh.evaluate("t", probe, "No data for the requested window.", TODAY)
        assert result.status == "FAIL"
        assert "forbidden pattern" in result.detail

    def test_min_chars_catches_a_truncated_answer(self):
        probe = sh.Probe(min_chars=50)
        result = sh.evaluate("t", probe, "Report\n", TODAY)
        assert result.status == "FAIL"
        assert "chars (want >=" in result.detail

    def test_text_checks_also_apply_to_structured_payloads(self):
        """The rendered JSON is searched, so a dict tool can assert content too."""
        probe = sh.Probe(must_match=(r"\"plan\": \"premium\"",), allow_empty=True)
        assert sh.evaluate("t", probe, {"plan": "premium"}, TODAY).status == "OK"

    def test_error_payload_fails(self):
        payload = {"error": True, "error_type": "APIError", "message": "boom"}
        result = sh.evaluate("t", sh.Probe(), payload, TODAY)
        assert result.status == "FAIL"
        assert "APIError" in result.detail

    def test_plan_restriction_passes_only_when_allowed(self):
        payload = {"error": True, "error_type": "PlanRestrictionError", "message": "nope"}
        assert sh.evaluate("t", sh.Probe(), payload, TODAY).status == "FAIL"
        probe = sh.Probe(allow_plan_restriction=True)
        assert sh.evaluate("t", probe, payload, TODAY).status == "RESTRICTED"

    def test_stale_data_fails_even_though_rows_exist(self):
        """The regression this whole harness exists for: plausible but old."""
        probe = sh.Probe(date_field="Date", fresh_within_days=0)
        payload = {"data": [{"Date": "2026-05-20"}, {"Date": "2026-07-24"}]}
        result = sh.evaluate("t", probe, payload, TODAY)
        assert result.status == "FAIL"
        assert "stale" in result.detail

    def test_fresh_data_passes(self):
        probe = sh.Probe(date_field="Date", fresh_within_days=0)
        payload = {"data": [{"Date": "2026-07-27"}]}
        assert sh.evaluate("t", probe, payload, TODAY).status == "OK"


class TestRunProbes:
    async def _run(self, names, probes, call, **kwargs):
        return await sh.run_probes(names, probes, call, REFERENCE, TODAY, **kwargs)

    async def test_registered_tool_without_a_spec_fails(self):
        async def call(name, args):
            return {"data": [1]}

        results = await self._run(["brand_new_tool"], {}, call)
        assert results[0].status == "NO_SPEC"

    async def test_skip_is_reported_not_run(self):
        called = []

        async def call(name, args):
            called.append(name)
            return {}

        probes = {"cache_clear": sh.Probe(skip="destructive")}
        results = await self._run(["cache_clear"], probes, call)
        assert results[0].status == "SKIP"
        assert called == []

    async def test_tokens_are_resolved_before_the_call(self):
        seen = {}

        async def call(name, args):
            seen.update(args)
            return {"data": [1]}

        probes = {"t": sh.Probe(args={"date": "{t}"})}
        await self._run(["t"], probes, call)
        assert seen == {"date": "2026-07-24"}

    async def test_args_factory_supplies_a_chained_argument(self):
        async def factory(call):
            listing = await call("list_tool", {})
            return {"key": listing["data"][0]["Key"]}

        async def call(name, args):
            if name == "list_tool":
                return {"data": [{"Key": "file-1"}]}
            assert args == {"key": "file-1"}
            return {"data": [1]}

        probes = {"download_tool": sh.Probe(args_factory=factory)}
        results = await self._run(["download_tool"], probes, call)
        assert results[0].status == "OK"

    async def test_args_factory_result_merges_over_static_args(self):
        """A probe that sets both means "fixed bounds, discovered identifier".

        Replacing rather than merging would silently drop a limit the author
        wrote down — the kind of loss that only shows up as an unexpectedly
        expensive call against a live server.
        """
        seen = {}

        async def factory(call):
            return {"host": "discovered"}

        async def call(name, args):
            seen.update(args)
            return {"data": [1]}

        probes = {"t": sh.Probe(args={"limit": 5, "host": "placeholder"}, args_factory=factory)}
        await self._run(["t"], probes, call)
        assert seen == {"limit": 5, "host": "discovered"}

    async def test_args_factory_can_skip(self):
        async def factory(call):
            raise sh.SkipProbe("no key available")

        probes = {"download_tool": sh.Probe(args_factory=factory)}
        results = await self._run(["download_tool"], probes, call=_unused_call)
        assert results[0].status == "SKIP"
        assert results[0].detail == "no key available"

    async def test_exception_becomes_a_failure(self):
        async def call(name, args):
            raise RuntimeError("cannot start a transaction within a transaction")

        probes = {"t": sh.Probe()}
        results = await self._run(["t"], probes, call)
        assert results[0].status == "FAIL"
        assert "RuntimeError" in results[0].detail

    async def test_timeout_becomes_a_failure(self):
        import asyncio

        async def call(name, args):
            await asyncio.sleep(5)

        probes = {"t": sh.Probe(timeout=0.05)}
        results = await self._run(["t"], probes, call)
        assert results[0].status == "FAIL"
        assert "timed out" in results[0].detail

    async def test_redact_details_hides_server_text(self):
        """Error text quotes the argument that failed; some servers must not echo it."""

        async def call(name, args):
            raise RuntimeError("User 'alice@example.org' not found")

        probes = {"t": sh.Probe()}
        leaky = await self._run(["t"], probes, call)
        assert "alice@example.org" in leaky[0].detail

        redacted = await self._run(["t"], probes, call, redact_details=True)
        assert redacted[0].status == "FAIL"
        assert "alice" not in redacted[0].detail
        assert "RuntimeError" in redacted[0].detail

    def test_redact_details_hides_payload_values_in_check_failures(self):
        """Redaction covers every detail derived from the payload, not just errors.

        A freshness or min_values failure quotes what it saw, and what it saw
        is server data — on a server holding personal data that is exactly the
        thing the report promises not to print.
        """
        stale = {"data": [{"Date": "2020-01-01"}]}
        probe = sh.Probe(date_field="Date", fresh_within_days=0)
        assert "2020-01-01" in sh.evaluate("t", probe, stale, TODAY).detail
        redacted = sh.evaluate("t", probe, stale, TODAY, redact_details=True)
        assert redacted.status == "FAIL"
        assert "2020" not in redacted.detail
        assert "stale" in redacted.detail

        thin = {"total": "not-a-number"}
        probe = sh.Probe(allow_empty=True, min_values={"total": 100})
        assert "not-a-number" in sh.evaluate("t", probe, thin, TODAY).detail
        redacted = sh.evaluate("t", probe, thin, TODAY, redact_details=True)
        assert "not-a-number" not in redacted.detail

    async def test_redact_details_hides_error_payload_message(self):
        async def call(name, args):
            return {"error": True, "error_type": "APIError", "message": "user bob is unknown"}

        results = await self._run(["t"], {"t": sh.Probe()}, call, redact_details=True)
        assert "bob" not in results[0].detail
        assert "APIError" in results[0].detail

    async def test_args_factory_respects_the_concurrency_bound(self):
        """Discovery calls hit the server too, so they must queue like the rest."""
        import asyncio

        live = 0
        peak = 0

        async def factory(call):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return {}

        async def call(name, args):
            return {"data": [1]}

        probes = {f"t{i}": sh.Probe(args_factory=factory) for i in range(8)}
        await self._run(list(probes), probes, call, concurrency=2)
        assert peak <= 2, f"{peak} factories ran at once with concurrency=2"

    async def test_a_named_rows_key_must_be_a_list_even_when_empty_is_allowed(self):
        """allow_empty waives the count, not the shape."""
        probe = sh.Probe(rows_key="events", allow_empty=True, require_keys=("count",))
        broken = sh.evaluate("t", probe, {"count": 0, "events": "broken"}, TODAY)
        assert broken.status == "FAIL"
        assert "not a list" in broken.detail

        assert sh.evaluate("t", probe, {"count": 0, "events": []}, TODAY).status == "OK"

    async def test_min_values_redacts_the_observed_number(self):
        """The bound is ours; the number the server returned is not."""
        probe = sh.Probe(allow_empty=True, require_keys=("total",), min_values={"total": 100})
        payload = {"total": 7}
        assert "7" in sh.evaluate("t", probe, payload, TODAY).detail
        redacted = sh.evaluate("t", probe, payload, TODAY, redact_details=True)
        assert redacted.status == "FAIL"
        # Exact text, not a substring: a detail that reads "totalbelow it" says
        # the same thing to a grep and the wrong thing to a person.
        assert redacted.detail == "total below the required 100"

    async def test_show_traceback_covers_a_failing_args_factory(self, capsys):
        """The commonest failure on discovery-heavy servers must print a stack too."""

        async def factory(call):
            raise RuntimeError("discovery blew up")

        async def call(name, args):
            return {"data": [1]}

        probes = {"t": sh.Probe(args_factory=factory)}
        results = await self._run(["t"], probes, call, show_traceback=True)

        assert results[0].status == "FAIL"
        assert "discovery blew up" in capsys.readouterr().err

    async def test_a_concurrency_below_one_is_refused(self):
        """Semaphore(0) would park every probe with no timeout to rescue it."""
        import pytest

        async def call(name, args):
            return {"data": [1]}

        with pytest.raises(ValueError, match="at least 1"):
            await self._run(["t"], {"t": sh.Probe()}, call, concurrency=0)

    async def test_failures_sort_first(self):
        async def call(name, args):
            return {"data": []} if name == "bad" else {"data": [1]}

        probes = {"bad": sh.Probe(), "good": sh.Probe()}
        results = await self._run(["good", "bad"], probes, call)
        assert [r.tool for r in results] == ["bad", "good"]


async def _unused_call(name, args):  # pragma: no cover - factory skips first
    raise AssertionError("the tool must not be called when the factory skips")


class TestRendering:
    @pytest.fixture
    def results(self):
        return [
            sh.Result("a", "OK", rows=3, elapsed=0.5),
            sh.Result("b", "FAIL", "stale", rows=1, elapsed=1.0),
            sh.Result("c", "RESTRICTED", "plan"),
        ]

    def test_markdown_lists_problems_separately(self, results):
        text = sh.render_markdown(results, REFERENCE, "in-process")
        assert "## Problems" in text
        assert "`b`" in text
        assert "OK: 1" in text and "FAIL: 1" in text

    def test_markdown_without_failures_has_no_problem_section(self):
        text = sh.render_markdown([sh.Result("a", "OK")], REFERENCE, "in-process")
        assert "## Problems" not in text

    def test_json_is_machine_readable(self, results):
        import json

        payload = json.loads(sh.render_json(results, REFERENCE, "in-process"))
        assert payload["reference_business_day"] == "2026-07-24"
        assert {r["tool"] for r in payload["results"]} == {"a", "b", "c"}

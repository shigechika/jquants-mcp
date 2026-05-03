"""Tests for screener tools."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache import screener_compute
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings


@pytest.fixture()
def mock_env(tmp_path):
    """Replace server globals with a fresh in-process cache."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


async def _call(tool_name: str, **kwargs) -> dict:
    """Invoke an MCP tool and decode the JSON result."""
    result = await server_module.mcp.call_tool(tool_name, kwargs)
    return json.loads(result.content[0].text)


def _bar(
    code: str,
    date: str,
    *,
    o: float = 100.0,
    h: float = 110.0,
    low: float = 90.0,
    c: float = 105.0,
    vo: float = 1_000.0,
    va: float | None = None,
    ul: int = 0,
    ll: int = 0,
    adj_factor: float = 1.0,
    adj_o: float | None = None,
    adj_h: float | None = None,
    adj_l: float | None = None,
    adj_c: float | None = None,
    adj_vo: float | None = None,
) -> dict:
    """Build a single cached bar row with sensible defaults."""
    return {
        "Code": code,
        "Date": date,
        "O": o,
        "H": h,
        "L": low,
        "C": c,
        "UL": ul,
        "LL": ll,
        "Vo": vo,
        "Va": va if va is not None else c * vo,
        "AdjFactor": adj_factor,
        "AdjO": adj_o if adj_o is not None else o,
        "AdjH": adj_h if adj_h is not None else h,
        "AdjL": adj_l if adj_l is not None else low,
        "AdjC": adj_c if adj_c is not None else c,
        "AdjVo": adj_vo if adj_vo is not None else vo,
    }


def _seed(cache: CacheStore, rows: list[dict]) -> None:
    cache.put_rows(
        "equities_bars_daily",
        rows,
        key_columns=["Code", "Date"],
        adj_factor_key="AdjFactor",
    )


class TestDetectPriceLimit:
    async def test_returns_triggered_codes_cross_section(self, mock_env):
        rows = [
            _bar("10000", "2026-04-01", h=200, c=200, ul=1),  # stop-high close
            _bar("20000", "2026-04-01", low=80, c=80, ll=1),  # stop-low close
            _bar("30000", "2026-04-01"),  # normal day, filtered out
            _bar("40000", "2026-04-01", h=180, c=150, ul=1),  # touched only
        ]
        _seed(mock_env["cache"], rows)

        result = await _call("detect_price_limit", date="2026-04-01", detail=True)

        assert result["count"] == 3
        by_code = {row["Code"]: row for row in result["data"]}
        assert by_code["10000"]["limit_high_close"] is True
        assert by_code["10000"]["limit_high_touched"] is True
        assert by_code["20000"]["limit_low_close"] is True
        assert by_code["40000"]["limit_high_touched"] is True
        assert by_code["40000"]["limit_high_close"] is False

    async def test_code_filter_returns_row_even_if_not_triggered(self, mock_env):
        _seed(mock_env["cache"], [_bar("27800", "2026-04-01")])
        result = await _call("detect_price_limit", date="2026-04-01", code="27800", detail=True)
        assert result["count"] == 1
        assert result["data"][0]["limit_high_touched"] is False
        assert result["data"][0]["limit_low_touched"] is False

    async def test_validation_error_on_bad_date(self, mock_env):
        result = await _call("detect_price_limit", date="2026/04/01")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"


class TestCompareCloseVsVwap:
    async def test_vwap_and_pct(self, mock_env):
        _seed(
            mock_env["cache"],
            [
                _bar("27800", "2026-04-01", c=110, vo=1000, va=100_000),
                _bar("27800", "2026-04-02", c=120, vo=2000, va=220_000),
            ],
        )
        result = await _call(
            "compare_close_vs_vwap",
            code="27800",
            date_from="2026-04-01",
            date_to="2026-04-02",
        )
        assert result["count"] == 2
        day1 = result["data"][0]
        assert day1["vwap"] == pytest.approx(100.0)
        assert day1["close_above_vwap"] is True
        assert day1["vwap_diff_pct"] == pytest.approx(10.0)

    async def test_zero_volume_returns_none_vwap(self, mock_env):
        _seed(
            mock_env["cache"],
            [_bar("27800", "2026-04-01", c=100, vo=0, va=0)],
        )
        result = await _call("compare_close_vs_vwap", code="27800", date="2026-04-01")
        row = result["data"][0]
        assert row["vwap"] is None
        assert row["vwap_diff_pct"] is None
        assert row["close_above_vwap"] is None

    async def test_requires_date_or_range(self, mock_env):
        result = await _call("compare_close_vs_vwap", code="27800")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"


class TestDetect52wHighLow:
    async def test_close_above_prior_high(self, mock_env):
        # Prior window high climbs to 119; today closes at 200 (new high).
        start = datetime(2026, 1, 5)
        rows = []
        for i in range(20):
            d = (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        final_date = (start + timedelta(days=20 * 7)).strftime("%Y-%m-%d")
        rows.append(_bar("27800", final_date, h=200, low=180, c=200))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_52w_high_low",
            date=final_date,
            code="27800",
            window_sessions=30,
            min_prior_sessions=1,
            detail=True,
        )
        assert result["mode"] == "52w"
        assert result["count"] == 1
        r = result["data"][0]
        assert r["new_high"] is True
        assert r["new_high_close"] is True
        assert r["new_low"] is False
        assert r["new_low_close"] is False
        assert r["prior_high"] == pytest.approx(119.0)

    async def test_intraday_break_then_close_below(self, mock_env):
        # H pierces prior 119, but C=110 sits below it: intraday yes,
        # close no.
        start = datetime(2026, 1, 5)
        rows = []
        for i in range(20):
            d = (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        final_date = (start + timedelta(days=20 * 7)).strftime("%Y-%m-%d")
        rows.append(_bar("27800", final_date, h=130, low=105, c=110))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_52w_high_low",
            date=final_date,
            code="27800",
            window_sessions=30,
            min_prior_sessions=1,
            detail=True,
        )
        r = result["data"][0]
        assert r["new_high"] is True
        assert r["new_high_close"] is False
        assert r["new_low"] is False

    async def test_tie_is_a_new_high(self, mock_env):
        # Today's H equals (does not strictly exceed) the prior max.
        # Convention: tie still flags as new high (>=).
        start = datetime(2026, 1, 5)
        rows = []
        for i in range(15):
            d = (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100, low=80, c=95))
        final_date = (start + timedelta(days=15 * 7)).strftime("%Y-%m-%d")
        rows.append(_bar("27800", final_date, h=100, low=95, c=100))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_52w_high_low",
            date=final_date,
            code="27800",
            window_sessions=20,
            min_prior_sessions=1,
            detail=True,
        )
        r = result["data"][0]
        assert r["new_high"] is True  # H == prior max → tie
        assert r["new_high_close"] is True  # C == prior max → tie

    async def test_flat_market_ties_high_intraday_only(self, mock_env):
        # Every prior bar uses the _bar() defaults (H=110, C=105). Today's
        # H=110 ties the prior max → new_high=True (>= convention). The
        # close at 105 is below the prior max → new_high_close=False.
        # Lows are also flat at 90, so today's L=90 ties → new_low=True.
        start = datetime(2026, 1, 5)
        rows = [
            _bar("27800", (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")) for i in range(20)
        ]
        _seed(mock_env["cache"], rows)
        final_date = (start + timedelta(days=19 * 7)).strftime("%Y-%m-%d")
        result = await _call(
            "detect_52w_high_low",
            date=final_date,
            code="27800",
            window_sessions=15,
            min_prior_sessions=1,
            detail=True,
        )
        r = result["data"][0]
        assert r["new_high"] is True  # H ties prior max
        assert r["new_high_close"] is False  # C below prior max
        assert r["new_low"] is True  # L ties prior min
        assert r["new_low_close"] is False  # C above prior min

    async def test_inside_prior_range_returns_all_false(self, mock_env):
        # Today's bar is strictly inside the prior range: nothing flags.
        start = datetime(2026, 1, 5)
        rows = []
        for i in range(15):
            d = (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")
            # prior: H climbs 200..214, L 100..114, C 150..164
            rows.append(_bar("27800", d, h=200 + i, low=100 + i, c=150 + i))
        # Today: well inside the prior range — H=180, L=120, C=160.
        final_date = (start + timedelta(days=15 * 7)).strftime("%Y-%m-%d")
        rows.append(_bar("27800", final_date, h=180, low=120, c=160))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_52w_high_low",
            date=final_date,
            code="27800",
            window_sessions=20,
            min_prior_sessions=1,
            detail=True,
        )
        r = result["data"][0]
        assert r["new_high"] is False
        assert r["new_high_close"] is False
        assert r["new_low"] is False
        assert r["new_low_close"] is False

    async def test_cross_sectional_filters_to_hits(self, mock_env):
        date_today = "2026-04-10"
        rows_a = []
        rows_b = []
        for i in range(30):
            d = (datetime(2026, 2, 1) + timedelta(days=i * 2)).strftime("%Y-%m-%d")
            rows_a.append(_bar("10000", d, h=50 + i, low=40, c=45 + i))
            rows_b.append(_bar("20000", d, h=100, low=80, c=90))
        rows_a.append(_bar("10000", date_today, h=200, low=180, c=200))
        rows_b.append(_bar("20000", date_today, h=100, low=80, c=90))
        _seed(mock_env["cache"], rows_a + rows_b)

        result = await _call(
            "detect_52w_high_low",
            date=date_today,
            window_sessions=60,
            min_prior_sessions=1,
            detail=True,
        )
        codes = {row["Code"] for row in result["data"]}
        assert "10000" in codes
        # Code 20000 ties on H=100 (matches prior 100) so it also flags.
        # Real point of the test: Code 10000 is present.

    async def test_min_prior_sessions_drops_recent_ipo(self, mock_env):
        # IPO-like pattern: only 5 sessions of history. Default
        # min_prior_sessions=60 should suppress this code in
        # cross-sectional mode.
        date_today = "2026-04-10"
        rows = []
        for i in range(5):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("99990", d, h=100 + i, low=90, c=95 + i))
        rows.append(_bar("99990", date_today, h=200, low=150, c=200))
        _seed(mock_env["cache"], rows)

        result = await _call("detect_52w_high_low", date=date_today, detail=True)  # default min=60
        codes = {row["Code"] for row in result["data"]}
        assert "99990" not in codes

        # But explicit code= bypasses the filter (caller asked specifically).
        result_explicit = await _call(
            "detect_52w_high_low", date=date_today, code="99990", min_prior_sessions=1
        )
        assert result_explicit["count"] == 1


class TestDetectYtdHighLow:
    async def test_ytd_high_signal(self, mock_env):
        # Year 2026 starts 1/5; data through April. Today closes at a
        # YTD high.
        rows = []
        for i in range(20):
            d = (datetime(2026, 1, 5) + timedelta(days=i * 5)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        final_date = "2026-04-25"
        rows.append(_bar("27800", final_date, h=200, low=180, c=200))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_ytd_high_low",
            date=final_date,
            code="27800",
            min_prior_sessions=1,
            detail=True,
        )
        assert result["mode"] == "ytd"
        r = result["data"][0]
        assert r["new_high"] is True
        assert r["new_high_close"] is True

    async def test_ytd_resets_across_year_boundary(self, mock_env):
        # 2025 had a high of 500. 2026 starts low and today (2026-02-15)
        # closes at 200 — well below 2025's 500, but a new YTD high for 2026.
        rows = []
        # 2025 history (high 500) — must be ignored
        for i in range(10):
            d = (datetime(2025, 6, 1) + timedelta(days=i * 5)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=500, low=400, c=450))
        # 2026 January
        for i in range(10):
            d = (datetime(2026, 1, 5) + timedelta(days=i * 3)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=120 + i, low=100, c=110 + i))
        final_date = "2026-02-15"
        rows.append(_bar("27800", final_date, h=200, low=180, c=200))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_ytd_high_low",
            date=final_date,
            code="27800",
            min_prior_sessions=1,
            detail=True,
        )
        r = result["data"][0]
        assert r["new_high"] is True
        # Prior YTD high is at most 129 (last 2026 row before today),
        # certainly far below the 2025 leftover of 500.
        assert r["prior_high"] < 200

    async def test_first_trading_day_of_year_skipped(self, mock_env):
        # No prior YTD sessions → row dropped (per docstring).
        _seed(mock_env["cache"], [_bar("27800", "2026-01-05", h=100, c=95)])
        result = await _call(
            "detect_ytd_high_low", date="2026-01-05", code="27800", min_prior_sessions=1
        )
        assert result["count"] == 0

    async def test_validation_error(self, mock_env):
        result = await _call("detect_ytd_high_low", date="2026/01/01")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"


class TestDetectVolumeSurge:
    async def test_surge_detected(self, mock_env):
        # 20 baseline sessions at Vo=1000, then Vo=5000 on the probe date.
        rows = []
        for i in range(20):
            d = (datetime(2026, 3, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, vo=1000.0))
        probe = "2026-03-21"
        rows.append(_bar("27800", probe, vo=5000.0))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_volume_surge", date=probe, multiplier=2.0, baseline_days=20, detail=True
        )
        assert result["count"] == 1
        assert result["data"][0]["Code"] == "27800"
        assert result["data"][0]["surge_ratio"] == pytest.approx(5.0)

    async def test_below_multiplier_filtered(self, mock_env):
        rows = []
        for i in range(20):
            d = (datetime(2026, 3, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, vo=1000.0))
        probe = "2026-03-21"
        rows.append(_bar("27800", probe, vo=1500.0))  # 1.5× only
        _seed(mock_env["cache"], rows)

        result = await _call("detect_volume_surge", date=probe, multiplier=2.0, baseline_days=20)
        assert result["count"] == 0

    async def test_zero_baseline_volume_skipped(self, mock_env):
        # Stocks whose entire baseline window has Vo=0 (suspended/illiquid)
        # are skipped to avoid divide-by-zero on the surge ratio.
        rows = []
        for i in range(5):
            d = (datetime(2026, 3, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, vo=0))
        probe = "2026-03-06"
        rows.append(_bar("27800", probe, vo=1000.0))
        _seed(mock_env["cache"], rows)

        result = await _call("detect_volume_surge", date=probe, baseline_days=5)
        assert result["count"] == 0

    async def test_invalid_multiplier(self, mock_env):
        result = await _call("detect_volume_surge", date="2026-04-01", multiplier=0)
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"

    async def test_invalid_baseline_days(self, mock_env):
        # baseline_days < 2 fails the validation guard.
        result = await _call("detect_volume_surge", date="2026-04-01", baseline_days=1)
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"


# ----------------------------------------------------------------
# Screener result cache (Issue #142)
# ----------------------------------------------------------------


def _put_cache_payload(
    cache: CacheStore,
    *,
    tool_name: str,
    params_hash: str,
    date: str,
    payload: dict,
) -> None:
    """Direct cache write — used in tests to simulate the populate step."""
    cache.screener_result_put(tool_name, params_hash, date, payload)


def _stub_payload_52w(date: str, codes_with_high: list[str]) -> dict:
    return {
        "count": len(codes_with_high),
        "mode": "52w",
        "data": [
            {
                "Code": c,
                "Date": date,
                "prior_sessions": 250,
                "AdjH": 1234.0,
                "AdjL": 1000.0,
                "AdjC": 1234.0,
                "prior_high": 1200.0,
                "prior_low": 950.0,
                "new_high": True,
                "new_low": False,
                "new_high_close": True,
                "new_low_close": False,
            }
            for c in codes_with_high
        ],
    }


def _stub_payload_ytd(date: str, codes_with_high: list[str]) -> dict:
    payload = _stub_payload_52w(date, codes_with_high)
    payload["mode"] = "ytd"
    return payload


class TestScreenerCachePersistence:
    """Direct CacheStore tests — the populate scripts depend on these."""

    def test_get_returns_none_on_miss(self, mock_env):
        assert (
            mock_env["cache"].screener_result_get("detect_52w_high_low", "h", "2026-04-01") is None
        )

    def test_put_then_get_round_trip(self, mock_env):
        cache = mock_env["cache"]
        cache.screener_result_put(
            "detect_52w_high_low",
            "h-default",
            "2026-04-01",
            {"count": 1, "mode": "52w", "data": [{"Code": "12340"}]},
        )
        got = cache.screener_result_get("detect_52w_high_low", "h-default", "2026-04-01")
        assert got is not None
        assert got["count"] == 1
        assert got["data"][0]["Code"] == "12340"

    def test_put_replaces_existing(self, mock_env):
        cache = mock_env["cache"]
        cache.screener_result_put("t", "p", "d", {"count": 1, "data": [1]})
        cache.screener_result_put("t", "p", "d", {"count": 2, "data": [1, 2]})
        got = cache.screener_result_get("t", "p", "d")
        assert got["count"] == 2

    def test_get_range_returns_dict_keyed_by_date(self, mock_env):
        cache = mock_env["cache"]
        for d, n in [("2026-04-01", 1), ("2026-04-02", 2), ("2026-04-03", 3)]:
            cache.screener_result_put("t", "p", d, {"count": n, "data": []})
        got = cache.screener_result_get_range("t", "p", "2026-04-01", "2026-04-02")
        assert set(got.keys()) == {"2026-04-01", "2026-04-02"}
        assert got["2026-04-02"]["count"] == 2

    def test_prune_drops_old_rows_only(self, mock_env, monkeypatch):
        cache = mock_env["cache"]
        # Fixed "today" via SQLite default — drop weeks=0 means delete all
        # rows older than today; only future / today rows survive.
        cache.screener_result_put("t", "p", "1900-01-01", {"count": 0, "data": []})
        cache.screener_result_put("t", "p", "2999-12-31", {"count": 0, "data": []})
        deleted = cache.screener_result_prune(retention_weeks=0)
        assert deleted == 1
        assert cache.screener_result_count() == 1


class TestDetect52wCacheRead:
    async def test_cache_hit_short_circuits(self, mock_env):
        cache = mock_env["cache"]
        params_hash = screener_compute.default_params_hash_52w()
        payload = _stub_payload_52w("2026-04-01", ["12340", "67890"])
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date="2026-04-01",
            payload=payload,
        )
        # Note: no equities_bars_daily seeded — cache hit must be the
        # only reason the tool returns non-empty data.
        result = await _call("detect_52w_high_low", date="2026-04-01", detail=True)
        assert result["count"] == 2
        assert {row["Code"] for row in result["data"]} == {"12340", "67890"}

    async def test_cache_miss_falls_through_to_compute(self, mock_env):
        # No cache row, but seeded bars: tool computes on-demand.
        start = datetime(2026, 1, 5)
        rows = []
        for i in range(20):
            d = (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        final_date = (start + timedelta(days=20 * 7)).strftime("%Y-%m-%d")
        rows.append(_bar("27800", final_date, h=200, low=180, c=200))
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_52w_high_low",
            date=final_date,
            code="27800",
            window_sessions=30,
            min_prior_sessions=1,
            detail=True,
        )
        assert result["count"] == 1
        assert result["data"][0]["new_high"] is True

    async def test_non_default_params_bypass_cache(self, mock_env):
        # Cached payload for default params; tool called with custom
        # window_sessions ⇒ params_hash differs ⇒ cache miss ⇒ falls
        # through to compute (which has no bars seeded, returns empty).
        cache = mock_env["cache"]
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=screener_compute.default_params_hash_52w(),
            date="2026-04-01",
            payload=_stub_payload_52w("2026-04-01", ["99999"]),
        )
        result = await _call(
            "detect_52w_high_low",
            date="2026-04-01",
            window_sessions=20,  # different from default 252
            min_prior_sessions=1,
        )
        # Cache populated for defaults only; non-default => empty compute.
        assert result["count"] == 0

    async def test_code_specified_skips_cache(self, mock_env):
        """Cache is cross-sectional only (built with min_prior_sessions
        active). An explicit code must bypass the cache and recompute
        from bars, otherwise IPO codes that the cross-sectional filter
        dropped would silently disappear from per-code queries.
        """
        cache = mock_env["cache"]

        # Cache says the cross-sectional payload is empty for 2026-04-15.
        params_hash = screener_compute.default_params_hash_52w(
            window_sessions=20, min_prior_sessions=1
        )
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date="2026-04-15",
            payload={"count": 0, "mode": "52w", "data": []},
        )

        # But the bars clearly show a new high for 27800 on that date.
        rows = []
        start = datetime(2026, 1, 5)
        for i in range(20):
            d = (start + timedelta(days=i * 5)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        rows.append(_bar("27800", "2026-04-15", h=300, low=280, c=300))
        _seed(cache, rows)

        # Explicit code → cache bypassed → bars-driven result wins.
        result = await _call(
            "detect_52w_high_low",
            date="2026-04-15",
            code="27800",
            window_sessions=20,
            min_prior_sessions=1,
            detail=True,
        )
        assert result["count"] == 1
        assert result["data"][0]["Code"] == "27800"
        assert result["data"][0]["new_high"] is True

    async def test_code_specified_with_ipo_recovers_signal(self, mock_env):
        """Regression: an IPO with < 60 prior sessions is dropped from
        the cross-sectional cache (correct, suppresses noise), but
        ``detect_52w_high_low(code='X')`` for that IPO must still fire
        because per-code mode bypasses the IPO filter on the on-demand
        path.
        """
        cache = mock_env["cache"]

        # The populate path would have called the tool with code=None
        # and filtered out IPO 99990 (only 5 prior sessions). Simulate
        # that by storing a payload that does NOT contain 99990.
        params_hash = screener_compute.default_params_hash_52w()
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date="2026-04-10",
            payload=_stub_payload_52w("2026-04-10", ["12340"]),  # IPO not here
        )

        # Real bars exist for the IPO with a clear new high.
        rows = []
        for i in range(5):
            d = (datetime(2026, 4, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("99990", d, h=100 + i, low=90, c=95 + i))
        rows.append(_bar("99990", "2026-04-10", h=300, low=200, c=300))
        _seed(cache, rows)

        result = await _call(
            "detect_52w_high_low",
            date="2026-04-10",
            code="99990",
            min_prior_sessions=1,  # bypass IPO filter on on-demand path
            detail=True,
        )
        assert result["count"] == 1
        assert result["data"][0]["Code"] == "99990"
        assert result["data"][0]["new_high"] is True


class TestDetectYtdCacheRead:
    async def test_cache_hit_returns_cached_payload(self, mock_env):
        cache = mock_env["cache"]
        params_hash = screener_compute.default_params_hash_ytd()
        payload = _stub_payload_ytd("2026-04-01", ["12340"])
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_YTD,
            params_hash=params_hash,
            date="2026-04-01",
            payload=payload,
        )
        result = await _call("detect_ytd_high_low", date="2026-04-01")
        assert result["mode"] == "ytd"
        assert result["count"] == 1


class TestDetect52wRange:
    async def test_full_range_cache_hit_skips_bar_table(self, mock_env):
        cache = mock_env["cache"]
        # Seed only enough bars to drive trading-day discovery; their
        # content is irrelevant because every date is a cache hit.
        bars = [_bar("00000", d) for d in ("2026-04-01", "2026-04-02")]
        _seed(cache, bars)
        params_hash = screener_compute.default_params_hash_52w()
        for d, codes in [("2026-04-01", ["12340"]), ("2026-04-02", ["67890"])]:
            _put_cache_payload(
                cache,
                tool_name=screener_compute.TOOL_DETECT_52W,
                params_hash=params_hash,
                date=d,
                payload=_stub_payload_52w(d, codes),
            )
        result = await _call(
            "detect_52w_high_low_range",
            date_from="2026-04-01",
            date_to="2026-04-02",
            detail=True,
        )
        assert result["count"] == 2
        codes = {row["Code"] for row in result["data"]}
        assert codes == {"12340", "67890"}
        assert result["mode"] == "52w"

    async def test_partial_hit_falls_through_for_misses(self, mock_env):
        cache = mock_env["cache"]
        # Hash must match the (window_sessions, min_prior_sessions) the
        # tool is called with — otherwise the lookup misses for trivially
        # different reasons than the test intends.
        params_hash = screener_compute.default_params_hash_52w(
            window_sessions=60, min_prior_sessions=1
        )
        # Day 1 cached; day 2 must be computed from bars.
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date="2026-04-01",
            payload=_stub_payload_52w("2026-04-01", ["00010"]),
        )
        # Build per-code histories long enough that on-demand compute
        # produces a non-empty payload on day 2.
        rows = []
        start = datetime(2026, 1, 5)
        for i in range(60):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("00020", d, h=100 + i, low=80, c=95 + i))
        rows.append(_bar("00020", "2026-04-02", h=300, low=200, c=300))
        # Also drop a stub bar on day 1 so trading-day enumeration sees
        # both calendar days.
        rows.append(_bar("00020", "2026-04-01", h=160, low=130, c=158))
        _seed(cache, rows)

        result = await _call(
            "detect_52w_high_low_range",
            date_from="2026-04-01",
            date_to="2026-04-02",
            window_sessions=60,
            min_prior_sessions=1,
            detail=True,
        )
        codes_by_date: dict[str, set[str]] = {}
        for row in result["data"]:
            codes_by_date.setdefault(row["Date"], set()).add(row["Code"])
        assert "00010" in codes_by_date["2026-04-01"]  # from cache
        assert "00020" in codes_by_date["2026-04-02"]  # on-demand

    async def test_full_miss_outside_cache(self, mock_env):
        # No cached rows. Range tool falls through to on-demand for every
        # trading day discovered from equities_bars_daily.
        rows = []
        start = datetime(2026, 1, 5)
        for i in range(60):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("12340", d, h=100 + i, low=80, c=95 + i))
        rows.append(_bar("12340", "2026-04-10", h=200, low=180, c=200))
        _seed(mock_env["cache"], rows)
        result = await _call(
            "detect_52w_high_low_range",
            date_from="2026-04-10",
            date_to="2026-04-10",
            window_sessions=60,
            min_prior_sessions=1,
            detail=True,
        )
        assert result["count"] == 1
        assert result["data"][0]["Code"] == "12340"

    async def test_single_date_range_works(self, mock_env):
        cache = mock_env["cache"]
        params_hash = screener_compute.default_params_hash_52w()
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date="2026-04-01",
            payload=_stub_payload_52w("2026-04-01", ["12340"]),
        )
        result = await _call(
            "detect_52w_high_low_range",
            date_from="2026-04-01",
            date_to="2026-04-01",
        )
        assert result["count"] == 1

    async def test_range_validates_order(self, mock_env):
        result = await _call(
            "detect_52w_high_low_range",
            date_from="2026-04-10",
            date_to="2026-04-01",
        )
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"

    async def test_range_with_code_skips_cache(self, mock_env):
        """Range + ``code`` must bypass the cross-sectional cache for
        the same reason ``detect_52w_high_low`` does: stored payloads
        omit IPO codes, and a per-code query must surface the IPO's
        bars-driven signal even when the cache says nothing.
        """
        cache = mock_env["cache"]
        params_hash = screener_compute.default_params_hash_52w(
            window_sessions=20, min_prior_sessions=1
        )
        # Cache claims 2026-04-01 has no signal — must NOT win for code='27800'.
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date="2026-04-01",
            payload={"count": 0, "mode": "52w", "data": []},
        )
        # Bars say 27800 hit a new high on 2026-04-01.
        rows = []
        start = datetime(2026, 1, 5)
        for i in range(20):
            d = (start + timedelta(days=i * 4)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        rows.append(_bar("27800", "2026-04-01", h=300, low=280, c=300))
        _seed(cache, rows)

        result = await _call(
            "detect_52w_high_low_range",
            date_from="2026-04-01",
            date_to="2026-04-01",
            code="27800",
            window_sessions=20,
            min_prior_sessions=1,
            detail=True,
        )
        assert result["count"] == 1
        assert result["data"][0]["Code"] == "27800"
        assert result["data"][0]["new_high"] is True


class TestDetectYtdRange:
    async def test_full_range_cache_hit(self, mock_env):
        cache = mock_env["cache"]
        bars = [_bar("00000", d) for d in ("2026-04-01", "2026-04-02")]
        _seed(cache, bars)
        params_hash = screener_compute.default_params_hash_ytd()
        for d, codes in [("2026-04-01", ["12340"]), ("2026-04-02", ["67890"])]:
            _put_cache_payload(
                cache,
                tool_name=screener_compute.TOOL_DETECT_YTD,
                params_hash=params_hash,
                date=d,
                payload=_stub_payload_ytd(d, codes),
            )
        result = await _call(
            "detect_ytd_high_low_range",
            date_from="2026-04-01",
            date_to="2026-04-02",
        )
        assert result["count"] == 2
        assert result["mode"] == "ytd"


class TestOutOfCacheRange:
    """Dates older than the 52-week cache window must error immediately.

    Cross-sectional on-demand compute for these dates can exceed client
    tool-call timeouts (Desktop hit 3-min timeout in PR #161 verification),
    so the screener tools refuse them with ``error_type=OutOfCacheRange``.
    """

    def _old_date(self) -> str:
        """Return an ISO date guaranteed to be outside the 52-week window."""
        return (date.today() - timedelta(weeks=60)).isoformat()

    async def test_detect_52w_rejects_old_date(self, mock_env):
        result = await _call("detect_52w_high_low", date=self._old_date())
        assert result.get("error") is True
        assert result.get("error_type") == "OutOfCacheRange"
        assert "cache_from" in result
        assert "hint" in result

    async def test_detect_52w_rejects_old_date_with_code(self, mock_env):
        # Even with explicit code: out-of-window is uniformly refused so
        # the rule is simple to remember and match in tool description.
        result = await _call("detect_52w_high_low", date=self._old_date(), code="72030")
        assert result.get("error") is True
        assert result.get("error_type") == "OutOfCacheRange"

    async def test_detect_ytd_rejects_old_date(self, mock_env):
        result = await _call("detect_ytd_high_low", date=self._old_date())
        assert result.get("error") is True
        assert result.get("error_type") == "OutOfCacheRange"

    async def test_range_rejects_old_date_from(self, mock_env):
        result = await _call(
            "detect_52w_high_low_range",
            date_from=self._old_date(),
            date_to=date.today().isoformat(),
        )
        assert result.get("error") is True
        assert result.get("error_type") == "OutOfCacheRange"

    async def test_ytd_range_rejects_old_date_from(self, mock_env):
        result = await _call(
            "detect_ytd_high_low_range",
            date_from=self._old_date(),
            date_to=date.today().isoformat(),
        )
        assert result.get("error") is True
        assert result.get("error_type") == "OutOfCacheRange"

    async def test_within_window_still_works(self, mock_env):
        # Sanity: a date inside the window goes through the normal path,
        # returning the standard (count, mode, data) shape (count=0 here
        # because no bars are seeded).
        d = (date.today() - timedelta(days=7)).isoformat()
        result = await _call("detect_52w_high_low", date=d, code="27800")
        assert result.get("error") is None
        assert result.get("mode") == "52w"

    async def test_exactly_52w_boundary_is_in_window(self, mock_env):
        # The cutoff uses strict ``<``, so today - 52 weeks exactly is
        # still in window. Pins the inclusive boundary so future
        # refactors don't accidentally flip ``<`` to ``<=``.
        d = (date.today() - timedelta(weeks=52)).isoformat()
        result = await _call("detect_52w_high_low", date=d, code="27800")
        assert result.get("error_type") != "OutOfCacheRange"
        assert result.get("mode") == "52w"

    async def test_one_day_past_52w_is_out_of_window(self, mock_env):
        d = (date.today() - timedelta(weeks=52, days=1)).isoformat()
        result = await _call("detect_52w_high_low", date=d)
        assert result.get("error") is True
        assert result.get("error_type") == "OutOfCacheRange"


class TestCacheNotReady:
    """Guard that fires when the requested date is beyond the latest cached date."""

    def _seed_yesterday(self, cache: CacheStore) -> str:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _seed(cache, [_bar("10000", yesterday)])
        return yesterday

    async def test_detect_price_limit_returns_cache_not_ready(self, mock_env):
        self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_price_limit", date=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"
        assert today in result["message"]

    async def test_detect_price_limit_passes_when_date_equals_latest(self, mock_env):
        yesterday = self._seed_yesterday(mock_env["cache"])
        result = await _call("detect_price_limit", date=yesterday)
        assert result.get("error_type") != "CacheNotReady"

    async def test_detect_price_limit_no_guard_when_cache_empty(self, mock_env):
        # No rows seeded → latest_date is None → guard must not fire
        today = date.today().isoformat()
        result = await _call("detect_price_limit", date=today)
        assert result.get("error_type") != "CacheNotReady"

    async def test_compare_close_vs_vwap_single_date_guard(self, mock_env):
        self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("compare_close_vs_vwap", code="10000", date=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_compare_close_vs_vwap_open_range_no_guard(self, mock_env):
        # date_from only (no date_to) → end is None → guard must not fire
        self._seed_yesterday(mock_env["cache"])
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        result = await _call("compare_close_vs_vwap", code="10000", date_from=yesterday)
        # Should return normally (empty or data), not cache_not_ready
        assert result.get("error_type") != "CacheNotReady"

    async def test_detect_52w_high_low_returns_cache_not_ready(self, mock_env):
        self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_52w_high_low", date=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_detect_ytd_high_low_returns_cache_not_ready(self, mock_env):
        self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_ytd_high_low", date=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_detect_volume_surge_returns_cache_not_ready(self, mock_env):
        self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_volume_surge", date=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_detect_52w_range_returns_cache_not_ready_when_to_beyond_latest(self, mock_env):
        yesterday = self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_52w_high_low_range", date_from=yesterday, date_to=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_detect_ytd_range_returns_cache_not_ready_when_to_beyond_latest(self, mock_env):
        yesterday = self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_ytd_high_low_range", date_from=yesterday, date_to=today)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_hint_mentions_retry_time(self, mock_env):
        self._seed_yesterday(mock_env["cache"])
        today = date.today().isoformat()
        result = await _call("detect_price_limit", date=today)
        assert "17:15" in result.get("hint", "")


class TestScreenerComputeHelpers:
    def test_params_hash_is_deterministic(self):
        h1 = screener_compute.params_hash({"a": 1, "b": 2})
        h2 = screener_compute.params_hash({"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 16

    def test_default_hashes_differ_per_tool(self):
        # 52w default-hash inputs (window_sessions, min_prior_sessions)
        # vs YTD inputs (min_prior_sessions only) must not collide.
        assert (
            screener_compute.default_params_hash_52w() != screener_compute.default_params_hash_ytd()
        )

    def test_compute_high_low_signals_matches_inline_logic(self):
        rows = [
            _bar("12340", "2026-01-05", h=100, low=80, c=95),
            _bar("12340", "2026-01-12", h=110, low=85, c=105),
            _bar("12340", "2026-01-19", h=200, low=180, c=200),
        ]
        result = screener_compute.compute_high_low_signals(
            rows,
            norm_date="2026-01-19",
            code=None,
            window_sessions=10,
            min_prior_sessions=1,
            mode_label="52w",
        )
        assert result["count"] == 1
        assert result["data"][0]["new_high"] is True
        assert result["data"][0]["new_high_close"] is True


@pytest.mark.usefixtures("mock_env")
class TestDetailParameter:
    """``detail`` parameter: False (default) strips ``data``; True keeps it."""

    async def test_price_limit_default_is_summary(self, mock_env):
        today = "2026-04-01"
        _seed(
            mock_env["cache"],
            [
                _bar("10000", today, h=200, c=200, ul=1),
                _bar("20000", today, low=80, c=80, ll=1),
                _bar("30000", today),
            ],
        )
        result = await _call("detect_price_limit", date=today)
        assert "data" not in result
        assert result["count"] == 2
        assert result["limit_high"] == 1
        assert result["limit_low"] == 1

    async def test_price_limit_detail_true_returns_data(self, mock_env):
        today = "2026-04-01"
        _seed(
            mock_env["cache"],
            [
                _bar("10000", today, ul=1),
            ],
        )
        result = await _call("detect_price_limit", date=today, detail=True)
        assert "data" in result
        assert result["count"] == 1

    async def test_52w_default_is_summary(self, mock_env):
        d = (date.today() - timedelta(days=7)).isoformat()
        bars = [
            _bar("11110", d2, h=100 + i, low=80, c=90)
            for i, d2 in enumerate(
                [(date.today() - timedelta(days=7 + j)).isoformat() for j in range(70, 0, -1)]
            )
        ]
        bars.append(_bar("11110", d, h=300, low=280, c=295))
        _seed(mock_env["cache"], bars)
        result = await _call("detect_52w_high_low", date=d, code="11110")
        assert "data" not in result
        assert "new_high" in result
        assert "new_low" in result
        assert result["mode"] == "52w"

    async def test_52w_detail_true_returns_data(self, mock_env):
        d = (date.today() - timedelta(days=7)).isoformat()
        bars = [
            _bar("11110", (date.today() - timedelta(days=7 + j)).isoformat(), h=100, low=80, c=90)
            for j in range(70, 0, -1)
        ]
        bars.append(_bar("11110", d, h=300, low=280, c=295))
        _seed(mock_env["cache"], bars)
        result = await _call("detect_52w_high_low", date=d, code="11110", detail=True)
        assert "data" in result
        assert result["count"] >= 1

    async def test_ytd_default_is_summary(self, mock_env):
        d = (date.today() - timedelta(days=7)).isoformat()
        result = await _call("detect_ytd_high_low", date=d, code="99990")
        assert "data" not in result
        assert "new_high" in result
        assert result["mode"] == "ytd"

    async def test_volume_surge_default_is_summary(self, mock_env):
        today = "2026-04-01"
        rows = [
            _bar("55550", (date(2026, 3, 1) + timedelta(days=i)).isoformat(), vo=1000.0)
            for i in range(21)
        ]
        rows.append(_bar("55550", today, vo=5000.0))
        _seed(mock_env["cache"], rows)
        result = await _call("detect_volume_surge", date=today)
        assert "data" not in result
        assert "count" in result
        assert result["multiplier"] == 2.0

    async def test_volume_surge_detail_true_returns_data(self, mock_env):
        today = "2026-04-01"
        rows = [
            _bar("55550", (date(2026, 3, 1) + timedelta(days=i)).isoformat(), vo=1000.0)
            for i in range(21)
        ]
        rows.append(_bar("55550", today, vo=5000.0))
        _seed(mock_env["cache"], rows)
        result = await _call("detect_volume_surge", date=today, detail=True)
        assert "data" in result

    async def test_52w_range_default_is_summary(self, mock_env):
        d_from = (date.today() - timedelta(days=14)).isoformat()
        d_to = (date.today() - timedelta(days=7)).isoformat()
        result = await _call("detect_52w_high_low_range", date_from=d_from, date_to=d_to)
        assert "data" not in result
        assert "new_high" in result
        assert "date_from" in result
        assert "date_to" in result

    async def test_ytd_range_default_is_summary(self, mock_env):
        d_from = (date.today() - timedelta(days=14)).isoformat()
        d_to = (date.today() - timedelta(days=7)).isoformat()
        result = await _call("detect_ytd_high_low_range", date_from=d_from, date_to=d_to)
        assert "data" not in result
        assert "new_high" in result
        assert "date_from" in result

    async def test_error_passes_through_unchanged(self, mock_env):
        # Validation errors must not be silently swallowed by the summariser.
        result = await _call("detect_price_limit", date="not-a-date")
        assert "error" in result or "errors" in result
        assert "data" not in result

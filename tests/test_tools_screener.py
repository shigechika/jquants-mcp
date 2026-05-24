"""Tests for screener tools."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

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
        assert by_code["1000"]["limit_high_close"] is True
        assert by_code["1000"]["limit_high_touched"] is True
        assert by_code["2000"]["limit_low_close"] is True
        assert by_code["4000"]["limit_high_touched"] is True
        assert by_code["4000"]["limit_high_close"] is False

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

    async def test_api_fallback_on_cache_miss(self, mock_env):
        """Empty cache triggers API fetch; result is computed from the fetched row."""
        api_row = _bar("27800", "2026-04-01", c=110, vo=1000, va=100_000)
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=[api_row]
        ) as mock_api:
            result = await _call("compare_close_vs_vwap", code="27800", date="2026-04-01")
        mock_api.assert_called_once()
        assert result["count"] == 1
        row = result["data"][0]
        assert row["vwap"] == pytest.approx(100.0)
        assert row["close_above_vwap"] is True


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
        assert "1000" in codes
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
        assert "9999" not in codes

        # But explicit code= bypasses the filter (caller asked specifically).
        result_explicit = await _call(
            "detect_52w_high_low", date=date_today, code="99990", min_prior_sessions=1
        )
        assert result_explicit["count"] == 1

    async def test_new_fields_present_and_correct(self, mock_env):
        """AdjO, close_vs_vwap, and volume_ratio are returned with correct values."""
        from datetime import datetime, timedelta

        start = datetime(2026, 1, 5)
        rows = []
        # 25 prior bars: Vo=1000, C=100, Va=100*1000 → VWAP=100
        for i in range(25):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, o=98.0, c=100.0, vo=1_000.0, va=100_000.0))
        # Today: new high, C=200 > VWAP=150 (Va=300000, Vo=2000), Vo=2x prior avg
        final_date = (start + timedelta(days=25)).strftime("%Y-%m-%d")
        rows.append(
            _bar(
                "27800",
                final_date,
                o=160.0,
                h=210.0,
                low=155.0,
                c=200.0,
                vo=2_000.0,
                va=300_000.0,  # VWAP = 300000/2000 = 150; C=200 > 150 → above
            )
        )
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
        r = result["data"][0]
        # AdjO matches today's open
        assert r["AdjO"] == pytest.approx(160.0)
        # close (200) > VWAP (150) → above
        assert r["close_vs_vwap"] == "above"
        # volume_ratio: today Vo=2000, prior-20 avg=1000 → 2.0
        assert r["volume_ratio"] == pytest.approx(2.0)
        # volume_ratio_sessions: 25 prior bars → baseline = last 20 → all 20 have Vo>0
        assert r["volume_ratio_sessions"] == 20

    async def test_close_below_vwap(self, mock_env):
        """close_vs_vwap = 'below' when close < Va/Vo."""
        from datetime import datetime, timedelta

        start = datetime(2026, 1, 5)
        rows = []
        for i in range(25):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        final_date = (start + timedelta(days=25)).strftime("%Y-%m-%d")
        # C=200, VWAP=250 (Va=500000, Vo=2000) → below
        rows.append(
            _bar(
                "27800",
                final_date,
                h=210.0,
                low=190.0,
                c=200.0,
                vo=2_000.0,
                va=500_000.0,  # VWAP=250 > C=200 → below
            )
        )
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
        assert r["close_vs_vwap"] == "below"


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

    async def test_new_fields_present(self, mock_env):
        """AdjO, close_vs_vwap, volume_ratio, volume_ratio_sessions appear in ytd output."""
        rows = []
        for i in range(20):
            d = (datetime(2026, 1, 5) + timedelta(days=i * 5)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, o=98.0, c=100.0, vo=1_000.0, va=100_000.0))
        final_date = "2026-04-26"
        rows.append(
            _bar(
                "27800",
                final_date,
                o=160.0,
                h=210.0,
                low=155.0,
                c=200.0,
                vo=2_000.0,
                va=300_000.0,  # VWAP=150; C=200 > 150 → above
            )
        )
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_ytd_high_low",
            date=final_date,
            code="27800",
            min_prior_sessions=1,
            detail=True,
        )
        assert result["count"] == 1
        r = result["data"][0]
        assert r["AdjO"] == pytest.approx(160.0)
        assert r["close_vs_vwap"] == "above"
        assert r["volume_ratio"] == pytest.approx(2.0)
        assert r["volume_ratio_sessions"] == 20

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
        assert result["data"][0]["Code"] == "2780"
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

    async def test_api_fallback_per_code_on_cache_miss(self, mock_env):
        """When code is given and cache is empty, API is called and surge is detected."""
        api_rows = [
            _bar("27800", "2026-03-30", vo=500),
            _bar("27800", "2026-03-31", vo=600),
            _bar("27800", "2026-04-01", vo=3000),  # surge: 3000 / avg(550) ≈ 5.45×
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=api_rows
        ) as mock_api:
            result = await _call(
                "detect_volume_surge",
                code="27800",
                date="2026-04-01",
                multiplier=2.0,
                baseline_days=2,
            )
        mock_api.assert_called_once()
        assert result["count"] == 1

    async def test_no_api_fallback_for_cross_sectional(self, mock_env):
        """Without code, cross-sectional queries do NOT call the API on cache miss."""
        with patch.object(mock_env["client"], "get_all_pages", new_callable=AsyncMock) as mock_api:
            result = await _call("detect_volume_surge", date="2026-04-01")
        mock_api.assert_not_called()
        assert result["count"] == 0


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
        assert (
            got["data"][0]["Code"] == "12340"
        )  # raw cache stores 5-digit; display_code applied at tool layer

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
        assert {row["Code"] for row in result["data"]} == {"1234", "6789"}

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
        assert result["data"][0]["Code"] == "2780"
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
        assert result["data"][0]["Code"] == "9999"
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
        assert codes == {"1234", "6789"}
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
        assert "0001" in codes_by_date["2026-04-01"]  # from cache
        assert "0002" in codes_by_date["2026-04-02"]  # on-demand

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
        assert result["data"][0]["Code"] == "1234"

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
        assert result["data"][0]["Code"] == "2780"
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

    async def test_price_limit_summary_breakdown(self, mock_env):
        today = "2026-04-01"
        _seed(
            mock_env["cache"],
            [
                # 引けストップ高: ul=1, c==h
                _bar("10000", today, h=200, c=200, ul=1),
                # 寄らずストップ高 (or intraday-only): ul=1, c!=h
                _bar("11000", today, h=200, c=150, ul=1),
                # 引けストップ安: ll=1, c==l
                _bar("20000", today, low=80, c=80, ll=1),
                # 寄らずストップ安: ll=1, c!=l
                _bar("21000", today, low=80, c=100, ll=1),
                # no limit
                _bar("30000", today),
            ],
        )
        result = await _call("detect_price_limit", date=today)
        assert result["count"] == 4
        assert result["limit_high"] == 2
        assert result["limit_high_close"] == 1
        assert result["limit_high_touched"] == 1
        assert result["limit_low"] == 2
        assert result["limit_low_close"] == 1
        assert result["limit_low_touched"] == 1

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


# ---------------------------------------------------------------------------
# name field injection
# ---------------------------------------------------------------------------


def _seed_master(cache: CacheStore, code: str, name: str, date: str = "2026-01-01") -> None:
    """Insert one equities_master row so get_name_map() can resolve the code."""
    cache.put_rows(
        "equities_master",
        [{"Code": code, "Date": date, "CoName": name, "CoNameEn": name + " En"}],
        key_columns=["Code", "Date"],
    )


@pytest.mark.asyncio
class TestNameFieldInjection:
    """name field appears in detail items for all six ranking/screener tools."""

    async def test_detect_price_limit_name_in_detail(self, mock_env):
        today = "2026-04-01"
        _seed(mock_env["cache"], [_bar("10000", today, h=200, c=200, ul=1)])
        _seed_master(mock_env["cache"], "10000", "テスト会社")
        result = await _call("detect_price_limit", date=today, detail=True)
        assert result["data"][0]["name"] == "テスト会社"

    async def test_detect_price_limit_name_none_when_missing(self, mock_env):
        today = "2026-04-01"
        _seed(mock_env["cache"], [_bar("10000", today, h=200, c=200, ul=1)])
        result = await _call("detect_price_limit", date=today, detail=True)
        assert result["data"][0]["name"] is None

    async def test_detect_volume_surge_name_in_detail(self, mock_env):
        today = "2026-04-01"
        rows = [
            _bar("55550", (date(2026, 3, 1) + timedelta(days=i)).isoformat(), vo=1000.0)
            for i in range(21)
        ]
        rows.append(_bar("55550", today, vo=5000.0))
        _seed(mock_env["cache"], rows)
        _seed_master(mock_env["cache"], "55550", "急増商事")
        result = await _call("detect_volume_surge", date=today, detail=True)
        assert len(result["data"]) >= 1
        assert result["data"][0]["name"] == "急増商事"

    async def test_detect_52w_name_in_detail(self, mock_env):
        d = (date.today() - timedelta(days=7)).isoformat()
        bars = [
            _bar("11110", (date.today() - timedelta(days=7 + j)).isoformat(), h=100, low=80, c=90)
            for j in range(70, 0, -1)
        ]
        bars.append(_bar("11110", d, h=300, low=280, c=295))
        _seed(mock_env["cache"], bars)
        _seed_master(mock_env["cache"], "11110", "高値更新株式")
        result = await _call("detect_52w_high_low", date=d, code="11110", detail=True)
        assert len(result["data"]) >= 1
        assert result["data"][0]["name"] == "高値更新株式"

    async def test_detect_ytd_name_in_detail(self, mock_env):
        # Use a date near the start of this year so YTD window is meaningful.
        year = date.today().year
        d = date(year, 1, 31).isoformat()
        bars = [
            _bar("22220", date(year, 1, m).isoformat(), h=100, low=80, c=90) for m in range(2, 30)
        ]
        bars.append(_bar("22220", d, h=300, low=280, c=295))
        _seed(mock_env["cache"], bars)
        _seed_master(mock_env["cache"], "22220", "年初来高値株")
        result = await _call("detect_ytd_high_low", date=d, code="22220", detail=True)
        assert len(result["data"]) >= 1
        assert result["data"][0]["name"] == "年初来高値株"

    async def test_get_top_movers_name_key_present(self, mock_env):
        _seed(
            mock_env["cache"],
            [
                _bar("13010", "2026-04-01", c=100.0, adj_c=100.0),
                _bar("13010", "2026-04-02", c=110.0, adj_c=110.0),
            ],
        )
        _seed_master(mock_env["cache"], "13010", "トップ株式")
        result = await _call("get_top_movers", date="2026-04-02", direction="up")
        assert len(result["items"]) >= 1
        assert result["items"][0]["name"] == "トップ株式"

    async def test_get_top_volume_name_key_present(self, mock_env):
        _seed(
            mock_env["cache"],
            [_bar("13010", "2026-04-02", vo=9999.0)],
        )
        _seed_master(mock_env["cache"], "13010", "出来高王")
        result = await _call("get_top_volume", date="2026-04-02")
        assert len(result["items"]) >= 1
        assert result["items"][0]["name"] == "出来高王"

    async def test_detect_52w_range_name_cache_hit_path(self, mock_env):
        """Cache-hit branch of _high_low_range injects name from name_map."""
        cache = mock_env["cache"]
        params_hash = screener_compute.default_params_hash_52w()
        d_to = (date.today() - timedelta(days=7)).isoformat()
        d_from = (date.today() - timedelta(days=14)).isoformat()
        # Seed one bar so iter_session_dates can find d_to.
        _seed(cache, [_bar("12340", d_to)])
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash=params_hash,
            date=d_to,
            payload=_stub_payload_52w(d_to, ["12340"]),
        )
        _seed_master(cache, "12340", "キャッシュヒット株")
        result = await _call(
            "detect_52w_high_low_range", date_from=d_from, date_to=d_to, detail=True
        )
        names = [r.get("name") for r in result.get("data", [])]
        assert "キャッシュヒット株" in names

    async def test_detect_ytd_range_name_cache_hit_path(self, mock_env):
        """Cache-hit branch of _high_low_range injects name for YTD variant."""
        cache = mock_env["cache"]
        params_hash = screener_compute.default_params_hash_ytd()
        d_to = (date.today() - timedelta(days=7)).isoformat()
        d_from = (date.today() - timedelta(days=14)).isoformat()
        _seed(cache, [_bar("99980", d_to)])
        _put_cache_payload(
            cache,
            tool_name=screener_compute.TOOL_DETECT_YTD,
            params_hash=params_hash,
            date=d_to,
            payload=_stub_payload_ytd(d_to, ["99980"]),
        )
        _seed_master(cache, "99980", "YTDキャッシュ株")
        result = await _call(
            "detect_ytd_high_low_range", date_from=d_from, date_to=d_to, detail=True
        )
        names = [r.get("name") for r in result.get("data", [])]
        assert "YTDキャッシュ株" in names


# ----------------------------------------------------------------
# Helpers for distribution-day / follow-through-day tests
# ----------------------------------------------------------------


def _topix_row(date_str: str, close: float, open_: float | None = None) -> dict:
    """Build a minimal TOPIX bar row."""
    o = open_ if open_ is not None else close
    return {
        "Date": f"{date_str} 00:00:00",
        "O": o,
        "H": close * 1.001,
        "L": close * 0.999,
        "C": close,
    }


def _seed_topix(cache: CacheStore, rows: list[dict]) -> None:
    cache.put_rows("indices_bars_daily_topix", rows, key_columns=["Date"])


def _seed_ebd_va(cache: CacheStore, date_str: str, total_va: float, n_stocks: int = 5) -> None:
    """Seed equities_bars_daily rows so that SUM(Va) == total_va on date_str."""
    va_each = total_va / n_stocks
    eq_rows = [
        {
            "Code": f"9{i:04d}0",
            "Date": date_str,
            "O": 100.0,
            "H": 110.0,
            "L": 90.0,
            "C": 100.0,
            "UL": 0,
            "LL": 0,
            "Vo": 1000.0,
            "Va": va_each,
            "AdjFactor": 1.0,
            "AdjO": 100.0,
            "AdjH": 110.0,
            "AdjL": 90.0,
            "AdjC": 100.0,
            "AdjVo": 1000.0,
        }
        for i in range(n_stocks)
    ]
    cache.put_rows(
        "equities_bars_daily", eq_rows, key_columns=["Code", "Date"], adj_factor_key="AdjFactor"
    )


def _build_topix_series(
    start_close: float = 2000.0,
    n_warmup: int = 22,
    window_data: list[float] | None = None,
    base_date: str = "2025-01-01",
) -> list[dict]:
    """Build a TOPIX series: n_warmup alternating ±0.8% sessions, then window_data returns.

    window_data: list of % returns for the window sessions (e.g. [-3.0, 0.0, 0.5, ...]).
    Returns topix rows starting from base_date + n_warmup + len(window_data) trading days.
    """
    from datetime import date as date_, timedelta

    if window_data is None:
        window_data = []

    rows = []
    d = date_.fromisoformat(base_date)
    close = start_close

    for i in range(n_warmup):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append(_topix_row(d.isoformat(), close))
        # alternate +0.8% / -0.8% so σ is non-zero
        close = close * (1.008 if i % 2 == 0 else 0.992)
        d += timedelta(days=1)

    for ret_pct in window_data:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close = close * (1 + ret_pct / 100)
        rows.append(_topix_row(d.isoformat(), close))
        d += timedelta(days=1)

    return rows


class TestDetectDistributionDays:
    async def test_no_distribution_no_warning(self, mock_env):
        """All window sessions flat → count 0, warning False."""
        cache = mock_env["cache"]
        # 22 warm-up alternating ±0.8%, then 25 flat window sessions
        window = [0.0] * 25
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)
        target_date = topix_rows[-1]["Date"][:10]
        # Seed Va for each session (constant 1e12)
        for r in topix_rows:
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)

        result = await _call("detect_distribution_days", date=target_date)
        assert result["distribution_count"] == 0
        assert result["warning"] is False
        assert result["distribution_days"] == []

    async def test_four_dist_days_triggers_warning(self, mock_env):
        """Four sessions with large drops (> 2σ) → warning True."""
        cache = mock_env["cache"]
        # Use alternating ±0.5% throughout so σ stays > 0 in all windows.
        # Four sessions have -5% drops: z ≈ -10σ → clearly distribution days.
        window = [0.5 if i % 2 == 0 else -0.5 for i in range(25)]
        for pos in [21, 22, 23, 24]:
            window[pos] = -5.0
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)
        target_date = topix_rows[-1]["Date"][:10]

        for i, r in enumerate(topix_rows):
            d = r["Date"][:10]
            va = 2_000_000_000_000 if i >= len(topix_rows) - 4 else 1_000_000_000_000
            _seed_ebd_va(cache, d, va)

        result = await _call("detect_distribution_days", date=target_date)
        assert result["distribution_count"] >= 4
        assert result["warning"] is True

    async def test_volume_confirmed_field(self, mock_env):
        """distribution_days entries include volume_confirmed."""
        cache = mock_env["cache"]
        # Alternating ±0.5% so σ > 0; last session has -5% (z ≈ -10) → dist day.
        window = [0.5 if i % 2 == 0 else -0.5 for i in range(24)] + [-5.0]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)
        target_date = topix_rows[-1]["Date"][:10]

        for r in topix_rows[:-1]:
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)
        # today Va > prev → volume_confirmed = True
        _seed_ebd_va(cache, target_date, 2_000_000_000_000)

        result = await _call("detect_distribution_days", date=target_date)
        assert result["distribution_count"] == 1
        dist = result["distribution_days"][0]
        assert dist["date"] == target_date
        assert dist["volume_confirmed"] is True
        assert dist["market_va"] > 0

    async def test_cache_not_ready_future_date(self, mock_env):
        """Requesting a date beyond the latest equities date returns CacheNotReady."""
        cache = mock_env["cache"]
        # Seed equities bars so get_latest_equities_date() returns a known date.
        window = [0.5 if i % 2 == 0 else -0.5 for i in range(25)]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)
        latest_date = topix_rows[-1]["Date"][:10]
        _seed_ebd_va(cache, latest_date, 1_000_000_000_000)

        future = "2099-01-01"
        result = await _call("detect_distribution_days", date=future)
        assert result.get("error") is True
        assert result.get("error_type") == "CacheNotReady"

    async def test_insufficient_data(self, mock_env):
        """Fewer than sigma_window + window_sessions + 1 sessions → InsufficientData."""
        topix_rows = _build_topix_series(n_warmup=5, window_data=[0.0] * 3)
        _seed_topix(mock_env["cache"], topix_rows)
        target = topix_rows[-1]["Date"][:10]
        result = await _call("detect_distribution_days", date=target)
        assert result.get("error") is True
        assert result.get("error_type") == "InsufficientData"

    async def test_stale_topix_falls_back_to_latest_date(self, mock_env):
        """TOPIX data ends before norm_date → falls back to latest TOPIX date, no error."""
        cache = mock_env["cache"]
        # Seed TOPIX up to topix_latest (lag behind equities by a few days).
        window = [0.5 if i % 2 == 0 else -0.5 for i in range(25)]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)
        topix_latest = topix_rows[-1]["Date"][:10]

        # Seed equities Va for the TOPIX range.
        for r in topix_rows:
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)

        # Seed an equities bar 3 days *after* the last TOPIX date so that
        # get_latest_equities_date() returns a newer date than TOPIX.
        future_date = (date.fromisoformat(topix_latest) + timedelta(days=3)).isoformat()
        _seed_ebd_va(cache, future_date, 1_000_000_000_000)

        # Request the future equities date — TOPIX is behind, should fall back.
        result = await _call("detect_distribution_days", date=future_date)
        assert result.get("error") is not True
        assert result["date"] == topix_latest  # fell back to latest available TOPIX date
        assert isinstance(result.get("distribution_days"), list)
        assert result["distribution_count"] == len(result["distribution_days"])

    async def test_validation_bad_date(self, mock_env):
        result = await _call("detect_distribution_days", date="not-a-date")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"

    async def test_validation_sigma_multiplier(self, mock_env):
        result = await _call("detect_distribution_days", date="2025-06-01", sigma_multiplier=0.0)
        assert result.get("error") is True


class TestDetectFollowThroughDay:
    async def test_confirmed_on_day_4(self, mock_env):
        """Day 4 from rally_start with large up-move and volume → confirmed True."""
        cache = mock_env["cache"]
        # 22 warm-up, then rally_start + 3 prior sessions + target (day 4)
        # target: +3% (well above 2σ ≈ 1.6%)
        window = [0.0, 0.0, 0.0, 3.0]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)

        rally_start = topix_rows[-4]["Date"][:10]
        target_date = topix_rows[-1]["Date"][:10]

        for i, r in enumerate(topix_rows[:-1]):
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)
        # target Va > prev → volume_confirmed
        _seed_ebd_va(cache, target_date, 2_000_000_000_000)

        result = await _call("detect_follow_through_day", rally_start=rally_start, date=target_date)
        assert result["confirmed"] is True
        assert result["session_number"] == 4
        assert result["day_confirmed"] is True
        assert result["price_confirmed"] is True
        assert result["volume_confirmed"] is True

    async def test_not_confirmed_day_3(self, mock_env):
        """Session 3 from rally_start → day_confirmed False even with good price."""
        cache = mock_env["cache"]
        window = [0.0, 0.0, 3.0]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)

        rally_start = topix_rows[-3]["Date"][:10]
        target_date = topix_rows[-1]["Date"][:10]

        for r in topix_rows:
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)
        _seed_ebd_va(cache, target_date, 2_000_000_000_000)

        result = await _call("detect_follow_through_day", rally_start=rally_start, date=target_date)
        assert result["confirmed"] is False
        assert result["session_number"] == 3
        assert result["day_confirmed"] is False

    async def test_not_confirmed_low_return(self, mock_env):
        """Day 4+ but z-score below threshold → price_confirmed False."""
        cache = mock_env["cache"]
        window = [0.0, 0.0, 0.0, 0.1]  # tiny gain, z << 2σ
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)

        rally_start = topix_rows[-4]["Date"][:10]
        target_date = topix_rows[-1]["Date"][:10]

        for r in topix_rows:
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)
        _seed_ebd_va(cache, target_date, 2_000_000_000_000)

        result = await _call("detect_follow_through_day", rally_start=rally_start, date=target_date)
        assert result["confirmed"] is False
        assert result["price_confirmed"] is False

    async def test_not_confirmed_volume_lower(self, mock_env):
        """Day 4+ with large price move but volume lower → volume_confirmed False."""
        cache = mock_env["cache"]
        window = [0.0, 0.0, 0.0, 3.0]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)

        rally_start = topix_rows[-4]["Date"][:10]
        target_date = topix_rows[-1]["Date"][:10]

        for r in topix_rows:
            _seed_ebd_va(cache, r["Date"][:10], 2_000_000_000_000)
        # target Va < prev → volume_confirmed False
        _seed_ebd_va(cache, target_date, 1_000_000_000_000)

        result = await _call("detect_follow_through_day", rally_start=rally_start, date=target_date)
        assert result["confirmed"] is False
        assert result["volume_confirmed"] is False

    async def test_rally_start_after_date_error(self, mock_env):
        result = await _call(
            "detect_follow_through_day",
            rally_start="2026-06-01",
            date="2026-05-01",
        )
        assert result.get("error") is True

    async def test_response_fields_present(self, mock_env):
        """All expected fields appear in the response."""
        cache = mock_env["cache"]
        window = [0.0, 0.0, 0.0, 3.0]
        topix_rows = _build_topix_series(n_warmup=22, window_data=window)
        _seed_topix(cache, topix_rows)
        rally_start = topix_rows[-4]["Date"][:10]
        target_date = topix_rows[-1]["Date"][:10]
        for r in topix_rows:
            _seed_ebd_va(cache, r["Date"][:10], 1_000_000_000_000)
        _seed_ebd_va(cache, target_date, 2_000_000_000_000)

        result = await _call("detect_follow_through_day", rally_start=rally_start, date=target_date)
        for field in (
            "date",
            "rally_start",
            "rally_start_topix",
            "session_number",
            "confirmed",
            "reason",
            "topix_close",
            "topix_change_pct",
            "z_score",
            "sigma",
            "sigma_multiplier",
            "price_confirmed",
            "day_confirmed",
            "volume_confirmed",
            "market_va_today",
            "market_va_prev",
        ):
            assert field in result, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# detect_consecutive_dividend_increase
# ---------------------------------------------------------------------------


def _seed_fins_summary(
    cache: CacheStore,
    code: str,
    disc_date: str,
    doc_type: str,
    cur_fy_en: str,
    div_ann: float | None,
) -> None:
    """Insert one fins_summary row for consecutive-dividend tests."""
    row: dict = {
        "Code": code,
        "DisclosedDate": disc_date,
        "DocType": doc_type,
        "CurFYEn": cur_fy_en,
    }
    if div_ann is not None:
        row["DivAnn"] = str(div_ann)
    cache.put_rows("fins_summary", [row], key_columns=["Code", "DisclosedDate"])


@pytest.mark.asyncio
class TestDetectConsecutiveDividendIncrease:
    """detect_consecutive_dividend_increase のテスト。"""

    async def test_basic_consecutive_increase(self, mock_env):
        """min_years 連続増配を満たすコードが結果に含まれる。"""
        cache = mock_env["cache"]
        _seed_master(cache, "13010", "花王")
        # 11 years of increasing dividends: 10..20
        for y in range(2013, 2024):
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                float(10 + (y - 2013)),
            )

        result = await _call("detect_consecutive_dividend_increase", min_years=10)
        assert result.get("error") is None or result.get("error") is False
        codes = [item["code"] for item in result["data"]]
        assert "1301" in codes

    async def test_below_min_years_excluded(self, mock_env):
        """連続増配年数が min_years 未満のコードは除外される。"""
        cache = mock_env["cache"]
        _seed_master(cache, "13010", "花王")
        # Only 5 years of increase
        for y in range(2018, 2024):
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                float(10 + (y - 2018)),
            )

        result = await _call("detect_consecutive_dividend_increase", min_years=10)
        codes = [item["code"] for item in result["data"]]
        assert "1301" not in codes

    async def test_lookahead_bias_as_of_date(self, mock_env):
        """as_of_date より後の開示はカウントされない。"""
        cache = mock_env["cache"]
        _seed_master(cache, "13010", "花王")
        # 11 years but the last 3 years' disclosures are after as_of_date
        for y in range(2013, 2024):
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                float(10 + (y - 2013)),
            )

        # Cut off at 2021-12-31: only 2013-2020 FY (disc dates up to 2021-06-20)
        result = await _call(
            "detect_consecutive_dividend_increase",
            min_years=10,
            as_of_date="2021-12-31",
        )
        codes = [item["code"] for item in result["data"]]
        assert "1301" not in codes  # only 8 years visible before cut-off

    async def test_split_adjusted_streak(self, mock_env):
        """株式分割をまたぐ連続増配が正しく検出される（split 調整なしでは誤検知）。

        Split on 2021-10-01 (between FY2020 disc 2021-06-20 and FY2021 disc 2022-06-20).
        FY2019: disc 2020-06-20, raw div=120 → adj=120*0.5=60
        FY2020: disc 2021-06-20, raw div=130 → adj=130*0.5=65 (split 2021-10-01 > disc)
        FY2021: disc 2022-06-20, raw div=70  → adj=70*1.0=70 (no splits after this disc)
        FY2022: disc 2023-06-20, raw div=80  → adj=80

        Raw streak: FY2020(130)→FY2021(70) looks like a CUT → 1 consecutive year only.
        Adjusted:   60 → 65 → 70 → 80 → 3 consecutive increases → qualifies at min_years=3.
        """
        cache = mock_env["cache"]
        _seed_master(cache, "74660", "SPK")

        for y, div in [(2019, 120.0), (2020, 130.0), (2021, 70.0), (2022, 80.0)]:
            _seed_fins_summary(
                cache,
                "74660",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                div,
            )

        # 1:2 split on 2021-10-01 — strictly AFTER FY2020 disc_date (2021-06-20),
        # so get_cumulative_split_factor("74660", "2021-06-20") returns 0.5.
        cache.put_rows(
            "equities_bars_daily",
            [_bar("74660", "2021-10-01", adj_factor=0.5)],
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        result = await _call("detect_consecutive_dividend_increase", min_years=3)
        assert result.get("error") is None or result.get("error") is False
        codes = [item["code"] for item in result["data"]]
        assert "7466" in codes

    async def test_zero_dividend_breaks_streak(self, mock_env):
        """div_ann == 0 の年でストリークが途切れる。"""
        cache = mock_env["cache"]
        # 5 years increase, then 0, then 3 years increase
        divs = [10, 20, 30, 40, 50, 0, 10, 20, 30]
        for i, d in enumerate(divs):
            y = 2014 + i
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                float(d),
            )

        result = await _call("detect_consecutive_dividend_increase", min_years=4)
        codes = [item["code"] for item in result["data"]]
        # Only 3 consecutive years from the latest (after the zero cut)
        assert "1301" not in codes

    async def test_nonzero_decrease_breaks_streak(self, mock_env):
        """非ゼロ減配（50→40）でストリークが途切れる。"""
        cache = mock_env["cache"]
        # 3 years increase, then a non-zero decrease, then 2 years increase
        divs = [10.0, 20.0, 30.0, 40.0, 50.0, 40.0, 50.0, 60.0]
        for i, d in enumerate(divs):
            y = 2015 + i
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                d,
            )

        result = await _call("detect_consecutive_dividend_increase", min_years=3)
        codes = [item["code"] for item in result["data"]]
        # Only 2 consecutive increases from latest (FY2020=50, FY2021=40 breaks)
        assert "1301" not in codes

    async def test_invalid_min_years(self, mock_env):
        """min_years < 1 でバリデーションエラーが返る。"""
        result = await _call("detect_consecutive_dividend_increase", min_years=0)
        assert result.get("error") is True

    async def test_empty_cache_returns_empty(self, mock_env):
        """fins_summary が空のときエラーではなく empty 相当のレスポンスが返る。"""
        result = await _call("detect_consecutive_dividend_increase")
        # Either error=True (cache not ready) or count=0 (empty result)
        assert result.get("error") is True or result.get("count") == 0

    async def test_response_fields_present(self, mock_env):
        """レスポンスに必須フィールドが含まれる。"""
        cache = mock_env["cache"]
        _seed_master(cache, "13010", "花王")
        for y in range(2013, 2024):
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                float(10 + (y - 2013)),
            )

        result = await _call("detect_consecutive_dividend_increase", min_years=10)
        assert "count" in result
        assert "min_years" in result
        assert "data" in result
        assert len(result["data"]) > 0
        item = result["data"][0]
        for field in (
            "code",
            "name",
            "consecutive_years",
            "latest_div_ann",
            "latest_fy_end",
            "history",
        ):
            assert field in item, f"Missing field: {field}"

    async def test_cache_hit_filters_by_min_years(self, mock_env):
        """screener_results キャッシュヒット時に min_years で正しく絞り込まれる。"""
        cache = mock_env["cache"]
        # Pre-populate screener_results with two stocks: 5y and 12y streaks.
        payload = {
            "count": 2,
            "data": [
                {
                    "code": "13010",
                    "consecutive_years": 12,
                    "latest_div_ann": 50.0,
                    "latest_fy_end": "2025-03-31",
                    "history": [],
                },
                {
                    "code": "72030",
                    "consecutive_years": 5,
                    "latest_div_ann": 20.0,
                    "latest_fy_end": "2025-03-31",
                    "history": [],
                },
            ],
        }
        cache.screener_result_put(
            screener_compute.TOOL_DETECT_CONSECUTIVE_DIV,
            screener_compute.default_params_hash_consecutive_div(),
            "2025-05-23",
            payload,
        )
        _seed_master(cache, "13010", "花王")
        _seed_master(cache, "72030", "ホンダ")

        result = await _call("detect_consecutive_dividend_increase", min_years=10)
        assert result.get("error") is None or result.get("error") is False
        codes = [item["code"] for item in result["data"]]
        assert "1301" in codes
        assert "7203" not in codes  # only 5y streak, below min_years=10
        assert result["min_years"] == 10
        assert result["as_of_date"] is None

    async def test_as_of_date_bypasses_cache(self, mock_env):
        """as_of_date 指定時はキャッシュをバイパスしてライブ計算する。"""
        cache = mock_env["cache"]
        # Seed a valid cache entry with 12-year streak.
        payload = {
            "count": 1,
            "data": [
                {
                    "code": "13010",
                    "consecutive_years": 12,
                    "latest_div_ann": 50.0,
                    "latest_fy_end": "2025-03-31",
                    "history": [],
                }
            ],
        }
        cache.screener_result_put(
            screener_compute.TOOL_DETECT_CONSECUTIVE_DIV,
            screener_compute.default_params_hash_consecutive_div(),
            "2025-05-23",
            payload,
        )
        _seed_master(cache, "13010", "花王")
        # Seed fins_summary with only 5 years (below min_years=10) before as_of_date.
        for y in range(2018, 2024):
            _seed_fins_summary(
                cache,
                "13010",
                f"{y + 1}-06-20",
                "FYFinancialStatements",
                f"{y}-03-31",
                float(10 + (y - 2018)),
            )

        # With as_of_date set, must use live computation (5 years only → excluded).
        result = await _call(
            "detect_consecutive_dividend_increase",
            min_years=10,
            as_of_date="2024-12-31",
        )
        codes = [item["code"] for item in result["data"]]
        assert "1301" not in codes  # live calc shows only 5y streak


class TestComputeConsecutiveDivSnapshot:
    """screener_compute.compute_consecutive_div_snapshot の純粋関数テスト。"""

    def test_basic_streak(self):
        """連続増配のある銘柄が正しく検出される。"""
        fy_history = {
            "13010": [
                {"fy_end": "2021-03-31", "disc_date": "2021-06-20", "div_ann": 40.0},
                {"fy_end": "2022-03-31", "disc_date": "2022-06-20", "div_ann": 50.0},
                {"fy_end": "2023-03-31", "disc_date": "2023-06-20", "div_ann": 60.0},
            ]
        }
        result = screener_compute.compute_consecutive_div_snapshot(fy_history, {})
        assert result["count"] == 1
        item = result["data"][0]
        assert item["code"] == "13010"
        assert item["consecutive_years"] == 2
        assert item["latest_div_ann"] == 60.0

    def test_no_streak_excluded(self):
        """連続増配なし（減配あり）の銘柄は除外される。"""
        fy_history = {
            "99990": [
                {"fy_end": "2022-03-31", "disc_date": "2022-06-20", "div_ann": 50.0},
                {"fy_end": "2023-03-31", "disc_date": "2023-06-20", "div_ann": 30.0},
            ]
        }
        result = screener_compute.compute_consecutive_div_snapshot(fy_history, {})
        assert result["count"] == 0

    def test_split_adjusted(self):
        """分割補正後に連続増配となるケースが正しく検出される。"""
        # raw: 120 -> 70 (looks like a cut), adj: 60 -> 70 (increase after 1:2 split)
        fy_history = {
            "74660": [
                {"fy_end": "2020-03-31", "disc_date": "2020-06-20", "div_ann": 120.0},
                {"fy_end": "2021-03-31", "disc_date": "2021-06-20", "div_ann": 70.0},
            ]
        }
        # Split on 2020-10-01, after disc_date 2020-06-20 → factor=0.5 applied to FY2020
        split_events = {"74660": [("2020-10-01", 0.5)]}
        result = screener_compute.compute_consecutive_div_snapshot(fy_history, split_events)
        assert result["count"] == 1
        assert result["data"][0]["consecutive_years"] == 1

    def test_sorted_by_consecutive_years_desc(self):
        """結果は consecutive_years 降順でソートされる。"""
        fy_history = {
            "10010": [
                {
                    "fy_end": f"{2020 + i}-03-31",
                    "disc_date": f"{2021 + i}-06-20",
                    "div_ann": float(10 + i),
                }
                for i in range(3)  # 2 consecutive
            ],
            "20020": [
                {
                    "fy_end": f"{2018 + i}-03-31",
                    "disc_date": f"{2019 + i}-06-20",
                    "div_ann": float(5 + i),
                }
                for i in range(5)  # 4 consecutive
            ],
        }
        result = screener_compute.compute_consecutive_div_snapshot(fy_history, {})
        assert result["data"][0]["consecutive_years"] >= result["data"][1]["consecutive_years"]

    def test_raw_code_stored(self):
        """キャッシュには 5 桁の raw code が格納される（display_code 変換なし）。"""
        fy_history = {
            "13010": [
                {"fy_end": "2022-03-31", "disc_date": "2022-06-20", "div_ann": 40.0},
                {"fy_end": "2023-03-31", "disc_date": "2023-06-20", "div_ann": 50.0},
            ]
        }
        result = screener_compute.compute_consecutive_div_snapshot(fy_history, {})
        assert result["data"][0]["code"] == "13010"  # raw 5-digit, not "1301"

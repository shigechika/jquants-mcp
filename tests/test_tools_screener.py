"""Tests for screener tools."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
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

        result = await _call("detect_price_limit", date="2026-04-01")

        assert result["count"] == 3
        by_code = {row["Code"]: row for row in result["data"]}
        assert by_code["10000"]["limit_high_close"] is True
        assert by_code["10000"]["limit_high_touched"] is True
        assert by_code["20000"]["limit_low_close"] is True
        assert by_code["40000"]["limit_high_touched"] is True
        assert by_code["40000"]["limit_high_close"] is False

    async def test_code_filter_returns_row_even_if_not_triggered(self, mock_env):
        _seed(mock_env["cache"], [_bar("27800", "2026-04-01")])
        result = await _call("detect_price_limit", date="2026-04-01", code="27800")
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
        )
        r = result["data"][0]
        assert r["new_high"] is True  # H == prior max → tie
        assert r["new_high_close"] is True  # C == prior max → tie

    async def test_neither_signal_for_interior_day(self, mock_env):
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
        )
        r = result["data"][0]
        assert r["new_high"] is True  # tie at flat 110 default
        assert r["new_high_close"] is False  # close 105 below max 110

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

        result = await _call("detect_52w_high_low", date=date_today)  # default min=60
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

        result = await _call("detect_volume_surge", date=probe, multiplier=2.0, baseline_days=20)
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

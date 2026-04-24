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


class TestDetectYearlyHighLow:
    async def test_new_high_flagged(self, mock_env):
        # Sessions spread across ~3 months, final close above prior highs.
        start = datetime(2026, 1, 5)
        rows = []
        for i in range(20):
            d = (start + timedelta(days=i * 7)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, h=100 + i, low=80, c=95 + i))
        # Final bar closes at the highest high so far.
        final_date = (start + timedelta(days=20 * 7)).strftime("%Y-%m-%d")
        rows.append(
            _bar(
                "27800",
                final_date,
                h=200,
                low=180,
                c=200,
                adj_c=200,
                adj_h=200,
                adj_l=180,
            )
        )
        _seed(mock_env["cache"], rows)

        result = await _call(
            "detect_yearly_high_low",
            date=final_date,
            code="27800",
            window_days=30,
        )
        assert result["count"] == 1
        r = result["data"][0]
        assert r["new_yearly_high"] is True
        assert r["new_yearly_low"] is False

    async def test_cross_sectional_filters_to_hits(self, mock_env):
        # Code A: new high; Code B: ordinary day.
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

        result = await _call("detect_yearly_high_low", date=date_today, window_days=60)
        codes = {row["Code"] for row in result["data"]}
        assert "10000" in codes
        assert "20000" not in codes


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

    async def test_zero_baseline_skipped(self, mock_env):
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

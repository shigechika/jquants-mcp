"""Tests for get_technical_indicators and cache.technical helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.cache.technical import compute_bb, compute_rsi, compute_sma
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_env(tmp_path):
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
    result = await server_module.mcp.call_tool(tool_name, kwargs)
    return json.loads(result.content[0].text)


def _bar(code: str, date: str, *, c: float = 100.0, adj_c: float | None = None) -> dict:
    a = adj_c if adj_c is not None else c
    return {
        "Code": code,
        "Date": date,
        "O": c,
        "H": c,
        "L": c,
        "C": c,
        "UL": 0,
        "LL": 0,
        "Vo": 1000,
        "Va": c * 1000,
        "AdjFactor": 1.0,
        "AdjO": a,
        "AdjH": a,
        "AdjL": a,
        "AdjC": a,
        "AdjVo": 1000,
    }


def _seed(cache: CacheStore, rows: list[dict]) -> None:
    cache.put_rows(
        "equities_bars_daily",
        rows,
        key_columns=["Code", "Date"],
        adj_factor_key="AdjFactor",
    )


# ---------------------------------------------------------------------------
# Unit tests: cache.technical helpers
# ---------------------------------------------------------------------------


class TestComputeSma:
    def test_basic(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = compute_sma(values, 3)
        assert result[0] is None
        assert result[1] is None
        assert result[2] == pytest.approx(2.0)
        assert result[3] == pytest.approx(3.0)
        assert result[4] == pytest.approx(4.0)

    def test_period_1_returns_self(self):
        values = [10.0, 20.0, 30.0]
        assert compute_sma(values, 1) == pytest.approx([10.0, 20.0, 30.0])

    def test_empty(self):
        assert compute_sma([], 5) == []


class TestComputeBb:
    def test_structure(self):
        values = [float(i) for i in range(1, 25)]
        mid, upper, lower = compute_bb(values, period=20)
        # First 19 positions should be None
        assert all(v is None for v in mid[:19])
        assert mid[19] is not None
        assert upper[19] is not None
        assert lower[19] is not None
        # upper > mid > lower
        assert upper[19] > mid[19] > lower[19]

    def test_flat_prices_zero_band(self):
        values = [100.0] * 25
        mid, upper, lower = compute_bb(values, period=20)
        assert mid[19] == pytest.approx(100.0)
        # std of constant series is 0 → bands collapse to mid
        assert upper[19] == pytest.approx(100.0)
        assert lower[19] == pytest.approx(100.0)

    def test_matches_pandas(self):
        """Sample-std (ddof=1) result matches pandas .rolling().std()."""
        import statistics

        values = [
            100.0,
            102.0,
            98.0,
            105.0,
            99.0,
            103.0,
            101.0,
            97.0,
            104.0,
            100.0,
            102.0,
            98.0,
            105.0,
            99.0,
            103.0,
            101.0,
            97.0,
            104.0,
            100.0,
            102.0,
        ]
        mid, upper, lower = compute_bb(values, period=20)
        m = statistics.mean(values)
        s = statistics.stdev(values)  # sample std (ddof=1)
        assert mid[19] == pytest.approx(m)
        assert upper[19] == pytest.approx(m + 2 * s)
        assert lower[19] == pytest.approx(m - 2 * s)


class TestComputeRsi:
    def test_insufficient_data_returns_all_none(self):
        assert all(v is None for v in compute_rsi([100.0] * 14, 14))

    def test_constant_prices_rsi_undefined(self):
        # All gains and losses are zero → avg_loss stays 0 → RSI = 100
        result = compute_rsi([100.0] * 20, 14)
        assert result[14] == pytest.approx(100.0)

    def test_monotone_rising_gives_high_rsi(self):
        values = [float(i) for i in range(1, 30)]
        result = compute_rsi(values, 14)
        # All moves are gains, no losses → RSI = 100
        assert result[14] == pytest.approx(100.0)

    def test_monotone_falling_gives_low_rsi(self):
        values = [float(30 - i) for i in range(30)]
        result = compute_rsi(values, 14)
        # All moves are losses, no gains → RSI = 0
        assert result[14] == pytest.approx(0.0)

    def test_rsi_in_range(self):
        import random

        random.seed(42)
        values = [100.0 + random.uniform(-5, 5) for _ in range(50)]
        for v in compute_rsi(values, 14):
            if v is not None:
                assert 0.0 <= v <= 100.0


# ---------------------------------------------------------------------------
# Integration tests: get_technical_indicators tool
# ---------------------------------------------------------------------------


class TestGetTechnicalIndicators:
    def _make_rows(self, code: str, start_price: float = 100.0, n: int = 30) -> list[dict]:
        """Generate n rows with incrementally rising prices from 2025-01-06."""
        from datetime import date, timedelta

        base = date(2025, 1, 6)
        rows = []
        for i in range(n):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar(code, d, c=start_price + i))
        return rows

    async def test_requires_date_or_range(self, mock_env):
        result = await _call("get_technical_indicators", code="27800")
        assert result["error"] is True
        assert result["error_type"] == "ValidationError"

    async def test_unknown_indicator(self, mock_env):
        result = await _call(
            "get_technical_indicators", code="27800", date="2025-02-10", indicators=["sma99"]
        )
        assert result["error"] is True
        assert result["error_type"] == "ValidationError"

    async def test_sma5_computed_correctly(self, mock_env):
        rows = self._make_rows("27800", start_price=100.0, n=30)
        _seed(mock_env["cache"], rows)
        result = await _call(
            "get_technical_indicators",
            code="27800",
            date="2025-02-04",  # 30th day (index 29)
            indicators=["sma5"],
        )
        assert result["count"] == 1
        row = result["data"][0]
        assert row["sma5"] is not None
        # start_price=100, day 0=2025-01-06. Day 29=2025-02-04: price=129.
        # Days 25-29: 125,126,127,128,129 → mean = 127.0
        assert row["sma5"] == pytest.approx(127.0)

    async def test_bb20_keys_present(self, mock_env):
        rows = self._make_rows("27800", n=30)
        _seed(mock_env["cache"], rows)
        result = await _call(
            "get_technical_indicators",
            code="27800",
            date="2025-02-04",
            indicators=["bb20"],
        )
        row = result["data"][0]
        assert "bb20_mid" in row
        assert "bb20_upper" in row
        assert "bb20_lower" in row
        assert row["bb20_upper"] > row["bb20_mid"] > row["bb20_lower"]

    async def test_rsi14_in_range(self, mock_env):
        rows = self._make_rows("27800", n=30)
        _seed(mock_env["cache"], rows)
        result = await _call(
            "get_technical_indicators",
            code="27800",
            date="2025-02-04",
            indicators=["rsi14"],
        )
        row = result["data"][0]
        assert row["rsi14"] is not None
        assert 0 <= row["rsi14"] <= 100

    async def test_date_range_returns_multiple_rows(self, mock_env):
        rows = self._make_rows("27800", n=30)
        _seed(mock_env["cache"], rows)
        result = await _call(
            "get_technical_indicators",
            code="27800",
            date_from="2025-02-01",
            date_to="2025-02-04",
            indicators=["sma5"],
        )
        assert result["count"] == 4

    async def test_empty_cache_no_api_key_returns_empty(self, mock_env):
        """Cache miss with no API fallback data → returns empty."""
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=[]
        ):
            result = await _call("get_technical_indicators", code="27800", date="2025-02-04")
        assert result["count"] == 0

    async def test_api_fallback_on_cache_miss(self, mock_env):
        """Cache miss triggers API fetch and returns indicator values."""
        api_rows = self._make_rows("27800", n=30)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            return_value=api_rows,
        ) as mock_api:
            result = await _call(
                "get_technical_indicators",
                code="27800",
                date="2025-02-04",
                indicators=["sma5"],
            )
        mock_api.assert_called_once()
        assert result["count"] == 1
        assert result["data"][0]["sma5"] is not None

    async def test_warmup_rows_not_in_output(self, mock_env):
        """Rows fetched for warmup are not included in the returned data."""
        rows = self._make_rows("27800", n=30)
        _seed(mock_env["cache"], rows)
        result = await _call(
            "get_technical_indicators",
            code="27800",
            date="2025-02-04",  # only this date
            indicators=["sma5"],
        )
        # Only the requested date should be in output
        assert result["count"] == 1
        assert result["data"][0]["Date"] == "2025-02-04"

"""Tests for chart-rendering tools."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings

# Skip the entire module when the optional [charts] extras are missing.
mplfinance = pytest.importorskip("mplfinance")


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


def _bar(
    code: str,
    date: str,
    *,
    o: float = 100.0,
    h: float = 110.0,
    low: float = 90.0,
    c: float = 105.0,
    vo: float = 1_000.0,
    adj_factor: float = 1.0,
) -> dict:
    return {
        "Code": code,
        "Date": date,
        "O": o,
        "H": h,
        "L": low,
        "C": c,
        "Vo": vo,
        "Va": c * vo,
        "AdjFactor": adj_factor,
        "AdjO": o,
        "AdjH": h,
        "AdjL": low,
        "AdjC": c,
        "AdjVo": vo,
    }


def _seed(cache: CacheStore, rows: list[dict]) -> None:
    cache.put_rows(
        "equities_bars_daily",
        rows,
        key_columns=["Code", "Date"],
        adj_factor_key="AdjFactor",
    )


async def _call_image(tool: str, **kwargs) -> bytes:
    """Invoke a chart tool and return the PNG bytes."""
    result = await server_module.mcp.call_tool(tool, kwargs)
    # FastMCP wraps the Image return as ImageContent (base64 in .data).
    blob = result.content[0]
    raw = blob.data
    if isinstance(raw, str):
        import base64

        raw = base64.b64decode(raw)
    return raw


def _is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


class TestRenderCandlestick:
    async def test_returns_png_image(self, mock_env):
        rows = []
        for i in range(40):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, o=100 + i, h=110 + i, low=95 + i, c=105 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-02-13",
        )
        assert _is_png(png)
        # Sanity: the rendered chart is a non-trivial size.
        assert 5_000 < len(png) < 500_000

    async def test_with_indicators(self, mock_env):
        rows = []
        for i in range(60):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append(_bar("27800", d, c=100 + i))
        _seed(mock_env["cache"], rows)

        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-03-05",
            indicators=["volume", "sma20", "bb20"],
        )
        assert _is_png(png)

    async def test_unknown_indicator_returns_error_image(self, mock_env):
        # Even a validation error should return a PNG (the contract is
        # "always return an Image"), not a dict.
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-10",
            indicators=["bogus"],
        )
        assert _is_png(png)

    async def test_unknown_style_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-10",
            style="rainbow",
        )
        assert _is_png(png)

    async def test_empty_cache_returns_error_image(self, mock_env):
        # No bars seeded — should produce an error PNG explaining how
        # to populate the cache.
        png = await _call_image(
            "render_candlestick",
            code="99999",
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_png(png)

    async def test_inverted_date_range_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-02-01",
            to_date="2026-01-01",
        )
        assert _is_png(png)

    async def test_validation_error_returns_error_image(self, mock_env):
        png = await _call_image(
            "render_candlestick",
            code="bad",
            from_date="2026-01-05",
            to_date="2026-01-10",
        )
        assert _is_png(png)

    async def test_unadjusted_uses_raw_columns(self, mock_env):
        # Seed bars where adjusted prices differ from raw to confirm
        # the `adjusted=False` branch picks up the raw columns.
        rows = []
        for i in range(20):
            d = (datetime(2026, 1, 5) + timedelta(days=i)).strftime("%Y-%m-%d")
            r = _bar("27800", d, o=100, h=110, low=90, c=105)
            # Adjust the adjusted columns separately.
            r["AdjO"] = 50
            r["AdjH"] = 55
            r["AdjL"] = 45
            r["AdjC"] = 52
            rows.append(r)
        _seed(mock_env["cache"], rows)

        png_adj = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-24",
            adjusted=True,
        )
        png_raw = await _call_image(
            "render_candlestick",
            code="27800",
            from_date="2026-01-05",
            to_date="2026-01-24",
            adjusted=False,
        )
        assert _is_png(png_adj)
        assert _is_png(png_raw)
        # The two charts should differ — the adjusted one uses 50/55/45/52
        # while the raw one uses 100/110/90/105.
        assert png_adj != png_raw


def test_register_no_op_when_extras_missing():
    """register() should silently skip when mplfinance isn't importable.

    Simulated by patching the import to raise. Confirms the lean stdio
    profile (no [charts] extras) starts cleanly.
    """
    import builtins
    from unittest.mock import MagicMock

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mplfinance":
            raise ModuleNotFoundError("simulated missing mplfinance")
        return real_import(name, *args, **kwargs)

    from jquants_mcp.tools import charts as charts_module

    mcp_stub = MagicMock()
    with patch.object(builtins, "__import__", side_effect=fake_import):
        # Should not raise.
        charts_module.register(mcp_stub, lambda: None, lambda: None)
    # And the tool decorator was never invoked.
    mcp_stub.tool.assert_not_called()

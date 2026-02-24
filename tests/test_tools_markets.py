"""Tests for market tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_dat_mcp.server as server_module
from jquants_dat_mcp.cache.store import CacheStore
from jquants_dat_mcp.client import JQuantsClient
from jquants_dat_mcp.config import Settings
from jquants_dat_mcp.exceptions import APIError


@pytest.fixture()
def mock_env(tmp_path):
    """server.py のグローバル変数を直接差し替えるフィクスチャ。"""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db")

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


async def _call(tool_name: str, **kwargs) -> dict:
    """FastMCP v3 の call_tool でツールを呼び出し、JSON を dict で返す。"""
    result = await server_module.mcp.call_tool(tool_name, kwargs)
    return json.loads(result.content[0].text)


class TestGetMarketsMarginInterest:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000, "ShortBalance": 500000},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_markets_margin_interest", code="72030")
            assert result["count"] == 1
            assert result["data"][0]["LoanBalance"] == 1000000

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_markets_margin_interest", code="72030")
            result = await _call("get_markets_margin_interest", code="72030")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_markets_margin_interest", code="99999")
            assert result["error"] is True


class TestGetMarketsMarginAlert:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "AlertType": "1"},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_markets_margin_alert", code="72030")
            assert result["count"] == 1

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "AlertType": "1"},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_markets_margin_alert", code="72030")
            result = await _call("get_markets_margin_alert", code="72030")
            assert result["count"] == 1
            assert mock_fn.call_count == 1


class TestGetMarketsShortRatio:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "S33": "0050", "ShortSaleRatio": 40.5},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_markets_short_ratio", s33="0050", date="2024-01-04")
            assert result["count"] == 1
            assert result["data"][0]["ShortSaleRatio"] == 40.5

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "S33": "0050", "ShortSaleRatio": 40.5},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_markets_short_ratio", s33="0050", date="2024-01-04")
            result = await _call("get_markets_short_ratio", s33="0050", date="2024-01-04")
            assert result["count"] == 1
            assert mock_fn.call_count == 1


class TestGetMarketsShortSaleReport:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-01-04",
                "CalcDate": "2024-01-03",
                "ShortPosition": 0.52,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_markets_short_sale_report", code="72030")
            assert result["count"] == 1
            assert result["data"][0]["ShortPosition"] == 0.52

    async def test_with_disc_date_range(self, mock_env):
        mock_data = [
            {"Code": "72030", "DiscDate": "2024-01-04", "ShortPosition": 0.52},
            {"Code": "72030", "DiscDate": "2024-01-05", "ShortPosition": 0.48},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_markets_short_sale_report",
                code="72030",
                disc_date_from="2024-01-04",
                disc_date_to="2024-01-05",
            )
            assert result["count"] == 2

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Code": "72030", "DiscDate": "2024-01-04", "ShortPosition": 0.52},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_markets_short_sale_report", code="72030")
            result = await _call("get_markets_short_sale_report", code="72030")
            assert result["count"] == 1
            assert mock_fn.call_count == 1


class TestGetMarketsBreakdown:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "FrgnBuy": 500000, "FrgnSell": 300000},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_markets_breakdown", code="72030", date="2024-01-04")
            assert result["count"] == 1
            assert result["data"][0]["FrgnBuy"] == 500000

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "FrgnBuy": 500000},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_markets_breakdown", code="72030", date="2024-01-04")
            result = await _call("get_markets_breakdown", code="72030", date="2024-01-04")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=403),
        ):
            result = await _call("get_markets_breakdown", code="72030")
            assert result["error"] is True


class TestGetMarketsCalendar:
    async def test_returns_data(self, mock_env):
        mock_response = {
            "data": [
                {"Date": "2024-01-04", "HolDiv": "0"},
                {"Date": "2024-01-05", "HolDiv": "0"},
                {"Date": "2024-01-06", "HolDiv": "1"},
            ],
        }
        with patch.object(
            mock_env["client"], "get", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await _call("get_markets_calendar")
            assert result["count"] == 3

    async def test_with_date_range(self, mock_env):
        mock_response = {
            "data": [
                {"Date": "2024-01-04", "HolDiv": "0"},
                {"Date": "2024-01-05", "HolDiv": "0"},
            ],
        }
        with patch.object(
            mock_env["client"], "get", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await _call(
                "get_markets_calendar",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            assert result["count"] == 2

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_response = {
            "data": [
                {"Date": "2024-01-04", "HolDiv": "0"},
            ],
        }
        mock_fn = AsyncMock(return_value=mock_response)
        with patch.object(mock_env["client"], "get", mock_fn):
            await _call("get_markets_calendar", date_from="2024-01-04", date_to="2024-01-04")
            result = await _call(
                "get_markets_calendar", date_from="2024-01-04", date_to="2024-01-04"
            )
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_markets_calendar")
            assert result["error"] is True

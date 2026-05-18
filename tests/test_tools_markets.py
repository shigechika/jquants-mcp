"""Tests for market tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings
from jquants_mcp.exceptions import APIError, PlanRestrictionError


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
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)

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

    async def test_tier1_cache_incremental(self, mock_env):
        """Tier 1 キャッシュ: 2回目の呼び出しは増分取得。"""
        data_day1 = [
            {"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000},
        ]
        data_day2 = [
            {"Code": "72030", "Date": "2024-01-05", "LoanBalance": 1100000},
        ]
        mock_fn = AsyncMock(side_effect=[data_day1, data_day2])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result1 = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            assert result1["count"] == 1

            result2 = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            # 2回目: キャッシュ + API マージ
            assert result2["count"] == 2
            assert mock_fn.call_count == 2

    async def test_tier1_cache_full_hit(self, mock_env):
        """Tier 1 キャッシュ: 全期間キャッシュ済みなら API 呼び出しなし。"""
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000},
            {"Code": "72030", "Date": "2024-01-05", "LoanBalance": 1100000},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            result = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            assert result["count"] == 2
            assert result["source"] == "cache"
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

    async def test_no_params_api_failure_falls_back_to_tier1(self, mock_env):
        """No-params path: API fails (e.g. PlanRestrictionError) → returns Tier 1 snapshot."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        seed_rows = [
            {"Code": "72030", "Date": recent_date, "LoanBalance": 9999},
            {"Code": "72031", "Date": recent_date, "LoanBalance": 8888},
        ]
        mock_env["cache"].put_rows(
            "markets_margin_interest",
            seed_rows,
            key_columns=["Code", "Date"],
        )
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_interest")
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["source"] == "cache"

    async def test_no_params_api_failure_no_tier1_returns_error(self, mock_env):
        """No-params path: API fails and Tier 1 is empty → returns error dict."""
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_interest")
        assert result["error"] is True


class TestGetMarketsMarginAlert:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Code": "72030", "PubDate": "2024-01-04", "AlertType": "1"},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_markets_margin_alert", code="72030")
            assert result["count"] == 1

    async def test_tier1_cache_full_hit(self, mock_env):
        mock_data = [
            {"Code": "72030", "PubDate": "2024-01-04", "AlertType": "1"},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_markets_margin_alert",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            result = await _call(
                "get_markets_margin_alert",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            assert result["count"] == 1
            assert result["source"] == "cache"
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

    async def test_tier1_cache_full_hit(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "S33": "0050", "ShortSaleRatio": 40.5},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_markets_short_ratio",
                s33="0050",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            result = await _call(
                "get_markets_short_ratio",
                s33="0050",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            assert result["count"] == 1
            assert result["source"] == "cache"
            assert mock_fn.call_count == 1

    async def test_no_params_api_failure_falls_back_to_tier1(self, mock_env):
        """No-params path: API fails → returns Tier 1 snapshot."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        seed_rows = [
            {"Date": recent_date, "S33": "0050", "ShortSaleRatio": 40.5},
            {"Date": recent_date, "S33": "0100", "ShortSaleRatio": 35.2},
        ]
        mock_env["cache"].put_rows(
            "markets_short_ratio",
            seed_rows,
            key_columns=["S33", "Date"],
        )
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_short_ratio")
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["source"] == "cache"

    async def test_no_params_api_failure_no_tier1_returns_error(self, mock_env):
        """No-params path: API fails and Tier 1 is empty → returns error dict."""
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_short_ratio")
        assert result["error"] is True


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

    async def test_tier1_cache_full_hit(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "FrgnBuy": 500000},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_markets_breakdown",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            result = await _call(
                "get_markets_breakdown",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            assert result["count"] == 1
            assert result["source"] == "cache"
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

    async def test_tier1_cache_full_hit(self, mock_env):
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
            assert result["source"] == "cache"
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

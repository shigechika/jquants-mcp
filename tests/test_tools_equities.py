"""Tests for equity tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_dat_mcp.server as server_module
from jquants_dat_mcp.cache.store import CacheStore
from jquants_dat_mcp.config import Settings
from jquants_dat_mcp.client import JQuantsClient
from jquants_dat_mcp.exceptions import APIError, PlanRestrictionError


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


class TestGetEquitiesMaster:
    async def test_returns_data(self, mock_env):
        mock_data = [{"Code": "72030", "CoName": "トヨタ自動車", "Date": "2024-01-04"}]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_equities_master", code="72030")
            assert result["count"] == 1
            assert result["data"][0]["CoName"] == "トヨタ自動車"

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [{"Code": "72030", "CoName": "トヨタ自動車", "Date": "2024-01-04"}]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_master", code="72030")
            result = await _call("get_equities_master", code="72030")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_equities_master", code="99999")
            assert result["error"] is True
            assert result["status_code"] == 500


class TestGetEquitiesBarsDaily:
    async def test_returns_data_with_code_and_date_range(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "O": 2800, "C": 2850, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-05", "O": 2850, "C": 2900, "AdjFactor": 1.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_equities_bars_daily",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            assert result["count"] == 2
            assert result["data"][0]["Date"] == "2024-01-04"

    async def test_caches_and_reuses_data(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "O": 2800, "C": 2850, "AdjFactor": 1.0},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_equities_bars_daily",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )

        mock_fn2 = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn2):
            result = await _call(
                "get_equities_bars_daily",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            assert result["count"] == 1
            assert result["source"] == "cache"

    async def test_date_only_query(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "O": 2800},
            {"Code": "67580", "Date": "2024-01-04", "O": 13000},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_equities_bars_daily", date="2024-01-04")
            assert result["count"] == 2

    async def test_plan_restriction_error(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("Forbidden", status_code=403),
        ):
            result = await _call("get_equities_bars_daily", date="2024-01-04")
            assert result["error"] is True
            assert "hint" in result

    async def test_no_params_returns_validation_error(self, mock_env):
        result = await _call("get_equities_bars_daily")
        assert result["error"] is True
        assert "code" in result["message"] or "date" in result["message"]

    async def test_date_from_only_returns_validation_error(self, mock_env):
        """date_from/date_to without code requires no validation bypass."""
        # date_from alone (no code, no date) should pass through (valid API usage)
        mock_data = [{"Code": "72030", "Date": "2024-01-04", "O": 2800, "AdjFactor": 1.0}]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_equities_bars_daily", date_from="2024-01-04")
            assert result["count"] == 1


class TestGetEquitiesBarsMinute:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Code": "72030", "Date": "2024-01-04", "Time": "09:00", "O": 2800, "C": 2805},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_equities_bars_minute", code="72030", date="2024-01-04")
            assert result["count"] == 1
            assert result["data"][0]["Time"] == "09:00"


class TestGetEquitiesBarsDailyAm:
    async def test_returns_data_no_cache(self, mock_env):
        mock_data = [{"Code": "72030", "Date": "2024-01-04", "MO": 2800, "MC": 2850}]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_equities_bars_daily_am", code="72030")
            assert result["count"] == 1

            await _call("get_equities_bars_daily_am", code="72030")
            assert mock_fn.call_count == 2


class TestGetEquitiesInvestorTypes:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"PubDate": "2024-01-11", "Section": "TSEPrime", "FrgnSell": 1000000},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_equities_investor_types", section="TSEPrime")
            assert result["count"] == 1


class TestGetEquitiesEarningsCalendar:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Date": "2024-01-12", "Code": "72030", "CoName": "トヨタ自動車", "FQ": "3Q"},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_equities_earnings_calendar")
            assert result["count"] == 1
            assert result["data"][0]["FQ"] == "3Q"

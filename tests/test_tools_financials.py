"""Tests for financial tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_dat_mcp.server as server_module
from jquants_dat_mcp.cache.store import CacheStore
from jquants_dat_mcp.config import Settings
from jquants_dat_mcp.client import JQuantsClient
from jquants_dat_mcp.exceptions import PlanRestrictionError


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


class TestGetFinsSummary:
    async def test_returns_data_with_code(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-02-06",
                "DiscNo": "001",
                "Sales": 37154290000000,
                "OP": 3952696000000,
                "NP": 3940992000000,
                "EPS": 297.3,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["count"] == 1
            assert result["data"][0]["EPS"] == 297.3

    async def test_caches_by_code(self, mock_env):
        mock_data = [
            {"Code": "72030", "DiscDate": "2024-02-06", "DiscNo": "001", "Sales": 100},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_fins_summary", code="72030", date="2024-02-06")

        mock_fn2 = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn2):
            result = await _call("get_fins_summary", code="72030", date="2024-02-06")
            assert result["count"] == 1
            assert result["source"] == "cache"

    async def test_date_only_uses_tier2(self, mock_env):
        mock_data = [
            {"Code": "72030", "DiscDate": "2024-02-06", "Sales": 100},
            {"Code": "67580", "DiscDate": "2024-02-06", "Sales": 200},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_fins_summary", date="2024-02-06")
            assert result["count"] == 2

            result2 = await _call("get_fins_summary", date="2024-02-06")
            assert result2["count"] == 2
            assert mock_fn.call_count == 1


class TestGetFinsDetails:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-02-06",
                "FS": {"CurrentAssets": 1000000},
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_details", code="72030")
            assert result["count"] == 1
            assert "FS" in result["data"][0]

    async def test_plan_restriction(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("Forbidden", status_code=403),
        ):
            result = await _call("get_fins_details", code="72030")
            assert result["error"] is True
            assert "プラン" in result["hint"]


class TestGetFinsDividend:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "PubDate": "2024-02-06",
                "DivRate": 40.0,
                "ExDate": "2024-03-28",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_dividend", code="72030")
            assert result["count"] == 1
            assert result["data"][0]["DivRate"] == 40.0

    async def test_with_date_range(self, mock_env):
        mock_data = [
            {"Code": "72030", "PubDate": "2024-02-06", "DivRate": 40.0},
            {"Code": "72030", "PubDate": "2024-08-01", "DivRate": 40.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_fins_dividend",
                code="72030",
                date_from="2024-01-01",
                date_to="2024-12-31",
            )
            assert result["count"] == 2

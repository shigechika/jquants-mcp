"""Tests for index tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings
from jquants_mcp.exceptions import APIError


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


class TestGetIndicesBarsDaily:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Code": "0000",
                "Date": "2024-01-04",
                "O": 2500.0,
                "H": 2520.0,
                "L": 2480.0,
                "C": 2510.0,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_indices_bars_daily", code="0000", date="2024-01-04")
            assert result["count"] == 1
            assert result["data"][0]["Code"] == "0000"

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Code": "0000", "Date": "2024-01-04", "O": 2500.0, "C": 2510.0},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_indices_bars_daily", code="0000", date="2024-01-04")
            result = await _call("get_indices_bars_daily", code="0000", date="2024-01-04")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_with_date_range(self, mock_env):
        mock_data = [
            {"Code": "0010", "Date": "2024-01-04", "C": 33000.0},
            {"Code": "0010", "Date": "2024-01-05", "C": 33100.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_indices_bars_daily",
                code="0010",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            assert result["count"] == 2

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_indices_bars_daily", code="0000")
            assert result["error"] is True
            assert result["status_code"] == 500


class TestGetIndicesBarsDailyTopix:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "O": 2500.0, "H": 2520.0, "L": 2480.0, "C": 2510.0},
            {"Date": "2024-01-05", "O": 2510.0, "H": 2530.0, "L": 2490.0, "C": 2520.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_indices_bars_daily_topix",
                date_from="2024-01-04",
                date_to="2024-01-05",
            )
            assert result["count"] == 2
            assert result["data"][0]["Date"] == "2024-01-04"

    async def test_caches_and_reuses_data(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "O": 2500.0, "C": 2510.0},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_indices_bars_daily_topix",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )

        # 2回目: キャッシュ済みデータのみ返る
        mock_fn2 = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn2):
            result = await _call(
                "get_indices_bars_daily_topix",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )
            assert result["count"] == 1
            assert result["source"] == "cache"

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_indices_bars_daily_topix")
            assert result["error"] is True

"""Tests for derivative tools."""

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


class TestGetDerivativesBarsDailyFutures:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Date": "2024-01-04",
                "Category": "Futures225",
                "O": 33000.0,
                "H": 33200.0,
                "L": 32900.0,
                "C": 33100.0,
                "Volume": 50000,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_derivatives_bars_daily_futures", date="2024-01-04")
            assert result["count"] == 1
            assert result["data"][0]["Category"] == "Futures225"

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "Category": "Futures225", "C": 33100.0},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_derivatives_bars_daily_futures", date="2024-01-04")
            result = await _call("get_derivatives_bars_daily_futures", date="2024-01-04")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_with_category_filter(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "Category": "FuturesTOPIX", "C": 2400.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_derivatives_bars_daily_futures",
                date="2024-01-04",
                category="FuturesTOPIX",
            )
            assert result["count"] == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_derivatives_bars_daily_futures", date="2024-01-04")
            assert result["error"] is True
            assert result["status_code"] == 500


class TestGetDerivativesBarsDailyOptions:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Date": "2024-01-04",
                "Category": "Options225",
                "Code": "13320240200C33000",
                "O": 500.0,
                "C": 520.0,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_derivatives_bars_daily_options", date="2024-01-04")
            assert result["count"] == 1
            assert result["data"][0]["Category"] == "Options225"

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "Category": "Options225", "C": 520.0},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_derivatives_bars_daily_options", date="2024-01-04")
            result = await _call("get_derivatives_bars_daily_options", date="2024-01-04")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_with_code_filter(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "Code": "13320240200C33000", "C": 520.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call(
                "get_derivatives_bars_daily_options",
                date="2024-01-04",
                code="13320240200C33000",
            )
            assert result["count"] == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=403),
        ):
            result = await _call("get_derivatives_bars_daily_options", date="2024-01-04")
            assert result["error"] is True


class TestGetDerivativesBarsDailyOptions225:
    async def test_returns_data(self, mock_env):
        mock_data = [
            {
                "Date": "2024-01-04",
                "Code": "13320240200C33000",
                "O": 500.0,
                "C": 520.0,
                "Volume": 1000,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_derivatives_bars_daily_options_225", date="2024-01-04")
            assert result["count"] == 1

    async def test_uses_cache_on_second_call(self, mock_env):
        mock_data = [
            {"Date": "2024-01-04", "Code": "13320240200C33000", "C": 520.0},
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_derivatives_bars_daily_options_225", date="2024-01-04")
            result = await _call("get_derivatives_bars_daily_options_225", date="2024-01-04")
            assert result["count"] == 1
            assert mock_fn.call_count == 1

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_derivatives_bars_daily_options_225", date="2024-01-04")
            assert result["error"] is True

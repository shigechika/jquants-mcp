"""Tests for bulk download tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings
from jquants_mcp.client import JQuantsClient
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


class TestGetBulkList:
    async def test_returns_file_list(self, mock_env):
        mock_data = [
            {
                "Key": "equities/bars/daily/2024/01/04.csv.gz",
                "LastModified": "2024-01-05T06:00:00Z",
                "Size": 1234567,
            },
            {
                "Key": "equities/bars/daily/2024/01/05.csv.gz",
                "LastModified": "2024-01-06T06:00:00Z",
                "Size": 1234568,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_bulk_list", endpoint="/equities/bars/daily")
            assert result["count"] == 2
            assert result["data"][0]["Key"] == "equities/bars/daily/2024/01/04.csv.gz"

    async def test_caches_result(self, mock_env):
        mock_data = [
            {
                "Key": "fins/summary/2024/01.csv.gz",
                "LastModified": "2024-02-01T06:00:00Z",
                "Size": 500000,
            },
        ]
        mock_fn = AsyncMock(return_value=mock_data)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_bulk_list", endpoint="/fins/summary")

        mock_fn2 = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn2):
            result = await _call("get_bulk_list", endpoint="/fins/summary")
            assert result["count"] == 1
            mock_fn2.assert_not_called()

    async def test_invalid_endpoint_returns_error(self, mock_env):
        result = await _call("get_bulk_list", endpoint="/invalid/endpoint")
        assert result["error"] is True
        assert "Invalid endpoint" in result["message"]
        assert "hint" in result

    async def test_plan_restriction(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("Forbidden", status_code=403),
        ):
            result = await _call("get_bulk_list", endpoint="/equities/bars/daily")
            assert result["error"] is True
            assert "plan" in result["hint"].lower()

    async def test_api_error(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("サーバーエラー", status_code=500),
        ):
            result = await _call("get_bulk_list", endpoint="/equities/bars/daily")
            assert result["error"] is True
            assert result["status_code"] == 500


class TestGetBulkDownloadUrl:
    async def test_returns_url(self, mock_env):
        mock_response = {"url": "https://example.com/download/signed-url?token=abc123"}
        with patch.object(
            mock_env["client"], "get", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await _call(
                "get_bulk_download_url",
                key="equities/bars/daily/2024/01/04.csv.gz",
            )
            assert result["url"] == "https://example.com/download/signed-url?token=abc123"
            assert "expires" in result["hint"]

    async def test_not_cached(self, mock_env):
        """署名付き URL はキャッシュされないことを確認。"""
        mock_response = {"url": "https://example.com/url1"}
        mock_fn = AsyncMock(return_value=mock_response)
        with patch.object(mock_env["client"], "get", mock_fn):
            await _call("get_bulk_download_url", key="some/key.csv.gz")

        mock_response2 = {"url": "https://example.com/url2"}
        mock_fn2 = AsyncMock(return_value=mock_response2)
        with patch.object(mock_env["client"], "get", mock_fn2):
            result = await _call("get_bulk_download_url", key="some/key.csv.gz")
            assert result["url"] == "https://example.com/url2"
            mock_fn2.assert_called_once()

    async def test_plan_restriction(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("Forbidden", status_code=403),
        ):
            result = await _call("get_bulk_download_url", key="some/key.csv.gz")
            assert result["error"] is True

    async def test_api_error(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=APIError("Not Found", status_code=404),
        ):
            result = await _call("get_bulk_download_url", key="nonexistent/key.csv.gz")
            assert result["error"] is True
            assert result["status_code"] == 404

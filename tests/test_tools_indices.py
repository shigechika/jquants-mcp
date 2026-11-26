"""Tests for index tools."""

from __future__ import annotations

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
    _, structured = await server_module.mcp.call_tool(tool_name, kwargs)
    return structured


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


class TestTopixCacheCompleteness:
    """get_indices_bars_daily_topix uses the bars completeness proof (jquants-mcp#625)."""

    DAYS = ["2024-01-04", "2024-01-05", "2024-01-09", "2024-01-10"]  # 01-08 is a holiday

    @pytest.fixture()
    def calendar(self, mock_env):
        rows = [
            {"Date": f"2024-01-{d:02d}", "HolDiv": "1" if f"2024-01-{d:02d}" in self.DAYS else "0"}
            for d in range(4, 11)
        ]
        mock_env["cache"].put_rows("markets_calendar", rows, key_columns=["Date"])

    @staticmethod
    def _rows(days: list[str]) -> list[dict]:
        return [{"Date": d, "Close": 2500.0} for d in days]

    def _seed(self, mock_env, days: list[str]) -> None:
        mock_env["cache"].put_rows(
            "indices_bars_daily_topix", self._rows(days), key_columns=["Date"]
        )

    async def test_fully_cached_range_needs_no_api(self, mock_env, calendar):
        self._seed(mock_env, self.DAYS)
        mock_fn = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_indices_bars_daily_topix", date_from="2024-01-04", date_to="2024-01-10"
            )
        assert mock_fn.call_count == 0
        assert result["source"] == "cache"
        assert result["count"] == 4

    async def test_hole_in_the_range_refetches_the_whole_range(self, mock_env, calendar):
        """The old check saw the latest cached date reach date_to and returned the hole."""
        self._seed(mock_env, [self.DAYS[0], self.DAYS[3]])
        mock_fn = AsyncMock(return_value=self._rows(self.DAYS))
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_indices_bars_daily_topix", date_from="2024-01-04", date_to="2024-01-10"
            )
        assert mock_fn.call_args.args[1] == {"from": "2024-01-04", "to": "2024-01-10"}
        assert [r["Date"] for r in result["data"]] == self.DAYS

    async def test_missing_tail_fetches_only_the_tail(self, mock_env, calendar):
        self._seed(mock_env, self.DAYS[:2])
        mock_fn = AsyncMock(return_value=self._rows(self.DAYS[1:]))
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_indices_bars_daily_topix", date_from="2024-01-04", date_to="2024-01-10"
            )
        assert mock_fn.call_args.args[1]["from"] == "2024-01-05"
        assert [r["Date"] for r in result["data"]] == self.DAYS

    async def test_api_failure_returns_cached_rows_as_partial(self, mock_env, calendar):
        self._seed(mock_env, [self.DAYS[0], self.DAYS[3]])
        mock_fn = AsyncMock(side_effect=APIError("boom", status_code=500))
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_indices_bars_daily_topix", date_from="2024-01-04", date_to="2024-01-10"
            )
        assert result["count"] == 2
        assert result["partial"] is True

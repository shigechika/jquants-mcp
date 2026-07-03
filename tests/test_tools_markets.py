"""Tests for market tools."""

from __future__ import annotations

import json
from datetime import date, timedelta
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

    async def _seed_margin_interest(self, cache, recent_date):
        rows = [
            {"Code": "72030", "Date": recent_date, "LoanBalance": 9999},
            {"Code": "72031", "Date": recent_date, "LoanBalance": 8888},
        ]
        cache.put_rows("markets_margin_interest", rows, key_columns=["Code", "Date"])

    async def test_no_params_api_failure_falls_back_to_tier1(self, mock_env):
        """No-params + detail=True: API fails → returns full Tier 1 snapshot."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_margin_interest(mock_env["cache"], recent_date)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_interest", detail=True)
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["source"] == "cache"
        assert "data" in result

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

    async def test_no_params_detail_false_returns_summary(self, mock_env):
        """No-params + detail=False (default): returns summary without data rows."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_margin_interest(mock_env["cache"], recent_date)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_interest")
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["latest_date"] == recent_date
        assert result["source"] == "cache"
        assert "data" not in result
        assert "note" in result

    async def test_no_params_detail_true_returns_full_data(self, mock_env):
        """No-params + detail=True: returns full data rows."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_margin_interest(mock_env["cache"], recent_date)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_interest", detail=True)
        assert result.get("error") is not True
        assert "data" in result
        assert len(result["data"]) == 2


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

    async def test_no_params_api_failure_falls_back_to_tier1(self, mock_env):
        """No-params path: API fails → returns Tier 1 snapshot."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        mock_env["cache"].put_rows(
            "markets_margin_alert",
            [
                {"Code": "72030", "Date": recent_date, "AlertType": "1"},
                {"Code": "72031", "Date": recent_date, "AlertType": "2"},
            ],
            key_columns=["Code", "Date"],
        )
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_alert")
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["source"] == "cache"
        assert "data" in result

    async def test_no_params_api_failure_no_tier1_returns_error(self, mock_env):
        """No-params path: API fails and Tier 1 is empty → returns error dict."""
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_margin_alert")
        assert result.get("error") is True


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

    async def _seed_short_ratio(self, cache, recent_date):
        rows = [
            {"Date": recent_date, "S33": "0050", "ShortSaleRatio": 40.5},
            {"Date": recent_date, "S33": "0100", "ShortSaleRatio": 35.2},
        ]
        cache.put_rows("markets_short_ratio", rows, key_columns=["S33", "Date"])

    async def test_no_params_api_failure_falls_back_to_tier1(self, mock_env):
        """No-params + detail=True: API fails → returns full Tier 1 snapshot."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_short_ratio(mock_env["cache"], recent_date)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_short_ratio", detail=True)
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["source"] == "cache"
        assert "data" in result

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

    async def test_no_params_detail_false_returns_summary(self, mock_env):
        """No-params + detail=False (default): returns summary without data rows."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_short_ratio(mock_env["cache"], recent_date)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_short_ratio")
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["latest_date"] == recent_date
        assert result["source"] == "cache"
        assert "data" not in result
        assert "note" in result

    async def test_no_params_detail_true_returns_full_data(self, mock_env):
        """No-params + detail=True: returns full data rows."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_short_ratio(mock_env["cache"], recent_date)
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_short_ratio", detail=True)
        assert result.get("error") is not True
        assert "data" in result
        assert len(result["data"]) == 2


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

    async def test_no_params_api_failure_falls_back_to_tier1(self, mock_env):
        """No-params path: API fails → returns Tier 1 snapshot."""
        from datetime import date, timedelta

        recent_date = (date.today() - timedelta(days=3)).isoformat()
        mock_env["cache"].put_rows(
            "markets_breakdown",
            [
                {"Code": "72030", "Date": recent_date, "FrgnBuy": 500000},
                {"Code": "72031", "Date": recent_date, "FrgnBuy": 300000},
            ],
            key_columns=["Code", "Date"],
        )
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_breakdown")
        assert result.get("error") is not True
        assert result["count"] == 2
        assert result["source"] == "cache"
        assert "data" in result

    async def test_no_params_api_failure_no_tier1_returns_error(self, mock_env):
        """No-params path: API fails and Tier 1 is empty → returns error dict."""
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("plan restriction", status_code=403),
        ):
            result = await _call("get_markets_breakdown")
        assert result.get("error") is True


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

    async def test_free_plan_uses_cache_for_recent_dates(self, tmp_path):
        """markets_calendar reads must not be plan-clamped — a recent date
        range already cached is served from cache even for a Free-plan user
        (regression for the markets_calendar plan-clamp fix)."""
        settings = Settings(
            jquants_api_key="test-key",
            jquants_plan="free",
            jquants_cache_dir=str(tmp_path),
            max_retries=1,
            retry_base_delay=0.01,
        )
        client = JQuantsClient(settings)
        cache = CacheStore(tmp_path / "free.db", default_plan="free")
        recent = (date.today() - timedelta(weeks=2)).isoformat()  # embargoed under Free
        cache.put_rows("markets_calendar", [{"Date": recent, "HolDiv": "0"}], key_columns=["Date"])

        mock_fn = AsyncMock(side_effect=AssertionError("API must not be called on a cache hit"))
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_client", client),
            patch.object(server_module, "_cache", cache),
            patch.object(client, "get", mock_fn),
        ):
            result = await _call("get_markets_calendar", date_from=recent, date_to=recent)
        cache.close()
        assert result["count"] == 1
        assert result["source"] == "cache"

    async def test_api_error_falls_back_to_cache(self, mock_env):
        """On API failure, calendar data already in the cache must be
        returned instead of a hard error (regression for the missing
        APIError fallback in _get_calendar_with_cache)."""
        cache = mock_env["cache"]
        cache.put_rows(
            "markets_calendar", [{"Date": "2024-01-04", "HolDiv": "0"}], key_columns=["Date"]
        )
        with patch.object(
            mock_env["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            # Range not fully covered by the cached date, so the tool must
            # attempt the API call (and fall back) rather than early-return
            # on a full cache hit.
            result = await _call(
                "get_markets_calendar", date_from="2024-01-01", date_to="2024-01-10"
            )
        assert result.get("error") is not True
        assert result["source"] == "cache"
        assert result["count"] == 1

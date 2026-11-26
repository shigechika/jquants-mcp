"""Tests for market tools."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings
from jquants_mcp.exceptions import APIError, PlanRestrictionError, RateLimitError


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

    async def test_cached_range_still_asks_the_api(self, mock_env):
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
            # A cached range cannot be proven complete for these tables, so
            # it asks the API again; cached and API rows are merged
            assert result["source"] == "cache+api"
            assert mock_fn.call_count == 2

    async def test_api_error_returns_error_dict(self, mock_env):
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=APIError("テストエラー", status_code=500),
        ):
            result = await _call("get_markets_margin_interest", code="99999")
            assert result["error"] is True

    async def test_rate_limit_falls_back_to_tier1_cache(self, mock_env):
        mock_env["cache"].put_rows(
            "markets_margin_interest",
            [{"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000}],
            key_columns=["Code", "Date"],
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-10",
            )
        assert mock_fn.call_count == 1
        assert result["source"] == "cache"
        assert result["count"] == 1
        # Only 01-04 of 01-04..01-10 is cached: the answer is incomplete
        assert result["partial"] is True

    async def test_rate_limit_on_cached_single_date_returns_only_that_day(self, mock_env):
        mock_env["cache"].put_rows(
            "markets_margin_interest",
            [
                {"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000},
                {"Code": "72030", "Date": "2024-01-05", "LoanBalance": 1100000},
            ],
            key_columns=["Code", "Date"],
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_markets_margin_interest", code="72030", date="2024-01-04")
        assert [r["Date"] for r in result["data"]] == ["2024-01-04"]
        assert "partial" not in result

    async def test_compact_single_date_hits_the_cache(self, mock_env):
        mock_env["cache"].put_rows(
            "markets_margin_interest",
            [{"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000}],
            key_columns=["Code", "Date"],
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_markets_margin_interest", code="72030", date="20240104")
        assert [r["Date"] for r in result["data"]] == ["2024-01-04"]
        assert "partial" not in result
        # The API still gets the caller's date form
        assert mock_fn.call_args.args[1]["date"] == "20240104"

    @staticmethod
    def _q1_rows() -> list[dict]:
        return [
            {"Code": "72030", "Date": d, "LoanBalance": 1000000}
            for d in ("2024-01-05", "2024-02-15", "2024-03-29")
        ]

    async def test_range_with_only_the_end_cached_asks_the_api(self, mock_env):
        """Only 03-29 is cached: the old shortcut returned that one row for 01-04..03-29."""
        mock_env["cache"].put_rows(
            "markets_margin_interest", self._q1_rows()[-1:], key_columns=["Code", "Date"]
        )
        mock_fn = AsyncMock(return_value=self._q1_rows())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-03-29",
            )
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.args[1]["from"] == "2024-01-04"
        assert result["count"] == 3

    async def test_answer_cut_by_the_page_limit_is_flagged_partial(self, mock_env):
        """A date-wide range can exceed max_pages; the cut-off answer must say so."""
        from jquants_mcp.client import PagedRows

        rows = PagedRows(self._q1_rows()[:2])
        rows.truncated = True
        mock_fn = AsyncMock(return_value=rows)
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_margin_interest", date_from="2024-01-04", date_to="2024-03-29"
            )
        assert result["count"] == 2
        assert result["partial"] is True

    async def test_range_with_a_middle_day_cached_asks_from_the_start(self, mock_env):
        """Only 02-15 is cached: the old incremental fetch started there and lost January."""
        mock_env["cache"].put_rows(
            "markets_margin_interest", self._q1_rows()[1:2], key_columns=["Code", "Date"]
        )
        mock_fn = AsyncMock(return_value=self._q1_rows())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-03-29",
            )
        assert mock_fn.call_args.args[1]["from"] == "2024-01-04"
        assert [r["Date"] for r in result["data"]] == ["2024-01-05", "2024-02-15", "2024-03-29"]

    async def test_compact_range_behaves_like_iso(self, mock_env):
        """YYYYMMDD finds the cached rows and still asks the API in the caller's form."""
        mock_env["cache"].put_rows(
            "markets_margin_interest", self._q1_rows()[-1:], key_columns=["Code", "Date"]
        )
        mock_fn = AsyncMock(return_value=self._q1_rows()[:2])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_margin_interest",
                code="72030",
                date_from="20240104",
                date_to="20240329",
            )
        assert mock_fn.call_args.args[1] == {"code": "72030", "from": "20240104", "to": "20240329"}
        # API rows merged with the cached 03-29 row
        assert result["count"] == 3
        assert result["source"] == "cache+api"

    async def test_daily_era_code_range_is_proven_and_served_from_cache(self, mock_env):
        """Daily since 2026-09-25: a code has a row every trading day, so coverage is provable."""
        rows = [
            {"Code": "72030", "Date": d, "LoanBalance": 1000000}
            for d in ("2026-10-01", "2026-10-02")  # Thu, Fri
        ]
        mock_env["cache"].put_rows("markets_margin_interest", rows, key_columns=["Code", "Date"])
        mock_fn = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_margin_interest",
                code="7203",
                date_from="2026-10-01",
                date_to="2026-10-02",
            )
        assert mock_fn.call_count == 0
        assert result["source"] == "cache"
        assert result["count"] == 2

    async def test_no_params_summary_keeps_the_partial_flag(self, mock_env):
        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_margin_interest(mock_env["cache"], recent_date)
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_markets_margin_interest")
        assert "data" not in result
        assert result["partial"] is True

    async def test_rate_limit_on_date_only_query_is_partial(self, mock_env):
        # A per-stock lookup cached one code for the day; a date-only query
        # asks for every code
        mock_env["cache"].put_rows(
            "markets_margin_interest",
            [{"Code": "72030", "Date": "2024-01-04", "LoanBalance": 1000000}],
            key_columns=["Code", "Date"],
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_markets_margin_interest", date="2024-01-04")
        assert result["count"] == 1
        assert result["partial"] is True

    async def test_rate_limit_on_uncached_single_date_raises(self, mock_env):
        # Only a later day is cached: it must not stand in for the asked day
        mock_env["cache"].put_rows(
            "markets_margin_interest",
            [{"Code": "72030", "Date": "2024-01-05", "LoanBalance": 1100000}],
            key_columns=["Code", "Date"],
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            with pytest.raises(Exception, match="(?i)rate limit"):
                await _call("get_markets_margin_interest", code="72030", date="2024-01-04")

    async def test_rate_limit_without_cache_still_raises(self, mock_env):
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            with pytest.raises(Exception, match="(?i)rate limit"):
                await _call(
                    "get_markets_margin_interest",
                    code="72030",
                    date_from="2024-01-04",
                    date_to="2024-01-10",
                )

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

    async def test_no_params_rate_limit_falls_back_to_tier1(self, mock_env):
        recent_date = (date.today() - timedelta(days=3)).isoformat()
        await self._seed_margin_interest(mock_env["cache"], recent_date)
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_markets_margin_interest", detail=True)
        assert result["source"] == "cache"
        assert result["count"] == 2
        assert result["partial"] is True

    async def test_no_params_rate_limit_without_tier1_raises(self, mock_env):
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            with pytest.raises(Exception, match="(?i)rate limit"):
                await _call("get_markets_margin_interest")

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

    async def test_cached_range_still_asks_the_api(self, mock_env):
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
            # A cached range cannot be proven complete for these tables, so
            # it asks the API again; cached and API rows are merged
            assert result["source"] == "cache+api"
            assert mock_fn.call_count == 2

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

    async def test_covered_key_range_is_served_from_cache(self, mock_env):
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
            # One key has a row on every trading day here, so the covered
            # range is proven and needs no second API call
            assert result["source"] == "cache"
            assert mock_fn.call_count == 1

    @staticmethod
    def _sector_rows(days: list[str]) -> list[dict]:
        return [{"Date": d, "S33": "0050", "ShortSaleRatio": 40.5} for d in days]

    async def test_sector_range_with_a_hole_refetches_the_whole_range(self, mock_env):
        """The published cache has such a hole; the old check served it as complete."""
        days = ["2024-01-04", "2024-01-05", "2024-01-09", "2024-01-10"]  # 01-08: holiday
        mock_env["cache"].put_rows(
            "markets_calendar",
            [
                {"Date": f"2024-01-{d:02d}", "HolDiv": "1" if f"2024-01-{d:02d}" in days else "0"}
                for d in range(4, 11)
            ],
            key_columns=["Date"],
        )
        mock_env["cache"].put_rows(
            "markets_short_ratio",
            self._sector_rows([days[0], days[3]]),
            key_columns=["S33", "Date"],
        )
        mock_fn = AsyncMock(return_value=self._sector_rows(days))
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_markets_short_ratio", s33="0050", date_from="2024-01-04", date_to="2024-01-10"
            )
        assert mock_fn.call_args.args[1] == {
            "s33": "0050",
            "from": "2024-01-04",
            "to": "2024-01-10",
        }
        assert [r["Date"] for r in result["data"]] == days

    async def test_plan_restricted_user_gets_a_covered_sector_range_without_a_call(self, mock_env):
        """A plan without this endpoint: no doomed request, no partial flag, when covered."""
        days = ["2024-01-04", "2024-01-05"]
        mock_env["cache"].put_rows(
            "markets_short_ratio", self._sector_rows(days), key_columns=["S33", "Date"]
        )
        mock_fn = AsyncMock(side_effect=PlanRestrictionError("Forbidden", status_code=403))
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            covered = await _call(
                "get_markets_short_ratio", s33="0050", date_from="2024-01-04", date_to="2024-01-05"
            )
            # Every sector for the range cannot be proven: asks, fails, flags partial
            all_keys = await _call(
                "get_markets_short_ratio", date_from="2024-01-04", date_to="2024-01-05"
            )
        assert covered["source"] == "cache" and "partial" not in covered
        assert mock_fn.call_count == 1
        assert all_keys["partial"] is True

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

    async def test_covered_key_range_is_served_from_cache(self, mock_env):
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
            # One key has a row on every trading day here, so the covered
            # range is proven and needs no second API call
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
        """A range whose every calendar day is cached is served without the API."""
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

    async def test_range_with_a_missing_day_asks_for_the_whole_range(self, mock_env):
        """Only the last day is cached: the old check returned it as the full answer."""
        mock_env["cache"].put_rows(
            "markets_calendar", [{"Date": "2024-01-06", "HolDiv": "0"}], key_columns=["Date"]
        )
        full = {
            "data": [
                {"Date": "2024-01-04", "HolDiv": "1"},
                {"Date": "2024-01-05", "HolDiv": "1"},
                {"Date": "2024-01-06", "HolDiv": "0"},
            ]
        }
        mock_fn = AsyncMock(return_value=full)
        with patch.object(mock_env["client"], "get", mock_fn):
            # hol_div=1 filters the cached Saturday out; completeness is judged first
            result = await _call(
                "get_markets_calendar", hol_div="1", date_from="2024-01-04", date_to="2024-01-06"
            )
        # The API is asked for every day (no hol_div), so the range can become complete
        assert mock_fn.call_args.args[1] == {"from": "2024-01-04", "to": "2024-01-06"}
        assert [r["Date"] for r in result["data"]] == ["2024-01-04", "2024-01-05"]

    async def test_compact_dates_hit_a_fully_cached_range(self, mock_env):
        mock_env["cache"].put_rows(
            "markets_calendar",
            [{"Date": "2024-01-04", "HolDiv": "1"}, {"Date": "2024-01-05", "HolDiv": "1"}],
            key_columns=["Date"],
        )
        mock_fn = AsyncMock(return_value={"data": []})
        with patch.object(mock_env["client"], "get", mock_fn):
            result = await _call("get_markets_calendar", date_from="20240104", date_to="20240105")
        assert mock_fn.call_count == 0
        assert result["count"] == 2

    async def test_unsupported_date_form_is_rejected(self, mock_env):
        """An ISO week date would give the cache lookup and the check different bounds."""
        result = await _call("get_markets_calendar", date_from="2024-01-04", date_to="2024-W01-4")
        assert result["error"] is True

    async def test_open_ended_range_asks_the_api(self, mock_env):
        mock_env["cache"].put_rows(
            "markets_calendar", [{"Date": "2024-01-04", "HolDiv": "1"}], key_columns=["Date"]
        )
        mock_fn = AsyncMock(return_value={"data": [{"Date": "2024-01-04", "HolDiv": "1"}]})
        with patch.object(mock_env["client"], "get", mock_fn):
            await _call("get_markets_calendar", date_from="2024-01-04")
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.args[1] == {"from": "2024-01-04"}

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

    async def test_rate_limit_falls_back_to_cache(self, mock_env):
        mock_env["cache"].put_rows(
            "markets_calendar", [{"Date": "2024-01-04", "HolDiv": "0"}], key_columns=["Date"]
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get", mock_fn):
            result = await _call(
                "get_markets_calendar", date_from="2024-01-01", date_to="2024-01-10"
            )
        assert mock_fn.call_count == 1
        assert result["source"] == "cache"
        assert result["count"] == 1
        assert result["partial"] is True

    async def test_rate_limit_with_no_rows_after_hol_div_filter_raises(self, mock_env):
        # Only a trading day is cached; the holiday filter leaves nothing
        mock_env["cache"].put_rows(
            "markets_calendar", [{"Date": "2024-01-04", "HolDiv": "1"}], key_columns=["Date"]
        )
        mock_fn = AsyncMock(side_effect=RateLimitError())
        with patch.object(mock_env["client"], "get", mock_fn):
            with pytest.raises(Exception, match="(?i)rate limit"):
                await _call(
                    "get_markets_calendar",
                    hol_div="0",
                    date_from="2024-01-04",
                    date_to="2024-01-06",
                )

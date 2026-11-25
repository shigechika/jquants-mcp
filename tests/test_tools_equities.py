"""Tests for equity tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore, make_cache_key
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
    _, structured = await server_module.mcp.call_tool(tool_name, kwargs)
    return structured


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

    async def test_split_mid_batch_still_triggers_full_refetch(self, mock_env):
        """jquants-mcp#597 の false negative 回帰: 新規取得バッチの最終行
        ではなく途中の行で分割が起きても、キャッシュ全体の再取得が走ること。
        AdjFactor は日付単位のイベントフラグのため、分割日の翌営業日以降は
        1.0 に戻っており、バッチ末尾だけを見る旧実装は検知できなかった。
        """
        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            return_value=[{"Code": "72030", "Date": "2024-01-04", "O": 2800, "AdjFactor": 1.0}],
        ):
            await _call(
                "get_equities_bars_daily",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-04",
            )

        # 分割は 01-05（バッチ2行目=末尾ではない）、01-06 は通常日で 1.0 に復帰
        incremental_batch = [
            {"Code": "72030", "Date": "2024-01-04", "O": 2800, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-05", "O": 280, "AdjFactor": 0.1},
            {"Code": "72030", "Date": "2024-01-06", "O": 285, "AdjFactor": 1.0},
        ]
        mock_fn = AsyncMock(return_value=incremental_batch)
        with (
            patch.object(mock_env["client"], "get_all_pages", mock_fn),
            # 01-05 has no calendar row, so the completeness check loads the calendar
            patch.object(mock_env["client"], "get", AsyncMock(return_value={"data": []})),
        ):
            result = await _call(
                "get_equities_bars_daily",
                code="72030",
                date_from="2024-01-04",
                date_to="2024-01-06",
            )
            assert result["count"] == 3
            # 分割検知 → invalidate 後の全件再取得で get_all_pages が2回呼ばれる
            assert mock_fn.call_count == 2, (
                "バッチ途中の分割が検知されず、無条件再取得が走らなかった"
            )


@pytest.fixture()
def light_env(tmp_path):
    """Like mock_env, but on the Light plan so date_to-only queries have a lower bound."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="light",
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


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _seed_calendar(cache: CacheStore, start: date, end: date, holidays=()) -> list[str]:
    """Write markets_calendar rows for [start, end]; return the business days (ISO)."""
    rows, business = [], []
    for d in _days(start, end):
        open_day = d.weekday() < 5 and d.isoformat() not in holidays
        rows.append({"Date": d.isoformat(), "HolDiv": "1" if open_day else "0"})
        if open_day:
            business.append(d.isoformat())
    cache.put_rows("markets_calendar", rows, key_columns=["Date"])
    return business


def _bars(days: list[str], code: str = "13060") -> list[dict]:
    return [{"Code": code, "Date": d, "O": 100, "C": 101, "AdjFactor": 1.0} for d in days]


def _seed_bars(cache: CacheStore, days: list[str]) -> None:
    cache.put_rows(
        "equities_bars_daily", _bars(days), key_columns=["Code", "Date"], adj_factor_key="AdjFactor"
    )


class TestBarsDailyCacheCompleteness:
    """jquants-mcp#625: serve from cache only when the requested range is fully covered."""

    @pytest.fixture()
    def window(self):
        today = date.today()
        return today - timedelta(days=60), today - timedelta(days=30)

    async def test_date_to_only_with_fragmented_cache_fetches_full_history(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business[-4:])  # only the last few days cached
        api_rows = _bars(business)
        mock_fn = AsyncMock(return_value=api_rows)
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_equities_bars_daily", code="1306", date_to=end.isoformat())
        params = mock_fn.call_args.args[1]
        assert "from" not in params
        assert params["to"] == end.isoformat()
        assert result["count"] == len(api_rows)

    async def test_hole_inside_range_refetches_whole_range(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business[:3] + business[6:])  # hole in the middle
        mock_fn = AsyncMock(return_value=_bars(business))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        params = mock_fn.call_args.args[1]
        assert params["from"] == start.isoformat()
        assert params["to"] == end.isoformat()
        assert [r["Date"] for r in result["data"]] == business

    async def test_missing_tail_fetches_only_the_tail(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business[:-3])
        mock_fn = AsyncMock(return_value=_bars(business[-4:]))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert mock_fn.call_args.args[1]["from"] == business[-4]
        assert result["source"] == "cache+api"
        assert [r["Date"] for r in result["data"]] == business

    async def test_full_range_is_served_from_cache(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business)
        mock_fn = AsyncMock(return_value=[])
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert mock_fn.call_count == 0
        assert result["source"] == "cache"
        assert result["count"] == len(business)

    async def test_open_ended_range_still_refetches_the_latest_cached_day(self, light_env):
        today = date.today()
        start = today - timedelta(days=30)
        business = _seed_calendar(light_env["cache"], start, today)
        _seed_bars(light_env["cache"], business)
        mock_fn = AsyncMock(return_value=_bars(business[-1:]))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily", code="1306", date_from=start.isoformat()
            )
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.args[1]["from"] == business[-1]
        assert result["count"] == len(business)

    async def test_open_ended_covered_range_is_not_partial_when_refresh_fails(self, light_env):
        today = date.today()
        start = today - timedelta(days=30)
        business = _seed_calendar(light_env["cache"], start, today)
        _seed_bars(light_env["cache"], business)
        mock_fn = AsyncMock(side_effect=APIError("boom", status_code=500))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily", code="1306", date_from=start.isoformat()
            )
        assert result["count"] == len(business)
        assert "partial" not in result

    async def test_open_ended_range_skips_calendar_load_past_latest_cached(self, light_env):
        today = date.today()
        start = today - timedelta(days=30)
        weekdays = [d.isoformat() for d in _days(start, today) if d.weekday() < 5]
        _seed_bars(light_env["cache"], weekdays[:-2])  # no calendar, tail missing
        bars_fn = AsyncMock(return_value=[])
        cal_fn = AsyncMock(return_value={"data": []})
        with (
            patch.object(light_env["client"], "get_all_pages", bars_fn),
            patch.object(light_env["client"], "get", cal_fn),
        ):
            await _call("get_equities_bars_daily", code="1306", date_from=start.isoformat())
        assert cal_fn.call_count == 0
        assert bars_fn.call_args.args[1]["from"] == weekdays[-3]

    async def test_calendar_holiday_on_weekday_is_not_expected(self, light_env, window):
        start, end = window
        weekday = next(d for d in _days(start + timedelta(days=7), end) if d.weekday() == 2)
        business = _seed_calendar(light_env["cache"], start, end, holidays={weekday.isoformat()})
        _seed_bars(light_env["cache"], business)
        mock_fn = AsyncMock(return_value=[])
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert mock_fn.call_count == 0
        assert result["source"] == "cache"

    async def test_yyyymmdd_dates_hit_the_cache(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business)
        mock_fn = AsyncMock(return_value=[])
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.strftime("%Y%m%d"),
                date_to=end.strftime("%Y%m%d"),
            )
        assert mock_fn.call_count == 0
        assert result["count"] == len(business)

    async def test_single_date_asks_the_api_and_returns_only_that_day(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business)
        # A later split restated the day: the API answer must win over the cache
        restated = [{**_bars([business[2]])[0], "C": 50.5}]
        mock_fn = AsyncMock(return_value=restated)
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_equities_bars_daily", code="1306", date=business[2])
        assert mock_fn.call_args.args[1]["date"] == business[2]
        assert result["data"] == restated

    async def test_single_date_api_failure_returns_only_that_cached_day(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business)
        mock_fn = AsyncMock(side_effect=APIError("boom", status_code=500))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily", code="1306", date=business[2].replace("-", "")
            )
        assert [r["Date"] for r in result["data"]] == [business[2]]
        # The requested day is in the cache, so the answer is complete
        assert "partial" not in result

    async def test_date_to_only_on_premium_asks_the_api_once_then_uses_the_head(
        self, mock_env, window
    ):
        start, end = window
        business = _seed_calendar(mock_env["cache"], start, end)
        _seed_bars(mock_env["cache"], business)
        mock_fn = AsyncMock(return_value=_bars(business))
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_bars_daily", code="1306", date_to=end.isoformat())
            result = await _call("get_equities_bars_daily", code="1306", date_to=end.isoformat())
        # No lower bound to prove at first; the first answer records where bars start
        assert mock_fn.call_count == 1
        assert "from" not in mock_fn.call_args.args[1]
        assert result["source"] == "cache"
        assert result["count"] == len(business)

    async def test_stock_listed_inside_the_window_stops_refetching(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        listed = business[5:]  # no bars before the listing date
        _seed_bars(light_env["cache"], listed[-3:])
        mock_fn = AsyncMock(return_value=_bars(listed))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_bars_daily", code="1306", date_to=end.isoformat())
            result = await _call("get_equities_bars_daily", code="1306", date_to=end.isoformat())
        assert mock_fn.call_count == 1
        assert result["source"] == "cache"
        assert [r["Date"] for r in result["data"]] == listed

    async def test_head_from_a_bounded_ask_does_not_excuse_earlier_days(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business[5:7])
        # Asked from business[3]; the API's first bar is business[5]
        mock_fn = AsyncMock(return_value=_bars(business[5:]))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=business[3],
                date_to=end.isoformat(),
            )
            # Starting before business[3] is not covered by that record
            await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert mock_fn.call_count == 2
        assert mock_fn.call_args.args[1]["from"] == start.isoformat()

    async def test_head_never_excuses_a_hole_after_the_first_bar(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        light_env["cache"].put_response(
            "bars_daily_head|code=13060&plan=light",
            {"asked_from": None, "first": business[0]},
            ttl_seconds=3600,
        )
        _seed_bars(light_env["cache"], business[:3] + business[6:])
        mock_fn = AsyncMock(return_value=_bars(business))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_bars_daily", code="1306", date_to=end.isoformat())
        assert mock_fn.call_count == 1
        assert "from" not in mock_fn.call_args.args[1]

    async def test_missing_calendar_is_loaded_for_a_weekday_holiday(self, light_env, window):
        start, end = window
        holiday = next(d for d in _days(start + timedelta(days=7), end) if d.weekday() == 2)
        weekdays = [d.isoformat() for d in _days(start, end) if d.weekday() < 5]
        _seed_bars(light_env["cache"], [d for d in weekdays if d != holiday.isoformat()])
        calendar = {
            "data": [
                {"Date": d.isoformat(), "HolDiv": "1" if d.weekday() < 5 and d != holiday else "0"}
                for d in _days(start, end)
            ]
        }
        bars_fn = AsyncMock(return_value=[])
        cal_fn = AsyncMock(return_value=calendar)
        with (
            patch.object(light_env["client"], "get_all_pages", bars_fn),
            patch.object(light_env["client"], "get", cal_fn),
        ):
            first = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
            second = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert bars_fn.call_count == 0
        assert cal_fn.call_count == 1  # stored, so the second call needs no load
        assert cal_fn.call_args.args[0] == "/markets/calendar"
        assert first["source"] == second["source"] == "cache"

    async def test_failed_calendar_load_keeps_the_weekday_guess(self, light_env, window):
        start, end = window
        holiday = next(d for d in _days(start + timedelta(days=7), end) if d.weekday() == 2)
        weekdays = [d.isoformat() for d in _days(start, end) if d.weekday() < 5]
        _seed_bars(light_env["cache"], [d for d in weekdays if d != holiday.isoformat()])
        bars_fn = AsyncMock(return_value=[])
        cal_fn = AsyncMock(side_effect=APIError("boom", status_code=500))
        with (
            patch.object(light_env["client"], "get_all_pages", bars_fn),
            patch.object(light_env["client"], "get", cal_fn),
        ):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert bars_fn.call_count == 1
        assert result["count"] == len(weekdays) - 1

    async def test_api_failure_with_incomplete_cache_is_flagged_partial(self, light_env, window):
        start, end = window
        business = _seed_calendar(light_env["cache"], start, end)
        _seed_bars(light_env["cache"], business[:3] + business[6:])
        mock_fn = AsyncMock(side_effect=APIError("boom", status_code=500))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call(
                "get_equities_bars_daily",
                code="1306",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert result["source"] == "cache"
        assert result["partial"] is True
        assert result["count"] == len(business) - 3


def _valuation(days: list[str], code: str = "72030") -> list[dict]:
    return [
        {"Date": d, "Code": code, "EPS": 344.11, "PER": 9.9, "PBR": 1.07, "MktCap": 40116710.0}
        for d in days
    ]


class TestGetEquitiesValuation:
    """get_equities_valuation (#628)."""

    async def test_requires_code_or_date(self, light_env):
        result = await _call("get_equities_valuation")
        assert result["error"] is True
        result = await _call("get_equities_valuation", date_from="2026-01-05")
        assert result["error"] is True
        result = await _call("get_equities_valuation", date="2026-01-05", date_from="2026-01-05")
        assert result["error"] is True

    async def test_cached_rows_that_left_the_plan_window_are_dropped(self, light_env):
        today = date.today()
        old = (today - timedelta(days=5 * 366 + 3)).isoformat()  # past Light's 5 years
        recent = (today - timedelta(days=3)).isoformat()
        light_env["cache"].put_response(
            "/equities/valuation|code=72030&plan=light",
            {"count": 2, "data": _valuation([old, recent])},
            ttl_seconds=3600,
        )
        mock_fn = AsyncMock(return_value=[])
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_equities_valuation", code="72030")
        assert mock_fn.call_count == 0
        assert [r["Date"] for r in result["data"]] == [recent]
        assert result["count"] == 1

    async def test_code_range_is_cached_in_tier1(self, light_env):
        today = date.today()
        start, end = today - timedelta(days=60), today - timedelta(days=30)
        business = _seed_calendar(light_env["cache"], start, end)
        mock_fn = AsyncMock(return_value=_valuation(business))
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            first = await _call(
                "get_equities_valuation",
                code="7203",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
            second = await _call(
                "get_equities_valuation",
                code="7203",
                date_from=start.isoformat(),
                date_to=end.isoformat(),
            )
        assert mock_fn.call_count == 1
        assert mock_fn.call_args.args[0] == "/equities/valuation"
        assert first["count"] == second["count"] == len(business)
        assert second["source"] == "cache"
        # Stored in its own table, not in the bars table
        rows = light_env["cache"].get_rows(
            "equities_valuation", key_filter={"code": "72030"}, date_from=start.isoformat()
        )
        assert len(rows) == len(business)
        assert not light_env["cache"].get_rows("equities_bars_daily", key_filter={"code": "72030"})

    async def test_date_only_is_cached_per_plan_and_empty_is_not_cached(self, light_env):
        mock_fn = AsyncMock(return_value=[])
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_valuation", date="2026-11-20")
            await _call("get_equities_valuation", date="2026-11-20")
        assert mock_fn.call_count == 2  # an empty answer is not kept

        rows = _valuation(["2026-11-20"]) + _valuation(["2026-11-20"], code="67580")
        mock_fn = AsyncMock(return_value=rows)
        with patch.object(light_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_valuation", date="2026-11-20")
            result = await _call("get_equities_valuation", date="2026-11-20")
        assert mock_fn.call_count == 1
        assert result["count"] == 2
        key = "/equities/valuation|date=2026-11-20&plan=light"
        assert light_env["cache"].get_response(key) is not None
        # A past day's cross-section is kept for the plain 24 hours
        assert self._stored_ttl(light_env["cache"], key) == 24 * 3600

    @staticmethod
    def _stored_ttl(cache: CacheStore, key: str) -> int:
        conn = cache._ensure_connection()
        return conn.execute(
            "SELECT ttl_seconds FROM response_cache WHERE cache_key = ?", (key,)
        ).fetchone()[0]

    async def test_full_history_expires_at_the_next_publication(self, light_env):
        import jquants_mcp.tools.equities as equities_module

        ttl_fn = patch.object(equities_module, "publication_ttl", return_value=1234)
        mock_fn = AsyncMock(return_value=_valuation(["2026-11-20"]))
        with ttl_fn as fake, patch.object(light_env["client"], "get_all_pages", mock_fn):
            await _call("get_equities_valuation", code="72030")
        assert fake.call_args.args[1] == "/equities/valuation"
        key = "/equities/valuation|code=72030&plan=light"
        assert self._stored_ttl(light_env["cache"], key) == 1234

    async def test_free_plan_history_lasts_until_midnight(self, light_env, tmp_path):
        import jquants_mcp.tools.equities as equities_module

        cache = CacheStore(tmp_path / "free.db", default_plan="free")
        mock_fn = AsyncMock(return_value=_valuation(["2026-08-20"]))
        with (
            patch.object(server_module, "_cache", cache),
            patch.object(equities_module, "publication_ttl", return_value=1234) as fake,
            patch.object(equities_module, "_seconds_until_midnight", return_value=4321),
            patch.object(light_env["client"], "get_all_pages", mock_fn),
        ):
            await _call("get_equities_valuation", code="72030")
        # The 12-week delay means today's row never arrives: no publication
        # re-checks, but the ceiling moves at midnight, so the entry ends there
        assert fake.call_count == 0
        stored = self._stored_ttl(cache, "/equities/valuation|code=72030&plan=free")
        assert stored == 4321
        cache.close()

    def test_seconds_until_midnight(self):
        from jquants_mcp.cache.publication import JST
        from jquants_mcp.tools.equities import _seconds_until_midnight

        # 30 s before JST midnight: never below the 60 s floor
        assert _seconds_until_midnight(datetime(2026, 11, 25, 23, 59, 30, tzinfo=JST)) == 60
        # 10:00 JST: at most the 14 h left in the JST day (less if local midnight is sooner)
        assert 60 <= _seconds_until_midnight(datetime(2026, 11, 25, 10, 0, tzinfo=JST)) <= 14 * 3600


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
    def _seed_tier1(self, cache: CacheStore, records: list[dict]) -> None:
        """Seed equities_earnings_calendar Tier 1 table."""
        cache.put_rows(
            "equities_earnings_calendar",
            records,
            key_columns=["Code", "Date"],
        )

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

    async def test_code_query_uses_tier1(self, mock_env):
        """code= query returns Tier 1 data without hitting the API."""
        cache = mock_env["cache"]
        self._seed_tier1(
            cache,
            [
                {"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"},
                {"Code": "72030", "Date": "2026-08-10", "FQ": "4Q"},
                {"Code": "99830", "Date": "2026-05-15", "FQ": "1Q"},
            ],
        )
        with patch.object(mock_env["client"], "get_all_pages", new_callable=AsyncMock) as mock_api:
            result = await _call("get_equities_earnings_calendar", code="72030")
        mock_api.assert_not_called()
        assert result["count"] == 2
        codes = {r["Code"] for r in result["data"]}
        assert codes == {"72030"}

    async def test_code_query_sorted_descending(self, mock_env):
        """code= results are sorted by Date descending."""
        cache = mock_env["cache"]
        self._seed_tier1(
            cache,
            [
                {"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"},
                {"Code": "72030", "Date": "2026-08-10", "FQ": "4Q"},
            ],
        )
        result = await _call("get_equities_earnings_calendar", code="72030")
        dates = [r["Date"] for r in result["data"]]
        assert dates == sorted(dates, reverse=True)

    async def test_date_query_uses_tier1(self, mock_env):
        """date= query returns Tier 1 data when available."""
        cache = mock_env["cache"]
        self._seed_tier1(
            cache,
            [
                {"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"},
                {"Code": "99830", "Date": "2026-05-10", "FQ": "1Q"},
                {"Code": "11110", "Date": "2026-05-11", "FQ": "2Q"},
            ],
        )
        result = await _call("get_equities_earnings_calendar", date="2026-05-10")
        assert result["count"] == 2
        dates = {r["Date"] for r in result["data"]}
        assert dates == {"2026-05-10"}

    async def test_date_query_yyyymmdd_format(self, mock_env):
        """date= accepts YYYYMMDD format and normalizes to YYYY-MM-DD for Tier 1 lookup."""
        cache = mock_env["cache"]
        self._seed_tier1(
            cache,
            [
                {"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"},
            ],
        )
        result = await _call("get_equities_earnings_calendar", date="20260510")
        assert result["count"] == 1

    async def test_date_query_no_tier1_falls_back_to_tier2(self, mock_env):
        """date= falls back to Tier 2 response_cache when Tier 1 is empty."""
        cache = mock_env["cache"]
        from jquants_mcp.cache.store import TTL_90D, make_cache_key

        tier2_data = [{"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"}]
        ck = make_cache_key("/equities/earnings-calendar", {"date": "20260510"})
        cache.put_response(ck, tier2_data, ttl_seconds=TTL_90D)
        result = await _call("get_equities_earnings_calendar", date="2026-05-10")
        assert result["count"] == 1

    async def test_date_query_reads_the_legacy_question_mark_key(self, mock_env):
        """#620: caches published before the fix hold "?date=" keys for 90 days."""
        from jquants_mcp.cache.store import TTL_90D

        tier2_data = [{"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"}]
        mock_env["cache"].put_response(
            "/equities/earnings-calendar?date=20260510", tier2_data, ttl_seconds=TTL_90D
        )
        result = await _call("get_equities_earnings_calendar", date="2026-05-10")
        assert result["count"] == 1

    async def test_code_query_scans_the_current_key_shape(self, mock_env):
        """#620: daily_fetch now writes make_cache_key-shaped date keys."""
        from jquants_mcp.cache.store import TTL_90D, make_cache_key

        tier2_data = [{"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"}]
        mock_env["cache"].put_response(
            make_cache_key("/equities/earnings-calendar", {"date": "20260510"}),
            tier2_data,
            ttl_seconds=TTL_90D,
        )
        result = await _call("get_equities_earnings_calendar", code="72030")
        assert result["count"] == 1

    async def test_code_query_ignores_a_superseded_legacy_day(self, mock_env):
        """A day rewritten under the current key must not resurrect its legacy rows."""
        from jquants_mcp.cache.store import TTL_90D, make_cache_key

        cache = mock_env["cache"]
        withdrawn = [{"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"}]
        cache.put_response(
            "/equities/earnings-calendar?date=20260510", withdrawn, ttl_seconds=TTL_90D
        )
        cache.put_response(
            make_cache_key("/equities/earnings-calendar", {"date": "20260510"}),
            [{"Code": "67580", "Date": "2026-05-10", "FQ": "3Q"}],
            ttl_seconds=TTL_90D,
        )
        # A legacy-only day is still read
        cache.put_response(
            "/equities/earnings-calendar?date=20260511",
            [{"Code": "72030", "Date": "2026-05-11", "FQ": "3Q"}],
            ttl_seconds=TTL_90D,
        )
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=[]
        ):
            result = await _call("get_equities_earnings_calendar", code="72030")
        assert [r["Date"] for r in result["data"]] == ["2026-05-11"]

    async def test_date_query_no_data_returns_empty(self, mock_env):
        """date= returns empty result when neither Tier 1 nor Tier 2 has data."""
        result = await _call("get_equities_earnings_calendar", date="2026-05-10")
        assert result["count"] == 0
        assert result["data"] == []

    async def test_code_query_empty_tier1_falls_back_to_tier2(self, mock_env):
        """code= falls back to Tier 2 LIKE scan when Tier 1 is empty."""
        import json as _json
        import time

        cache = mock_env["cache"]
        conn = cache._ensure_connection()
        now = time.time()
        tier2_data = [{"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"}]
        conn.execute(
            "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) "
            "VALUES (?, ?, ?, ?)",
            (
                "/equities/earnings-calendar?date=20260510",
                _json.dumps(tier2_data),
                now,
                90 * 24 * 3600,
            ),
        )
        conn.commit()
        result = await _call("get_equities_earnings_calendar", code="72030")
        assert result["count"] == 1
        assert result["data"][0]["FQ"] == "3Q"

    async def test_4digit_code_padded(self, mock_env):
        """4-digit code is padded to 5 digits before querying."""
        cache = mock_env["cache"]
        self._seed_tier1(cache, [{"Code": "72030", "Date": "2026-05-10", "FQ": "3Q"}])
        result = await _call("get_equities_earnings_calendar", code="7203")
        assert result["count"] == 1


def _seed_master(cache: CacheStore, rows: list[dict]) -> None:
    """Seed equities_master Tier 1 rows."""
    cache.put_rows("equities_master", rows, key_columns=["Code", "Date"])


class TestGetEarningsThisWeek:
    def _seed_calendar(self, cache: CacheStore, records: list[dict]) -> None:
        cache.put_rows("equities_earnings_calendar", records, key_columns=["Code", "Date"])

    async def test_groups_by_date_and_enriches(self, mock_env):
        cache = mock_env["cache"]
        self._seed_calendar(
            cache,
            [
                # two on day 1 (seeded out of code order to verify per-day sort)
                {
                    "Code": "99830",
                    "Date": "2026-06-09",
                    "CoName": "ファーストリテイリング",
                    "FQ": "3Q",
                },
                {"Code": "72030", "Date": "2026-06-09", "CoName": "トヨタ自動車", "FQ": "FY"},
                {"Code": "67580", "Date": "2026-06-12", "CoName": "ソニーグループ", "FQ": "FY"},
            ],
        )
        # master supplies sector/market for the enrichment join (calendar rows
        # here intentionally omit SectorNm/Section to exercise the fallback).
        _seed_master(
            cache,
            [
                {"Code": "72030", "Date": "2026-06-05", "S33Nm": "輸送用機器", "MktNm": "プライム"},
            ],
        )
        result = await _call("get_earnings_this_week", date_from="2026-06-09", date_to="2026-06-12")
        assert result["count"] == 3
        assert result["date_from"] == "2026-06-09"
        assert result["date_to"] == "2026-06-12"
        assert [d["date"] for d in result["days"]] == ["2026-06-09", "2026-06-12"]

        day1 = result["days"][0]
        assert day1["count"] == 2
        # within a day, sorted by display code ("7203" < "9983")
        assert [c["code"] for c in day1["companies"]] == ["7203", "9983"]
        toyota = day1["companies"][0]
        assert toyota["name"] == "トヨタ自動車"  # from the calendar row
        assert toyota["sector"] == "輸送用機器"  # from the master fallback
        assert toyota["market"] == "プライム"
        assert toyota["fiscal_quarter"] == "FY"

    async def test_default_window_is_today_plus_7(self, mock_env):
        cache = mock_env["cache"]
        in_window = (date.today() + timedelta(days=2)).isoformat()
        out_window = (date.today() + timedelta(days=10)).isoformat()
        self._seed_calendar(
            cache,
            [
                {"Code": "72030", "Date": in_window, "CoName": "トヨタ自動車", "FQ": "FY"},
                {"Code": "67580", "Date": out_window, "CoName": "ソニーグループ", "FQ": "FY"},
            ],
        )
        result = await _call("get_earnings_this_week")
        assert result["count"] == 1
        codes = {c["code"] for day in result["days"] for c in day["companies"]}
        assert codes == {"7203"}

    async def test_boundaries_inclusive(self, mock_env):
        cache = mock_env["cache"]
        self._seed_calendar(
            cache,
            [
                {"Code": "72030", "Date": "2026-06-09", "CoName": "A", "FQ": "FY"},
                {"Code": "67580", "Date": "2026-06-12", "CoName": "B", "FQ": "FY"},
                {"Code": "65010", "Date": "2026-06-20", "CoName": "C", "FQ": "1Q"},
            ],
        )
        result = await _call("get_earnings_this_week", date_from="2026-06-09", date_to="2026-06-12")
        codes = {c["code"] for day in result["days"] for c in day["companies"]}
        assert codes == {"7203", "6758"}  # 2026-06-20 excluded

    async def test_yyyymmdd_format_accepted(self, mock_env):
        cache = mock_env["cache"]
        self._seed_calendar(
            cache, [{"Code": "72030", "Date": "2026-06-09", "CoName": "A", "FQ": "FY"}]
        )
        result = await _call("get_earnings_this_week", date_from="20260609", date_to="20260612")
        assert result["count"] == 1

    async def test_empty_when_no_data(self, mock_env):
        result = await _call("get_earnings_this_week", date_from="2026-06-09", date_to="2026-06-12")
        assert result["count"] == 0
        assert result["days"] == []

    async def test_date_to_before_date_from_errors(self, mock_env):
        result = await _call("get_earnings_this_week", date_from="2026-06-12", date_to="2026-06-09")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"

    async def test_invalid_date_format_errors(self, mock_env):
        result = await _call("get_earnings_this_week", date_from="2026/06/09")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"


class TestEarningsCalendarLiveFallback:
    """Cache-miss live fallback for same-day announcements (#523).

    The nightly cron can run before the upstream 19:00 JST update, so the
    cache may lack "today's" announcements — the tools then fetch once live.
    """

    def _mock_api(self, mock_env, rows):
        return patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=rows
        )

    async def test_code_query_falls_back_to_live_fetch(self, mock_env):
        today = date.today().isoformat()
        rows = [{"Code": "40630", "Date": today, "CoName": "信越化学工業", "FQ": "1Q"}]
        with self._mock_api(mock_env, rows) as mock_api:
            result = await _call("get_equities_earnings_calendar", code="4063")
        mock_api.assert_awaited_once()
        assert result["count"] == 1
        assert result["data"][0]["Code"] == "40630"
        # The live rows were upserted into Tier 1: a second call needs no API.
        with patch.object(mock_env["client"], "get_all_pages", new_callable=AsyncMock) as m2:
            again = await _call("get_equities_earnings_calendar", code="40630")
        m2.assert_not_called()
        assert again["count"] == 1

    async def test_this_week_falls_back_when_window_includes_today(self, mock_env):
        today = date.today().isoformat()
        rows = [{"Code": "40630", "Date": today, "CoName": "信越化学工業", "FQ": "1Q"}]
        with self._mock_api(mock_env, rows) as mock_api:
            result = await _call("get_earnings_this_week", date_from=today, date_to=today)
        mock_api.assert_awaited_once()
        assert result["count"] == 1
        assert result["days"][0]["companies"][0]["code"] == "4063"

    async def test_this_week_past_window_does_not_hit_api(self, mock_env):
        with self._mock_api(mock_env, []) as mock_api:
            result = await _call(
                "get_earnings_this_week", date_from="2026-06-09", date_to="2026-06-12"
            )
        mock_api.assert_not_called()
        assert result["count"] == 0

    async def test_refresh_marker_prevents_refetch_storm(self, mock_env):
        # Empty upstream (weekend/holiday): the short-TTL GLOBAL marker
        # suppresses any further live fetch for 30 minutes.
        with self._mock_api(mock_env, []) as mock_api:
            await _call("get_earnings_this_week")
            await _call("get_earnings_this_week")
        assert mock_api.await_count == 1

    async def test_date_query_today_falls_back(self, mock_env):
        today = date.today().isoformat()
        rows = [{"Code": "40630", "Date": today, "CoName": "信越化学工業", "FQ": "1Q"}]
        with self._mock_api(mock_env, rows) as mock_api:
            result = await _call("get_equities_earnings_calendar", date=today)
        mock_api.assert_awaited_once()
        assert result["count"] == 1

    async def test_date_query_past_does_not_hit_api(self, mock_env):
        with self._mock_api(mock_env, []) as mock_api:
            result = await _call("get_equities_earnings_calendar", date="2026-05-10")
        mock_api.assert_not_called()
        assert result["count"] == 0

    async def test_this_week_falls_back_when_today_missing_but_window_has_rows(self, mock_env):
        # The #523 failure mode with a partially-populated window: tomorrow's
        # rows are cached but today's are not — the fallback must still fire.
        cache = mock_env["cache"]
        today = date.today()
        tomorrow = (today + timedelta(days=1)).isoformat()
        cache.put_rows(
            "equities_earnings_calendar",
            [{"Code": "72030", "Date": tomorrow, "CoName": "トヨタ自動車", "FQ": "FY"}],
            key_columns=["Code", "Date"],
        )
        rows = [{"Code": "40630", "Date": today.isoformat(), "CoName": "信越化学工業", "FQ": "1Q"}]
        with self._mock_api(mock_env, rows) as mock_api:
            result = await _call(
                "get_earnings_this_week",
                date_from=today.isoformat(),
                date_to=(today + timedelta(days=7)).isoformat(),
            )
        mock_api.assert_awaited_once()
        assert result["count"] == 2
        codes = {c["code"] for day in result["days"] for c in day["companies"]}
        assert codes == {"4063", "7203"}

    async def test_no_filter_prefers_tier1_over_the_stale_blob(self, mock_env):
        """The no-argument query must not serve an old snapshot (#536).

        Production had a 22-hour-old Tier 2 blob (90-day TTL) ending 2026-07-24
        while Tier 1 already held rows three days newer. The blob won, so the
        tool answered plausibly and wrongly.
        """
        cache = mock_env["cache"]
        today = date.today()
        fresh = (today + timedelta(days=2)).isoformat()
        cache.put_rows(
            "equities_earnings_calendar",
            [{"Code": "72030", "Date": fresh, "CoName": "トヨタ自動車", "FQ": "FY"}],
            key_columns=["Code", "Date"],
        )
        # A stale blob of the shape the old code returned unconditionally.
        cache.put_response(
            make_cache_key("/equities/earnings-calendar"),
            {"count": 1, "data": [{"Code": "99990", "Date": "2020-01-01"}]},
            ttl_seconds=7776000,
        )

        with self._mock_api(mock_env, []) as mock_api:
            result = await _call("get_equities_earnings_calendar")

        mock_api.assert_not_called()
        assert [r["Code"] for r in result["data"]] == ["72030"]
        assert result["data"][0]["Date"] == fresh

    async def test_no_filter_refreshes_when_nothing_is_scheduled_ahead(self, mock_env):
        """Only past rows in Tier 1 is the signature of a stale calendar."""
        cache = mock_env["cache"]
        today = date.today()
        cache.put_rows(
            "equities_earnings_calendar",
            [
                {
                    "Code": "72030",
                    "Date": (today - timedelta(days=3)).isoformat(),
                    "CoName": "トヨタ自動車",
                }
            ],
            key_columns=["Code", "Date"],
        )
        upstream = [{"Code": "40630", "Date": today.isoformat(), "CoName": "信越化学工業"}]

        with self._mock_api(mock_env, upstream) as mock_api:
            result = await _call("get_equities_earnings_calendar")

        mock_api.assert_awaited_once()
        assert {r["Code"] for r in result["data"]} == {"72030", "40630"}

    async def test_no_filter_does_not_refresh_when_future_rows_exist(self, mock_env):
        cache = mock_env["cache"]
        ahead = (date.today() + timedelta(days=1)).isoformat()
        cache.put_rows(
            "equities_earnings_calendar",
            [{"Code": "72030", "Date": ahead, "CoName": "トヨタ自動車"}],
            key_columns=["Code", "Date"],
        )
        with self._mock_api(mock_env, []) as mock_api:
            result = await _call("get_equities_earnings_calendar")
        mock_api.assert_not_called()
        assert result["count"] == 1

    async def test_no_filter_window_excludes_beyond_ahead_days(self, mock_env):
        """The no-arg window is +/- a handful of days, not +/-90: a row far
        outside it must not appear (see _EARNINGS_WINDOW_AHEAD_DAYS)."""
        cache = mock_env["cache"]
        near = (date.today() + timedelta(days=10)).isoformat()
        far = (date.today() + timedelta(days=30)).isoformat()
        cache.put_rows(
            "equities_earnings_calendar",
            [
                {"Code": "72030", "Date": near, "CoName": "トヨタ自動車"},
                {"Code": "99830", "Date": far, "CoName": "遠い将来"},
            ],
            key_columns=["Code", "Date"],
        )
        with self._mock_api(mock_env, []) as mock_api:
            result = await _call("get_equities_earnings_calendar")
        mock_api.assert_not_called()
        codes = {r["Code"] for r in result["data"]}
        assert codes == {"72030"}

    async def test_live_fetch_rate_limit_degrades_gracefully(self, mock_env):
        # RateLimitError is outside TOOL_API_ERRORS; the best-effort side
        # fetch must swallow it and serve the (empty) cached result.
        from jquants_mcp.exceptions import RateLimitError

        with patch.object(
            mock_env["client"],
            "get_all_pages",
            new_callable=AsyncMock,
            side_effect=RateLimitError(),
        ) as mock_api:
            result = await _call("get_earnings_this_week")
        mock_api.assert_awaited_once()
        assert result["count"] == 0
        assert result["days"] == []


class TestSearchEquities:
    async def test_exact_match(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "80530",
                    "Date": "2026-01-04",
                    "CoName": "住友商事",
                    "CoNameEn": "Sumitomo Corp",
                },
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                },
            ],
        )
        result = await _call("search_equities", name="住友商事")
        assert result["count"] == 1
        assert result["data"][0]["code"] == "8053"
        assert result["data"][0]["name"] == "住友商事"

    async def test_partial_match(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                },
                {
                    "Code": "71820",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ紡織",
                    "CoNameEn": "Toyota Boshoku",
                },
                {
                    "Code": "80530",
                    "Date": "2026-01-04",
                    "CoName": "住友商事",
                    "CoNameEn": "Sumitomo Corp",
                },
            ],
        )
        result = await _call("search_equities", name="トヨタ")
        assert result["count"] == 2
        codes = [r["code"] for r in result["data"]]
        assert "7203" in codes
        assert "7182" in codes

    async def test_english_name_match(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                },
                {
                    "Code": "80530",
                    "Date": "2026-01-04",
                    "CoName": "住友商事",
                    "CoNameEn": "Sumitomo Corp",
                },
            ],
        )
        result = await _call("search_equities", name="Toyota")
        assert result["count"] == 1
        assert result["data"][0]["code"] == "7203"

    async def test_case_insensitive(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                },
            ],
        )
        result = await _call("search_equities", name="toyota")
        assert result["count"] == 1

    async def test_deduplicates_by_latest_date(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {"Code": "72030", "Date": "2024-01-04", "CoName": "旧社名", "CoNameEn": "Old Name"},
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                },
            ],
        )
        result = await _call("search_equities", name="トヨタ")
        assert result["count"] == 1
        assert result["data"][0]["name"] == "トヨタ自動車"

    async def test_empty_cache_returns_empty(self, mock_env):
        result = await _call("search_equities", name="住友商事")
        assert result["count"] == 0
        assert result["data"] == []

    async def test_no_match_returns_empty(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                },
            ],
        )
        result = await _call("search_equities", name="住友商事")
        assert result["count"] == 0

    async def test_empty_name_returns_error(self, mock_env):
        result = await _call("search_equities", name="")
        assert result.get("error") is True
        assert result.get("error_type") == "ValidationError"

    async def test_sorted_by_code(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "80530",
                    "Date": "2026-01-04",
                    "CoName": "住友商事",
                    "CoNameEn": "Sumitomo Corp",
                },
                {
                    "Code": "80540",
                    "Date": "2026-01-04",
                    "CoName": "住友電気工業",
                    "CoNameEn": "Sumitomo Electric",
                },
                {
                    "Code": "80550",
                    "Date": "2026-01-04",
                    "CoName": "住友不動産",
                    "CoNameEn": "Sumitomo Realty",
                },
            ],
        )
        result = await _call("search_equities", name="住友")
        codes = [r["code"] for r in result["data"]]
        assert codes == sorted(codes)

    async def test_includes_optional_fields_when_present(self, mock_env):
        """market/sector must be read from the actual stored field names
        (MktNm/S33Nm, matching get_sector_map) — not the long-form names
        that never appear in real equities_master data (regression for the
        search_equities market/sector field-name fix)."""
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "CoNameEn": "Toyota Motor",
                    "Mkt": 111,
                    "MktNm": "プライム",
                    "S33": "3700",
                    "S33Nm": "輸送用機器",
                }
            ],
        )
        result = await _call("search_equities", name="トヨタ")
        item = result["data"][0]
        assert item["market"] == "プライム"
        assert item["sector"] == "輸送用機器"
        assert item["name_en"] == "Toyota Motor"

    async def test_market_falls_back_to_code_as_string(self, mock_env):
        """When MktNm is absent, market falls back to the raw Mkt code and
        must be coerced to a string (Mkt is stored as an int)."""
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {
                    "Code": "72030",
                    "Date": "2026-01-04",
                    "CoName": "トヨタ自動車",
                    "Mkt": 111,
                }
            ],
        )
        result = await _call("search_equities", name="トヨタ")
        item = result["data"][0]
        assert item["market"] == "111"

    async def test_5digit_code_normalized_to_display(self, mock_env):
        cache = mock_env["cache"]
        _seed_master(
            cache,
            [
                {"Code": "72030", "Date": "2026-01-04", "CoName": "トヨタ自動車"},
                # ETF-style code: 5-digit not ending in 0 stays as-is
                {"Code": "13050", "Date": "2026-01-04", "CoName": "大和 iFreeETF TOPIX"},
            ],
        )
        result = await _call("search_equities", name="トヨタ")
        assert result["data"][0]["code"] == "7203"

    async def test_free_plan_finds_recently_dated_code(self, tmp_path):
        """search_equities must not be plan-clamped — a code whose only
        cached equities_master snapshot is inside the Free-plan embargo
        window must still be found (regression for the reference-table
        plan-clamp fix; mirrors get_name_map/get_sector_map)."""
        free_cache = CacheStore(tmp_path / "free.db", default_plan="free")
        recent = (date.today() - timedelta(weeks=2)).isoformat()  # embargoed for Free
        free_cache.put_rows(
            "equities_master",
            [{"Code": "72030", "Date": recent, "CoName": "トヨタ自動車"}],
            key_columns=["Code", "Date"],
        )
        with patch.object(server_module, "_cache", free_cache):
            result = await _call("search_equities", name="トヨタ")
        free_cache.close()
        assert result["count"] == 1
        assert result["data"][0]["code"] == "7203"

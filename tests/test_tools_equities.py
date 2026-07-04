"""Tests for equity tools."""

from __future__ import annotations

import json
from datetime import date, timedelta
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

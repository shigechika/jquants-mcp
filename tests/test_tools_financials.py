"""Tests for financial tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings
from jquants_mcp.client import JQuantsClient
from jquants_mcp.exceptions import PlanRestrictionError


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


class TestFinsSummarySplitAdjustment:
    """Stock split adjustment for fins_summary per-share fields."""

    async def test_adj_fields_added_when_split_detected(self, mock_env):
        """AdjBPS/AdjEPS/AdjDivAnn are added when split occurred after DiscDate."""
        cache = mock_env["cache"]
        # J-Quants AdjFactor: 0.2 on split day (1:5 split), 1.0 otherwise
        cache.put_rows(
            "equities_bars_daily",
            [
                {"Code": "18220", "Date": "2024-01-04", "O": 3500, "AdjFactor": 1.0},
                {"Code": "18220", "Date": "2025-03-28", "O": 3500, "AdjFactor": 0.2},
                {"Code": "18220", "Date": "2025-04-01", "O": 700, "AdjFactor": 1.0},
            ],
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        mock_data = [
            {
                "Code": "18220",
                "DiscDate": "2024-02-06",
                "DiscNo": "001",
                "BPS": 6000.0,
                "EPS": 500.0,
                "DivAnn": 100.0,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="18220")
            row = result["data"][0]
            # Split 1:5 after 2024-02-06 -> cum_factor = 0.2
            # AdjBPS = 6000 * 0.2 = 1200
            assert row["AdjBPS"] == 1200.0
            assert row["AdjEPS"] == 100.0
            assert row["AdjDivAnn"] == 20.0

    async def test_adj_fields_on_cache_hit(self, mock_env):
        """Split adjustment applies even on Tier1 cache hit (date specified)."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [
                {"Code": "18220", "Date": "2024-01-04", "O": 3500, "AdjFactor": 1.0},
                {"Code": "18220", "Date": "2025-03-28", "O": 3500, "AdjFactor": 0.2},
                {"Code": "18220", "Date": "2025-04-01", "O": 700, "AdjFactor": 1.0},
            ],
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )
        # Pre-populate fins_summary cache
        cache.put_rows(
            "fins_summary",
            [{"Code": "18220", "DiscDate": "2024-02-06", "DiscNo": "001", "BPS": 6000.0}],
            key_columns=["Code", "DiscDate"],
        )

        # Call with date to hit cache early-return path
        mock_fn = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_fins_summary", code="18220", date="2024-02-06")
            assert result["source"] == "cache"
            # Split 1:5 after 2024-02-06 -> AdjBPS = 6000 * 0.2 = 1200
            assert result["data"][0]["AdjBPS"] == 1200.0

    async def test_no_adjustment_when_no_split(self, mock_env):
        """No split after DiscDate: AdjBPS == BPS."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [
                {"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0},
                {"Code": "72030", "Date": "2025-04-01", "O": 100, "AdjFactor": 1.0},
            ],
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-02-06",
                "DiscNo": "001",
                "BPS": 3000.0,
                "EPS": 200.0,
                "DivAnn": 80.0,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            row = result["data"][0]
            assert row["AdjBPS"] == 3000.0
            assert row["AdjEPS"] == 200.0

    async def test_not_applied_without_daily_data(self, mock_env):
        """Without equities_bars_daily cache, split_adjustment='not_applied'."""
        mock_data = [
            {
                "Code": "99990",
                "DiscDate": "2024-02-06",
                "DiscNo": "001",
                "BPS": 5000.0,
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="99990")
            assert result.get("split_adjustment") == "not_applied"
            assert "AdjBPS" not in result["data"][0]

    async def test_no_crash_when_adj_factor_zero(self, mock_env):
        """AdjFactor=0.0 is ignored (treated as no split), no ZeroDivisionError."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [{"Code": "99990", "Date": "2024-01-04", "O": 100, "AdjFactor": 0.0}],
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        mock_data = [
            {"Code": "99990", "DiscDate": "2024-02-06", "DiscNo": "001", "BPS": 5000.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="99990")
            # Should not crash; AdjBPS == BPS (no effective split)
            assert result["data"][0]["AdjBPS"] == 5000.0

    async def test_date_only_query_notes_not_applied(self, mock_env):
        """Date-only queries note that split adjustment is not applied."""
        mock_data = [
            {"Code": "72030", "DiscDate": "2024-02-06", "BPS": 3000.0},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", date="2024-02-06")
            assert result.get("split_adjustment") == "not_applied"


class TestFinsSummaryFiscalPeriod:
    """Derived ``FiscalPeriod`` field on fins_summary rows."""

    async def test_fiscal_period_from_cur_per_type(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-02-06",
                "DiscNo": "001",
                "CurPerType": "2Q",
                "DocType": "2QFinancialStatements_Consolidated_JP",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] == "2Q"

    async def test_fiscal_period_from_doc_type_when_cur_per_blank(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-08-06",
                "DiscNo": "002",
                "CurPerType": "",
                "DocType": "1QFinancialStatements_Consolidated_JP",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] == "1Q"

    async def test_fiscal_period_fy_from_doc_type(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-05-15",
                "DiscNo": "003",
                "DocType": "FYFinancialStatements_Consolidated_JP",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] == "FY"

    async def test_fiscal_period_other_period(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-09-15",
                "DiscNo": "004",
                "DocType": "OtherPeriodFinancialStatements_Consolidated_JP",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] == "Other"

    async def test_fiscal_period_none_for_forecast_revision(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-10-15",
                "DiscNo": "005",
                "DocType": "EarnForecastRevision",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] is None

    async def test_fiscal_period_none_when_fields_absent(self, mock_env):
        mock_data = [
            {"Code": "72030", "DiscDate": "2024-11-15", "DiscNo": "006"},
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] is None

    async def test_fiscal_period_on_cache_hit(self, mock_env):
        cache = mock_env["cache"]
        cache.put_rows(
            "fins_summary",
            [
                {
                    "Code": "72030",
                    "DiscDate": "2024-02-06",
                    "DiscNo": "001",
                    "CurPerType": "3Q",
                    "DocType": "3QFinancialStatements_Consolidated_JP",
                }
            ],
            key_columns=["Code", "DiscDate"],
        )
        mock_fn = AsyncMock(return_value=[])
        with patch.object(mock_env["client"], "get_all_pages", mock_fn):
            result = await _call("get_fins_summary", code="72030", date="2024-02-06")
            assert result["source"] == "cache"
            assert result["data"][0]["FiscalPeriod"] == "3Q"

    async def test_fiscal_period_on_date_only_query(self, mock_env):
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-02-06",
                "DocType": "FYFinancialStatements_Consolidated_JP",
            },
            {
                "Code": "67580",
                "DiscDate": "2024-02-06",
                "DocType": "1QFinancialStatements_Consolidated_JP",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", date="2024-02-06")
            periods = {r["Code"]: r["FiscalPeriod"] for r in result["data"]}
            assert periods["72030"] == "FY"
            assert periods["67580"] == "1Q"

    async def test_fiscal_period_handles_non_jp_doctype_variants(self, mock_env):
        # The DocType prefix-match logic should work uniformly for all
        # accounting-standard variants (IFRS / JMIS / REIT / Foreign / US),
        # not just `_Consolidated_JP`.
        mock_data = [
            {
                "Code": "11000",
                "DiscDate": "2024-02-01",
                "DocType": "1QFinancialStatements_Consolidated_IFRS",
            },
            {
                "Code": "12000",
                "DiscDate": "2024-02-02",
                "DocType": "FYFinancialStatements_Consolidated_REIT",
            },
            {
                "Code": "13000",
                "DiscDate": "2024-02-03",
                "DocType": "3QFinancialStatements_NonConsolidated_Foreign",
            },
            {
                "Code": "14000",
                "DiscDate": "2024-02-04",
                "DocType": "2QFinancialStatements_Consolidated_JMIS",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", date="2024-02-01")
            periods = {r["Code"]: r["FiscalPeriod"] for r in result["data"]}
            assert periods["11000"] == "1Q"
            assert periods["12000"] == "FY"
            assert periods["13000"] == "3Q"
            assert periods["14000"] == "2Q"

    async def test_fiscal_period_legacy_long_keys(self, mock_env):
        # Defensive: J-Quants historically used `TypeOfCurrentPeriod` and
        # `TypeOfDocument`; cache rows from older fetches may still carry them.
        mock_data = [
            {
                "Code": "72030",
                "DiscDate": "2024-02-06",
                "TypeOfCurrentPeriod": "FY",
                "TypeOfDocument": "FYFinancialStatements_Consolidated_JP",
            },
        ]
        with patch.object(
            mock_env["client"], "get_all_pages", new_callable=AsyncMock, return_value=mock_data
        ):
            result = await _call("get_fins_summary", code="72030")
            assert result["data"][0]["FiscalPeriod"] == "FY"


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
            assert "plan" in result["hint"].lower()


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

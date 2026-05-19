"""Tests for tools/summary.py (get_stock_briefing)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call(result) -> dict:
    return json.loads(result.content[0].text)


def _make_cache(tmp_path: Path) -> CacheStore:
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE equities_bars_daily "
        "(code TEXT NOT NULL, date TEXT NOT NULL, adj_factor REAL, "
        "data TEXT NOT NULL, fetched_at REAL, PRIMARY KEY (code, date))"
    )
    conn.execute("CREATE INDEX idx_ebd_date ON equities_bars_daily (date)")
    conn.execute(
        "CREATE TABLE equities_master "
        "(code TEXT NOT NULL, date TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'standard', "
        "data TEXT NOT NULL, fetched_at REAL, PRIMARY KEY (code, date))"
    )
    conn.execute(
        "CREATE TABLE fins_summary "
        "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
        "data TEXT NOT NULL, fetched_at REAL, PRIMARY KEY (code, disc_date))"
    )
    conn.execute(
        "CREATE TABLE markets_margin_interest "
        "(code TEXT NOT NULL, date TEXT NOT NULL, "
        "data TEXT NOT NULL, fetched_at REAL, PRIMARY KEY (code, date))"
    )
    conn.commit()
    conn.close()
    settings = Settings()
    settings.jquants_plan = "premium"
    return CacheStore(db_path, settings)


def _insert_master(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    s17_name: str = "Electric Appliances",
    s33_name: str = "Electric Machinery",
    mkt_name: str = "Prime",
    date: str = "2026-05-01",
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "CoName": name,
        "CoNameEn": name + " Co",
        "S17": "3050",
        "S17Nm": s17_name,
        "S33": "3050",
        "S33Nm": s33_name,
        "Mkt": 111,
        "MktNm": mkt_name,
    }
    conn.execute(
        "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), time.time()),
    )


def _insert_bar(
    conn: sqlite3.Connection,
    code: str,
    date: str,
    adj_c: float,
    adj_factor: float = 1.0,
    vo: int = 10_000,
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "O": adj_c,
        "H": adj_c + 50,
        "L": adj_c - 50,
        "C": adj_c,
        "AdjO": adj_c,
        "AdjH": adj_c + 50,
        "AdjL": adj_c - 50,
        "AdjC": adj_c,
        "Vo": vo,
        "AdjFactor": adj_factor,
    }
    conn.execute(
        "INSERT OR REPLACE INTO equities_bars_daily "
        "(code, date, adj_factor, data, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (code, date, adj_factor, json.dumps(data), time.time()),
    )


def _insert_fins(
    conn: sqlite3.Connection,
    code: str,
    disc_date: str,
    *,
    net_sales: float = 100_000,
    op_profit: float = 10_000,
    ord_profit: float = 9_500,
    profit: float = 7_000,
    eps: float = 100.0,
    bps: float = 1_000.0,
    div_ann: float = 20.0,
    cur_per_type: str = "FY",
    fy_end: str = "2026-03-31",
) -> None:
    data = {
        "Code": code,
        "DiscDate": disc_date,
        "CurPerType": cur_per_type,
        "FiscalYearEndDate": fy_end,
        "NetSales": net_sales,
        "OperatingProfit": op_profit,
        "OrdinaryProfit": ord_profit,
        "Profit": profit,
        "EPS": eps,
        "BPS": bps,
        "DivAnn": div_ann,
    }
    conn.execute(
        "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, disc_date, json.dumps(data), time.time()),
    )


def _insert_margin(
    conn: sqlite3.Connection,
    code: str,
    date: str,
    *,
    long_vol: float = 10_000.0,
    short_vol: float = 5_000.0,
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "LongVol": long_vol,
        "ShrtVol": short_vol,
        "LongNegVol": 0.0,
        "ShrtNegVol": 0.0,
        "LongStdVol": long_vol,
        "ShrtStdVol": short_vol,
    }
    conn.execute(
        "INSERT OR REPLACE INTO markets_margin_interest (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), time.time()),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_env(tmp_path):
    cache = _make_cache(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "cache.db"))

    # 13010: two bars + master + fins_summary (FY) + margin
    _insert_bar(conn, "13010", "2026-05-01", 1000.0)
    _insert_bar(conn, "13010", "2026-05-02", 1050.0)
    _insert_master(conn, "13010", "Test Corp")
    _insert_fins(
        conn,
        "13010",
        "2026-05-01",
        net_sales=500_000,
        op_profit=50_000,
        ord_profit=48_000,
        profit=35_000,
        eps=100.0,
        bps=1_000.0,
        div_ann=20.0,
    )
    _insert_margin(conn, "13010", "2026-05-02", long_vol=10_000.0, short_vol=5_000.0)
    conn.commit()
    conn.close()

    settings = Settings()
    settings.jquants_plan = "premium"
    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"cache": cache, "tmp_path": tmp_path}

    cache.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetStockBriefing:
    async def test_basic_fields_returned(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        # display_code strips trailing "0" → "13010" becomes "1301"
        assert data["code"] == "1301"
        assert data["name"] == "Test Corp"
        assert data["market"] == "Prime"
        assert data["sector_17"] == "Electric Appliances"
        assert data["sector_33"] == "Electric Machinery"

    async def test_latest_price(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        price = data["price"]
        assert price["date"] == "2026-05-02"
        assert price["close"] == 1050.0
        assert price["volume"] == 10_000

    async def test_change_pct(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        # (1050 - 1000) / 1000 * 100 = 5.0 %
        assert data["price"]["change_pct"] == pytest.approx(5.0, rel=1e-3)

    async def test_financials(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        fins = data["financials"]
        assert fins["revenue"] == 500_000
        assert fins["operating_profit"] == 50_000
        assert fins["net_income"] == 35_000
        assert fins["fiscal_period"] == "FY"

    async def test_valuation_per_pbr(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        val = data["valuation"]
        # PER = 1050 / 100 = 10.5
        assert val["per"] == pytest.approx(10.5, rel=1e-3)
        # PBR = 1050 / 1000 = 1.05
        assert val["pbr"] == pytest.approx(1.05, rel=1e-3)

    async def test_valuation_roe(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        val = data["valuation"]
        # ROE = EPS / BPS * 100 = 100 / 1000 * 100 = 10.0
        assert val["roe"] == pytest.approx(10.0, rel=1e-3)

    async def test_dividend_yield(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"})
        data = _call(result)

        val = data["valuation"]
        # yield = round(20 / 1050 * 100, 2) = 1.90
        assert val["dividend_yield_pct"] == pytest.approx(round(20 / 1050 * 100, 2), rel=1e-6)

    async def test_per_null_when_eps_negative(self, tmp_path):
        """PER must be null when the company is in a net-loss period (EPS <= 0)."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13020", "2026-05-02", 500.0)
        _insert_master(conn, "13020", "Loss Corp")
        _insert_fins(conn, "13020", "2026-05-01", eps=-50.0, bps=800.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13020"})
            data = _call(result)

        cache.close()
        assert data["valuation"]["per"] is None
        assert data["valuation"]["roe"] is None  # ROE also null when EPS <= 0
        assert data["valuation"]["pbr"] is not None  # PBR still valid

    async def test_stale_div_ann_yields_null(self, tmp_path):
        """DivAnn disclosed more than 18 months ago must not produce a dividend yield."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13030", "2026-05-02", 1000.0)
        _insert_master(conn, "13030", "Stale Div Corp")
        # disc_date 3 years ago → stale
        _insert_fins(conn, "13030", "2023-05-01", div_ann=30.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13030"})
            data = _call(result)

        cache.close()
        assert data["valuation"]["dividend_yield_pct"] is None
        assert data["valuation"]["div_per_share"] is None

    async def test_split_adjusted_per(self, tmp_path):
        """PER must use split-adjusted EPS (AdjEPS) so a 1:2 split halves EPS correctly."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Bar on split date: adj_factor = 0.5 (1:2 split)
        _insert_bar(conn, "13040", "2026-03-28", 1500.0, adj_factor=0.5)
        # Latest bar after split (no split here, adj_factor = 1.0)
        _insert_bar(conn, "13040", "2026-05-02", 1600.0, adj_factor=1.0)
        _insert_master(conn, "13040", "Split Corp")
        # EPS disclosed before the split → needs 0.5 adjustment → AdjEPS = 200 * 0.5 = 100
        _insert_fins(conn, "13040", "2026-02-10", eps=200.0, bps=2000.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13040"})
            data = _call(result)

        cache.close()
        val = data["valuation"]
        # Without split adj: PER = 1600 / 200 = 8.0 (wrong)
        # With split adj:    PER = 1600 / 100 = 16.0 (correct)
        assert val["per"] == pytest.approx(16.0, rel=1e-3)
        assert val["pbr"] == pytest.approx(1600 / 1000, rel=1e-3)  # AdjBPS = 2000 * 0.5 = 1000
        # ROE = AdjEPS / AdjBPS * 100 = 100 / 1000 * 100 = 10.0 (split-invariant: 200/2000*100 = 10.0)
        assert val["roe"] == pytest.approx(10.0, rel=1e-3)

    async def test_no_price_data_returns_error(self, tmp_path):
        cache = _make_cache(tmp_path)
        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "99999"})
            data = _call(result)

        cache.close()
        assert "error" in data

    async def test_invalid_code_returns_validation_error(self, mock_env):
        result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "XXXX"})
        data = _call(result)
        assert "error" in data or "errors" in data

    async def test_tier2_cache_hit(self, mock_env):
        """Second call must return the cached result (same dict)."""
        r1 = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"}))
        r2 = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"}))
        assert r1 == r2

    async def test_no_fins_data_returns_null_valuation(self, tmp_path):
        """When fins_summary has no FY row, valuation fields are null."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13050", "2026-05-01", 800.0)
        _insert_bar(conn, "13050", "2026-05-02", 820.0)
        _insert_master(conn, "13050", "No Fins Corp")
        # Intentionally no fins_summary row
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool("get_stock_briefing", {"code": "13050"})
            data = _call(result)

        cache.close()
        assert data["valuation"]["per"] is None
        assert data["valuation"]["pbr"] is None
        assert data["financials"]["revenue"] is None

    async def test_margin_ratio(self, mock_env):
        """margin.ratio = long_vol / short_vol (margin ratio)."""
        data = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"}))
        margin = data["margin"]
        assert margin["date"] == "2026-05-02"
        assert margin["long_vol"] == 10_000.0
        assert margin["short_vol"] == 5_000.0
        # 10000 / 5000 = 2.0
        assert margin["ratio"] == pytest.approx(2.0, rel=1e-3)

    async def test_margin_ratio_null_when_short_zero(self, tmp_path):
        """margin.ratio must be null when short_vol == 0 (division by zero guard)."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13060", "2026-05-02", 1000.0)
        _insert_master(conn, "13060", "Zero Short Corp")
        _insert_margin(conn, "13060", "2026-05-02", long_vol=5_000.0, short_vol=0.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13060"}))

        cache.close()
        assert data["margin"]["ratio"] is None
        assert data["margin"]["long_vol"] == 5_000.0
        assert data["margin"]["short_vol"] == 0.0

    async def test_margin_null_when_no_data(self, tmp_path):
        """margin fields are all null when no markets_margin_interest row exists."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13070", "2026-05-02", 1000.0)
        _insert_master(conn, "13070", "No Margin Corp")
        # Intentionally no margin row
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13070"}))

        cache.close()
        margin = data["margin"]
        assert margin["ratio"] is None
        assert margin["long_vol"] is None
        assert margin["short_vol"] is None
        assert margin["date"] is None

    async def test_sector_short_sale_ratio_present(self, mock_env):
        """margin.sector_short_sale_ratio is populated when short_ratio cache has data."""
        # mock_env uses S33 = "3050" for code 13010 (from _insert_master)
        # (350+75)/(575+350+75)*100 = 42.5%
        mock_env["cache"].put_rows(
            "markets_short_ratio",
            [
                {
                    "S33": "3050",
                    "Date": "2026-05-02",
                    "SellExShortVa": 575000000,
                    "ShrtWithResVa": 350000000,
                    "ShrtNoResVa": 75000000,
                }
            ],
            key_columns=["S33", "Date"],
        )
        data = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"}))
        margin = data["margin"]
        assert margin["sector_short_sale_ratio"] == pytest.approx(42.5)
        assert margin["sector_short_ratio_date"] == "2026-05-02"

    async def test_sector_short_sale_ratio_null_when_not_cached(self, mock_env):
        """margin.sector_short_sale_ratio is null when markets_short_ratio is empty."""
        # No short_ratio rows seeded — should gracefully return null
        data = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "13010"}))
        assert data["margin"]["sector_short_sale_ratio"] is None
        assert data["margin"]["sector_short_ratio_date"] is None

    async def test_fye_split_before_disc_adjusts_div_yield(self, tmp_path):
        """DivAnn and dividend_yield are corrected for FY-end splits before disc_date.

        Mirrors the 京王電鉄 (9008) real-world case:
          90080: 5:1 split on 2026-03-30 (adj_factor=0.2).
                 FY disclosed 2026-05-13 with DivAnn=110 (pre-split).
                 EPS=150, BPS=1500 are already in post-split terms per Japanese GAAP.
                 Current AdjC=775.
        Expected div_per_share = 22 (= 110 * 0.2).
        Expected dividend_yield_pct = 22/775*100 ≈ 2.84%.
        PER and PBR must use EPS/BPS unchanged (post-split already).
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "90080", "2026-03-30", 799.0, adj_factor=0.2)
        _insert_bar(conn, "90080", "2026-05-13", 775.0)
        _insert_master(conn, "90080", "京王電鉄")
        _insert_fins(
            conn,
            "90080",
            "2026-05-13",
            eps=150.0,
            bps=1500.0,
            div_ann=110.0,
            fy_end="2026-03-31",
        )
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(await server_module.mcp.call_tool("get_stock_briefing", {"code": "90080"}))

        cache.close()
        val = data["valuation"]
        # div_per_share must be split-adjusted: 110 * 0.2 = 22
        assert val["div_per_share"] == pytest.approx(22.0, rel=1e-3)
        assert val["dividend_yield_pct"] == pytest.approx(22.0 / 775.0 * 100, rel=1e-2)
        # EPS/BPS unchanged (already post-split in the disclosure)
        assert val["eps"] == pytest.approx(150.0, rel=1e-3)
        assert val["bps"] == pytest.approx(1500.0, rel=1e-3)
        # PER = AdjC / EPS = 775 / 150
        assert val["per"] == pytest.approx(775.0 / 150.0, rel=1e-2)

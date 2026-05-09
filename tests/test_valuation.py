"""Tests for tools/valuation.py (get_sector_briefing)."""

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
    s17: str = "3050",
    s17_name: str = "Electric Appliances",
    s33: str = "3050",
    s33_name: str = "Electric Machinery",
    mkt_name: str = "Prime",
    date: str = "2026-05-01",
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "CoName": name,
        "S17": s17,
        "S17Nm": s17_name,
        "S33": s33,
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
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "C": adj_c,
        "AdjC": adj_c,
        "Vo": 10_000,
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
    eps: float = 100.0,
    bps: float = 1_000.0,
    cur_per_type: str = "FY",
    fy_end: str = "2026-03-31",
) -> None:
    data = {
        "Code": code,
        "DiscDate": disc_date,
        "CurPerType": cur_per_type,
        "FiscalYearEndDate": fy_end,
        "EPS": eps,
        "BPS": bps,
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
    }
    conn.execute(
        "INSERT OR REPLACE INTO markets_margin_interest (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), time.time()),
    )


# ---------------------------------------------------------------------------
# Fixture: two stocks in the same sector, one in a different sector
# ---------------------------------------------------------------------------
#
# Stock layout:
#   13010  s33=3050 "Electric Machinery"  close=1000  EPS=100  BPS=1000  LongVol=10000  ShrtVol=5000  ratio=2.0
#   13020  s33=3050 "Electric Machinery"  close=2000  EPS=200  BPS=1500  LongVol=3000   ShrtVol=1000  ratio=3.0
#   13030  s33=5050 "Chemical"            close=500   EPS=50   BPS=600   LongVol=2000   ShrtVol=4000  ratio=0.5


@pytest.fixture()
def mock_env(tmp_path):
    cache = _make_cache(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "cache.db"))

    for code, close, eps, bps, s33, s33_name, long_vol, short_vol in [
        ("13010", 1000.0, 100.0, 1000.0, "3050", "Electric Machinery", 10_000.0, 5_000.0),
        ("13020", 2000.0, 200.0, 1500.0, "3050", "Electric Machinery", 3_000.0, 1_000.0),
        ("13030", 500.0, 50.0, 600.0, "5050", "Chemical", 2_000.0, 4_000.0),
    ]:
        _insert_bar(conn, code, "2026-05-02", close)
        _insert_master(conn, code, f"Corp {code}", s33=s33, s33_name=s33_name)
        _insert_fins(conn, code, "2026-05-01", eps=eps, bps=bps)
        _insert_margin(conn, code, "2026-05-02", long_vol=long_vol, short_vol=short_vol)

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
class TestGetSectorBriefing:
    async def test_response_shape(self, mock_env):
        result = await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        data = _call(result)
        assert data["price_date"] == "2026-05-02"
        assert data["sector_type"] == "s33"
        assert isinstance(data["sectors"], list)
        elec = data["sectors"][0]
        assert "margin_ratio_median" in elec
        assert "margin_ratio_count" in elec

    async def test_sector_count(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        # Two distinct s33 sectors in fixture
        assert len(data["sectors"]) == 2

    async def test_per_median_two_stocks(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        # PER values: 1000/100=10.0, 2000/200=10.0 → median = 10.0
        assert elec["per_median"] == pytest.approx(10.0, rel=1e-3)
        assert elec["per_count"] == 2

    async def test_pbr_median_two_stocks(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        # PBR values: 1000/1000=1.0, 2000/1500≈1.333 → median = 1.1666... → round(,2) = 1.17
        assert elec["pbr_median"] == pytest.approx(round((1.0 + 2000 / 1500) / 2, 2), rel=1e-6)
        assert elec["pbr_count"] == 2

    async def test_roe_median(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        # ROE: 100/1000*100=10%, 200/1500*100≈13.33% → median ≈ 11.67%
        assert elec["roe_median"] == pytest.approx((10.0 + 200 / 1500 * 100) / 2, rel=1e-3)
        assert elec["roe_count"] == 2

    async def test_single_sector_stock(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        chem = next(s for s in data["sectors"] if s["code"] == "5050")
        assert chem["per_median"] == pytest.approx(500.0 / 50.0, rel=1e-3)
        assert chem["per_count"] == 1

    async def test_sorted_by_per_ascending(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        pers = [s["per_median"] for s in data["sectors"] if s["per_median"] is not None]
        assert pers == sorted(pers)

    async def test_per_null_when_eps_negative(self, tmp_path):
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13010", "2026-05-02", 1000.0)
        _insert_master(conn, "13010", "Loss Corp", s33="3050", s33_name="Electric Machinery")
        _insert_fins(conn, "13010", "2026-05-01", eps=-50.0, bps=1000.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(
                await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
            )
        cache.close()

        elec = next((s for s in data["sectors"] if s["code"] == "3050"), None)
        assert elec is not None
        assert elec["per_median"] is None
        assert elec["per_count"] == 0
        # PBR still valid (BPS > 0)
        assert elec["pbr_median"] is not None

    async def test_split_adjusted_per(self, tmp_path):
        """PER must use split-adjusted EPS so a 1:2 split halves EPS correctly."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Bar on split date: adj_factor = 0.5 (1:2 split)
        _insert_bar(conn, "13010", "2026-03-28", 1600.0, adj_factor=0.5)
        # Latest bar after split
        _insert_bar(conn, "13010", "2026-05-02", 1600.0, adj_factor=1.0)
        _insert_master(conn, "13010", "Split Corp", s33="3050", s33_name="Electric Machinery")
        # EPS disclosed before split → needs 0.5 factor → AdjEPS = 200 * 0.5 = 100
        _insert_fins(conn, "13010", "2026-02-10", eps=200.0, bps=2000.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(
                await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
            )
        cache.close()

        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        # Without adj: PER = 1600/200 = 8.0 (wrong)
        # With adj:    PER = 1600/100 = 16.0 (correct)
        assert elec["per_median"] == pytest.approx(16.0, rel=1e-3)
        # PBR: close/AdjBPS = 1600/(2000*0.5) = 1600/1000 = 1.6
        assert elec["pbr_median"] == pytest.approx(1.6, rel=1e-3)

    async def test_s17_aggregation(self, mock_env):
        """s17 should produce sector groupings using the s17 column."""
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s17"})
        )
        assert data["sector_type"] == "s17"
        # Fixture uses same s17 code "3050" for all three stocks
        assert len(data["sectors"]) == 1
        elec = data["sectors"][0]
        assert elec["count"] == 3

    async def test_invalid_sector_type(self, mock_env):
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s99"})
        )
        assert "error" in data or "errors" in data

    async def test_tier2_cache_hit(self, mock_env):
        r1 = _call(await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"}))
        r2 = _call(await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"}))
        assert r1 == r2

    async def test_no_price_data_returns_error(self, tmp_path):
        cache = _make_cache(tmp_path)
        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(
                await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
            )
        cache.close()
        assert "error" in data

    async def test_margin_ratio_median_two_stocks(self, mock_env):
        """margin_ratio_median must be the median of per-stock LongVol/ShrtVol."""
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        # ratios: 10000/5000=2.0, 3000/1000=3.0 → median = 2.5
        assert elec["margin_ratio_median"] == pytest.approx(2.5, rel=1e-3)
        assert elec["margin_ratio_count"] == 2

    async def test_margin_ratio_median_single_stock(self, mock_env):
        """Single stock sector: margin_ratio_median equals that stock's ratio."""
        data = _call(
            await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
        )
        chem = next(s for s in data["sectors"] if s["code"] == "5050")
        # ratio: 2000/4000 = 0.5
        assert chem["margin_ratio_median"] == pytest.approx(0.5, rel=1e-3)
        assert chem["margin_ratio_count"] == 1

    async def test_margin_ratio_null_when_no_margin_data(self, tmp_path):
        """margin_ratio_median must be null when markets_margin_interest has no rows."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13010", "2026-05-02", 1000.0)
        _insert_master(conn, "13010", "Corp 13010")
        _insert_fins(conn, "13010", "2026-05-01", eps=100.0, bps=1000.0)
        # Intentionally no margin row
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(
                await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
            )
        cache.close()

        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        assert elec["margin_ratio_median"] is None
        assert elec["margin_ratio_count"] == 0

    async def test_margin_ratio_excludes_zero_short(self, tmp_path):
        """Stocks with ShrtVol == 0 must not contribute to margin_ratio_median."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13010", "2026-05-02", 1000.0)
        _insert_master(conn, "13010", "Corp 13010")
        _insert_fins(conn, "13010", "2026-05-01", eps=100.0, bps=1000.0)
        _insert_margin(conn, "13010", "2026-05-02", long_vol=5_000.0, short_vol=0.0)
        conn.commit()
        conn.close()

        settings = Settings()
        settings.jquants_plan = "premium"
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_cache", cache),
        ):
            data = _call(
                await server_module.mcp.call_tool("get_sector_briefing", {"sector_type": "s33"})
            )
        cache.close()

        elec = next(s for s in data["sectors"] if s["code"] == "3050")
        assert elec["margin_ratio_median"] is None
        assert elec["margin_ratio_count"] == 0

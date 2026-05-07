"""Tests for tools/market_overview.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call(result):
    """Unwrap the text content from a tool call result."""
    return json.loads(result.content[0].text)


def _make_cache(tmp_path: Path) -> CacheStore:
    """Create a minimal CacheStore backed by an in-memory SQLite DB."""
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE equities_bars_daily "
        "(code TEXT NOT NULL, date TEXT NOT NULL, adj_factor REAL, "
        "data TEXT, fetched_at REAL, PRIMARY KEY (code, date))"
    )
    conn.execute("CREATE INDEX idx_ebd_date ON equities_bars_daily (date)")
    conn.execute(
        "CREATE TABLE equities_master "
        "(code TEXT NOT NULL, date TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'standard', "
        "data TEXT, fetched_at REAL, PRIMARY KEY (code, date))"
    )
    conn.commit()
    conn.close()
    settings = Settings()
    settings.jquants_plan = "premium"
    return CacheStore(db_path, settings)


def _insert_master(
    conn: sqlite3.Connection, code: str, name: str, date: str = "2026-05-01"
) -> None:
    data = {"Code": code, "Date": date, "CoName": name, "CoNameEn": name + " Co"}
    conn.execute(
        "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), 0.0),
    )


def _insert_bar(
    conn: sqlite3.Connection,
    code: str,
    date: str,
    adj_c: float,
    vo: int = 1000,
    c: float | None = None,
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "O": c or adj_c,
        "H": adj_c + 100,
        "L": adj_c - 100,
        "C": c or adj_c,
        "AdjC": adj_c,
        "Vo": vo,
        "Va": adj_c * vo,
        "AdjFactor": 1.0,
        "UL": 0,
        "LL": 0,
    }
    conn.execute(
        "INSERT OR REPLACE INTO equities_bars_daily (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), 0.0),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_day_cache(tmp_path):
    """Cache with 3 stocks over 2 dates: prev=2026-05-01, today=2026-05-02."""
    cache = _make_cache(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "cache.db"))
    # 13010: rose (100 -> 110)
    _insert_bar(conn, "13010", "2026-05-01", 100.0, vo=5000)
    _insert_bar(conn, "13010", "2026-05-02", 110.0, vo=6000)
    # 13020: fell (200 -> 180)
    _insert_bar(conn, "13020", "2026-05-01", 200.0, vo=3000)
    _insert_bar(conn, "13020", "2026-05-02", 180.0, vo=2000)
    # 13030: unchanged (300 -> 300)
    _insert_bar(conn, "13030", "2026-05-01", 300.0, vo=1000)
    _insert_bar(conn, "13030", "2026-05-02", 300.0, vo=1000)
    conn.commit()
    conn.close()
    return cache


@pytest.fixture()
def mock_server(two_day_cache):
    with (
        patch.object(server_module, "_settings", Settings()),
        patch.object(server_module, "_cache", two_day_cache),
    ):
        yield server_module.mcp


# ---------------------------------------------------------------------------
# detect_price_change
# ---------------------------------------------------------------------------


class TestDetectPriceChange:
    @pytest.mark.asyncio
    async def test_basic_counts(self, mock_server):
        result = await mock_server.call_tool("detect_price_change", {"date": "2026-05-02"})
        data = _call(result)
        assert data["date"] == "2026-05-02"
        assert data["previous_date"] == "2026-05-01"
        assert data["advances"] == 1
        assert data["declines"] == 1
        assert data["unchanged"] == 1
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_advance_decline_ratio(self, mock_server):
        result = await mock_server.call_tool("detect_price_change", {"date": "2026-05-02"})
        data = _call(result)
        assert data["advance_decline_ratio"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_invalid_date(self, mock_server):
        result = await mock_server.call_tool("detect_price_change", {"date": "not-a-date"})
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_cache_not_ready(self, mock_server):
        result = await mock_server.call_tool("detect_price_change", {"date": "2099-01-01"})
        data = _call(result)
        assert data["error_type"] == "CacheNotReady"


# ---------------------------------------------------------------------------
# get_advance_decline_ratio
# ---------------------------------------------------------------------------


class TestGetAdvanceDeclineRatio:
    @pytest.fixture()
    def multi_day_cache(self, tmp_path):
        """5 trading days: 3 ups and 2 downs each day for simplicity."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        dates = ["2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01", "2026-05-02"]
        # stock A: always rises day-over-day (100, 110, 120, 130, 140)
        # stock B: always falls (200, 190, 180, 170, 160)
        # stock C: rises on even indexes, falls on odd (300, 310, 300, 310, 300)
        for i, d in enumerate(dates):
            _insert_bar(conn, "A0000", d, 100.0 + i * 10)
            _insert_bar(conn, "B0000", d, 200.0 - i * 10)
            _insert_bar(conn, "C0000", d, 310.0 if i % 2 == 0 else 300.0)
        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_server_multi(self, multi_day_cache):
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", multi_day_cache),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_ratio_calculated(self, mock_server_multi):
        result = await mock_server_multi.call_tool(
            "get_advance_decline_ratio", {"date": "2026-05-02", "period": 4}
        )
        data = _call(result)
        # A always up (4), B always down (4), C alternates up/down/up/down over 4 days
        # period 1: A↑ B↓ C↓ → adv=1 dec=2
        # period 2: A↑ B↓ C↑ → adv=2 dec=1
        # period 3: A↑ B↓ C↓ → adv=1 dec=2
        # period 4: A↑ B↓ C↑ → adv=2 dec=1
        # total: advances=6 declines=6 ratio=100.0
        assert data["period"] == 4
        assert data["advances_sum"] == 6
        assert data["declines_sum"] == 6
        assert data["ratio"] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_ratio_null_when_no_declines(self, tmp_path):
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        for i, d in enumerate(["2026-04-30", "2026-05-01", "2026-05-02"]):
            _insert_bar(conn, "A0000", d, 100.0 + i * 10)
            _insert_bar(conn, "B0000", d, 200.0 + i * 10)
        conn.commit()
        conn.close()
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool(
                "get_advance_decline_ratio", {"date": "2026-05-02", "period": 2}
            )
        data = _call(result)
        assert data["declines_sum"] == 0
        assert data["ratio"] is None

    @pytest.mark.asyncio
    async def test_invalid_period(self, mock_server_multi):
        result = await mock_server_multi.call_tool(
            "get_advance_decline_ratio", {"date": "2026-05-02", "period": 0}
        )
        data = _call(result)
        assert data.get("error") is True


# ---------------------------------------------------------------------------
# get_top_movers
# ---------------------------------------------------------------------------


class TestGetTopMovers:
    @pytest.mark.asyncio
    async def test_top_gainer(self, mock_server):
        result = await mock_server.call_tool(
            "get_top_movers", {"date": "2026-05-02", "direction": "up", "n": 3}
        )
        data = _call(result)
        assert data["direction"] == "up"
        items = data["items"]
        assert len(items) <= 3
        # 13010 rose 10% — should be top
        top = items[0]
        assert top["code"] == "1301"
        assert top["change_pct"] == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_top_loser(self, mock_server):
        result = await mock_server.call_tool(
            "get_top_movers", {"date": "2026-05-02", "direction": "down", "n": 3}
        )
        data = _call(result)
        assert data["direction"] == "down"
        items = data["items"]
        # 13020 fell 10% — should be top loser
        top = items[0]
        assert top["code"] == "1302"
        assert top["change_pct"] == pytest.approx(-10.0)

    @pytest.mark.asyncio
    async def test_sorted_descending_for_up(self, mock_server):
        result = await mock_server.call_tool(
            "get_top_movers", {"date": "2026-05-02", "direction": "up", "n": 10}
        )
        data = _call(result)
        pcts = [item["change_pct"] for item in data["items"]]
        assert pcts == sorted(pcts, reverse=True)

    @pytest.mark.asyncio
    async def test_invalid_direction(self, mock_server):
        result = await mock_server.call_tool(
            "get_top_movers", {"date": "2026-05-02", "direction": "sideways"}
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_n_exceeds_available(self, mock_server):
        result = await mock_server.call_tool("get_top_movers", {"date": "2026-05-02", "n": 100})
        data = _call(result)
        # Only 3 stocks in fixture — result should have at most 3
        assert len(data["items"]) <= 3


# ---------------------------------------------------------------------------
# get_top_volume
# ---------------------------------------------------------------------------


class TestGetTopVolume:
    @pytest.mark.asyncio
    async def test_sorted_by_volume(self, mock_server):
        result = await mock_server.call_tool("get_top_volume", {"date": "2026-05-02", "n": 10})
        data = _call(result)
        assert data["date"] == "2026-05-02"
        volumes = [item["volume"] for item in data["items"]]
        assert volumes == sorted(volumes, reverse=True)

    @pytest.mark.asyncio
    async def test_top_is_highest_volume(self, mock_server):
        result = await mock_server.call_tool("get_top_volume", {"date": "2026-05-02", "n": 1})
        data = _call(result)
        # 13010 has vo=6000 on 2026-05-02 — highest
        assert data["items"][0]["code"] == "1301"
        assert data["items"][0]["volume"] == 6000

    @pytest.mark.asyncio
    async def test_fields_present(self, mock_server):
        result = await mock_server.call_tool("get_top_volume", {"date": "2026-05-02"})
        data = _call(result)
        item = data["items"][0]
        assert "code" in item
        assert "volume" in item
        assert "turnover_value" in item
        assert "close" in item

    @pytest.mark.asyncio
    async def test_n_too_large(self, mock_server):
        result = await mock_server.call_tool("get_top_volume", {"date": "2026-05-02", "n": 101})
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_non_trading_day_returns_error(self, mock_server):
        # 2026-04-30 is within cache range (latest=2026-05-02) but has no data in fixture
        result = await mock_server.call_tool("get_top_volume", {"date": "2026-04-30"})
        data = _call(result)
        assert data.get("error") is True
        assert data["error_type"] == "NoTradingData"


# ---------------------------------------------------------------------------
# get_top_turnover_value
# ---------------------------------------------------------------------------


class TestGetTopTurnoverValue:
    @pytest.fixture()
    def turnover_cache(self, tmp_path):
        """Cache where the volume ranking and turnover ranking diverge.

        Stock A: low price, high volume → tops volume ranking
        Stock B: high price, medium volume → tops turnover ranking
        Stock C: middle price, low volume → middle of both
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # A: price 10, volume 1,000,000 → Va = 10,000,000
        _insert_bar(conn, "10000", "2026-05-02", 10.0, vo=1_000_000)
        # B: price 5000, volume 5000 → Va = 25,000,000 (highest turnover)
        _insert_bar(conn, "20000", "2026-05-02", 5000.0, vo=5_000)
        # C: price 1000, volume 10000 → Va = 10,000,000 (ties with A on turnover)
        _insert_bar(conn, "30000", "2026-05-02", 1000.0, vo=10_000)
        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_turnover(self, turnover_cache):
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", turnover_cache),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_sorted_by_turnover_value(self, mock_turnover):
        result = await mock_turnover.call_tool(
            "get_top_turnover_value", {"date": "2026-05-02", "n": 10}
        )
        data = _call(result)
        assert data["date"] == "2026-05-02"
        values = [item["turnover_value"] for item in data["items"]]
        assert values == sorted(values, reverse=True)

    @pytest.mark.asyncio
    async def test_top_differs_from_volume_ranking(self, mock_turnover):
        # Volume ranking: A (1M shares) > C (10k shares) > B (5k shares)
        # Turnover ranking: B (¥25M) > A (¥10M) ≥ C (¥10M)
        # So top by turnover should be B (2000), not A (1000) which leads volume.
        result = await mock_turnover.call_tool(
            "get_top_turnover_value", {"date": "2026-05-02", "n": 1}
        )
        data = _call(result)
        assert data["items"][0]["code"] == "2000"
        assert data["items"][0]["turnover_value"] == pytest.approx(25_000_000.0)

    @pytest.mark.asyncio
    async def test_volume_ranking_picks_low_priced_stock(self, mock_turnover):
        # Sanity check: get_top_volume picks A (low-priced, high-volume) at the
        # top, demonstrating that volume and turnover rankings differ.
        result = await mock_turnover.call_tool("get_top_volume", {"date": "2026-05-02", "n": 1})
        data = _call(result)
        assert data["items"][0]["code"] == "1000"
        assert data["items"][0]["volume"] == 1_000_000

    @pytest.mark.asyncio
    async def test_fields_present(self, mock_turnover):
        result = await mock_turnover.call_tool("get_top_turnover_value", {"date": "2026-05-02"})
        data = _call(result)
        item = data["items"][0]
        for key in ("code", "name", "turnover_value", "volume", "close"):
            assert key in item

    @pytest.mark.asyncio
    async def test_n_too_large(self, mock_turnover):
        result = await mock_turnover.call_tool(
            "get_top_turnover_value", {"date": "2026-05-02", "n": 101}
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_non_trading_day_returns_error(self, mock_turnover):
        result = await mock_turnover.call_tool("get_top_turnover_value", {"date": "2026-04-30"})
        data = _call(result)
        assert data.get("error") is True
        assert data["error_type"] == "NoTradingData"

    @pytest.mark.asyncio
    async def test_invalid_date(self, mock_turnover):
        result = await mock_turnover.call_tool("get_top_turnover_value", {"date": "not-a-date"})
        data = _call(result)
        assert data.get("error") is True


# ---------------------------------------------------------------------------
# name field injection
# ---------------------------------------------------------------------------


class TestNameField:
    """get_top_movers / get_top_volume / get_top_turnover_value all inject ``name`` per item."""

    @pytest.fixture()
    def named_cache(self, tmp_path):
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13010", "2026-05-01", 100.0, vo=5000)
        _insert_bar(conn, "13010", "2026-05-02", 110.0, vo=6000)
        _insert_bar(conn, "13020", "2026-05-01", 200.0, vo=3000)
        _insert_bar(conn, "13020", "2026-05-02", 180.0, vo=2000)
        _insert_master(conn, "13010", "テスト一番")
        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_named(self, named_cache):
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", named_cache),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_top_movers_name_populated(self, mock_named):
        result = await mock_named.call_tool(
            "get_top_movers", {"date": "2026-05-02", "direction": "up", "n": 2}
        )
        data = _call(result)
        items = {i["code"]: i for i in data["items"]}
        assert items["1301"]["name"] == "テスト一番"
        assert items["1302"]["name"] is None

    @pytest.mark.asyncio
    async def test_top_movers_name_key_always_present(self, mock_named):
        result = await mock_named.call_tool(
            "get_top_movers", {"date": "2026-05-02", "direction": "up"}
        )
        data = _call(result)
        for item in data["items"]:
            assert "name" in item

    @pytest.mark.asyncio
    async def test_top_volume_name_populated(self, mock_named):
        result = await mock_named.call_tool("get_top_volume", {"date": "2026-05-02", "n": 2})
        data = _call(result)
        items = {i["code"]: i for i in data["items"]}
        assert items["1301"]["name"] == "テスト一番"
        assert items["1302"]["name"] is None

    @pytest.mark.asyncio
    async def test_top_volume_name_key_always_present(self, mock_named):
        result = await mock_named.call_tool("get_top_volume", {"date": "2026-05-02"})
        data = _call(result)
        for item in data["items"]:
            assert "name" in item

    @pytest.mark.asyncio
    async def test_top_turnover_value_name_populated(self, mock_named):
        result = await mock_named.call_tool(
            "get_top_turnover_value", {"date": "2026-05-02", "n": 2}
        )
        data = _call(result)
        items = {i["code"]: i for i in data["items"]}
        assert items["1301"]["name"] == "テスト一番"
        assert items["1302"]["name"] is None

    @pytest.mark.asyncio
    async def test_top_turnover_value_name_key_always_present(self, mock_named):
        result = await mock_named.call_tool("get_top_turnover_value", {"date": "2026-05-02"})
        data = _call(result)
        for item in data["items"]:
            assert "name" in item


# ---------------------------------------------------------------------------
# get_sector_performance
# ---------------------------------------------------------------------------


def _insert_master_with_sector(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    s33: str,
    s33_name: str,
    s17: str = "",
    s17_name: str = "",
    date: str = "2026-05-01",
) -> None:
    data = {
        "Code": code,
        "Date": date,
        "CoName": name,
        "CoNameEn": name + " Co",
        "S33": s33,
        "S33Nm": s33_name,
        "S17": s17,
        "S17Nm": s17_name,
    }
    conn.execute(
        "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), 0.0),
    )


class TestGetSectorPerformance:
    @pytest.fixture()
    def sector_cache(self, tmp_path):
        """Three sectors with different aggregate behaviour:

        - Banks (s33="7050"): two stocks, both up (avg ~+5%)
        - IT (s33="5250"): one stock, down (avg -10%)
        - Steel (s33="3450"): two stocks, mixed (avg 0%)
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Banks (sector 7050 / s17 7)
        _insert_bar(conn, "83060", "2026-05-01", 1000.0)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0)  # +10%
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        _insert_bar(conn, "83160", "2026-05-01", 2000.0)
        _insert_bar(conn, "83160", "2026-05-02", 2000.0)  # 0%
        _insert_master_with_sector(conn, "83160", "三井住友", "7050", "銀行業", "7", "金融")
        # IT (sector 5250 / s17 5)
        _insert_bar(conn, "97660", "2026-05-01", 5000.0)
        _insert_bar(conn, "97660", "2026-05-02", 4500.0)  # -10%
        _insert_master_with_sector(conn, "97660", "コナミ", "5250", "情報・通信業", "5", "情報通信")
        # Steel (sector 3450 / s17 3)
        _insert_bar(conn, "53010", "2026-05-01", 1000.0)
        _insert_bar(conn, "53010", "2026-05-02", 1100.0)  # +10%
        _insert_master_with_sector(conn, "53010", "新日鉄", "3450", "鉄鋼", "3", "素材・化学")
        _insert_bar(conn, "53020", "2026-05-01", 1000.0)
        _insert_bar(conn, "53020", "2026-05-02", 900.0)  # -10%
        _insert_master_with_sector(conn, "53020", "JFE", "3450", "鉄鋼", "3", "素材・化学")
        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_sector(self, sector_cache):
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", sector_cache),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_basic_aggregation_s33(self, mock_sector):
        result = await mock_sector.call_tool("get_sector_performance", {"date": "2026-05-02"})
        data = _call(result)
        assert data["date"] == "2026-05-02"
        assert data["previous_date"] == "2026-05-01"
        assert data["sector_type"] == "s33"
        sectors = {s["code"]: s for s in data["sectors"]}
        # Banks: +10% and 0% → avg = 5%
        assert sectors["7050"]["name"] == "銀行業"
        assert sectors["7050"]["count"] == 2
        assert sectors["7050"]["advances"] == 1
        assert sectors["7050"]["unchanged"] == 1
        assert sectors["7050"]["avg_change_pct"] == pytest.approx(5.0)
        # IT: -10% only → avg = -10%
        assert sectors["5250"]["avg_change_pct"] == pytest.approx(-10.0)
        # Steel: +10% and -10% → avg = 0%
        assert sectors["3450"]["count"] == 2
        assert sectors["3450"]["advances"] == 1
        assert sectors["3450"]["declines"] == 1
        assert sectors["3450"]["avg_change_pct"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_sectors_sorted_descending(self, mock_sector):
        result = await mock_sector.call_tool("get_sector_performance", {"date": "2026-05-02"})
        data = _call(result)
        pcts = [s["avg_change_pct"] for s in data["sectors"]]
        assert pcts == sorted(pcts, reverse=True)
        # Banks (+5%) > Steel (0%) > IT (-10%)
        assert data["sectors"][0]["code"] == "7050"
        assert data["sectors"][-1]["code"] == "5250"

    @pytest.mark.asyncio
    async def test_s17_aggregation_collapses_codes(self, mock_sector):
        result = await mock_sector.call_tool(
            "get_sector_performance", {"date": "2026-05-02", "sector_type": "s17"}
        )
        data = _call(result)
        assert data["sector_type"] == "s17"
        sectors = {s["code"]: s for s in data["sectors"]}
        # s17="7" Finance: same as s33 banks (2 stocks)
        assert sectors["7"]["count"] == 2
        assert sectors["7"]["avg_change_pct"] == pytest.approx(5.0)
        # s17="3" Materials: same as s33 steel
        assert sectors["3"]["count"] == 2

    @pytest.mark.asyncio
    async def test_invalid_sector_type(self, mock_sector):
        result = await mock_sector.call_tool(
            "get_sector_performance", {"date": "2026-05-02", "sector_type": "s99"}
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_invalid_date(self, mock_sector):
        result = await mock_sector.call_tool("get_sector_performance", {"date": "not-a-date"})
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_cache_not_ready(self, mock_sector):
        result = await mock_sector.call_tool("get_sector_performance", {"date": "2099-01-01"})
        data = _call(result)
        assert data["error_type"] == "CacheNotReady"

    @pytest.mark.asyncio
    async def test_stocks_without_sector_are_skipped(self, tmp_path):
        # Stock with bar data but no equities_master entry → has no sector
        # mapping, should be silently dropped from aggregation.
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業")
        # Orphan: bar present but no master
        _insert_bar(conn, "99999", "2026-05-01", 500.0)
        _insert_bar(conn, "99999", "2026-05-02", 600.0)
        conn.commit()
        conn.close()
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool(
                "get_sector_performance", {"date": "2026-05-02"}
            )
        data = _call(result)
        sectors = {s["code"]: s for s in data["sectors"]}
        # Only the bank shows up — orphan dropped
        assert "7050" in sectors
        assert sectors["7050"]["count"] == 1
        assert len(data["sectors"]) == 1

    @pytest.mark.asyncio
    async def test_stocks_with_empty_sector_code_are_skipped(self, tmp_path):
        # equities_master row exists but S33 is an empty string (J-Quants
        # occasionally emits this for unclassified or special-listing
        # securities). Such stocks should be skipped from aggregation.
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業")
        # Master row exists but sector code is blank
        _insert_bar(conn, "12340", "2026-05-01", 500.0)
        _insert_bar(conn, "12340", "2026-05-02", 600.0)
        _insert_master_with_sector(conn, "12340", "未分類銘柄", "", "")
        conn.commit()
        conn.close()
        with (
            patch.object(server_module, "_settings", Settings()),
            patch.object(server_module, "_cache", cache),
        ):
            result = await server_module.mcp.call_tool(
                "get_sector_performance", {"date": "2026-05-02"}
            )
        data = _call(result)
        sectors = {s["code"]: s for s in data["sectors"]}
        assert list(sectors) == ["7050"]
        assert sectors["7050"]["count"] == 1


# ---------------------------------------------------------------------------
# get_market_briefing
# ---------------------------------------------------------------------------


class TestGetMarketBriefing:
    @pytest.fixture()
    def briefing_cache(self, tmp_path):
        """Cache with 4 stocks across 2 dates plus master rows with sector codes.

        Designed so the briefing output covers every section meaningfully:
        advances + declines + unchanged populated, multiple sectors so top/bottom
        differ, top movers and turnover both have content.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Banks (s33=7050 / s17=7) — strong sector
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)  # +10%
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        _insert_bar(conn, "83160", "2026-05-01", 2000.0, vo=5_000_000)
        _insert_bar(conn, "83160", "2026-05-02", 2200.0, vo=5_000_000)  # +10%
        _insert_master_with_sector(conn, "83160", "三井住友", "7050", "銀行業", "7", "金融")
        # IT (s33=5250 / s17=5) — weak sector
        _insert_bar(conn, "97660", "2026-05-01", 5000.0, vo=200_000)
        _insert_bar(conn, "97660", "2026-05-02", 4500.0, vo=300_000)  # -10%
        _insert_master_with_sector(conn, "97660", "コナミ", "5250", "情報・通信業", "5", "情報通信")
        # Steel (s33=3450 / s17=3) — flat sector
        _insert_bar(conn, "53010", "2026-05-01", 1000.0, vo=100_000)
        _insert_bar(conn, "53010", "2026-05-02", 1000.0, vo=100_000)  # 0%
        _insert_master_with_sector(conn, "53010", "新日鉄", "3450", "鉄鋼", "3", "素材・化学")
        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_briefing(self, briefing_cache):
        # Patch `_client` to None so the TOPIX best-effort path can't reach a
        # real J-Quants API key from the developer's home dir; the briefing
        # tool's own _call_json swallows the resulting failure and returns
        # topix_change_pct=None.
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", briefing_cache),
            patch.object(server_module, "_client", None),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_basic_shape(self, mock_briefing):
        result = await mock_briefing.call_tool("get_market_briefing", {"date": "2026-05-02"})
        data = _call(result)
        # Top-level structure
        assert data["date"] == "2026-05-02"
        assert data["previous_date"] == "2026-05-01"
        assert data["sector_type"] == "s33"
        # Summary subsection
        assert "advances" in data["summary"]
        assert "declines" in data["summary"]
        assert "unchanged" in data["summary"]
        assert "advance_decline_ratio_25d" in data["summary"]
        # TOPIX best-effort: with no client, fail-soft to None
        assert data["summary"]["topix_change_pct"] is None
        # Sectors top/bottom present
        assert isinstance(data["sectors"]["top"], list)
        assert isinstance(data["sectors"]["bottom"], list)
        # Movers and turnover lists
        assert isinstance(data["top_movers_up"], list)
        assert isinstance(data["top_movers_down"], list)
        assert isinstance(data["top_turnover_value"], list)
        # Highlights aggregate keys
        for key in (
            "ytd_new_highs",
            "ytd_new_lows",
            "volume_surges",
            "limit_high_close",
            "limit_high_touched",
            "limit_low_close",
            "limit_low_touched",
        ):
            assert key in data["highlights"]

    @pytest.mark.asyncio
    async def test_advances_declines_match(self, mock_briefing):
        # In the fixture: 2 banks +10%, 1 IT -10%, 1 steel flat → advances=2,
        # declines=1, unchanged=1.
        result = await mock_briefing.call_tool("get_market_briefing", {"date": "2026-05-02"})
        data = _call(result)
        assert data["summary"]["advances"] == 2
        assert data["summary"]["declines"] == 1
        assert data["summary"]["unchanged"] == 1

    @pytest.mark.asyncio
    async def test_sectors_top_and_bottom_differ(self, mock_briefing):
        result = await mock_briefing.call_tool("get_market_briefing", {"date": "2026-05-02"})
        data = _call(result)
        top = data["sectors"]["top"]
        bottom = data["sectors"]["bottom"]
        # Banks (avg +10%) should rank higher than IT (-10%)
        assert top[0]["code"] == "7050"
        assert bottom[0]["code"] == "5250"

    @pytest.mark.asyncio
    async def test_response_cache_hit_within_ttl(self, mock_briefing):
        # Two identical calls within the TTL must return the same payload and
        # the second one should be served from response cache.
        first = await mock_briefing.call_tool("get_market_briefing", {"date": "2026-05-02", "n": 3})
        second = await mock_briefing.call_tool(
            "get_market_briefing", {"date": "2026-05-02", "n": 3}
        )
        assert _call(first) == _call(second)

    @pytest.mark.asyncio
    async def test_equities_bars_daily_fetched_once(self, briefing_cache):
        """get_market_briefing must call get_rows('equities_bars_daily') exactly once.

        The N+1 elimination refactor fetches the full ADR span in one shot and
        passes pre-fetched rows to all _compute_* helpers.  This test pins that
        invariant so a future refactor cannot silently reintroduce extra reads.
        """
        with patch.object(briefing_cache, "get_rows", wraps=briefing_cache.get_rows) as spy:
            with (
                patch.object(server_module, "_settings", Settings(jquants_api_key="")),
                patch.object(server_module, "_cache", briefing_cache),
                patch.object(server_module, "_client", None),
            ):
                result = await server_module.mcp.call_tool(
                    "get_market_briefing", {"date": "2026-05-02"}
                )
        _call(result)  # assert no error key before checking call count
        equities_calls = [
            c for c in spy.call_args_list if c.args and c.args[0] == "equities_bars_daily"
        ]
        # Expected breakdown (total == 4):
        #   1  wide ADR fetch from the main computation
        #   3  screener sub-tool reads: detect_ytd_high_low, detect_volume_surge,
        #      detect_price_limit (each issues its own get_rows via mcp.call_tool)
        # Before this refactor the main path alone issued 5+ redundant reads
        # (one per advance/decline, sector, top-movers-up, top-movers-down,
        # top-turnover); grand total was 8+.
        assert len(equities_calls) == 4, (
            f"expected exactly 4 get_rows('equities_bars_daily') calls, "
            f"got {len(equities_calls)}: {[str(c) for c in equities_calls]}"
        )

    @pytest.mark.asyncio
    async def test_insufficient_data(self, tmp_path):
        """InsufficientData is returned when the cache has only one trading session.

        The briefing needs at least 2 sessions (today + previous) to compute
        advance/decline.  This fires when norm_date <= latest but the cache
        has no prior session — e.g. the very first day of data.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Only one date in the cache — no previous session to compare against.
        _insert_bar(conn, "13010", "2026-05-02", 100.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        assert data.get("error") is True
        assert data.get("error_type") == "InsufficientData"

    @pytest.mark.asyncio
    async def test_invalid_sector_type(self, mock_briefing):
        result = await mock_briefing.call_tool(
            "get_market_briefing", {"date": "2026-05-02", "sector_type": "s99"}
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_cache_not_ready(self, mock_briefing):
        result = await mock_briefing.call_tool("get_market_briefing", {"date": "2099-01-01"})
        data = _call(result)
        assert data.get("error_type") == "CacheNotReady"

    @pytest.mark.asyncio
    async def test_topix_change_pct_from_seeded_cache(self, tmp_path):
        # Seed both equities and TOPIX rows so the briefing's best-effort
        # path returns a real percentage (not None). Without TOPIX rows the
        # Tier-1 cache lookup is empty and the tool falls back to an API call,
        # which is fail-soft to None when the client is not available.
        from unittest.mock import MagicMock

        from jquants_mcp.client import JQuantsClient

        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Equities (need at least 1 stock + master so detect_price_change works)
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")

        # TOPIX cache table and rows. Cache hit covers the full requested
        # range so the indices tool returns from cache without an API call.
        conn.execute(
            "CREATE TABLE indices_bars_daily_topix "
            "(date TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'standard', "
            "data TEXT, fetched_at REAL, PRIMARY KEY (date))"
        )
        # 2026-05-01: 2700, 2026-05-02: 2727 → change_pct = +1.0%
        conn.execute(
            "INSERT INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-05-01", json.dumps({"Date": "2026-05-01", "Close": 2700.0}), 0.0),
        )
        conn.execute(
            "INSERT INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-05-02", json.dumps({"Date": "2026-05-02", "Close": 2727.0}), 0.0),
        )
        conn.commit()
        conn.close()

        # Pin _plan_detected and supply a stub client so the indices tool can
        # acquire a client without triggering plan auto-detection (which would
        # otherwise fire an API call and AuthenticationError on no key).
        stub_client = MagicMock(spec=JQuantsClient)
        with (
            patch.object(server_module, "_settings", Settings(jquants_plan="premium")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", stub_client),
            patch.object(server_module, "_plan_detected", True),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        assert data["summary"]["topix_change_pct"] == pytest.approx(1.0, abs=1e-3)

    @pytest.mark.asyncio
    async def test_topix_change_pct_uses_short_c_field(self, tmp_path):

        # _topix_change_pct_best_effort accepts both `Close` (current J-Quants
        # response shape) and `C` (legacy short form, see _LEGACY_FIELD_MAP in
        # cache.store). Seed rows with `C` only to pin the fallback branch so a
        # future J-Quants migration to short keys does not silently drop the
        # field on us.
        from unittest.mock import MagicMock

        from jquants_mcp.client import JQuantsClient

        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        conn.execute(
            "CREATE TABLE indices_bars_daily_topix "
            "(date TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'standard', "
            "data TEXT, fetched_at REAL, PRIMARY KEY (date))"
        )
        # Use the short `C` field, not `Close`. 2700 → 2754 = +2.0%.
        conn.execute(
            "INSERT INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-05-01", json.dumps({"Date": "2026-05-01", "C": 2700.0}), 0.0),
        )
        conn.execute(
            "INSERT INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-05-02", json.dumps({"Date": "2026-05-02", "C": 2754.0}), 0.0),
        )
        conn.commit()
        conn.close()

        stub_client = MagicMock(spec=JQuantsClient)
        with (
            patch.object(server_module, "_settings", Settings(jquants_plan="premium")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", stub_client),
            patch.object(server_module, "_plan_detected", True),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        assert data["summary"]["topix_change_pct"] == pytest.approx(2.0, abs=1e-3)


# ---------------------------------------------------------------------------
# get_dividend_yield_ranking
# ---------------------------------------------------------------------------


def _insert_fins_summary(
    conn: sqlite3.Connection, code: str, disc_date: str, div_ann: float | None
) -> None:
    data: dict = {"Code": code, "DisclosedDate": disc_date}
    if div_ann is not None:
        data["DivAnn"] = div_ann
    conn.execute(
        "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, disc_date, json.dumps(data), 0.0),
    )


class TestGetDividendYieldRanking:
    """Tests for get_dividend_yield_ranking tool."""

    @pytest.fixture()
    def yield_cache(self, tmp_path):
        """Cache with fins_summary + bars for 4 stocks on 2026-05-02.

        Yields:
          13010: DivAnn=100, AdjC=2000 → yield=5.0% (qualifies at default 3%)
          13020: DivAnn=60,  AdjC=3000 → yield=2.0% (below default 3%)
          13030: DivAnn=200, AdjC=2500 → yield=8.0% (qualifies)
          13040: no DivAnn entry        → excluded
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 2000.0)
        _insert_fins_summary(conn, "13010", "2026-03-31", 100.0)
        _insert_master(conn, "13010", "高配当A")

        _insert_bar(conn, "13020", "2026-05-02", 3000.0)
        _insert_fins_summary(conn, "13020", "2026-03-31", 60.0)
        _insert_master(conn, "13020", "低配当B")

        _insert_bar(conn, "13030", "2026-05-02", 2500.0)
        _insert_fins_summary(conn, "13030", "2026-03-31", 200.0)
        _insert_master(conn, "13030", "高配当C")

        _insert_bar(conn, "13040", "2026-05-02", 1000.0)
        _insert_master(conn, "13040", "無配D")

        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_yield_server(self, yield_cache):
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", yield_cache),
            patch.object(server_module, "_client", None),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_basic_ranking(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2026-05-02"}
        )
        data = _call(result)
        assert data["date"] == "2026-05-02"
        # Default min_yield=3.0 → 13010 (5%) and 13030 (8%) qualify
        assert data["count"] == 2
        assert len(data["items"]) == 2
        # Sorted by yield desc: 13030 (8%) first, then 13010 (5%)
        assert data["items"][0]["code"] == "1303"
        assert data["items"][1]["code"] == "1301"

    @pytest.mark.asyncio
    async def test_yield_values(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2026-05-02", "min_yield": 0.0}
        )
        data = _call(result)
        assert data["count"] == 3
        by_code = {item["code"]: item for item in data["items"]}
        assert by_code["1303"]["yield_pct"] == pytest.approx(8.0)
        assert by_code["1301"]["yield_pct"] == pytest.approx(5.0)
        assert by_code["1302"]["yield_pct"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_min_yield_filter(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2026-05-02", "min_yield": 6.0}
        )
        data = _call(result)
        assert data["count"] == 1
        assert data["items"][0]["yield_pct"] == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_name_injected(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2026-05-02"}
        )
        data = _call(result)
        by_code = {item["code"]: item["name"] for item in data["items"]}
        assert by_code["1303"] == "高配当C"
        assert by_code["1301"] == "高配当A"

    @pytest.mark.asyncio
    async def test_no_date_uses_latest(self, yield_cache):
        """Omitting date resolves to the latest cached trading day."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", yield_cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_dividend_yield_ranking", {})
        data = _call(result)
        assert data["date"] == "2026-05-02"
        assert "items" in data

    @pytest.mark.asyncio
    async def test_cache_not_ready(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2099-01-01"}
        )
        data = _call(result)
        assert data["error_type"] == "CacheNotReady"

    @pytest.mark.asyncio
    async def test_invalid_n(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2026-05-02", "n": 0}
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_invalid_min_yield(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking", {"date": "2026-05-02", "min_yield": -1.0}
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_latest_valid_div_ann_per_code(self, tmp_path):
        """Latest disclosure with positive DivAnn wins over a later empty one."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 2000.0)
        # Older (Q3) disclosure has valid DivAnn=100; newer (Q1 interim) has empty DivAnn
        _insert_fins_summary(conn, "13010", "2025-11-14", 100.0)
        _insert_fins_summary(conn, "13010", "2026-02-14", None)  # interim, no DivAnn
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking", {"date": "2026-05-02", "min_yield": 0.0}
            )
        data = _call(result)
        assert data["count"] == 1
        # Uses DivAnn=100 from the 2025-11-14 disclosure (not skipped)
        assert data["items"][0]["yield_pct"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_split_adjusted_yield(self, tmp_path):
        """DivAnn is adjusted for stock splits that occurred after disc_date.

        50010: disc_date=2025-06-30 DivAnn=86.0, split 1:10 on 2025-09-27
               (adj_factor=0.1 stored in the column), current AdjC=220.0.
        Unadjusted yield = 86/220*100 = 39.1% (the old bug).
        Adjusted yield   = (86*0.1)/220*100 = 3.9%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        # Current bar (query date)
        _insert_bar(conn, "50010", "2026-05-02", 220.0)
        # Split bar: adj_factor=0.1 stored in the column (1:10 split)
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("50010", "2025-09-27", '{"Code":"50010","AdjC":22.0}', 0.0, 0.1),
        )
        _insert_fins_summary(conn, "50010", "2025-06-30", 86.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking", {"date": "2026-05-02", "min_yield": 0.0}
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["yield_pct"] == pytest.approx(86.0 * 0.1 / 220.0 * 100, rel=1e-3)
        assert item["div_ann"] == pytest.approx(8.6, rel=1e-3)

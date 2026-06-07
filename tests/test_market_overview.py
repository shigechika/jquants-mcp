"""Tests for tools/market_overview.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache import screener_compute
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


def _insert_bar_ul(
    conn: sqlite3.Connection,
    code: str,
    d: str,
    adj_c: float,
    vo: int = 100_000,
    ul: int = 0,
    ll: int = 0,
) -> None:
    data = {
        "Code": code,
        "Date": d,
        "O": adj_c,
        "H": adj_c * 1.1,
        "L": adj_c * 0.9,
        "C": adj_c,
        "AdjC": adj_c,
        "Vo": vo,
        "Va": adj_c * vo,
        "AdjFactor": 1.0,
        "UL": ul,
        "LL": ll,
    }
    conn.execute(
        "INSERT OR REPLACE INTO equities_bars_daily (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, d, json.dumps(data), 0.0),
    )


def _make_dates(n: int = 26, end: str = "2026-05-02") -> list[str]:
    """Return n consecutive calendar dates ending at end (oldest first)."""
    end_dt = date.fromisoformat(end)
    return [(end_dt - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]


def _insert_margin(
    conn: sqlite3.Connection,
    code: str,
    date: str,
    *,
    long_vol: float = 10_000.0,
    short_vol: float = 5_000.0,
) -> None:
    data = {"Code": code, "Date": date, "LongVol": long_vol, "ShrtVol": short_vol}
    conn.execute(
        "INSERT OR REPLACE INTO markets_margin_interest (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, date, json.dumps(data), 0.0),
    )


def _make_topix_rows(
    n: int = 50,
    last_date: str = "2026-05-02",
    drops_at: list[int] | None = None,
    downtrend: bool = False,
) -> list[dict]:
    """Generate n TOPIX rows as consecutive calendar dates ending on last_date.

    drops_at: list of 0-based indices where return is -2.5% instead of ±0.5%.
    downtrend: if True, each session is -0.3% so current close is the minimum.
    """
    from datetime import date as date_, timedelta

    end = date_.fromisoformat(last_date)
    dates = [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
    close = 3000.0
    rows = []
    for i, d in enumerate(dates):
        rows.append(
            {
                "Date": f"{d} 00:00:00",
                "O": close,
                "H": close * 1.001,
                "L": close * 0.999,
                "C": close,
            }
        )
        if downtrend:
            close *= 0.997
        elif drops_at and i in drops_at:
            close *= 0.975  # -2.5%
        else:
            close *= 1.005 if i % 2 == 0 else 0.995
    return rows


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
        # Margin data: ratios 2.0, 3.0, 1.0, 4.0 → median = 2.5
        _insert_margin(conn, "83060", "2026-05-02", long_vol=20_000.0, short_vol=10_000.0)  # 2.0
        _insert_margin(conn, "83160", "2026-05-02", long_vol=9_000.0, short_vol=3_000.0)  # 3.0
        _insert_margin(conn, "97660", "2026-05-02", long_vol=5_000.0, short_vol=5_000.0)  # 1.0
        _insert_margin(conn, "53010", "2026-05-02", long_vol=12_000.0, short_vol=3_000.0)  # 4.0
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
        assert "market_margin_ratio_median" in data["summary"]
        assert "market_margin_ratio_count" in data["summary"]
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
        # trend_signals always present (null sub-sections when TOPIX data absent)
        assert "trend_signals" in data
        assert "distribution" in data["trend_signals"]
        assert "follow_through" in data["trend_signals"]

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
    async def test_market_margin_ratio_median(self, mock_briefing):
        """market_margin_ratio_median is the median of per-stock ratios across all stocks."""
        result = await mock_briefing.call_tool("get_market_briefing", {"date": "2026-05-02"})
        data = _call(result)
        summary = data["summary"]
        # ratios: 2.0, 3.0, 1.0, 4.0 → sorted: 1.0, 2.0, 3.0, 4.0 → median = 2.5
        assert summary["market_margin_ratio_median"] == pytest.approx(2.5, rel=1e-3)
        assert summary["market_margin_ratio_count"] == 4

    @pytest.mark.asyncio
    async def test_market_margin_ratio_null_when_no_data(self, tmp_path):
        """market_margin_ratio_median is null when markets_margin_interest has no rows."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "13010", "2026-05-01", 1000.0)
        _insert_bar(conn, "13010", "2026-05-02", 1050.0)
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
        assert data["summary"]["market_margin_ratio_median"] is None
        assert data["summary"]["market_margin_ratio_count"] == 0

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

    # ------------------------------------------------------------------
    # trend_signals tests — require TOPIX data in cache
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_trend_signals_distribution_present(self, tmp_path):
        """With 50 TOPIX sessions and normal returns, distribution section is populated."""
        from unittest.mock import MagicMock

        from jquants_mcp.client import JQuantsClient

        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        conn.commit()
        conn.close()
        # 50 rows: alternating ±0.5% — no big drops, so distribution_count=0
        cache.put_rows("indices_bars_daily_topix", _make_topix_rows(n=50), key_columns=["Date"])
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
        dist = data["trend_signals"]["distribution"]
        assert dist is not None
        assert isinstance(dist["distribution_count"], int)
        assert dist["warning"] is False
        assert isinstance(dist["recent_distribution_days"], list)

    @pytest.mark.asyncio
    async def test_trend_signals_distribution_warning_fires(self, tmp_path):
        """With 4 big-drop sessions in the last 25-session window, warning is True."""
        from unittest.mock import MagicMock

        from jquants_mcp.client import JQuantsClient

        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        conn.commit()
        conn.close()
        # drops_at=[26,27,28,29]: the -2.5% return fires on the *next* row after each
        # index (rows 27–30), placing all 4 distribution days within the 25-session window.
        rows = _make_topix_rows(n=50, drops_at=[26, 27, 28, 29])
        cache.put_rows("indices_bars_daily_topix", rows, key_columns=["Date"])
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
        dist = data["trend_signals"]["distribution"]
        assert dist is not None
        assert dist["distribution_count"] >= 4
        assert dist["warning"] is True

    @pytest.mark.asyncio
    async def test_trend_signals_no_rally_attempt(self, tmp_path):
        """Downtrend TOPIX (current close is the minimum) → follow_through no_rally_attempt."""
        from unittest.mock import MagicMock

        from jquants_mcp.client import JQuantsClient

        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        conn.commit()
        conn.close()
        # 50 rows in continuous downtrend: current close is the minimum
        rows = _make_topix_rows(n=50, downtrend=True)
        cache.put_rows("indices_bars_daily_topix", rows, key_columns=["Date"])
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
        ftd = data["trend_signals"]["follow_through"]
        assert ftd is not None
        assert ftd["status"] == "no_rally_attempt"
        assert "auto_rally_start" in ftd


# ---------------------------------------------------------------------------
# get_dividend_yield_ranking
# ---------------------------------------------------------------------------


def _insert_fins_summary(
    conn: sqlite3.Connection,
    code: str,
    disc_date: str,
    div_ann: float | None = None,
    fdivann: float | None = None,
    nxfdivann: float | None = None,
    doc_type: str | None = None,
    type_of_document: str | None = None,
) -> None:
    data: dict = {"Code": code, "DisclosedDate": disc_date}
    if div_ann is not None:
        data["DivAnn"] = div_ann
    if fdivann is not None:
        data["FDivAnn"] = fdivann
    if nxfdivann is not None:
        data["NxFDivAnn"] = nxfdivann
    if doc_type is not None:
        data["DocType"] = doc_type
    if type_of_document is not None:
        data["TypeOfDocument"] = type_of_document
    conn.execute(
        "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, disc_date, json.dumps(data), 0.0),
    )


class TestGetDividendYieldRanking:
    """Tests for get_dividend_yield_ranking tool."""

    @pytest.fixture()
    def yield_cache(self, tmp_path):
        """Cache with fins_summary + bars for 4 stocks on 2026-05-02.

        Uses FDivAnn (forward forecast) so tests work with the default
        include_trailing=False behavior.

        Yields:
          13010: FDivAnn=100, AdjC=2000 → yield=5.0% (qualifies at default 3%)
          13020: FDivAnn=60,  AdjC=3000 → yield=2.0% (below default 3%)
          13030: FDivAnn=200, AdjC=2500 → yield=8.0% (qualifies)
          13040: no dividend entry       → excluded
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 2000.0)
        _insert_fins_summary(conn, "13010", "2026-03-31", fdivann=100.0)
        _insert_master(conn, "13010", "高配当A")

        _insert_bar(conn, "13020", "2026-05-02", 3000.0)
        _insert_fins_summary(conn, "13020", "2026-03-31", fdivann=60.0)
        _insert_master(conn, "13020", "低配当B")

        _insert_bar(conn, "13030", "2026-05-02", 2500.0)
        _insert_fins_summary(conn, "13030", "2026-03-31", fdivann=200.0)
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
        # Forward dividends (FDivAnn) → div_source must be "forward"
        assert all(item["div_source"] == "forward" for item in data["items"])

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
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        # Uses DivAnn=100 from the 2025-11-14 disclosure (not skipped)
        assert data["items"][0]["yield_pct"] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_explicit_zero_div_ann_excluded(self, tmp_path):
        """A code whose most recent non-null DivAnn is 0 is excluded from the ranking.

        Mirrors the アドバンスクリエイト (8798) real-world case:
          13010: DivAnn=100 disclosed 2025-02-28 (older, positive)
                 DivAnn=0.0 disclosed 2025-11-14 (newer, explicit dividend cut)
        Expected: 13010 NOT in results (dividend was cut to zero).

        Both disclosures are within the default disc_months=18 staleness window
        (query_date=2026-05-02, cutoff≈2024-11-03), so staleness is not a factor.

        This is distinct from an interim report with null DivAnn, which falls
        back to the previous positive disclosure.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 2000.0)
        _insert_fins_summary(conn, "13010", "2025-02-28", 100.0)  # older, positive
        _insert_fins_summary(conn, "13010", "2025-11-14", 0.0)  # newer, explicit zero (cut)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        # Dividend cut → excluded regardless of the older positive disclosure
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_null_interim_falls_back_to_positive(self, tmp_path):
        """Interim report with null DivAnn is skipped; older positive DivAnn is used.

        13020: DivAnn=80 disclosed 2025-11-14 (FY annual, positive)
               DivAnn=null disclosed 2026-02-14 (1Q interim, no DivAnn)
        Expected: 13020 included with yield from DivAnn=80 (not cut, just interim).
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13020", "2026-05-02", 2000.0)
        _insert_fins_summary(conn, "13020", "2025-11-14", 80.0)  # annual, positive
        _insert_fins_summary(conn, "13020", "2026-02-14", None)  # interim, no DivAnn
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        assert data["items"][0]["yield_pct"] == pytest.approx(80.0 / 2000.0 * 100, rel=1e-3)

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
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["yield_pct"] == pytest.approx(86.0 * 0.1 / 220.0 * 100, rel=1e-3)
        assert item["div_ann"] == pytest.approx(8.6, rel=1e-3)

    @pytest.mark.asyncio
    async def test_split_adjusted_yield_adj_factor_in_json(self, tmp_path):
        """Split factor is read from data JSON when adj_factor column is NULL.

        Legacy rows written before the adj_factor column was added store
        AdjFactor only inside the data JSON blob.  COALESCE must fall back
        to json_extract(data, '$.AdjFactor') in that case.

        70010: disc_date=2025-06-30 DivAnn=86.0, split 1:10 on 2025-09-27
               with adj_factor column = NULL but AdjFactor in data JSON = 0.1.
        Adjusted yield = (86*0.1)/220*100 = 3.9%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "70010", "2026-05-02", 220.0)
        # adj_factor column is NULL; AdjFactor lives only in data JSON
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("70010", "2025-09-27", '{"Code":"70010","AdjC":22.0,"AdjFactor":0.1}', 0.0, None),
        )
        _insert_fins_summary(conn, "70010", "2025-06-30", 86.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["yield_pct"] == pytest.approx(86.0 * 0.1 / 220.0 * 100, rel=1e-3)
        assert item["div_ann"] == pytest.approx(8.6, rel=1e-3)

    @pytest.mark.asyncio
    async def test_split_on_disc_date_not_applied(self, tmp_path):
        """A split bar whose date equals disc_date must NOT be included in the factor.

        J-Quants records DivAnn at disc_date on a pre-split basis; the split
        ratio stored on the same day should not be double-counted.
        bar_date <= disc_date is excluded, so adj_factor on the same date is ignored.

        60010: disc_date=2025-09-27, split adj_factor=0.1 also on 2025-09-27,
               current AdjC=220.0.
        Expected: cum_factor=1.0 (split on disc_date excluded) → yield=86/220*100≈39.1%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "60010", "2026-05-02", 220.0)
        # Split bar date == disc_date: must be excluded from the factor
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("60010", "2025-09-27", '{"Code":"60010","AdjC":22.0}', 0.0, 0.1),
        )
        _insert_fins_summary(conn, "60010", "2025-09-27", 86.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        # cum_factor == 1.0 because split on disc_date is excluded
        assert item["div_ann"] == pytest.approx(86.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(86.0 / 220.0 * 100, rel=1e-3)

    @pytest.mark.asyncio
    async def test_fye_split_before_disc_adjusted(self, tmp_path):
        """DivAnn is adjusted for FY-end splits that occurred just before disc_date.

        Mirrors the 京王電鉄 (9008) real-world case:
          90080: 5:1 split on 2026-03-30 (adj_factor=0.2).
                 Annual results disclosed 2026-05-13 with DivAnn=110 (pre-split terms).
                 Current AdjC=775.1.
        Unadjusted yield = 110/775.1*100 = 14.19% (the bug).
        Adjusted yield   = (110*0.2)/775.1*100 = 2.84%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "90080", "2026-05-13", 775.1)
        # FY-end split on 2026-03-30 (45 days before disc_date)
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("90080", "2026-03-30", '{"Code":"90080","AdjC":799.0}', 0.0, 0.2),
        )
        _insert_fins_summary(conn, "90080", "2026-05-13", 110.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-13", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        # 110 * 0.2 = 22 → yield = 22/775.1*100 ≈ 2.84%
        assert item["div_ann"] == pytest.approx(22.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(22.0 / 775.1 * 100, rel=1e-2)

    @pytest.mark.asyncio
    async def test_fye_split_outside_lookback_not_applied(self, tmp_path):
        """Splits older than 90 days before disc_date must NOT be applied.

        A split from the previous fiscal year (> 90 days before disc_date)
        should not affect the current year's DivAnn.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "80010", "2026-05-13", 500.0)
        # Old split 200 days before disc_date — outside lookback window
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("80010", "2025-10-25", '{"Code":"80010","AdjC":250.0}', 0.0, 0.5),
        )
        _insert_fins_summary(conn, "80010", "2026-05-13", 40.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-13", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        # Old split not applied: DivAnn stays at 40
        assert item["div_ann"] == pytest.approx(40.0, rel=1e-3)

    # ------------------------------------------------------------------
    # disc_months / max_yield / market / sector filters
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_disc_months_excludes_stale(self, tmp_path):
        """disc_date older than disc_months cutoff is excluded.

        query_date=2026-05-02, disc_months=18 → cutoff ≈ 2024-09-22.
        13010 disc_date=2024-01-01 (stale) → excluded.
        13020 disc_date=2025-01-01 (fresh) → included.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 1000.0)
        _insert_fins_summary(conn, "13010", "2024-01-01", fdivann=100.0)  # stale
        _insert_master(conn, "13010", "古い配当")

        _insert_bar(conn, "13020", "2026-05-02", 1000.0)
        _insert_fins_summary(conn, "13020", "2025-01-01", fdivann=100.0)  # fresh
        _insert_master(conn, "13020", "新しい配当")
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "disc_months": 18},
            )
        data = _call(result)
        assert data["count"] == 1
        assert data["items"][0]["code"] == "1302"  # 13020 only

    @pytest.mark.asyncio
    async def test_max_yield_filter(self, mock_yield_server):
        """max_yield caps the upper bound of reported yield."""
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking",
            {"date": "2026-05-02", "min_yield": 0.0, "max_yield": 6.0},
        )
        data = _call(result)
        # 13030 yield=8% excluded, 13010 yield=5% included, 13020 yield=2% included
        assert data["count"] == 2
        yields = [item["yield_pct"] for item in data["items"]]
        assert all(y <= 6.0 for y in yields)

    @pytest.mark.asyncio
    async def test_market_filter(self, tmp_path):
        """market='prime' keeps only Mkt=111 stocks."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 1000.0)
        _insert_fins_summary(conn, "13010", "2025-06-01", fdivann=100.0)
        conn.execute(
            "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "13010",
                "2026-05-01",
                '{"Code":"13010","CoName":"プライム銘柄","Mkt":111,"MktNm":"プライム","S33":"50","S33Nm":"水産"}',
                0.0,
            ),
        )

        _insert_bar(conn, "13020", "2026-05-02", 1000.0)
        _insert_fins_summary(conn, "13020", "2025-06-01", fdivann=100.0)
        conn.execute(
            "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "13020",
                "2026-05-01",
                '{"Code":"13020","CoName":"グロース銘柄","Mkt":113,"MktNm":"グロース","S33":"50","S33Nm":"水産"}',
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "market": "prime"},
            )
        data = _call(result)
        assert data["count"] == 1
        assert data["items"][0]["code"] == "1301"
        assert data["items"][0]["market"] == "プライム"

    @pytest.mark.asyncio
    async def test_sector_filter(self, tmp_path):
        """sector='50' keeps only S33='50' stocks."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "13010", "2026-05-02", 1000.0)
        _insert_fins_summary(conn, "13010", "2025-06-01", fdivann=100.0)
        conn.execute(
            "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "13010",
                "2026-05-01",
                '{"Code":"13010","CoName":"水産A","Mkt":111,"MktNm":"プライム","S33":"50","S33Nm":"水産・農林業"}',
                0.0,
            ),
        )

        _insert_bar(conn, "13020", "2026-05-02", 1000.0)
        _insert_fins_summary(conn, "13020", "2025-06-01", fdivann=100.0)
        conn.execute(
            "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "13020",
                "2026-05-01",
                '{"Code":"13020","CoName":"銀行B","Mkt":111,"MktNm":"プライム","S33":"7050","S33Nm":"銀行業"}',
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "sector": "50"},
            )
        data = _call(result)
        assert data["count"] == 1
        assert data["items"][0]["code"] == "1301"
        assert data["items"][0]["sector"] == "水産・農林業"

    @pytest.mark.asyncio
    async def test_filters_in_response(self, mock_yield_server):
        """Response includes applied filters under 'filters' key."""
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking",
            {"date": "2026-05-02", "min_yield": 2.0, "max_yield": 9.0, "disc_months": 12},
        )
        data = _call(result)
        assert data["filters"]["min_yield"] == 2.0
        assert data["filters"]["max_yield"] == 9.0
        assert data["filters"]["disc_months"] == 12
        assert data["filters"]["include_trailing"] is False
        assert data["filters"]["market"] is None
        assert data["filters"]["sector"] is None

    @pytest.mark.asyncio
    async def test_invalid_market(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking",
            {"date": "2026-05-02", "market": "tokyo_stock_exchange"},
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_invalid_disc_months(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking",
            {"date": "2026-05-02", "disc_months": 0},
        )
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_max_yield_below_min_yield(self, mock_yield_server):
        result = await mock_yield_server.call_tool(
            "get_dividend_yield_ranking",
            {"date": "2026-05-02", "min_yield": 5.0, "max_yield": 3.0},
        )
        data = _call(result)
        assert data.get("error") is True

    # ------------------------------------------------------------------
    # Forward dividend (FDivAnn / NxFDivAnn) tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_fdivann_used_over_trailing_divann(self, tmp_path):
        """FDivAnn (forward forecast) takes priority over trailing DivAnn.

        Mirrors バリューコマース (24910) real-world case:
          Annual disc 2026-01-30: DivAnn=49 (trailing actual)
          Q1 disc     2026-04-28: FDivAnn=16 (current-FY forecast)
        Kabutan shows 16/497*100 = 3.22%, not 49/497*100 = 9.86%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "24910", "2026-05-02", 497.0)
        # Annual: DivAnn=49 (older)
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            ("24910", "2026-01-30", json.dumps({"Code": "24910", "DivAnn": 49.0}), 0.0),
        )
        # Q1: FDivAnn=16, DivAnn="" (newer)
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "24910",
                "2026-04-28",
                json.dumps({"Code": "24910", "DivAnn": "", "FDivAnn": 16.0}),
                0.0,
            ),
        )
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
        # Forward FDivAnn=16 used, not trailing DivAnn=49
        assert item["div_ann"] == pytest.approx(16.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(16.0 / 497.0 * 100, rel=1e-2)

    @pytest.mark.asyncio
    async def test_nxfdivann_used_when_no_fdivann(self, tmp_path):
        """NxFDivAnn from an annual report is used when no FDivAnn is available.

        Mirrors 日本創発グループ (78140) real-world case:
          Annual disc 2026-02-13: DivAnn=60 (trailing), NxFDivAnn=15 (next-FY forecast)
          Only one row; no quarterly with FDivAnn yet.
        Kabutan shows 15/544*100 = 2.76%, not 60/544*100 = 11.03%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "78140", "2026-05-02", 544.0)
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "78140",
                "2026-02-13",
                json.dumps({"Code": "78140", "DivAnn": 60.0, "NxFDivAnn": 15.0}),
                0.0,
            ),
        )
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
        # NxFDivAnn=15 used, not DivAnn=60
        assert item["div_ann"] == pytest.approx(15.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(15.0 / 544.0 * 100, rel=1e-2)

    @pytest.mark.asyncio
    async def test_nxfdivann_no_fye_correction(self, tmp_path):
        """NxFDivAnn must NOT receive FY-end split correction.

        Mirrors 京王電鉄 (9008) real-world case:
          5:1 split on 2026-03-30 (adj_factor=0.2).
          Annual disc 2026-05-13: DivAnn=110 (pre-split), NxFDivAnn=22 (post-split).
        Correct yield = 22/775.1*100 ≈ 2.84%.
        Applying FYE factor 0.2 to NxFDivAnn would wrongly give 4.4/775.1*100.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "90080", "2026-05-13", 775.1)
        # FY-end split on 2026-03-30 (45 days before disc_date)
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("90080", "2026-03-30", '{"Code":"90080","AdjC":799.0}', 0.0, 0.2),
        )
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "90080",
                "2026-05-13",
                json.dumps({"Code": "90080", "DivAnn": 110.0, "NxFDivAnn": 22.0}),
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking", {"date": "2026-05-13", "min_yield": 0.0}
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        # NxFDivAnn=22 used as-is (no FYE correction); NOT 22*0.2=4.4
        assert item["div_ann"] == pytest.approx(22.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(22.0 / 775.1 * 100, rel=1e-2)

    @pytest.mark.asyncio
    async def test_trailing_excluded_by_default(self, tmp_path):
        """DivAnn-only stocks (no FDivAnn/NxFDivAnn) are excluded by default.

        Default include_trailing=False matches Kabutan 予想配当利回りランキング which only
        uses current-FY forecast dividends.  Stocks with 今期予想非開示 (no current-FY
        forecast filed) must not appear.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "55550", "2026-05-02", 1000.0)
        # Only trailing DivAnn; no FDivAnn or NxFDivAnn
        _insert_fins_summary(conn, "55550", "2025-11-14", div_ann=60.0)
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
        # Default include_trailing=False → trailing-only stock excluded
        assert data["count"] == 0, (
            "trailing-only stock must be excluded when include_trailing=False"
        )

    @pytest.mark.asyncio
    async def test_include_trailing_restores_trailing_fallback(self, tmp_path):
        """include_trailing=True restores pre-v0.38 behavior where DivAnn is used as fallback.

        Stocks with only trailing DivAnn (no FDivAnn/NxFDivAnn) appear when
        include_trailing=True, and their div_source is 'trailing'.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "55550", "2026-05-02", 1000.0)
        # Only trailing DivAnn; no FDivAnn or NxFDivAnn
        _insert_fins_summary(conn, "55550", "2025-11-14", div_ann=60.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 0.0, "include_trailing": True},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["div_ann"] == pytest.approx(60.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(60.0 / 1000.0 * 100, rel=1e-2)
        assert item["div_source"] == "trailing"

    @pytest.mark.asyncio
    async def test_stale_forward_disc_date_excluded(self, tmp_path):
        """Forward dividend with disc_date older than disc_months cutoff is excluded.

        With disc_months=18 (default) and query date 2026-05-02,
        cutoff ≈ 2024-10-21.  A disc_date of 2024-10-01 is stale and must
        not appear in the ranking.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "33330", "2026-05-02", 1000.0)
        # FDivAnn present but stale
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            ("33330", "2024-10-01", json.dumps({"Code": "33330", "FDivAnn": 30.0}), 0.0),
        )
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
        assert data["count"] == 0, "stale forward disc_date must be excluded from ranking"

    @pytest.mark.asyncio
    async def test_nxfdivann_wins_over_fdivann_in_same_row(self, tmp_path):
        """When FDivAnn and NxFDivAnn both appear in the same row, NxFDivAnn takes priority.

        NxFDivAnn is only filed in annual (FY) earnings disclosures and always represents
        the NEXT fiscal year forecast.  In such filings FDivAnn equals the just-confirmed
        trailing actual, so NxFDivAnn is the only genuinely forward-looking value.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "44440", "2026-05-02", 1000.0)
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "44440",
                "2026-04-28",
                json.dumps({"Code": "44440", "FDivAnn": 25.0, "NxFDivAnn": 15.0}),
                0.0,
            ),
        )
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
        # NxFDivAnn=15 wins over FDivAnn=25 (NxFDivAnn is always the next-FY forecast)
        assert item["div_ann"] == pytest.approx(15.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(15.0 / 1000.0 * 100, rel=1e-2)

    @pytest.mark.asyncio
    async def test_fdivann_zero_blocks_trailing_divann_fallback(self, tmp_path):
        """Explicit FDivAnn=0 (dividend cut forecast) must not fall back to trailing DivAnn.

        Uses include_trailing=True so the trailing-fallback path is active; even then,
        FDivAnn=0 must prevent the old trailing DivAnn from being used.
        With min_yield=1.0 the zero-yield entry is filtered out; if trailing DivAnn
        were used instead, the code would incorrectly appear with a high yield.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "66660", "2026-05-02", 1000.0)
        # Annual with trailing DivAnn=50 (older)
        _insert_fins_summary(conn, "66660", "2026-01-30", div_ann=50.0)
        # Q1 forecast: FDivAnn=0 (dividend cut announced)
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "66660",
                "2026-04-28",
                json.dumps({"Code": "66660", "DivAnn": "", "FDivAnn": 0.0}),
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-02", "min_yield": 1.0, "include_trailing": True},
            )
        data = _call(result)
        # FDivAnn=0 → yield=0 < min_yield=1.0 → filtered.
        # If trailing DivAnn=50 had been used, yield=5% ≥ 1% would appear (wrong).
        assert data["count"] == 0, "FDivAnn=0 must block trailing DivAnn fallback"

    @pytest.mark.asyncio
    async def test_nxfdivann_wins_over_fdivann_when_annual_results_filed(self, tmp_path):
        """Regression for 1798 (守谷商会): NxFDivAnn must win over FDivAnn in annual results.

        When annual earnings are filed, the J-Quants API stores both fields in a
        single row: FDivAnn=180 (the just-confirmed trailing FY actual, same as DivAnn)
        and NxFDivAnn=38 (the forecast for the next FY — the genuinely forward value).
        get_forward_div_ann_map() must return 38 (NxFDivAnn), not 180 (FDivAnn).
        Without this fix the tool showed 17.66% (180/1019) while Kabutan shows 3.73%.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "17980", "2026-05-20", 1019.0)
        _insert_master(conn, "17980", "守谷商会")
        # Single row as stored by daily_fetch (one row per code+disc_date):
        # FDivAnn=180 is the confirmed trailing FY actual; NxFDivAnn=38 is the
        # next-FY forecast that Kabutan displays as 予想配当.
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "17980",
                "2026-05-12",
                json.dumps({"Code": "17980", "FDivAnn": 180.0, "NxFDivAnn": 38.0}),
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        # NxFDivAnn=38 wins; FDivAnn=180 (trailing actual confirmation) must NOT be used
        assert item["div_ann"] == pytest.approx(38.0, rel=1e-3), (
            f"Expected 38 (NxFDivAnn, next-FY forecast), got {item['div_ann']}"
        )
        expected_yield = round(38.0 / 1019.0 * 100, 2)
        assert item["yield_pct"] == pytest.approx(expected_yield, rel=1e-2)

    @pytest.mark.asyncio
    async def test_nxfdivann_preserved_when_two_rows_same_disc_date(self, tmp_path):
        """COALESCE(MAX(NxFDivAnn), MAX(FDivAnn)) picks NxFDivAnn when two rows share disc_date.

        Annual results can produce two rows per (code, disc_date) in raw API data:
          - FYFinancialStatements  : NxFDivAnn=38 (next-FY forecast), FDivAnn=''
          - DividendForecastRevision: FDivAnn=180 (trailing actual),   NxFDivAnn=''
        If both survive in the DB (no PRIMARY KEY dedup), MAX(COALESCE(...)) = 180 (wrong),
        while COALESCE(MAX(NxFDivAnn), MAX(FDivAnn)) = 38 (correct).
        This test verifies the store.py SQL aggregation using a table without PK.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        # Intentionally no PRIMARY KEY to allow two rows with the same (code, disc_date)
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL)"
        )
        _insert_bar(conn, "17980", "2026-05-20", 1019.0)
        _insert_master(conn, "17980", "守谷商会")
        disc_date = "2026-05-12"
        # FYFinancialStatements: NxFDivAnn=38 (next-FY forecast), FDivAnn absent
        conn.execute(
            "INSERT INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "17980",
                disc_date,
                json.dumps({"Code": "17980", "NxFDivAnn": 38.0, "FDivAnn": ""}),
                0.0,
            ),
        )
        # DividendForecastRevision: FDivAnn=180 (trailing actual), NxFDivAnn absent
        conn.execute(
            "INSERT INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "17980",
                disc_date,
                json.dumps({"Code": "17980", "FDivAnn": 180.0, "NxFDivAnn": ""}),
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["div_ann"] == pytest.approx(38.0, rel=1e-3), (
            f"COALESCE(MAX(NxFDivAnn), MAX(FDivAnn)) must return 38, got {item['div_ann']}"
        )
        expected_yield = round(38.0 / 1019.0 * 100, 2)
        assert item["yield_pct"] == pytest.approx(expected_yield, rel=1e-2)

    @pytest.mark.asyncio
    async def test_fdivann_zero_with_nxfdivann_uses_nxfdivann(self, tmp_path):
        """FDivAnn=0 (current-FY cut) + NxFDivAnn>0 (next-FY forecast) → NxFDivAnn wins.

        A company that cut its current-FY dividend to zero but disclosed a positive
        next-FY forecast should appear in the ranking using NxFDivAnn, not FDivAnn=0.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "55550", "2026-05-20", 1000.0)
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (
                "55550",
                "2026-05-12",
                json.dumps({"Code": "55550", "FDivAnn": 0.0, "NxFDivAnn": 30.0}),
                0.0,
            ),
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["div_ann"] == pytest.approx(30.0, rel=1e-3)
        assert item["yield_pct"] == pytest.approx(3.0, rel=1e-2)

    @pytest.mark.asyncio
    async def test_stale_fdivann_excluded_when_newer_fy_exists(self, tmp_path):
        """Stale Q-report FDivAnn is excluded when a newer FYFinancialStatements row exists.

        Regression for 9444 (トーシンHD): FY2025 annual results (2025-10-31) have
        FDivAnn='' and NxFDivAnn='', so get_forward_div_ann_map used to fall back
        to Q3-2025 (2025-03-14) FDivAnn=20 — a completed-FY forecast.  The fy_latest
        CTE detects that a newer FYFinancialStatements exists and excludes the result.
        240+ codes are affected in real data.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "94440", "2026-05-20", 220.0)
        _insert_master(conn, "94440", "トーシンHD")
        # Newer FYFinancialStatements row — no div forecast yet
        _insert_fins_summary(
            conn,
            "94440",
            "2025-10-31",
            div_ann=10.0,
            doc_type="FYFinancialStatements_Consolidated_JP",
        )
        # Older Q3 FDivAnn — belongs to the completed FY, must NOT be used
        _insert_fins_summary(conn, "94440", "2025-03-14", fdivann=20.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 0, (
            "Stale Q3 FDivAnn=20 must be excluded when FYFinancialStatements 2025-10-31 "
            "is newer; count must be 0 (not 1 with bogus 9.09% yield)"
        )

    @pytest.mark.asyncio
    async def test_fy_with_nxfdivann_not_excluded_by_fy_cte(self, tmp_path):
        """FYFinancialStatements row that sets NxFDivAnn must still appear in ranking.

        When a FYFinancialStatements row is the source of NxFDivAnn (the normal case),
        fy_latest.fy_md equals m.md and HAVING fl.fy_md <= m.md is satisfied → kept.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "94440", "2026-05-20", 220.0)
        _insert_master(conn, "94440", "トーシンHD")
        # FY annual result with NxFDivAnn — within disc_months=18 cutoff, must appear
        _insert_fins_summary(
            conn,
            "94440",
            "2025-10-31",
            div_ann=22.0,
            nxfdivann=20.0,
            doc_type="FYFinancialStatements_Consolidated_JP",
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 1
        item = data["items"][0]
        assert item["div_ann"] == pytest.approx(20.0, rel=1e-3), (
            f"NxFDivAnn=20 from FYFinancialStatements must be used; got {item['div_ann']}"
        )

    @pytest.mark.asyncio
    async def test_dividend_forecast_revision_after_fy_not_excluded(self, tmp_path):
        """DividendForecastRevision filed after FYFinancialStatements must NOT be excluded.

        A company may update its current-FY forecast via DividendForecastRevision after
        the annual results.  In this case:
          - fy_latest.fy_md = FYFinancialStatements disc_date (older)
          - m.md            = DividendForecastRevision disc_date (newer)
          - fl.fy_md <= m.md is satisfied → HAVING passes → must appear in ranking.
        DividendForecastRevision is intentionally excluded from fy_latest (it shares
        CurPerType='FY' but is not an annual-result filing), so this test verifies that
        the exclusion of DivForecastRevision from fy_latest does NOT break valid results.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "94440", "2026-05-20", 220.0)
        _insert_master(conn, "94440", "トーシンHD")
        # FY annual result filed 2025-10-31 (no div forecast)
        _insert_fins_summary(
            conn,
            "94440",
            "2025-10-31",
            div_ann=10.0,
            doc_type="FYFinancialStatements_Consolidated_JP",
        )
        # DividendForecastRevision filed after the FY result — current-FY forecast update
        _insert_fins_summary(
            conn,
            "94440",
            "2025-11-20",
            fdivann=15.0,
        )
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 1, (
            "DividendForecastRevision filed after FYFinancialStatements must not be excluded"
        )
        item = data["items"][0]
        assert item["div_ann"] == pytest.approx(15.0, rel=1e-3), (
            f"FDivAnn=15 from DividendForecastRevision must be used; got {item['div_ann']}"
        )

    @pytest.mark.asyncio
    async def test_stale_fdivann_excluded_via_type_of_document(self, tmp_path):
        """fy_latest CTE works with TypeOfDocument (old API format) as well as DocType.

        Older J-Quants data uses TypeOfDocument instead of DocType.  The CTE uses
        OR to cover both fields.  This test verifies that TypeOfDocument='FYFinancial%'
        correctly identifies an annual-result row and triggers stale-exclusion.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        _insert_bar(conn, "99990", "2026-05-20", 500.0)
        _insert_master(conn, "99990", "テスト会社")
        # Newer FY row identified via TypeOfDocument (old format) — no div forecast
        _insert_fins_summary(
            conn,
            "99990",
            "2025-10-31",
            div_ann=5.0,
            type_of_document="FYFinancialStatements_Consolidated_JP",
        )
        # Older Q3 FDivAnn — stale, must be excluded
        _insert_fins_summary(conn, "99990", "2025-03-14", fdivann=30.0)
        conn.commit()
        conn.close()

        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_dividend_yield_ranking",
                {"date": "2026-05-20", "min_yield": 0.0},
            )
        data = _call(result)
        assert data["count"] == 0, (
            "TypeOfDocument='FYFinancial%' must also trigger stale-FDivAnn exclusion"
        )


class TestGetMarketBriefingShortRatio:
    """Tests for sector_short_ratios in get_market_briefing."""

    @pytest.fixture()
    def briefing_cache_with_short_ratio(self, tmp_path):
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        _insert_bar(conn, "97660", "2026-05-01", 5000.0, vo=200_000)
        _insert_bar(conn, "97660", "2026-05-02", 4500.0, vo=300_000)
        _insert_master_with_sector(conn, "97660", "コナミ", "5250", "情報・通信業", "5", "情報通信")
        conn.commit()
        conn.close()
        # Seed short_ratio via put_rows (auto-creates table)
        cache.put_rows(
            "markets_short_ratio",
            [
                # (300+85)/(615+300+85)*100 = 38.5%
                {
                    "S33": "7050",
                    "Date": "2026-05-02",
                    "SellExShortVa": 615000000,
                    "ShrtWithResVa": 300000000,
                    "ShrtNoResVa": 85000000,
                },
                # (450+102)/(448+450+102)*100 = 55.2%
                {
                    "S33": "5250",
                    "Date": "2026-05-02",
                    "SellExShortVa": 448000000,
                    "ShrtWithResVa": 450000000,
                    "ShrtNoResVa": 102000000,
                },
            ],
            key_columns=["S33", "Date"],
        )
        return cache

    @pytest.mark.asyncio
    async def test_sector_short_ratios_present(self, briefing_cache_with_short_ratio):
        """sector_short_ratios list is populated when markets_short_ratio is cached."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", briefing_cache_with_short_ratio),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        sr_list = data["sector_short_ratios"]
        assert isinstance(sr_list, list)
        assert len(sr_list) == 2
        # Sorted by ratio descending: 5250 (55.2) before 7050 (38.5)
        assert sr_list[0]["sector_code"] == "5250"
        assert sr_list[0]["short_sale_ratio"] == pytest.approx(55.2)
        assert sr_list[1]["sector_code"] == "7050"
        assert sr_list[1]["short_sale_ratio"] == pytest.approx(38.5)

    @pytest.mark.asyncio
    async def test_sectors_top_bottom_include_short_sale_ratio(
        self, briefing_cache_with_short_ratio
    ):
        """Each entry in sectors.top/bottom includes short_sale_ratio."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", briefing_cache_with_short_ratio),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        for entry in data["sectors"]["top"] + data["sectors"]["bottom"]:
            assert "short_sale_ratio" in entry

    @pytest.mark.asyncio
    async def test_sectors_top_bottom_short_ratio_nonnull_with_zero_padded_s33(self, tmp_path):
        """sectors.top/bottom short_sale_ratio resolves when master S33 is zero-padded.

        Regression: short_ratio_map keys are normalised via _norm_s33 ("0050"->"50"),
        but sector entry["code"] is the raw master S33 ("0050"). The enrichment lookup
        must normalise too, otherwise short_sale_ratio is always null in production.
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        # Master stores the zero-padded S33 form "0050".
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "0050", "銀行業", "7", "金融")
        conn.commit()
        conn.close()
        cache.put_rows(
            "markets_short_ratio",
            [
                {
                    "S33": "0050",
                    "Date": "2026-05-02",
                    "SellExShortVa": 615000000,
                    "ShrtWithResVa": 300000000,
                    "ShrtNoResVa": 85000000,
                },
            ],
            key_columns=["S33", "Date"],
        )
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        entries = data["sectors"]["top"] + data["sectors"]["bottom"]
        bank = next(e for e in entries if CacheStore._norm_s33(e["code"]) == "50")
        # (300+85)/(615+300+85)*100 = 38.5%
        assert bank["short_sale_ratio"] == pytest.approx(38.5)

    @pytest.mark.asyncio
    async def test_sector_short_ratios_empty_when_not_cached(self, tmp_path):
        """sector_short_ratios is an empty list when markets_short_ratio is absent."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
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
        assert data["sector_short_ratios"] == []
        # short_sale_ratio in sector entries should be null
        for entry in data["sectors"]["top"] + data["sectors"]["bottom"]:
            assert entry["short_sale_ratio"] is None

    @pytest.mark.asyncio
    async def test_sector_short_ratios_dedup_s33_formats(self, tmp_path):
        """sector_short_ratios deduplicates '0050' and '50' to a single entry."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_bar(conn, "83060", "2026-05-01", 1000.0, vo=10_000_000)
        _insert_bar(conn, "83060", "2026-05-02", 1100.0, vo=12_000_000)
        _insert_master_with_sector(conn, "83060", "三菱UFJ", "7050", "銀行業", "7", "金融")
        conn.commit()
        conn.close()
        # Seed both "0050" (legacy zero-padded) and "50" (int-derived) for the same sector
        cache.put_rows(
            "markets_short_ratio",
            [
                {
                    "S33": "0050",
                    "Date": "2026-05-01",
                    "SellExShortVa": 615000000,
                    "ShrtWithResVa": 300000000,
                    "ShrtNoResVa": 85000000,
                },
                {
                    "S33": "50",
                    "Date": "2026-05-02",
                    "SellExShortVa": 615000000,
                    "ShrtWithResVa": 300000000,
                    "ShrtNoResVa": 85000000,
                },
            ],
            key_columns=["S33", "Date"],
        )
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool(
                "get_market_briefing", {"date": "2026-05-02"}
            )
        data = _call(result)
        sr_list = data["sector_short_ratios"]
        codes = [e["sector_code"] for e in sr_list]
        assert codes.count("50") <= 1, "sector_code '50' must not appear more than once"
        assert "0050" not in codes, "legacy '0050' must be merged into '50'"
        # The newer date ("50" row: 2026-05-02) must win over the older ("0050": 2026-05-01)
        entry = next(e for e in sr_list if e["sector_code"] == "50")
        assert entry["date"] == "2026-05-02", "newer Date must be preferred over older duplicate"


class TestGetMarketBriefingNotableStocks:
    """Tests for highlights.notable_stocks in get_market_briefing."""

    DATES = _make_dates(26, "2026-05-02")
    TODAY = "2026-05-02"

    CODE_A = "11110"  # steadily rising close → high RSI → overbought (52w_high)
    CODE_B = "22220"  # alternating close → medium RSI → overbought (52w_high)
    CODE_C = "33330"  # steadily falling close → low RSI → oversold (52w_low)
    CODE_D = "44440"  # price-limit high only (no screener entry)

    @pytest.fixture()
    def notable_cache(self, tmp_path):
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        for code, name in [
            (self.CODE_A, "アルファ"),
            (self.CODE_B, "ベータ"),
            (self.CODE_C, "ガンマ"),
            (self.CODE_D, "デルタ"),
        ]:
            _insert_master(conn, code, name)
        for i, d in enumerate(self.DATES):
            _insert_bar(conn, self.CODE_A, d, 100.0 + i, vo=100_000)
            _insert_bar(conn, self.CODE_B, d, 200.0 + (i % 2), vo=100_000)
            _insert_bar(conn, self.CODE_C, d, 300.0 - i, vo=100_000)
        _insert_bar_ul(conn, self.CODE_D, self.TODAY, 400.0, ul=1)
        conn.commit()
        conn.close()
        # Seed pre-computed 52w screener result (CODE_A/B=new_high, CODE_C=new_low).
        payload = {
            "count": 3,
            "mode": "52w",
            "data": [
                {
                    "Code": self.CODE_A,
                    "Date": self.TODAY,
                    "prior_sessions": 61,
                    "new_high": True,
                    "new_low": False,
                    "new_high_close": True,
                    "new_low_close": False,
                    "volume_ratio": 2.5,
                    "volume_ratio_sessions": 20,
                    "AdjC": 125.0,
                },
                {
                    "Code": self.CODE_B,
                    "Date": self.TODAY,
                    "prior_sessions": 61,
                    "new_high": True,
                    "new_low": False,
                    "new_high_close": True,
                    "new_low_close": False,
                    "volume_ratio": 1.2,
                    "volume_ratio_sessions": 20,
                    "AdjC": 201.0,
                },
                {
                    "Code": self.CODE_C,
                    "Date": self.TODAY,
                    "prior_sessions": 61,
                    "new_high": False,
                    "new_low": True,
                    "new_high_close": False,
                    "new_low_close": True,
                    "volume_ratio": 1.8,
                    "volume_ratio_sessions": 20,
                    "AdjC": 275.0,
                },
            ],
        }
        cache.screener_result_put(
            screener_compute.TOOL_DETECT_52W,
            screener_compute.default_params_hash_52w(),
            self.TODAY,
            payload,
        )
        return cache

    @pytest.mark.asyncio
    async def test_notable_stocks_key_present(self, notable_cache):
        """highlights.notable_stocks always has overbought/oversold keys."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", notable_cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        ns = data["highlights"]["notable_stocks"]
        assert "overbought" in ns
        assert "oversold" in ns

    @pytest.mark.asyncio
    async def test_notable_stocks_overbought_sorted_desc(self, notable_cache):
        """overbought is sorted by rsi14 descending (higher RSI first)."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", notable_cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        overbought = data["highlights"]["notable_stocks"]["overbought"]
        rsi_values = [e["rsi14"] for e in overbought if e["rsi14"] is not None]
        assert rsi_values == sorted(rsi_values, reverse=True)

    @pytest.mark.asyncio
    async def test_notable_stocks_oversold_sorted_asc(self, notable_cache):
        """oversold is sorted by rsi14 ascending (lower RSI first)."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", notable_cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        oversold = data["highlights"]["notable_stocks"]["oversold"]
        rsi_values = [e["rsi14"] for e in oversold if e["rsi14"] is not None]
        assert rsi_values == sorted(rsi_values)

    @pytest.mark.asyncio
    async def test_notable_stocks_signals_field(self, notable_cache):
        """Each entry has a non-empty signals list with valid signal names."""
        valid = {"52w_high", "52w_low", "limit_high", "limit_low"}
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", notable_cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        ns = data["highlights"]["notable_stocks"]
        for entry in ns["overbought"] + ns["oversold"]:
            assert isinstance(entry["signals"], list) and entry["signals"]
            assert all(s in valid for s in entry["signals"])

    @pytest.mark.asyncio
    async def test_notable_stocks_limit_high_in_overbought(self, notable_cache):
        """Price-limit-high stock appears in overbought."""
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", notable_cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        overbought = data["highlights"]["notable_stocks"]["overbought"]
        codes = [e["code"] for e in overbought]
        assert "4444" in codes  # display_code("44440")

    @pytest.mark.asyncio
    async def test_notable_stocks_cold_screener_cache(self, tmp_path):
        """Without screener_results, notable_stocks uses only price-limit data."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_master(conn, self.CODE_D, "デルタ")
        for d in self.DATES:
            _insert_bar(conn, self.CODE_D, d, 400.0, vo=50_000)
        _insert_bar_ul(conn, self.CODE_D, self.TODAY, 400.0, ul=1)
        conn.commit()
        conn.close()
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        ns = data["highlights"]["notable_stocks"]
        entry = next((e for e in ns["overbought"] if e["code"] == "4444"), None)
        assert entry is not None, "CODE_D (4444) must appear in overbought"
        assert entry["signals"] == ["limit_high"], "cold cache: signal must be limit_high only"

    @pytest.mark.asyncio
    async def test_notable_stocks_empty_without_universe(self, tmp_path):
        """No screener cache and no price-limit stocks → empty lists."""
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        _insert_master(conn, "11110", "テスト")
        for d in self.DATES:
            _insert_bar(conn, "11110", d, 100.0)
        conn.commit()
        conn.close()
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", cache),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_market_briefing", {"date": self.TODAY})
        data = _call(result)
        ns = data["highlights"]["notable_stocks"]
        assert ns["overbought"] == []
        assert ns["oversold"] == []


def _insert_fins_fy(
    conn: sqlite3.Connection,
    code: str,
    disc_date: str,
    eps: float | None,
    bps: float | None,
) -> None:
    """Insert an FY financial-summary row carrying EPS/BPS for valuation tests."""
    data: dict = {
        "Code": code,
        "DiscDate": disc_date,
        "CurPerType": "FY",
        "DocType": "FYFinancialStatements_Consolidated_IFRS",
    }
    if eps is not None:
        data["EPS"] = eps
    if bps is not None:
        data["BPS"] = bps
    conn.execute(
        "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, disc_date, json.dumps(data), 0.0),
    )


def _insert_val_master(
    conn: sqlite3.Connection, code: str, name: str, mkt: str, mkt_name: str
) -> None:
    """Insert an equities_master row with market segment for valuation tests."""
    data = {
        "Code": code,
        "Date": "2026-05-01",
        "CoName": name,
        "Mkt": mkt,
        "MktNm": mkt_name,
        "S33": "0050",
        "S33Nm": "水産・農林業",
    }
    conn.execute(
        "INSERT OR REPLACE INTO equities_master (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        (code, "2026-05-01", json.dumps(data), 0.0),
    )


class TestGetValuationRanking:
    """Tests for get_valuation_ranking tool."""

    @pytest.fixture()
    def val_cache(self, tmp_path):
        """Cache with FY fins + latest close on 2026-05-02 for five stocks.

        close / EPS / BPS chosen for clean ratios:
          13010 prime    close=1000 EPS=100 PER=10.0  BPS=500  PBR=2.0
          13020 prime    close=3000 EPS=100 PER=30.0  BPS=1000 PBR=3.0
          13030 standard close=500  EPS=100 PER=5.0   BPS=200  PBR=2.5
          13040 prime    close=2000 EPS=-50 PER=None  BPS=2000 PBR=1.0  (net loss)
          13050 growth   close=1500 EPS=100 PER=15.0  BPS=-100 PBR=None (neg book)
          13060 prime    close=1000 EPS=100 PER=10.0  BPS=500  PBR=2.0  (STALE disc 2024-01-01)
        """
        cache = _make_cache(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "cache.db"))
        conn.execute(
            "CREATE TABLE fins_summary "
            "(code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT, fetched_at REAL, PRIMARY KEY (code, disc_date))"
        )
        rows = [
            ("13010", 1000.0, 100.0, 500.0, "プライムA", "111", "プライム"),
            ("13020", 3000.0, 100.0, 1000.0, "プライムB", "111", "プライム"),
            ("13030", 500.0, 100.0, 200.0, "スタンダードC", "112", "スタンダード"),
            ("13040", 2000.0, -50.0, 2000.0, "プライムD", "111", "プライム"),
            ("13050", 1500.0, 100.0, -100.0, "グロースE", "113", "グロース"),
        ]
        for code, close, eps, bps, name, mkt, mkt_name in rows:
            _insert_bar(conn, code, "2026-05-02", close)
            _insert_fins_fy(conn, code, "2026-03-31", eps, bps)
            _insert_val_master(conn, code, name, mkt, mkt_name)
        # Stale FY disclosure: should be dropped by the default disc_months=18.
        _insert_bar(conn, "13060", "2026-05-02", 1000.0)
        _insert_fins_fy(conn, "13060", "2024-01-01", 100.0, 500.0)
        _insert_val_master(conn, "13060", "古いF", "111", "プライム")
        conn.commit()
        conn.close()
        return cache

    @pytest.fixture()
    def mock_val_server(self, val_cache):
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", val_cache),
            patch.object(server_module, "_client", None),
        ):
            yield server_module.mcp

    @pytest.mark.asyncio
    async def test_per_ranking_default(self, mock_val_server):
        result = await mock_val_server.call_tool("get_valuation_ranking", {})
        data = _call(result)
        assert data["metric"] == "per"
        assert data["price_date"] == "2026-05-02"
        # net-loss (13040) and stale (13060) excluded; ascending = cheapest first.
        assert [i["code"] for i in data["items"]] == ["1303", "1301", "1305", "1302"]
        assert data["count"] == 4
        by_code = {i["code"]: i for i in data["items"]}
        assert by_code["1303"]["per"] == 5.0
        assert by_code["1301"]["per"] == 10.0
        # both ratios + name surfaced
        assert by_code["1301"]["pbr"] == 2.0
        assert by_code["1301"]["name"] == "プライムA"

    @pytest.mark.asyncio
    async def test_pbr_ranking(self, mock_val_server):
        result = await mock_val_server.call_tool("get_valuation_ranking", {"metric": "pbr"})
        data = _call(result)
        # negative-book (13050) excluded; 13060 stale; ascending by PBR.
        assert [i["code"] for i in data["items"]] == ["1304", "1301", "1303", "1302"]
        by_code = {i["code"]: i for i in data["items"]}
        assert by_code["1304"]["pbr"] == 1.0
        assert by_code["1303"]["pbr"] == 2.5

    @pytest.mark.asyncio
    async def test_descending(self, mock_val_server):
        result = await mock_val_server.call_tool(
            "get_valuation_ranking", {"metric": "per", "ascending": False}
        )
        data = _call(result)
        assert [i["code"] for i in data["items"]] == ["1302", "1305", "1301", "1303"]

    @pytest.mark.asyncio
    async def test_min_max_value_filter(self, mock_val_server):
        result = await mock_val_server.call_tool(
            "get_valuation_ranking", {"metric": "per", "min_value": 8, "max_value": 20}
        )
        data = _call(result)
        assert [i["code"] for i in data["items"]] == ["1301", "1305"]  # PER 10, 15

    @pytest.mark.asyncio
    async def test_market_filter(self, mock_val_server):
        result = await mock_val_server.call_tool(
            "get_valuation_ranking", {"metric": "per", "market": "standard"}
        )
        data = _call(result)
        assert [i["code"] for i in data["items"]] == ["1303"]  # only the standard-market stock

    @pytest.mark.asyncio
    async def test_n_limit(self, mock_val_server):
        result = await mock_val_server.call_tool("get_valuation_ranking", {"n": 2})
        data = _call(result)
        assert data["count"] == 2
        assert [i["code"] for i in data["items"]] == ["1303", "1301"]

    @pytest.mark.asyncio
    async def test_disc_months_staleness(self, mock_val_server):
        # Default disc_months=18 drops the 2024-01-01 disclosure (13060)...
        default = _call(await mock_val_server.call_tool("get_valuation_ranking", {}))
        assert "1306" not in {i["code"] for i in default["items"]}
        # ...but a wide window keeps it.
        wide = _call(await mock_val_server.call_tool("get_valuation_ranking", {"disc_months": 120}))
        assert "1306" in {i["code"] for i in wide["items"]}

    @pytest.mark.asyncio
    async def test_invalid_metric(self, mock_val_server):
        result = await mock_val_server.call_tool("get_valuation_ranking", {"metric": "roe"})
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_invalid_n(self, mock_val_server):
        result = await mock_val_server.call_tool("get_valuation_ranking", {"n": 0})
        data = _call(result)
        assert data.get("error") is True

    @pytest.mark.asyncio
    async def test_cache_not_ready(self, tmp_path):
        empty = _make_cache(tmp_path)
        with (
            patch.object(server_module, "_settings", Settings(jquants_api_key="")),
            patch.object(server_module, "_cache", empty),
            patch.object(server_module, "_client", None),
        ):
            result = await server_module.mcp.call_tool("get_valuation_ranking", {})
        data = _call(result)
        assert data["error_type"] == "CacheNotReady"

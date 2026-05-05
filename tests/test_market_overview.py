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
# name field injection
# ---------------------------------------------------------------------------


class TestNameField:
    """Both get_top_movers and get_top_volume inject a ``name`` field per item."""

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

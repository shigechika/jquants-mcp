"""Tests for scripts/verify_cache_completeness.py."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from verify_cache_completeness import (  # noqa: E402
    _check_daily_table,
    _check_earnings_calendar,
    _check_markets_calendar,
    _check_screener_results,
    _latest_trading_day,
    _table_exists,
    check_all,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
OLD_DATE = (date.today() - timedelta(days=60)).isoformat()
FUTURE_DATE = (date.today() + timedelta(days=30)).isoformat()


@pytest.fixture()
def conn(tmp_path):
    """Return an in-memory-like SQLite connection with all Tier 1 tables created."""
    db = tmp_path / "cache.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    # Minimal DDL for tables under test
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS equities_bars_daily (
            code TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS equities_master (
            code TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS fins_summary (
            code TEXT NOT NULL, disc_date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, disc_date)
        );
        CREATE TABLE IF NOT EXISTS equities_earnings_calendar (
            code TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS markets_calendar (
            date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (date)
        );
        CREATE TABLE IF NOT EXISTS indices_bars_daily_topix (
            date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (date)
        );
        CREATE TABLE IF NOT EXISTS investor_types (
            pub_date TEXT NOT NULL, section TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (pub_date, section)
        );
        CREATE TABLE IF NOT EXISTS markets_short_ratio (
            s33 TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (s33, date)
        );
        CREATE TABLE IF NOT EXISTS markets_margin_interest (
            code TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS markets_margin_alert (
            code TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS markets_breakdown (
            code TEXT NOT NULL, date TEXT NOT NULL,
            data TEXT NOT NULL, fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS screener_results (
            tool_name TEXT NOT NULL, params_hash TEXT NOT NULL,
            date TEXT NOT NULL, payload_json TEXT NOT NULL,
            computed_at REAL NOT NULL,
            PRIMARY KEY (tool_name, params_hash, date)
        );
        """
    )
    yield c
    c.close()


def _insert(conn, table: str, **cols) -> None:
    """Insert a single row with data='{}' and fetched_at=now."""
    col_names = list(cols.keys()) + ["data", "fetched_at"]
    placeholders = ", ".join("?" for _ in col_names)
    values = list(cols.values()) + ["{}", time.time()]
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def _insert_screener(conn, tool: str, d: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO screener_results "
        "(tool_name, params_hash, date, payload_json, computed_at) VALUES (?,?,?,?,?)",
        (tool, "hash", d, "{}", time.time()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _table_exists
# ---------------------------------------------------------------------------


def test_table_exists_true(conn):
    assert _table_exists(conn, "equities_bars_daily") is True


def test_table_exists_false(conn):
    assert _table_exists(conn, "nonexistent_table") is False


# ---------------------------------------------------------------------------
# _latest_trading_day
# ---------------------------------------------------------------------------


def test_latest_trading_day_from_calendar(conn):
    """Returns the most recent HolDivision=0 date from markets_calendar."""
    conn.execute(
        "INSERT INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
        (YESTERDAY, json.dumps({"HolDivision": "0"}), time.time()),
    )
    conn.execute(
        "INSERT INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
        (TODAY, json.dumps({"HolDivision": "1"}), time.time()),
    )
    conn.commit()
    result = _latest_trading_day(conn)
    assert result == YESTERDAY


def test_latest_trading_day_fallback_to_weekday(conn):
    """Falls back to nearest weekday when calendar is empty."""
    result = _latest_trading_day(conn)
    d = date.fromisoformat(result)
    assert d.weekday() < 5  # Monday–Friday


# ---------------------------------------------------------------------------
# _check_daily_table
# ---------------------------------------------------------------------------


def test_check_daily_table_ok(conn):
    _insert(conn, "equities_bars_daily", code="10000", date=TODAY)
    result = _check_daily_table(conn, "equities_bars_daily", "date", TODAY)
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["latest_date"] == TODAY
    assert result["gap_days"] == 0


def test_check_daily_table_stale(conn):
    _insert(conn, "equities_bars_daily", code="10000", date=OLD_DATE)
    result = _check_daily_table(conn, "equities_bars_daily", "date", TODAY)
    assert result["status"] == "stale"
    assert result["gap_days"] >= 50


def test_check_daily_table_empty(conn):
    result = _check_daily_table(conn, "equities_bars_daily", "date", TODAY)
    assert result["status"] == "empty"
    assert result["count"] == 0


def test_check_daily_table_missing(conn):
    result = _check_daily_table(conn, "nonexistent_table", "date", TODAY)
    assert result["status"] == "missing_table"


# ---------------------------------------------------------------------------
# fins_summary (uses _check_daily_table with disc_date column)
# ---------------------------------------------------------------------------


def test_check_fins_summary_ok(conn):
    _insert(conn, "fins_summary", code="10000", disc_date=TODAY)
    result = _check_daily_table(conn, "fins_summary", "disc_date", TODAY)
    assert result["status"] == "ok"


def test_check_fins_summary_stale(conn):
    _insert(conn, "fins_summary", code="10000", disc_date=OLD_DATE)
    result = _check_daily_table(conn, "fins_summary", "disc_date", TODAY)
    assert result["status"] == "stale"


def test_check_fins_summary_empty(conn):
    result = _check_daily_table(conn, "fins_summary", "disc_date", TODAY)
    assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# _check_markets_calendar
# ---------------------------------------------------------------------------


def test_check_markets_calendar_ok(conn):
    now = time.time()
    # Generate 3 years of daily entries → 1095 rows (> 1000 threshold)
    base = date(2023, 1, 1)
    rows = [((base + timedelta(days=i)).isoformat(), "{}", now) for i in range(1095)]
    conn.executemany(
        "INSERT OR REPLACE INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    result = _check_markets_calendar(conn)
    assert result["status"] == "ok"
    assert result["count"] >= 1000


def test_check_markets_calendar_partial(conn):
    _insert(conn, "markets_calendar", date=TODAY)
    result = _check_markets_calendar(conn)
    assert result["status"] == "partial"


def test_check_markets_calendar_empty(conn):
    result = _check_markets_calendar(conn)
    assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# _check_earnings_calendar
# ---------------------------------------------------------------------------


def test_check_earnings_calendar_ok(conn):
    _insert(conn, "equities_earnings_calendar", code="10000", date=FUTURE_DATE)
    result = _check_earnings_calendar(conn)
    assert result["status"] == "ok"
    assert result["has_future_dates"] is True


def test_check_earnings_calendar_stale_when_no_future(conn):
    _insert(conn, "equities_earnings_calendar", code="10000", date=OLD_DATE)
    result = _check_earnings_calendar(conn)
    assert result["status"] == "stale"
    assert result["has_future_dates"] is False


def test_check_earnings_calendar_empty(conn):
    result = _check_earnings_calendar(conn)
    assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# _check_screener_results
# ---------------------------------------------------------------------------


def test_check_screener_results_ok(conn):
    tool = "detect_price_limit"
    for i in range(200):
        d = (date.today() - timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO screener_results "
            "(tool_name, params_hash, date, payload_json, computed_at) VALUES (?,?,?,?,?)",
            (tool, f"h{i}", d, "{}", time.time()),
        )
    conn.commit()
    result = _check_screener_results(conn)
    assert result["status"] == "ok"
    assert result["by_tool"][tool]["count"] == 200


def test_check_screener_results_partial_when_few_rows(conn):
    _insert_screener(conn, "detect_price_limit", TODAY)
    result = _check_screener_results(conn)
    assert result["status"] == "partial"


def test_check_screener_results_empty(conn):
    result = _check_screener_results(conn)
    assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# check_all — integration
# ---------------------------------------------------------------------------


def test_check_all_overall_ok_when_all_tables_current(conn):
    """overall=ok when every applicable table has fresh data."""
    _insert(conn, "equities_bars_daily", code="10000", date=TODAY)
    _insert(conn, "equities_master", code="10000", date=TODAY)
    _insert(conn, "fins_summary", code="10000", disc_date=TODAY)
    _insert(conn, "equities_earnings_calendar", code="10000", date=FUTURE_DATE)
    _insert(conn, "indices_bars_daily_topix", date=TODAY)
    _insert(conn, "investor_types", pub_date=TODAY, section="TSEPrime")
    # seed enough markets_calendar rows (>1000 required)
    now = time.time()
    cal_base = date(2023, 1, 1)
    conn.executemany(
        "INSERT OR REPLACE INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
        [
            (
                (cal_base + timedelta(days=i)).isoformat(),
                json.dumps({"HolDivision": "0"}),
                now,
            )
            for i in range(1095)
        ],
    )
    conn.commit()
    # seed enough screener rows
    tool = "detect_price_limit"
    for i in range(200):
        d = (date.today() - timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO screener_results "
            "(tool_name, params_hash, date, payload_json, computed_at) VALUES (?,?,?,?,?)",
            (tool, f"h{i}", d, "{}", now),
        )
    conn.commit()

    result = check_all(conn, "light")
    assert result["overall"] == "ok"
    assert result["plan"] == "light"
    assert "trading_day" in result


def test_check_all_overall_degraded_when_tables_empty(conn):
    """overall=degraded when all tables are present but empty."""
    result = check_all(conn, "free")
    # All tables exist but are empty → 'empty' status → overall='degraded'
    assert result["overall"] == "degraded"


def test_check_all_overall_error_when_core_table_missing(tmp_path):
    """overall=error when a core table is absent."""
    db = tmp_path / "empty.db"
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    # No tables created → every core table is missing_table → overall='error'
    result = check_all(c, "free")
    c.close()
    assert result["overall"] == "error"
    assert any(info["status"] == "missing_table" for info in result["tables"].values())


def test_check_all_standard_tables_excluded_for_light(conn):
    """markets_short_ratio is not checked for light plan."""
    result = check_all(conn, "light")
    assert "markets_short_ratio" not in result["tables"]
    assert "markets_margin_interest" not in result["tables"]


def test_check_all_standard_tables_included_for_standard(conn):
    """markets_short_ratio is checked for standard plan."""
    result = check_all(conn, "standard")
    assert "markets_short_ratio" in result["tables"]
    assert "markets_margin_interest" in result["tables"]


def test_check_all_breakdown_excluded_for_standard(conn):
    """markets_breakdown is premium-only, not included for standard."""
    result = check_all(conn, "standard")
    assert "markets_breakdown" not in result["tables"]


def test_check_all_breakdown_included_for_premium(conn):
    """markets_breakdown is included for premium plan."""
    result = check_all(conn, "premium")
    assert "markets_breakdown" in result["tables"]


def test_check_all_result_has_required_keys(conn):
    result = check_all(conn, "free")
    assert "overall" in result
    assert "plan" in result
    assert "trading_day" in result
    assert "tables" in result


def test_check_all_json_serializable(conn):
    result = check_all(conn, "free")
    serialized = json.dumps(result)
    parsed = json.loads(serialized)
    assert parsed["plan"] == "free"

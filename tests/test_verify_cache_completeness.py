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
    _collect_fix_dates,
    _latest_trading_day,
    _table_exists,
    _trading_dates_in_range,
    auto_fix_gaps,
    check_all,
    check_date_gaps,
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
            adj_factor REAL,
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


# ---------------------------------------------------------------------------
# check_date_gaps
# ---------------------------------------------------------------------------


def _seed_bars(conn, date_str: str, num_codes: int, base_code: int = 1000) -> None:
    """Insert num_codes rows into equities_bars_daily for the given date."""
    for i in range(num_codes):
        code = str(base_code + i)
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily (code, date, data, fetched_at) "
            "VALUES (?, ?, '{}', ?)",
            (code, date_str, time.time()),
        )
    conn.commit()


def test_check_date_gaps_no_gaps(conn):
    # 5 normal days, all with ~4000 codes → no gap
    for i in range(5):
        d = (date(2026, 3, 9) + timedelta(days=i)).isoformat()
        _seed_bars(conn, d, 4000)
    result = check_date_gaps(conn)
    assert result["status"] == "ok"
    assert result["gaps"] == []
    assert result["total_dates"] == 5
    assert result["median_count"] == 4000


def test_check_date_gaps_detects_partial_fetch_day(conn):
    # 4 normal days (4000 codes) + 1 partial day (88 codes, like 2026-03-11)
    for i in range(4):
        d = (date(2026, 3, 9) + timedelta(days=i)).isoformat()
        _seed_bars(conn, d, 4000)
    _seed_bars(conn, "2026-03-13", 88)  # partial fetch day
    result = check_date_gaps(conn)
    assert result["status"] == "gaps_found"
    assert len(result["gaps"]) == 1
    assert result["gaps"][0]["date"] == "2026-03-13"
    assert result["gaps"][0]["count"] == 88
    assert result["gaps"][0]["pct"] < 80.0


def test_check_date_gaps_threshold_respected(conn):
    # With threshold=50, a day at 60% of median should NOT be flagged
    for i in range(4):
        d = (date(2026, 3, 9) + timedelta(days=i)).isoformat()
        _seed_bars(conn, d, 1000)
    _seed_bars(conn, "2026-03-13", 600)  # 60% of 1000
    result = check_date_gaps(conn, threshold_pct=50)
    assert result["status"] == "ok"
    assert result["gaps"] == []


def test_check_date_gaps_from_to_date_filter(conn):
    # Seed a bad day outside the filter window — must not be flagged
    _seed_bars(conn, "2026-01-10", 88)  # outside range
    for i in range(5):
        d = (date(2026, 3, 9) + timedelta(days=i)).isoformat()
        _seed_bars(conn, d, 4000)
    result = check_date_gaps(conn, from_date="2026-03-01")
    assert result["status"] == "ok"
    assert result["gaps"] == []


def test_check_date_gaps_missing_table(conn):
    # Drop the table and verify graceful handling
    conn.execute("DROP TABLE equities_bars_daily")
    result = check_date_gaps(conn)
    assert result["status"] == "missing_table"


def test_check_date_gaps_empty_table(conn):
    result = check_date_gaps(conn)
    assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# _trading_dates_in_range
# ---------------------------------------------------------------------------


def test_trading_dates_in_range_weekday_fallback(conn):
    # Monday 2026-03-09 to Friday 2026-03-13 (markets_calendar empty)
    dates = _trading_dates_in_range(conn, "2026-03-09", "2026-03-13")
    assert dates == [
        "2026-03-09",
        "2026-03-10",
        "2026-03-11",
        "2026-03-12",
        "2026-03-13",
    ]


def test_trading_dates_in_range_excludes_weekends(conn):
    # Saturday 2026-03-14 and Sunday 2026-03-15 should be excluded
    dates = _trading_dates_in_range(conn, "2026-03-14", "2026-03-16")
    assert dates == ["2026-03-16"]


def test_trading_dates_in_range_uses_calendar(conn):
    # Insert calendar rows: 2026-03-09 is a holiday (HolDivision='1'), 2026-03-10 is a trading day
    for d, hol in [("2026-03-09", "1"), ("2026-03-10", "0")]:
        conn.execute(
            "INSERT OR REPLACE INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
            (d, json.dumps({"HolDivision": hol}), time.time()),
        )
    conn.commit()
    dates = _trading_dates_in_range(conn, "2026-03-09", "2026-03-10")
    assert dates == ["2026-03-10"]


def test_trading_dates_in_range_all_holidays_returns_empty(conn):
    # All calendar entries are holidays → calendar is trusted → empty list (not weekday fallback)
    for d in ["2026-03-09", "2026-03-10"]:
        conn.execute(
            "INSERT OR REPLACE INTO markets_calendar (date, data, fetched_at) VALUES (?,?,?)",
            (d, json.dumps({"HolDivision": "1"}), time.time()),
        )
    conn.commit()
    dates = _trading_dates_in_range(conn, "2026-03-09", "2026-03-10")
    # Calendar says both days are holidays → no trading days; must NOT fall back to weekdays
    assert dates == []


# ---------------------------------------------------------------------------
# _collect_fix_dates
# ---------------------------------------------------------------------------


def test_collect_fix_dates_no_gaps_no_trailing(conn):
    # Table is up to date: max_date = latest trading day
    trading_day = _latest_trading_day(conn)
    _seed_bars(conn, trading_day, 4000)
    gaps_result = {"gaps": []}
    gap_dates, trailing = _collect_fix_dates(conn, gaps_result, None, None)
    assert gap_dates == []
    assert trailing == []


def test_collect_fix_dates_gap_included(conn):
    trading_day = _latest_trading_day(conn)
    _seed_bars(conn, trading_day, 4000)
    gaps_result = {"gaps": [{"date": "2026-03-19"}]}
    gap_dates, _ = _collect_fix_dates(conn, gaps_result, None, None)
    assert "2026-03-19" in gap_dates


def test_collect_fix_dates_trailing_detected(conn):
    # Insert data 10 days ago → trailing dates should appear
    old = (date.today() - timedelta(days=10)).isoformat()
    _seed_bars(conn, old, 4000)
    gaps_result = {"gaps": []}
    _, trailing = _collect_fix_dates(conn, gaps_result, None, None)
    assert len(trailing) > 0


def test_collect_fix_dates_from_date_filter(conn):
    trading_day = _latest_trading_day(conn)
    _seed_bars(conn, trading_day, 4000)
    gaps_result = {"gaps": [{"date": "2026-01-10"}, {"date": "2026-03-19"}]}
    gap_dates, _ = _collect_fix_dates(conn, gaps_result, from_date="2026-03-01", to_date=None)
    assert "2026-01-10" not in gap_dates
    assert "2026-03-19" in gap_dates


# ---------------------------------------------------------------------------
# auto_fix_gaps — dry_run
# ---------------------------------------------------------------------------


def test_auto_fix_gaps_dry_run_returns_plan(conn):
    # Seed old data so there are trailing dates
    old = (date.today() - timedelta(days=5)).isoformat()
    _seed_bars(conn, old, 4000)
    gaps_result = {"gaps": []}
    result = auto_fix_gaps(
        conn,
        gaps_result,
        api_key="dummy",
        base_url="https://api.jquants.com/v2",
        plan="light",
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert "all_dates" in result
    # Seeded data is 5 days old → at least 1 weekday must be trailing
    assert result["total_count"] >= 1


def test_auto_fix_gaps_dry_run_no_db_writes(conn):
    # Even with trailing dates, dry_run must not modify the DB
    old = (date.today() - timedelta(days=5)).isoformat()
    _seed_bars(conn, old, 4000)
    before = conn.execute("SELECT COUNT(*) FROM equities_bars_daily").fetchone()[0]
    gaps_result = {"gaps": []}
    auto_fix_gaps(
        conn,
        gaps_result,
        api_key="dummy",
        base_url="https://api.jquants.com/v2",
        plan="light",
        dry_run=True,
    )
    after = conn.execute("SELECT COUNT(*) FROM equities_bars_daily").fetchone()[0]
    assert before == after


# ---------------------------------------------------------------------------
# auto_fix_gaps — actual fix (mocked API)
# ---------------------------------------------------------------------------


def test_auto_fix_gaps_fixes_gap_date(conn, monkeypatch):
    # Seed a gap day (1 code) and a normal day
    _seed_bars(conn, "2026-03-18", 4000)
    _seed_bars(conn, "2026-03-19", 1)  # gap

    fake_rows = [
        {"Code": str(1000 + i), "Date": "2026-03-19", "AdjFactor": 1.0} for i in range(4000)
    ]

    import verify_cache_completeness as vcc  # noqa: PLC0415

    monkeypatch.setattr(vcc, "_fetch_bars_for_date", lambda *_a, **_kw: fake_rows)

    gaps_result = {"gaps": [{"date": "2026-03-19"}]}
    result = auto_fix_gaps(
        conn,
        gaps_result,
        api_key="dummy",
        base_url="https://api.jquants.com/v2",
        plan="light",
        dry_run=False,
    )
    assert result["dry_run"] is False
    assert any(f["date"] == "2026-03-19" for f in result["dates_fixed"])
    count = conn.execute(
        "SELECT COUNT(*) FROM equities_bars_daily WHERE date='2026-03-19'"
    ).fetchone()[0]
    assert count == 4000


def test_auto_fix_gaps_skips_empty_api_response(conn, monkeypatch):
    # API returns empty list → date is skipped, no errors
    old = (date.today() - timedelta(days=3)).isoformat()
    _seed_bars(conn, old, 4000)

    import verify_cache_completeness as vcc  # noqa: PLC0415

    monkeypatch.setattr(vcc, "_fetch_bars_for_date", lambda *_a, **_kw: [])

    gaps_result = {"gaps": []}
    result = auto_fix_gaps(
        conn,
        gaps_result,
        api_key="dummy",
        base_url="https://api.jquants.com/v2",
        plan="light",
        dry_run=False,
    )
    assert result["errors"] == []


def test_auto_fix_gaps_records_api_error(conn, monkeypatch):
    old = (date.today() - timedelta(days=3)).isoformat()
    _seed_bars(conn, old, 4000)

    import verify_cache_completeness as vcc  # noqa: PLC0415

    def _raise(*_a, **_kw):
        raise RuntimeError("network error")

    monkeypatch.setattr(vcc, "_fetch_bars_for_date", _raise)

    gaps_result = {"gaps": []}
    result = auto_fix_gaps(
        conn,
        gaps_result,
        api_key="dummy",
        base_url="https://api.jquants.com/v2",
        plan="light",
        dry_run=False,
    )
    assert len(result["errors"]) > 0
    assert "network error" in result["errors"][0]["error"]

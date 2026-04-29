"""Verify cache completeness for jquants-mcp.

Checks each Tier 1 table in the SQLite cache and reports status per table.
Useful after bulk imports, plan changes, or daily_fetch failures.

Usage:
    uv run python scripts/verify_cache_completeness.py
    uv run python scripts/verify_cache_completeness.py --plan standard --output json
    uv run python scripts/verify_cache_completeness.py --db /path/to/cache.db

Exit code:
    0  all tables ok
    1  one or more tables stale / empty / missing
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Plan constants (mirrors daily_fetch.py)
# ---------------------------------------------------------------------------

PLAN_LEVELS: dict[str, int] = {"free": 0, "light": 1, "standard": 2, "premium": 3}

# Tolerated staleness in days per table (how many days behind is still "ok")
_STALE_THRESHOLD: dict[str, int] = {
    "equities_bars_daily": 3,  # weekend + holiday gap
    "equities_master": 7,  # updated less frequently
    "fins_summary": 30,  # quarterly releases; uses disc_date column
    "indices_bars_daily_topix": 3,
    "investor_types": 10,  # published weekly
    "markets_short_ratio": 10,  # published weekly
    "markets_margin_interest": 7,
    "markets_margin_alert": 7,
    "markets_breakdown": 7,
}

# Minimum expected rows for screener_results per tool (52w * ~3 trading days)
_SCREENER_MIN_ROWS = 52 * 3

# ---------------------------------------------------------------------------
# Config / DB helpers
# ---------------------------------------------------------------------------


def _load_plan() -> str:
    plan = os.environ.get("JQUANTS_PLAN")
    if plan:
        return plan.lower()
    cfg = configparser.ConfigParser()
    cfg.read(
        [
            str(Path.home() / ".config" / "jquants-mcp" / "config.ini"),
            "config.ini",
        ],
        encoding="utf-8",
    )
    try:
        return cfg.get("jquants", "plan").lower()
    except (configparser.NoSectionError, configparser.NoOptionError):
        return "free"


def _default_db() -> str:
    cache_dir = os.environ.get("JQUANTS_CACHE_DIR", str(Path.home() / ".cache" / "jquants-mcp"))
    return str(Path(cache_dir) / "cache.db")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Trading day helper
# ---------------------------------------------------------------------------


def _latest_trading_day(conn: sqlite3.Connection) -> str:
    """Return most recent trading day (HolDivision='0') from markets_calendar.

    Falls back to the nearest past weekday when the table is unavailable or empty.
    """
    today = date.today()
    today_str = today.isoformat()
    if _table_exists(conn, "markets_calendar"):
        try:
            rows = conn.execute(
                "SELECT date, data FROM markets_calendar "
                "WHERE date <= ? ORDER BY date DESC LIMIT 14",
                (today_str,),
            ).fetchall()
            for row in rows:
                try:
                    cal = json.loads(row["data"]) if row["data"] else {}
                except (json.JSONDecodeError, TypeError):
                    cal = {}
                if str(cal.get("HolDivision", "")).strip() == "0":
                    return str(row["date"])[:10]
        except sqlite3.OperationalError:
            pass
    d = today
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


# ---------------------------------------------------------------------------
# Per-table checks
# ---------------------------------------------------------------------------


def _check_daily_table(
    conn: sqlite3.Connection, table: str, date_col: str, trading_day: str
) -> dict:
    """Check a table that should have fresh daily data."""
    if not _table_exists(conn, table):
        return {"status": "missing_table", "count": 0}
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS cnt, MAX({date_col}) AS latest FROM {table}"
        ).fetchone()
    except sqlite3.OperationalError as e:
        return {"status": "error", "count": 0, "detail": str(e)}
    count, latest = row["cnt"], row["latest"]
    if count == 0:
        return {"status": "empty", "count": 0, "latest_date": None}
    latest_str = str(latest)[:10]
    threshold = _STALE_THRESHOLD.get(table, 7)
    gap = (date.fromisoformat(trading_day) - date.fromisoformat(latest_str)).days
    status = "ok" if gap <= threshold else "stale"
    return {
        "status": status,
        "count": count,
        "latest_date": latest_str,
        "expected_latest": trading_day,
        "gap_days": gap,
    }


def _check_markets_calendar(conn: sqlite3.Connection) -> dict:
    """markets_calendar should have thousands of trading day entries."""
    if not _table_exists(conn, "markets_calendar"):
        return {"status": "missing_table", "count": 0}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, MAX(date) AS latest, MIN(date) AS earliest "
            "FROM markets_calendar"
        ).fetchone()
    except sqlite3.OperationalError as e:
        return {"status": "error", "count": 0, "detail": str(e)}
    count, latest, earliest = row["cnt"], row["latest"], row["earliest"]
    if count == 0:
        return {"status": "empty", "count": 0}
    # Expect at least several years of calendar data (>1000 rows)
    status = "ok" if count >= 1000 else "partial"
    return {
        "status": status,
        "count": count,
        "latest_date": str(latest)[:10] if latest else None,
        "earliest_date": str(earliest)[:10] if earliest else None,
    }


def _check_earnings_calendar(conn: sqlite3.Connection) -> dict:
    """equities_earnings_calendar should contain future announcement dates."""
    if not _table_exists(conn, "equities_earnings_calendar"):
        return {"status": "missing_table", "count": 0}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, MAX(date) AS latest FROM equities_earnings_calendar"
        ).fetchone()
    except sqlite3.OperationalError as e:
        return {"status": "error", "count": 0, "detail": str(e)}
    count, latest = row["cnt"], row["latest"]
    if count == 0:
        return {"status": "empty", "count": 0, "latest_date": None}
    latest_str = str(latest)[:10]
    today_str = date.today().isoformat()
    has_future = latest_str > today_str
    # ok if populated and contains forward-looking dates; stale if only past dates
    status = "ok" if has_future else "stale"
    return {
        "status": status,
        "count": count,
        "latest_date": latest_str,
        "has_future_dates": has_future,
    }


def _check_screener_results(conn: sqlite3.Connection) -> dict:
    """screener_results should have >= 156 rows per tool (52w × ~3 trading days/week)."""
    if not _table_exists(conn, "screener_results"):
        return {"status": "missing_table", "count": 0}
    try:
        rows = conn.execute(
            "SELECT tool_name, COUNT(*) AS cnt, MAX(date) AS latest "
            "FROM screener_results GROUP BY tool_name"
        ).fetchall()
    except sqlite3.OperationalError as e:
        return {"status": "error", "count": 0, "detail": str(e)}
    if not rows:
        return {"status": "empty", "count": 0}
    by_tool = {
        r["tool_name"]: {"count": r["cnt"], "latest_date": str(r["latest"])[:10]} for r in rows
    }
    total = sum(v["count"] for v in by_tool.values())
    status = "ok" if all(v["count"] >= _SCREENER_MIN_ROWS for v in by_tool.values()) else "partial"
    return {"status": status, "count": total, "by_tool": by_tool}


# ---------------------------------------------------------------------------
# Main check runner
# ---------------------------------------------------------------------------


def check_all(conn: sqlite3.Connection, plan: str) -> dict:
    """Run all applicable checks and return a summary dict."""
    trading_day = _latest_trading_day(conn)
    plan_level = PLAN_LEVELS.get(plan, 0)

    tables: dict[str, dict] = {}

    # Core tables (always checked)
    tables["equities_bars_daily"] = _check_daily_table(
        conn, "equities_bars_daily", "date", trading_day
    )
    tables["equities_master"] = _check_daily_table(conn, "equities_master", "date", trading_day)
    tables["fins_summary"] = _check_daily_table(conn, "fins_summary", "disc_date", trading_day)
    tables["equities_earnings_calendar"] = _check_earnings_calendar(conn)
    tables["markets_calendar"] = _check_markets_calendar(conn)
    tables["screener_results"] = _check_screener_results(conn)

    # Light+ tables
    if plan_level >= PLAN_LEVELS["light"]:
        tables["indices_bars_daily_topix"] = _check_daily_table(
            conn, "indices_bars_daily_topix", "date", trading_day
        )
        tables["investor_types"] = _check_daily_table(
            conn, "investor_types", "pub_date", trading_day
        )

    # Standard+ tables
    if plan_level >= PLAN_LEVELS["standard"]:
        tables["markets_short_ratio"] = _check_daily_table(
            conn, "markets_short_ratio", "date", trading_day
        )
        tables["markets_margin_interest"] = _check_daily_table(
            conn, "markets_margin_interest", "date", trading_day
        )
        tables["markets_margin_alert"] = _check_daily_table(
            conn, "markets_margin_alert", "date", trading_day
        )

    # Premium+ tables
    if plan_level >= PLAN_LEVELS["premium"]:
        tables["markets_breakdown"] = _check_daily_table(
            conn, "markets_breakdown", "date", trading_day
        )

    statuses = [t["status"] for t in tables.values()]
    if all(s == "ok" for s in statuses):
        overall = "ok"
    elif any(s in ("error", "missing_table") for s in statuses):
        overall = "error"
    else:
        overall = "degraded"

    return {
        "overall": overall,
        "plan": plan,
        "trading_day": trading_day,
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

_STATUS_SYMBOL = {
    "ok": "✓",
    "stale": "!",
    "partial": "~",
    "empty": "✗",
    "missing_table": "✗",
    "error": "✗",
}
_OVERALL_SYMBOL = {"ok": "✓", "degraded": "!", "error": "✗"}


def _print_text(result: dict) -> None:
    overall = result["overall"]
    sym = _OVERALL_SYMBOL.get(overall, "?")
    print(f"[{sym}] overall={overall}  plan={result['plan']}  trading_day={result['trading_day']}")
    print()
    for table, info in result["tables"].items():
        status = info["status"]
        sym = _STATUS_SYMBOL.get(status, "?")
        count = info.get("count", 0)
        latest = info.get("latest_date") or info.get("earliest_date") or "-"
        gap = info.get("gap_days")
        gap_str = f"  gap={gap}d" if gap is not None else ""
        print(f"  [{sym}] {table:<35} {status:<15} rows={count:<8} latest={latest}{gap_str}")
        if "by_tool" in info:
            for tool, tinfo in info["by_tool"].items():
                print(
                    f"         {tool:<51}"
                    f" rows={tinfo['count']:<8} latest={tinfo.get('latest_date', '-')}"
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    plan_default = _load_plan()
    db_default = _default_db()

    parser = argparse.ArgumentParser(description="Verify jquants-mcp cache completeness.")
    parser.add_argument(
        "--plan",
        default=plan_default,
        help=f"Subscription plan (default: {plan_default})",
    )
    parser.add_argument(
        "--db",
        default=db_default,
        help=f"Path to cache.db (default: {db_default})",
    )
    parser.add_argument(
        "--output",
        choices=["json", "text"],
        default="text",
        help="Output format (default: text)",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: cache.db not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = _connect(args.db)
    try:
        result = check_all(conn, args.plan)
    finally:
        conn.close()

    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)

    sys.exit(0 if result["overall"] == "ok" else 1)


if __name__ == "__main__":
    main()

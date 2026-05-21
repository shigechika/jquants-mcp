"""Fetch daily J-Quants data and insert directly into the SQLite cache.

Intended as a cron / scheduled-task companion to the MCP server: it
pulls the latest day's data into the local cache so subsequent tool
calls hit warm rows instead of round-tripping to the J-Quants API.

Dependencies: ``jquantsapi`` + the Python standard library. The script
does NOT import from the ``jquants_mcp`` package beyond the schema
module (imported via sys.path) so it can also run inside a consumer
project's own virtualenv.

Plan is read from ``~/.config/jquants-mcp/config.ini`` section
``[jquants]`` key ``plan``. The plan decides which endpoints are
fetched (Free / Light / Standard / Premium). Individual flags override
the plan default.

Usage:
    python3 scripts/daily_fetch.py                    # fetch everything allowed by the plan
    python3 scripts/daily_fetch.py --topix             # TOPIX only
    python3 scripts/daily_fetch.py --fins-summary      # earnings summary only
    python3 scripts/daily_fetch.py --earnings-cal      # earnings calendar only
    python3 scripts/daily_fetch.py --short-ratio       # short ratio only (Standard+)
    python3 scripts/daily_fetch.py --margin-interest   # margin interest only (Standard+)
    python3 scripts/daily_fetch.py --backfill 90       # backfill the last 90 days
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_mcp.cache.schema import (  # noqa: E402
    RESPONSE_CACHE_DDL,
    SCREENER_RESULTS_DDL,
    SCREENER_RESULTS_INDEX_DDL,
    TIER1_TABLES,
    generate_ddl,
)
from jquants_mcp.cache import screener_compute  # noqa: E402  # stdlib-only

import jquantsapi  # noqa: E402

# キャッシュ DB のデフォルトパス
DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-mcp" / "cache.db"

# 決算サマリーのデフォルト遡り日数
FINS_LOOKBACK_DAYS = 7

# プラン別取得可能エンドポイント（J-Quants API v2 仕様に基づく）
# 各エンドポイントの最低必要プランを定義
PLAN_LEVELS = {"free": 0, "light": 1, "standard": 2, "premium": 3}

ENDPOINT_MIN_PLAN: dict[str, str] = {
    "fins_summary": "free",
    "earnings_cal": "free",
    "calendar": "free",
    "topix": "light",
    "investor_types": "light",
    "short_ratio": "standard",
    "margin_interest": "standard",
    "margin_alert": "standard",
    "short_sale_report": "standard",
    "breakdown": "premium",
}

# Tier 2 キャッシュの TTL（秒）— MCP サーバーの設定に合わせる
TTL_6H = 6 * 3600
TTL_24H = 24 * 3600
TTL_7D = 7 * 24 * 3600
TTL_90D = 90 * 24 * 3600


def _load_plan() -> str:
    """config.ini と環境変数からプランを読み取る。"""
    # 環境変数が最優先
    plan = os.environ.get("JQUANTS_PLAN")
    if plan:
        return plan.lower()

    # config.ini を探索
    config = configparser.ConfigParser()
    search_paths = [
        str(Path.home() / ".config" / "jquants-mcp" / "config.ini"),
        "config.ini",
    ]
    config.read(search_paths, encoding="utf-8")

    try:
        return config.get("jquants", "plan").lower()
    except (configparser.NoSectionError, configparser.NoOptionError):
        return "free"


def _available_endpoints(plan: str) -> list[str]:
    """プランで取得可能なエンドポイント一覧を返す。"""
    plan_level = PLAN_LEVELS.get(plan, 0)
    return [
        ep
        for ep, min_plan in ENDPOINT_MIN_PLAN.items()
        if PLAN_LEVELS.get(min_plan, 0) <= plan_level
    ]


_DAILY_FETCH_TABLES = [
    "indices_bars_daily_topix",
    "fins_summary",
    "investor_types",
    "markets_margin_interest",
    "markets_margin_alert",
    "markets_short_ratio",
    "markets_breakdown",
    "markets_calendar",
    "equities_earnings_calendar",
]


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create required tables if they do not exist."""
    for name in _DAILY_FETCH_TABLES:
        conn.execute(generate_ddl(name, TIER1_TABLES[name]))
    conn.execute(RESPONSE_CACHE_DDL)
    conn.execute(SCREENER_RESULTS_DDL)
    conn.execute(SCREENER_RESULTS_INDEX_DDL)
    conn.commit()
    _migrate_drop_plan(conn)


def _migrate_drop_plan(conn: sqlite3.Connection) -> None:
    """Remove plan column from Tier 1 tables if present.

    Mirrors the same migration in store.py._migrate_drop_plan() so that
    daily_fetch.py (which connects directly, bypassing CacheStore) can
    also clean up legacy plan columns.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return

    _TABLES_WITH_PLAN_SCHEMA = {
        "indices_bars_daily_topix": ("date", "date"),
        "fins_summary": ("code, disc_date", "code, disc_date"),
        "investor_types": ("pub_date, section", "pub_date, section"),
        "markets_margin_interest": ("code, date", "code, date"),
        "markets_margin_alert": ("code, date", "code, date"),
        "markets_short_ratio": ("s33, date", "s33, date"),
        "markets_breakdown": ("code, date", "code, date"),
        "markets_calendar": ("date", "date"),
    }

    migrated = False
    for table, (key_cols, pk_cols) in _TABLES_WITH_PLAN_SCHEMA.items():
        cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = [c[1] for c in cols_info]
        if "plan" not in col_names:
            continue

        pk_positions = [c[1] for c in cols_info if c[5] > 0]
        select_cols = [c for c in col_names if c != "plan"]
        select_str = ", ".join(select_cols)

        if "plan" in pk_positions:
            # Rebuild: deduplicate (highest plan wins)
            conn.execute(f"""
                CREATE TABLE {table}_v2 AS SELECT {select_str} FROM (
                    SELECT {select_str},
                        ROW_NUMBER() OVER (
                            PARTITION BY {pk_cols}
                            ORDER BY CASE plan
                                WHEN 'premium' THEN 3 WHEN 'standard' THEN 2
                                WHEN 'light' THEN 1 ELSE 0 END DESC
                        ) AS rn
                    FROM {table}
                ) WHERE rn = 1
            """)
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_v2 RENAME TO {table}")
        else:
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN plan")
            except sqlite3.OperationalError:
                pass
        migrated = True

    # Tier 2: strip |plan= suffix
    try:
        has_plan_keys = conn.execute(
            "SELECT COUNT(*) FROM response_cache WHERE cache_key LIKE '%|plan=%'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        has_plan_keys = 0

    if has_plan_keys:
        conn.execute("""
            CREATE TABLE response_cache_v2 (
                cache_key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                ttl_seconds INTEGER NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR REPLACE INTO response_cache_v2
                (cache_key, data, fetched_at, ttl_seconds)
            SELECT
                CASE WHEN INSTR(cache_key, '|plan=') > 0
                    THEN SUBSTR(cache_key, 1, INSTR(cache_key, '|plan=') - 1)
                    ELSE cache_key END,
                data, fetched_at, ttl_seconds
            FROM response_cache
            ORDER BY fetched_at ASC
        """)
        conn.execute("DROP TABLE response_cache")
        conn.execute("ALTER TABLE response_cache_v2 RENAME TO response_cache")
        migrated = True

    if migrated:
        print("  マイグレーション: plan カラムを除去しました")

    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def _sanitize_row(row_data: dict) -> dict:
    """NaN を None に変換する（JSON シリアライズ用）。"""
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in row_data.items()}


def _store_tier1(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict],
    key_mapping: list[tuple[str, str]],
) -> int:
    """Insert records into a Tier 1 table.

    Args:
        conn: SQLite connection
        table: Table name
        rows: API response row data
        key_mapping: List of (API column name, DB column name)

    Returns:
        Number of inserted rows
    """
    if not rows:
        return 0

    now = time.time()
    db_col_names = ", ".join([db_col for _, db_col in key_mapping])
    placeholders = ", ".join(["?"] * (len(key_mapping) + 2))
    sql = (
        f"INSERT OR REPLACE INTO {table} ({db_col_names}, data, fetched_at) VALUES ({placeholders})"
    )

    count = 0
    for row in rows:
        # Normalize key column values: str(pandas.Timestamp) → "YYYY-MM-DD HH:MM:SS"; strip to date only.
        # Apply to both the DB key and the stored JSON so they stay consistent.
        normalized_keys = {
            api_col: str(row.get(api_col, "")).split(" ")[0].split("T")[0]
            for api_col, _ in key_mapping
        }
        key_values = [normalized_keys[api_col] for api_col, _ in key_mapping]
        row_for_json = {**row, **normalized_keys}
        data_json = json.dumps(row_for_json, ensure_ascii=False, default=str)
        conn.execute(sql, key_values + [data_json, now])
        count += 1

    conn.commit()
    return count


def _get_max_date(
    conn: sqlite3.Connection,
    table: str,
    date_column: str = "date",
) -> str | None:
    """Get the latest date from a Tier 1 table."""
    try:
        row = conn.execute(f"SELECT MAX({date_column}) FROM {table}").fetchone()
        return row[0][:10] if row and row[0] else None
    except sqlite3.OperationalError:
        return None


def fetch_topix(cli: jquantsapi.ClientV2, conn: sqlite3.Connection, plan: str) -> int:
    """Fetch TOPIX daily bars incrementally and insert into cache."""
    max_date = _get_max_date(conn, "indices_bars_daily_topix")

    if max_date:
        from_date = (datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        print(f"  キャッシュ最新日: {max_date}、{from_date} から取得")
        df = cli.get_idx_bars_daily_topix(from_yyyymmdd=from_date)
    else:
        print("  キャッシュ空、全期間取得")
        df = cli.get_idx_bars_daily_topix()

    if df is None or len(df) == 0:
        print("  新しいデータなし")
        return 0

    now = time.time()
    count = 0
    for _, r in df.iterrows():
        # str(pandas.Timestamp) produces "YYYY-MM-DD HH:MM:SS"; strip to date only
        date_str = str(r["Date"])[:10]
        row_dict = _sanitize_row(r.to_dict())
        row_dict["Date"] = date_str
        data_json = json.dumps(row_dict, ensure_ascii=False, default=str)
        conn.execute(
            "INSERT OR REPLACE INTO indices_bars_daily_topix "
            "(date, data, fetched_at) VALUES (?, ?, ?)",
            (date_str, data_json, now),
        )
        count += 1

    conn.commit()
    return count


def fetch_fins_summary(cli: jquantsapi.ClientV2, conn: sqlite3.Connection, plan: str) -> int:
    """Fetch recent financial summaries and insert into cache."""
    today = datetime.today()
    count = 0
    now = time.time()

    for days_ago in range(FINS_LOOKBACK_DAYS):
        date = today - timedelta(days=days_ago)
        date_str = date.strftime("%Y%m%d")
        date_iso = date.strftime("%Y-%m-%d")

        try:
            df = cli.get_fin_summary(date_yyyymmdd=date_str)
        except Exception as e:
            print(f"  {date_iso}: エラー ({e})")
            continue

        if df is None or len(df) == 0:
            continue

        for _, r in df.iterrows():
            data_dict = _sanitize_row(r.to_dict())
            code = str(r.get("Code", ""))
            disc_date = str(r.get("DiscDate", date_iso))[:10]
            # Annual results produce two API rows on the same disc_date:
            #   FYFinancialStatements  : NxFDivAnn=<next-FY forecast>, FDivAnn=''
            #   DividendForecastRevision: FDivAnn=<trailing actual>,    NxFDivAnn=''
            # INSERT OR REPLACE deletes then re-inserts, so whichever arrives second
            # wins and the other's field is lost.  Carry NxFDivAnn forward so the
            # next-FY forecast survives even when the revision row is processed last.
            # Most non-annual filings also lack NxFDivAnn, so a SELECT fires for
            # the majority of rows on any given day; this is acceptable for a
            # background script.
            nx = data_dict.get("NxFDivAnn")
            if nx is None or nx == "":
                existing = conn.execute(
                    "SELECT data FROM fins_summary WHERE code=? AND disc_date=? LIMIT 1",
                    (code, disc_date),
                ).fetchone()
                if existing:
                    existing_data = json.loads(existing[0])
                    existing_nx = existing_data.get("NxFDivAnn")
                    if existing_nx is not None and existing_nx != "":
                        data_dict["NxFDivAnn"] = existing_nx
            data_json = json.dumps(data_dict, ensure_ascii=False, default=str)
            conn.execute(
                "INSERT OR REPLACE INTO fins_summary "
                "(code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
                (code, disc_date, data_json, now),
            )
            count += 1

        conn.commit()
        print(f"  {date_iso}: {len(df)} 件")

    return count


def fetch_earnings_calendar(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch earnings calendar and store in Tier 2 response cache by date.

    The API returns earnings announcements for the next business day.
    Stored with date-keyed entries to accumulate ~3 months of data (TTL 90 days).
    """
    df = cli.get_eq_earnings_cal()

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    records = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]

    # Normalize Date to YYYY-MM-DD (pd.Timestamp serializes as "YYYY-MM-DD HH:MM:SS")
    for rec in records:
        if rec.get("Date") is not None:
            rec["Date"] = str(rec["Date"])[:10]

    date_val = records[0].get("Date") if records else None
    if date_val is not None:
        date_key = str(date_val)[:10].replace("-", "")
    else:
        date_key = datetime.today().strftime("%Y%m%d")

    now = time.time()
    response_data = json.dumps(records, ensure_ascii=False, default=str)

    # 日付別キーで蓄積（TTL 90日）
    cache_key = f"/equities/earnings-calendar?date={date_key}"
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (cache_key, response_data, now, TTL_90D),
    )

    # パラメータなしキーも更新（最新データ用）
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        ("/equities/earnings-calendar", response_data, now, TTL_90D),
    )

    # Tier 1: store each record by (code, date) for efficient cross-sectional queries
    for rec in records:
        code = str(rec.get("Code", ""))
        ann_date = str(rec.get("Date", ""))[:10]  # normalize to YYYY-MM-DD
        if not code or len(ann_date) < 10:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO equities_earnings_calendar "
            "(code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (code, ann_date, json.dumps(rec, ensure_ascii=False, default=str), now),
        )

    conn.commit()

    return len(records)


def fetch_investor_types(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch investor type data and insert into Tier 1 cache (Light+).

    Weekly data (published every Thursday). Fetches the last 2 weeks.
    """
    today = datetime.today()
    from_date = (today - timedelta(days=14)).strftime("%Y%m%d")
    to_date = today.strftime("%Y%m%d")

    df = cli.get_eq_investor_types(from_yyyymmdd=from_date, to_yyyymmdd=to_date)

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    now = time.time()
    count = 0
    for _, r in df.iterrows():
        data_json = json.dumps(_sanitize_row(r.to_dict()), ensure_ascii=False, default=str)
        pub_date = str(r.get("PublishedDate", r.get("PubDate", "")))[:10]
        section = str(r.get("Section", ""))
        conn.execute(
            "INSERT OR REPLACE INTO investor_types "
            "(pub_date, section, data, fetched_at) VALUES (?, ?, ?, ?)",
            (pub_date, section, data_json, now),
        )
        count += 1

    conn.commit()
    return count


def _store_response_cache(
    conn: sqlite3.Connection,
    cache_key: str,
    data: "list[dict] | dict",
    ttl: int,
) -> int:
    """Store data in Tier 2 response cache.

    data must be either a plain list[dict] or a pre-wrapped
    {"count": N, "data": [...]} dict to match the format stored by the MCP
    tool itself when it falls back to the API.
    """
    if not data:
        print("  データなし")
        return 0

    response_data = json.dumps(data, ensure_ascii=False, default=str)
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (cache_key, response_data, now, ttl),
    )
    conn.commit()
    if isinstance(data, dict):
        return len(data.get("data", []))
    return len(data)


# ------------------------------------------------------------------
# Markets Tier 1 取得関数
# ------------------------------------------------------------------


def _fetch_markets_tier1(
    cli_method,
    conn: sqlite3.Connection,
    table: str,
    key_mapping: list[tuple[str, str]],
    *,
    plan: str = "free",
    from_yyyymmdd: str = "",
    to_yyyymmdd: str = "",
    date_yyyymmdd: str = "",
    date_column: str = "date",
    incremental: bool = True,
    **extra_params,
) -> int:
    """Insert Markets data into a Tier 1 cache table.

    Args:
        cli_method: jquantsapi method
        conn: SQLite connection
        table: Tier 1 table name
        key_mapping: List of (API column name, DB column name)
        plan: Subscription plan (used for endpoint access control only)
        from_yyyymmdd: Start date
        to_yyyymmdd: End date
        date_yyyymmdd: Specific date
        date_column: DB date column name
        incremental: If True, fetch only new data
        **extra_params: Extra params passed to cli_method
    """
    # 差分取得: キャッシュ最新日の翌日から
    if incremental and not from_yyyymmdd and not date_yyyymmdd:
        max_date = _get_max_date(conn, table, date_column)
        if max_date:
            from_yyyymmdd = (
                datetime.strptime(max_date[:10], "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y%m%d")
            print(f"  キャッシュ最新日: {max_date}、{from_yyyymmdd} から取得")

    params = {**extra_params}
    if from_yyyymmdd:
        params["from_yyyymmdd"] = from_yyyymmdd
    if to_yyyymmdd:
        params["to_yyyymmdd"] = to_yyyymmdd
    if date_yyyymmdd:
        params["date_yyyymmdd"] = date_yyyymmdd

    # 日次取得で日付指定なしの場合はデフォルトで当日
    if not from_yyyymmdd and not to_yyyymmdd and not date_yyyymmdd:
        params["date_yyyymmdd"] = datetime.today().strftime("%Y%m%d")

    try:
        df = cli_method(**params)
    except Exception as e:
        print(f"  エラー: {e}")
        return 0

    if df is None or len(df) == 0:
        # 当日データなしの場合、パラメータなしでフォールバック
        if date_yyyymmdd or params.get("date_yyyymmdd"):
            print("  当日データなし、パラメータなしで取得")
            try:
                df = cli_method(**{k: v for k, v in extra_params.items()})
            except Exception as e:
                print(f"  フォールバックエラー: {e}")
                return 0

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    rows = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_tier1(conn, table, rows, key_mapping)


def fetch_short_ratio(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch sector short-selling ratios into Tier 1 and Tier 2 cache (Standard+)."""
    count = _fetch_markets_tier1(
        cli.get_mkt_short_ratio,
        conn,
        table="markets_short_ratio",
        key_mapping=[("S33", "s33"), ("Date", "date")],
        plan=plan,
    )
    # Populate Tier 2 cache for no-params tool calls
    try:
        df_all = cli.get_mkt_short_ratio()
        if df_all is not None and len(df_all) > 0:
            records = [_sanitize_row(r.to_dict()) for _, r in df_all.iterrows()]
            _store_response_cache(
                conn, "/markets/short-ratio", {"count": len(records), "data": records}, TTL_24H
            )
            print(f"  Tier 2: {len(records)} 件")
    except Exception as e:
        print(f"  Tier 2 エラー: {e}")
    return count


def fetch_margin_interest(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch margin interest data into Tier 1 cache (Standard+)."""
    return _fetch_markets_tier1(
        cli.get_mkt_margin_interest,
        conn,
        table="markets_margin_interest",
        key_mapping=[("Code", "code"), ("Date", "date")],
        plan=plan,
    )


def fetch_margin_alert(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch margin alert data into Tier 1 cache (Standard+)."""
    return _fetch_markets_tier1(
        cli.get_mkt_margin_alert,
        conn,
        table="markets_margin_alert",
        key_mapping=[("Code", "code"), ("PubDate", "date")],
        date_column="date",
        plan=plan,
    )


def fetch_short_sale_report(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch short sale report into Tier 2 cache (Standard+).

    Multiple reporters per code+date, so Tier 2 is used.
    """
    today = datetime.today().strftime("%Y%m%d")
    try:
        df = cli.get_mkt_short_sale_report(calculated_date=today)
    except Exception as e:
        print(f"  エラー: {e}")
        return 0

    if df is None or len(df) == 0:
        print("  当日データなし、パラメータなしで取得")
        try:
            df = cli.get_mkt_short_sale_report()
        except Exception as e:
            print(f"  フォールバックエラー: {e}")
            return 0

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    records = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_response_cache(
        conn, "/markets/short-sale-report", {"count": len(records), "data": records}, TTL_24H
    )


def fetch_breakdown(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch trade breakdown data into Tier 1 cache (Premium)."""
    return _fetch_markets_tier1(
        cli.get_mkt_breakdown,
        conn,
        table="markets_breakdown",
        key_mapping=[("Code", "code"), ("Date", "date")],
        plan=plan,
    )


# ------------------------------------------------------------------
# Screener result pre-compute (Issue #142)
# ------------------------------------------------------------------


# Re-exported so the print message below has a stable name even if the
# upstream constant is renamed; the source of truth lives in
# ``screener_compute`` so reader/writer can never drift apart.
_SCREENER_RETENTION_WEEKS = screener_compute.SCREENER_CACHE_LOOKBACK_WEEKS


def _screener_default_jobs() -> list[tuple[str, str, dict]]:
    """Return the (tool_name, params_hash, kwargs) triples to pre-compute.

    Kwargs match the ``screener_compute.compute_for_date`` signature
    (excluding ``conn`` / ``norm_date`` / ``tool_name``).
    """
    return [
        (
            screener_compute.TOOL_DETECT_52W,
            screener_compute.default_params_hash_52w(),
            {
                "window_sessions": screener_compute.DEFAULT_FIFTY_TWO_WEEK_SESSIONS,
                "min_prior_sessions": screener_compute.DEFAULT_MIN_PRIOR_SESSIONS,
                "mode_label": "52w",
            },
        ),
        (
            screener_compute.TOOL_DETECT_YTD,
            screener_compute.default_params_hash_ytd(),
            {
                "window_sessions": None,
                "min_prior_sessions": screener_compute.DEFAULT_MIN_PRIOR_SESSIONS,
                "mode_label": "ytd",
            },
        ),
    ]


def populate_screener_results(conn: sqlite3.Connection) -> int:
    """Pre-compute and cache screener payloads for the latest session.

    Computes ``detect_52w_high_low`` and ``detect_ytd_high_low`` with
    default parameters for the most recent date in
    ``equities_bars_daily`` and ``INSERT OR REPLACE``s the rows.
    Then prunes rows older than 52 weeks. Returns the number of new
    or replaced rows.
    """
    latest = screener_compute.latest_session_date(conn)
    if latest is None:
        print("  equities_bars_daily が空のためスキップ")
        return 0

    print(f"  対象日: {latest}")
    written = 0
    for tool_name, params_hash, kwargs in _screener_default_jobs():
        t0 = time.time()
        payload = screener_compute.compute_for_date(
            conn,
            norm_date=latest,
            **kwargs,
        )
        screener_compute.upsert_screener_result(
            conn,
            tool_name=tool_name,
            params_hash_value=params_hash,
            norm_date=latest,
            payload=payload,
            computed_at=time.time(),
        )
        written += 1
        elapsed = time.time() - t0
        print(f"    {tool_name}: count={payload.get('count')} ({elapsed:.1f}s)")

    pruned = screener_compute.prune_old_results(conn, retention_weeks=_SCREENER_RETENTION_WEEKS)
    conn.commit()
    if pruned:
        print(f"  保持期間外 ({_SCREENER_RETENTION_WEEKS} 週) の {pruned} 行を削除")
    return written


def fetch_calendar(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch trading calendar into Tier 1 cache (Free+)."""
    try:
        df = cli.get_mkt_calendar()
    except Exception as e:
        print(f"  エラー: {e}")
        return 0

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    rows = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_tier1(conn, "markets_calendar", rows, [("Date", "date")])


# ------------------------------------------------------------------
# バックフィル: 過去データの一括取得
# ------------------------------------------------------------------


def _backfill_markets_tier1(
    cli_method,
    conn: sqlite3.Connection,
    table: str,
    key_mapping: list[tuple[str, str]],
    from_yyyymmdd: str,
    to_yyyymmdd: str,
    plan: str,
    **extra_params,
) -> int:
    """Bulk-fetch historical Markets data into Tier 1."""
    return _fetch_markets_tier1(
        cli_method,
        conn,
        table=table,
        key_mapping=key_mapping,
        plan=plan,
        from_yyyymmdd=from_yyyymmdd,
        to_yyyymmdd=to_yyyymmdd,
        incremental=False,
        **extra_params,
    )


# エンドポイント名 → (表示名, 取得関数)
FETCH_REGISTRY: dict[str, tuple[str, callable]] = {
    "topix": ("TOPIX 日足", fetch_topix),
    "fins_summary": ("決算サマリー", fetch_fins_summary),
    "earnings_cal": ("決算発表予定", fetch_earnings_calendar),
    "investor_types": ("投資部門別売買動向", fetch_investor_types),
    "short_ratio": ("業種別空売り比率", fetch_short_ratio),
    "margin_interest": ("信用取引残高", fetch_margin_interest),
    "margin_alert": ("増担保規制情報", fetch_margin_alert),
    "short_sale_report": ("空売り残高報告", fetch_short_sale_report),
    "breakdown": ("売買内訳", fetch_breakdown),
    "calendar": ("取引カレンダー", fetch_calendar),
}

# バックフィル対応エンドポイント（from/to 範囲指定可能なもの）
BACKFILL_REGISTRY: dict[str, tuple[str, callable, str, list[tuple[str, str]]]] = {
    # key: (表示名, cli_method_name, table, key_mapping)
    "short_ratio": (
        "業種別空売り比率",
        "get_mkt_short_ratio",
        "markets_short_ratio",
        [("S33", "s33"), ("Date", "date")],
    ),
    "margin_interest": (
        "信用取引残高",
        "get_mkt_margin_interest",
        "markets_margin_interest",
        [("Code", "code"), ("Date", "date")],
    ),
    "margin_alert": (
        "増担保規制情報",
        "get_mkt_margin_alert",
        "markets_margin_alert",
        [("Code", "code"), ("PubDate", "date")],
    ),
    "breakdown": (
        "売買内訳",
        "get_mkt_breakdown",
        "markets_breakdown",
        [("Code", "code"), ("Date", "date")],
    ),
}


def _run_backfill(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    targets: list[str],
    days: int,
    plan: str,
) -> None:
    """Run backfill for the specified number of days."""
    today = datetime.today()
    from_date = (today - timedelta(days=days)).strftime("%Y%m%d")
    to_date = today.strftime("%Y%m%d")

    print(f"バックフィル: {from_date} → {to_date} ({days}日間)")

    for ep in targets:
        if ep not in BACKFILL_REGISTRY:
            print(f"  {ep}: バックフィル非対応、スキップ")
            continue

        label, method_name, table, key_mapping = BACKFILL_REGISTRY[ep]
        cli_method = getattr(cli, method_name)
        print(f"{label}をバックフィル中...")
        t0 = time.time()
        try:
            n = _backfill_markets_tier1(
                cli_method,
                conn,
                table,
                key_mapping,
                from_yyyymmdd=from_date,
                to_yyyymmdd=to_date,
                plan=plan,
            )
        except Exception as e:
            print(f"  エラー: {e}")
            n = 0
        print(f"  完了: {n} 件 ({time.time() - t0:.1f}秒)")

    # カレンダーはバックフィル対象に含まれていたら全件取得
    if "calendar" in targets:
        print("取引カレンダーを取得中...")
        t0 = time.time()
        try:
            n = fetch_calendar(cli, conn, plan)
        except Exception as e:
            print(f"  エラー: {e}")
            n = 0
        print(f"  完了: {n} 件 ({time.time() - t0:.1f}秒)")


# Tier 1 テーブル一覧（結果サマリー用）
_TIER1_TABLES = [
    "indices_bars_daily_topix",
    "fins_summary",
    "investor_types",
    "markets_margin_interest",
    "markets_margin_alert",
    "markets_short_ratio",
    "markets_breakdown",
    "markets_calendar",
    "response_cache",
]


def main() -> None:
    plan = _load_plan()
    available = _available_endpoints(plan)

    parser = argparse.ArgumentParser(
        description="J-Quants 追加データを取得してキャッシュに投入",
        epilog=f"現在のプラン: {plan}（取得可能: {', '.join(available)}）",
    )
    parser.add_argument("--topix", action="store_true", help="TOPIX 日足を取得 (Light+)")
    parser.add_argument("--fins-summary", action="store_true", help="決算サマリーを取得 (Free+)")
    parser.add_argument("--earnings-cal", action="store_true", help="決算発表予定を取得 (Free+)")
    parser.add_argument(
        "--investor-types", action="store_true", help="投資部門別売買動向を取得 (Light+)"
    )
    parser.add_argument(
        "--short-ratio", action="store_true", help="業種別空売り比率を取得 (Standard+)"
    )
    parser.add_argument(
        "--margin-interest", action="store_true", help="信用取引残高を取得 (Standard+)"
    )
    parser.add_argument(
        "--margin-alert", action="store_true", help="増担保規制情報を取得 (Standard+)"
    )
    parser.add_argument(
        "--short-sale-report", action="store_true", help="空売り残高報告を取得 (Standard+)"
    )
    parser.add_argument("--breakdown", action="store_true", help="売買内訳データを取得 (Premium)")
    parser.add_argument("--calendar", action="store_true", help="取引カレンダーを取得 (Free+)")
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="DAYS",
        help="過去N日分のバックフィル（Markets系 Tier 1 対象）",
    )
    parser.add_argument(
        "--skip-screener-results",
        action="store_true",
        help="末尾の screener_results 事前計算をスキップ",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"キャッシュ DB パス (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    # オプション指定があればそれを使う、なければプランに応じて自動決定
    explicit = {
        "topix": args.topix,
        "fins_summary": args.fins_summary,
        "earnings_cal": args.earnings_cal,
        "investor_types": args.investor_types,
        "short_ratio": args.short_ratio,
        "margin_interest": args.margin_interest,
        "margin_alert": args.margin_alert,
        "short_sale_report": args.short_sale_report,
        "breakdown": args.breakdown,
        "calendar": args.calendar,
    }
    has_explicit = any(explicit.values())

    if has_explicit:
        targets = [ep for ep, selected in explicit.items() if selected]
        # 明示指定でもプラン外なら警告
        for ep in targets:
            if ep not in available:
                min_plan = ENDPOINT_MIN_PLAN[ep]
                print(f"⚠️ {ep} は {min_plan}+ プランが必要です（現在: {plan}）、スキップ")
        targets = [ep for ep in targets if ep in available]
    else:
        targets = available

    print(f"プラン: {plan} | 取得対象: {', '.join(targets)}")
    print(f"キャッシュ DB: {args.db}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)

    cli = jquantsapi.ClientV2()

    # バックフィルモード
    if args.backfill:
        _run_backfill(cli, conn, targets, args.backfill, plan)
    else:
        # 通常の日次取得
        for ep in targets:
            label, func = FETCH_REGISTRY[ep]
            print(f"{label}を取得中...")
            t0 = time.time()
            try:
                n = func(cli, conn, plan)
            except Exception as e:
                print(f"  エラー: {e}")
                n = 0
            print(f"  完了: {n} 件 ({time.time() - t0:.1f}秒)")

    # 最新営業日の screener_results を事前計算（Issue #142）
    # backfill モードでは過去日付向けに populate_history を別途使うので skip
    if not args.backfill and not args.skip_screener_results:
        print("screener_results を事前計算中...")
        t0 = time.time()
        try:
            n = populate_screener_results(conn)
        except Exception as e:
            print(f"  エラー: {e}")
            n = 0
        print(f"  完了: {n} 件 ({time.time() - t0:.1f}秒)")

    # 結果サマリー
    print("--- テーブル件数 ---")
    for table in _TIER1_TABLES + ["screener_results"]:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            print(f"  {table}: {row[0]:,} 行")
        except sqlite3.OperationalError:
            pass

    db_size = args.db.stat().st_size / (1024 * 1024)
    print(f"  DB サイズ: {db_size:.1f} MB")

    conn.close()
    print("取得完了")


if __name__ == "__main__":
    main()

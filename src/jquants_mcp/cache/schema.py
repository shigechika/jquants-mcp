"""Single source of truth for cache table schemas.

This module has ZERO non-stdlib imports so that external scripts
(daily_fetch.py, import_csv_to_cache.py) running in foreign venvs
can import it via sys.path without pulling in the full package.

Public API (stable contract):
    TIER1_TABLES       — dict[str, dict[str, str]] : Tier 1 (row-level) table
                          schemas keyed by table name.
    BULK_TABLES        — dict[str, dict[str, str]] : Bulk-only table schemas.
    RESPONSE_CACHE_DDL — str : DDL for the response-level Tier 2 cache table.
    SCREENER_RESULTS_DDL / SCREENER_RESULTS_INDEX_DDL — DDL for the
                          pre-computed screener result cache.
    generate_ddl(name, schema) — Build CREATE TABLE DDL from a schema dict.
    all_ddl()          — dict[str, str] : {table_name: DDL} for all
                          row-level tables (TIER1_TABLES + BULK_TABLES).
    all_tier1_ddl() / all_bulk_ddl() — convenience subsets of `all_ddl()`.

Compatibility policy:
    * Breaking changes to the symbols above (renames, removals, structural
      changes to TIER1_TABLES / BULK_TABLES entries) require a major bump.
    * Additive changes (new tables, new optional columns) are minor.
    * Downstream consumers (external CSV-import / bulk-fetch tools) import
      these symbols to keep their scripts in sync with the server's schema.
"""

from __future__ import annotations

import logging
import sqlite3

__all__ = [
    "ALL_TABLE_NAMES",
    "BULK_TABLES",
    "CROSS_SECTION_INDEX_DDL",
    "FINS_INDEX_DDL",
    "RESPONSE_CACHE_DDL",
    "SCREENER_RESULTS_DDL",
    "SCREENER_RESULTS_INDEX_DDL",
    "TIER1_KEY_COLUMNS",
    "TIER1_TABLES",
    "all_bulk_ddl",
    "all_ddl",
    "all_tier1_ddl",
    "ensure_cross_section_indexes",
    "generate_ddl",
    "migrate_add_fins_indexes",
    "migrate_drop_plan",
]

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
# Tier 1 tables: row-level cache (date x code granularity)
# Used by the MCP server (store.py) and daily scripts.
# ----------------------------------------------------------------

TIER1_TABLES: dict[str, dict[str, str]] = {
    "equities_bars_daily": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "adj_factor REAL",
    },
    "equities_master": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
    "fins_summary": {
        "key_columns": "code TEXT NOT NULL, disc_date TEXT NOT NULL",
        "primary_key": "code, disc_date",
        "extra_columns": "",
    },
    "indices_bars_daily_topix": {
        "key_columns": "date TEXT NOT NULL",
        "primary_key": "date",
        "extra_columns": "",
    },
    "investor_types": {
        "key_columns": "pub_date TEXT NOT NULL, section TEXT NOT NULL",
        "primary_key": "pub_date, section",
        "extra_columns": "",
    },
    "markets_margin_interest": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
    "markets_margin_alert": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
    "markets_short_ratio": {
        "key_columns": "s33 TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "s33, date",
        "extra_columns": "",
    },
    "markets_breakdown": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
    "markets_calendar": {
        "key_columns": "date TEXT NOT NULL",
        "primary_key": "date",
        "extra_columns": "",
    },
    "equities_earnings_calendar": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
}

# ----------------------------------------------------------------
# Bulk-only tables: used by bulk_fetch_all.py but not by the MCP
# server's row-level cache logic.
# ----------------------------------------------------------------

BULK_TABLES: dict[str, dict[str, str]] = {
    "markets_short_sale_report": {
        "key_columns": ("code TEXT NOT NULL, disc_date TEXT NOT NULL, reporter_name TEXT NOT NULL"),
        "primary_key": "code, disc_date, reporter_name",
        "extra_columns": "",
    },
    "indices_bars_daily": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
    "derivatives_bars_daily_options_225": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
}

# ----------------------------------------------------------------
# Tier 2 table: response-level cache (different structure)
# ----------------------------------------------------------------

RESPONSE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL
)
"""

# ----------------------------------------------------------------
# Screener result cache: pre-computed cross-sectional screener
# outputs keyed by (tool, params, date). Populated by daily_fetch on
# the self-hosted publisher (Cloud Run instances are read-only because
# /tmp is ephemeral). Rolling 52-week retention.
# ----------------------------------------------------------------

SCREENER_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS screener_results (
    tool_name TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    computed_at REAL NOT NULL,
    PRIMARY KEY (tool_name, params_hash, date)
)
"""

SCREENER_RESULTS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_screener_results_date ON screener_results(date)"
)

# Speeds up MAX(date) queries in get_latest_equities_date().
# The composite PK (code, date) requires a full index scan for MAX(date) across
# all codes; this single-column index lets SQLite resolve it in O(log n).
EBD_DATE_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_ebd_date ON equities_bars_daily(date)"

# ----------------------------------------------------------------
# fins_summary FY / dividend indexes (user_version 4)
# ----------------------------------------------------------------
# The cross-sectional financials readers (get_div_ann_map, get_all_latest_fy_fins,
# get_forward_div_ann_map) filter/group on fields inside the JSON ``data`` blob,
# so without these they FULL-SCAN fins_summary and run json_extract per row.
# Two VIRTUAL generated columns precompute the FY-document flags — the index
# materialises the value, and VIRTUAL keeps the table small AND is addable via
# ALTER (a STORED generated column is not). Partial indexes cover only matching
# rows. All four indexes were verified to be used via EXPLAIN QUERY PLAN.
_FINS_IS_FY_DDL = (
    "ALTER TABLE fins_summary ADD COLUMN is_fy INTEGER GENERATED ALWAYS AS ("
    "CASE WHEN json_extract(data, '$.CurPerType') = 'FY'"
    " OR json_extract(data, '$.TypeOfCurrentPeriod') = 'FY'"
    " OR json_extract(data, '$.DocType') LIKE 'FYFinancial%'"
    " OR json_extract(data, '$.TypeOfDocument') LIKE 'FYFinancial%'"
    " THEN 1 ELSE 0 END) VIRTUAL"
)
# Annual-RESULTS only (DocType LIKE 'FYFinancial%'). Distinct from is_fy because
# DividendForecastRevision rows carry CurPerType='FY' but are NOT results filings;
# get_forward_div_ann_map's fy_latest CTE must exclude them.
_FINS_IS_FY_RESULTS_DDL = (
    "ALTER TABLE fins_summary ADD COLUMN is_fy_results INTEGER GENERATED ALWAYS AS ("
    "CASE WHEN json_extract(data, '$.DocType') LIKE 'FYFinancial%'"
    " OR json_extract(data, '$.TypeOfDocument') LIKE 'FYFinancial%'"
    " THEN 1 ELSE 0 END) VIRTUAL"
)
# Each WHERE must MATCH the reader's predicate verbatim (SQLite only uses a
# partial/expression index on an identical expression). The two FY indexes
# require the readers to filter on the generated column (is_fy=1 / is_fy_results=1).
FINS_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_fs_is_fy "
    "ON fins_summary(code, substr(disc_date, 1, 10)) WHERE is_fy = 1",
    "CREATE INDEX IF NOT EXISTS idx_fs_fy_results "
    "ON fins_summary(code, substr(disc_date, 1, 10)) WHERE is_fy_results = 1",
    "CREATE INDEX IF NOT EXISTS idx_fs_divann "
    "ON fins_summary(code, substr(disc_date, 1, 10)) "
    "WHERE json_extract(data, '$.DivAnn') IS NOT NULL AND json_extract(data, '$.DivAnn') != ''",
    "CREATE INDEX IF NOT EXISTS idx_fs_fwddiv "
    "ON fins_summary(code, substr(disc_date, 1, 10)) "
    "WHERE COALESCE(NULLIF(json_extract(data, '$.NxFDivAnn'), ''), "
    "NULLIF(json_extract(data, '$.FDivAnn'), '')) IS NOT NULL",
)

# ----------------------------------------------------------------
# Cross-section date indexes (mirror idx_ebd_date for the other tables)
# ----------------------------------------------------------------
# These tables have a composite PK whose date is the 2nd column, so a date-only
# filter (WHERE date >= cutoff / latest-snapshot scan in markets._get_latest_
# tier1_snapshot) cannot use the PK and full-scans. A single-column date index
# fixes it (and unlocks by-date cross-sections). Plain CREATE INDEX IF NOT
# EXISTS — idempotent, no migration/version needed.
#
# idx_fs_disc_date is the same idea for fins_summary (PK = (code, disc_date)):
# a by-disclosure-date cross-section (get_fins_disclosures_in_range, used by
# get_earnings_results_this_week). disc_date may carry a 19-char timestamp, so
# the index keys substr(disc_date, 1, 10) — the query MUST filter on the same
# expression for the index to be used. code is the 2nd index column so the
# ORDER BY date, code is served without a separate sort.
CROSS_SECTION_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_mmi_date ON markets_margin_interest(date)",
    "CREATE INDEX IF NOT EXISTS idx_mma_date ON markets_margin_alert(date)",
    "CREATE INDEX IF NOT EXISTS idx_msr_date ON markets_short_ratio(date)",
    "CREATE INDEX IF NOT EXISTS idx_mbd_date ON markets_breakdown(date)",
    "CREATE INDEX IF NOT EXISTS idx_eec_date ON equities_earnings_calendar(date)",
    "CREATE INDEX IF NOT EXISTS idx_fs_disc_date ON fins_summary(substr(disc_date, 1, 10), code)",
)


def ensure_cross_section_indexes(conn: sqlite3.Connection) -> None:
    """Create the cross-section date indexes (idempotent; no version bump).

    Shared by store._init_tables, daily_fetch.py and gcs_export_cache.py so the
    indexes ship inside cache.db.zst. Tables that do not exist yet are skipped
    (CREATE INDEX on a missing table would raise).
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for ddl in CROSS_SECTION_INDEX_DDL:
        # "... ON <table>(date)" -> extract <table> to skip if absent.
        table = ddl.split(" ON ", 1)[1].split("(", 1)[0].strip()
        if table in tables:
            conn.execute(ddl)
    conn.commit()


# ----------------------------------------------------------------
# Derived constants
# ----------------------------------------------------------------

TIER1_KEY_COLUMNS: dict[str, frozenset[str]] = {
    table: frozenset(part.strip().split()[0] for part in schema["key_columns"].split(","))
    for table, schema in TIER1_TABLES.items()
}

ALL_TABLE_NAMES: frozenset[str] = frozenset(TIER1_TABLES.keys()) | frozenset(
    ["response_cache", "screener_results"]
)

# ----------------------------------------------------------------
# DDL generation
# ----------------------------------------------------------------


def generate_ddl(table_name: str, schema: dict[str, str]) -> str:
    """Generate CREATE TABLE IF NOT EXISTS DDL from a schema dict."""
    extra = f",\n        {schema['extra_columns']}" if schema.get("extra_columns") else ""
    return (
        f"CREATE TABLE IF NOT EXISTS {table_name} (\n"
        f"        {schema['key_columns']},\n"
        f"        data TEXT NOT NULL,\n"
        f"        fetched_at REAL NOT NULL{extra},\n"
        f"        PRIMARY KEY ({schema['primary_key']})\n"
        f"    )"
    )


def all_tier1_ddl() -> dict[str, str]:
    """Return {table_name: DDL_string} for all Tier 1 tables."""
    return {name: generate_ddl(name, schema) for name, schema in TIER1_TABLES.items()}


def all_bulk_ddl() -> dict[str, str]:
    """Return {table_name: DDL_string} for bulk-only tables."""
    return {name: generate_ddl(name, schema) for name, schema in BULK_TABLES.items()}


def all_ddl() -> dict[str, str]:
    """Return {table_name: DDL_string} for all row-level tables (Tier1 + Bulk)."""
    return {**all_tier1_ddl(), **all_bulk_ddl()}


# ----------------------------------------------------------------
# Migrations (stdlib-only, shared by store.py and daily_fetch.py)
# ----------------------------------------------------------------

# Plan precedence for dedup: keep the highest plan's row when the same natural
# key was stored under several plans (premium > standard > light > free).
_PLAN_RANK_SQL = (
    "CASE plan WHEN 'free' THEN 0 WHEN 'light' THEN 1 "
    "WHEN 'standard' THEN 2 WHEN 'premium' THEN 3 ELSE 0 END"
)


def _rebuild_tier1_without_plan(
    conn: sqlite3.Connection, table_name: str, schema: dict[str, str], col_names: list[str]
) -> tuple[int, int]:
    """Rebuild a Tier 1 table from its declared DDL, dropping the plan column.

    The new table is created from ``TIER1_TABLES`` so it keeps typed columns
    and the PRIMARY KEY (a ``CREATE TABLE ... AS SELECT`` would silently drop
    both). Rows are deduplicated by inserting lowest-plan first so the trailing
    ``INSERT OR REPLACE`` keeps the highest-plan row. Returns (old, new) counts.
    """
    extra = f", {schema['extra_columns']}" if schema["extra_columns"] else ""
    conn.execute(
        f"CREATE TABLE {table_name}_v2 (\n"
        f"    {schema['key_columns']},\n"
        f"    data TEXT NOT NULL,\n"
        f"    fetched_at REAL NOT NULL{extra},\n"
        f"    PRIMARY KEY ({schema['primary_key']})\n"
        f")"
    )
    select_str = ", ".join(c for c in col_names if c != "plan")
    old_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    conn.execute(
        f"INSERT OR REPLACE INTO {table_name}_v2 ({select_str}) "
        f"SELECT {select_str} FROM {table_name} ORDER BY {_PLAN_RANK_SQL} ASC"
    )
    new_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}_v2").fetchone()[0]
    conn.execute(f"DROP TABLE {table_name}")
    conn.execute(f"ALTER TABLE {table_name}_v2 RENAME TO {table_name}")
    return old_count, new_count


def migrate_drop_plan(conn: sqlite3.Connection) -> None:
    """Remove the legacy ``plan`` column from Tier 1 tables and the ``|plan=``
    suffix from Tier 2 ``response_cache`` keys.

    Idempotent (skipped when ``PRAGMA user_version >= 2``) and stdlib-only, so
    the MCP server (``CacheStore``) and ``daily_fetch.py`` — which connects
    directly, bypassing ``CacheStore`` — run the *same* migration instead of a
    hand-copied variant. Tables where ``plan`` is part of the PRIMARY KEY are
    rebuilt from the declared ``TIER1_TABLES`` DDL (preserving column types and
    the PRIMARY KEY) with duplicate rows deduplicated to the highest plan.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return

    has_plan_anywhere = False
    for table_name in TIER1_TABLES:
        cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(c[1] == "plan" for c in cols):
            has_plan_anywhere = True
            break

    has_plan_keys = False
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM response_cache WHERE cache_key LIKE '%|plan=%'"
        ).fetchone()
        has_plan_keys = row[0] > 0
    except sqlite3.OperationalError:
        pass

    if not has_plan_anywhere and not has_plan_keys:
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        return

    logger.info("Migration: removing plan column from Tier 1 tables")

    for table_name, schema in TIER1_TABLES.items():
        cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        col_names = [c[1] for c in cols]
        if "plan" not in col_names:
            continue

        pk_cols = [c[1] for c in cols if c[5] > 0]

        if "plan" in pk_cols:
            old_count, new_count = _rebuild_tier1_without_plan(conn, table_name, schema, col_names)
            logger.info(
                "Migration: rebuilt %s without plan (PK rebuild, %d -> %d rows)",
                table_name,
                old_count,
                new_count,
            )
        else:
            # plan is not in the PK — drop the column in place (SQLite 3.35+),
            # falling back to a full rebuild on older SQLite.
            try:
                conn.execute(f"ALTER TABLE {table_name} DROP COLUMN plan")
                logger.info("Migration: dropped plan column from %s", table_name)
            except sqlite3.OperationalError:
                _rebuild_tier1_without_plan(conn, table_name, schema, col_names)
                logger.info("Migration: rebuilt %s without plan (fallback)", table_name)

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
        logger.info("Migration: stripped plan suffix from response_cache keys")

    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    logger.info("Migration: plan removal complete (user_version=2)")


def migrate_add_fins_indexes(conn: sqlite3.Connection) -> None:
    """Add fins_summary FY/dividend generated columns and partial indexes.

    The cross-sectional financials readers (get_div_ann_map, get_all_latest_fy_fins,
    get_forward_div_ann_map) filter and group on JSON-blob fields, so without
    indexes they full-scan fins_summary with a per-row json_extract. This adds two
    VIRTUAL generated columns (``is_fy``, ``is_fy_results``) and four partial
    indexes that turn those scans into index seeks (verified with EXPLAIN QUERY
    PLAN). The FY readers must filter on the generated columns (``is_fy=1`` /
    ``is_fy_results=1``) to hit the indexes; the dividend indexes match the
    readers' verbatim ``json_extract`` predicates and need no query change.

    Idempotent — skipped at ``user_version >= 4``. Mirrored by store._init_tables,
    daily_fetch.py and gcs_export_cache.py so the publisher build ships the
    indexes inside cache.db.zst. Stdlib-only (mirrors ``migrate_drop_plan``).
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 4:
        return

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "fins_summary" not in tables:
        # Caller has not created the Tier 1 tables yet; nothing to migrate.
        return

    # table_xinfo (not table_info) lists generated columns, so a partial-failure
    # retry at user_version < 4 doesn't re-ALTER an already-present is_fy column.
    cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(fins_summary)")}
    if "is_fy" not in cols:
        conn.execute(_FINS_IS_FY_DDL)
    if "is_fy_results" not in cols:
        conn.execute(_FINS_IS_FY_RESULTS_DDL)
    for ddl in FINS_INDEX_DDL:
        conn.execute(ddl)

    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    logger.info("Migration: added fins_summary FY/dividend indexes (user_version=4)")

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

__all__ = [
    "ALL_TABLE_NAMES",
    "BULK_TABLES",
    "RESPONSE_CACHE_DDL",
    "SCREENER_RESULTS_DDL",
    "SCREENER_RESULTS_INDEX_DDL",
    "TIER1_KEY_COLUMNS",
    "TIER1_TABLES",
    "all_bulk_ddl",
    "all_ddl",
    "all_tier1_ddl",
    "generate_ddl",
]

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

"""Single source of truth for cache table schemas.

This module has ZERO non-stdlib imports so that external scripts
(daily_fetch.py, import_csv_to_cache.py) running in foreign venvs
can import it via sys.path without pulling in the full package.
"""

from __future__ import annotations

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
}

# ----------------------------------------------------------------
# Bulk-only tables: used by bulk_fetch_all.py but not by the MCP
# server's row-level cache logic.
# ----------------------------------------------------------------

BULK_TABLES: dict[str, dict[str, str]] = {
    "markets_short_sale_report": {
        "key_columns": (
            "code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "reporter_name TEXT NOT NULL"
        ),
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
# Derived constants
# ----------------------------------------------------------------

TIER1_KEY_COLUMNS: dict[str, frozenset[str]] = {
    table: frozenset(
        part.strip().split()[0] for part in schema["key_columns"].split(",")
    )
    for table, schema in TIER1_TABLES.items()
}

ALL_TABLE_NAMES: frozenset[str] = (
    frozenset(TIER1_TABLES.keys()) | frozenset(["response_cache"])
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

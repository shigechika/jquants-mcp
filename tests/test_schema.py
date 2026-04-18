"""Tests for cache schema consistency.

Ensures the single-source-of-truth schema (schema.py) is valid,
covers all consumers, and forbids legacy columns like 'plan'.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

from jquants_mcp.cache.schema import (
    ALL_TABLE_NAMES,
    BULK_TABLES,
    RESPONSE_CACHE_DDL,
    TIER1_KEY_COLUMNS,
    TIER1_TABLES,
    all_ddl,
    generate_ddl,
)
from jquants_mcp.cache.store import CacheStore

# daily_fetch.py depends on jquantsapi (not in this venv) — mock it
sys.modules.setdefault("jquantsapi", MagicMock())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from daily_fetch import _DAILY_FETCH_TABLES, _ensure_tables as df_ensure_tables  # noqa: E402
from import_csv_to_cache import _ensure_tables as csv_ensure_tables  # noqa: E402


# ============================================================
# Forbidden column check
# ============================================================

_FORBIDDEN_COLUMNS = {"plan"}


class TestForbiddenColumns:
    """Ensure legacy columns like 'plan' never appear in schema definitions."""

    def test_no_plan_in_tier1_tables(self):
        for name, schema in TIER1_TABLES.items():
            col_names = {part.strip().split()[0] for part in schema["key_columns"].split(",")}
            assert not col_names & _FORBIDDEN_COLUMNS, (
                f"TIER1_TABLES[{name!r}] key_columns contains forbidden column(s): "
                f"{col_names & _FORBIDDEN_COLUMNS}"
            )
            if schema.get("extra_columns"):
                extra_names = {
                    part.strip().split()[0] for part in schema["extra_columns"].split(",")
                }
                assert not extra_names & _FORBIDDEN_COLUMNS, (
                    f"TIER1_TABLES[{name!r}] extra_columns contains forbidden column(s)"
                )

    def test_no_plan_in_bulk_tables(self):
        for name, schema in BULK_TABLES.items():
            col_names = {part.strip().split()[0] for part in schema["key_columns"].split(",")}
            assert not col_names & _FORBIDDEN_COLUMNS, (
                f"BULK_TABLES[{name!r}] key_columns contains forbidden column(s)"
            )

    def test_no_plan_in_generated_ddl(self):
        """Generated DDL must not contain 'plan' column."""
        for name, ddl in all_ddl().items():
            assert "plan " not in ddl.lower(), f"DDL for {name!r} contains 'plan'"

    def test_no_plan_in_actual_tables(self, tmp_path: Path):
        """Tables created by CacheStore must not have a 'plan' column."""
        store = CacheStore(tmp_path / "test.db", default_plan="standard")
        conn = store._ensure_connection()
        assert conn is not None

        for table_name in TIER1_TABLES:
            cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            col_names = {c[1] for c in cols}
            assert "plan" not in col_names, f"Table {table_name!r} has forbidden 'plan' column"
        store.close()


# ============================================================
# DDL validity
# ============================================================


class TestDDLValidity:
    """Ensure all generated DDL is valid SQLite."""

    def test_all_ddl_is_valid_sql(self):
        conn = sqlite3.connect(":memory:")
        for name, ddl in all_ddl().items():
            conn.execute(ddl)
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchall()
            assert len(rows) == 1, f"Table {name!r} was not created"

    def test_response_cache_ddl_is_valid(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(RESPONSE_CACHE_DDL)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='response_cache'"
        ).fetchall()
        assert len(rows) == 1


# ============================================================
# Schema consistency across entry points
# ============================================================


def _get_table_schema(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Return column schema as [(name, type, notnull, pk_index), ...]."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [(r[1], r[2], r[3], r[5]) for r in info]


class TestSchemaConsistency:
    """Tables created by different entry points must have identical schemas."""

    def test_store_matches_schema_py(self, tmp_path: Path):
        """CacheStore creates tables identical to generate_ddl() output."""
        # Reference: create via generate_ddl
        ref_conn = sqlite3.connect(":memory:")
        for name, schema in TIER1_TABLES.items():
            ref_conn.execute(generate_ddl(name, schema))

        # Actual: create via CacheStore
        store = CacheStore(tmp_path / "test.db", default_plan="free")
        store_conn = store._ensure_connection()
        assert store_conn is not None

        for name in TIER1_TABLES:
            expected = _get_table_schema(ref_conn, name)
            actual = _get_table_schema(store_conn, name)
            assert actual == expected, (
                f"Schema mismatch for {name!r}: expected {expected}, got {actual}"
            )
        store.close()

    def test_daily_fetch_matches_schema_py(self, tmp_path: Path):
        """daily_fetch._ensure_tables creates tables identical to schema.py."""
        ref_conn = sqlite3.connect(":memory:")
        for name in _DAILY_FETCH_TABLES:
            ref_conn.execute(generate_ddl(name, TIER1_TABLES[name]))

        test_conn = sqlite3.connect(str(tmp_path / "df_test.db"))
        test_conn.execute("PRAGMA journal_mode=WAL")
        df_ensure_tables(test_conn)

        for name in _DAILY_FETCH_TABLES:
            expected = _get_table_schema(ref_conn, name)
            actual = _get_table_schema(test_conn, name)
            assert actual == expected, f"daily_fetch schema mismatch for {name!r}"
        test_conn.close()

    def test_import_csv_matches_schema_py(self, tmp_path: Path):
        """import_csv._ensure_tables creates tables identical to schema.py."""
        ref_conn = sqlite3.connect(":memory:")
        for name in ("equities_bars_daily", "equities_master"):
            ref_conn.execute(generate_ddl(name, TIER1_TABLES[name]))

        test_conn = sqlite3.connect(str(tmp_path / "csv_test.db"))
        test_conn.execute("PRAGMA journal_mode=WAL")
        csv_ensure_tables(test_conn)

        for name in ("equities_bars_daily", "equities_master"):
            expected = _get_table_schema(ref_conn, name)
            actual = _get_table_schema(test_conn, name)
            assert actual == expected, f"import_csv schema mismatch for {name!r}"
        test_conn.close()


# ============================================================
# Coverage checks
# ============================================================


class TestCoverage:
    """Ensure schema.py covers all consumers."""

    def test_daily_fetch_tables_are_in_tier1(self):
        """All tables used by daily_fetch.py exist in TIER1_TABLES."""
        for t in _DAILY_FETCH_TABLES:
            assert t in TIER1_TABLES, f"{t!r} not found in TIER1_TABLES"

    def test_bulk_ddl_covers_all_endpoints(self):
        """all_ddl() covers every table referenced by bulk_fetch_all.py."""
        from bulk_fetch_all import ENDPOINTS

        ddl_keys = set(all_ddl().keys())
        for ep_info in ENDPOINTS.values():
            table = ep_info["table"]
            assert table in ddl_keys, f"Missing DDL for bulk table {table!r}"

    def test_tier1_key_columns_consistent(self):
        """TIER1_KEY_COLUMNS matches TIER1_TABLES."""
        for table, expected_keys in TIER1_KEY_COLUMNS.items():
            actual_keys = frozenset(
                part.strip().split()[0] for part in TIER1_TABLES[table]["key_columns"].split(",")
            )
            assert actual_keys == expected_keys

    def test_all_table_names_complete(self):
        """ALL_TABLE_NAMES includes all Tier 1 tables + response_cache."""
        expected = frozenset(TIER1_TABLES.keys()) | {"response_cache"}
        assert ALL_TABLE_NAMES == expected

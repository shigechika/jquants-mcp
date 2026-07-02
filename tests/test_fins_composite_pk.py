"""Tests for the fins_summary composite primary key (issue #473).

The PK became ``(code, disc_date, doc_type)`` so a company's same-day
FYFinancialStatements and DividendForecastRevision filings coexist
instead of one overwriting the other.
"""

from __future__ import annotations

import sqlite3

from jquants_mcp.cache.schema import TIER1_TABLES, migrate_split_fins_pk
from jquants_mcp.cache.store import CacheStore

FY_ROW = {
    "Code": "94320",
    "DiscDate": "2024-05-10",
    "DocType": "FYFinancialStatements_Consolidated_IFRS",
    "CurPerType": "FY",
    "EPS": "15.09",
    "DivAnn": "5.1",
    "Sales": "1000000",
}
REVISION_ROW = {
    "Code": "94320",
    "DiscDate": "2024-05-10",
    "DocType": "DividendForecastRevision",
    "CurPerType": "FY",
    "FDivAnn": "5.1",
}


def _pk_cols(conn: sqlite3.Connection) -> list[str]:
    return [c[1] for c in conn.execute("PRAGMA table_info(fins_summary)") if c[5] > 0]


class TestSchema:
    def test_pk_includes_doc_type(self):
        assert TIER1_TABLES["fins_summary"]["primary_key"] == "code, disc_date, doc_type"

    def test_fresh_db_has_doc_type_in_pk(self, cache_store: CacheStore):
        conn = cache_store._ensure_connection()
        assert "doc_type" in _pk_cols(conn)


class TestPutRows:
    def test_same_day_multi_doc_coexist(self, cache_store: CacheStore):
        """本決算 + 予想修正 の同日開示が両方残る（issue #473 の核心）。"""
        n = cache_store.put_rows(
            "fins_summary", [FY_ROW, REVISION_ROW], key_columns=["Code", "DiscDate"]
        )
        assert n == 2
        conn = cache_store._ensure_connection()
        rows = conn.execute(
            "SELECT doc_type FROM fins_summary WHERE code='94320' "
            "AND disc_date='2024-05-10' ORDER BY doc_type"
        ).fetchall()
        assert [r[0] for r in rows] == [
            "DividendForecastRevision",
            "FYFinancialStatements_Consolidated_IFRS",
        ]

    def test_doc_type_from_legacy_field_and_reit_not_mangled(self, cache_store: CacheStore):
        """TypeOfDocument からも取得でき、'T' を含む REIT doc_type が壊れない。"""
        reit = {
            "Code": "89510",
            "DiscDate": "2024-05-10",
            "TypeOfDocument": "REITFinancialStatements_Consolidated_JP",
            "CurPerType": "FY",
        }
        cache_store.put_rows("fins_summary", [reit], key_columns=["Code", "DiscDate"])
        conn = cache_store._ensure_connection()
        dt = conn.execute("SELECT doc_type FROM fins_summary WHERE code='89510'").fetchone()[0]
        assert dt == "REITFinancialStatements_Consolidated_JP"

    def test_get_latest_fins_row_prefers_statements(self, cache_store: CacheStore):
        """同日に本決算と修正が並ぶとき、実績のある本決算を返す。"""
        cache_store.put_rows(
            "fins_summary", [REVISION_ROW, FY_ROW], key_columns=["Code", "DiscDate"]
        )
        row = cache_store.get_latest_fins_row("94320")
        assert row is not None
        assert row["DocType"] == "FYFinancialStatements_Consolidated_IFRS"
        assert row.get("EPS") == "15.09"  # revision には無い実績


class TestMigration:
    def _old_style_db(self, path):
        """Build a pre-#473 DB: 2-column PK, user_version 4, two rows."""
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE fins_summary ("
            "code TEXT NOT NULL, disc_date TEXT NOT NULL, "
            "data TEXT NOT NULL, fetched_at REAL NOT NULL, "
            "PRIMARY KEY (code, disc_date))"
        )
        import json

        conn.execute(
            "INSERT INTO fins_summary VALUES (?, ?, ?, ?)",
            ("94320", "2024-05-10", json.dumps(FY_ROW), 0.0),
        )
        conn.execute(
            "INSERT INTO fins_summary VALUES (?, ?, ?, ?)",
            (
                "72030",
                "2024-02-08",
                json.dumps(
                    {
                        "Code": "72030",
                        "DiscDate": "2024-02-08",
                        "DocType": "3QFinancialStatements_Consolidated_IFRS",
                    }
                ),
                0.0,
            ),
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        return conn

    def test_migration_rebuilds_pk_and_preserves_rows(self, tmp_path):
        conn = self._old_style_db(tmp_path / "old.db")
        assert "doc_type" not in _pk_cols(conn)

        migrate_split_fins_pk(conn)

        assert "doc_type" in _pk_cols(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        # rows preserved and doc_type extracted from the blob
        rows = dict(
            conn.execute("SELECT code, doc_type FROM fins_summary ORDER BY code").fetchall()
        )
        assert rows["94320"] == "FYFinancialStatements_Consolidated_IFRS"
        assert rows["72030"] == "3QFinancialStatements_Consolidated_IFRS"
        # generated columns re-created
        cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(fins_summary)")}
        assert {"is_fy", "is_fy_results"} <= cols
        conn.close()

    def test_migration_idempotent(self, tmp_path):
        conn = self._old_style_db(tmp_path / "old.db")
        migrate_split_fins_pk(conn)
        before = conn.execute("SELECT COUNT(*) FROM fins_summary").fetchone()[0]
        migrate_split_fins_pk(conn)  # no-op second run
        after = conn.execute("SELECT COUNT(*) FROM fins_summary").fetchone()[0]
        assert before == after == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        conn.close()

    def test_migration_allows_same_day_after_rebuild(self, tmp_path):
        """After migration, a second same-day doc_type can be inserted."""
        conn = self._old_style_db(tmp_path / "old.db")
        migrate_split_fins_pk(conn)
        import json

        conn.execute(
            "INSERT INTO fins_summary (code, disc_date, doc_type, data, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("94320", "2024-05-10", "DividendForecastRevision", json.dumps(REVISION_ROW), 0.0),
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM fins_summary WHERE code='94320' AND disc_date='2024-05-10'"
        ).fetchone()[0]
        assert n == 2  # 本決算 + 修正が共存
        conn.close()

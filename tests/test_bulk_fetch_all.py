"""Tests for scripts/bulk_fetch_all.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from bulk_fetch_all import ENDPOINTS, BulkFetcher  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fetcher(tmp_path: Path) -> BulkFetcher:
    """Return a BulkFetcher pointed at a temp DB (no real API calls)."""
    from jquants_mcp.config import Settings

    settings = Settings()
    settings.jquants_api_key = "dummy"
    return BulkFetcher(settings, tmp_path / "cache.db", dry_run=False)


def _conn_with_table(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE equities_bars_daily "
        "(code TEXT NOT NULL, date TEXT NOT NULL, adj_factor REAL, "
        "data TEXT, fetched_at REAL, PRIMARY KEY (code, date))"
    )
    return conn


# ---------------------------------------------------------------------------
# Tests for _import_csv_text with numeric_columns
# ---------------------------------------------------------------------------


class TestImportCsvTextNumericColumns:
    def test_adj_factor_stored_as_real(self, tmp_path):
        fetcher = _make_fetcher(tmp_path)
        conn = _conn_with_table(tmp_path)
        csv_text = "Code,Date,AdjFactor,C\n72030,2026-05-01,1.0,1500.0\n"
        fetcher._import_csv_text(
            csv_text,
            conn,
            "equities_bars_daily",
            [("Code", "code"), ("Date", "date")],
            numeric_columns=[("AdjFactor", "adj_factor")],
        )
        row = conn.execute("SELECT adj_factor FROM equities_bars_daily").fetchone()
        assert row is not None
        assert isinstance(row["adj_factor"], float)
        assert abs(row["adj_factor"] - 1.0) < 1e-9

    def test_empty_adj_factor_stored_as_null(self, tmp_path):
        fetcher = _make_fetcher(tmp_path)
        conn = _conn_with_table(tmp_path)
        csv_text = "Code,Date,AdjFactor,C\n72030,2026-05-01,,1500.0\n"
        fetcher._import_csv_text(
            csv_text,
            conn,
            "equities_bars_daily",
            [("Code", "code"), ("Date", "date")],
            numeric_columns=[("AdjFactor", "adj_factor")],
        )
        row = conn.execute("SELECT adj_factor FROM equities_bars_daily").fetchone()
        assert row is not None
        assert row["adj_factor"] is None

    def test_data_json_still_populated(self, tmp_path):
        fetcher = _make_fetcher(tmp_path)
        conn = _conn_with_table(tmp_path)
        csv_text = "Code,Date,AdjFactor,C\n72030,2026-05-01,2.0,3000.0\n"
        fetcher._import_csv_text(
            csv_text,
            conn,
            "equities_bars_daily",
            [("Code", "code"), ("Date", "date")],
            numeric_columns=[("AdjFactor", "adj_factor")],
        )
        row = conn.execute("SELECT data FROM equities_bars_daily").fetchone()
        data = json.loads(row["data"])
        assert data["AdjFactor"] == 2.0
        assert data["C"] == 3000.0

    def test_no_numeric_columns_unchanged_behaviour(self, tmp_path):
        fetcher = _make_fetcher(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.execute(
            "CREATE TABLE t (code TEXT, date TEXT, data TEXT, fetched_at REAL, PRIMARY KEY (code, date))"
        )
        csv_text = "Code,Date,Val\n99840,2026-05-01,100\n"
        fetcher._import_csv_text(csv_text, conn, "t", [("Code", "code"), ("Date", "date")])
        row = conn.execute("SELECT code, date FROM t").fetchone()
        assert row[0] == "99840"
        assert row[1] == "2026-05-01"


# ---------------------------------------------------------------------------
# Tests for ENDPOINTS config
# ---------------------------------------------------------------------------


class TestEndpointsConfig:
    def test_equities_bars_daily_present(self):
        assert "equities_bars_daily" in ENDPOINTS

    def test_equities_bars_daily_api_path(self):
        assert ENDPOINTS["equities_bars_daily"]["api_path"] == "/equities/bars/daily"

    def test_equities_bars_daily_has_adj_factor_numeric_column(self):
        nc = ENDPOINTS["equities_bars_daily"].get("numeric_columns", [])
        assert ("AdjFactor", "adj_factor") in nc

    def test_all_endpoints_have_required_keys(self):
        for name, cfg in ENDPOINTS.items():
            assert "api_path" in cfg, f"{name} missing api_path"
            assert "table" in cfg, f"{name} missing table"
            assert "csv_key_map" in cfg, f"{name} missing csv_key_map"

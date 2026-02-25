"""Tests for scripts/import_csv_to_cache.py."""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

# スクリプトの関数を直接インポートできないため、sys.path で追加
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_csv_to_cache import (
    _ensure_tables,
    import_market_history,
    import_market_history_incremental,
    import_tickers,
)


@pytest.fixture()
def db_conn(tmp_path: Path):
    """テスト用 SQLite 接続を提供する。"""
    db_path = tmp_path / "test_cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)
    yield conn
    conn.close()


def _write_market_csv(path: Path, rows: list[dict]) -> Path:
    """テスト用の株価 CSV を書き出す。"""
    csv_path = path / "market.csv"
    headers = ["Date", "Code", "O", "H", "L", "C", "Vo", "Va", "AdjFactor", "AdjC"]
    lines = [",".join(headers)]
    for r in rows:
        line = ",".join(str(r.get(h, "")) for h in headers)
        lines.append(line)
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path


def _write_tickers_csv(path: Path, rows: list[dict]) -> Path:
    """テスト用の銘柄マスタ CSV を書き出す。"""
    csv_path = path / "tickers.csv"
    headers = ["Date", "Code", "CoName", "S33", "Mkt"]
    lines = [",".join(headers)]
    for r in rows:
        line = ",".join(str(r.get(h, "")) for h in headers)
        lines.append(line)
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _get_all_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(f"SELECT * FROM {table} ORDER BY code, date").fetchall()


# ============================================================
# 全件インポート
# ============================================================


class TestImportMarketHistory:
    """import_market_history のテスト。"""

    def test_basic_import(self, db_conn, tmp_path):
        csv = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
            {"Date": "2026-02-25", "Code": "72030", "O": 2850, "H": 2950, "L": 2800, "C": 2900, "Vo": 1200, "Va": 3480000, "AdjFactor": 1.0, "AdjC": 2900},
        ])
        n = import_market_history(db_conn, csv)
        assert n == 2
        assert _count_rows(db_conn, "equities_bars_daily") == 2

    def test_upsert(self, db_conn, tmp_path):
        """同じ code+date で再インポートすると上書きされること。"""
        csv1 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
        ])
        import_market_history(db_conn, csv1)

        # 終値が変わった CSV で再インポート
        csv2 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 9999, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 9999},
        ])
        import_market_history(db_conn, csv2)

        assert _count_rows(db_conn, "equities_bars_daily") == 1
        import json
        row = db_conn.execute("SELECT data FROM equities_bars_daily").fetchone()
        data = json.loads(row[0])
        assert data["C"] == 9999


class TestImportTickers:
    """import_tickers のテスト。"""

    def test_basic_import(self, db_conn, tmp_path):
        csv = _write_tickers_csv(tmp_path, [
            {"Date": "2026-02-25", "Code": "72030", "CoName": "トヨタ自動車", "S33": "3050", "Mkt": "0111"},
            {"Date": "2026-02-25", "Code": "99830", "CoName": "ソフトバンクG", "S33": "3650", "Mkt": "0111"},
        ])
        n = import_tickers(db_conn, csv)
        assert n == 2
        assert _count_rows(db_conn, "equities_master") == 2


# ============================================================
# 差分インポート
# ============================================================


class TestIncrementalImport:
    """import_market_history_incremental のテスト。"""

    def test_empty_cache_falls_back_to_full(self, db_conn, tmp_path):
        """キャッシュが空の場合は全件インポートにフォールバック。"""
        csv = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
            {"Date": "2026-02-25", "Code": "72030", "O": 2850, "H": 2950, "L": 2800, "C": 2900, "Vo": 1200, "Va": 3480000, "AdjFactor": 1.0, "AdjC": 2900},
        ])
        n, splits = import_market_history_incremental(db_conn, csv)
        assert n == 2
        assert splits == []
        assert _count_rows(db_conn, "equities_bars_daily") == 2

    def test_no_new_data(self, db_conn, tmp_path):
        """新しいデータがない場合は 0行。"""
        csv = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
        ])
        # まず全件インポート
        import_market_history(db_conn, csv)
        assert _count_rows(db_conn, "equities_bars_daily") == 1

        # 同じ CSV で差分インポート → 0行
        n, splits = import_market_history_incremental(db_conn, csv)
        assert n == 0
        assert splits == []

    def test_append_new_rows(self, db_conn, tmp_path):
        """新しい日付の行だけが追加されること。"""
        # 初期データ
        csv1 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
            {"Date": "2026-02-24", "Code": "99830", "O": 8000, "H": 8100, "L": 7900, "C": 8050, "Vo": 5000, "Va": 40250000, "AdjFactor": 1.0, "AdjC": 8050},
        ])
        import_market_history(db_conn, csv1)
        assert _count_rows(db_conn, "equities_bars_daily") == 2

        # 翌日分を追加した CSV
        csv2 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
            {"Date": "2026-02-24", "Code": "99830", "O": 8000, "H": 8100, "L": 7900, "C": 8050, "Vo": 5000, "Va": 40250000, "AdjFactor": 1.0, "AdjC": 8050},
            {"Date": "2026-02-25", "Code": "72030", "O": 2850, "H": 2950, "L": 2800, "C": 2900, "Vo": 1200, "Va": 3480000, "AdjFactor": 1.0, "AdjC": 2900},
            {"Date": "2026-02-25", "Code": "99830", "O": 8050, "H": 8200, "L": 8000, "C": 8150, "Vo": 6000, "Va": 48900000, "AdjFactor": 1.0, "AdjC": 8150},
        ])
        n, splits = import_market_history_incremental(db_conn, csv2)
        assert n == 2  # 2/25 の2行だけ
        assert splits == []
        assert _count_rows(db_conn, "equities_bars_daily") == 4

    def test_stock_split_detection(self, db_conn, tmp_path):
        """株式分割を検知し、該当コードの全行が再インポートされること。"""
        # 初期データ: 72030 と 99830 の2銘柄
        csv1 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
            {"Date": "2026-02-24", "Code": "99830", "O": 8000, "H": 8100, "L": 7900, "C": 8050, "Vo": 5000, "Va": 40250000, "AdjFactor": 1.0, "AdjC": 8050},
        ])
        import_market_history(db_conn, csv1)
        assert _count_rows(db_conn, "equities_bars_daily") == 2

        # 72030 が 10:1 株式分割。過去の AdjC も更新される。
        csv2 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 285},
            {"Date": "2026-02-24", "Code": "99830", "O": 8000, "H": 8100, "L": 7900, "C": 8050, "Vo": 5000, "Va": 40250000, "AdjFactor": 1.0, "AdjC": 8050},
            {"Date": "2026-02-25", "Code": "72030", "O": 285, "H": 295, "L": 280, "C": 290, "Vo": 10000, "Va": 2900000, "AdjFactor": 0.1, "AdjC": 290},
            {"Date": "2026-02-25", "Code": "99830", "O": 8050, "H": 8200, "L": 8000, "C": 8150, "Vo": 6000, "Va": 48900000, "AdjFactor": 1.0, "AdjC": 8150},
        ])
        n, splits = import_market_history_incremental(db_conn, csv2)

        # 72030 が分割検知される
        assert splits == ["72030"]
        # 72030: 過去1行再インポート + 新規1行 = 2行、99830: 新規1行 = 1行 → 合計3行
        assert n == 3
        assert _count_rows(db_conn, "equities_bars_daily") == 4

        # 72030 の過去データが更新されていること（AdjC: 2850 → 285）
        import json
        row = db_conn.execute(
            "SELECT data FROM equities_bars_daily WHERE code='72030' AND date='2026-02-24'"
        ).fetchone()
        data = json.loads(row[0])
        assert data["AdjC"] == 285  # 分割後の調整済み値

    def test_split_does_not_affect_other_codes(self, db_conn, tmp_path):
        """分割検知は該当コードのみ再インポートし、他のコードに影響しないこと。"""
        csv1 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 2850},
            {"Date": "2026-02-24", "Code": "99830", "O": 8000, "H": 8100, "L": 7900, "C": 8050, "Vo": 5000, "Va": 40250000, "AdjFactor": 1.0, "AdjC": 8050},
        ])
        import_market_history(db_conn, csv1)

        # 99830 の AdjC を確認用に取得
        import json
        row_before = db_conn.execute(
            "SELECT data, fetched_at FROM equities_bars_daily WHERE code='99830'"
        ).fetchone()
        fetched_before = row_before[1]

        # 72030 のみ分割
        csv2 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "72030", "O": 2800, "H": 2900, "L": 2750, "C": 2850, "Vo": 1000, "Va": 2850000, "AdjFactor": 1.0, "AdjC": 285},
            {"Date": "2026-02-24", "Code": "99830", "O": 8000, "H": 8100, "L": 7900, "C": 8050, "Vo": 5000, "Va": 40250000, "AdjFactor": 1.0, "AdjC": 8050},
            {"Date": "2026-02-25", "Code": "72030", "O": 285, "H": 295, "L": 280, "C": 290, "Vo": 10000, "Va": 2900000, "AdjFactor": 0.1, "AdjC": 290},
            {"Date": "2026-02-25", "Code": "99830", "O": 8050, "H": 8200, "L": 8000, "C": 8150, "Vo": 6000, "Va": 48900000, "AdjFactor": 1.0, "AdjC": 8150},
        ])
        import_market_history_incremental(db_conn, csv2)

        # 99830 の過去データは fetched_at が変わっていない（再インポートされていない）
        row_after = db_conn.execute(
            "SELECT data, fetched_at FROM equities_bars_daily WHERE code='99830' AND date='2026-02-24'"
        ).fetchone()
        assert row_after[1] == fetched_before
        data = json.loads(row_after[0])
        assert data["AdjC"] == 8050  # 変更なし

    def test_multiple_splits(self, db_conn, tmp_path):
        """同日に複数銘柄が分割した場合。"""
        csv1 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "11110", "O": 1000, "H": 1100, "L": 950, "C": 1050, "Vo": 500, "Va": 525000, "AdjFactor": 1.0, "AdjC": 1050},
            {"Date": "2026-02-24", "Code": "22220", "O": 5000, "H": 5200, "L": 4900, "C": 5100, "Vo": 300, "Va": 1530000, "AdjFactor": 1.0, "AdjC": 5100},
            {"Date": "2026-02-24", "Code": "33330", "O": 3000, "H": 3100, "L": 2900, "C": 3050, "Vo": 800, "Va": 2440000, "AdjFactor": 1.0, "AdjC": 3050},
        ])
        import_market_history(db_conn, csv1)

        # 11110: 5:1 分割、22220: 2:1 分割、33330: 分割なし
        csv2 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "11110", "O": 1000, "H": 1100, "L": 950, "C": 1050, "Vo": 500, "Va": 525000, "AdjFactor": 1.0, "AdjC": 210},
            {"Date": "2026-02-24", "Code": "22220", "O": 5000, "H": 5200, "L": 4900, "C": 5100, "Vo": 300, "Va": 1530000, "AdjFactor": 1.0, "AdjC": 2550},
            {"Date": "2026-02-24", "Code": "33330", "O": 3000, "H": 3100, "L": 2900, "C": 3050, "Vo": 800, "Va": 2440000, "AdjFactor": 1.0, "AdjC": 3050},
            {"Date": "2026-02-25", "Code": "11110", "O": 210, "H": 220, "L": 205, "C": 215, "Vo": 2500, "Va": 537500, "AdjFactor": 0.2, "AdjC": 215},
            {"Date": "2026-02-25", "Code": "22220", "O": 2550, "H": 2600, "L": 2500, "C": 2580, "Vo": 600, "Va": 1548000, "AdjFactor": 0.5, "AdjC": 2580},
            {"Date": "2026-02-25", "Code": "33330", "O": 3050, "H": 3150, "L": 3000, "C": 3100, "Vo": 900, "Va": 2790000, "AdjFactor": 1.0, "AdjC": 3100},
        ])
        n, splits = import_market_history_incremental(db_conn, csv2)

        assert sorted(splits) == ["11110", "22220"]
        assert _count_rows(db_conn, "equities_bars_daily") == 6

    def test_merger_reverse_split(self, db_conn, tmp_path):
        """株式併合（AdjFactor > 1.0）も検知されること。"""
        csv1 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "44440", "O": 100, "H": 110, "L": 95, "C": 105, "Vo": 50000, "Va": 5250000, "AdjFactor": 1.0, "AdjC": 105},
        ])
        import_market_history(db_conn, csv1)

        # 1:10 併合 → AdjFactor = 10.0
        csv2 = _write_market_csv(tmp_path, [
            {"Date": "2026-02-24", "Code": "44440", "O": 100, "H": 110, "L": 95, "C": 105, "Vo": 50000, "Va": 5250000, "AdjFactor": 1.0, "AdjC": 1050},
            {"Date": "2026-02-25", "Code": "44440", "O": 1050, "H": 1100, "L": 1000, "C": 1080, "Vo": 5000, "Va": 5400000, "AdjFactor": 10.0, "AdjC": 1080},
        ])
        n, splits = import_market_history_incremental(db_conn, csv2)
        assert splits == ["44440"]
        assert _count_rows(db_conn, "equities_bars_daily") == 2

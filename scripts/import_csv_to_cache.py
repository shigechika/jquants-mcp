"""Import CSV data into jquants-dat-mcp SQLite cache.

ローカルの CSV ファイルをキャッシュ DB にバルクインポートする。
既存データは INSERT OR REPLACE で上書きされる。

Usage:
    # 全件インポート（初回）
    uv run python scripts/import_csv_to_cache.py \
        --market-history /path/to/jpx-market-history.csv \
        --tickers /path/to/jpx-tickers.csv

    # 差分インポート（日次運用）
    uv run python scripts/import_csv_to_cache.py \
        --market-history /path/to/jpx-market-history.csv \
        --tickers /path/to/jpx-tickers.csv \
        --incremental
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

# schema.py は stdlib のみ依存 — 外部 venv でもインポート可能
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_mcp.cache.schema import TIER1_TABLES, generate_ddl  # noqa: E402

# キャッシュ DB のデフォルトパス
DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-dat-mcp" / "cache.db"

# バッチサイズ（メモリ使用量と速度のバランス）
BATCH_SIZE = 10_000


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create cache tables if they do not exist."""
    for name in ("equities_bars_daily", "equities_master"):
        conn.execute(generate_ddl(name, TIER1_TABLES[name]))
    conn.commit()
    _migrate_drop_plan(conn)


def _migrate_drop_plan(conn: sqlite3.Connection) -> None:
    """Remove plan column from Tier 1 tables if present.

    Mirrors the same migration in store.py._migrate_drop_plan().
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 2:
        return

    _TABLES = {
        "equities_bars_daily": "code, date",
        "equities_master": "code, date",
    }

    migrated = False
    for table, pk_cols in _TABLES.items():
        cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_names = [c[1] for c in cols_info]
        if "plan" not in col_names:
            continue

        pk_positions = [c[1] for c in cols_info if c[5] > 0]
        select_cols = [c for c in col_names if c != "plan"]
        select_str = ", ".join(select_cols)

        if "plan" in pk_positions:
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

    if migrated:
        print("  マイグレーション: plan カラムを除去しました")

    conn.execute("PRAGMA user_version = 2")
    conn.commit()


def _convert_numeric(value: str) -> int | float | str:
    """数値文字列を適切な型に変換する。"""
    if not value:
        return value
    try:
        # 整数を試す
        iv = int(value)
        return iv
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _make_row_tuple(
    row: dict,
    now: float,
) -> tuple[str, str, str, float, float | None]:
    """Convert a CSV row to an insert tuple."""
    data = {k: _convert_numeric(v) for k, v in row.items()}
    data_json = json.dumps(data, ensure_ascii=False)
    adj_factor = float(row["AdjFactor"]) if row.get("AdjFactor") else None
    return (row["Code"], row["Date"], data_json, now, adj_factor)


def _insert_batch(conn: sqlite3.Connection, batch: list) -> None:
    """Insert a batch of rows into equities_bars_daily."""
    conn.executemany(
        "INSERT OR REPLACE INTO equities_bars_daily "
        "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()


def import_market_history(conn: sqlite3.Connection, csv_path: Path, plan: str = "") -> int:
    """Import all rows from a market-history CSV."""
    now = time.time()
    count = 0
    batch: list[tuple[str, str, str, float, float | None]] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            batch.append(_make_row_tuple(row, now))
            count += 1

            if len(batch) >= BATCH_SIZE:
                _insert_batch(conn, batch)
                print(f"  equities_bars_daily: {count:,} 行処理済み", flush=True)
                batch.clear()

    if batch:
        _insert_batch(conn, batch)

    return count


def import_market_history_incremental(
    conn: sqlite3.Connection,
    csv_path: Path,
    plan: str = "",
) -> tuple[int, list[str]]:
    """Incrementally import a market-history CSV.

    Normal day: INSERT only rows newer than the latest cached date (~4,000 rows).
    Split day: detect codes with AdjFactor != 1.0, DELETE + re-INSERT all rows
    for those codes (adjusted values change for the entire history).

    Returns:
        (number of imported rows, list of split-detected codes)
    """
    # キャッシュの最新日を取得
    row = conn.execute("SELECT MAX(date) FROM equities_bars_daily").fetchone()
    max_date = row[0] if row and row[0] else None

    if max_date is None:
        print("  キャッシュが空のため全件インポートに切り替え")
        count = import_market_history(conn, csv_path)
        return count, []

    print(f"  キャッシュ最新日: {max_date}")
    now = time.time()

    # Phase 1: CSV を1パスで読み、新しい行を収集 + 分割検知
    new_rows: list[tuple[str, str, str, float, float | None]] = []
    split_codes: set[str] = set()

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_data in reader:
            if row_data["Date"] <= max_date:
                continue

            tup = _make_row_tuple(row_data, now)
            new_rows.append(tup)

            # AdjFactor != 1.0 → 株式分割・併合
            adj_factor = tup[4]
            if adj_factor is not None and abs(adj_factor - 1.0) > 1e-10:
                split_codes.add(row_data["Code"])

    if not new_rows and not split_codes:
        print("  新しいデータなし")
        return 0, []

    print(f"  新規: {len(new_rows):,} 行")

    # Phase 2: 株式分割コードの全件再取得
    split_reimport = 0
    if split_codes:
        codes_str = ", ".join(sorted(split_codes))
        print(f"  株式分割検知: {codes_str}")

        for code in split_codes:
            deleted = conn.execute(
                "DELETE FROM equities_bars_daily WHERE code = ?",
                (code,),
            ).rowcount
            print(f"    {code}: キャッシュ {deleted:,} 行削除")
        conn.commit()

        # CSV を再度読み、該当コードの過去データを収集
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            split_batch: list[tuple[str, str, str, float, float | None]] = []
            for row_data in reader:
                if row_data["Code"] not in split_codes:
                    continue
                # 新規行は既に new_rows に含まれているのでスキップ
                if row_data["Date"] > max_date:
                    continue
                split_batch.append(_make_row_tuple(row_data, now))

            if split_batch:
                _insert_batch(conn, split_batch)
                split_reimport = len(split_batch)
                print(f"  分割コード再インポート: {split_reimport:,} 行")

    # Phase 3: 新規行をインサート
    if new_rows:
        _insert_batch(conn, new_rows)

    total = len(new_rows) + split_reimport
    return total, sorted(split_codes)


def import_tickers(conn: sqlite3.Connection, csv_path: Path, plan: str = "") -> int:
    """Import ticker-master CSV."""
    now = time.time()
    count = 0
    batch: list[tuple[str, str, str, float]] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row["Date"]
            code = row["Code"]

            data = {k: _convert_numeric(v) for k, v in row.items()}
            data_json = json.dumps(data, ensure_ascii=False)

            batch.append((code, date, data_json, now))
            count += 1

    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO equities_master "
            "(code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            batch,
        )
        conn.commit()

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="CSV データをキャッシュ DB にインポート")
    parser.add_argument("--market-history", type=Path, help="株価四本値 CSV ファイルパス")
    parser.add_argument("--tickers", type=Path, help="銘柄マスタ CSV ファイルパス")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"キャッシュ DB パス (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="差分インポート（新しい日付のみ。株式分割検知時は該当コードを全件再取得）",
    )
    args = parser.parse_args()

    if not args.market_history and not args.tickers:
        parser.error("--market-history または --tickers のいずれかを指定してください")

    mode = "差分" if args.incremental else "全件"
    print(f"キャッシュ DB: {args.db} ({mode}インポート)")
    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)

    if args.market_history:
        print(f"株価四本値をインポート中: {args.market_history}")
        t0 = time.time()
        if args.incremental:
            n, splits = import_market_history_incremental(conn, args.market_history)
            if splits:
                print(f"  株式分割対応済み: {', '.join(splits)}")
        else:
            n = import_market_history(conn, args.market_history)
        elapsed = time.time() - t0
        print(f"  完了: {n:,} 行 ({elapsed:.1f}秒)")

    if args.tickers:
        # 銘柄マスタは少量（~4,000行）なので常に全件インポート
        print(f"銘柄マスタをインポート中: {args.tickers}")
        t0 = time.time()
        n = import_tickers(conn, args.tickers)
        elapsed = time.time() - t0
        print(f"  完了: {n:,} 行 ({elapsed:.1f}秒)")

    # 結果確認
    for table in ["equities_bars_daily", "equities_master"]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {table}: {row[0]:,} 行")

    db_size = args.db.stat().st_size / (1024 * 1024)
    print(f"  DB サイズ: {db_size:.1f} MB")

    conn.close()
    print("インポート完了")


if __name__ == "__main__":
    main()

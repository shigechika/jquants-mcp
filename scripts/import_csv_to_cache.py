"""Import CSV data into jquants-dat-mcp SQLite cache.

ローカルの CSV ファイルをキャッシュ DB にバルクインポートする。
既存データは INSERT OR REPLACE で上書きされる。

Usage:
    uv run python scripts/import_csv_to_cache.py \
        --market-history /path/to/jpx-market-history.csv \
        --tickers /path/to/jpx-tickers.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from pathlib import Path

# キャッシュ DB のデフォルトパス
DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-dat-mcp" / "cache.db"

# バッチサイズ（メモリ使用量と速度のバランス）
BATCH_SIZE = 10_000


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """キャッシュテーブルが存在しない場合は作成する。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equities_bars_daily (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            adj_factor REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equities_master (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        )
    """)
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


def import_market_history(conn: sqlite3.Connection, csv_path: Path) -> int:
    """株価四本値 CSV をインポートする。"""
    now = time.time()
    count = 0
    batch: list[tuple[str, str, str, float, float | None]] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 日付フォーマット統一（YYYY-MM-DD）
            date = row["Date"]
            code = row["Code"]

            # 全カラムを適切な型に変換して JSON 化
            data = {k: _convert_numeric(v) for k, v in row.items()}
            data_json = json.dumps(data, ensure_ascii=False)

            adj_factor = float(row["AdjFactor"]) if row.get("AdjFactor") else None

            batch.append((code, date, data_json, now, adj_factor))
            count += 1

            if len(batch) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT OR REPLACE INTO equities_bars_daily "
                    "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                print(f"  equities_bars_daily: {count:,} 行処理済み", flush=True)
                batch.clear()

    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            batch,
        )
        conn.commit()

    return count


def import_tickers(conn: sqlite3.Connection, csv_path: Path) -> int:
    """銘柄マスタ CSV をインポートする。"""
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
        "--db", type=Path, default=DEFAULT_DB_PATH, help=f"キャッシュ DB パス (default: {DEFAULT_DB_PATH})"
    )
    args = parser.parse_args()

    if not args.market_history and not args.tickers:
        parser.error("--market-history または --tickers のいずれかを指定してください")

    print(f"キャッシュ DB: {args.db}")
    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)

    if args.market_history:
        print(f"株価四本値をインポート中: {args.market_history}")
        t0 = time.time()
        n = import_market_history(conn, args.market_history)
        elapsed = time.time() - t0
        print(f"  完了: {n:,} 行 ({elapsed:.1f}秒)")

    if args.tickers:
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

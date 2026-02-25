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


def _make_row_tuple(row: dict, now: float) -> tuple[str, str, str, float, float | None]:
    """CSV 行をインサート用タプルに変換する。"""
    data = {k: _convert_numeric(v) for k, v in row.items()}
    data_json = json.dumps(data, ensure_ascii=False)
    adj_factor = float(row["AdjFactor"]) if row.get("AdjFactor") else None
    return (row["Code"], row["Date"], data_json, now, adj_factor)


def _insert_batch(conn: sqlite3.Connection, batch: list) -> None:
    """バッチを equities_bars_daily にインサートする。"""
    conn.executemany(
        "INSERT OR REPLACE INTO equities_bars_daily "
        "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()


def import_market_history(conn: sqlite3.Connection, csv_path: Path) -> int:
    """株価四本値 CSV を全件インポートする。"""
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


def import_market_history_incremental(conn: sqlite3.Connection, csv_path: Path) -> tuple[int, list[str]]:
    """株価四本値 CSV を差分インポートする。

    通常日: キャッシュ最新日より新しい行だけ INSERT（~4,000行）。
    分割日: AdjFactor != 1.0 の銘柄を検知し、該当コードの全行を
            DELETE → 再 INSERT する（調整済み値が全期間更新されるため）。

    Returns:
        (インポート行数, 分割検知コードのリスト)
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

        # キャッシュから該当コードを削除
        for code in split_codes:
            deleted = conn.execute(
                "DELETE FROM equities_bars_daily WHERE code = ?", (code,)
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
    parser.add_argument(
        "--incremental", action="store_true",
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

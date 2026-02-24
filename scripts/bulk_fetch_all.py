#!/usr/bin/env python3
"""Bulk fetch all Light plan data from J-Quants API into SQLite cache.

Lightプランで取得可能な全データをバルクダウンロードし、キャッシュDBにインポートする。
Bulk API で月次/日次 CSV を一括取得し、INSERT OR REPLACE で既存データを上書きする。

Usage:
    uv run python scripts/bulk_fetch_all.py
    uv run python scripts/bulk_fetch_all.py --endpoints fins_summary topix investor_types
    uv run python scripts/bulk_fetch_all.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import httpx

# プロジェクトの設定を再利用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_dat_mcp.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-dat-mcp" / "cache.db"
BATCH_SIZE = 5_000

# ========================================
# エンドポイント設定
# csv_key_map: [(CSVカラム名, DBカラム名), ...]
# ========================================

ENDPOINTS: dict[str, dict] = {
    "fins_summary": {
        "api_path": "/fins/summary",
        "table": "fins_summary",
        "csv_key_map": [("Code", "code"), ("DiscDate", "disc_date")],
    },
    "investor_types": {
        "api_path": "/equities/investor-types",
        "table": "investor_types",
        "csv_key_map": [("PubDate", "pub_date"), ("Section", "section")],
    },
    "topix": {
        "api_path": "/indices/bars/daily/topix",
        "table": "indices_bars_daily_topix",
        "csv_key_map": [("Date", "date")],
    },
    "equities_master": {
        "api_path": "/equities/master",
        "table": "equities_master",
        "csv_key_map": [("Code", "code"), ("Date", "date")],
    },
}

# テーブル DDL（store.py の定義と同じ構造）
TABLE_DDL: dict[str, str] = {
    "fins_summary": """
        CREATE TABLE IF NOT EXISTS fins_summary (
            code TEXT NOT NULL,
            disc_date TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (code, disc_date)
        )
    """,
    "investor_types": """
        CREATE TABLE IF NOT EXISTS investor_types (
            pub_date TEXT NOT NULL,
            section TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (pub_date, section)
        )
    """,
    "indices_bars_daily_topix": """
        CREATE TABLE IF NOT EXISTS indices_bars_daily_topix (
            date TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (date)
        )
    """,
    "equities_master": """
        CREATE TABLE IF NOT EXISTS equities_master (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (code, date)
        )
    """,
}


def _convert_numeric(value: str) -> int | float | str:
    """数値文字列を適切な型に変換する。"""
    if not value:
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


class BulkFetcher:
    """J-Quants Bulk API からデータを取得してSQLiteにインポートする。"""

    def __init__(self, settings: Settings, db_path: Path, dry_run: bool = False):
        self._settings = settings
        self._db_path = db_path
        self._dry_run = dry_run
        self._base_url = settings.jquants_base_url
        self._api_headers = {"x-api-key": settings.jquants_api_key}
        self._request_count = 0
        self._rate_limit = settings.get_rate_limit()
        # リクエスト間の最小間隔（秒）: バースト防止
        self._min_interval = 60.0 / self._rate_limit
        self._last_request_at = 0.0

    def _api_get(
        self,
        client: httpx.Client,
        path: str,
        params: dict | None = None,
        max_retries: int = 5,
    ) -> dict:
        """レート制限付き API GET リクエスト（429 リトライ対応）。"""
        for attempt in range(max_retries):
            # リクエスト間隔を制御
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)

            self._last_request_at = time.monotonic()
            self._request_count += 1

            resp = client.get(path, params=params)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2**attempt))
                wait = max(retry_after, self._min_interval * 2)
                logger.warning(
                    "  429 レート制限 (試行 %d/%d): %.1f秒待機",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        resp.raise_for_status()
        return {}

    def _download_csv(self, url: str) -> str:
        """署名付きURLからgzip CSVをダウンロードして展開する。"""
        resp = httpx.get(url, timeout=120.0, follow_redirects=True)
        resp.raise_for_status()
        raw = gzip.decompress(resp.content)
        return raw.decode("utf-8")

    def _import_csv_text(
        self,
        text: str,
        conn: sqlite3.Connection,
        table: str,
        csv_key_map: list[tuple[str, str]],
    ) -> int:
        """CSV テキストをパースして SQLite にインポートする。"""
        reader = csv.DictReader(io.StringIO(text))
        now = time.time()
        batch: list[tuple] = []
        count = 0

        db_cols = [db_col for _, db_col in csv_key_map]
        col_names = ", ".join(db_cols) + ", data, fetched_at"
        placeholders = ", ".join(["?"] * (len(db_cols) + 2))
        sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"

        for row in reader:
            key_values = [str(row.get(csv_col, "")) for csv_col, _ in csv_key_map]
            data = {k: _convert_numeric(v) for k, v in row.items()}
            data_json = json.dumps(data, ensure_ascii=False)
            batch.append((*key_values, data_json, now))
            count += 1

            if len(batch) >= BATCH_SIZE:
                conn.executemany(sql, batch)
                conn.commit()
                batch.clear()

        if batch:
            conn.executemany(sql, batch)
            conn.commit()

        return count

    def fetch_endpoint(self, name: str, conn: sqlite3.Connection) -> int:
        """1つのエンドポイントのバルクデータを取得・インポートする。"""
        config = ENDPOINTS[name]
        api_path = config["api_path"]
        table = config["table"]
        csv_key_map = config["csv_key_map"]

        logger.info("=" * 60)
        logger.info("開始: %s → テーブル %s", api_path, table)

        # テーブル作成
        conn.execute(TABLE_DDL[table])
        conn.commit()

        with httpx.Client(
            base_url=self._base_url,
            headers=self._api_headers,
            timeout=60.0,
        ) as client:
            # バルクリスト取得
            result = self._api_get(client, "/bulk/list", {"endpoint": api_path})
            files = result.get("data", [])
            total_size = sum(f.get("Size", 0) for f in files)
            logger.info("  ファイル数: %d (合計 %.1f MB gz)", len(files), total_size / 1024 / 1024)

            if self._dry_run:
                for f in files:
                    logger.info("    %s (%.1f KB)", f["Key"], f.get("Size", 0) / 1024)
                return 0

            total_rows = 0
            for i, file_info in enumerate(files, 1):
                key = file_info["Key"]
                size = file_info.get("Size", 0)

                # 署名付き URL 取得
                url_result = self._api_get(client, "/bulk/get", {"key": key})
                download_url = url_result.get("url", "")
                if not download_url:
                    logger.warning("  [%d/%d] URL 取得失敗: %s", i, len(files), key)
                    continue

                # CSV ダウンロード＆インポート
                text = self._download_csv(download_url)
                rows = self._import_csv_text(text, conn, table, csv_key_map)
                total_rows += rows
                logger.info(
                    "  [%d/%d] %s → %d 行 (%.1f KB)",
                    i,
                    len(files),
                    key.split("/")[-1],
                    rows,
                    size / 1024,
                )

            logger.info("  完了: %s 合計 %s 行", table, f"{total_rows:,}")
            return total_rows

    def run(self, endpoints: list[str] | None = None) -> dict[str, int]:
        """指定されたエンドポイント（未指定時は全て）のデータを取得する。"""
        targets = endpoints or list(ENDPOINTS.keys())

        logger.info("=" * 60)
        logger.info("J-Quants バルクデータ取得")
        logger.info("対象: %s", ", ".join(targets))
        logger.info("キャッシュ DB: %s", self._db_path)
        logger.info(
            "レート制限: %d req/min (%s プラン)",
            self._rate_limit,
            self._settings.jquants_plan,
        )
        if self._dry_run:
            logger.info("*** ドライラン ***")

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        results: dict[str, int] = {}
        t0 = time.time()

        try:
            for name in targets:
                if name not in ENDPOINTS:
                    logger.warning("不明なエンドポイント: %s（スキップ）", name)
                    continue
                try:
                    rows = self.fetch_endpoint(name, conn)
                    results[name] = rows
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:
                        logger.warning("  プラン制限でスキップ: %s (403)", name)
                        results[name] = 0
                    else:
                        logger.error("  HTTP エラー: %s: %s", name, e)
                        results[name] = -1
                except Exception as e:
                    logger.error("  エラー: %s: %s", name, e)
                    results[name] = -1
        finally:
            # DB 統計表示
            for tbl in set(ENDPOINTS[n]["table"] for n in targets if n in ENDPOINTS):
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                    if row:
                        logger.info("  テーブル %s: %s 行", tbl, f"{row[0]:,}")
                except sqlite3.OperationalError:
                    pass

            if self._db_path.exists():
                db_size = self._db_path.stat().st_size / (1024 * 1024)
                logger.info("  DB サイズ: %.1f MB", db_size)

            conn.close()

        elapsed = time.time() - t0
        logger.info("=" * 60)
        logger.info("完了: %.1f秒, API リクエスト: %d 回", elapsed, self._request_count)
        for name, rows in results.items():
            status = f"{rows:,} 行" if rows >= 0 else "エラー"
            logger.info("  %s: %s", name, status)

        return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Light プランの全データをバルクダウンロードしてキャッシュ DB にインポート",
    )
    parser.add_argument(
        "--endpoints",
        nargs="+",
        choices=list(ENDPOINTS.keys()),
        help="対象エンドポイント（省略時は全て）",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"キャッシュ DB パス (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument("--dry-run", action="store_true", help="ファイル一覧のみ表示")
    args = parser.parse_args()

    settings = Settings()
    if not settings.jquants_api_key:
        logger.error("JQUANTS_API_KEY が設定されていません")
        sys.exit(1)

    fetcher = BulkFetcher(settings, args.db, dry_run=args.dry_run)
    fetcher.run(args.endpoints)


if __name__ == "__main__":
    main()

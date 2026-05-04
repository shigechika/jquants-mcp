#!/usr/bin/env python3
"""Bulk fetch data from the J-Quants API into the SQLite cache.

Pulls monthly / daily CSVs via the J-Quants Bulk API and loads them
into the jquants-mcp cache with INSERT OR REPLACE semantics. Use this
for initial cache hydration or periodic backfill — it is dramatically
faster than warming the cache via per-tool MCP calls, which are
rate-limited per plan.

Usage:
    uv run python scripts/bulk_fetch_all.py
    uv run python scripts/bulk_fetch_all.py --endpoints fins_summary topix margin_interest
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

# Reuse the project's settings + schema modules via sys.path so this
# script can run inside a foreign venv that doesn't have jquants-mcp
# installed, as long as the repo is on disk.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_mcp.cache.schema import all_ddl
from jquants_mcp.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-mcp" / "cache.db"
BATCH_SIZE = 5_000

# ========================================
# Endpoint configuration
# csv_key_map: [(CSV column name, DB column name), ...]
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
    "margin_interest": {
        "api_path": "/markets/margin-interest",
        "table": "markets_margin_interest",
        "csv_key_map": [("Code", "code"), ("Date", "date")],
    },
    "margin_alert": {
        "api_path": "/markets/margin-alert",
        "table": "markets_margin_alert",
        "csv_key_map": [("Code", "code"), ("PubDate", "date")],
    },
    "short_ratio": {
        "api_path": "/markets/short-ratio",
        "table": "markets_short_ratio",
        "csv_key_map": [("S33", "s33"), ("Date", "date")],
    },
    "short_sale_report": {
        "api_path": "/markets/short-sale-report",
        "table": "markets_short_sale_report",
        "csv_key_map": [("Code", "code"), ("DiscDate", "disc_date"), ("SSName", "reporter_name")],
    },
    "breakdown": {
        "api_path": "/markets/breakdown",
        "table": "markets_breakdown",
        "csv_key_map": [("Code", "code"), ("Date", "date")],
    },
    "calendar": {
        "api_path": "/markets/calendar",
        "table": "markets_calendar",
        "csv_key_map": [("Date", "date")],
    },
    "indices_bars_daily": {
        "api_path": "/indices/bars/daily",
        "table": "indices_bars_daily",
        "csv_key_map": [("Code", "code"), ("Date", "date")],
    },
    "options_225": {
        "api_path": "/derivatives/bars/daily/options/225",
        "table": "derivatives_bars_daily_options_225",
        "csv_key_map": [("Code", "code"), ("Date", "date")],
    },
    "equities_bars_daily": {
        "api_path": "/equities/bars/daily",
        "table": "equities_bars_daily",
        "csv_key_map": [("Code", "code"), ("Date", "date")],
        # AdjFactor is queried directly from the column for split detection;
        # store it as REAL rather than leaving it NULL.
        "numeric_columns": [("AdjFactor", "adj_factor")],
    },
}

# Table DDL (single source of truth in schema.py)
TABLE_DDL: dict[str, str] = all_ddl()


def _convert_numeric(value: str) -> int | float | str:
    """Convert a numeric string into the most specific type it fits."""
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
    """Fetches data via the J-Quants Bulk API and loads it into SQLite."""

    def __init__(
        self, settings: Settings, db_path: Path, dry_run: bool = False, plan: str | None = None
    ):
        self._settings = settings
        self._db_path = db_path
        self._dry_run = dry_run
        self._plan = plan or settings.jquants_plan
        self._base_url = settings.jquants_base_url
        self._api_headers = {"x-api-key": settings.jquants_api_key}
        self._request_count = 0
        self._rate_limit = settings.get_rate_limit()
        # Minimum inter-request interval (seconds) to prevent bursts.
        self._min_interval = 60.0 / self._rate_limit
        self._last_request_at = 0.0

    def _api_get(
        self,
        client: httpx.Client,
        path: str,
        params: dict | None = None,
        max_retries: int = 5,
    ) -> dict:
        """Rate-limited GET with 429 retry handling."""
        for attempt in range(max_retries):
            # Pace requests to stay within the plan's rate limit.
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
                    "  429 rate limited (attempt %d/%d): sleeping %.1fs",
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
        """Download a gzipped CSV from a signed URL and decompress it."""
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
        numeric_columns: list[tuple[str, str]] | None = None,
    ) -> int:
        """Parse CSV text and import rows into SQLite."""
        reader = csv.DictReader(io.StringIO(text))
        now = time.time()
        batch: list[tuple] = []
        count = 0
        num_cols = numeric_columns or []

        db_cols = [db_col for _, db_col in csv_key_map] + [db_col for _, db_col in num_cols]
        col_names = ", ".join(db_cols) + ", data, fetched_at"
        placeholders = ", ".join(["?"] * (len(db_cols) + 2))
        sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"

        for row in reader:
            key_values = [str(row.get(csv_col, "")) for csv_col, _ in csv_key_map]
            num_values: list[float | None] = [
                (float(v) if (v := row.get(csv_col, "")) else None) for csv_col, _ in num_cols
            ]
            data = {k: _convert_numeric(v) for k, v in row.items()}
            data_json = json.dumps(data, ensure_ascii=False)
            batch.append((*key_values, *num_values, data_json, now))
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
        """Fetch and import bulk data for a single endpoint."""
        config = ENDPOINTS[name]
        api_path = config["api_path"]
        table = config["table"]
        csv_key_map = config["csv_key_map"]
        numeric_columns = config.get("numeric_columns")

        logger.info("=" * 60)
        logger.info("start: %s -> table %s", api_path, table)

        # Ensure the destination table exists.
        conn.execute(TABLE_DDL[table])
        conn.commit()

        with httpx.Client(
            base_url=self._base_url,
            headers=self._api_headers,
            timeout=60.0,
        ) as client:
            # List the bulk files available for this endpoint.
            result = self._api_get(client, "/bulk/list", {"endpoint": api_path})
            files = result.get("data", [])
            total_size = sum(f.get("Size", 0) for f in files)
            logger.info("  files: %d (%.1f MB gz total)", len(files), total_size / 1024 / 1024)

            if self._dry_run:
                for f in files:
                    logger.info("    %s (%.1f KB)", f["Key"], f.get("Size", 0) / 1024)
                return 0

            total_rows = 0
            for i, file_info in enumerate(files, 1):
                key = file_info["Key"]
                size = file_info.get("Size", 0)

                # Get a signed download URL for the file.
                url_result = self._api_get(client, "/bulk/get", {"key": key})
                download_url = url_result.get("url", "")
                if not download_url:
                    logger.warning("  [%d/%d] failed to get signed URL: %s", i, len(files), key)
                    continue

                # Download the CSV and import it.
                text = self._download_csv(download_url)
                rows = self._import_csv_text(text, conn, table, csv_key_map, numeric_columns)
                total_rows += rows
                logger.info(
                    "  [%d/%d] %s -> %d rows (%.1f KB)",
                    i,
                    len(files),
                    key.split("/")[-1],
                    rows,
                    size / 1024,
                )

            logger.info("  done: %s total %s rows", table, f"{total_rows:,}")
            return total_rows

    def run(self, endpoints: list[str] | None = None) -> dict[str, int]:
        """Fetch the specified endpoints (or all endpoints when unspecified)."""
        targets = endpoints or list(ENDPOINTS.keys())

        logger.info("=" * 60)
        logger.info("J-Quants bulk fetch")
        logger.info("targets: %s", ", ".join(targets))
        logger.info("cache DB: %s", self._db_path)
        logger.info("saving plan: %s", self._plan)
        logger.info(
            "rate limit: %d req/min (%s plan)",
            self._rate_limit,
            self._settings.jquants_plan,
        )
        if self._dry_run:
            logger.info("*** dry run ***")

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")

        results: dict[str, int] = {}
        t0 = time.time()

        try:
            for name in targets:
                if name not in ENDPOINTS:
                    logger.warning("unknown endpoint: %s (skipped)", name)
                    continue
                try:
                    rows = self.fetch_endpoint(name, conn)
                    results[name] = rows
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403:
                        logger.warning("  skipped due to plan restriction: %s (403)", name)
                        results[name] = 0
                    else:
                        logger.error("  HTTP error: %s: %s", name, e)
                        results[name] = -1
                except Exception as e:
                    logger.error("  error: %s: %s", name, e)
                    results[name] = -1
        finally:
            # Log per-table row counts.
            for tbl in set(ENDPOINTS[n]["table"] for n in targets if n in ENDPOINTS):
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                    if row:
                        logger.info("  table %s: %s rows", tbl, f"{row[0]:,}")
                except sqlite3.OperationalError:
                    pass

            if self._db_path.exists():
                db_size = self._db_path.stat().st_size / (1024 * 1024)
                logger.info("  DB size: %.1f MB", db_size)

            conn.close()

        elapsed = time.time() - t0
        logger.info("=" * 60)
        logger.info("done: %.1fs, API requests: %d", elapsed, self._request_count)
        for name, rows in results.items():
            status = f"{rows:,} rows" if rows >= 0 else "error"
            logger.info("  %s: %s", name, status)

        return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-download J-Quants data and import it into the SQLite cache.",
    )
    parser.add_argument(
        "--endpoints",
        nargs="+",
        choices=list(ENDPOINTS.keys()),
        help="Target endpoints (default: all endpoints allowed by the plan).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Cache DB path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--plan",
        choices=["free", "light", "standard", "premium"],
        default=None,
        help="Plan to record rows under (default: value from config file).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the files that would be fetched without downloading them.",
    )
    args = parser.parse_args()

    settings = Settings()
    if not settings.jquants_api_key:
        logger.error("JQUANTS_API_KEY is not configured")
        sys.exit(1)

    fetcher = BulkFetcher(settings, args.db, dry_run=args.dry_run, plan=args.plan)
    fetcher.run(args.endpoints)


if __name__ == "__main__":
    main()

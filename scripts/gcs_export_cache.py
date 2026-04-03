#!/usr/bin/env python3
"""Export a standard-plan-only cache.db to GCS for Cloud Run.

Creates a temporary copy of the local cache.db, removes non-standard
plan rows, runs VACUUM, and uploads the result to GCS.  This keeps
Cloud Run's cache compact (~40% of the full DB) while m1.local retains
all plans.

Usage:
    python scripts/gcs_export_cache.py [--dry-run]

Environment variables:
    GCS_BUCKET          GCS bucket name (required)
    GCS_PREFIX          Object key prefix (default: "jquants-dat-mcp/")
    JQUANTS_CACHE_DIR   Local cache directory (default: ~/.cache/jquants-dat-mcp)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gcs_export_cache")

# Tier 1 テーブル名（cache/store.py の _TIER1_TABLES と同期）
_TIER1_TABLES = [
    "equities_bars_daily",
    "equities_master",
    "fins_summary",
    "indices_bars_daily_topix",
    "investor_types",
    "markets_margin_interest",
    "markets_margin_alert",
    "markets_short_ratio",
    "markets_breakdown",
    "markets_calendar",
]

_EXPORT_PATH = Path("/tmp/cache_gcs_export.db")


def _get_source_path() -> Path:
    """Return the path to the source cache.db."""
    cache_dir = os.environ.get("JQUANTS_CACHE_DIR", "")
    if cache_dir:
        return Path(cache_dir) / "cache.db"
    return Path.home() / ".cache" / "jquants-dat-mcp" / "cache.db"


def _trim_to_standard(db_path: Path) -> dict[str, int]:
    """Remove non-standard plan rows from all Tier 1 tables.

    Returns:
        Dict of table_name -> deleted row count.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    deleted: dict[str, int] = {}
    for table in _TIER1_TABLES:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        except sqlite3.OperationalError:
            continue
        if "plan" not in cols:
            continue

        count = conn.execute(
            f"SELECT count(*) FROM {table} WHERE plan != 'standard'"  # noqa: S608
        ).fetchone()[0]
        if count == 0:
            continue

        conn.execute(f"DELETE FROM {table} WHERE plan != 'standard'")  # noqa: S608
        deleted[table] = count
        logger.info("Deleted %d non-standard rows from %s", count, table)

    conn.commit()
    conn.close()
    return deleted


def _vacuum(db_path: Path) -> None:
    """Run VACUUM to reclaim disk space."""
    logger.info("Running VACUUM...")
    conn = sqlite3.connect(str(db_path))
    conn.execute("VACUUM")
    conn.close()


def _upload_to_gcs(db_path: Path) -> None:
    """Upload the export DB to GCS."""
    from google.cloud import storage  # type: ignore[import-untyped]

    bucket_name = os.environ.get("GCS_BUCKET", "")
    if not bucket_name:
        logger.error("GCS_BUCKET environment variable is not set")
        sys.exit(1)

    prefix = os.environ.get("GCS_PREFIX", "jquants-dat-mcp/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    blob_name = f"{prefix}cache.db"
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    size_gb = db_path.stat().st_size / (1024**3)
    logger.info("Uploading %.1f GB to gs://%s/%s ...", size_gb, bucket_name, blob_name)
    blob.upload_from_filename(str(db_path))
    logger.info("Upload complete: gs://%s/%s", bucket_name, blob_name)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export standard-plan-only cache.db to GCS",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Trim and vacuum locally but skip GCS upload",
    )
    args = parser.parse_args()

    source = _get_source_path()
    if not source.exists():
        logger.error("Source cache.db not found: %s", source)
        sys.exit(1)

    source_size = source.stat().st_size / (1024**3)
    logger.info("Source: %s (%.1f GB)", source, source_size)

    # 一時コピー作成
    start = time.time()
    logger.info("Copying to %s ...", _EXPORT_PATH)
    shutil.copy2(str(source), str(_EXPORT_PATH))
    logger.info("Copy done (%.0fs)", time.time() - start)

    # non-standard 行の削除
    start = time.time()
    deleted = _trim_to_standard(_EXPORT_PATH)
    total_deleted = sum(deleted.values())
    logger.info("Trimmed %d rows (%.0fs)", total_deleted, time.time() - start)

    # VACUUM
    start = time.time()
    _vacuum(_EXPORT_PATH)
    export_size = _EXPORT_PATH.stat().st_size / (1024**3)
    logger.info(
        "VACUUM done (%.0fs): %.1f GB -> %.1f GB", time.time() - start, source_size, export_size
    )

    # GCS アップロード
    if args.dry_run:
        logger.info("Dry run: skipping GCS upload")
    else:
        _upload_to_gcs(_EXPORT_PATH)

    # 一時ファイル削除
    _EXPORT_PATH.unlink(missing_ok=True)
    logger.info("Cleanup done")


if __name__ == "__main__":
    main()

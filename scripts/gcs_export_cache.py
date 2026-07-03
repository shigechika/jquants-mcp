#!/usr/bin/env python3
"""Export cache.db to GCS for Cloud Run.

Creates a temporary copy of the local cache.db, trims old data to the
Cloud Run retention window (default 5 years), runs VACUUM, and uploads
the result to GCS.

Legacy plan-column cleanup is retained for backward compatibility but
is a no-op once the plan column has been removed (user_version >= 2).

Usage:
    python scripts/gcs_export_cache.py [--dry-run]
    python scripts/gcs_export_cache.py --retention-years 3

Environment variables:
    GCS_BUCKET          GCS bucket name (required)
    GCS_PREFIX          Object key prefix (default: "jquants-mcp/")
    JQUANTS_CACHE_DIR   Local cache directory (default: ~/.cache/jquants-mcp)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_mcp.cache.schema import (  # noqa: E402
    TIER1_TABLES,
    ensure_cross_section_indexes,
    migrate_add_fins_indexes,
migrate_split_fins_pk,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gcs_export_cache")

_TIER1_TABLE_NAMES = list(TIER1_TABLES.keys())

# zstd compression level for cache.db.zst. Level 10 with all cores is a good
# balance for the daily publish: ~2-3x smaller (so the Cloud Run cold-start
# download transfers far fewer bytes) without a slow compression step.
_ZSTD_LEVEL = 10


def _get_source_path() -> Path:
    """Return the path to the source cache.db."""
    cache_dir = os.environ.get("JQUANTS_CACHE_DIR", "")
    if cache_dir:
        return Path(cache_dir) / "cache.db"
    return Path.home() / ".cache" / "jquants-mcp" / "cache.db"


def _trim_to_standard(db_path: Path) -> dict[str, int]:
    """Remove non-standard plan rows from all Tier 1 tables.

    Returns:
        Dict of table_name -> deleted row count.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    deleted: dict[str, int] = {}
    for table in _TIER1_TABLE_NAMES:
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


# Table name -> date column name for trimming
_DATE_COLUMN: dict[str, str] = {
    "equities_bars_daily": "date",
    "equities_master": "date",
    "fins_summary": "disc_date",
    "indices_bars_daily_topix": "date",
    "investor_types": "pub_date",
    "markets_margin_interest": "date",
    "markets_margin_alert": "date",
    "markets_short_ratio": "date",
    "markets_breakdown": "date",
    "markets_calendar": "date",
}


def _trim_by_date(db_path: Path, retention_years: int) -> dict[str, int]:
    """Delete rows older than retention_years from all Tier 1 tables.

    Cloud Run serves Light-plan users (5-year window). Trimming old data
    keeps the exported DB compact.

    Returns:
        Dict of table_name -> deleted row count.
    """
    today = date.today()
    try:
        cutoff = today.replace(year=today.year - retention_years)
    except ValueError:
        cutoff = today.replace(year=today.year - retention_years, day=28)
    cutoff_str = cutoff.isoformat()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    deleted: dict[str, int] = {}
    for table in _TIER1_TABLE_NAMES:
        date_col = _DATE_COLUMN.get(table)
        if not date_col:
            continue
        try:
            count = conn.execute(
                f"SELECT count(*) FROM {table} WHERE {date_col} < ?",  # noqa: S608
                (cutoff_str,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if count == 0:
            continue

        conn.execute(
            f"DELETE FROM {table} WHERE {date_col} < ?",  # noqa: S608
            (cutoff_str,),
        )
        deleted[table] = count
        logger.info("Trimmed %d rows older than %s from %s", count, cutoff_str, table)

    conn.commit()
    conn.close()
    return deleted


def _ensure_indexes(db_path: Path) -> None:
    """Build the cache read indexes into the export DB.

    Guarantees the shipped cache.db (compressed to cache.db.zst) carries the
    fins_summary FY/dividend indexes and the cross-section date indexes
    regardless of the source DB's state. Run BEFORE VACUUM so the fresh index
    pages are packed tightly (better zstd ratio). Idempotent.
    """
    logger.info("Ensuring cache read indexes...")
    conn = sqlite3.connect(str(db_path))
    try:
        migrate_add_fins_indexes(conn)
        migrate_split_fins_pk(conn)
        ensure_cross_section_indexes(conn)
    finally:
        conn.close()


def _vacuum(db_path: Path) -> None:
    """Run VACUUM to reclaim disk space."""
    logger.info("Running VACUUM...")
    conn = sqlite3.connect(str(db_path))
    conn.execute("VACUUM")
    conn.close()


def _ensure_user_version(db_path: Path) -> None:
    """Ensure user_version is at least 2 so Cloud Run skips all migrations.

    store.py migrations:
      - user_version < 1: _migrate_normalize_fields (rewrite legacy field names)
      - user_version < 2: _migrate_drop_plan (remove plan column)
    Both are expensive on large DBs. The export DB should already be fully
    migrated, but set user_version = 2 explicitly as a safety net.
    """
    conn = sqlite3.connect(str(db_path))
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < 2:
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        logger.info("Set PRAGMA user_version = 2 (was %d)", current)
    else:
        logger.info("PRAGMA user_version = %d (already up to date)", current)
    conn.close()


def _compress_to_zst(db_path: Path) -> Path:
    """Stream-compress ``db_path`` to a sibling ``.zst`` file and return its path.

    Streaming keeps memory flat regardless of DB size; ``threads=-1`` uses all
    cores and ``write_checksum`` adds a frame checksum so a corrupt transfer is
    caught at decompression time.
    """
    import zstandard

    zst_path = Path(f"{db_path}.zst")
    cctx = zstandard.ZstdCompressor(level=_ZSTD_LEVEL, threads=-1, write_checksum=True)
    with open(db_path, "rb") as src, open(zst_path, "wb") as dst:
        cctx.copy_stream(src, dst)
    return zst_path


def _upload_blob_atomic(bucket, local_path: Path, blob_name: str) -> None:
    """Upload ``local_path`` to ``blob_name`` via a temp object + server-side rename.

    A crash mid-upload leaves the previous live object untouched instead of
    exposing a half-written object to the Cloud Run startup copy.
    """
    upload_name = f"{blob_name}.uploading"
    upload_blob = bucket.blob(upload_name)
    size_gb = local_path.stat().st_size / (1024**3)
    logger.info("Uploading %.2f GB to gs://%s/%s ...", size_gb, bucket.name, blob_name)
    upload_blob.upload_from_filename(str(local_path))
    bucket.rename_blob(upload_blob, blob_name)
    logger.info("Upload complete: gs://%s/%s", bucket.name, blob_name)


def _upload_to_gcs(db_path: Path) -> None:
    """Upload the export DB to GCS as cache.db.zst (preferred) and cache.db.

    The Cloud Run downloader prefers the compressed object; the uncompressed
    cache.db is kept for a backward-compatible rollback of the downloader and
    can be dropped once .zst is the only consumer.
    """
    from google.cloud import storage  # type: ignore[import-untyped]

    bucket_name = os.environ.get("GCS_BUCKET", "")
    if not bucket_name:
        logger.error("GCS_BUCKET environment variable is not set")
        sys.exit(1)

    prefix = os.environ.get("GCS_PREFIX", "jquants-mcp/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    # 1. Compressed object (preferred by the Cloud Run downloader).
    start = time.time()
    zst_path = _compress_to_zst(db_path)
    try:
        ratio = db_path.stat().st_size / max(zst_path.stat().st_size, 1)
        logger.info(
            "Compressed cache.db -> .zst in %.0fs (%.2fx smaller)", time.time() - start, ratio
        )
        _upload_blob_atomic(bucket, zst_path, f"{prefix}cache.db.zst")
    finally:
        zst_path.unlink(missing_ok=True)

    # 2. Uncompressed object (rollback fallback for the downloader).
    _upload_blob_atomic(bucket, db_path, f"{prefix}cache.db")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export cache.db to GCS for Cloud Run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Trim and vacuum locally but skip GCS upload",
    )
    parser.add_argument(
        "--retention-years",
        type=int,
        default=5,
        help="Keep only the last N years of data (default: 5, Light plan window)",
    )
    args = parser.parse_args()

    source = _get_source_path()
    if not source.exists():
        logger.error("Source cache.db not found: %s", source)
        sys.exit(1)

    source_size = source.stat().st_size / (1024**3)
    logger.info("Source: %s (%.1f GB)", source, source_size)

    # Unique temp path so concurrent runs do not clobber each other; cleaned up
    # in the finally block even when an intermediate step raises.
    fd, tmp_name = tempfile.mkstemp(suffix=".db", prefix="cache_gcs_export_", dir="/tmp")
    os.close(fd)
    export_path = Path(tmp_name)
    try:
        # Make a working copy of the source DB.
        start = time.time()
        logger.info("Copying to %s ...", export_path)
        shutil.copy2(str(source), str(export_path))
        logger.info("Copy done (%.0fs)", time.time() - start)

        # Legacy: drop non-standard plan rows (no-op once the plan column is gone).
        start = time.time()
        deleted = _trim_to_standard(export_path)
        total_deleted = sum(deleted.values())
        if total_deleted:
            logger.info("Trimmed %d plan rows (%.0fs)", total_deleted, time.time() - start)

        # Date-based trim (limit the data window for Cloud Run).
        start = time.time()
        date_deleted = _trim_by_date(export_path, args.retention_years)
        total_date_deleted = sum(date_deleted.values())
        logger.info(
            "Date trim (%d-year retention): %d rows deleted (%.0fs)",
            args.retention_years,
            total_date_deleted,
            time.time() - start,
        )

        # Build the cache read indexes BEFORE VACUUM so their pages are packed
        # tightly (better zstd ratio) and ship inside cache.db.zst.
        _ensure_indexes(export_path)

        # VACUUM
        start = time.time()
        _vacuum(export_path)
        export_size = export_path.stat().st_size / (1024**3)
        logger.info(
            "VACUUM done (%.0fs): %.1f GB -> %.1f GB",
            time.time() - start,
            source_size,
            export_size,
        )

        # Skip Cloud Run migration by marking the DB as already-migrated
        _ensure_user_version(export_path)

        # Upload to GCS
        if args.dry_run:
            logger.info("Dry run: skipping GCS upload")
        else:
            _upload_to_gcs(export_path)
    finally:
        export_path.unlink(missing_ok=True)
        logger.info("Cleanup done")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""GCS sync utility for Cloud Run deployment of jquants-mcp.

Manages database synchronization between Cloud Run's ephemeral /tmp
filesystem and Google Cloud Storage.

Usage:
    # Download cache.db from GCS (background, large)
    python gcs_sync.py --init-cache

    # Download auth DBs from GCS (fast)
    python gcs_sync.py --init

    # Run background daemon: upload every 5 minutes, final upload on SIGTERM
    python gcs_sync.py --daemon

    # Upload once and exit
    python gcs_sync.py --upload

Environment variables:
    GCS_BUCKET      GCS bucket name (required)
    GCS_PREFIX      Object key prefix (default: "jquants-mcp/")
    JQUANTS_CACHE_DIR  Local cache directory (default: /tmp)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gcs_sync")

# Files to download from GCS at startup (auth DBs)
# On Cloud Run, oauth_state.db and users.db live in Firestore instead
# of GCS-synced SQLite so they survive instance restarts immediately.
# Nothing currently needs to be downloaded on Cloud Run startup.
_DOWNLOAD_FILES: list[str] = []

# Files to upload to GCS (daemon / --upload)
# cache.db is excluded here: it is owned by the self-hosted publisher
# (see scripts/daily_fetch.py + scripts/gcs_export_cache.py) which pushes
# a fresh snapshot to GCS on its own schedule.
# users.db and oauth_state.db now live in Firestore on Cloud Run.
_UPLOAD_FILES: list[str] = []

# Sync interval in seconds
_SYNC_INTERVAL = int(os.environ.get("GCS_SYNC_INTERVAL", "300"))  # 5 minutes


def _get_config() -> tuple[str, str, Path]:
    """Return (bucket, prefix, cache_dir) from environment variables."""
    bucket = os.environ.get("GCS_BUCKET", "")
    if not bucket:
        logger.error("GCS_BUCKET environment variable is not set")
        sys.exit(1)

    prefix = os.environ.get("GCS_PREFIX", "jquants-mcp/")
    # Ensure prefix ends with /
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    cache_dir_str = os.environ.get("JQUANTS_CACHE_DIR", "/tmp")
    cache_dir = Path(cache_dir_str)
    cache_dir.mkdir(parents=True, exist_ok=True)

    return bucket, prefix, cache_dir


def download_files(file_list: list[str] | None = None) -> int:
    """Download files from GCS to local cache dir.

    Args:
        file_list: List of filenames to download. Defaults to _DOWNLOAD_FILES.

    Missing objects are silently skipped (first-run case).
    Returns immediately without initializing the GCS client when the
    resolved file list is empty, avoiding unnecessary credential lookups
    that can hang indefinitely on non-GCP hosts.

    Returns:
        The number of files that failed to download (a missing object is not a
        failure). Callers running one-shot can map this to an exit code.
    """
    files = file_list if file_list is not None else _DOWNLOAD_FILES
    if not files:
        logger.debug("No files configured for download, skipping")
        return 0

    from google.cloud import storage  # type: ignore[import-untyped]
    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    bucket, prefix, cache_dir = _get_config()
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)

    failures = 0
    for filename in files:
        blob_name = f"{prefix}{filename}"
        local_path = cache_dir / filename
        # Download to a temp file first, then atomic rename.
        # This prevents the MCP server from reading a half-written file.
        tmp_path = cache_dir / f".{filename}.download"
        blob = gcs_bucket.blob(blob_name)
        try:
            blob.download_to_filename(str(tmp_path))
            tmp_path.rename(local_path)
            size_mb = local_path.stat().st_size / 1024 / 1024
            logger.info(
                "Downloaded gs://%s/%s -> %s (%.1f MB)", bucket, blob_name, local_path, size_mb
            )
        except NotFound:
            logger.info("gs://%s/%s not found, skipping (first run?)", bucket, blob_name)
            tmp_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed to download %s: %s", blob_name, e)
            tmp_path.unlink(missing_ok=True)
            failures += 1
    return failures


def _download_zst_to(gcs_bucket, zst_blob_name: str, dest: Path) -> bool:
    """Stream-download a zstd object from GCS and decompress it to ``dest``.

    Streaming (GCS read -> zstd decompress -> file) keeps memory bounded: the
    full compressed object is never held in RAM or staged on disk, so the tmpfs
    peak is just the decompressed output (matching the uncompressed path).

    Returns:
        True on success. False when zstandard is unavailable, the object is
        missing, or decompression fails — in every False case the caller falls
        back to the uncompressed object, so a missing/old ``.zst`` never breaks
        startup.
    """
    try:
        import zstandard
    except ImportError:
        logger.info("zstandard not installed; falling back to uncompressed cache.db")
        return False

    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    blob = gcs_bucket.blob(zst_blob_name)
    try:
        dctx = zstandard.ZstdDecompressor()
        with blob.open("rb") as src, open(dest, "wb") as out:
            dctx.copy_stream(src, out)
        return True
    except NotFound:
        logger.info("%s not found; falling back to uncompressed cache.db", zst_blob_name)
        dest.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.warning(
            "zstd download/decompress of %s failed (%s); falling back to uncompressed",
            zst_blob_name,
            e,
        )
        dest.unlink(missing_ok=True)
        return False


def download_cache_db() -> int:
    """Download cache.db from GCS, preferring the zstd-compressed object.

    Tries ``<prefix>cache.db.zst`` (stream-decompressed — far faster to transfer)
    and falls back to the uncompressed ``<prefix>cache.db`` when the compressed
    object is missing or zstandard is unavailable. This lets the publisher and
    Cloud Run roll out independently: until the publisher writes ``.zst`` the
    fallback keeps startup working unchanged.

    Returns:
        The number of failures (0 on success or a first-run missing object, 1 on
        a genuine download failure) so ``--init-cache`` can map it to the alert.
    """
    from google.cloud import storage  # type: ignore[import-untyped]
    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    bucket, prefix, cache_dir = _get_config()
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)

    local_path = cache_dir / "cache.db"
    tmp_path = cache_dir / ".cache.db.download"

    # 1. Preferred: compressed cache.db.zst, stream-decompressed.
    if _download_zst_to(gcs_bucket, f"{prefix}cache.db.zst", tmp_path):
        tmp_path.rename(local_path)
        size_mb = local_path.stat().st_size / 1024 / 1024
        logger.info(
            "Downloaded gs://%s/%scache.db.zst -> %s (%.1f MB decompressed)",
            bucket,
            prefix,
            local_path,
            size_mb,
        )
        return 0

    # 2. Fallback: uncompressed cache.db.
    blob = gcs_bucket.blob(f"{prefix}cache.db")
    try:
        blob.download_to_filename(str(tmp_path))
        tmp_path.rename(local_path)
        size_mb = local_path.stat().st_size / 1024 / 1024
        logger.info(
            "Downloaded gs://%s/%scache.db -> %s (%.1f MB)", bucket, prefix, local_path, size_mb
        )
        return 0
    except NotFound:
        logger.info("gs://%s/%scache.db not found, skipping (first run?)", bucket, prefix)
        tmp_path.unlink(missing_ok=True)
        return 0
    except Exception as e:
        logger.warning("Failed to download cache.db: %s", e)
        tmp_path.unlink(missing_ok=True)
        return 1


def _checkpoint_sqlite(db_path: Path) -> None:
    """Run a WAL checkpoint to ensure all data is in the main DB file.

    SQLite WAL mode writes to .db-wal first; without checkpointing, the
    main .db file will be missing recent changes when uploaded to GCS.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning("Failed to checkpoint %s: %s", db_path, e)


def upload_files() -> int:
    """Upload local cache files to GCS.

    Files that do not exist locally are silently skipped.
    Returns immediately without initializing the GCS client when
    _UPLOAD_FILES is empty, avoiding unnecessary credential lookups that
    can hang indefinitely on non-GCP hosts.

    Returns:
        The number of files that failed to upload. Callers running one-shot can
        map this to an exit code; the daemon loop ignores it and retries.
    """
    if not _UPLOAD_FILES:
        logger.debug("No files configured for upload, skipping")
        return 0

    from google.cloud import storage  # type: ignore[import-untyped]

    bucket, prefix, cache_dir = _get_config()
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)

    failures = 0
    for filename in _UPLOAD_FILES:
        local_path = cache_dir / filename
        if not local_path.exists():
            logger.debug("Local file %s not found, skipping upload", local_path)
            continue

        # Checkpoint WAL to ensure recent writes are flushed to main DB.
        _checkpoint_sqlite(local_path)

        blob_name = f"{prefix}{filename}"
        blob = gcs_bucket.blob(blob_name)
        try:
            blob.upload_from_filename(str(local_path))
            size_mb = local_path.stat().st_size / 1024 / 1024
            logger.info(
                "Uploaded %s -> gs://%s/%s (%.1f MB)", local_path, bucket, blob_name, size_mb
            )
        except Exception as e:
            logger.warning("Failed to upload %s: %s", blob_name, e)
            failures += 1
    return failures


def run_daemon() -> None:
    """Run background sync daemon.

    Uploads files every GCS_SYNC_INTERVAL seconds.
    On SIGTERM, performs a final upload and exits cleanly.
    """
    _shutdown_requested = False

    def _sigterm_handler(signum: int, frame: object) -> None:
        nonlocal _shutdown_requested
        logger.info("SIGTERM received, performing final GCS upload...")
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    logger.info("GCS sync daemon started (interval: %ds)", _SYNC_INTERVAL)

    while not _shutdown_requested:
        # Sleep in short intervals to respond to SIGTERM quickly
        for _ in range(_SYNC_INTERVAL):
            if _shutdown_requested:
                break
            time.sleep(1)

        if not _shutdown_requested:
            logger.info("Periodic GCS sync...")
            upload_files()

    # Final upload before exit
    upload_files()
    logger.info("GCS sync daemon stopped")


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="GCS cache sync utility for jquants-mcp")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--init-cache",
        action="store_true",
        help="Download cache.db from GCS (background startup)",
    )
    group.add_argument(
        "--init", action="store_true", help="Download auth DBs from GCS (users.db, oauth_state.db)"
    )
    group.add_argument("--daemon", action="store_true", help="Run background sync daemon")
    group.add_argument("--upload", action="store_true", help="Upload local cache to GCS and exit")
    args = parser.parse_args()

    # One-shot modes surface failures as a non-zero exit code so callers
    # (entrypoint.sh, cron) can detect them. The daemon stays resilient and
    # ignores the return value (it retries on the next tick).
    if args.init_cache:
        failures = download_cache_db()
        if failures:
            # Emit the exact phrase the Cloud Monitoring policy
            # (ops/alerts/05-cache-db-download-fail.yaml) greps for, so a
            # genuine startup download failure actually pages instead of
            # silently disabling the only alert guarding the cache pipeline.
            logger.error("cache.db download failed")
    elif args.init:
        failures = download_files()
    elif args.daemon:
        run_daemon()
        return
    elif args.upload:
        failures = upload_files()
    else:
        failures = 0

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

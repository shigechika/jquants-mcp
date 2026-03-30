#!/usr/bin/env python3
"""GCS sync utility for Cloud Run deployment of jquants-dat-mcp.

Manages auth database synchronization between Cloud Run's ephemeral /tmp
filesystem and Google Cloud Storage. cache.db is gcsfuse-mounted read-only
and does not need download/upload via this script.

Usage:
    # Download auth DBs from GCS (run at startup)
    python gcs_sync.py --init

    # Run background daemon: upload every 5 minutes, final upload on SIGTERM
    python gcs_sync.py --daemon

    # Upload once and exit
    python gcs_sync.py --upload

Environment variables:
    GCS_BUCKET      GCS bucket name (required)
    GCS_PREFIX      Object key prefix (default: "jquants-dat-mcp/")
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

# Files to download from GCS at startup (auth DBs only; cache.db is gcsfuse-mounted)
_DOWNLOAD_FILES = ["users.db", "oauth_state.db"]

# Files to upload to GCS (daemon / --upload)
# cache.db is excluded: it is gcsfuse-mounted read-only from GCS,
# and owned by self-hosted server (jpx-short-report daily.sh).
_UPLOAD_FILES = ["users.db", "oauth_state.db"]

# Sync interval in seconds
_SYNC_INTERVAL = int(os.environ.get("GCS_SYNC_INTERVAL", "300"))  # 5 minutes


def _get_config() -> tuple[str, str, Path]:
    """Return (bucket, prefix, cache_dir) from environment variables."""
    bucket = os.environ.get("GCS_BUCKET", "")
    if not bucket:
        logger.error("GCS_BUCKET environment variable is not set")
        sys.exit(1)

    prefix = os.environ.get("GCS_PREFIX", "jquants-dat-mcp/")
    # Ensure prefix ends with /
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    cache_dir_str = os.environ.get("JQUANTS_CACHE_DIR", "/tmp")
    cache_dir = Path(cache_dir_str)
    cache_dir.mkdir(parents=True, exist_ok=True)

    return bucket, prefix, cache_dir


def download_files(file_list: list[str] | None = None) -> None:
    """Download files from GCS to local cache dir.

    Args:
        file_list: List of filenames to download. Defaults to _DOWNLOAD_FILES.

    Missing objects are silently skipped (first-run case).
    """
    from google.cloud import storage  # type: ignore[import-untyped]
    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    files = file_list if file_list is not None else _DOWNLOAD_FILES
    bucket, prefix, cache_dir = _get_config()
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)

    for filename in files:
        blob_name = f"{prefix}{filename}"
        local_path = cache_dir / filename
        blob = gcs_bucket.blob(blob_name)
        try:
            blob.download_to_filename(str(local_path))
            size_mb = local_path.stat().st_size / 1024 / 1024
            logger.info(
                "Downloaded gs://%s/%s -> %s (%.1f MB)", bucket, blob_name, local_path, size_mb
            )
        except NotFound:
            logger.info("gs://%s/%s not found, skipping (first run?)", bucket, blob_name)
        except Exception as e:
            logger.warning("Failed to download %s: %s", blob_name, e)


def upload_files() -> None:
    """Upload local cache files to GCS.

    Files that do not exist locally are silently skipped.
    """
    from google.cloud import storage  # type: ignore[import-untyped]

    bucket, prefix, cache_dir = _get_config()
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)

    for filename in _UPLOAD_FILES:
        local_path = cache_dir / filename
        if not local_path.exists():
            logger.debug("Local file %s not found, skipping upload", local_path)
            continue

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

    parser = argparse.ArgumentParser(description="GCS cache sync utility for jquants-dat-mcp")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--init", action="store_true", help="Download auth DBs from GCS (users.db, oauth_state.db)"
    )
    group.add_argument("--daemon", action="store_true", help="Run background sync daemon")
    group.add_argument("--upload", action="store_true", help="Upload local cache to GCS and exit")
    args = parser.parse_args()

    if args.init:
        download_files()
    elif args.daemon:
        run_daemon()
    elif args.upload:
        upload_files()


if __name__ == "__main__":
    main()

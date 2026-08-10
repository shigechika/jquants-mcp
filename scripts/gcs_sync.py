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


_GENERATION_SIDECAR_NAME = "cache.db.generation.json"
_GENERATION_SIDECAR_VERSION = 1


def _generation_sidecar_path(cache_dir: Path) -> Path:
    """Return the generation-tracking sidecar path for the cache dir.

    Parallel in spirit to ``_sidecar_path`` in ``src/jquants_mcp/cache/store.py``
    (PR #578), but keyed on ``cache_dir`` rather than a specific db path: this
    sidecar tracks GCS object generation (whether a re-download is needed at
    all), a distinct, independent concern from that module's file-identity
    integrity cache (whether an already-downloaded file is structurally
    valid). No coupling is needed between the two.
    """
    return cache_dir / _GENERATION_SIDECAR_NAME


def _fetch_effective_generation(
    gcs_bucket, prefix: str
) -> tuple[str, int | None, int | None] | None:
    """Determine which GCS object download_cache_db would prefer right now
    (mirroring the zst-then-fallback precedence used by the download logic
    below), plus both objects' raw generations.

    Both generations are always returned -- not just the preferred object's
    -- so that if the preferred (zst) object's actual *download* fails at
    fetch time and the code falls back to plain, the fallback branch can
    record plain's already-observed generation directly, rather than
    reusing the zst branch's prediction. An earlier version of this
    function returned only the winning object's (source, generation) pair,
    and ``download_cache_db`` threaded that single predicted value through
    to whichever branch happened to run. When zst existed at generation G
    but the *download* of it failed for a transient, non-"missing" reason
    (a corrupt frame, a dropped connection mid-stream -- errors
    ``stream_download_zst`` swallows and reports as a plain ``False``), the
    code correctly fell back to plain but still recorded ``("zst", G)``.
    Because zst's existence and generation hadn't actually changed, every
    subsequent poll's pre-check kept agreeing with that mislabeled sidecar
    and skipping forever -- an indefinite staleness bug, not the bounded,
    self-healing TOCTOU race this design otherwise relies on (see
    ``download_cache_db``'s docstring). Recording the branch that actually
    ran, using the generation this function already observed for that
    specific object, closes the gap. See jquants-mcp#581 review round 3.

    An earlier design still (pre-round-2) checked both objects' generations
    independently and used both independently as the *skip* signal -- which
    could report "unchanged" even when the object actually used for content
    was stale relative to the OTHER, unused object's generation. That is
    still avoided here: the skip decision below only ever compares the
    *effective* source's generation, never both.

    Skips the ``cache.db.zst`` ``get_blob`` call when zstandard isn't
    importable (it can never be the effective source either way), mirroring
    ``stream_download_zst``'s own upfront import check.

    Returns None when neither object exists (nothing published yet).
    """
    try:
        import zstandard  # noqa: F401

        zstandard_available = True
    except ImportError:
        zstandard_available = False

    zst_gen: int | None = None
    if zstandard_available:
        zst_blob = gcs_bucket.get_blob(f"{prefix}cache.db.zst")
        zst_gen = zst_blob.generation if zst_blob is not None else None

    plain_blob = gcs_bucket.get_blob(f"{prefix}cache.db")
    plain_gen = plain_blob.generation if plain_blob is not None else None

    if zst_gen is None and plain_gen is None:
        return None

    effective_source = "zst" if zst_gen is not None else "plain"
    return effective_source, zst_gen, plain_gen


def _read_recorded_generation(cache_dir: Path) -> tuple[str, int] | None:
    """Best-effort read of the last-downloaded (source, generation) pair.

    Returns None on any read/parse failure, a version mismatch, or an
    unrecognized source -- so the caller falls through to an unconditional
    download. This sidecar is a pure optimization, never a source of truth
    for whether cache.db needs downloading. A version mismatch also covers
    the pre-review-round-2 two-key sidecar shape (``zst_generation``/
    ``plain_generation``, no ``version`` key): that old shape has neither a
    ``source`` nor a ``generation`` key, so it fails the version check (and
    would fail the source/generation checks too) -- a stale on-disk sidecar
    left over from an in-place upgrade is treated exactly like "no
    sidecar", forcing one harmless re-download rather than misreading an
    incompatible format.
    """
    import json

    try:
        data = json.loads(_generation_sidecar_path(cache_dir).read_text(encoding="utf-8"))
        if data.get("version") != _GENERATION_SIDECAR_VERSION:
            return None
        source = data.get("source")
        generation = data.get("generation")
        if source not in ("zst", "plain") or generation is None:
            return None
        return source, generation
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None


def _write_recorded_generation(cache_dir: Path, source: str, generation: int) -> None:
    """Best-effort atomic write of the last-downloaded (source, generation).

    tempfile.mkstemp (same directory, so os.replace stays same-filesystem and
    atomic) plus os.replace avoids a torn read from a concurrent reader. Every
    failure is swallowed: this sidecar only ever saves a redundant re-download,
    so a failed write must never surface as a download_cache_db failure.

    Broad ``except Exception`` (not just OSError) to match the precedent this
    sidecar is parallel to, ``_write_verified_sidecar`` in
    ``src/jquants_mcp/cache/store.py`` (PR #578): disk-full, read-only
    filesystem, or an unexpected serialization error must all be swallowed
    rather than surfaced.
    """
    import json
    import tempfile

    sidecar = _generation_sidecar_path(cache_dir)
    payload = {
        "version": _GENERATION_SIDECAR_VERSION,
        "source": source,
        "generation": generation,
    }
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(sidecar.parent), prefix=f".{sidecar.name}.", suffix=".tmp"
        )
    except Exception:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_name, sidecar)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _invalidate_recorded_generation(cache_dir: Path) -> None:
    """Best-effort removal of the generation sidecar.

    Called when a download succeeded but its true generation is unknown
    (the pre-check itself raised, so ``download_cache_db`` proceeded with an
    unconditional download rather than a generation-informed one -- see its
    docstring). Any pre-existing sidecar now describes content that this
    download just overwrote; leaving it in place would let a later tick,
    once the pre-check succeeds again, match that stale value against
    unrelated current GCS state and skip indefinitely even though the file
    on disk was never actually confirmed to match. Deleting it forces the
    next tick to re-evaluate from scratch instead. See jquants-mcp#581
    review round 4.

    Swallows every failure, same rationale as ``_write_recorded_generation``:
    a failed unlink must never surface as a download_cache_db failure.
    """
    try:
        _generation_sidecar_path(cache_dir).unlink(missing_ok=True)
    except Exception:
        pass


def download_cache_db() -> int:
    """Download cache.db from GCS, preferring the zstd-compressed object.

    Tries ``<prefix>cache.db.zst`` (stream-decompressed — far faster to transfer)
    and falls back to the uncompressed ``<prefix>cache.db`` when the compressed
    object is missing or zstandard is unavailable. This lets the publisher and
    Cloud Run roll out independently: until the publisher writes ``.zst`` the
    fallback keeps startup working unchanged.

    Skips the download entirely when the GCS generation of the object that
    would actually be used (mirroring the same zst-then-fallback precedence
    below -- see ``_fetch_effective_generation``) is unchanged since the
    last successful download and the local file is still present, avoiding a
    redundant transfer and the atomic replace that always allocates a new
    inode (which would invalidate ``CacheStore``'s integrity sidecar). See
    jquants-mcp#579, jquants-mcp#581.

    On Cloud Run this skip is effectively dormant: the periodic poller it was
    written for is gone (jquants-mcp#584) and a cold start always begins with
    an empty cache dir, so the local-file check fails and the download runs.
    It stays because it is still correct and load-bearing wherever the script
    IS re-run against a populated cache dir -- repeat ``--init-cache``
    invocations during development, and any future re-introduction of a
    periodic or push-triggered refresh.

    The generation pre-check is best-effort: if it fails (transient GCS
    error), the function falls through to an unconditional download instead
    of failing outright, so a metadata-only blip can never turn a healthy
    tick into a reported download failure. The actual content download keeps
    its own (unconditional) failure handling below.

    The sidecar is kept honest across four possible outcomes (jquants-mcp#581
    rounds 2-4):

    1. Pre-check succeeds, zst wins -> record ("zst", zst_gen).
    2. Pre-check succeeds, plain wins -> record ("plain", plain_gen).
    3. Pre-check raises, either branch succeeds anyway (unconditional
       download) -> the true generation is unknown, so any pre-existing
       sidecar is invalidated (deleted) rather than left describing content
       this download just overwrote (see ``_invalidate_recorded_generation``).
    4. Nothing downloaded (skip, both objects missing, or a genuine download
       failure) -> the sidecar is left untouched, since it still accurately
       describes whatever is on disk.

    Each success branch records only the generation it observed for *that
    specific object* -- never a value predicted for a different branch. This
    is deliberate: see ``_fetch_effective_generation``'s docstring for the
    mislabeling bug this avoids (jquants-mcp#581 round 3).

    Returns:
        The number of failures (0 on success, an unchanged-generation skip, or
        a first-run missing object, 1 on a genuine download failure) so
        ``--init-cache`` can map it to the alert.
    """
    from google.cloud import storage  # type: ignore[import-untyped]
    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    from jquants_mcp.cache.gcs_download import stream_download_zst

    bucket, prefix, cache_dir = _get_config()
    client = storage.Client()
    gcs_bucket = client.bucket(bucket)

    local_path = cache_dir / "cache.db"
    tmp_path = cache_dir / ".cache.db.download"

    zst_gen: int | None = None
    plain_gen: int | None = None
    try:
        effective = _fetch_effective_generation(gcs_bucket, prefix)
    except Exception as e:
        # Broad on purpose, matching this file's other GCS call sites: a
        # metadata-only pre-check blip must not abort the content download
        # below. Fall through to an unconditional download instead so a
        # transient get_blob error can never turn a healthy tick into a
        # reported "download failed" (see docstring above, jquants-mcp#579).
        logger.warning("Failed to check cache.db generation on GCS: %s", e)
    else:
        if effective is None:
            logger.info("gs://%s/%scache.db(.zst) not found, skipping (first run?)", bucket, prefix)
            return 0
        effective_source, zst_gen, plain_gen = effective
        effective_generation = zst_gen if effective_source == "zst" else plain_gen
        if local_path.exists() and _read_recorded_generation(cache_dir) == (
            effective_source,
            effective_generation,
        ):
            logger.info(
                "cache.db generation unchanged (%s=%s), skipping re-download",
                effective_source,
                effective_generation,
            )
            return 0

    # 1. Preferred: compressed cache.db.zst, stream-decompressed.
    if stream_download_zst(gcs_bucket, f"{prefix}cache.db.zst", tmp_path):
        tmp_path.replace(local_path)
        size_mb = local_path.stat().st_size / 1024 / 1024
        logger.info(
            "Downloaded gs://%s/%scache.db.zst -> %s (%.1f MB decompressed)",
            bucket,
            prefix,
            local_path,
            size_mb,
        )
        if zst_gen is not None:
            _write_recorded_generation(cache_dir, "zst", zst_gen)
        else:
            # zst_gen is only None here if the pre-check itself raised (a
            # successful pre-check that found zst genuinely absent, or
            # zstandard unavailable, would have made stream_download_zst
            # fail identically and never reach this branch). We don't know
            # the true generation of what was just downloaded, so any
            # pre-existing sidecar is now stale -- see
            # _invalidate_recorded_generation's docstring.
            _invalidate_recorded_generation(cache_dir)
        return 0

    # 2. Fallback: uncompressed cache.db.
    blob = gcs_bucket.blob(f"{prefix}cache.db")
    try:
        blob.download_to_filename(str(tmp_path))
        tmp_path.replace(local_path)
        size_mb = local_path.stat().st_size / 1024 / 1024
        logger.info(
            "Downloaded gs://%s/%scache.db -> %s (%.1f MB)", bucket, prefix, local_path, size_mb
        )
        if plain_gen is not None:
            _write_recorded_generation(cache_dir, "plain", plain_gen)
        else:
            # Same reasoning as the zst branch above: plain_gen is only
            # None here if the pre-check raised (a successful pre-check
            # finding plain genuinely absent would have made this
            # download_to_filename call raise NotFound instead of
            # succeeding).
            _invalidate_recorded_generation(cache_dir)
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

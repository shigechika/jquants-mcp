#!/usr/bin/env python3
"""Cache integrity pre-warm CLI for jquants-mcp Cloud Run deployments.

claude.ai's Web UI re-initializes its MCP session on every message, and
mcp-stdio serve's per-user model spawns a fresh jquants-mcp child process for
each one. Every fresh child pays for CacheStore's connect-time
``PRAGMA quick_check`` against the ~3GB cache.db (4-6 seconds), so
``health_check`` was effectively always reporting ``cache_integrity: pending``
even though the DB itself was fine. CacheStore now short-circuits that check
when a sidecar file records a prior "verified" (st_dev, st_ino) generation
of the same cache.db. This script runs that same check standalone, ahead of
any request, so the sidecar is already warm by the time the next child
process connects (see the two call sites below).

Called from scripts/entrypoint-stdio.sh, backgrounded right after the
synchronous startup download, so it does not delay ``mcp-stdio serve``
binding its port. Since that download gives cache.db a fresh inode, this
is what makes the very first per-message child process report
``cache_integrity: ok`` immediately.

Previously also chained after ``gcs_sync.py --init-cache`` on a 15-minute
supercronic poll (scripts/cache-poll.crontab), removed in jquants-mcp#584
along with the poll itself: with min-instances=0 every cold start already
re-downloads a current cache.db, so the poll spent a full quick_check on
all 96 daily ticks to cover only the narrow case of an instance staying
warm across the publisher's once-a-weekday export.

Usage:
    python verify_cache.py

Environment variables:
    JQUANTS_CACHE_DIR  Local cache directory (default: /tmp) — the same
                        variable gcs_sync.py uses to locate cache.db.

Exit codes:
    0   No cache.db yet (routine first-run / pre-download state, matching
        CacheStore's own constructor exists-guard), or the check ran and
        produced any status other than "error: ..." (including "ok" and a
        genuinely failed "failed: ..." integrity verdict — that is a real,
        cacheable result, not an environmental problem to page on).
    1   verify_and_record() raised, or it returned an "error: ..." status
        (a transient/environmental failure, e.g. the DB could not be opened
        at all) — surfaced in the container log rather than exiting silently.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("verify_cache")


def _resolve_db_path() -> Path:
    """Return the local cache.db path.

    Mirrors gcs_sync.py's ``_get_config()`` cache-dir resolution
    (``JQUANTS_CACHE_DIR``, default ``/tmp``) without its GCS_BUCKET
    requirement — this script never talks to GCS, it only inspects a file
    gcs_sync.py (or a prior run of this script) already put in place.
    """
    cache_dir = Path(os.environ.get("JQUANTS_CACHE_DIR", "/tmp"))
    return cache_dir / "cache.db"


def main() -> int:
    """CLI entry point. Returns a process exit code."""
    db_path = _resolve_db_path()

    if not db_path.exists():
        # Matches CacheStore's constructor exists-guard: no cache.db yet is a
        # routine state (pre-download, or --init-cache's own NotFound
        # "first run" case, which itself exits 0). entrypoint-stdio.sh runs
        # this unconditionally after that download, so treating "missing" as
        # an error here would turn a legitimately cache-less deployment
        # (GCS_BUCKET unset, live-API-only) into a startup alarm.
        logger.info("%s does not exist yet, skipping verification", db_path)
        return 0

    # Imported lazily (mirroring gcs_sync.py's own lazy imports) and only
    # once we know there is something to verify.
    from jquants_mcp.cache.store import INTEGRITY_ERROR_PREFIX, verify_and_record

    try:
        status = verify_and_record(db_path)
    except Exception:
        # An exception here means the shared verification path itself broke
        # rather than producing a normal "error: ..." status string — that is
        # exactly the kind of genuine environmental problem the exit code
        # should surface, not the routine "no cache yet" case handled above.
        logger.exception("verify_and_record raised for %s", db_path)
        return 1

    logger.info("cache integrity verification for %s: %s", db_path, status)

    if status.startswith(INTEGRITY_ERROR_PREFIX):
        # "error: ..." is the transient/environmental bucket (see
        # store.py's INTEGRITY_ERROR_PREFIX docstring) — surface it as a
        # non-zero exit. A "failed: ..." result is a real, cacheable
        # integrity verdict (the check ran to completion and the DB is
        # genuinely corrupt); it is intentionally NOT treated as a script
        # failure here so routine "cron surfaces genuine environmental
        # problems" alerting does not fire on a finding the sidecar itself
        # exists to record and report accurately to the next child process.
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

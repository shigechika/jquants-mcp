"""Tests for cache.db background integrity check (#71)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from jquants_mcp.cache.store import CacheStore


def _wait_for(pred, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


def test_integrity_ok_on_healthy_db(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache.db")
    # Connecting triggers the background quick_check.
    store._ensure_connection()
    _wait_for(lambda: store.integrity_status != "pending")
    assert store.integrity_status == "ok"


def test_integrity_reported_in_status(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache.db")
    stats = store.status()
    # Check completes quickly on a fresh tiny db; but "pending" is also valid.
    assert stats["integrity"] in {"ok", "pending"}
    _wait_for(lambda: store.integrity_status == "ok")
    assert store.status()["integrity"] == "ok"


def test_integrity_default_before_connection(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache.db")
    # No _ensure_connection() call yet.
    assert store.integrity_status == "not-checked"


def test_integrity_kicked_off_on_init_when_async_flag_set(tmp_path: Path) -> None:
    # Issue #156 follow-up — passing ``check_integrity_async=True`` at
    # construction time means callers that read ``integrity_status``
    # without first opening a connection (notably ``health_check``) see
    # ``"pending"`` / ``"ok"`` instead of ``"not-checked"``.
    db_path = tmp_path / "cache.db"
    # Create the file so the kick-off path takes effect (the flag is
    # ignored when the path doesn't exist yet, e.g. Cloud Run cold start
    # before GCS copy completes).
    sqlite3.connect(str(db_path)).close()

    store = CacheStore(db_path, check_integrity_async=True)
    # Right after init, the status should already be "pending" or "ok"
    # (small db completes the check almost instantly).
    assert store.integrity_status in {"ok", "pending"}
    _wait_for(lambda: store.integrity_status == "ok")
    assert store.integrity_status == "ok"


def test_integrity_async_flag_skipped_when_db_missing(tmp_path: Path) -> None:
    # When the cache.db file doesn't exist yet (e.g. Cloud Run cold start
    # mid-GCS-copy), the kick-off path should be a no-op rather than
    # spawning a thread that tries to open a non-existent file.
    db_path = tmp_path / "missing.db"
    assert not db_path.exists()

    store = CacheStore(db_path, check_integrity_async=True)
    # Status stays at the default — no thread was started.
    assert store.integrity_status == "not-checked"
    assert store._integrity_thread is None


def test_integrity_detects_corruption(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    # Create a valid db first, then clobber the header.
    sqlite3.connect(str(db_path)).close()
    with open(db_path, "r+b") as f:
        f.seek(24)
        f.write(b"\x00" * 32)  # scramble page-size / change-counter region

    store = CacheStore(db_path)
    conn = store._ensure_connection()
    # _ensure_connection may itself fail on a corrupted header — if so, the
    # integrity thread never starts. That outcome is also acceptable: the
    # corruption is visible via store.ready == False.
    if conn is None:
        assert store.ready is False
        return

    _wait_for(lambda: store.integrity_status not in {"pending", "not-checked"})
    assert store.integrity_status != "ok"


@pytest.mark.skipif(True, reason="interactive debug aid, kept for local use")
def test_integrity_timing(tmp_path: Path) -> None:  # pragma: no cover
    """Measure quick_check wall time — useful when tuning the approach."""
    store = CacheStore(tmp_path / "cache.db")
    store._ensure_connection()
    t0 = time.monotonic()
    _wait_for(lambda: store.integrity_status != "pending")
    print(f"quick_check took {time.monotonic() - t0:.3f}s")

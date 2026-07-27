"""Tests for cache.db background integrity check (#71)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from jquants_mcp.cache import store
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


def test_failed_prefix_is_actually_produced(tmp_path: Path, monkeypatch) -> None:
    """Actually take the path where quick_check returns something other than ok.

    test_integrity_detects_corruption asserts only `!= "ok"`, and its
    header-scrambling setup has a branch that returns early when
    _ensure_connection itself fails — so the producer writing
    `failed: <detail>` had never run. The docstring promises that prefix, so
    pin that it is really produced.
    """
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    real_connect = sqlite3.connect

    class _Cursor:
        def fetchone(self):
            return ("*** in database main ***\nPage 3 is never used",)

    class _Proxy:
        """sqlite3.Connection attributes are read-only, so wrap instead."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            if "quick_check" in sql:
                return _Cursor()
            return self._conn.execute(sql, *a, **k)

        def close(self):
            self._conn.close()

    def _fake_connect(*args, **kwargs):
        return _Proxy(real_connect(*args, **kwargs))

    # Do not call _ensure_connection(): it starts the real quick_check thread
    # first, and _start_integrity_check() then returns early because a thread is
    # already alive. The probe opens its own connection, so db_path is enough.
    store_obj = CacheStore(db_path)
    monkeypatch.setattr(sqlite3, "connect", _fake_connect)
    store_obj._start_integrity_check()

    _wait_for(lambda: store_obj.integrity_status not in {"pending", "not-checked"})
    assert store_obj.integrity_status.startswith(store.INTEGRITY_FAILED_PREFIX)
    assert store.integrity_is_failure(store_obj.integrity_status)


def test_error_prefix_is_actually_produced(tmp_path: Path, monkeypatch) -> None:
    """Take the path where opening the probe connection itself raises.

    This is the fifth documented form, and the one the docstring had omitted
    entirely before this change.
    """
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    store_obj = CacheStore(db_path)

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _boom)
    store_obj._start_integrity_check()

    _wait_for(lambda: store_obj.integrity_status not in {"pending", "not-checked"})
    assert store_obj.integrity_status.startswith(store.INTEGRITY_ERROR_PREFIX)
    assert store.integrity_is_failure(store_obj.integrity_status)


def test_every_documented_state_has_a_scenario() -> None:
    """A mechanical reminder that a new state needs a new scenario.

    Weak bookkeeping on its own, but it is what closes the loop between the
    docstring, the constants, and the producers that actually run.
    """
    covered = {
        store.INTEGRITY_NOT_CHECKED,  # test_integrity_default_before_connection
        store.INTEGRITY_PENDING,  # test_integrity_kicked_off_on_init_when_async_flag_set
        store.INTEGRITY_OK,  # test_integrity_ok_on_healthy_db
        store.INTEGRITY_FAILED_PREFIX,  # test_failed_prefix_is_actually_produced
        store.INTEGRITY_ERROR_PREFIX,  # test_error_prefix_is_actually_produced
    }
    assert covered == set(store.INTEGRITY_STATES)

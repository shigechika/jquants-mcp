"""Tests for cache.db background integrity check (#71)."""

from __future__ import annotations

import json
import os
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


# --- Verified-sidecar cache (mtime/size-blind, keyed on (dev, ino)) --------
#
# These cover the "verify once per file generation, not once per per-message
# child process" optimization described in _start_integrity_check's
# docstring: mcp-stdio serve's per-user child model spawns a fresh
# CacheStore on every claude.ai Web UI message, and without the sidecar every
# one of those pays for a full multi-second PRAGMA quick_check against an
# unchanged multi-GB file.


def test_sidecar_written_after_successful_check(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()
    sidecar = store._sidecar_path(db_path)

    store_obj = CacheStore(db_path, check_integrity_async=True)
    # Wait for the sidecar FILE itself, not just integrity_status == "ok":
    # the write happens as a side effect inside verify_and_record, strictly
    # before _run() assigns self._integrity_status — so the file can already
    # exist while the in-memory flag is still "pending". Poll both
    # independently rather than assuming either order.
    _wait_for(sidecar.exists)
    _wait_for(lambda: store_obj.integrity_status == store.INTEGRITY_OK)

    data = json.loads(sidecar.read_text())
    assert data["status"] == store.INTEGRITY_OK
    st = db_path.stat()
    assert (data["dev"], data["ino"]) == (st.st_dev, st.st_ino)


def test_second_store_same_inode_fast_paths_without_a_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    store1 = CacheStore(db_path, check_integrity_async=True)
    _wait_for(lambda: store1.integrity_status == store.INTEGRITY_OK)
    _wait_for(store._sidecar_path(db_path).exists)

    # A second store built against the exact same file (same inode) should
    # see the recorded verdict immediately, without spawning a background
    # quick_check thread at all — the "is_alive" guard is not what skips it,
    # the sidecar match is.
    store2 = CacheStore(db_path, check_integrity_async=True)
    assert store2.integrity_status == store.INTEGRITY_OK
    assert store2._integrity_thread is None


def test_replacing_file_invalidates_sidecar(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    store1 = CacheStore(db_path, check_integrity_async=True)
    _wait_for(lambda: store1.integrity_status == store.INTEGRITY_OK)
    # Windows CI safety: do not call _ensure_connection() here — it would
    # hold an open sqlite3.Connection on db_path, and os.replace() can raise
    # PermissionError on Windows while a target-file handle is open.
    # check_integrity_async=True construction is enough to drive the
    # verification; wait for the background thread to fully exit (it opens
    # and closes its own probe connection) before replacing the file.
    _wait_for(lambda: not (store1._integrity_thread and store1._integrity_thread.is_alive()))

    replacement = tmp_path / "cache_new.db"
    sqlite3.connect(str(replacement)).close()
    # Simulates an atomic GCS download swap: always a rename onto db_path,
    # which always allocates a new inode.
    os.replace(replacement, db_path)

    store2 = CacheStore(db_path, check_integrity_async=True)
    # The stale sidecar still names the old (dev, ino); a fresh full
    # recheck must run rather than short-circuiting off it.
    assert store2._integrity_thread is not None
    _wait_for(lambda: store2.integrity_status == store.INTEGRITY_OK)


def test_malformed_sidecar_json_falls_through_to_full_recheck(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()
    store._sidecar_path(db_path).write_text("{not valid json")

    # Pin the rejection at the reader itself, independent of whether a
    # background thread happens to spawn -- a refactor that dropped sidecar
    # reading entirely would still spawn a thread and pass the assertions
    # below, so this line is what actually proves the malformed content was
    # read-and-rejected rather than merely ignored.
    assert store._read_verified_sidecar(db_path) is None

    store_obj = CacheStore(db_path, check_integrity_async=True)
    # A sidecar that fails to parse must not be trusted -- a real
    # background thread should have been spawned instead of a fast path.
    assert store_obj._integrity_thread is not None
    _wait_for(lambda: store_obj.integrity_status == store.INTEGRITY_OK)


def test_non_utf8_sidecar_falls_through_to_full_recheck(tmp_path: Path) -> None:
    """A sidecar that isn't valid UTF-8 must degrade like any other malformed file.

    ``Path.read_text()`` raises ``UnicodeDecodeError`` (a ``ValueError``
    subtype, not an ``OSError``) on undecodable bytes, which
    ``_read_verified_sidecar`` must catch alongside ``OSError`` -- otherwise
    a hand-corrupted sidecar crashes the caller instead of degrading to a
    fresh quick_check. The existing malformed-JSON test above does not cover
    this: invalid JSON is still valid UTF-8, so it never exercises the
    decode step at all.
    """
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()
    store._sidecar_path(db_path).write_bytes(b"\xff\xfe\x00\x01not utf-8")

    assert store._read_verified_sidecar(db_path) is None

    store_obj = CacheStore(db_path, check_integrity_async=True)
    assert store_obj._integrity_thread is not None
    _wait_for(lambda: store_obj.integrity_status == store.INTEGRITY_OK)


def test_in_place_corruption_same_inode_short_circuits_via_sidecar(tmp_path: Path) -> None:
    """Documents an accepted trade-off, not a bug.

    The sidecar answers "did a GCS replacement swap in a different file
    generation?" (keyed on (dev, ino)), not "is the file's current content
    still healthy?". In-place byte corruption that leaves the inode alone —
    as done here — therefore still matches the sidecar and short-circuits to
    the stale "ok" verdict instead of re-running quick_check. In production
    this scenario does not arise: verify_and_record's docstring establishes
    that every real replacement path is an atomic rename (GCS re-download),
    which always allocates a new inode and so always invalidates the
    sidecar on its own. The sidecar was built to answer "did claude.ai just
    reconnect for the Nth time this conversation against an unchanged
    file?", not to serve as a tamper-detection mechanism for an external
    process scribbling into a live cache.db without going through a
    rename — that threat model was never in scope.
    """
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    store1 = CacheStore(db_path, check_integrity_async=True)
    _wait_for(lambda: store1.integrity_status == store.INTEGRITY_OK)
    _wait_for(store._sidecar_path(db_path).exists)

    # Corrupt in place -- same inode, no rename/replace involved. `connect()`
    # + `close()` never writes a SQLite header (the file is 0 bytes), so
    # seeking to 24 and writing 32 zero bytes does not "scramble" an
    # existing header -- it zero-pads the gap and leaves a 56-byte
    # all-zeros file. That is a corrupt (non-SQLite) file either way, which
    # is all this test needs: the point being demonstrated is that the
    # sidecar short-circuits *any* in-place same-inode change, not that
    # this particular corruption resembles a damaged real database.
    with open(db_path, "r+b") as f:
        f.seek(24)
        f.write(b"\x00" * 32)

    store2 = CacheStore(db_path, check_integrity_async=True)
    assert store2.integrity_status == store.INTEGRITY_OK
    assert store2._integrity_thread is None


def test_failed_status_is_cached_and_reused(tmp_path: Path, monkeypatch) -> None:
    """The ``failed: <detail>`` verdict is durable across store instances.

    Mirrors test_failed_prefix_is_actually_produced's fake-connection setup
    (same rationale: quick_check needs to actually return a non-"ok" row,
    not merely raise), then constructs a second store on the same file to
    confirm the sidecar served that verdict back immediately.
    """
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    real_connect = sqlite3.connect

    class _Cursor:
        def fetchone(self):
            return ("*** in database main ***\nPage 3 is never used",)

    class _Proxy:
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

    store_obj = CacheStore(db_path)
    monkeypatch.setattr(sqlite3, "connect", _fake_connect)
    store_obj._start_integrity_check()

    _wait_for(lambda: store_obj.integrity_status.startswith(store.INTEGRITY_FAILED_PREFIX))
    _wait_for(store._sidecar_path(db_path).exists)
    # Restore the real sqlite3.connect before building the next store: the
    # thing under test here is the sidecar fast path (which, on a match,
    # never calls sqlite3.connect at all), not the fake quick_check result.
    monkeypatch.undo()

    store2 = CacheStore(db_path, check_integrity_async=True)
    assert store2.integrity_status.startswith(store.INTEGRITY_FAILED_PREFIX)
    assert store2._integrity_thread is None


def test_error_status_is_never_written_to_sidecar(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    store_obj = CacheStore(db_path)

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _boom)
    store_obj._start_integrity_check()

    _wait_for(lambda: store_obj.integrity_status.startswith(store.INTEGRITY_ERROR_PREFIX))
    # "error: ..." means the check itself couldn't run (transient/
    # environmental), not that the DB is bad -- verify_and_record's
    # docstring is explicit that this form is never cached, so a later
    # store on the same file cannot see a false, frozen-in verdict.
    assert not store._sidecar_path(db_path).exists()


def test_request_reload_unlinks_sidecar(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    sqlite3.connect(str(db_path)).close()

    store_obj = CacheStore(db_path, check_integrity_async=True)
    _wait_for(lambda: store_obj.integrity_status == store.INTEGRITY_OK)
    sidecar = store._sidecar_path(db_path)
    _wait_for(sidecar.exists)

    store_obj.request_reload()
    assert not sidecar.exists()

"""Tests for scripts/verify_cache.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_cache


def test_missing_db_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """No cache.db yet (pre-download / first-run) is a routine state, not an error.

    Mirrors CacheStore's own constructor exists-guard, and matters
    operationally: entrypoint-stdio.sh runs this unconditionally after the
    startup download, so treating "missing" as a failure would turn a
    legitimately cache-less deployment (GCS_BUCKET unset, live-API-only)
    into a startup alarm.
    """
    monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
    assert not (tmp_path / "cache.db").exists()

    assert verify_cache.main() == 0

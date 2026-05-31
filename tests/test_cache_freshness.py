"""Tests for cache-staleness logging and the matching alert policy.

CacheStore logs the freshly-loaded cache's latest equities date on every
(re)connect, emitting a WARNING when it is more than a week behind today. The
phrase it emits must stay in lockstep with the Cloud Monitoring policy that
greps for it (ops/alerts/07-cache-stale.yaml) — a mismatch silently disables
the alert, which is exactly the bug that left 05 dead (PR #443). These tests
pin both halves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jquants_mcp.cache.schema import TIER1_TABLES, generate_ddl
from jquants_mcp.cache.store import (
    _CACHE_STALE_DAYS,
    _STALE_LOG_PHRASE,
    CacheStore,
    _cache_stale_message,
)

_ALERT_YAML = Path(__file__).resolve().parent.parent / "ops" / "alerts" / "07-cache-stale.yaml"


class TestStaleMessage:
    """The pure _cache_stale_message helper."""

    def test_fresh_within_tolerance_returns_none(self):
        # Same day, 1 day, and exactly the threshold are all considered fresh.
        assert _cache_stale_message("2026-05-31", "2026-05-31", _CACHE_STALE_DAYS) is None
        assert _cache_stale_message("2026-05-30", "2026-05-31", _CACHE_STALE_DAYS) is None
        assert _cache_stale_message("2026-05-24", "2026-05-31", _CACHE_STALE_DAYS) is None  # gap 7

    def test_beyond_tolerance_is_stale(self):
        msg = _cache_stale_message("2026-05-23", "2026-05-31", _CACHE_STALE_DAYS)  # gap 8
        assert msg is not None
        assert _STALE_LOG_PHRASE in msg
        assert "8 days behind" in msg

    def test_no_data_is_stale(self):
        msg = _cache_stale_message(None, "2026-05-31", _CACHE_STALE_DAYS)
        assert msg is not None
        assert _STALE_LOG_PHRASE in msg

    def test_does_not_fire_on_normal_pre_publish_lag(self):
        # A weekend (Fri close -> Mon morning, gap 3) must not page.
        assert _cache_stale_message("2026-05-29", "2026-06-01", _CACHE_STALE_DAYS) is None

    def test_malformed_date_returns_none(self):
        assert _cache_stale_message("not-a-date", "2026-05-31", _CACHE_STALE_DAYS) is None


def test_alert_filter_matches_the_emitted_phrase():
    """Gate against a dead alert: the YAML filter must contain the emitted phrase."""
    yaml_text = _ALERT_YAML.read_text(encoding="utf-8")
    assert f'textPayload:"{_STALE_LOG_PHRASE}"' in yaml_text
    # And a real stale message actually contains that substring.
    stale = _cache_stale_message("2000-01-01", "2026-05-31", _CACHE_STALE_DAYS)
    assert stale is not None and _STALE_LOG_PHRASE in stale


def _make_cache_db(path: Path, latest_date: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(generate_ddl("equities_bars_daily", TIER1_TABLES["equities_bars_daily"]))
    conn.execute(
        "INSERT INTO equities_bars_daily (code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
        ("72030", latest_date, "{}", 0.0),
    )
    conn.commit()
    conn.close()


class TestCacheStoreFreshnessLog:
    """The WARNING is emitted on connect for a stale cache, INFO for a fresh one."""

    def test_stale_cache_logs_warning_on_connect(self, tmp_path, caplog):
        db = tmp_path / "cache.db"
        _make_cache_db(db, "2000-01-01")  # ancient -> always stale
        store = CacheStore(db, default_plan="standard")
        with caplog.at_level("INFO"):
            assert store._ensure_connection() is not None
        assert _STALE_LOG_PHRASE in caplog.text
        store.close()

    def test_fresh_cache_logs_no_warning(self, tmp_path, caplog):
        from datetime import date

        db = tmp_path / "cache.db"
        _make_cache_db(db, date.today().isoformat())  # today -> fresh
        store = CacheStore(db, default_plan="standard")
        with caplog.at_level("INFO"):
            assert store._ensure_connection() is not None
        assert _STALE_LOG_PHRASE not in caplog.text
        assert "cache.db freshness" in caplog.text
        store.close()

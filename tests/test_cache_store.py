"""Tests for SQLite cache store."""

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from jquants_mcp.cache.store import (
    CacheStore,
    _plan_date_bounds,
    make_cache_key,
)


class TestMakeCacheKey:
    """make_cache_key のテスト。"""

    def test_endpoint_only(self):
        assert make_cache_key("/equities/master") == "/equities/master"

    def test_with_params(self):
        key = make_cache_key("/equities/bars/daily", {"code": "72030", "date": "2024-01-01"})
        assert key == "/equities/bars/daily|code=72030&date=2024-01-01"

    def test_params_sorted(self):
        """パラメータはキー名でソートされること。"""
        key1 = make_cache_key("/test", {"b": "2", "a": "1"})
        key2 = make_cache_key("/test", {"a": "1", "b": "2"})
        assert key1 == key2

    def test_none_params_ignored(self):
        key = make_cache_key("/test", {"a": "1", "b": None})
        assert key == "/test|a=1"

    def test_all_none_params_no_trailing_pipe(self):
        """All-None params must not produce a trailing | (regression for short_sale_report)."""
        key = make_cache_key("/markets/short-sale-report", {"code": None, "disc_date": None})
        assert key == "/markets/short-sale-report"

    def test_empty_dict_same_as_no_params(self):
        assert make_cache_key("/test", {}) == make_cache_key("/test")


class TestTier1RowCache:
    """Tier 1 行レベルキャッシュのテスト。"""

    def test_put_and_get_rows(self, cache_store: CacheStore):
        rows = [
            {"Code": "72030", "Date": "2024-01-04", "O": 2800, "C": 2850, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-05", "O": 2850, "C": 2900, "AdjFactor": 1.0},
        ]
        count = cache_store.put_rows(
            "equities_bars_daily",
            rows,
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )
        assert count == 2

        result = cache_store.get_rows(
            "equities_bars_daily",
            key_filter={"code": "72030"},
            date_from="2024-01-04",
            date_to="2024-01-05",
        )
        assert len(result) == 2
        assert result[0]["O"] == 2800
        assert result[1]["O"] == 2850

    def test_get_cached_dates(self, cache_store: CacheStore):
        rows = [
            {"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-05", "O": 200, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-08", "O": 300, "AdjFactor": 1.0},
        ]
        cache_store.put_rows(
            "equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor"
        )

        dates = cache_store.get_cached_dates(
            "equities_bars_daily",
            key_filter={"code": "72030"},
            date_from="2024-01-01",
            date_to="2024-01-10",
        )
        assert dates == {"2024-01-04", "2024-01-05", "2024-01-08"}

    def test_invalidate_rows(self, cache_store: CacheStore):
        rows = [
            {"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-05", "O": 200, "AdjFactor": 1.0},
        ]
        cache_store.put_rows(
            "equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor"
        )

        deleted = cache_store.invalidate_rows("equities_bars_daily", {"code": "72030"})
        assert deleted == 2

        result = cache_store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 0

    def test_check_adj_factor_no_split(self, cache_store: CacheStore):
        rows = [{"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0}]
        cache_store.put_rows(
            "equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        assert cache_store.check_adj_factor("72030", 1.0) is True

    def test_check_adj_factor_split_detected(self, cache_store: CacheStore):
        rows = [{"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0}]
        cache_store.put_rows(
            "equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        # 株式分割: AdjFactor が変わった
        assert cache_store.check_adj_factor("72030", 0.1) is False

    def test_upsert_overwrites(self, cache_store: CacheStore):
        """同じキーで INSERT すると上書きされること。"""
        rows1 = [{"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0}]
        rows2 = [{"Code": "72030", "Date": "2024-01-04", "O": 999, "AdjFactor": 1.0}]
        cache_store.put_rows(
            "equities_bars_daily", rows1, ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        cache_store.put_rows(
            "equities_bars_daily", rows2, ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        result = cache_store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 1
        assert result[0]["O"] == 999


class TestTier2ResponseCache:
    """Tier 2 レスポンスレベルキャッシュのテスト。"""

    def test_put_and_get_response(self, cache_store: CacheStore):
        data = {"count": 1, "data": [{"Date": "2024-01-04"}]}
        cache_store.put_response("test|key", data, ttl_seconds=3600)

        result = cache_store.get_response("test|key")
        assert result is not None
        assert result["count"] == 1

    def test_expired_response_returns_none(self, cache_store: CacheStore):
        data = {"expired": True}
        cache_store.put_response("old|key", data, ttl_seconds=1)

        # TTL を強制的に過去にする
        conn = cache_store._ensure_connection()
        conn.execute(
            "UPDATE response_cache SET fetched_at = ? WHERE cache_key = ?",
            (time.time() - 10, "old|key"),
        )
        conn.commit()

        result = cache_store.get_response("old|key")
        assert result is None

    def test_ttl_none_not_cached(self, cache_store: CacheStore):
        """TTL_NONE (0) の場合はキャッシュしない。"""
        cache_store.put_response("no-cache|key", {"data": 1}, ttl_seconds=0)
        result = cache_store.get_response("no-cache|key")
        assert result is None


class TestPlanIsolation:
    """Plan-based access control tests.

    Tier 1 reads do NOT filter by plan column — date restrictions
    from _plan_date_bounds are the sole access control mechanism.
    Tier 2 response cache keys still include plan suffix for isolation.
    """

    def test_tier1_free_restricted_by_date(self, tmp_path: Path):
        """Free plan cannot see data outside its date window, even if cached."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        old_date = (date.today() - timedelta(days=365 * 3)).isoformat()
        rows = [{"Code": "72030", "Date": old_date, "O": 999, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        # Standard can see it (10-year window)
        assert len(store.get_rows("equities_bars_daily", {"code": "72030"})) == 1

        # Free cannot see it (2-year window)
        result = store.get_rows("equities_bars_daily", {"code": "72030"}, plan="free")
        assert len(result) == 0, "Free plan must not see data outside 2-year window"
        store.close()

    def test_tier1_different_plans_share_cache(self, tmp_path: Path):
        """All plans can read the same cached data within their date window."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        recent_date = (date.today() - timedelta(days=365)).isoformat()
        rows = [{"Code": "72030", "Date": recent_date, "O": 500, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        # All plans can see recent data
        assert len(store.get_rows("equities_bars_daily", {"code": "72030"}, plan="free")) == 1
        assert len(store.get_rows("equities_bars_daily", {"code": "72030"}, plan="light")) == 1
        assert len(store.get_rows("equities_bars_daily", {"code": "72030"}, plan="standard")) == 1
        assert len(store.get_rows("equities_bars_daily", {"code": "72030"}, plan="premium")) == 1
        store.close()

    def test_tier2_shared_across_plans(self, tmp_path: Path):
        """Response cache entries are shared (no plan isolation)."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")

        cache_key = make_cache_key("/equities/master")
        store.put_response(cache_key, {"data": "shared"}, ttl_seconds=3600)

        assert store.get_response(cache_key)["data"] == "shared"
        store.close()

    def test_tier1_invalidate_removes_rows(self, tmp_path: Path):
        """invalidate_rows removes rows."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        store.put_rows(
            "equities_bars_daily",
            [{"Code": "72030", "Date": "2025-01-04", "O": 1, "AdjFactor": 1.0}],
            ["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        deleted = store.invalidate_rows("equities_bars_daily", {"code": "72030"})
        assert deleted == 1
        assert len(store.get_rows("equities_bars_daily", {"code": "72030"})) == 0
        store.close()

    def test_migration_drops_plan_column(self, tmp_path: Path):
        """Tables with plan column are migrated to drop it."""
        db_path = tmp_path / "cache.db"

        import sqlite3

        conn = sqlite3.connect(str(db_path))
        # Old schema with plan in PK
        conn.execute(
            "CREATE TABLE equities_bars_daily "
            "(code TEXT NOT NULL, date TEXT NOT NULL, "
            "plan TEXT NOT NULL DEFAULT 'free', "
            "data TEXT NOT NULL, fetched_at REAL NOT NULL, "
            "adj_factor REAL, PRIMARY KEY (code, date, plan))"
        )
        # Same row stored under two plans
        conn.execute(
            "INSERT INTO equities_bars_daily VALUES "
            "('72030', '2024-01-04', 'free', '{\"O\":1}', 0, 1.0)"
        )
        conn.execute(
            "INSERT INTO equities_bars_daily VALUES "
            "('72030', '2024-01-04', 'standard', '{\"O\":999}', 1.0, 1.0)"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        # CacheStore triggers migration
        store = CacheStore(db_path, default_plan="standard")
        store._ensure_connection()

        conn2 = store._ensure_connection()
        # plan column should be gone
        cols = [c[1] for c in conn2.execute("PRAGMA table_info(equities_bars_daily)").fetchall()]
        assert "plan" not in cols

        # Deduplicated: only 1 row, highest plan (standard) wins
        count = conn2.execute("SELECT COUNT(*) FROM equities_bars_daily").fetchone()[0]
        assert count == 1

        import json

        row = conn2.execute("SELECT data FROM equities_bars_daily WHERE code = '72030'").fetchone()
        assert json.loads(row[0])["O"] == 999

        assert conn2.execute("PRAGMA user_version").fetchone()[0] >= 2
        store.close()


class TestCacheUtility:
    """キャッシュユーティリティのテスト。"""

    def test_status(self, cache_store: CacheStore):
        status = cache_store.status()
        assert "equities_bars_daily" in status
        assert "response_cache" in status
        assert "db_size_mb" in status
        # Standard plan: markets_breakdown requires Premium -> None
        assert status["markets_breakdown"] is None
        # Standard plan: equities_bars_daily is available -> int
        assert isinstance(status["equities_bars_daily"], int)

    def test_status_no_plan_restriction_when_plan_empty(self, tmp_path: Path):
        """status() shows all table counts when plan is empty (auto-detect pending)."""
        store = CacheStore(tmp_path / "empty_plan.db", default_plan="")
        status = store.status()
        # All tables should have integer counts, not None
        for table in ("equities_bars_daily", "markets_breakdown", "markets_margin_interest"):
            assert isinstance(status[table], int), f"{table} should be int, got {status[table]}"
        store.close()

    def test_status_response_cache_excludes_expired(self, cache_store: CacheStore):
        """status() counts only non-expired response_cache entries."""
        # Insert an already-expired entry (fetched 2 hours ago, TTL 1 hour)
        cache_store.put_response("expired_key", {"data": 1}, ttl_seconds=3600)
        conn = cache_store._ensure_connection()
        conn.execute(
            "UPDATE response_cache SET fetched_at = ? WHERE cache_key = ?",
            (time.time() - 7200, "expired_key"),
        )
        conn.commit()
        # Insert a fresh entry
        cache_store.put_response("fresh_key", {"data": 2}, ttl_seconds=3600)

        status = cache_store.status()
        assert status["response_cache"] == 1  # only fresh_key counted

    def test_status_evicts_expired_entries(self, cache_store: CacheStore):
        """status() evicts expired response_cache entries."""
        cache_store.put_response("old_key", {"data": 1}, ttl_seconds=3600)
        conn = cache_store._ensure_connection()
        conn.execute(
            "UPDATE response_cache SET fetched_at = ? WHERE cache_key = ?",
            (time.time() - 7200, "old_key"),
        )
        conn.commit()

        cache_store.status()  # triggers eviction

        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM response_cache WHERE cache_key = ?",
            ("old_key",),
        ).fetchone()
        assert row["cnt"] == 0

    def test_clear_all(self, cache_store: CacheStore):
        rows = [{"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0}]
        cache_store.put_rows(
            "equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        cache_store.put_response("key", {"data": 1}, ttl_seconds=3600)

        result = cache_store.clear()
        assert result["equities_bars_daily"] == 1
        assert result["response_cache"] == 1

    def test_clear_specific_table(self, cache_store: CacheStore):
        rows = [{"Code": "72030", "Date": "2024-01-04", "O": 100, "AdjFactor": 1.0}]
        cache_store.put_rows(
            "equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        cache_store.put_response("key", {"data": 1}, ttl_seconds=3600)

        result = cache_store.clear("equities_bars_daily")
        assert result["equities_bars_daily"] == 1

        # response_cache はクリアされていない
        assert cache_store.get_response("key") is not None


class TestCorruptDatabase:
    """DB が壊れている場合（GCS コピー途中等）の graceful degradation テスト。"""

    def test_corrupt_db_returns_not_ready(self, tmp_path: Path):
        """Corrupt DB file causes store to enter not-ready state."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a valid sqlite database")

        store = CacheStore(db_path)
        assert store.ready is False
        assert store._ensure_connection() is None
        assert store.ready is False

    def test_corrupt_db_read_returns_empty(self, tmp_path: Path):
        """All read operations return empty results when DB is corrupt."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"corrupt data here")

        store = CacheStore(db_path)
        assert store.get_rows("equities_bars_daily", {"code": "72030"}) == []
        assert store.get_cached_dates("equities_bars_daily", {"code": "72030"}) == set()
        assert store.get_response("some_key") is None
        assert store.check_adj_factor("72030", 1.0) is True

    def test_corrupt_db_write_is_noop(self, tmp_path: Path):
        """All write operations silently skip when DB is corrupt."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"corrupt data here")

        store = CacheStore(db_path)
        assert (
            store.put_rows(
                "equities_bars_daily", [{"Code": "72030", "Date": "2024-01-04"}], ["Code", "Date"]
            )
            == 0
        )
        store.put_response("key", {"data": 1}, ttl_seconds=3600)  # no error
        assert store.invalidate_rows("equities_bars_daily", {"code": "72030"}) == 0

    def test_corrupt_db_status_shows_db_path(self, tmp_path: Path):
        """status() returns db_path even when DB is corrupt."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"corrupt data here")

        store = CacheStore(db_path)
        status = store.status()
        assert "db_path" in status

    def test_corrupt_db_clear_returns_empty(self, tmp_path: Path):
        """clear() returns empty dict when DB is corrupt."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"corrupt data here")

        store = CacheStore(db_path)
        assert store.clear() == {}

    def test_recovery_after_db_becomes_valid(self, tmp_path: Path):
        """Store recovers when corrupt DB is replaced with valid one."""
        db_path = tmp_path / "cache.db"
        db_path.write_bytes(b"corrupt data here")

        store = CacheStore(db_path)
        # リトライ間隔をバイパスするため _last_retry をリセット
        store._RETRY_INTERVAL = 0
        assert store.ready is False

        # 正常な DB に差し替え
        db_path.unlink()
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.close()

        # 再試行で復帰
        assert store._ensure_connection() is not None
        assert store.ready is True

    def test_retry_interval_prevents_frequent_retries(self, tmp_path: Path):
        """Retry interval prevents hammering a corrupt DB."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"corrupt data here")

        store = CacheStore(db_path)
        assert store._ensure_connection() is None  # 初回リトライ

        # リトライ間隔内 — 再試行しない
        assert store._ensure_connection() is None  # スキップされる

    def test_healthy_db_reports_ready(self, cache_store: CacheStore):
        """Normal DB is reported as ready after first access."""
        cache_store._ensure_connection()
        assert cache_store.ready is True

    def test_status_excludes_readonly(self, cache_store: CacheStore):
        """status() does not include readonly (removed after gcsfuse removal)."""
        status = cache_store.status()
        assert "readonly" not in status


class TestPlanDateBounds:
    """_plan_date_bounds のテスト。"""

    def test_premium_no_limits(self):
        min_d, max_d = _plan_date_bounds("premium")
        assert min_d is None
        assert max_d is None

    def test_standard_10_years(self):
        min_d, max_d = _plan_date_bounds("standard")
        assert min_d is not None
        assert max_d is None
        expected_year = date.today().year - 10
        assert min_d.startswith(str(expected_year))

    def test_light_5_years(self):
        min_d, max_d = _plan_date_bounds("light")
        assert min_d is not None
        assert max_d is None
        expected_year = date.today().year - 5
        assert min_d.startswith(str(expected_year))

    def test_free_2_years_with_delay(self):
        min_d, max_d = _plan_date_bounds("free")
        assert min_d is not None
        assert max_d is not None
        expected_year = date.today().year - 2
        assert min_d.startswith(str(expected_year))
        expected_max = (date.today() - timedelta(weeks=12)).isoformat()
        assert max_d == expected_max

    def test_unknown_plan_defaults_to_no_retention(self):
        """Unknown plan returns no limits (same as premium)."""
        min_d, max_d = _plan_date_bounds("unknown")
        assert min_d is None
        assert max_d is None

    def test_leap_year_boundary(self):
        """Feb 29 minus N years should not raise."""
        with patch("jquants_mcp.cache.store.date") as mock_date:
            mock_date.today.return_value = date(2024, 2, 29)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            min_d, _ = _plan_date_bounds("free")
            assert min_d is not None


class TestPlanDateRestrictionIntegration:
    """プラン別日付制限の統合テスト。"""

    def test_free_plan_restricts_old_data(self, tmp_path: Path):
        """Free plan cannot access data older than 2 years."""
        store = CacheStore(tmp_path / "cache.db", default_plan="free")
        old_date = (date.today() - timedelta(days=365 * 3)).isoformat()
        rows = [{"Code": "72030", "Date": old_date, "O": 100, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 0, "Free plan should not see data older than 2 years"
        store.close()

    def test_free_plan_restricts_recent_data(self, tmp_path: Path):
        """Free plan cannot access data within the 12-week delay window."""
        store = CacheStore(tmp_path / "cache.db", default_plan="free")
        recent_date = (date.today() - timedelta(weeks=4)).isoformat()
        rows = [{"Code": "72030", "Date": recent_date, "O": 100, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 0, "Free plan should not see data within 12-week delay"
        store.close()

    def test_free_plan_can_access_valid_window(self, tmp_path: Path):
        """Free plan can access data within its valid window."""
        store = CacheStore(tmp_path / "cache.db", default_plan="free")
        valid_date = (date.today() - timedelta(days=365)).isoformat()
        rows = [{"Code": "72030", "Date": valid_date, "O": 100, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 1, "Free plan should see data within valid window"
        store.close()

    def test_standard_plan_no_delay(self, tmp_path: Path):
        """Standard plan has no delay restriction."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        recent_date = (date.today() - timedelta(days=1)).isoformat()
        rows = [{"Code": "72030", "Date": recent_date, "O": 100, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 1
        store.close()

    def test_premium_plan_no_limits(self, tmp_path: Path):
        """Premium plan can access all data."""
        store = CacheStore(tmp_path / "cache.db", default_plan="premium")
        old_date = "2000-01-04"
        rows = [{"Code": "72030", "Date": old_date, "O": 100, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 1
        store.close()

    def test_caller_date_range_respected(self, tmp_path: Path):
        """Caller's date_from/date_to still work alongside plan restriction."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        rows = [
            {"Code": "72030", "Date": "2025-01-01", "O": 100, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2025-06-01", "O": 200, "AdjFactor": 1.0},
        ]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        result = store.get_rows(
            "equities_bars_daily",
            {"code": "72030"},
            date_from="2025-03-01",
            date_to="2025-12-31",
        )
        assert len(result) == 1
        assert result[0]["O"] == 200
        store.close()


class TestLegacyFieldNormalization:
    """get_rows() が旧形式フィールド名を新形式に変換するテスト。"""

    def test_legacy_fields_normalized(self, tmp_path: Path):
        """Legacy field names (Open, Close, etc.) are converted to short names."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        legacy_row = {
            "Code": "72030",
            "Date": "2024-01-04",
            "Open": 2800,
            "High": 2900,
            "Low": 2750,
            "Close": 2850,
            "Volume": 1000000,
            "TurnoverValue": 2850000,
            "AdjustmentOpen": 2800,
            "AdjustmentHigh": 2900,
            "AdjustmentLow": 2750,
            "AdjustmentClose": 2850,
            "AdjustmentVolume": 1000000,
            "AdjustmentFactor": 1.0,
            "UpperLimit": 3000,
            "LowerLimit": 2500,
        }
        store.put_rows(
            "equities_bars_daily", [legacy_row], ["Code", "Date"], adj_factor_key="AdjustmentFactor"
        )
        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 1
        row = result[0]
        assert row["O"] == 2800
        assert row["H"] == 2900
        assert row["L"] == 2750
        assert row["C"] == 2850
        assert row["Vo"] == 1000000
        assert row["Va"] == 2850000
        assert row["AdjO"] == 2800
        assert row["AdjC"] == 2850
        assert row["AdjFactor"] == 1.0
        assert row["UL"] == 3000
        assert row["LL"] == 2500
        # 旧名は残らない
        assert "Open" not in row
        assert "Close" not in row
        assert "AdjustmentFactor" not in row
        store.close()

    def test_current_fields_unchanged(self, tmp_path: Path):
        """Current short field names pass through without modification."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        current_row = {
            "Code": "72030",
            "Date": "2024-01-04",
            "O": 2800,
            "C": 2850,
            "AdjFactor": 1.0,
        }
        store.put_rows(
            "equities_bars_daily", [current_row], ["Code", "Date"], adj_factor_key="AdjFactor"
        )
        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        row = result[0]
        assert row["O"] == 2800
        assert row["C"] == 2850
        assert row["AdjFactor"] == 1.0
        assert row["Code"] == "72030"
        assert row["Date"] == "2024-01-04"
        store.close()

    def test_non_ohlc_fields_preserved(self, tmp_path: Path):
        """Non-OHLC fields like Code, Date are not affected by normalization."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        row = {
            "Code": "72030",
            "Date": "2024-01-04",
            "O": 100,
            "AdjFactor": 1.0,
            "CustomField": "x",
        }
        store.put_rows("equities_bars_daily", [row], ["Code", "Date"], adj_factor_key="AdjFactor")
        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert result[0]["CustomField"] == "x"
        assert result[0]["Code"] == "72030"
        store.close()

    def test_dual_fields_legacy_value_wins(self, tmp_path: Path):
        """When both legacy and current field names exist, non-empty value wins."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        # 実際のキャッシュデータ: 旧名に値あり、新名は空文字列
        row = {
            "Code": "72030",
            "Date": "2024-01-04",
            "Open": 3103,
            "High": 3104,
            "Low": 3000,
            "Close": 3011,
            "Volume": 41298500,
            "TurnoverValue": 125154283500,
            "AdjustmentFactor": 1,
            "AdjustmentOpen": 3103,
            "AdjustmentClose": 3011,
            "AdjustmentVolume": 41298500,
            "O": "",
            "H": "",
            "L": "",
            "C": "",
            "Vo": "",
            "Va": "",
            "AdjFactor": "",
            "AdjO": "",
            "AdjC": "",
            "AdjVo": "",
        }
        store.put_rows(
            "equities_bars_daily", [row], ["Code", "Date"], adj_factor_key="AdjustmentFactor"
        )
        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        r = result[0]
        assert r["O"] == 3103
        assert r["H"] == 3104
        assert r["L"] == 3000
        assert r["C"] == 3011
        assert r["Vo"] == 41298500
        assert r["Va"] == 125154283500
        assert r["AdjFactor"] == 1
        assert r["AdjO"] == 3103
        assert r["AdjC"] == 3011
        assert r["AdjVo"] == 41298500
        # 旧名は残らない
        assert "Open" not in r
        assert "Close" not in r
        store.close()


class TestMigrateNormalizeFields:
    """_migrate_normalize_fields() のテスト。"""

    @staticmethod
    def _get_conn(store: CacheStore) -> sqlite3.Connection:
        """Trigger lazy connection and return the raw SQLite connection."""
        # put_rows で接続を確立（_init_tables → _migrate も初回実行される）
        dummy = [{"Code": "00000", "Date": "1970-01-01", "O": 0, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", dummy, ["Code", "Date"], adj_factor_key="AdjFactor")
        conn = store._conn
        assert conn is not None
        # ダミーデータを削除
        conn.execute("DELETE FROM equities_bars_daily WHERE code = '00000'")
        conn.commit()
        return conn

    def test_migration_rewrites_legacy_data_in_db(self, tmp_path: Path):
        """Migration normalizes legacy field names in the data column itself."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        conn = self._get_conn(store)

        # user_version をリセットして旧形式データを INSERT
        legacy_json = '{"Code":"72030","Date":"2024-01-04","Open":2800,"O":"","AdjustmentFactor":1}'
        conn.execute("PRAGMA user_version = 0")
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("72030", "2024-01-04", legacy_json, 0.0, 1.0),
        )
        conn.commit()

        store._migrate_normalize_fields()

        row = conn.execute("SELECT data FROM equities_bars_daily WHERE code = '72030'").fetchone()
        import json

        data = json.loads(row["data"])
        assert data["O"] == 2800
        assert "Open" not in data
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        store.close()

    def test_migration_skips_when_already_done(self, tmp_path: Path):
        """Migration is skipped when user_version >= 1."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        conn = self._get_conn(store)

        # user_version は _init_tables で既に 1 → 旧形式データを INSERT しても変換されない
        legacy_json = '{"Code":"72030","Date":"2024-01-04","Open":9999}'
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            ("72030", "2024-01-04", legacy_json, 0.0),
        )
        conn.commit()

        store._migrate_normalize_fields()

        row = conn.execute("SELECT data FROM equities_bars_daily WHERE code = '72030'").fetchone()
        assert "Open" in row["data"]
        store.close()

    def test_migration_ignores_rows_without_legacy_fields(self, tmp_path: Path):
        """Rows with only current field names are not touched."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        conn = self._get_conn(store)

        conn.execute("PRAGMA user_version = 0")
        current_json = '{"Code":"72030","Date":"2024-01-04","O":2800,"AdjFactor":1}'
        conn.execute(
            "INSERT OR REPLACE INTO equities_bars_daily "
            "(code, date, data, fetched_at, adj_factor) VALUES (?, ?, ?, ?, ?)",
            ("72030", "2024-01-04", current_json, 0.0, 1.0),
        )
        conn.commit()

        store._migrate_normalize_fields()

        row = conn.execute("SELECT data FROM equities_bars_daily WHERE code = '72030'").fetchone()
        assert row["data"] == current_json
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        store.close()


class TestRequestReload:
    """Tests for CacheStore.request_reload() (SIGHUP-driven lazy reload)."""

    def test_reload_reopens_connection(self, cache_store: CacheStore):
        """After request_reload(), the next access establishes a fresh connection."""
        # Force initial connection
        rows = [{"Code": "72030", "Date": "2024-01-04", "O": 2800, "AdjFactor": 1.0}]
        cache_store.put_rows(
            "equities_bars_daily",
            rows,
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )
        original_conn = cache_store._conn
        assert original_conn is not None
        assert cache_store._ready is True

        # Request a reload
        cache_store.request_reload()
        assert cache_store._needs_reload is True
        # The connection object is still held until the next access
        assert cache_store._conn is original_conn

        # Next access triggers reconnect
        new_conn = cache_store._ensure_connection()
        assert new_conn is not None
        assert new_conn is not original_conn
        assert cache_store._needs_reload is False
        assert cache_store._ready is True

    def test_reload_preserves_on_disk_data(self, cache_store: CacheStore):
        """Data written before reload remains readable after reload."""
        rows = [
            {"Code": "72030", "Date": "2024-01-04", "O": 2800, "C": 2850, "AdjFactor": 1.0},
            {"Code": "72030", "Date": "2024-01-05", "O": 2850, "C": 2900, "AdjFactor": 1.0},
        ]
        cache_store.put_rows(
            "equities_bars_daily",
            rows,
            key_columns=["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        cache_store.request_reload()
        # Force the reconnect
        cache_store._ensure_connection()

        result = cache_store.get_rows(
            "equities_bars_daily",
            key_filter={"code": "72030"},
            date_from="2024-01-04",
            date_to="2024-01-05",
        )
        assert len(result) == 2

    def test_reload_before_first_connection_is_noop(self, tmp_path: Path):
        """Calling request_reload() before any DB access does not crash."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        # No connection yet
        assert store._conn is None
        store.request_reload()
        assert store._needs_reload is True
        # First access after reload request still works
        conn = store._ensure_connection()
        assert conn is not None
        assert store._needs_reload is False
        store.close()

    def test_reload_resets_retry_interval(self, cache_store: CacheStore):
        """request_reload() bypasses the _RETRY_INTERVAL throttle."""
        # Put the store in a not-ready state with a recent retry timestamp
        cache_store._ready = False
        cache_store._last_retry = time.time()  # just retried

        # Without reload request, _ensure_connection would return None (throttled)
        # But the DB file exists and is valid, so a reload request should immediately reconnect
        cache_store.request_reload()
        conn = cache_store._ensure_connection()
        assert conn is not None
        assert cache_store._ready is True


class TestMigrateNormalizeCalendarDates:
    """Tests for _migrate_normalize_calendar_dates() (user_version=3)."""

    @staticmethod
    def _open_store(tmp_path: Path) -> tuple["CacheStore", sqlite3.Connection]:
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        store.put_rows(
            "equities_bars_daily",
            [{"Code": "00000", "Date": "1970-01-01", "O": 0, "AdjFactor": 1.0}],
            ["Code", "Date"],
            adj_factor_key="AdjFactor",
        )
        conn = store._conn
        assert conn is not None
        conn.execute("DELETE FROM equities_bars_daily WHERE code = '00000'")
        conn.commit()
        return store, conn

    def _seed_calendar(self, conn: sqlite3.Connection, dates: list[str]) -> None:
        """Insert both clean and timestamp versions of each date."""
        for d in dates:
            for dv in (d, d + " 00:00:00"):
                conn.execute(
                    "INSERT OR REPLACE INTO markets_calendar "
                    "(date, data, fetched_at) VALUES (?, ?, ?)",
                    (dv, f'{{"Date":"{dv}","HolDivision":"1"}}', 0.0),
                )
        conn.commit()

    def test_migration_removes_timestamp_rows(self, tmp_path: Path):
        """Timestamp-suffix rows are removed; clean rows remain."""
        store, conn = self._open_store(tmp_path)
        conn.execute("PRAGMA user_version = 2")
        self._seed_calendar(conn, ["2026-05-18", "2026-05-19", "2026-05-20"])
        assert conn.execute("SELECT COUNT(*) FROM markets_calendar").fetchone()[0] == 6

        store._migrate_normalize_calendar_dates()

        assert conn.execute("SELECT COUNT(*) FROM markets_calendar").fetchone()[0] == 3
        assert (
            conn.execute("SELECT COUNT(*) FROM markets_calendar WHERE date LIKE '% %'").fetchone()[
                0
            ]
            == 0
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

    def test_migration_skips_when_already_done(self, tmp_path: Path):
        """Migration is a no-op when user_version >= 3."""
        store, conn = self._open_store(tmp_path)
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            "INSERT OR REPLACE INTO markets_calendar (date, data, fetched_at) VALUES (?, ?, ?)",
            ("2026-05-18 00:00:00", '{"Date":"2026-05-18 00:00:00"}', 0.0),
        )
        conn.commit()

        store._migrate_normalize_calendar_dates()

        assert (
            conn.execute("SELECT COUNT(*) FROM markets_calendar WHERE date LIKE '% %'").fetchone()[
                0
            ]
            == 1
        ), "Migration must not run when user_version >= 3"

    def test_migration_absent_table_does_not_raise(self, tmp_path: Path):
        """Migration handles missing markets_calendar table gracefully."""
        store, conn = self._open_store(tmp_path)
        conn.execute("PRAGMA user_version = 2")
        conn.execute("DROP TABLE markets_calendar")
        conn.commit()

        store._migrate_normalize_calendar_dates()

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

    def test_get_rows_no_duplicates_after_migration(self, tmp_path: Path):
        """get_rows returns exactly one row per date after migration."""
        store, conn = self._open_store(tmp_path)
        conn.execute("PRAGMA user_version = 2")
        self._seed_calendar(conn, ["2026-05-19", "2026-05-20", "2026-05-21"])

        store._migrate_normalize_calendar_dates()

        rows = store.get_rows(
            "markets_calendar",
            key_filter={},
            date_from="2026-05-19",
            date_to="2026-05-21",
        )
        dates = [r["Date"] for r in rows]
        assert len(dates) == 3


class TestGetLatestFinsRow:
    """get_latest_fins_row: timestamp-format disc_date の優先度テスト。"""

    def _insert_fins(
        self,
        conn: sqlite3.Connection,
        code: str,
        disc_date: str,
        sales: float,
    ) -> None:
        import json

        data = {
            "Code": code,
            "DiscDate": disc_date[:10],
            "CurPerType": "FY",
            "Sales": sales,
        }
        conn.execute(
            "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at)"
            " VALUES (?, ?, ?, ?)",
            (code, disc_date, json.dumps(data), time.time()),
        )

    def test_clean_date_preferred_over_timestamp_same_date(self, tmp_path: Path):
        """同じ開示日でタイムスタンプ行とクリーン行が共存するとき、クリーン行が返る。"""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        conn = store._ensure_connection()
        self._insert_fins(conn, "72030", "2026-05-08 00:00:00", sales=100.0)
        self._insert_fins(conn, "72030", "2026-05-08", sales=200.0)
        conn.commit()

        row = store.get_latest_fins_row("72030")
        assert row is not None
        # clean-date 行（Sales=200）が選ばれること
        assert row.get("Sales") == 200.0
        store.close()

    def test_most_recent_date_returned_when_no_timestamp_conflict(self, tmp_path: Path):
        """タイムスタンプ重複なしの場合、最新 disc_date の行が返る。"""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        conn = store._ensure_connection()
        self._insert_fins(conn, "72030", "2026-03-31", sales=100.0)
        self._insert_fins(conn, "72030", "2026-05-08", sales=999.0)
        conn.commit()

        row = store.get_latest_fins_row("72030")
        assert row is not None
        assert row.get("Sales") == 999.0
        store.close()

    def test_timestamp_row_alone_still_returned(self, tmp_path: Path):
        """タイムスタンプ行しかない場合でも正常に返る。"""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        conn = store._ensure_connection()
        self._insert_fins(conn, "72030", "2026-05-08 00:00:00", sales=50.0)
        conn.commit()

        row = store.get_latest_fins_row("72030")
        assert row is not None
        assert row.get("Sales") == 50.0
        store.close()

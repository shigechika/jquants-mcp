"""Tests for SQLite cache store."""

from __future__ import annotations

import time
from pathlib import Path

from jquants_dat_mcp.cache.store import CacheStore, make_cache_key


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

        # TTL を強制的に過去にする（実際のストレージキーは plan suffix 付き）
        full_key = cache_store._plan_cache_key("old|key")
        conn = cache_store._ensure_connection()
        conn.execute(
            "UPDATE response_cache SET fetched_at = ? WHERE cache_key = ?",
            (time.time() - 10, full_key),
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
    """Plan-scoped cache isolation tests."""

    def test_tier1_free_cannot_read_premium_cache(self, tmp_path: Path):
        """Free plan must not see data written by a premium plan instance.

        This simulates the security scenario: a premium user's cached rows
        must not be returned when the server is reconfigured to free plan.
        Tier 1 uses PK=(code, date) so the cache is effectively plan-scoped
        via the plan column filter: a free-plan GET finds nothing until the
        free plan writes its own row (overwriting the premium row).
        """
        premium_cache = CacheStore(tmp_path / "cache.db", default_plan="premium")
        free_cache = CacheStore(tmp_path / "cache.db", default_plan="free")

        premium_rows = [{"Code": "72030", "Date": "2024-01-04", "O": 999, "AdjFactor": 1.0}]

        # premium ユーザーがデータを保存
        premium_cache.put_rows(
            "equities_bars_daily",
            premium_rows,
            ["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        # free ユーザーには premium のキャッシュが見えない（plan フィルタで除外）
        result = free_cache.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 0, "Free plan must not see premium cached rows"

        premium_cache.close()
        free_cache.close()

    def test_tier1_default_plan_used(self, tmp_path: Path):
        """CacheStore default_plan is applied when no explicit plan is passed."""
        store = CacheStore(tmp_path / "cache.db", default_plan="standard")
        rows = [{"Code": "72030", "Date": "2024-01-04", "O": 500, "AdjFactor": 1.0}]
        store.put_rows("equities_bars_daily", rows, ["Code", "Date"], adj_factor_key="AdjFactor")

        # default_plan で取得できる
        result = store.get_rows("equities_bars_daily", {"code": "72030"})
        assert len(result) == 1
        assert result[0]["O"] == 500

        # 異なるプランでは取得できない
        result_free = store.get_rows("equities_bars_daily", {"code": "72030"}, plan="free")
        assert len(result_free) == 0
        store.close()

    def test_tier2_different_plans_isolated(self, tmp_path: Path):
        """Response cache entries written under different plans must not collide."""
        free_cache = CacheStore(tmp_path / "cache.db", default_plan="free")
        premium_cache = CacheStore(tmp_path / "cache.db", default_plan="premium")

        cache_key = make_cache_key("/equities/master")
        free_cache.put_response(cache_key, {"data": "free"}, ttl_seconds=3600)
        premium_cache.put_response(cache_key, {"data": "premium"}, ttl_seconds=3600)

        assert free_cache.get_response(cache_key)["data"] == "free"
        assert premium_cache.get_response(cache_key)["data"] == "premium"

        free_cache.close()
        premium_cache.close()

    def test_tier1_invalidate_scoped_to_plan(self, tmp_path: Path):
        """invalidate_rows removes only rows for the active plan.

        Two different code values are used (different PKs) so that both
        free and premium rows can coexist in the same table.
        """
        free_cache = CacheStore(tmp_path / "cache.db", default_plan="free")
        premium_cache = CacheStore(tmp_path / "cache.db", default_plan="premium")

        # 異なるコード（PK）でそれぞれのプランのデータを保存
        free_cache.put_rows(
            "equities_bars_daily",
            [{"Code": "10010", "Date": "2024-01-04", "O": 1, "AdjFactor": 1.0}],
            ["Code", "Date"],
            adj_factor_key="AdjFactor",
        )
        premium_cache.put_rows(
            "equities_bars_daily",
            [{"Code": "20020", "Date": "2024-01-04", "O": 2, "AdjFactor": 1.0}],
            ["Code", "Date"],
            adj_factor_key="AdjFactor",
        )

        # free のみ無効化
        deleted = free_cache.invalidate_rows("equities_bars_daily", {"code": "10010"})
        assert deleted == 1

        # premium のデータは残る
        assert len(premium_cache.get_rows("equities_bars_daily", {"code": "20020"})) == 1
        # free のデータは消える
        assert len(free_cache.get_rows("equities_bars_daily", {"code": "10010"})) == 0

        free_cache.close()
        premium_cache.close()

    def test_migration_adds_plan_column_to_existing_table(self, tmp_path: Path):
        """Tables created without plan column are migrated transparently."""
        db_path = tmp_path / "cache.db"

        # plan カラムなしで古いテーブルを作成
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE equities_bars_daily "
            "(code TEXT NOT NULL, date TEXT NOT NULL, "
            "data TEXT NOT NULL, fetched_at REAL NOT NULL, "
            "adj_factor REAL, PRIMARY KEY (code, date))"
        )
        conn.execute(
            "INSERT INTO equities_bars_daily VALUES ('72030', '2024-01-04', '{\"O\":1}', 0, 1.0)"
        )
        conn.commit()
        conn.close()

        # CacheStore で開くとマイグレーションが走る
        store = CacheStore(db_path, default_plan="free")
        store._ensure_connection()

        # 既存データに plan='free' が付与されていること
        conn2 = store._ensure_connection()
        row = conn2.execute(
            "SELECT plan FROM equities_bars_daily WHERE code = '72030'"
        ).fetchone()
        assert row["plan"] == "free"

        store.close()


class TestCacheUtility:
    """キャッシュユーティリティのテスト。"""

    def test_status(self, cache_store: CacheStore):
        status = cache_store.status()
        assert "equities_bars_daily" in status
        assert "response_cache" in status
        assert "db_size_mb" in status

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

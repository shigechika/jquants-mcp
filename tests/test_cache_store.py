"""Tests for SQLite cache store."""

from __future__ import annotations

import time

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

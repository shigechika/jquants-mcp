"""Tests for SQLiteKeyValueStore (AsyncKeyValue implementation for OAuth persistence)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from jquants_mcp.oauth_kv_store import SQLiteKeyValueStore


@pytest.fixture()
def store(tmp_path: Path) -> SQLiteKeyValueStore:
    """Provide a fresh SQLiteKeyValueStore backed by a temp file."""
    return SQLiteKeyValueStore(tmp_path / "oauth_state.db")


class TestGet:
    """get() のテスト。"""

    @pytest.mark.asyncio
    async def test_missing_key_returns_none(self, store: SQLiteKeyValueStore):
        result = await store.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_and_get(self, store: SQLiteKeyValueStore):
        await store.put("k1", {"client_id": "abc", "secret": "xyz"})
        result = await store.get("k1")
        assert result == {"client_id": "abc", "secret": "xyz"}

    @pytest.mark.asyncio
    async def test_get_expired_returns_none(self, store: SQLiteKeyValueStore):
        # TTL=1秒で保存し、直後に期限切れ状態を強制する
        await store.put("expired_key", {"data": "value"}, ttl=1)
        # expires_at を過去に書き換え
        conn = store._ensure_connection()
        conn.execute(
            'UPDATE "__default__" SET expires_at = ? WHERE key = ?',
            (time.time() - 10, "expired_key"),
        )
        conn.commit()
        result = await store.get("expired_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_with_collection(self, store: SQLiteKeyValueStore):
        await store.put("key", {"v": 1}, collection="col_a")
        await store.put("key", {"v": 2}, collection="col_b")
        assert (await store.get("key", collection="col_a")) == {"v": 1}
        assert (await store.get("key", collection="col_b")) == {"v": 2}


class TestPut:
    """put() のテスト。"""

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, store: SQLiteKeyValueStore):
        await store.put("k", {"x": 1})
        await store.put("k", {"x": 2})
        assert (await store.get("k")) == {"x": 2}

    @pytest.mark.asyncio
    async def test_no_ttl(self, store: SQLiteKeyValueStore):
        await store.put("k", {"a": "b"})
        _value, remaining = await store.ttl("k")
        assert remaining is None  # TTL なし

    @pytest.mark.asyncio
    async def test_with_ttl(self, store: SQLiteKeyValueStore):
        await store.put("k", {"a": "b"}, ttl=3600)
        _value, remaining = await store.ttl("k")
        assert remaining is not None
        assert 3590 < remaining <= 3600


class TestDelete:
    """delete() のテスト。"""

    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, store: SQLiteKeyValueStore):
        await store.put("k", {"data": 1})
        deleted = await store.delete("k")
        assert deleted is True
        assert (await store.get("k")) is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, store: SQLiteKeyValueStore):
        result = await store.delete("nonexistent")
        assert result is False


class TestTtl:
    """ttl() のテスト。"""

    @pytest.mark.asyncio
    async def test_missing_key(self, store: SQLiteKeyValueStore):
        value, remaining = await store.ttl("missing")
        assert value is None
        assert remaining is None

    @pytest.mark.asyncio
    async def test_key_without_ttl(self, store: SQLiteKeyValueStore):
        await store.put("k", {"x": 1})
        value, remaining = await store.ttl("k")
        assert value == {"x": 1}
        assert remaining is None


class TestBulkOperations:
    """get_many / put_many / delete_many / ttl_many のテスト。"""

    @pytest.mark.asyncio
    async def test_put_many_and_get_many(self, store: SQLiteKeyValueStore):
        keys = ["a", "b", "c"]
        values = [{"n": 1}, {"n": 2}, {"n": 3}]
        await store.put_many(keys, values)
        results = await store.get_many(keys)
        assert results == values

    @pytest.mark.asyncio
    async def test_get_many_with_missing(self, store: SQLiteKeyValueStore):
        await store.put("exists", {"v": 1})
        results = await store.get_many(["exists", "missing"])
        assert results[0] == {"v": 1}
        assert results[1] is None

    @pytest.mark.asyncio
    async def test_delete_many(self, store: SQLiteKeyValueStore):
        await store.put_many(["x", "y", "z"], [{"v": 1}, {"v": 2}, {"v": 3}])
        deleted = await store.delete_many(["x", "z", "nonexistent"])
        assert deleted == 2
        assert (await store.get("y")) == {"v": 2}

    @pytest.mark.asyncio
    async def test_ttl_many(self, store: SQLiteKeyValueStore):
        await store.put("with_ttl", {"v": 1}, ttl=3600)
        await store.put("no_ttl", {"v": 2})
        results = await store.ttl_many(["with_ttl", "no_ttl", "missing"])
        assert results[0][0] == {"v": 1}
        assert results[0][1] is not None
        assert results[1][0] == {"v": 2}
        assert results[1][1] is None
        assert results[2] == (None, None)


class TestPersistence:
    """データが DB ファイルに永続化されること。"""

    @pytest.mark.asyncio
    async def test_data_survives_store_recreation(self, tmp_path: Path):
        db_path = tmp_path / "oauth_state.db"
        store1 = SQLiteKeyValueStore(db_path)
        await store1.put("client_abc", {"secret": "s3cr3t", "scopes": ["read"]})

        # 別のインスタンスで同じ DB を開く
        store2 = SQLiteKeyValueStore(db_path)
        result = await store2.get("client_abc")
        assert result == {"secret": "s3cr3t", "scopes": ["read"]}

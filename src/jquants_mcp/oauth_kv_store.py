"""SQLite-backed AsyncKeyValue store for OAuth client registration persistence.

OAuth DCR (Dynamic Client Registration) client data is stored here so it
survives Cloud Run container restarts instead of being lost in the ephemeral
~/.cache/fastmcp/oauth-proxy/ filesystem.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_DEFAULT_COLLECTION = "__default__"


def _safe_table_name(name: str) -> str:
    """Sanitize a collection name to a safe SQLite table name.

    Replaces any character that is not alphanumeric or underscore with ``_``.

    Invariant: collection names are internal (FastMCP DCR uses a small fixed
    set), so the sanitized names are effectively unique. The mapping is NOT
    injective — ``foo-bar`` and ``foo_bar`` collapse to the same table — so if
    collection names ever become user-derived, add a hash suffix here to
    guarantee uniqueness (and migrate existing tables) before doing so.
    """
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


class SQLiteKeyValueStore:
    """SQLite-backed asynchronous key-value store implementing AsyncKeyValue protocol.

    Each collection is stored as a separate SQLite table with columns:
        key TEXT PRIMARY KEY
        value TEXT  (JSON-encoded dict)
        expires_at REAL | NULL  (Unix timestamp; NULL means no expiry)

    Uses WAL mode and busy_timeout for concurrent access safety.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
        return self._conn

    def _collection_name(self, collection: str | None) -> str:
        return collection if collection else _DEFAULT_COLLECTION

    def _ensure_table(self, conn: sqlite3.Connection, table: str) -> None:
        # Derive a safe table name from the collection name (alphanumerics and underscores only)
        safe = _safe_table_name(table)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{safe}" (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL
            )
            """
        )
        conn.commit()

    def _get_row(
        self, conn: sqlite3.Connection, table: str, key: str
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Return (value_dict, expires_at) for key, deleting expired rows."""
        safe = _safe_table_name(table)
        row = conn.execute(
            f'SELECT value, expires_at FROM "{safe}" WHERE key = ?', (key,)
        ).fetchone()
        if row is None:
            return None, None

        expires_at: float | None = row["expires_at"]
        if expires_at is not None and time.time() > expires_at:
            # Delete the expired entry
            conn.execute(f'DELETE FROM "{safe}" WHERE key = ?', (key,))
            conn.commit()
            return None, None

        value: dict[str, Any] = json.loads(row["value"])
        return value, expires_at

    # ------------------------------------------------------------------
    # AsyncKeyValue protocol implementation
    # ------------------------------------------------------------------

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        """Retrieve a value by key from the specified collection."""
        table = self._collection_name(collection)
        conn = self._ensure_connection()
        self._ensure_table(conn, table)
        value, _ = self._get_row(conn, table, key)
        return value

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Store a key-value pair with optional TTL (seconds)."""
        table = self._collection_name(collection)
        conn = self._ensure_connection()
        self._ensure_table(conn, table)
        safe = _safe_table_name(table)
        expires_at = (time.time() + float(ttl)) if ttl is not None else None
        conn.execute(
            f'INSERT OR REPLACE INTO "{safe}" (key, value, expires_at) VALUES (?, ?, ?)',
            (key, json.dumps(dict(value)), expires_at),
        )
        conn.commit()

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        """Delete a key. Returns True if the key existed."""
        table = self._collection_name(collection)
        conn = self._ensure_connection()
        self._ensure_table(conn, table)
        safe = _safe_table_name(table)
        cursor = conn.execute(f'DELETE FROM "{safe}" WHERE key = ?', (key,))
        conn.commit()
        return cursor.rowcount > 0

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Return (value, remaining_ttl_seconds) for key.

        remaining_ttl_seconds is None when no TTL is set, and negative when expired.
        """
        table = self._collection_name(collection)
        conn = self._ensure_connection()
        self._ensure_table(conn, table)
        value, expires_at = self._get_row(conn, table, key)
        if value is None:
            return None, None
        remaining: float | None = None
        if expires_at is not None:
            remaining = expires_at - time.time()
        return value, remaining

    async def get_many(
        self,
        keys: Sequence[str],
        *,
        collection: str | None = None,
    ) -> list[dict[str, Any] | None]:
        """Retrieve multiple values by key."""
        return [await self.get(k, collection=collection) for k in keys]

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: float | None = None,
    ) -> None:
        """Store multiple key-value pairs."""
        for k, v in zip(keys, values):
            await self.put(k, v, collection=collection, ttl=ttl)

    async def delete_many(
        self,
        keys: Sequence[str],
        *,
        collection: str | None = None,
    ) -> int:
        """Delete multiple keys. Returns count of keys that existed."""
        count = 0
        for k in keys:
            if await self.delete(k, collection=collection):
                count += 1
        return count

    async def ttl_many(
        self,
        keys: Sequence[str],
        *,
        collection: str | None = None,
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        """Return (value, remaining_ttl_seconds) for multiple keys."""
        return [await self.ttl(k, collection=collection) for k in keys]

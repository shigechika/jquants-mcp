"""SQLite-based cache store with row-level and response-level caching."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tier 1 テーブル: 行レベルキャッシュ（日付×コード単位）
_TIER1_TABLES = {
    "equities_bars_daily": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "adj_factor REAL",
    },
    "equities_master": {
        "key_columns": "code TEXT NOT NULL, date TEXT NOT NULL",
        "primary_key": "code, date",
        "extra_columns": "",
    },
    "fins_summary": {
        "key_columns": "code TEXT NOT NULL, disc_date TEXT NOT NULL",
        "primary_key": "code, disc_date",
        "extra_columns": "",
    },
    "indices_bars_daily_topix": {
        "key_columns": "date TEXT NOT NULL",
        "primary_key": "date",
        "extra_columns": "",
    },
    "investor_types": {
        "key_columns": "pub_date TEXT NOT NULL, section TEXT NOT NULL",
        "primary_key": "pub_date, section",
        "extra_columns": "",
    },
}

# Tier 2 テーブル: レスポンスレベルキャッシュ
_RESPONSE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL
)
"""

# TTL 定義（秒）
TTL_NONE = 0  # キャッシュしない
TTL_6H = 6 * 3600
TTL_24H = 24 * 3600
TTL_7D = 7 * 24 * 3600
TTL_90D = 90 * 24 * 3600

# エンドポイントパス → TTL のマッピング
ENDPOINT_TTL: dict[str, int] = {
    "/markets/calendar": TTL_7D,
    "/equities/earnings-calendar": TTL_90D,
    "/equities/investor-types": TTL_7D,
    "/markets/margin-interest": TTL_7D,
    "/markets/margin-alert": TTL_24H,
    "/markets/short-ratio": TTL_24H,
    "/markets/short-sale-report": TTL_24H,
    "/markets/breakdown": TTL_24H,
    "/indices/bars/daily": TTL_24H,
    "/derivatives/bars/daily/futures": TTL_24H,
    "/derivatives/bars/daily/options": TTL_24H,
    "/derivatives/bars/daily/options/225": TTL_24H,
    "/equities/bars/daily/am": TTL_NONE,  # 当日データ、キャッシュしない
    "/equities/bars/minute": TTL_24H,
    "/fins/details": TTL_24H,
    "/fins/dividend": TTL_24H,
    "/bulk/list": TTL_6H,
    "/bulk/get": TTL_NONE,  # 署名付きURL、キャッシュしない
}


class CacheStore:
    """SQLite-based two-tier cache store."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_connection(self) -> sqlite3.Connection:
        """Lazy initialization of SQLite connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._init_tables()
            logger.info("キャッシュDB接続: %s", self._db_path)
        return self._conn

    def _init_tables(self) -> None:
        """Create cache tables if they don't exist."""
        conn = self._conn
        assert conn is not None

        # Tier 1 テーブル
        for table_name, schema in _TIER1_TABLES.items():
            extra = f", {schema['extra_columns']}" if schema["extra_columns"] else ""
            ddl = f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    {schema["key_columns"]},
                    data TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                    {extra},
                    PRIMARY KEY ({schema["primary_key"]})
                )
            """
            conn.execute(ddl)

        # Tier 2 テーブル
        conn.execute(_RESPONSE_CACHE_DDL)
        conn.commit()

    # ----------------------------------------------------------------
    # Tier 1: 行レベルキャッシュ
    # ----------------------------------------------------------------

    def get_rows(
        self,
        table: str,
        key_filter: dict[str, str],
        date_column: str = "date",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve cached rows matching filters.

        Args:
            table: Tier 1 table name
            key_filter: Column name → value pairs (e.g. {"code": "72030"})
            date_column: Name of the date column for range filtering
            date_from: Start date (inclusive)
            date_to: End date (inclusive)

        Returns:
            List of cached data dicts
        """
        if table not in _TIER1_TABLES:
            return []

        conn = self._ensure_connection()
        conditions = []
        params: list[str] = []

        for col, val in key_filter.items():
            conditions.append(f"{col} = ?")
            params.append(val)

        if date_from:
            conditions.append(f"{date_column} >= ?")
            params.append(date_from)
        if date_to:
            conditions.append(f"{date_column} <= ?")
            params.append(date_to)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT data FROM {table} WHERE {where} ORDER BY {date_column}"

        rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def get_cached_dates(
        self,
        table: str,
        key_filter: dict[str, str],
        date_column: str = "date",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> set[str]:
        """Return the set of dates already cached for the given key."""
        if table not in _TIER1_TABLES:
            return set()

        conn = self._ensure_connection()
        conditions = []
        params: list[str] = []

        for col, val in key_filter.items():
            conditions.append(f"{col} = ?")
            params.append(val)

        if date_from:
            conditions.append(f"{date_column} >= ?")
            params.append(date_from)
        if date_to:
            conditions.append(f"{date_column} <= ?")
            params.append(date_to)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT {date_column} FROM {table} WHERE {where}"

        rows = conn.execute(sql, params).fetchall()
        return {row[0] for row in rows}

    def put_rows(
        self,
        table: str,
        rows: list[dict[str, Any]],
        key_columns: list[str],
        adj_factor_key: str | None = None,
    ) -> int:
        """Insert or replace rows into a Tier 1 table.

        Args:
            table: Tier 1 table name
            rows: List of data dicts from the API response
            key_columns: Column names to extract as key values (e.g. ["code", "date"])
            adj_factor_key: If set, extract this key from data as adj_factor column

        Returns:
            Number of rows inserted
        """
        if table not in _TIER1_TABLES or not rows:
            return 0

        conn = self._ensure_connection()
        now = time.time()
        count = 0

        has_adj = bool(adj_factor_key) and "adj_factor" in (
            _TIER1_TABLES[table].get("extra_columns", "")
        )

        for row in rows:
            key_values = [str(row.get(k, "")) for k in key_columns]
            data_json = json.dumps(row, ensure_ascii=False)

            if has_adj:
                adj = row.get(adj_factor_key)
                col_names = ", ".join(_key_col_names(table)) + ", data, fetched_at, adj_factor"
                placeholders = ", ".join(["?"] * (len(key_values) + 3))
                values = key_values + [data_json, now, adj]
            else:
                col_names = ", ".join(_key_col_names(table)) + ", data, fetched_at"
                placeholders = ", ".join(["?"] * (len(key_values) + 2))
                values = key_values + [data_json, now]

            sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
            conn.execute(sql, values)
            count += 1

        conn.commit()
        return count

    def invalidate_rows(self, table: str, key_filter: dict[str, str]) -> int:
        """Delete cached rows matching the filter (e.g. for stock split invalidation).

        Returns:
            Number of rows deleted
        """
        if table not in _TIER1_TABLES:
            return 0

        conn = self._ensure_connection()
        conditions = [f"{col} = ?" for col in key_filter]
        params = list(key_filter.values())

        sql = f"DELETE FROM {table} WHERE {' AND '.join(conditions)}"
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    def check_adj_factor(self, code: str, new_adj_factor: float | None) -> bool:
        """Check if AdjFactor has changed for a stock (split detection).

        Returns:
            True if cache is valid (no split detected), False if invalidation needed
        """
        if new_adj_factor is None:
            return True

        conn = self._ensure_connection()
        row = conn.execute(
            "SELECT adj_factor FROM equities_bars_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()

        if row is None:
            return True  # キャッシュなし → 問題なし

        cached_adj = row["adj_factor"]
        if cached_adj is not None and abs(cached_adj - new_adj_factor) > 1e-10:
            logger.info(
                "株式分割検知: code=%s (AdjFactor: %s → %s)", code, cached_adj, new_adj_factor
            )
            return False

        return True

    # ----------------------------------------------------------------
    # Tier 2: レスポンスレベルキャッシュ
    # ----------------------------------------------------------------

    def get_response(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve a cached response if it exists and hasn't expired."""
        conn = self._ensure_connection()
        row = conn.execute(
            "SELECT data, fetched_at, ttl_seconds FROM response_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()

        if row is None:
            return None

        age = time.time() - row["fetched_at"]
        if row["ttl_seconds"] > 0 and age > row["ttl_seconds"]:
            # 期限切れ → 削除
            conn.execute("DELETE FROM response_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None

        return json.loads(row["data"])

    def put_response(self, cache_key: str, data: Any, ttl_seconds: int) -> None:
        """Store a response in the cache."""
        if ttl_seconds == TTL_NONE:
            return  # キャッシュしない設定

        conn = self._ensure_connection()
        conn.execute(
            "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) "
            "VALUES (?, ?, ?, ?)",
            (cache_key, json.dumps(data, ensure_ascii=False), time.time(), ttl_seconds),
        )
        conn.commit()

    # ----------------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return cache statistics."""
        conn = self._ensure_connection()
        stats: dict[str, Any] = {"db_path": str(self._db_path)}

        for table_name in _TIER1_TABLES:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table_name}").fetchone()
            stats[table_name] = row["cnt"] if row else 0

        row = conn.execute("SELECT COUNT(*) as cnt FROM response_cache").fetchone()
        stats["response_cache"] = row["cnt"] if row else 0

        # DB ファイルサイズ
        if self._db_path.exists():
            stats["db_size_mb"] = round(self._db_path.stat().st_size / (1024 * 1024), 2)

        return stats

    def clear(self, table: str | None = None) -> dict[str, int]:
        """Clear cache data.

        Args:
            table: If specified, clear only this table. Otherwise clear all.

        Returns:
            Dict of table_name → rows_deleted
        """
        conn = self._ensure_connection()
        result: dict[str, int] = {}

        tables = [table] if table else list(_TIER1_TABLES.keys()) + ["response_cache"]
        for t in tables:
            cursor = conn.execute(f"DELETE FROM {t}")
            result[t] = cursor.rowcount

        conn.commit()
        return result

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _key_col_names(table: str) -> list[str]:
    """Extract key column names from table schema definition."""
    schema = _TIER1_TABLES[table]
    # "code TEXT NOT NULL, date TEXT NOT NULL" → ["code", "date"]
    return [part.strip().split()[0] for part in schema["key_columns"].split(",")]


def make_cache_key(endpoint: str, params: dict[str, Any] | None = None) -> str:
    """Generate a deterministic cache key from endpoint and params."""
    parts = [endpoint]
    if params:
        sorted_params = sorted((k, str(v)) for k, v in params.items() if v is not None)
        parts.append("&".join(f"{k}={v}" for k, v in sorted_params))
    return "|".join(parts)

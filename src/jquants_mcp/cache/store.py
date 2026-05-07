"""SQLite-based cache store with row-level and response-level caching."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jquants_mcp.cache.schema import (
    ALL_TABLE_NAMES as _ALL_TABLE_NAMES,
    EBD_DATE_INDEX_DDL as _EBD_DATE_INDEX_DDL,
    RESPONSE_CACHE_DDL as _RESPONSE_CACHE_DDL,
    SCREENER_RESULTS_DDL as _SCREENER_RESULTS_DDL,
    SCREENER_RESULTS_INDEX_DDL as _SCREENER_RESULTS_INDEX_DDL,
    TIER1_KEY_COLUMNS as _TIER1_KEY_COLUMNS,
    TIER1_TABLES as _TIER1_TABLES,
    generate_ddl,
)
from jquants_mcp.cache.screener_compute import (
    SCREENER_CACHE_LOOKBACK_WEEKS as _SCREENER_CACHE_LOOKBACK_WEEKS,
)

logger = logging.getLogger(__name__)

# TTL 定義（秒）
TTL_NONE = 0  # キャッシュしない
TTL_6H = 6 * 3600
TTL_24H = 24 * 3600
TTL_7D = 7 * 24 * 3600
TTL_90D = 90 * 24 * 3600

# 旧 J-Quants API フィールド名 → 現行短縮名のマッピング
# キャッシュに旧形式で保存されたデータを読み出し時に正規化する
_LEGACY_FIELD_MAP: dict[str, str] = {
    "Open": "O",
    "High": "H",
    "Low": "L",
    "Close": "C",
    "Volume": "Vo",
    "TurnoverValue": "Va",
    "AdjustmentOpen": "AdjO",
    "AdjustmentHigh": "AdjH",
    "AdjustmentLow": "AdjL",
    "AdjustmentClose": "AdjC",
    "AdjustmentVolume": "AdjVo",
    "AdjustmentFactor": "AdjFactor",
    "UpperLimit": "UL",
    "LowerLimit": "LL",
}


def _normalize_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Rename legacy J-Quants field names to current short names.

    Cached rows may contain both legacy and current field names
    (e.g. {"Open": 3103, "O": ""}).  Non-empty values always win.
    """
    result: dict[str, Any] = {}
    for k, v in row.items():
        new_key = _LEGACY_FIELD_MAP.get(k, k)
        if new_key not in result or v not in ("", None):
            result[new_key] = v
    return result


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

# Tier 1 table -> minimum required plan
_TABLE_MIN_PLAN: dict[str, str] = {
    "equities_bars_daily": "free",
    "equities_master": "free",
    "fins_summary": "free",
    "indices_bars_daily_topix": "light",
    "investor_types": "light",
    "markets_margin_interest": "standard",
    "markets_margin_alert": "standard",
    "markets_short_ratio": "standard",
    "markets_breakdown": "premium",
    "markets_calendar": "free",
}

# Plan hierarchy for comparison
_PLAN_LEVEL: dict[str, int] = {
    "free": 0,
    "light": 1,
    "standard": 2,
    "premium": 3,
}

# プラン別データ保持期間（年）。None = 制限なし
_PLAN_RETENTION_YEARS: dict[str, int | None] = {
    "free": 2,
    "light": 5,
    "standard": 10,
    "premium": None,
}

# Free プランのデータ遅延（週）
_FREE_DELAY_WEEKS = 12


class CacheStore:
    """SQLite-based two-tier cache store.

    Plan is used only for date-range restriction on reads (via
    ``_plan_date_bounds``), not for storage isolation.

    When the database file is missing or corrupt (e.g. GCS copy still in
    progress on Cloud Run), the store enters a *not-ready* state where all
    reads return cache-miss results and all writes are silently skipped.
    The store periodically retries the connection so it recovers
    automatically once the file becomes valid.
    """

    # DB が使えない場合のリトライ間隔（秒）
    _RETRY_INTERVAL = 30

    def __init__(
        self,
        db_path: Path,
        default_plan: str = "free",
        *,
        check_integrity_async: bool = False,
    ):
        self._db_path = db_path
        self._default_plan = default_plan
        self._conn: sqlite3.Connection | None = None
        self._ready: bool = False
        self._last_retry: float = 0.0
        self._needs_reload: bool = False
        # Integrity check state — populated asynchronously after first
        # successful connection. Values: "pending", "ok", "not-checked", or
        # a short error description.
        self._integrity_status: str = "not-checked"
        self._integrity_thread: threading.Thread | None = None
        # When True, kick off the background SQLite integrity check at
        # construction time so callers that read ``integrity_status``
        # without first opening a connection (e.g. ``health_check``)
        # see ``"pending"`` / ``"ok"`` instead of ``"not-checked"``.
        # Defaults to False so test fixtures don't spawn extra threads.
        if check_integrity_async and self._db_path.exists():
            self._start_integrity_check()

    @property
    def default_plan(self) -> str:
        """Return the default plan used for cache operations."""
        return self._default_plan

    @default_plan.setter
    def default_plan(self, value: str) -> None:
        """Update the default plan (e.g. after auto-detection)."""
        self._default_plan = value

    @property
    def ready(self) -> bool:
        """Return whether the cache database is usable."""
        return self._ready

    @property
    def integrity_status(self) -> str:
        """Return the result of the background SQLite integrity check.

        Values: ``"not-checked"`` (DB never opened), ``"pending"`` (check
        running), ``"ok"`` (passed), or a short error description. Surfaced
        via ``cache_status`` / ``health_check`` so operators can spot cache
        corruption without waiting for downstream queries to silent-fail.
        """
        return self._integrity_status

    def _start_integrity_check(self) -> None:
        """Kick off PRAGMA quick_check in a background thread.

        quick_check on a 3.5 GB cache.db takes ~1 minute — running it on the
        event loop would freeze the server. A dedicated thread with its own
        sqlite connection runs the check in parallel with normal traffic
        and records the result on ``self._integrity_status``.
        """
        if self._integrity_thread is not None and self._integrity_thread.is_alive():
            return
        self._integrity_status = "pending"

        def _run() -> None:
            try:
                probe = sqlite3.connect(str(self._db_path), check_same_thread=False)
                try:
                    row = probe.execute("PRAGMA quick_check").fetchone()
                finally:
                    probe.close()
                result = row[0] if row else "unknown"
                if result == "ok":
                    self._integrity_status = "ok"
                    logger.info("cache.db integrity check: ok")
                else:
                    self._integrity_status = f"failed: {result}"
                    logger.warning("cache.db integrity check failed: %s", result)
            except Exception as exc:  # pragma: no cover — defensive
                self._integrity_status = f"error: {exc}"
                logger.warning("cache.db integrity check errored: %s", exc)

        t = threading.Thread(target=_run, name="cache-integrity-check", daemon=True)
        self._integrity_thread = t
        t.start()

    def request_reload(self) -> None:
        """Request a lazy reload of the SQLite connection.

        The actual reconnection happens on the next ``_ensure_connection``
        call. The current connection object is detached without calling
        ``close()`` so that any in-flight queries holding a reference
        continue to succeed; the old connection is released when all
        references go out of scope. This is intended to be called from
        a signal handler after an external process (e.g. daily.sh's
        ``import_csv_to_cache.py``) has updated the on-disk database.
        """
        self._needs_reload = True

    def _ensure_connection(self) -> sqlite3.Connection | None:
        """Lazy initialization of SQLite connection with integrity check.

        Returns the connection if the database is ready, or ``None`` if
        the database file is missing, corrupt, or still being copied.
        When not ready, retries at most once every ``_RETRY_INTERVAL``
        seconds.
        """
        # Handle a pending reload request from request_reload()
        if self._needs_reload:
            self._needs_reload = False
            # Do not explicitly close the old connection: in-flight queries
            # may still hold a reference, and closing would break them.
            # Setting self._conn to None forces a fresh connection on the
            # next access; the old connection is released by Python's GC
            # once all references go out of scope.
            self._conn = None
            self._ready = False
            self._last_retry = 0.0  # reset retry interval for immediate reconnect
            logger.info("Cache DB reload requested; will reconnect on next access")

        if self._conn is not None and self._ready:
            return self._conn

        # リトライ間隔の制御
        now = time.time()
        if not self._ready and (now - self._last_retry) < self._RETRY_INTERVAL:
            return None
        self._last_retry = now

        # 既存の壊れた接続を閉じる
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._ready = False

        # If a download is in progress (sentinel file present), wait for it
        # to finish. Otherwise we'd create an empty DB, and the subsequent
        # atomic rename would produce a new inode that our open handle never
        # sees, leaving us to serve empty results forever.
        download_sentinel = self._db_path.parent / f".{self._db_path.name}.download"
        if download_sentinel.exists() and not self._db_path.exists():
            logger.info(
                "Cache DB download in progress: %s (waiting for completion)",
                download_sentinel,
            )
            return None

        try:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Previously we ran `PRAGMA quick_check` here, but on the
            # 3.5 GB cache.db it takes ~1 minute and blocks the event loop.
            # Atomic download (see gcs_sync._reinitialize temp-file rename)
            # already prevents reading a half-written file, so this check
            # is no longer needed. See #71 for a non-blocking alternative.
            conn.execute("PRAGMA journal_mode=WAL")
            self._conn = conn
            self._init_tables()
            self._ready = True
            logger.info("キャッシュDB接続: %s", self._db_path)
            self._start_integrity_check()
            return self._conn
        except sqlite3.DatabaseError as e:
            logger.warning("キャッシュDB接続失敗: %s", e)
            return None

    def _init_tables(self) -> None:
        """Create cache tables if they don't exist, then migrate existing ones."""
        conn = self._conn
        assert conn is not None

        # Tier 1 テーブル
        for table_name, schema in _TIER1_TABLES.items():
            conn.execute(generate_ddl(table_name, schema))

        # Tier 2 テーブル
        conn.execute(_RESPONSE_CACHE_DDL)

        # Screener result cache（事前計算結果、PK = tool/params/date）
        conn.execute(_SCREENER_RESULTS_DDL)
        conn.execute(_SCREENER_RESULTS_INDEX_DDL)

        # Single-column index on equities_bars_daily(date) for O(log n) MAX(date).
        conn.execute(_EBD_DATE_INDEX_DDL)
        conn.commit()

        # 既存テーブルのマイグレーション
        self._migrate_normalize_fields()
        self._migrate_drop_plan()

    def _migrate_drop_plan(self) -> None:
        """Remove plan column from Tier 1 tables and plan suffix from Tier 2 keys.

        Runs once: skipped when ``PRAGMA user_version >= 2``.
        For tables where plan is part of the PRIMARY KEY, the table is
        rebuilt.  Duplicate rows (same natural key, different plan) are
        deduplicated by keeping the highest-plan row (standard > light > free).
        """
        conn = self._conn
        assert conn is not None

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 2:
            return

        # Check if any table actually has a plan column
        has_plan_anywhere = False
        for table_name in _TIER1_TABLES:
            cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            if any(c[1] == "plan" for c in cols):
                has_plan_anywhere = True
                break

        # Also check response_cache for plan-suffixed keys
        has_plan_keys = False
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM response_cache WHERE cache_key LIKE '%|plan=%'"
            ).fetchone()
            has_plan_keys = row[0] > 0
        except sqlite3.OperationalError:
            pass

        if not has_plan_anywhere and not has_plan_keys:
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
            return

        logger.info("Migration: removing plan column from Tier 1 tables")

        for table_name, schema in _TIER1_TABLES.items():
            cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            col_names = [c[1] for c in cols]
            if "plan" not in col_names:
                continue

            # Check if plan is in PK
            pk_cols = [c[1] for c in cols if c[5] > 0]

            if "plan" in pk_cols:
                # Rebuild table: deduplicate by inserting lowest plan first,
                # highest last (INSERT OR REPLACE keeps the last = highest)
                extra = f", {schema['extra_columns']}" if schema["extra_columns"] else ""
                new_ddl = f"""
                    CREATE TABLE {table_name}_v2 (
                        {schema["key_columns"]},
                        data TEXT NOT NULL,
                        fetched_at REAL NOT NULL
                        {extra},
                        PRIMARY KEY ({schema["primary_key"]})
                    )
                """
                conn.execute(new_ddl)

                # Build column list for SELECT (exclude plan)
                select_cols = [c for c in col_names if c != "plan"]
                select_str = ", ".join(select_cols)

                conn.execute(f"""
                    INSERT OR REPLACE INTO {table_name}_v2 ({select_str})
                    SELECT {select_str} FROM {table_name}
                    ORDER BY CASE plan
                        WHEN 'free' THEN 0
                        WHEN 'light' THEN 1
                        WHEN 'standard' THEN 2
                        WHEN 'premium' THEN 3
                        ELSE 0 END ASC
                """)

                old_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                new_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}_v2").fetchone()[0]

                conn.execute(f"DROP TABLE {table_name}")
                conn.execute(f"ALTER TABLE {table_name}_v2 RENAME TO {table_name}")
                logger.info(
                    "Migration: rebuilt %s without plan (PK rebuild, %d -> %d rows)",
                    table_name,
                    old_count,
                    new_count,
                )
            else:
                # plan is not in PK — just drop the column (SQLite 3.35+)
                try:
                    conn.execute(f"ALTER TABLE {table_name} DROP COLUMN plan")
                    logger.info("Migration: dropped plan column from %s", table_name)
                except sqlite3.OperationalError:
                    # Fallback for older SQLite: rebuild
                    extra = f", {schema['extra_columns']}" if schema["extra_columns"] else ""
                    new_ddl = f"""
                        CREATE TABLE {table_name}_v2 (
                            {schema["key_columns"]},
                            data TEXT NOT NULL,
                            fetched_at REAL NOT NULL
                            {extra},
                            PRIMARY KEY ({schema["primary_key"]})
                        )
                    """
                    conn.execute(new_ddl)
                    select_cols = [c for c in col_names if c != "plan"]
                    select_str = ", ".join(select_cols)
                    conn.execute(
                        f"INSERT OR REPLACE INTO {table_name}_v2 ({select_str}) "
                        f"SELECT {select_str} FROM {table_name}"
                    )
                    conn.execute(f"DROP TABLE {table_name}")
                    conn.execute(f"ALTER TABLE {table_name}_v2 RENAME TO {table_name}")
                    logger.info("Migration: rebuilt %s without plan (fallback)", table_name)

        # Tier 2: strip |plan=X suffix from response_cache keys
        if has_plan_keys:
            conn.execute("""
                CREATE TABLE response_cache_v2 (
                    cache_key TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    ttl_seconds INTEGER NOT NULL
                )
            """)
            conn.execute("""
                INSERT OR REPLACE INTO response_cache_v2
                    (cache_key, data, fetched_at, ttl_seconds)
                SELECT
                    CASE WHEN INSTR(cache_key, '|plan=') > 0
                        THEN SUBSTR(cache_key, 1, INSTR(cache_key, '|plan=') - 1)
                        ELSE cache_key END,
                    data, fetched_at, ttl_seconds
                FROM response_cache
                ORDER BY fetched_at ASC
            """)
            conn.execute("DROP TABLE response_cache")
            conn.execute("ALTER TABLE response_cache_v2 RENAME TO response_cache")
            logger.info("Migration: stripped plan suffix from response_cache keys")

        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        logger.info("Migration: plan removal complete (user_version=2)")

    def _migrate_normalize_fields(self) -> None:
        """Normalize legacy J-Quants v1 field names to v2 in Tier 1 data JSON.

        Runs once: skipped when ``PRAGMA user_version >= 1``.
        Rewrites the ``data`` column in-place, removing redundant legacy
        fields (Open, Close, AdjustmentOpen, etc.) and keeping only
        current short names (O, C, AdjO, etc.) with non-empty values.
        """
        conn = self._conn
        assert conn is not None

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 1:
            return

        total_updated = 0
        for table_name in _TIER1_TABLES:
            try:
                rows = conn.execute(f"SELECT rowid, data FROM {table_name}").fetchall()
            except sqlite3.OperationalError:
                continue

            updates: list[tuple[str, int]] = []
            for row in rows:
                original = row["data"]
                parsed = json.loads(original)
                # 旧フィールド名が含まれていなければスキップ
                if not any(k in parsed for k in _LEGACY_FIELD_MAP):
                    continue
                normalized = _normalize_fields(parsed)
                updates.append((json.dumps(normalized, ensure_ascii=False), row["rowid"]))

            if updates:
                conn.executemany(f"UPDATE {table_name} SET data = ? WHERE rowid = ?", updates)
                total_updated += len(updates)
                logger.info("Migration: normalized %d rows in %s", len(updates), table_name)

        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        if total_updated:
            logger.info("Migration: field normalization complete (%d rows total)", total_updated)

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
        plan: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve cached rows matching filters.

        Args:
            table: Tier 1 table name
            key_filter: Column name -> value pairs (e.g. {"code": "72030"})
            date_column: Name of the date column for range filtering
            date_from: Start date (inclusive)
            date_to: End date (inclusive)
            plan: Subscription plan for date range restriction.
                  Defaults to ``default_plan``.

        Returns:
            List of cached data dicts
        """
        if table not in _TIER1_TABLES:
            return []

        _validate_column(date_column, table)
        for col in key_filter:
            _validate_column(col, table)

        effective_plan = plan if plan is not None else self._default_plan
        conn = self._ensure_connection()
        if conn is None:
            return []
        where, params = _build_where_clause(
            key_filter, effective_plan, date_column, date_from, date_to
        )
        sql = f"SELECT data FROM {table} WHERE {where} ORDER BY {date_column}"
        rows = conn.execute(sql, params).fetchall()
        if table == "equities_bars_daily":
            return [_normalize_fields(json.loads(row["data"])) for row in rows]
        return [json.loads(row["data"]) for row in rows]

    def get_name_map(self) -> dict[str, str]:
        """Return a mapping of 5-digit code to company name from equities_master.

        Uses the most recent record per code. Falls back to the English name when
        the Japanese name is absent. Returns an empty dict when the table is empty
        or the connection is unavailable.

        Note: intentionally bypasses ``_build_where_clause`` / plan-based date
        restrictions.  ``equities_master`` is a reference table; plan gating on
        the name lookup would incorrectly suppress names for older codes.
        """
        conn = self._ensure_connection()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT e.data FROM equities_master e "
                "JOIN (SELECT code, MAX(date) AS max_date "
                "      FROM equities_master GROUP BY code) AS m "
                "ON e.code = m.code AND e.date = m.max_date"
            ).fetchall()
        except Exception:
            return {}
        result: dict[str, str] = {}
        for row in rows:
            try:
                data = json.loads(row["data"])
            except Exception:
                continue
            code = str(data.get("Code") or "")
            name = str(data.get("CoName") or data.get("CoNameEn") or "")
            if code and name:
                result[code] = name
        return result

    def get_sector_map(self) -> dict[str, dict[str, str]]:
        """Return a mapping of 5-digit code to sector codes/names from equities_master.

        Each value is a dict with keys ``s33``, ``s33_name``, ``s17``, ``s17_name``
        (each may be an empty string if the master row lacks that field).
        Uses the most recent record per code, mirroring ``get_name_map``.

        Note: intentionally bypasses ``_build_where_clause`` / plan-based date
        restrictions for the same reason as ``get_name_map``.
        """
        conn = self._ensure_connection()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT e.data FROM equities_master e "
                "JOIN (SELECT code, MAX(date) AS max_date "
                "      FROM equities_master GROUP BY code) AS m "
                "ON e.code = m.code AND e.date = m.max_date"
            ).fetchall()
        except Exception:
            return {}
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            try:
                data = json.loads(row["data"])
            except Exception:
                continue
            code = str(data.get("Code") or "")
            if not code:
                continue
            result[code] = {
                "s33": str(data.get("S33") or ""),
                "s33_name": str(data.get("S33Nm") or ""),
                "s17": str(data.get("S17") or ""),
                "s17_name": str(data.get("S17Nm") or ""),
            }
        return result

    def get_div_ann_map(self) -> dict[str, tuple[float, str]]:
        """Return a mapping of 5-digit code to (annual dividend per share, disclosure date).

        Uses the most recent disclosure date with a positive DivAnn per code,
        skipping interim/quarterly reports where DivAnn is empty or zero.
        The disclosure date is needed by callers to apply split adjustments.
        Returns an empty dict when the table is missing, empty, or the
        connection is unavailable.
        """
        conn = self._ensure_connection()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT f.code, json_extract(f.data, '$.DivAnn') AS div_ann, m.md "
                "FROM fins_summary f "
                "JOIN ("
                "  SELECT code, MAX(disc_date) AS md "
                "  FROM fins_summary "
                "  WHERE json_extract(data, '$.DivAnn') IS NOT NULL "
                "    AND json_extract(data, '$.DivAnn') != '' "
                "    AND CAST(json_extract(data, '$.DivAnn') AS REAL) > 0 "
                "  GROUP BY code"
                ") m ON f.code = m.code AND f.disc_date = m.md"
            ).fetchall()
        except Exception:
            return {}
        result: dict[str, tuple[float, str]] = {}
        for row in rows:
            code = str(row[0] or "")
            disc_date = str(row[2] or "")
            try:
                val = float(row[1])
            except (TypeError, ValueError):
                continue
            if code and val > 0 and disc_date:
                result[code] = (val, disc_date)
        return result

    def get_split_factors_after(self, code_disc_dates: dict[str, str]) -> dict[str, float]:
        """Return cumulative split adjustment factors for multiple codes.

        For each code, multiplies all AdjFactor values in equities_bars_daily
        where date > disc_date for that code.  Codes with no splits after their
        date are omitted (callers should default to 1.0).

        Splits on disc_date itself are excluded (strict ``>``, not ``>=``).
        In J-Quants, DisclosedDate (TDnet filing date) and adj_factor record
        date (split ex-date) coinciding on the same calendar day is virtually
        impossible in practice, so this boundary is safe to ignore.

        Args:
            code_disc_dates: Mapping of 5-digit code to disclosure date (YYYY-MM-DD).

        Returns:
            Mapping of code to cumulative split factor (e.g., 0.1 for a 1:10 split).
        """
        conn = self._ensure_connection()
        if conn is None or not code_disc_dates:
            return {}
        codes = list(code_disc_dates.keys())
        # Fetch all split rows for relevant codes; SQLite limit is 999 variables.
        all_rows: list[tuple[str, str, float]] = []
        for i in range(0, len(codes), 900):
            batch = codes[i : i + 900]
            placeholders = ",".join("?" * len(batch))
            try:
                rows = conn.execute(
                    f"SELECT code, date, adj_factor FROM equities_bars_daily "
                    f"WHERE adj_factor IS NOT NULL AND adj_factor != 1.0 "
                    f"AND adj_factor != 0.0 AND code IN ({placeholders})",
                    batch,
                ).fetchall()
            except Exception:
                continue
            all_rows.extend((str(r[0]), str(r[1]), float(r[2])) for r in rows)
        result: dict[str, float] = {}
        for code, bar_date, factor in all_rows:
            disc_date = code_disc_dates.get(code)
            if disc_date is None or bar_date <= disc_date:
                continue
            result[code] = result.get(code, 1.0) * factor
        return result

    def iter_session_dates(
        self,
        date_from: str,
        date_to: str,
    ) -> list[str]:
        """Return distinct trading dates in ``equities_bars_daily``.

        Used by the screener range tools to enumerate trading days
        without round-tripping through ``get_cached_dates`` (which
        requires a key_filter and would touch every code's row to
        deduplicate). The returned list is ordered ascending.
        """
        conn = self._ensure_connection()
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT DISTINCT date FROM equities_bars_daily "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (date_from, date_to),
        ).fetchall()
        return [str(r[0])[:10] for r in rows]

    def get_cached_dates(
        self,
        table: str,
        key_filter: dict[str, str],
        date_column: str = "date",
        date_from: str | None = None,
        date_to: str | None = None,
        plan: str | None = None,
    ) -> set[str]:
        """Return the set of dates already cached for the given key.

        Args:
            plan: Subscription plan for date range restriction.
                  Defaults to ``default_plan``.
        """
        if table not in _TIER1_TABLES:
            return set()

        _validate_column(date_column, table)
        for col in key_filter:
            _validate_column(col, table)

        effective_plan = plan if plan is not None else self._default_plan
        conn = self._ensure_connection()
        if conn is None:
            return set()
        where, params = _build_where_clause(
            key_filter, effective_plan, date_column, date_from, date_to
        )
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
        if conn is None:
            return 0
        now = time.time()
        count = 0

        has_adj = bool(adj_factor_key) and "adj_factor" in (
            _TIER1_TABLES[table].get("extra_columns", "")
        )

        for row in rows:
            key_values = [_normalize_date_value(str(row.get(k, ""))) for k in key_columns]
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

    def invalidate_rows(
        self,
        table: str,
        key_filter: dict[str, str],
    ) -> int:
        """Delete cached rows matching the filter (e.g. for stock split invalidation).

        Returns:
            Number of rows deleted
        """
        if table not in _TIER1_TABLES:
            return 0

        for col in key_filter:
            _validate_column(col, table)

        conn = self._ensure_connection()
        if conn is None:
            return 0
        conditions = [f"{col} = ?" for col in key_filter]
        params = list(key_filter.values())

        sql = f"DELETE FROM {table} WHERE {' AND '.join(conditions)}"
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    def check_adj_factor(
        self,
        code: str,
        new_adj_factor: float | None,
    ) -> bool:
        """Check if AdjFactor has changed for a stock (split detection).

        Returns:
            True if cache is valid (no split detected), False if invalidation needed
        """
        if new_adj_factor is None:
            return True

        conn = self._ensure_connection()
        if conn is None:
            return True  # DB 未準備 → 分割チェック不可、キャッシュなしとして扱う

        _adj_sql = (
            "SELECT adj_factor FROM equities_bars_daily WHERE code = ? ORDER BY date DESC LIMIT 1"
        )
        row = conn.execute(_adj_sql, (code,)).fetchone()

        if row is None:
            return True  # キャッシュなし → 問題なし

        cached_adj = row["adj_factor"]
        if cached_adj is not None and abs(cached_adj - new_adj_factor) > 1e-10:
            logger.info(
                "株式分割検知: code=%s (AdjFactor: %s → %s)", code, cached_adj, new_adj_factor
            )
            return False

        return True

    def get_cumulative_split_factor(self, code: str, target_date: str) -> float:
        """Get the cumulative split adjustment factor for a stock after target_date.

        J-Quants AdjFactor represents the split ratio on the day it occurred
        (e.g., 0.2 for a 1:5 split). This method multiplies all AdjFactor
        values after target_date to compute the total adjustment needed.

        Returns:
            Cumulative factor (e.g., 0.2 for a 1:5 split after target_date).
            Returns 1.0 if no splits occurred after target_date.
        """
        conn = self._ensure_connection()
        if conn is None:
            return 1.0
        rows = conn.execute(
            "SELECT adj_factor FROM equities_bars_daily "
            "WHERE code = ? AND date > ? AND adj_factor IS NOT NULL "
            "AND adj_factor != 1.0 AND adj_factor != 0.0",
            (code, target_date),
        ).fetchall()
        if not rows:
            return 1.0
        factor = 1.0
        for row in rows:
            factor *= row["adj_factor"]
        return factor

    def get_latest_adj_factor(self, code: str) -> float | None:
        """Get the most recent AdjFactor for a stock.

        Returns:
            Latest AdjFactor value, or None if no cached data available.
        """
        conn = self._ensure_connection()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT adj_factor FROM equities_bars_daily WHERE code = ? ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
        if row is None or row["adj_factor"] is None:
            return None
        return float(row["adj_factor"])

    # ----------------------------------------------------------------
    # Tier 2: レスポンスレベルキャッシュ
    # ----------------------------------------------------------------

    def get_response(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve a cached response if it exists and hasn't expired."""
        conn = self._ensure_connection()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT data, fetched_at, ttl_seconds FROM response_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()

        if row is None:
            return None

        age = time.time() - row["fetched_at"]
        if row["ttl_seconds"] > 0 and age > row["ttl_seconds"]:
            conn.execute("DELETE FROM response_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None

        return json.loads(row["data"])

    def put_response(
        self,
        cache_key: str,
        data: Any,
        ttl_seconds: int,
    ) -> None:
        """Store a response in the cache."""
        if ttl_seconds == TTL_NONE:
            return  # キャッシュしない設定

        conn = self._ensure_connection()
        if conn is None:
            return
        conn.execute(
            "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) "
            "VALUES (?, ?, ?, ?)",
            (cache_key, json.dumps(data, ensure_ascii=False), time.time(), ttl_seconds),
        )
        conn.commit()

    # ----------------------------------------------------------------
    # Screener result cache (pre-computed cross-sectional outputs)
    # Read-only on Cloud Run; writes happen on the self-hosted publisher
    # via daily_fetch and the one-off populate-history script.
    # ----------------------------------------------------------------

    def screener_result_get(
        self,
        tool_name: str,
        params_hash: str,
        date: str,
    ) -> dict[str, Any] | None:
        """Return the cached payload for ``(tool_name, params_hash, date)``.

        Returns ``None`` on miss or when the cache DB is not ready.
        """
        conn = self._ensure_connection()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT payload_json FROM screener_results "
            "WHERE tool_name = ? AND params_hash = ? AND date = ?",
            (tool_name, params_hash, date),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None

    def screener_result_get_range(
        self,
        tool_name: str,
        params_hash: str,
        date_from: str,
        date_to: str,
    ) -> dict[str, dict[str, Any]]:
        """Return all cached payloads in [date_from, date_to] keyed by date."""
        conn = self._ensure_connection()
        if conn is None:
            return {}
        rows = conn.execute(
            "SELECT date, payload_json FROM screener_results "
            "WHERE tool_name = ? AND params_hash = ? "
            "AND date >= ? AND date <= ? ORDER BY date",
            (tool_name, params_hash, date_from, date_to),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            try:
                out[str(r["date"])] = json.loads(r["payload_json"])
            except (TypeError, ValueError):
                continue
        return out

    def screener_result_put(
        self,
        tool_name: str,
        params_hash: str,
        date: str,
        payload: dict[str, Any],
    ) -> None:
        """INSERT OR REPLACE one row. Caller must own write rights.

        The MCP tool layer never calls this — Cloud Run instances run on
        ephemeral ``/tmp`` storage, so any tool-side write would be lost
        on cold start. The on-disk cache is owned by the self-hosted publisher
        daily fetch pipeline.
        """
        conn = self._ensure_connection()
        if conn is None:
            return
        conn.execute(
            "INSERT OR REPLACE INTO screener_results "
            "(tool_name, params_hash, date, payload_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                tool_name,
                params_hash,
                date,
                json.dumps(payload, ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()

    def screener_result_prune(self, retention_weeks: int = _SCREENER_CACHE_LOOKBACK_WEEKS) -> int:
        """Delete ``screener_results`` rows older than ``retention_weeks``.

        Returns the number of rows deleted. The cutoff is computed in
        Python (``date.today()``) rather than via SQLite's
        ``date('now')`` so the boundary follows the local timezone of
        the writing host (JST). SQLite ``date('now')`` is
        UTC and would shift the cutoff by ~9 hours during the JST
        evening window — harmless on rolling 52-week retention but
        avoidably ambiguous.
        """
        conn = self._ensure_connection()
        if conn is None:
            return 0
        cutoff = (date.today() - timedelta(weeks=int(retention_weeks))).isoformat()
        cursor = conn.execute(
            "DELETE FROM screener_results WHERE date < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        conn.commit()
        return deleted

    def screener_result_count(self) -> int:
        """Return the row count in ``screener_results`` (0 when not ready)."""
        conn = self._ensure_connection()
        if conn is None:
            return 0
        row = conn.execute("SELECT COUNT(*) FROM screener_results").fetchone()
        return int(row[0]) if row else 0

    def get_latest_equities_date(self) -> str | None:
        """Return the most recent date in equities_bars_daily, or None when not ready."""
        conn = self._ensure_connection()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT MAX(date) FROM equities_bars_daily").fetchone()
            return str(row[0])[:10] if row and row[0] else None
        except sqlite3.OperationalError:
            return None

    def get_trading_date_today(self) -> str:
        """Return today's date if it is a trading day, else the most recent trading day.

        Queries markets_calendar for the latest date with HolDivision='0' (trading day)
        at or before today. Falls back to the nearest past weekday when the calendar
        table is unavailable.
        """
        today = date.today()
        today_str = today.isoformat()
        conn = self._ensure_connection()
        if conn is not None:
            try:
                # LIMIT 14 covers Japan's longest holiday streak (~10 days,
                # e.g. Golden Week + adjacent weekends) plus a safety buffer.
                rows = conn.execute(
                    "SELECT date, data FROM markets_calendar "
                    "WHERE date <= ? ORDER BY date DESC LIMIT 14",
                    (today_str,),
                ).fetchall()
                for row in rows:
                    cal_data = json.loads(row[1]) if row[1] else {}
                    if str(cal_data.get("HolDivision", "")).strip() == "0":
                        return str(row[0])[:10]
            except Exception:
                pass
        d = today
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()

    # ----------------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return cache statistics."""
        conn = self._ensure_connection()

        stats: dict[str, Any] = {
            "db_path": str(self._db_path),
            "plan": self._default_plan,
        }

        if conn is None:
            # DB 未準備 — ファイルサイズだけ返す
            if self._db_path.exists():
                stats["db_size_mb"] = round(self._db_path.stat().st_size / (1024 * 1024), 2)
            return stats

        # Empty plan (auto-detect pending): show all tables without restriction
        plan_known = self._default_plan in _PLAN_LEVEL
        current_level = _PLAN_LEVEL.get(self._default_plan, 0) if plan_known else None
        for table_name in _TIER1_TABLES:
            if plan_known:
                min_plan = _TABLE_MIN_PLAN.get(table_name, "free")
                if _PLAN_LEVEL.get(min_plan, 0) > current_level:
                    stats[table_name] = None  # plan restriction
                    continue
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table_name}").fetchone()
            stats[table_name] = row["cnt"] if row else 0

        now = time.time()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM response_cache "
            "WHERE ttl_seconds = 0 OR fetched_at + ttl_seconds > ?",
            (now,),
        ).fetchone()
        stats["response_cache"] = row["cnt"] if row else 0

        # Evict expired entries while we're here
        conn.execute(
            "DELETE FROM response_cache WHERE ttl_seconds > 0 AND fetched_at + ttl_seconds <= ?",
            (now,),
        )
        conn.commit()

        # Pre-computed screener results
        try:
            stats["screener_results"] = self.screener_result_count()
        except sqlite3.OperationalError:
            stats["screener_results"] = 0

        # DB ファイルサイズ
        if self._db_path.exists():
            stats["db_size_mb"] = round(self._db_path.stat().st_size / (1024 * 1024), 2)

        stats["integrity"] = self._integrity_status
        return stats

    def clear(self, table: str | None = None) -> dict[str, int]:
        """Clear cache data.

        Args:
            table: If specified, clear only this table. Otherwise clear all.

        Returns:
            Dict of table_name -> rows_deleted
        """
        if table is not None:
            _validate_table(table)

        conn = self._ensure_connection()
        if conn is None:
            return {}

        result: dict[str, int] = {}

        tables = (
            [table]
            if table
            else list(_TIER1_TABLES.keys()) + ["response_cache", "screener_results"]
        )
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
        self._ready = False


def _plan_date_bounds(plan: str) -> tuple[str | None, str | None]:
    """Return (min_date, max_date) for the given subscription plan.

    Args:
        plan: Subscription plan name (free, light, standard, premium).

    Returns:
        Tuple of (earliest_date, latest_date) as ISO strings.
        ``None`` means no limit on that side.
    """
    retention_years = _PLAN_RETENTION_YEARS.get(plan)
    if retention_years is None:
        return (None, None)

    today = date.today()
    try:
        min_date = today.replace(year=today.year - retention_years)
    except ValueError:
        # 2/29 → 2/28 フォールバック（うるう年対策）
        min_date = today.replace(year=today.year - retention_years, month=today.month, day=28)

    max_date = None
    if plan == "free":
        max_date = (today - timedelta(weeks=_FREE_DELAY_WEEKS)).isoformat()

    return (min_date.isoformat(), max_date)


def _build_where_clause(
    key_filter: dict[str, str],
    effective_plan: str,
    date_column: str = "date",
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list[str]]:
    """Build a WHERE clause and parameter list for Tier 1 cache queries.

    Date range is restricted based on ``effective_plan`` so that
    lower-tier users see only their entitled window
    (e.g. Free = 2 years with 12-week delay).

    Args:
        key_filter: Column name -> value pairs (e.g. {"code": "72030"})
        effective_plan: Subscription plan (for date restriction only).
        date_column: Name of the date column for range filtering.
        date_from: Start date (inclusive).
        date_to: End date (inclusive).

    Returns:
        (where_clause, params) tuple ready to be used in a SQL query.
    """
    conditions: list[str] = []
    params: list[str] = []

    for col, val in key_filter.items():
        conditions.append(f"{col} = ?")
        params.append(val)

    # plan カラムではフィルタしない（DB に複数プランのデータが混在しても OK）
    # プラン別日付範囲制限を適用
    plan_min, plan_max = _plan_date_bounds(effective_plan)
    if plan_min and (not date_from or date_from < plan_min):
        date_from = plan_min
    if plan_max and (not date_to or date_to > plan_max):
        date_to = plan_max

    if date_from:
        conditions.append(f"{date_column} >= ?")
        params.append(date_from)
    if date_to:
        conditions.append(f"{date_column} <= ?")
        params.append(date_to)

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


def _validate_column(name: str, table: str) -> None:
    """Raise ValueError if *name* is not a known column for *table*.

    Prevents SQL injection via untrusted column name interpolation.
    """
    allowed = _TIER1_KEY_COLUMNS.get(table, frozenset())
    if name not in allowed:
        raise ValueError(
            f"Invalid column name {name!r} for table {table!r}. Allowed: {sorted(allowed)}"
        )


def _validate_table(name: str) -> None:
    """Raise ValueError if *name* is not a known cache table.

    Prevents SQL injection via untrusted table name interpolation.
    """
    if name not in _ALL_TABLE_NAMES:
        raise ValueError(f"Invalid table name {name!r}. Allowed: {sorted(_ALL_TABLE_NAMES)}")


def _key_col_names(table: str) -> list[str]:
    """Extract key column names from table schema definition."""
    schema = _TIER1_TABLES[table]
    # "code TEXT NOT NULL, date TEXT NOT NULL" -> ["code", "date"]
    return [part.strip().split()[0] for part in schema["key_columns"].split(",")]


def _normalize_date_value(value: str) -> str:
    """Normalize date-like strings: strip time suffix and hyphens.

    "2026-03-16 00:00:00" -> "2026-03-16"
    "2026-03-16T00:00:00" -> "2026-03-16"
    """
    if " " in value:
        value = value.split(" ")[0]
    elif "T" in value:
        value = value.split("T")[0]
    return value


def make_cache_key(endpoint: str, params: dict[str, Any] | None = None) -> str:
    """Generate a deterministic cache key from endpoint and params."""
    parts = [endpoint]
    if params:
        sorted_params = sorted((k, str(v)) for k, v in params.items() if v is not None)
        parts.append("&".join(f"{k}={v}" for k, v in sorted_params))
    return "|".join(parts)

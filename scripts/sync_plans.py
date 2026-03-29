"""Sync Standard plan data to Free/Light plans and purge expired rows.

Called after daily_fetch.py / import_csv_to_cache.py to propagate
Standard-plan data into lower-tier plans and delete rows outside
each plan's retention window.

Dependencies: standard library only (runs in jpx-short-report .venv).

Usage:
    python3 scripts/sync_plans.py                    # デフォルト DB
    python3 scripts/sync_plans.py --db /path/to.db   # カスタム DB パス
    python3 scripts/sync_plans.py --dry-run           # 削除をスキップ
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# キャッシュ DB のデフォルトパス
DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-dat-mcp" / "cache.db"

# テーブル定義: テーブル名 → 日付カラム名
# 日付カラムが None のテーブルは日付制限なし（全コピー）
TABLE_DATE_COLUMN: dict[str, str | None] = {
    "equities_bars_daily": "date",
    "equities_master": None,  # 日付制限なし
    "fins_summary": "disc_date",
    "indices_bars_daily_topix": "date",
    "investor_types": "pub_date",
    "markets_margin_interest": "date",
    "markets_margin_alert": "date",
    "markets_short_ratio": "date",
    "markets_calendar": None,  # 日付制限なし
    "markets_breakdown": "date",
}

# プラン別対象テーブル（上位プランは下位プランのテーブルを全て含む）
FREE_TABLES = {
    "equities_bars_daily",
    "equities_master",
    "fins_summary",
    "markets_calendar",
}

LIGHT_TABLES = FREE_TABLES | {
    "indices_bars_daily_topix",
    "investor_types",
}

STANDARD_TABLES = LIGHT_TABLES | {
    "markets_margin_interest",
    "markets_margin_alert",
    "markets_short_ratio",
}

PREMIUM_TABLES = STANDARD_TABLES | {
    "markets_breakdown",
}

# プラン別データ保持期間
# Free: 過去2年（12週間遅延あり）
# Light: 過去5年
# Standard: 過去10年
# Premium: 全期間
FREE_RETENTION_YEARS = 2
FREE_DELAY_WEEKS = 12
LIGHT_RETENTION_YEARS = 5
STANDARD_RETENTION_YEARS = 10


def _get_all_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return all column names for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _copy_rows(
    conn: sqlite3.Connection,
    table: str,
    src_plan: str,
    dst_plan: str,
    date_col: str | None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Copy rows from src_plan to dst_plan using INSERT OR REPLACE.

    Returns the number of rows copied.
    """
    columns = _get_all_columns(conn, table)
    # plan 以外のカラムを src から取得し、plan を dst_plan に差し替える
    src_cols = ", ".join(c if c != "plan" else f"'{dst_plan}'" for c in columns)

    conditions = ["plan = ?"]
    params: list[str] = [src_plan]

    if date_col is not None and date_from is not None:
        conditions.append(f"{date_col} >= ?")
        params.append(date_from)
    if date_col is not None and date_to is not None:
        conditions.append(f"{date_col} < ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    col_list = ", ".join(columns)
    sql = (
        f"INSERT OR REPLACE INTO {table} ({col_list}) SELECT {src_cols} FROM {table} WHERE {where}"
    )
    cursor = conn.execute(sql, params)
    return cursor.rowcount


def _delete_old_rows(
    conn: sqlite3.Connection,
    table: str,
    plan: str,
    date_col: str,
    cutoff: str,
) -> int:
    """Delete rows older than cutoff date for a given plan.

    Returns the number of rows deleted.
    """
    sql = f"DELETE FROM {table} WHERE plan = ? AND {date_col} < ?"
    cursor = conn.execute(sql, [plan, cutoff])
    return cursor.rowcount


def sync_plans(db_path: Path, dry_run: bool = False) -> None:
    """Sync Standard plan data to lower-tier plans and purge expired data."""
    if not db_path.exists():
        logger.error("DB ファイルが見つかりません: %s", db_path)
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    today = date.today()
    # 日付カットオフ計算
    free_oldest = (
        today - timedelta(days=FREE_RETENTION_YEARS * 365) - timedelta(weeks=FREE_DELAY_WEEKS)
    ).isoformat()
    free_newest = (today - timedelta(weeks=FREE_DELAY_WEEKS)).isoformat()
    light_oldest = (today - timedelta(days=LIGHT_RETENTION_YEARS * 365)).isoformat()
    standard_oldest = (today - timedelta(days=STANDARD_RETENTION_YEARS * 365)).isoformat()

    logger.info("=== sync_plans 開始 ===")
    logger.info("DB: %s", db_path)
    logger.info(
        "today=%s, free_oldest=%s, free_newest=%s, light_oldest=%s, standard_oldest=%s",
        today,
        free_oldest,
        free_newest,
        light_oldest,
        standard_oldest,
    )
    if dry_run:
        logger.info("*** DRY-RUN モード: DELETE はスキップされます ***")

    try:
        # -------------------------------------------------------
        # Step 1: Standard → Light コピー（対象テーブル全て）
        # -------------------------------------------------------
        logger.info("--- Standard → Light コピー ---")
        for table in sorted(LIGHT_TABLES):
            if table not in TABLE_DATE_COLUMN:
                continue
            date_col = TABLE_DATE_COLUMN[table]
            if date_col is None:
                # 日付制限なし: 全データコピー
                count = _copy_rows(conn, table, "standard", "light", None)
            else:
                count = _copy_rows(
                    conn, table, "standard", "light", date_col, date_from=light_oldest
                )
            logger.info("  %s: %d rows copied", table, count)

        conn.commit()

        # -------------------------------------------------------
        # Step 2: Standard → Free コピー（対象テーブル、遅延あり）
        # -------------------------------------------------------
        logger.info("--- Standard → Free コピー ---")
        for table in sorted(FREE_TABLES):
            if table not in TABLE_DATE_COLUMN:
                continue
            date_col = TABLE_DATE_COLUMN[table]
            if date_col is None:
                # 日付制限なし: 全データコピー
                count = _copy_rows(conn, table, "standard", "free", None)
            else:
                # Free は12週間遅延 + 過去2年
                count = _copy_rows(
                    conn,
                    table,
                    "standard",
                    "free",
                    date_col,
                    date_from=free_oldest,
                    date_to=free_newest,
                )
            logger.info("  %s: %d rows copied", table, count)

        conn.commit()

        # -------------------------------------------------------
        # Step 3: 各プランの古いデータを DELETE
        # -------------------------------------------------------
        logger.info("--- 古いデータの削除 ---")

        # Free: 2年+12週より古いデータを削除
        for table in sorted(FREE_TABLES):
            date_col = TABLE_DATE_COLUMN.get(table)
            if date_col is None:
                # 日付制限なしのテーブルは削除不要
                continue
            if dry_run:
                # 削除対象件数を確認
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan = 'free' AND {date_col} < ?",
                    [free_oldest],
                )
                count = cursor.fetchone()[0]
                logger.info("  %s (free): %d rows would be deleted [DRY-RUN]", table, count)
            else:
                count = _delete_old_rows(conn, table, "free", date_col, free_oldest)
                logger.info("  %s (free): %d rows deleted", table, count)

        # Free: 12週遅延より新しいデータも削除（遅延期間内のデータ）
        for table in sorted(FREE_TABLES):
            date_col = TABLE_DATE_COLUMN.get(table)
            if date_col is None:
                continue
            if dry_run:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan = 'free' AND {date_col} >= ?",
                    [free_newest],
                )
                count = cursor.fetchone()[0]
                logger.info("  %s (free, delay): %d rows would be deleted [DRY-RUN]", table, count)
            else:
                sql = f"DELETE FROM {table} WHERE plan = 'free' AND {date_col} >= ?"
                cursor = conn.execute(sql, [free_newest])
                count = cursor.rowcount
                logger.info("  %s (free, delay): %d rows deleted", table, count)

        # Light: 5年より古いデータを削除
        for table in sorted(LIGHT_TABLES):
            date_col = TABLE_DATE_COLUMN.get(table)
            if date_col is None:
                continue
            if dry_run:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan = 'light' AND {date_col} < ?",
                    [light_oldest],
                )
                count = cursor.fetchone()[0]
                logger.info("  %s (light): %d rows would be deleted [DRY-RUN]", table, count)
            else:
                count = _delete_old_rows(conn, table, "light", date_col, light_oldest)
                logger.info("  %s (light): %d rows deleted", table, count)

        # Standard: 10年より古いデータを削除
        for table in sorted(STANDARD_TABLES):
            date_col = TABLE_DATE_COLUMN.get(table)
            if date_col is None:
                continue
            if dry_run:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan = 'standard' AND {date_col} < ?",
                    [standard_oldest],
                )
                count = cursor.fetchone()[0]
                logger.info("  %s (standard): %d rows would be deleted [DRY-RUN]", table, count)
            else:
                count = _delete_old_rows(conn, table, "standard", date_col, standard_oldest)
                logger.info("  %s (standard): %d rows deleted", table, count)

        if not dry_run:
            conn.commit()

        logger.info("=== sync_plans 完了 ===")

    finally:
        conn.close()


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Sync Standard plan data to Free/Light plans and purge expired rows.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to cache.db (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    args = parser.parse_args()
    sync_plans(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

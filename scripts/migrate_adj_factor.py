#!/usr/bin/env python3
"""One-time migration: backfill adj_factor from JSON data column.

The equities_bars_daily table has an adj_factor column added after the
initial bulk import. Rows inserted before this schema change have
adj_factor IS NULL while the value is stored in data JSON as $.AdjFactor.

This script reads $.AdjFactor from the data column and writes it back
to adj_factor for all rows where adj_factor IS NULL.

Usage:
    uv run python scripts/migrate_adj_factor.py
    uv run python scripts/migrate_adj_factor.py --db /path/to/cache.db
    uv run python scripts/migrate_adj_factor.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-mcp" / "cache.db"
BATCH_SIZE = 50_000


def count_null_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM equities_bars_daily WHERE adj_factor IS NULL"
    ).fetchone()
    return row[0] if row else 0


def count_non_trivial_rows(conn: sqlite3.Connection) -> int:
    """Count rows where JSON AdjFactor exists and is != 1.0."""
    row = conn.execute(
        "SELECT COUNT(*) FROM equities_bars_daily "
        "WHERE adj_factor IS NULL "
        "AND json_extract(data, '$.AdjFactor') IS NOT NULL "
        "AND json_extract(data, '$.AdjFactor') != 1.0 "
        "AND json_extract(data, '$.AdjFactor') != 0.0"
    ).fetchone()
    return row[0] if row else 0


def run_backfill(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Update adj_factor from JSON data in batches. Returns total rows updated."""
    if dry_run:
        null_count = count_null_rows(conn)
        non_trivial = count_non_trivial_rows(conn)
        logger.info("dry-run: %s rows have adj_factor IS NULL", f"{null_count:,}")
        logger.info(
            "dry-run: %s rows have AdjFactor != 1.0 in JSON (split events)", f"{non_trivial:,}"
        )
        return 0

    total_updated = 0
    batch_num = 0
    t0 = time.time()

    while True:
        batch_num += 1
        t_batch = time.time()

        conn.execute("BEGIN")
        result = conn.execute(
            f"""
            UPDATE equities_bars_daily
            SET adj_factor = json_extract(data, '$.AdjFactor')
            WHERE rowid IN (
                SELECT rowid FROM equities_bars_daily
                WHERE adj_factor IS NULL
                LIMIT {BATCH_SIZE}
            )
            """
        )
        updated = result.rowcount
        conn.execute("COMMIT")

        if updated == 0:
            break

        total_updated += updated
        elapsed_batch = time.time() - t_batch
        elapsed_total = time.time() - t0
        logger.info(
            "batch %d: %s rows updated in %.1fs (total: %s, elapsed: %.0fs)",
            batch_num,
            f"{updated:,}",
            elapsed_batch,
            f"{total_updated:,}",
            elapsed_total,
        )

    return total_updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill adj_factor column from JSON data in equities_bars_daily.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Cache DB path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without making changes.",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run VACUUM after backfill to reclaim WAL space.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        logger.error("DB not found: %s", args.db)
        raise SystemExit(1)

    db_size_before = args.db.stat().st_size / (1024**3)
    logger.info("DB: %s (%.2f GB)", args.db, db_size_before)

    conn = sqlite3.connect(str(args.db))
    conn.isolation_level = None  # autocommit; manual BEGIN/COMMIT in run_backfill
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        null_before = count_null_rows(conn)
        logger.info("adj_factor IS NULL before: %s rows", f"{null_before:,}")

        total = run_backfill(conn, dry_run=args.dry_run)

        if not args.dry_run:
            null_after = count_null_rows(conn)
            logger.info("adj_factor IS NULL after:  %s rows", f"{null_after:,}")
            logger.info("total rows updated: %s", f"{total:,}")

            if args.vacuum:
                logger.info("running VACUUM (this may take several minutes)...")
                t_vac = time.time()
                conn.execute("VACUUM")
                logger.info("VACUUM done in %.0fs", time.time() - t_vac)
                db_size_after = args.db.stat().st_size / (1024**3)
                logger.info(
                    "DB size: %.2f GB -> %.2f GB (delta %.2f GB)",
                    db_size_before,
                    db_size_after,
                    db_size_after - db_size_before,
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

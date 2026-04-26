"""One-off back-fill of the ``screener_results`` cache.

Companion to ``scripts/daily_fetch.py``. ``daily_fetch`` only computes
the latest finalized session each night; this script back-fills the
prior 52 weeks (or any custom window) so the cache is dense from day 1.

Run on m1.local after deploying the Issue #142 schema migration:

    uv run python3 scripts/screener_populate_history.py
    uv run python3 scripts/screener_populate_history.py --weeks 8
    uv run python3 scripts/screener_populate_history.py --from 2025-01-04 --to 2026-04-25
    uv run python3 scripts/screener_populate_history.py --skip-existing

After 252 days have elapsed since merge, every nightly populate already
covers the rolling window and this script becomes a no-op.

Like ``daily_fetch.py`` this script depends only on ``jquantsapi`` and
the standard library; it imports from the ``jquants_mcp.cache``
package only via stdlib-only modules (``schema``, ``screener_compute``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_mcp.cache import screener_compute  # noqa: E402  # stdlib-only
from jquants_mcp.cache.schema import (  # noqa: E402
    RESPONSE_CACHE_DDL,
    SCREENER_RESULTS_DDL,
    SCREENER_RESULTS_INDEX_DDL,
    TIER1_TABLES,
    generate_ddl,
)

DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-mcp" / "cache.db"
DEFAULT_RETENTION_WEEKS = 52


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the tables this script writes to if they do not exist."""
    conn.execute(generate_ddl("equities_bars_daily", TIER1_TABLES["equities_bars_daily"]))
    conn.execute(SCREENER_RESULTS_DDL)
    conn.execute(SCREENER_RESULTS_INDEX_DDL)
    # ``daily_fetch`` and the MCP server both expect the response cache
    # to exist; create defensively in case this script runs against a
    # fresh DB ahead of the others.
    conn.execute(RESPONSE_CACHE_DDL)
    conn.commit()


def _existing_dates(
    conn: sqlite3.Connection,
    tool_name: str,
    params_hash: str,
) -> set[str]:
    """Dates already cached for the given (tool_name, params_hash) tuple.

    Looked up once per job before iterating sessions because we only
    cache one ``params_hash`` per tool today (the default-params
    cross-sectional output). If a future change starts caching multiple
    parameter shapes per tool, this lookup will need a per-iteration
    refresh or a (tool_name, params_hash) batch query rebuild.
    """
    rows = conn.execute(
        "SELECT date FROM screener_results WHERE tool_name = ? AND params_hash = ?",
        (tool_name, params_hash),
    ).fetchall()
    return {str(r[0])[:10] for r in rows}


def _date_window(
    conn: sqlite3.Connection,
    *,
    weeks: int,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, str]:
    if date_from and date_to:
        return date_from, date_to
    latest = screener_compute.latest_session_date(conn)
    if latest is None:
        raise SystemExit("equities_bars_daily が空のため back-fill できません")
    end = date_to or latest
    if date_from:
        return date_from, end
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(weeks=weeks)).date().isoformat()
    return start, end


def main() -> None:
    parser = argparse.ArgumentParser(
        description="screener_results を 52 週分（またはカスタム範囲で）一括計算",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"キャッシュ DB パス (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_RETENTION_WEEKS,
        help=f"過去 N 週分を計算（default: {DEFAULT_RETENTION_WEEKS}）",
    )
    parser.add_argument("--from", dest="date_from", help="開始日 YYYY-MM-DD（--weeks より優先）")
    parser.add_argument("--to", dest="date_to", help="終了日 YYYY-MM-DD（既定は最新営業日）")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="既に screener_results にある日付はスキップ（resume / 増分実行向け）",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="完了後に保持期間外の行を削除（既定は --weeks に合わせる）",
    )
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"cache.db が見つかりません: {args.db}")

    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)

    date_from, date_to = _date_window(
        conn, weeks=args.weeks, date_from=args.date_from, date_to=args.date_to
    )
    print(f"対象範囲: {date_from} → {date_to}")

    sessions = screener_compute.distinct_session_dates(conn, date_from, date_to)
    if not sessions:
        print("対象期間に営業日データがありません")
        return
    print(f"対象営業日: {len(sessions)} 日")

    jobs = [
        (
            screener_compute.TOOL_DETECT_52W,
            screener_compute.default_params_hash_52w(),
            {
                "window_sessions": screener_compute.DEFAULT_FIFTY_TWO_WEEK_SESSIONS,
                "min_prior_sessions": screener_compute.DEFAULT_MIN_PRIOR_SESSIONS,
                "mode_label": "52w",
            },
        ),
        (
            screener_compute.TOOL_DETECT_YTD,
            screener_compute.default_params_hash_ytd(),
            {
                "window_sessions": None,
                "min_prior_sessions": screener_compute.DEFAULT_MIN_PRIOR_SESSIONS,
                "mode_label": "ytd",
            },
        ),
    ]

    total_written = 0
    overall_t0 = time.time()
    for tool_name, params_hash, kwargs in jobs:
        print(f"== {tool_name} ==")
        existing = _existing_dates(conn, tool_name, params_hash) if args.skip_existing else set()
        written = 0
        t0 = time.time()
        for i, d in enumerate(sessions, start=1):
            if d in existing:
                continue
            payload = screener_compute.compute_for_date(
                conn,
                norm_date=d,
                **kwargs,
            )
            screener_compute.upsert_screener_result(
                conn,
                tool_name=tool_name,
                params_hash_value=params_hash,
                norm_date=d,
                payload=payload,
                computed_at=time.time(),
            )
            written += 1
            if i % 10 == 0:
                conn.commit()
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0.0
                print(
                    f"  {i}/{len(sessions)} ({d}) — count={payload.get('count'):>5}"
                    f"  | {rate:.2f} days/s"
                )
        conn.commit()
        total_written += written
        print(
            f"  完了: 書き込み {written} 行 / スキップ {len(sessions) - written} 行"
            f" ({time.time() - t0:.1f}s)"
        )

    if args.prune:
        deleted = screener_compute.prune_old_results(conn, retention_weeks=args.weeks)
        conn.commit()
        if deleted:
            print(f"保持期間外を削除: {deleted} 行")

    print(f"=== 全体 {total_written} 行 ({time.time() - overall_t0:.1f}s) ===")
    conn.close()


if __name__ == "__main__":
    main()

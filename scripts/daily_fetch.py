"""Fetch daily J-Quants data and insert directly into SQLite cache.

jpx-short-report の daily.sh から呼び出され、jquantsapi.ClientV2 で
追加データを取得して jquants-dat-mcp のキャッシュに直接投入する。

依存: jquantsapi + 標準ライブラリのみ（jpx-short-report の .venv で動作可能）

プラン設定は ~/.config/jquants-dat-mcp/config.ini の [jquants] plan を参照。
プランに応じて取得対象を自動決定する（--オプションで個別指定も可能）。

Usage:
    python3 scripts/daily_fetch.py                  # プランに応じた全データ取得
    python3 scripts/daily_fetch.py --topix           # TOPIX のみ
    python3 scripts/daily_fetch.py --fins-summary    # 決算サマリーのみ
    python3 scripts/daily_fetch.py --earnings-cal    # 決算発表予定のみ
    python3 scripts/daily_fetch.py --short-ratio     # 業種別空売り比率のみ (Standard+)
    python3 scripts/daily_fetch.py --margin-interest # 信用取引残高のみ (Standard+)
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import jquantsapi

# キャッシュ DB のデフォルトパス
DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-dat-mcp" / "cache.db"

# 決算サマリーのデフォルト遡り日数
FINS_LOOKBACK_DAYS = 7

# プラン別取得可能エンドポイント（J-Quants API v2 仕様に基づく）
# 各エンドポイントの最低必要プランを定義
PLAN_LEVELS = {"free": 0, "light": 1, "standard": 2, "premium": 3}

ENDPOINT_MIN_PLAN: dict[str, str] = {
    "fins_summary": "free",
    "earnings_cal": "free",
    "topix": "light",
    "investor_types": "light",
    "short_ratio": "standard",
    "margin_interest": "standard",
    "margin_alert": "standard",
    "short_sale_report": "standard",
    "breakdown": "premium",
}

# Tier 2 キャッシュの TTL（秒）— MCP サーバーの設定に合わせる
TTL_6H = 6 * 3600
TTL_24H = 24 * 3600
TTL_7D = 7 * 24 * 3600
TTL_90D = 90 * 24 * 3600


def _load_plan() -> str:
    """config.ini と環境変数からプランを読み取る。"""
    # 環境変数が最優先
    plan = os.environ.get("JQUANTS_PLAN")
    if plan:
        return plan.lower()

    # config.ini を探索
    config = configparser.ConfigParser()
    search_paths = [
        str(Path.home() / ".config" / "jquants-dat-mcp" / "config.ini"),
        "config.ini",
    ]
    config.read(search_paths, encoding="utf-8")

    try:
        return config.get("jquants", "plan").lower()
    except (configparser.NoSectionError, configparser.NoOptionError):
        return "free"


def _available_endpoints(plan: str) -> list[str]:
    """プランで取得可能なエンドポイント一覧を返す。"""
    plan_level = PLAN_LEVELS.get(plan, 0)
    return [
        ep
        for ep, min_plan in ENDPOINT_MIN_PLAN.items()
        if PLAN_LEVELS.get(min_plan, 0) <= plan_level
    ]


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """必要なテーブルを作成する。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indices_bars_daily_topix (
            date TEXT NOT NULL PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fins_summary (
            code TEXT NOT NULL,
            disc_date TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (code, disc_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS investor_types (
            pub_date TEXT NOT NULL,
            section TEXT NOT NULL,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (pub_date, section)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS response_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )
    """)
    conn.commit()


def _sanitize_row(row_data: dict) -> dict:
    """NaN を None に変換する（JSON シリアライズ用）。"""
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in row_data.items()}


def fetch_topix(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """TOPIX 日足を差分取得してキャッシュに投入する。"""
    row = conn.execute("SELECT MAX(date) FROM indices_bars_daily_topix").fetchone()
    max_date = row[0] if row and row[0] else None

    if max_date:
        # 日付に時刻部分が含まれる場合があるので先頭10文字だけ使う
        from_date = (datetime.strptime(max_date[:10], "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y%m%d"
        )
        print(f"  キャッシュ最新日: {max_date}、{from_date} から取得")
        df = cli.get_idx_bars_daily_topix(from_yyyymmdd=from_date)
    else:
        print("  キャッシュ空、全期間取得")
        df = cli.get_idx_bars_daily_topix()

    if df is None or len(df) == 0:
        print("  新しいデータなし")
        return 0

    now = time.time()
    count = 0
    for _, r in df.iterrows():
        data_json = json.dumps(_sanitize_row(r.to_dict()), ensure_ascii=False, default=str)
        conn.execute(
            "INSERT OR REPLACE INTO indices_bars_daily_topix (date, data, fetched_at) VALUES (?, ?, ?)",
            (str(r["Date"]), data_json, now),
        )
        count += 1

    conn.commit()
    return count


def fetch_fins_summary(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """決算サマリーを直近日付分取得してキャッシュに投入する。"""
    today = datetime.today()
    count = 0
    now = time.time()

    for days_ago in range(FINS_LOOKBACK_DAYS):
        date = today - timedelta(days=days_ago)
        date_str = date.strftime("%Y%m%d")
        date_iso = date.strftime("%Y-%m-%d")

        try:
            df = cli.get_fin_summary(date_yyyymmdd=date_str)
        except Exception as e:
            print(f"  {date_iso}: エラー ({e})")
            continue

        if df is None or len(df) == 0:
            continue

        for _, r in df.iterrows():
            data_json = json.dumps(_sanitize_row(r.to_dict()), ensure_ascii=False, default=str)
            code = str(r.get("Code", ""))
            disc_date = str(r.get("DiscDate", date_iso))
            conn.execute(
                "INSERT OR REPLACE INTO fins_summary (code, disc_date, data, fetched_at) VALUES (?, ?, ?, ?)",
                (code, disc_date, data_json, now),
            )
            count += 1

        conn.commit()
        print(f"  {date_iso}: {len(df)} 件")

    return count


def fetch_earnings_calendar(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """決算発表予定を取得して Tier 2 レスポンスキャッシュに日付別で蓄積する。

    APIは翌営業日の発表予定を返す。日付付きキーで保存することで
    約3ヶ月分（TTL 90日）の決算カレンダーを蓄積できる。
    """
    df = cli.get_eq_earnings_cal()

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    records = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]

    # 発表予定日（Date フィールド）を取得してキーに含める
    date_str = records[0].get("Date", "") if records else ""
    if date_str:
        date_key = date_str.replace("-", "")
    else:
        date_key = datetime.today().strftime("%Y%m%d")

    now = time.time()
    response_data = json.dumps(records, ensure_ascii=False, default=str)

    # 日付別キーで蓄積（TTL 90日）
    cache_key = f"/equities/earnings-calendar?date={date_key}"
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (cache_key, response_data, now, TTL_90D),
    )

    # パラメータなしキーも更新（最新データ用）
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        ("/equities/earnings-calendar", response_data, now, TTL_90D),
    )

    conn.commit()

    return len(records)


def fetch_investor_types(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """投資部門別売買動向を取得して Tier 1 キャッシュに投入する (Light+)。

    週次データ（毎週木曜公表）。直近2週間分を取得して差分投入する。
    """
    today = datetime.today()
    from_date = (today - timedelta(days=14)).strftime("%Y%m%d")
    to_date = today.strftime("%Y%m%d")

    df = cli.get_eq_investor_types(from_yyyymmdd=from_date, to_yyyymmdd=to_date)

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    now = time.time()
    count = 0
    for _, r in df.iterrows():
        data_json = json.dumps(_sanitize_row(r.to_dict()), ensure_ascii=False, default=str)
        pub_date = str(r.get("PublishedDate", r.get("PubDate", "")))
        section = str(r.get("Section", ""))
        conn.execute(
            "INSERT OR REPLACE INTO investor_types (pub_date, section, data, fetched_at) VALUES (?, ?, ?, ?)",
            (pub_date, section, data_json, now),
        )
        count += 1

    conn.commit()
    return count


def _store_response_cache(
    conn: sqlite3.Connection,
    cache_key: str,
    records: list[dict],
    ttl: int,
) -> int:
    """レコード群を Tier 2 レスポンスキャッシュに投入する汎用ヘルパー。"""
    if not records:
        print("  データなし")
        return 0

    response_data = json.dumps(records, ensure_ascii=False, default=str)
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (cache_key, response_data, now, ttl),
    )
    conn.commit()
    return len(records)


def _fetch_daily_to_cache(
    cli_method,
    conn: sqlite3.Connection,
    cache_key: str,
    ttl: int,
    *,
    date_param: str = "date_yyyymmdd",
) -> int:
    """当日分のデータを取得して Tier 2 キャッシュに投入する汎用関数。

    403 等の権限エラーは捕捉してスキップする。
    当日データが空の場合はパラメータなしでフォールバック取得を試行する。
    """
    today = datetime.today().strftime("%Y%m%d")
    try:
        df = cli_method(**{date_param: today})
    except Exception as e:
        print(f"  エラー: {e}")
        return 0

    if df is None or len(df) == 0:
        print("  当日データなし、パラメータなしで取得")
        try:
            df = cli_method()
        except Exception as e:
            print(f"  フォールバックエラー: {e}")
            return 0

    if df is None or len(df) == 0:
        print("  データなし")
        return 0

    records = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_response_cache(conn, cache_key, records, ttl)


def fetch_short_ratio(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """業種別空売り比率を取得して Tier 2 キャッシュに投入する (Standard+)。"""
    return _fetch_daily_to_cache(
        cli.get_mkt_short_ratio,
        conn,
        "/markets/short-ratio",
        TTL_24H,
    )


def fetch_margin_interest(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """信用取引残高を取得して Tier 2 キャッシュに投入する (Standard+)。"""
    return _fetch_daily_to_cache(
        cli.get_mkt_margin_interest,
        conn,
        "/markets/margin-interest",
        TTL_7D,
    )


def fetch_margin_alert(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """増担保規制情報を取得して Tier 2 キャッシュに投入する (Standard+)。"""
    return _fetch_daily_to_cache(
        cli.get_mkt_margin_alert,
        conn,
        "/markets/margin-alert",
        TTL_24H,
    )


def fetch_short_sale_report(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """空売り残高報告を取得して Tier 2 キャッシュに投入する (Standard+)。"""
    return _fetch_daily_to_cache(
        cli.get_mkt_short_sale_report,
        conn,
        "/markets/short-sale-report",
        TTL_24H,
        date_param="calculated_date",
    )


def fetch_breakdown(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """売買内訳データを取得して Tier 2 キャッシュに投入する (Premium)。"""
    return _fetch_daily_to_cache(
        cli.get_mkt_breakdown,
        conn,
        "/markets/breakdown",
        TTL_24H,
    )


# エンドポイント名 → (表示名, 取得関数)
FETCH_REGISTRY: dict[str, tuple[str, callable]] = {
    "topix": ("TOPIX 日足", fetch_topix),
    "fins_summary": ("決算サマリー", fetch_fins_summary),
    "earnings_cal": ("決算発表予定", fetch_earnings_calendar),
    "investor_types": ("投資部門別売買動向", fetch_investor_types),
    "short_ratio": ("業種別空売り比率", fetch_short_ratio),
    "margin_interest": ("信用取引残高", fetch_margin_interest),
    "margin_alert": ("増担保規制情報", fetch_margin_alert),
    "short_sale_report": ("空売り残高報告", fetch_short_sale_report),
    "breakdown": ("売買内訳", fetch_breakdown),
}


def main() -> None:
    plan = _load_plan()
    available = _available_endpoints(plan)

    parser = argparse.ArgumentParser(
        description="J-Quants 追加データを取得してキャッシュに投入",
        epilog=f"現在のプラン: {plan}（取得可能: {', '.join(available)}）",
    )
    parser.add_argument("--topix", action="store_true", help="TOPIX 日足を取得 (Light+)")
    parser.add_argument("--fins-summary", action="store_true", help="決算サマリーを取得 (Free+)")
    parser.add_argument("--earnings-cal", action="store_true", help="決算発表予定を取得 (Free+)")
    parser.add_argument(
        "--investor-types", action="store_true", help="投資部門別売買動向を取得 (Light+)"
    )
    parser.add_argument(
        "--short-ratio", action="store_true", help="業種別空売り比率を取得 (Standard+)"
    )
    parser.add_argument(
        "--margin-interest", action="store_true", help="信用取引残高を取得 (Standard+)"
    )
    parser.add_argument(
        "--margin-alert", action="store_true", help="増担保規制情報を取得 (Standard+)"
    )
    parser.add_argument(
        "--short-sale-report", action="store_true", help="空売り残高報告を取得 (Standard+)"
    )
    parser.add_argument("--breakdown", action="store_true", help="売買内訳データを取得 (Premium)")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"キャッシュ DB パス (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    # オプション指定があればそれを使う、なければプランに応じて自動決定
    explicit = {
        "topix": args.topix,
        "fins_summary": args.fins_summary,
        "earnings_cal": args.earnings_cal,
        "investor_types": args.investor_types,
        "short_ratio": args.short_ratio,
        "margin_interest": args.margin_interest,
        "margin_alert": args.margin_alert,
        "short_sale_report": args.short_sale_report,
        "breakdown": args.breakdown,
    }
    has_explicit = any(explicit.values())

    if has_explicit:
        targets = [ep for ep, selected in explicit.items() if selected]
        # 明示指定でもプラン外なら警告
        for ep in targets:
            if ep not in available:
                min_plan = ENDPOINT_MIN_PLAN[ep]
                print(f"⚠️ {ep} は {min_plan}+ プランが必要です（現在: {plan}）、スキップ")
        targets = [ep for ep in targets if ep in available]
    else:
        targets = available

    print(f"プラン: {plan} | 取得対象: {', '.join(targets)}")
    print(f"キャッシュ DB: {args.db}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)

    cli = jquantsapi.ClientV2()

    for ep in targets:
        label, func = FETCH_REGISTRY[ep]
        print(f"{label}を取得中...")
        t0 = time.time()
        try:
            n = func(cli, conn)
        except Exception as e:
            print(f"  エラー: {e}")
            n = 0
        print(f"  完了: {n} 件 ({time.time() - t0:.1f}秒)")

    # 結果確認
    for table in ["indices_bars_daily_topix", "fins_summary", "response_cache"]:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            print(f"  {table}: {row[0]:,} 行")
        except sqlite3.OperationalError:
            pass

    db_size = args.db.stat().st_size / (1024 * 1024)
    print(f"  DB サイズ: {db_size:.1f} MB")

    conn.close()
    print("取得完了")


if __name__ == "__main__":
    main()

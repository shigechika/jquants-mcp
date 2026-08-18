"""Fetch daily J-Quants data and insert directly into the SQLite cache.

Intended as a cron / scheduled-task companion to the MCP server: it
pulls the latest day's data into the local cache so subsequent tool
calls hit warm rows instead of round-tripping to the J-Quants API.

Dependencies: ``jquantsapi`` + the Python standard library. The script
does NOT import from the ``jquants_mcp`` package beyond the schema
module (imported via sys.path) so it can also run inside a consumer
project's own virtualenv.

Plan detection order:
1. ``JQUANTS_PLAN`` environment variable (highest priority)
2. ``~/.config/jquants-mcp/config.ini`` section ``[jquants]`` key ``plan``
3. Auto-detected by probing plan-specific API endpoints (default)

The plan decides which endpoints are fetched (Free / Light / Standard /
Premium). Individual flags override the plan default.

Usage:
    python3 scripts/daily_fetch.py                    # fetch everything allowed by the plan
    python3 scripts/daily_fetch.py --topix             # TOPIX only
    python3 scripts/daily_fetch.py --fins-summary      # earnings summary only
    python3 scripts/daily_fetch.py --earnings-cal      # earnings calendar only
    python3 scripts/daily_fetch.py --short-ratio       # short ratio only (Standard+)
    python3 scripts/daily_fetch.py --margin-interest   # margin interest only (Standard+)
    python3 scripts/daily_fetch.py --backfill 90       # backfill the last 90 days
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from jquants_mcp.cache.schema import (  # noqa: E402
    RESPONSE_CACHE_DDL,
    SCREENER_RESULTS_DDL,
    SCREENER_RESULTS_INDEX_DDL,
    TIER1_TABLES,
    ensure_cross_section_indexes,
    generate_ddl,
    migrate_add_fins_indexes,
    migrate_add_fwdnp_index,
    migrate_split_fins_pk,
    migrate_drop_plan,
)
from jquants_mcp.cache import screener_compute  # noqa: E402  # stdlib-only

import jquantsapi  # noqa: E402

# Default cache DB path
DEFAULT_DB_PATH = Path.home() / ".cache" / "jquants-mcp" / "cache.db"

# Default lookback days for financial summaries
FINS_LOOKBACK_DAYS = 7

# Endpoints available per plan (based on the J-Quants API v2 spec).
# Defines the minimum required plan for each endpoint.
PLAN_LEVELS = {"free": 0, "light": 1, "standard": 2, "premium": 3}

ENDPOINT_MIN_PLAN: dict[str, str] = {
    "fins_summary": "free",
    "earnings_cal": "free",
    "calendar": "free",
    "topix": "light",
    "investor_types": "light",
    "short_ratio": "standard",
    "margin_interest": "standard",
    "margin_alert": "standard",
    "short_sale_report": "standard",
    "breakdown": "premium",
}

# Tier 2 cache TTL (seconds) — matches the MCP server configuration
TTL_6H = 6 * 3600
TTL_24H = 24 * 3600
TTL_7D = 7 * 24 * 3600
TTL_90D = 90 * 24 * 3600

# Earnings calendar sweep window (Light+ plans only; see fetch_earnings_calendar).
# BACK covers the weekend gap for the weekday-only cron (30 8 * * 1-5): Monday's
# run needs to pick up Fri-Sun. AHEAD is roughly one reporting cycle so "next
# earnings date" stays answerable for effectively every issue.
EARNINGS_SWEEP_BACK_DAYS = 3
EARNINGS_SWEEP_AHEAD_DAYS = 90
EARNINGS_MAX_CONSECUTIVE_ERRORS = 3
# jquantsapi's own urllib3.Retry(total=3, status_forcelist=[429,...]) has no
# backoff_factor, so it retries near-instantly and does not actually avoid a
# 429 storm. A sequential sweep at 0.4s spacing was observed to trip a 429
# lockout after ~30 requests that persisted through a 20s cooldown retry, so
# pacing is not derived from the (unreliable) auto-detected plan tier.
EARNINGS_SWEEP_MIN_INTERVAL_S = 1.5


def _load_plan() -> str | None:
    """Load plan from environment variable or config file.

    Returns None when not explicitly configured so the caller can
    trigger API-based auto-detection via :func:`_detect_plan_from_api`.
    """
    plan = os.environ.get("JQUANTS_PLAN")
    if plan:
        return plan.lower()

    config = configparser.ConfigParser()
    search_paths = [
        str(Path.home() / ".config" / "jquants-mcp" / "config.ini"),
        "config.ini",
    ]
    config.read(search_paths, encoding="utf-8")

    try:
        return config.get("jquants", "plan").lower()
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None


def _detect_plan_from_api(cli: "jquantsapi.ClientV2") -> str:
    """Probe the J-Quants API to detect the active subscription plan.

    Tries plan-specific endpoints from Premium down to Light.  Returns the
    highest plan whose endpoint responds with HTTP 200; returns "free" when
    all higher-plan probes are rejected (HTTP 403).  Raises RuntimeError on
    HTTP 401 (bad API key) or unexpected errors.

    Uses duck-typing on exception.response.status_code so this function does
    not need to import ``requests`` directly — jquantsapi is an optional
    runtime dependency not available in the test environment.
    """
    probes: list[tuple[str, Callable[[], Any]]] = [
        ("premium", lambda: cli.get_fin_details(date_yyyymmdd="20240101")),
        ("standard", lambda: cli.get_mkt_short_ratio(date_yyyymmdd="20240101")),
        ("light", lambda: cli.get_eq_investor_types()),
    ]

    for plan_name, probe in probes:
        try:
            probe()
            return plan_name
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
            if code == 403:
                continue
            if code == 401:
                raise RuntimeError(
                    "J-Quants API authentication failed (401). Check your API key."
                ) from exc
            if code is not None:
                raise  # HTTP error with unexpected status code
            raise RuntimeError(
                f"Plan auto-detection failed: {exc}. "
                "Set JQUANTS_PLAN env var or config.ini [jquants] plan to bypass."
            ) from exc

    return "free"


def _available_endpoints(plan: str) -> list[str]:
    """Return the list of endpoints available under *plan*."""
    plan_level = PLAN_LEVELS.get(plan, 0)
    return [
        ep
        for ep, min_plan in ENDPOINT_MIN_PLAN.items()
        if PLAN_LEVELS.get(min_plan, 0) <= plan_level
    ]


_DAILY_FETCH_TABLES = [
    "indices_bars_daily_topix",
    "fins_summary",
    "investor_types",
    "markets_margin_interest",
    "markets_margin_alert",
    "markets_short_ratio",
    "markets_breakdown",
    "markets_calendar",
    "equities_earnings_calendar",
]


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create required tables if they do not exist."""
    for name in _DAILY_FETCH_TABLES:
        conn.execute(generate_ddl(name, TIER1_TABLES[name]))
    conn.execute(RESPONSE_CACHE_DDL)
    conn.execute(SCREENER_RESULTS_DDL)
    conn.execute(SCREENER_RESULTS_INDEX_DDL)
    conn.commit()
    migrate_drop_plan(conn)
    migrate_add_fins_indexes(conn)
    migrate_split_fins_pk(conn)
    migrate_add_fwdnp_index(conn)
    ensure_cross_section_indexes(conn)


def _sanitize_row(row_data: dict) -> dict:
    """Convert NaN to None (for JSON serialization)."""
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in row_data.items()}


def _store_tier1(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict],
    key_mapping: list[tuple[str, str]],
) -> int:
    """Insert records into a Tier 1 table.

    Args:
        conn: SQLite connection
        table: Table name
        rows: API response row data
        key_mapping: List of (API column name, DB column name)

    Returns:
        Number of inserted rows
    """
    if not rows:
        return 0

    now = time.time()
    db_col_names = ", ".join([db_col for _, db_col in key_mapping])
    placeholders = ", ".join(["?"] * (len(key_mapping) + 2))
    sql = (
        f"INSERT OR REPLACE INTO {table} ({db_col_names}, data, fetched_at) VALUES ({placeholders})"
    )

    count = 0
    for row in rows:
        # Normalize key column values: str(pandas.Timestamp) → "YYYY-MM-DD HH:MM:SS"; strip to date only.
        # Apply to both the DB key and the stored JSON so they stay consistent.
        normalized_keys = {
            api_col: str(row.get(api_col, "")).split(" ")[0].split("T")[0]
            for api_col, _ in key_mapping
        }
        key_values = [normalized_keys[api_col] for api_col, _ in key_mapping]
        row_for_json = {**row, **normalized_keys}
        data_json = json.dumps(row_for_json, ensure_ascii=False, default=str)
        conn.execute(sql, key_values + [data_json, now])
        count += 1

    conn.commit()
    return count


def _get_max_date(
    conn: sqlite3.Connection,
    table: str,
    date_column: str = "date",
) -> str | None:
    """Get the latest date from a Tier 1 table."""
    try:
        row = conn.execute(f"SELECT MAX({date_column}) FROM {table}").fetchone()
        return row[0][:10] if row and row[0] else None
    except sqlite3.OperationalError:
        return None


def fetch_topix(cli: jquantsapi.ClientV2, conn: sqlite3.Connection, plan: str) -> int:
    """Fetch TOPIX daily bars incrementally and insert into cache."""
    max_date = _get_max_date(conn, "indices_bars_daily_topix")

    if max_date:
        from_date = (datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        print(f"  latest cached date: {max_date}, fetching from {from_date}")
        df = cli.get_idx_bars_daily_topix(from_yyyymmdd=from_date)
    else:
        print("  cache empty, fetching full history")
        df = cli.get_idx_bars_daily_topix()

    if df is None or len(df) == 0:
        print("  no new data")
        return 0

    now = time.time()
    count = 0
    for _, r in df.iterrows():
        # str(pandas.Timestamp) produces "YYYY-MM-DD HH:MM:SS"; strip to date only
        date_str = str(r["Date"])[:10]
        row_dict = _sanitize_row(r.to_dict())
        row_dict["Date"] = date_str
        data_json = json.dumps(row_dict, ensure_ascii=False, default=str)
        conn.execute(
            "INSERT OR REPLACE INTO indices_bars_daily_topix "
            "(date, data, fetched_at) VALUES (?, ?, ?)",
            (date_str, data_json, now),
        )
        count += 1

    conn.commit()
    return count


def fetch_fins_summary(cli: jquantsapi.ClientV2, conn: sqlite3.Connection, plan: str) -> int:
    """Fetch recent financial summaries and insert into cache."""
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
            print(f"  {date_iso}: error ({e})")
            continue

        if df is None or len(df) == 0:
            continue

        for _, r in df.iterrows():
            data_dict = _sanitize_row(r.to_dict())
            code = str(r.get("Code", ""))
            disc_date = str(r.get("DiscDate", date_iso))[:10]
            # doc_type is part of the PK since issue #473 so the same-day
            # FYFinancialStatements + DividendForecastRevision now coexist
            # instead of one overwriting the other. Not date-normalised
            # (doc_type can contain 'T', e.g. "REITFinancial...").
            doc_type = str(data_dict.get("DocType") or data_dict.get("TypeOfDocument") or "")
            # Carry NxFDivAnn forward across re-fetches of the SAME filing
            # (matched on the full PK) so a later fetch that drops the field
            # does not lose the next-FY forecast.
            nx = data_dict.get("NxFDivAnn")
            if nx is None or nx == "":
                existing = conn.execute(
                    "SELECT data FROM fins_summary "
                    "WHERE code=? AND disc_date=? AND doc_type=? LIMIT 1",
                    (code, disc_date, doc_type),
                ).fetchone()
                if existing:
                    existing_data = json.loads(existing[0])
                    existing_nx = existing_data.get("NxFDivAnn")
                    if existing_nx is not None and existing_nx != "":
                        data_dict["NxFDivAnn"] = existing_nx
            data_json = json.dumps(data_dict, ensure_ascii=False, default=str)
            conn.execute(
                "INSERT OR REPLACE INTO fins_summary "
                "(code, disc_date, doc_type, data, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (code, disc_date, doc_type, data_json, now),
            )
            count += 1

        conn.commit()
        print(f"  {date_iso}: {len(df)} rows")

    return count


# /fins/earnings-date's FQName/FYE are machine-friendly ("1Q"/"0331"); the
# legacy /equities/earnings-calendar (and everything downstream that reads its
# stored records — get_earnings_this_week's fiscal_quarter/fiscal_year_end have
# no fallback for these two) expects the old display strings. Mapping at write
# time means no reader, cache schema, or test needs to change. Verified against
# 4,040 production rows: these are the only five FQ values on record ("本決算"
# is the annual label, not "通期"), and FY always renders as month+day.
_FQ_NAME_TO_LEGACY = {
    "1Q": "第１四半期",
    "2Q": "第２四半期",
    "3Q": "第３四半期",
    "4Q": "第４四半期",
    "FY": "本決算",
}


def _fye_to_legacy_fy(fye: str) -> str | None:
    """Render "0331" as "3月31日" (the legacy FY string). None if unparseable."""
    if not fye or len(fye) != 4 or not fye.isdigit():
        return None
    month, day = int(fye[:2]), int(fye[2:])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{month}月{day}日"


def _map_earnings_record(raw: dict, swept_date_iso: str) -> dict | None:
    """Map a /fins/earnings-date record onto the legacy record shape.

    Returns None when there is no usable Code (mirrors the legacy skip at the
    Tier 1 write site). SchDate can be "" (未定) — via jquantsapi this becomes
    NaT, and str(NaT)[:10] == "NaT" (isinstance(NaT, float) is False, so the
    usual _sanitize_row NaN handling does not catch it) — so an unparseable
    Date falls back to the day being swept, which is always the authoritative
    date for that record under scheduled_date= queries.
    """
    code_raw = raw.get("Code")
    if code_raw is None:
        # _sanitize_row turns a NaN Code into None; str(None) == "None" is
        # truthy, so this must be checked before stringifying.
        return None
    code = str(code_raw).strip()
    if not code:
        return None
    sch_date = str(raw.get("SchDate", ""))[:10]
    if len(sch_date) != 10 or not sch_date[0].isdigit():
        sch_date = swept_date_iso
    fq_name = raw.get("FQName", "")
    fye = str(raw.get("FYE", ""))
    return {
        "Date": sch_date,
        "Code": code,
        "CoName": raw.get("CoName"),
        "FQ": _FQ_NAME_TO_LEGACY.get(fq_name, fq_name),
        "FY": _fye_to_legacy_fy(fye) or fye,
        # Additive, new — not present in the legacy payload.
        "PubDate": str(raw.get("PubDate", ""))[:10] or None,
        "CoNameEn": raw.get("CoNameEn"),
    }


def _earnings_sweep_dates(today: datetime, back: int, ahead: int) -> list[str]:
    """ISO dates from today-back to today+ahead, weekends excluded.

    Verified against production data: 4,040 rows, zero on Saturday/Sunday.
    """
    dates = []
    for delta in range(-back, ahead + 1):
        d = today + timedelta(days=delta)
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y-%m-%d"))
    return dates


def _earnings_api_call(cli: jquantsapi.ClientV2, date_iso: str, *, max_retries: int = 5):
    """Paced, 429-retrying single-day scheduled_date= call.

    jquantsapi's own retry (urllib3.Retry(total=3, status_forcelist=[429,...]))
    has no backoff_factor and does not actually avoid a 429 storm — see the
    EARNINGS_SWEEP_MIN_INTERVAL_S comment. This wrapper paces every call and,
    on a 429 that survives jquantsapi's built-in retries, backs off further.

    When jquantsapi's own retry is itself exhausted by a sustained 429 storm,
    requests raises ``RetryError`` with ``response=None`` (verified against
    the real requests/urllib3 stack — no HTTP response object survives to
    attach), not the ``HTTPError(response=...)`` shape a plain status-code
    check expects. That case is treated as retryable too, just without a
    Retry-After header to read.
    """
    date_ymd = date_iso.replace("-", "")
    time.sleep(EARNINGS_SWEEP_MIN_INTERVAL_S)
    for attempt in range(max_retries):
        try:
            return cli.get_fin_earnings_date(scheduled_date_yyyymmdd=date_ymd)
        except Exception as e:  # noqa: BLE001 — duck-typed HTTP status below; any
            # other failure is retried the same way since jquantsapi's own
            # exception types are not part of its stable import surface.
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status == 429 or type(e).__name__ == "RetryError"
            if not retryable or attempt == max_retries - 1:
                raise
            retry_after = None
            headers = getattr(getattr(e, "response", None), "headers", None)
            if headers:
                retry_after = headers.get("Retry-After")
            wait = None
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    pass  # Retry-After can be HTTP-date formatted (RFC 9110)
            if wait is None:
                wait = EARNINGS_SWEEP_MIN_INTERVAL_S * (2**attempt)
            print(
                f"  {date_iso}: {status or 'retry exhausted'}, retrying in {wait:.1f}s "
                f"(attempt {attempt + 1}/{max_retries})"
            )
            # This sleep also serves as the pacing delay before the next
            # attempt — no separate pacing sleep is added per retry.
            time.sleep(wait)


def _fetch_earnings_calendar_legacy(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """Fetch earnings calendar and store in Tier 2 response cache by date.

    Free-plan path: /equities/earnings-calendar covers only March/September
    fiscal-year issues and only the next business day (see
    fetch_earnings_calendar's docstring for why Light+ uses a different
    endpoint instead of gating this one out). Kept verbatim from before the
    /fins/earnings-date migration so Free-plan behaviour is unchanged.

    The API returns earnings announcements for the next business day.
    Stored with date-keyed entries to accumulate ~3 months of data (TTL 90 days).

    Timing caveat (#523): upstream updates around 19:00 JST. A cron running
    earlier in the evening captures the list published the PREVIOUS evening —
    the current day's own announcements, most of which have already been made
    by then — and the NEXT day's list (published at 19:00) is not captured
    until the following evening. Same-day morning coverage therefore relies on
    the tools' cache-miss live fallback. Schedule this fetch after 19:00 JST
    to accumulate the next day's list proactively.
    """
    df = cli.get_eq_earnings_cal()

    if df is None or len(df) == 0:
        print("  no data")
        return 0

    records = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]

    # Normalize Date to YYYY-MM-DD (pd.Timestamp serializes as "YYYY-MM-DD HH:MM:SS")
    for rec in records:
        if rec.get("Date") is not None:
            rec["Date"] = str(rec["Date"])[:10]

    date_val = records[0].get("Date") if records else None
    if date_val is not None:
        date_key = str(date_val)[:10].replace("-", "")
    else:
        date_key = datetime.today().strftime("%Y%m%d")

    now = time.time()
    response_data = json.dumps(records, ensure_ascii=False, default=str)

    # Accumulate under date-keyed entries (TTL 90 days)
    cache_key = f"/equities/earnings-calendar?date={date_key}"
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (cache_key, response_data, now, TTL_90D),
    )

    # Also update the no-params key (for latest data)
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        ("/equities/earnings-calendar", response_data, now, TTL_90D),
    )

    # Tier 1: store each record by (code, date) for efficient cross-sectional queries
    for rec in records:
        code = str(rec.get("Code", ""))
        ann_date = str(rec.get("Date", ""))[:10]  # normalize to YYYY-MM-DD
        if not code or len(ann_date) < 10:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO equities_earnings_calendar "
            "(code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
            (code, ann_date, json.dumps(rec, ensure_ascii=False, default=str), now),
        )

    conn.commit()

    return len(records)


def _fetch_earnings_calendar_sweep(cli: jquantsapi.ClientV2, conn: sqlite3.Connection) -> int:
    """Fetch earnings calendar via /fins/earnings-date (Light+ plans).

    Sweeps scheduled_date= over a forward window and replaces Tier 1 rows PER
    DAY. scheduled_date= is server-resolved to "currently valid", so a given
    day's response is authoritative for that day — a reschedule away from a
    date is corrected the moment that date is next swept, with no supersession
    logic or identity key needed (contrast the (Code,FQName,FYE)-keyed state
    machine a PubDate-delta approach would require, verified against production
    data to have real reschedule cases). This is what makes the per-day
    delete-and-replace safe against phantom rows without a schema change.

    Two-phase to guard against a misdetected plan or an entitlement change
    silently emptying the table: fetch every day into memory first, and if the
    WHOLE sweep came back with zero rows across every day, write nothing.
    A day that legitimately has zero scheduled announcements still wipes that
    day once the whole-sweep-empty guard has passed — that is intentional; it
    is the mechanism that removes phantoms.
    """
    today = datetime.today()
    dates = _earnings_sweep_dates(today, EARNINGS_SWEEP_BACK_DAYS, EARNINGS_SWEEP_AHEAD_DAYS)

    fetched: dict[str, list[dict]] = {}
    consecutive_errors = 0
    for date_iso in dates:
        try:
            df = _earnings_api_call(cli, date_iso)
        except Exception as e:  # noqa: BLE001 — see EARNINGS_MAX_CONSECUTIVE_ERRORS:
            # a handful of bad days should not abort a ~90-day sweep, but a
            # sustained failure (e.g. exhausted rate limit, bad key) should
            # stop early rather than burn through the remaining slow no-ops.
            consecutive_errors += 1
            print(f"  {date_iso}: error ({e})")
            if consecutive_errors >= EARNINGS_MAX_CONSECUTIVE_ERRORS:
                raise RuntimeError(
                    f"earnings sweep: {consecutive_errors} consecutive failures, aborting"
                ) from e
            continue
        consecutive_errors = 0
        records = [
            _map_earnings_record(_sanitize_row(r.to_dict()), date_iso) for _, r in df.iterrows()
        ]
        fetched[date_iso] = [r for r in records if r is not None]

    total = sum(len(rows) for rows in fetched.values())
    if total == 0:
        print(f"  no rows across {len(dates)} days — skipping replace")
        return 0

    now = time.time()
    for date_iso, records in fetched.items():
        conn.execute("DELETE FROM equities_earnings_calendar WHERE date = ?", (date_iso,))
        for rec in records:
            code = str(rec.get("Code", ""))
            ann_date = str(rec.get("Date", ""))[:10]
            if not code or len(ann_date) < 10:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO equities_earnings_calendar "
                "(code, date, data, fetched_at) VALUES (?, ?, ?, ?)",
                (code, ann_date, json.dumps(rec, ensure_ascii=False, default=str), now),
            )
        # Tier 2: these are cache keys, not an endpoint declaration — the data
        # now comes from /fins/earnings-date, but readers (_search_earnings_by_code's
        # LIKE scan, the no-arg branch's bare-key fallback) still look under the
        # legacy key shape, so keep writing it.
        response_data = json.dumps(records, ensure_ascii=False, default=str)
        date_key = date_iso.replace("-", "")
        conn.execute(
            "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
            (f"/equities/earnings-calendar?date={date_key}", response_data, now, TTL_90D),
        )

    # Bare no-params key: kept narrow (near-term window) to match the tool's
    # no-arg branch and avoid a ~1MB blob now that coverage is much larger.
    # Clamped to the sweep's actual coverage (EARNINGS_SWEEP_BACK_DAYS is 3,
    # narrower than the tool's -7) — requesting a wider window than what was
    # actually swept would silently omit those days rather than error.
    near_back = min(7, EARNINGS_SWEEP_BACK_DAYS)
    near_ahead = min(14, EARNINGS_SWEEP_AHEAD_DAYS)
    near_dates = _earnings_sweep_dates(today, near_back, near_ahead)
    near_records = [rec for d in near_dates for rec in fetched.get(d, [])]
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (
            "/equities/earnings-calendar",
            json.dumps(near_records, ensure_ascii=False, default=str),
            now,
            TTL_90D,
        ),
    )

    conn.commit()
    print(f"  swept {len(dates)} days, {total} rows")
    return total


def fetch_earnings_calendar(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch earnings calendar; source depends on plan.

    Free: /equities/earnings-calendar (legacy). Its two long-standing limits
    (March/September fiscal-year issues only, next-business-day only) are
    unrelated to the 2026-08-03 J-Quants release, which only renamed the
    endpoint's display label ("機能・レスポンスの変更はありません") — see
    jquants-mcp#618. Kept for Free because the newer endpoint's Free-tier
    window is delayed by publication date (12 weeks), so a Free key would see
    an empty 200 for anything recent rather than an error — silently going
    dark is worse than staying on the endpoint that has always worked here.

    Light+: /fins/earnings-date via a scheduled_date= forward sweep (see
    _fetch_earnings_calendar_sweep). All listed issues incl. REITs, any
    fiscal year-end, and the ~10:05 JST daily update removes the #523 timing
    race entirely (the legacy feed updated ~19:00 JST, irregularly).
    """
    if PLAN_LEVELS.get(plan, 0) < PLAN_LEVELS["light"]:
        return _fetch_earnings_calendar_legacy(cli, conn)
    return _fetch_earnings_calendar_sweep(cli, conn)


def fetch_investor_types(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch investor type data and insert into Tier 1 cache (Light+).

    Weekly data (published every Thursday). Fetches the last 2 weeks.
    """
    today = datetime.today()
    from_date = (today - timedelta(days=14)).strftime("%Y%m%d")
    to_date = today.strftime("%Y%m%d")

    df = cli.get_eq_investor_types(from_yyyymmdd=from_date, to_yyyymmdd=to_date)

    if df is None or len(df) == 0:
        print("  no data")
        return 0

    now = time.time()
    count = 0
    for _, r in df.iterrows():
        data_json = json.dumps(_sanitize_row(r.to_dict()), ensure_ascii=False, default=str)
        pub_date = str(r.get("PublishedDate", r.get("PubDate", "")))[:10]
        section = str(r.get("Section", ""))
        conn.execute(
            "INSERT OR REPLACE INTO investor_types "
            "(pub_date, section, data, fetched_at) VALUES (?, ?, ?, ?)",
            (pub_date, section, data_json, now),
        )
        count += 1

    conn.commit()
    return count


def _store_response_cache(
    conn: sqlite3.Connection,
    cache_key: str,
    data: "list[dict] | dict",
    ttl: int,
) -> int:
    """Store data in Tier 2 response cache.

    data must be either a plain list[dict] or a pre-wrapped
    {"count": N, "data": [...]} dict to match the format stored by the MCP
    tool itself when it falls back to the API.
    """
    if not data:
        print("  no data")
        return 0

    response_data = json.dumps(data, ensure_ascii=False, default=str)
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO response_cache (cache_key, data, fetched_at, ttl_seconds) VALUES (?, ?, ?, ?)",
        (cache_key, response_data, now, ttl),
    )
    conn.commit()
    if isinstance(data, dict):
        return len(data.get("data", []))
    return len(data)


# ------------------------------------------------------------------
# Markets Tier 1 fetch functions
# ------------------------------------------------------------------


def _fetch_markets_tier1(
    cli_method,
    conn: sqlite3.Connection,
    table: str,
    key_mapping: list[tuple[str, str]],
    *,
    plan: str = "free",
    from_yyyymmdd: str = "",
    to_yyyymmdd: str = "",
    date_yyyymmdd: str = "",
    date_column: str = "date",
    incremental: bool = True,
    **extra_params,
) -> int:
    """Insert Markets data into a Tier 1 cache table.

    Args:
        cli_method: jquantsapi method
        conn: SQLite connection
        table: Tier 1 table name
        key_mapping: List of (API column name, DB column name)
        plan: Subscription plan (used for endpoint access control only)
        from_yyyymmdd: Start date
        to_yyyymmdd: End date
        date_yyyymmdd: Specific date
        date_column: DB date column name
        incremental: If True, fetch only new data
        **extra_params: Extra params passed to cli_method
    """
    # Incremental fetch: start from the day after the latest cached date
    if incremental and not from_yyyymmdd and not date_yyyymmdd:
        max_date = _get_max_date(conn, table, date_column)
        if max_date:
            from_yyyymmdd = (
                datetime.strptime(max_date[:10], "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y%m%d")
            print(f"  latest cached date: {max_date}, fetching from {from_yyyymmdd}")

    params = {**extra_params}
    if from_yyyymmdd:
        params["from_yyyymmdd"] = from_yyyymmdd
    if to_yyyymmdd:
        params["to_yyyymmdd"] = to_yyyymmdd
    if date_yyyymmdd:
        params["date_yyyymmdd"] = date_yyyymmdd

    # For daily fetch with no date specified, default to today
    if not from_yyyymmdd and not to_yyyymmdd and not date_yyyymmdd:
        params["date_yyyymmdd"] = datetime.today().strftime("%Y%m%d")

    try:
        df = cli_method(**params)
    except Exception as e:
        print(f"  error: {e}")
        return 0

    if df is None or len(df) == 0:
        # No data for today: fall back to fetching without params
        if date_yyyymmdd or params.get("date_yyyymmdd"):
            print("  no data for today, fetching without params")
            try:
                df = cli_method(**{k: v for k, v in extra_params.items()})
            except Exception as e:
                print(f"  fallback error: {e}")
                return 0

    if df is None or len(df) == 0:
        print("  no data")
        return 0

    rows = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_tier1(conn, table, rows, key_mapping)


def fetch_short_ratio(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch sector short-selling ratios into Tier 1 and Tier 2 cache (Standard+)."""
    count = _fetch_markets_tier1(
        cli.get_mkt_short_ratio,
        conn,
        table="markets_short_ratio",
        key_mapping=[("S33", "s33"), ("Date", "date")],
        plan=plan,
    )
    # Populate Tier 2 cache for no-params tool calls
    try:
        df_all = cli.get_mkt_short_ratio()
        if df_all is not None and len(df_all) > 0:
            records = [_sanitize_row(r.to_dict()) for _, r in df_all.iterrows()]
            _store_response_cache(
                conn, "/markets/short-ratio", {"count": len(records), "data": records}, TTL_24H
            )
            print(f"  Tier 2: {len(records)} rows")
    except Exception as e:
        print(f"  Tier 2 error: {e}")
    return count


def fetch_margin_interest(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch margin interest data into Tier 1 cache (Standard+)."""
    return _fetch_markets_tier1(
        cli.get_mkt_margin_interest,
        conn,
        table="markets_margin_interest",
        key_mapping=[("Code", "code"), ("Date", "date")],
        plan=plan,
    )


def fetch_margin_alert(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch margin alert data into Tier 1 cache (Standard+)."""
    return _fetch_markets_tier1(
        cli.get_mkt_margin_alert,
        conn,
        table="markets_margin_alert",
        key_mapping=[("Code", "code"), ("PubDate", "date")],
        date_column="date",
        plan=plan,
    )


def fetch_short_sale_report(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch short sale report into Tier 2 cache (Standard+).

    Multiple reporters per code+date, so Tier 2 is used.
    """
    today = datetime.today().strftime("%Y%m%d")
    try:
        df = cli.get_mkt_short_sale_report(calculated_date=today)
    except Exception as e:
        print(f"  error: {e}")
        return 0

    if df is None or len(df) == 0:
        print("  no data for today, fetching without params")
        try:
            df = cli.get_mkt_short_sale_report()
        except Exception as e:
            print(f"  fallback error: {e}")
            return 0

    if df is None or len(df) == 0:
        print("  no data")
        return 0

    records = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_response_cache(
        conn, "/markets/short-sale-report", {"count": len(records), "data": records}, TTL_24H
    )


def fetch_breakdown(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch trade breakdown data into Tier 1 cache (Premium)."""
    return _fetch_markets_tier1(
        cli.get_mkt_breakdown,
        conn,
        table="markets_breakdown",
        key_mapping=[("Code", "code"), ("Date", "date")],
        plan=plan,
    )


# ------------------------------------------------------------------
# Screener result pre-compute (Issue #142)
# ------------------------------------------------------------------


# Re-exported so the print message below has a stable name even if the
# upstream constant is renamed; the source of truth lives in
# ``screener_compute`` so reader/writer can never drift apart.
_SCREENER_RETENTION_WEEKS = screener_compute.SCREENER_CACHE_LOOKBACK_WEEKS


def _screener_default_jobs() -> list[tuple[str, str, dict]]:
    """Return the (tool_name, params_hash, kwargs) triples to pre-compute.

    Kwargs match the ``screener_compute.compute_for_date`` signature
    (excluding ``conn`` / ``norm_date`` / ``tool_name``).
    """
    return [
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


def populate_screener_results(conn: sqlite3.Connection) -> int:
    """Pre-compute and cache screener payloads for the latest session.

    Computes ``detect_52w_high_low``, ``detect_ytd_high_low``, and
    ``detect_consecutive_dividend_increase`` with default parameters for
    the most recent date in ``equities_bars_daily`` and
    ``INSERT OR REPLACE``s the rows.  Then prunes rows older than 52 weeks.
    Returns the number of new or replaced rows.
    """
    latest = screener_compute.latest_session_date(conn)
    if latest is None:
        print("  skipped: equities_bars_daily is empty")
        return 0

    print(f"  target date: {latest}")
    written = 0

    # --- equities_bars_daily based screeners (52w / YTD high-low) ---
    for tool_name, params_hash, kwargs in _screener_default_jobs():
        t0 = time.time()
        payload = screener_compute.compute_for_date(
            conn,
            norm_date=latest,
            **kwargs,
        )
        screener_compute.upsert_screener_result(
            conn,
            tool_name=tool_name,
            params_hash_value=params_hash,
            norm_date=latest,
            payload=payload,
            computed_at=time.time(),
        )
        written += 1
        elapsed = time.time() - t0
        print(f"    {tool_name}: count={payload.get('count')} ({elapsed:.1f}s)")

    # --- fins_summary based screeners (consecutive dividend increase) ---
    t0 = time.time()
    fy_history = screener_compute.fetch_fy_dividend_history(conn)
    if fy_history:
        all_codes = list(fy_history.keys())
        split_events = screener_compute.fetch_split_events_by_code(conn, all_codes)
        payload = screener_compute.compute_consecutive_div_snapshot(fy_history, split_events)
        screener_compute.upsert_screener_result(
            conn,
            tool_name=screener_compute.TOOL_DETECT_CONSECUTIVE_DIV,
            params_hash_value=screener_compute.default_params_hash_consecutive_div(),
            norm_date=latest,
            payload=payload,
            computed_at=time.time(),
        )
        written += 1
        elapsed = time.time() - t0
        print(
            f"    {screener_compute.TOOL_DETECT_CONSECUTIVE_DIV}: "
            f"count={payload.get('count')} ({elapsed:.1f}s)"
        )
    else:
        print(f"    {screener_compute.TOOL_DETECT_CONSECUTIVE_DIV}: skipped, fins_summary is empty")

    pruned = screener_compute.prune_old_results(conn, retention_weeks=_SCREENER_RETENTION_WEEKS)
    conn.commit()
    if pruned:
        print(f"  pruned {pruned} rows outside retention ({_SCREENER_RETENTION_WEEKS} weeks)")
    return written


def fetch_calendar(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    plan: str,
) -> int:
    """Fetch trading calendar into Tier 1 cache (Free+)."""
    try:
        df = cli.get_mkt_calendar()
    except Exception as e:
        print(f"  error: {e}")
        return 0

    if df is None or len(df) == 0:
        print("  no data")
        return 0

    rows = [_sanitize_row(r.to_dict()) for _, r in df.iterrows()]
    return _store_tier1(conn, "markets_calendar", rows, [("Date", "date")])


# ------------------------------------------------------------------
# Backfill: bulk-fetch historical data
# ------------------------------------------------------------------


def _backfill_markets_tier1(
    cli_method,
    conn: sqlite3.Connection,
    table: str,
    key_mapping: list[tuple[str, str]],
    from_yyyymmdd: str,
    to_yyyymmdd: str,
    plan: str,
    **extra_params,
) -> int:
    """Bulk-fetch historical Markets data into Tier 1."""
    return _fetch_markets_tier1(
        cli_method,
        conn,
        table=table,
        key_mapping=key_mapping,
        plan=plan,
        from_yyyymmdd=from_yyyymmdd,
        to_yyyymmdd=to_yyyymmdd,
        incremental=False,
        **extra_params,
    )


# Endpoint name → (display name, fetch function)
FETCH_REGISTRY: dict[str, tuple[str, callable]] = {
    "topix": ("TOPIX daily bars", fetch_topix),
    "fins_summary": ("financial summaries", fetch_fins_summary),
    "earnings_cal": ("earnings calendar", fetch_earnings_calendar),
    "investor_types": ("investor types", fetch_investor_types),
    "short_ratio": ("sector short-selling ratios", fetch_short_ratio),
    "margin_interest": ("margin interest", fetch_margin_interest),
    "margin_alert": ("margin regulation alerts", fetch_margin_alert),
    "short_sale_report": ("short sale report", fetch_short_sale_report),
    "breakdown": ("trade breakdown", fetch_breakdown),
    "calendar": ("trading calendar", fetch_calendar),
}

# Backfill-capable endpoints (those that accept a from/to date range)
BACKFILL_REGISTRY: dict[str, tuple[str, callable, str, list[tuple[str, str]]]] = {
    # key: (display name, cli_method_name, table, key_mapping)
    "short_ratio": (
        "sector short-selling ratios",
        "get_mkt_short_ratio",
        "markets_short_ratio",
        [("S33", "s33"), ("Date", "date")],
    ),
    "margin_interest": (
        "margin interest",
        "get_mkt_margin_interest",
        "markets_margin_interest",
        [("Code", "code"), ("Date", "date")],
    ),
    "margin_alert": (
        "margin regulation alerts",
        "get_mkt_margin_alert",
        "markets_margin_alert",
        [("Code", "code"), ("PubDate", "date")],
    ),
    "breakdown": (
        "trade breakdown",
        "get_mkt_breakdown",
        "markets_breakdown",
        [("Code", "code"), ("Date", "date")],
    ),
}


def _run_backfill(
    cli: jquantsapi.ClientV2,
    conn: sqlite3.Connection,
    targets: list[str],
    days: int,
    plan: str,
) -> None:
    """Run backfill for the specified number of days."""
    today = datetime.today()
    from_date = (today - timedelta(days=days)).strftime("%Y%m%d")
    to_date = today.strftime("%Y%m%d")

    print(f"backfill: {from_date} → {to_date} ({days} days)")

    for ep in targets:
        if ep not in BACKFILL_REGISTRY:
            print(f"  {ep}: backfill not supported, skipping")
            continue

        label, method_name, table, key_mapping = BACKFILL_REGISTRY[ep]
        cli_method = getattr(cli, method_name)
        print(f"backfilling {label}...")
        t0 = time.time()
        try:
            n = _backfill_markets_tier1(
                cli_method,
                conn,
                table,
                key_mapping,
                from_yyyymmdd=from_date,
                to_yyyymmdd=to_date,
                plan=plan,
            )
        except Exception as e:
            print(f"  error: {e}")
            n = 0
        print(f"  done: {n} rows ({time.time() - t0:.1f}s)")

    # If the calendar is in the backfill targets, fetch all of it
    if "calendar" in targets:
        print("fetching trading calendar...")
        t0 = time.time()
        try:
            n = fetch_calendar(cli, conn, plan)
        except Exception as e:
            print(f"  error: {e}")
            n = 0
        print(f"  done: {n} rows ({time.time() - t0:.1f}s)")


# List of Tier 1 tables (for the result summary)
_TIER1_TABLES = [
    "indices_bars_daily_topix",
    "fins_summary",
    "investor_types",
    "markets_margin_interest",
    "markets_margin_alert",
    "markets_short_ratio",
    "markets_breakdown",
    "markets_calendar",
    "response_cache",
]


def main() -> None:
    global EARNINGS_SWEEP_BACK_DAYS, EARNINGS_SWEEP_AHEAD_DAYS
    configured_plan = _load_plan()
    auto_detect = configured_plan is None

    parser = argparse.ArgumentParser(
        description="Fetch additional J-Quants data and insert into the cache",
        epilog="Plan is auto-detected from the J-Quants API"
        if auto_detect
        else f"Active plan: {configured_plan} (available: {', '.join(_available_endpoints(configured_plan))})",
    )
    parser.add_argument("--topix", action="store_true", help="fetch TOPIX daily bars (Light+)")
    parser.add_argument(
        "--fins-summary", action="store_true", help="fetch financial summaries (Free+)"
    )
    parser.add_argument(
        "--earnings-cal", action="store_true", help="fetch earnings calendar (Free+)"
    )
    parser.add_argument(
        "--earnings-back",
        type=int,
        metavar="DAYS",
        help=f"earnings sweep: days to look back, Light+ only (default: {EARNINGS_SWEEP_BACK_DAYS})",
    )
    parser.add_argument(
        "--earnings-ahead",
        type=int,
        metavar="DAYS",
        help=f"earnings sweep: days to look ahead, Light+ only (default: {EARNINGS_SWEEP_AHEAD_DAYS})",
    )
    parser.add_argument(
        "--investor-types", action="store_true", help="fetch investor types (Light+)"
    )
    parser.add_argument(
        "--short-ratio", action="store_true", help="fetch sector short-selling ratios (Standard+)"
    )
    parser.add_argument(
        "--margin-interest", action="store_true", help="fetch margin interest (Standard+)"
    )
    parser.add_argument(
        "--margin-alert", action="store_true", help="fetch margin regulation alerts (Standard+)"
    )
    parser.add_argument(
        "--short-sale-report", action="store_true", help="fetch short sale report (Standard+)"
    )
    parser.add_argument(
        "--breakdown", action="store_true", help="fetch trade breakdown data (Premium)"
    )
    parser.add_argument("--calendar", action="store_true", help="fetch trading calendar (Free+)")
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="DAYS",
        help="backfill the last N days (Markets-family Tier 1 endpoints)",
    )
    parser.add_argument(
        "--skip-screener-results",
        action="store_true",
        help="skip the trailing screener_results pre-compute",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"cache DB path (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    # Backfill/one-off widening of the earnings sweep window (Light+ only;
    # _fetch_earnings_calendar_sweep reads these as module globals).
    if args.earnings_back is not None:
        EARNINGS_SWEEP_BACK_DAYS = args.earnings_back
    if args.earnings_ahead is not None:
        EARNINGS_SWEEP_AHEAD_DAYS = args.earnings_ahead

    # Use explicit options if given, otherwise auto-decide based on the plan
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
        "calendar": args.calendar,
    }
    has_explicit = any(explicit.values())

    print(f"cache DB: {args.db}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)

    cli = jquantsapi.ClientV2()

    if auto_detect:
        print("Detecting plan from J-Quants API...")
        plan = _detect_plan_from_api(cli)
        print(f"Detected plan: {plan}")
    else:
        assert configured_plan is not None
        plan = configured_plan

    available = _available_endpoints(plan)

    if has_explicit:
        targets = [ep for ep, selected in explicit.items() if selected]
        # Warn when an explicitly requested endpoint is outside the plan
        for ep in targets:
            if ep not in available:
                min_plan = ENDPOINT_MIN_PLAN[ep]
                print(f"⚠️ {ep} requires the {min_plan}+ plan (current: {plan}), skipping")
        targets = [ep for ep in targets if ep in available]
    else:
        targets = available

    print(f"plan: {plan} | targets: {', '.join(targets)}")

    # Track steps that raised so the process can exit non-zero. A scheduler
    # (supercronic / launchd / cron) can only detect a failed run via the
    # exit code; without this, a run where every endpoint errored is
    # process-level indistinguishable from a clean one and the stale-data
    # condition goes unnoticed.
    failed_steps: list[str] = []

    # Backfill mode
    if args.backfill:
        _run_backfill(cli, conn, targets, args.backfill, plan)
    else:
        # Normal daily fetch
        for ep in targets:
            label, func = FETCH_REGISTRY[ep]
            print(f"fetching {label}...")
            t0 = time.time()
            try:
                n = func(cli, conn, plan)
            except Exception as e:
                print(f"  error: {e}")
                n = 0
                failed_steps.append(ep)
            print(f"  done: {n} rows ({time.time() - t0:.1f}s)")

    # Pre-compute screener_results for the latest trading day (Issue #142).
    # Skipped in backfill mode, which uses populate_history separately for past dates.
    if not args.backfill and not args.skip_screener_results:
        print("pre-computing screener_results...")
        t0 = time.time()
        try:
            n = populate_screener_results(conn)
        except Exception as e:
            print(f"  error: {e}")
            n = 0
            failed_steps.append("screener_results")
        print(f"  done: {n} rows ({time.time() - t0:.1f}s)")

    # Result summary
    print("--- table row counts ---")
    for table in _TIER1_TABLES + ["screener_results"]:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            print(f"  {table}: {row[0]:,} rows")
        except sqlite3.OperationalError:
            pass

    db_size = args.db.stat().st_size / (1024 * 1024)
    print(f"  DB size: {db_size:.1f} MB")

    conn.close()
    print("done")

    # Surface failures to the scheduler. The summary above still reports what
    # succeeded, so exit 1 means "one or more steps failed", not "nothing ran".
    if failed_steps:
        print(f"FAILED: {len(failed_steps)} step(s): {', '.join(failed_steps)}")
        sys.exit(1)


if __name__ == "__main__":
    main()

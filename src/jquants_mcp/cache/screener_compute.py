"""Pure-Python helpers for the screener result cache.

Stdlib-only by design so that ``scripts/daily_fetch.py`` (and any other
external script that imports ``jquants_mcp.cache.schema`` via sys.path)
can populate ``screener_results`` without pulling in fastmcp/httpx.

Two responsibilities live here:

1. ``params_hash`` — deterministic short hash of a screener parameter
   dict, used as part of the ``screener_results`` primary key.
2. ``compute_high_low_signals`` — pure cross-sectional new-high / new-low
   computation over already-loaded daily bar rows. Shared by the MCP
   tools (``detect_52w_high_low`` / ``detect_ytd_high_low``) and by the
   daily populate / one-off back-fill scripts.

The MCP tool registration logic stays in ``tools/screener.py``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Public default screener parameters. The MCP tool layer also exports
# these (via its own constants) — keep them in sync.
DEFAULT_FIFTY_TWO_WEEK_SESSIONS = 252
DEFAULT_MIN_PRIOR_SESSIONS = 60

# Rolling lookback shared by the daily prune (writer side) and the
# tool-side rejection of out-of-cache queries (reader side). The two
# must move together — pruning to 52 weeks while accepting queries
# 53 weeks deep would force the slow on-demand path that the
# rejection was introduced to avoid.
SCREENER_CACHE_LOOKBACK_WEEKS = 52

# Tool names used as the primary-key ``tool_name`` column in
# ``screener_results``. Externalised so that the populate scripts and
# the MCP tools can not drift apart.
TOOL_DETECT_52W = "detect_52w_high_low"
TOOL_DETECT_YTD = "detect_ytd_high_low"
TOOL_DETECT_CONSECUTIVE_DIV = "detect_consecutive_dividend_increase"

# Bump when compute_high_low_signals output schema changes so that stale
# pre-computed Tier-2 cache entries are bypassed rather than served.
_SCHEMA_VERSION = 2

# Bump when compute_consecutive_div_snapshot output schema changes.
_CONSECUTIVE_DIV_SCHEMA_VERSION = 1

# Number of prior sessions used for volume_ratio baseline.
_VOLUME_BASELINE_SESSIONS = 20


def params_hash(params: dict[str, Any]) -> str:
    """Return a deterministic 16-char hash of screener parameters.

    Parameters that do not affect the cached cross-sectional payload
    (``code``, ``date``) must be excluded by the caller — only include
    keys that change the computed result.
    """
    sorted_items = sorted((k, str(params[k])) for k in params)
    serialized = "&".join(f"{k}={v}" for k, v in sorted_items)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def default_params_hash_52w(
    window_sessions: int = DEFAULT_FIFTY_TWO_WEEK_SESSIONS,
    min_prior_sessions: int = DEFAULT_MIN_PRIOR_SESSIONS,
) -> str:
    """Hash for ``detect_52w_high_low`` default-shaped parameters."""
    return params_hash(
        {
            "schema_version": _SCHEMA_VERSION,
            "window_sessions": window_sessions,
            "min_prior_sessions": min_prior_sessions,
        }
    )


def default_params_hash_ytd(
    min_prior_sessions: int = DEFAULT_MIN_PRIOR_SESSIONS,
) -> str:
    """Hash for ``detect_ytd_high_low`` default-shaped parameters."""
    return params_hash(
        {"schema_version": _SCHEMA_VERSION, "min_prior_sessions": min_prior_sessions}
    )


def default_params_hash_consecutive_div() -> str:
    """Hash for ``detect_consecutive_dividend_increase`` default-shaped parameters."""
    return params_hash({"schema_version": _CONSECUTIVE_DIV_SCHEMA_VERSION})


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_high_low_signals(
    rows: list[dict[str, Any]],
    *,
    norm_date: str,
    code: str | None,
    window_sessions: int | None,
    min_prior_sessions: int,
    mode_label: str,
) -> dict[str, Any]:
    """Compute new-high / new-low signals from already-fetched bar rows.

    Mirrors the historical logic that previously lived inline in
    ``tools/screener._high_low_signals``. Caller is responsible for
    fetching ``rows`` (typically ``equities_bars_daily`` between the
    window start and ``norm_date``) — this function is purely
    in-memory.
    """
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        c = str(row.get("Code") or "")
        if not c:
            continue
        by_code.setdefault(c, []).append(row)

    matches: list[dict[str, Any]] = []
    for c, sessions in sorted(by_code.items()):
        sessions.sort(key=lambda r: r.get("Date") or "")
        window = sessions if window_sessions is None else sessions[-window_sessions:]
        if not window or window[-1].get("Date") != norm_date:
            continue
        today = window[-1]
        prior = window[:-1]

        prior_highs = [_as_float(s.get("AdjH")) for s in prior]
        prior_highs = [h for h in prior_highs if h is not None]
        prior_lows = [_as_float(s.get("AdjL")) for s in prior]
        prior_lows = [low for low in prior_lows if low is not None]
        if not prior_highs or not prior_lows:
            continue
        if code is None and len(prior) < min_prior_sessions:
            continue

        today_high = _as_float(today.get("AdjH"))
        today_low = _as_float(today.get("AdjL"))
        today_close = _as_float(today.get("AdjC"))
        today_open = _as_float(today.get("AdjO"))

        prior_high = max(prior_highs)
        prior_low = min(prior_lows)

        new_high = today_high is not None and today_high >= prior_high
        new_low = today_low is not None and today_low <= prior_low
        new_high_close = today_close is not None and today_close >= prior_high
        new_low_close = today_close is not None and today_close <= prior_low

        if code is None and not (new_high or new_low or new_high_close or new_low_close):
            continue

        # VWAP = Va / Vo (raw yen per share); compare raw close vs VWAP
        va = _as_float(today.get("Va"))
        vo = _as_float(today.get("Vo"))
        raw_close = _as_float(today.get("C"))
        vwap = va / vo if (va is not None and vo is not None and vo > 0) else None
        close_vs_vwap: str | None = None
        if raw_close is not None and vwap is not None:
            close_vs_vwap = "above" if raw_close > vwap else "below"

        # volume_ratio = today Vo / mean(last _VOLUME_BASELINE_SESSIONS prior sessions)
        # volume_ratio_sessions reports the actual baseline used (< 20 near year-start)
        baseline = prior[-_VOLUME_BASELINE_SESSIONS:]
        prior_vols = [_as_float(s.get("Vo")) for s in baseline]
        prior_vols = [v for v in prior_vols if v is not None and v > 0]
        vol_avg = sum(prior_vols) / len(prior_vols) if prior_vols else None
        volume_ratio: float | None = None
        if vo is not None and vol_avg is not None:
            volume_ratio = round(vo / vol_avg, 2)

        matches.append(
            {
                "Code": c,
                "Date": norm_date,
                "prior_sessions": len(prior),
                "AdjO": today_open,
                "AdjH": today_high,
                "AdjL": today_low,
                "AdjC": today_close,
                "prior_high": prior_high,
                "prior_low": prior_low,
                "new_high": new_high,
                "new_low": new_low,
                "new_high_close": new_high_close,
                "new_low_close": new_low_close,
                "close_vs_vwap": close_vs_vwap,
                "volume_ratio": volume_ratio,
                "volume_ratio_sessions": len(prior_vols),
            }
        )
    return {"count": len(matches), "mode": mode_label, "data": matches}


# ----------------------------------------------------------------
# Direct-SQL helpers for scripts (daily_fetch.py /
# screener_populate_history.py) that hold a raw ``sqlite3.Connection``
# rather than a ``CacheStore``.
# ----------------------------------------------------------------


def _calendar_window_start_iso(end_date_iso: str, trading_days: int) -> str:
    """Return ISO calendar date >= ``trading_days`` trading days earlier.

    Pads the calendar window by 2x plus 14 days so long holiday clusters
    (Golden Week, year-end) do not eat into the requested window.
    """
    from datetime import datetime, timedelta

    end = datetime.strptime(end_date_iso, "%Y-%m-%d").date()
    calendar_days = trading_days * 2 + 14
    return (end - timedelta(days=calendar_days)).isoformat()


def fetch_daily_bars(
    conn: Any,  # sqlite3.Connection — typed as Any to keep stdlib-only imports
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """Load all ``equities_bars_daily`` rows in [date_from, date_to].

    Returns a list of parsed ``data`` dicts. Used by the populate
    scripts; the MCP tool layer goes through ``CacheStore.get_rows``
    which additionally applies plan-based date gating.
    """
    rows = conn.execute(
        "SELECT data FROM equities_bars_daily WHERE date >= ? AND date <= ?",
        (date_from, date_to),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        data = r[0] if not isinstance(r, dict) else r["data"]
        try:
            out.append(json.loads(data))
        except (TypeError, ValueError):
            continue
    return out


def compute_for_date(
    conn: Any,
    *,
    norm_date: str,
    window_sessions: int | None,
    min_prior_sessions: int,
    mode_label: str,
) -> dict[str, Any]:
    """Fetch the right window of bars and compute the screener payload.

    Bridges the SQL fetch + pure-Python compute for callers that hold
    a raw connection (daily_fetch / populate scripts). Callers
    determine the target ``tool_name`` independently when storing the
    result via :func:`upsert_screener_result`.
    """
    if window_sessions is None:
        # YTD mode: from Jan 1 of the same year through ``norm_date``.
        start = norm_date[:4] + "-01-01"
    else:
        start = _calendar_window_start_iso(norm_date, window_sessions)
    rows = fetch_daily_bars(conn, start, norm_date)
    return compute_high_low_signals(
        rows,
        norm_date=norm_date,
        code=None,
        window_sessions=window_sessions,
        min_prior_sessions=min_prior_sessions,
        mode_label=mode_label,
    )


def upsert_screener_result(
    conn: Any,
    *,
    tool_name: str,
    params_hash_value: str,
    norm_date: str,
    payload: dict[str, Any],
    computed_at: float,
) -> None:
    """INSERT OR REPLACE one ``screener_results`` row."""
    conn.execute(
        "INSERT OR REPLACE INTO screener_results "
        "(tool_name, params_hash, date, payload_json, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            tool_name,
            params_hash_value,
            norm_date,
            json.dumps(payload, ensure_ascii=False),
            computed_at,
        ),
    )


def prune_old_results(conn: Any, *, retention_weeks: int = SCREENER_CACHE_LOOKBACK_WEEKS) -> int:
    """Drop ``screener_results`` rows older than ``retention_weeks``.

    Returns the number of rows deleted. The cutoff is computed in
    Python (host local time) so the boundary follows the self-hosted
    publisher's timezone (JST) rather than SQLite's UTC ``date('now')``
    (avoids a ~9 h drift on
    JST evenings).
    """
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(weeks=int(retention_weeks))).isoformat()
    cursor = conn.execute(
        "DELETE FROM screener_results WHERE date < ?",
        (cutoff,),
    )
    return cursor.rowcount if cursor.rowcount is not None else 0


def latest_session_date(conn: Any) -> str | None:
    """Return the latest ISO date in ``equities_bars_daily``, or None."""
    row = conn.execute("SELECT MAX(date) FROM equities_bars_daily").fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])[:10]


def fetch_fy_dividend_history(conn: Any) -> dict[str, list[dict[str, Any]]]:
    """Load full FY dividend history from fins_summary.

    Mirrors ``CacheStore.get_fy_dividend_history`` for callers that hold
    a raw ``sqlite3.Connection`` (daily_fetch / populate scripts).  No
    ``as_of_date`` filter — loads the complete history for pre-computation.

    Returns:
        {code: [{fy_end, disc_date, div_ann}, ...]} sorted ascending by fy_end.
    """
    sql = (
        "WITH ranked AS ("
        "  SELECT code,"
        "    substr(json_extract(data, '$.CurFYEn'), 1, 10) AS fy_end,"
        "    substr(disc_date, 1, 10) AS disc_date_norm,"
        "    json_extract(data, '$.DivAnn') AS div_ann,"
        "    ROW_NUMBER() OVER ("
        "      PARTITION BY code, substr(json_extract(data, '$.CurFYEn'), 1, 10)"
        "      ORDER BY substr(disc_date, 1, 10) DESC"
        "    ) AS rn"
        "  FROM fins_summary"
        "  WHERE json_extract(data, '$.DocType') LIKE 'FY%'"
        "    AND json_extract(data, '$.DocType') NOT LIKE '%REIT%'"
        "    AND json_extract(data, '$.DocType') NOT LIKE '%US%'"
        ")"
        " SELECT code, fy_end, disc_date_norm, div_ann"
        " FROM ranked"
        " WHERE rn = 1"
        "   AND fy_end IS NOT NULL AND fy_end != ''"
        "   AND div_ann IS NOT NULL AND div_ann != ''"
        " ORDER BY code, fy_end"
    )
    try:
        rows = conn.execute(sql).fetchall()
    except Exception:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row[0] or "")
        if not code:
            continue
        try:
            div_ann = float(row[3])
        except (TypeError, ValueError):
            continue
        result.setdefault(code, []).append(
            {
                "fy_end": str(row[1] or "")[:10],
                "disc_date": str(row[2] or ""),
                "div_ann": div_ann,
            }
        )
    return result


def fetch_split_events_by_code(
    conn: Any,
    codes: list[str],
) -> dict[str, list[tuple[str, float]]]:
    """Return split events for multiple codes from equities_bars_daily.

    Mirrors ``CacheStore.get_split_events_by_code`` for callers that hold
    a raw ``sqlite3.Connection`` (daily_fetch / populate scripts).

    Note: equities_bars_daily has no index on ``code`` (only on ``date``),
    so each batch performs a full-table scan.  With ~3800 codes and 5.6M rows
    this takes ~12s on m1.local — acceptable for a nightly batch.
    Adding ``CREATE INDEX idx_ebd_code ON equities_bars_daily(code)`` would
    reduce this significantly if latency becomes a concern.

    Returns:
        {code: [(date, factor), ...]} sorted ascending by date.
        Codes with no split events are omitted.
    """
    if not codes:
        return {}
    all_rows: list[tuple[str, str, float]] = []
    for i in range(0, len(codes), 900):
        batch = codes[i : i + 900]
        placeholders = ",".join("?" * len(batch))
        try:
            rows = conn.execute(
                f"SELECT code, date, adj_factor FROM ("
                f"  SELECT code, date,"
                f"  COALESCE(adj_factor, json_extract(data, '$.AdjFactor')) AS adj_factor"
                f"  FROM equities_bars_daily WHERE code IN ({placeholders})"
                f") WHERE adj_factor IS NOT NULL AND adj_factor != 1.0 AND adj_factor != 0.0"
                f" ORDER BY code, date",
                batch,
            ).fetchall()
        except Exception:
            continue
        all_rows.extend((str(r[0]), str(r[1]), float(r[2])) for r in rows)
    # Build per-code lists.  all_rows is already ORDER BY code, date from SQL,
    # but sort explicitly to guarantee ascending order regardless of batch merging.
    result: dict[str, list[tuple[str, float]]] = {}
    for code, bar_date, factor in all_rows:
        result.setdefault(code, []).append((bar_date, factor))
    for lst in result.values():
        lst.sort()
    return result


def _apply_split_adj(
    entries: list[dict[str, Any]],
    split_events: list[tuple[str, float]],
) -> list[dict[str, Any]]:
    """Apply cumulative post-disc split correction to a single code's div_ann values.

    Assumes ``split_events`` is sorted ascending by date — callers must guarantee
    this (``fetch_split_events_by_code`` does; ``CacheStore.get_split_events_by_code``
    also guarantees ascending order).

    Split adjustment logic mirrors ``_compute_consecutive_div_years`` in
    ``tools/screener.py``.  If the computation rule changes, update both.
    """
    adj: list[dict[str, Any]] = []
    for entry in entries:
        disc_date = entry["disc_date"]
        factor = 1.0
        for ev_date, ev_factor in split_events:
            if ev_date > disc_date:
                factor *= ev_factor
        adj.append(
            {
                "fy_end": entry["fy_end"],
                "disc_date": disc_date,
                "div_ann": round(entry["div_ann"] * factor, 4),
            }
        )
    return adj


def compute_consecutive_div_snapshot(
    fy_history: dict[str, list[dict[str, Any]]],
    split_events_by_code: dict[str, list[tuple[str, float]]],
) -> dict[str, Any]:
    """Compute split-adjusted consecutive dividend increase years for all codes.

    Pure function — no I/O. All codes with >= 1 consecutive year of dividend
    increase are stored; ``min_years`` filtering is applied by the MCP tool at
    read time so a single cache entry serves every ``min_years`` value.

    Args:
        fy_history: Mapping of 5-digit code to sorted-ascending list of
            {fy_end, disc_date, div_ann} dicts (from ``fetch_fy_dividend_history``).
        split_events_by_code: Mapping of code to split events from
            ``fetch_split_events_by_code``.

    Returns:
        Dict with ``"count"`` (total records) and ``"data"`` list sorted by
        ``consecutive_years`` descending.  Each item contains:
        ``code`` (raw 5-digit), ``consecutive_years``, ``latest_div_ann``,
        ``latest_fy_end``, ``history`` (sorted ascending streak entries).
    """
    records: list[dict[str, Any]] = []
    for code, entries in fy_history.items():
        if len(entries) < 2:
            continue
        split_events = split_events_by_code.get(code, [])
        adj_entries = _apply_split_adj(entries, split_events)

        consecutive = 0
        for i in range(len(adj_entries) - 1, 0, -1):
            curr = adj_entries[i]["div_ann"]
            prev = adj_entries[i - 1]["div_ann"]
            if curr > 0 and curr > prev:
                consecutive += 1
            else:
                break

        if consecutive < 1:
            continue

        latest = adj_entries[-1]
        streak_entries = adj_entries[-(consecutive + 1) :]
        records.append(
            {
                "code": code,
                "consecutive_years": consecutive,
                "latest_div_ann": round(latest["div_ann"], 2),
                "latest_fy_end": latest["fy_end"],
                "history": [
                    {"fy_end": e["fy_end"], "div_ann": round(e["div_ann"], 2)}
                    for e in streak_entries
                ],
            }
        )

    records.sort(key=lambda x: -x["consecutive_years"])
    return {"count": len(records), "data": records}


def distinct_session_dates(
    conn: Any,
    date_from: str,
    date_to: str,
) -> list[str]:
    """Distinct ``equities_bars_daily`` dates in [date_from, date_to]."""
    rows = conn.execute(
        "SELECT DISTINCT date FROM equities_bars_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (date_from, date_to),
    ).fetchall()
    return [str(r[0])[:10] for r in rows]

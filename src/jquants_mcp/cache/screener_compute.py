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

# Tool names used as the primary-key ``tool_name`` column in
# ``screener_results``. Externalised so that the populate scripts and
# the MCP tools can not drift apart.
TOOL_DETECT_52W = "detect_52w_high_low"
TOOL_DETECT_YTD = "detect_ytd_high_low"


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
            "window_sessions": window_sessions,
            "min_prior_sessions": min_prior_sessions,
        }
    )


def default_params_hash_ytd(
    min_prior_sessions: int = DEFAULT_MIN_PRIOR_SESSIONS,
) -> str:
    """Hash for ``detect_ytd_high_low`` default-shaped parameters."""
    return params_hash({"min_prior_sessions": min_prior_sessions})


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

        prior_high = max(prior_highs)
        prior_low = min(prior_lows)

        new_high = today_high is not None and today_high >= prior_high
        new_low = today_low is not None and today_low <= prior_low
        new_high_close = today_close is not None and today_close >= prior_high
        new_low_close = today_close is not None and today_close <= prior_low

        if code is None and not (new_high or new_low or new_high_close or new_low_close):
            continue

        matches.append(
            {
                "Code": c,
                "Date": norm_date,
                "prior_sessions": len(prior),
                "AdjH": today_high,
                "AdjL": today_low,
                "AdjC": today_close,
                "prior_high": prior_high,
                "prior_low": prior_low,
                "new_high": new_high,
                "new_low": new_low,
                "new_high_close": new_high_close,
                "new_low_close": new_low_close,
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
    tool_name: str,
    norm_date: str,
    window_sessions: int | None,
    min_prior_sessions: int,
    mode_label: str,
) -> dict[str, Any]:
    """Fetch the right window of bars and compute the screener payload.

    Bridges the SQL fetch + pure-Python compute for callers that hold
    a raw connection (daily_fetch / populate scripts).
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


def prune_old_results(conn: Any, *, retention_weeks: int = 52) -> int:
    """Drop ``screener_results`` rows older than ``retention_weeks``.

    Returns the number of rows deleted. SQLite's ``date()`` modifier
    does not accept a ``weeks`` unit (it silently returns NULL), so we
    convert to days here.
    """
    days = int(retention_weeks) * 7
    cursor = conn.execute(
        "DELETE FROM screener_results WHERE date < date('now', ?)",
        (f"-{days} days",),
    )
    return cursor.rowcount if cursor.rowcount is not None else 0


def latest_session_date(conn: Any) -> str | None:
    """Return the latest ISO date in ``equities_bars_daily``, or None."""
    row = conn.execute("SELECT MAX(date) FROM equities_bars_daily").fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])[:10]


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

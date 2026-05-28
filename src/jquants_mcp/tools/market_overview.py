"""Market overview tools for jquants-mcp.

Cross-sectional tools that scan all listed equities to provide market-wide
summary statistics and rankings. All four tools operate on the
``equities_bars_daily`` Tier 1 cache and require no extra API calls.

Exposed tools:

- ``detect_price_change`` — advance/decline summary (値上がり/値下がり銘柄数)
- ``get_advance_decline_ratio`` — advance/decline ratio over N periods (騰落レシオ)
- ``get_top_movers`` — top gainers/losers by percentage price change
- ``get_top_volume`` — top stocks by trading volume (出来高ランキング)
- ``get_top_turnover_value`` — top stocks by turnover value (売買代金ランキング)
- ``get_sector_performance`` — sector-level average percentage change (業種別騰落率)
- ``get_dividend_yield_ranking`` — top stocks by dividend yield (高配当利回りランキング)
- ``get_market_briefing`` — composite daily briefing aggregating the above plus
  TOPIX change and screener summaries (相場ブリーフィング)
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache import screener_compute
from ..cache.store import CacheStore, make_cache_key
from ..cache.technical import compute_rsi
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_CACHE
from ..validators import (
    collect_errors,
    display_code,
    make_validation_error_response,
    normalize_code,
    validate_date,
)

logger = logging.getLogger(__name__)

_MAX_N = 100
_MAX_PERIOD = 90


def _validate_n(n: int) -> str | None:
    return "n must be between 1 and 100" if not (1 <= n <= _MAX_N) else None


def _validate_min_yield(min_yield: float) -> str | None:
    return "min_yield must be >= 0" if min_yield < 0 else None


# market segment name → equities_master Mkt integer code
_MARKET_CODE_MAP: dict[str, int] = {
    "prime": 111,
    "standard": 112,
    "growth": 113,
    "tokyo_pro": 105,
}

_VALID_MARKETS: frozenset[str] = frozenset(_MARKET_CODE_MAP)


def _validate_market(market: str | None) -> str | None:
    if market is None:
        return None
    if market not in _VALID_MARKETS:
        return f"market must be one of {sorted(_VALID_MARKETS)} or null"
    return None


def _validate_disc_months(disc_months: int) -> str | None:
    return "disc_months must be between 1 and 120" if not (1 <= disc_months <= 120) else None


def _validate_max_yield(max_yield: float | None, min_yield: float) -> str | None:
    if max_yield is None:
        return None
    if max_yield < 0:
        return "max_yield must be >= 0"
    if max_yield < min_yield:
        return "max_yield must be >= min_yield"
    return None


def _validate_period(period: int) -> str | None:
    return (
        f"period must be between 1 and {_MAX_PERIOD}" if not (1 <= period <= _MAX_PERIOD) else None
    )


def _validate_direction(direction: str) -> str | None:
    return "direction must be 'up' or 'down'" if direction not in ("up", "down") else None


def _normalize_date(d: str) -> str:
    if "-" in d:
        return d
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _cache_not_ready_error(requested_date: str, latest: str | None) -> dict[str, Any]:
    return {
        "error": True,
        "error_type": "CacheNotReady",
        "message": (
            f"Data for {requested_date} not yet available. "
            f"Latest cache date: {latest or 'unknown'}."
        ),
        "hint": "Try again after 17:15 JST on trading days.",
    }


def _get_session_dates(cache: CacheStore, end_date: str, count: int) -> list[str]:
    """Return up to *count* most recent trading dates up to and including *end_date*."""
    # Pad calendar days generously to cover holidays (Golden Week = 5 days, etc.)
    start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=count * 3)).strftime(
        "%Y-%m-%d"
    )
    dates = cache.iter_session_dates(date_from=start, date_to=end_date)
    return dates[-count:] if len(dates) >= count else dates


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _calc_short_ratio(row: dict) -> float | None:
    """Compute short-sale ratio (%) from raw /markets/short-ratio API fields.

    Returns (ShrtWithResVa + ShrtNoResVa) / total_sell * 100, or None when
    any field is missing or the total is zero.
    """
    sell_ex = _as_float(row.get("SellExShortVa"))
    shrt_with = _as_float(row.get("ShrtWithResVa"))
    shrt_no = _as_float(row.get("ShrtNoResVa"))
    if None in (sell_ex, shrt_with, shrt_no):
        return None
    total = sell_ex + shrt_with + shrt_no  # type: ignore[operator]
    if total == 0:
        return None
    return (shrt_with + shrt_no) / total * 100  # type: ignore[operator]


# Safety cap: in heavy sell-off days 52w-low candidates can reach several
# hundred; capping the universe before RSI computation keeps latency bounded.
_MAX_NOTABLE_UNIVERSE = 200


def _get_52w_screener_data(cache: CacheStore, norm_date: str) -> dict[str, Any] | None:
    """Return the pre-computed 52w high/low screener payload for norm_date.

    Returns None when the screener_results cache is cold (daily_fetch has not
    yet run). Callers must degrade gracefully rather than triggering the slow
    on-demand scan that can exceed Cloud Run tool-call timeouts.
    """
    params_hash = screener_compute.default_params_hash_52w()
    return cache.screener_result_get(screener_compute.TOOL_DETECT_52W, params_hash, norm_date)


def _compute_notable_stocks(
    w52_payload: dict[str, Any] | None,
    plimit_full: dict[str, Any],
    code_closes: dict[str, list[float]],
    code_volumes: dict[str, list[float]],
    name_map: dict[str, str],
    prev_close_map: dict[str, float],
    today_close_map: dict[str, float | None],
    n: int,
) -> dict[str, Any]:
    """Score and rank notable stocks from the 52w high/low + price-limit universe.

    Universe: stocks hitting a new 52-week high/low (from pre-computed screener)
    or touching the daily price limit (ストップ高/安).  For each candidate the
    RSI14 is computed from the already-fetched ADR window closes, then:

    - overbought: sorted by RSI14 descending (higher = more stretched), top n
    - oversold:   sorted by RSI14 ascending  (lower  = more beaten down), top n

    Returns {"overbought": [...], "oversold": [...]} — each list may be empty.
    """

    def _vol_ratio(code_5: str) -> float | None:
        vols = code_volumes.get(code_5, [])
        if len(vols) < 2:
            return None
        baseline = vols[-21:-1]
        if not baseline:
            return None
        avg = sum(baseline) / len(baseline)
        if avg <= 0:
            return None
        return round(vols[-1] / avg, 2)

    def _rsi14(code_5: str) -> float | None:
        closes = code_closes.get(code_5, [])
        series = compute_rsi(closes, 14)
        val = next((v for v in reversed(series) if v is not None), None)
        return round(val, 1) if val is not None else None

    def _change_pct(code_5: str) -> float | None:
        c = today_close_map.get(code_5)
        p = prev_close_map.get(code_5)
        if c is not None and p is not None and p != 0:
            return round((c - p) / p * 100, 2)
        return None

    # Build universe: code_5 → {"signals": [...], "volume_ratio": float|None}
    overbought_u: dict[str, dict[str, Any]] = {}
    oversold_u: dict[str, dict[str, Any]] = {}

    if w52_payload:
        for item in w52_payload.get("data", [])[:_MAX_NOTABLE_UNIVERSE]:
            code_5 = str(item.get("Code") or "")
            if not code_5:
                continue
            vr = item.get("volume_ratio")
            if item.get("new_high"):
                entry = overbought_u.setdefault(code_5, {"signals": [], "volume_ratio": vr})
                entry["signals"].append("52w_high")
            if item.get("new_low"):
                entry = oversold_u.setdefault(code_5, {"signals": [], "volume_ratio": vr})
                entry["signals"].append("52w_low")

    for item in plimit_full.get("data", [])[:_MAX_NOTABLE_UNIVERSE]:
        code_5 = normalize_code(str(item.get("Code") or ""))
        if not code_5:
            continue
        if item.get("limit_high_touched"):
            entry = overbought_u.setdefault(code_5, {"signals": [], "volume_ratio": None})
            if "limit_high" not in entry["signals"]:
                entry["signals"].append("limit_high")
        if item.get("limit_low_touched"):
            entry = oversold_u.setdefault(code_5, {"signals": [], "volume_ratio": None})
            if "limit_low" not in entry["signals"]:
                entry["signals"].append("limit_low")

    def _build_entries(universe: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        entries = []
        for code_5, info in universe.items():
            vr = info["volume_ratio"] if info["volume_ratio"] is not None else _vol_ratio(code_5)
            entries.append(
                {
                    "code": display_code(code_5),
                    "name": name_map.get(code_5, ""),
                    "close": today_close_map.get(code_5),
                    "change_pct": _change_pct(code_5),
                    "rsi14": _rsi14(code_5),
                    "volume_ratio": vr,
                    "signals": info["signals"],
                }
            )
        return entries

    overbought_entries = _build_entries(overbought_u)
    oversold_entries = _build_entries(oversold_u)

    overbought_entries.sort(key=lambda x: (x["rsi14"] is None, -(x["rsi14"] or 0.0)))
    oversold_entries.sort(key=lambda x: (x["rsi14"] is None, x["rsi14"] or 0.0))

    return {
        "overbought": overbought_entries[:n],
        "oversold": oversold_entries[:n],
    }


def _compute_advance_decline(
    today_rows: list[dict[str, Any]],
    prev_close_map: dict[str, float],
) -> tuple[int, int, int]:
    """Return (advances, declines, unchanged) given today's rows and a prev-close map."""
    advances = declines = unchanged = 0
    for row in today_rows:
        code = str(row.get("Code") or "")
        prev = prev_close_map.get(code)
        today = _as_float(row.get("AdjC"))
        if prev is None or today is None:
            continue
        if today > prev:
            advances += 1
        elif today < prev:
            declines += 1
        else:
            unchanged += 1
    return advances, declines, unchanged


def _rows_to_close_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        code = str(row.get("Code") or "")
        v = _as_float(row.get("AdjC"))
        if code and v is not None:
            result[code] = v
    return result


# ---------------------------------------------------------------------------
# Pure computation helpers — no cache access; accept pre-fetched rows/maps.
# These are called by both the individual tools and get_market_briefing, so
# that the briefing can fetch equities_bars_daily exactly once instead of
# once per sub-tool (N+1 elimination).
# ---------------------------------------------------------------------------


def _compute_advance_decline_summary(
    today_rows: list[dict[str, Any]],
    prev_close_map: dict[str, float],
) -> dict[str, Any]:
    """Return advance/decline summary dict from pre-fetched rows."""
    advances, declines, unchanged = _compute_advance_decline(today_rows, prev_close_map)
    total = advances + declines + unchanged
    ratio = round(advances / declines, 4) if declines > 0 else None
    return {
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "total": total,
        "advance_decline_ratio": ratio,
    }


def _compute_advance_decline_ratio(
    by_date: dict[str, list[dict[str, Any]]],
    session_dates: list[str],
) -> dict[str, Any]:
    """Return ADR dict from pre-grouped rows and ordered session date list."""
    advances_sum = declines_sum = actual_period = 0
    for i in range(1, len(session_dates)):
        prev_d = session_dates[i - 1]
        today_d = session_dates[i]
        prev_rows_d = by_date.get(prev_d, [])
        today_rows_d = by_date.get(today_d, [])
        if not prev_rows_d or not today_rows_d:
            continue
        prev_map = _rows_to_close_map(prev_rows_d)
        adv, dec, _ = _compute_advance_decline(today_rows_d, prev_map)
        advances_sum += adv
        declines_sum += dec
        actual_period += 1
    ratio = round(advances_sum / declines_sum * 100, 2) if declines_sum > 0 else None
    return {
        "period": actual_period,
        "ratio": ratio,
        "advances_sum": advances_sum,
        "declines_sum": declines_sum,
    }


def _compute_top_movers(
    today_rows: list[dict[str, Any]],
    prev_close_map: dict[str, float],
    name_map: dict[str, str | None],
    direction: str,
    n: int,
) -> list[dict[str, Any]]:
    """Return top movers list sorted by change_pct from pre-fetched rows."""
    movers: list[dict[str, Any]] = []
    for row in today_rows:
        code = str(row.get("Code") or "")
        today_close = _as_float(row.get("AdjC"))
        prev_close = prev_close_map.get(code)
        if today_close is None or prev_close is None or prev_close == 0:
            continue
        change_pct = round((today_close - prev_close) / prev_close * 100, 4)
        movers.append(
            {
                "code": display_code(code),
                "name": name_map.get(code),
                "close": today_close,
                "prev_close": prev_close,
                "change_pct": change_pct,
            }
        )
    movers.sort(key=lambda x: x["change_pct"], reverse=(direction == "up"))
    return movers[:n]


def _compute_top_turnover_value(
    today_rows: list[dict[str, Any]],
    name_map: dict[str, str | None],
    n: int,
) -> list[dict[str, Any]]:
    """Return top turnover value list sorted by Va from pre-fetched rows."""
    items: list[dict[str, Any]] = []
    for row in today_rows:
        code = str(row.get("Code") or "")
        turnover = _as_float(row.get("Va"))
        if turnover is None:
            continue
        volume = _as_float(row.get("Vo"))
        items.append(
            {
                "code": display_code(code),
                "name": name_map.get(code),
                "turnover_value": turnover,
                "volume": int(volume) if volume is not None else None,
                "close": _as_float(row.get("C")),
            }
        )
    items.sort(key=lambda x: x["turnover_value"], reverse=True)
    return items[:n]


def _compute_sector_performance(
    today_rows: list[dict[str, Any]],
    prev_close_map: dict[str, float],
    sector_map: dict[str, dict[str, Any]],
    sector_type: str,
) -> list[dict[str, Any]]:
    """Return sector performance list sorted by avg_change_pct from pre-fetched rows."""
    code_key = sector_type
    name_key = f"{sector_type}_name"

    # buckets[sector_code] = {"name": str, "advances": int, "declines": int,
    #                         "unchanged": int, "pct_sum": float, "count": int}
    buckets: dict[str, dict[str, Any]] = {}
    for row in today_rows:
        code = str(row.get("Code") or "")
        today_close = _as_float(row.get("AdjC"))
        prev_close = prev_close_map.get(code)
        if today_close is None or prev_close is None or prev_close == 0:
            continue
        sector = sector_map.get(code, {})
        sec_code = sector.get(code_key) or ""
        if not sec_code:
            continue
        sec_name = sector.get(name_key) or ""
        change_pct = (today_close - prev_close) / prev_close * 100.0
        bucket = buckets.setdefault(
            sec_code,
            {
                "name": sec_name,
                "advances": 0,
                "declines": 0,
                "unchanged": 0,
                "pct_sum": 0.0,
                "count": 0,
            },
        )
        # The first stock in this sector seeds bucket["name"] via setdefault
        # above; this branch only fires when that initial seed was an empty
        # string (rare J-Quants data quality case) and a later stock has
        # the non-empty form, so we backfill the proper sector name.
        if not bucket["name"] and sec_name:
            bucket["name"] = sec_name
        if today_close > prev_close:
            bucket["advances"] += 1
        elif today_close < prev_close:
            bucket["declines"] += 1
        else:
            bucket["unchanged"] += 1
        bucket["pct_sum"] += change_pct
        bucket["count"] += 1

    sectors: list[dict[str, Any]] = []
    for sec_code, bucket in buckets.items():
        if bucket["count"] == 0:
            continue
        sectors.append(
            {
                "code": sec_code,
                "name": bucket["name"] or None,
                "count": bucket["count"],
                "advances": bucket["advances"],
                "declines": bucket["declines"],
                "unchanged": bucket["unchanged"],
                "avg_change_pct": round(bucket["pct_sum"] / bucket["count"], 4),
            }
        )

    sectors.sort(key=lambda x: x["avg_change_pct"], reverse=True)
    return sectors


def register(
    mcp: FastMCP,
    get_client: Any,  # noqa: ARG001
    get_cache: Any,
) -> None:
    """Register market overview tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_price_change(
        date: str,
    ) -> dict[str, Any]:
        """Return the daily advance/decline summary for all listed equities (騰落集計). All plans.

        Use for 値上がり銘柄数・値下がり銘柄数・騰落集計 queries.
        For rolling ADR ratio use get_advance_decline_ratio; for sector breakdown use get_sector_performance.
        Data available ~17:15 JST on trading days.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            date: Trading date (YYYY-MM-DD or YYYYMMDD).
        """
        errors = collect_errors(validate_date(date, "date"))
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        session_dates = _get_session_dates(cache, norm_date, 2)
        if len(session_dates) < 2:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": f"Not enough trading days before {norm_date}.",
            }

        prev_date = session_dates[-2]
        today_date = session_dates[-1]

        try:
            today_rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=today_date, date_to=today_date
            )
            prev_rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=prev_date, date_to=prev_date
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        prev_close_map = _rows_to_close_map(prev_rows)
        summary = _compute_advance_decline_summary(today_rows, prev_close_map)

        return {
            "date": today_date,
            "previous_date": prev_date,
            **summary,
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_advance_decline_ratio(
        date: str,
        period: int = 25,
    ) -> dict[str, Any]:
        """Return the advance/decline ratio (騰落レシオ) over the last period trading days. All plans.

        Use for 騰落レシオ・市場過熱感 queries. >120 = overbought; <70 = oversold (general convention).
        For daily advance/decline counts use detect_price_change; for sector breakdown use get_sector_performance.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            date: End date (YYYY-MM-DD or YYYYMMDD).
            period: Trailing trading days to accumulate (default 25).
        """
        errors = collect_errors(validate_date(date, "date"), _validate_period(period))
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        # Need period+1 dates: the extra date is the "previous" base for the first comparison
        session_dates = _get_session_dates(cache, norm_date, period + 1)
        if len(session_dates) < 2:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": f"Not enough trading days before {norm_date}.",
            }

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={},
                date_from=session_dates[0],
                date_to=norm_date,
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            d = str(row.get("Date") or "")[:10]
            by_date.setdefault(d, []).append(row)

        adr = _compute_advance_decline_ratio(by_date, session_dates)

        return {
            "date": norm_date,
            "period": adr["period"],
            "ratio": adr["ratio"],
            "advances_sum": adr["advances_sum"],
            "declines_sum": adr["declines_sum"],
            "note": "Universe: all listed equities in J-Quants cache (not limited to Nikkei 225 / TOPIX).",
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_top_movers(
        date: str,
        direction: str = "up",
        n: int = 10,
    ) -> dict[str, Any]:
        """Return top stocks by percentage price change on a given trading date.

        Uses split-adjusted closing prices (AdjC) to compute change_pct =
        (today - prev) / prev * 100.

        Args:
            date: Trading date in YYYY-MM-DD or YYYYMMDD format.
            direction: "up" for top gainers, "down" for top losers. Default: "up".
            n: Number of stocks to return (1–100). Default: 10.

        Returns:
            dict with keys:
            - date: the requested trading date
            - previous_date: the comparison base date
            - direction: "up" or "down"
            - items: list of up to *n* dicts, each with:
                - code: stock code (5-digit)
                - close: today's closing price
                - prev_close: previous day's closing price
                - change_pct: percentage price change
        """
        errors = collect_errors(
            validate_date(date, "date"), _validate_direction(direction), _validate_n(n)
        )
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        session_dates = _get_session_dates(cache, norm_date, 2)
        if len(session_dates) < 2:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": f"Not enough trading days before {norm_date}.",
            }

        prev_date = session_dates[-2]
        today_date = session_dates[-1]

        try:
            today_rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=today_date, date_to=today_date
            )
            prev_rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=prev_date, date_to=prev_date
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        prev_close_map = _rows_to_close_map(prev_rows)
        name_map = cache.get_name_map()

        return {
            "date": today_date,
            "previous_date": prev_date,
            "direction": direction,
            "items": _compute_top_movers(today_rows, prev_close_map, name_map, direction, n),
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_top_volume(
        date: str,
        n: int = 10,
    ) -> dict[str, Any]:
        """Return top stocks by trading volume on a given date.

        Args:
            date: Trading date in YYYY-MM-DD or YYYYMMDD format.
            n: Number of stocks to return (1–100). Default: 10.

        Returns:
            dict with keys:
            - date: the requested trading date
            - items: list of up to *n* dicts, each with:
                - code: stock code (5-digit)
                - volume: number of shares traded
                - turnover_value: trading value in yen
                - close: closing price
        """
        errors = collect_errors(validate_date(date, "date"), _validate_n(n))
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        try:
            rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=norm_date, date_to=norm_date
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        if not rows:
            return {
                "error": True,
                "error_type": "NoTradingData",
                "message": f"No trading data found for {norm_date}. It may be a holiday or non-trading day.",
            }

        name_map = cache.get_name_map()
        items: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("Code") or "")
            volume = _as_float(row.get("Vo"))
            if volume is None:
                continue
            items.append(
                {
                    "code": display_code(code),
                    "name": name_map.get(code),
                    "volume": int(volume),
                    "turnover_value": _as_float(row.get("Va")),
                    "close": _as_float(row.get("C")),
                }
            )

        items.sort(key=lambda x: x["volume"], reverse=True)

        return {
            "date": norm_date,
            "items": items[:n],
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_top_turnover_value(
        date: str,
        n: int = 10,
    ) -> dict[str, Any]:
        """Return top stocks by turnover value (売買代金ランキング) on a given date. All plans.

        Use for 売買代金ランキング・売買代金・turnover・trading value queries.
        Ranks by price×volume (get_top_volume ranks by share count instead).

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            date: Trading date (YYYY-MM-DD or YYYYMMDD).
            n: Number of stocks to return (1–100). Default 10.
        """
        errors = collect_errors(validate_date(date, "date"), _validate_n(n))
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        try:
            rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=norm_date, date_to=norm_date
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        if not rows:
            return {
                "error": True,
                "error_type": "NoTradingData",
                "message": f"No trading data found for {norm_date}. It may be a holiday or non-trading day.",
            }

        name_map = cache.get_name_map()

        return {
            "date": norm_date,
            "items": _compute_top_turnover_value(rows, name_map, n),
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_sector_performance(
        date: str,
        sector_type: str = "s33",
    ) -> dict[str, Any]:
        """Sector-level average price change ranking (業種別騰落率). All plans.

        Use for 業種別騰落率, セクター別パフォーマンス, 業種別ランキング, sector performance.
        For sector valuation (PER/PBR) use get_sector_briefing instead.
        For full market briefing use get_market_briefing instead.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            date: Trading date (YYYY-MM-DD or YYYYMMDD).
            sector_type: "s33" (default, 33 sub-sectors) or "s17" (17 top-level).
        """
        if sector_type not in ("s33", "s17"):
            return make_validation_error_response(["sector_type must be 's33' or 's17'"])
        errors = collect_errors(validate_date(date, "date"))
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        session_dates = _get_session_dates(cache, norm_date, 2)
        if len(session_dates) < 2:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": f"Not enough trading days before {norm_date}.",
            }

        prev_date = session_dates[-2]
        today_date = session_dates[-1]

        try:
            today_rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=today_date, date_to=today_date
            )
            prev_rows = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=prev_date, date_to=prev_date
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        prev_close_map = _rows_to_close_map(prev_rows)
        sector_map = cache.get_sector_map()

        return {
            "date": today_date,
            "previous_date": prev_date,
            "sector_type": sector_type,
            "sectors": _compute_sector_performance(
                today_rows, prev_close_map, sector_map, sector_type
            ),
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_dividend_yield_ranking(
        n: int = 20,
        min_yield: float = 3.0,
        max_yield: float | None = None,
        disc_months: int = 18,
        include_trailing: bool = False,
        market: str | None = None,
        sector: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """High dividend yield stock ranking (高配当利回りランキング). All plans.

        Use for 高配当, 配当利回り, dividend yield ranking, 高利回り銘柄.
        For single-stock yield see get_stock_briefing instead.

        Default (include_trailing=False) matches Kabutan 予想配当利回りランキング:
        only stocks with a forward forecast (FDivAnn / NxFDivAnn) appear.
        Set include_trailing=True to also include trailing-DivAnn-only stocks.
        Dividend priority: NxFDivAnn (next-FY forecast, annual filings only) >
        FDivAnn (current-FY forecast) > DivAnn (trailing; only when include_trailing=True).

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            n: Stocks to return (1–100, default 20).
            min_yield: Minimum yield % (default 3.0).
            max_yield: Maximum yield % cap (default null).
            disc_months: Max disclosure age in months (default 18).
            include_trailing: Include DivAnn-only stocks (default False = Kabutan-equivalent).
            market: "prime" / "standard" / "growth" / "tokyo_pro" (default all).
            sector: S33 sector code filter (default all).
            date: Trading date (YYYY-MM-DD or YYYYMMDD, default latest cached).
        """
        errors = collect_errors(
            _validate_n(n),
            _validate_min_yield(min_yield),
            _validate_max_yield(max_yield, min_yield),
            _validate_disc_months(disc_months),
            _validate_market(market),
        )
        if date is not None:
            errors += collect_errors(validate_date(date, "date"))
        if errors:
            return make_validation_error_response(errors)

        cache: CacheStore = get_cache()

        if date is None:
            latest = cache.get_latest_equities_date()
            if latest is None:
                return _cache_not_ready_error("latest", None)
            norm_date = latest
        else:
            norm_date = _normalize_date(date)
            latest = cache.get_latest_equities_date()
            if latest and norm_date > latest:
                return _cache_not_ready_error(norm_date, latest)
            session = _get_session_dates(cache, norm_date, 1)
            if not session:
                return _cache_not_ready_error(norm_date, latest)
            norm_date = session[-1]

        cache_key = make_cache_key(
            "tool:get_dividend_yield_ranking",
            {
                "date": norm_date,
                "n": n,
                "min_yield": min_yield,
                "max_yield": max_yield,
                "disc_months": disc_months,
                "include_trailing": include_trailing,
                "market": market,
                "sector": sector,
            },
        )
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        try:
            bars = cache.get_rows(
                "equities_bars_daily", key_filter={}, date_from=norm_date, date_to=norm_date
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        if not bars:
            return {
                "error": True,
                "error_type": "NoTradingData",
                "message": f"No trading data found for {norm_date}. It may be a holiday or non-trading day.",
            }

        # Forward dividend (FDivAnn > NxFDivAnn): already in post-split terms,
        # must NOT receive FY-end split correction.
        fwd_div_map = cache.get_forward_div_ann_map()
        # Trailing actual dividend (DivAnn): fallback when include_trailing=True.
        # May be in pre-split terms; requires FY-end split correction.
        trailing_div_map = cache.get_div_ann_map() if include_trailing else {}
        name_map = cache.get_name_map()
        sector_map = cache.get_sector_map()

        # Merge: forward takes priority over trailing.
        # Track which codes use trailing DivAnn so FY-end correction is applied
        # only to those (applying it to FDivAnn/NxFDivAnn would give wrong values).
        merged_div_map: dict[str, tuple[float, str]] = {}
        trailing_codes: set[str] = set()
        for code, entry in fwd_div_map.items():
            merged_div_map[code] = entry
        for code, entry in trailing_div_map.items():
            if code not in merged_div_map:
                merged_div_map[code] = entry
                trailing_codes.add(code)

        # disc_months cutoff: exclude stale disclosures.
        # Use 31 days/month intentionally: slightly over-estimates so borderline
        # disclosures (e.g. filed on the exact cutoff day) are consistently excluded
        # rather than flickering in/out across month-length differences.
        cutoff_date = (
            datetime.strptime(norm_date, "%Y-%m-%d") - timedelta(days=disc_months * 31)
        ).strftime("%Y-%m-%d")

        # market filter: resolve to Mkt integer string
        mkt_code: str | None = str(_MARKET_CODE_MAP[market]) if market is not None else None

        code_disc_dates = {code: disc for code, (_, disc) in merged_div_map.items()}
        split_factors = cache.get_split_factors_after(code_disc_dates)
        # FY-end splits: DivAnn (trailing) may be in pre-split terms when the split
        # occurred ~45 days before the annual results filing.
        # Apply ONLY to trailing DivAnn codes — FDivAnn/NxFDivAnn are already post-split.
        if trailing_div_map:
            trailing_disc_dates = {code: disc for code, (_, disc) in trailing_div_map.items()}
            split_factors_fye = cache.get_split_factors_before_disc(trailing_disc_dates)
        else:
            split_factors_fye: dict[str, float] = {}

        items: list[dict[str, Any]] = []
        for row in bars:
            code = str(row.get("Code") or "")
            adj_c = _as_float(row.get("AdjC"))
            entry = merged_div_map.get(code)
            if adj_c is None or adj_c <= 0 or entry is None:
                continue
            div_ann, disc_date = entry

            # exclude stale disclosures
            if disc_date < cutoff_date:
                continue

            # market filter
            info = sector_map.get(code, {})
            if mkt_code is not None and info.get("mkt") != mkt_code:
                continue

            # sector filter (S33 code as string)
            if sector is not None and info.get("s33") != str(sector):
                continue

            is_trailing = code in trailing_codes
            fye_factor = split_factors_fye.get(code, 1.0) if is_trailing else 1.0
            adj_div_ann = div_ann * split_factors.get(code, 1.0) * fye_factor
            yield_pct = round(adj_div_ann / adj_c * 100, 4)
            if yield_pct < min_yield:
                continue
            if max_yield is not None and yield_pct > max_yield:
                continue
            items.append(
                {
                    "code": display_code(code),
                    "name": name_map.get(code),
                    "market": info.get("mkt_name") or info.get("mkt", ""),
                    "sector": info.get("s33_name", ""),
                    "div_ann": round(adj_div_ann, 4),
                    "disc_date": disc_date,
                    "div_source": "trailing" if is_trailing else "forward",
                    "close": adj_c,
                    "yield_pct": yield_pct,
                }
            )

        items.sort(key=lambda x: x["yield_pct"], reverse=True)
        result: dict[str, Any] = {
            "date": norm_date,
            "count": min(len(items), n),
            "filters": {
                "min_yield": min_yield,
                "max_yield": max_yield,
                "disc_months": disc_months,
                "include_trailing": include_trailing,
                "market": market,
                "sector": sector,
            },
            "items": items[:n],
        }
        cache.put_response(cache_key, result, ttl_seconds=3600)
        return result

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_market_briefing(
        date: str,
        sector_type: str = "s33",
        n: int = 5,
    ) -> dict[str, Any]:
        """Daily market briefing: ADR, sector ranking, top movers, turnover, screener highlights (相場ブリーフィング).

        Use for 相場ブリーフィング, 市場概況, 今日の相場, daily briefing, market summary.
        For sector valuation (PER/PBR) use get_sector_briefing instead.
        For single-stock detail use get_stock_briefing instead.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Returns: summary (ADR 25d, TOPIX change, margin ratio), sector top/bottom n,
        sector_short_ratios (S33 空売り比率, Standard+), top movers, top turnover,
        screener highlights (52w/YTD highs/lows, volume surges, price limits,
        notable stocks by RSI14), trend signals (distribution days, follow-through).
        Margin/short-ratio fields are null when those caches are absent.

        Args:
            date: Trading date (YYYY-MM-DD or YYYYMMDD).
            sector_type: "s33" (default, 33 TSE sub-sectors) or "s17" (17 top-level).
            n: TopN size for movers/turnover sections (1–100, default 5).
        """
        if sector_type not in ("s33", "s17"):
            return make_validation_error_response(["sector_type must be 's33' or 's17'"])
        errors = collect_errors(validate_date(date, "date"), _validate_n(n))
        if errors:
            return make_validation_error_response(errors)

        norm_date = _normalize_date(date)
        cache: CacheStore = get_cache()

        # ``norm_date > latest`` (future date / cache empty) returns CacheNotReady.
        # InsufficientData below fires only when norm_date ≤ latest but the cache
        # has fewer than 2 trading sessions — effectively "first day of data".
        latest = cache.get_latest_equities_date()
        if latest and norm_date > latest:
            return _cache_not_ready_error(norm_date, latest)

        # Tier 2 response cache: same params within 1h reuse the composite result.
        cache_key = make_cache_key(
            "tool:get_market_briefing",
            {"date": norm_date, "sector_type": sector_type, "n": n},
        )
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        # Internal helper: call MCP tool by name and unwrap the JSON content.
        # Used for screener tools (registered in screener.register) and TOPIX.
        async def _call_json(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            import json as _json

            try:
                result = await mcp.call_tool(tool_name, args)
            except Exception:
                # Fail-soft: a missing API key, network blip, or any other tool
                # error must not derail the whole briefing. Caller checks
                # `result.get("error")` and substitutes a neutral default.
                return {"error": True, "error_type": "ToolCallFailed"}
            text = result.content[0].text if result.content else "{}"
            try:
                return _json.loads(text)
            except (ValueError, TypeError):
                return {}

        # Fetch equities_bars_daily exactly once — a span wide enough for the
        # 25-day ADR window (period+1 = 26 session dates). today and prev are
        # extracted from by_date, so no separate single-date fetches are needed.
        adr_period = 25
        session_dates_adr = _get_session_dates(cache, norm_date, adr_period + 1)
        if len(session_dates_adr) < 2:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": f"Not enough trading days before {norm_date}.",
            }

        today_date = session_dates_adr[-1]
        prev_date = session_dates_adr[-2]

        try:
            adr_rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={},
                date_from=session_dates_adr[0],
                date_to=today_date,
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        # Group by date once; all sub-computations read from this dict.
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in adr_rows:
            d = str(row.get("Date") or "")[:10]
            by_date.setdefault(d, []).append(row)

        today_rows = by_date.get(today_date, [])
        prev_rows = by_date.get(prev_date, [])

        if not today_rows:
            return {
                "error": True,
                "error_type": "NoTradingData",
                "message": f"No trading data found for {today_date}. It may be a holiday or non-trading day.",
            }

        # Per-code close/volume series for RSI14 and volume-ratio used by
        # _compute_notable_stocks.  Built in one pass over the already-fetched
        # adr_rows so no additional DB queries are needed.
        # Use split-adjusted close (AdjC) only — consistent with prev_close_map
        # (_rows_to_close_map), advance/decline, and top_movers. Falling back to
        # raw C would mix adjusted and raw prices across days and corrupt
        # cross-day deltas (RSI series, change_pct) whenever a split occurred.
        code_closes: dict[str, list[float]] = {}
        code_volumes: dict[str, list[float]] = {}
        for d in sorted(by_date.keys()):
            for row in by_date[d]:
                c5 = str(row.get("Code") or "")
                cl = _as_float(row.get("AdjC"))
                vo = _as_float(row.get("Vo"))
                if c5 and cl is not None:
                    code_closes.setdefault(c5, []).append(cl)
                if c5 and vo is not None:
                    code_volumes.setdefault(c5, []).append(vo)

        today_close_map: dict[str, float | None] = {
            str(r.get("Code") or ""): _as_float(r.get("AdjC")) for r in today_rows if r.get("Code")
        }

        prev_close_map = _rows_to_close_map(prev_rows)
        name_map = cache.get_name_map()
        sector_map = cache.get_sector_map()

        # Market-wide margin ratio (optional — empty dict when not cached)
        margin_map = cache.get_all_latest_margin_interest()
        margin_ratios = []
        for mrow in margin_map.values():
            long_v = _as_float(mrow.get("LongVol"))
            short_v = _as_float(mrow.get("ShrtVol"))
            if long_v is not None and short_v is not None and short_v > 0:
                margin_ratios.append(long_v / short_v)
        market_margin_ratio_median = (
            round(statistics.median(margin_ratios), 2) if margin_ratios else None
        )

        # Sector short-sale ratios (optional — empty dict when not cached; Standard+)
        short_ratio_map = cache.get_all_latest_short_ratio()
        # Build s33_code → name from sector_map for label enrichment.
        # Normalise keys through int so "0050" and "50" map to the same entry.
        s33_name_map: dict[str, str] = {}
        for info in sector_map.values():
            sc = CacheStore._norm_s33(info.get("s33", ""))
            sn = info.get("s33_name", "")
            if sc and sn and sc not in s33_name_map:
                s33_name_map[sc] = sn

        # 1. Core advance/decline summary.
        ad = _compute_advance_decline_summary(today_rows, prev_close_map)

        # 2. 25-day advance/decline ratio (騰落レシオ).
        adr = _compute_advance_decline_ratio(by_date, session_dates_adr)
        adr_value = adr.get("ratio")

        # 3. Sector top/bottom (n each side, may overlap when total sectors < 2n).
        sectors_list = _compute_sector_performance(
            today_rows, prev_close_map, sector_map, sector_type
        )
        sectors_top = sectors_list[:n]
        sectors_bottom = list(reversed(sectors_list[-n:])) if sectors_list else []

        # Enrich each sector entry with short_sale_ratio (null when not cached).
        # Note: when sector_type="s17", entry["code"] is an S17 code (e.g. "7")
        # which never matches the S33 keys in short_ratio_map → always null.
        # sector_short_ratios below is always S33-based regardless of sector_type.
        for entry in sectors_top + sectors_bottom:
            # short_ratio_map keys are normalised via _norm_s33 (e.g. "50"),
            # but entry["code"] is the raw S33 from sector_map (e.g. "0050"),
            # so the lookup must normalise too. (S17 codes never match and stay null.)
            sr_row = short_ratio_map.get(CacheStore._norm_s33(entry["code"]))
            ratio = _calc_short_ratio(sr_row) if sr_row else None
            entry["short_sale_ratio"] = round(ratio, 2) if ratio is not None else None

        # Full sector short-sale ratio list sorted by ratio descending.
        sector_short_ratios: list[dict[str, Any]] = []
        for s33_code, sr_row in short_ratio_map.items():
            ratio = _calc_short_ratio(sr_row)
            if ratio is not None:
                sector_short_ratios.append(
                    {
                        "sector_code": s33_code,
                        "sector_name": s33_name_map.get(s33_code, ""),
                        "short_sale_ratio": round(ratio, 2),
                        "date": str(sr_row.get("Date") or ""),
                    }
                )
        sector_short_ratios.sort(key=lambda x: x["short_sale_ratio"], reverse=True)
        if short_ratio_map and not sector_short_ratios:
            logger.warning(
                "short_ratio cache has %d rows but all ratios are null — API field names may have changed",
                len(short_ratio_map),
            )

        # 4. Top movers and top turnover.
        movers_up = _compute_top_movers(today_rows, prev_close_map, name_map, "up", n)
        movers_down = _compute_top_movers(today_rows, prev_close_map, name_map, "down", n)
        turnover_items = _compute_top_turnover_value(today_rows, name_map, n)

        # 5. Screener summaries via call_tool (registered by screener.register).
        #    detect_price_limit uses detail=True so we reuse its per-stock data
        #    for the notable_stocks highlights without a second call.
        ytd = await _call_json("detect_ytd_high_low", {"date": norm_date})
        vsurge = await _call_json("detect_volume_surge", {"date": norm_date})
        plimit = await _call_json("detect_price_limit", {"date": norm_date, "detail": True})

        # Screener summary payload shapes:
        #   detect_ytd_high_low: {count, mode, new_high, new_low}
        #   detect_volume_surge: {count, multiplier, baseline_days}
        #   detect_price_limit (detail=True): {count, data: [...]}
        ytd_new_highs = ytd.get("new_high", 0) if not ytd.get("error") else 0
        ytd_new_lows = ytd.get("new_low", 0) if not ytd.get("error") else 0
        volume_surges = vsurge.get("count", 0) if not vsurge.get("error") else 0

        # Derive price-limit summary counts from the detailed data.
        plimit_data = plimit.get("data", []) if not plimit.get("error") else []
        lh_total = sum(1 for r in plimit_data if r.get("limit_high_touched"))
        lh_close = sum(1 for r in plimit_data if r.get("limit_high_close"))
        ll_total = sum(1 for r in plimit_data if r.get("limit_low_touched"))
        ll_close = sum(1 for r in plimit_data if r.get("limit_low_close"))

        # 5b. Notable stocks: 52w high/low + price limit universe → RSI14 scoring.
        #     Direct screener-cache lookup avoids the slow on-demand scan path.
        w52_payload = _get_52w_screener_data(cache, norm_date)
        notable_stocks = _compute_notable_stocks(
            w52_payload=w52_payload,
            plimit_full=plimit,
            code_closes=code_closes,
            code_volumes=code_volumes,
            name_map=name_map,
            prev_close_map=prev_close_map,
            today_close_map=today_close_map,
            n=n,
        )

        # 6. TOPIX change percentage — best effort, fail-soft to None.
        topix_change_pct = await _topix_change_pct_best_effort(_call_json, norm_date)

        # 7. Trend signals: distribution days + follow-through day, both fail-soft.
        trend_signals = await _trend_signals_best_effort(_call_json, norm_date)

        result: dict[str, Any] = {
            "date": today_date,
            "previous_date": prev_date,
            "sector_type": sector_type,
            "summary": {
                "advances": ad["advances"],
                "declines": ad["declines"],
                "unchanged": ad["unchanged"],
                "advance_decline_ratio_25d": adr_value,
                "topix_change_pct": topix_change_pct,
                "market_margin_ratio_median": market_margin_ratio_median,
                "market_margin_ratio_count": len(margin_ratios),
            },
            "sectors": {
                "top": sectors_top,
                "bottom": sectors_bottom,
            },
            "sector_short_ratios": sector_short_ratios,
            "top_movers_up": movers_up,
            "top_movers_down": movers_down,
            "top_turnover_value": turnover_items,
            "highlights": {
                "ytd_new_highs": ytd_new_highs,
                "ytd_new_lows": ytd_new_lows,
                "volume_surges": volume_surges,
                "limit_high_close": lh_close,
                "limit_high_touched": lh_total - lh_close,
                "limit_low_close": ll_close,
                "limit_low_touched": ll_total - ll_close,
                "notable_stocks": notable_stocks,
            },
            "trend_signals": trend_signals,
        }

        # 1h response cache so reloading "今日の相場" within the hour is instant.
        cache.put_response(cache_key, result, ttl_seconds=3600)
        return result


async def _topix_change_pct_best_effort(call_json, norm_date: str) -> float | None:
    """Compute TOPIX day-over-day percentage change.

    Pulls the last few sessions of indices_bars_daily_topix via the existing
    MCP tool (its own Tier 1 cache backs it). Returns ``None`` on any failure
    so the briefing surfaces a missing TOPIX field instead of an error.
    """
    # Pad calendar days generously to cover holidays.
    start = (datetime.strptime(norm_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    payload = await call_json(
        "get_indices_bars_daily_topix",
        {"date_from": start, "date_to": norm_date},
    )
    if payload.get("error"):
        return None
    rows = payload.get("data") or []
    if len(rows) < 2:
        return None
    # The endpoint returns rows sorted by Date asc; use the last two.
    try:
        prev_close = float(rows[-2].get("Close") or rows[-2].get("C"))
        today_close = float(rows[-1].get("Close") or rows[-1].get("C"))
    except (TypeError, ValueError):
        return None
    if prev_close == 0:
        return None
    return round((today_close - prev_close) / prev_close * 100, 4)


async def _trend_signals_best_effort(
    call_json, norm_date: str, rally_window: int = 30
) -> dict[str, Any]:
    """Assemble distribution-day count and follow-through day status for the briefing.

    Both sub-sections are fail-soft: a null value means insufficient TOPIX data
    or an API error; the briefing is never aborted.

    Distribution section: ``detect_distribution_days`` result summarised to the
    count, warning flag, and the most recent two entries.

    Follow-through section: auto-detects ``rally_start`` as the minimum TOPIX
    close over the last ``rally_window`` sessions (90 calendar days padded).
    When TOPIX has not recovered at least 1 % from that bottom
    (current_close < rally_start_close × 1.01) the section is set to
    ``{"status": "no_rally_attempt", "auto_rally_start": "..."}`` so the
    briefing conveys that the market is still in a downtrend or too early
    to confirm.
    """
    # -- Fetch TOPIX first so Tier 1 cache is populated before READ_ONLY_CACHE tools run.
    # detect_distribution_days and detect_follow_through_day read only from Tier 1 cache;
    # calling get_indices_bars_daily_topix first ensures the rows are available even when
    # the local cache.db is stale (e.g. Cloud Run startup copy is a few days old).
    # 90 calendar days covers the 45 sessions (20 σ warm-up + 25 window) needed for
    # distribution-day detection, so one fetch serves both sub-sections.
    topix_start = (datetime.strptime(norm_date, "%Y-%m-%d") - timedelta(days=90)).strftime(
        "%Y-%m-%d"
    )
    topix_payload = await call_json(
        "get_indices_bars_daily_topix", {"date_from": topix_start, "date_to": norm_date}
    )

    # -- Distribution days --------------------------------------------------
    dist_raw = await call_json("detect_distribution_days", {"date": norm_date})
    dist_section: dict[str, Any] | None = None
    if not dist_raw.get("error"):
        all_days = dist_raw.get("distribution_days") or []
        dist_section = {
            "distribution_count": dist_raw.get("distribution_count", 0),
            "warning": dist_raw.get("warning", False),
            "window_sessions": dist_raw.get("window_sessions", 25),
            "sigma_multiplier": dist_raw.get("sigma_multiplier", 2.0),
            "recent_distribution_days": all_days[-2:],
        }

    def _c(row: dict) -> float:
        v = row.get("Close") or row.get("C")
        try:
            return float(v) if v is not None else float("inf")
        except (TypeError, ValueError):
            return float("inf")

    ftd_section: dict[str, Any] | None = None
    if not topix_payload.get("error"):
        rows = sorted(
            topix_payload.get("data") or [],
            key=lambda r: str(r.get("Date") or ""),
        )
        if rows:
            recent = rows[-rally_window:] if len(rows) >= rally_window else rows
            min_row = min(recent, key=_c)
            auto_rally_start = str(min_row.get("Date") or "")[:10]
            rally_start_close = _c(min_row)
            current_close = _c(rows[-1])

            if (
                auto_rally_start
                and rally_start_close != float("inf")
                and current_close != float("inf")
            ):
                if current_close >= rally_start_close * 1.01:
                    ftd_raw = await call_json(
                        "detect_follow_through_day",
                        {"rally_start": auto_rally_start, "date": norm_date},
                    )
                    if not ftd_raw.get("error"):
                        ftd_section = {**ftd_raw, "auto_detected": True}
                    else:
                        ftd_section = {
                            "status": "unavailable",
                            "auto_rally_start": auto_rally_start,
                        }
                else:
                    ftd_section = {
                        "status": "no_rally_attempt",
                        "auto_rally_start": auto_rally_start,
                    }

    return {"distribution": dist_section, "follow_through": ftd_section}

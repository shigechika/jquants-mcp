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
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, make_cache_key
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_CACHE
from ..validators import collect_errors, display_code, make_validation_error_response, validate_date

logger = logging.getLogger(__name__)

_MAX_N = 100
_MAX_PERIOD = 90


def _validate_n(n: int) -> str | None:
    return "n must be between 1 and 100" if not (1 <= n <= _MAX_N) else None


def _validate_min_yield(min_yield: float) -> str | None:
    return "min_yield must be >= 0" if min_yield < 0 else None


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
        """Return the daily advance/decline summary for all listed equities.

        Counts how many stocks rose, fell, or were unchanged on *date*
        compared to the previous trading day, using split-adjusted closing
        prices (AdjC). Useful as a quick market breadth indicator.

        Args:
            date: Trading date in YYYY-MM-DD or YYYYMMDD format.

        Returns:
            dict with keys:
            - date: the requested trading date
            - previous_date: the comparison base date
            - advances: number of stocks that rose
            - declines: number of stocks that fell
            - unchanged: number of stocks with no price change
            - total: total stocks with data on both dates
            - advance_decline_ratio: advances / declines (null when declines == 0)
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
        """Return the advance/decline ratio (騰落レシオ) over the last *period* trading days.

        Computed as:
            ratio = sum(advances over period days) / sum(declines over period days) * 100

        Values above 120 are commonly interpreted as overbought; below 70 as oversold.
        Universe: all listed equities in the J-Quants cache (not limited to Nikkei 225).

        Args:
            date: End date in YYYY-MM-DD or YYYYMMDD format.
            period: Number of trading days to accumulate. Default: 25.

        Returns:
            dict with keys:
            - date: the end date
            - period: number of trading days used (may be less than requested near cache start)
            - ratio: advance/decline ratio × 100 (null when declines_sum == 0)
            - advances_sum: total advancing instances over the period
            - declines_sum: total declining instances over the period
            - note: universe description
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
        """Return top stocks by turnover value (売買代金ランキング) on a given date.

        Use for 売買代金ランキング, 売買代金, turnover, trading value.
        Distinct from ``get_top_volume`` which ranks by share count: turnover
        value (= price × volume) surfaces the names that moved the most money,
        so higher-priced names dominate the ranking instead of being crowded
        out by thinly priced low-priced shares.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (no API call)

        Args:
            date: Trading date in YYYY-MM-DD or YYYYMMDD format.
            n: Number of stocks to return (1–100). Default: 10.

        Returns:
            dict with keys:
            - date: the requested trading date
            - items: list of up to *n* dicts, each with:
                - code: stock code (4- or 5-digit display form)
                - name: company name (from equities_master) or null
                - turnover_value: trading value in yen
                - volume: number of shares traded
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

        return {
            "date": norm_date,
            "items": _compute_top_turnover_value(rows, name_map, n),
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_sector_performance(
        date: str,
        sector_type: str = "s33",
    ) -> dict[str, Any]:
        """Return sector-level average percentage change (業種別騰落率) on a date.

        Use for 業種別騰落率, セクター別パフォーマンス, sector performance,
        業種別ランキング.
        Groups all listed equities by TSE sector classification and reports the
        average daily change percentage per sector along with advance/decline
        counts. The default ``s33`` partitions the market into the 33 TSE
        sub-sectors; ``s17`` collapses to the 17-sector top-level grouping.

        Only stocks with a sector code populated in ``equities_master`` are
        aggregated; orphan rows (no master entry) and rows with an empty sector
        field are silently dropped from the bucket.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily + equities_master Tier 1 cache (no API call)

        Args:
            date: Trading date in YYYY-MM-DD or YYYYMMDD format.
            sector_type: ``"s33"`` (default, 33 sectors) or ``"s17"`` (17 sectors).

        Returns:
            dict with keys:
            - date: the requested trading date
            - previous_date: comparison base trading date
            - sector_type: ``"s33"`` or ``"s17"``
            - sectors: list of dicts (sorted by avg_change_pct desc), each with:
                - code: sector code
                - name: sector name
                - count: stocks with both today and prev close
                - advances: stocks that rose
                - declines: stocks that fell
                - unchanged: stocks with no price change
                - avg_change_pct: mean daily change in percent
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
        date: str | None = None,
    ) -> dict[str, Any]:
        """Return high dividend yield stock ranking (高配当利回りランキング).

        Use for 高配当, 配当利回り, dividend yield, 高利回り銘柄.
        Joins the latest valid annual dividend per share (DivAnn) from
        ``fins_summary`` with the split-adjusted closing price (AdjC) from
        ``equities_bars_daily`` to compute yield_pct = DivAnn / AdjC × 100.

        Uses the most recent disclosure with a positive DivAnn per code, so
        interim/quarterly reports where DivAnn is empty are skipped in favour
        of the most recent full-year disclosure.

        Note: ``DivAnn`` is the ordinary dividend only; special dividends are
        recorded in a separate ``FDivAnn`` field (not included here).

        [Supported plans] Free / Light / Standard / Premium
        [Source] fins_summary + equities_bars_daily Tier 1 cache (no API call)

        Args:
            n: Number of stocks to return (1–100). Default: 20.
            min_yield: Minimum dividend yield percentage to include (>= 0). Default: 3.0.
            date: Trading date in YYYY-MM-DD or YYYYMMDD format (default: latest
                  cached trading day). Automatically rounds back to the nearest
                  past trading day so weekend/holiday inputs work.

        Returns:
            dict with keys:
            - date: the resolved trading date used for closing prices
            - count: number of stocks returned
            - min_yield: the applied minimum yield filter
            - items: list of up to *n* dicts sorted by yield_pct desc, each with:
                - code: stock code (4- or 5-digit display form)
                - name: company name (from equities_master) or null
                - div_ann: annual dividend per share in yen
                - close: split-adjusted closing price (AdjC)
                - yield_pct: dividend yield in percent (DivAnn / AdjC × 100)
        """
        errors = collect_errors(_validate_n(n), _validate_min_yield(min_yield))
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
            {"date": norm_date, "n": n, "min_yield": min_yield},
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

        div_ann_map = cache.get_div_ann_map()
        name_map = cache.get_name_map()

        items: list[dict[str, Any]] = []
        for row in bars:
            code = str(row.get("Code") or "")
            adj_c = _as_float(row.get("AdjC"))
            div_ann = div_ann_map.get(code)
            if adj_c is None or adj_c <= 0 or div_ann is None:
                continue
            yield_pct = round(div_ann / adj_c * 100, 4)
            if yield_pct < min_yield:
                continue
            items.append(
                {
                    "code": display_code(code),
                    "name": name_map.get(code),
                    "div_ann": div_ann,
                    "close": adj_c,
                    "yield_pct": yield_pct,
                }
            )

        items.sort(key=lambda x: x["yield_pct"], reverse=True)
        result: dict[str, Any] = {
            "date": norm_date,
            "count": min(len(items), n),
            "min_yield": min_yield,
            "items": items[:n],
        }
        cache.put_response(cache_key, result, ttl_seconds=3600)
        return result

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_market_briefing(
        date: str,
        sector_type: str = "s17",
        n: int = 5,
    ) -> dict[str, Any]:
        """Daily market briefing — composite of advance/decline, sector ranking, top movers, top turnover, and screener highlights (相場ブリーフィング).

        Use for 相場ブリーフィング, 市場概況, マーケットブリーフィング, daily briefing,
        market briefing, market summary, 今日の相場.
        Single call to get a structured snapshot of "what happened in the
        Japanese market today". Fetches equities_bars_daily once for the full
        ADR window (26 sessions), then computes all sub-sections in memory —
        no redundant cache reads.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily + equities_master + indices_bars_daily_topix
                 (Tier 1 / Tier 2 cache; underlying tools may make API calls
                 if the cache is cold)

        Args:
            date: Trading date in YYYY-MM-DD or YYYYMMDD format.
            sector_type: ``"s33"`` or ``"s17"`` (default: ``"s17"`` — collapses
                to 17 top-level TSE sectors which is more readable on mobile).
            n: TopN size for movers / turnover sections (1–100, default: 5).

        Returns:
            dict with keys:
            - date / previous_date: the trading date and comparison base
            - summary: {advances, declines, unchanged, advance_decline_ratio_25d,
                topix_change_pct (null on TOPIX fetch failure)}
            - sectors: {top: [...], bottom: [...]} — top n and bottom n sectors
                by avg_change_pct
            - top_movers_up / top_movers_down: TopN by daily change_pct
            - top_turnover_value: TopN by trading value (yen)
            - highlights: {ytd_new_highs, ytd_new_lows, volume_surges,
                limit_high_close, limit_high_touched, limit_low_close,
                limit_low_touched}
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

        prev_close_map = _rows_to_close_map(prev_rows)
        name_map = cache.get_name_map()
        sector_map = cache.get_sector_map()

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

        # 4. Top movers and top turnover.
        movers_up = _compute_top_movers(today_rows, prev_close_map, name_map, "up", n)
        movers_down = _compute_top_movers(today_rows, prev_close_map, name_map, "down", n)
        turnover_items = _compute_top_turnover_value(today_rows, name_map, n)

        # 5. Screener summaries via call_tool (registered by screener.register).
        ytd = await _call_json("detect_ytd_high_low", {"date": norm_date})
        vsurge = await _call_json("detect_volume_surge", {"date": norm_date})
        plimit = await _call_json("detect_price_limit", {"date": norm_date})

        # Screener summary payload shapes (when detail=False, the default):
        #   detect_ytd_high_low: {count, mode, new_high, new_low}
        #   detect_volume_surge: {count, multiplier, baseline_days}
        #   detect_price_limit:  {count, limit_high_close, limit_high_touched, ...}
        ytd_new_highs = ytd.get("new_high", 0) if not ytd.get("error") else 0
        ytd_new_lows = ytd.get("new_low", 0) if not ytd.get("error") else 0
        volume_surges = vsurge.get("count", 0) if not vsurge.get("error") else 0

        # 6. TOPIX change percentage — best effort, fail-soft to None.
        topix_change_pct = await _topix_change_pct_best_effort(_call_json, norm_date)

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
            },
            "sectors": {
                "top": sectors_top,
                "bottom": sectors_bottom,
            },
            "top_movers_up": movers_up,
            "top_movers_down": movers_down,
            "top_turnover_value": turnover_items,
            "highlights": {
                "ytd_new_highs": ytd_new_highs,
                "ytd_new_lows": ytd_new_lows,
                "volume_surges": volume_surges,
                "limit_high_close": plimit.get("limit_high_close", 0),
                "limit_high_touched": plimit.get("limit_high_touched", 0),
                "limit_low_close": plimit.get("limit_low_close", 0),
                "limit_low_touched": plimit.get("limit_low_touched", 0),
            },
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

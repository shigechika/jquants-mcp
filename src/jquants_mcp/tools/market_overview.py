"""Market overview tools for jquants-mcp.

Cross-sectional tools that scan all listed equities to provide market-wide
summary statistics and rankings. All four tools operate on the
``equities_bars_daily`` Tier 1 cache and require no extra API calls.

Exposed tools:

- ``detect_price_change`` — advance/decline summary (値上がり/値下がり銘柄数)
- ``get_advance_decline_ratio`` — advance/decline ratio over N periods (騰落レシオ)
- ``get_top_movers`` — top gainers/losers by percentage price change
- ``get_top_volume`` — top stocks by trading volume
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_CACHE
from ..validators import collect_errors, make_validation_error_response, validate_date

logger = logging.getLogger(__name__)

_MAX_N = 100
_MAX_PERIOD = 90


def _validate_n(n: int) -> str | None:
    return "n must be between 1 and 100" if not (1 <= n <= _MAX_N) else None


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
    start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=count * 3)).isoformat()
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
        advances, declines, unchanged = _compute_advance_decline(today_rows, prev_close_map)
        total = advances + declines + unchanged
        ratio = round(advances / declines, 4) if declines > 0 else None

        return {
            "date": today_date,
            "previous_date": prev_date,
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "total": total,
            "advance_decline_ratio": ratio,
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

        # Group rows by date
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            d = str(row.get("Date") or "")[:10]
            by_date.setdefault(d, []).append(row)

        advances_sum = declines_sum = 0
        actual_period = 0

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
            "date": norm_date,
            "period": actual_period,
            "ratio": ratio,
            "advances_sum": advances_sum,
            "declines_sum": declines_sum,
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
                    "code": code,
                    "close": today_close,
                    "prev_close": prev_close,
                    "change_pct": change_pct,
                }
            )

        movers.sort(key=lambda x: x["change_pct"], reverse=(direction == "up"))

        return {
            "date": today_date,
            "previous_date": prev_date,
            "direction": direction,
            "items": movers[:n],
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

        items: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("Code") or "")
            volume = _as_float(row.get("Vo"))
            if volume is None:
                continue
            items.append(
                {
                    "code": code,
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

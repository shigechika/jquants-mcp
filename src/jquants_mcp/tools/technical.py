"""Technical indicator tool for jquants-mcp.

Exposes one tool:

- ``get_technical_indicators`` — compute SMA, Bollinger Bands, and RSI for a
  single stock over a date range. Uses split-adjusted close (AdjC) so values
  stay consistent across stock splits.

Indicators are computed in pure Python (no NumPy / pandas required) via
``cache.technical``. When the requested code/date is absent from the local
cache the tool falls back to the J-Quants API and stores the result for
subsequent calls — the same pattern used by ``compare_close_vs_vwap``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore
from ..cache.technical import compute_bb, compute_rsi, compute_sma
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_API
from ..validators import (
    collect_errors,
    display_code,
    make_validation_error_response,
    normalize_code,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Supported indicator names → warmup period (trading sessions)
_INDICATORS: dict[str, int] = {
    "sma5": 5,
    "sma25": 25,
    "sma75": 75,
    "bb20": 20,
    "rsi14": 14,
}

_DEFAULT_INDICATORS: list[str] = ["sma5", "sma25", "bb20", "rsi14"]

# Padding multiplier for calendar-day lookback so holiday clusters don't eat
# into the warmup window (same factor used in screener._calendar_window_start).
_LOOKBACK_MULTIPLIER = 2
_LOOKBACK_EXTRA_DAYS = 14


def _calendar_warmup_start(display_start: str, warmup_sessions: int) -> str:
    """Return a calendar date >= warmup_sessions trading sessions before display_start."""
    start = datetime.strptime(display_start, "%Y-%m-%d").date()
    calendar_days = warmup_sessions * _LOOKBACK_MULTIPLIER + _LOOKBACK_EXTRA_DAYS
    return (start - timedelta(days=calendar_days)).isoformat()


def _normalize_date(date: str) -> str:
    if "-" in date:
        return date
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"


def register(
    mcp: FastMCP,
    get_client: Any,
    get_cache: Any,
) -> None:
    """Register technical indicator tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_API)
    async def get_technical_indicators(
        code: str,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        indicators: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compute technical indicators (SMA, Bollinger Bands, RSI) for a single stock (テクニカル指標). All plans.

        Use for SMA・移動平均・ボリンジャーバンド・RSI queries on a specific stock.
        For charting use render_candlestick; for VWAP pressure use compare_close_vs_vwap.
        Supported: sma5, sma25, sma75, bb20 (→ bb20_mid/upper/lower ±2σ), rsi14. Null when not warmed up.

        [Supported plans] Free / Light / Standard / Premium (API fallback on cache miss)

        Args:
            code: Stock code (required).
            date: Single trading date (YYYYMMDD or YYYY-MM-DD). Overrides date_from/date_to.
            date_from: Range start inclusive (YYYYMMDD or YYYY-MM-DD).
            date_to: Range end inclusive (YYYYMMDD or YYYY-MM-DD).
            indicators: Indicator names list. Default ["sma5","sma25","bb20","rsi14"].
        """
        # --- Input validation ---
        errors = collect_errors(
            validate_code(code),
            validate_date(date),
            validate_date(date_from),
            validate_date(date_to),
        )
        if errors:
            return make_validation_error_response(errors)
        if not (date or date_from or date_to):
            return make_validation_error_response(
                ["Specify `date`, or at least one of `date_from` / `date_to`."]
            )

        ind_list = indicators if indicators is not None else list(_DEFAULT_INDICATORS)
        unknown = [i for i in ind_list if i not in _INDICATORS]
        if unknown:
            return make_validation_error_response(
                [f"Unknown indicator(s): {unknown}. Supported: {sorted(_INDICATORS)}."]
            )

        cache: CacheStore = get_cache()

        norm_code = normalize_code(code)
        if date:
            display_start = display_end = _normalize_date(date)
        else:
            display_start = _normalize_date(date_from) if date_from else None
            display_end = _normalize_date(date_to) if date_to else None

        # Guard against future dates
        check_date = display_end or display_start
        if check_date is not None:
            latest_date = cache.get_latest_equities_date()
            if latest_date is not None and check_date > latest_date:
                return {
                    "error": True,
                    "error_type": "CacheNotReady",
                    "message": (
                        f"Data for {check_date} not yet available. "
                        f"Latest cache date: {latest_date}."
                    ),
                    "hint": "Try again after 17:15 JST on trading days.",
                }

        # Extended lookback for indicator warmup
        max_period = max(_INDICATORS[i] for i in ind_list)
        warmup_start = _calendar_warmup_start(
            display_start or display_end or datetime.today().strftime("%Y-%m-%d"),
            max_period,
        )

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={"code": norm_code},
                date_from=warmup_start,
                date_to=display_end,
            )
            # API fallback when the code is absent from the local cache
            if not rows:
                client = await get_client()
                params: dict[str, Any] = {"code": code}
                if date:
                    # Fetch wider range to warm up indicators
                    params["from"] = warmup_start
                    params["to"] = display_end
                else:
                    params["from"] = warmup_start
                    if display_end:
                        params["to"] = display_end
                api_data = await client.get_all_pages("/equities/bars/daily", params)
                if api_data:
                    cache.put_rows(
                        "equities_bars_daily",
                        api_data,
                        key_columns=["Code", "Date"],
                        adj_factor_key="AdjFactor",
                    )
                    rows = cache.get_rows(
                        "equities_bars_daily",
                        key_filter={"code": norm_code},
                        date_from=warmup_start,
                        date_to=display_end,
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
            return {"count": 0, "data": []}

        # Sort by date and extract adjusted close prices
        rows.sort(key=lambda r: r.get("Date") or "")
        # Filter rows where close price is unavailable (e.g., non-trading days with empty strings)
        valid = []
        for r in rows:
            adj = _safe_float(r.get("AdjC"))
            close = adj if adj is not None else _safe_float(r.get("C"))
            valid.append((str(r.get("Date") or ""), close, r))
        valid = [(d, c, r) for d, c, r in valid if c is not None]
        if not valid:
            return {"count": 0, "data": []}
        dates, closes_raw, rows = zip(*valid)  # type: ignore[assignment]
        dates = list(dates)
        closes = list(closes_raw)
        rows = list(rows)

        # Compute requested indicators over the full extended series
        computed: dict[str, list[float | None]] = {}
        for ind in ind_list:
            period = _INDICATORS[ind]
            if ind.startswith("sma"):
                computed[ind] = compute_sma(closes, period)
            elif ind == "bb20":
                mid, upper, lower = compute_bb(closes, period)
                computed["bb20_mid"] = mid
                computed["bb20_upper"] = upper
                computed["bb20_lower"] = lower
            elif ind == "rsi14":
                computed["rsi14"] = compute_rsi(closes, period)

        # Expand bb20 key into three sub-keys for display window filtering
        display_keys = []
        for ind in ind_list:
            if ind == "bb20":
                display_keys += ["bb20_mid", "bb20_upper", "bb20_lower"]
            else:
                display_keys.append(ind)

        # Trim to the requested display window
        def _in_window(d: str) -> bool:
            if display_start and d < display_start:
                return False
            if display_end and d > display_end:
                return False
            return True

        out: list[dict[str, Any]] = []
        for i, (d, row) in enumerate(zip(dates, rows)):
            if not _in_window(d):
                continue
            entry: dict[str, Any] = {
                "Code": display_code(norm_code),
                "Date": d,
                "C": row.get("C"),
                "AdjC": row.get("AdjC"),
            }
            for key in display_keys:
                val = computed.get(key, [None] * len(dates))[i]
                entry[key] = round(val, 2) if val is not None else None
            out.append(entry)

        return {"count": len(out), "data": out}

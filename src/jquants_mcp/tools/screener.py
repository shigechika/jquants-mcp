"""Screener tools for jquants-mcp.

All four tools operate on the ``equities_bars_daily`` Tier 1 cache and
require no extra API calls. They are pure Python (stdlib only) — no
numpy/pandas.

Exposed tools:

- ``detect_price_limit`` — find stocks that touched the daily upper/lower
  price limit on a given date (``UL == 1`` / ``LL == 1`` in the J-Quants
  response). Optionally narrows to one code.
- ``compare_close_vs_vwap`` — compute the daily VWAP (``Va / Vo``) for a
  code and compare to the close.
- ``detect_yearly_high_low`` — check whether today's close is the
  highest/lowest close over the trailing 252 trading days (yearly
  high/low signal), using split-adjusted prices.
- ``detect_volume_surge`` — list stocks whose volume on a given date is
  at least ``multiplier`` times the trailing ``baseline_days`` average.

Plan note: the underlying table is available from the Free plan onwards,
so these tools impose no extra plan restriction beyond the normal
date-range gating applied by the cache layer.
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
from ..validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)


# Conventional yearly trading-day window (JPX ~247–252 sessions/year;
# use 252 to match common market convention).
_YEARLY_WINDOW_DAYS = 252

# Default baseline for volume-surge detection.
_DEFAULT_VOLUME_BASELINE = 20


def _normalize_date(date: str) -> str:
    """Normalize a date string to ``YYYY-MM-DD``.

    Accepts ``YYYYMMDD`` or ``YYYY-MM-DD`` (both already vetted by
    ``validate_date``). Cache rows key off the dashed form.
    """
    if "-" in date:
        return date
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"


def _normalize_code(code: str) -> str:
    """Pad a 4-digit code to the 5-digit (ordinary-share) form.

    Mirrors the convention in ``tools/equities.py``: J-Quants stores
    5-digit codes, and a 4-digit input refers to the ordinary share
    (5th digit = 0).
    """
    return code + "0" if len(code) == 4 else code


def _calendar_window_start(end_date: str, trading_days: int) -> str:
    """Return a calendar-date start ≥ ``trading_days`` trading days earlier.

    Trading days ≈ 252/year in Japan. Pad by 2× plus an extra two weeks so
    that long holiday clusters (Golden Week, year-end) do not eat into the
    requested window.
    """
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    calendar_days = trading_days * 2 + 14
    return (end - timedelta(days=calendar_days)).isoformat()


def register(
    mcp: FastMCP,
    get_client: Any,  # noqa: ARG001 — kept for signature parity with other tool modules
    get_cache: Any,
) -> None:
    """Register screener tools on the MCP server."""

    @mcp.tool()
    async def detect_price_limit(
        date: str,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Detect stocks that hit the daily upper/lower price limit (ストップ高/安).

        Uses the ``UL`` / ``LL`` flags in the cached daily bars.
        ``UL == 1`` means the upper limit was touched intraday at
        least once; ``LL == 1`` means the lower limit was touched.
        When ``C == H`` and ``UL == 1``, the close is effectively at
        the upper limit (ストップ高引け); analogous for lower.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (no API call)

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. If omitted, scans all
                stocks with a row on ``date``.
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)

        cache: CacheStore = get_cache()

        norm_date = _normalize_date(date)
        key_filter: dict[str, str] = {}
        if code:
            key_filter["code"] = _normalize_code(code)

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter=key_filter,
                date_from=norm_date,
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

        matches: list[dict[str, Any]] = []
        for row in rows:
            ul = _as_int(row.get("UL"))
            ll = _as_int(row.get("LL"))
            if ul != 1 and ll != 1 and code is None:
                # Cross-sectional: only include triggered rows.
                continue
            high = row.get("H")
            low = row.get("L")
            close = row.get("C")
            matches.append(
                {
                    "Code": str(row.get("Code") or ""),
                    "Date": row.get("Date") or norm_date,
                    "C": close,
                    "H": high,
                    "L": low,
                    "UL": ul,
                    "LL": ll,
                    "limit_high_touched": ul == 1,
                    "limit_low_touched": ll == 1,
                    "limit_high_close": ul == 1 and close is not None and close == high,
                    "limit_low_close": ll == 1 and close is not None and close == low,
                }
            )

        return {"count": len(matches), "data": matches}

    @mcp.tool()
    async def compare_close_vs_vwap(
        code: str,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Compare each session's close against the daily VWAP.

        Daily VWAP is ``Va / Vo`` (turnover value divided by volume).
        When volume is zero (suspended / non-trading day) the VWAP is
        reported as ``None``.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache

        Args:
            code: 4- or 5-digit code (required).
            date: Single trading date. If given, ``date_from``/``date_to``
                are ignored.
            date_from: Range start (inclusive) when ``date`` is omitted.
            date_to: Range end (inclusive) when ``date`` is omitted.
        """
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
                ["Specify `date`, or at least one of `date_from`/`date_to`."]
            )

        cache: CacheStore = get_cache()

        norm_code = _normalize_code(code)
        if date:
            start = end = _normalize_date(date)
        else:
            start = _normalize_date(date_from) if date_from else None
            end = _normalize_date(date_to) if date_to else None

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={"code": norm_code},
                date_from=start,
                date_to=end,
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            return format_api_error(e)

        out: list[dict[str, Any]] = []
        for row in rows:
            va = _as_float(row.get("Va"))
            vo = _as_float(row.get("Vo"))
            close = _as_float(row.get("C"))
            vwap: float | None = None
            vwap_diff_pct: float | None = None
            close_above_vwap: bool | None = None
            if va is not None and vo is not None and vo > 0:
                vwap = va / vo
                if close is not None and vwap > 0:
                    vwap_diff_pct = (close - vwap) / vwap * 100.0
                    close_above_vwap = close > vwap
            out.append(
                {
                    "Code": str(row.get("Code") or ""),
                    "Date": row.get("Date"),
                    "C": close,
                    "Va": va,
                    "Vo": vo,
                    "vwap": vwap,
                    "vwap_diff_pct": vwap_diff_pct,
                    "close_above_vwap": close_above_vwap,
                }
            )
        return {"count": len(out), "data": out}

    @mcp.tool()
    async def detect_yearly_high_low(
        date: str,
        code: str | None = None,
        window_days: int = _YEARLY_WINDOW_DAYS,
    ) -> dict[str, Any]:
        """Flag stocks making a new 52-week (≈252 session) high or low.

        Uses split-adjusted prices (``AdjH`` / ``AdjL`` / ``AdjC``) so
        corporate actions inside the window do not distort the signal.

        Today's bar is compared against the **prior** ``window_days - 1``
        sessions:

        - ``new_yearly_high`` — today's intraday high (``AdjH``) strictly
          exceeds the prior window's max high
        - ``new_yearly_high_close`` — today's close (``AdjC``) strictly
          exceeds the prior window's max high (a stronger signal than
          intraday-only)
        - ``new_yearly_low`` / ``new_yearly_low_close`` — analogous for
          lows.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache

        Performance: cross-sectional mode (``code=None``) with the default
        252-session window scans roughly 1M rows on a populated cache and
        can take 10–30 seconds. Specify ``code`` for sub-second response,
        or shrink ``window_days`` for cross-sectional scans.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. If omitted, checks every
                code with a row on ``date`` (cross-sectional scan).
            window_days: Trailing trading-day window (today included).
                Default 252.
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)
        if window_days < 2:
            return make_validation_error_response(["`window_days` must be >= 2."])

        cache: CacheStore = get_cache()

        norm_date = _normalize_date(date)
        start = _calendar_window_start(norm_date, window_days)
        key_filter: dict[str, str] = {}
        if code:
            key_filter["code"] = _normalize_code(code)

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter=key_filter,
                date_from=start,
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

        # Group rows per code, keep only the trailing `window_days`
        # sessions so that the "yearly" signal is computed over
        # exactly the intended window even if the cache spans more.
        by_code: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            c = str(row.get("Code") or "")
            if not c:
                continue
            by_code.setdefault(c, []).append(row)

        matches: list[dict[str, Any]] = []
        for c, sessions in by_code.items():
            sessions.sort(key=lambda r: r.get("Date") or "")
            window = sessions[-window_days:]
            if not window or window[-1].get("Date") != norm_date:
                # Last row in the window must be the requested date;
                # otherwise the stock didn't trade that day.
                continue
            if len(window) < 2:
                # Need at least one prior session for a comparison.
                continue
            today = window[-1]
            prior = window[:-1]

            today_high = _as_float(today.get("AdjH"))
            today_low = _as_float(today.get("AdjL"))
            today_close = _as_float(today.get("AdjC"))

            prior_highs = [_as_float(s.get("AdjH")) for s in prior]
            prior_highs = [h for h in prior_highs if h is not None]
            prior_lows = [_as_float(s.get("AdjL")) for s in prior]
            prior_lows = [low for low in prior_lows if low is not None]
            if not prior_highs or not prior_lows:
                continue

            prior_high = max(prior_highs)
            prior_low = min(prior_lows)

            new_high = today_high is not None and today_high > prior_high
            new_low = today_low is not None and today_low < prior_low
            new_high_close = today_close is not None and today_close > prior_high
            new_low_close = today_close is not None and today_close < prior_low

            if code is None and not (new_high or new_low or new_high_close or new_low_close):
                continue

            matches.append(
                {
                    "Code": c,
                    "Date": norm_date,
                    "window_days": len(window),
                    "AdjH": today_high,
                    "AdjL": today_low,
                    "AdjC": today_close,
                    "prior_window_high": prior_high,
                    "prior_window_low": prior_low,
                    "new_yearly_high": new_high,
                    "new_yearly_low": new_low,
                    "new_yearly_high_close": new_high_close,
                    "new_yearly_low_close": new_low_close,
                }
            )
        return {"count": len(matches), "data": matches}

    @mcp.tool()
    async def detect_volume_surge(
        date: str,
        multiplier: float = 2.0,
        baseline_days: int = _DEFAULT_VOLUME_BASELINE,
        code: str | None = None,
    ) -> dict[str, Any]:
        """List stocks whose volume on ``date`` exceeds a trailing average.

        For each stock with a row on ``date``:

          surge_ratio = Vo[date] / mean(Vo over prior `baseline_days`)

        Stocks with ``surge_ratio >= multiplier`` are returned. Codes
        whose baseline volume is zero (always suspended, new listing
        inside the window) are skipped.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            multiplier: Ratio threshold. Default 2.0.
            baseline_days: Trailing trading days used for the average.
                Default 20.
            code: Optional 4- or 5-digit code. If omitted, scans all
                stocks with a row on ``date``.
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)
        if multiplier <= 0:
            return make_validation_error_response(["`multiplier` must be > 0."])
        if baseline_days < 2:
            return make_validation_error_response(["`baseline_days` must be >= 2."])

        cache: CacheStore = get_cache()

        norm_date = _normalize_date(date)
        start = _calendar_window_start(norm_date, baseline_days + 1)
        key_filter: dict[str, str] = {}
        if code:
            key_filter["code"] = _normalize_code(code)

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter=key_filter,
                date_from=start,
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

        by_code: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            c = str(row.get("Code") or "")
            if not c:
                continue
            by_code.setdefault(c, []).append(row)

        matches: list[dict[str, Any]] = []
        for c, sessions in by_code.items():
            sessions.sort(key=lambda r: r.get("Date") or "")
            if sessions[-1].get("Date") != norm_date:
                continue
            today_vol = _as_float(sessions[-1].get("Vo"))
            baseline = [_as_float(s.get("Vo")) for s in sessions[:-1]]
            baseline = [v for v in baseline[-baseline_days:] if v is not None]
            if today_vol is None or not baseline:
                continue
            avg = sum(baseline) / len(baseline)
            if avg <= 0:
                continue
            ratio = today_vol / avg
            if ratio < multiplier:
                continue
            matches.append(
                {
                    "Code": c,
                    "Date": norm_date,
                    "Vo": today_vol,
                    "baseline_days_used": len(baseline),
                    "baseline_avg_vol": avg,
                    "surge_ratio": ratio,
                }
            )

        matches.sort(key=lambda m: m["surge_ratio"], reverse=True)
        return {
            "count": len(matches),
            "multiplier": multiplier,
            "baseline_days": baseline_days,
            "data": matches,
        }


# ----------------------------------------------------------------
# helpers
# ----------------------------------------------------------------


def _as_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

"""Screener tools for jquants-mcp.

All five tools operate on the ``equities_bars_daily`` Tier 1 cache and
require no extra API calls. They are pure Python (stdlib only) — no
numpy/pandas.

Exposed tools:

- ``detect_price_limit`` — find stocks that touched the daily upper/lower
  price limit on a given date (``UL == 1`` / ``LL == 1`` in the J-Quants
  response). Optionally narrows to one code.
- ``compare_close_vs_vwap`` — compute the daily VWAP (``Va / Vo``) for a
  code and compare to the close.
- ``detect_52w_high_low`` — check whether today's bar makes a new
  52-week (rolling 252-session) high or low using split-adjusted prices.
  Matches the convention used by Yahoo Finance, Bloomberg, TradingView.
- ``detect_ytd_high_low`` — check whether today's bar makes a new
  year-to-date (年初来) high or low using split-adjusted prices. Matches
  the convention used by Kabutan, JPX, and most JP retail-broker UIs.
- ``detect_volume_surge`` — list stocks whose volume on a given date is
  at least ``multiplier`` times the trailing ``baseline_days`` average.
- ``detect_52w_high_low_range`` / ``detect_ytd_high_low_range`` —
  multi-date variants of the high/low detectors. Use these when
  scanning more than one date to avoid parallel-dispatch timeouts.

The 52w/YTD detectors are also backed by the ``screener_results``
pre-compute cache (default-params cross-sectional outputs are
populated nightly by ``scripts/daily_fetch.py`` on m1.local), so
default-params calls return in sub-second.

Plan note: the underlying table is available from the Free plan onwards,
so these tools impose no extra plan restriction beyond the normal
date-range gating applied by the cache layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache import screener_compute
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
from ..validators import (
    collect_errors,
    make_validation_error_response,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)


# Conventional 52-week trading-day window (JPX ~247–252 sessions/year;
# use 252 to match common market convention).
_FIFTY_TWO_WEEK_SESSIONS = 252

# Default baseline for volume-surge detection.
_DEFAULT_VOLUME_BASELINE = 20

# Default minimum prior-session count required for a cross-sectional
# yearly-high/low signal to be reported. Suppresses noise from stocks
# that listed inside the window (a 5-day-old IPO will hit "new high"
# trivially every up-day, which clutters cross-sectional results).
# Per-code mode bypasses this filter — the caller asked explicitly.
_DEFAULT_MIN_PRIOR_SESSIONS = 60


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

    @mcp.tool(annotations=READ_ONLY_CACHE)
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

    @mcp.tool(annotations=READ_ONLY_CACHE)
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

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_52w_high_low(
        date: str,
        code: str | None = None,
        window_sessions: int = _FIFTY_TWO_WEEK_SESSIONS,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
    ) -> dict[str, Any]:
        """Flag stocks making a new 52-week rolling high or low.

        Convention used by Yahoo Finance, Bloomberg, TradingView, JPX
        official 52週高値/安値. Today's bar is compared against the
        prior ``window_sessions - 1`` sessions (today excluded).

        Returns four signals per row, all using split-adjusted prices
        (``AdjH`` / ``AdjL`` / ``AdjC``):

        - ``new_high``       — today's ``AdjH`` >= prior window max
        - ``new_high_close`` — today's ``AdjC`` >= prior window max
        - ``new_low``        — today's ``AdjL`` <= prior window min
        - ``new_low_close``  — today's ``AdjC`` <= prior window min

        ``>=`` (not strict ``>``) so days that tie the prior extreme
        also flag, matching standard market-data convention.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (cross-sectional
        results for default parameters are pre-computed nightly and
        served from ``screener_results``).

        Performance:
        - Cross-sectional default-params calls (``code=None``,
          ``window_sessions=252``, ``min_prior_sessions=60``) hit the
          pre-computed cache and return in sub-second.
        - Other shapes (custom params, ``code`` filter, dates outside
          the 52-week cache window) compute on-demand and may take
          10–30 seconds for cross-sectional scans.

        **For multi-date scans use ``detect_52w_high_low_range``** —
        firing this single-date tool N times in parallel multiplies
        server load and risks client-side tool-call timeouts.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. If omitted, scans every
                code with a row on ``date`` (cross-sectional).
            window_sessions: Trailing trading-day window including today.
                Default 252 (52 weeks).
            min_prior_sessions: Cross-sectional only — drop codes whose
                prior history inside the window has fewer than this many
                sessions (suppresses noise from recent IPOs). Default 60.
                Set to 1 to disable.
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)
        if window_sessions < 2:
            return make_validation_error_response(["`window_sessions` must be >= 2."])
        if min_prior_sessions < 1:
            return make_validation_error_response(["`min_prior_sessions` must be >= 1."])

        cache: CacheStore = get_cache()
        norm_date = _normalize_date(date)

        cached = _try_screener_cache_52w(
            cache,
            norm_date=norm_date,
            code=code,
            window_sessions=window_sessions,
            min_prior_sessions=min_prior_sessions,
        )
        if cached is not None:
            return cached

        start = _calendar_window_start(norm_date, window_sessions)
        return await _high_low_signals(
            cache=cache,
            norm_date=norm_date,
            range_start=start,
            code=code,
            window_sessions=window_sessions,
            min_prior_sessions=min_prior_sessions,
            mode_label="52w",
        )

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_ytd_high_low(
        date: str,
        code: str | None = None,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
    ) -> dict[str, Any]:
        """Flag stocks making a new year-to-date (年初来) high or low.

        Convention used by Kabutan (株探), Yahoo!ファイナンス JP, JPX
        official 年初来高値/安値, and most JP retail-broker UIs. Today's
        bar is compared against every prior session **since the first
        trading day of the same calendar year**.

        Returns four signals per row, all using split-adjusted prices
        (``AdjH`` / ``AdjL`` / ``AdjC``):

        - ``new_high``       — today's ``AdjH`` >= YTD prior max
        - ``new_high_close`` — today's ``AdjC`` >= YTD prior max
        - ``new_low``        — today's ``AdjL`` <= YTD prior min
        - ``new_low_close``  — today's ``AdjC`` <= YTD prior min

        Edge case: the very first trading day of the year has no prior
        YTD sessions and is skipped (the row would be empty by
        definition; "新年最初" is not a meaningful screening signal).

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (cross-sectional
        results for default parameters are pre-computed nightly and
        served from ``screener_results``).

        Performance:
        - Cross-sectional default-params calls (``code=None``,
          ``min_prior_sessions=60``) hit the pre-computed cache and
          return in sub-second.
        - Other shapes (custom params, ``code`` filter, dates outside
          the 52-week cache window) compute on-demand. Late in the
          year (~December) this approaches ~1M rows.

        **For multi-date scans use ``detect_ytd_high_low_range``** —
        firing this single-date tool N times in parallel multiplies
        server load and risks client-side tool-call timeouts.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. If omitted, scans every
                code with a row on ``date`` (cross-sectional).
            min_prior_sessions: Cross-sectional only — drop codes whose
                YTD history has fewer than this many prior sessions
                (suppresses noise from recent IPOs / January itself).
                Default 60. Set to 1 to disable.
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)
        if min_prior_sessions < 1:
            return make_validation_error_response(["`min_prior_sessions` must be >= 1."])

        cache: CacheStore = get_cache()
        norm_date = _normalize_date(date)

        cached = _try_screener_cache_ytd(
            cache,
            norm_date=norm_date,
            code=code,
            min_prior_sessions=min_prior_sessions,
        )
        if cached is not None:
            return cached

        year_start = norm_date[:4] + "-01-01"
        return await _high_low_signals(
            cache=cache,
            norm_date=norm_date,
            range_start=year_start,
            code=code,
            window_sessions=None,  # YTD has no fixed window cap
            min_prior_sessions=min_prior_sessions,
            mode_label="ytd",
        )

    @mcp.tool(annotations=READ_ONLY_CACHE)
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

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_52w_high_low_range(
        date_from: str,
        date_to: str,
        code: str | None = None,
        window_sessions: int = _FIFTY_TWO_WEEK_SESSIONS,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
    ) -> dict[str, Any]:
        """Multi-date variant of ``detect_52w_high_low``.

        Returns the union of single-date results across the inclusive
        ``[date_from, date_to]`` range. Use this instead of firing
        N parallel ``detect_52w_high_low`` calls — the single-date tool
        is CPU-heavy and parallel dispatch causes client-side timeouts.

        For default parameters and dates inside the past 52 weeks every
        date is a pre-computed cache hit and the entire range completes
        in sub-second. Out-of-cache dates fall through to on-demand
        computation; a 10-day out-of-cache range can take ~3 minutes.

        Args:
            date_from: Range start (inclusive, YYYYMMDD or YYYY-MM-DD).
            date_to: Range end (inclusive, YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code (post-filter).
            window_sessions: See ``detect_52w_high_low``.
            min_prior_sessions: See ``detect_52w_high_low``.
        """
        errors = collect_errors(
            validate_date(date_from),
            validate_date(date_to),
            validate_code(code),
        )
        if errors:
            return make_validation_error_response(errors)
        if window_sessions < 2:
            return make_validation_error_response(["`window_sessions` must be >= 2."])
        if min_prior_sessions < 1:
            return make_validation_error_response(["`min_prior_sessions` must be >= 1."])

        cache: CacheStore = get_cache()
        d_from = _normalize_date(date_from)
        d_to = _normalize_date(date_to)
        if d_from > d_to:
            return make_validation_error_response(["`date_from` must be <= `date_to`."])

        async def _compute_one(d: str) -> dict[str, Any]:
            start = _calendar_window_start(d, window_sessions)
            return await _high_low_signals(
                cache=cache,
                norm_date=d,
                range_start=start,
                code=code,
                window_sessions=window_sessions,
                min_prior_sessions=min_prior_sessions,
                mode_label="52w",
            )

        return await _high_low_range(
            cache=cache,
            tool_name=screener_compute.TOOL_DETECT_52W,
            params_hash_value=screener_compute.default_params_hash_52w(
                window_sessions=window_sessions,
                min_prior_sessions=min_prior_sessions,
            ),
            date_from=d_from,
            date_to=d_to,
            code=code,
            mode_label="52w",
            on_demand=_compute_one,
        )

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_ytd_high_low_range(
        date_from: str,
        date_to: str,
        code: str | None = None,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
    ) -> dict[str, Any]:
        """Multi-date variant of ``detect_ytd_high_low``.

        Returns the union of single-date results across the inclusive
        ``[date_from, date_to]`` range. Use this instead of firing
        N parallel ``detect_ytd_high_low`` calls — the single-date tool
        is CPU-heavy and parallel dispatch causes client-side timeouts.

        For default parameters and dates inside the past 52 weeks every
        date is a pre-computed cache hit and the entire range completes
        in sub-second. Out-of-cache dates fall through to on-demand
        computation.

        Args:
            date_from: Range start (inclusive, YYYYMMDD or YYYY-MM-DD).
            date_to: Range end (inclusive, YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code (post-filter).
            min_prior_sessions: See ``detect_ytd_high_low``.
        """
        errors = collect_errors(
            validate_date(date_from),
            validate_date(date_to),
            validate_code(code),
        )
        if errors:
            return make_validation_error_response(errors)
        if min_prior_sessions < 1:
            return make_validation_error_response(["`min_prior_sessions` must be >= 1."])

        cache: CacheStore = get_cache()
        d_from = _normalize_date(date_from)
        d_to = _normalize_date(date_to)
        if d_from > d_to:
            return make_validation_error_response(["`date_from` must be <= `date_to`."])

        async def _compute_one(d: str) -> dict[str, Any]:
            year_start = d[:4] + "-01-01"
            return await _high_low_signals(
                cache=cache,
                norm_date=d,
                range_start=year_start,
                code=code,
                window_sessions=None,
                min_prior_sessions=min_prior_sessions,
                mode_label="ytd",
            )

        return await _high_low_range(
            cache=cache,
            tool_name=screener_compute.TOOL_DETECT_YTD,
            params_hash_value=screener_compute.default_params_hash_ytd(
                min_prior_sessions=min_prior_sessions,
            ),
            date_from=d_from,
            date_to=d_to,
            code=code,
            mode_label="ytd",
            on_demand=_compute_one,
        )


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


async def _high_low_signals(
    *,
    cache: CacheStore,
    norm_date: str,
    range_start: str,
    code: str | None,
    window_sessions: int | None,
    min_prior_sessions: int,
    mode_label: str,
) -> dict[str, Any]:
    """Shared implementation for ``detect_52w_high_low`` / ``detect_ytd_high_low``.

    Loads bars between ``range_start`` and ``norm_date`` (inclusive) via
    the cache, then delegates to the pure-Python compute helper in
    ``cache.screener_compute`` so that the populate scripts share the
    exact same logic.
    """
    key_filter: dict[str, str] = {}
    if code:
        key_filter["code"] = _normalize_code(code)

    try:
        rows = cache.get_rows(
            "equities_bars_daily",
            key_filter=key_filter,
            date_from=range_start,
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

    return screener_compute.compute_high_low_signals(
        rows,
        norm_date=norm_date,
        code=code,
        window_sessions=window_sessions,
        min_prior_sessions=min_prior_sessions,
        mode_label=mode_label,
    )


def _filter_payload_by_code(
    payload: dict[str, Any],
    norm_code: str,
) -> dict[str, Any]:
    """Post-filter a cross-sectional cache payload to a single code."""
    data = [row for row in payload.get("data", []) if str(row.get("Code")) == norm_code]
    return {"count": len(data), "mode": payload.get("mode"), "data": data}


def _try_screener_cache_52w(
    cache: CacheStore,
    *,
    norm_date: str,
    code: str | None,
    window_sessions: int,
    min_prior_sessions: int,
) -> dict[str, Any] | None:
    """Look up a pre-computed 52w-high/low payload, post-filter on code.

    Returns ``None`` on miss. Hits are returned as a fresh dict so the
    caller can mutate without touching the cached object.
    """
    params_hash = screener_compute.default_params_hash_52w(
        window_sessions=window_sessions,
        min_prior_sessions=min_prior_sessions,
    )
    payload = cache.screener_result_get(screener_compute.TOOL_DETECT_52W, params_hash, norm_date)
    if payload is None:
        return None
    if code:
        return _filter_payload_by_code(payload, _normalize_code(code))
    # Defensive copy so callers can mutate the response.
    return {
        "count": payload.get("count", 0),
        "mode": payload.get("mode", "52w"),
        "data": list(payload.get("data", [])),
    }


def _try_screener_cache_ytd(
    cache: CacheStore,
    *,
    norm_date: str,
    code: str | None,
    min_prior_sessions: int,
) -> dict[str, Any] | None:
    """Look up a pre-computed YTD-high/low payload, post-filter on code."""
    params_hash = screener_compute.default_params_hash_ytd(
        min_prior_sessions=min_prior_sessions,
    )
    payload = cache.screener_result_get(screener_compute.TOOL_DETECT_YTD, params_hash, norm_date)
    if payload is None:
        return None
    if code:
        return _filter_payload_by_code(payload, _normalize_code(code))
    return {
        "count": payload.get("count", 0),
        "mode": payload.get("mode", "ytd"),
        "data": list(payload.get("data", [])),
    }


async def _high_low_range(
    *,
    cache: CacheStore,
    tool_name: str,
    params_hash_value: str,
    date_from: str,
    date_to: str,
    code: str | None,
    mode_label: str,
    on_demand,
) -> dict[str, Any]:
    """Range scan: bulk cache lookup + on-demand fallback per missing day.

    Trading-day enumeration uses ``equities_bars_daily`` (the dense
    source of truth). For each trading day:
      * cache hit → use payload directly
      * cache miss → call ``on_demand(date)`` (single-date compute)

    Code filtering is applied uniformly after lookup/compute.
    """
    cached_by_date = cache.screener_result_get_range(
        tool_name, params_hash_value, date_from, date_to
    )

    # Determine the trading days in range. Prefer the screener cache
    # itself when it covers the whole range; otherwise discover trading
    # days from the bar table to handle out-of-cache dates.
    trading_days = sorted(cached_by_date.keys())
    if not trading_days or trading_days[0] > date_from or trading_days[-1] < date_to:
        trading_days = _distinct_trading_days(cache, date_from, date_to) or trading_days

    norm_code = _normalize_code(code) if code else None
    aggregated: list[dict[str, Any]] = []
    for d in sorted(set(trading_days)):
        if d in cached_by_date:
            payload = cached_by_date[d]
            rows = payload.get("data", [])
        else:
            payload = await on_demand(d)
            if payload.get("error"):
                # Validation/API error mid-range: surface immediately.
                return payload
            rows = payload.get("data", [])
        if norm_code is not None:
            rows = [row for row in rows if str(row.get("Code")) == norm_code]
        aggregated.extend(rows)

    return {
        "count": len(aggregated),
        "mode": mode_label,
        "date_from": date_from,
        "date_to": date_to,
        "data": aggregated,
    }


def _distinct_trading_days(cache: CacheStore, date_from: str, date_to: str) -> list[str]:
    """Distinct ``equities_bars_daily`` dates in [date_from, date_to].

    Goes through ``CacheStore._ensure_connection`` to respect the
    cache's not-ready / reload state, but executes a direct
    ``SELECT DISTINCT`` because the public ``get_cached_dates`` API
    requires a key filter.
    """
    conn = cache._ensure_connection()  # noqa: SLF001 — internal helper, contract-stable
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT date FROM equities_bars_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (date_from, date_to),
    ).fetchall()
    return [str(r[0])[:10] for r in rows]

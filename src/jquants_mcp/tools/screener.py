"""Screener tools for jquants-mcp.

Most tools operate on the ``equities_bars_daily`` Tier 1 cache.
Two per-code tools fall back to the J-Quants API when the requested
code/date is absent from the local cache:

- ``compare_close_vs_vwap`` — always per-code; fetches the requested
  date range from the API when not cached, then stores the result.
- ``detect_volume_surge`` — when a specific ``code`` is given; fetches
  the baseline window from the API when not cached.

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
- ``detect_distribution_days`` — count TOPIX distribution days (IBD —
  Investor's Business Daily — method) within a rolling 25-session window
  using a 20-session rolling σ threshold.
  Uses ``indices_bars_daily_topix`` for price and ``equities_bars_daily``
  Va aggregate for volume.
- ``detect_follow_through_day`` — check whether a TOPIX follow-through day
  (day 4+ of a rally attempt, z ≥ +σ threshold, volume increase) has
  occurred from a specified ``rally_start`` date.

The 52w/YTD detectors are also backed by the ``screener_results``
pre-compute cache (default-params cross-sectional outputs are
populated nightly by ``scripts/daily_fetch.py`` on the self-hosted publisher), so
default-params calls return in sub-second.

Plan note: equity screener tools are available from the Free plan onwards.
The distribution-day / follow-through-day tools use TOPIX data, which is
subject to the same plan-based date gating as other cache tables.
"""

from __future__ import annotations

import bisect
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache import screener_compute
from ..cache.screener_compute import SCREENER_CACHE_LOOKBACK_WEEKS
from ..cache.store import CacheStore
from ..exceptions import (
    APIError,
    DecryptionError,
    InvalidAPIKeyError,
    UserNotAllowedError,
    UserNotConfiguredError,
    format_api_error,
)
from ..tool_annotations import READ_ONLY_API, READ_ONLY_CACHE
from ..validators import (
    collect_errors,
    display_code,
    make_validation_error_response,
    normalize_code,
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

# Maximum lookback (in weeks) supported by the high/low detectors.
# Sourced from ``cache.screener_compute`` so the writer-side prune
# retention and the reader-side rejection cutoff cannot drift apart.
_CACHE_LOOKBACK_WEEKS = SCREENER_CACHE_LOOKBACK_WEEKS

# Distribution day / follow-through day (IBD method adapted for TOPIX).
# Rolling σ window matches BB20; session window and min count follow IBD convention.
_DIST_DAY_SIGMA_WINDOW = 20
_DIST_DAY_SESSION_WINDOW = 25
_DIST_DAY_MIN_COUNT = 4
_DIST_DAY_DEFAULT_SIGMA_MULT = 2.0


def _normalize_date(date: str) -> str:
    """Normalize a date string to ``YYYY-MM-DD``.

    Accepts ``YYYYMMDD`` or ``YYYY-MM-DD`` (both already vetted by
    ``validate_date``). Cache rows key off the dashed form.
    """
    if "-" in date:
        return date
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"


def _cache_window_cutoff() -> str:
    """ISO date for the oldest date covered by the screener cache.

    Computed at call time from the host's local date. The self-hosted
    publisher runs in JST so the cutoff tracks Asia/Tokyo trading days;
    Cloud Run runs in UTC so on a JST-evening request its cutoff is up
    to ~9 hours behind. The 1-day worst-case drift is acceptable for a 52-week
    rolling window and avoids depending on system tz configuration.
    """
    return (date.today() - timedelta(weeks=_CACHE_LOOKBACK_WEEKS)).isoformat()


def _cache_not_ready_error(requested_date: str, latest_cache_date: str | None) -> dict[str, Any]:
    """Error response when the cache does not yet have data for the requested date.

    Returned when ``requested_date > latest_cache_date`` so callers get an
    actionable message instead of silently empty results.
    """
    latest = latest_cache_date or "unknown"
    return {
        "error": True,
        "error_type": "CacheNotReady",
        "message": (f"Data for {requested_date} not yet available. Latest cache date: {latest}."),
        "hint": "Try again after 17:15 JST on trading days.",
    }


def _out_of_cache_error(norm_date: str) -> dict[str, Any]:
    """Error response for a date older than the supported cache window.

    Cross-sectional on-demand computation for these dates can take
    several minutes on Cloud Run's 1-vCPU runtime — long enough to
    exceed client-side tool-call timeouts (Desktop verification of
    PR #161 hit a 3-minute timeout on a 53-week-old date). Refusing
    immediately with a clear error keeps the user out of the
    timeout-or-wait limbo.
    """
    cutoff = _cache_window_cutoff()
    return {
        "error": True,
        "error_type": "OutOfCacheRange",
        "message": (
            f"{norm_date} is outside the {_CACHE_LOOKBACK_WEEKS}-week cache "
            f"window. On-demand computation for older dates is not supported."
        ),
        "cache_from": cutoff,
        "hint": f"Use a date >= {cutoff}.",
    }


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
    get_client: Any,
    get_cache: Any,
) -> None:
    """Register screener tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_price_limit(
        date: str,
        code: str | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Find stocks that hit the daily price limit (ストップ高/安) on a given trading day.

        Use this when the user asks about ストップ高、ストップ安、値幅制限 (daily price
        bands), or stocks that couldn't trade freely because of limit moves. Call with
        ``code=None`` to get a cross-sectional list of all limit-hit stocks on a date.

        ``UL == 1`` means the upper limit was touched intraday at least once;
        ``LL == 1`` means the lower limit was touched. When ``C == H`` and
        ``UL == 1``, the close is at the upper limit (ストップ高引け); analogous for
        lower.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (no API call)
        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Requests for
        today before that time return empty results.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. If omitted, scans all
                stocks with a row on ``date``.
            detail: If True, include the full per-stock ``data`` array.
                Default False returns summary counts only: ``count``,
                ``limit_high`` / ``limit_low`` (totals), plus close/touched
                breakdowns (``limit_high_close``, ``limit_high_touched``,
                ``limit_low_close``, ``limit_low_touched``).
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)

        cache: CacheStore = get_cache()

        norm_date = _normalize_date(date)
        latest_date = cache.get_latest_equities_date()
        if latest_date is not None and norm_date > latest_date:
            return _cache_not_ready_error(norm_date, latest_date)

        key_filter: dict[str, str] = {}
        if code:
            key_filter["code"] = normalize_code(code)

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

        name_map = cache.get_name_map() if detail else {}
        matches: list[dict[str, Any]] = []
        for row in rows:
            ul = _as_int(row.get("UL"))
            ll = _as_int(row.get("LL"))
            if ul != 1 and ll != 1 and code is None:
                # Cross-sectional: only include triggered rows.
                continue
            raw_code = str(row.get("Code") or "")
            high = row.get("H")
            low = row.get("L")
            close = row.get("C")
            matches.append(
                {
                    "Code": display_code(raw_code),
                    "name": name_map.get(raw_code),
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

        full = {"count": len(matches), "data": matches}
        return full if detail else _summarise_price_limit(full)

    @mcp.tool(annotations=READ_ONLY_API)
    async def compare_close_vs_vwap(
        code: str,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Compare a single stock's close against its daily VWAP to gauge buy/sell pressure (買い圧力・売り圧力).

        Use this when the user asks how a specific stock's close compares to its VWAP,
        or whether 買い圧力 or 売り圧力 dominated a session. A close above VWAP suggests
        buying was strong into the close; below suggests selling. Requires ``code`` —
        this tool is per-stock only, not a cross-sectional screener.

        Returns ``close_above_vwap`` (bool) and raw ``vwap`` / ``C`` values per session;
        does not compute a deviation percentage.

        Daily VWAP is ``Va / Vo`` (turnover value divided by volume).
        When volume is zero (suspended / non-trading day) the VWAP is
        reported as ``None``.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache
        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Requests for
        today before that time return empty results.

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

        norm_code = normalize_code(code)
        if date:
            start = end = _normalize_date(date)
        else:
            start = _normalize_date(date_from) if date_from else None
            end = _normalize_date(date_to) if date_to else None

        if end is not None:
            latest_date = cache.get_latest_equities_date()
            if latest_date is not None and end > latest_date:
                return _cache_not_ready_error(end, latest_date)

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={"code": norm_code},
                date_from=start,
                date_to=end,
            )
            # API fallback: Cloud Run cache may lag by a few trading days
            # after the last GCS export. Fetch and store for this code when absent.
            if not rows:
                client = await get_client()
                params: dict[str, Any] = {"code": code}
                if date:
                    params["date"] = date
                else:
                    if start:
                        params["from"] = start
                    if end:
                        params["to"] = end
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
                    "Code": display_code(str(row.get("Code") or "")),
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
        detail: bool = False,
    ) -> dict[str, Any]:
        """Identify stocks making a new 52-week rolling high or low (52週高値/安値 ブレイク).

        Use this when the user asks about 52週高値、52週安値、年間高値、年間安値, stocks
        breaking to new one-year highs or lows, or momentum screeners based on 52-week
        price extremes. For multi-date queries call ``detect_52w_high_low_range`` instead.

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

        Each row also includes conviction context fields:

        - ``AdjO``                — split-adjusted open (``AdjO < AdjC`` = bullish candle)
        - ``close_vs_vwap``       — ``"above"`` / ``"below"`` (raw close vs ``Va/Vo``)
        - ``volume_ratio``        — today ``Vo`` / 20-session prior average (≥1.5 = surge)
        - ``volume_ratio_sessions`` — actual sessions used in the baseline (< 20 near year-start)

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (cross-sectional
        results for default parameters are pre-computed nightly and
        served from ``screener_results``).
        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Requests for
        today before that time return empty results.

        **Lookback limit:** ``date`` must fall within the past 52 weeks.
        Older dates are rejected with ``error_type=OutOfCacheRange``;
        their on-demand cross-sectional compute can take minutes on the
        Cloud Run 1-vCPU runtime and exceed client tool-call timeouts.

        Performance (within the 52-week window):
        - Cross-sectional default-params calls (``code=None``,
          ``window_sessions=252``, ``min_prior_sessions=60``) hit the
          pre-computed cache and return in sub-second.
        - Custom params or ``code`` filter compute on-demand. Single-code
          queries are fast (~10 ms); custom-param cross-sectional scans
          take 10–30 seconds.

        **For multi-date scans use ``detect_52w_high_low_range``** —
        firing this single-date tool N times in parallel multiplies
        server load and risks client-side tool-call timeouts.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD). Must be within
                the past 52 weeks.
            code: Optional 4- or 5-digit code. If omitted, scans every
                code with a row on ``date`` (cross-sectional).
            window_sessions: Trailing trading-day window including today.
                Default 252 (52 weeks).
            min_prior_sessions: Cross-sectional only — drop codes whose
                prior history inside the window has fewer than this many
                sessions (suppresses noise from recent IPOs). Default 60.
                Set to 1 to disable.
            detail: If True, include the full per-stock ``data`` array.
                Default False returns summary counts only (``count``,
                ``mode``, ``new_high``, ``new_low``).
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

        if norm_date < _cache_window_cutoff():
            return _out_of_cache_error(norm_date)

        latest_date = cache.get_latest_equities_date()
        if latest_date is not None and norm_date > latest_date:
            return _cache_not_ready_error(norm_date, latest_date)

        # The pre-computed cache only stores cross-sectional payloads,
        # which were built with min_prior_sessions=60 active. An explicit
        # ``code`` argument bypasses that filter on the on-demand path
        # (e.g. so newly-listed stocks still return their signal), so
        # serving code-specific calls from the cross-sectional payload
        # would silently drop IPO codes. Skip the cache when code is set.
        name_map = cache.get_name_map()
        if code is None:
            cached = _try_screener_cache_52w(
                cache,
                norm_date=norm_date,
                window_sessions=window_sessions,
                min_prior_sessions=min_prior_sessions,
                name_map=name_map,
            )
            if cached is not None:
                return cached if detail else _summarise_high_low(cached)

        start = _calendar_window_start(norm_date, window_sessions)
        result = await _high_low_signals(
            cache=cache,
            norm_date=norm_date,
            range_start=start,
            code=code,
            window_sessions=window_sessions,
            min_prior_sessions=min_prior_sessions,
            mode_label="52w",
            name_map=name_map,
        )
        return result if detail else _summarise_high_low(result)

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_ytd_high_low(
        date: str,
        code: str | None = None,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Identify stocks making a new year-to-date high or low (年初来高値/安値 更新).

        Use this when the user asks about 年初来高値、年初来安値, YTD price extremes, or
        stocks setting new records since the start of the current calendar year.
        For multi-date queries call ``detect_ytd_high_low_range`` instead.

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

        Each row also includes conviction context fields:

        - ``AdjO``                — split-adjusted open (``AdjO < AdjC`` = bullish candle)
        - ``close_vs_vwap``       — ``"above"`` / ``"below"`` (raw close vs ``Va/Vo``)
        - ``volume_ratio``        — today ``Vo`` / 20-session prior average (≥1.5 = surge)
        - ``volume_ratio_sessions`` — actual sessions used in the baseline (< 20 in January)

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (cross-sectional
        results for default parameters are pre-computed nightly and
        served from ``screener_results``).
        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Requests for
        today before that time return empty results.

        **Lookback limit:** ``date`` must fall within the past 52 weeks.
        Older dates are rejected with ``error_type=OutOfCacheRange`` to
        avoid the slow cross-sectional on-demand path that can exceed
        client tool-call timeouts.

        Performance (within the 52-week window):
        - Cross-sectional default-params calls (``code=None``,
          ``min_prior_sessions=60``) hit the pre-computed cache and
          return in sub-second.
        - Custom params or ``code`` filter compute on-demand. Single-code
          queries are fast; custom-param cross-sectional scans approach
          ~1M rows late in the year.

        **For multi-date scans use ``detect_ytd_high_low_range``** —
        firing this single-date tool N times in parallel multiplies
        server load and risks client-side tool-call timeouts.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD). Must be within
                the past 52 weeks.
            code: Optional 4- or 5-digit code. If omitted, scans every
                code with a row on ``date`` (cross-sectional).
            min_prior_sessions: Cross-sectional only — drop codes whose
                YTD history has fewer than this many prior sessions
                (suppresses noise from recent IPOs / January itself).
                Default 60. Set to 1 to disable.
            detail: If True, include the full per-stock ``data`` array.
                Default False returns summary counts only (``count``,
                ``mode``, ``new_high``, ``new_low``).
        """
        errors = collect_errors(validate_date(date), validate_code(code))
        if errors:
            return make_validation_error_response(errors)
        if min_prior_sessions < 1:
            return make_validation_error_response(["`min_prior_sessions` must be >= 1."])

        cache: CacheStore = get_cache()
        norm_date = _normalize_date(date)

        if norm_date < _cache_window_cutoff():
            return _out_of_cache_error(norm_date)

        latest_date = cache.get_latest_equities_date()
        if latest_date is not None and norm_date > latest_date:
            return _cache_not_ready_error(norm_date, latest_date)

        # See note in detect_52w_high_low: cache payload omits codes that
        # the cross-sectional min_prior_sessions filter dropped, but the
        # on-demand path bypasses that filter when ``code`` is set. Skip
        # the cache for explicit-code calls to avoid losing IPO rows.
        name_map = cache.get_name_map()
        if code is None:
            cached = _try_screener_cache_ytd(
                cache,
                norm_date=norm_date,
                min_prior_sessions=min_prior_sessions,
                name_map=name_map,
            )
            if cached is not None:
                return cached if detail else _summarise_high_low(cached)

        year_start = norm_date[:4] + "-01-01"
        result = await _high_low_signals(
            cache=cache,
            norm_date=norm_date,
            range_start=year_start,
            code=code,
            window_sessions=None,  # YTD has no fixed window cap
            min_prior_sessions=min_prior_sessions,
            mode_label="ytd",
            name_map=name_map,
        )
        return result if detail else _summarise_high_low(result)

    @mcp.tool(annotations=READ_ONLY_API)
    async def detect_volume_surge(
        date: str,
        multiplier: float = 2.0,
        baseline_days: int = _DEFAULT_VOLUME_BASELINE,
        code: str | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Identify stocks with abnormally high trading volume (出来高急増) on a given day.

        Use this when the user asks about 出来高急増、出来高異常、売買活況、出来高ランキング、
        活発に売買された銘柄、取引量が増えた銘柄、unusual trading activity, volume spikes,
        or volume-driven momentum screeners. Stocks are flagged when today's volume
        exceeds the trailing ``baseline_days``-day average by at least ``multiplier`` times.

        For 52-week price extremes use ``detect_52w_high_low``; for YTD highs/lows use
        ``detect_ytd_high_low``; for price-limit events (ストップ高/安) use
        ``detect_price_limit``; for VWAP buy/sell pressure use ``compare_close_vs_vwap``.

        For each stock with a row on ``date``:

          surge_ratio = Vo[date] / mean(Vo over prior `baseline_days`)

        Stocks with ``surge_ratio >= multiplier`` are returned. Codes
        whose baseline volume is zero (always suspended, new listing
        inside the window) are skipped.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache
        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Requests for
        today before that time return empty results.

        Args:
            date: Trading date (YYYYMMDD or YYYY-MM-DD).
            multiplier: Ratio threshold. Default 2.0.
            baseline_days: Trailing trading days used for the average.
                Default 20.
            code: Optional 4- or 5-digit code. If omitted, scans all
                stocks with a row on ``date``.
            detail: If True, include the full per-stock ``data`` array.
                Default False returns summary counts only (``count``,
                ``multiplier``, ``baseline_days``).
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
        latest_date = cache.get_latest_equities_date()
        if latest_date is not None and norm_date > latest_date:
            return _cache_not_ready_error(norm_date, latest_date)

        start = _calendar_window_start(norm_date, baseline_days + 1)
        key_filter: dict[str, str] = {}
        if code:
            key_filter["code"] = normalize_code(code)

        try:
            rows = cache.get_rows(
                "equities_bars_daily",
                key_filter=key_filter,
                date_from=start,
                date_to=norm_date,
            )
            # API fallback for per-code queries when the code is absent from cache
            if not rows and code:
                client = await get_client()
                api_data = await client.get_all_pages(
                    "/equities/bars/daily",
                    {"code": code, "from": start, "to": norm_date},
                )
                if api_data:
                    cache.put_rows(
                        "equities_bars_daily",
                        api_data,
                        key_columns=["Code", "Date"],
                        adj_factor_key="AdjFactor",
                    )
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

        name_map = cache.get_name_map() if detail else {}
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
                    "Code": display_code(c),
                    "name": name_map.get(c),
                    "Date": norm_date,
                    "Vo": today_vol,
                    "baseline_days_used": len(baseline),
                    "baseline_avg_vol": avg,
                    "surge_ratio": ratio,
                }
            )

        matches.sort(key=lambda m: m["surge_ratio"], reverse=True)
        full = {
            "count": len(matches),
            "multiplier": multiplier,
            "baseline_days": baseline_days,
            "data": matches,
        }
        return full if detail else _summarise_volume_surge(full)

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_52w_high_low_range(
        date_from: str,
        date_to: str,
        code: str | None = None,
        window_sessions: int = _FIFTY_TWO_WEEK_SESSIONS,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Scan 52-week high/low signals (52週高値/安値 ブレイク) across a date range.

        Use this — not repeated ``detect_52w_high_low`` calls — when the user asks for
        52-week high/low signals over multiple days, a week, or a month.

        Returns the union of single-date results across the inclusive
        ``[date_from, date_to]`` range. The single-date tool is CPU-heavy and
        parallel dispatch causes client-side timeouts.

        **Lookback limit:** ``date_from`` must fall within the past 52
        weeks. Older ranges are rejected with
        ``error_type=OutOfCacheRange``.

        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Including today
        in the range before that time returns empty results for today's date.

        For default parameters every trading day in range is a
        pre-computed cache hit and the full window completes in
        sub-second. Custom params or ``code`` filter compute on-demand
        per date.

        **Issue one range call per query.** Splitting the range into
        several non-overlapping ``detect_52w_high_low_range`` calls
        fired in parallel re-introduces the dispatch problem this tool
        exists to prevent — pass the full window in a single invocation
        instead.

        Args:
            date_from: Range start (inclusive, YYYYMMDD or YYYY-MM-DD).
                Must be within the past 52 weeks.
            date_to: Range end (inclusive, YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. When set, the
                pre-computed cache is bypassed (it omits codes the
                cross-sectional IPO filter dropped) and every date is
                computed on-demand.
            window_sessions: See ``detect_52w_high_low``.
            min_prior_sessions: See ``detect_52w_high_low``.
            detail: If True, include the full per-stock ``data`` array.
                Default False returns summary counts only (``count``,
                ``mode``, ``date_from``, ``date_to``, ``new_high``,
                ``new_low``).
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
        if d_from < _cache_window_cutoff():
            return _out_of_cache_error(d_from)

        latest_date = cache.get_latest_equities_date()
        if latest_date is not None and d_to > latest_date:
            return _cache_not_ready_error(d_to, latest_date)

        name_map = cache.get_name_map()

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
                name_map=name_map,
            )

        result = await _high_low_range(
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
            name_map=name_map,
            on_demand=_compute_one,
        )
        return result if detail else _summarise_high_low(result)

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_ytd_high_low_range(
        date_from: str,
        date_to: str,
        code: str | None = None,
        min_prior_sessions: int = _DEFAULT_MIN_PRIOR_SESSIONS,
        detail: bool = False,
    ) -> dict[str, Any]:
        """Scan year-to-date high/low signals (年初来高値/安値 更新) across a date range.

        Use this — not repeated ``detect_ytd_high_low`` calls — when the user asks for
        YTD high/low signals over multiple days, a week, or a month.

        Returns the union of single-date results across the inclusive
        ``[date_from, date_to]`` range. The single-date tool is CPU-heavy and
        parallel dispatch causes client-side timeouts.

        **Lookback limit:** ``date_from`` must fall within the past 52
        weeks. Older ranges are rejected with
        ``error_type=OutOfCacheRange``.

        **Data availability:** Today's data is not available until the daily
        cache update completes (~17:15 JST on trading days). Including today
        in the range before that time returns empty results for today's date.

        For default parameters every trading day in range is a
        pre-computed cache hit and the full window completes in
        sub-second. Custom params or ``code`` filter compute on-demand
        per date.

        **Issue one range call per query.** Splitting the range into
        several non-overlapping ``detect_ytd_high_low_range`` calls
        fired in parallel re-introduces the dispatch problem this tool
        exists to prevent — pass the full window in a single invocation
        instead.

        Args:
            date_from: Range start (inclusive, YYYYMMDD or YYYY-MM-DD).
                Must be within the past 52 weeks.
            date_to: Range end (inclusive, YYYYMMDD or YYYY-MM-DD).
            code: Optional 4- or 5-digit code. When set, the
                pre-computed cache is bypassed (it omits codes the
                cross-sectional IPO filter dropped) and every date is
                computed on-demand.
            min_prior_sessions: See ``detect_ytd_high_low``.
            detail: If True, include the full per-stock ``data`` array.
                Default False returns summary counts only (``count``,
                ``mode``, ``date_from``, ``date_to``, ``new_high``,
                ``new_low``).
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
        if d_from < _cache_window_cutoff():
            return _out_of_cache_error(d_from)

        latest_date = cache.get_latest_equities_date()
        if latest_date is not None and d_to > latest_date:
            return _cache_not_ready_error(d_to, latest_date)

        name_map = cache.get_name_map()

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
                name_map=name_map,
            )

        result = await _high_low_range(
            cache=cache,
            tool_name=screener_compute.TOOL_DETECT_YTD,
            params_hash_value=screener_compute.default_params_hash_ytd(
                min_prior_sessions=min_prior_sessions,
            ),
            date_from=d_from,
            date_to=d_to,
            code=code,
            mode_label="ytd",
            name_map=name_map,
            on_demand=_compute_one,
        )
        return result if detail else _summarise_high_low(result)

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_distribution_days(
        date: str | None = None,
        sigma_multiplier: float = _DIST_DAY_DEFAULT_SIGMA_MULT,
        window_sessions: int = _DIST_DAY_SESSION_WINDOW,
        min_dist_days: int = _DIST_DAY_MIN_COUNT,
    ) -> dict[str, Any]:
        """Count TOPIX distribution days (institutional selling pressure) in a rolling window.

        Use when the user asks whether the market is "under distribution", showing
        institutional selling, or whether the current uptrend is at risk.  Also use
        before confirming a follow-through day — a market already under heavy
        distribution is less likely to sustain a rally.

        A **distribution day** is a session where TOPIX falls ≥ ``sigma_multiplier`` σ
        below the 20-session rolling mean of daily returns (z-score ≤ −``sigma_multiplier``).
        At the default 2.0 σ threshold this fires ~9 times per year, capturing only
        genuine institutional-selling episodes (SVB crisis, yen carry-trade unwind,
        Trump tariff shock) rather than routine volatility.  Four or more within
        ``window_sessions`` (25) sessions signals that the uptrend may be failing.

        Each distribution day entry includes ``volume_confirmed`` — whether total
        market turnover (``SUM(Va)``) exceeded the prior session, which strengthens
        the institutional-selling interpretation.

        See also: ``detect_follow_through_day`` — confirms a new uptrend after a bottom.

        [Supported plans] Free / Light / Standard / Premium
        [Source] indices_bars_daily_topix + equities_bars_daily (no API call)
        **Data availability:** ~17:15 JST on trading days.

        Args:
            date: Target date (YYYYMMDD or YYYY-MM-DD). Defaults to the latest
                cached trading date.
            sigma_multiplier: z-score threshold for a distribution day (default 2.0).
                Uses 20-session rolling σ of TOPIX daily returns.
            window_sessions: Rolling session window for counting distribution days
                (default 25, IBD convention).
            min_dist_days: Count at which ``warning`` is set to ``true``
                (default 4, IBD convention).
        """
        cache: CacheStore = get_cache()

        latest_date = cache.get_latest_equities_date()
        norm_date = _normalize_date(date) if date else (latest_date or "")
        if not norm_date:
            return {
                "error": True,
                "error_type": "CacheNotReady",
                "message": "Cache has no data yet.",
            }

        errors = collect_errors(validate_date(norm_date))
        if errors:
            return make_validation_error_response(errors)
        if latest_date is not None and norm_date > latest_date:
            return _cache_not_ready_error(norm_date, latest_date)
        if sigma_multiplier <= 0:
            return make_validation_error_response(["`sigma_multiplier` must be > 0."])
        if window_sessions < 5:
            return make_validation_error_response(["`window_sessions` must be >= 5."])

        # Warm-up (σ) + count window sessions; pad calendar days for holidays.
        total_sessions = _DIST_DAY_SIGMA_WINDOW + window_sessions
        start = _calendar_window_start(norm_date, total_sessions)

        topix_series = _load_topix_series(cache, start, norm_date)
        # Minimum sessions: sigma_window (warm-up) + window_sessions + 1 so that
        # zscore_series has at least window_sessions entries covering norm_date.
        min_sessions = _DIST_DAY_SIGMA_WINDOW + window_sessions + 1
        if len(topix_series) < min_sessions:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": (
                    f"Insufficient TOPIX data for {norm_date}. "
                    f"Need at least {min_sessions} sessions "
                    f"({_DIST_DAY_SIGMA_WINDOW} warm-up + {window_sessions} window)."
                ),
            }

        zscore_series = _compute_topix_zscore_series(topix_series)
        window = zscore_series[-window_sessions:]

        if not window:
            return _cache_not_ready_error(norm_date, None)
        if window[-1][0] < norm_date:
            # TOPIX Tier 1 cache is behind norm_date (e.g. Cloud Run startup copy is
            # a few days old and the user's plan does not permit an API refresh).
            # Degrade gracefully: run the analysis on the latest available TOPIX date
            # rather than returning CacheNotReady.
            norm_date = window[-1][0]
        elif window[-1][0] > norm_date:
            latest = zscore_series[-1][0]
            return _cache_not_ready_error(norm_date, latest)

        va_by_date = cache.get_market_va_by_date(start, norm_date)
        sorted_va_dates = sorted(va_by_date)

        dist_days: list[dict[str, Any]] = []
        for d, pct, z, sigma, close in window:
            if z > -sigma_multiplier:
                continue
            va_today = va_by_date.get(d)
            idx = bisect.bisect_left(sorted_va_dates, d)
            va_prev = va_by_date.get(sorted_va_dates[idx - 1]) if idx > 0 else None
            vol_confirmed = va_today is not None and va_prev is not None and va_today > va_prev
            dist_days.append(
                {
                    "date": d,
                    "topix_close": round(close, 2),
                    "topix_change_pct": round(pct, 2),
                    "z_score": round(z, 2),
                    "sigma": round(sigma, 4),
                    "volume_confirmed": vol_confirmed,
                    "market_va": int(va_today) if va_today is not None else None,
                }
            )

        count = len(dist_days)
        return {
            "date": norm_date,
            "topix_close": round(window[-1][4], 2),
            "window_sessions": window_sessions,
            "sigma_window": _DIST_DAY_SIGMA_WINDOW,
            "sigma_multiplier": sigma_multiplier,
            "distribution_count": count,
            "warning": count >= min_dist_days,
            "min_dist_days": min_dist_days,
            "distribution_days": dist_days,
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def detect_follow_through_day(
        rally_start: str,
        date: str | None = None,
        sigma_multiplier: float = _DIST_DAY_DEFAULT_SIGMA_MULT,
    ) -> dict[str, Any]:
        """Check whether a follow-through day (フォロースルーデイ) confirms a new uptrend.

        Use when the user asks whether a recent market bottom or bounce has been
        confirmed, or whether the current rally attempt "is for real".

        A **follow-through day** (IBD method) requires all three conditions:
        1. TOPIX rises ≥ ``sigma_multiplier`` σ above the 20-session rolling mean
           (z-score ≥ +``sigma_multiplier``).
        2. Occurs on session **4 or later** from ``rally_start`` (the reversal/low day).
           Sessions 1–3 are too close to the bottom to be reliable.
        3. Total market turnover (``SUM(Va)``) is higher than the prior session.

        To use: identify the low or reversal day as ``rally_start``, then call with
        each subsequent date until a confirmed follow-through (or distribution) occurs.

        See also: ``detect_distribution_days`` — run before confirming a rally to
        check whether the market is already under distribution pressure.

        [Supported plans] Free / Light / Standard / Premium
        [Source] indices_bars_daily_topix + equities_bars_daily (no API call)
        **Data availability:** ~17:15 JST on trading days.

        Args:
            rally_start: First day of the rally attempt — the low or reversal day
                (YYYYMMDD or YYYY-MM-DD).  This counts as session 1.
            date: Date to check for the follow-through signal
                (YYYYMMDD or YYYY-MM-DD). Defaults to the latest cached date.
            sigma_multiplier: z-score threshold for the price condition
                (default 2.0, symmetric with ``detect_distribution_days``).
        """
        cache: CacheStore = get_cache()

        latest_date = cache.get_latest_equities_date()
        norm_rally_start = _normalize_date(rally_start)
        norm_date = _normalize_date(date) if date else (latest_date or "")
        if not norm_date:
            return {
                "error": True,
                "error_type": "CacheNotReady",
                "message": "Cache has no data yet.",
            }

        errors = collect_errors(validate_date(norm_rally_start), validate_date(norm_date))
        if errors:
            return make_validation_error_response(errors)
        if latest_date is not None and norm_date > latest_date:
            return _cache_not_ready_error(norm_date, latest_date)
        if norm_rally_start > norm_date:
            return make_validation_error_response(["`rally_start` must be on or before `date`."])
        if sigma_multiplier <= 0:
            return make_validation_error_response(["`sigma_multiplier` must be > 0."])

        # Pad calendar window so σ can be computed before rally_start.
        pre_start = _calendar_window_start(norm_rally_start, _DIST_DAY_SIGMA_WINDOW)
        topix_series = _load_topix_series(cache, pre_start, norm_date)

        # Need sigma_window sessions for warm-up + at least 1 for z-score computation.
        if len(topix_series) < _DIST_DAY_SIGMA_WINDOW + 2:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": (
                    f"Insufficient TOPIX data. Need at least {_DIST_DAY_SIGMA_WINDOW + 2} sessions."
                ),
            }

        zscore_series = _compute_topix_zscore_series(topix_series)
        rally_sessions = [
            (d, pct, z, s, c) for d, pct, z, s, c in zscore_series if d >= norm_rally_start
        ]

        if not rally_sessions:
            return {
                "error": True,
                "error_type": "InsufficientData",
                "message": (
                    f"No TOPIX sessions found from rally_start={norm_rally_start} to {norm_date}."
                ),
            }

        target = None
        session_number = None
        for i, row in enumerate(rally_sessions):
            if row[0] == norm_date:
                target = row
                session_number = i + 1
                break

        if target is None:
            latest = rally_sessions[-1][0]
            return _cache_not_ready_error(norm_date, latest)

        d, pct, z, sigma, close = target

        va_by_date = cache.get_market_va_by_date(pre_start, norm_date)
        va_today = va_by_date.get(norm_date)
        sorted_va_dates_ftd = sorted(va_by_date)
        idx = bisect.bisect_left(sorted_va_dates_ftd, norm_date)
        va_prev_date = sorted_va_dates_ftd[idx - 1] if idx > 0 else None
        va_prev = va_by_date.get(va_prev_date) if va_prev_date else None
        vol_confirmed = va_today is not None and va_prev is not None and va_today > va_prev

        rally_start_close: float | None = None
        for rd, rc in topix_series:
            if rd == norm_rally_start:
                rally_start_close = round(rc, 2)
                break

        price_confirmed = z >= sigma_multiplier
        day_confirmed = session_number >= 4
        confirmed = price_confirmed and day_confirmed and vol_confirmed

        reasons: list[str] = []
        if not day_confirmed:
            reasons.append(
                f"Session {session_number} is before day 4 (minimum for FTD confirmation)."
            )
        if not price_confirmed:
            reasons.append(f"TOPIX z-score {z:.2f} < +{sigma_multiplier}σ threshold.")
        if not vol_confirmed:
            reasons.append("Market turnover did not increase vs prior session.")

        return {
            "date": norm_date,
            "rally_start": norm_rally_start,
            "rally_start_topix": rally_start_close,
            "session_number": session_number,
            "confirmed": confirmed,
            "reason": " ".join(reasons) if reasons else "All conditions met.",
            "topix_close": round(close, 2),
            "topix_change_pct": round(pct, 2),
            "z_score": round(z, 2),
            "sigma": round(sigma, 4),
            "sigma_multiplier": sigma_multiplier,
            "price_confirmed": price_confirmed,
            "day_confirmed": day_confirmed,
            "volume_confirmed": vol_confirmed,
            "market_va_today": int(va_today) if va_today is not None else None,
            "market_va_prev": int(va_prev) if va_prev is not None else None,
        }


# ----------------------------------------------------------------
# helpers
# ----------------------------------------------------------------


def _load_topix_series(
    cache: CacheStore,
    date_from: str,
    date_to: str,
) -> list[tuple[str, float]]:
    """Return (date, close) pairs sorted ascending for TOPIX.

    Reads ``indices_bars_daily_topix`` via the cache layer; ``date_from``
    must be padded by the caller to cover the σ warm-up window.
    """
    rows = cache.get_rows("indices_bars_daily_topix", {}, date_from=date_from, date_to=date_to)
    series: list[tuple[str, float]] = []
    for row in rows:
        d = str(row.get("Date") or "")[:10]
        c = _as_float(row.get("C") or row.get("Close"))
        if d and c is not None:
            series.append((d, c))
    series.sort()
    return series


def _compute_topix_zscore_series(
    topix_series: list[tuple[str, float]],
    sigma_window: int = _DIST_DAY_SIGMA_WINDOW,
) -> list[tuple[str, float, float, float, float]]:
    """Compute rolling z-scores from a TOPIX close series.

    Returns a list of ``(date, pct_change, z_score, sigma, close)`` tuples
    for each session once the ``sigma_window``-session window is fully
    populated.  Sessions before the warm-up window are omitted.
    """
    rets: list[tuple[str, float, float]] = []
    for i in range(1, len(topix_series)):
        prev_c = topix_series[i - 1][1]
        curr_d, curr_c = topix_series[i]
        if prev_c > 0:
            pct = (curr_c - prev_c) / prev_c * 100
            rets.append((curr_d, pct, curr_c))

    results: list[tuple[str, float, float, float, float]] = []
    for i in range(sigma_window, len(rets)):
        window_rets = [p for _, p, _ in rets[i - sigma_window : i]]
        mean = sum(window_rets) / sigma_window
        variance = sum((r - mean) ** 2 for r in window_rets) / (sigma_window - 1)
        sigma = math.sqrt(variance) if variance > 0 else 0.0
        d, pct, close = rets[i]
        z = (pct - mean) / sigma if sigma > 0 else 0.0
        results.append((d, pct, z, sigma, close))
    return results


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
    name_map: dict[str, str],
) -> dict[str, Any]:
    """Shared implementation for ``detect_52w_high_low`` / ``detect_ytd_high_low``.

    Loads bars between ``range_start`` and ``norm_date`` (inclusive) via
    the cache, then delegates to the pure-Python compute helper in
    ``cache.screener_compute`` so that the populate scripts share the
    exact same logic.
    """
    key_filter: dict[str, str] = {}
    if code:
        key_filter["code"] = normalize_code(code)

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

    result = screener_compute.compute_high_low_signals(
        rows,
        norm_date=norm_date,
        code=code,
        window_sessions=window_sessions,
        min_prior_sessions=min_prior_sessions,
        mode_label=mode_label,
    )
    for item in result.get("data", []):
        if "Code" in item:
            item["name"] = name_map.get(item["Code"])
            item["Code"] = display_code(item["Code"])
    return result


def _try_screener_cache_52w(
    cache: CacheStore,
    *,
    norm_date: str,
    window_sessions: int,
    min_prior_sessions: int,
    name_map: dict[str, str],
) -> dict[str, Any] | None:
    """Look up a pre-computed cross-sectional 52w-high/low payload.

    Caller must guarantee ``code is None`` — payloads were built with
    the cross-sectional IPO filter active and are not safe to serve to
    code-specific queries. Returns ``None`` on miss; hits are returned
    as a fresh dict so the caller can mutate without touching the
    cached object.
    """
    params_hash = screener_compute.default_params_hash_52w(
        window_sessions=window_sessions,
        min_prior_sessions=min_prior_sessions,
    )
    payload = cache.screener_result_get(screener_compute.TOOL_DETECT_52W, params_hash, norm_date)
    if payload is None:
        return None
    data = [dict(item) for item in payload.get("data", [])]
    for item in data:
        if "Code" in item:
            item["name"] = name_map.get(item["Code"])
            item["Code"] = display_code(item["Code"])
    return {"count": payload.get("count", 0), "mode": payload.get("mode", "52w"), "data": data}


def _try_screener_cache_ytd(
    cache: CacheStore,
    *,
    norm_date: str,
    min_prior_sessions: int,
    name_map: dict[str, str],
) -> dict[str, Any] | None:
    """Look up a pre-computed cross-sectional YTD-high/low payload.

    See ``_try_screener_cache_52w`` for the ``code is None`` invariant.
    """
    params_hash = screener_compute.default_params_hash_ytd(
        min_prior_sessions=min_prior_sessions,
    )
    payload = cache.screener_result_get(screener_compute.TOOL_DETECT_YTD, params_hash, norm_date)
    if payload is None:
        return None
    data = [dict(item) for item in payload.get("data", [])]
    for item in data:
        if "Code" in item:
            item["name"] = name_map.get(item["Code"])
            item["Code"] = display_code(item["Code"])
    return {"count": payload.get("count", 0), "mode": payload.get("mode", "ytd"), "data": data}


def _summarise_price_limit(full: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate counts without the per-stock ``data`` array.

    Summary fields:
    - ``limit_high`` / ``limit_low``: total stocks that touched each limit
      (backward-compatible aggregate).
    - ``limit_high_close`` / ``limit_low_close``: closed at the limit
      (引けストップ高/安).
    - ``limit_high_touched`` / ``limit_low_touched``: touched the limit but
      did NOT close there — includes 寄らずストップ and intraday-only touches
      (stronger directional signal than a close-at-limit).
    """
    if "data" not in full:
        return full
    data = full["data"]
    lh_close = sum(1 for r in data if r.get("limit_high_close"))
    ll_close = sum(1 for r in data if r.get("limit_low_close"))
    lh_total = sum(1 for r in data if r.get("limit_high_touched"))
    ll_total = sum(1 for r in data if r.get("limit_low_touched"))
    return {
        "count": full.get("count", 0),
        "limit_high": lh_total,
        "limit_high_close": lh_close,
        "limit_high_touched": lh_total - lh_close,
        "limit_low": ll_total,
        "limit_low_close": ll_close,
        "limit_low_touched": ll_total - ll_close,
    }


def _summarise_high_low(full: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate counts without the per-stock ``data`` array.

    Handles both single-date (52w/ytd) and range results; range payloads
    include ``date_from``/``date_to`` in the summary.
    """
    if "data" not in full:
        return full
    data = full["data"]
    result: dict[str, Any] = {
        "count": full.get("count", 0),
        "mode": full.get("mode", ""),
        "new_high": sum(1 for r in data if r.get("new_high")),
        "new_low": sum(1 for r in data if r.get("new_low")),
    }
    if "date_from" in full:
        result["date_from"] = full["date_from"]
        result["date_to"] = full["date_to"]
    return result


def _summarise_volume_surge(full: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate counts without the per-stock ``data`` array."""
    if "data" not in full:
        return full
    return {
        "count": full.get("count", 0),
        "multiplier": full.get("multiplier"),
        "baseline_days": full.get("baseline_days"),
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
    name_map: dict[str, str],
    on_demand,
) -> dict[str, Any]:
    """Range scan: bulk cache lookup + on-demand fallback per missing day.

    When ``code is None`` the pre-computed cross-sectional cache is
    consulted in bulk and only missing days call ``on_demand(date)``.
    When ``code`` is given the cache is skipped entirely (the stored
    payloads were built with the cross-sectional IPO filter, so serving
    them to a code-specific query would silently drop newly-listed
    stocks). The ``on_demand`` callable already has ``code`` bound by
    closure, so its result is naturally code-filtered.
    """
    if code is None:
        cached_by_date = cache.screener_result_get_range(
            tool_name, params_hash_value, date_from, date_to
        )
    else:
        cached_by_date = {}

    # Determine the trading days in range. Prefer the screener cache
    # itself when it covers the whole range; otherwise discover trading
    # days from the bar table to handle out-of-cache dates and the
    # code-given case.
    trading_days = sorted(cached_by_date.keys())
    if not trading_days or trading_days[0] > date_from or trading_days[-1] < date_to:
        trading_days = cache.iter_session_dates(date_from, date_to) or trading_days

    aggregated: list[dict[str, Any]] = []
    for d in sorted(set(trading_days)):
        if d in cached_by_date:
            rows = [dict(r) for r in cached_by_date[d].get("data", [])]
            for item in rows:
                if "Code" in item:
                    item["name"] = name_map.get(item["Code"])
                    item["Code"] = display_code(item["Code"])
        else:
            payload = await on_demand(d)
            if payload.get("error"):
                # Validation/API error mid-range: surface immediately.
                return payload
            rows = payload.get("data", [])
        aggregated.extend(rows)

    return {
        "count": len(aggregated),
        "mode": mode_label,
        "date_from": date_from,
        "date_to": date_to,
        "data": aggregated,
    }

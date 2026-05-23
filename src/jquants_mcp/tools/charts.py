"""Chart tools for jquants-mcp.

Three tools are exposed:

* ``get_comparison_chart_data`` — returns JSON time-series data (wide
  Recharts format) for multi-stock performance comparison. No optional
  dependencies; always registered.

* ``get_candlestick_data`` — returns candlestick OHLCV + indicator data
  as JSON parallel arrays (Plotly/React artifact format). No optional
  dependencies; always registered.

* ``render_candlestick`` — reads daily bars and renders a candlestick PNG
  via ``mplfinance``. Requires the ``[charts]`` extra (~60 MB). Install
  with::

      pip install "jquants-mcp[charts]"
      uv sync --extra charts

  ``register()`` silently skips ``render_candlestick`` registration when
  the extra is not installed; the other two tools are still available.
"""

from __future__ import annotations

import io
import logging
import math
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

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
    display_code,
    normalize_code,
    validate_code,
    validate_date,
)

logger = logging.getLogger(__name__)


# Indicator names accepted by ``render_candlestick``.
# JP convention favours 5 / 25 / 75 (短期/中期/長期); US convention is
# closer to 20 / 50 / 200. Accept both families so JP traders and
# international users don't have to fight defaults.
_VALID_INDICATORS: frozenset[str] = frozenset(
    {
        "volume",
        "sma5",
        "sma20",
        "sma25",
        "sma60",
        "sma75",
        "sma200",
        "bb20",
    }
)

# Visual styles passed to ``mplfinance``.
_STYLE_ALIASES: dict[str, str] = {
    "default": "yahoo",
    "dark": "nightclouds",
    "colorblind": "blueskies",
}

# matplotlib color names per style alias, keyed (up_color, down_color).
# Used when overlaying lock-day horizontal bars: a 寄らずストップ高 is
# drawn in the up-candle colour, 寄らずストップ安 in the down-candle
# colour, so the visual matches how a non-lock day of the same direction
# would have been coloured.
_LOCK_COLORS: dict[str, tuple[str, str]] = {
    "default": ("g", "r"),
    "dark": ("lime", "red"),
    "colorblind": ("blue", "orange"),
}

# Half-width of the lock-day horizontal overlay bar, in mplfinance's
# integer date-index space. mplfinance draws daily candles at width
# ~0.6, so 0.4 is roughly two-thirds of a candle — wide enough to read
# at a glance, narrow enough not to overlap neighbouring bars.
_LOCK_BAR_HALF_WIDTH = 0.4

# Annotation types accepted by ``render_candlestick``.
# "earnings" draws a vertical dashed line on each earnings announcement date
# found in the equities_earnings_calendar Tier 1 cache (~3 months history).
_VALID_ANNOTATIONS: frozenset[str] = frozenset({"earnings"})

# Vertical line style for earnings annotations.
_EARNINGS_LINE_STYLE: dict[str, object] = {
    "linestyle": "--",
    "linewidth": 1.0,
    "alpha": 0.65,
}

# Colour of earnings vertical lines per style alias.
# colorblind uses black: it is the only hue guaranteed to be distinguishable
# across all common colour-vision deficiencies (protanopia, deuteranopia,
# tritanopia) without relying on the Okabe-Ito palette already used for
# stock-price lines in that style.
_EARNINGS_COLORS: dict[str, str] = {
    "default": "purple",
    "dark": "violet",
    "colorblind": "black",
}

_DPI = 100

# Default display range for render_candlestick when from_date / to_date are omitted.
_DEFAULT_RANGE_DAYS = 91

# Accepted aspect ratios for render_candlestick.
# "square" is the default — fits naturally in both chat and mobile viewports.
_ASPECT_RATIOS: dict[str, tuple[float, float]] = {
    "square": (8.0, 8.0),
    "landscape": (12.0, 6.0),
    "portrait": (6.0, 9.0),
}

# Maximum display length for auto-shortened company name labels.
_BRIEF_NAME_MAX_LEN = 20

# Compiled patterns used by _brief_company_name.
_CORP_SUFFIX_RE = re.compile(r"(?:株式会社|合同会社|有限会社)")
_ETF_SUFFIX_RE = re.compile(r"(?:ETF|ETN)$", re.IGNORECASE)
_ETF_PREFIX_RE = re.compile(r"^(?:ETF|ETN)(?=[^A-Za-z0-9])", re.IGNORECASE)
_ETF_STANDALONE = frozenset({"etf", "etn"})


# CJK-aware font fallback chain so the chart title (company name)
# renders in Japanese instead of tofu. mplfinance styles override
# matplotlib's global rcParams, so register() builds per-style
# ``mpf_style`` objects with this dict injected as ``rc=``.
# Cloud Run image installs ``fonts-noto-cjk`` (Dockerfile) so
# ``Noto Sans CJK JP`` is the production hit; the rest cover macOS
# / other Linux distros for local development.
_CJK_RC: dict[str, Any] = {
    "font.family": "sans-serif",
    "font.sans-serif": [
        "Noto Sans CJK JP",
        "Noto Sans JP",
        "Hiragino Sans",
        "Hiragino Maru Gothic Pro",
        "Yu Gothic",
        "Meiryo",
        "TakaoGothic",
        "IPAexGothic",
        "DejaVu Sans",
    ],
    "axes.unicode_minus": False,
}


def _max_indicator_window(indicators: list[str]) -> int:
    """Return the maximum rolling-window length required by *indicators*."""
    max_win = 0
    for ind in indicators:
        if ind.startswith("sma"):
            max_win = max(max_win, int(ind[3:]))
        elif ind == "bb20":
            max_win = max(max_win, 20)
    return max_win


def _normalize_date(d: str) -> str:
    if "-" in d:
        return d
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _get_company_name(cache: CacheStore, code: str) -> str | None:
    """Best-effort lookup of the listed company name from the
    ``equities_master`` cache.

    Returns the most recent ``CoName`` (Japanese) or ``CoNameEn``
    (English) for the code, or ``None`` if the cache has no entry —
    a charting call must keep working even when the master cache is
    empty or stale.

    Note: J-Quants API v2 uses the short-form field names ``CoName`` /
    ``CoNameEn``; the longer ``CompanyName`` / ``CompanyNameEnglish``
    forms appear only in the API documentation, never in actual
    responses.
    """
    try:
        rows = cache.get_rows("equities_master", key_filter={"code": code})
    except (sqlite3.OperationalError, KeyError) as e:
        # Missing table / corrupted index → render the chart without the
        # company name rather than failing the whole call. Log so the
        # cause is visible in operator debug output.
        logger.debug("equities_master lookup failed for code=%s: %s", code, e)
        return None
    if not rows:
        return None
    # ``Date`` is the listing's master-record date; pick the most recent
    # so renames are picked up. ``or ""`` keeps rows with a missing Date
    # at the bottom (treat them as oldest) instead of raising on None.
    rows.sort(key=lambda r: r.get("Date") or "", reverse=True)
    latest = rows[0]
    for key in ("CoName", "CoNameEn"):
        name = latest.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _brief_company_name(name: str) -> str:
    """Shorten a Japanese company name for use as a chart legend label.

    J-Quants ETF ``CoName`` values lead with an asset-management company name
    separated from the actual fund name by ideographic spaces (U+3000).  This
    function strips that prefix so the legend shows the meaningful fund
    identifier, not the manager.

    Steps:
    1. Split on U+3000 *before* NFKC so the structural boundary survives.
    2. Drop the leading segment(s) that carry a corporate-type suffix
       (株式会社 / 合同会社 / 有限会社) when multiple segments exist.
    3. Apply NFKC to the rest: full-width ASCII → half-width, U+3000 → space,
       （）→ ().
    4. Strip ETF/ETN product-type tokens (standalone, suffix, or prefix)
       so "iFreeETF" → "iFree" and "ETF(年1回決算型)" → "(年1回決算型)".
    5. Truncate to ``_BRIEF_NAME_MAX_LEN`` characters.

    Parenthetical content is intentionally **kept** — for ETFs the
    parenthetical often distinguishes otherwise identical names
    (e.g. 年1回決算型 vs 毎月分配型).

    Returns an empty string when the result is blank so the caller can fall
    back to a code-only label.
    """
    # Split on ideographic space first, before NFKC converts it to ASCII space,
    # so the asset-management company prefix boundary is still detectable.
    parts = name.split("　")
    if len(parts) > 1:
        # The management company name may itself span multiple U+3000-delimited
        # parts (e.g. "Global　X　Japan株式会社"). Scan the first three
        # parts and drop everything up to and including the one with a corp suffix.
        for i, p in enumerate(parts[:3]):
            if _CORP_SUFFIX_RE.search(p):
                parts = parts[i + 1 :]
                break
    name = unicodedata.normalize("NFKC", " ".join(parts))
    # Strip ETF/ETN product-type markers so they don't crowd the label.
    tokens = name.split()
    cleaned = []
    for t in tokens:
        if t.lower() in _ETF_STANDALONE:
            continue
        t = _ETF_SUFFIX_RE.sub("", t)  # iFreeETF → iFree
        t = _ETF_PREFIX_RE.sub("", t)  # ETF(年1回) → (年1回)
        if t:
            cleaned.append(t)
    name = re.sub(r"\s+", " ", " ".join(cleaned)).strip()
    if not name:
        return ""
    if len(name) > _BRIEF_NAME_MAX_LEN:
        name = name[: _BRIEF_NAME_MAX_LEN - 1].rstrip() + "…"
    return name


def _short_date(date_str: str) -> str:
    """Shorten ``YYYY-MM-DD`` to ``'YY/MM`` for compact chart titles.

    Examples: ``"2026-01-05"`` → ``"'26/01"``, ``"2026-05-01"`` → ``"'26/05"``.
    """
    return f"'{date_str[2:4]}/{date_str[5:7]}"


def _build_chart_title(code: str, company: str | None, norm_from: str, norm_to: str) -> str:
    """Compose the chart title used by ``mpf.plot``.

    Format: ``CODE [COMPANY ]'YY/MM → 'YY/MM``.

    The adjusted/raw distinction is intentionally omitted — Kabutan,
    Yahoo! Finance Japan, JPX official pages, every JP brokerage chart,
    and TradingView all show the chart title without an explicit
    "adjusted" suffix. Adjusted is the universal default convention; a
    suffix would be surprising rather than informative. The
    ``render_candlestick`` caller may pass ``adjusted=False`` to use
    raw prices, but that's a deliberate choice and the title doesn't
    advertise it.

    Extracted so the title format can be unit-tested without spinning
    up matplotlib.
    """
    name_part = f" {company}" if company else ""
    return f"{code}{name_part} {_short_date(norm_from)} → {_short_date(norm_to)}"


def _detect_lock_days(rows: list[dict], adjusted: bool) -> list[dict]:
    """Find 寄らずストップ高/安 (lock-up / lock-down) days.

    Lock days are bars where ``Open == High == Low == Close`` AND the
    J-Quants ``UpperLimit`` / ``LowerLimit`` flag is set. mplfinance
    draws these as a single-pixel horizontal line (degenerate doji)
    that visually disappears into the axis even though the day itself
    — a stock locked at the daily limit without trading — is usually
    the most informative bar in the window.

    Returns:
        List of ``{"date": str, "direction": "high"|"low", "price": float}``
        for each detected lock day. Empty list if none found.

    Notes:
        - The cache stores J-Quants ``UpperLimit`` / ``LowerLimit`` under
          the short field names ``UL`` / ``LL`` (see
          ``cache.store._LEGACY_FIELD_MAP``). Both the short and long
          names are accepted here so callers passing raw API responses
          also work. Values may be ``"0"`` / ``"1"`` strings, ints, or
          bools.
        - When ``adjusted=True`` the OHLC comparison uses the
          ``AdjO`` / ``AdjH`` / ``AdjL`` / ``AdjC`` fields so a split
          inside the window does not synthesise a fake non-lock bar.
        - The limit flag itself is not adjusted by J-Quants — it
          reflects the raw trading session.
    """
    prefix = "Adj" if adjusted else ""
    o_key = f"{prefix}O" if adjusted else "O"
    h_key = f"{prefix}H" if adjusted else "H"
    l_key = f"{prefix}L" if adjusted else "L"
    c_key = f"{prefix}C" if adjusted else "C"

    out: list[dict] = []
    for r in rows:
        try:
            o = float(r[o_key])
            h = float(r[h_key])
            low = float(r[l_key])
            c = float(r[c_key])
            date = r["Date"]
        except (KeyError, TypeError, ValueError):
            continue
        if not (o == h == low == c):
            continue
        ul_raw = r.get("UL", r.get("UpperLimit", "0"))
        ll_raw = r.get("LL", r.get("LowerLimit", "0"))
        ul = str(ul_raw) == "1"
        ll = str(ll_raw) == "1"
        if not (ul or ll):
            continue
        out.append(
            {
                "date": date,
                "direction": "high" if ul else "low",
                "price": c,
            }
        )
    return out


def _rolling_mean(values: list[float | None], window: int) -> list[float | None]:
    """Compute a simple rolling mean over *values* with the given *window* size.

    Returns ``None`` for each position that has fewer than *window* preceding
    non-``None`` values (i.e. the warm-up period). A ``None`` input value
    resets the accumulation buffer so a gap in the series propagates correctly.
    """
    result: list[float | None] = []
    buf: list[float] = []
    for v in values:
        if v is None:
            buf.clear()
            result.append(None)
        else:
            buf.append(v)
            if len(buf) > window:
                buf.pop(0)
            result.append(sum(buf) / window if len(buf) == window else None)
    return result


def _rolling_std(values: list[float | None], window: int) -> list[float | None]:
    """Compute a rolling sample standard deviation (ddof=1) with *window* size.

    Matches ``pandas.Series.rolling(window).std()`` (Bessel's correction).
    Returns ``None`` during the warm-up period; a ``None`` input resets the buffer.
    """
    result: list[float | None] = []
    buf: list[float] = []
    for v in values:
        if v is None:
            buf.clear()
            result.append(None)
        else:
            buf.append(v)
            if len(buf) > window:
                buf.pop(0)
            if len(buf) == window:
                mean = sum(buf) / window
                variance = sum((x - mean) ** 2 for x in buf) / (window - 1)
                result.append(math.sqrt(variance))
            else:
                result.append(None)
    return result


def register(
    mcp: FastMCP,
    get_client: Any,  # noqa: ARG001 — signature parity with other tool modules
    get_cache: Any,
) -> None:
    """Register chart-rendering tools.

    ``get_comparison_chart_data`` is always registered (no optional
    dependencies). ``render_candlestick`` requires the ``[charts]``
    extra (mplfinance + matplotlib) and is silently skipped when those
    are not installed.
    """

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_comparison_chart_data(
        codes: list[str],
        from_date: str,
        to_date: str,
        mode: str = "return_pct",
        labels: list[str] | None = None,
    ) -> dict:
        """Return time-series data for a multi-stock comparison (複数銘柄比較データ). All plans.

        Use for 比較チャート・パフォーマンス比較・リターン比較・relative performance queries (up to 10 codes).
        Returns JSON records suitable for React artifact rendering with Recharts LineChart.
        For ローソク足・candlestick charts use sibling render_candlestick (returns PNG).

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            codes: 1–10 stock codes (e.g. ["7203", "8697"]).
            from_date: Range start (YYYYMMDD or YYYY-MM-DD), inclusive.
            to_date: Range end (YYYYMMDD or YYYY-MM-DD), inclusive.
            mode: "return_pct" (default, normalised to 0% at first bar) or "price" (raw adjusted close).
            labels: Custom legend labels per code. Omit for auto-generated names.

        Returns:
            dict with keys:
              mode        — echoes the requested mode
              from_date   — normalised YYYY-MM-DD
              to_date     — normalised YYYY-MM-DD
              records     — list of {"date": str, <label>: float, ...} rows (Recharts dataKey format)
              series_keys — ordered list of label strings matching records keys
            On error: {"error": "<message>"}
        """
        if not codes or len(codes) > 10:
            return {"error": "codes must be a list of 1–10 stock codes."}

        code_errors: list[str] = []
        for c in codes:
            err = validate_code(c, param=f"codes[{c!r}]")
            if err:
                code_errors.append(err)
        date_errors = collect_errors(
            validate_date(from_date, param="from_date"),
            validate_date(to_date, param="to_date"),
        )
        all_errors = code_errors + date_errors
        if all_errors:
            return {"error": "; ".join(all_errors)}

        if mode not in ("return_pct", "price"):
            return {"error": f"Unknown mode: {mode!r}. Accepted: 'return_pct', 'price'"}
        if labels is not None and len(labels) != len(codes):
            return {
                "error": f"labels length ({len(labels)}) must match codes length ({len(codes)})."
            }

        norm_from = _normalize_date(from_date)
        norm_to = _normalize_date(to_date)
        if norm_from > norm_to:
            return {"error": "`from_date` must be <= `to_date`."}

        cache: CacheStore = get_cache()

        series_map: dict[str, dict[str, float]] = {}
        for idx, code in enumerate(codes):
            norm_code = normalize_code(code)
            try:
                rows = cache.get_rows(
                    "equities_bars_daily",
                    key_filter={"code": norm_code},
                    date_from=norm_from,
                    date_to=norm_to,
                )
            except (
                APIError,
                InvalidAPIKeyError,
                UserNotConfiguredError,
                DecryptionError,
                UserNotAllowedError,
            ) as e:
                err = format_api_error(e)
                return {"error": err.get("message") or "API error"}

            raw: dict[str, float] = {}
            for r in rows:
                try:
                    d = _normalize_date(r["Date"])
                    adj_c = r.get("AdjC")
                    raw_c = r.get("C")
                    val = float(adj_c if adj_c not in (None, "") else raw_c)
                    raw[d] = val
                except (KeyError, TypeError, ValueError):
                    continue

            if not raw:
                logger.debug("get_comparison_chart_data: no bars for %s", norm_code)
                continue

            display = display_code(norm_code)
            if labels is not None and labels[idx].strip():
                label = labels[idx]
            else:
                company = _get_company_name(cache, norm_code)
                if company:
                    brief = _brief_company_name(company)
                    label = f"{display} {brief}" if brief else display
                else:
                    label = display
            series_map[label] = raw

        if not series_map:
            return {"error": f"No cached bars found for any code in {norm_from}..{norm_to}."}

        # Collect all unique dates and ordered labels
        all_dates = sorted({d for series in series_map.values() for d in series})
        all_labels = list(series_map.keys())

        # Build matrix: date -> label -> value (None if not yet seen)
        matrix: dict[str, dict[str, float | None]] = {d: {} for d in all_dates}
        for lbl, series in series_map.items():
            for d in all_dates:
                matrix[d][lbl] = series.get(d)

        # Forward-fill isolated missing days (e.g., one stock absent from
        # a specific API response) so a single gap doesn't break the line.
        last_known: dict[str, float | None] = {lbl: None for lbl in all_labels}
        for d in all_dates:
            for lbl in all_labels:
                if matrix[d][lbl] is not None:
                    last_known[lbl] = matrix[d][lbl]
                elif last_known[lbl] is not None:
                    matrix[d][lbl] = last_known[lbl]

        if mode == "return_pct":
            # bfill so a stock that starts mid-window (late IPO) uses its
            # own first real bar as baseline rather than giving a None row.
            baseline: dict[str, float | None] = {}
            for lbl in all_labels:
                baseline[lbl] = None
                for d in all_dates:
                    if matrix[d][lbl] is not None:
                        baseline[lbl] = matrix[d][lbl]
                        break

            for d in all_dates:
                for lbl in all_labels:
                    v = matrix[d][lbl]
                    b = baseline[lbl]
                    if v is not None and b is not None and b != 0:
                        matrix[d][lbl] = round((v / b - 1) * 100, 6)
                    else:
                        matrix[d][lbl] = None

        # Assemble Recharts-compatible wide-format records
        records = []
        for d in all_dates:
            row: dict = {"date": d}
            for lbl in all_labels:
                v = matrix[d][lbl]
                if v is not None:
                    row[lbl] = v
            records.append(row)

        return {
            "mode": mode,
            "from_date": norm_from,
            "to_date": norm_to,
            "records": records,
            "series_keys": all_labels,
        }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_candlestick_data(
        code: str,
        from_date: str | None = None,
        to_date: str | None = None,
        indicators: list[str] | None = None,
        adjusted: bool = True,
    ) -> dict:
        """Return candlestick OHLCV + indicator data as JSON (ローソク足データJSON). All plans.

        Use for ローソク足・株価チャート・React artifact チャート queries (JSON format, no PNG).
        Returns parallel arrays for Plotly/Recharts React artifact rendering.
        For PNG chart use sibling render_candlestick; for comparison charts use get_comparison_chart_data.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)

        Args:
            code: Stock code (e.g. "7203" or "72030").
            from_date: Range start (YYYYMMDD or YYYY-MM-DD). Default: 91 days before to_date.
            to_date: Range end (YYYYMMDD or YYYY-MM-DD). Default: today.
            indicators: Overlays list. Default ["volume","sma5","sma25"]. Options:
                volume, sma5, sma20, sma25, sma60, sma75, sma200, bb20.
            adjusted: Use split-adjusted prices (default True).

        Returns:
            dict with keys:
              code          — normalised 5-char code
              display_code  — 4-char display code (e.g. "7203")
              company       — brief company name or null
              from_date     — YYYY-MM-DD display start
              to_date       — YYYY-MM-DD display end
              adjusted      — bool
              dates         — list[str] YYYY-MM-DD
              ohlcv         — {open, high, low, close, volume} each list[float]
              indicators    — {sma5, ..., bb20_upper, bb20_mid, bb20_lower} list[float|null]
              lock_days     — list[{date, direction, price}]
              earnings_dates — list[str] YYYY-MM-DD within the display window
            On error: {"error": "<message>"}
        """
        if indicators is None:
            indicators = ["volume", "sma5", "sma25"]

        errors = collect_errors(
            validate_code(code),
            validate_date(from_date, param="from_date"),
            validate_date(to_date, param="to_date"),
        )
        if errors:
            return {"error": errors[0]}

        unknown = sorted(set(indicators) - _VALID_INDICATORS)
        if unknown:
            return {
                "error": f"Unknown indicators: {unknown}. Accepted: {sorted(_VALID_INDICATORS)}"
            }

        today_str = date.today().strftime("%Y-%m-%d")
        norm_to = _normalize_date(to_date) if to_date is not None else today_str
        norm_from = (
            _normalize_date(from_date)
            if from_date is not None
            else (
                datetime.strptime(norm_to, "%Y-%m-%d") - timedelta(days=_DEFAULT_RANGE_DAYS - 1)
            ).strftime("%Y-%m-%d")
        )
        if norm_from > norm_to:
            return {"error": "`from_date` must be <= `to_date`."}

        norm_code = normalize_code(code)

        # Extend fetch window so indicators are warmed before the first display bar.
        max_win = _max_indicator_window(indicators)
        warmup_start = (
            (datetime.strptime(norm_from, "%Y-%m-%d") - timedelta(days=max_win * 2)).strftime(
                "%Y-%m-%d"
            )
            if max_win > 0
            else norm_from
        )

        cache: CacheStore = get_cache()
        try:
            all_rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={"code": norm_code},
                date_from=warmup_start,
                date_to=norm_to,
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            err = format_api_error(e)
            return {"error": err.get("message") or "API error"}

        all_rows.sort(key=lambda r: str(r.get("Date") or ""))

        o_key = "AdjO" if adjusted else "O"
        h_key = "AdjH" if adjusted else "H"
        l_key = "AdjL" if adjusted else "L"
        c_key = "AdjC" if adjusted else "C"
        v_key = "AdjVo" if adjusted else "Vo"

        # Parse all rows (warmup + display) for indicator computation.
        all_parsed: list[tuple[str, float, float, float, float, float]] = []
        for r in all_rows:
            try:
                d = _normalize_date(str(r["Date"])[:10])
                all_parsed.append(
                    (
                        d,
                        float(r[o_key]),
                        float(r[h_key]),
                        float(r[l_key]),
                        float(r[c_key]),
                        float(r[v_key]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

        # Slice to the display range.
        display_parsed = [row for row in all_parsed if row[0] >= norm_from]

        if not display_parsed:
            return {
                "error": (
                    f"No cached bars for code={norm_code} in {norm_from}..{norm_to}. "
                    "Run scripts/daily_fetch.py to populate the cache."
                )
            }

        # Compute indicators over the full (warmup + display) close series.
        closes_all = [row[4] for row in all_parsed]
        ind_series: dict[str, list[float | None]] = {}
        for ind in indicators:
            if ind == "volume":
                continue
            if ind.startswith("sma"):
                length = int(ind[3:])
                ind_series[ind] = _rolling_mean(closes_all, length)
            elif ind == "bb20":
                mid = _rolling_mean(closes_all, 20)
                std = _rolling_std(closes_all, 20)
                ind_series["bb20_upper"] = [
                    m + 2 * s if m is not None and s is not None else None for m, s in zip(mid, std)
                ]
                ind_series["bb20_mid"] = mid
                ind_series["bb20_lower"] = [
                    m - 2 * s if m is not None and s is not None else None for m, s in zip(mid, std)
                ]

        display_start_idx = len(all_parsed) - len(display_parsed)

        dates: list[str] = [row[0] for row in display_parsed]
        ohlcv = {
            "open": [row[1] for row in display_parsed],
            "high": [row[2] for row in display_parsed],
            "low": [row[3] for row in display_parsed],
            "close": [row[4] for row in display_parsed],
            "volume": [row[5] for row in display_parsed],
        }
        indicators_out: dict[str, list[float | None]] = {
            key: series[display_start_idx:] for key, series in ind_series.items()
        }

        display_rows = [r for r in all_rows if str(r.get("Date") or "")[:10] >= norm_from]
        lock_days = _detect_lock_days(display_rows, adjusted)

        earnings_dates = cache.get_earnings_dates(norm_code, norm_from, norm_to)

        company_raw = _get_company_name(cache, norm_code)
        company = _brief_company_name(company_raw) if company_raw else None

        return {
            "code": norm_code,
            "display_code": display_code(norm_code),
            "company": company,
            "from_date": norm_from,
            "to_date": norm_to,
            "adjusted": adjusted,
            "dates": dates,
            "ohlcv": ohlcv,
            "indicators": indicators_out,
            "lock_days": lock_days,
            "earnings_dates": earnings_dates,
        }

    try:
        import mplfinance as mpf
        import pandas as pd
        from matplotlib import pyplot as plt
    except ModuleNotFoundError:
        logger.info(
            "charts: mplfinance / matplotlib not installed; "
            "render_candlestick tool will not be registered. "
            "Install with: pip install 'jquants-mcp[charts]'"
        )
        return

    # ``mpf_style`` objects built per alias with the module-level CJK
    # rcParams so the title font falls back to a CJK-capable family.
    _STYLES = {
        alias: mpf.make_mpf_style(base_mpf_style=base, rc=_CJK_RC)
        for alias, base in _STYLE_ALIASES.items()
    }

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def render_candlestick(
        code: str,
        from_date: str | None = None,
        to_date: str | None = None,
        indicators: list[str] | None = None,
        style: str = "default",
        adjusted: bool = True,
        aspect_ratio: str = "square",
        annotations: list[str] | None = None,
    ) -> Image:
        """Render a candlestick chart as PNG (ローソク足チャート). All plans.

        Use for チャート・ローソク足・株価チャート・日足チャート・テクニカルチャート queries.
        Render charts sequentially (not in parallel) — parallel renders can OOM on Cloud Run.
        SMA/Bollinger warmup fetches extra days before from_date for accurate indicator values.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no API call)
        [Optional dependency] pip install 'jquants-mcp[charts]' (mplfinance + matplotlib)

        Args:
            code: Stock code (e.g. "7203" or "72030").
            from_date: Range start (YYYYMMDD or YYYY-MM-DD). Default: 91 days before to_date.
            to_date: Range end (YYYYMMDD or YYYY-MM-DD). Default: today.
            indicators: Overlays list. Default ["volume","sma5","sma25"]. Options:
                volume, sma5, sma20, sma25, sma60, sma75, sma200, bb20.
            style: "default" (Yahoo-like), "dark", or "colorblind".
            adjusted: Use split-adjusted prices (default True).
            aspect_ratio: "square" (default), "landscape", or "portrait".
            annotations: Optional overlays e.g. ["earnings"] for earnings date lines.
        """
        if indicators is None:
            indicators = ["volume", "sma5", "sma25"]
        if annotations is None:
            annotations = []

        errors = collect_errors(
            validate_code(code),
            validate_date(from_date, param="from_date"),
            validate_date(to_date, param="to_date"),
        )
        if errors:
            return _error_image(errors[0])

        unknown = sorted(set(indicators) - _VALID_INDICATORS)
        if unknown:
            return _error_image(
                f"Unknown indicators: {unknown}. Accepted: {sorted(_VALID_INDICATORS)}"
            )
        unknown_ann = sorted(set(annotations) - _VALID_ANNOTATIONS)
        if unknown_ann:
            return _error_image(
                f"Unknown annotations: {unknown_ann}. Accepted: {sorted(_VALID_ANNOTATIONS)}"
            )
        if style not in _STYLE_ALIASES:
            return _error_image(f"Unknown style: {style!r}. Accepted: {sorted(_STYLE_ALIASES)}")
        if aspect_ratio not in _ASPECT_RATIOS:
            return _error_image(
                f"Unknown aspect_ratio: {aspect_ratio!r}. Accepted: {sorted(_ASPECT_RATIOS)}"
            )

        today_str = date.today().strftime("%Y-%m-%d")
        norm_to = _normalize_date(to_date) if to_date is not None else today_str
        norm_from = (
            _normalize_date(from_date)
            if from_date is not None
            else (
                datetime.strptime(norm_to, "%Y-%m-%d") - timedelta(days=_DEFAULT_RANGE_DAYS - 1)
            ).strftime("%Y-%m-%d")
        )
        if norm_from > norm_to:
            return _error_image("`from_date` must be <= `to_date`.")

        norm_code = normalize_code(code)

        # Extend the fetch window backwards so rolling-average indicators
        # (SMA, Bollinger) are fully warmed before the first displayed bar.
        max_win = _max_indicator_window(indicators)
        warmup_start = (
            (datetime.strptime(norm_from, "%Y-%m-%d") - timedelta(days=max_win * 2)).strftime(
                "%Y-%m-%d"
            )
            if max_win > 0
            else norm_from
        )

        cache: CacheStore = get_cache()
        try:
            all_rows = cache.get_rows(
                "equities_bars_daily",
                key_filter={"code": norm_code},
                date_from=warmup_start,
                date_to=norm_to,
            )
        except (
            APIError,
            InvalidAPIKeyError,
            UserNotConfiguredError,
            DecryptionError,
            UserNotAllowedError,
        ) as e:
            err = format_api_error(e)
            return _error_image(err.get("message") or "API error")

        # Rows visible in the rendered chart (≥ norm_from).
        display_rows = [r for r in all_rows if str(r.get("Date") or "")[:10] >= norm_from]

        if not display_rows:
            return _error_image(
                f"No cached bars for code={norm_code} in {norm_from}..{norm_to}. "
                "Run scripts/daily_fetch.py to populate the cache."
            )

        prefix = "Adj" if adjusted else ""
        cols = {
            "Date": "Date",
            "Open": f"{prefix}O" if adjusted else "O",
            "High": f"{prefix}H" if adjusted else "H",
            "Low": f"{prefix}L" if adjusted else "L",
            "Close": f"{prefix}C" if adjusted else "C",
            "Volume": f"{prefix}Vo" if adjusted else "Vo",
        }

        # Build extended DataFrame (warmup + display) for indicator computation.
        all_records = []
        for r in all_rows:
            try:
                all_records.append(
                    {
                        "Date": pd.to_datetime(r[cols["Date"]]),
                        "Open": float(r[cols["Open"]]),
                        "High": float(r[cols["High"]]),
                        "Low": float(r[cols["Low"]]),
                        "Close": float(r[cols["Close"]]),
                        "Volume": float(r[cols["Volume"]]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                # Skip malformed rows rather than fail the whole chart.
                continue
        if not all_records:
            return _error_image(f"Cached rows for code={norm_code} are missing OHLC columns.")

        df_extended = pd.DataFrame.from_records(all_records).set_index("Date").sort_index()
        display_start = pd.Timestamp(norm_from)
        df = df_extended[df_extended.index >= display_start]

        if df.empty:
            return _error_image(f"Cached rows for code={norm_code} are missing OHLC columns.")

        addplots = []
        for ind in indicators:
            if ind == "volume":
                continue  # handled by mpf.plot(volume=True)
            if ind.startswith("sma"):
                length = int(ind[3:])
                if len(df_extended) >= length:
                    sma_ext = df_extended["Close"].rolling(length).mean()
                    sma = sma_ext[sma_ext.index >= display_start]
                    addplots.append(mpf.make_addplot(sma, width=1.0))
            elif ind == "bb20":
                if len(df_extended) >= 20:
                    mid_ext = df_extended["Close"].rolling(20).mean()
                    std_ext = df_extended["Close"].rolling(20).std()
                    mid = mid_ext[mid_ext.index >= display_start]
                    std = std_ext[std_ext.index >= display_start]
                    addplots.append(mpf.make_addplot(mid + 2 * std, width=0.8))
                    addplots.append(mpf.make_addplot(mid - 2 * std, width=0.8))

        company_raw = _get_company_name(cache, norm_code)
        # Run the company name through the same normaliser that
        # ``render_comparison_chart`` uses for legend labels: NFKC folds
        # full-width ASCII (e.g. "ＨＥＮＮＧＥ" → "HENNGE") so the title
        # does not render with phantom inter-character spacing, the
        # corporate-suffix prefix is dropped (e.g. "野村アセットマネジメント
        # 株式会社　NEXT FUNDS …" → "NEXT FUNDS …"), and the result is
        # truncated to ``_BRIEF_NAME_MAX_LEN`` so long ETF names no
        # longer overflow the figure width.
        company = _brief_company_name(company_raw) if company_raw else None
        # Cache lookups always use the 5-digit form, but display the
        # conventional 4-digit form (``72030`` → ``7203``) for
        # ordinary shares so the title matches how JP investors
        # actually refer to the stock.
        title = _build_chart_title(display_code(norm_code), company, norm_from, norm_to)

        lock_days = _detect_lock_days(display_rows, adjusted)

        # Fetch earnings dates within the display window when requested.
        earnings_dates: list[str] = []
        if "earnings" in annotations:
            earnings_dates = cache.get_earnings_dates(norm_code, norm_from, norm_to)

        buf = io.BytesIO()
        # mplfinance's addplot validator rejects ``None`` (only dict / list
        # of dicts allowed), so omit the kwarg entirely when there are no
        # overlay addplots — e.g. ``indicators=["volume"]`` only.
        plot_kwargs = {
            "type": "candle",
            "style": _STYLES[style],
            "volume": "volume" in indicators,
            "title": title,
            "figsize": _ASPECT_RATIOS[aspect_ratio],
        }
        if addplots:
            plot_kwargs["addplot"] = addplots

        # ``bbox_inches="tight"`` crops the surrounding figure padding at
        # save time, which is the canonical mplfinance + matplotlib fix
        # for the "extra right-side margin" that mpf.plot leaves when the
        # title is shorter than the figure or when the title pushes the
        # axes inward. Applied to both the lock-day ``returnfig`` path
        # and the default ``savefig`` path so the two are visually
        # identical.
        savefig_kwargs = {"fname": buf, "dpi": _DPI, "format": "png", "bbox_inches": "tight"}

        try:
            if lock_days or earnings_dates:
                # Take the ``returnfig`` path whenever we need to draw custom
                # overlays: lock-day horizontal bars and/or earnings vertical lines.
                plot_kwargs["returnfig"] = True
                fig, axes = mpf.plot(df, **plot_kwargs)
                try:
                    price_ax = axes[0]
                    # Lock days: invisible doji lines → replace with short hlines.
                    up_color, down_color = _LOCK_COLORS[style]
                    for lock in lock_days:
                        lock_date = pd.to_datetime(lock["date"])
                        if lock_date not in df.index:
                            continue
                        x_idx = df.index.get_loc(lock_date)
                        color = up_color if lock["direction"] == "high" else down_color
                        price_ax.hlines(
                            lock["price"],
                            x_idx - _LOCK_BAR_HALF_WIDTH,
                            x_idx + _LOCK_BAR_HALF_WIDTH,
                            colors=color,
                            linewidth=2.0,
                        )
                    # Earnings annotations: vertical dashed lines.
                    earn_color = _EARNINGS_COLORS[style]
                    for earn_date in earnings_dates:
                        earn_ts = pd.to_datetime(earn_date)
                        if earn_ts not in df.index:
                            continue
                        x_idx = df.index.get_loc(earn_ts)
                        price_ax.axvline(x=x_idx, color=earn_color, **_EARNINGS_LINE_STYLE)
                    # Reuse the same savefig kwargs as the default path
                    # so the two visual outputs match exactly.
                    fig.savefig(**savefig_kwargs)
                finally:
                    plt.close(fig)
            else:
                plot_kwargs["savefig"] = savefig_kwargs
                mpf.plot(df, **plot_kwargs)
        except Exception as exc:  # mplfinance / matplotlib runtime errors
            logger.warning("render_candlestick: rendering failed: %s", exc)
            return _error_image(f"Chart rendering failed: {exc}")

        return Image(data=buf.getvalue(), format="png")


def _error_image(message: str) -> Image:
    """Render a plain-text PNG carrying the error message.

    Returning an Image (rather than a dict) keeps the tool's contract
    consistent — clients that expect an inline image always get one.
    The message is encoded into the PNG by matplotlib so Claude can
    surface it visually instead of failing the call.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        # If matplotlib itself isn't installed we shouldn't be here —
        # register() would have skipped. As a last resort raise so the
        # caller surfaces something instead of silently truncating.
        raise

    fig, ax = plt.subplots(figsize=(8, 2), dpi=100)
    ax.axis("off")
    ax.text(
        0.5,
        0.5,
        message,
        ha="center",
        va="center",
        wrap=True,
        fontsize=11,
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return Image(data=buf.getvalue(), format="png")

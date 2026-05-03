"""Chart-rendering tools for jquants-mcp.

Reads daily bars from the ``equities_bars_daily`` Tier 1 cache, builds a
pandas DataFrame, and renders a candlestick PNG via ``mplfinance``. The
PNG is returned through FastMCP's ``Image`` helper so Claude Desktop /
mobile clients display it inline.

The module is **opt-in**: ``mplfinance`` and ``matplotlib`` are not core
dependencies (~60 MB). ``register()`` returns silently if either import
fails, so the lean stdio install simply omits the tool. Install with::

    pip install "jquants-mcp[charts]"
    uv sync --extra charts
"""

from __future__ import annotations

import io
import logging
import sqlite3
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

# PNG render dimensions: comfortable for Claude clients without bloating
# the response payload (typical output well under 200 KB at this size).
_FIG_WIDTH = 12.0
_FIG_HEIGHT = 8.0
_DPI = 100


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


def _normalize_date(date: str) -> str:
    if "-" in date:
        return date
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"


def _normalize_code(code: str) -> str:
    return code + "0" if len(code) == 4 else code


def _display_code(code: str) -> str:
    """Render a J-Quants stock code in the form Japanese investors read.

    JP stock codes have a 4-character "display" form and a 5-character
    "J-Quants API" form. The 5th character is ``0`` for ordinary
    shares, non-zero for preferred / second-class shares. JPX, Kabutan,
    Yahoo! Finance Japan and the rest of the JP equity ecosystem all
    show ordinary shares in the 4-character form (``7203`` not
    ``72030``, ``130A`` not ``130A0``).

    The alphanumeric codes (e.g. ``130A``) were introduced by JPX in
    2024 to extend the ticker space — they follow the same 4-char
    display / 5-char API duality as the legacy numeric codes.

    Examples:
        ``"7203"`` → ``"7203"`` (already 4-char display form)
        ``"72030"`` → ``"7203"`` (5-char ordinary share → 4-char)
        ``"25935"`` → ``"25935"`` (5-char non-ordinary, suffix ≠ 0)
        ``"130A0"`` → ``"130A"`` (5-char alphanumeric ordinary share)
    """
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


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


def _build_chart_title(code: str, company: str | None, norm_from: str, norm_to: str) -> str:
    """Compose the chart title used by ``mpf.plot``.

    Format: ``CODE [COMPANY ]FROM → TO``.

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
    return f"{code}{name_part} {norm_from} → {norm_to}"


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


def register(
    mcp: FastMCP,
    get_client: Any,  # noqa: ARG001 — signature parity with other tool modules
    get_cache: Any,
) -> None:
    """Register chart-rendering tools.

    Returns silently when the optional ``mplfinance`` / ``matplotlib``
    extras are not installed, so production servers running the lean
    stdio profile skip the tool registration without raising.
    """
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
        from_date: str,
        to_date: str,
        indicators: list[str] | None = None,
        style: str = "default",
        adjusted: bool = True,
    ) -> Image:
        """Render a stock candlestick chart as a PNG (ローソク足チャート). All plans.

        Use for チャート, ローソク足, 株価チャート, 日足チャート, chart, candlestick,
        テクニカルチャート, price chart.
        Reads daily bars from the local cache (no API call). The image is returned
        inline; Claude Desktop and Claude mobile display it directly in chat.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (no API call)
        [Optional dependency] ``mplfinance`` + ``matplotlib`` (install
        with ``pip install 'jquants-mcp[charts]'``)

        **Call sequentially when rendering multiple charts** (one at a
        time, not in parallel). Each render allocates ~50–500 MB of
        matplotlib workspace depending on the date range and overlays;
        firing N renders in parallel can exhaust the Cloud Run memory
        budget and trigger OOM kills. For 2+ charts in a row, issue
        the calls one after another.

        Args:
            code: 4- or 5-digit stock code (e.g. "72030" or "7203").
            from_date: Range start (YYYYMMDD or YYYY-MM-DD), inclusive.
            to_date: Range end (YYYYMMDD or YYYY-MM-DD), inclusive.
            indicators: List of overlays. Defaults to
                ``["volume", "sma5", "sma25"]`` (Japanese 短期/中期
                convention). Accepted values: ``volume``, ``sma5``,
                ``sma20``, ``sma25``, ``sma60``, ``sma75``, ``sma200``,
                ``bb20`` (20-session Bollinger band ボリンジャーバンド).
            style: ``default`` (Yahoo-like), ``dark``, or ``colorblind``.
            adjusted: When ``True`` (default) use split-adjusted prices
                (``AdjO`` / ``AdjH`` / ``AdjL`` / ``AdjC``) so corporate
                actions inside the window do not produce a price gap.
                Set ``False`` to render unadjusted prices.
        """
        if indicators is None:
            indicators = ["volume", "sma5", "sma25"]

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
        if style not in _STYLE_ALIASES:
            return _error_image(f"Unknown style: {style!r}. Accepted: {sorted(_STYLE_ALIASES)}")

        norm_code = _normalize_code(code)
        norm_from = _normalize_date(from_date)
        norm_to = _normalize_date(to_date)
        if norm_from > norm_to:
            return _error_image("`from_date` must be <= `to_date`.")

        cache: CacheStore = get_cache()
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
            return _error_image(err.get("message") or "API error")

        if not rows:
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
        records = []
        for r in rows:
            try:
                records.append(
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
        if not records:
            return _error_image(f"Cached rows for code={norm_code} are missing OHLC columns.")

        df = pd.DataFrame.from_records(records).set_index("Date").sort_index()

        addplots = []
        for ind in indicators:
            if ind == "volume":
                continue  # handled by mpf.plot(volume=True)
            if ind.startswith("sma"):
                length = int(ind[3:])
                if len(df) >= length:
                    sma = df["Close"].rolling(length).mean()
                    addplots.append(mpf.make_addplot(sma, width=1.0))
            elif ind == "bb20":
                if len(df) >= 20:
                    mid = df["Close"].rolling(20).mean()
                    std = df["Close"].rolling(20).std()
                    addplots.append(mpf.make_addplot(mid + 2 * std, width=0.8))
                    addplots.append(mpf.make_addplot(mid - 2 * std, width=0.8))

        company = _get_company_name(cache, norm_code)
        # Cache lookups always use the 5-digit form, but display the
        # conventional 4-digit form (``72030`` → ``7203``) for
        # ordinary shares so the title matches how JP investors
        # actually refer to the stock.
        title = _build_chart_title(_display_code(norm_code), company, norm_from, norm_to)

        lock_days = _detect_lock_days(rows, adjusted)

        buf = io.BytesIO()
        # mplfinance's addplot validator rejects ``None`` (only dict / list
        # of dicts allowed), so omit the kwarg entirely when there are no
        # overlay addplots — e.g. ``indicators=["volume"]`` only.
        plot_kwargs = {
            "type": "candle",
            "style": _STYLES[style],
            "volume": "volume" in indicators,
            "title": title,
            "figsize": (_FIG_WIDTH, _FIG_HEIGHT),
        }
        if addplots:
            plot_kwargs["addplot"] = addplots

        try:
            if lock_days:
                # Lock days (O=H=L=C with UL/LL set) render as invisible
                # doji lines under default mplfinance behaviour, so we
                # take the ``returnfig`` path and overlay short coloured
                # horizontal bars in the up/down candle colour.
                plot_kwargs["returnfig"] = True
                fig, axes = mpf.plot(df, **plot_kwargs)
                try:
                    price_ax = axes[0]
                    up_color, down_color = _LOCK_COLORS[style]
                    for lock in lock_days:
                        date = pd.to_datetime(lock["date"])
                        if date not in df.index:
                            continue
                        x_idx = df.index.get_loc(date)
                        color = up_color if lock["direction"] == "high" else down_color
                        price_ax.hlines(
                            lock["price"],
                            x_idx - _LOCK_BAR_HALF_WIDTH,
                            x_idx + _LOCK_BAR_HALF_WIDTH,
                            colors=color,
                            linewidth=2.0,
                        )
                    fig.savefig(buf, dpi=_DPI, format="png")
                finally:
                    plt.close(fig)
            else:
                plot_kwargs["savefig"] = {"fname": buf, "dpi": _DPI, "format": "png"}
                mpf.plot(df, **plot_kwargs)
        except Exception as exc:  # mplfinance / matplotlib runtime errors
            logger.warning("render_candlestick: rendering failed: %s", exc)
            return _error_image(f"Chart rendering failed: {exc}")

        return Image(data=buf.getvalue(), format="png")

    # Okabe-Ito colorblind-safe palette, 10 slots (1 per stock).
    _OI_COLORS = [
        "#0072B2",
        "#E69F00",
        "#56B4E9",
        "#009E73",
        "#F0E442",
        "#D55E00",
        "#CC79A7",
        "#999999",
        "#000000",
        "#7F7F7F",
    ]

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def render_comparison_chart(
        codes: list[str],
        from_date: str,
        to_date: str,
        mode: str = "return_pct",
        style: str = "default",
    ) -> Image:
        """Render a multi-stock performance comparison line chart as PNG (複数銘柄比較チャート).

        Plots up to 10 stocks on the same axis so relative performance is visible at a
        glance. Reads adjusted-close prices from the local Tier 1 cache (no API call).
        The image is returned inline; Claude Desktop and Claude mobile display it directly
        in chat.

        Use for 比較チャート, パフォーマンス比較, 複数銘柄比較, comparison chart,
        relative performance, return chart, リターン比較.

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily Tier 1 cache (no API call)
        [Optional dependency] ``mplfinance`` + ``matplotlib``

        **Call sequentially when rendering multiple charts** (not in parallel).

        Args:
            codes: List of 1–10 stock codes (4- or 5-digit, e.g. ["72030", "86970"]).
            from_date: Range start (YYYYMMDD or YYYY-MM-DD), inclusive.
            to_date: Range end (YYYYMMDD or YYYY-MM-DD), inclusive.
            mode: ``return_pct`` (default) — normalise each stock to 0 % at its first
                available bar so performance is directly comparable. ``price`` — plot raw
                adjusted-close prices without normalisation.
            style: ``default``, ``dark``, or ``colorblind`` (Okabe-Ito palette).
        """
        if not codes or len(codes) > 10:
            return _error_image("codes must be a list of 1–10 stock codes.")

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
            return _error_image("; ".join(all_errors))

        if mode not in ("return_pct", "price"):
            return _error_image(f"Unknown mode: {mode!r}. Accepted: 'return_pct', 'price'")
        if style not in _STYLE_ALIASES:
            return _error_image(f"Unknown style: {style!r}. Accepted: {sorted(_STYLE_ALIASES)}")

        norm_from = _normalize_date(from_date)
        norm_to = _normalize_date(to_date)
        if norm_from > norm_to:
            return _error_image("`from_date` must be <= `to_date`.")

        cache: CacheStore = get_cache()

        series_map: dict[str, pd.Series] = {}
        for code in codes:
            norm_code = _normalize_code(code)
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
                return _error_image(err.get("message") or "API error")

            records: dict = {}
            for r in rows:
                try:
                    d = pd.to_datetime(r["Date"])
                    adj_c = r.get("AdjC")
                    raw_c = r.get("C")
                    val = float(adj_c if adj_c not in (None, "") else raw_c)
                    records[d] = val
                except (KeyError, TypeError, ValueError):
                    continue

            if not records:
                logger.debug("render_comparison_chart: no bars for %s", norm_code)
                continue

            company = _get_company_name(cache, norm_code)
            label = _display_code(norm_code)
            if company:
                label = f"{label} {company}"
            series_map[label] = pd.Series(records).sort_index()

        if not series_map:
            return _error_image(f"No cached bars found for any code in {norm_from}..{norm_to}.")

        df = pd.DataFrame(series_map).sort_index()

        if mode == "return_pct":
            # bfill so a stock that starts mid-window (late IPO) uses its
            # own first real bar as baseline rather than giving a NaN row.
            baseline = df.bfill().iloc[0]
            df = df.div(baseline).sub(1).mul(100)

        comp_buf = io.BytesIO()
        try:
            mpl_style = "dark_background" if style == "dark" else "default"
            with plt.style.context(mpl_style), plt.rc_context(_CJK_RC):
                fig, ax = plt.subplots(figsize=(_FIG_WIDTH, _FIG_HEIGHT), dpi=_DPI)
                try:
                    if style == "colorblind":
                        ax.set_prop_cycle(color=_OI_COLORS)
                    # Plot on integer index so non-trading days (weekends /
                    # holidays / long holidays like GW) produce no gap in
                    # the line — matplotlib treats DatetimeIndex as a
                    # continuous time axis and leaves blank spans for missing
                    # dates, which causes visible line breaks.
                    date_index = df.index
                    df_int = df.copy()
                    df_int.index = range(len(df_int))
                    df_int.plot(ax=ax, linewidth=1.5)
                    # Replace auto integer ticks with readable date labels.
                    n = len(date_index)
                    tick_every = max(1, n // 8)
                    tick_pos = list(range(0, n, tick_every))
                    if tick_pos[-1] != n - 1:
                        tick_pos.append(n - 1)
                    ax.set_xticks(tick_pos)
                    ax.set_xticklabels(
                        [date_index[i].strftime("%Y-%m-%d") for i in tick_pos],
                        rotation=30,
                        ha="right",
                    )
                    ax.set_title(f"Comparison {norm_from} → {norm_to}", pad=10)
                    if mode == "return_pct":
                        ax.set_ylabel("Return (%)")
                        ax.axhline(0, color="gray", linewidth=0.8, linestyle="--", alpha=0.7)
                    else:
                        ax.set_ylabel("Price (adjusted close)")
                    ax.legend(loc="best", fontsize=8)
                    ax.grid(True, alpha=0.3)
                    fig.tight_layout()
                    fig.savefig(comp_buf, dpi=_DPI, format="png")
                finally:
                    plt.close(fig)
        except Exception as exc:
            logger.warning("render_comparison_chart: rendering failed: %s", exc)
            return _error_image(f"Chart rendering failed: {exc}")

        return Image(data=comp_buf.getvalue(), format="png")


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

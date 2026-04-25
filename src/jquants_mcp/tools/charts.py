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

    Historically JP stock codes were 4 digits. J-Quants moved to a
    5-digit form (the 5th digit is ``0`` for ordinary shares,
    non-zero for preferred / second-class shares). Most users still
    think of "Toyota" as ``7203`` (not ``72030``), so collapse the
    trailing ``0`` for ordinary shares; keep 5 digits when the suffix
    encodes a non-ordinary share class.

    Examples:
        ``"7203"`` → ``"7203"`` (already 4-digit)
        ``"72030"`` → ``"7203"`` (5-digit ordinary share)
        ``"25935"`` → ``"25935"`` (5-digit non-ordinary, suffix ≠ 0)
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


def _build_chart_title(
    code: str, company: str | None, norm_from: str, norm_to: str, adjusted: bool
) -> str:
    """Compose the chart title used by ``mpf.plot``.

    Extracted so the title format can be unit-tested without spinning
    up matplotlib. Format: ``CODE [COMPANY ]FROM → TO (adjusted|raw)``.
    """
    name_part = f" {company}" if company else ""
    return f"{code}{name_part} {norm_from} → {norm_to} ({'adjusted' if adjusted else 'raw'})"


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
        """Render an OHLC candlestick chart as a PNG.

        Reads daily bars from the local cache and draws a chart with
        optional moving-average / volume / Bollinger-band overlays.
        The image is returned inline; Claude Desktop and Claude mobile
        display it directly in chat.

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
                ``bb20`` (20-session Bollinger band).
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
        title = _build_chart_title(_display_code(norm_code), company, norm_from, norm_to, adjusted)

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
                plt.close(fig)
            else:
                plot_kwargs["savefig"] = {"fname": buf, "dpi": _DPI, "format": "png"}
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

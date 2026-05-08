"""Single-stock summary tool for jquants-mcp."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, make_cache_key
from ..tool_annotations import READ_ONLY_CACHE
from ..validators import (
    collect_errors,
    display_code,
    float_or_none,
    make_validation_error_response,
    validate_code,
)
from .financials import _annotate_fiscal_period, _apply_split_adjustment

logger = logging.getLogger(__name__)


def _api_code(code: str) -> str:
    """Return the 5-digit J-Quants API form of a stock code.

    J-Quants stores ordinary shares as 5-digit codes (e.g. ``"13010"``).
    A 4-digit input is the display/user form; pad with ``"0"`` to get the
    cache key.
    """
    return code + "0" if len(code) == 4 else code


# DivAnn disclosures older than this are treated as stale (no-dividend transition).
# Mirrors the default used by get_dividend_yield_ranking (disc_months=18).
# Uses 31 days/month intentionally: slightly over-estimates so borderline
# disclosures are consistently excluded rather than flickering in/out.
_DISC_MONTHS_DEFAULT = 18
_DISC_DAYS_CUTOFF = _DISC_MONTHS_DEFAULT * 31


def register(
    mcp: FastMCP,
    get_client: callable,  # not used: this tool is cache-only, no live API call
    get_cache: callable,
) -> None:
    """Register stock summary tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_stock_summary(code: str) -> dict[str, Any]:
        """One-page summary for a single stock: price, financials, and valuation (株式サマリー).

        Returns the latest price (close, change_pct, volume), most recent FY financial
        metrics (revenue, operating profit, net income), and valuation ratios (PER, PBR,
        dividend yield).  All figures use split-adjusted values (AdjC, AdjEPS, AdjBPS)
        so PER/PBR remain accurate even after stock splits.

        PER and ROE are null when EPS <= 0 (net-loss period).  Dividend yield uses the
        most recent annual dividend (DivAnn) disclosed within the past 18 months; null
        when no recent disclosure exists (company stopped paying dividends).

        [Supported plans] Free / Light / Standard / Premium (cache-only, no live API call)

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only)
        """
        errors = collect_errors(validate_code(code))
        if errors:
            return make_validation_error_response(errors)

        # J-Quants stores 5-digit codes in the cache.  Display code is 4-digit
        # for ordinary shares (trailing "0" stripped).
        cache_code = _api_code(code)
        out_code = display_code(cache_code)
        cache: CacheStore = get_cache()

        cache_key = make_cache_key("get_stock_summary", {"code": out_code})
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        # --- 1. Name / sector / market from equities_master -------------------------
        name_map = cache.get_name_map()
        sector_map = cache.get_sector_map()
        name = name_map.get(cache_code, "")
        sector_info = sector_map.get(cache_code, {})

        # --- 2. Latest price bars (2 bars: today + prev for change_pct) -------------
        bars = cache.get_latest_bars(cache_code, n=2)
        if not bars:
            return {"error": f"No price data cached for {out_code}. Run daily_fetch first."}

        latest = bars[0]
        price_date = str(latest.get("Date") or "")
        adj_close = float_or_none(latest.get("AdjC") or latest.get("C"))
        adj_open = float_or_none(latest.get("AdjO") or latest.get("O"))
        adj_high = float_or_none(latest.get("AdjH") or latest.get("H"))
        adj_low = float_or_none(latest.get("AdjL") or latest.get("L"))
        volume = float_or_none(latest.get("Vo"))

        change_pct: float | None = None
        if len(bars) >= 2:
            prev_adj_close = float_or_none(bars[1].get("AdjC") or bars[1].get("C"))
            if adj_close is not None and prev_adj_close and prev_adj_close != 0:
                change_pct = round((adj_close - prev_adj_close) / prev_adj_close * 100, 2)

        # --- 3. Latest FY financials ------------------------------------------------
        fins_row = cache.get_latest_fins_row(cache_code)

        per: float | None = None
        pbr: float | None = None
        roe: float | None = None
        revenue: float | None = None
        op_profit: float | None = None
        ord_profit: float | None = None
        net_income: float | None = None
        eps: float | None = None
        bps: float | None = None
        div_per_share: float | None = None
        fiscal_period: str | None = None
        fiscal_year_end: str | None = None
        fins_disc_date: str | None = None

        if fins_row:
            adjusted, _ = _apply_split_adjustment([fins_row], cache)
            _annotate_fiscal_period(adjusted)
            row = adjusted[0]

            fiscal_period = row.get("FiscalPeriod")
            fiscal_year_end = str(row.get("FiscalYearEndDate") or "")
            fins_disc_date = str(row.get("DiscDate") or row.get("disc_date") or "")

            revenue = float_or_none(row.get("NetSales"))
            op_profit = float_or_none(row.get("OperatingProfit"))
            ord_profit = float_or_none(row.get("OrdinaryProfit"))
            net_income = float_or_none(row.get("Profit"))

            # Prefer Adj variants: _apply_split_adjustment fills AdjEPS/AdjBPS/AdjDivAnn
            eps = float_or_none(row.get("AdjEPS") or row.get("EPS"))
            bps = float_or_none(row.get("AdjBPS") or row.get("BPS"))

            # DivAnn staleness filter — reject disclosures older than 18 months
            div_raw = float_or_none(row.get("AdjDivAnn") or row.get("DivAnn"))
            if div_raw and fins_disc_date:
                cutoff = (datetime.now() - timedelta(days=_DISC_DAYS_CUTOFF)).strftime("%Y-%m-%d")
                if fins_disc_date >= cutoff:
                    div_per_share = div_raw

            # PER: null when EPS <= 0 (net-loss) — negative PER is meaningless
            if adj_close and eps and eps > 0:
                per = round(adj_close / eps, 2)

            if adj_close and bps and bps > 0:
                pbr = round(adj_close / bps, 2)

            # ROE: split factor cancels in the ratio (AdjEPS / AdjBPS = EPS_raw / BPS_raw)
            if eps is not None and bps is not None and bps > 0 and eps > 0:
                roe = round(eps / bps * 100, 2)

        # --- 4. Dividend yield -------------------------------------------------------
        div_yield: float | None = None
        if div_per_share and adj_close and adj_close > 0:
            div_yield = round(div_per_share / adj_close * 100, 2)

        result: dict[str, Any] = {
            "code": out_code,
            "name": name,
            "market": sector_info.get("mkt_name", ""),
            "sector_17": sector_info.get("s17_name", ""),
            "sector_33": sector_info.get("s33_name", ""),
            "price": {
                "date": price_date,
                "close": adj_close,
                "open": adj_open,
                "high": adj_high,
                "low": adj_low,
                "volume": volume,
                "change_pct": change_pct,
            },
            "financials": {
                "fiscal_period": fiscal_period,
                "fiscal_year_end": fiscal_year_end,
                "disclosed_date": fins_disc_date,
                "revenue": revenue,
                "operating_profit": op_profit,
                "ordinary_profit": ord_profit,
                "net_income": net_income,
            },
            "valuation": {
                "per": per,
                "pbr": pbr,
                "roe": roe,
                "eps": eps,
                "bps": bps,
                "div_per_share": div_per_share,
                "dividend_yield_pct": div_yield,
            },
        }

        cache.put_response(cache_key, result, ttl_seconds=3600)
        return result

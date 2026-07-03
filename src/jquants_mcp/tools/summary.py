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
    normalize_code,
    validate_code,
)
from .financials import _annotate_fiscal_period, _apply_split_adjustment
from .market_overview import _calc_short_ratio

logger = logging.getLogger(__name__)


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
    """Register stock briefing tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_stock_briefing(code: str) -> dict[str, Any]:
        """One-page briefing for a single stock: price, financials, valuation, and margin (株式ブリーフィング). All plans.

        Returns latest price, FY financials, PER/PBR/dividend yield, margin ratio, and
        sector short-sale ratio. PER/ROE null when EPS≤0. Margin fields null without
        Standard/Premium cache. See also get_sector_briefing, get_market_briefing.

        [Supported plans] Free / Light / Standard / Premium (cache-only, no live API call)

        Args:
            code: Stock code (5 digits, e.g. 27800; 4-digit codes match ordinary shares only).
        """
        errors = collect_errors(validate_code(code))
        if errors:
            return make_validation_error_response(errors)

        # J-Quants stores 5-digit codes in the cache.  Display code is 4-digit
        # for ordinary shares (trailing "0" stripped).
        cache_code = normalize_code(code)
        out_code = display_code(cache_code)
        cache: CacheStore = get_cache()

        # `plan` is part of the key so a briefing computed under one user's
        # plan is never served to a different plan's embargo window.
        cache_key = make_cache_key(
            "get_stock_briefing", {"code": out_code, "plan": cache.effective_plan()}
        )
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
            fiscal_year_end = str(row.get("CurFYEn") or row.get("FiscalYearEndDate") or "")[:10]
            fins_disc_date = str(row.get("DiscDate") or row.get("disc_date") or "")[:10]

            revenue = float_or_none(row["Sales"] if "Sales" in row else row.get("NetSales"))
            op_profit = float_or_none(row["OP"] if "OP" in row else row.get("OperatingProfit"))
            ord_profit = float_or_none(row["OdP"] if "OdP" in row else row.get("OrdinaryProfit"))
            net_income = float_or_none(row["NP"] if "NP" in row else row.get("Profit"))

            # Prefer Adj variants: _apply_split_adjustment fills AdjEPS/AdjBPS/AdjDivAnn
            eps = float_or_none(row.get("AdjEPS") or row.get("EPS"))
            bps = float_or_none(row.get("AdjBPS") or row.get("BPS"))

            # Dividend: forward (FDivAnn/NxFDivAnn) takes priority over trailing (DivAnn).
            # Forward values are already in post-split terms; no FYE correction needed.
            # Trailing DivAnn may be pre-split at FYE; requires FYE correction.
            cutoff = (datetime.now() - timedelta(days=_DISC_DAYS_CUTOFF)).strftime("%Y-%m-%d")
            fwd_entry = cache.get_forward_div_ann_map().get(cache_code)
            if fwd_entry is not None:
                fwd_val, fwd_disc = fwd_entry
                if fwd_disc >= cutoff:
                    fwd_split = cache.get_split_factors_after({cache_code: fwd_disc[:10]})
                    div_per_share = fwd_val * fwd_split.get(cache_code, 1.0)
            if div_per_share is None and fins_disc_date:
                # Trailing DivAnn fallback
                div_raw = float_or_none(row.get("AdjDivAnn") or row.get("DivAnn"))
                if div_raw and fins_disc_date >= cutoff:
                    fye_factors = cache.get_split_factors_before_disc(
                        {cache_code: fins_disc_date[:10]}
                    )
                    div_per_share = div_raw * fye_factors.get(cache_code, 1.0)

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
        if div_per_share is not None and adj_close and adj_close > 0:
            div_yield = round(div_per_share / adj_close * 100, 2)

        # --- 5. Margin interest ---------------------------------------------------
        margin_ratio: float | None = None
        margin_long_vol: float | None = None
        margin_short_vol: float | None = None
        margin_date: str | None = None

        margin_row = cache.get_latest_margin_interest_row(cache_code)
        if margin_row:
            margin_date = str(margin_row.get("Date") or "")
            long_v = float_or_none(margin_row.get("LongVol"))
            short_v = float_or_none(margin_row.get("ShrtVol"))
            margin_long_vol = long_v
            margin_short_vol = short_v
            if short_v is not None and short_v > 0 and long_v is not None:
                margin_ratio = round(long_v / short_v, 2)

        # --- 6. Sector short-sale ratio ----------------------------
        sector_short_sale_ratio: float | None = None
        sector_short_ratio_date: str | None = None
        s33_code = sector_info.get("s33", "")
        if s33_code:
            sr_row = cache.get_latest_short_ratio_row(s33_code)
            if sr_row:
                ratio = _calc_short_ratio(sr_row)
                if ratio is None:
                    logger.warning(
                        "short_ratio fields missing or zero for s33=%s — API field names may have changed",
                        s33_code,
                    )
                else:
                    sector_short_sale_ratio = round(ratio, 2)
                sector_short_ratio_date = str(sr_row.get("Date") or "")

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
            "margin": {
                "date": margin_date,
                "ratio": margin_ratio,
                "long_vol": margin_long_vol,
                "short_vol": margin_short_vol,
                "sector_short_sale_ratio": sector_short_sale_ratio,
                "sector_short_ratio_date": sector_short_ratio_date,
            },
        }

        cache.put_response(cache_key, result, ttl_seconds=3600)
        return result

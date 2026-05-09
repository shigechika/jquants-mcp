"""Sector-level valuation aggregation tool for jquants-mcp."""

from __future__ import annotations

import logging
import statistics
from typing import Any

from fastmcp import FastMCP

from ..cache.store import CacheStore, TTL_6H, make_cache_key
from ..tool_annotations import READ_ONLY_CACHE
from ..validators import float_or_none, make_validation_error_response

logger = logging.getLogger(__name__)


def register(
    mcp: FastMCP,
    get_client: callable,  # not used: cache-only tool
    get_cache: callable,
) -> None:
    """Register sector briefing tools on the MCP server."""

    @mcp.tool(annotations=READ_ONLY_CACHE)
    async def get_sector_briefing(
        sector_type: str = "s33",
    ) -> dict[str, Any]:
        """Return sector-level median PER, PBR, ROE, and margin ratio aggregated across all listed stocks (業種別ブリーフィング).

        Use for セクターバリュエーション, 業種別PER, 業種別PBR, 割安セクター, セクター比較,
        業種別ブリーフィング, 業種別信用倍率.
        Aggregates PER (price/earnings), PBR (price/book), ROE (return on equity), and
        margin ratio (LongVol/ShrtVol) at the TSE sector level using the most recent
        full-year (FY) financial disclosures and margin interest data.
        All metrics use split-adjusted values so a 1:2 stock split does not distort ratios.

        PER and ROE exclude stocks in a net-loss period (EPS ≤ 0); PBR excludes
        negative-book stocks; margin ratio excludes stocks with ShrtVol == 0.
        ``per_count`` / ``pbr_count`` / ``roe_count`` / ``margin_ratio_count`` report
        how many stocks contributed to each median, letting you judge sector coverage.
        Margin data requires markets_margin_interest cache (populated by daily_fetch.py
        for Standard/Premium plans); margin fields are null when not cached.

        See also: ``get_market_briefing`` for market-wide overview,
        ``get_stock_briefing`` for single-stock detail,
        ``get_sector_performance`` for sector-level daily price change (騰落率).

        [Supported plans] Free / Light / Standard / Premium
        [Source] equities_bars_daily + fins_summary + equities_master + markets_margin_interest
        Tier 1 cache (no API call)

        Args:
            sector_type: ``"s33"`` (default, 33 TSE sub-sectors) or ``"s17"`` (17 top-level sectors).

        Returns:
            dict with keys:
            - price_date: latest close-price date used for PER/PBR
            - sector_type: ``"s33"`` or ``"s17"``
            - sectors: list sorted by ``per_median`` ascending (cheapest first), each with:
                - code: sector code
                - name: sector name
                - count: total stocks with both price and FY financials
                - per_median: median PER (null when no stocks have positive EPS)
                - per_count: stocks contributing to per_median
                - pbr_median: median PBR (null when no stocks have positive BPS)
                - pbr_count: stocks contributing to pbr_median
                - roe_median: median ROE in percent (null when no stocks have positive BPS)
                - roe_count: stocks contributing to roe_median
                - margin_ratio_median: median margin ratio LongVol/ShrtVol (null when not cached)
                - margin_ratio_count: stocks contributing to margin_ratio_median
        """
        if sector_type not in ("s33", "s17"):
            return make_validation_error_response(["sector_type must be 's33' or 's17'"])

        cache: CacheStore = get_cache()

        cache_key = make_cache_key("get_sector_briefing", {"sector_type": sector_type})
        cached = cache.get_response(cache_key)
        if cached is not None:
            return cached

        # Latest close prices (determines price_date shown in output)
        close_map = cache.get_latest_close_map()
        if not close_map:
            return {"error": "No price data cached. Run daily_fetch first."}

        price_date = cache.get_latest_equities_date() or ""

        # Sector membership
        sector_map = cache.get_sector_map()

        # Latest FY fins for all codes
        fins_map = cache.get_all_latest_fy_fins()
        if not fins_map:
            return {"error": "No financial data cached. Run daily_fetch first."}

        # Latest margin interest for all codes (optional — empty dict if not cached)
        margin_map = cache.get_all_latest_margin_interest()

        # Batch split-factor lookup: disc_date per code
        code_disc_dates: dict[str, str] = {}
        for code, row in fins_map.items():
            disc_date = str(row.get("DiscDate") or row.get("disc_date") or "")
            if disc_date:
                code_disc_dates[code] = disc_date
        split_factors = cache.get_split_factors_after(code_disc_dates)

        sec_code_key = sector_type  # "s33" or "s17"
        sec_name_key = f"{sector_type}_name"

        # Bucket per-stock metrics by sector
        buckets: dict[str, dict[str, Any]] = {}

        for code, fins_row in fins_map.items():
            close = close_map.get(code)
            if close is None:
                continue

            sec_info = sector_map.get(code, {})
            sec_code = sec_info.get(sec_code_key, "")
            sec_name = sec_info.get(sec_name_key, "")
            if not sec_code:
                continue

            if sec_code not in buckets:
                buckets[sec_code] = {
                    "name": sec_name,
                    "count": 0,
                    "pers": [],
                    "pbrs": [],
                    "roes": [],
                    "margin_ratios": [],
                }
            buckets[sec_code]["count"] += 1

            factor = split_factors.get(code, 1.0)

            eps_raw = float_or_none(fins_row.get("EPS"))
            bps_raw = float_or_none(fins_row.get("BPS"))

            eps_adj = eps_raw * factor if eps_raw is not None else None
            bps_adj = bps_raw * factor if bps_raw is not None else None

            if eps_adj is not None and eps_adj > 0:
                buckets[sec_code]["pers"].append(close / eps_adj)
            if bps_adj is not None and bps_adj > 0:
                buckets[sec_code]["pbrs"].append(close / bps_adj)
            # ROE = EPS / BPS — split factor cancels, so use raw values
            if eps_raw is not None and bps_raw is not None and bps_raw > 0:
                buckets[sec_code]["roes"].append(eps_raw / bps_raw * 100)

            margin_row = margin_map.get(code)
            if margin_row:
                long_v = float_or_none(margin_row.get("LongVol"))
                short_v = float_or_none(margin_row.get("ShrtVol"))
                if long_v is not None and short_v is not None and short_v > 0:
                    buckets[sec_code]["margin_ratios"].append(long_v / short_v)

        # Build sector list; sort by per_median ascending (cheapest first),
        # with null-PER sectors pushed to the end.
        sectors = []
        for sec_code, bucket in buckets.items():
            pers = bucket["pers"]
            pbrs = bucket["pbrs"]
            roes = bucket["roes"]
            margin_ratios = bucket["margin_ratios"]
            per_med = round(statistics.median(pers), 2) if pers else None
            pbr_med = round(statistics.median(pbrs), 2) if pbrs else None
            roe_med = round(statistics.median(roes), 2) if roes else None
            margin_ratio_med = round(statistics.median(margin_ratios), 2) if margin_ratios else None
            sectors.append(
                {
                    "code": sec_code,
                    "name": bucket["name"],
                    "count": bucket["count"],
                    "per_median": per_med,
                    "per_count": len(pers),
                    "pbr_median": pbr_med,
                    "pbr_count": len(pbrs),
                    "roe_median": roe_med,
                    "roe_count": len(roes),
                    "margin_ratio_median": margin_ratio_med,
                    "margin_ratio_count": len(margin_ratios),
                }
            )

        sectors.sort(key=lambda x: (x["per_median"] is None, x["per_median"] or 0))

        result: dict[str, Any] = {
            "price_date": price_date,
            "sector_type": sector_type,
            "sectors": sectors,
        }
        cache.put_response(cache_key, result, ttl_seconds=TTL_6H)
        return result

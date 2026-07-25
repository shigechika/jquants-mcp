"""Probe specs for this server's tools — the jquants-specific half of the smoke test.

Every registered tool needs an entry here (the harness fails on a tool with no
spec), so adding a tool forces a decision: how would we know it works?

Conventions:

* Date arguments use the harness tokens — ``{t}`` is the reference business day,
  ``{t-N}`` is N calendar days earlier — never a hardcoded date, which would rot
  into "tested nothing" once it ages out of cache.
* ``allow_plan_restriction`` marks endpoints above the current subscription: the
  tool passes when it either serves data (cache may hold it) or returns the
  explicit plan-restriction error. Both prove the tool is wired correctly.
* ``allow_empty`` marks detectors and screens whose empty answer is a real
  market observation, not a malfunction.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from smoke_harness import Caller, Probe, SkipProbe

# Liquid, long-listed stocks: their data exists for every historical window,
# so an empty answer means the tool is broken rather than the code being odd.
TOYOTA = "72030"
JPX = "86970"
SUMITOMO_MITSUI = "80530"

#: Cross-sectional tools scan the whole listed universe (~4,000 names). A
#: handful of rows means the underlying day is barely populated, which reads as
#: success to a naive "did it return anything?" check — the exact blind spot
#: this smoke test exists to close.
UNIVERSE_MIN = 100


async def reference_date(call: Caller) -> str | None:
    """Latest exchange business day strictly before today, from the calendar tool.

    Asking the server keeps the reference honest across holidays. Returning None
    lets the harness fall back to plain weekday arithmetic.
    """
    payload = await call("get_markets_calendar", {"date_from": "{t-21}", "date_to": "{today}"})
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    rows: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            rows = value
            break
    today = date.today().isoformat()
    business_days = sorted(
        day
        for row in rows
        # HolDiv "1" = normal business day; "0" is a market holiday.
        if str(row.get("HolDiv", "")) == "1" and (day := str(row.get("Date", ""))[:10]) < today
    )
    return business_days[-1] if business_days else None


async def _first_bulk_key(call: Caller) -> dict[str, Any]:
    """Feed get_bulk_download_url a real key produced by get_bulk_list."""
    payload = await call("get_bulk_list", {"endpoint": "/equities/master"})
    if isinstance(payload, dict) and payload.get("error"):
        raise SkipProbe(f"get_bulk_list unavailable: {payload.get('error_type', 'error')}")
    for value in (payload or {}).values() if isinstance(payload, dict) else []:
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict) and row.get("Key"):
                    return {"key": row["Key"]}
    raise SkipProbe("get_bulk_list returned no downloadable key")


PROBES: dict[str, Probe] = {
    # -- server-local utilities ------------------------------------------
    "health_check": Probe(require_keys=("plan",), allow_empty=True),
    "cache_status": Probe(allow_empty=True),
    "cache_clear": Probe(skip="destructive: would drop cached tables"),
    "register_api_key": Probe(skip="destructive: would overwrite the stored API key"),
    "delete_api_key": Probe(skip="destructive: would remove the stored API key"),
    # -- equities ---------------------------------------------------------
    "get_equities_master": Probe(args={"code": TOYOTA}),
    "get_equities_bars_daily": Probe(
        args={"code": TOYOTA, "date_from": "{t-30}", "date_to": "{t}"},
        date_field="Date",
        fresh_within_days=7,
    ),
    "get_equities_bars_daily_am": Probe(args={"code": TOYOTA}, allow_plan_restriction=True),
    "get_equities_bars_minute": Probe(
        args={"code": TOYOTA, "date": "{t}"}, allow_plan_restriction=True
    ),
    "get_equities_investor_types": Probe(args={"date_from": "{t-30}", "date_to": "{t}"}),
    # The regression probe for the incident where every earnings query returned
    # a well-formed empty result: the calendar must reach today or beyond, not
    # merely contain rows.
    "get_equities_earnings_calendar": Probe(date_field="Date", fresh_within_days=0),
    "get_earnings_this_week": Probe(allow_empty=True),
    "get_earnings_results_this_week": Probe(allow_empty=True),
    "search_equities": Probe(args={"name": "トヨタ"}),
    # -- financials -------------------------------------------------------
    "get_fins_summary": Probe(args={"code": TOYOTA}),
    "get_fins_details": Probe(args={"code": TOYOTA}, allow_plan_restriction=True),
    "get_fins_dividend": Probe(args={"code": TOYOTA}, allow_plan_restriction=True),
    # -- indices ----------------------------------------------------------
    "get_indices_bars_daily_topix": Probe(
        args={"date_from": "{t-30}", "date_to": "{t}"}, date_field="Date", fresh_within_days=7
    ),
    "get_indices_bars_daily": Probe(
        args={"code": "0000", "date_from": "{t-30}", "date_to": "{t}"},
        allow_plan_restriction=True,
    ),
    # -- markets ----------------------------------------------------------
    "get_markets_calendar": Probe(args={"date_from": "{t-21}", "date_to": "{today}"}),
    "get_markets_margin_interest": Probe(
        args={"code": TOYOTA, "date_from": "{t-60}", "date_to": "{t}"},
        allow_plan_restriction=True,
    ),
    "get_markets_margin_alert": Probe(
        args={"date_from": "{t-30}", "date_to": "{t}"}, allow_plan_restriction=True
    ),
    "get_markets_short_ratio": Probe(
        args={"date_from": "{t-30}", "date_to": "{t}"}, allow_plan_restriction=True
    ),
    "get_markets_short_sale_report": Probe(
        args={"code": TOYOTA, "disc_date_from": "{t-90}", "disc_date_to": "{t}"},
        allow_plan_restriction=True,
        allow_empty=True,
    ),
    "get_markets_breakdown": Probe(
        args={"code": TOYOTA, "date_from": "{t-30}", "date_to": "{t}"},
        allow_plan_restriction=True,
    ),
    # -- derivatives ------------------------------------------------------
    "get_derivatives_bars_daily_futures": Probe(args={"date": "{t}"}, allow_plan_restriction=True),
    "get_derivatives_bars_daily_options": Probe(args={"date": "{t}"}, allow_plan_restriction=True),
    "get_derivatives_bars_daily_options_225": Probe(
        args={"date": "{t}"}, allow_plan_restriction=True
    ),
    # -- bulk -------------------------------------------------------------
    "get_bulk_list": Probe(args={"endpoint": "/equities/master"}, allow_plan_restriction=True),
    "get_bulk_download_url": Probe(args_factory=_first_bulk_key, allow_empty=True),
    # -- screener / detectors --------------------------------------------
    # Detectors answer "did this happen today?"; an empty answer is a market
    # observation, so they assert the envelope instead of row counts.
    "detect_52w_high_low": Probe(args={"date": "{t}"}, allow_empty=True),
    "detect_52w_high_low_range": Probe(
        args={"date_from": "{t-14}", "date_to": "{t}"}, allow_empty=True
    ),
    "detect_ytd_high_low": Probe(args={"date": "{t}"}, allow_empty=True),
    "detect_ytd_high_low_range": Probe(
        args={"date_from": "{t-14}", "date_to": "{t}"}, allow_empty=True
    ),
    # Returns a market-wide summary rather than rows, so assert the universe it
    # was computed over: "advances 0 / declines 1" is well-formed and useless.
    "detect_price_change": Probe(
        args={"date": "{t}"},
        allow_empty=True,
        require_keys=("advances", "declines", "total"),
        min_values={"total": UNIVERSE_MIN},
    ),
    "detect_price_limit": Probe(args={"date": "{t}"}, allow_empty=True),
    "detect_volume_surge": Probe(args={"date": "{t}"}, allow_empty=True, timeout=300),
    "detect_distribution_days": Probe(args={"date": "{t}"}, allow_empty=True, timeout=300),
    "detect_follow_through_day": Probe(
        args={"rally_start": "{t-60}", "date": "{t}"}, allow_empty=True, timeout=300
    ),
    "detect_consecutive_dividend_increase": Probe(
        args={"min_years": 5}, allow_empty=True, timeout=300
    ),
    "get_value_stock_screen": Probe(allow_empty=True, timeout=300),
    "get_dividend_yield_ranking": Probe(args={"n": 10}, min_rows=10, timeout=300),
    "get_valuation_ranking": Probe(args={"n": 10}, min_rows=10, timeout=300),
    # -- market overview --------------------------------------------------
    # Ask for a fixed N and require it: a short list means a thin trading day in
    # the cache, not a quiet market.
    "get_advance_decline_ratio": Probe(args={"date": "{t}"}, allow_empty=True, timeout=300),
    "get_top_movers": Probe(args={"date": "{t}", "n": 20}, min_rows=20, timeout=300),
    "get_top_volume": Probe(args={"date": "{t}", "n": 20}, min_rows=20, timeout=300),
    "get_top_turnover_value": Probe(args={"date": "{t}", "n": 20}, min_rows=20, timeout=300),
    "get_sector_performance": Probe(args={"date": "{t}"}, min_rows=17, timeout=300),
    # -- briefings (composite readers) ------------------------------------
    "get_market_briefing": Probe(args={"date": "{t}"}, min_rows=17, timeout=300),
    "get_sector_briefing": Probe(min_rows=17, timeout=300),
    "get_stock_briefing": Probe(args={"code": TOYOTA}, allow_empty=True, timeout=300),
    # -- technical / charts -----------------------------------------------
    "get_technical_indicators": Probe(
        args={"code": TOYOTA, "date_from": "{t-90}"}, min_rows=20, timeout=300
    ),
    "compare_close_vs_vwap": Probe(args={"code": TOYOTA, "date_from": "{t-30}", "date_to": "{t}"}),
    "get_candlestick_data": Probe(
        args={"code": TOYOTA, "from_date": "{t-90}", "to_date": "{t}"}, min_rows=20, timeout=300
    ),
    "get_comparison_chart_data": Probe(
        args={"codes": [TOYOTA, JPX, SUMITOMO_MITSUI], "from_date": "{t-90}", "to_date": "{t}"}
    ),
}

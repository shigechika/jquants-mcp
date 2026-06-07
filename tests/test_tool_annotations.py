"""Tests that every registered MCP tool advertises proper annotations.

Background: Claude Desktop / mobile use ``readOnlyHint`` /
``destructiveHint`` / ``openWorldHint`` to decide trust policy
(auto-approve vs. confirm). A tool with no annotations is treated as
"unknown safety" and the client picks a conservative default. This
test pins the policy explicitly so a newly-added tool does not slip
through unannotated.
"""

from __future__ import annotations

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.tool_annotations import (
    DESTRUCTIVE_LOCAL,
    READ_ONLY_API,
    READ_ONLY_CACHE,
    READ_ONLY_LOCAL,
)


# Expected annotation per tool. The mapping must cover every tool
# registered on the server. A new tool added without an entry here will
# fail ``test_every_registered_tool_is_in_the_expected_map`` below.
EXPECTED_ANNOTATIONS: dict[str, dict[str, bool]] = {
    # tools/equities.py — calls J-Quants API with cache fallback
    "get_equities_master": READ_ONLY_API,
    "get_equities_bars_daily": READ_ONLY_API,
    "get_equities_bars_minute": READ_ONLY_API,
    "get_equities_bars_daily_am": READ_ONLY_API,
    "get_equities_investor_types": READ_ONLY_API,
    "get_equities_earnings_calendar": READ_ONLY_API,
    "get_earnings_this_week": READ_ONLY_CACHE,
    "search_equities": READ_ONLY_CACHE,
    # tools/financials.py
    "get_fins_summary": READ_ONLY_API,
    "get_fins_details": READ_ONLY_API,
    "get_fins_dividend": READ_ONLY_API,
    "get_earnings_results_this_week": READ_ONLY_CACHE,
    # tools/indices.py
    "get_indices_bars_daily": READ_ONLY_API,
    "get_indices_bars_daily_topix": READ_ONLY_API,
    # tools/derivatives.py
    "get_derivatives_bars_daily_futures": READ_ONLY_API,
    "get_derivatives_bars_daily_options": READ_ONLY_API,
    "get_derivatives_bars_daily_options_225": READ_ONLY_API,
    # tools/markets.py
    "get_markets_margin_interest": READ_ONLY_API,
    "get_markets_margin_alert": READ_ONLY_API,
    "get_markets_short_ratio": READ_ONLY_API,
    "get_markets_short_sale_report": READ_ONLY_API,
    "get_markets_breakdown": READ_ONLY_API,
    "get_markets_calendar": READ_ONLY_API,
    # tools/bulk.py
    "get_bulk_list": READ_ONLY_API,
    "get_bulk_download_url": READ_ONLY_API,
    # tools/market_overview.py — cache only, no API
    "detect_price_change": READ_ONLY_CACHE,
    "get_advance_decline_ratio": READ_ONLY_CACHE,
    "get_top_movers": READ_ONLY_CACHE,
    "get_top_volume": READ_ONLY_CACHE,
    "get_top_turnover_value": READ_ONLY_CACHE,
    "get_sector_performance": READ_ONLY_CACHE,
    "get_dividend_yield_ranking": READ_ONLY_CACHE,
    "get_valuation_ranking": READ_ONLY_CACHE,
    "get_market_briefing": READ_ONLY_CACHE,
    # tools/screener.py
    "detect_price_limit": READ_ONLY_CACHE,
    "compare_close_vs_vwap": READ_ONLY_API,  # has API fallback for per-code cache miss
    "detect_52w_high_low": READ_ONLY_CACHE,
    "detect_ytd_high_low": READ_ONLY_CACHE,
    "detect_volume_surge": READ_ONLY_API,  # has API fallback when code is given
    "detect_52w_high_low_range": READ_ONLY_CACHE,
    "detect_ytd_high_low_range": READ_ONLY_CACHE,
    "detect_distribution_days": READ_ONLY_CACHE,
    "detect_follow_through_day": READ_ONLY_CACHE,
    "detect_consecutive_dividend_increase": READ_ONLY_CACHE,
    # tools/technical.py — API fallback on cache miss
    "get_technical_indicators": READ_ONLY_API,
    # tools/summary.py — cache only, no API
    "get_stock_briefing": READ_ONLY_CACHE,
    # tools/valuation.py — cache only, no API
    "get_sector_briefing": READ_ONLY_CACHE,
    # tools/charts.py — cache only
    "get_comparison_chart_data": READ_ONLY_CACHE,
    "get_candlestick_data": READ_ONLY_CACHE,
    # server.py utilities — pure server-local read
    "health_check": READ_ONLY_LOCAL,
    "cache_status": READ_ONLY_LOCAL,
    # server.py utilities — destructive
    "cache_clear": DESTRUCTIVE_LOCAL,
    "register_api_key": DESTRUCTIVE_LOCAL,
    "delete_api_key": DESTRUCTIVE_LOCAL,
}


async def _registered_tools() -> dict:
    """Return the FastMCP tool registry as ``{name: Tool}``.

    ``list_tools`` is the public FastMCP API (returns ``Sequence[Tool]``);
    we key by ``tool.name`` for direct lookup.
    """
    tools = await server_module.mcp.list_tools()
    return {tool.name: tool for tool in tools}


@pytest.mark.parametrize("name,expected", sorted(EXPECTED_ANNOTATIONS.items()))
async def test_tool_has_expected_annotations(name: str, expected: dict[str, bool]):
    tools = await _registered_tools()
    if name not in tools:
        pytest.fail(f"tool {name!r} not registered")
    tool = tools[name]
    annotations = tool.annotations
    assert annotations is not None, f"{name} has no annotations"
    # FastMCP normalises dict input into a ToolAnnotations model — compare
    # the relevant fields rather than the whole object. ``getattr`` is
    # necessary here because the field name is dynamic per loop iteration.
    for key, expected_value in expected.items():
        actual = getattr(annotations, key, None)
        assert actual == expected_value, f"{name}.{key} expected {expected_value!r}, got {actual!r}"


async def test_every_registered_tool_is_in_the_expected_map():
    """A new tool added without an EXPECTED_ANNOTATIONS entry fails here.

    Forces whoever adds a tool to think about the trust profile rather
    than letting it ship with the FastMCP / MCP-client default.
    """
    tools = await _registered_tools()
    registered = set(tools.keys())
    expected = set(EXPECTED_ANNOTATIONS.keys())
    extra = registered - expected
    assert not extra, (
        f"Tool(s) registered without an EXPECTED_ANNOTATIONS entry: {sorted(extra)}. "
        "Add an entry mapping the new tool to the right preset (READ_ONLY_API / "
        "READ_ONLY_CACHE / READ_ONLY_LOCAL / DESTRUCTIVE_LOCAL)."
    )

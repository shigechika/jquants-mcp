"""Drift guard for skills/jquants-mcp-usage/SKILL.md (issue #507).

The usage skill went stale once before (20 of 55 tools missing, including the
composite briefings). These tests keep it honest without requiring a full 1:1
enumeration:

- every tool-like name the skill mentions must be a registered MCP tool, so a
  rename or removal fails CI instead of leaving dead guidance behind;
- a curated set of behavior-critical tools (the composite briefings, the value
  screen, the ranking tools) must stay mentioned, so the skill keeps steering
  agents toward the one-call composites.

The token regex derives its verb-prefix set from the registered tool names, so
a future tool with a new prefix (e.g. ``list_``) is covered automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

import jquants_mcp.server as server_module

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "jquants-mcp-usage" / "SKILL.md"

# Identifier-looking tokens that share a verb prefix with tool names but are
# response fields, not tools (health_check's `cache_ready`; `today_cache_ready`
# never matches because the regex requires a word boundary before the prefix).
NON_TOOL_TOKENS = {"cache_ready"}

# Behavior-critical tools the skill must keep mentioning: without these an
# agent regresses to hand-assembling briefings from primitive tools.
MUST_MENTION = {
    "health_check",
    "get_market_briefing",
    "get_sector_briefing",
    "get_stock_briefing",
    "get_value_stock_screen",
    "get_dividend_yield_ranking",
    "get_valuation_ranking",
    "detect_distribution_days",
    "detect_follow_through_day",
    "search_equities",
    "get_technical_indicators",
    "cache_clear",
}


async def _registered_tool_names() -> set[str]:
    return {tool.name for tool in await server_module.mcp.list_tools()}


def _mentioned_tool_tokens(registered: set[str]) -> set[str]:
    prefixes = sorted({name.split("_", 1)[0] for name in registered if "_" in name})
    token_re = re.compile(r"\b(?:" + "|".join(prefixes) + r")_[a-z0-9_]+\b")
    return set(token_re.findall(SKILL_PATH.read_text(encoding="utf-8"))) - NON_TOOL_TOKENS


async def test_every_tool_mentioned_in_skill_is_registered():
    registered = await _registered_tool_names()
    unknown = _mentioned_tool_tokens(registered) - registered
    assert not unknown, (
        f"SKILL.md references tools that are not registered on the server: {sorted(unknown)}. "
        "Fix the skill (or NON_TOOL_TOKENS for non-tool identifiers)."
    )


async def test_behavior_critical_tools_are_mentioned():
    registered = await _registered_tool_names()
    stale_expectation = MUST_MENTION - registered
    assert not stale_expectation, (
        f"MUST_MENTION contains tools no longer registered: {sorted(stale_expectation)}"
    )
    missing = MUST_MENTION - _mentioned_tool_tokens(registered)
    assert not missing, f"SKILL.md no longer mentions behavior-critical tools: {sorted(missing)}"


def test_skill_frontmatter_is_well_formed():
    """`npx skills add` and skill discovery rely on the YAML frontmatter."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with a YAML frontmatter block"
    front = match.group(1)
    assert re.search(r'^name: "jquants-mcp-usage"$', front, re.MULTILINE)
    assert re.search(r"^description: \"[^\"]+\"$", front, re.MULTILINE)

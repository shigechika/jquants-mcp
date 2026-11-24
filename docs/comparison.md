# J-Quants MCP Server Comparison

Survey date: 2026-11-25.

On 2026-11-24, JPX Market Innovation & Research (JPXI) launched an official
**remote** MCP server for the J-Quants API at `https://mcp.jquants.com/mcp`.
This page compares it with jquants-mcp. Every statement about the official
server is taken from the official pages listed under [Sources](#sources);
anything those pages do not state is marked "not stated" rather than guessed.

## Choose in 30 seconds

- **Use the official remote server** if you want the shortest path: add one URL
  to Claude or ChatGPT, log in with your J-Quants account, done. No install, no
  hosting, run by the data provider itself.
- **Use jquants-mcp** if you want whole-market screening and rankings computed
  for you, a local cache that spares your API quota, an MCP client other than
  Claude / ChatGPT, or a server you can read, modify and run yourself.

The two are not mutually exclusive: both work with the same J-Quants account
and plan.

## Side-by-side

| | Official remote MCP server (JPXI) | **jquants-mcp** |
|---|---|---|
| Provider | JPXI (the data provider) | Individual open-source project (MIT) |
| Setup | Add the URL and log in ("about 5 minutes" per the docs) | `uvx jquants-mcp`, `pip install`, or the Claude Code plugin; remote access via a self-hosted gateway |
| Where it runs | JPXI's servers | Your machine, or a server you control |
| Transport | Streamable HTTP (remote) | stdio; remote via an `mcp-stdio` gateway in front of it |
| Supported clients | Claude (claude.ai / Desktop / Code) and ChatGPT only; the docs say other clients cannot connect | Any MCP client that speaks stdio (Claude Code, Claude Desktop, Codex, …); web clients such as claude.ai via a self-hosted gateway |
| ChatGPT | Requires a paid ChatGPT plan (custom MCP servers) | Not a target |
| Authentication | OAuth (J-Quants account login) only; API keys are not accepted | API key (env / config file / `jquants-mcp login` browser flow); gateway OAuth when self-hosted |
| Plans | All plans, including Free; data scope follows your plan | All plans, including Free; plan auto-detected from the key |
| Extra cost | None | None (you pay only for any server you choose to host) |
| API quota | MCP calls consume the same quota as REST calls | Rows already in the local cache are served without an API call |
| Large responses | Truncated at about 50 KB, with guidance to narrow the query or continue via `pagination_key` | Aggregation tools return compact, pre-computed results (see below) |
| Tool list | Not stated in the official docs | 55 tools, all documented in the [README](../README.md#available-tools) |
| Source code | Not stated | Public (MIT), 1,280+ tests, CI on Python 3.11–3.15 |

## What jquants-mcp adds on top of the raw API

The official docs describe the remote server as giving AI agents access to
J-Quants API data, with comparison, aggregation and summarisation done in the
conversation. They do not list any server-side screening or analytics tools.
jquants-mcp ships these as tools, so the work happens on the server, over the
whole market, before anything reaches the model's context:

| Category | jquants-mcp tools |
|---|---|
| Whole-market rankings | `get_top_movers`, `get_top_volume`, `get_top_turnover_value`, `get_dividend_yield_ranking`, `get_valuation_ranking` |
| Market breadth and sectors | `get_advance_decline_ratio`, `get_sector_performance`, `get_sector_briefing`, `get_market_briefing` |
| Screeners (computed from the cache, no API calls) | `detect_52w_high_low`, `detect_ytd_high_low`, `detect_volume_surge`, `detect_price_limit`, `compare_close_vs_vwap`, `detect_distribution_days`, `detect_follow_through_day`, `detect_consecutive_dividend_increase`, … |
| Value screen | `get_value_stock_screen` (valuation, dividend and margin ratio in one pass) |
| Single-stock digest | `get_stock_briefing` |
| Technical indicators | `get_technical_indicators` (SMA, Bollinger Bands, RSI) |
| Chart-ready data | `get_candlestick_data`, `get_comparison_chart_data` |
| Cache-backed lookups | equity name search; this week's earnings schedule and results |

Why this matters:

- **Quota.** A question like "which Prime stocks hit a 52-week high today?"
  needs a year of bars for every listed stock. Through a server that relays
  each call to the API, every one of those requests counts against your plan's
  rate limit (Free: 5 requests/min). jquants-mcp answers it from its SQLite
  cache.
- **Context size.** A 50 KB cap with pagination suits single-stock questions.
  Whole-market questions need the reduction done before the result reaches the
  model; jquants-mcp returns the ranked or filtered list, not the raw rows.
- **Corrected history.** jquants-mcp's cache detects retroactive `AdjFactor`
  changes (splits, consolidations) and refetches the affected rows.

## Where the official server is the better choice

- Zero install and zero maintenance; nothing runs on your machine.
- Run by the data provider, so new endpoints and data fixes arrive on its side.
- One login flow; no API key to copy around.
- ChatGPT support (on paid ChatGPT plans).

## Note on the earlier official local server

JPXI previously offered a local MCP server installed with `uvx`. With the
remote server's launch, the release notes say it will receive no further
features or fixes, and the press release says it will be withdrawn from GitHub
for new downloads; existing installs keep working. As of this survey the
[`J-Quants/j-quants-doc-mcp`](https://github.com/J-Quants/j-quants-doc-mcp)
repository (an API-documentation search server, not a data server) is still
public, and the official pages do not name the affected repository.

## Other community servers

| Project | Scope |
|---|---|
| [cygkichi/jquants-free-mcp-server](https://github.com/cygkichi/jquants-free-mcp-server) | 3 tools for the Free plan (company search, daily quotes, financial statements) |
| [umicho/j-quants-api-mcp](https://lobehub.com/mcp/umicho-j-quants-api-mcp) | V2 endpoint calls; listed on the LobeHub registry; source repository not found |

## Sources

Official server facts (read on 2026-11-25):

- J-Quants API Reference, "MCPサーバー":
  <https://jpx-jquants.com/ja/spec/mcp-server> (endpoint, OAuth-only
  authentication, supported clients, ~50 KB truncation, shared rate limit,
  no extra cost, setup steps)
- J-Quants API Reference, release notes (2026-11-24 entry):
  <https://jpx-jquants.com/ja/spec/release>
- JPX press release (2026-11-24):
  <https://www.jpx.co.jp/corporate/news/news-releases/6020/20261124-01.html>

jquants-mcp facts: this repository's README and test suite at the survey date.

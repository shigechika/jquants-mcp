# jquants-mcp

**Talk through Japanese stocks with Claude — backed by the [J-Quants API v2](https://jpx-jquants.com/).**

jquants-mcp is a Japanese-equities-focused [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
server. It gives Claude — Desktop, CLI, or mobile — 55 specialist tools and
a local SQLite cache, turning it into a hands-on companion for your stock
research rather than a one-off query tool.

<p align="center">
  <video controls width="330" preload="metadata" playsinline
         poster="screenshots/jquants-mcp-demo1.png">
    <source src="screenshots/jquants-mcp-demo.mp4" type="video/mp4">
    Your browser does not support inline video. The clip walks through sector
    performance, top turnover ranking, candlestick charts, quarterly financial
    summaries, and a multi-stock return comparison on the Claude iPhone app.
  </video>
</p>

## What you can ask Claude

Once jquants-mcp is connected, conversational queries like these just work:

- "Sector performance ranking today" — sector-by-sector advance/decline
- "Kioxia (285A) — 3-month candlestick chart" — split-adjusted with SMA overlays
- "Q4 earnings digest for the five major trading houses" — pulls the latest fins_summary rows for each
- "Stocks hitting new year-to-date highs" — `detect_ytd_high_low` screener
- "What's the code for SoftBank?" — reverse-lookup via `search_equities`
- "Compare TOPIX vs Nikkei 225 over 1 year" — multi-stock comparison chart
- "Show me the sector briefing" — advance/decline count, AD ratio, top/bottom 5 sectors by return, and cheapest-PER sector ranking, all in one call

<p align="center">
  <video controls width="330" preload="metadata" playsinline
         poster="screenshots/jquants-mcp-demo-briefing-market.png">
    <source src="screenshots/jquants-mcp-demo-briefing.mp4" type="video/mp4">
    Your browser does not support inline video. The clip demonstrates the market
    briefing, sector PER ranking, and individual stock briefing on the Claude iPhone app.
  </video>
</p>

## Features

- Ask in plain English or Japanese — jquants-mcp picks the right tool
  and returns a clean answer. No tool names to memorise.
- Instant responses — market data is cached locally, so most queries
  never hit the network at all.
- Works on any J-Quants plan — Free through Premium, auto-detected.
- Chains naturally — "screen for top movers, then chart the leader"
  works as a single request.

## Get started

- **[Quickstart →](quickstart.md)** — install, register your API key, and have
  Claude answer your first stock question in 5 minutes.
- **[Tools →](tools.md)** — the user-facing tour of what jquants-mcp can do.
- **[FAQ →](faq.md)** — plan recommendations, common errors, and tips.

For the full technical reference (config schema, deployment shapes, multi-user
mode, OAuth setup, every tool with parameter tables), see the
[README on GitHub](https://github.com/shigechika/jquants-mcp).

---

!!! warning "Not investment advice"
    This software is a data-access tool, not a financial advisory service.
    Investment decisions are made at your own risk. See the full
    [disclaimer](disclaimer.md) for details.

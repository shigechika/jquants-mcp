# jquants-mcp

**Japanese stock market data for Claude — via the [J-Quants API v2](https://jpx-jquants.com/).**

jquants-mcp is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
server that lets Claude — Desktop, CLI, or mobile — answer questions about the
Japanese stock market by calling 43 specialised tools backed by a local
SQLite cache.

<p align="center">
  <img src="screenshots/jquants-mcp-demo.gif"
       alt="24-second loop on the Claude iPhone app cycling through sector performance, top turnover ranking, candlestick chart with SMA, quarterly financial summary, and a 5-stock return comparison"
       width="330">
</p>

## What you can ask Claude

Once jquants-mcp is connected, conversational queries like these just work:

- 「今日の業種別騰落率は？」 — sector-by-sector performance ranking
- 「キオクシアのチャートを 3 か月分」 — split-adjusted candlestick with SMA overlays
- 「5 大商社の今期業績ダイジェスト」 — pulls the latest fins_summary rows for each
- 「年初来高値を更新した銘柄を一覧」 — `detect_ytd_high_low` screener
- 「ソフトバンクのコードを教えて」 — reverse-lookup via `search_equities`
- 「TOPIX と日経225 の 1 年リターンを比較」 — multi-stock comparison chart

## Why this exists

J-Quants gives institutional-quality Japanese market data, but the raw API is
chatty (per-stock pagination, 5–500 requests/min plan limits, unfamiliar field
names). jquants-mcp:

- Caches everything locally so repeat queries are instant.
- Adapts to your J-Quants plan automatically (Free / Light / Standard / Premium).
- Exposes high-level tools that Claude can compose ("show me top movers and
  draw the chart for the leader") rather than forcing it through low-level
  endpoint calls.

## Get started

- **[Quickstart →](quickstart.md)** — install, register your API key, and have
  Claude answer your first stock question in 5 minutes.
- **[Tools →](tools.md)** — the user-facing tour of what jquants-mcp can do.
- **[FAQ →](faq.md)** — plan recommendations, common errors, and tips.

For the full technical reference (config schema, deployment shapes, multi-user
mode, OAuth setup, every tool with parameter tables), see the
[README on GitHub](https://github.com/shigechika/jquants-mcp).

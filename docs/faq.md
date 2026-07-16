# FAQ

## Which J-Quants plan do I need?

| Plan | What you can do |
|---|---|
| **Free** | Daily bars (12-week delay), basic financials, screeners on the cached window |
| **Light** | Realtime daily bars, minute-level bars (tick add-on), investor flow, TOPIX bars, bulk downloads |
| **Standard** | Margin interest / alert, short ratio, short-sale report, index bars, Nikkei 225 options |
| **Premium** | Detailed financials (BS/PL/CF), dividends, futures / options bars, morning session bars, market breakdown |

The Free plan is enough to try the chart tools, screeners, and the
market-overview ranking tools. Most retail use cases fit in Light. See
[plan comparison](https://github.com/shigechika/jquants-mcp/blob/main/docs/comparison.md)
for the exhaustive table.

## Why is the first query slow?

jquants-mcp caches J-Quants responses on first call. A query like 「今日の業種別騰落率」
needs the full daily bars table for the day, which is one API call but multiple
pages. Subsequent queries hit the cache and return in milliseconds.

To pre-warm the cache, run `scripts/daily_fetch.py` from the repo, or just let
it warm up naturally as you ask Claude questions.

## I see "rate limit exceeded" — what do I do?

The Free plan allows only 5 requests per minute. If Claude is making bulk
queries (e.g. fetching daily bars for many codes), the rate limiter will
back off automatically with exponential retry. If you keep hitting the wall,
upgrade to Light (60 req/min) or Standard (120 req/min).

## Does it work on iPhone?

Yes — install the
[Claude iOS app](https://claude.ai/download), connect it to a self-hosted
jquants-mcp instance over Streamable HTTP + Bearer token, and the chart
images render inline in the chat. The
[demo on the home page](index.md) was recorded on iPhone.

For the host-side setup (TLS, OAuth, multi-user mode), see the
[deploy/](https://github.com/shigechika/jquants-mcp/tree/main/docs/deploy)
guides on GitHub.

## How do I run jquants-mcp for multiple users?

Run it as a Streamable HTTP server with Google or GitHub OAuth. Each user
registers their own J-Quants API key via `register_api_key`, encrypted with
AES-256-GCM. Cloud Run is the supported managed deployment.

See the
[multi-user section of the README](https://github.com/shigechika/jquants-mcp#multi-user-mode).

## Where are my cached files?

By default `~/.cache/jquants-mcp/cache.db` (SQLite, two-tier: row-level cache
for time-series data, response-level cache with TTL for everything else).
Override with `[jquants] cache_dir = …` in your config or `JQUANTS_CACHE_DIR`
env var.

## Stock split detection

When the J-Quants `AdjFactor` field changes for a code, jquants-mcp invalidates
the cached daily bars for that code automatically. Adjusted prices in
`get_candlestick_data`, `get_fins_summary` (`AdjEPS` / `AdjBPS`), and the
screeners all account for splits without manual intervention.

## I want to run a query that doesn't fit any tool

You probably want
[Bulk download](https://github.com/shigechika/jquants-mcp#bulk-download-2-tools).
`get_bulk_list` and `get_bulk_download_url` give you direct CSV access for
custom processing in pandas / spreadsheet tools.

## Where do I report bugs / request features?

[GitHub Issues](https://github.com/shigechika/jquants-mcp/issues).
Include the output of `health_check` so plan, version, and cache state are
captured.

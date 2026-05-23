---
name: "jquants-mcp-usage"
description: "Operational guidance for jquants-mcp: cache tiers, plan limits, screener patterns, and safe cache management."
---

# jquants-mcp Usage Guide

jquants-mcp is an MCP server that retrieves Japanese stock market data via the J-Quants API.
It serves data from a local SQLite cache (fast, offline) and falls back to the live API when needed.

## Session Start

Always call `health_check` at the beginning of a session:

```text
health_check()
```

- `cache_ready: true` means the cache has finished loading and screener tools are available.
- `cache_ready: false` (integrity is "pending") at first call after server start — wait 10–60 s and retry.
- `today_cache_ready: true` means today's equities data is already in cache.
- Check `plan` to know which endpoints and date ranges are available.

## Plan Tiers and Date Ranges

| Plan | Historical depth | Exclusive endpoints |
|---|---|---|
| free | Latest 2 years (12-week delay) | — |
| light | Latest 5 years | investor_types, indices_bars_daily, earnings_calendar |
| standard | Latest 10 years | margin_interest, margin_alert, short_ratio, short_sale_report |
| premium | Full history | breakdown, derivatives |

Queries outside the plan's date range return an error — do not retry with the same date.

## Stock Code Format

- Use 5-digit numeric codes: `72030` (Toyota), `86970` (Japan Exchange Group)
- 4-char alphanumeric codes (`130A`) are accepted and normalised to `130A0`
- Do not use ticker symbols or company names as codes

## Tool Categories

### Equities
- `get_equities_master` — listed company master (name, sector, market)
- `get_equities_bars_daily` — OHLCV daily bars with split adjustment
- `get_equities_bars_minute` — intraday minute bars (light+ with tick add-on)
- `get_equities_bars_daily_am` — morning session bars
- `get_equities_investor_types` — weekly investor type flows (light+)
- `get_equities_earnings_calendar` — scheduled earnings dates (light+)

### Financials
- `get_fins_summary` — quarterly earnings summary (EPS, revenue, guidance)
- `get_fins_details` — detailed P&L / balance sheet items (standard+)
- `get_fins_dividend` — dividend history (standard+)

### Markets
- `get_markets_margin_interest` / `get_markets_margin_alert` — margin trading data (standard+)
- `get_markets_short_ratio` / `get_markets_short_sale_report` — short-selling data (standard+)
- `get_markets_breakdown` — trading breakdown by investor type (premium+)
- `get_markets_calendar` — trading calendar (holidays, market open/close)

### Indices
- `get_indices_bars_daily_topix` — TOPIX daily bars (all plans)
- `get_indices_bars_daily` — other indices (light+)

### Screener (cached, fast)
Screener tools use a pre-built `screener_results` cache populated by `daily_fetch.py`.
Cache hit response time: ~0.01 s. Range: last 52 weeks only.

- `detect_price_limit` — stocks at daily price limit (upper/lower)
- `compare_close_vs_vwap` — close above/below VWAP
- `detect_52w_high_low` — 52-week high/low hits on a given date
- `detect_ytd_high_low` — year-to-date high/low hits on a given date
- `detect_volume_surge` — unusual volume spikes

Use `_range` variants (`detect_52w_high_low_range`, `detect_ytd_high_low_range`) to retrieve
multiple consecutive days in a single call.

Dates beyond the 52-week cache window return `OutOfCacheRange` immediately — do not retry.
When a specific stock `code` is provided, cache is bypassed for correctness (IPO edge cases).

The 6 cross-sectional screener tools above (`detect_price_limit`, `detect_52w_high_low`,
`detect_ytd_high_low`, `detect_volume_surge`, `detect_52w_high_low_range`,
`detect_ytd_high_low_range`) accept `detail: bool = False`.
- `detail=False` (default): returns summary counts only — no stock-level array. Prefer this on
  mobile and whenever you only need totals.
- `detail=True`: returns the full `data` array with per-stock rows.

`compare_close_vs_vwap` is per-code only and always returns a `data` array (no `detail` param).

### Charts
- `get_candlestick_data` — returns OHLCV + indicator data as JSON for React artifact rendering
- `get_comparison_chart_data` — multi-stock performance comparison data as JSON; up to 10 codes, `mode="return_pct"` (default) or `"price"`

### Bulk
- `get_bulk_list` — list available bulk data files
- `get_bulk_download_url` — get a signed URL for bulk file download

### Derivatives (premium)
- `get_derivatives_bars_daily_futures` / `_options` / `_options_225`

## Cache Management

### cache_status
```text
cache_status()
```
Shows row counts per table, file size, and detected plan. Does **not** return market data.

### cache_clear — USE WITH CARE

| Call | Effect |
|---|---|
| `cache_clear(table="response_cache")` | Clears only the API response cache (Tier 2). Safe — historical Tier 1 data is preserved. Use when you suspect stale live-API responses. |
| `cache_clear()` (no argument) | **Clears ALL data** including all Tier 1 historical rows. Only use if you intend a full cache rebuild. |

Never call bare `cache_clear()` unless you are prepared to re-run the full bulk fetch (can take ~1 hour).

## Multi-user Mode (Cloud Run)

1. Authenticate via Google OAuth in the MCP client
2. Register your J-Quants API key at `https://<host>/settings` (recommended) or via `register_api_key`
3. The server auto-detects your plan and applies the correct date-range restrictions per user

Single-user local mode uses `JQUANTS_API_KEY` env var or the `[jquants]` section in the config file.

## Common Patterns

**Check what data is available today:**
```text
health_check()          # confirm cache_ready and plan
cache_status()          # see latest dates per table
```

**Screen for 52-week highs on the latest trading day:**
```text
health_check()          # confirm today_cache_ready: true
detect_52w_high_low()   # omit date → uses latest cached date
```

**Retrieve a week of screener results at once:**
```text
detect_52w_high_low_range(start_date="2025-04-28", end_date="2025-05-02")
```

**Look up a company before querying bars:**
```text
get_equities_master(code="72030")   # confirm code, name, sector
get_equities_bars_daily(code="72030", date_from="2024-01-01")
```

**Render a chart:**
```text
get_candlestick_data(code="72030", from_date="2024-10-01")
```

---
name: "jquants-mcp-usage"
description: "Operational guidance for jquants-mcp: daily briefing workflows, value screening, cache tiers, plan limits, and safe cache management."
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

## Daily Workflows

Prefer the composite one-call tools below over hand-assembling the same picture
from primitive tools — they encode the correct definitions and are cached.

### Morning market brief — one call

```text
health_check()                           # read latest_cache_date from the response
get_market_briefing(date="2026-07-14")   # date is required (YYYY-MM-DD or YYYYMMDD)
```

Pass the **latest cached trading day** as `date` (`health_check` reports it as
`latest_cache_date`) — a weekend/future date beyond the cache returns CacheNotReady.

Sections and how to read them:

- `summary` — advances/declines, 25-day advance/decline ratio, TOPIX change, and market
  margin ratio (margin figures need the Standard+ margin cache; null otherwise).
- `sectors.top` / `sectors.bottom` and `sector_short_ratios` — TSE 33-sector performance and
  short-sale pressure (short-sale ratios need the Standard+ short-ratio cache; empty/null otherwise).
- `top_movers_up` / `top_movers_down` / `top_turnover_value` — daily leaders.
- `highlights` — YTD high-low counts (`ytd_new_highs` / `ytd_new_lows`), volume surges,
  price-limit counts, and `notable_stocks` (RSI14 overbought/oversold from the
  52w-high/low + price-limit universe).
- `value_screen` — the 年安・割安・高配当・好決算 screen (top n). **`null` means the
  section was unavailable (e.g. cold financials cache); `{"count": 0}` means it ran
  and genuinely nothing matched.** For different thresholds call
  `get_value_stock_screen` directly.
- `trend_signals` — distribution-day count and follow-through-day status (auto-detected
  rally start), so you rarely need to call the two detect tools yourself.

### Value screening (年安・割安・高配当・好決算)

```text
get_value_stock_screen()   # defaults: near_low_pct=5, max_per=15, max_pbr=1,
                           # min_yield=3.5, require_profit_increase=True
```

All criteria are ANDed; REITs are excluded. Definition pitfalls that ad-hoc
screening gets wrong — this tool enforces the correct ones:

- **年安 (year-low)** means the close is within `near_low_pct` % of the 52-week LOW
  (or a fresh 52w low was touched that day). "Down by half from the 52-week high"
  is NOT year-low — a stock 30% above its low never qualifies at the default.
- **好決算 (strong earnings)** means the company-forecast net profit exceeds the
  latest FY actual (増益予想). Trailing black ink alone does not qualify.
- **高配当** uses the forward dividend forecast (same logic as
  `get_dividend_yield_ranking`), never the trailing dividend.

Empty result? Loosen one axis at a time: raise `near_low_pct` (e.g. 10–20), raise
`max_pbr`, lower `min_yield`, or set `require_profit_increase=False` (items then
still carry `net_profit` / `forecast_net_profit` so you can judge). `market`
("prime" / "standard" / "growth" / "tokyo_pro") and `sector` (S33 code) narrow the
universe.

### Single-stock deep dive

```text
search_equities(name="住友商事")        # company name → code, when the code is unknown
get_stock_briefing(code="80530")        # price + FY financials + PER/PBR/yield in one call
                                        # (+ margin fields with the Standard+ cache)
get_fins_summary(code="80530")          # quarterly history and forecasts
get_technical_indicators(code="80530", date_from="2026-06-15")
                                        # SMA 5/25 + bb20 + RSI14 by default (date or
                                        # date_from/date_to required); add sma75 via
                                        # indicators=[...]; API fallback on cache miss
get_candlestick_data(code="80530")      # OHLCV + indicators as JSON for chart rendering
```

### Sector value hunt

```text
get_sector_briefing()                          # median PER/PBR/ROE per TSE 33 sector, cheapest first
get_valuation_ranking(sector="2050")           # then drill into one sector by PER (or metric="pbr")
get_dividend_yield_ranking(min_yield=4.0)      # forward-yield ranking (Kabutan-equivalent default)
```

### Market temperature checks

- `detect_price_change` / `get_advance_decline_ratio` — breadth; ratio >120 overbought, <70 oversold.
- `detect_distribution_days` / `detect_follow_through_day` — TOPIX-based IBD-style trend
  signals (already embedded in the briefing's `trend_signals`).
- Leaders and sector moves beyond the briefing's top-n: `get_top_movers`, `get_top_volume`,
  `get_top_turnover_value`, `get_sector_performance` (`sector_type="s17"` for 17 sectors).

### Earnings week

```text
get_earnings_this_week()            # upcoming schedule, grouped by day
get_earnings_results_this_week()    # actual reported numbers with forecast progress
                                    # (empty on Free — results carry the 12-week delay)
get_equities_earnings_calendar(code="72030")   # next earnings date for one stock (all plans)
```

## Plan Tiers and Date Ranges

| Plan | Historical depth | Adds (over the tier below) |
|---|---|---|
| free | Latest 2 years (12-week delay) | — |
| light | Latest 5 years | investor_types, indices_bars_daily_topix, bulk downloads, minute bars (with tick add-on) |
| standard | Latest 10 years | margin_interest, margin_alert, short_ratio, short_sale_report, indices_bars_daily, derivatives options_225 |
| premium | Full history | breakdown, derivatives futures/options, fins_details, fins_dividend, morning-session bars |

Queries outside the plan's date range return an error — do not retry with the same date.

## Stock Code Format

- Use 5-digit numeric codes: `72030` (Toyota), `86970` (Japan Exchange Group)
- 4-char alphanumeric codes (`130A`) are accepted and normalised to `130A0`
- Do not use ticker symbols or company names as codes (use `search_equities` for name lookup)

## Tool Categories

### Composite briefings (cache-first, start here)
- `get_market_briefing` — whole-market daily brief (see workflow above)
- `get_sector_briefing` — sector median PER/PBR/ROE
- `get_stock_briefing` — one-page single-stock brief
- `get_value_stock_screen` — combined value screen (年安・割安・高配当・好決算)

### Market overview & rankings (cache-only)
- `detect_price_change`, `get_advance_decline_ratio`, `get_top_movers`,
  `get_top_volume`, `get_top_turnover_value`, `get_sector_performance`
- `get_dividend_yield_ranking`, `get_valuation_ranking`

### Equities
- `get_equities_master` — listed company master (name, sector, market)
- `search_equities` — company name → code reverse lookup (cache-only)
- `get_equities_bars_daily` — OHLCV daily bars with split adjustment
- `get_equities_bars_minute` — intraday minute bars (light+ with tick add-on)
- `get_equities_bars_daily_am` — today's morning session bars (premium)
- `get_equities_investor_types` — weekly investor type flows (light+)
- `get_equities_earnings_calendar` — scheduled earnings dates (all plans)
- `get_earnings_this_week` / `get_earnings_results_this_week` — weekly earnings schedule / results (cache-only)

### Financials
- `get_fins_summary` — quarterly earnings summary (EPS, revenue, guidance)
- `get_fins_details` — detailed P&L / balance sheet items (premium)
- `get_fins_dividend` — dividend history (premium)

### Markets
- `get_markets_margin_interest` / `get_markets_margin_alert` — margin trading data (standard+)
- `get_markets_short_ratio` / `get_markets_short_sale_report` — short-selling data (standard+)
- `get_markets_breakdown` — trading breakdown by investor type (premium)
- `get_markets_calendar` — trading calendar (holidays, market open/close)

### Indices
- `get_indices_bars_daily_topix` — TOPIX daily bars (light+)
- `get_indices_bars_daily` — other indices (standard+)

### Screener (fast)
The 52w/YTD detectors and `detect_consecutive_dividend_increase` hit a nightly
pre-built cache (~0.01 s on default params); the other screeners compute from
the cached daily bars. The 52w/YTD detectors (and their `_range` variants) are
limited to the last 52 weeks — older dates return `OutOfCacheRange` immediately;
do not retry. The other screeners accept any date within your plan's window.

- `detect_52w_high_low` / `detect_ytd_high_low` — fresh high/low breakouts on a date.
  These list stocks that SET a new high/low that day. There is no plain
  near-the-low lister: `get_value_stock_screen` finds near-low stocks that ALSO
  pass its value criteria (loosen them per the workflow section — note it always
  excludes REITs and net-loss / negative-book stocks).
- `detect_52w_high_low_range` / `detect_ytd_high_low_range` — multi-day variants;
  prefer one `_range` call over per-day loops:
  `detect_52w_high_low_range(date_from="2026-07-06", date_to="2026-07-10")`
- `detect_price_limit` — stocks at daily price limit (ストップ高/安)
- `detect_volume_surge` — unusual volume spikes vs the 20-day average
- `compare_close_vs_vwap` — close above/below VWAP (per-code, always returns `data`)
- `detect_consecutive_dividend_increase` — consecutive annual dividend increases (連続増配)
- `detect_distribution_days` / `detect_follow_through_day` — TOPIX trend signals

Most screeners are cache-only, but `compare_close_vs_vwap` and per-code
`detect_volume_surge` calls fall back to the live API on a cache miss (they need
a working API key there), as does `get_technical_indicators`.

`detect_price_limit`, `detect_volume_surge`, and the 52w/YTD detectors
(+`_range`) accept `detail: bool = False`
(`detect_consecutive_dividend_increase` always returns full data):
- `detail=False` (default): summary counts only — prefer this on mobile and whenever you only need totals.
- `detail=True`: full per-stock `data` array.

A `code=` argument bypasses the pre-built cross-sectional payload (so newly-listed
stocks are not dropped) — the tool then computes from cached daily bars, with the
API fallbacks noted above where applicable.

### Charts
- `get_candlestick_data` — OHLCV + indicator data as JSON for React artifact rendering
- `get_comparison_chart_data` — multi-stock comparison; up to 10 codes, `mode="return_pct"` (default) or `"price"`

### Bulk (light+)
- `get_bulk_list` — list available bulk data files
- `get_bulk_download_url` — get a signed URL for bulk file download

### Derivatives
- `get_derivatives_bars_daily_futures` / `get_derivatives_bars_daily_options` — premium
- `get_derivatives_bars_daily_options_225` — Nikkei 225 options (standard+)

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
   (`delete_api_key` removes it); the server auto-detects your plan and applies the
   correct date-range restrictions per user

Single-user local mode instead uses the `JQUANTS_API_KEY` env var or the `[jquants]`
section in the config file.


# Tools

A user-facing tour of what jquants-mcp lets Claude do. For exact parameter
tables and endpoint mappings, see the
[GitHub README](https://github.com/shigechika/jquants-mcp#available-tools).

## Asking the right way

You don't have to memorise tool names — Claude picks the right one from your
question. The examples below show queries that map cleanly to a single tool;
Claude can also chain several tools (e.g. screen for top movers, then chart
the leader) without you having to ask.

## Daily market overview

What's the market doing today as a whole?

| Question | Tool |
|---|---|
| 「今日の値上がり/値下がり銘柄数」 | `detect_price_change` |
| 「25 日騰落レシオ」 | `get_advance_decline_ratio` |
| 「今日の値上がり率トップ 10」 | `get_top_movers` |
| 「出来高ランキング」 | `get_top_volume` |
| 「売買代金ランキング」（金額ベース、機関投資家フロー把握向け） | `get_top_turnover_value` |
| 「業種別騰落率」（東証 33 業種または 17 業種） | `get_sector_performance` |

These all run against the local cache — no API call, no rate limit.

## Per-stock data

Drill into a specific code:

| Question | Tool |
|---|---|
| 「7203 のここ 1 か月の株価」 | `get_equities_bars_daily` |
| 「8053 住友商事の決算」 | `get_fins_summary` |
| 「9984 SBG の配当履歴」 | `get_fins_dividend` |
| 「285A のチャートを 3 か月」 | `render_candlestick` |
| 「住友商事のコードを教えて」 | `search_equities` |

`render_candlestick` defaults to a 91-day window with `volume + sma5 + sma25`
overlays. SMAs are warmed up from earlier bars so the moving averages are
fully populated from the first displayed candle.

## Screening

Find stocks matching a signal:

| Question | Tool |
|---|---|
| 「年初来高値を更新した銘柄」 | `detect_ytd_high_low` |
| 「52 週高値を更新した銘柄」 | `detect_52w_high_low` |
| 「ストップ高/安銘柄」（引け / 寄らずの内訳付き） | `detect_price_limit` |
| 「20 日平均の 2 倍以上の出来高」 | `detect_volume_surge` |
| 「VWAP より上で引けた銘柄」 | `compare_close_vs_vwap` |

All screeners are pure-Python over the cached daily bars — no extra API calls
even for full-universe scans.

## Comparison charts

Side-by-side return comparison for up to 10 codes:

> 5 大商社（8001 8002 8031 8053 8058）の年初来リターンを比較して

Claude calls `render_comparison_chart` with `mode="return_pct"` (the default),
producing a return chart with each series normalised to 0% at the first bar.
Add `mode="price"` if you want the raw split-adjusted close instead.

## Investor positioning (Standard plan and above)

| Question | Tool |
|---|---|
| 「投資部門別売買代金」 | `get_equities_investor_types` |
| 「業種別空売り比率」 | `get_markets_short_ratio` |
| 「信用取引残高」 | `get_markets_margin_interest` |
| 「増担保規制銘柄」 | `get_markets_margin_alert` |

## Calendar and reference

| Question | Tool |
|---|---|
| 「今週の決算発表予定」 | `get_equities_earnings_calendar` |
| 「来週の祝日」 | `get_markets_calendar` |
| 「上場銘柄一覧」 | `get_equities_master` |

## Utility / admin

| Question | Tool |
|---|---|
| 「サーバーの状態を教えて」 | `health_check` |
| 「キャッシュの状況」 | `cache_status` |
| 「キャッシュをクリアして」 | `cache_clear` |

The full list of 43 tools (with endpoints, plan requirements, and parameter
tables) is on the
[Available Tools section of the GitHub README](https://github.com/shigechika/jquants-mcp#available-tools).

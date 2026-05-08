# ツール

jquants-mcp で Claude に何ができるか、ユースケース別ツアー。各ツールの完全な
パラメータ表とエンドポイント対応は [GitHub README](https://github.com/shigechika/jquants-mcp#available-tools)
を参照してください。

## 質問の仕方

ツール名を覚える必要はありません — Claude は質問内容から適切なツールを選びます。
以下の例は単一ツールに綺麗にマッピングされる質問群ですが、Claude は複数ツールを
連鎖させることもできます（例: 値上がり率トップをスクリーニングしてからリーダーを
チャート化）。

## 日次のマーケット概況

「今日のマーケット全体はどうだった？」系：

| 質問 | ツール |
|---|---|
| 「今日の値上がり/値下がり銘柄数」 | `detect_price_change` |
| 「25 日騰落レシオ」 | `get_advance_decline_ratio` |
| 「今日の値上がり率トップ 10」 | `get_top_movers` |
| 「出来高ランキング」 | `get_top_volume` |
| 「売買代金ランキング」（金額ベース、機関投資家フロー把握向け） | `get_top_turnover_value` |
| 「業種別騰落率」（東証 33 業種または 17 業種） | `get_sector_performance` |
| 「業種別 PER/PBR/ROE」（セクターバリュエーション、割安業種探し） | `get_sector_valuation` |
| 「高配当利回りランキング」（`DivAnn / AdjC × 100`、中間報告の空配当はスキップ） | `get_dividend_yield_ranking` |
| 「今日の相場ブリーフィング」（値上がり/値下がり + 騰落レシオ + 33業種別 + ランキング + TOPIX 変化率 + screener ハイライトを 1 コールで） | `get_market_briefing` |

すべてローカルキャッシュ上で動作 — API コール無し、レート制限無し。

<p align="center" markdown>
![Claude iPhone アプリ上の売買代金ランキング](screenshots/jquants-mcp-demo3.png){ width="280" }
</p>

## 銘柄ごとのデータ

特定の銘柄を深掘り：

| 質問 | ツール |
|---|---|
| 「7203 のここ 1 か月の株価」 | `get_equities_bars_daily` |
| 「8053 住友商事の株価・財務・PER を一覧で」（単銘柄サマリー） | `get_stock_summary` |
| 「8053 住友商事の決算」 | `get_fins_summary` |
| 「9984 SBG の配当履歴」 | `get_fins_dividend` |
| 「285A のチャートを 3 か月」 | `render_candlestick` |
| 「住友商事のコードを教えて」 | `search_equities` |

`render_candlestick` は省略時 91 日窓 + `volume + sma5 + sma25` がデフォルト。
SMA は前倒しで計算されているので、表示開始バーから完全に温まった状態で描画されます。

<p align="center" markdown>
![5 大商社の四半期決算ダイジェスト](screenshots/jquants-mcp-demo6.png){ width="280" }
</p>

## スクリーニング

シグナルにマッチする銘柄を探す：

| 質問 | ツール |
|---|---|
| 「年初来高値を更新した銘柄」 | `detect_ytd_high_low` |
| 「52 週高値を更新した銘柄」 | `detect_52w_high_low` |
| 「ストップ高/安銘柄」（引け / 寄らずの内訳付き） | `detect_price_limit` |
| 「20 日平均の 2 倍以上の出来高」 | `detect_volume_surge` |
| 「VWAP より上で引けた銘柄」 | `compare_close_vs_vwap` |

スクリーナーはすべてキャッシュ上の純 Python 実装 — 全銘柄スキャンでも追加 API コール無し。

## 比較チャート

最大 10 銘柄まで横並びリターン比較：

> 5 大商社（8001 8002 8031 8053 8058）の年初来リターンを比較して

Claude が `render_comparison_chart` を `mode="return_pct"`（デフォルト）で呼び、
各系列を最初のバーで 0% に正規化したリターン推移を返します。
分割調整済み終値そのままが欲しければ `mode="price"` を指定。

<p align="center" markdown>
![5 大商社のリターン比較チャート、ダークモード](screenshots/jquants-mcp-demo7.png){ width="280" }
</p>

## 投資家ポジショニング（Standard プラン以上）

| 質問 | ツール |
|---|---|
| 「投資部門別売買代金」 | `get_equities_investor_types` |
| 「業種別空売り比率」 | `get_markets_short_ratio` |
| 「信用取引残高」 | `get_markets_margin_interest` |
| 「増担保規制銘柄」 | `get_markets_margin_alert` |

## カレンダー・参照データ

| 質問 | ツール |
|---|---|
| 「今週の決算発表予定」 | `get_equities_earnings_calendar` |
| 「来週の祝日」 | `get_markets_calendar` |
| 「上場銘柄一覧」 | `get_equities_master` |

## ユーティリティ / 管理

| 質問 | ツール |
|---|---|
| 「サーバーの状態を教えて」 | `health_check` |
| 「キャッシュの状況」 | `cache_status` |
| 「キャッシュをクリアして」 | `cache_clear` |

47 ツールの完全な一覧（エンドポイント / プラン要件 / パラメータ表）は
[GitHub README の Available Tools](https://github.com/shigechika/jquants-mcp#available-tools)
にまとまっています。

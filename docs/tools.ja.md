# ツール

jquants-mcp で Claude に何ができるか、ユースケース別ツアー。各ツールの完全な
パラメータ表とエンドポイント対応は [GitHub README](https://github.com/shigechika/jquants-mcp#available-tools)
を参照してください。

## 質問の仕方

ツール名を覚える必要はありません — Claude は質問内容から適切なツールを選びます。
以下の例は単一ツールに綺麗にマッピングされる質問群ですが、Claude は複数ツールを
連鎖させることもできます（例: 値上がり率トップをスクリーニングしてからリーダーを
チャート化）。

## 日次のマーケット概況・バリュエーション

「今日のマーケット全体はどうだった？」「業種別の割安感は？」系：

| 質問 | ツール |
|---|---|
| 「今日の値上がり/値下がり銘柄数」 | `detect_price_change` |
| 「25 日騰落レシオ」 | `get_advance_decline_ratio` |
| 「今日の値上がり率トップ 10」 | `get_top_movers` |
| 「出来高ランキング」 | `get_top_volume` |
| 「売買代金ランキング」（金額ベース、機関投資家フロー把握向け） | `get_top_turnover_value` |
| 「業種別騰落率」（東証 33 業種または 17 業種） | `get_sector_performance` |
| 「業種別 PER/PBR/ROE」（セクターブリーフィング、割安業種探し） | `get_sector_briefing` |
| 「高配当利回りランキング」（`DivAnn / AdjC × 100`、中間報告の空配当はスキップ） | `get_dividend_yield_ranking` |
| 「今日の相場ブリーフィング」（値上がり/値下がり + 騰落レシオ + 33業種別 + ランキング + TOPIX 変化率 + screener ハイライトを 1 コールで） | `get_market_briefing` |

すべてローカルキャッシュ上で動作 — API コール無し、レート制限無し。

<p align="center" markdown>
![Claude iPhone アプリ上の売買代金ランキング](screenshots/jquants-mcp-demo3.png){ width="280" }
</p>

## ワンコールブリーフィング

「朝のブリーフィングをお願い」と頼むだけで、Claude が相場概況・業種バリュエーション・
個別株サマリーをまとめて返します — 複数ツールを連鎖させる必要はありません：

| 質問 | ツール |
|---|---|
| 「今日の相場ブリーフィング」 | `get_market_briefing` |
| 「業種別バリュエーション、割安順で」 | `get_sector_briefing` |
| 「485A のブリーフィング」 | `get_stock_briefing` |

<p align="center" markdown>
![Claude iPhone アプリ上の相場ブリーフィング — 値上がり/値下がり数、騰落レシオ、強い/弱い業種トップ5](screenshots/jquants-mcp-demo-briefing-market.png){ width="280" }
</p>

<p align="center" markdown>
![業種別ブリーフィング — 東証 33 業種を中央値 PER で割安順にソート、バリュエーション評価付き](screenshots/jquants-mcp-demo-briefing-sector.png){ width="280" }
</p>

<p align="center" markdown>
![パワーエックス（485A）株式ブリーフィング — 株価・財務・バリュエーション指標・信用取引残高を一覧](screenshots/jquants-mcp-demo-briefing-stock.png){ width="280" }
</p>

## 銘柄ごとのデータ

特定の銘柄を深掘り：

| 質問 | ツール |
|---|---|
| 「8053 住友商事の株価・財務・PER を一覧で」（株式ブリーフィング） | `get_stock_briefing` |
| 「7203（トヨタ）のここ 1 か月の株価」 | `get_equities_bars_daily` |
| 「8053 住友商事の決算」 | `get_fins_summary` |
| 「9984 SBG の配当履歴」 | `get_fins_dividend` |
| 「285A（キオクシア）のチャートを 3 か月」 | `render_candlestick` |
| 「7203 は SMA25 の上？RSI は？」 | `get_technical_indicators` |
| 「住友商事のコードを教えて」 | `search_equities` |

`render_candlestick` は省略時 91 日窓 + `volume + sma5 + sma25` がデフォルト。
SMA は前倒しで計算されているので、表示開始バーから完全に温まった状態で描画されます。
RSI のチャート描画は現時点で未対応です — RSI 数値は `get_technical_indicators` をご利用ください。

`get_technical_indicators` は SMA（5/25/75）・ボリンジャーバンド（bb20）・RSI（rsi14）を数値で返します。
「終値は SMA25 を上抜けたか？」「RSI は過熱していないか？」をチャートを描かずに確認できます。
すべての値は分割調整済み終値（AdjC）を使用します。

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
| 「相場はディストリビューション（機関投資家の売り圧力）？」 | `detect_distribution_days` |
| 「この反騰はフォロースルーデイが出た？」 | `detect_follow_through_day` |

`detect_distribution_days` は TOPIX を市場指標、東証全銘柄の売買代金合計（`SUM(Va)`）を出来高代替として使用します。
TOPIX 日次リターンが 20 日ローリング平均から 2.0σ 以上下落した日をディストリビューションデイとし、
25 日間に 4 日以上でトレンド悪化警告を発します（IBD メソッドを TOPIX 向けに校正 — 2021〜2026 年のデータで年約 9 回発火）。
各エントリには `volume_confirmed`（当日の市場売買代金が前日を上回ったか）が含まれます。

`detect_follow_through_day` は新しい上昇トレンドを確認します。`rally_start`（安値・反転日 = セッション 1）から
4 日目以降に TOPIX が 2.0σ 以上上昇し、かつ市場出来高が前日を上回った日がフォロースルーデイです。
`rally_start` に反転日を渡し、シグナルが出るまで各日付でチェックしてください。

`detect_ytd_high_low` と `detect_52w_high_low` には 4 つのフィールドが追加されています：
`AdjO`（分割調整済み始値 — 陽線・陰線の判定用）、`close_vs_vwap`（`"above"` / `"below"` —
当日引値を日中 VWAP `Va/Vo` と比較）、`volume_ratio`（当日出来高 ÷ 直近 20 日平均出来高 —
1.5 超で「出来高を伴った動き」と判断）、`volume_ratio_sessions`（実際に平均に使ったセッション数 —
年初は 20 未満になる場合あり）。
「年高を陽線・大出来高・VWAP 超えで確認」という質問にチャート無しで答えられます。

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

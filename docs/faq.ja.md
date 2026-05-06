# FAQ

## どの J-Quants プランを選べばいいですか？

| プラン | できること |
|---|---|
| **Free** | 日次株価（12 週遅延）、基本的な財務情報、キャッシュ済期間でのスクリーナー |
| **Light** | リアルタイム日次株価、分足、投資部門別売買、デリバティブ |
| **Standard** | 信用取引残高 / 増担保規制、空売り比率、空売り残高報告 |
| **Premium** | 詳細財務（BS/PL/CF）、前場のみのデータ、売買内訳 |

Free プランでもチャートツール、スクリーナー、マーケット概況ランキングは試せます。
個人投資家用途なら Light でほぼ十分。網羅的な比較表は
[plan comparison](https://github.com/shigechika/jquants-mcp/blob/main/docs/comparison.md)
を参照。

## なぜ最初のクエリが遅いのですか？

jquants-mcp は J-Quants のレスポンスを最初の呼び出し時にキャッシュします。
「今日の業種別騰落率」のようなクエリには 1 日分の全銘柄日次バーが必要で、
これは API コール 1 回（複数ページネーション）で取得します。
2 回目以降はキャッシュヒットでミリ秒オーダーで返答します。

事前にキャッシュを温めたい場合はリポジトリの `scripts/daily_fetch.py` を実行してください。
あるいは Claude に質問しながら自然に温まっていくのを待つだけでも OK です。

## 「rate limit exceeded」と表示される

Free プランは 1 分あたり 5 リクエストまで。Claude が大量クエリ（多銘柄の日次バー取得など）
を作ると、レートリミッタが指数バックオフで自動的に待機します。
頻繁に上限に当たる場合は Light（60 req/min）または Standard（120 req/min）への
アップグレードを検討してください。

## チャートが「[charts] not installed」になる

extras を含めて再インストール：

```bash
uv tool install --reinstall "jquants-mcp[charts]"
# または
pipx reinstall --pip-args '"jquants-mcp[charts]"' jquants-mcp
```

`mplfinance` と `matplotlib` が追加で入ります（約 60 MB）。

## iPhone でも動きますか？

動きます。[Claude iOS アプリ](https://claude.ai/download)をインストールし、
Streamable HTTP + Bearer token でセルフホストの jquants-mcp に接続すれば、
チャート画像がチャットに inline 表示されます。
[トップページのデモ](index.md) は実際に iPhone で収録したものです。

ホスト側のセットアップ（TLS、OAuth、マルチユーザーモード）は GitHub の
[deploy/](https://github.com/shigechika/jquants-mcp/tree/main/docs/deploy)
ガイドを参照。

## 複数ユーザー向けに動かしたい

Streamable HTTP サーバーとして起動し、Google または GitHub OAuth と組み合わせます。
各ユーザーは `register_api_key` で自分の J-Quants API キーを登録し、AES-256-GCM で
暗号化保存されます。Cloud Run がサポート対象のマネージドデプロイ先です。

詳細は [README のマルチユーザーセクション](https://github.com/shigechika/jquants-mcp#multi-user-mode)
を参照。

## キャッシュファイルはどこ？

デフォルトは `~/.cache/jquants-mcp/cache.db`（SQLite、2 層構造：時系列データは
行レベル、その他は TTL 付きレスポンスレベル）。
config.ini の `[jquants] cache_dir = …` または環境変数 `JQUANTS_CACHE_DIR` で上書き可能。

## 株式分割の扱い

J-Quants の `AdjFactor` フィールドが変化したら、jquants-mcp は該当銘柄の日次バー
キャッシュを自動的に無効化します。`render_candlestick` の調整済み価格、
`get_fins_summary` の `AdjEPS` / `AdjBPS`、各スクリーナーすべてが分割を考慮済みで動作します。

## ツールではカバーできないクエリを書きたい

[Bulk download](https://github.com/shigechika/jquants-mcp#bulk-download-2-tools)
を検討してください。`get_bulk_list` と `get_bulk_download_url` で CSV を直接取得し、
pandas / 表計算ツールで自由に処理できます。

## バグ報告 / 機能要望はどこへ？

[GitHub Issues](https://github.com/shigechika/jquants-mcp/issues) へ。
`health_check` の出力（プラン・バージョン・キャッシュ状態）を含めると診断が捗ります。

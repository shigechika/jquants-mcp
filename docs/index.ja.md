# jquants-mcp

**Claude で日本株市場データを — [J-Quants API v2](https://jpx-jquants.com/) 経由。**

jquants-mcp は [MCP (Model Context Protocol)](https://modelcontextprotocol.io/)
サーバーで、Claude（Desktop / CLI / モバイル）が日本株市場についての質問に
答えられるよう、ローカル SQLite キャッシュをバックエンドにした 43 の専門ツールを提供します。

<p align="center">
  <video controls width="330" preload="metadata" playsinline
         poster="../screenshots/jquants-mcp-demo1.png">
    <source src="../screenshots/jquants-mcp-demo.mp4" type="video/mp4">
    お使いのブラウザは inline 動画再生に対応していません。Claude iPhone アプリ上で
    業種別騰落率、売買代金ランキング、ローソク足チャート、四半期決算ダイジェスト、
    複数銘柄リターン比較を順に巡るデモ動画です。
  </video>
</p>

## Claude にできる質問

jquants-mcp を接続すると、以下のような自然な日本語クエリがそのまま動きます：

- 「今日の業種別騰落率は？」 — 業種ごとのパフォーマンスランキング
- 「キオクシアのチャートを 3 か月分」 — SMA 付き分割調整済みローソク足
- 「5 大商社の今期業績ダイジェスト」 — 各社の最新 fins_summary 行をまとめて取得
- 「年初来高値を更新した銘柄を一覧」 — `detect_ytd_high_low` スクリーナー
- 「ソフトバンクのコードを教えて」 — `search_equities` で銘柄名逆引き
- 「TOPIX と日経 225 の 1 年リターンを比較」 — 複数銘柄比較チャート

## なぜ存在するか

J-Quants は機関投資家品質の日本株データを提供しますが、生 API は煩雑です（銘柄ごとの
ページネーション、5〜500 リクエスト/分のプラン制限、慣れないフィールド名）。
jquants-mcp は：

- すべてをローカルキャッシュするので、繰り返しのクエリは即時に返答
- J-Quants のプラン（Free / Light / Standard / Premium）を自動検出して動作
- Claude が組み合わせ可能な高レベルツールを公開（「値上がり率トップを表示してリーダーのチャートを描画」など）。低レベルなエンドポイント呼び出しを強要しない

## 始め方

- **[クイックスタート →](quickstart.md)** — 5 分でインストール、API キー登録、最初の株式クエリ
- **[ツール →](tools.md)** — 何ができるかのユーザー向けツアー
- **[FAQ →](faq.md)** — プラン選び、よくあるエラー、Tips

完全な技術リファレンス（設定 schema、デプロイ形態、マルチユーザーモード、OAuth、
全ツールのパラメータ表）は [GitHub README](https://github.com/shigechika/jquants-mcp)
を参照してください。

# クイックスタート

jquants-mcp を Claude に接続して日本株のクエリに答えさせるまで、約 5 分です。

## 前提条件

- Python 3.10 以上（`python3 --version` で確認）
- [J-Quants アカウント](https://jpx-jquants.com/)（最低 Free プラン。Light 以上で 12 週遅延が解消され日次株価がリアルタイム化）
- Claude Code（CLI）/ Claude Desktop / その他 MCP 対応クライアントのいずれか

## 1. jquants-mcp をインストール

=== "uv（推奨）"

    ```bash
    uv tool install jquants-mcp
    ```

=== "pipx"

    ```bash
    pipx install jquants-mcp
    ```

=== "pip"

    ```bash
    pip install --user jquants-mcp
    ```

## 2. J-Quants API キーを取得

最も簡単なのはブラウザログイン（PKCE フロー）：

```bash
jquants-mcp login
```

J-Quants の OAuth ページがブラウザで開き、承認後 API キーが
`~/.config/jquants-mcp/config.ini`（mode 0600）に保存されます。
クリアしたいときは `jquants-mcp logout`。

自分でキーを管理したい場合は、[J-Quants ダッシュボード](https://jpx-jquants.com/)
からキーをコピーして同じファイルに記述：

```ini
# ~/.config/jquants-mcp/config.ini
[jquants]
api_key = YOUR_API_KEY_HERE
```

設定ファイルを作りたくない場合は環境変数 `JQUANTS_API_KEY` でも OK です。

## 3. Claude に接続

=== "claude.ai（ブラウザ / デスクトップ / スマホ）"

    1. [claude.ai](https://claude.ai) を開き **プロジェクト** を作成
       （左サイドバー → **プロジェクト** → **新規プロジェクト**）。
    2. プロジェクトを開く → 歯車アイコン → **Integrations** → **Add integration** →
       **Custom** → jquants-mcp の前段に置いたゲートウェイの URL を入力
       （例：Cloud Run のエンドポイント）。jquants-mcp 自体は stdio しか話さないため、
       リモートからの接続は必ずゲートウェイ経由になります。リファレンス構成の
       Cloud Run では Google アカウントでサインインします。
    3. _（推奨）_ **指示を追加** をクリックし、
       [`docs/claude-project-instructions.md`](claude-project-instructions.md)
       の内容を貼り付ける。
       これにより React artifact でのチャート描画パターンが Claude に伝わり、
       追加のプロンプトなしで正しいレイアウトが得られる。
    4. 数分以内に、ブラウザ / デスクトップ版の設定がスマホアプリに自動連携する。

=== "Claude Code（プラグイン）"

    このリポジトリはプラグイン 1 個のマーケットプレイスも兼ねているので、
    Claude Code からそのまま導入できます:

    ```
    /plugin marketplace add shigechika/jquants-mcp
    /plugin install jquants-mcp@jquants-mcp
    ```

    プラグインは `uvx` を起動するため、Claude Code を実行するプロセスの
    `PATH` に `uvx` が通っている必要があります。ログインシェルなら通常
    問題ありませんが、GUI から起動した場合は通っていないことがあります。
    プラグインが起動しない場合は [uv](https://docs.astral.sh/uv/) を
    システム全体にインストールしてください。

    上の[手順 2](#2-j-quants-api)と同じ環境変数を読みます。プラグイン
    自身の `.mcp.json` には `JQUANTS_API_KEY` をあえて含めていません。
    ちょうど設定したキー ── `jquants-mcp login` で保存した
    `~/.config/jquants-mcp/config.ini`、または既存の
    `~/.jquants-api/jquants-api.toml` ── がそのまま見つかるようにするためです。
    両方とも上書きしたい場合のみ、Claude Code を起動する前に自分で
    `JQUANTS_API_KEY` を export してください。

=== "Claude Code（CLI）"

    ```bash
    claude mcp add jquants -- jquants-mcp
    ```

    `claude mcp list` で確認。次回 `claude` 起動時からサーバーが利用可能。

=== "Claude Desktop"

    `~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）または
    Windows / Linux の対応ファイルを編集：

    ```json
    {
      "mcpServers": {
        "jquants": {
          "command": "jquants-mcp"
        }
      }
    }
    ```

    Claude Desktop を再起動して反映。

## 4. 試してみる

Claude を開いて聞いてみる：

> 今日の業種別騰落率を教えて

Claude が `get_sector_performance` を呼んで業種ランキング表を返します。
最初の 1 回でローカルキャッシュが温まり、以後のクエリは即時返答に。

<p align="center" markdown>
![Claude iPhone アプリの東証17業種ランキング全表示](screenshots/jquants-mcp-demo2.png){ width="280" }
</p>

チャートも試してみる：

> キオクシア（285A）のチャートを 3 か月分

Claude が `get_candlestick_data` を呼んでローソク足 React artifact をチャットに inline 表示します。

<p align="center" markdown>
![Claude iPhone アプリ上のキオクシアホールディングスのローソク足チャート](screenshots/jquants-mcp-demo5.png){ width="280" }
</p>

## 次のステップ

- **[ツール →](tools.md)** — Claude にできることの一覧
- **[FAQ →](faq.md)** — よくあるエラー、プラン選び、マルチユーザーモード
- **完全なリファレンス**: [GitHub README](https://github.com/shigechika/jquants-mcp)
  に設定 schema、デプロイ形態（Docker / Cloud Run / ゲートウェイ経由のセルフホスト）、
  ツール別パラメータ表、ゲートウェイ側の認証設定が網羅されています

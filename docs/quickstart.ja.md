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

ローソク足 / 比較チャートをチャットに inline 描画したい場合は `[charts]` extras も追加：

```bash
uv tool install "jquants-mcp[charts]"
```

`mplfinance` と `matplotlib` が追加で入ります（約 60 MB）。extras 未インストール時は
チャートツールが silent skip されるだけで、それ以外は通常通り動作します。

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
       **Custom** → jquants-mcp サーバーの URL を入力（例：Cloud Run のエンドポイント）。
       Google OAuth で認証。
    3. _（推奨）_ **指示を追加** をクリックし、
       [`docs/claude-project-instructions.md`](claude-project-instructions.md)
       の内容を貼り付ける。
       これにより React artifact でのチャート描画パターンが Claude に伝わり、
       追加のプロンプトなしで正しいレイアウトが得られる。
    4. 数分以内に、ブラウザ / デスクトップ版の設定がスマホアプリに自動連携する。

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

`[charts]` extras がインストールされていれば、Claude がローソク足 PNG をチャットに inline 表示します。

<p align="center" markdown>
![Claude iPhone アプリ上のキオクシアホールディングスのローソク足チャート](screenshots/jquants-mcp-demo5.png){ width="280" }
</p>

## 次のステップ

- **[ツール →](tools.md)** — Claude にできることの一覧
- **[FAQ →](faq.md)** — よくあるエラー、プラン選び、マルチユーザーモード
- **完全なリファレンス**: [GitHub README](https://github.com/shigechika/jquants-mcp)
  に設定 schema、デプロイ形態（Docker / Cloud Run / セルフホスト HTTP）、
  ツール別パラメータ表、OAuth 設定が網羅されています

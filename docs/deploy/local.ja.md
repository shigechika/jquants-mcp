# セルフホストデプロイ

jquants-mcp を自分で管理するホストで動かし、Claude Desktop / Claude Code から接続します。

OAuth マルチユーザーデプロイは代わりに [gcp.ja.md](gcp.ja.md) を参照。

---

## Option A: Docker（Python 不要）

Docker がインストール済みなら、これがローカル MCP サーバーを最速で立ち上げる方法です。
Python も TLS 証明書も GCS アカウントも不要です。

### 前提条件

- Docker Desktop（macOS / Windows）または Docker Engine（Linux）
- J-Quants アカウントと API キー

### 1. サーバーを起動

```bash
JQUANTS_API_KEY=xxx docker compose up -d
# → MCP エンドポイント: http://localhost:8080/mcp
```

サーバーは `127.0.0.1:8080` のみで待ち受けます。
キャッシュデータは Docker named volume（`jquants-mcp_cache`）に保存され、再起動後も維持されます。

Bearer トークン認証を追加する場合（`mcp-stdio` を使わない場合に推奨）:

```bash
JQUANTS_API_KEY=xxx MCP_BEARER_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  docker compose up -d
```

### 2. Claude Desktop から接続（stdio）

Claude Desktop のセッションごとに新しいコンテナが起動します。
Claude Desktop の MCP 設定（macOS では `~/Library/Application Support/Claude/claude_desktop_config.json`）を編集:

```json
{
  "mcpServers": {
    "jquants": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--entrypoint", "jquants-mcp",
        "-e", "JQUANTS_API_KEY=xxx",
        "-e", "JQUANTS_CACHE_DIR=/home/appuser/.cache/jquants-mcp",
        "-v", "jquants-mcp_cache:/home/appuser/.cache/jquants-mcp",
        "ghcr.io/shigechika/jquants-mcp:latest"
      ]
    }
  }
}
```

TLS も Bearer トークンも不要です。セッション終了時にコンテナは終了します。
named volume `jquants-mcp_cache` は compose スタックと共有されるため、
`docker compose exec ... daily_fetch.py --all` で投入したキャッシュは stdio セッションからも利用できます。

### 3. Claude Code から接続（HTTP）

```bash
claude mcp add jquants-mcp --transport http http://localhost:8080/mcp
```

`MCP_BEARER_TOKEN` を設定した場合:

```bash
claude mcp add jquants-mcp --transport http http://localhost:8080/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

Claude Code には一部の HTTP トランスポートで `Authorization` ヘッダーが落ちるバグがあります
（[claude-code#28293](https://github.com/anthropics/claude-code/issues/28293)）。
該当する場合は [mcp-stdio](https://pypi.org/project/mcp-stdio/) をプロキシとして使用してください:

```bash
claude mcp add jquants-mcp --env MCP_BEARER_TOKEN=<TOKEN> \
  -- uvx mcp-stdio http://localhost:8080/mcp
```

### 4. キャッシュを投入（初回のみ）

コンテナは空のキャッシュ DB で起動します。
フル履歴取得を実行してください（J-Quants プランによって 1〜3 時間程度）:

```bash
docker compose exec jquants-mcp python /app/scripts/daily_fetch.py --all
```

**自動日次更新:** `ENABLE_DAILY_FETCH=true` を設定すると、平日 17:30 JST（08:30 UTC）に
コンテナ内で差分更新が自動実行されます:

```bash
JQUANTS_API_KEY=xxx ENABLE_DAILY_FETCH=true docker compose up -d
```

これはコンテナ内で MCP サーバーと並行して [supercronic](https://github.com/aptible/supercronic) を起動します。
ホスト側の cron や launchd 設定は不要です。

**手動更新**（`ENABLE_DAILY_FETCH` を使わない場合）:

```bash
docker compose exec jquants-mcp python /app/scripts/daily_fetch.py
```

### 5. よく使うコマンド

```bash
docker compose logs -f          # ログを追跡
docker compose stop             # 正常停止
docker compose pull             # 最新イメージへ更新
docker compose down -v          # 停止してキャッシュ volume を削除（データ消失注意！）
```

---

## Option B: Python インストール（TLS + リモートアクセス）

この方法では、TLS 付きの公開ドメインでサーバーを公開できるため、
ラップトップ・モバイルなどローカルネットワーク外の端末からも接続できます。

このガイドは以下を前提とします:
- 単一ユーザー、または Bearer トークンを共有する少数の信頼ユーザー向け
- ホストに向けた TLS 証明書を取得できること
- ホストが常時稼働していること（cron / launchd / systemd でプロセス管理）

### 前提条件

- Python 3.10+ が使える Linux または macOS ホスト
- ホストに向いているドメイン名（IPv4 または IPv6）。IPv6 DDNS の例は [shigechika/macos-ddns6](https://github.com/shigechika/macos-ddns6) を参照
- TLS 証明書。[acme.sh](https://github.com/acmesh-official/acme.sh) の DNS-01 チャレンジが IPv6 専用ホストやワイルドカード証明書に対応しておりおすすめ
- J-Quants アカウントと API キー

### 1. インストール

```bash
uv tool install jquants-mcp      # または: pipx install jquants-mcp
```

### 2. 設定

`~/.config/jquants-mcp/config.ini` に記載する方法:

```ini
[jquants]
api_key = <J-Quants API キー>

[server]
ssl_certfile = /etc/letsencrypt/live/mcp.example.com/fullchain.pem
ssl_keyfile = /etc/letsencrypt/live/mcp.example.com/privkey.pem
bearer_token = <生成したトークン>
```

または環境変数（`JQUANTS_API_KEY`, `SSL_CERTFILE`, `SSL_KEYFILE`, `MCP_BEARER_TOKEN`）でも設定可能。

Bearer トークンの生成:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3. 起動

```bash
jquants-mcp --transport streamable-http --host 0.0.0.0 --port 8080
```

`--host 0.0.0.0` は全インターフェースにバインドします。IPv6 デュアルスタックには `--host ::` を使用。ローカルのみでよければデフォルトの `127.0.0.1` で十分です。

### バックグラウンドサービスとして起動

**macOS（launchd）:** `~/Library/LaunchAgents/com.example.jquants-mcp.plist` を KeepAlive + RunAtLoad で作成します。macOS 26+ の TCC サンドボックス問題が発生する場合は、`JQUANTS_API_TOML_PATH` で設定ファイルパスを明示してください（詳細: [README の macOS launchd note](../../README.md#macos-launchd-note)）。

**Linux（systemd）:** `/etc/systemd/system/jquants-mcp.service` を作成:

```ini
[Unit]
Description=jquants-mcp
After=network-online.target

[Service]
Type=simple
User=mcp
ExecStart=/home/mcp/.local/bin/jquants-mcp --transport streamable-http --host :: --port 8080
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now jquants-mcp
```

### 4. Claude クライアントから接続

#### Claude Code / Claude Desktop（mcp-stdio 経由）

Claude Code には HTTP トランスポートで `Authorization` ヘッダーが落ちるバグがあります（[claude-code#28293](https://github.com/anthropics/claude-code/issues/28293)）。[mcp-stdio](https://pypi.org/project/mcp-stdio/) をプロキシとして使用してください:

```bash
# Claude Code
claude mcp add jquants-mcp --env MCP_BEARER_TOKEN=<TOKEN> \
  -- uvx mcp-stdio https://mcp.example.com:8080/mcp
```

Claude Desktop の場合は MCP 設定で同じ env 付きで `mcp-stdio` を起動します。

#### Claude Code（直接 HTTP）

ヘッダーバグ修正後は直接 HTTP トランスポートが使えるようになります:

```bash
claude mcp add jquants-mcp \
  --transport http https://mcp.example.com:8080/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

### 5. 運用

- ログ: `journalctl -u jquants-mcp -f`（systemd）または `/tmp/jquants-mcp.err.log`（launchd デフォルト）
- キャッシュ DB: `~/.cache/jquants-mcp/cache.db` — 取得データが増えると数 GB になります（[Caching](../../README.md#caching) 参照）
- キャッシュ投入: `jquants-mcp daily-fetch` または `uv run scripts/daily_fetch.py` を cron / launchd タイマーで毎日実行

---

## Cloud Run への移行タイミング

以下の場合は [gcp.ja.md](gcp.ja.md) への移行を検討:

- 自分以外のユーザーに各自の J-Quants アカウントで使わせたい
- Bearer トークン共有ではなく OAuth ログインが欲しい
- Claude Desktop Connectors UI や Claude モバイルの OAuth フローに対応したい
- ホストが不安定でオートスケーリング / ゼロオペレーションが必要

それ以外は J-Quants API・キャッシュスキーマ・ツール群はすべて同じです。

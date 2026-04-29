# セルフホスト HTTP デプロイ

jquants-mcp を自分で管理するホストで動かし、Streamable HTTP + TLS 経由で他の端末（ラップトップ・モバイル）から接続します。

- 単一ユーザー、または Bearer トークンを共有する少数の信頼ユーザー向け
- TLS 証明書を取得できるドメインが必要
- ホストが常時稼働していること（cron / launchd / systemd でプロセス管理）

OAuth マルチユーザーデプロイは [gcp.ja.md](gcp.ja.md) を参照。

## 前提条件

- Python 3.10+ が使える Linux または macOS ホスト
- ホストに向いているドメイン名（IPv4 または IPv6）。IPv6 DDNS の例は [shigechika/macos-ddns6](https://github.com/shigechika/macos-ddns6) を参照
- TLS 証明書。[acme.sh](https://github.com/acmesh-official/acme.sh) の DNS-01 チャレンジが IPv6 専用ホストやワイルドカード証明書に対応しておりおすすめ
- J-Quants アカウントと API キー

## 1. インストール

```bash
uv tool install jquants-mcp      # または: pipx install jquants-mcp
```

## 2. 設定

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

## 3. 起動

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

## 4. Claude クライアントから接続

### Claude Code / Claude Desktop（mcp-stdio 経由）

Claude Code には HTTP トランスポートで `Authorization` ヘッダーが落ちるバグがあります（[claude-code#28293](https://github.com/anthropics/claude-code/issues/28293)）。[mcp-stdio](https://pypi.org/project/mcp-stdio/) をプロキシとして使用してください:

```bash
# Claude Code
claude mcp add jquants-mcp --env MCP_BEARER_TOKEN=<TOKEN> \
  -- uvx mcp-stdio https://mcp.example.com:8080/mcp
```

Claude Desktop の場合は MCP 設定で同じ env 付きで `mcp-stdio` を起動します。

### Claude Code（直接 HTTP）

ヘッダーバグ修正後は直接 HTTP トランスポートが使えるようになります:

```bash
claude mcp add jquants-mcp \
  --transport http https://mcp.example.com:8080/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

## 5. 運用

- ログ: `journalctl -u jquants-mcp -f`（systemd）または `/tmp/jquants-mcp.err.log`（launchd デフォルト）
- キャッシュ DB: `~/.cache/jquants-mcp/cache.db` — 取得データが増えると数 GB になります（[Caching](../../README.md#caching) 参照）
- キャッシュ投入: `jquants-mcp daily-fetch` または `uv run scripts/daily_fetch.py` を cron / launchd タイマーで毎日実行

## Cloud Run への移行タイミング

以下の場合は [gcp.ja.md](gcp.ja.md) に移行を検討:

- 自分以外のユーザーに各自の J-Quants アカウントで使わせたい
- Bearer トークン共有ではなく OAuth ログインが欲しい
- Claude Desktop Connectors UI や Claude モバイルの OAuth フローに対応したい
- ホストが不安定でオートスケーリング / ゼロオペレーションが必要

それ以外は J-Quants API・キャッシュスキーマ・ツール群はすべて同じです。

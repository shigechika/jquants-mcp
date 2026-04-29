# Local Deployment

Run jquants-mcp on a host you control and connect from Claude Desktop or Claude Code.

For OAuth-based multi-user deployment, see [gcp.md](gcp.md) instead.

---

## Option A: Docker (no Python required)

If you have Docker installed, this is the fastest path to a running local MCP server.
No Python, no TLS certificate, and no GCS account needed.

### Prerequisites

- Docker Desktop (macOS / Windows) or Docker Engine (Linux)
- A J-Quants account + API key

### 1. Start the server

```bash
JQUANTS_API_KEY=xxx docker compose up -d
# → MCP endpoint: http://localhost:8080/mcp
```

The server listens on `127.0.0.1:8080` only.
Cache data is stored in a Docker named volume (`jquants-mcp_cache`) and persists across restarts.

To add Bearer token authentication (recommended when not using `mcp-stdio`):

```bash
JQUANTS_API_KEY=xxx MCP_BEARER_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  docker compose up -d
```

### 2. Connect from Claude Desktop (stdio)

Each Claude Desktop session spawns a fresh container.
Edit your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

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

No TLS or Bearer token needed; the container exits when the session ends.
The named volume `jquants-mcp_cache` is shared with the compose stack, so cache populated
via `docker compose exec ... daily_fetch.py --all` is also available in stdio sessions.

### 3. Connect from Claude Code (HTTP)

```bash
claude mcp add jquants-mcp --transport http http://localhost:8080/mcp
```

If you set `MCP_BEARER_TOKEN`, add:

```bash
claude mcp add jquants-mcp --transport http http://localhost:8080/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

Claude Code has a known bug that drops the `Authorization` header on some HTTP transports
([claude-code#28293](https://github.com/anthropics/claude-code/issues/28293)).
Use [mcp-stdio](https://pypi.org/project/mcp-stdio/) as a proxy if you hit it:

```bash
claude mcp add jquants-mcp --env MCP_BEARER_TOKEN=<TOKEN> \
  -- uvx mcp-stdio http://localhost:8080/mcp
```

### 4. Populate the cache (first run)

The container starts with an empty cache DB.
Run a full historical fetch (takes 1–3 hours depending on your J-Quants plan):

```bash
docker compose exec jquants-mcp python /app/scripts/daily_fetch.py --all
```

Subsequent daily updates (run manually or via cron):

```bash
docker compose exec jquants-mcp python /app/scripts/daily_fetch.py
```

> **Note:** The Cloud Run deployment receives automatic cache updates via
> GCS → Pub/Sub → `/internal/reload`.  Local Docker has no Pub/Sub integration;
> schedule `daily_fetch.py` yourself (e.g. a daily cron or launchd timer that
> runs `docker compose exec jquants-mcp python /app/scripts/daily_fetch.py`).

### 5. Useful commands

```bash
docker compose logs -f          # follow logs
docker compose stop             # graceful stop
docker compose pull             # upgrade to latest image
docker compose down -v          # stop and delete cache volume (data loss!)
```

---

## Option B: Python install (TLS + remote access)

This option lets you expose the server over a public domain with TLS, so you can
connect from laptops, mobile, and other machines outside your local network.

This guide assumes:
- You are the only user (or a small group of trusted users sharing one Bearer token)
- You can get a TLS certificate for a domain that points to the host
- The host is always on (cron / launchd / systemd keeps the server alive)

### Prerequisites

- Linux or macOS host with Python 3.10+
- A domain name pointing at the host (IPv4 or IPv6). For IPv6 see [shigechika/macos-ddns6](https://github.com/shigechika/macos-ddns6) for an example DDNS setup
- A TLS certificate. [acme.sh](https://github.com/acmesh-official/acme.sh) with DNS-01 challenge works well (supports IPv6-only hosts and wildcard certs)
- A J-Quants account + API key

### 1. Install jquants-mcp

```bash
uv tool install jquants-mcp      # or: pipx install jquants-mcp
```

### 2. Configure

Either `~/.config/jquants-mcp/config.ini`:

```ini
[jquants]
api_key = <your J-Quants API key>

[server]
ssl_certfile = /etc/letsencrypt/live/mcp.example.com/fullchain.pem
ssl_keyfile = /etc/letsencrypt/live/mcp.example.com/privkey.pem
bearer_token = <generated token>
```

Or via environment variables (`JQUANTS_API_KEY`, `SSL_CERTFILE`, `SSL_KEYFILE`, `MCP_BEARER_TOKEN`).

Generate a Bearer token:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Run

```bash
jquants-mcp --transport streamable-http --host 0.0.0.0 --port 8080
```

`--host 0.0.0.0` binds on all interfaces. Use `--host ::` for IPv6 dual-stack, or stick with the default `127.0.0.1` if you only need local access.

### Run as a background service

**macOS (launchd):** Create `~/Library/LaunchAgents/com.example.jquants-mcp.plist` with KeepAlive + RunAtLoad. Point `JQUANTS_API_TOML_PATH` at a non-sandboxed path if you hit the macOS 26+ TCC issue — see the [macOS launchd note](../../README.md#macos-launchd-note) in README.

**Linux (systemd):** Create `/etc/systemd/system/jquants-mcp.service`:

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

### 4. Connect from Claude clients

#### Claude Code / Claude Desktop via mcp-stdio

Claude Code has a bug that drops the `Authorization` header on HTTP transports ([claude-code#28293](https://github.com/anthropics/claude-code/issues/28293)). Use [mcp-stdio](https://pypi.org/project/mcp-stdio/) as a proxy:

```bash
# Claude Code
claude mcp add jquants-mcp --env MCP_BEARER_TOKEN=<TOKEN> \
  -- uvx mcp-stdio https://mcp.example.com:8080/mcp
```

For Claude Desktop, edit the MCP config to spawn `mcp-stdio` with the same env var.

#### Claude Code (direct HTTP)

Once the header bug is fixed, direct HTTP transport will work:

```bash
claude mcp add jquants-mcp \
  --transport http https://mcp.example.com:8080/mcp \
  --header "Authorization: Bearer <TOKEN>"
```

### 5. Operate

- Logs: `journalctl -u jquants-mcp -f` (systemd) or `/tmp/jquants-mcp.err.log` (launchd default)
- Cache DB: `~/.cache/jquants-mcp/cache.db` grows as you fetch data — see [Caching](../../README.md#caching) in README
- Populate cache: `jquants-mcp daily-fetch` or `uv run scripts/daily_fetch.py` (schedule daily via cron / launchd timer)

---

## When to graduate to Cloud Run

Move to [gcp.md](gcp.md) when:
- You want to share the server with people who have their own J-Quants accounts
- You want proper OAuth login instead of a shared Bearer token
- You want Claude Desktop Connectors UI / Claude mobile OAuth flow
- The host is unreliable and you need autoscaling / zero-ops

Everything else stays the same — the same J-Quants API, the same cache schema, the same tools.

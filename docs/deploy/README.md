# Deployment Overview

jquants-mcp can be deployed in four shapes. Pick the one that matches your usage pattern.

| Shape | Who runs it | Cost | Setup effort | Best for |
|---|---|---|---|---|
| **stdio** (local) | One user, one machine | Free | < 5 min | Single-user desktop via Claude Code / Claude Desktop; no persistent cache needed |
| **Docker Compose** (local) | One user, one machine | Free | < 10 min | Local HTTP server without installing Python; cache persists across restarts |
| **Self-hosted HTTP** | One or a few trusted users, one host | Host + J-Quants plan | ~1 hour | Homelab / always-on server reachable from mobile or other machines |
| **Cloud Run** (GCP) | Multiple users, OAuth auth | GCP (~\$0–\$10/mo for low traffic) + J-Quants plan | 2–4 hours first time | Family / team, mobile clients, OAuth login per user |

## stdio

```mermaid
graph BT
    C["J-Quants API v2"]
    B["jquants-mcp (local)"]
    A["Claude Code / Claude Desktop"]

    A -->|stdio| B
    B -->|HTTPS| C
```

- Launched by the MCP client as a subprocess (`uvx jquants-mcp` or `claude mcp add`)
- Single API key via env var, config file, or `jquants-mcp login` (PKCE)
- Local SQLite cache at `~/.cache/jquants-mcp/cache.db`
- Cannot be reached from mobile or a different machine

Set up: see the main [README](../../README.md#installation).

## Docker Compose

```mermaid
graph BT
    C["J-Quants API v2"]
    B["jquants-mcp (Docker container)"]
    A["Claude Code / Claude Desktop"]

    A -->|"HTTP localhost:8080"| B
    B -->|HTTPS| C
```

- No Python installation required — just Docker
- Runs as a persistent local HTTP server on `http://localhost:8080/mcp`
- Cache stored in a named Docker volume; survives container restarts
- Optional: set `ENABLE_DAILY_FETCH=true` for automatic weekday cache updates

Set up: see [local.md](local.md) (Option A).

## Self-hosted HTTP

```mermaid
graph BT
    D["J-Quants API v2"]
    C["jquants-mcp (your host)"]
    B["mcp-stdio (proxy)"]
    A["Claude Code / Claude Desktop"]

    A -->|stdio| B
    B -->|"HTTPS + Bearer"| C
    C -->|HTTPS| D
```

- Runs on any host that can hold a TLS cert (laptop at home, NUC, VPS)
- Streamable HTTP transport, Bearer token authentication
- One SQLite cache on the host, shared between invocations
- Mobile clients work via `mcp-stdio` proxy (Claude Code header bug workaround)

Set up: see [local.md](local.md) (Option B).

## Cloud Run (GCP)

```mermaid
graph BT
    C["J-Quants API v2"]
    D["GCS (cache.db)"]
    E["Firestore\n(users, oauth_state)"]
    B["Cloud Run jquants-mcp"]
    A["Claude mobile / Claude Desktop / Claude Code"]
    F["Self-hosted publisher (cron)"]

    A -->|"OAuth 2.1"| B
    B -->|HTTPS| C
    B -->|read| D
    B <-->|read/write| E
    F -->|write| D

    style F fill:#4a5,stroke:#333,color:#fff
```

- Managed by Google Cloud Run, autoscaling, HTTPS out-of-the-box
- Multi-user: per-user encrypted J-Quants API keys in Firestore, OAuth 2.1 login
- Allowlist (`JQUANTS_ALLOWED_EMAILS`) controls who can sign in
- Requires a self-hosted publisher to populate `cache.db` in GCS
- Compatible with Claude Desktop Connectors, Claude mobile, Claude Code

Set up: see [gcp.md](gcp.md).

## Decision flowchart

```mermaid
flowchart TD
    Q1{"Will anyone other than you use it?"}
    Q1 -->|No| Q2{"Does your mobile or another<br/>machine need to reach it?"}
    Q1 -->|Yes| Q3{"Do you want OAuth login so<br/>users bring their own<br/>J-Quants API keys?"}

    Q2 -->|No| Q4{"Do you have Docker and want<br/>a persistent local HTTP server?"}
    Q2 -->|Yes| R3["self-hosted HTTP"]

    Q4 -->|Yes| R1["Docker Compose"]
    Q4 -->|No| R2["stdio"]

    Q3 -->|Yes| R4["Cloud Run"]
    Q3 -->|No| R3

    style R1 fill:#4a5,stroke:#333,color:#fff
    style R2 fill:#4a5,stroke:#333,color:#fff
    style R3 fill:#4a5,stroke:#333,color:#fff
    style R4 fill:#4a5,stroke:#333,color:#fff
```

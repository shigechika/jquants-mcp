# Deployment Overview

jquants-mcp can be deployed in three shapes. Pick the one that matches your usage pattern.

| Shape | Who runs it | Cost | Setup effort | Best for |
|---|---|---|---|---|
| **stdio** (local) | One user, one machine | Free | < 5 min | Single-user desktop use via Claude Code / Claude Desktop |
| **self-hosted HTTP** | One or a few trusted users, one host | Host + J-Quants plan | ~1 hour | Homelab / always-on server reachable from mobile or laptop |
| **Cloud Run** (GCP) | Multiple users, OAuth auth | GCP (~\$0–\$10/mo for low traffic) + J-Quants plan | 2–4 hours first time | Family / team, mobile clients, remote auth |

## stdio

```mermaid
graph LR
    A["Claude Code<br/>Claude Desktop"] -->|stdio| B["jquants-mcp<br/>(local)"]
    B -->|HTTPS| C["J-Quants<br/>API v2"]
```

- Launched by the MCP client as a subprocess (`uvx jquants-mcp` or `claude mcp add`)
- Single API key via env var, config file, or `jquants-mcp login` (PKCE)
- Local SQLite cache at `~/.cache/jquants-mcp/cache.db`
- Cannot be reached from mobile or a different machine

Set up: see the main [README](../../README.md#installation).

## Self-hosted HTTP

```mermaid
graph LR
    A["Claude Code<br/>Claude Desktop"] -->|stdio| B["mcp-stdio<br/>(proxy)"]
    B -->|"HTTPS + Bearer"| C["jquants-mcp<br/>(your host)"]
    C -->|HTTPS| D["J-Quants<br/>API v2"]
```

- Runs on any host that can hold a TLS cert (laptop at home, NUC, VPS)
- Streamable HTTP transport, Bearer token or OAuth authentication
- One SQLite cache on the host, shared between invocations
- Mobile clients work via `mcp-stdio` proxy (Claude Code header bug workaround)

Set up: see [local.md](local.md).

## Cloud Run (GCP)

```mermaid
graph LR
    A["Claude mobile<br/>Claude Desktop<br/>Claude Code"] -->|"OAuth 2.1"| B["Cloud Run<br/>jquants-mcp"]
    B -->|HTTPS| C["J-Quants<br/>API v2"]
    B -->|read| D["GCS<br/>(cache.db)"]
    B <-->|read/write| E["Firestore<br/>(users, oauth_state)"]
    F["Self-hosted<br/>publisher<br/>(cron)"] -->|write| D

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
    Q1["Will anyone other than you use it?"]
    Q1 -->|No| Q2["Does your mobile or another<br/>machine need to reach it?"]
    Q1 -->|Yes| Q3["Do you want OAuth login so<br/>users bring their own<br/>J-Quants API keys?"]

    Q2 -->|No| R1["stdio"]
    Q2 -->|Yes| R2["self-hosted HTTP"]

    Q3 -->|Yes| R3["Cloud Run"]
    Q3 -->|No| R4["self-hosted HTTP<br/>(shared Bearer token)"]

    style R1 fill:#4a5,stroke:#333,color:#fff
    style R2 fill:#4a5,stroke:#333,color:#fff
    style R3 fill:#4a5,stroke:#333,color:#fff
    style R4 fill:#4a5,stroke:#333,color:#fff
```

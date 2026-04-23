# Deployment Overview

jquants-mcp can be deployed in three shapes. Pick the one that matches your usage pattern.

| Shape | Who runs it | Cost | Setup effort | Best for |
|---|---|---|---|---|
| **stdio** (local) | One user, one machine | Free | < 5 min | Single-user desktop use via Claude Code / Claude Desktop |
| **self-hosted HTTP** | One or a few trusted users, one host | Host + J-Quants plan | ~1 hour | Homelab / always-on server reachable from mobile or laptop |
| **Cloud Run** (GCP) | Multiple users, OAuth auth | GCP (~\$0–\$10/mo for low traffic) + J-Quants plan | 2–4 hours first time | Family / team, mobile clients, remote auth |

## stdio

```
┌─────────────┐  stdio  ┌──────────────┐  HTTPS  ┌─────────┐
│ Claude Code │ ────── │  jquants-mcp │ ───────▶│ J-Quants│
│   Desktop   │         │  (local)     │          │  API v2 │
└─────────────┘         └──────────────┘          └─────────┘
```

- Launched by the MCP client as a subprocess (`uvx jquants-mcp` or `claude mcp add`)
- Single API key via env var, config file, or `jquants-mcp login` (PKCE)
- Local SQLite cache at `~/.cache/jquants-mcp/cache.db`
- Cannot be reached from mobile or a different machine

Set up: see the main [README](../../README.md#installation).

## Self-hosted HTTP

```
┌───────────┐  HTTPS + Bearer  ┌──────────────┐  HTTPS  ┌─────────┐
│ mcp-stdio │ ───────────────▶│  jquants-mcp │ ───────▶│ J-Quants│
│  (proxy)  │                  │  (your host) │          │  API v2 │
└───────────┘                  └──────────────┘          └─────────┘
      ▲
      │ stdio
┌─────────────┐
│ Claude Code │
│   Desktop   │
└─────────────┘
```

- Runs on any host that can hold a TLS cert (laptop at home, NUC, VPS)
- Streamable HTTP transport, Bearer token or OAuth authentication
- One SQLite cache on the host, shared between invocations
- Mobile clients work via `mcp-stdio` proxy (Claude Code header bug workaround)

Set up: see [local.md](local.md).

## Cloud Run (GCP)

```
┌───────────────┐            ┌──────────────────┐      ┌─────────┐
│ Claude mobile │  OAuth 2.1 │   Cloud Run      │ HTTPS│ J-Quants│
│ Claude Desktop│ ──────────▶│   jquants-mcp    │ ────▶│  API v2 │
│ Claude Code   │            └─────────┬────────┘      └─────────┘
└───────────────┘                      │
                                        ├──▶ GCS (cache.db snapshot)
                                        └──▶ Firestore (users, oauth_state)
                                                 ▲
                                                 │
                                        ┌────────┴───────┐
                                        │ Self-hosted    │
                                        │ publisher host │
                                        │ (cron / cache  │
                                        │  fetcher)      │
                                        └────────────────┘
```

- Managed by Google Cloud Run, autoscaling, HTTPS out-of-the-box
- Multi-user: per-user encrypted J-Quants API keys in Firestore, OAuth 2.1 login
- Allowlist (`JQUANTS_ALLOWED_EMAILS`) controls who can sign in
- Requires a self-hosted publisher to populate `cache.db` in GCS
- Compatible with Claude Desktop Connectors, Claude mobile, Claude Code

Set up: see [gcp.md](gcp.md).

## Decision flowchart

```
                        Will anyone other than you use it?
                                    │
                        ┌───────────┴───────────┐
                       No                      Yes
                        │                       │
              Does your mobile phone     Do you want OAuth login
              or another machine need    so users bring their own
              to reach it?               J-Quants API keys?
                        │                       │
               ┌────────┴────────┐      ┌──────┴──────┐
              No                 Yes   Yes            No
               │                  │     │              │
             stdio         self-hosted  Cloud Run   self-hosted
                               HTTP                     HTTP
                                                    (single Bearer token
                                                     shared with trusted
                                                     users)
```

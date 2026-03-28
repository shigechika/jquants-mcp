# CLAUDE.md

## Project Overview

jquants-dat-mcp is an MCP server that retrieves Japanese stock market data via J-Quants API v2.
Built with FastMCP v3, httpx, SQLite cache. Supports multi-user OAuth and Cloud Run deployment.

## Commands

```bash
uv sync --dev          # Install dependencies
uv run pytest -v       # Run tests
uv run ruff check src/ tests/   # Lint
uv run ruff format src/ tests/  # Format
```

## Architecture

- `src/jquants_dat_mcp/` — Main package
  - `server.py` — FastMCP server, per-user client management, tool registration
  - `client.py` — httpx async client with rate limiting, retry, pagination
  - `config.py` — configparser + env vars hybrid configuration
  - `cache/store.py` — 2-tier SQLite cache (Tier1: row-level, Tier2: response-level with TTL)
  - `tools/` — Tool modules registered via `register(mcp, get_client, get_cache)` pattern
  - `auth.py` — Bearer token + Google/GitHub OAuth authentication
  - `google_provider.py` — Custom Google OAuth 2.0 provider (pending upstream FastMCP release)
  - `crypto.py` — AES-256-GCM encryption for user API keys
  - `db/users.py` — Per-user API key storage (SQLite, encrypted)
  - `validators.py` — Input validation (code, date, sector)
  - `settings/` — Web UI for API key registration (/settings endpoint)
  - `oauth_kv_store.py` — SQLite-backed OAuth state persistence
- `scripts/` — Operational scripts
  - `daily_fetch.py` — Daily data fetch (called from jpx-short-report)
  - `import_csv_to_cache.py` — CSV bulk import to cache
  - `bulk_fetch_all.py` — Historical data bulk fetch
  - `gcs_sync.py` — GCS cache sync for Cloud Run
  - `mcp-stdio-proxy.py` — stdio-to-HTTP proxy for Claude Desktop/Code
  - `entrypoint.sh` — Docker/Cloud Run entrypoint
- `tests/` — pytest + pytest-asyncio tests (306 tests)

## Key Patterns

- Tools are closures inside `register()` functions, capturing `get_client` and `get_cache` callables
- Multi-user mode: per-user `JQuantsClient` instances resolved via OAuth user ID
- Single-user mode: global `_client` with env/config API key (backward compatible)
- Tests patch `server_module._settings`, `_client`, `_cache` globals directly
- `_call()` helper uses `mcp.call_tool(name, kwargs)` then parses `result.content[0].text`
- docstring は英語で記述、コード内コメントは日本語
- README.md は英語、README.ja.md は日本語
- Commit messages in English (Public repository)

## CI/CD

- **CI**: GitHub Actions — ruff lint/format + pytest on Python 3.10–3.13
- **CD**: GitHub Actions — auto-deploy to Cloud Run after CI passes on main (WIF auth, keyless)
- Manual deploy: `workflow_dispatch` from Actions tab

## Deployment Targets

- **Local (stdio)**: `jquants-dat-mcp` — single user, env/config API key
- **Remote (m1.local)**: Streamable HTTP + TLS + Bearer token via `mcp.aikawa.jp:8080`
- **Cloud Run**: `us-west1`, Google OAuth, multi-user, GCS cache persistence

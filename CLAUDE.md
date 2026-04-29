# CLAUDE.md

## Project Overview

jquants-mcp is an MCP server that retrieves Japanese stock market data via J-Quants API v2.
Built with FastMCP v3, httpx, SQLite cache. Supports multi-user OAuth and Cloud Run deployment.

## Commands

```bash
uv sync --dev          # Install dependencies
uv run pytest -v       # Run tests
uv run ruff check src/ tests/   # Lint
uv run ruff format src/ tests/  # Format
```

## Architecture

- `src/jquants_mcp/` — Main package
  - `server.py` — FastMCP server, per-user client management, tool registration
  - `client.py` — httpx async client with rate limiting, retry, pagination
  - `config.py` — configparser + env vars hybrid configuration
  - `cache/store.py` — 2-tier SQLite cache (Tier1: row-level, Tier2: response-level with TTL)
  - `tools/` — Tool modules registered via `register(mcp, get_client, get_cache)` pattern
  - `auth.py` — Bearer token + Google/GitHub OAuth authentication (Google via upstream FastMCP GoogleProvider)
  - `crypto.py` — AES-256-GCM encryption for user API keys
  - `db/users.py` — Per-user API key storage (SQLite, encrypted)
  - `validators.py` — Input validation (code, date, sector)
  - `settings/` — Web UI for API key registration (/settings endpoint)
  - `oauth_kv_store.py` — SQLite-backed OAuth state persistence
- `scripts/` — Operational scripts
  - `daily_fetch.py` — Daily data fetch (cron / scheduled-task companion for cache population)
  - `bulk_fetch_all.py` — Historical data bulk fetch via J-Quants Bulk API
  - `gcs_sync.py` — GCS auth DB sync for Cloud Run (users.db + oauth_state.db)
  - `gcs_export_cache.py` — Export cache.db to GCS (used by the daily publisher)
  - `rotate_encryption_key.py` — Re-encrypt user API keys during MCP_ENCRYPTION_KEY rotation
  - `collect_metrics.py` / `load_test.py` — Cloud Run sizing helpers
  - `entrypoint.sh` — Docker/Cloud Run entrypoint
- `tests/` — pytest + pytest-asyncio tests (412 tests)

## Key Patterns

- Tools are closures inside `register()` functions, capturing `get_client` and `get_cache` callables
- Multi-user mode: per-user `JQuantsClient` instances resolved via OAuth user ID
- Single-user mode: global `_client` with env/config API key (backward compatible)
- Tests patch `server_module._settings`, `_client`, `_cache` globals directly
- `_call()` helper uses `mcp.call_tool(name, kwargs)` then parses `result.content[0].text`
- Code is English-only: docstrings, inline comments, log messages, exception messages (Public repository)
- README.md is in English, README.ja.md is the Japanese translation
- Commit messages in English
- Existing Japanese comments are being migrated to English gradually; new code should always be written in English

## Security

- Cloud Run secrets must use Secret Manager, not plain env vars
- User API keys encrypted with AES-256-GCM (crypto.py)
- All tool exception handlers must catch DecryptionError
- CLI default --host is 127.0.0.1 (not 0.0.0.0)
- Dockerfile runs as non-root user (appuser)

## CI/CD

- **CI**: GitHub Actions — ruff lint/format + pytest on Python 3.10–3.13
- **CD**: GitHub Actions — auto-deploy to Cloud Run after CI passes on main (WIF auth, keyless)
- Manual deploy: `workflow_dispatch` from Actions tab

## Deployment Targets

- **Local (stdio)**: `jquants-mcp` — single user, env/config API key
- **Remote (self-hosted)**: Streamable HTTP + TLS + Bearer token
- **Cloud Run**: `us-west1`, Google OAuth, multi-user, GCS startup copy (cache.db)

## CI/CD Notes

- CD workflow declares ALL env vars and secrets — never use manual `gcloud run services update` (it gets overwritten by next CD deploy)
- `gcloud storage cp` with parallel composite upload corrupts SQLite files — use `parallel_composite_upload_enabled=False`
- Cloud Run: cache.db is downloaded from GCS at startup (`entrypoint.sh`), not gcsfuse-mounted
- Cloud Run GCS daemon uploads only users.db and oauth_state.db (not cache.db — owned by self-hosted server)
- gcsfuse is NOT viable for large SQLite DBs (>100 MB) due to random read latency — see `docs/gcsfuse-postmortem.md`
- Cloud Run: 1 vCPU + 4Gi memory (cache.db ~2.7 GB after 5-year trim + VACUUM)
- Always research technology compatibility BEFORE implementing (e.g., "gcsfuse sqlite" would have revealed issues immediately)

## Cache Plan Scoping

- All Tier 1 cache tables have `plan` column (data stored as `plan='standard'`)
- `daily_fetch.py` must include `plan` in all INSERTs
- `_build_where_clause` enforces plan-based date restrictions at query time
- Plan data retention: Free=2y (12w delay), Light=5y, Standard=10y, Premium=all
- `sync_plans.py` is removed — no longer copy data between plans

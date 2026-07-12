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
  - `request_context.py` — Request-scoped plan contextvar set by `PlanContextMiddleware.on_call_tool`; read by `CacheStore._effective_plan` so each user's plan date window applies without threading `plan` through tools
- `scripts/` — Operational scripts
  - `daily_fetch.py` — Daily data fetch (cron / scheduled-task companion for cache population)
  - `bulk_fetch_all.py` — Historical data bulk fetch via J-Quants Bulk API
  - `gcs_sync.py` — Cloud Run cache.db startup download from GCS (`--init-cache`). Auth DBs are **no longer synced here**: `users.db`/`oauth_state.db` moved to Firestore on Cloud Run, so `_DOWNLOAD_FILES`/`_UPLOAD_FILES` are empty and `--init`/`--daemon` are auth-DB no-ops
  - `gcs_export_cache.py` — Export cache.db to GCS (used by the daily publisher)
  - `rotate_encryption_key.py` — Re-encrypt user API keys during MCP_ENCRYPTION_KEY rotation
  - `collect_metrics.py` / `load_test.py` — Cloud Run sizing helpers
  - `entrypoint.sh` — Docker/Cloud Run entrypoint
- `tests/` — pytest + pytest-asyncio tests (1000+ tests as of 2026-05)

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
- Cloud Run user/OAuth data lives in **Firestore** (`FirestoreUserStore` in `db/users_firestore.py`; OAuth client store via `FirestoreStore`; selected at `server.py:559-566`), not in GCS-synced SQLite — so the `gcs_sync` daemon uploads nothing (`_UPLOAD_FILES` is empty; see `docs/runbooks/firestore-*.md`). cache.db is still GCS-downloaded at startup and owned by the self-hosted server
- gcsfuse is NOT viable for large SQLite DBs (>100 MB) due to random read latency — see `docs/gcsfuse-postmortem.md`
- Cloud Run: 2 vCPU + 8Gi memory (cache.db ~2.7 GB after 5-year trim + VACUUM; reload briefly holds ~2x cache.db in /tmp tmpfs, 6Gi caused SIGBUS)
- Always research technology compatibility BEFORE implementing (e.g., "gcsfuse sqlite" would have revealed issues immediately)

## Cache Plan Scoping

- Tier 1 cache data is **plan-agnostic** — there is no `plan` column. The legacy
  column was dropped by the `migrate_drop_plan` migration (`PRAGMA user_version=2`),
  now shared in `cache/schema.py` and called from both `cache/store.py` and
  `daily_fetch.py` (the hand-mirrored copies were removed — they produced
  structurally-degraded tables). Do NOT add `plan` to INSERTs.
- Plan-based date restriction is enforced **at query time**: `_build_where_clause`
  (row queries) and `_plan_bounds` (latest-aggregate readers) clamp the date range to
  `_plan_date_bounds(_effective_plan())`. The stored rows are not tagged by plan; only
  the returned date window depends on the plan.
- `_effective_plan()` resolves: explicit `plan` arg > per-request plan (set by
  `PlanContextMiddleware` from the authenticated user, see `request_context.py`) >
  `default_plan`. This applies each user's plan window on multi-user deployments;
  single-user / bearer paths fall back to `default_plan`.
- The per-request path's wiring (middleware → contextvar → cache gating) IS
  unit-tested by `TestPlanContextMiddlewareE2E` (it patches `get_access_token`).
  What stays live-only is whether FastMCP actually delivers a token to
  `on_call_tool` in production — a mock cannot prove that; verify on Cloud Run
  via the `Resolved plan=...` INFO log (emitted on a plan-cache miss).
- Plan data retention: Free=2y (12w delay), Light=5y, Standard=10y, Premium=all
- `sync_plans.py` is removed — no longer copy data between plans

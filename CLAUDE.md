# CLAUDE.md

## Project Overview

jquants-mcp is an MCP server that retrieves Japanese stock market data via J-Quants API v2.
The stdio server (`server.py` + `tools/`) is built on the official `mcp` SDK's `mcp.server.fastmcp.FastMCP`.
The standalone FastMCP v3 package (`fastmcp`) remains a dependency for the still-unreachable HTTP/OAuth
path (`auth.py`, `settings/`, the non-stdio branch of `run_server()`) — see "Architecture" below.
httpx, SQLite cache. Supports multi-user OAuth and Cloud Run deployment.

## Commands

```bash
uv sync --dev          # Install dependencies
uv run pytest -v       # Run tests
uv run ruff check src/ tests/   # Lint
uv run ruff format src/ tests/  # Format

uv run python scripts/smoke_test.py            # Exercise every tool against real data
uv run python scripts/smoke_test.py --only earnings --traceback   # Debug one tool
```

## Architecture

- `src/jquants_mcp/` — Main package
  - `server.py` — Official-SDK `FastMCP` server (stdio), per-user client management, tool registration
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
  - `request_context.py` — Request-scoped plan contextvar; read by `CacheStore._effective_plan` as a fallback before its `plan_resolver` (see below), so each user's plan date window applies without threading `plan` through tools
- `scripts/` — Operational scripts
  - `daily_fetch.py` — Daily data fetch (cron / scheduled-task companion for cache population)
  - `bulk_fetch_all.py` — Historical data bulk fetch via J-Quants Bulk API
  - `gcs_sync.py` — Cloud Run cache.db startup download from GCS (`--init-cache`). Auth DBs are **no longer synced here**: `users.db`/`oauth_state.db` moved to Firestore on Cloud Run, so `_DOWNLOAD_FILES`/`_UPLOAD_FILES` are empty and `--init`/`--daemon` are auth-DB no-ops
  - `gcs_export_cache.py` — Export cache.db to GCS (used by the daily publisher)
  - `rotate_encryption_key.py` — Re-encrypt user API keys during MCP_ENCRYPTION_KEY rotation
  - `collect_metrics.py` / `load_test.py` — Cloud Run sizing helpers
  - `smoke_test.py` — Live smoke test: runs **every registered tool** against real data (in-process, or `--url` against a deployment) and fails on empty/stale/error answers. `smoke_harness.py` is the server-agnostic engine; `smoke_probes.py` holds the per-tool specs. Needs a populated cache + API key, so it runs on the host that has them — not in CI. CI enforces only the coverage half (`tests/test_smoke_probes.py`: a new tool without a probe spec fails the build)
  - `entrypoint.sh` — Docker/Cloud Run entrypoint
- `tests/` — pytest + pytest-asyncio tests (1000+ tests as of 2026-05)

## Key Patterns

- Tools are closures inside `register()` functions, capturing `get_client` and `get_cache` callables
- Multi-user mode: per-user `JQuantsClient` instances resolved via OAuth user ID
- Single-user mode: global `_client` with env/config API key (backward compatible)
- Tests patch `server_module._settings`, `_client`, `_cache` globals directly
- `_call()` test helper uses `mcp.call_tool(name, kwargs)`; unpack `_, structured = await ...` for tools annotated `-> dict[str, Any]` (the SDK returns `(unstructured, structured)`), or parse `result[0].text` for tools annotated with a bare `-> dict` (no output schema generated, so no tuple)
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
- `_effective_plan()` resolves: explicit `plan` arg > per-request plan (the
  `request_context.py` contextvar, if a caller has pushed one explicitly) >
  `CacheStore`'s constructor-injected `plan_resolver` (wired to `server.py`'s
  `_resolve_current_plan`, which reads the gateway-injected identity via
  `_current_user_id()` and looks up that user's plan) > `default_plan`. This
  applies each user's plan window on multi-user deployments; single-user /
  no-gateway-identity paths fall back to `default_plan`.
- The identity → plan-resolver wiring IS unit-tested by `TestPlanResolutionE2E`
  (`tests/test_tools_screener.py`, injects `plan_resolver` directly since the
  `mock_env` fixture there builds `CacheStore` without going through
  `_get_cache()`). What stays live-only is whether the stdio gateway (mcp-stdio
  `serve --user-env`) actually delivers the identity in production — a mock
  cannot prove that; verify via the `Resolved plan=...` INFO log (emitted on a
  plan-cache miss).
- Plan data retention: Free=2y (12w delay), Light=5y, Standard=10y, Premium=all
- `sync_plans.py` is removed — no longer copy data between plans

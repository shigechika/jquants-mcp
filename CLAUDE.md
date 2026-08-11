# CLAUDE.md

## Project Overview

jquants-mcp is an MCP server that retrieves Japanese stock market data via J-Quants API v2.
The server is **stdio-only** and is built on the official `mcp` SDK's `mcp.server.fastmcp.FastMCP`
(`server.py` + `tools/`). The standalone FastMCP v3 package (`fastmcp`) is **no longer a dependency**:
its last in-repo users — the HTTP/OAuth surface (`auth.py`, `settings/`, `oauth_kv_store.py`, the
non-stdio branch of `run_server()`) and `scripts/smoke_test.py`'s client — were deleted or ported to
the official SDK in #601. httpx, SQLite cache. Multi-user OAuth is terminated **upstream** by the
Cloud Run gateway (`oauth2-proxy` + `mcp-stdio serve`), not in this process — see "Deployment Targets".

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
  - `cache/store.py` — 2-tier SQLite cache (Tier1: row-level, Tier2: response-level with TTL); background `PRAGMA quick_check` integrity verification (`verify_and_record`) with a `(dev, ino)`-keyed sidecar (`cache.db.verified.json`) so a fresh per-message `CacheStore` reuses a prior generation's verdict instead of re-running the multi-second check on every claude.ai message
  - `tools/` — Tool modules registered via `register(mcp, get_client, get_cache)` pattern
  - `crypto.py` — AES-256-GCM encryption for user API keys
  - `db/users.py` — Per-user API key storage (SQLite, encrypted)
  - `validators.py` — Input validation (code, date, sector)
  - `allowlist.py` — `JQUANTS_ALLOWED_EMAILS` gate; the gateway-injected principal *is* the verified email, so `server.py` passes it straight to `is_email_allowed`
  - `request_context.py` — Request-scoped plan contextvar; read by `CacheStore._effective_plan` as a fallback before its `plan_resolver` (see below), so each user's plan date window applies without threading `plan` through tools
- `scripts/` — Operational scripts
  - `daily_fetch.py` — Daily data fetch (cron / scheduled-task companion for cache population)
  - `bulk_fetch_all.py` — Historical data bulk fetch via J-Quants Bulk API
  - `gcs_sync.py` — Cloud Run cache.db startup download from GCS (`--init-cache`). Skips the download (and the atomic replace that always allocates a new inode, which would invalidate `CacheStore`'s `(dev, ino)` integrity sidecar) when the GCS generation of the effective object -- whichever `cache.db.zst`/`cache.db` the zst-then-fallback precedence would actually use -- is unchanged since the last successful download, tracked in a `cache.db.generation.json` sidecar. On Cloud Run that skip is dormant since #584 removed the periodic poll it was written for (a cold start always starts from an empty cache dir), but it stays correct and load-bearing for repeat `--init-cache` runs against a populated dir. Auth DBs are **no longer synced here**: `users.db`/`oauth_state.db` moved to Firestore on Cloud Run, so `_DOWNLOAD_FILES`/`_UPLOAD_FILES` are empty and `--init`/`--daemon` are auth-DB no-ops
  - `gcs_export_cache.py` — Export cache.db to GCS (used by the daily publisher)
  - `verify_cache.py` — Stand-alone cache-integrity prewarm CLI: runs the same `verify_and_record` quick_check the `CacheStore` sidecar uses, ahead of the next request, so a freshly downloaded cache.db already has a warm `(dev, ino)` sidecar by the time the first per-message child process connects. Invoked by `entrypoint-stdio.sh` (backgrounded after the synchronous startup download)
  - `rotate_encryption_key.py` — Re-encrypt user API keys during MCP_ENCRYPTION_KEY rotation
  - `collect_metrics.py` / `load_test.py` — Cloud Run sizing helpers
  - `smoke_test.py` — Live smoke test: runs **every registered tool** against real data (in-process, or `--url` against a deployment) and fails on empty/stale/error answers. `smoke_harness.py` is the server-agnostic engine; `smoke_probes.py` holds the per-tool specs. Needs a populated cache + API key, so it runs on the host that has them — not in CI. CI enforces only the coverage half (`tests/test_smoke_probes.py`: a new tool without a probe spec fails the build)
  - `entrypoint.sh` — Docker/Cloud Run entrypoint for the `jquants-mcp` service (streamable-http, no longer CD-deployed — see "Deployment Targets")
  - `entrypoint-stdio.sh` — Docker/Cloud Run entrypoint for the `jquants` service (`mcp-stdio serve`, behind an `oauth2-proxy` sidecar); downloads cache.db synchronously at startup like `entrypoint.sh`, then backgrounds `verify_cache.py` to warm the integrity sidecar. It has **no in-container refresh mechanism** (#584 removed the 15-minute `cache-poll.crontab` supercronic poll): with `min-instances=0` every cold start already re-downloads a current cache.db, and the only window a refresh could help is an instance staying warm across the publisher's once-a-weekday export. There, not-yet-cached days fall through to the live API (correct, just slower), while **corrections to already-cached rows do not** — the cache-vs-API decision is presence-based and Tier 1 `get_rows` applies no TTL, so a restated statement or retroactive split adjustment stays stale until the instance recycles (measured lifetimes 15-26 min under `min-instances=0`, i.e. the same order as the 15-minute poll it replaces). `entrypoint.sh`'s Pub/Sub-pushed reload route has no equivalent here because a stdio-only server exposes no HTTP route for a push to land on; push-based alternatives were designed and rejected as not worth the moving parts (see #584)
- `tests/` — pytest + pytest-asyncio tests (1272 tests as of 2026-08, after the HTTP/OAuth removal in #601)

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
- The server binds no network socket at all (stdio-only since #601), so there is no
  `--host`/`--port` to get wrong; exposure decisions belong to the gateway in front of it
- Dockerfile runs as non-root user (appuser)

## CI/CD

- **CI**: GitHub Actions — ruff lint/format + pytest on Python 3.10–3.13
- **CD**: GitHub Actions — deploys the `jquants` service (WIF auth, keyless; `--container app --image` only, never scaling/CPU flags) after CI passes on main, or via `workflow_dispatch`. Does **not** deploy `jquants-mcp` (the older service) at all — see "Deployment Targets" and #588

## Deployment Targets

- **Local (stdio)**: `jquants-mcp` — single user, env/config API key. This is
  the **only** transport the code still speaks. Running the command with no
  subcommand starts the server, and it takes no transport options at all:
  `--transport`, `--host`, `--port`, `--ssl-*`, `--bearer-token`,
  `--github-client-*` and `--oauth-base-url` are gone (#601). Only `login`
  and `logout` remain as subcommands.
- **Remote (self-hosted)**: front the stdio server with a gateway
  (`mcp-stdio serve`, as Cloud Run does below). The in-process
  Streamable-HTTP + TLS + Bearer-token listener that used to serve this role
  is **gone**.
- **Cloud Run, `jquants-mcp` service** (older, no longer CD-deployed): `us-west1`,
  multi-user, GCS startup copy (cache.db), `entrypoint.sh`
  (streamable-http transport). That transport was removed from `server.py`
  in #566 (official mcp SDK migration, stdio-only), and its Google OAuth
  provider (`auth.py`, via the upstream FastMCP `GoogleProvider`), the
  `/settings` web UI and `oauth_kv_store.py` were **deleted** in #601 —
  `entrypoint.sh` was never updated to match, so it can no longer start a
  server at all and this service's revisions are frozen at whatever was
  last manually deployed; `cd.yml` no longer targets it (#588).
  Existing deployed revisions are unaffected — Cloud Run's atomic
  cutover keeps them serving. Scheduled for decommissioning (#568).
- **Cloud Run, `jquants` service** (CD-deployed, same project/region):
  an `oauth2-proxy` sidecar (Google OAuth) in front of `mcp-stdio serve
  --enable-oauth --trusted-user-header X-Forwarded-Email --user-env
  JQUANTS_MCP_USER`, which spawns this repo's stdio server as a per-session
  child process. Uses `entrypoint-stdio.sh`, not `entrypoint.sh`. This is
  the target architecture streamable-http removal migrated toward, and is
  the only service `cd.yml` deploys (#588).
  Verified end-to-end in production with a real Google account (see
  "Cache Plan Scoping" below). Firestore token store wired in
  (`--token-store-firestore`, mcp-stdio 0.42.0+, #575); instance restarts
  no longer force reconnected clients to re-authenticate.

## CI/CD Notes

- CD workflow (`jquants` service) updates **only** the `app` container's image (`--container app --image`, never scaling/CPU flags) — env vars, secrets, and the `oauth2-proxy` container are set once by hand and left alone; a manual `gcloud run services update --container app --image` for a hotfix is safe and won't be reverted by the next CD run, but manually changing scaling/CPU/env/secrets will be silently preserved (not reset) until someone changes them back
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
  `_get_cache()`). Whether the stdio gateway (mcp-stdio `serve --user-env`)
  actually delivers the identity in production is **verified**, not just
  unit-tested: real Google login through the `jquants` Cloud Run service
  (oauth2-proxy → `mcp-stdio serve --trusted-user-header X-Forwarded-Email
  --user-env JQUANTS_MCP_USER`) → `register_api_key` → a subsequent
  `health_check` showing the registered user's own `plan` (not the
  single-user fallback) → a real tool call returning real market data
  (2026-08-09). See "Deployment Targets" above. When debugging a similar
  setup, the `Resolved plan=...` INFO log (emitted on a plan-cache miss) is
  the fastest way to confirm identity is flowing.
- Plan data retention: Free=2y (12w delay), Light=5y, Standard=10y, Premium=all
- `sync_plans.py` is removed — no longer copy data between plans

## Cache Freshness

- Tier 1 cache has **no time-based freshness bound**: `CacheStore.get_rows` returns
  cached rows regardless of `fetched_at` age, and the cache-vs-API fetch decision in
  tools is presence-based (a missing date triggers a live fetch; a cached one never
  does), not freshness-based. See the `entrypoint-stdio.sh` bullet under
  "Architecture" for the Cloud Run instance-lifecycle angle on this.
- This is by design for the common case: cache.db is a **published snapshot**
  (jpx-short-report's `daily.sh` re-fetches and re-exports it once a weekday;
  consumers download the new artifact wholesale). A blanket TTL would fight that
  model — re-fetching from the live API when a fresher whole-snapshot is already en
  route, and hammering a rate-limited API against years of history that essentially
  never changes (#587).
- The actual gap: a row that is already cached is **never re-verified**, even if
  J-Quants silently corrects it before the next snapshot lands. `equities_bars_daily`
  has one concrete, targeted fix for this — `CacheStore.detect_split_in_batch()`
  compares every row of a freshly-fetched batch against the cache's per-date value
  and forces an invalidate+refetch on mismatch, catching retroactive `AdjFactor`
  corrections (splits/consolidations/rights-issue reversals) without a time-based TTL
  (#597, #598).
- No equivalent check exists for other Tier 1 tables (e.g. `fins_summary`
  restatements) — deferred until an actual stale-row incident is observed, per #587.
  Do not add a blanket TTL preemptively; if a restatement-staleness problem shows up,
  prefer a targeted signal (like `detect_split_in_batch`) over a time-based expiry,
  for the same reasons as above.

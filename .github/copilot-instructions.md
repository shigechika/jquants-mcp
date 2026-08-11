# Repository overview

`jquants-mcp` is an MCP (Model Context Protocol) server exposing Japanese
stock market data from the J-Quants API v2 to AI assistants. It is
**stdio-only** and built on the official `mcp` SDK's FastMCP
(`from mcp.server.fastmcp import FastMCP`, in `src/jquants_mcp/server.py`) —
*not* the standalone `fastmcp` package, which was dropped together with the
HTTP/OAuth surface (#568). Other core modules: `client.py` (httpx async
client, rate limiting, retry, pagination), `cache/store.py` (two-tier SQLite
cache), `allowlist.py` (the `JQUANTS_ALLOWED_EMAILS` gate), `crypto.py`
(AES-256-GCM), `db/users.py` / `db/users_firestore.py` (per-user encrypted
key storage), `request_context.py` (per-request plan contextvar),
`oauth_login.py` (the `login` subcommand's PKCE flow against J-Quants' own
Cognito pool — this obtains the *upstream* API key and is unrelated to
authenticating end users of this server).

The server authenticates nobody and binds no socket. On Cloud Run an
`oauth2-proxy` sidecar in front of `mcp-stdio serve` verifies the user and
spawns one stdio child process per session, injecting the verified email as
the `JQUANTS_MCP_USER` environment variable (read by `server.py`'s
`_current_user_id()`). What remains in this repo's scope is what the server
does *with* that injected identity: per-user client/plan state and per-user
encrypted API keys (SQLite locally, Firestore on Cloud Run). See `CLAUDE.md`
for the full architecture breakdown — read it before reviewing changes to
`server.py`, `crypto.py`, or `cache/store.py`.

# Build & validate

```bash
uv sync --dev
uv run ruff check src/ tests/
uv run ruff format --check --exclude _version.py src/ tests/
uv run pytest -v
```

This mirrors `.github/workflows/ci.yml`: a `lint` job (ruff check + `ruff
format --check`) and a `test` job matrixed over Python 3.10–3.13, plus a
separate `test-windows` job on Python 3.12 (kept as its own job, not folded
into the matrix, specifically to preserve stable `test (3.x)` check names for
branch protection while still catching Windows-only breakage such as file
encoding/path issues at PR time). Note CI's format check uses `--check
--exclude _version.py`; the plain `ruff format src/ tests/` from `CLAUDE.md`
is the local auto-fix form of the same rule — don't treat a diff that only
needs `ruff format` (no `--check`) run locally as a CI mismatch.

There is no separate "on merge" deploy validation step to worry about in PR
review: `.github/workflows/cd.yml` only runs after CI succeeds on `main` and
is out of scope for PR-time review (see below).

# What to focus review on in this repo

## 1. Multi-user credential isolation

`server.py` keeps process-wide dicts keyed by `user_id`: `_user_clients`,
`_user_client_last_used`, `_plan_cache`. The `user_id` comes from
`_current_user_id()`, which returns the gateway-injected `JQUANTS_MCP_USER`
value (the user's verified email), or `None` on single-user /
no-gateway-identity deployments, where callers fall back to the global
`_client` and the cache's `default_plan`. A diff that reads or writes one of
these dicts under anything other than the current `_current_user_id()` — a
shared/global default, a key derived from a tool argument, or a fall-through
that reaches another user's cached client — is a cross-user data leak.
`db/users.py` (SQLite) and `db/users_firestore.py` (Cloud Run) are both keyed
by `user_id` in every query; confirm any new query in either file passes
`user_id` as a bound parameter rather than interpolating it into SQL/Firestore
document paths as free text.

That identity is trusted outright, because the gateway is the thing that
verified it. So the boundary to protect is its provenance: flag any diff that
lets a tool argument, request payload, or config value write
`JQUANTS_MCP_USER` (or otherwise feed `_current_user_id()`), which would let a
caller name whichever user it likes.

`request_context.py`'s `_current_plan` is a `contextvars.ContextVar`, but
**nothing in the server sets it per tool call** — the official SDK exposes no
per-call middleware hook, so plan resolution runs through `CacheStore`'s
constructor-injected `plan_resolver` (`server.py`'s `_resolve_current_plan`,
which reads `_current_user_id()` and looks up that user's plan), with the
contextvar consulted only as an explicit override a caller has pushed. Flag a
change that resolves a plan from module-level mutable state, or that caches a
resolved plan without keying it by `user_id` (`_plan_cache` does) — either one
silently applies one user's plan limits, or lack thereof, to another's query.

## 2. Secrets never logged; Cloud Run secrets via Secret Manager

`MCP_ENCRYPTION_KEY` and J-Quants API keys — including the ID token and API
key `oauth_login.py` handles during `jquants-mcp login` — must never appear in
log output, including at `DEBUG` level. `crypto.py` encrypts API keys with
AES-256-GCM (PBKDF2-HMAC-SHA256 key derivation, a random salt per encryption)
before they reach `db/users.py`; `db/users.py` logs `user_id` and `plan` on
save but never the key itself — match that pattern in new code. On Cloud Run
the secrets come from Secret Manager but are wired **out of band**:
`.github/workflows/cd.yml` runs only `gcloud run services update --container
app --image`, so env vars and `--set-secrets` references are set once by hand
and survive every deploy. A diff that hardcodes a secret, bakes a default
credential into `config.py`, or introduces a new secret that only works if
someone passes it as a plain env var should be flagged.

## 3. Tool-return auto-wrapping — don't ask for manual envelope code

Tools in `server.py` and `tools/` return plain `dict[str, Any]` (e.g.
`register_api_key`, `health_check`) or other JSON-serializable values. The
official SDK converts a raw return value into the MCP `content` +
`structuredContent` envelope on its own: `FastMCP.call_tool` passes
`convert_result=True` into `Tool.run`, which applies
`fn_metadata.convert_result` (`mcp.server.fastmcp.tools.base`). Manual
`{"content": [...], "isError": ...}` construction is unnecessary and
inconsistent with the rest of the codebase. Do not suggest wrapping a tool's
return value in a content envelope by hand.

## 4. Tool inputs are adversarial (LLM-driven)

Tool parameters (stock codes, dates, sector filters, free-text company names
for `search_equities`) are supplied by an LLM acting on a user's behalf —
treat them as untrusted. `validators.py` centralizes code/date/sector
validation; a new tool or endpoint wrapper that accepts a raw string and
interpolates it into a SQL query, file path, or external API URL without
going through an existing validator (or an equivalent explicit check) should
be flagged. Also check that a vague tool name or a docstring missing
parameter format details (e.g. date format, code format) isn't shipped — the
docstring is what the calling model uses to decide how to invoke the tool.

## 5. Cache correctness (`cache/store.py`)

Two tiers: **Tier 1** (row-level, e.g. `equities_bars_daily`, `fins_summary`)
supports incremental fetch and detects splits/consolidations/rights-issue
reversals by comparing every row of a freshly-fetched batch against the
cache's own `AdjFactor` for that same date (`AdjFactor` is a per-date event
flag, not a cumulative value — see jquants-mcp#597; a retroactive correction
back to `1.0` is a real event too — jquants-mcp#598). On a hit the caller in
`tools/equities.py` invalidates that code's cached rows and refetches them. **Tier 2** (response-level) caches full API responses with a
per-endpoint TTL (`ENDPOINT_TTL` map — currently ranging 6h/24h/7d/90d, with
a few endpoints explicitly set to `TTL_NONE`, e.g. `/equities/bars/daily/am`,
intraday data that must never be cached). A diff touching TTL values, the
`ENDPOINT_TTL` map, or the split-detection comparison in
`CacheStore.detect_split_in_batch` needs scrutiny — a wrong TTL or a broken
split comparison produces silently stale/incorrect data rather than a
visible error. Also: Tier 1 data is
plan-agnostic (no `plan` column per
`CLAUDE.md`/the `_migrate_drop_plan` migration) — plan-based date
restriction happens at query time via `_effective_plan()`, not at insert
time. A diff that adds a `plan` column back to an INSERT, or that applies
plan filtering somewhere other than the query-time helpers, contradicts this
documented design and should be flagged.

## 6. Test conventions

- `tests/conftest.py`'s autouse `_reset_server_globals` fixture clears
  `server_module._user_clients`, `_user_client_last_used`, and `_plan_cache`
  (and resets `_plan_detected`) between tests, because they're process-wide
  state that would otherwise leak cached per-user values across unrelated test
  cases. A new test that populates one of these globals directly should not
  assume it starts empty without this fixture, and should not need to manually
  clear it afterward.
- HTTP-level tests predominantly use `unittest.mock` (`AsyncMock`/`MagicMock`);
  a handful of client tests use `respx`. Follow whichever pattern the file
  you're editing already uses rather than mixing both in one test module.
- New tools/endpoints need tests covering both a normal response and at
  least one edge case (empty result, pagination boundary, 4xx/5xx from the
  upstream API, or — for multi-user paths — an unregistered/unauthorized
  user). A change to `allowlist.py`, `crypto.py`, or `oauth_login.py` should
  come with a test in the corresponding `test_allowlist*.py` /
  `test_crypto.py` / `test_oauth_login.py` file, not just incidental coverage
  from an unrelated tool test.

## 7. Two invariants that read as harmless refactors

- **`TOOL_API_ERRORS`, not the base class.** Tool handlers catch the
  `TOOL_API_ERRORS` tuple (`exceptions.py`), which deliberately includes
  `DecryptionError` but **excludes** the `JQuantsDatMCPError` base
  (Authentication/RateLimit/Validation are surfaced differently). Flag a new
  tool that broadens to `except JQuantsDatMCPError`, or hand-copies a narrower
  tuple that drops `DecryptionError` — both break a test-enforced design.
- **Alert-phrase lockstep.** Some Cloud Monitoring policies in `ops/alerts/`
  grep exact log-message phrases (e.g. the cache-stale / cache-download-fail
  phrases pinned by tests). Rewording such a log line silently disables its
  alert — this already killed an alert once (PR #443). Keep the log text and
  the `ops/alerts/*.yaml` phrase in sync.

# Out of scope for review comments

- `.github/workflows/cd.yml` deploy mechanics (Cloud Run flags, memory
  sizing, GCS/Firestore wiring) — these are deployment-operations concerns
  documented in `CLAUDE.md` and `README.md`, not something a code PR's diff
  usually touches, and are already covered by runbooks under `docs/runbooks/`.
- Formatting nits `ruff` would already catch in CI — don't duplicate what the
  `lint` job reports.
- Japanese comments in code predating the English-only convention —
  `CLAUDE.md` notes these are being migrated gradually; don't ask an
  unrelated diff to translate pre-existing comments it didn't touch.

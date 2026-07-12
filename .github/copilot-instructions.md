# Repository overview

`jquants-mcp` is an MCP (Model Context Protocol) server exposing Japanese
stock market data from the J-Quants API v2 to AI assistants. Built on the
standalone **FastMCP v3** package (`from fastmcp import FastMCP`, in
`src/jquants_mcp/server.py`) — not the `mcp.server.fastmcp` submodule used by
some sibling repos in this family; behavior can differ (see item 3 below).
Other core modules: `client.py` (httpx async client, rate limiting, retry,
pagination), `cache/store.py` (two-tier SQLite cache), `auth.py` /
`allowlist.py` (OAuth 2.1 + email allowlist), `crypto.py` (AES-256-GCM),
`db/users.py` / `db/users_firestore.py` (per-user encrypted key storage),
`request_context.py` (per-request plan contextvar).

This repo is more architecturally complex than most in this family: it
supports multi-user OAuth (GitHub/Google) with per-user encrypted API keys,
and a Cloud Run deployment mode backed by Firestore. See `CLAUDE.md` for the
full architecture breakdown — read it before reviewing changes to
`server.py`, `auth.py`, `crypto.py`, or `cache/store.py`.

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

`server.py` keeps process-wide dicts keyed by `user_id` (the OAuth token's
`client_id`): `_user_clients`, `_user_client_last_used`, `_plan_cache`. A diff
that reads or writes one of these without going through the authenticated
`user_id` for the *current* request — e.g. a shared/global default, a key
derived from something other than `token.client_id`, or a code path that
falls through to another user's cached client — is a cross-user data leak.
`db/users.py` (SQLite) and `db/users_firestore.py` (Cloud Run) are both keyed
by `user_id` in every query; confirm any new query in either file passes
`user_id` as a bound parameter rather than interpolating it into SQL/Firestore
document paths as free text.

`request_context.py`'s `_current_plan` is a `contextvars.ContextVar`, set by
`PlanContextMiddleware.on_call_tool` and reset in a `finally` block for each
tool call. This is what keeps one user's subscription plan (and its
date-range restrictions) from leaking into a concurrent request from another
user. Flag any change that replaces this with a module-level variable, a
mutable default, or anything else that isn't per-request-scoped — that would
silently apply one user's plan limits (or lack thereof) to another user's
query.

## 2. Secrets never logged; Cloud Run secrets via Secret Manager

`MCP_ENCRYPTION_KEY`, J-Quants API keys, OAuth client secrets, bearer tokens,
and JWT signing keys must never appear in log output, including at `DEBUG`
level. `crypto.py` encrypts API keys with AES-256-GCM (PBKDF2-HMAC-SHA256 key
derivation, a random salt per encryption) before they reach `db/users.py`;
`db/users.py` logs `user_id` and `plan` on save but never the key itself —
match that pattern in new code. On Cloud Run, `.github/workflows/cd.yml`
passes secrets (`MCP_ENCRYPTION_KEY`, `JQUANTS_API_KEY`, OAuth client
secrets, `JQUANTS_ALLOWED_EMAILS`) via `--set-secrets` (Secret Manager
references), while non-secret config (project ID, bucket name, OAuth base
URL, client *IDs*) goes through `--set-env-vars`. A diff that moves a secret
value into `--set-env-vars`, or that adds a new secret without a
corresponding Secret Manager reference, should be flagged.

## 3. FastMCP tool-return auto-wrapping — don't ask for manual envelope code

Tools in `server.py` return plain `dict[str, Any]` (e.g. `register_api_key`,
`health_check`) or other JSON-serializable values. The `fastmcp` package used
here (`FunctionTool.convert_result`, in `fastmcp.tools.function_tool`; the older `fastmcp.tools.tool` import path still resolves as an alias)
automatically converts a raw return value into the MCP `content` +
`structuredContent` envelope — manual `{"content": [...], "isError": ...}`
construction is unnecessary and inconsistent with the rest of the codebase.
Do not suggest wrapping a tool's return value in a content envelope by hand.

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
supports incremental fetch and detects stock splits by comparing `AdjFactor`
across fetches, rewriting historical adjusted prices when a split is found.
**Tier 2** (response-level) caches full API responses with a per-endpoint TTL
(`ENDPOINT_TTL` map — currently ranging 6h/24h/7d/90d, with a few endpoints
explicitly set to `TTL_NONE`, e.g. `/equities/bars/daily/am`, intraday data
that must never be cached). A diff touching TTL values, the `ENDPOINT_TTL`
map, or the split-detection comparison in `CacheStore.check_adj_factor`
needs scrutiny — a wrong TTL or a broken split comparison produces silently
stale/incorrect data rather than a visible error. Also: Tier 1 data is
plan-agnostic (no `plan` column per
`CLAUDE.md`/the `_migrate_drop_plan` migration) — plan-based date
restriction happens at query time via `_effective_plan()`, not at insert
time. A diff that adds a `plan` column back to an INSERT, or that applies
plan filtering somewhere other than the query-time helpers, contradicts this
documented design and should be flagged.

## 6. Test conventions

- `tests/conftest.py`'s autouse `_reset_server_globals` fixture clears
  `server_module._user_clients`, `_user_client_last_used`, and `_plan_cache`
  between tests, because they're process-wide dicts that would otherwise leak
  cached per-user state across unrelated test cases. A new test that
  populates one of these globals directly should not assume it starts empty
  without this fixture, and should not need to manually clear it afterward.
- HTTP-level tests predominantly use `unittest.mock` (`AsyncMock`/`MagicMock`);
  a handful of client tests use `respx`. Follow whichever pattern the file
  you're editing already uses rather than mixing both in one test module.
- New tools/endpoints need tests covering both a normal response and at
  least one edge case (empty result, pagination boundary, 4xx/5xx from the
  upstream API, or — for multi-user paths — an unregistered/unauthorized
  user). A change to `auth.py`, `allowlist.py`, or `crypto.py` should come
  with a test in the corresponding `test_auth.py` / `test_allowlist*.py` /
  `test_crypto.py` file, not just incidental coverage from an unrelated tool
  test.

## 7. `/settings` Web UI is an authenticated write surface

`settings/` serves a browser UI that registers/deletes per-user J-Quants API
keys, so its auth properties are load-bearing — treat a weakening of any as a
security regression, not a style nit:

- **Signed sessions.** `settings/session.py` signs the session cookie with
  HMAC-SHA256 (`sign_session`/`parse_session`, `hmac.compare_digest`) and a
  24h TTL. The session cookie is `httponly=True` / `samesite=lax` /
  `secure=not is_dev`; the CSRF cookie is intentionally `httponly=False` /
  `samesite=strict`. Flag a diff that flips these, drops the constant-time
  compare, or weakens the signing-key handling.
- **Double-submit CSRF.** State-changing routes validate the form token
  against the CSRF cookie (`validate_csrf`); a mutating route that skips it is
  a bug.
- **Email allowlist on BOTH identity paths.** `resolve_user_id` enforces the
  email allowlist for the cookie identity *and* the OAuth-token identity — a
  diff that gates only one path lets an authenticated-but-not-allowlisted user
  write/delete keys.

## 8. Three invariants that read as harmless refactors

- **`TOOL_API_ERRORS`, not the base class.** Tool handlers catch the
  `TOOL_API_ERRORS` tuple (`exceptions.py`), which deliberately includes
  `DecryptionError` but **excludes** the `JQuantsDatMCPError` base
  (Authentication/RateLimit/Validation are surfaced differently). Flag a new
  tool that broadens to `except JQuantsDatMCPError`, or hand-copies a narrower
  tuple that drops `DecryptionError` — both break a test-enforced design.
- **Open-redirect guard.** `auth.py`'s `_ALLOWED_CLIENT_REDIRECT_URIS` is what
  keeps FastMCP validating OAuth client redirect URIs; without it (or with a
  wildcard host added) FastMCP accepts any URI — an open-redirect /
  client-confusion risk. Scrutinize any widening.
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

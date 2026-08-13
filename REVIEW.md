# Review rules for this repository

Review rules on top of the reviewer's default focus. Three things:
which findings are blocking here, which classes to report that the
default focus would otherwise skip, and which are noise. The reasoning
behind the rules lives in `.github/copilot-instructions.md` (its
numbered focus items are cited below) and `CLAUDE.md`, which the
reviewer also receives.

## Always blocking

- **Reaching per-user state under anything other than the current
  `_current_user_id()` (§1).** `_user_clients`,
  `_user_client_last_used` and `_plan_cache` are process-wide dicts
  keyed by user. A shared or global default, a key derived from a tool
  argument, or a fall-through that reaches another user's cached client
  is a cross-user data leak, not a caching detail.
- **Letting a tool argument, request payload or config value feed
  `JQUANTS_MCP_USER` or `_current_user_id()` (§1).** That identity is
  trusted outright *because the gateway verified it*, so its provenance
  is the whole boundary. A path that lets a caller name whichever user
  it likes collapses the isolation above.
- **A `db/users.py` or `db/users_firestore.py` query that interpolates
  `user_id` as free text (§1)** instead of passing it as a bound
  parameter or an explicit document path component.
- **Resolving a plan from module-level mutable state, or caching a
  resolved plan without keying it by `user_id` (§1).** Plan resolution
  runs through `CacheStore`'s constructor-injected `plan_resolver`
  (`_resolve_current_plan`), with `request_context.py`'s
  `_current_plan` contextvar consulted only as an explicit caller
  override — nothing sets it per tool call. Either mistake silently
  applies one user's plan limits, or lack of them, to another's query.
- **A secret reaching log output at any level, `DEBUG` included (§2).**
  `MCP_ENCRYPTION_KEY`, a J-Quants API key, or the ID token and API key
  `oauth_login.py` handles during `jquants-mcp login`. `db/users.py`
  logs `user_id` and `plan` on save and never the key itself — match
  that. A hardcoded secret, a default credential baked into
  `config.py`, or a new secret that only works when passed as a plain
  env var belongs here too: on Cloud Run these are wired out of band
  through Secret Manager, since `cd.yml` only updates the image.
- **A raw string interpolated into a SQL query, file path or external
  API URL without going through `validators.py` (§4)** or an equivalent
  explicit check. Stock codes, dates, sector filters and free-text
  company names all arrive from an LLM acting on a user's behalf.
- **Broadening a tool handler to `except JQuantsDatMCPError`, or
  hand-copying a narrower tuple that drops `DecryptionError` (§7).**
  Handlers catch the `TOOL_API_ERRORS` tuple, which deliberately
  includes `DecryptionError` and excludes the base class because
  Authentication, RateLimit and Validation are surfaced differently.
  Both mistakes break a test-enforced design.
- **Rewording a log line that a Cloud Monitoring policy in
  `ops/alerts/` greps for (§7)** without updating the matching
  `ops/alerts/*.yaml` phrase. This silently disables the alert, and it
  already killed one once.

## Report even though the default focus would not

- **A new tool's name and docstring (§4).** The calling model decides
  how to invoke a tool by reading them, so a vague name or a docstring
  missing a parameter format — date format, code format — is a
  functional defect here. Report it even though docstring accuracy is
  normally out of scope when reviewing code.
- **A `plan` column added back to an INSERT, or plan filtering applied
  outside the query-time helpers (§5).** Restriction happens at query
  time via `_effective_plan()`, not at insert time.
- **A diff touching `allowlist.py`, `crypto.py` or `oauth_login.py`
  that also touches `tests/` without a test in the corresponding
  `test_allowlist*.py` / `test_crypto.py` / `test_oauth_login.py`
  (§6)**, as advisory — incidental coverage from an unrelated tool test
  does not count. Judge this from the diff only: you receive changed
  files, so a pull request that leaves `tests/` alone may well be
  covered by tests you were not given.
- **A test that mixes mocking styles within one module (§6)**, as
  advisory. Most HTTP-level tests use `unittest.mock`
  (`AsyncMock`/`MagicMock`) and a handful of client tests use `respx`;
  follow whichever the file already uses.

## Never report

- Suggestions to hand-build an MCP content envelope
  (`{"content": [...], "isError": ...}`). The official SDK converts a
  raw return value into the `content` + `structuredContent` envelope
  itself, via `FastMCP.call_tool` passing `convert_result=True`.
- `.github/workflows/cd.yml` deploy mechanics — Cloud Run flags, memory
  sizing, GCS and Firestore wiring. Those are operations concerns
  documented in `CLAUDE.md`, `README.md` and the runbooks under
  `docs/runbooks/`, not something a code diff usually touches.
- Pre-existing Japanese comments that predate the English-only
  convention. They are being migrated gradually; do not ask an
  unrelated diff to translate comments it did not touch.
- Anything `ruff check` or `ruff format --check` already fails the
  build on. Both gate `src/` and `tests/`, so restating a finding costs
  a round trip and no information.

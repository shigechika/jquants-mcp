"""FastMCP server definition and tool registration."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .cache import store
from .cache.store import CacheStore
from .client import JQuantsClient
from .config import Settings
from .tool_annotations import DESTRUCTIVE_LOCAL, READ_ONLY_LOCAL

logger = logging.getLogger(__name__)

mcp = FastMCP("jquants-mcp")

# Shared global state — lazily initialized on the first request.
_settings: Settings | None = None
_cache: CacheStore | None = None

# Single-user global client (Bearer token / no-auth mode).
_client: JQuantsClient | None = None

# Multi-user client pool: user_id -> JQuantsClient (one per authenticated user).
_user_clients: dict[str, JQuantsClient] = {}

# Last-used timestamps for stale-client eviction: user_id -> monotonic timestamp.
_user_client_last_used: dict[str, float] = {}

# Short-TTL cache of each user's plan (user_id -> (plan, expiry_monotonic)) so
# repeated cache-plan resolution (see CacheStore's plan_resolver, wired in
# _get_cache()) does not hit the user DB (Firestore) on every tool call. A plan
# change is reflected within _PLAN_CACHE_TTL seconds.
_plan_cache: dict[str, tuple[str | None, float]] = {}
_PLAN_CACHE_TTL = 60.0


def _current_user_id() -> str | None:
    """Return the gateway-authenticated user's identity for this process.

    Identity is injected by mcp-stdio's ``serve`` gateway at child-process
    spawn time via ``--user-env JQUANTS_MCP_USER`` (per-user child process
    design, "case A"): one child serves exactly one authenticated principal
    for its whole lifetime, and the principal is the user's verified email
    (mcp-stdio's ``--trusted-user-header X-Forwarded-Email``). Returns
    ``None`` for single-user / static-bearer-token / unauthenticated
    deployments, where callers fall back to the global client and the
    cache's configured ``default_plan`` — identical to today's
    ``token is None or token.client_id == "bearer"`` behavior.
    """
    return os.environ.get("JQUANTS_MCP_USER") or None


def _resolve_current_plan() -> str | None:
    """Resolve the authenticated user's plan for the current tool call.

    Returns ``None`` for single-user / unauthenticated paths (the cache then
    uses its configured ``default_plan``). Cached per user_id for a short TTL
    to avoid a user-DB round-trip on every tool call. Never raises — any
    failure resolves to ``None`` so tool calls are not broken by plan lookup.
    Wired into ``CacheStore`` as its ``plan_resolver`` (see ``_get_cache()``),
    which calls this only when the ``request_context`` contextvar is unset.
    """
    user_db = _get_user_db()
    if user_db is None:
        return None
    user_id = _current_user_id()
    if user_id is None:
        return None

    now = time.monotonic()
    cached = _plan_cache.get(user_id)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        meta = user_db.get_user_meta(user_id)
    except Exception:
        return None
    plan = meta.plan if meta is not None else None
    _plan_cache[user_id] = (plan, now + _PLAN_CACHE_TTL)
    logger.info("Resolved plan=%s for user=%s", plan, user_id)
    return plan


# Timestamp of the last stale-client cleanup run (monotonic).
_last_cleanup: float = 0.0

# Per-user rate limiter (multi-user mode only). Lazily initialized.
_rate_limiter: Any | None = None

# Single-user mode: whether plan auto-detection has completed.
_plan_detected: bool = False

# Run cleanup at most once every 5 minutes.
_CLEANUP_INTERVAL = 300

# User store — lazily initialized when encryption_key is configured.
# Backend is SQLite (local) or Firestore (Cloud Run); both share the same
# duck-typed interface, so the concrete type is not annotated here.
_user_db: Any | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _get_client() -> JQuantsClient:
    global _client
    if _client is None:
        _client = JQuantsClient(_get_settings())
    return _client


def _get_cache() -> CacheStore:
    global _cache
    if _cache is None:
        settings = _get_settings()
        db_path = settings.get_cache_db_path()
        # ``check_integrity_async=True`` so ``health_check`` returns
        # ``"pending"`` / ``"ok"`` on first call instead of
        # ``"not-checked"`` — without this, the first ``health_check``
        # against a fresh server reads ``integrity_status`` before any
        # connection-establishing call has triggered the background
        # check.
        _cache = CacheStore(
            db_path,
            default_plan=settings.jquants_plan,
            check_integrity_async=True,
            plan_resolver=_resolve_current_plan,
        )
    return _cache


def _sighup_handler(signum: int, frame: Any) -> None:
    """Handle SIGHUP by requesting a lazy reload of the cache database.

    Triggered externally (e.g. by ``launchctl kill SIGHUP``) after an
    offline process such as ``daily.sh`` has updated ``cache.db``.
    The handler only sets a flag; the actual reconnection happens on
    the next request to avoid disturbing in-flight queries. uvicorn
    does not install its own SIGHUP handler, so this handler coexists
    with its SIGINT/SIGTERM shutdown handling.
    """
    logger.info("Received SIGHUP; scheduling cache DB reload")
    if _cache is not None:
        _cache.request_reload()
    else:
        logger.info("Cache DB not yet initialized; reload is a no-op")


def _get_rate_limiter():
    """Return the per-user rate limiter, creating it on first access."""
    global _rate_limiter
    if _rate_limiter is None:
        from .rate_limit import RateLimiter

        settings = _get_settings()
        _rate_limiter = RateLimiter(
            per_minute=settings.rate_limit_per_minute,
            burst=settings.rate_limit_burst,
        )
    return _rate_limiter


def _get_user_db():
    """Return the UserStore, creating it if encryption is configured.

    Returns None when no encryption_key is set (single-user mode).
    """
    global _user_db
    if _user_db is not None:
        return _user_db

    settings = _get_settings()
    if not settings.encryption_key:
        return None

    from .crypto import decrypt, decrypt_with_fallback, encrypt

    passphrase = settings.encryption_key
    previous = getattr(settings, "encryption_key_previous", "")

    def enc(pt: str) -> str:
        return encrypt(pt, passphrase)

    if previous:
        logger.info("MCP_ENCRYPTION_KEY_PREVIOUS is set; dual-key decrypt is active")
        candidates = [passphrase, previous]

        def dec(blob: str) -> str:
            return decrypt_with_fallback(blob, candidates)
    else:

        def dec(blob: str) -> str:
            return decrypt(blob, passphrase)

    # On Cloud Run, use Firestore so user data is shared across instances
    # and survives restarts. Locally, use SQLite.
    if os.environ.get("K_SERVICE"):
        from .db.users_firestore import FirestoreUserStore

        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        _user_db = FirestoreUserStore(project=project, encrypt_fn=enc, decrypt_fn=dec)
        logger.info("UserStore backend: Firestore (project=%s)", project)
    else:
        from .db.users import UserStore

        db_path = settings.get_cache_dir() / "users.db"
        _user_db = UserStore(db_path, encrypt_fn=enc, decrypt_fn=dec)
        logger.info("UserStore backend: SQLite (%s)", db_path)

    return _user_db


async def _ensure_plan_detected(client: JQuantsClient) -> None:
    """Auto-detect the J-Quants plan on first call when JQUANTS_PLAN is not configured."""
    global _plan_detected

    if _plan_detected:
        return

    settings = _get_settings()
    if settings.jquants_plan:
        # Explicitly configured -> no detection needed.
        _plan_detected = True
        return

    _plan_detected = True  # Do not retry (fall back to free on failure).

    from .validation import detect_plan

    try:
        detected = await detect_plan(client)
    except Exception as e:
        logger.warning("Plan auto-detection failed (falling back to free): %s", e)
        detected = "free"

    logger.info("Plan auto-detected: %s", detected)
    settings.jquants_plan = detected
    client.update_rate_limit(detected)

    # Update the CacheStore if it is already initialized.
    if _cache is not None:
        _cache.default_plan = detected


async def _evict_stale_clients() -> None:
    """Evict in-memory client instances that have been idle for more than 1 hour."""
    from .validation import _STALE_CLIENT_TTL

    now = time.monotonic()
    stale = [uid for uid, ts in _user_client_last_used.items() if now - ts > _STALE_CLIENT_TTL]
    for uid in stale:
        client = _user_clients.pop(uid, None)
        _user_client_last_used.pop(uid, None)
        if client is not None:
            await client.close()
        logger.info("Evicted stale client for user %s (idle >%ds)", uid, _STALE_CLIENT_TTL)


def _evict_expired_plan_cache_entries() -> None:
    """Remove expired entries from ``_plan_cache``.

    Without this, ``_plan_cache`` grows without bound on a long-running
    multi-user deployment: every distinct authenticated user who ever calls a
    tool adds an entry via ``_resolve_current_plan()``, and an expired entry
    is only overwritten (never removed) the next time that same user calls
    again — a one-off visitor's entry lives for the process lifetime.
    Unlike ``_user_clients``, which ``_evict_stale_clients()`` actively prunes.
    """
    now = time.monotonic()
    expired = [uid for uid, (_, expiry) in _plan_cache.items() if expiry <= now]
    for uid in expired:
        _plan_cache.pop(uid, None)


async def _validate_user_client(
    user_db: Any,
    client: JQuantsClient,
    user_id: str,
    last_validated_at: int | None,
) -> None:
    """Run the once-per-day API key validation for a per-user client.

    On revocation, evict the cached client and raise InvalidAPIKeyError so the
    caller surfaces an actionable error instead of repeated 401s.
    """
    from .exceptions import AuthenticationError, InvalidAPIKeyError
    from .validation import needs_validation, validate_api_key

    if not needs_validation(last_validated_at):
        return
    try:
        await validate_api_key(client)
        user_db.update_last_validated(user_id)
        logger.info("Daily validation passed for user %s", user_id)
    except AuthenticationError:
        _user_clients.pop(user_id, None)
        _user_client_last_used.pop(user_id, None)
        raise InvalidAPIKeyError(user_id)


async def _get_user_client() -> JQuantsClient:
    """Return the J-Quants client for the currently authenticated user.

    Resolution order:
    1. No auth / bearer-token auth → global single-user client (settings.jquants_api_key)
    2. OAuth user without encryption_key configured → global client (shared)
    3. OAuth user with encryption_key → per-user client from UserStore

    Performs daily API key validation and stale client cleanup as side effects.

    Raises:
        UserNotConfiguredError: When multi-user mode is active and the current
            user has not yet registered their J-Quants API key.
        InvalidAPIKeyError: When daily validation detects that the stored API key
            has been revoked.
    """
    global _last_cleanup

    from .exceptions import UserNotConfiguredError

    user_id = _current_user_id()

    # No gateway identity (single-user / static bearer token) -> global client.
    if user_id is None:
        client = _get_client()
        await _ensure_plan_detected(client)
        return client

    # Per-user rate limiting (multi-user only; the no-identity path was handled above).
    from .audit import audit
    from .rate_limit import RateLimitExceededError

    # Allowlist: reject before rate limiter so untrusted traffic cannot
    # consume our shared bucket capacity. The gateway-authenticated principal
    # *is* the user's verified email (case A: mcp-stdio injects
    # X-Forwarded-Email via --user-env), so no separate claims lookup is
    # needed here.
    from .allowlist import is_email_allowed
    from .exceptions import UserNotAllowedError

    allowed = _get_settings().get_allowed_emails()
    email = user_id
    if not is_email_allowed(email, allowed):
        audit("allowlist_rejected", user_id=user_id, email=email, where="tool")
        raise UserNotAllowedError(email or user_id)

    try:
        await _get_rate_limiter().acquire(user_id)
    except RateLimitExceededError as exc:
        audit("rate_limited", user_id=user_id, retry_after=exc.retry_after)
        raise

    user_db = _get_user_db()

    # encryption_key unset -> all OAuth users share the global client.
    if user_db is None:
        return _get_client()

    # Periodically evict stale clients and expired plan-cache entries.
    now_mono = time.monotonic()
    if now_mono - _last_cleanup > _CLEANUP_INTERVAL:
        await _evict_stale_clients()
        _evict_expired_plan_cache_entries()
        _last_cleanup = now_mono

    from .exceptions import DecryptionError

    # Fast path: a per-user client is already cached. Reuse it without
    # decrypting the stored API key — get_user() runs PBKDF2 (200k iterations)
    # on every call, which is wasteful on the per-tool-call hot path when we
    # already hold a working client. Only lightweight metadata is read here.
    cached_client = _user_clients.get(user_id)
    if cached_client is not None:
        meta = user_db.get_user_meta(user_id)
        if meta is not None:
            _user_client_last_used[user_id] = now_mono
            await _validate_user_client(user_db, cached_client, user_id, meta.last_validated_at)
            return cached_client
        # User row vanished (deleted or store reset) since the client was
        # cached — drop the stale client and fall through to full resolution.
        _user_clients.pop(user_id, None)
        _user_client_last_used.pop(user_id, None)

    # Full path: decrypt the stored key to build a new client.
    user = user_db.get_user(user_id)
    if user is None:
        if user_db.has_corrupted_key(user_id):
            # Key exists in the DB but decryption failed — return an error explaining how to recover.
            raise DecryptionError()
        if _get_settings().cache_bypass_auth:
            # Bypass: fall back to global client so cache reads succeed without
            # per-user API key registration (self-hosted with pre-populated cache).
            # Do NOT enable on Cloud Run — all bypass users share the global API
            # key quota, which can exhaust it in multi-user deployments.
            audit("cache_bypass_used", user_id=user_id)
            client = _get_client()
            await _ensure_plan_detected(client)
            return client
        raise UserNotConfiguredError(user_id)

    user_settings = Settings(
        jquants_api_key=user.api_key,
        jquants_plan=user.plan,
    )
    client = JQuantsClient(user_settings)
    _user_clients[user_id] = client
    _user_client_last_used[user_id] = now_mono

    await _validate_user_client(user_db, client, user_id, user.last_validated_at)
    return client


# ------------------------------------------------------------------
# Utility tools
# ------------------------------------------------------------------


def _health_check_impl() -> dict[str, Any]:
    settings = _get_settings()
    has_key = bool(settings.jquants_api_key)
    plan = settings.jquants_plan or "auto (not yet detected)"

    # In multi-user mode, resolve the actual user's plan.
    user_id = _current_user_id()
    if user_id is not None:
        user_db = _get_user_db()
        if user_db is not None:
            user = user_db.get_user(user_id)
            if user is not None:
                plan = user.plan
                has_key = True

    cache = _get_cache()
    integrity = cache.integrity_status
    status = "healthy"
    if store.integrity_is_failure(integrity):
        status = "degraded"
    cache_ready = integrity == store.INTEGRITY_OK

    latest_date = cache.get_latest_equities_date()
    trading_today = cache.get_trading_date_today()
    today_cache_ready = cache_ready and latest_date is not None and latest_date >= trading_today

    return {
        "status": status,
        "service": "jquants-mcp",
        "version": __version__,
        "api_key_configured": has_key,
        "plan": plan,
        "cache_integrity": integrity,
        "cache_ready": cache_ready,
        "latest_cache_date": latest_date,
        "trading_date_today": trading_today,
        "today_cache_ready": today_cache_ready,
    }


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def health_check() -> dict[str, Any]:
    """Check server health, API key configuration, and cache readiness.

    Offloaded to a worker thread (see ``_health_check_impl``): the body can
    trigger the slow lazy cache initialization (connect + migrations), and
    the official mcp SDK — unlike the standalone fastmcp package this server
    used to run on — invokes sync tool bodies directly on the event loop
    rather than in a worker thread, so an explicit ``asyncio.to_thread``
    offload is required here to keep that work off the loop. Sharing the
    SQLite connection with the loop from that thread is what caused #537;
    the store's write lock, not this offload, is what makes it safe.

    Call this at session start to confirm cache.db has finished loading
    before issuing detect_* or cache_status — the first call after server
    start may take 10–60 seconds while the cache initialises lazily.
    After a tool-call timeout, use this to distinguish a transient
    cache-loading delay from a permanent failure.

    Returns server version, API key status, active plan, ``status``
    (healthy / degraded), ``cache_integrity`` and ``cache_ready``.

    ``status`` is degraded only when the integrity check reports a failure;
    there is no error state, since this call does no I/O that can fail.

    ``cache_integrity`` (ok / pending / not-checked / failed: <detail> /
    error: <detail>) is the integrity check's own result. The last two carry a
    detail string appended to the prefix, so test them with ``startswith``,
    not ``==``. ``cache_ready`` is a boolean shorthand: true only when
    cache_integrity is exactly "ok".

    In multi-user mode, returns the authenticated user's plan.
    """
    return await asyncio.to_thread(_health_check_impl)


def _cache_status_impl() -> dict[str, Any]:
    result = _get_cache().status()

    # In multi-user mode, resolve the actual user's plan.
    user_id = _current_user_id()
    if user_id is not None:
        user_db = _get_user_db()
        if user_db is not None:
            user = user_db.get_user(user_id)
            if user is not None:
                result["plan"] = user.plan

    return result


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def cache_status() -> dict[str, Any]:
    """Show database metadata: table row counts, file size, and detected plan.

    This tool returns cache metadata — it does NOT query screener signals. To detect
    52-week highs/lows use ``detect_52w_high_low``; for YTD highs/lows use
    ``detect_ytd_high_low``; for volume spikes use ``detect_volume_surge``; for price
    limits use ``detect_price_limit``. Do not call this tool to look up market data or
    screener results.

    In multi-user mode, returns the authenticated user's plan instead of the global
    default.

    Offloaded to a worker thread (see ``_cache_status_impl``): this does a
    multi-GB row-count scan, and the official mcp SDK runs sync tool bodies
    directly on the event loop (see ``health_check``'s docstring for why an
    explicit offload is needed here).
    """
    return await asyncio.to_thread(_cache_status_impl)


def _cache_clear_impl(table: str | None) -> dict[str, Any]:
    result = _get_cache().clear(table)
    return {"cleared": result}


@mcp.tool(annotations=DESTRUCTIVE_LOCAL)
async def cache_clear(table: str | None = None) -> dict[str, Any]:
    """Clear cached data.

    Offloaded to a worker thread (see ``_cache_clear_impl``): this does a
    bulk DELETE, and the official mcp SDK runs sync tool bodies directly on
    the event loop (see ``health_check``'s docstring for why an explicit
    offload is needed here).

    Args:
        table: Table name to clear. Clears all tables when omitted.
    """
    return await asyncio.to_thread(_cache_clear_impl, table)


@mcp.tool(annotations=DESTRUCTIVE_LOCAL)
async def register_api_key(api_key: str) -> dict[str, Any]:
    """Register or update your J-Quants API key (multi-user mode).

    ⚠️ SECURITY WARNING: The API key is transmitted in plaintext via the MCP
    protocol and may be logged by the MCP client or LLM provider. Use the
    browser-based /settings page instead for secure key registration.

    Stores your J-Quants API key encrypted in the server's user database,
    associated with your OAuth identity. The server probes plan-specific
    J-Quants endpoints to auto-detect the plan (free / light / standard /
    premium) and stores it alongside the key. Subsequent tool calls will
    automatically use this key and the detected plan's rate limits and
    date-range restrictions.

    This tool requires OAuth 2.1 authentication and server-side encryption
    (MCP_ENCRYPTION_KEY) to be configured.

    Args:
        api_key: Your J-Quants API key (refresh token from the J-Quants portal).
    """
    from .models.user import User

    user_id = _current_user_id()
    if user_id is None:
        return {
            "error": True,
            "message": "register_api_key requires OAuth 2.1 authentication.",
        }

    user_db = _get_user_db()
    if user_db is None:
        return {
            "error": True,
            "message": (
                "Multi-user mode is not enabled. "
                "Set MCP_ENCRYPTION_KEY on the server to enable per-user API key storage."
            ),
        }

    # Allowlist check — prevent unauthorized users from registering keys. The
    # gateway-authenticated principal *is* the user's verified email (case A).
    from .allowlist import is_email_allowed, unauthorized_message
    from .audit import audit as _audit_allowlist

    email = user_id
    if not is_email_allowed(email, _get_settings().get_allowed_emails()):
        _audit_allowlist(
            "allowlist_rejected", user_id=user_id, email=email, where="register_api_key"
        )
        return {"error": True, "message": unauthorized_message(email or user_id)}

    # Save with a temporary plan that will be overwritten by auto-detection below.
    plan = "free"
    user_db.save_user(User(user_id=user_id, api_key=api_key, plan=plan))

    # Invalidate the cached client so the next call uses the new key.
    _user_clients.pop(user_id, None)
    _user_client_last_used.pop(user_id, None)

    # Probe plan-specific endpoints to auto-detect the actual plan.
    from .audit import audit
    from .config import Settings as _Settings
    from .validation import detect_plan

    probe_client = JQuantsClient(_Settings(jquants_api_key=api_key, jquants_plan=plan))
    warnings: list[str] = []
    try:
        detected_plan = await detect_plan(probe_client)
        user_db.update_plan(user_id, detected_plan)
        plan = detected_plan
    except Exception as e:
        logger.debug("Plan detection failed during registration for user %s: %s", user_id, e)
        warnings.append("Plan detection skipped due to internal error")
    finally:
        await probe_client.close()

    audit("register_api_key", user_id=user_id, plan=plan)

    result: dict[str, Any] = {
        "status": "ok",
        "user_id": user_id,
        "plan": plan,
        "message": "API key registered successfully.",
    }
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool(annotations=DESTRUCTIVE_LOCAL)
async def delete_api_key() -> dict[str, Any]:
    """Delete your registered J-Quants API key (multi-user mode).

    Removes your API key from the server. Subsequent tool calls will fail
    until you register a new key with register_api_key.

    This tool requires OAuth 2.1 authentication.
    """
    user_id = _current_user_id()
    if user_id is None:
        return {
            "error": True,
            "message": "delete_api_key requires OAuth 2.1 authentication.",
        }

    user_db = _get_user_db()
    if user_db is None:
        return {
            "error": True,
            "message": "Multi-user mode is not enabled (MCP_ENCRYPTION_KEY not set).",
        }

    from .allowlist import is_email_allowed, unauthorized_message
    from .audit import audit

    # The gateway-authenticated principal *is* the user's verified email (case A).
    email = user_id

    if not is_email_allowed(email, _get_settings().get_allowed_emails()):
        audit("allowlist_rejected", user_id=user_id, email=email, where="delete_api_key")
        return {"error": True, "message": unauthorized_message(email or user_id)}

    deleted = user_db.delete_user(user_id)
    _user_clients.pop(user_id, None)
    _user_client_last_used.pop(user_id, None)

    if deleted:
        audit("delete_api_key", user_id=user_id)
        return {"status": "ok", "message": "API key deleted."}
    return {"status": "not_found", "message": "No API key was registered for this user."}


# ------------------------------------------------------------------
# Tool registration
# ------------------------------------------------------------------


def _register_tools() -> None:
    """Register all endpoint tools. Called during module import."""
    from .tools import (
        bulk,
        derivatives,
        equities,
        financials,
        indices,
        market_overview,
        markets,
        screener,
        summary,
        technical,
        valuation,
    )

    equities.register(mcp, _get_user_client, _get_cache)
    financials.register(mcp, _get_user_client, _get_cache)
    indices.register(mcp, _get_user_client, _get_cache)
    derivatives.register(mcp, _get_user_client, _get_cache)
    markets.register(mcp, _get_user_client, _get_cache)
    bulk.register(mcp, _get_user_client, _get_cache)
    screener.register(mcp, _get_user_client, _get_cache)
    market_overview.register(mcp, _get_user_client, _get_cache)
    summary.register(mcp, _get_user_client, _get_cache)
    technical.register(mcp, _get_user_client, _get_cache)
    valuation.register(mcp, _get_user_client, _get_cache)

    from .tools import charts

    charts.register(mcp, _get_user_client, _get_cache)


_register_tools()


# ------------------------------------------------------------------
# Server startup
# ------------------------------------------------------------------


def run_server() -> None:
    """Start the MCP server over stdio.

    stdio is the only transport this server supports: since the migration to
    the official mcp SDK's ``FastMCP``, ``run()`` accepts only ``transport``
    and ``mount_path`` — no ``host``, ``port``, ``uvicorn_config`` or
    ``middleware``. HTTP is terminated upstream instead, by the ``mcp-stdio``
    gateway that spawns one child process per authenticated user.
    """
    logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")
    logger.info("jquants-mcp v%s starting (transport=stdio)", __version__)

    mcp.run(transport="stdio")

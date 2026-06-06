"""FastMCP server definition and tool registration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any

import httpx

from fastmcp import FastMCP
from fastmcp.server.middleware.middleware import Middleware as ToolMiddleware
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import __version__
from .cache.store import CacheStore
from .client import JQuantsClient
from .config import Settings
from .request_context import reset_current_plan, set_current_plan
from .tool_annotations import DESTRUCTIVE_LOCAL, READ_ONLY_LOCAL

logger = logging.getLogger(__name__)

# Paths whose requests are logged for OAuth debugging.
_OAUTH_DEBUG_PATHS = ("/oauth/", "/.well-known/")

# Query params and headers redacted from OAuth debug logs to avoid leaking
# short-lived authorization secrets and session credentials.
_REDACTED_QUERY_PARAMS = frozenset({"code", "state", "token", "access_token", "id_token"})
_REDACTED_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})


class OAuthDebugMiddleware(BaseHTTPMiddleware):
    """Log OAuth-related HTTP requests to help diagnose auth flow issues."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        is_oauth = any(p in path for p in _OAUTH_DEBUG_PATHS)

        if is_oauth:
            # Redact short-lived OAuth secrets from query params (authorization
            # code, state, token) so they never reach the logs.
            query = {
                k: (v if k.lower() not in _REDACTED_QUERY_PARAMS else "[REDACTED]")
                for k, v in request.query_params.items()
            }
            # Redact credential-bearing headers (auth bearer token, session cookie).
            headers = {
                k: (v if k.lower() not in _REDACTED_HEADERS else "[REDACTED]")
                for k, v in request.headers.items()
            }
            logger.info(
                "OAuth request: method=%s path=%s query=%r headers=%r",
                request.method,
                path,
                query,
                headers,
            )

        try:
            response = await call_next(request)
        except RuntimeError as exc:
            # Starlette BaseHTTPMiddleware raises "No response returned." when
            # the client disconnects mid-request. That's a cosmetic symptom of
            # the well-known BaseHTTPMiddleware limitation, not a server bug —
            # swallow it and let the ASGI layer handle the disconnect.
            if "No response returned" in str(exc):
                if is_oauth:
                    logger.info(
                        "OAuth request aborted (client disconnect): method=%s path=%s",
                        request.method,
                        path,
                    )
                return Response(status_code=499)
            raise

        if is_oauth:
            logger.info(
                "OAuth response: method=%s path=%s status=%d",
                request.method,
                path,
                response.status_code,
            )

        return response


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

# Short-TTL cache of each user's plan (user_id -> (plan, expiry_monotonic)) so the
# per-call PlanContextMiddleware does not hit the user DB (Firestore) on every
# tool call. A plan change is reflected within _PLAN_CACHE_TTL seconds.
_plan_cache: dict[str, tuple[str | None, float]] = {}
_PLAN_CACHE_TTL = 60.0


def _resolve_current_plan() -> str | None:
    """Resolve the authenticated user's plan for the current tool call.

    Returns ``None`` for single-user / bearer / unauthenticated paths (the cache
    then uses its configured ``default_plan``). Cached per user_id for a short
    TTL to avoid a user-DB round-trip on every tool call. Never raises — any
    failure resolves to ``None`` so tool calls are not broken by plan lookup.
    """
    user_db = _get_user_db()
    if user_db is None:
        return None
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        return None
    if token is None or token.client_id == "bearer":
        return None
    user_id = token.client_id

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
    # Verifies per-user plan resolution on the live OAuth path. The wiring is
    # unit-tested by TestPlanContextMiddlewareE2E (with a patched
    # get_access_token); what only production exercises is whether FastMCP
    # delivers a real token to on_call_tool (a mock cannot prove that). Logged
    # at INFO because the app's root level is INFO; only fires on a plan-cache
    # miss (<= once per user per _PLAN_CACHE_TTL), so the volume is low.
    logger.info("Resolved plan=%s for user=%s", plan, user_id)
    return plan


class PlanContextMiddleware(ToolMiddleware):
    """Bind the authenticated user's plan to the request for each tool call.

    Lets ``CacheStore`` apply per-user plan date restrictions (e.g. Free =
    2 years + 12-week delay) without threading a ``plan`` argument through every
    tool. Single-user / bearer deployments resolve to ``None`` and keep using
    the cache's configured ``default_plan``.
    """

    async def on_call_tool(self, context, call_next):  # type: ignore[override]
        token = set_current_plan(_resolve_current_plan())
        try:
            return await call_next(context)
        finally:
            reset_current_plan(token)


mcp.add_middleware(PlanContextMiddleware())

# Timestamp of the last stale-client cleanup run (monotonic).
_last_cleanup: float = 0.0

# Per-user rate limiter (multi-user mode only). Lazily initialized.
_rate_limiter: Any | None = None

# Single-user mode: whether plan auto-detection has completed.
_plan_detected: bool = False

# Run cleanup at most once every 5 minutes.
_CLEANUP_INTERVAL = 300

# Pub/Sub reload state
_last_reload_at: float | None = None
_reload_in_progress: bool = False

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


def _verify_pubsub_oidc_token(token: str, expected_email: str, audience: str) -> None:
    """Verify a Google-signed OIDC token delivered by Pub/Sub.

    Args:
        token: Raw JWT string from the Authorization header.
        expected_email: Service-account email that must match the token's ``email`` claim.
        audience: Expected ``aud`` claim (typically the push endpoint URL).

    Raises:
        ValueError: When verification fails or the email / audience does not match.
    """
    try:
        import google.auth.transport.requests  # type: ignore[import-untyped]
        import google.oauth2.id_token  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError("google-auth not installed; install [cloud-run] extras") from exc

    if not audience:
        raise ValueError("audience must not be empty")

    request = google.auth.transport.requests.Request()
    try:
        claims: dict[str, Any] = google.oauth2.id_token.verify_oauth2_token(
            token, request, audience=audience
        )
    except Exception as exc:
        raise ValueError(f"OIDC token verification failed: {exc}") from exc

    if not claims.get("email_verified", False):
        raise ValueError("OIDC token email not verified")

    email = claims.get("email", "")
    if email != expected_email:
        raise ValueError(f"OIDC token email {email!r} does not match expected {expected_email!r}")


def _download_cache_db_from_gcs() -> None:
    """Download cache.db from GCS to the local cache directory (blocking).

    Uses atomic write: downloads to ``.cache.db.download`` then renames to
    ``cache.db`` to prevent the MCP server from reading a half-written file.

    Raises:
        RuntimeError: When ``GCS_BUCKET`` is not set or the object is not found.
    """
    from pathlib import Path

    bucket_name = os.environ.get("GCS_BUCKET", "")
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET environment variable is not set")

    from google.cloud import storage as gcs  # type: ignore[import-untyped]
    from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]

    prefix = os.environ.get("GCS_PREFIX", "jquants-mcp/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    cache_dir = Path(os.environ.get("JQUANTS_CACHE_DIR", "/tmp"))
    local_path = cache_dir / "cache.db"
    tmp_path = cache_dir / ".cache.db.download"

    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    blob_name = f"{prefix}cache.db"
    blob = bucket.blob(blob_name)

    logger.info("Downloading gs://%s/%s ...", bucket_name, blob_name)
    try:
        blob.download_to_filename(str(tmp_path))
    except NotFound as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"gs://{bucket_name}/{blob_name} not found") from exc
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"GCS download failed: {exc}") from exc

    tmp_path.rename(local_path)
    size_mb = local_path.stat().st_size / 1024 / 1024
    logger.info("Downloaded cache.db from GCS (%.1f MB)", size_mb)


async def _reload_cache_background() -> None:
    """Background task: download cache.db from GCS then request lazy reload.

    Returns immediately if another reload is already in progress.
    When ``GCS_BUCKET`` is not set (local dev), skips the download and
    just flips the lazy-reconnect flag — behaves like SIGHUP.
    """
    global _reload_in_progress, _last_reload_at

    if _reload_in_progress:
        logger.info("Pub/Sub reload: already in progress, ignoring duplicate request")
        return

    _reload_in_progress = True
    try:
        if os.environ.get("GCS_BUCKET"):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _download_cache_db_from_gcs)
        else:
            logger.info("GCS_BUCKET not set; skipping download, flagging lazy reconnect only")

        _get_cache().request_reload()
        _last_reload_at = time.time()
        logger.info("Cache reload scheduled (last_reload_at=%.3f)", _last_reload_at)
    except Exception as exc:
        logger.error("Cache reload background task failed: %s", exc)
    finally:
        _reload_in_progress = False


@mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"])
async def _handle_protected_resource_metadata(request: Request) -> Response:
    """RFC 9728: OAuth 2.0 Protected Resource Metadata.

    Lets MCP clients discover the authorization server from the resource URL.
    Only handles root-level deployments (resource = {base_url}/mcp).
    When deployed behind a path-prefix reverse proxy the proxy must serve the
    RFC 9728 well-known URL at the domain root level.
    """
    base_url = _get_settings().oauth_base_url.rstrip("/")
    if not base_url:
        return Response(
            status_code=404,
            content='{"error":"OAuth not configured"}',
            media_type="application/json",
        )
    data = {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
    }
    return Response(
        content=json.dumps(data),
        status_code=200,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET", "OPTIONS"])
async def _handle_openid_configuration(request: Request) -> Response:
    """OIDC discovery — alias for OAuth 2.0 Authorization Server Metadata (RFC 8414).

    Claude Desktop and its backend probe this endpoint before the MCP-spec
    RFC 8414 path.  jquants-mcp uses GitHub OAuth (not OpenID Connect), but
    returning the OAuth server metadata here is sufficient for clients to
    discover the authorization and token endpoints.
    """
    if not _get_settings().oauth_base_url:
        return Response(
            status_code=404,
            content='{"error":"OAuth not configured"}',
            media_type="application/json",
        )
    host, port = request.scope.get("server", ("127.0.0.1", 8080))
    host_str = f"[{host}]" if ":" in str(host) else host
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://{host_str}:{port}/.well-known/oauth-authorization-server",
                timeout=5.0,
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except httpx.RequestError as exc:
        logger.warning("/.well-known/openid-configuration proxy failed: %s", exc)
        return Response(
            status_code=503,
            content='{"error":"service unavailable"}',
            media_type="application/json",
        )


@mcp.custom_route("/internal/reload", methods=["POST"])
async def _handle_pubsub_reload(request: Request) -> Response:
    """Accept a GCS Pub/Sub push notification and schedule a cache.db reload.

    Security: when ``PUBSUB_INVOKER_SA`` is configured, the endpoint verifies
    the Google-signed OIDC token delivered in the ``Authorization`` header.
    The audience must match ``PUBSUB_AUDIENCE`` (or defaults to the request URL).

    The cache.db download runs synchronously, before the 200 is returned, so
    the work happens while this push request is still active. Under request-based
    billing (CPU throttled between requests) a detached background task would be
    CPU-starved the instant the handler returned and could die at scale-to-zero,
    leaving a freshly published cache.db unloaded. The push subscription's ack
    deadline must therefore exceed the download time
    (see ops/pubsub/setup.md: ``--ack-deadline=180``); the ``_reload_in_progress``
    guard dedups any Pub/Sub redelivery that a slow download might trigger.
    """
    expected_sa = os.environ.get("PUBSUB_INVOKER_SA", "")
    if expected_sa:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Pub/Sub reload: missing or malformed Authorization header")
            return Response(
                content='{"error":"missing token"}',
                status_code=401,
                media_type="application/json",
            )

        raw_token = auth_header[len("Bearer ") :]
        audience = os.environ.get("PUBSUB_AUDIENCE", str(request.url))

        try:
            await asyncio.get_running_loop().run_in_executor(
                None, _verify_pubsub_oidc_token, raw_token, expected_sa, audience
            )
        except ValueError as exc:
            logger.warning("Pub/Sub reload: OIDC verification failed: %s", exc)
            return Response(
                content='{"error":"unauthorized"}',
                status_code=403,
                media_type="application/json",
            )
    else:
        logger.debug("PUBSUB_INVOKER_SA not set; skipping OIDC verification")

    # Await the download here (rather than a detached create_task) so it runs
    # under the allocated CPU of this active push request — see the docstring.
    await _reload_cache_background()
    return Response(
        content='{"status":"reloaded"}',
        status_code=200,
        media_type="application/json",
    )


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

    from fastmcp.server.dependencies import get_access_token

    from .exceptions import UserNotConfiguredError

    token = get_access_token()

    # No auth or static Bearer token -> use the global client.
    if token is None or token.client_id == "bearer":
        client = _get_client()
        await _ensure_plan_detected(client)
        return client

    user_id = token.client_id

    # Per-user rate limiting (multi-user only; bearer / anonymous were handled above).
    from .audit import audit
    from .rate_limit import RateLimitExceededError

    # Allowlist: reject before rate limiter so untrusted traffic cannot
    # consume our shared bucket capacity.
    from .allowlist import get_user_email, is_email_allowed
    from .exceptions import UserNotAllowedError

    allowed = _get_settings().get_allowed_emails()
    email = get_user_email(token)
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

    # Periodically evict stale clients.
    now_mono = time.monotonic()
    if now_mono - _last_cleanup > _CLEANUP_INTERVAL:
        await _evict_stale_clients()
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


@mcp.tool(annotations=READ_ONLY_LOCAL)
def health_check() -> dict[str, Any]:
    """Check server health, API key configuration, and cache readiness.

    Call this at session start to confirm cache.db has finished loading
    before issuing detect_* or cache_status — the first call after server
    start may take 10–60 seconds while the cache initialises lazily.
    After a tool-call timeout, use this to distinguish a transient
    cache-loading delay from a permanent failure.

    Returns server version, API key status, active plan,
    ``cache_integrity`` (ok / pending / failed / not-checked), and
    ``cache_ready`` (boolean shorthand: true only when cache_integrity is "ok").
    In multi-user mode, returns the authenticated user's plan.
    """
    from fastmcp.server.dependencies import get_access_token

    settings = _get_settings()
    has_key = bool(settings.jquants_api_key)
    plan = settings.jquants_plan or "auto (not yet detected)"

    # In multi-user mode, resolve the actual user's plan.
    token = get_access_token()
    if token is not None and token.client_id != "bearer":
        user_db = _get_user_db()
        if user_db is not None:
            user = user_db.get_user(token.client_id)
            if user is not None:
                plan = user.plan
                has_key = True

    cache = _get_cache()
    integrity = cache.integrity_status
    status = "healthy"
    if integrity.startswith("failed") or integrity.startswith("error"):
        status = "degraded"
    cache_ready = integrity == "ok"

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
        "last_reload_at": _last_reload_at,
    }


@mcp.tool(annotations=READ_ONLY_LOCAL)
def cache_status() -> dict[str, Any]:
    """Show database metadata: table row counts, file size, and detected plan.

    This tool returns cache metadata — it does NOT query screener signals. To detect
    52-week highs/lows use ``detect_52w_high_low``; for YTD highs/lows use
    ``detect_ytd_high_low``; for volume spikes use ``detect_volume_surge``; for price
    limits use ``detect_price_limit``. Do not call this tool to look up market data or
    screener results.

    In multi-user mode, returns the authenticated user's plan instead of the global
    default.
    """
    from fastmcp.server.dependencies import get_access_token

    result = _get_cache().status()

    # In multi-user mode, resolve the actual user's plan.
    token = get_access_token()
    if token is not None and token.client_id != "bearer":
        user_db = _get_user_db()
        if user_db is not None:
            user = user_db.get_user(token.client_id)
            if user is not None:
                result["plan"] = user.plan

    return result


@mcp.tool(annotations=DESTRUCTIVE_LOCAL)
def cache_clear(table: str | None = None) -> dict[str, Any]:
    """Clear cached data.

    Args:
        table: Table name to clear. Clears all tables when omitted.
    """
    result = _get_cache().clear(table)
    return {"cleared": result}


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
    from fastmcp.server.dependencies import get_access_token

    from .models.user import User

    token = get_access_token()
    if token is None or token.client_id == "bearer":
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

    user_id = token.client_id

    # Allowlist check — prevent unauthorized users from registering keys.
    from .allowlist import get_user_email, is_email_allowed, unauthorized_message
    from .audit import audit as _audit_allowlist

    email = get_user_email(token)
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
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None or token.client_id == "bearer":
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

    from .allowlist import get_user_email, is_email_allowed, unauthorized_message
    from .audit import audit

    user_id = token.client_id
    email = get_user_email(token)

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

from .settings import register_settings_routes  # noqa: E402

register_settings_routes(mcp, _get_user_db, _user_clients, _user_client_last_used, _get_settings)


# ------------------------------------------------------------------
# Server startup
# ------------------------------------------------------------------


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8080,
    ssl_certfile: str = "",
    ssl_keyfile: str = "",
    bearer_token: str = "",
    github_client_id: str = "",
    github_client_secret: str = "",
    oauth_base_url: str = "",
) -> None:
    """Start the MCP server.

    Args:
        transport: Transport type ("stdio" or "streamable-http")
        host: Bind address for HTTP transport
        port: Port number for HTTP transport
        ssl_certfile: Path to SSL certificate file
        ssl_keyfile: Path to SSL private key file
        bearer_token: Bearer token for authentication (fallback if OAuth not configured)
        github_client_id: GitHub OAuth App client ID (enables OAuth 2.1)
        github_client_secret: GitHub OAuth App client secret
        oauth_base_url: Public base URL for OAuth endpoints (e.g. https://mcp.example.com)
    """
    logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")
    logger.info("jquants-mcp v%s starting (transport=%s)", __version__, transport)

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # Install SIGHUP handler for lazy cache reload. Safe here because
        # uvicorn only manages SIGINT/SIGTERM, and the handler itself only
        # flips a flag (no I/O), so async reentrancy is not a concern.
        try:
            signal.signal(signal.SIGHUP, _sighup_handler)
            logger.info("SIGHUP handler installed for cache DB reload")
        except (ValueError, OSError) as e:
            # ValueError: signal only works in main thread
            # OSError: platform without SIGHUP (e.g. Windows)
            logger.warning("Could not install SIGHUP handler: %s", e)

        # Apply CLI overrides to settings before creating the auth provider.
        settings = _get_settings()
        ssl_certfile = ssl_certfile or settings.ssl_certfile
        ssl_keyfile = ssl_keyfile or settings.ssl_keyfile

        # For OAuth/Bearer settings, CLI overrides take precedence over the config file.
        if bearer_token:
            settings.bearer_token = bearer_token
        if github_client_id:
            settings.github_client_id = github_client_id
        if github_client_secret:
            settings.github_client_secret = github_client_secret
        if oauth_base_url:
            settings.oauth_base_url = oauth_base_url

        # Configure authentication.
        from .auth import create_auth_provider

        auth_provider = create_auth_provider(settings)
        if auth_provider is not None:
            mcp.auth = auth_provider
        else:
            logger.warning(
                "HTTP transport running without authentication. "
                "Set bearer_token or OAuth provider for security."
            )

        # Configure TLS.
        uvicorn_config: dict[str, Any] = {}
        if ssl_certfile and ssl_keyfile:
            uvicorn_config["ssl_certfile"] = ssl_certfile
            uvicorn_config["ssl_keyfile"] = ssl_keyfile
            scheme = "https"
        else:
            scheme = "http"

        logger.info("%s server: %s://%s:%d/mcp", transport, scheme, host, port)
        debug_middleware = [Middleware(OAuthDebugMiddleware)]
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            uvicorn_config=uvicorn_config,
            middleware=debug_middleware,
        )

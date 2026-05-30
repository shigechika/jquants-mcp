"""Session cookie and CSRF token utilities for the /settings Web UI."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

from starlette.requests import Request

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "jquants_session"
_CSRF_COOKIE = "jquants_csrf"
_SESSION_TTL = 86400  # 24 hours


def get_signing_key(settings) -> str:
    """Return the session cookie signing key.

    Prefers ``oauth_jwt_signing_key`` when set.  Falls back to a SHA-256 hash
    of ``encryption_key``, but logs a warning because sharing a secret between
    encryption and signing means a single compromised key breaks both.
    """
    if settings and settings.oauth_jwt_signing_key:
        return settings.oauth_jwt_signing_key
    if settings and settings.encryption_key:
        logger.warning(
            "Session signing key is derived from encryption_key via SHA-256. "
            "Set OAUTH_JWT_SIGNING_KEY (or [oauth] jwt_signing_key) to an "
            "independent secret to isolate session signing from encryption."
        )
        return hashlib.sha256(settings.encryption_key.encode()).hexdigest()
    return ""


def sign_session(
    user_id: str, signing_key: str, ttl: int = _SESSION_TTL, *, email: str = ""
) -> str:
    """Create a signed session cookie value."""
    expires = int(time.time()) + ttl
    payload: dict = {"sub": user_id, "exp": expires}
    if email:
        payload["email"] = email
    payload_str = json.dumps(payload)
    sig = hmac.new(signing_key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
    return f"{payload_str}|{sig}"


def parse_session(cookie: str, signing_key: str) -> dict | None:
    """Verify a signed session cookie and return the payload dict, or None if invalid."""
    try:
        payload_str, sig = cookie.rsplit("|", 1)
        expected = hmac.new(signing_key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(payload_str)
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def verify_session(cookie: str, signing_key: str) -> str | None:
    """Verify a signed session cookie and return user_id, or None if invalid."""
    payload = parse_session(cookie, signing_key)
    return payload.get("sub") if payload else None


def get_or_create_csrf_token(request: Request) -> str:
    """Return the existing CSRF token cookie, or generate a fresh 32-byte hex token."""
    existing = request.cookies.get(_CSRF_COOKIE)
    if existing and len(existing) == 64:
        return existing
    return os.urandom(32).hex()


def validate_csrf(request: Request, form_token: str | None) -> bool:
    """Return True if the submitted CSRF token matches the cookie value."""
    cookie_token = request.cookies.get(_CSRF_COOKIE)
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token, form_token)


def resolve_user_id(
    request: Request, signing_key: str, allowed_emails: list[str] | None = None
) -> str | None:
    """Resolve user identity from session cookie or OAuth access token.

    Resolution order:
    1. Signed session cookie (set after Google Sign-In)
    2. MCP OAuth access token (for MCP client access)

    When ``allowed_emails`` is a non-empty list, the resolved identity must
    carry an email on that allowlist; otherwise the caller is treated as
    unauthenticated (returns ``None``). This mirrors the email allowlist the
    MCP tool paths enforce in ``server.py``, so the /settings Web UI cannot be
    used by an authenticated-but-not-allowlisted user to write or delete a key
    row via *either* the cookie path or the OAuth-token path. An empty or
    omitted allowlist allows any authenticated user (the self-host default).
    """
    from ..allowlist import is_email_allowed

    user_id: str | None = None
    email: str | None = None

    if signing_key:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            payload = parse_session(cookie, signing_key)
            if payload and payload.get("sub"):
                user_id = payload["sub"]
                email = payload.get("email")

    if user_id is None:
        from fastmcp.server.dependencies import get_access_token

        from ..allowlist import get_user_email

        token = get_access_token()
        if token is not None and token.client_id != "bearer":
            user_id = token.client_id
            email = get_user_email(token)

    if user_id is None:
        return None

    if not is_email_allowed(email, allowed_emails or []):
        logger.warning("Settings access denied: resolved identity not on the email allowlist")
        return None

    return user_id

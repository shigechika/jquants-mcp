"""Session cookie and CSRF token utilities for the /settings Web UI."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from starlette.requests import Request

_SESSION_COOKIE = "jquants_session"
_CSRF_COOKIE = "jquants_csrf"
_SESSION_TTL = 86400  # 24 hours


def get_signing_key(settings) -> str:
    """Return the session cookie signing key derived from settings."""
    if settings and settings.oauth_jwt_signing_key:
        return settings.oauth_jwt_signing_key
    if settings and settings.encryption_key:
        return hashlib.sha256(settings.encryption_key.encode()).hexdigest()
    return ""


def sign_session(user_id: str, signing_key: str, ttl: int = _SESSION_TTL) -> str:
    """Create a signed session cookie value."""
    expires = int(time.time()) + ttl
    payload = json.dumps({"sub": user_id, "exp": expires})
    sig = hmac.new(signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_session(cookie: str, signing_key: str) -> str | None:
    """Verify a signed session cookie and return user_id, or None if invalid."""
    try:
        payload_str, sig = cookie.rsplit("|", 1)
        expected = hmac.new(signing_key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(payload_str)
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except Exception:
        return None


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


def resolve_user_id(request: Request, signing_key: str) -> str | None:
    """Resolve user identity from session cookie or OAuth access token.

    Resolution order:
    1. Signed session cookie (set after Google Sign-In)
    2. MCP OAuth access token (for MCP client access)
    """
    if signing_key:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            user_id = verify_session(cookie, signing_key)
            if user_id:
                return user_id
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is not None and token.client_id != "bearer":
        return token.client_id
    return None

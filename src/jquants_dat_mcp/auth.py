"""Authentication providers for the MCP server."""

from __future__ import annotations

import hmac
import logging
import os
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, TokenVerifier

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import OAuthProvider

    from .config import Settings

logger = logging.getLogger(__name__)


class BearerTokenVerifier(TokenVerifier):
    """Verify bearer tokens using constant-time comparison.

    Uses hmac.compare_digest to prevent timing attacks.
    """

    def __init__(self, expected_token: str) -> None:
        super().__init__()
        self._expected = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify the provided token against the expected value."""
        if not token or not hmac.compare_digest(token, self._expected):
            logger.warning("Bearer token authentication failed")
            return None
        return AccessToken(
            token=token,
            client_id="bearer",
            scopes=[],
            expires_at=None,
        )


def _enforce_https(base_url: str) -> None:
    """Enforce HTTPS requirement. HTTP is allowed in development environment."""
    if not base_url.startswith("https://"):
        env = os.environ.get("JQUANTS_ENV", "production")
        if env != "development":
            raise ValueError(
                f"oauth_base_url must use HTTPS in production. "
                f"Got: '{base_url}'. "
                "Set JQUANTS_ENV=development to allow HTTP for local testing."
            )
        logger.warning("oauth_base_url uses HTTP (JQUANTS_ENV=development): %s", base_url)


def _create_github_provider(settings: Settings) -> OAuthProvider:
    """Create a GitHub OAuth 2.1 provider."""
    from fastmcp.server.auth.providers.github import GitHubProvider

    from .oauth_kv_store import SQLiteKeyValueStore

    _enforce_https(settings.oauth_base_url)

    oauth_db_path = settings.get_cache_dir() / "oauth_state.db"
    client_storage = SQLiteKeyValueStore(oauth_db_path)
    logger.info(
        "Initializing GitHub OAuth 2.1 provider (base_url=%s, storage=%s)",
        settings.oauth_base_url,
        oauth_db_path,
    )
    return GitHubProvider(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        base_url=settings.oauth_base_url,
        redirect_path="/oauth/callback",
        jwt_signing_key=settings.oauth_jwt_signing_key or None,
        require_authorization_consent=settings.oauth_require_consent,
        client_storage=client_storage,
    )


def _create_google_provider(settings: Settings) -> OAuthProvider:
    """Create a Google OAuth 2.0 provider."""
    from fastmcp.server.auth.providers.google import GoogleProvider

    from .oauth_kv_store import SQLiteKeyValueStore

    _enforce_https(settings.oauth_base_url)

    oauth_db_path = settings.get_cache_dir() / "oauth_state.db"
    client_storage = SQLiteKeyValueStore(oauth_db_path)
    logger.info(
        "Initializing Google OAuth 2.0 provider (base_url=%s, storage=%s)",
        settings.oauth_base_url,
        oauth_db_path,
    )
    return GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=settings.oauth_base_url,
        redirect_path="/oauth/callback",
        required_scopes=["openid", "email", "profile"],
        jwt_signing_key=settings.oauth_jwt_signing_key or None,
        require_authorization_consent=settings.oauth_require_consent,
        client_storage=client_storage,
        # RFC 8252: localhost redirect_uri can use dynamic ports
        # Claude Desktop uses claude.ai, Claude Code CLI uses localhost
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "http://localhost:*/callback",
            "http://127.0.0.1:*/callback",
        ],
    )


def create_auth_provider(settings: Settings) -> OAuthProvider | TokenVerifier | None:
    """Create the appropriate auth provider based on settings.

    Priority:
    1. OAuth (GitHub or Google, based on oauth_provider setting)
    2. Bearer token (if bearer_token is set)
    3. None (no authentication)

    Args:
        settings: Application settings.

    Returns:
        An auth provider instance, or None if authentication is disabled.
    """
    provider_type = getattr(settings, "oauth_provider", "github").lower()

    # Google OAuth 認証
    if provider_type == "google":
        if settings.google_client_id and settings.google_client_secret and settings.oauth_base_url:
            return _create_google_provider(settings)
        logger.debug(
            "Google OAuth selected but missing credentials "
            "(google_client_id, google_client_secret, oauth_base_url)"
        )

    # GitHub OAuth（デフォルト）
    if settings.github_client_id and settings.github_client_secret and settings.oauth_base_url:
        return _create_github_provider(settings)

    # Bearer token フォールバック
    if settings.bearer_token:
        logger.info("Initializing Bearer token authentication")
        return BearerTokenVerifier(settings.bearer_token)

    return None

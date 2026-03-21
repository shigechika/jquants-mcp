"""Authentication providers for the MCP server."""

from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, TokenVerifier

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import OAuthProvider

    from .config import Settings

logger = logging.getLogger(__name__)


class BearerTokenVerifier(TokenVerifier):
    """Verify bearer tokens using constant-time comparison.

    タイミング攻撃を防止するため hmac.compare_digest を使用する。
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


def create_auth_provider(settings: Settings) -> OAuthProvider | TokenVerifier | None:
    """Create the appropriate auth provider based on settings.

    Priority:
    1. GitHub OAuth 2.1 (if github_client_id + github_client_secret + oauth_base_url are set)
    2. Bearer token (if bearer_token is set)
    3. None (no authentication)

    Args:
        settings: Application settings.

    Returns:
        An auth provider instance, or None if authentication is disabled.
    """
    if settings.github_client_id and settings.github_client_secret and settings.oauth_base_url:
        from fastmcp.server.auth.providers.github import GitHubProvider

        from .oauth_kv_store import SQLiteKeyValueStore

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
            jwt_signing_key=settings.oauth_jwt_signing_key or None,
            require_authorization_consent=settings.oauth_require_consent,
            client_storage=client_storage,
        )

    if settings.bearer_token:
        logger.info("Initializing Bearer token authentication")
        return BearerTokenVerifier(settings.bearer_token)

    return None

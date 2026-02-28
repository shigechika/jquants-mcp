"""Bearer token authentication for the MCP server."""

from __future__ import annotations

import hmac
import logging

from fastmcp.server.auth import AccessToken, TokenVerifier

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
            logger.warning("Bearer token 認証に失敗しました")
            return None
        return AccessToken(
            token=token,
            client_id="bearer",
            scopes=[],
            expires_at=None,
        )

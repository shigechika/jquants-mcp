"""Google OAuth provider for jquants-dat-mcp.

Authentication provider using Google OAuth 2.0.
Implemented with the same OAuthProxy pattern as FastMCP's GitHubProvider.
A future PR to FastMCP v3 upstream is planned.
"""

from __future__ import annotations

import logging

import httpx
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.utilities.auth import parse_scopes
from key_value.aio.protocols import AsyncKeyValue
from pydantic import AnyHttpUrl

logger = logging.getLogger(__name__)


class GoogleTokenVerifier(TokenVerifier):
    """Token verifier for Google OAuth tokens.

    Google OAuth tokens are opaque, so we verify them by calling
    Google's userinfo endpoint to check validity and get user info.
    """

    def __init__(
        self,
        *,
        required_scopes: list[str] | None = None,
        timeout_seconds: int = 10,
    ):
        """Initialize the Google token verifier.

        Args:
            required_scopes: Required OAuth scopes (e.g., ['openid', 'email'])
            timeout_seconds: HTTP request timeout
        """
        super().__init__(required_scopes=required_scopes)
        self.timeout_seconds = timeout_seconds

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify Google OAuth token by calling Google userinfo API."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                # Google userinfo エンドポイントでトークンを検証
                response = await client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={
                        "Authorization": f"Bearer {token}",
                    },
                )

                if response.status_code != 200:
                    logger.debug(
                        "Google token verification failed: %d - %s",
                        response.status_code,
                        response.text[:200],
                    )
                    return None

                user_data = response.json()

                # sub が必須（Google の一意ユーザーID）
                sub = user_data.get("sub")
                if not sub:
                    logger.debug("Google token missing 'sub' claim")
                    return None

                # トークンに紐づくスコープを推定
                # Google の userinfo からはスコープ情報が直接取れないため、
                # レスポンスの内容から推定する
                token_scopes = ["openid"]
                if user_data.get("email"):
                    token_scopes.append("email")
                if user_data.get("name") or user_data.get("picture"):
                    token_scopes.append("profile")

                # required_scopes のチェック
                if self.required_scopes:
                    token_scopes_set = set(token_scopes)
                    required_scopes_set = set(self.required_scopes)
                    if not required_scopes_set.issubset(token_scopes_set):
                        logger.debug(
                            "Google token missing required scopes. Has %s, needs %s",
                            token_scopes_set,
                            required_scopes_set,
                        )
                        return None

                return AccessToken(
                    token=token,
                    client_id=sub,
                    scopes=token_scopes,
                    expires_at=None,
                    claims={
                        "sub": sub,
                        "email": user_data.get("email"),
                        "email_verified": user_data.get("email_verified"),
                        "name": user_data.get("name"),
                        "picture": user_data.get("picture"),
                        "locale": user_data.get("locale"),
                        "google_user_data": user_data,
                    },
                )

        except httpx.RequestError as e:
            logger.debug("Failed to verify Google token: %s", e)
            return None
        except Exception as e:
            logger.debug("Google token verification error: %s", e)
            return None


class GoogleProvider(OAuthProxy):
    """Complete Google OAuth provider for FastMCP.

    This provider adds Google OAuth 2.0 protection to any FastMCP server.
    Just provide your Google OAuth client credentials and a base URL.

    Features:
    - Transparent OAuth proxy to Google
    - Automatic token validation via Google userinfo API
    - OpenID Connect support (openid, email, profile scopes)
    - Offline access with refresh tokens

    Example:
        ```python
        from fastmcp import FastMCP
        from jquants_dat_mcp.google_provider import GoogleProvider

        auth = GoogleProvider(
            client_id="your-google-client-id.apps.googleusercontent.com",
            client_secret="GOCSPX-...",
            base_url="https://my-server.com"
        )

        mcp = FastMCP("My App", auth=auth)
        ```
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: AnyHttpUrl | str,
        issuer_url: AnyHttpUrl | str | None = None,
        redirect_path: str | None = None,
        required_scopes: list[str] | None = None,
        timeout_seconds: int = 10,
        allowed_client_redirect_uris: list[str] | None = None,
        client_storage: AsyncKeyValue | None = None,
        jwt_signing_key: str | bytes | None = None,
        require_authorization_consent: bool = True,
    ):
        """Initialize Google OAuth provider.

        Args:
            client_id: Google OAuth client ID (e.g., "123456.apps.googleusercontent.com")
            client_secret: Google OAuth client secret
            base_url: Public URL where OAuth endpoints will be accessible
            issuer_url: Issuer URL for OAuth metadata (defaults to base_url)
            redirect_path: Redirect path configured in Google Cloud Console
                (defaults to "/auth/callback")
            required_scopes: Required Google scopes
                (defaults to ["openid", "email", "profile"])
            timeout_seconds: HTTP request timeout for Google API calls (defaults to 10)
            allowed_client_redirect_uris: List of allowed redirect URI patterns
            client_storage: Storage backend for OAuth state
            jwt_signing_key: Secret for signing FastMCP JWT tokens
            require_authorization_consent: Whether to require MCP consent screen
                (default True). Google has its own consent screen, so this can
                be disabled to avoid double consent.
        """
        # スコープのデフォルト: OpenID Connect の標準3スコープ
        required_scopes_final = (
            parse_scopes(required_scopes)
            if required_scopes is not None
            else ["openid", "email", "profile"]
        )

        # Google トークン検証器を作成
        token_verifier = GoogleTokenVerifier(
            required_scopes=required_scopes_final,
            timeout_seconds=timeout_seconds,
        )

        # Google OAuth 2.0 エンドポイントで OAuthProxy を初期化
        super().__init__(
            upstream_authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            upstream_token_endpoint="https://oauth2.googleapis.com/token",
            upstream_client_id=client_id,
            upstream_client_secret=client_secret,
            token_verifier=token_verifier,
            base_url=base_url,
            redirect_path=redirect_path,
            issuer_url=issuer_url or base_url,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            client_storage=client_storage,
            jwt_signing_key=jwt_signing_key,
            require_authorization_consent=require_authorization_consent,
            # Google 固有パラメータ:
            # access_type=offline でリフレッシュトークンを取得
            # prompt=consent でリフレッシュトークンの確実な発行
            extra_authorize_params={
                "access_type": "offline",
                "prompt": "consent",
            },
            # Google は client_secret_post を使用
            token_endpoint_auth_method="client_secret_post",
        )

        logger.debug(
            "Initialized Google OAuth provider for client %s with scopes: %s",
            client_id,
            required_scopes_final,
        )

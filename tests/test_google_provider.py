"""Tests for Google OAuth provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jquants_dat_mcp.auth import create_auth_provider
from jquants_dat_mcp.config import Settings
from jquants_dat_mcp.google_provider import GoogleProvider, GoogleTokenVerifier


# ---------------------------------------------------------------------------
# GoogleTokenVerifier tests
# ---------------------------------------------------------------------------

GOOGLE_USERINFO_RESPONSE = {
    "sub": "1234567890",
    "email": "test@example.com",
    "email_verified": True,
    "name": "Test User",
    "picture": "https://lh3.googleusercontent.com/photo.jpg",
    "locale": "ja",
}


@pytest.fixture
def verifier():
    """デフォルトスコープの GoogleTokenVerifier。"""
    return GoogleTokenVerifier(required_scopes=["openid", "email", "profile"])


@pytest.mark.asyncio
async def test_google_verify_token_success(verifier):
    """有効なトークンで AccessToken が返る。"""
    # httpx の response.json() は同期メソッドなので MagicMock を使う
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = GOOGLE_USERINFO_RESPONSE

    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("valid-google-token")

    assert result is not None
    assert result.client_id == "1234567890"
    assert result.claims["email"] == "test@example.com"
    assert result.claims["name"] == "Test User"
    assert "openid" in result.scopes
    assert "email" in result.scopes
    assert "profile" in result.scopes


@pytest.mark.asyncio
async def test_google_verify_token_invalid(verifier):
    """無効なトークンで None が返る。"""
    mock_response = AsyncMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"

    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("invalid-token")

    assert result is None


@pytest.mark.asyncio
async def test_google_verify_token_missing_sub(verifier):
    """sub が欠けたレスポンスで None が返る。"""
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"email": "test@example.com"}  # sub なし

    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("token-without-sub")

    assert result is None


@pytest.mark.asyncio
async def test_google_verify_token_network_error(verifier):
    """ネットワークエラーで None が返る。"""
    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("token")

    assert result is None


@pytest.mark.asyncio
async def test_google_verify_token_timeout(verifier):
    """タイムアウトで None が返る。"""
    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ReadTimeout("Timeout")
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("token")

    assert result is None


@pytest.mark.asyncio
async def test_google_verify_token_insufficient_scopes():
    """必要なスコープが不足している場合 None が返る。"""
    # email のみ返す（profile は返さない）
    verifier = GoogleTokenVerifier(required_scopes=["openid", "email", "profile"])
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "sub": "123",
        "email": "test@example.com",
        # name/picture がないので profile スコープは推定されない
    }

    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("token")

    assert result is None


@pytest.mark.asyncio
async def test_google_verify_token_no_required_scopes():
    """required_scopes なしではスコープチェックをスキップする。"""
    verifier = GoogleTokenVerifier()  # required_scopes=None
    # httpx の response.json() は同期メソッドなので MagicMock を使う
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"sub": "123"}

    with patch("jquants_dat_mcp.google_provider.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await verifier.verify_token("token")

    assert result is not None
    assert result.client_id == "123"


# ---------------------------------------------------------------------------
# GoogleProvider initialization tests
# ---------------------------------------------------------------------------


def test_google_provider_init():
    """GoogleProvider が正しいエンドポイントで初期化される。"""
    with patch.object(GoogleProvider, "__init__", return_value=None):
        # OAuthProxy の super().__init__ が呼ばれることを確認するため
        # 直接コンストラクタ引数を検証
        pass

    # 実際の初期化テスト（OAuthProxy の内部状態を確認）
    provider = GoogleProvider(
        client_id="test-client-id.apps.googleusercontent.com",
        client_secret="GOCSPX-test-secret",
        base_url="https://mcp.example.com",
    )
    assert (
        provider._upstream_authorization_endpoint == "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert provider._upstream_token_endpoint == "https://oauth2.googleapis.com/token"
    assert provider._upstream_client_id == "test-client-id.apps.googleusercontent.com"


def test_google_provider_default_scopes():
    """GoogleProvider のデフォルトスコープが openid, email, profile。"""
    provider = GoogleProvider(
        client_id="test.apps.googleusercontent.com",
        client_secret="secret",
        base_url="https://mcp.example.com",
    )
    # required_scopes は _token_validator に設定される（OAuthProxy の内部属性名）
    assert provider._token_validator.required_scopes == ["openid", "email", "profile"]


def test_google_provider_custom_scopes():
    """カスタムスコープを指定できる。"""
    provider = GoogleProvider(
        client_id="test.apps.googleusercontent.com",
        client_secret="secret",
        base_url="https://mcp.example.com",
        required_scopes=["openid", "email"],
    )
    assert provider._token_validator.required_scopes == ["openid", "email"]


def test_google_provider_extra_authorize_params():
    """Google 固有の extra_authorize_params が設定される。"""
    provider = GoogleProvider(
        client_id="test.apps.googleusercontent.com",
        client_secret="secret",
        base_url="https://mcp.example.com",
    )
    assert provider._extra_authorize_params["access_type"] == "offline"
    assert provider._extra_authorize_params["prompt"] == "consent"


# ---------------------------------------------------------------------------
# Config settings tests
# ---------------------------------------------------------------------------


def test_google_oauth_settings_defaults():
    """Google OAuth 設定のデフォルト値。"""
    settings = Settings()
    assert settings.google_client_id == ""
    assert settings.google_client_secret == ""
    assert settings.oauth_provider == "github"


def test_oauth_provider_setting():
    """oauth_provider を設定できる。"""
    settings = Settings(oauth_provider="google")
    assert settings.oauth_provider == "google"


# ---------------------------------------------------------------------------
# create_auth_provider() with Google OAuth
# ---------------------------------------------------------------------------


def test_create_auth_provider_google_oauth():
    """oauth_provider=google で GoogleProvider が返る。"""
    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="GOCSPX-secret",
        oauth_base_url="https://mcp.example.com",
    )
    # auth.py は _create_google_provider() 内でローカルインポートするため
    # インポート元モジュールをパッチする
    with patch("jquants_dat_mcp.google_provider.GoogleProvider") as MockProvider:
        MockProvider.return_value = object()
        create_auth_provider(settings)
        _, kwargs = MockProvider.call_args
        assert kwargs["client_id"] == "test.apps.googleusercontent.com"
        assert kwargs["redirect_path"] == "/oauth/callback"


def test_create_auth_provider_google_incomplete_fallback_to_bearer():
    """Google OAuth が不完全な場合は GitHub → Bearer にフォールバック。"""
    from jquants_dat_mcp.auth import BearerTokenVerifier

    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="",  # secret なし
        oauth_base_url="https://mcp.example.com",
        bearer_token="fallback-token",
    )
    result = create_auth_provider(settings)
    assert isinstance(result, BearerTokenVerifier)


def test_create_auth_provider_google_https_required(monkeypatch):
    """Google OAuth でも HTTPS が必須。"""
    monkeypatch.delenv("JQUANTS_ENV", raising=False)

    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="GOCSPX-secret",
        oauth_base_url="http://mcp.example.com",  # HTTP → エラー
    )
    with pytest.raises(ValueError, match="HTTPS"):
        create_auth_provider(settings)


def test_create_auth_provider_google_http_ok_in_development(monkeypatch):
    """開発環境では Google OAuth でも HTTP を許可。"""
    monkeypatch.setenv("JQUANTS_ENV", "development")

    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="GOCSPX-secret",
        oauth_base_url="http://localhost:8080",
    )
    # auth.py は _create_google_provider() 内でローカルインポートするため
    # インポート元モジュールをパッチする
    with patch("jquants_dat_mcp.google_provider.GoogleProvider") as MockProvider:
        MockProvider.return_value = object()
        create_auth_provider(settings)  # エラーなし

"""Tests for authentication providers."""

import pytest

from jquants_dat_mcp.auth import BearerTokenVerifier, create_auth_provider
from jquants_dat_mcp.config import Settings

VALID_TOKEN = "abc123secret"


@pytest.fixture
def verifier():
    return BearerTokenVerifier(VALID_TOKEN)


# ---------------------------------------------------------------------------
# BearerTokenVerifier tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_token(verifier):
    """Valid token returns an AccessToken."""
    result = await verifier.verify_token(VALID_TOKEN)
    assert result is not None
    assert result.token == VALID_TOKEN
    assert result.client_id == "bearer"


@pytest.mark.asyncio
async def test_invalid_token(verifier):
    """Invalid token returns None."""
    result = await verifier.verify_token("wrong-token")
    assert result is None


@pytest.mark.asyncio
async def test_empty_token(verifier):
    """Empty token returns None."""
    result = await verifier.verify_token("")
    assert result is None


# ---------------------------------------------------------------------------
# create_auth_provider() factory tests
# ---------------------------------------------------------------------------


def test_create_auth_provider_no_auth():
    """Returns None when no auth settings are configured."""
    settings = Settings(
        bearer_token="", github_client_id="", github_client_secret="", oauth_base_url=""
    )
    result = create_auth_provider(settings)
    assert result is None


def test_create_auth_provider_bearer_token():
    """Returns BearerTokenVerifier when only bearer_token is set."""
    settings = Settings(
        bearer_token="mysecret", github_client_id="", github_client_secret="", oauth_base_url=""
    )
    result = create_auth_provider(settings)
    assert isinstance(result, BearerTokenVerifier)


def test_create_auth_provider_github_oauth():
    """Returns GitHubProvider when GitHub OAuth settings are fully configured."""
    from unittest.mock import patch

    settings = Settings(
        bearer_token="ignored",
        github_client_id="Ov23liTEST",
        github_client_secret="gh_secret_abc",
        oauth_base_url="https://mcp.example.com",
    )
    # GitHubProvider is lazily imported inside create_auth_provider; patch at source
    with patch("fastmcp.server.auth.providers.github.GitHubProvider") as MockProvider:
        MockProvider.return_value = object()
        create_auth_provider(settings)
        _, kwargs = MockProvider.call_args
        assert kwargs.get("redirect_path") == "/oauth/callback"


def test_create_auth_provider_github_oauth_incomplete():
    """Returns BearerTokenVerifier (fallback) when GitHub OAuth is only partially configured."""
    settings = Settings(
        bearer_token="fallback",
        github_client_id="Ov23liTEST",
        github_client_secret="",  # missing secret → not a complete OAuth config
        oauth_base_url="https://mcp.example.com",
    )
    result = create_auth_provider(settings)
    # Falls back to bearer token because OAuth config is incomplete
    assert isinstance(result, BearerTokenVerifier)


def test_create_auth_provider_github_no_base_url():
    """Returns BearerTokenVerifier (fallback) when oauth_base_url is missing."""
    settings = Settings(
        bearer_token="fallback",
        github_client_id="Ov23liTEST",
        github_client_secret="gh_secret_abc",
        oauth_base_url="",  # missing base URL
    )
    result = create_auth_provider(settings)
    assert isinstance(result, BearerTokenVerifier)


# ---------------------------------------------------------------------------
# OAuth settings in config.py
# ---------------------------------------------------------------------------


def test_oauth_require_consent_default():
    """oauth_require_consent defaults to True."""
    settings = Settings()
    assert settings.oauth_require_consent is True


def test_oauth_require_consent_false():
    """oauth_require_consent can be set to False."""
    settings = Settings(oauth_require_consent="false")
    assert settings.oauth_require_consent is False


def test_oauth_require_consent_zero():
    """oauth_require_consent treats '0' as False."""
    settings = Settings(oauth_require_consent="0")
    assert settings.oauth_require_consent is False


def test_oauth_settings_defaults():
    """GitHub OAuth settings default to empty strings."""
    settings = Settings()
    assert settings.github_client_id == ""
    assert settings.github_client_secret == ""
    assert settings.oauth_base_url == ""
    assert settings.oauth_jwt_signing_key == ""


# ---------------------------------------------------------------------------
# HTTPS validation for oauth_base_url
# ---------------------------------------------------------------------------


def test_create_auth_provider_https_base_url_ok():
    """HTTPS base_url is accepted without error."""
    from unittest.mock import patch

    settings = Settings(
        github_client_id="Ov23liTEST",
        github_client_secret="gh_secret_abc",
        oauth_base_url="https://mcp.example.com",
    )
    with patch("fastmcp.server.auth.providers.github.GitHubProvider") as MockProvider:
        MockProvider.return_value = object()
        # Should not raise
        create_auth_provider(settings)


def test_create_auth_provider_http_base_url_raises_in_production(monkeypatch):
    """HTTP base_url raises ValueError in production (JQUANTS_ENV != development)."""
    monkeypatch.delenv("JQUANTS_ENV", raising=False)

    settings = Settings(
        github_client_id="Ov23liTEST",
        github_client_secret="gh_secret_abc",
        oauth_base_url="http://mcp.example.com",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        create_auth_provider(settings)


def test_create_auth_provider_http_base_url_ok_in_development(monkeypatch):
    """HTTP base_url is allowed when JQUANTS_ENV=development."""
    monkeypatch.setenv("JQUANTS_ENV", "development")

    from unittest.mock import patch

    settings = Settings(
        github_client_id="Ov23liTEST",
        github_client_secret="gh_secret_abc",
        oauth_base_url="http://localhost:8080",
    )
    with patch("fastmcp.server.auth.providers.github.GitHubProvider") as MockProvider:
        MockProvider.return_value = object()
        # Should not raise
        create_auth_provider(settings)

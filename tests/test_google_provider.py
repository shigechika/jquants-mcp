"""Tests for Google OAuth provider integration."""

from unittest.mock import patch

import pytest

from jquants_dat_mcp.auth import create_auth_provider
from jquants_dat_mcp.config import Settings


# ---------------------------------------------------------------------------
# GoogleProvider initialization tests (upstream FastMCP)
# ---------------------------------------------------------------------------


def test_google_provider_init():
    """GoogleProvider initializes with correct Google endpoints."""
    from fastmcp.server.auth.providers.google import GoogleProvider

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


def test_google_provider_extra_authorize_params():
    """Google-specific extra_authorize_params are set."""
    from fastmcp.server.auth.providers.google import GoogleProvider

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
    """Google OAuth settings have correct defaults."""
    settings = Settings()
    assert settings.google_client_id == ""
    assert settings.google_client_secret == ""
    assert settings.oauth_provider == "github"


def test_oauth_provider_setting():
    """oauth_provider can be set to google."""
    settings = Settings(oauth_provider="google")
    assert settings.oauth_provider == "google"


# ---------------------------------------------------------------------------
# create_auth_provider() with Google OAuth
# ---------------------------------------------------------------------------


def test_create_auth_provider_google_oauth():
    """oauth_provider=google creates GoogleProvider with correct params."""
    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="GOCSPX-secret",
        oauth_base_url="https://mcp.example.com",
    )
    with patch("fastmcp.server.auth.providers.google.GoogleProvider") as MockProvider:
        MockProvider.return_value = object()
        create_auth_provider(settings)
        _, kwargs = MockProvider.call_args
        assert kwargs["client_id"] == "test.apps.googleusercontent.com"
        assert kwargs["redirect_path"] == "/oauth/callback"
        assert kwargs["required_scopes"] == ["openid", "email", "profile"]


def test_create_auth_provider_google_incomplete_fallback_to_bearer():
    """Incomplete Google OAuth falls back to GitHub then Bearer."""
    from jquants_dat_mcp.auth import BearerTokenVerifier

    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="",  # missing secret
        oauth_base_url="https://mcp.example.com",
        bearer_token="fallback-token",
    )
    result = create_auth_provider(settings)
    assert isinstance(result, BearerTokenVerifier)


def test_create_auth_provider_google_https_required(monkeypatch):
    """Google OAuth requires HTTPS in production."""
    monkeypatch.delenv("JQUANTS_ENV", raising=False)

    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="GOCSPX-secret",
        oauth_base_url="http://mcp.example.com",  # HTTP -> error
    )
    with pytest.raises(ValueError, match="HTTPS"):
        create_auth_provider(settings)


def test_create_auth_provider_google_http_ok_in_development(monkeypatch):
    """Google OAuth allows HTTP in development environment."""
    monkeypatch.setenv("JQUANTS_ENV", "development")

    settings = Settings(
        oauth_provider="google",
        google_client_id="test.apps.googleusercontent.com",
        google_client_secret="GOCSPX-secret",
        oauth_base_url="http://localhost:8080",
    )
    with patch("fastmcp.server.auth.providers.google.GoogleProvider") as MockProvider:
        MockProvider.return_value = object()
        create_auth_provider(settings)  # no error

"""Tests for /.well-known/* custom routes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_env(tmp_path):
    """Patch server globals for testing."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        oauth_base_url="https://mcp.example.com",
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


@pytest.fixture()
def mock_env_no_oauth(tmp_path):
    """Patch server globals with no OAuth configured."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        oauth_base_url="",
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


def _mock_request(server=("127.0.0.1", 8080)):
    req = MagicMock()
    req.scope = {"server": server}
    return req


# ---------------------------------------------------------------------------
# /.well-known/oauth-protected-resource/mcp
# ---------------------------------------------------------------------------


class TestProtectedResourceMetadata:
    async def test_returns_404_when_oauth_not_configured(self, mock_env_no_oauth):
        req = _mock_request()
        response = await server_module._handle_protected_resource_metadata(req)
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "error" in body

    async def test_returns_rfc9728_metadata(self, mock_env):
        req = _mock_request()
        response = await server_module._handle_protected_resource_metadata(req)
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["resource"] == "https://mcp.example.com/mcp"
        assert body["authorization_servers"] == ["https://mcp.example.com"]

    async def test_trailing_slash_stripped_from_base_url(self, tmp_path):
        settings = Settings(
            jquants_api_key="test-key",
            jquants_plan="premium",
            jquants_cache_dir=str(tmp_path),
            oauth_base_url="https://mcp.example.com/",
        )
        client = JQuantsClient(settings)
        cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)
        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_client", client),
            patch.object(server_module, "_cache", cache),
        ):
            req = _mock_request()
            response = await server_module._handle_protected_resource_metadata(req)
        cache.close()
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["resource"] == "https://mcp.example.com/mcp"

    async def test_cache_control_header_present(self, mock_env):
        req = _mock_request()
        response = await server_module._handle_protected_resource_metadata(req)
        assert response.headers.get("cache-control") == "public, max-age=3600"


# ---------------------------------------------------------------------------
# /.well-known/openid-configuration
# ---------------------------------------------------------------------------


class TestOpenIDConfiguration:
    async def test_returns_404_when_oauth_not_configured(self, mock_env_no_oauth):
        req = _mock_request()
        response = await server_module._handle_openid_configuration(req)
        assert response.status_code == 404
        body = json.loads(response.body)
        assert "error" in body

    async def test_proxies_oauth_server_metadata(self, mock_env):
        metadata = {
            "issuer": "https://mcp.example.com",
            "authorization_endpoint": "https://accounts.example.com/o/oauth2/v2/auth",
        }
        mock_resp = MagicMock()
        mock_resp.content = json.dumps(metadata).encode()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            req = _mock_request()
            response = await server_module._handle_openid_configuration(req)

        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["issuer"] == "https://mcp.example.com"
        assert response.headers.get("cache-control") == "public, max-age=3600"

    async def test_returns_503_on_request_error(self, mock_env):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            req = _mock_request()
            response = await server_module._handle_openid_configuration(req)

        assert response.status_code == 503
        body = json.loads(response.body)
        assert "error" in body

    async def test_ipv6_host_uses_bracket_notation(self, mock_env):
        """IPv6 loopback address should be wrapped in brackets for URL."""
        metadata = {"issuer": "https://mcp.example.com"}
        mock_resp = MagicMock()
        mock_resp.content = json.dumps(metadata).encode()
        mock_resp.status_code = 200

        captured_url = []

        async def _fake_get(url, **kwargs):
            captured_url.append(url)
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = _fake_get
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            req = _mock_request(server=("::1", 8080))
            await server_module._handle_openid_configuration(req)

        assert captured_url[0].startswith("http://[::1]:8080/")

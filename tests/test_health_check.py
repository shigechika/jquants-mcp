"""Tests for health_check and cache_status tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import jquants_dat_mcp.server as server_module
from jquants_dat_mcp.cache.store import CacheStore
from jquants_dat_mcp.config import Settings
from jquants_dat_mcp.client import JQuantsClient
from jquants_dat_mcp.models.user import User

import pytest


@pytest.fixture()
def mock_env(tmp_path):
    """Patch server globals for testing."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
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


async def _call(tool_name: str, **kwargs) -> dict:
    """Call a tool via FastMCP and parse JSON result."""
    result = await server_module.mcp.call_tool(tool_name, kwargs)
    return json.loads(result.content[0].text)


class TestHealthCheck:
    async def test_single_user_returns_settings_plan(self, mock_env):
        """Single-user mode returns the plan from global settings."""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["plan"] == "premium"
        assert result["api_key_configured"] is True

    async def test_bearer_token_returns_settings_plan(self, mock_env):
        """Bearer token auth returns global settings plan."""
        token = MagicMock()
        token.client_id = "bearer"
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            result = await _call("health_check")
        assert result["plan"] == "premium"

    async def test_multi_user_returns_user_plan(self, mock_env):
        """Multi-user mode returns the authenticated user's actual plan."""
        token = MagicMock()
        token.client_id = "user-123"
        user = User(user_id="user-123", api_key="key", plan="standard")
        mock_db = MagicMock()
        mock_db.get_user.return_value = user

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch.object(server_module, "_user_db", mock_db),
        ):
            result = await _call("health_check")

        assert result["plan"] == "standard"
        assert result["api_key_configured"] is True

    async def test_multi_user_unregistered_returns_default(self, mock_env):
        """Unregistered OAuth user gets the global default plan."""
        token = MagicMock()
        token.client_id = "new-user"
        mock_db = MagicMock()
        mock_db.get_user.return_value = None

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch.object(server_module, "_user_db", mock_db),
        ):
            result = await _call("health_check")

        assert result["plan"] == "premium"  # global settings plan
        assert result["api_key_configured"] is True  # from global settings

    async def test_no_user_db_returns_settings_plan(self, mock_env):
        """OAuth user without encryption_key returns global plan."""
        token = MagicMock()
        token.client_id = "user-456"

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch.object(server_module, "_user_db", None),
        ):
            result = await _call("health_check")

        assert result["plan"] == "premium"


class TestCacheStatus:
    async def test_single_user_returns_settings_plan(self, mock_env):
        """Single-user mode returns the plan from global settings (default_plan on cache)."""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("cache_status")
        assert result["plan"] == "premium"  # CacheStore uses settings.jquants_plan
        assert "db_path" in result

    async def test_multi_user_returns_user_plan(self, mock_env):
        """Multi-user mode returns the authenticated user's actual plan."""
        token = MagicMock()
        token.client_id = "user-123"
        user = User(user_id="user-123", api_key="key", plan="standard")
        mock_db = MagicMock()
        mock_db.get_user.return_value = user

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch.object(server_module, "_user_db", mock_db),
        ):
            result = await _call("cache_status")

        assert result["plan"] == "standard"

    async def test_multi_user_unregistered_returns_default(self, mock_env):
        """Unregistered OAuth user gets the cache's default plan."""
        token = MagicMock()
        token.client_id = "new-user"
        mock_db = MagicMock()
        mock_db.get_user.return_value = None

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch.object(server_module, "_user_db", mock_db),
        ):
            result = await _call("cache_status")

        assert result["plan"] == "premium"  # falls back to cache default_plan

    async def test_no_user_db_returns_cache_default_plan(self, mock_env):
        """OAuth user without encryption_key returns cache default plan."""
        token = MagicMock()
        token.client_id = "user-456"

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch.object(server_module, "_user_db", None),
        ):
            result = await _call("cache_status")

        assert result["plan"] == "premium"

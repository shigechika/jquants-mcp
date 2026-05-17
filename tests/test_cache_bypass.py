"""Tests for cache_bypass_auth: OAuth users without a registered API key
fall back to the global client when the bypass flag is enabled."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_token(user_id: str = "github|12345") -> MagicMock:
    tok = MagicMock()
    tok.client_id = user_id
    return tok


def _make_env(tmp_path, cache_bypass_auth: str = "false"):
    settings = Settings(
        jquants_api_key="global-key",
        jquants_plan="standard",
        jquants_cache_dir=str(tmp_path),
        encryption_key="A" * 32,  # enables user_db path
        cache_bypass_auth=cache_bypass_auth,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)
    return settings, client, cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCacheBypassAuth:
    @pytest.mark.asyncio
    async def test_raises_user_not_configured_when_bypass_off(self, tmp_path):
        """Without bypass, OAuth user without key gets UserNotConfiguredError."""
        from jquants_mcp.exceptions import UserNotConfiguredError

        settings, client, cache = _make_env(tmp_path, cache_bypass_auth="false")

        mock_user_db = MagicMock()
        mock_user_db.get_user.return_value = None
        mock_user_db.has_corrupted_key.return_value = False

        mock_rate_limiter = AsyncMock()
        mock_rate_limiter.acquire = AsyncMock()

        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_client", client),
            patch.object(server_module, "_cache", cache),
            patch("fastmcp.server.dependencies.get_access_token", return_value=_fake_token()),
            patch.object(server_module, "_get_user_db", return_value=mock_user_db),
            patch.object(server_module, "_get_rate_limiter", return_value=mock_rate_limiter),
            patch("jquants_mcp.allowlist.get_user_email", return_value="user@example.com"),
            patch("jquants_mcp.allowlist.is_email_allowed", return_value=True),
        ):
            with pytest.raises(UserNotConfiguredError):
                await server_module._get_user_client()

        cache.close()

    @pytest.mark.asyncio
    async def test_returns_global_client_when_bypass_on(self, tmp_path):
        """With bypass enabled, OAuth user without key gets the global client."""
        settings, client, cache = _make_env(tmp_path, cache_bypass_auth="true")

        mock_user_db = MagicMock()
        mock_user_db.get_user.return_value = None
        mock_user_db.has_corrupted_key.return_value = False

        mock_rate_limiter = AsyncMock()
        mock_rate_limiter.acquire = AsyncMock()

        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_client", client),
            patch.object(server_module, "_cache", cache),
            patch("fastmcp.server.dependencies.get_access_token", return_value=_fake_token()),
            patch.object(server_module, "_get_user_db", return_value=mock_user_db),
            patch.object(server_module, "_get_rate_limiter", return_value=mock_rate_limiter),
            patch("jquants_mcp.allowlist.get_user_email", return_value="user@example.com"),
            patch("jquants_mcp.allowlist.is_email_allowed", return_value=True),
        ):
            result = await server_module._get_user_client()

        assert result is client
        cache.close()

    @pytest.mark.asyncio
    async def test_bypass_does_not_suppress_decryption_error(self, tmp_path):
        """Even with bypass on, a corrupted key still raises DecryptionError."""
        from jquants_mcp.exceptions import DecryptionError

        settings, client, cache = _make_env(tmp_path, cache_bypass_auth="true")

        mock_user_db = MagicMock()
        mock_user_db.get_user.return_value = None
        mock_user_db.has_corrupted_key.return_value = True

        mock_rate_limiter = AsyncMock()
        mock_rate_limiter.acquire = AsyncMock()

        with (
            patch.object(server_module, "_settings", settings),
            patch.object(server_module, "_client", client),
            patch.object(server_module, "_cache", cache),
            patch("fastmcp.server.dependencies.get_access_token", return_value=_fake_token()),
            patch.object(server_module, "_get_user_db", return_value=mock_user_db),
            patch.object(server_module, "_get_rate_limiter", return_value=mock_rate_limiter),
            patch("jquants_mcp.allowlist.get_user_email", return_value="user@example.com"),
            patch("jquants_mcp.allowlist.is_email_allowed", return_value=True),
        ):
            with pytest.raises(DecryptionError):
                await server_module._get_user_client()

        cache.close()

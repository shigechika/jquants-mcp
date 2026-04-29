"""Tests for health_check and cache_status tools."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings
from jquants_mcp.client import JQuantsClient
from jquants_mcp.models.user import User

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

    async def test_cache_ready_true_when_ok(self, mock_env):
        """cache_ready is True only when cache_integrity is 'ok'."""
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch.object(
                type(mock_env["cache"]),
                "integrity_status",
                new_callable=lambda: property(lambda self: "ok"),
            ),
        ):
            result = await _call("health_check")
        assert result["cache_ready"] is True
        assert result["cache_integrity"] == "ok"

    async def test_cache_ready_false_when_pending(self, mock_env):
        """cache_ready is False when cache_integrity is 'pending'."""
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch.object(
                type(mock_env["cache"]),
                "integrity_status",
                new_callable=lambda: property(lambda self: "pending"),
            ),
        ):
            result = await _call("health_check")
        assert result["cache_ready"] is False

    async def test_cache_ready_false_when_failed(self, mock_env):
        """cache_ready is False when cache_integrity starts with 'failed'."""
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch.object(
                type(mock_env["cache"]),
                "integrity_status",
                new_callable=lambda: property(lambda self: "failed: checksum mismatch"),
            ),
        ):
            result = await _call("health_check")
        assert result["cache_ready"] is False
        assert result["status"] == "degraded"

    async def test_latest_cache_date_none_when_empty(self, mock_env):
        """latest_cache_date is None when equities_bars_daily has no rows."""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["latest_cache_date"] is None

    async def test_latest_cache_date_returns_max_date(self, mock_env):
        """latest_cache_date returns the most recent date in equities_bars_daily."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [
                {"Code": "10000", "Date": "2026-04-27", "C": 100},
                {"Code": "10000", "Date": "2026-04-28", "C": 101},
            ],
            key_columns=["Code", "Date"],
        )
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["latest_cache_date"] == "2026-04-28"

    async def test_trading_date_today_falls_back_to_weekday(self, mock_env):
        """trading_date_today falls back to nearest past weekday when calendar is empty."""
        # Use a known Monday as today to ensure deterministic result
        known_monday = date(2026, 4, 27)  # Monday
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch("jquants_mcp.cache.store.date") as mock_date,
        ):
            mock_date.today.return_value = known_monday
            result = await _call("health_check")
        assert result["trading_date_today"] == "2026-04-27"

    async def test_trading_date_today_from_calendar(self, mock_env):
        """trading_date_today returns the last HolDivision=0 date from markets_calendar."""
        cache = mock_env["cache"]
        # Seed calendar: 2026-04-28 is trading day, 2026-04-29 is holiday
        cache.put_rows(
            "markets_calendar",
            [
                {"date": "2026-04-28", "HolDivision": "0"},
                {"date": "2026-04-29", "HolDivision": "1"},
            ],
            key_columns=["date"],
        )
        # Simulate today = 2026-04-29 (holiday)
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch("jquants_mcp.cache.store.date") as mock_date,
        ):
            mock_date.today.return_value = date(2026, 4, 29)
            result = await _call("health_check")
        assert result["trading_date_today"] == "2026-04-28"

    async def test_today_cache_ready_true_when_data_is_current(self, mock_env):
        """today_cache_ready is True when cache is ok and latest_date >= trading_today."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [{"Code": "10000", "Date": "2026-04-28", "C": 100}],
            key_columns=["Code", "Date"],
        )
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch.object(
                type(cache),
                "integrity_status",
                new_callable=lambda: property(lambda self: "ok"),
            ),
            patch("jquants_mcp.cache.store.date") as mock_date,
        ):
            mock_date.today.return_value = date(2026, 4, 28)
            result = await _call("health_check")
        assert result["today_cache_ready"] is True
        assert result["latest_cache_date"] == "2026-04-28"

    async def test_today_cache_ready_false_when_data_stale(self, mock_env):
        """today_cache_ready is False when latest_cache_date < trading_date_today."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [{"Code": "10000", "Date": "2026-04-27", "C": 100}],
            key_columns=["Code", "Date"],
        )
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch.object(
                type(cache),
                "integrity_status",
                new_callable=lambda: property(lambda self: "ok"),
            ),
            patch("jquants_mcp.cache.store.date") as mock_date,
        ):
            mock_date.today.return_value = date(2026, 4, 28)
            result = await _call("health_check")
        assert result["today_cache_ready"] is False

    async def test_today_cache_ready_false_when_cache_pending(self, mock_env):
        """today_cache_ready is False when cache_integrity is not ok."""
        cache = mock_env["cache"]
        cache.put_rows(
            "equities_bars_daily",
            [{"Code": "10000", "Date": "2026-04-28", "C": 100}],
            key_columns=["Code", "Date"],
        )
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            patch.object(
                type(cache),
                "integrity_status",
                new_callable=lambda: property(lambda self: "pending"),
            ),
            patch("jquants_mcp.cache.store.date") as mock_date,
        ):
            mock_date.today.return_value = date(2026, 4, 28)
            result = await _call("health_check")
        assert result["today_cache_ready"] is False


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

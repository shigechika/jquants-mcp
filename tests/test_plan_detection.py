"""Tests for automatic plan detection in server.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import jquants_dat_mcp.server as server_module
from jquants_dat_mcp.cache.store import CacheStore
from jquants_dat_mcp.client import JQuantsClient
from jquants_dat_mcp.config import RATE_LIMITS, Settings
from jquants_dat_mcp.exceptions import AuthenticationError, PlanRestrictionError


@pytest.fixture()
def mock_env_auto(tmp_path):
    """Server globals with jquants_plan="" (auto-detect mode)."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="",  # 自動検出モード
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan="")

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


@pytest.fixture()
def mock_env_explicit(tmp_path):
    """Server globals with explicit jquants_plan (no auto-detect)."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="standard",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan="standard")

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


async def _call(tool_name: str, **kwargs) -> dict:
    result = await server_module.mcp.call_tool(tool_name, kwargs)
    return json.loads(result.content[0].text)


class TestEnsurePlanDetected:
    """Tests for _ensure_plan_detected()."""

    async def test_auto_detect_light(self, mock_env_auto):
        """Plan auto-detect resolves to 'light' when standard probe returns 403."""

        async def side_effect(path, *args, **kwargs):
            if "details" in path or "short-ratio" in path:
                raise PlanRestrictionError("forbidden", status_code=403)
            return {"data": []}

        with patch.object(
            mock_env_auto["client"], "get", new_callable=AsyncMock, side_effect=side_effect
        ):
            with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
                await server_module._ensure_plan_detected(mock_env_auto["client"])

        assert mock_env_auto["settings"].jquants_plan == "light"
        assert mock_env_auto["cache"].default_plan == "light"
        assert server_module._plan_detected is True

    async def test_auto_detect_standard(self, mock_env_auto):
        """Plan auto-detect resolves to 'standard' when standard probe succeeds."""
        call_count = 0

        async def side_effect(path, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "details" in path:
                raise PlanRestrictionError("forbidden", status_code=403)
            return {"data": []}

        with patch.object(
            mock_env_auto["client"], "get", new_callable=AsyncMock, side_effect=side_effect
        ):
            await server_module._ensure_plan_detected(mock_env_auto["client"])

        assert mock_env_auto["settings"].jquants_plan == "standard"
        assert mock_env_auto["cache"].default_plan == "standard"

    async def test_auto_detect_free_fallback(self, mock_env_auto):
        """All probes return 403 → fallback to 'free'."""
        with patch.object(
            mock_env_auto["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("forbidden", status_code=403),
        ):
            await server_module._ensure_plan_detected(mock_env_auto["client"])

        assert mock_env_auto["settings"].jquants_plan == "free"
        assert mock_env_auto["cache"].default_plan == "free"

    async def test_explicit_plan_skips_detection(self, mock_env_explicit):
        """When jquants_plan is explicitly set, detection is skipped entirely."""
        mock_get = AsyncMock()
        with patch.object(mock_env_explicit["client"], "get", mock_get):
            await server_module._ensure_plan_detected(mock_env_explicit["client"])

        mock_get.assert_not_awaited()
        assert mock_env_explicit["settings"].jquants_plan == "standard"
        assert server_module._plan_detected is True

    async def test_detection_runs_only_once(self, mock_env_auto):
        """Plan detection runs only on the first call."""
        with patch.object(
            mock_env_auto["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=PlanRestrictionError("forbidden", status_code=403),
        ) as mock_get:
            await server_module._ensure_plan_detected(mock_env_auto["client"])
            # 2回目は何もしない
            await server_module._ensure_plan_detected(mock_env_auto["client"])

        # 3回呼ばれる（premium, standard, light の各プローブ）× 1回のみ
        assert mock_get.call_count == 3

    async def test_detection_error_fallback_to_free(self, mock_env_auto):
        """Network error during detection falls back to 'free'."""
        with patch.object(
            mock_env_auto["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=Exception("network error"),
        ):
            await server_module._ensure_plan_detected(mock_env_auto["client"])

        assert mock_env_auto["settings"].jquants_plan == "free"

    async def test_auth_error_fallback_to_free(self, mock_env_auto):
        """AuthenticationError during detection falls back to 'free' (not re-raised)."""
        with patch.object(
            mock_env_auto["client"],
            "get",
            new_callable=AsyncMock,
            side_effect=AuthenticationError("invalid key"),
        ):
            # AuthenticationError は detect_plan 内で re-raise されるが、
            # _ensure_plan_detected は全例外をキャッチして free にフォールバック
            await server_module._ensure_plan_detected(mock_env_auto["client"])

        assert mock_env_auto["settings"].jquants_plan == "free"

    async def test_rate_limiter_updated(self, mock_env_auto):
        """Rate limiter is updated to match the detected plan."""

        async def side_effect(path, *args, **kwargs):
            if "details" in path:
                raise PlanRestrictionError("forbidden", status_code=403)
            return {"data": []}

        with patch.object(
            mock_env_auto["client"], "get", new_callable=AsyncMock, side_effect=side_effect
        ):
            await server_module._ensure_plan_detected(mock_env_auto["client"])

        assert mock_env_auto["settings"].jquants_plan == "standard"
        # レートリミッターが更新されていることを確認
        limiter = mock_env_auto["client"]._rate_limiter
        assert limiter._max_requests == RATE_LIMITS["standard"]


class TestCacheStoreDefaultPlanSetter:
    """Tests for CacheStore.default_plan setter."""

    def test_setter_updates_plan(self, tmp_path):
        store = CacheStore(tmp_path / "test.db", default_plan="free")
        assert store.default_plan == "free"
        store.default_plan = "standard"
        assert store.default_plan == "standard"
        store.close()


class TestUpdateRateLimit:
    """Tests for JQuantsClient.update_rate_limit()."""

    def test_update_rate_limit(self, settings):
        client = JQuantsClient(settings)
        assert client._rate_limiter._max_requests == RATE_LIMITS["free"]

        client.update_rate_limit("standard")
        assert client._rate_limiter._max_requests == RATE_LIMITS["standard"]

        client.update_rate_limit("premium")
        assert client._rate_limiter._max_requests == RATE_LIMITS["premium"]

    def test_unknown_plan_falls_back_to_free(self, settings):
        client = JQuantsClient(settings)
        client.update_rate_limit("unknown")
        assert client._rate_limiter._max_requests == RATE_LIMITS["free"]


class TestHealthCheckPlanDisplay:
    """Tests for health_check plan display with auto-detect."""

    async def test_undetected_plan_shows_auto(self, mock_env_auto):
        """Before detection, health_check shows 'auto (not yet detected)'."""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["plan"] == "auto (not yet detected)"

    async def test_detected_plan_shows_actual(self, mock_env_auto):
        """After detection, health_check shows the detected plan."""
        mock_env_auto["settings"].jquants_plan = "light"

        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["plan"] == "light"

"""Behavioural tests for the ``register_api_key`` / ``delete_api_key`` MCP tools.

These assertions used to live only in ``tests/test_settings_ui.py``, which
covered the browser ``/settings`` HTTP routes. That path is being removed
along with the rest of the streamable-http surface (#568), but the same
behaviours — plan auto-detection, cached-client eviction, graceful
degradation when plan detection fails, and audit logging on delete — are
implemented independently by the MCP tools, which are kept. Porting the
assertions here means deleting the HTTP path costs no coverage.

Only the behavioural assertions are ported: HTTP status codes, CSRF token
handling and HTML body rendering have no analogue on the tool path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.config import Settings

USER = "alice@example.com"


@pytest.fixture()
def mock_env(tmp_path):
    """server.py globals patched, multi-user mode on, allowlist empty.

    ``allowed_emails=""`` is passed explicitly ("allow all", the self-host
    default) so the allowlist gate never fires: it calls the very ``audit``
    function these tests assert on. Only ``_settings`` and ``_user_db`` are
    patched — neither tool touches ``_client`` or ``_cache``.
    """
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        encryption_key="x" * 32,  # enables multi-user mode
        allowed_emails="",
    )
    user_db = MagicMock()  # avoid real Firestore / SQLite + encryption setup
    user_db.delete_user.return_value = True

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_user_db", user_db),
    ):
        yield user_db


@pytest.fixture()
def probe_client():
    """Stand-in for the client the tool builds to probe the user's plan."""
    client = MagicMock()
    client.close = AsyncMock()
    return client


async def _call(tool: str, **kwargs) -> dict:
    _, structured = await server_module.mcp.call_tool(tool, kwargs)
    return structured


class TestRegisterApiKey:
    async def test_detected_plan_is_stored(self, mock_env, probe_client, monkeypatch):
        # The plan is never taken from the caller: the tool probes J-Quants
        # and persists whatever detect_plan() reports.
        monkeypatch.setenv("JQUANTS_MCP_USER", USER)
        with (
            patch("jquants_mcp.validation.detect_plan", return_value="standard"),
            patch.object(server_module, "JQuantsClient", return_value=probe_client),
        ):
            result = await _call("register_api_key", api_key="my-key")

        mock_env.update_plan.assert_called_once_with(USER, "standard")
        assert result.get("status") == "ok"
        assert result.get("plan") == "standard"
        # The key is absent (not an empty list) when nothing went wrong.
        assert "warnings" not in result
        probe_client.close.assert_awaited_once()

    async def test_registering_evicts_cached_client(self, mock_env, probe_client, monkeypatch):
        # A stale client would keep using the previous API key. The autouse
        # _reset_server_globals fixture (conftest) clears these dicts again.
        monkeypatch.setenv("JQUANTS_MCP_USER", USER)
        server_module._user_clients[USER] = MagicMock()
        server_module._user_client_last_used[USER] = 12345.0

        with (
            patch("jquants_mcp.validation.detect_plan", return_value="free"),
            patch.object(server_module, "JQuantsClient", return_value=probe_client),
        ):
            await _call("register_api_key", api_key="new-key")

        assert USER not in server_module._user_clients
        assert USER not in server_module._user_client_last_used

    async def test_detect_plan_failure_still_registers_with_warning(
        self, mock_env, probe_client, monkeypatch
    ):
        # Plan detection is a best-effort probe: a failure downgrades to the
        # provisional "free" plan and surfaces a warning, it does not abort
        # the registration.
        monkeypatch.setenv("JQUANTS_MCP_USER", USER)
        with (
            patch(
                "jquants_mcp.validation.detect_plan",
                side_effect=Exception("network error"),
            ),
            patch.object(server_module, "JQuantsClient", return_value=probe_client),
        ):
            result = await _call("register_api_key", api_key="my-key")

        assert result.get("status") == "ok"
        assert result.get("plan") == "free"
        assert result.get("warnings") == ["Plan detection skipped due to internal error"]
        mock_env.save_user.assert_called_once()
        mock_env.update_plan.assert_not_called()
        probe_client.close.assert_awaited_once()


class TestDeleteApiKey:
    async def test_delete_evicts_cached_client(self, mock_env, monkeypatch):
        monkeypatch.setenv("JQUANTS_MCP_USER", USER)
        server_module._user_clients[USER] = MagicMock()
        server_module._user_client_last_used[USER] = 99.9

        await _call("delete_api_key")

        assert USER not in server_module._user_clients
        assert USER not in server_module._user_client_last_used

    async def test_audit_logged_on_successful_delete(self, mock_env, monkeypatch):
        monkeypatch.setenv("JQUANTS_MCP_USER", USER)
        with patch("jquants_mcp.audit.audit") as mock_audit:
            result = await _call("delete_api_key")

        mock_audit.assert_called_once_with("delete_api_key", user_id=USER)
        assert result.get("status") == "ok"

    async def test_audit_not_logged_when_user_not_found(self, mock_env, monkeypatch):
        monkeypatch.setenv("JQUANTS_MCP_USER", USER)
        mock_env.delete_user.return_value = False

        with patch("jquants_mcp.audit.audit") as mock_audit:
            result = await _call("delete_api_key")

        mock_audit.assert_not_called()
        assert result.get("status") == "not_found"

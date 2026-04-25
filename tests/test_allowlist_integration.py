"""Integration tests for the allowlist gate at the server.py call sites.

The unit tests in ``test_allowlist.py`` cover the helpers in isolation,
but the original bug was at the **call site** — the server passed the
upstream-IdP ``sub`` (``token.client_id``) where the helper expected an
email. These integration tests exercise the full path from a mocked
OAuth ``AccessToken`` through ``server.py``'s allowlist gate, so a
regression at any call site is caught.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.client import JQuantsClient
from jquants_mcp.config import Settings


@pytest.fixture()
def mock_env(tmp_path):
    """server.py globals patched, allowlist set, multi-user mode on."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
        encryption_key="x" * 32,  # enables multi-user mode
        allowed_emails="alice@example.com,bob@example.com",
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)
    user_db = MagicMock()  # avoid real Firestore / SQLite + encryption setup

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
        patch.object(server_module, "_user_db", user_db),
    ):
        yield {
            "settings": settings,
            "client": client,
            "cache": cache,
            "user_db": user_db,
        }

    cache.close()


def _token(*, client_id: str, email: str | None, email_verified: bool | None = True):
    """Build a fake FastMCP AccessToken with the relevant claim fields.

    Includes the attributes FastMCP's telemetry middleware reads
    (``client_id``, ``scopes``) so the test fixture survives the request
    pipeline before reaching the allowlist gate.
    """
    claims: dict = {"sub": client_id}
    if email is not None:
        claims["email"] = email
    if email_verified is not None:
        claims["email_verified"] = email_verified
    return SimpleNamespace(client_id=client_id, scopes=[], claims=claims)


async def _call(tool: str, **kwargs) -> dict:
    result = await server_module.mcp.call_tool(tool, kwargs)
    return json.loads(result.content[0].text)


class TestRegisterApiKeyAllowlist:
    async def test_allowed_email_passes_gate(self, mock_env):
        # The token's email matches the allowlist; the call gets past
        # the allowlist gate and returns a normal response (not the
        # 'Access denied' error). Plan detection is mocked to a no-op.
        token = _token(client_id="100526143775213853355", email="alice@example.com")
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch("jquants_mcp.validation.detect_plan", return_value="premium"),
        ):
            result = await _call("register_api_key", api_key="abc")
        assert result.get("status") == "ok"

    async def test_unallowed_email_is_rejected(self, mock_env):
        # Same numeric sub as the allowed user above (proves we no
        # longer use sub for matching), but the email is not on the
        # allowlist.
        token = _token(client_id="100526143775213853355", email="mallory@evil.com")
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            result = await _call("register_api_key", api_key="abc")
        assert result.get("error") is True
        assert "mallory@evil.com" in result.get("message", "")

    async def test_numeric_sub_alone_is_rejected(self, mock_env):
        # Pre-fix regression: client_id is the Google sub (numeric).
        # If the gate ever regresses to comparing client_id, the sub
        # would be "in" no allowlist — but importantly it would also
        # not look like an email. Without an email claim the new gate
        # fails closed.
        token = SimpleNamespace(
            client_id="100526143775213853355",
            scopes=[],
            claims={"sub": "100526143775213853355"},  # email missing on purpose
        )
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            result = await _call("register_api_key", api_key="abc")
        assert result.get("error") is True

    async def test_email_verified_false_is_rejected(self, mock_env):
        token = _token(
            client_id="100526143775213853355",
            email="alice@example.com",
            email_verified=False,
        )
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            result = await _call("register_api_key", api_key="abc")
        assert result.get("error") is True

    async def test_audit_log_records_email(self, mock_env, caplog):
        # The audit log should record both the upstream sub (user_id)
        # and the email so operators can grep for either when chasing
        # an incident.
        import logging

        caplog.set_level(logging.INFO, logger="jquants_mcp.audit")
        token = _token(client_id="100526143775213853355", email="mallory@evil.com")
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            await _call("register_api_key", api_key="abc")

        rejection_logs = [
            json.loads(rec.getMessage())
            for rec in caplog.records
            if rec.name == "jquants_mcp.audit"
        ]
        rejection_entries = [e for e in rejection_logs if e.get("action") == "allowlist_rejected"]
        assert rejection_entries, "expected an allowlist_rejected audit entry"
        entry = rejection_entries[-1]
        assert entry.get("user_id") == "100526143775213853355"
        assert entry.get("email") == "mallory@evil.com"


class TestDeleteApiKeyAllowlist:
    async def test_unallowed_email_is_rejected(self, mock_env):
        token = _token(client_id="100526143775213853355", email="mallory@evil.com")
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            result = await _call("delete_api_key")
        assert result.get("error") is True
        assert "mallory@evil.com" in result.get("message", "")

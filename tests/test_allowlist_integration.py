"""Integration tests for the allowlist gate at the server.py call sites.

Under the gateway-identity design (mcp-stdio's per-user child process +
``--user-env JQUANTS_MCP_USER``, "case A"), the server never sees a raw OAuth
token: the gateway resolves and injects a single verified principal, which
*is* the user's email (mcp-stdio's ``--trusted-user-header
X-Forwarded-Email``). ``_current_user_id()`` reads that principal directly, so
there is no longer a separate "sub" vs. "email" distinction to guard against
at this layer (that was a FastMCP-era bug where the server compared the
upstream-IdP sub to the allowlist instead of the email claim; the gateway now
owns that resolution entirely). These tests exercise the full path from the
injected identity through server.py's allowlist gate, so a regression at any
call site is still caught.
"""

from __future__ import annotations

import json
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


async def _call(tool: str, **kwargs) -> dict:
    _, structured = await server_module.mcp.call_tool(tool, kwargs)
    return structured


class TestRegisterApiKeyAllowlist:
    async def test_allowed_email_passes_gate(self, mock_env, monkeypatch):
        # The injected principal matches the allowlist; the call gets past
        # the allowlist gate and returns a normal response (not the
        # 'Access denied' error). Plan detection is mocked to a no-op.
        monkeypatch.setenv("JQUANTS_MCP_USER", "alice@example.com")
        with patch("jquants_mcp.validation.detect_plan", return_value="premium"):
            result = await _call("register_api_key", api_key="abc")
        assert result.get("status") == "ok"

    async def test_unallowed_email_is_rejected(self, mock_env, monkeypatch):
        monkeypatch.setenv("JQUANTS_MCP_USER", "mallory@evil.com")
        result = await _call("register_api_key", api_key="abc")
        assert result.get("error") is True
        assert "mallory@evil.com" in result.get("message", "")

    async def test_no_gateway_identity_requires_auth(self, mock_env, monkeypatch):
        # JQUANTS_MCP_USER unset -> single-user / static-bearer-token path,
        # distinct from an allowlist rejection.
        monkeypatch.delenv("JQUANTS_MCP_USER", raising=False)
        result = await _call("register_api_key", api_key="abc")
        assert result.get("error") is True
        assert "OAuth 2.1" in result.get("message", "")

    async def test_audit_log_records_rejected_email(self, mock_env, monkeypatch, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="jquants_mcp.audit")
        monkeypatch.setenv("JQUANTS_MCP_USER", "mallory@evil.com")
        await _call("register_api_key", api_key="abc")

        rejection_logs = [
            json.loads(rec.getMessage())
            for rec in caplog.records
            if rec.name == "jquants_mcp.audit"
        ]
        rejection_entries = [e for e in rejection_logs if e.get("action") == "allowlist_rejected"]
        assert rejection_entries, "expected an allowlist_rejected audit entry"
        entry = rejection_entries[-1]
        assert entry.get("user_id") == "mallory@evil.com"
        assert entry.get("email") == "mallory@evil.com"


class TestDeleteApiKeyAllowlist:
    async def test_unallowed_email_is_rejected(self, mock_env, monkeypatch):
        monkeypatch.setenv("JQUANTS_MCP_USER", "mallory@evil.com")
        result = await _call("delete_api_key")
        assert result.get("error") is True
        assert "mallory@evil.com" in result.get("message", "")

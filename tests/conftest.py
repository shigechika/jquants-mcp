"""Shared test fixtures for jquants-mcp."""

from __future__ import annotations

from pathlib import Path

import pytest

from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings


@pytest.fixture(autouse=True)
def _v1_shaped_call_tool(request, monkeypatch):
    """Give ``MCPServer.call_tool`` the SDK 1.x return shape inside tests.

    SDK 2.x returns a ``CallToolResult``; 1.x returned ``(content, structured)``
    for tools with an output schema and the bare ``content`` list otherwise. The
    tool-logic tests (200 call sites) were written against the 1.x shape, so
    this adapter keeps them unchanged. Failures already raise ``ToolError`` from
    ``call_tool`` in 2.x (the ``CallToolResult(is_error=True)`` is built later,
    by the request handler), so ``pytest.raises`` checks need no adapting.

    Tests marked ``sdk_call_tool`` opt out and see the real 2.x path, end to end
    through an in-process ``mcp.Client`` (``tests/test_sdk_call_path.py``).
    """
    if request.node.get_closest_marker("sdk_call_tool"):
        return
    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult

    original = MCPServer.call_tool

    async def call_tool(self, name, arguments, context=None):
        result = await original(self, name, arguments, context)
        if not isinstance(result, CallToolResult):
            return result
        if result.structured_content is not None:
            return result.content, result.structured_content
        return result.content

    monkeypatch.setattr(MCPServer, "call_tool", call_tool)


@pytest.fixture(autouse=True)
def _reset_server_globals():
    """Reset mutable server-module globals between tests.

    ``_user_clients`` / ``_user_client_last_used`` are process-wide dicts; left
    populated they leak per-user clients across tests and can mask or trigger
    the cached-client fast path in unrelated cases.
    """
    import jquants_mcp.server as server_module

    def _reset() -> None:
        server_module._plan_detected = False
        server_module._user_clients.clear()
        server_module._user_client_last_used.clear()
        server_module._plan_cache.clear()

    _reset()
    yield
    _reset()


@pytest.fixture()
def tmp_cache_dir(tmp_path: Path) -> Path:
    """Provide a temporary cache directory."""
    return tmp_path


@pytest.fixture()
def settings(tmp_cache_dir: Path) -> Settings:
    """Provide test settings with a temporary cache directory."""
    return Settings(
        jquants_api_key="test-api-key-dummy",
        jquants_plan="free",
        jquants_cache_dir=str(tmp_cache_dir),
    )


@pytest.fixture()
def cache_store(tmp_cache_dir: Path) -> CacheStore:
    """Provide a CacheStore with a temporary database."""
    store = CacheStore(tmp_cache_dir / "test_cache.db", default_plan="standard")
    yield store
    store.close()

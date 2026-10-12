"""The real SDK 2.x tool-call path, end to end through an in-process client.

Every other test sees ``call_tool`` through the 1.x-shape adapter in
``conftest.py``. These go through ``mcp.Client`` -> the request handler ->
``CallToolResult``, which is what a connected client actually receives, and
through ``smoke_test._decode``, which reads that result in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import Context

from jquants_mcp.exceptions import AuthenticationError
from jquants_mcp.server import _JQuantsMCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import smoke_test  # noqa: E402 - needs the sys.path line above

pytestmark = pytest.mark.sdk_call_tool


def _server() -> _JQuantsMCPServer:
    server = _JQuantsMCPServer("t")

    @server.tool()
    async def ok() -> dict:
        return {"rows": 3}

    @server.tool()
    async def auth_fails() -> dict:
        raise AuthenticationError("API key is invalid. Please check JQUANTS_API_KEY.")

    @server.tool()
    async def crashes() -> dict:
        raise RuntimeError("secret detail")

    @server.tool()
    async def nested_auth_fails(ctx: Context) -> dict:
        await ctx.mcp_server.call_tool("auth_fails", {})
        return {}

    return server


async def _call(name: str):
    async with Client(_server()) as client:
        return await client.call_tool(name, {})


async def test_structured_result_decodes():
    result = await _call("ok")
    assert result.is_error is False
    assert smoke_test._decode(result) == {"rows": 3}


async def test_auth_error_reaches_the_client_with_its_text():
    result = await _call("auth_fails")
    assert result.is_error is True
    with pytest.raises(RuntimeError, match="Please check JQUANTS_API_KEY"):
        smoke_test._decode(result)


async def test_auth_error_from_a_nested_call_keeps_its_text():
    result = await _call("nested_auth_fails")
    assert result.is_error is True
    assert "Please check JQUANTS_API_KEY" in smoke_test._first_text(result)


async def test_crash_text_stays_on_the_server():
    result = await _call("crashes")
    assert result.is_error is True
    assert "secret detail" not in smoke_test._first_text(result)

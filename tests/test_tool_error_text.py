"""Anticipated errors keep their message under SDK 2.x (which hides crash text)."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from jquants_mcp.exceptions import AuthenticationError, RateLimitError
from jquants_mcp.server import _JQuantsMCPServer


def _server(exc: Exception) -> _JQuantsMCPServer:
    server = _JQuantsMCPServer("t")

    @server.tool()
    async def boom() -> str:
        raise exc

    return server


@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationError("API key is invalid. Please check JQUANTS_API_KEY."),
        RateLimitError("Rate limit exceeded"),
    ],
)
async def test_anticipated_errors_keep_their_text(exc):
    with pytest.raises(ToolError) as info:
        await _server(exc).call_tool("boom", {})
    assert not isinstance(info.value, UnexpectedToolError)
    assert str(info.value) == f"Error executing tool boom: {exc}"


async def test_other_exceptions_stay_generic():
    with pytest.raises(UnexpectedToolError) as info:
        await _server(RuntimeError("secret detail")).call_tool("boom", {})
    assert "secret detail" not in str(info.value)


async def test_wrapped_anticipated_error_is_unwrapped():
    # A nested tool call or resource read that crashed arrives as an
    # UnexpectedToolError chained onto the original error.
    try:
        raise AuthenticationError("API key is invalid.")
    except AuthenticationError as auth:
        wrapped = UnexpectedToolError("Error executing tool inner")
        wrapped.__cause__ = auth
    with pytest.raises(ToolError) as info:
        await _server(wrapped).call_tool("boom", {})
    assert not isinstance(info.value, UnexpectedToolError)
    assert str(info.value) == "Error executing tool boom: API key is invalid."

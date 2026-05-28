"""Tests for OAuthDebugMiddleware request logging."""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from jquants_mcp.server import (
    _REDACTED_QUERY_PARAMS,
    OAuthDebugMiddleware,
)


def _make_client() -> TestClient:
    async def ok(request):  # noqa: ANN001
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/oauth/callback", ok), Route("/other", ok)])
    app.add_middleware(OAuthDebugMiddleware)
    return TestClient(app)


def test_oauth_callback_redacts_code_and_state(caplog):
    """Authorization code/state in query params are redacted from logs."""
    client = _make_client()
    with caplog.at_level(logging.INFO, logger="jquants_mcp.server"):
        resp = client.get("/oauth/callback?code=SECRET_AUTH_CODE&state=SECRET_STATE&scope=email")
    assert resp.status_code == 200
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET_AUTH_CODE" not in logged
    assert "SECRET_STATE" not in logged
    assert "[REDACTED]" in logged
    # Non-secret params are preserved.
    assert "email" in logged


@pytest.mark.parametrize("param", sorted(_REDACTED_QUERY_PARAMS))
def test_every_sensitive_query_param_is_redacted(caplog, param):
    """Each secret query param in _REDACTED_QUERY_PARAMS is redacted from logs."""
    client = _make_client()
    secret = f"SECRET_{param.upper()}"
    with caplog.at_level(logging.INFO, logger="jquants_mcp.server"):
        client.get(f"/oauth/callback?{param}={secret}")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged
    assert "[REDACTED]" in logged


def test_oauth_request_redacts_credential_headers(caplog):
    """Authorization and Cookie headers are redacted from logs."""
    client = _make_client()
    with caplog.at_level(logging.INFO, logger="jquants_mcp.server"):
        client.get(
            "/oauth/callback",
            headers={
                "Authorization": "Bearer SECRET_BEARER",
                "Cookie": "session=SECRET_COOKIE",
            },
        )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET_BEARER" not in logged
    assert "SECRET_COOKIE" not in logged


def test_non_oauth_path_not_logged(caplog):
    """Requests outside OAuth debug paths are not logged."""
    client = _make_client()
    with caplog.at_level(logging.INFO, logger="jquants_mcp.server"):
        client.get("/other?code=NOT_LOGGED")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "OAuth request" not in logged

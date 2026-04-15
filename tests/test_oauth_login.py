"""Tests for the PKCE login helper (#85)."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import respx

from jquants_dat_mcp import oauth_login
from jquants_dat_mcp.oauth_login import (
    COGNITO_CLIENT_ID,
    COGNITO_DOMAIN,
    LoginError,
    _build_authorize_url,
    _compute_challenge,
    _exchange_code_for_id_token,
    _generate_verifier,
    _post_api_key,
)


def test_verifier_and_challenge_are_s256_compliant():
    verifier = _generate_verifier()
    # PKCE RFC 7636: 43–128 chars, URL-safe, unpadded
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier

    challenge = _compute_challenge(verifier)
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert challenge == expected


def test_authorize_url_contains_required_params():
    url = _build_authorize_url("CHALLENGE")
    assert url.startswith(f"https://{COGNITO_DOMAIN}/oauth2/authorize?")
    assert f"client_id={COGNITO_CLIENT_ID}" in url
    assert "response_type=code" in url
    assert "code_challenge=CHALLENGE" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8697%2Fcallback" in url


@respx.mock
def test_exchange_code_returns_id_token():
    respx.post(f"https://{COGNITO_DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "id_token": "ID.TOKEN.VALUE",
                "access_token": "unused",
                "refresh_token": "unused",
            },
        )
    )
    assert _exchange_code_for_id_token("CODE", "VERIFIER") == "ID.TOKEN.VALUE"


@respx.mock
def test_exchange_code_non_200_raises():
    respx.post(f"https://{COGNITO_DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(400, text="invalid_grant")
    )
    with pytest.raises(LoginError, match="Token exchange failed"):
        _exchange_code_for_id_token("BAD_CODE", "VERIFIER")


@respx.mock
def test_exchange_code_missing_id_token_raises():
    respx.post(f"https://{COGNITO_DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "x"})
    )
    with pytest.raises(LoginError, match="missing id_token"):
        _exchange_code_for_id_token("CODE", "VERIFIER")


@respx.mock
def test_post_api_key_success():
    respx.post("https://api.jquants.com/v2/cli/api-key").mock(
        return_value=httpx.Response(200, json={"apiKey": "refresh.token.xyz"})
    )
    assert _post_api_key("https://api.jquants.com/v2", "ID") == "refresh.token.xyz"


@respx.mock
def test_post_api_key_strips_trailing_slash():
    respx.post("https://api.jquants.com/v2/cli/api-key").mock(
        return_value=httpx.Response(200, json={"apiKey": "K"})
    )
    assert _post_api_key("https://api.jquants.com/v2/", "ID") == "K"


@respx.mock
def test_post_api_key_forbidden_raises():
    respx.post("https://api.jquants.com/v2/cli/api-key").mock(
        return_value=httpx.Response(403, text="no active subscription")
    )
    with pytest.raises(LoginError, match=r"/cli/api-key returned 403"):
        _post_api_key("https://api.jquants.com/v2", "ID")


def test_login_error_is_exception():
    assert issubclass(LoginError, Exception)


def test_module_constants_match_official_cli():
    # Guard against accidental drift from the values published in
    # J-Quants/jquants-cli — changing these requires a coordinated update.
    assert COGNITO_DOMAIN == "auth.jpx-jquants.com"
    assert COGNITO_CLIENT_ID == "3p2n2njg72hq4emn9lr1hksva2"
    assert oauth_login.CALLBACK_PORT == 8697

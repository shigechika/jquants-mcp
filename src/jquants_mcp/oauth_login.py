"""Browser-based OAuth2 PKCE login against J-Quants' Cognito user pool.

Mirrors the flow used by the official
`jquants-cli <https://github.com/J-Quants/jquants-cli>`_: PKCE S256 code
challenge → ``auth.jpx-jquants.com/oauth2/authorize`` → local loopback
callback → code exchange for an ID token → POST to
``{JQUANTS_BASE_URL}/cli/api-key`` → API key (refresh-token equivalent).

Everything here is public information, verified against the CLI source.
Used both by the ``jquants-mcp login`` subcommand and (optionally) by
a future /settings UI helper.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import os
import socketserver
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

COGNITO_DOMAIN = "auth.jpx-jquants.com"
COGNITO_CLIENT_ID = "3p2n2njg72hq4emn9lr1hksva2"
COGNITO_SCOPES = "openid"
DEFAULT_JQUANTS_BASE_URL = "https://api.jquants.com/v2"
CALLBACK_PORT = 8697
CALLBACK_PATH = "/callback"
LOGIN_TIMEOUT_SECS = 300


class LoginError(Exception):
    """Raised when the PKCE login flow fails."""


@dataclass
class LoginResult:
    api_key: str
    id_token: str


def _generate_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(96)).rstrip(b"=").decode("ascii")


def _compute_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot HTTP handler that captures the ?code= query param."""

    captured_code: str | None = None
    captured_error: str | None = None

    # Silence the default access log.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        if "error" in params:
            type(self).captured_error = params["error"][0]
            body = "<h1>Login failed</h1><p>You can close this tab.</p>"
        elif "code" in params:
            type(self).captured_code = params["code"][0]
            body = (
                "<h1>Login successful</h1><p>You can close this tab and return to the terminal.</p>"
            )
        else:
            self.send_error(400, "missing code or error parameter")
            return
        body_bytes = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)


def _build_authorize_url(challenge: str) -> str:
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
    params = {
        "response_type": "code",
        "client_id": COGNITO_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": COGNITO_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"https://{COGNITO_DOMAIN}/oauth2/authorize?{urllib.parse.urlencode(params)}"


def _exchange_code_for_id_token(code: str, verifier: str) -> str:
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
    resp = httpx.post(
        f"https://{COGNITO_DOMAIN}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": COGNITO_CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise LoginError(f"Token exchange failed ({resp.status_code}): {resp.text[:400]}")
    token = resp.json().get("id_token")
    if not token:
        raise LoginError("Token exchange response missing id_token")
    return token


def _post_api_key(base_url: str, id_token: str) -> str:
    resp = httpx.post(
        f"{base_url.rstrip('/')}/cli/api-key",
        headers={"Authorization": f"Bearer {id_token}"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise LoginError(f"/cli/api-key returned {resp.status_code}: {resp.text[:400]}")
    key = resp.json().get("apiKey")
    if not key:
        raise LoginError("/cli/api-key response missing apiKey")
    return key


def perform_login(
    *,
    base_url: str = DEFAULT_JQUANTS_BASE_URL,
    open_browser: bool = True,
) -> LoginResult:
    """Run the PKCE login flow and return the fetched API key.

    Blocks until the user finishes the browser flow, hits the callback, or
    ``LOGIN_TIMEOUT_SECS`` elapses.
    """
    verifier = _generate_verifier()
    challenge = _compute_challenge(verifier)
    authorize_url = _build_authorize_url(challenge)

    _CallbackHandler.captured_code = None
    _CallbackHandler.captured_error = None

    server = socketserver.TCPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    server.timeout = 1.0

    def _serve() -> None:
        deadline = threading.Event()
        threading.Timer(LOGIN_TIMEOUT_SECS, deadline.set).start()
        while not deadline.is_set():
            server.handle_request()
            if (
                _CallbackHandler.captured_code is not None
                or _CallbackHandler.captured_error is not None
            ):
                return

    serve_thread = threading.Thread(target=_serve, name="pkce-callback", daemon=True)
    serve_thread.start()

    print(f"Opening browser to {authorize_url}")
    if open_browser:
        webbrowser.open(authorize_url)
    else:
        print("(open_browser=False — please open the URL above manually)")

    serve_thread.join(timeout=LOGIN_TIMEOUT_SECS + 5)
    server.server_close()

    if _CallbackHandler.captured_error:
        raise LoginError(f"Authorization denied: {_CallbackHandler.captured_error}")
    if _CallbackHandler.captured_code is None:
        raise LoginError(f"Login did not complete within {LOGIN_TIMEOUT_SECS}s")

    id_token = _exchange_code_for_id_token(_CallbackHandler.captured_code, verifier)
    api_key = _post_api_key(base_url, id_token)
    return LoginResult(api_key=api_key, id_token=id_token)

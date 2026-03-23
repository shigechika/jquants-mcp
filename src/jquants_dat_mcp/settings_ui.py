"""Web UI for J-Quants API key registration via browser.

Provides GET/POST /settings, POST /settings/delete, and POST /settings/verify routes
registered as FastMCP custom routes.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import time

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from .audit import audit
from .client import JQuantsClient
from .config import Settings as _Settings
from .models.user import User
from .validation import detect_plan

logger = logging.getLogger(__name__)

_VALID_PLANS = ("free", "light", "standard", "premium")
_SESSION_COOKIE = "jquants_session"
_SESSION_TTL = 86400  # 24 hours
_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


# ------------------------------------------------------------------
# セッション cookie ユーティリティ
# ------------------------------------------------------------------


def _get_signing_key(settings) -> str:
    """セッション cookie の署名キーを取得。"""
    if settings and settings.oauth_jwt_signing_key:
        return settings.oauth_jwt_signing_key
    if settings and settings.encryption_key:
        return hashlib.sha256(settings.encryption_key.encode()).hexdigest()
    return ""


def _sign_session(user_id: str, signing_key: str, ttl: int = _SESSION_TTL) -> str:
    """署名付きセッションcookieを生成。"""
    expires = int(time.time()) + ttl
    payload = json.dumps({"sub": user_id, "exp": expires})
    sig = hmac.new(signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def _verify_session(cookie: str, signing_key: str) -> str | None:
    """セッションcookieを検証。user_idを返す。"""
    try:
        payload_str, sig = cookie.rsplit("|", 1)
        expected = hmac.new(signing_key.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(payload_str)
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except Exception:
        return None


def _resolve_user_id(request: Request, signing_key: str) -> str | None:
    """Cookie → OAuth token の順でユーザーIDを解決。"""
    # セッション cookie を先に確認
    if signing_key:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            user_id = _verify_session(cookie, signing_key)
            if user_id:
                return user_id
    # MCP OAuth トークンを確認（MCPクライアント経由のアクセス）
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is not None and token.client_id != "bearer":
        return token.client_id
    return None


# ------------------------------------------------------------------
# HTML テンプレート
# ------------------------------------------------------------------


def _html_page(title: str, body: str) -> str:
    """Wrap body content in a minimal HTML page."""
    escaped_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title} \u2014 jquants-dat-mcp</title>
  <style>
    body {{ font-family: sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; color: #333; }}
    h1 {{ font-size: 1.4rem; }}
    label {{ display: block; margin-top: 12px; font-weight: bold; }}
    input[type=password], select {{ width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }}
    button {{ margin-top: 16px; padding: 10px 20px; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; }}
    button:hover {{ background: #0052a3; }}
    button.danger {{ background: #cc2200; }}
    button.danger:hover {{ background: #a31b00; }}
    .status {{ margin: 12px 0; padding: 10px; background: #f0f0f0; border-radius: 4px; }}
    .success {{ background: #d4edda; color: #155724; padding: 10px; border-radius: 4px; margin: 12px 0; }}
    .error {{ background: #f8d7da; color: #721c24; padding: 10px; border-radius: 4px; margin: 12px 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def _login_page_html(google_client_id: str) -> str:
    """Google Sign-In ログインページの HTML を生成。"""
    escaped_cid = html.escape(google_client_id)
    body = f"""<h1>J-Quants API Key Settings</h1>
<p>Please sign in with your Google account to manage your API key.</p>
<div id="g_id_onload"
     data-client_id="{escaped_cid}"
     data-callback="onSignIn"
     data-auto_prompt="false">
</div>
<div class="g_id_signin" data-type="standard" data-size="large"></div>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<script>
function onSignIn(response) {{
  fetch('/settings/verify', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{credential: response.credential}})
  }}).then(r => {{ if(r.ok) window.location.reload(); }});
}}
</script>"""
    return _html_page("Sign In", body)


def _form_html(registered_plan: str | None) -> str:
    """Generate the /settings form page body."""
    if registered_plan is not None:
        status_html = (
            f'<div class="status">Currently registered \u2014 Plan: '
            f"<strong>{html.escape(registered_plan)}</strong></div>"
        )
        button_label = "Update API Key"
        delete_section = (
            '<hr style="margin-top:24px">'
            '<form method="post" action="/settings/delete"'
            " onsubmit=\"return confirm('Delete your registered API key?')\">"
            '<button type="submit" class="danger">Delete API Key</button>'
            "</form>"
        )
    else:
        status_html = '<div class="status">No API key registered yet.</div>'
        button_label = "Register API Key"
        delete_section = ""

    plan_options = "\n".join(
        f'  <option value="{p}"{" selected" if p == (registered_plan or "free") else ""}>'
        f"{p}</option>"
        for p in _VALID_PLANS
    )

    body = f"""<h1>J-Quants API Key Settings</h1>
{status_html}
<form method="post" action="/settings">
  <label for="api_key">J-Quants API Key (refresh token)</label>
  <input type="password" id="api_key" name="api_key" required autocomplete="off"
         placeholder="Enter your J-Quants API key">
  <label for="plan">Plan</label>
  <select id="plan" name="plan">
{plan_options}
  </select>
  <button type="submit">{html.escape(button_label)}</button>
</form>
{delete_section}"""
    return _html_page("API Key Settings", body)


# ------------------------------------------------------------------
# ルートハンドラ
# ------------------------------------------------------------------


async def handle_settings_get(request: Request, get_user_db_fn, settings=None) -> Response:
    """Handle GET /settings — show registration form."""
    signing_key = _get_signing_key(settings)
    user_id = _resolve_user_id(request, signing_key)

    if user_id is None:
        # Google Sign-In が設定されていればログインページを表示（200）
        google_client_id = settings.google_client_id if settings else ""
        if google_client_id:
            return HTMLResponse(_login_page_html(google_client_id))
        return HTMLResponse(
            _html_page("Unauthorized", "<p>OAuth authentication is required.</p>"),
            status_code=401,
        )

    user_db = get_user_db_fn()
    if user_db is None:
        return HTMLResponse(
            _html_page(
                "Unavailable",
                "<p>Multi-user mode is not enabled on this server.</p>",
            ),
            status_code=503,
        )

    user = user_db.get_user(user_id)
    registered_plan = user.plan if user is not None else None
    return HTMLResponse(_form_html(registered_plan))


async def handle_settings_post(
    request: Request,
    get_user_db_fn,
    user_clients: dict,
    user_client_last_used: dict,
    settings=None,
) -> Response:
    """Handle POST /settings — save API key."""
    signing_key = _get_signing_key(settings)
    user_id = _resolve_user_id(request, signing_key)

    if user_id is None:
        return HTMLResponse(
            _html_page("Unauthorized", "<p>OAuth authentication is required.</p>"),
            status_code=401,
        )

    user_db = get_user_db_fn()
    if user_db is None:
        return HTMLResponse(
            _html_page(
                "Unavailable",
                "<p>Multi-user mode is not enabled on this server.</p>",
            ),
            status_code=503,
        )

    form = await request.form()
    api_key = (form.get("api_key") or "").strip()
    plan = (form.get("plan") or "free").strip()

    if not api_key:
        body = _html_page(
            "Error",
            '<div class="error">API key is required.</div><p><a href="/settings">Back</a></p>',
        )
        return HTMLResponse(body, status_code=400)

    if plan not in _VALID_PLANS:
        body = _html_page(
            "Error",
            f'<div class="error">Invalid plan: {html.escape(plan)}</div>'
            '<p><a href="/settings">Back</a></p>',
        )
        return HTMLResponse(body, status_code=400)

    user_db.save_user(User(user_id=user_id, api_key=api_key, plan=plan))

    # キャッシュクリア（次回リクエストで新しいキーを使う）
    user_clients.pop(user_id, None)
    user_client_last_used.pop(user_id, None)

    # プラン自動検出
    probe_client = JQuantsClient(_Settings(jquants_api_key=api_key, jquants_plan=plan))
    warnings: list[str] = []
    try:
        detected_plan = await detect_plan(probe_client)
        if detected_plan != plan:
            user_db.update_plan(user_id, detected_plan)
            warnings.append(
                f"Claimed plan '{html.escape(plan)}' differs from detected plan "
                f"'{html.escape(detected_plan)}'. Updated to '{html.escape(detected_plan)}'."
            )
            plan = detected_plan
    except Exception as e:
        logger.warning("Plan detection failed for user %s: %s", user_id, e)
        warnings.append(f"Plan detection skipped: {html.escape(str(e))}")

    audit("register_api_key", user_id=user_id, plan=plan, source="settings_ui")

    warning_html = "".join(f'<div class="status">{w}</div>' for w in warnings)
    body = _html_page(
        "Saved",
        f'<div class="success">API key registered. Plan: <strong>{html.escape(plan)}</strong></div>'
        f"{warning_html}"
        '<p><a href="/settings">Back to settings</a></p>',
    )
    return HTMLResponse(body)


async def handle_settings_delete(
    request: Request,
    get_user_db_fn,
    user_clients: dict,
    user_client_last_used: dict,
    settings=None,
) -> Response:
    """Handle POST /settings/delete — delete registered API key."""
    signing_key = _get_signing_key(settings)
    user_id = _resolve_user_id(request, signing_key)

    if user_id is None:
        return HTMLResponse(
            _html_page("Unauthorized", "<p>OAuth authentication is required.</p>"),
            status_code=401,
        )

    user_db = get_user_db_fn()
    if user_db is None:
        return HTMLResponse(
            _html_page(
                "Unavailable",
                "<p>Multi-user mode is not enabled on this server.</p>",
            ),
            status_code=503,
        )

    deleted = user_db.delete_user(user_id)
    user_clients.pop(user_id, None)
    user_client_last_used.pop(user_id, None)

    if deleted:
        audit("delete_api_key", user_id=user_id, source="settings_ui")
        body = _html_page(
            "Deleted",
            '<div class="success">API key deleted.</div>'
            '<p><a href="/settings">Back to settings</a></p>',
        )
    else:
        body = _html_page(
            "Not Found",
            '<div class="status">No API key was registered.</div>'
            '<p><a href="/settings">Back to settings</a></p>',
        )
    return HTMLResponse(body)


async def handle_settings_verify(request: Request, settings=None) -> Response:
    """Handle POST /settings/verify — verify Google ID token and set session cookie."""
    google_client_id = settings.google_client_id if settings else ""
    signing_key = _get_signing_key(settings)

    if not google_client_id or not signing_key:
        return Response("Google Sign-In not configured", status_code=503)

    try:
        body = await request.json()
        credential = body.get("credential", "")
    except Exception:
        return Response("Invalid request body", status_code=400)

    if not credential:
        return Response("Missing credential", status_code=400)

    # Google tokeninfo API で ID token を検証
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GOOGLE_TOKENINFO_URL,
                params={"id_token": credential},
                timeout=10.0,
            )
            resp.raise_for_status()
            token_data = resp.json()
    except Exception as e:
        logger.warning("Google tokeninfo verification failed: %s", e)
        return Response("Token verification failed", status_code=401)

    # aud（client_id）の検証
    if token_data.get("aud") != google_client_id:
        logger.warning(
            "Token aud mismatch: expected=%s got=%s",
            google_client_id,
            token_data.get("aud"),
        )
        return Response("Token audience mismatch", status_code=401)

    email = token_data.get("email", "")
    if not email:
        return Response("No email in token", status_code=401)

    # 署名付きセッション cookie を生成
    session_value = _sign_session(email, signing_key)
    response = Response("OK", status_code=200)
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=session_value,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/settings",
        max_age=_SESSION_TTL,
    )
    return response


# ------------------------------------------------------------------
# ルート登録
# ------------------------------------------------------------------


def register_settings_routes(
    mcp,
    get_user_db_fn,
    user_clients: dict,
    user_client_last_used: dict,
    get_settings_fn=None,
) -> None:
    """Register /settings custom routes on the FastMCP instance."""

    @mcp.custom_route("/settings", methods=["GET"])
    async def settings_get(request: Request) -> Response:
        settings = get_settings_fn() if get_settings_fn else None
        return await handle_settings_get(request, get_user_db_fn, settings)

    @mcp.custom_route("/settings", methods=["POST"])
    async def settings_post(request: Request) -> Response:
        settings = get_settings_fn() if get_settings_fn else None
        return await handle_settings_post(
            request, get_user_db_fn, user_clients, user_client_last_used, settings
        )

    @mcp.custom_route("/settings/delete", methods=["POST"])
    async def settings_delete(request: Request) -> Response:
        settings = get_settings_fn() if get_settings_fn else None
        return await handle_settings_delete(
            request, get_user_db_fn, user_clients, user_client_last_used, settings
        )

    @mcp.custom_route("/settings/verify", methods=["POST"])
    async def settings_verify(request: Request) -> Response:
        settings = get_settings_fn() if get_settings_fn else None
        return await handle_settings_verify(request, settings)

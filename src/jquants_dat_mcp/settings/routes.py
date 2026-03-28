"""Route handlers and registration for the /settings Web UI."""

from __future__ import annotations

import html
import logging
import os

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from ..audit import audit
from ..client import JQuantsClient
from ..config import Settings as _Settings
from ..models.user import User
from ..validation import detect_plan
from .session import (
    _CSRF_COOKIE,
    _SESSION_COOKIE,
    _SESSION_TTL,
    get_or_create_csrf_token,
    get_signing_key,
    parse_session,
    resolve_user_id,
    sign_session,
    validate_csrf,
)
from .templates import _VALID_PLANS, form_html, html_page, login_page_html

logger = logging.getLogger(__name__)

_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


async def handle_settings_get(request: Request, get_user_db_fn, settings=None) -> Response:
    """Handle GET /settings — show registration form."""
    signing_key = get_signing_key(settings)
    user_id = resolve_user_id(request, signing_key)

    if user_id is None:
        google_client_id = settings.google_client_id if settings else ""
        if google_client_id:
            return HTMLResponse(login_page_html(google_client_id))
        return HTMLResponse(
            html_page("Unauthorized", "<p>OAuth authentication is required.</p>"),
            status_code=401,
        )

    user_db = get_user_db_fn()
    if user_db is None:
        return HTMLResponse(
            html_page("Unavailable", "<p>Multi-user mode is not enabled on this server.</p>"),
            status_code=503,
        )

    # セッション cookie からメールアドレスを取得（表示用）
    display_email: str | None = None
    if signing_key:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            parsed = parse_session(cookie, signing_key)
            if parsed:
                display_email = parsed.get("email")

    user = user_db.get_user(user_id)
    registered_plan = user.plan if user is not None else None
    csrf_token = get_or_create_csrf_token(request)
    is_dev = os.environ.get("JQUANTS_ENV") == "development"
    response = HTMLResponse(form_html(registered_plan, csrf_token, user_email=display_email))
    response.set_cookie(
        key=_CSRF_COOKIE,
        value=csrf_token,
        httponly=False,  # フォームから読み取り可能（same-site が保護機構）
        secure=not is_dev,
        samesite="strict",
        path="/settings",
        max_age=_SESSION_TTL,
    )
    return response


async def handle_settings_post(
    request: Request,
    get_user_db_fn,
    user_clients: dict,
    user_client_last_used: dict,
    settings=None,
) -> Response:
    """Handle POST /settings — save API key."""
    signing_key = get_signing_key(settings)
    user_id = resolve_user_id(request, signing_key)

    if user_id is None:
        return HTMLResponse(
            html_page("Unauthorized", "<p>OAuth authentication is required.</p>"),
            status_code=401,
        )

    user_db = get_user_db_fn()
    if user_db is None:
        return HTMLResponse(
            html_page("Unavailable", "<p>Multi-user mode is not enabled on this server.</p>"),
            status_code=503,
        )

    form = await request.form()
    csrf_token = form.get("csrf_token") or ""
    if not validate_csrf(request, csrf_token):
        return HTMLResponse(
            html_page("Forbidden", "<p>Invalid or missing CSRF token. Please reload the page.</p>"),
            status_code=403,
        )

    api_key = (form.get("api_key") or "").strip()
    plan = (form.get("plan") or "free").strip()

    if not api_key:
        return HTMLResponse(
            html_page(
                "Error",
                '<div class="error">API key is required.</div><p><a href="/settings">Back</a></p>',
            ),
            status_code=400,
        )

    if plan not in _VALID_PLANS:
        return HTMLResponse(
            html_page(
                "Error",
                f'<div class="error">Invalid plan: {html.escape(plan)}</div>'
                '<p><a href="/settings">Back</a></p>',
            ),
            status_code=400,
        )

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
    finally:
        await probe_client.close()

    audit("register_api_key", user_id=user_id, plan=plan, source="settings_ui")

    warning_html = "".join(f'<div class="status">{w}</div>' for w in warnings)
    return HTMLResponse(
        html_page(
            "Saved",
            f'<div class="success">API key registered. Plan: <strong>{html.escape(plan)}</strong></div>'
            f"{warning_html}"
            '<p><a href="/settings">Back to settings</a></p>',
        )
    )


async def handle_settings_delete(
    request: Request,
    get_user_db_fn,
    user_clients: dict,
    user_client_last_used: dict,
    settings=None,
) -> Response:
    """Handle POST /settings/delete — delete registered API key."""
    signing_key = get_signing_key(settings)
    user_id = resolve_user_id(request, signing_key)

    if user_id is None:
        return HTMLResponse(
            html_page("Unauthorized", "<p>OAuth authentication is required.</p>"),
            status_code=401,
        )

    form_data = await request.form()
    csrf_token = form_data.get("csrf_token") or ""
    if not validate_csrf(request, csrf_token):
        return HTMLResponse(
            html_page("Forbidden", "<p>Invalid or missing CSRF token. Please reload the page.</p>"),
            status_code=403,
        )

    user_db = get_user_db_fn()
    if user_db is None:
        return HTMLResponse(
            html_page("Unavailable", "<p>Multi-user mode is not enabled on this server.</p>"),
            status_code=503,
        )

    deleted = user_db.delete_user(user_id)
    user_clients.pop(user_id, None)
    user_client_last_used.pop(user_id, None)

    if deleted:
        audit("delete_api_key", user_id=user_id, source="settings_ui")
        return HTMLResponse(
            html_page(
                "Deleted",
                '<div class="success">API key deleted.</div>'
                '<p><a href="/settings">Back to settings</a></p>',
            )
        )
    return HTMLResponse(
        html_page(
            "Not Found",
            '<div class="status">No API key was registered.</div>'
            '<p><a href="/settings">Back to settings</a></p>',
        )
    )


async def handle_settings_verify(request: Request, settings=None) -> Response:
    """Handle POST /settings/verify — verify Google ID token and set session cookie."""
    google_client_id = settings.google_client_id if settings else ""
    signing_key = get_signing_key(settings)

    if not google_client_id or not signing_key:
        return Response("Google Sign-In not configured", status_code=503)

    try:
        body = await request.json()
        credential = body.get("credential", "")
    except Exception:
        return Response("Invalid request body", status_code=400)

    if not credential:
        return Response("Missing credential", status_code=400)

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

    if token_data.get("aud") != google_client_id:
        logger.warning(
            "Token aud mismatch: expected=%s got=%s",
            google_client_id,
            token_data.get("aud"),
        )
        return Response("Token audience mismatch", status_code=401)

    # email_verified チェック — 未検証メールアドレスからのログインを拒否
    # TODO: tokeninfo エンドポイントは deprecated。google-auth または PyJWT を用いた
    #       JWKS ベースの検証に移行することで、ネットワーク往復を排除しセキュリティを向上できる。
    email_verified = token_data.get("email_verified")
    if email_verified is not True and email_verified != "true":
        logger.warning("Token email_verified is not True: %s", email_verified)
        return Response("Email not verified", status_code=401)

    # sub はGoogleの不変ユーザーID（email はアカウント移行で変わる可能性がある）
    sub = token_data.get("sub", "")
    if not sub:
        return Response("No sub in token", status_code=401)

    email = token_data.get("email", "")
    if not email:
        return Response("No email in token", status_code=401)

    # 署名付きセッション cookie を生成（sub をユーザーID、email を表示用として保存）
    session_value = sign_session(sub, signing_key, email=email)
    response = Response("OK", status_code=200)
    is_dev = os.environ.get("JQUANTS_ENV") == "development"
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=session_value,
        httponly=True,
        secure=not is_dev,
        samesite="lax",
        path="/settings",
        max_age=_SESSION_TTL,
    )
    return response


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

"""Tests for /settings Web UI route handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from jquants_dat_mcp.models.user import User
from jquants_dat_mcp.settings.routes import (
    handle_settings_delete,
    handle_settings_get,
    handle_settings_post,
    handle_settings_verify,
)
from jquants_dat_mcp.settings.session import sign_session as _sign_session
from jquants_dat_mcp.settings.session import verify_session as _verify_session

# CSRF test token — 64 hex chars (same format as os.urandom(32).hex())
_CSRF_TOKEN = "a" * 64

# Patch targets (after refactor to settings/routes.py)
_PATCH_DETECT_PLAN = "jquants_dat_mcp.settings.routes.detect_plan"
_PATCH_JQUANTS_CLIENT = "jquants_dat_mcp.settings.routes.JQuantsClient"
_PATCH_AUDIT = "jquants_dat_mcp.settings.routes.audit"
_PATCH_HTTPX = "jquants_dat_mcp.settings.routes.httpx.AsyncClient"


# ---- ヘルパー ----


def _mock_token(client_id: str = "gh-test-user"):
    """モック OAuth トークン。"""
    token = MagicMock()
    token.client_id = client_id
    return token


def _mock_request(form_data: dict | None = None, csrf: bool = False):
    """モック Starlette Request。

    csrf=True のとき、CSRF cookie とフォームトークンを一致させる。
    """
    req = MagicMock()
    if csrf:
        req.cookies = {"jquants_csrf": _CSRF_TOKEN}
        merged = dict(form_data or {})
        merged.setdefault("csrf_token", _CSRF_TOKEN)
        req.form = AsyncMock(return_value=merged)
    else:
        req.cookies = {}
        req.form = AsyncMock(return_value=form_data or {})
    return req


def _mock_user_db(existing_user: User | None = None, delete_result: bool = True):
    """モック UserStore。"""
    db = MagicMock()
    db.get_user.return_value = existing_user
    db.save_user.return_value = None
    db.delete_user.return_value = delete_result
    db.update_plan.return_value = None
    return db


# ---- GET /settings ----


class TestHandleSettingsGet:
    async def test_unauthenticated_returns_401(self):
        """認証なしで GET すると 401。"""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            resp = await handle_settings_get(_mock_request(), lambda: None)
        assert resp.status_code == 401

    async def test_bearer_token_returns_401(self):
        """Bearer token 認証ユーザーは 401（OAuth 専用）。"""
        token = _mock_token(client_id="bearer")
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_get(_mock_request(), lambda: None)
        assert resp.status_code == 401

    async def test_no_user_db_returns_503(self):
        """encryption_key 未設定（マルチユーザーモード無効）で 503。"""
        token = _mock_token()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_get(_mock_request(), lambda: None)
        assert resp.status_code == 503

    async def test_new_user_shows_empty_form(self):
        """未登録ユーザーにはフォームを表示。"""
        token = _mock_token()
        user_db = _mock_user_db(existing_user=None)
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_get(_mock_request(), lambda: user_db)
        assert resp.status_code == 200
        body = resp.body.decode()
        assert "No API key registered yet" in body
        assert 'name="api_key"' in body

    async def test_registered_user_shows_plan(self):
        """登録済みユーザーにはプランを表示。"""
        token = _mock_token()
        user = User(user_id="gh-test-user", api_key="dummy-key", plan="light")
        user_db = _mock_user_db(existing_user=user)
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_get(_mock_request(), lambda: user_db)
        assert resp.status_code == 200
        body = resp.body.decode()
        assert "light" in body
        assert "Currently registered" in body


# ---- POST /settings ----


class TestHandleSettingsPost:
    async def test_unauthenticated_returns_401(self):
        """認証なしで POST すると 401。"""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            resp = await handle_settings_post(
                _mock_request({"api_key": "k", "plan": "free"}), lambda: None, {}, {}
            )
        assert resp.status_code == 401

    async def test_no_user_db_returns_503(self):
        """マルチユーザーモード無効で 503。"""
        token = _mock_token()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_post(
                _mock_request({"api_key": "k", "plan": "free"}), lambda: None, {}, {}
            )
        assert resp.status_code == 503

    async def test_missing_csrf_token_returns_403(self):
        """CSRF トークンなしで POST すると 403。"""
        token = _mock_token()
        user_db = _mock_user_db()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_post(
                _mock_request({"api_key": "k", "plan": "free"}, csrf=False),
                lambda: user_db,
                {},
                {},
            )
        assert resp.status_code == 403

    async def test_empty_api_key_returns_400(self):
        """空の API キーで 400。"""
        token = _mock_token()
        user_db = _mock_user_db()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_post(
                _mock_request({"api_key": "", "plan": "free"}, csrf=True),
                lambda: user_db,
                {},
                {},
            )
        assert resp.status_code == 400
        assert "required" in resp.body.decode()

    async def test_plan_field_is_ignored(self):
        """Plan field from the form is ignored — plan is auto-detected."""
        token = _mock_token()
        user_db = _mock_user_db()
        mock_probe = MagicMock()
        mock_probe.close = AsyncMock()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                _PATCH_DETECT_PLAN,
                new_callable=AsyncMock,
                return_value="standard",
            ),
            patch(_PATCH_JQUANTS_CLIENT, return_value=mock_probe),
            patch(_PATCH_AUDIT),
        ):
            # Client sends an arbitrary "plan" value — it should be ignored.
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-key", "plan": "ultra"}, csrf=True),
                lambda: user_db,
                {},
                {},
            )
        assert resp.status_code == 200
        # Detected plan wins
        user_db.update_plan.assert_called_once_with("gh-test-user", "standard")

    async def test_successful_registration(self):
        """正常登録で 200 と成功メッセージ。"""
        token = _mock_token()
        user_db = _mock_user_db()

        mock_probe = MagicMock()
        mock_probe.close = AsyncMock()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                _PATCH_DETECT_PLAN,
                new_callable=AsyncMock,
                return_value="light",
            ),
            patch(_PATCH_JQUANTS_CLIENT, return_value=mock_probe),
            patch(_PATCH_AUDIT),
        ):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-api-key", "plan": "light"}, csrf=True),
                lambda: user_db,
                {},
                {},
            )

        assert resp.status_code == 200
        body = resp.body.decode()
        assert "registered" in body
        user_db.save_user.assert_called_once()
        mock_probe.close.assert_awaited_once()

    async def test_detected_plan_is_stored(self):
        """Auto-detected plan is saved via update_plan()."""
        token = _mock_token()
        user_db = _mock_user_db()
        mock_probe = MagicMock()
        mock_probe.close = AsyncMock()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                _PATCH_DETECT_PLAN,
                new_callable=AsyncMock,
                return_value="free",
            ),
            patch(_PATCH_JQUANTS_CLIENT, return_value=mock_probe),
            patch(_PATCH_AUDIT),
        ):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-api-key"}, csrf=True),
                lambda: user_db,
                {},
                {},
            )

        assert resp.status_code == 200
        body = resp.body.decode()
        assert "free" in body
        user_db.update_plan.assert_called_once_with("gh-test-user", "free")
        mock_probe.close.assert_awaited_once()

    async def test_old_client_evicted_from_cache(self):
        """登録時にユーザーのキャッシュクライアントが削除される。"""
        token = _mock_token(client_id="gh-evict-me")
        user_db = _mock_user_db()
        user_clients = {"gh-evict-me": MagicMock()}
        user_client_last_used = {"gh-evict-me": 12345.0}
        mock_probe = MagicMock()
        mock_probe.close = AsyncMock()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                _PATCH_DETECT_PLAN,
                new_callable=AsyncMock,
                return_value="free",
            ),
            patch(_PATCH_JQUANTS_CLIENT, return_value=mock_probe),
            patch(_PATCH_AUDIT),
        ):
            await handle_settings_post(
                _mock_request({"api_key": "new-key", "plan": "free"}, csrf=True),
                lambda: user_db,
                user_clients,
                user_client_last_used,
            )

        assert "gh-evict-me" not in user_clients
        assert "gh-evict-me" not in user_client_last_used
        mock_probe.close.assert_awaited_once()

    async def test_detect_plan_failure_adds_warning(self):
        """プラン検出失敗時は警告を追加して登録は完了。"""
        token = _mock_token()
        user_db = _mock_user_db()
        mock_probe = MagicMock()
        mock_probe.close = AsyncMock()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                _PATCH_DETECT_PLAN,
                new_callable=AsyncMock,
                side_effect=Exception("network error"),
            ),
            patch(_PATCH_JQUANTS_CLIENT, return_value=mock_probe),
            patch(_PATCH_AUDIT),
        ):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-key", "plan": "free"}, csrf=True),
                lambda: user_db,
                {},
                {},
            )

        assert resp.status_code == 200
        body = resp.body.decode()
        assert "skipped" in body
        # 登録自体は完了している
        user_db.save_user.assert_called_once()


# ---- POST /settings/delete ----


class TestHandleSettingsDelete:
    async def test_unauthenticated_returns_401(self):
        """認証なしで DELETE すると 401。"""
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            resp = await handle_settings_delete(_mock_request(), lambda: None, {}, {})
        assert resp.status_code == 401

    async def test_missing_csrf_returns_403(self):
        """CSRF トークンなしで 403。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=True)
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_delete(_mock_request(csrf=False), lambda: user_db, {}, {})
        assert resp.status_code == 403

    async def test_no_user_db_returns_503(self):
        """マルチユーザーモード無効で 503。"""
        token = _mock_token()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_delete(_mock_request(csrf=True), lambda: None, {}, {})
        assert resp.status_code == 503

    async def test_delete_existing_user(self):
        """登録済みユーザーを削除すると成功メッセージ。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=True)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(_PATCH_AUDIT),
        ):
            resp = await handle_settings_delete(_mock_request(csrf=True), lambda: user_db, {}, {})

        assert resp.status_code == 200
        assert "deleted" in resp.body.decode()
        user_db.delete_user.assert_called_once_with("gh-test-user")

    async def test_delete_nonexistent_user(self):
        """未登録ユーザーの削除は not_found メッセージ。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=False)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(_PATCH_AUDIT),
        ):
            resp = await handle_settings_delete(_mock_request(csrf=True), lambda: user_db, {}, {})

        assert resp.status_code == 200
        assert "No API key" in resp.body.decode()

    async def test_client_evicted_on_delete(self):
        """削除時にキャッシュクライアントが削除される。"""
        token = _mock_token(client_id="gh-del-user")
        user_db = _mock_user_db(delete_result=True)
        user_clients = {"gh-del-user": MagicMock()}
        user_client_last_used = {"gh-del-user": 99.9}

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(_PATCH_AUDIT),
        ):
            await handle_settings_delete(
                _mock_request(csrf=True), lambda: user_db, user_clients, user_client_last_used
            )

        assert "gh-del-user" not in user_clients
        assert "gh-del-user" not in user_client_last_used

    async def test_audit_called_on_successful_delete(self):
        """正常削除時に audit ログが記録される。"""
        token = _mock_token(client_id="gh-audit-user")
        user_db = _mock_user_db(delete_result=True)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(_PATCH_AUDIT) as mock_audit,
        ):
            await handle_settings_delete(_mock_request(csrf=True), lambda: user_db, {}, {})

        mock_audit.assert_called_once_with(
            "delete_api_key", user_id="gh-audit-user", source="settings_ui"
        )

    async def test_audit_not_called_when_not_found(self):
        """未登録ユーザーの削除では audit ログは記録しない。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=False)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(_PATCH_AUDIT) as mock_audit,
        ):
            await handle_settings_delete(_mock_request(csrf=True), lambda: user_db, {}, {})

        mock_audit.assert_not_called()


# ---- セッション cookie ユーティリティ ----


class TestSessionCookie:
    def test_sign_and_verify(self):
        """署名・検証の往復テスト。"""
        key = "test-signing-key"
        user_id = "user@example.com"
        cookie = _sign_session(user_id, key)
        assert _verify_session(cookie, key) == user_id

    def test_wrong_key_returns_none(self):
        """署名キーが違えば None を返す。"""
        cookie = _sign_session("user@example.com", "key-a")
        assert _verify_session(cookie, "key-b") is None

    def test_expired_session_returns_none(self):
        """期限切れのセッションは None を返す。"""
        key = "test-key"
        cookie = _sign_session("user@example.com", key, ttl=-1)
        assert _verify_session(cookie, key) is None

    def test_tampered_payload_returns_none(self):
        """ペイロードが改ざんされたら None を返す。"""
        import json

        key = "test-key"
        cookie = _sign_session("user@example.com", key)
        payload_str, sig = cookie.rsplit("|", 1)
        data = json.loads(payload_str)
        data["sub"] = "attacker@example.com"
        tampered = f"{json.dumps(data)}|{sig}"
        assert _verify_session(tampered, key) is None


# ---- GET /settings（Google Sign-In 統合）----


class TestHandleSettingsGetWithGSI:
    def _mock_settings(self, google_client_id="gsi-client-id", signing_key="test-key"):
        """Google Sign-In が設定された settings モック。"""
        s = MagicMock()
        s.google_client_id = google_client_id
        s.oauth_jwt_signing_key = signing_key
        s.encryption_key = ""
        return s

    async def test_unauthenticated_shows_login_page_when_gsi_configured(self):
        """Google Sign-In 設定済みなら未認証時に 200 のログインページを返す。"""
        settings = self._mock_settings()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            resp = await handle_settings_get(_mock_request(), lambda: None, settings)
        assert resp.status_code == 200
        body = resp.body.decode()
        assert "gsi-client-id" in body
        assert "g_id_signin" in body

    async def test_unauthenticated_returns_401_when_gsi_not_configured(self):
        """Google Sign-In 未設定の場合は従来通り 401。"""
        settings = self._mock_settings(google_client_id="")
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            resp = await handle_settings_get(_mock_request(), lambda: None, settings)
        assert resp.status_code == 401

    async def test_valid_session_cookie_is_accepted(self):
        """有効なセッション cookie でフォームを表示。"""
        settings = self._mock_settings()
        session = _sign_session("user@example.com", "test-key")

        req = MagicMock()
        req.cookies = {"jquants_session": session}

        user_db = _mock_user_db(existing_user=None)
        resp = await handle_settings_get(req, lambda: user_db, settings)
        assert resp.status_code == 200
        assert "No API key registered yet" in resp.body.decode()

    async def test_invalid_session_cookie_shows_login_page(self):
        """無効な cookie の場合はログインページを表示。"""
        settings = self._mock_settings()
        req = MagicMock()
        req.cookies = {"jquants_session": "invalid|cookie"}

        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            resp = await handle_settings_get(req, lambda: None, settings)
        assert resp.status_code == 200
        assert "g_id_signin" in resp.body.decode()


# ---- POST /settings/verify ----


class TestHandleSettingsVerify:
    def _mock_settings(self, google_client_id="gsi-client-id", signing_key="test-key"):
        s = MagicMock()
        s.google_client_id = google_client_id
        s.oauth_jwt_signing_key = signing_key
        s.encryption_key = ""
        return s

    def _mock_json_request(self, body: dict):
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        return req

    async def test_returns_503_when_not_configured(self):
        """google_client_id 未設定で 503。"""
        settings = self._mock_settings(google_client_id="")
        req = self._mock_json_request({"credential": "token"})
        resp = await handle_settings_verify(req, settings)
        assert resp.status_code == 503

    async def test_returns_400_on_missing_credential(self):
        """credential がなければ 400。"""
        settings = self._mock_settings()
        req = self._mock_json_request({})
        resp = await handle_settings_verify(req, settings)
        assert resp.status_code == 400

    async def test_returns_401_on_google_api_error(self):
        """Google tokeninfo がエラーを返したら 401。"""
        import httpx

        settings = self._mock_settings()
        req = self._mock_json_request({"credential": "bad-token"})

        with patch(_PATCH_HTTPX) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "401", request=MagicMock(), response=MagicMock()
            )
            resp = await handle_settings_verify(req, settings)

        assert resp.status_code == 401

    async def test_returns_401_on_aud_mismatch(self):
        """aud が client_id と一致しなければ 401。"""
        settings = self._mock_settings(google_client_id="expected-client-id")
        req = self._mock_json_request({"credential": "token"})

        tokeninfo = {"aud": "other-client-id", "email": "user@example.com"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = tokeninfo
        mock_resp.raise_for_status.return_value = None

        with patch(_PATCH_HTTPX) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            resp = await handle_settings_verify(req, settings)

        assert resp.status_code == 401

    async def test_sets_session_cookie_on_success(self):
        """正常検証でセッション cookie を設定して 200 を返す。"""
        settings = self._mock_settings(google_client_id="gsi-client-id")
        req = self._mock_json_request({"credential": "valid-token"})

        tokeninfo = {
            "aud": "gsi-client-id",
            "email": "user@example.com",
            "sub": "uid-123",
            "email_verified": True,
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = tokeninfo
        mock_resp.raise_for_status.return_value = None

        with patch(_PATCH_HTTPX) as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp
            resp = await handle_settings_verify(req, settings)

        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "jquants_session" in set_cookie
        assert "httponly" in set_cookie.lower()

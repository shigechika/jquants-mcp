"""Tests for /settings Web UI route handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from jquants_dat_mcp.models.user import User
from jquants_dat_mcp.settings_ui import (
    handle_settings_delete,
    handle_settings_get,
    handle_settings_post,
)


# ---- ヘルパー ----


def _mock_token(client_id: str = "gh-test-user"):
    """モック OAuth トークン。"""
    token = MagicMock()
    token.client_id = client_id
    return token


def _mock_request(form_data: dict | None = None):
    """モック Starlette Request。"""
    req = MagicMock()
    if form_data is not None:
        req.form = AsyncMock(return_value=form_data)
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

    async def test_empty_api_key_returns_400(self):
        """空の API キーで 400。"""
        token = _mock_token()
        user_db = _mock_user_db()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_post(
                _mock_request({"api_key": "", "plan": "free"}), lambda: user_db, {}, {}
            )
        assert resp.status_code == 400
        assert "required" in resp.body.decode()

    async def test_invalid_plan_returns_400(self):
        """無効なプランで 400。"""
        token = _mock_token()
        user_db = _mock_user_db()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-key", "plan": "ultra"}),
                lambda: user_db,
                {},
                {},
            )
        assert resp.status_code == 400
        assert "Invalid plan" in resp.body.decode()

    async def test_successful_registration(self):
        """正常登録で 200 と成功メッセージ。"""
        token = _mock_token()
        user_db = _mock_user_db()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                "jquants_dat_mcp.settings_ui.detect_plan",
                new_callable=AsyncMock,
                return_value="light",
            ),
            patch("jquants_dat_mcp.settings_ui.JQuantsClient"),
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-api-key", "plan": "light"}),
                lambda: user_db,
                {},
                {},
            )

        assert resp.status_code == 200
        body = resp.body.decode()
        assert "registered" in body
        user_db.save_user.assert_called_once()

    async def test_plan_mismatch_shows_warning(self):
        """プラン不一致時に警告を表示し DB を更新。"""
        token = _mock_token()
        user_db = _mock_user_db()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                "jquants_dat_mcp.settings_ui.detect_plan",
                new_callable=AsyncMock,
                return_value="free",  # 入力は "light" だが検出は "free"
            ),
            patch("jquants_dat_mcp.settings_ui.JQuantsClient"),
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-api-key", "plan": "light"}),
                lambda: user_db,
                {},
                {},
            )

        assert resp.status_code == 200
        body = resp.body.decode()
        assert "differs" in body
        user_db.update_plan.assert_called_once_with("gh-test-user", "free")

    async def test_old_client_evicted_from_cache(self):
        """登録時にユーザーのキャッシュクライアントが削除される。"""
        token = _mock_token(client_id="gh-evict-me")
        user_db = _mock_user_db()
        user_clients = {"gh-evict-me": MagicMock()}
        user_client_last_used = {"gh-evict-me": 12345.0}

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                "jquants_dat_mcp.settings_ui.detect_plan",
                new_callable=AsyncMock,
                return_value="free",
            ),
            patch("jquants_dat_mcp.settings_ui.JQuantsClient"),
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            await handle_settings_post(
                _mock_request({"api_key": "new-key", "plan": "free"}),
                lambda: user_db,
                user_clients,
                user_client_last_used,
            )

        assert "gh-evict-me" not in user_clients
        assert "gh-evict-me" not in user_client_last_used

    async def test_detect_plan_failure_adds_warning(self):
        """プラン検出失敗時は警告を追加して登録は完了。"""
        token = _mock_token()
        user_db = _mock_user_db()

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch(
                "jquants_dat_mcp.settings_ui.detect_plan",
                new_callable=AsyncMock,
                side_effect=Exception("network error"),
            ),
            patch("jquants_dat_mcp.settings_ui.JQuantsClient"),
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            resp = await handle_settings_post(
                _mock_request({"api_key": "my-key", "plan": "free"}),
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

    async def test_no_user_db_returns_503(self):
        """マルチユーザーモード無効で 503。"""
        token = _mock_token()
        with patch("fastmcp.server.dependencies.get_access_token", return_value=token):
            resp = await handle_settings_delete(_mock_request(), lambda: None, {}, {})
        assert resp.status_code == 503

    async def test_delete_existing_user(self):
        """登録済みユーザーを削除すると成功メッセージ。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=True)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            resp = await handle_settings_delete(_mock_request(), lambda: user_db, {}, {})

        assert resp.status_code == 200
        assert "deleted" in resp.body.decode()
        user_db.delete_user.assert_called_once_with("gh-test-user")

    async def test_delete_nonexistent_user(self):
        """未登録ユーザーの削除は not_found メッセージ。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=False)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            resp = await handle_settings_delete(_mock_request(), lambda: user_db, {}, {})

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
            patch("jquants_dat_mcp.settings_ui.audit"),
        ):
            await handle_settings_delete(
                _mock_request(), lambda: user_db, user_clients, user_client_last_used
            )

        assert "gh-del-user" not in user_clients
        assert "gh-del-user" not in user_client_last_used

    async def test_audit_called_on_successful_delete(self):
        """正常削除時に audit ログが記録される。"""
        token = _mock_token(client_id="gh-audit-user")
        user_db = _mock_user_db(delete_result=True)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch("jquants_dat_mcp.settings_ui.audit") as mock_audit,
        ):
            await handle_settings_delete(_mock_request(), lambda: user_db, {}, {})

        mock_audit.assert_called_once_with(
            "delete_api_key", user_id="gh-audit-user", source="settings_ui"
        )

    async def test_audit_not_called_when_not_found(self):
        """未登録ユーザーの削除では audit ログは記録しない。"""
        token = _mock_token()
        user_db = _mock_user_db(delete_result=False)

        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=token),
            patch("jquants_dat_mcp.settings_ui.audit") as mock_audit,
        ):
            await handle_settings_delete(_mock_request(), lambda: user_db, {}, {})

        mock_audit.assert_not_called()

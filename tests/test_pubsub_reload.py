"""Tests for the Pub/Sub-triggered cache reload endpoint."""

from __future__ import annotations

import json
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jquants_mcp.server as server_module
from jquants_mcp.cache.store import CacheStore
from jquants_mcp.config import Settings
from jquants_mcp.client import JQuantsClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_reload_state():
    """Reset global reload state before/after each test."""
    server_module._last_reload_at = None
    server_module._reload_in_progress = False
    yield
    server_module._last_reload_at = None
    server_module._reload_in_progress = False


@pytest.fixture()
def mock_env(tmp_path):
    """Patch server globals for testing."""
    settings = Settings(
        jquants_api_key="test-key",
        jquants_plan="premium",
        jquants_cache_dir=str(tmp_path),
        max_retries=1,
        retry_base_delay=0.01,
    )
    client = JQuantsClient(settings)
    cache = CacheStore(tmp_path / "test.db", default_plan=settings.jquants_plan)

    with (
        patch.object(server_module, "_settings", settings),
        patch.object(server_module, "_client", client),
        patch.object(server_module, "_cache", cache),
    ):
        yield {"settings": settings, "client": client, "cache": cache}

    cache.close()


def _mock_request(
    auth_header: str | None = None,
    url: str = "https://example.com/internal/reload",
) -> MagicMock:
    """Build a minimal Starlette Request mock."""
    req = MagicMock()
    req.headers = {"Authorization": auth_header} if auth_header else {}
    req.url = MagicMock()
    req.url.__str__ = MagicMock(return_value=url)
    return req


@pytest.fixture()
def mock_google_auth(monkeypatch):
    """Inject fake google.auth / google.oauth2 modules into sys.modules.

    google-cloud-* packages are in the [cloud-run] optional extra and are not
    installed in the standard dev environment. Injecting lightweight mock
    modules lets us test the OIDC verification logic without the real packages.
    """
    google_mod = types.ModuleType("google")
    google_auth_mod = types.ModuleType("google.auth")
    google_auth_transport_mod = types.ModuleType("google.auth.transport")
    google_auth_transport_requests_mod = types.ModuleType("google.auth.transport.requests")
    google_auth_transport_requests_mod.Request = MagicMock
    google_oauth2_mod = types.ModuleType("google.oauth2")
    google_oauth2_id_token_mod = types.ModuleType("google.oauth2.id_token")
    google_oauth2_id_token_mod.verify_oauth2_token = MagicMock()

    # Set child modules as attributes on parents so dotted imports resolve correctly.
    google_mod.auth = google_auth_mod
    google_mod.oauth2 = google_oauth2_mod
    google_auth_mod.transport = google_auth_transport_mod
    google_auth_transport_mod.requests = google_auth_transport_requests_mod
    google_oauth2_mod.id_token = google_oauth2_id_token_mod

    for name, mod in [
        ("google", google_mod),
        ("google.auth", google_auth_mod),
        ("google.auth.transport", google_auth_transport_mod),
        ("google.auth.transport.requests", google_auth_transport_requests_mod),
        ("google.oauth2", google_oauth2_mod),
        ("google.oauth2.id_token", google_oauth2_id_token_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    return {
        "id_token": google_oauth2_id_token_mod,
        "transport_requests": google_auth_transport_requests_mod,
    }


@pytest.fixture()
def mock_google_cloud_storage(monkeypatch):
    """Inject fake google.cloud.storage / google.cloud.exceptions into sys.modules."""

    class _NotFound(Exception):
        pass

    google_mod = types.ModuleType("google")
    google_cloud_mod = types.ModuleType("google.cloud")
    google_cloud_storage_mod = types.ModuleType("google.cloud.storage")
    mock_storage_client = MagicMock()
    google_cloud_storage_mod.Client = MagicMock(return_value=mock_storage_client)
    google_cloud_exceptions_mod = types.ModuleType("google.cloud.exceptions")
    google_cloud_exceptions_mod.NotFound = _NotFound

    # Set child modules as attributes on parents so dotted imports resolve correctly.
    google_mod.cloud = google_cloud_mod
    google_cloud_mod.storage = google_cloud_storage_mod
    google_cloud_mod.exceptions = google_cloud_exceptions_mod

    for name, mod in [
        ("google", google_mod),
        ("google.cloud", google_cloud_mod),
        ("google.cloud.storage", google_cloud_storage_mod),
        ("google.cloud.exceptions", google_cloud_exceptions_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    return {
        "storage": google_cloud_storage_mod,
        "Client": google_cloud_storage_mod.Client,
        "NotFound": _NotFound,
        "mock_storage_client": mock_storage_client,
    }


# ---------------------------------------------------------------------------
# _verify_pubsub_oidc_token
# ---------------------------------------------------------------------------


class TestVerifyPubsubOidcToken:
    def test_valid_token_passes(self, mock_google_auth):
        claims = {
            "email": "pubsub@project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        mock_google_auth["id_token"].verify_oauth2_token.return_value = claims
        # Should not raise
        server_module._verify_pubsub_oidc_token(
            "fake-jwt",
            "pubsub@project.iam.gserviceaccount.com",
            "https://example.com/internal/reload",
        )

    def test_wrong_email_raises(self, mock_google_auth):
        claims = {
            "email": "other@project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        mock_google_auth["id_token"].verify_oauth2_token.return_value = claims
        with pytest.raises(ValueError, match="does not match expected"):
            server_module._verify_pubsub_oidc_token(
                "fake-jwt",
                "pubsub@project.iam.gserviceaccount.com",
                "https://example.com/internal/reload",
            )

    def test_email_not_verified_raises(self, mock_google_auth):
        claims = {
            "email": "pubsub@project.iam.gserviceaccount.com",
            "email_verified": False,
        }
        mock_google_auth["id_token"].verify_oauth2_token.return_value = claims
        with pytest.raises(ValueError, match="email not verified"):
            server_module._verify_pubsub_oidc_token(
                "fake-jwt",
                "pubsub@project.iam.gserviceaccount.com",
                "https://example.com/internal/reload",
            )

    def test_invalid_jwt_raises(self, mock_google_auth):
        mock_google_auth["id_token"].verify_oauth2_token.side_effect = Exception("bad token")
        with pytest.raises(ValueError, match="OIDC token verification failed"):
            server_module._verify_pubsub_oidc_token(
                "bad-jwt",
                "pubsub@project.iam.gserviceaccount.com",
                "https://example.com/internal/reload",
            )

    def test_empty_audience_raises(self, mock_google_auth):
        with pytest.raises(ValueError, match="audience must not be empty"):
            server_module._verify_pubsub_oidc_token(
                "fake-jwt",
                "pubsub@project.iam.gserviceaccount.com",
                "",
            )
        # verify_oauth2_token must NOT have been called (audience check is pre-flight)
        mock_google_auth["id_token"].verify_oauth2_token.assert_not_called()

    def test_missing_google_auth_raises(self, monkeypatch):
        # Ensure google modules are NOT in sys.modules so the ImportError path is hit
        for key in list(sys.modules):
            if key.startswith("google"):
                monkeypatch.delitem(sys.modules, key)
        with pytest.raises(ValueError, match="google-auth not installed"):
            server_module._verify_pubsub_oidc_token("tok", "sa@x.com", "aud")


# ---------------------------------------------------------------------------
# _download_cache_db_from_gcs
# ---------------------------------------------------------------------------


class TestDownloadCacheDbFromGcs:
    def test_raises_when_no_bucket(self, monkeypatch):
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        with pytest.raises(RuntimeError, match="GCS_BUCKET"):
            server_module._download_cache_db_from_gcs()

    def test_downloads_atomically(self, tmp_path, monkeypatch, mock_google_cloud_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("GCS_PREFIX", "jquants-mcp/")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))

        def fake_download(path):
            import pathlib

            pathlib.Path(path).write_bytes(b"fake-db-content")

        mock_blob = MagicMock()
        # No compressed object -> the .zst probe returns False (avoids feeding a
        # mock stream to zstandard) and the uncompressed download is exercised.
        mock_blob.open.side_effect = mock_google_cloud_storage["NotFound"]("no zst")
        mock_blob.download_to_filename.side_effect = fake_download
        mock_storage_client = mock_google_cloud_storage["mock_storage_client"]
        mock_storage_client.bucket.return_value.blob.return_value = mock_blob

        server_module._download_cache_db_from_gcs()

        assert (tmp_path / "cache.db").exists()
        assert not (tmp_path / ".cache.db.reload").exists()
        assert (tmp_path / "cache.db").read_bytes() == b"fake-db-content"

    def test_not_found_raises_runtime_error(self, tmp_path, monkeypatch, mock_google_cloud_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))

        NotFound = mock_google_cloud_storage["NotFound"]
        mock_blob = MagicMock()
        mock_blob.open.side_effect = NotFound("no zst")  # compressed object absent
        mock_blob.download_to_filename.side_effect = NotFound("not found")
        mock_storage_client = mock_google_cloud_storage["mock_storage_client"]
        mock_storage_client.bucket.return_value.blob.return_value = mock_blob

        with pytest.raises(RuntimeError, match="not found"):
            server_module._download_cache_db_from_gcs()

        assert not (tmp_path / ".cache.db.reload").exists()

    def test_prefers_zst_when_present(self, tmp_path, monkeypatch, mock_google_cloud_storage):
        import io

        import zstandard

        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("GCS_PREFIX", "jquants-mcp/")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        original = b"zstd-reload-db" * 2000
        compressed = zstandard.ZstdCompressor().compress(original)
        blob = mock_google_cloud_storage[
            "mock_storage_client"
        ].bucket.return_value.blob.return_value
        cm = blob.open.return_value
        cm.__enter__.return_value = io.BytesIO(compressed)
        cm.__exit__.return_value = False

        server_module._download_cache_db_from_gcs()

        assert (tmp_path / "cache.db").read_bytes() == original
        blob.download_to_filename.assert_not_called()


# ---------------------------------------------------------------------------
# _reload_cache_background
# ---------------------------------------------------------------------------


class TestReloadCacheBackground:
    async def test_without_gcs_calls_request_reload(self, mock_env, monkeypatch):
        """When GCS_BUCKET is not set, reload just calls request_reload()."""
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        cache = mock_env["cache"]

        with patch.object(cache, "request_reload") as mock_reload:
            result = await server_module._reload_cache_background()

        assert result is True
        mock_reload.assert_called_once()
        assert server_module._last_reload_at is not None
        assert server_module._reload_in_progress is False

    async def test_with_gcs_downloads_then_reloads(self, mock_env, monkeypatch):
        """When GCS_BUCKET is set, downloads cache.db then calls request_reload()."""
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        cache = mock_env["cache"]

        with (
            patch.object(server_module, "_download_cache_db_from_gcs") as mock_dl,
            patch.object(cache, "request_reload") as mock_reload,
        ):
            result = await server_module._reload_cache_background()

        assert result is True
        mock_dl.assert_called_once()
        mock_reload.assert_called_once()
        assert server_module._last_reload_at is not None

    async def test_idempotent_when_in_progress(self, mock_env, monkeypatch):
        """Second call is a no-op while first is still running."""
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        server_module._reload_in_progress = True
        cache = mock_env["cache"]

        with patch.object(cache, "request_reload") as mock_reload:
            result = await server_module._reload_cache_background()

        # A duplicate (already-in-progress) reload acks True so Pub/Sub stops.
        assert result is True
        mock_reload.assert_not_called()
        # Flag must remain True since we didn't enter the try block
        assert server_module._reload_in_progress is True

    async def test_flag_reset_on_exception(self, mock_env, monkeypatch):
        """_reload_in_progress is reset even when download raises."""
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")

        with patch.object(
            server_module,
            "_download_cache_db_from_gcs",
            side_effect=RuntimeError("GCS unavailable"),
        ):
            result = await server_module._reload_cache_background()

        # A failed download returns False so the caller can 500 → Pub/Sub retry.
        assert result is False
        assert server_module._reload_in_progress is False
        assert server_module._last_reload_at is None


# ---------------------------------------------------------------------------
# _handle_pubsub_reload (HTTP endpoint)
# ---------------------------------------------------------------------------


class TestHandlePubsubReload:
    async def test_no_sa_configured_reloads(self, mock_env, monkeypatch):
        """Without PUBSUB_INVOKER_SA, accepts any request and reloads."""
        monkeypatch.delenv("PUBSUB_INVOKER_SA", raising=False)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        req = _mock_request()
        response = await server_module._handle_pubsub_reload(req)

        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "reloaded"

    async def test_reload_runs_synchronously_before_ack(self, mock_env, monkeypatch):
        """The download must complete *before* the 200 is returned.

        Under request-based billing a detached background task would be
        CPU-starved once this handler returns, so the reload must be awaited
        inline. We assert the reload coroutine has already been awaited by the
        time the handler returns — without any event-loop draining.
        """
        monkeypatch.delenv("PUBSUB_INVOKER_SA", raising=False)

        reload_mock = AsyncMock(return_value=True)
        with patch.object(server_module, "_reload_cache_background", new=reload_mock):
            response = await server_module._handle_pubsub_reload(_mock_request())

        # No `await asyncio.sleep(0)` drain: a create_task would still be pending.
        reload_mock.assert_awaited_once()
        assert response.status_code == 200

    async def test_reload_failure_returns_500(self, mock_env, monkeypatch):
        """A failed reload returns 500 so Pub/Sub redelivers the snapshot."""
        monkeypatch.delenv("PUBSUB_INVOKER_SA", raising=False)

        with patch.object(
            server_module, "_reload_cache_background", new=AsyncMock(return_value=False)
        ):
            response = await server_module._handle_pubsub_reload(_mock_request())

        assert response.status_code == 500
        assert json.loads(response.body)["status"] == "reload failed"

    async def test_missing_bearer_returns_401(self, mock_env, monkeypatch):
        """With PUBSUB_INVOKER_SA set but no Authorization header → 401."""
        monkeypatch.setenv("PUBSUB_INVOKER_SA", "pubsub@proj.iam.gserviceaccount.com")

        req = _mock_request(auth_header=None)
        response = await server_module._handle_pubsub_reload(req)

        assert response.status_code == 401

    async def test_invalid_token_returns_403(self, mock_env, monkeypatch):
        """With PUBSUB_INVOKER_SA set and bad token → 403."""
        monkeypatch.setenv("PUBSUB_INVOKER_SA", "pubsub@proj.iam.gserviceaccount.com")
        monkeypatch.setenv("PUBSUB_AUDIENCE", "https://example.com/internal/reload")

        req = _mock_request(auth_header="Bearer bad-token")

        with patch.object(
            server_module,
            "_verify_pubsub_oidc_token",
            side_effect=ValueError("bad token"),
        ):
            response = await server_module._handle_pubsub_reload(req)

        assert response.status_code == 403

    async def test_valid_token_returns_200(self, mock_env, monkeypatch):
        """With PUBSUB_INVOKER_SA set and valid token → 200."""
        monkeypatch.setenv("PUBSUB_INVOKER_SA", "pubsub@proj.iam.gserviceaccount.com")
        monkeypatch.setenv("PUBSUB_AUDIENCE", "https://example.com/internal/reload")

        req = _mock_request(auth_header="Bearer valid-token")

        with (
            patch.object(server_module, "_verify_pubsub_oidc_token"),
            patch.object(server_module, "_reload_cache_background", new=AsyncMock()),
        ):
            response = await server_module._handle_pubsub_reload(req)

        assert response.status_code == 200

    async def test_audience_defaults_to_request_url(self, mock_env, monkeypatch):
        """Audience defaults to the request URL when PUBSUB_AUDIENCE is unset."""
        monkeypatch.setenv("PUBSUB_INVOKER_SA", "pubsub@proj.iam.gserviceaccount.com")
        monkeypatch.delenv("PUBSUB_AUDIENCE", raising=False)

        req = _mock_request(
            auth_header="Bearer tok",
            url="https://jquants-mcp.example.com/internal/reload",
        )

        captured_audience: list[str] = []

        def capture_verify(token, expected_email, audience):
            captured_audience.append(audience)

        with (
            patch.object(server_module, "_verify_pubsub_oidc_token", side_effect=capture_verify),
            patch.object(server_module, "_reload_cache_background", new=AsyncMock()),
        ):
            response = await server_module._handle_pubsub_reload(req)

        assert response.status_code == 200
        assert captured_audience == ["https://jquants-mcp.example.com/internal/reload"]


# ---------------------------------------------------------------------------
# health_check: last_reload_at field
# ---------------------------------------------------------------------------


async def _call(tool_name: str, **kwargs) -> dict:
    result = await server_module.mcp.call_tool(tool_name, kwargs)
    return json.loads(result.content[0].text)


class TestHealthCheckLastReloadAt:
    async def test_last_reload_at_none_initially(self, mock_env):
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["last_reload_at"] is None

    async def test_last_reload_at_reflects_global(self, mock_env):
        ts = time.time()
        server_module._last_reload_at = ts
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            result = await _call("health_check")
        assert result["last_reload_at"] == pytest.approx(ts)

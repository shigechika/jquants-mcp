"""Tests for scripts/gcs_sync.py.

gcs_sync.py imports google-cloud-storage lazily (inside functions) and is
not part of the jquants_mcp package; google.cloud.storage is therefore not
available in the test venv.  Tests for the early-return paths rely on the
fact that if storage.Client() were called, ModuleNotFoundError would be
raised.  Tests for the "files configured" path inject a sys.modules mock.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gcs_sync


@pytest.fixture()
def mock_google_storage(monkeypatch):
    """Inject a lightweight google.cloud.storage mock into sys.modules.

    ``from google.cloud import storage`` resolves via the ``storage``
    attribute of the ``google.cloud`` module object, not via
    ``sys.modules["google.cloud.storage"]`` directly.  We therefore set
    both so that the attribute lookup and direct-import lookup both return
    the same mock object.
    """
    mock_storage = MagicMock()
    mock_exceptions = MagicMock()
    mock_exceptions.NotFound = Exception  # make except NotFound catchable

    mock_google_cloud = MagicMock()
    mock_google_cloud.storage = mock_storage
    mock_google_cloud.exceptions = mock_exceptions

    monkeypatch.setitem(sys.modules, "google", MagicMock())
    monkeypatch.setitem(sys.modules, "google.cloud", mock_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", mock_storage)
    monkeypatch.setitem(sys.modules, "google.cloud.exceptions", mock_exceptions)
    return mock_storage


class TestUploadFilesEmpty:
    """upload_files() skips GCS client init when _UPLOAD_FILES is empty."""

    def test_returns_immediately_without_error(self, monkeypatch):
        """If early return works, ModuleNotFoundError for google is never raised."""
        monkeypatch.setattr(gcs_sync, "_UPLOAD_FILES", [])
        gcs_sync.upload_files()

    def test_calls_client_when_files_configured(self, monkeypatch, tmp_path, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(gcs_sync, "_UPLOAD_FILES", ["users.db"])
        gcs_sync.upload_files()
        mock_google_storage.Client.assert_called_once()


class TestDownloadFilesEmpty:
    """download_files() skips GCS client init when resolved file list is empty."""

    def test_returns_immediately_with_default_empty(self, monkeypatch):
        """If early return works, ModuleNotFoundError for google is never raised."""
        monkeypatch.setattr(gcs_sync, "_DOWNLOAD_FILES", [])
        gcs_sync.download_files()

    def test_returns_immediately_with_explicit_empty(self):
        """Explicit empty list triggers early return regardless of _DOWNLOAD_FILES."""
        gcs_sync.download_files([])

    def test_calls_client_when_files_configured(self, monkeypatch, tmp_path, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        gcs_sync.download_files(["cache.db"])
        mock_google_storage.Client.assert_called_once()


class TestFailureExitCode:
    """One-shot invocations surface failures as a non-zero exit code."""

    def test_upload_returns_failure_count(self, monkeypatch, tmp_path, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(gcs_sync, "_UPLOAD_FILES", ["users.db"])
        (tmp_path / "users.db").write_bytes(b"x")
        monkeypatch.setattr(gcs_sync, "_checkpoint_sqlite", lambda _p: None)
        blob = mock_google_storage.Client.return_value.bucket.return_value.blob.return_value
        blob.upload_from_filename.side_effect = RuntimeError("network down")
        assert gcs_sync.upload_files() == 1

    def test_main_upload_exits_nonzero_on_failure(self, monkeypatch, tmp_path, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(gcs_sync, "_UPLOAD_FILES", ["users.db"])
        (tmp_path / "users.db").write_bytes(b"x")
        monkeypatch.setattr(gcs_sync, "_checkpoint_sqlite", lambda _p: None)
        monkeypatch.setattr(sys, "argv", ["gcs_sync.py", "--upload"])
        blob = mock_google_storage.Client.return_value.bucket.return_value.blob.return_value
        blob.upload_from_filename.side_effect = RuntimeError("network down")
        with pytest.raises(SystemExit) as exc:
            gcs_sync.main()
        assert exc.value.code == 1

    def test_main_upload_exits_zero_on_success(self, monkeypatch, tmp_path, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(gcs_sync, "_UPLOAD_FILES", ["users.db"])
        (tmp_path / "users.db").write_bytes(b"x")
        monkeypatch.setattr(gcs_sync, "_checkpoint_sqlite", lambda _p: None)
        monkeypatch.setattr(sys, "argv", ["gcs_sync.py", "--upload"])
        # Upload succeeds (no side_effect) → main returns without SystemExit.
        gcs_sync.main()


class TestInitCacheFailureAlert:
    """--init-cache failure emits the exact phrase the Cloud Monitoring policy
    (ops/alerts/05-cache-db-download-fail.yaml) greps for, so the alert can fire.
    """

    def test_init_cache_failure_logs_alert_phrase(
        self, monkeypatch, tmp_path, mock_google_storage, caplog
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["gcs_sync.py", "--init-cache"])

        # The fixture aliases NotFound to the base Exception, which would
        # swallow any download error as a benign "first run" skip. Narrow it
        # so a genuine RuntimeError reaches the failure branch — as it does in
        # production, where NotFound is a specific subclass.
        class _NotFound(Exception):
            pass

        monkeypatch.setattr(sys.modules["google.cloud.exceptions"], "NotFound", _NotFound)

        blob = mock_google_storage.Client.return_value.bucket.return_value.blob.return_value
        blob.download_to_filename.side_effect = RuntimeError("network down")
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
            gcs_sync.main()
        assert exc.value.code == 1
        # The load-bearing assertion: the alert filter substring must be emitted.
        assert "cache.db download failed" in caplog.text

    def test_init_cache_success_does_not_log_alert_phrase(
        self, monkeypatch, tmp_path, mock_google_storage, caplog
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["gcs_sync.py", "--init-cache"])
        # download succeeds (no side_effect); rename needs a real temp file.
        blob = mock_google_storage.Client.return_value.bucket.return_value.blob.return_value
        blob.download_to_filename.side_effect = lambda p: Path(p).write_bytes(b"db")
        with caplog.at_level("ERROR"):
            gcs_sync.main()
        assert "cache.db download failed" not in caplog.text

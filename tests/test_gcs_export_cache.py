"""Tests for scripts/gcs_export_cache.py.

google-cloud-storage is imported lazily inside the script and is not in the
test venv, so we inject a sys.modules mock (same approach as test_gcs_sync).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gcs_export_cache


@pytest.fixture()
def mock_google_storage(monkeypatch):
    mock_storage = MagicMock()
    mock_google_cloud = MagicMock()
    mock_google_cloud.storage = mock_storage
    monkeypatch.setitem(sys.modules, "google", MagicMock())
    monkeypatch.setitem(sys.modules, "google.cloud", mock_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", mock_storage)
    return mock_storage


class TestUploadToGcsAtomic:
    def test_uploads_to_temp_blob_then_renames(self, monkeypatch, tmp_path, mock_google_storage):
        """Upload writes a .uploading object then server-side renames onto cache.db.

        This keeps the live cache.db intact if the upload crashes mid-way.
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.delenv("GCS_PREFIX", raising=False)

        db = tmp_path / "export.db"
        db.write_bytes(b"sqlite-bytes")

        bucket = mock_google_storage.Client.return_value.bucket.return_value
        upload_blob = MagicMock()
        bucket.blob.return_value = upload_blob

        gcs_export_cache._upload_to_gcs(db)

        # Uploaded to the temporary name, not the live name.
        bucket.blob.assert_called_once_with("jquants-mcp/cache.db.uploading")
        upload_blob.upload_from_filename.assert_called_once_with(str(db))
        # Then renamed onto the live object.
        bucket.rename_blob.assert_called_once_with(upload_blob, "jquants-mcp/cache.db")

    def test_rename_happens_after_upload(self, monkeypatch, tmp_path, mock_google_storage):
        """The rename must not run before the upload finishes."""
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.delenv("GCS_PREFIX", raising=False)

        db = tmp_path / "export.db"
        db.write_bytes(b"x")

        order: list[str] = []
        bucket = mock_google_storage.Client.return_value.bucket.return_value
        upload_blob = MagicMock()
        upload_blob.upload_from_filename.side_effect = lambda *_a, **_k: order.append("upload")
        bucket.blob.return_value = upload_blob
        bucket.rename_blob.side_effect = lambda *_a, **_k: order.append("rename")

        gcs_export_cache._upload_to_gcs(db)
        assert order == ["upload", "rename"]

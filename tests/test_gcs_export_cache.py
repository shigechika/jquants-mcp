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


class TestUploadBlobAtomic:
    def test_uploads_to_temp_blob_then_renames(self, tmp_path):
        """Upload writes a .uploading object then server-side renames onto the live name.

        This keeps the live object intact if the upload crashes mid-way.
        """
        db = tmp_path / "export.db"
        db.write_bytes(b"sqlite-bytes")

        bucket = MagicMock()
        upload_blob = MagicMock()
        bucket.blob.return_value = upload_blob

        gcs_export_cache._upload_blob_atomic(bucket, db, "jquants-mcp/cache.db")

        bucket.blob.assert_called_once_with("jquants-mcp/cache.db.uploading")
        upload_blob.upload_from_filename.assert_called_once_with(str(db))
        bucket.rename_blob.assert_called_once_with(upload_blob, "jquants-mcp/cache.db")

    def test_log_includes_full_gs_uri_with_bucket(self, tmp_path, caplog):
        """The log line must show gs://<bucket>/<object>, not just the object path."""
        db = tmp_path / "export.db"
        db.write_bytes(b"x")

        bucket = MagicMock()
        bucket.name = "my-bucket"
        bucket.blob.return_value = MagicMock()

        with caplog.at_level("INFO"):
            gcs_export_cache._upload_blob_atomic(bucket, db, "jquants-mcp/cache.db")

        assert "gs://my-bucket/jquants-mcp/cache.db" in caplog.text

    def test_rename_happens_after_upload(self, tmp_path):
        """The rename must not run before the upload finishes."""
        db = tmp_path / "export.db"
        db.write_bytes(b"x")

        order: list[str] = []
        bucket = MagicMock()
        upload_blob = MagicMock()
        upload_blob.upload_from_filename.side_effect = lambda *_a, **_k: order.append("upload")
        bucket.blob.return_value = upload_blob
        bucket.rename_blob.side_effect = lambda *_a, **_k: order.append("rename")

        gcs_export_cache._upload_blob_atomic(bucket, db, "jquants-mcp/cache.db")
        assert order == ["upload", "rename"]


class TestCompressAndUpload:
    def test_compress_to_zst_round_trip(self, tmp_path):
        import io

        import zstandard

        original = b"sqlite-db-bytes" * 5000
        db = tmp_path / "export.db"
        db.write_bytes(original)

        zst = gcs_export_cache._compress_to_zst(db)

        assert zst == Path(f"{db}.zst")
        assert zst.exists()
        out = io.BytesIO()
        zstandard.ZstdDecompressor().copy_stream(io.BytesIO(zst.read_bytes()), out)
        assert out.getvalue() == original

    def test_upload_to_gcs_uploads_zst_and_uncompressed(
        self, monkeypatch, tmp_path, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.delenv("GCS_PREFIX", raising=False)

        db = tmp_path / "export.db"
        db.write_bytes(b"sqlite-bytes" * 1000)
        bucket = mock_google_storage.Client.return_value.bucket.return_value
        bucket.blob.return_value = MagicMock()

        gcs_export_cache._upload_to_gcs(db)

        uploaded = [c.args[0] for c in bucket.blob.call_args_list]
        assert "jquants-mcp/cache.db.zst.uploading" in uploaded
        assert "jquants-mcp/cache.db.uploading" in uploaded
        renamed = [c.args[1] for c in bucket.rename_blob.call_args_list]
        assert "jquants-mcp/cache.db.zst" in renamed
        assert "jquants-mcp/cache.db" in renamed
        # The compressed temp file is cleaned up after upload.
        assert not Path(f"{db}.zst").exists()

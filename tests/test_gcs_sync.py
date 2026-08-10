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

from jquants_mcp.cache.gcs_download import stream_download_zst


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
        # No compressed object -> the .zst probe returns False (avoids feeding a
        # mock stream to zstandard) and the uncompressed download is exercised.
        blob.open.side_effect = _NotFound("no zst")
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
        # No compressed object -> fall through to the uncompressed download.
        blob.open.side_effect = sys.modules["google.cloud.exceptions"].NotFound("no zst")
        blob.download_to_filename.side_effect = lambda p: Path(p).write_bytes(b"db")
        with caplog.at_level("ERROR"):
            gcs_sync.main()
        assert "cache.db download failed" not in caplog.text


def _zst_stream_blob(payload: bytes):
    """Return a blob mock whose open('rb') streams the zstd-compressed payload."""
    import io

    import zstandard

    compressed = zstandard.ZstdCompressor().compress(payload)
    blob = MagicMock()
    cm = blob.open.return_value
    cm.__enter__.return_value = io.BytesIO(compressed)
    cm.__exit__.return_value = False
    return blob


class TestZstCacheDownload:
    """download_cache_db prefers cache.db.zst, falls back to uncompressed."""

    def test_stream_download_zst_round_trip(self, tmp_path, mock_google_storage):
        payload = b"sqlite-bytes" * 5000
        bucket = MagicMock()
        bucket.blob.return_value = _zst_stream_blob(payload)
        dest = tmp_path / ".cache.db.download"

        assert stream_download_zst(bucket, "p/cache.db.zst", dest) is True
        assert dest.read_bytes() == payload

    def test_stream_download_zst_false_when_missing(self, tmp_path, mock_google_storage):
        NotFound = sys.modules["google.cloud.exceptions"].NotFound
        bucket = MagicMock()
        bucket.blob.return_value.open.side_effect = NotFound("no zst")
        dest = tmp_path / ".cache.db.download"

        assert stream_download_zst(bucket, "p/cache.db.zst", dest) is False
        assert not dest.exists()

    def test_stream_download_zst_false_when_zstandard_missing(self, tmp_path, monkeypatch):
        # `import zstandard` raises -> the helper returns False without touching GCS.
        monkeypatch.setitem(sys.modules, "zstandard", None)
        bucket = MagicMock()

        assert stream_download_zst(bucket, "p/cache.db.zst", tmp_path / "out") is False
        bucket.blob.assert_not_called()

    def test_cache_db_prefers_zst(self, tmp_path, monkeypatch, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_google_storage.Client.return_value.bucket.return_value.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        blob.download_to_filename.assert_not_called()

    def test_cache_db_falls_back_to_uncompressed(self, tmp_path, monkeypatch, mock_google_storage):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        NotFound = sys.modules["google.cloud.exceptions"].NotFound
        blob = mock_google_storage.Client.return_value.bucket.return_value.blob.return_value
        blob.open.side_effect = NotFound("no zst")  # compressed object absent
        blob.download_to_filename.side_effect = lambda p: Path(p).write_bytes(b"raw-db")

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == b"raw-db"
        blob.download_to_filename.assert_called_once()


class TestGenerationCheck:
    """download_cache_db skips re-download when the GCS generation of the
    effective (zst-preferred, precedence-mirrored) source object is
    unchanged since the last successful download (jquants-mcp#579,
    jquants-mcp#581 review round 2).
    """

    def _configure_get_blob(self, mock_bucket, zst_gen, plain_gen):
        """Configure get_blob(name) to return a blob stub with .generation.

        A generation of None means the object is missing (get_blob's own
        None-on-missing contract).
        """

        def _get_blob(name: str):
            if name.endswith(".zst"):
                if zst_gen is None:
                    return None
                stub = MagicMock()
                stub.generation = zst_gen
                return stub
            if plain_gen is None:
                return None
            stub = MagicMock()
            stub.generation = plain_gen
            return stub

        mock_bucket.get_blob.side_effect = _get_blob

    def test_skips_download_when_generation_unchanged(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=111, plain_gen=222)

        # Local cache.db already present, and the sidecar records the same
        # effective (zst-preferred) generation that get_blob will report.
        (tmp_path / "cache.db").write_bytes(b"existing-db")
        gcs_sync._write_recorded_generation(tmp_path, "zst", 111)

        assert gcs_sync.download_cache_db() == 0
        # Content is untouched -- proves no download (and therefore no
        # atomic replace / new inode) happened.
        assert (tmp_path / "cache.db").read_bytes() == b"existing-db"
        # Neither download path (zst nor fallback) was ever attempted --
        # both go through gcs_bucket.blob(...), so this single assertion
        # covers both.
        mock_bucket.blob.assert_not_called()

    def test_download_proceeds_and_sidecar_updated_when_generation_changed(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=999, plain_gen=888)

        # Local cache.db present, but the recorded generation is stale.
        (tmp_path / "cache.db").write_bytes(b"old-db")
        gcs_sync._write_recorded_generation(tmp_path, "zst", 111)

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload

        recorded = gcs_sync._read_recorded_generation(tmp_path)
        assert recorded == ("zst", 999)
        # Also assert on the raw JSON content, not just the round-trip
        # helper, per the task's explicit instruction.
        import json

        sidecar_data = json.loads(
            (tmp_path / gcs_sync._GENERATION_SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert sidecar_data == {
            "version": gcs_sync._GENERATION_SIDECAR_VERSION,
            "source": "zst",
            "generation": 999,
        }

    def test_download_proceeds_when_generation_matches_but_local_file_missing(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=111, plain_gen=222)

        # Sidecar matches, but cache.db itself is gone (manual cleanup,
        # corruption removal, etc.) -- the safety-net case.
        gcs_sync._write_recorded_generation(tmp_path, "zst", 111)
        assert not (tmp_path / "cache.db").exists()

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        mock_bucket.blob.assert_called_once()

    def test_first_run_no_sidecar_downloads_and_creates_sidecar(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=42, plain_gen=43)

        assert not gcs_sync._generation_sidecar_path(tmp_path).exists()

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        assert gcs_sync._read_recorded_generation(tmp_path) == ("zst", 42)

    def test_malformed_sidecar_falls_through_to_normal_download(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=111, plain_gen=222)

        (tmp_path / "cache.db").write_bytes(b"existing-db")
        gcs_sync._generation_sidecar_path(tmp_path).write_text("{not valid json", encoding="utf-8")

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        # Must not raise despite the corrupt sidecar.
        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        mock_bucket.blob.assert_called_once()

    def test_both_objects_missing_returns_zero_no_download_no_sidecar(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=None, plain_gen=None)

        assert gcs_sync.download_cache_db() == 0
        # Neither download path was attempted.
        mock_bucket.blob.assert_not_called()
        # Nothing was downloaded, so nothing should be recorded either.
        assert not gcs_sync._generation_sidecar_path(tmp_path).exists()

    def test_only_zst_generation_changed_triggers_download(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """A publisher updating cache.db.zst (the effective source) must be
        detected as a change (jquants-mcp#579).
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=999, plain_gen=222)

        (tmp_path / "cache.db").write_bytes(b"old-db")
        gcs_sync._write_recorded_generation(tmp_path, "zst", 111)  # zst differs

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        assert gcs_sync._read_recorded_generation(tmp_path) == ("zst", 999)

    def test_plain_only_change_does_not_trigger_download_when_zst_is_effective_source(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """Regression for jquants-mcp#581 review round 2: an earlier design
        checked both objects' generations independently, so a publisher
        updating only the uncompressed ``cache.db`` (``cache.db.zst``
        untouched) would still be seen as "changed", trigger a download that
        -- correctly, per the zst-preferred precedence -- re-fetched the
        unchanged (stale relative to the publisher's intent) zst content,
        and then incorrectly record that as "in sync" with the new plain
        generation. Since zst remains the effective source throughout, a
        plain-only change must not trigger a download at all: verified
        against the pre-fix implementation with a hand exercise of
        download_cache_db() (zst=111 fixed, plain 222->777, recorded
        ("zst", 111)) that observed ``mock_bucket.blob.called is True`` and
        the sidecar rewritten to ("zst"-shaped: `{"zst_generation": 111,
        "plain_generation": 777}`) pre-fix, versus ``False`` / unchanged
        content and sidecar post-fix.
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        # zst unchanged (111), plain changed (222 -> 777). zst is the
        # effective source (exists, zstandard available), so plain's change
        # is irrelevant to what actually gets served.
        self._configure_get_blob(mock_bucket, zst_gen=111, plain_gen=777)

        (tmp_path / "cache.db").write_bytes(b"existing-db")
        gcs_sync._write_recorded_generation(tmp_path, "zst", 111)

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == b"existing-db"
        mock_bucket.blob.assert_not_called()
        assert gcs_sync._read_recorded_generation(tmp_path) == ("zst", 111)

    def test_pre_zst_rollout_state_skips_when_plain_generation_matches(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """Realistic pre-``.zst``-rollout production state: the publisher has
        never written ``cache.db.zst`` (``get_blob`` -> None for it on every
        poll), only the uncompressed ``cache.db`` exists and is unchanged.
        Must still skip -- plain becomes the effective source once zst is
        absent, and its generation matches what was recorded.
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=None, plain_gen=222)

        (tmp_path / "cache.db").write_bytes(b"existing-db")
        gcs_sync._write_recorded_generation(tmp_path, "plain", 222)

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == b"existing-db"
        mock_bucket.blob.assert_not_called()

    def test_warm_upgrade_local_file_exists_no_sidecar_downloads_and_creates_sidecar(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """Rollout-critical branch: a pre-#579 deployment already has
        cache.db on disk (warm instance), but the generation sidecar itself
        is new and does not exist yet. Must download once (sidecar miss
        forces it) and then create the sidecar so subsequent polls can skip.
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=42, plain_gen=43)

        (tmp_path / "cache.db").write_bytes(b"pre-existing-warm-instance-db")
        assert not gcs_sync._generation_sidecar_path(tmp_path).exists()

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        assert gcs_sync._read_recorded_generation(tmp_path) == ("zst", 42)

    def test_pre_581_two_key_sidecar_shape_is_treated_as_no_sidecar(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """A sidecar left over from the pre-review-round-2 two-key shape
        (``zst_generation``/``plain_generation``, no ``version`` key) must be
        treated exactly like "no sidecar" -- forcing one harmless
        re-download rather than misreading an incompatible format via
        ``.get("source")`` silently returning None and comparing equal to
        some unintended state.
        """
        import json

        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=111, plain_gen=222)

        (tmp_path / "cache.db").write_bytes(b"old-db")
        gcs_sync._generation_sidecar_path(tmp_path).write_text(
            json.dumps({"zst_generation": 111, "plain_generation": 222}), encoding="utf-8"
        )

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        mock_bucket.blob.assert_called_once()
        assert gcs_sync._read_recorded_generation(tmp_path) == ("zst", 111)

    def test_malformed_sidecar_non_dict_json_falls_through_to_normal_download(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """Valid JSON that is not an object (e.g. a list) hits AttributeError
        on ``.get(...)``, a distinct failure mode from JSONDecodeError that
        _read_recorded_generation also promises to swallow.
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=111, plain_gen=222)

        (tmp_path / "cache.db").write_bytes(b"existing-db")
        gcs_sync._generation_sidecar_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        mock_bucket.blob.assert_called_once()

    def test_sidecar_write_failure_does_not_crash_download(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        """A sidecar-write failure (any Exception, not just OSError -- see
        _write_recorded_generation's broad catch) must never surface as a
        download_cache_db failure: the download itself already succeeded.
        """
        import tempfile

        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        self._configure_get_blob(mock_bucket, zst_gen=42, plain_gen=43)

        def _raise_mkstemp(*_args, **_kwargs):
            raise ValueError("boom: not even an OSError")

        monkeypatch.setattr(tempfile, "mkstemp", _raise_mkstemp)

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        # The sidecar write failed, so no sidecar file should exist.
        assert not gcs_sync._generation_sidecar_path(tmp_path).exists()


class TestGenerationCheckFailsOpen:
    """A transient GCS error during the generation pre-check itself
    (_fetch_effective_generation) must not propagate out of
    download_cache_db() uncaught -- it must fall through to an
    unconditional download instead, so a metadata-only blip can never turn
    a healthy tick into a reported "download failed" (jquants-mcp#579).
    """

    def test_generation_check_error_falls_through_and_download_succeeds(
        self, tmp_path, monkeypatch, mock_google_storage
    ):
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        mock_bucket.get_blob.side_effect = RuntimeError("network down")

        payload = b"zstd-db" * 3000
        blob = _zst_stream_blob(payload)
        mock_bucket.blob.return_value = blob

        # Must not raise despite the pre-check failure.
        assert gcs_sync.download_cache_db() == 0
        assert (tmp_path / "cache.db").read_bytes() == payload
        # The generations were never actually observed (the fetch raised),
        # so nothing must be persisted -- writing a bogus value here would
        # be recording a lie about what generation was downloaded.
        assert not gcs_sync._generation_sidecar_path(tmp_path).exists()

    def test_generation_check_error_then_download_failure_still_alerts(
        self, tmp_path, monkeypatch, mock_google_storage, caplog
    ):
        """End-to-end: pre-check fails open, the subsequent real download
        also fails, and the failure must still reach --init-cache's alert
        phrase -- this is the finding's actual "alert goes dark" concern,
        verified with the failure now on the download side.
        """
        monkeypatch.setenv("GCS_BUCKET", "test-bucket")
        monkeypatch.setenv("JQUANTS_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "argv", ["gcs_sync.py", "--init-cache"])
        mock_bucket = mock_google_storage.Client.return_value.bucket.return_value
        mock_bucket.get_blob.side_effect = RuntimeError("network down")

        # The fixture aliases NotFound to the base Exception, which would
        # swallow the injected download RuntimeError as a benign "first run"
        # skip. Narrow it so the error reaches the real failure branch, as
        # in test_init_cache_failure_logs_alert_phrase above.
        class _NotFound(Exception):
            pass

        monkeypatch.setattr(sys.modules["google.cloud.exceptions"], "NotFound", _NotFound)

        blob = mock_bucket.blob.return_value
        blob.open.side_effect = _NotFound("no zst")
        blob.download_to_filename.side_effect = RuntimeError("download also down")

        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
            gcs_sync.main()
        assert exc.value.code == 1
        assert "cache.db download failed" in caplog.text

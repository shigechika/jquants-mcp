"""Tests for db/users_firestore.py.

FirestoreUserStore imports google-cloud-firestore lazily (inside __init__
and the update_* methods); the package is only installed via the
`cloud-run` extra, which the default dev/CI test environment does not
install. Tests inject a sys.modules mock for google.cloud.firestore /
google.cloud.exceptions, following the same pattern as
tests/test_gcs_sync.py.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def mock_google_cloud(monkeypatch):
    """Inject a lightweight google.cloud.firestore mock into sys.modules.

    ``from google.cloud import firestore`` resolves via the ``firestore``
    attribute of the ``google.cloud`` module object, not via
    ``sys.modules["google.cloud.firestore"]`` directly, so both are set.
    """
    mock_firestore = MagicMock()
    mock_exceptions = MagicMock()
    mock_exceptions.NotFound = Exception  # make `except NotFound` catchable

    mock_google_cloud_pkg = MagicMock()
    mock_google_cloud_pkg.firestore = mock_firestore
    mock_google_cloud_pkg.exceptions = mock_exceptions

    monkeypatch.setitem(sys.modules, "google", MagicMock())
    monkeypatch.setitem(sys.modules, "google.cloud", mock_google_cloud_pkg)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", mock_firestore)
    monkeypatch.setitem(sys.modules, "google.cloud.exceptions", mock_exceptions)
    return mock_firestore


def _make_store(mock_google_cloud):
    from jquants_mcp.db.users_firestore import FirestoreUserStore

    return FirestoreUserStore(
        project="test-project",
        encrypt_fn=lambda s: f"enc:{s}",
        decrypt_fn=lambda s: s.removeprefix("enc:"),
    )


class TestUpdateLastValidated:
    def test_noop_when_document_concurrently_deleted(self, mock_google_cloud):
        """update_last_validated must not raise when the document was
        concurrently deleted (e.g. a racing delete_api_key call) — mirrors
        the SQLite UserStore's silent no-op on UPDATE ... WHERE user_id = ?
        matching zero rows (regression for the Firestore NotFound fix)."""
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.update.side_effect = Exception("document not found")

        store.update_last_validated("user-1")  # must not raise

    def test_updates_when_document_exists(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value

        store.update_last_validated("user-1")

        doc_ref.update.assert_called_once()
        assert "last_validated_at" in doc_ref.update.call_args[0][0]


class TestUpdatePlan:
    def test_noop_when_document_concurrently_deleted(self, mock_google_cloud):
        """update_plan must not raise when the document was concurrently
        deleted (regression for the Firestore NotFound fix)."""
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.update.side_effect = Exception("document not found")

        store.update_plan("user-1", "premium")  # must not raise

    def test_updates_when_document_exists(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value

        store.update_plan("user-1", "premium")

        doc_ref.update.assert_called_once()
        call_args = doc_ref.update.call_args[0][0]
        assert call_args["plan"] == "premium"
        assert "updated_at" in call_args

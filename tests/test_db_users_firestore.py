"""Tests for db/users_firestore.py.

FirestoreUserStore imports google-cloud-firestore lazily (inside __init__
and the update_* methods); the package is only installed via the
`cloud-run` extra, which the default dev/CI test environment does not
install. Tests inject a sys.modules mock for google.cloud.firestore /
google.cloud.exceptions, following the same pattern as
tests/test_gcs_sync.py, and stub Firestore document snapshots with
MagicMock. Real encrypt/decrypt (jquants_mcp.crypto) is used so the
encryption round-trip is exercised, mirroring tests/test_db_users.py for
the SQLite-backed UserStore.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from jquants_mcp.crypto import decrypt, encrypt
from jquants_mcp.models.user import User

_PASSPHRASE = "test-encryption-key"
_ENCRYPT = lambda pt: encrypt(pt, _PASSPHRASE)  # noqa: E731
_DECRYPT = lambda blob: decrypt(blob, _PASSPHRASE)  # noqa: E731


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


def _make_store(mock_google_cloud, *, encrypt_fn=_ENCRYPT, decrypt_fn=_DECRYPT):
    from jquants_mcp.db.users_firestore import FirestoreUserStore

    return FirestoreUserStore(project="test-project", encrypt_fn=encrypt_fn, decrypt_fn=decrypt_fn)


def _snapshot(exists: bool, data: dict | None = None):
    """Build a mock Firestore DocumentSnapshot."""
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = data
    return snap


class TestGetUser:
    def test_returns_none_when_document_missing(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(False)

        assert store.get_user("u1") is None

    def test_returns_user_when_document_exists(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(
            True,
            {
                "encrypted_api_key": _ENCRYPT("secret-key"),
                "plan": "premium",
                "created_at": 100,
                "updated_at": 200,
                "last_validated_at": 300,
            },
        )

        user = store.get_user("u1")
        assert user is not None
        assert user.user_id == "u1"
        assert user.api_key == "secret-key"
        assert user.plan == "premium"
        assert user.created_at == 100
        assert user.updated_at == 200
        assert user.last_validated_at == 300

    def test_returns_none_when_decryption_fails(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {"encrypted_api_key": "not-valid-ciphertext"})

        assert store.get_user("u1") is None


class TestGetUserMeta:
    def test_returns_none_when_document_missing(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(False)

        assert store.get_user_meta("u1") is None

    def test_returns_plan_and_last_validated(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {"plan": "standard", "last_validated_at": 123})

        meta = store.get_user_meta("u1")
        assert meta is not None
        assert meta.plan == "standard"
        assert meta.last_validated_at == 123

    def test_last_validated_none_when_absent(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {"plan": "light"})

        meta = store.get_user_meta("u1")
        assert meta is not None
        assert meta.last_validated_at is None

    def test_does_not_decrypt(self, mock_google_cloud):
        """get_user_meta must not invoke the decrypt function (avoids PBKDF2),
        mirroring tests/test_db_users.py's equivalent SQLite test."""
        calls = {"decrypt": 0}

        def counting_decrypt(blob):
            calls["decrypt"] += 1
            return _DECRYPT(blob)

        store = _make_store(mock_google_cloud, decrypt_fn=counting_decrypt)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {"plan": "light", "encrypted_api_key": "x"})

        store.get_user_meta("u1")

        assert calls["decrypt"] == 0


class TestHasCorruptedKey:
    def test_false_when_document_missing(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(False)

        assert store.has_corrupted_key("u1") is False

    def test_false_when_decrypts_ok(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {"encrypted_api_key": _ENCRYPT("k")})

        assert store.has_corrupted_key("u1") is False

    def test_true_when_decrypt_fails(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {"encrypted_api_key": "corrupted"})

        assert store.has_corrupted_key("u1") is True


class TestSaveUser:
    def test_creates_new_document_when_missing(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(False)

        store.save_user(User(user_id="u1", api_key="new-key", plan="light"))

        doc_ref.set.assert_called_once()
        payload = doc_ref.set.call_args[0][0]
        assert _DECRYPT(payload["encrypted_api_key"]) == "new-key"
        assert payload["plan"] == "light"
        assert payload["last_validated_at"] is None
        doc_ref.update.assert_not_called()

    def test_updates_existing_document(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(
            True, {"encrypted_api_key": _ENCRYPT("old-key"), "plan": "free"}
        )

        store.save_user(User(user_id="u1", api_key="new-key", plan="premium"))

        doc_ref.update.assert_called_once()
        payload = doc_ref.update.call_args[0][0]
        assert _DECRYPT(payload["encrypted_api_key"]) == "new-key"
        assert payload["plan"] == "premium"
        assert "created_at" not in payload
        doc_ref.set.assert_not_called()


class TestDeleteUser:
    def test_returns_false_when_document_missing(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(False)

        assert store.delete_user("u1") is False
        doc_ref.delete.assert_not_called()

    def test_deletes_and_returns_true_when_document_exists(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_ref = store._collection.document.return_value
        doc_ref.get.return_value = _snapshot(True, {})

        assert store.delete_user("u1") is True
        doc_ref.delete.assert_called_once()


class TestListUsers:
    def test_returns_document_ids_ordered_by_created_at(self, mock_google_cloud):
        store = _make_store(mock_google_cloud)
        doc_a = MagicMock(id="a")
        doc_b = MagicMock(id="b")
        store._collection.order_by.return_value.stream.return_value = [doc_a, doc_b]

        assert store.list_users() == ["a", "b"]
        store._collection.order_by.assert_called_once_with("created_at")


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

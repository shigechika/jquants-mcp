"""Tests for the UserStore SQLite-backed user database."""

import pytest

from jquants_dat_mcp.crypto import decrypt, derive_key, encrypt
from jquants_dat_mcp.db.users import UserStore
from jquants_dat_mcp.models.user import User

_KEY = derive_key("test-encryption-key")
_ENCRYPT = lambda pt: encrypt(pt, _KEY)  # noqa: E731
_DECRYPT = lambda blob: decrypt(blob, _KEY)  # noqa: E731


@pytest.fixture
def store(tmp_path):
    """Return a fresh UserStore backed by a temporary DB file."""
    return UserStore(tmp_path / "users.db", _ENCRYPT, _DECRYPT)


def test_get_nonexistent_user(store):
    """Getting a user that does not exist returns None."""
    assert store.get_user("unknown-user") is None


def test_save_and_get_user(store):
    """Saving a user and retrieving it returns the original data."""
    user = User(user_id="gh-12345", api_key="jquants-key-abc", plan="light")
    store.save_user(user)

    result = store.get_user("gh-12345")
    assert result is not None
    assert result.user_id == "gh-12345"
    assert result.api_key == "jquants-key-abc"
    assert result.plan == "light"


def test_api_key_is_encrypted_on_disk(store, tmp_path):
    """The raw database does not contain the plain-text API key."""
    import sqlite3

    user = User(user_id="gh-99999", api_key="super-secret-key", plan="premium")
    store.save_user(user)

    conn = sqlite3.connect(tmp_path / "users.db")
    rows = conn.execute("SELECT encrypted_api_key FROM users").fetchall()
    conn.close()

    raw_values = [row[0] for row in rows]
    assert all("super-secret-key" not in v for v in raw_values), (
        "Plain-text API key found in database — encryption is not working"
    )


def test_save_user_upsert(store):
    """Saving the same user_id twice updates the existing record."""
    user1 = User(user_id="gh-1", api_key="old-key", plan="free")
    store.save_user(user1)

    user2 = User(user_id="gh-1", api_key="new-key", plan="standard")
    store.save_user(user2)

    result = store.get_user("gh-1")
    assert result.api_key == "new-key"
    assert result.plan == "standard"


def test_delete_user(store):
    """Deleting a registered user removes it from the store."""
    store.save_user(User(user_id="gh-del", api_key="key", plan="free"))
    assert store.delete_user("gh-del") is True
    assert store.get_user("gh-del") is None


def test_delete_nonexistent_user(store):
    """Deleting a user that does not exist returns False."""
    assert store.delete_user("nobody") is False


def test_list_users_empty(store):
    """An empty store returns an empty list."""
    assert store.list_users() == []


def test_list_users(store):
    """list_users returns all registered user_ids."""
    store.save_user(User(user_id="a", api_key="k1", plan="free"))
    store.save_user(User(user_id="b", api_key="k2", plan="light"))
    ids = store.list_users()
    assert set(ids) == {"a", "b"}


def test_multiple_users_independent(store):
    """Each user gets their own independently encrypted API key."""
    store.save_user(User(user_id="u1", api_key="key-for-u1", plan="free"))
    store.save_user(User(user_id="u2", api_key="key-for-u2", plan="premium"))

    u1 = store.get_user("u1")
    u2 = store.get_user("u2")
    assert u1.api_key == "key-for-u1"
    assert u2.api_key == "key-for-u2"

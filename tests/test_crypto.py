"""Tests for AES-256-GCM encryption utilities."""

import base64

import pytest

from jquants_mcp.crypto import (
    _FORMAT_V2,
    _IV_BYTES,
    _LEGACY_SALT,
    _SALT_BYTES,
    decrypt,
    derive_key,
    encrypt,
)

_PASSPHRASE = "test-secret-passphrase"


# ---------------------------------------------------------------------------
# derive_key
# ---------------------------------------------------------------------------


def test_derive_key_length():
    """derive_key returns a 32-byte key."""
    key = derive_key(_PASSPHRASE)
    assert len(key) == 32


def test_derive_key_deterministic():
    """Same passphrase + same salt always produce the same key."""
    assert derive_key(_PASSPHRASE) == derive_key(_PASSPHRASE)


def test_derive_key_bytes_input():
    """Bytes passphrase is accepted and produces the same key as str."""
    assert derive_key(_PASSPHRASE.encode()) == derive_key(_PASSPHRASE)


def test_derive_key_custom_salt():
    """Different salts produce different keys."""
    k1 = derive_key(_PASSPHRASE, salt=b"salt-one")
    k2 = derive_key(_PASSPHRASE, salt=b"salt-two")
    assert k1 != k2


# ---------------------------------------------------------------------------
# encrypt / decrypt (new interface: passphrase-based with random salt)
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    """Encrypting then decrypting returns the original plaintext."""
    plaintext = "my-j-quants-api-key-abc123"
    blob = encrypt(plaintext, _PASSPHRASE)
    assert decrypt(blob, _PASSPHRASE) == plaintext


def test_encrypt_produces_different_ciphertexts():
    """Two encrypt calls on the same plaintext produce different blobs (random salt + IV)."""
    plaintext = "same-text"
    blob1 = encrypt(plaintext, _PASSPHRASE)
    blob2 = encrypt(plaintext, _PASSPHRASE)
    assert blob1 != blob2


def test_decrypt_wrong_passphrase_raises():
    """Decrypting with the wrong passphrase raises ValueError."""
    blob = encrypt("secret", _PASSPHRASE)
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt(blob, "other-passphrase")


def test_decrypt_tampered_data_raises():
    """Decrypting tampered data raises ValueError."""
    blob = encrypt("secret", _PASSPHRASE)
    chars = list(blob)
    mid = len(chars) // 2
    chars[mid] = "A" if chars[mid] != "A" else "B"
    tampered = "".join(chars)
    with pytest.raises(ValueError):
        decrypt(tampered, _PASSPHRASE)


def test_decrypt_empty_blob_raises():
    """Decrypting an empty string raises ValueError."""
    with pytest.raises((ValueError, Exception)):
        decrypt("", _PASSPHRASE)


def test_encrypt_empty_string():
    """Empty string can be encrypted and decrypted."""
    blob = encrypt("", _PASSPHRASE)
    assert decrypt(blob, _PASSPHRASE) == ""


def test_encrypt_unicode():
    """Unicode strings are handled correctly."""
    plaintext = "日本語テスト-αβγ-🔑"
    blob = encrypt(plaintext, _PASSPHRASE)
    assert decrypt(blob, _PASSPHRASE) == plaintext


def test_new_blob_starts_with_format_v2():
    """New blobs start with FORMAT_V2 version marker after base64-decoding."""
    blob = encrypt("test", _PASSPHRASE)
    raw = base64.urlsafe_b64decode(blob.encode() + b"==")
    assert raw[:1] == _FORMAT_V2


def test_new_blob_contains_random_salt():
    """New blobs contain a 16-byte random salt after the version marker."""
    blob = encrypt("test", _PASSPHRASE)
    raw = base64.urlsafe_b64decode(blob.encode() + b"==")
    salt1 = raw[1 : 1 + _SALT_BYTES]

    blob2 = encrypt("test", _PASSPHRASE)
    raw2 = base64.urlsafe_b64decode(blob2.encode() + b"==")
    salt2 = raw2[1 : 1 + _SALT_BYTES]

    assert salt1 != salt2, "Salt must be random per encryption"


# ---------------------------------------------------------------------------
# Backward compatibility: decrypt legacy blobs (fixed salt, IV-first format)
# ---------------------------------------------------------------------------


def _make_legacy_blob(plaintext: str, passphrase: str) -> str:
    """Create a blob in the old format: base64url(IV(12) || CT+TAG) with fixed salt."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key(passphrase, salt=_LEGACY_SALT)
    iv = os.urandom(_IV_BYTES)
    ct = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return base64.urlsafe_b64encode(iv + ct).decode()


def test_decrypt_legacy_blob():
    """Old-format blobs (fixed salt, no version marker) are still decryptable."""
    legacy_blob = _make_legacy_blob("old-api-key", _PASSPHRASE)
    assert decrypt(legacy_blob, _PASSPHRASE) == "old-api-key"


def test_decrypt_legacy_wrong_passphrase_raises():
    """Wrong passphrase for a legacy blob raises ValueError."""
    legacy_blob = _make_legacy_blob("secret", _PASSPHRASE)
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt(legacy_blob, "wrong-passphrase")


# ---------------------------------------------------------------------------
# decrypt_with_fallback (dual-key rotation window)
# ---------------------------------------------------------------------------


def test_fallback_primary_key_wins():
    from jquants_mcp.crypto import decrypt_with_fallback

    blob = encrypt("secret", "new-key")
    assert decrypt_with_fallback(blob, ["new-key", "old-key"]) == "secret"


def test_fallback_uses_previous_key():
    from jquants_mcp.crypto import decrypt_with_fallback

    blob = encrypt("secret", "old-key")
    assert decrypt_with_fallback(blob, ["new-key", "old-key"]) == "secret"


def test_fallback_all_keys_fail_raises():
    from jquants_mcp.crypto import decrypt_with_fallback

    blob = encrypt("secret", "real-key")
    with pytest.raises(ValueError, match="Decryption failed with all"):
        decrypt_with_fallback(blob, ["wrong1", "wrong2"])


def test_fallback_empty_list_raises():
    from jquants_mcp.crypto import decrypt_with_fallback

    with pytest.raises(ValueError, match="must not be empty"):
        decrypt_with_fallback("whatever", [])

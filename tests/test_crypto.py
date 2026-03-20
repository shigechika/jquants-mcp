"""Tests for AES-256-GCM encryption utilities."""

import pytest

from jquants_dat_mcp.crypto import decrypt, derive_key, encrypt

_PASSPHRASE = "test-secret-passphrase"
_KEY = derive_key(_PASSPHRASE)


def test_derive_key_length():
    """derive_key returns a 32-byte key."""
    assert len(_KEY) == 32


def test_derive_key_deterministic():
    """Same passphrase always produces the same key."""
    assert derive_key(_PASSPHRASE) == derive_key(_PASSPHRASE)


def test_derive_key_bytes_input():
    """Bytes passphrase is accepted and produces the same key as str."""
    assert derive_key(_PASSPHRASE.encode()) == _KEY


def test_encrypt_decrypt_roundtrip():
    """Encrypting then decrypting returns the original plaintext."""
    plaintext = "my-j-quants-api-key-abc123"
    blob = encrypt(plaintext, _KEY)
    assert decrypt(blob, _KEY) == plaintext


def test_encrypt_produces_different_ciphertexts():
    """Two encrypt calls on the same plaintext produce different blobs (random IV)."""
    plaintext = "same-text"
    blob1 = encrypt(plaintext, _KEY)
    blob2 = encrypt(plaintext, _KEY)
    assert blob1 != blob2


def test_decrypt_wrong_key_raises():
    """Decrypting with the wrong key raises ValueError."""
    blob = encrypt("secret", _KEY)
    wrong_key = derive_key("other-passphrase")
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt(blob, wrong_key)


def test_decrypt_tampered_data_raises():
    """Decrypting tampered data raises ValueError."""
    blob = encrypt("secret", _KEY)
    # Flip a character in the middle of the blob
    chars = list(blob)
    mid = len(chars) // 2
    chars[mid] = "A" if chars[mid] != "A" else "B"
    tampered = "".join(chars)
    with pytest.raises(ValueError):
        decrypt(tampered, _KEY)


def test_decrypt_empty_blob_raises():
    """Decrypting an empty string raises ValueError."""
    with pytest.raises((ValueError, Exception)):
        decrypt("", _KEY)


def test_encrypt_empty_string():
    """Empty string can be encrypted and decrypted."""
    blob = encrypt("", _KEY)
    assert decrypt(blob, _KEY) == ""


def test_encrypt_unicode():
    """Unicode strings are handled correctly."""
    plaintext = "日本語テスト-αβγ-🔑"
    blob = encrypt(plaintext, _KEY)
    assert decrypt(blob, _KEY) == plaintext

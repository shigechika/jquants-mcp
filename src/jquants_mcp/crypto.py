"""AES-256-GCM encryption utilities for storing sensitive values at rest."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

_SALT_BYTES = 16  # 128-bit random salt, generated per encryption
_IV_BYTES = 12  # 96-bit IV for AES-GCM
_ITERATIONS = 200_000

# Version marker for the new blob format (1 byte prepended before salt+iv+ct)
_FORMAT_V2 = b"\x02"

# Legacy fixed salt — kept only to decrypt old blobs created before this change
_LEGACY_SALT = b"jquants-dat-mcp-v1"


def _pbkdf2(passphrase: bytes, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from passphrase + salt using PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_ITERATIONS,
    )
    return kdf.derive(passphrase)


def derive_key(passphrase: str | bytes, salt: bytes = _LEGACY_SALT) -> bytes:
    """Derive a 256-bit AES key from an arbitrary passphrase using PBKDF2-HMAC-SHA256.

    Kept for backward compatibility and direct testing of key derivation.
    Production code should use encrypt()/decrypt() which handle salt internally.

    Args:
        passphrase: A secret string or bytes used as the master key material.
        salt: KDF salt bytes. Defaults to the legacy fixed salt.

    Returns:
        32-byte AES-256 key.
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    return _pbkdf2(passphrase, salt)


def encrypt(plaintext: str, passphrase: str | bytes) -> str:
    """Encrypt a plaintext string with AES-256-GCM using a random per-message salt.

    Output blob format: base64url( FORMAT_V2(1) || SALT(16) || IV(12) || CT+TAG )

    Each call generates a fresh random salt, so two encryptions of the same
    plaintext always produce distinct ciphertext blobs.

    Args:
        plaintext: The string to encrypt.
        passphrase: Secret passphrase from which the encryption key is derived.

    Returns:
        Base64url-encoded ciphertext blob.
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    salt = os.urandom(_SALT_BYTES)
    iv = os.urandom(_IV_BYTES)
    key = _pbkdf2(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return base64.urlsafe_b64encode(_FORMAT_V2 + salt + iv + ciphertext).decode()


def decrypt(blob: str, passphrase: str | bytes) -> str:
    """Decrypt an AES-256-GCM ciphertext blob produced by encrypt().

    Supports both the current format (random salt, FORMAT_V2 marker) and the
    legacy format (fixed salt, no version marker) for backward compatibility
    with blobs created before the random-salt change.

    Detection logic:
    - New blob: first raw byte == FORMAT_V2 (0x02) AND length >= 45 bytes
    - Legacy blob: anything else → IV is the first 12 bytes, key from legacy salt

    In the rare case (~1/256) that a legacy blob's first byte happens to equal
    FORMAT_V2, the new-format attempt is tried first; if it fails (wrong GCM
    tag), the legacy path is used as a fallback.

    Args:
        blob: Base64url-encoded ciphertext blob.
        passphrase: Secret passphrase used during encryption.

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If decryption fails (wrong passphrase, tampered data, bad blob).
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    try:
        raw = base64.urlsafe_b64decode(blob.encode() + b"==")

        # Try the new format: FORMAT_V2(1) || SALT(16) || IV(12) || CT+TAG (min 45 bytes)
        min_v2_len = 1 + _SALT_BYTES + _IV_BYTES + 16  # 16 = min size of GCM tag + CT
        if raw[:1] == _FORMAT_V2 and len(raw) >= min_v2_len:
            salt = raw[1 : 1 + _SALT_BYTES]
            iv = raw[1 + _SALT_BYTES : 1 + _SALT_BYTES + _IV_BYTES]
            ct = raw[1 + _SALT_BYTES + _IV_BYTES :]
            try:
                return AESGCM(_pbkdf2(passphrase, salt)).decrypt(iv, ct, None).decode()
            except Exception:
                # Rare version-byte collision with a legacy blob — fall through to legacy path
                pass

        # Legacy format: IV(12) || CT+TAG (key derived from the fixed salt)
        iv = raw[:_IV_BYTES]
        ct = raw[_IV_BYTES:]
        return AESGCM(_pbkdf2(passphrase, _LEGACY_SALT)).decrypt(iv, ct, None).decode()

    except Exception as exc:
        raise ValueError("Decryption failed — wrong key or corrupted data") from exc


def decrypt_with_fallback(blob: str, passphrases: list[str]) -> str:
    """Try each passphrase in order until one decrypts successfully.

    Used during a key rotation window where some ciphertexts were created
    with the previous passphrase and others with the new one. Pass the
    primary (new) key first for efficiency.

    Args:
        blob: Base64url-encoded ciphertext blob.
        passphrases: Non-empty list of candidate passphrases.

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: When no passphrase decrypts the blob.
    """
    if not passphrases:
        raise ValueError("passphrases list must not be empty")
    last_exc: Exception | None = None
    for pw in passphrases:
        try:
            return decrypt(blob, pw)
        except ValueError as exc:
            last_exc = exc
    raise ValueError("Decryption failed with all provided keys") from last_exc

"""AES-256-GCM encryption utilities for storing sensitive values at rest."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Fixed salt for key derivation — not secret, but must be stable across restarts
_KDF_SALT = b"jquants-dat-mcp-v1"
_ITERATIONS = 200_000
_IV_BYTES = 12  # 96-bit IV for AES-GCM


def derive_key(passphrase: str | bytes) -> bytes:
    """Derive a 256-bit AES key from an arbitrary passphrase using PBKDF2-HMAC-SHA256.

    Args:
        passphrase: A secret string or bytes used as the master key material.

    Returns:
        32-byte AES-256 key.
    """
    if isinstance(passphrase, str):
        passphrase = passphrase.encode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=_ITERATIONS,
    )
    return kdf.derive(passphrase)


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext string with AES-256-GCM.

    The output is a URL-safe base64-encoded blob: IV (12 bytes) || ciphertext+tag.

    Args:
        plaintext: The string to encrypt.
        key: 32-byte AES-256 key (from derive_key).

    Returns:
        Base64url-encoded ciphertext blob.
    """
    iv = os.urandom(_IV_BYTES)
    aes = AESGCM(key)
    ciphertext = aes.encrypt(iv, plaintext.encode(), None)
    return base64.urlsafe_b64encode(iv + ciphertext).decode()


def decrypt(blob: str, key: bytes) -> str:
    """Decrypt an AES-256-GCM ciphertext blob produced by encrypt().

    Args:
        blob: Base64url-encoded ciphertext blob.
        key: 32-byte AES-256 key (from derive_key).

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If decryption fails (wrong key, tampered data).
    """
    try:
        raw = base64.urlsafe_b64decode(blob.encode())
        iv, ciphertext = raw[:_IV_BYTES], raw[_IV_BYTES:]
        aes = AESGCM(key)
        return aes.decrypt(iv, ciphertext, None).decode()
    except Exception as exc:
        raise ValueError("Decryption failed — wrong key or corrupted data") from exc

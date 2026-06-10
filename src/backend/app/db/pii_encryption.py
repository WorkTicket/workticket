"""Field-level encryption for PII (GDPR/CCPA compliance).

Delegates to app.security.encryption for the unified AES-256-GCM implementation.
Provides a PiiEncryptor class with backward-compatibility fallback for data
encrypted with the legacy v1 salt (b"workticket-pii-encryption-v1").

Encrypted format: "v1:<base64_nonce>:<base64_ciphertext>"

Usage:
    from app.db.pii_encryption import encrypt_pii, decrypt_pii

    encrypted = encrypt_pii("user@example.com")
    decrypted = decrypt_pii(encrypted)
"""

import logging
import os

from app.security.encryption import (
    decrypt,
    derive_key,
    encrypt,
    get_or_derive_key,
)

logger = logging.getLogger(__name__)

_V1_SALT = b"workticket-pii-encryption-v1"


class PiiEncryptor:
    """Encryptor for PII fields with v1→v2 salt migration support.

    Reads PII_ENCRYPTION_KEY from the environment and derives keys using
    PBKDF2. Tries the current (v2) salt first for decryption, then falls
    back to the legacy v1 salt so that data encrypted before the
    consolidation can still be read transparently.
    """

    def __init__(self):
        self._key: bytes | None = None
        self._v1_key: bytes | None = None

    def _get_key(self) -> bytes:
        if self._key is None:
            try:
                self._key = get_or_derive_key()
            except ValueError:
                raise
        return self._key

    def _get_v1_key(self) -> bytes:
        """Legacy key derived with the v1 salt for reading old data."""
        if self._v1_key is None:
            raw = os.environ.get("PII_ENCRYPTION_KEY", "")
            if raw:
                self._v1_key = derive_key(raw, salt=_V1_SALT)
            else:
                self._v1_key = b""
        return self._v1_key

    def encrypt(self, plaintext: str) -> str:
        key = self._get_key()
        if not key:
            return plaintext
        return encrypt(plaintext, key)

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ciphertext

        key = self._get_key()
        if key:
            result = decrypt(ciphertext, key)
            if result != ciphertext:
                return result

        v1_key = self._get_v1_key()
        if v1_key:
            result = decrypt(ciphertext, v1_key)
            if result != ciphertext:
                return result

        return ciphertext


_default_encryptor = PiiEncryptor()


def encrypt_pii(plaintext: str) -> str:
    """Encrypt a PII value using the unified encryption module.

    Returns the plaintext unchanged if encryption is unavailable (debug mode).
    """
    return _default_encryptor.encrypt(plaintext)


def decrypt_pii(encrypted: str) -> str:
    """Decrypt a PII value. Falls back to legacy v1 salt if needed.

    Returns the original value unchanged if it is not encrypted or decryption fails.
    """
    return _default_encryptor.decrypt(encrypted)


def is_encrypted(value: str) -> bool:
    """Check if a value appears to be PII-encrypted."""
    from app.security.encryption import is_encrypted as _is_encrypted

    return _is_encrypted(value)

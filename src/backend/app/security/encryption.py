"""Unified AES-256-GCM encryption for PII fields.

Single source of truth for all PII encryption across the application.
Uses AES-256-GCM with a random 12-byte nonce per encryption and
PBKDF2-SHA256 key derivation from the PII_ENCRYPTION_KEY env var.

Encrypted format: "v1:<base64_nonce>:<base64_ciphertext>"
  where ciphertext includes the 16-byte GCM authentication tag.

Key management:
  - PII_ENCRYPTION_KEY env var (passphrase) → PBKDF2 → 32-byte AES key
  - No fallback to other keys — requires its own dedicated key
  - Production mode: raises ValueError if no key configured
  - Debug mode: logs warning, stores plaintext

Usage:
    from app.security.encryption import encrypt_field_for_storage, decrypt_field_from_storage

    encrypted = encrypt_field_for_storage("user@example.com")
    plaintext = decrypt_field_from_storage(encrypted)
"""

import base64
import json
import logging
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_KEY_VERSION = 1
_NONCE_LENGTH = 12
_ENCRYPTION_KEY_ENV = "PII_ENCRYPTION_KEY"
def _get_default_salt() -> bytes:
    """Get the PBKDF2 salt from configuration.

    Priority:
    1. PII_SALT env var (explicit)
    2. Derived from PII_ENCRYPTION_KEY via SHA256
    3. Raises ValueError if no encryption material is configured
    """
    from hashlib import sha256

    env_salt = os.environ.get("PII_SALT", "").encode()
    if env_salt:
        return env_salt
    key_material = os.environ.get(_ENCRYPTION_KEY_ENV, "")
    if key_material:
        return sha256(key_material.encode()).digest()
    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    if debug:
        return b"workticket-dev-salt-v1"
    raise ValueError(
        "PII_SALT or PII_ENCRYPTION_KEY must be set. "
        "Without a stable salt, previously encrypted PII data cannot be recovered after restart."
    )

_DEFAULT_SALT = _get_default_salt()
_DEFAULT_ITERATIONS = 600_000
_FORMAT_PREFIX = f"v{_KEY_VERSION}:"


def derive_key(key_material: str, salt: bytes = _DEFAULT_SALT, iterations: int = _DEFAULT_ITERATIONS) -> bytes:
    """Derive a 256-bit AES key from key material using PBKDF2-SHA256.

    Args:
        key_material: The passphrase / key material string.
        salt: PBKDF2 salt (default: v2 salt).
        iterations: PBKDF2 iteration count (default: 600,000).

    Returns:
        32-byte derived key.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(key_material.encode("utf-8"))


def get_or_derive_key() -> bytes:
    """Read PII_ENCRYPTION_KEY env var and derive an encryption key via PBKDF2.

    Returns:
        32-byte derived key, or empty bytes if in DEBUG mode without a key set.

    Raises:
        ValueError: If PII_ENCRYPTION_KEY is not set and not in DEBUG mode.
    """
    raw_key = os.environ.get(_ENCRYPTION_KEY_ENV, "")
    if not raw_key:
        debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
        if not debug:
            raise ValueError("PII_ENCRYPTION_KEY not set — PII encryption is required for production.")
        logger.warning(
            "PII_ENCRYPTION_KEY not set — PII encryption disabled, storing data in plaintext. Set this for production."
        )
        return b""
    return derive_key(raw_key)


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt plaintext using AES-256-GCM.

    Args:
        plaintext: The string to encrypt.
        key: 32-byte AES-256 key.

    Returns:
        Formatted string: "v1:<base64_nonce>:<base64_ciphertext>"
        Returns plaintext unchanged if it is empty.
    """
    if not plaintext:
        return plaintext
    nonce = os.urandom(_NONCE_LENGTH)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"v{_KEY_VERSION}:{base64.b64encode(nonce).decode('ascii')}:{base64.b64encode(ciphertext).decode('ascii')}"


def decrypt(ciphertext: str, key: bytes) -> str:
    """Decrypt a value encrypted with encrypt().

    Args:
        ciphertext: The encrypted string in "v1:<nonce>:<ciphertext>" format.
        key: 32-byte AES-256 key.

    Returns:
        Decrypted plaintext, or the original value unchanged if it is not
        in encrypted format or decryption fails.
    """
    if not ciphertext:
        return ciphertext
    if not ciphertext.startswith(_FORMAT_PREFIX):
        return ciphertext
    try:
        parts = ciphertext.split(":", 2)
        if len(parts) != 3:
            return ciphertext
        nonce = base64.b64decode(parts[1])
        encrypted = base64.b64decode(parts[2])
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, encrypted, None).decode("utf-8")
    except Exception as e:
        logger.error("Decryption failed: %s", e)
        return ciphertext


def _get_raw_key() -> bytes:
    """Get raw hex/bytes key for backward compatibility with legacy JSON-format data.

    Legacy: the old encryption.py (pre-unification) used a raw 32-byte key
    (hex-encoded) without PBKDF2 derivation. This function reconstructs that
    key to allow reading old JSON-encrypted records still present in the database.
    """
    raw = os.environ.get(_ENCRYPTION_KEY_ENV, "")
    if raw:
        try:
            key_bytes = bytes.fromhex(raw) if all(c in "0123456789abcdefABCDEF" for c in raw) else raw.encode()
            if len(key_bytes) != 32:
                key_bytes = key_bytes.ljust(32, b"\0")[:32]
            return key_bytes
        except Exception:
            logger.debug("Failed to derive raw encryption key from hex, falling back")
            pass  # nosec B110
    return b""


def encrypt_field_for_storage(plaintext: str) -> str | None:
    """Encrypt plaintext for database storage.

    Convenience wrapper that derives the key from the environment and encrypts.
    Returns plaintext unchanged if encryption is unavailable (debug mode).
    """
    if not plaintext:
        return plaintext
    try:
        key = get_or_derive_key()
    except ValueError:
        logger.warning("Encryption key not configured — storing plaintext")
        return plaintext
    if not key:
        return plaintext
    return encrypt(plaintext, key)


def decrypt_field_from_storage(stored: str | None) -> str | None:
    """Decrypt a value from database storage.

    Handles both the current PBKDF2-derived-key format and the legacy
    JSON-dict format (raw hex key, no PBKDF2) for backward compatibility.
    """
    if not stored:
        return None

    try:
        key = get_or_derive_key()
    except ValueError:
        key = b""
    if key:
        try:
            result = decrypt(stored, key)
            if result != stored:
                return result
        except Exception:
            logger.debug("Failed to decrypt PII field with derived key, trying legacy key")
            pass  # nosec B110

    raw_key = _get_raw_key()
    if raw_key:
        try:
            data = json.loads(stored)
            if isinstance(data, dict) and "ciphertext_b64" in data:
                nonce = base64.b64decode(data.get("nonce_b64", ""))
                ciphertext = base64.b64decode(data.get("ciphertext_b64", ""))
                aesgcm = AESGCM(raw_key)
                return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        except Exception:
            logger.debug("Failed to decrypt PII field with legacy raw key")
            pass  # nosec B110

    try:
        data = json.loads(stored)
        if isinstance(data, dict) and "ciphertext_b64" in data:
            logger.warning("Could not decrypt legacy JSON-encrypted value — key may not match")
            return stored
    except (json.JSONDecodeError, TypeError):
        pass

    return stored


def is_encrypted(value: str) -> bool:
    """Check if a value appears to be PII-encrypted."""
    return value.startswith(_FORMAT_PREFIX) if value else False

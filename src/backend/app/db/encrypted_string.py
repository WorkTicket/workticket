"""EncryptedString SQLAlchemy type for PII encryption at rest.

Provides transparent AES-256-GCM encryption for sensitive database columns.
Values are encrypted before write and decrypted after read automatically.

Delegates to app.security.encryption for all cryptographic operations
(the single source of truth for PII encryption).

v1→v2 migration: Data encrypted with the old v1 salt
(b"workticket-pii-encryption-v1") is no longer produced by this module.
If old v1-salt data needs to be read, use app.db.pii_encryption.PiiEncryptor
which includes a legacy fallback path.

Encrypted format: "v1:<base64_nonce>:<base64_ciphertext>"

Usage:
    from app.db.encrypted_string import EncryptedString
    from app.jobs.models import User

    class User(Base):
        email_encrypted = Column(EncryptedString(255), nullable=True)
"""

import logging
import threading
from typing import ClassVar

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.security.encryption import decrypt, encrypt, get_or_derive_key

logger = logging.getLogger(__name__)

_KEY_VERSION = 1
_ENCRYPTED_PREFIX = f"v{_KEY_VERSION}:"


def _encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    try:
        key = get_or_derive_key()
    except ValueError:
        raise
    if not key:
        logger.warning("PII_ENCRYPTION_KEY not set — EncryptedString will store plaintext (DEBUG mode)")
        return plaintext
    return encrypt(plaintext, key)


def _decrypt_value(encrypted: str) -> str:
    if not encrypted:
        return encrypted
    if not encrypted.startswith(_ENCRYPTED_PREFIX):
        return encrypted
    try:
        key = get_or_derive_key()
    except ValueError:
        return encrypted
    if not key:
        return encrypted
    return decrypt(encrypted, key)


def is_encrypted(value: str) -> bool:
    return value.startswith(_ENCRYPTED_PREFIX) if value else False


class EncryptedString(TypeDecorator):
    """SQLAlchemy type that encrypts values at rest using AES-256-GCM.

    Stores encrypted values as TEXT with a version prefix.
    Falls back to plaintext storage when PII_ENCRYPTION_KEY is not configured.
    """

    impl = String
    cache_ok = True

    def __init__(self, length=None, **kwargs):
        super().__init__(length, **kwargs)

    def process_bind_param(self, value, dialect: Dialect):
        if value is None:
            return None
        if isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIX):
            return value
        return _encrypt_value(str(value)) if value else value

    def process_result_value(self, value, dialect: Dialect):
        if value is None:
            return None
        if not isinstance(value, str) or not value.startswith(_ENCRYPTED_PREFIX):
            return value
        return _decrypt_value(value)

    def copy(self, **kw):
        return EncryptedString(self.impl.length)


class PiiAccessAudit:
    """Tracks PII access for compliance audit logging."""

    _access_log: ClassVar[list[dict]] = []
    _max_entries = 10000
    _lock = threading.Lock()

    @classmethod
    def log_access(cls, user_id: str, company_id: str, field: str, table: str, reason: str = "read"):
        entry = {
            "user_id": user_id,
            "company_id": company_id,
            "field": field,
            "table": table,
            "reason": reason,
            "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }
        with cls._lock:
            cls._access_log.append(entry)
            if len(cls._access_log) > cls._max_entries:
                cls._access_log = cls._access_log[-cls._max_entries :]
        logger.debug("PII access: user=%s company=%s field=%s.%s reason=%s", user_id, company_id, table, field, reason)

    @classmethod
    def get_recent(cls, limit: int = 100) -> list[dict]:
        with cls._lock:
            return cls._access_log[-limit:]

import base64
import hashlib
import logging

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    from app.config import get_settings

    settings = get_settings()
    raw_key = settings.push_token_encryption_key
    if not raw_key or raw_key == "__REQUIRED__":
        if settings.debug:
            raw_key = "workticket-dev-push-token-key-not-for-production"
        else:
            raise ValueError(
                "push_token_encryption_key is not set. "
                "A dedicated push token encryption key is required for production. "
                "Do not reuse other cryptographic keys."
            )
    key_bytes = hashlib.sha256(raw_key.encode()).digest()
    encoded_key = base64.urlsafe_b64encode(key_bytes)
    try:
        from cryptography.fernet import Fernet

        _fernet = Fernet(encoded_key)
    except Exception as e:
        logger.error("Failed to initialize Fernet for push token encryption: %s", e)
        return None
    return _fernet


def encrypt_push_token(plaintext: str) -> str:
    f = _get_fernet()
    if f is None:
        return plaintext
    try:
        return f.encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error("Push token encryption failed: %s", e)
        return plaintext


def decrypt_push_token(ciphertext: str) -> str | None:
    f = _get_fernet()
    if f is None:
        logger.warning("Cannot decrypt push token: encryption not configured")
        return None
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        logger.warning("Push token decryption failed — token may have been encrypted with a different key")
        return None

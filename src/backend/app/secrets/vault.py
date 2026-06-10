"""HashiCorp Vault secrets manager integration.

Provides secure secret retrieval from Vault KV v2 engine, with automatic
token renewal and caching. Falls back to environment variables if Vault
is not configured.

S2 FIX: Enforces Vault usage in production via startup validation.
Secrets marked as VAULT_ENFORCED will refuse to fall back to env vars.

Configuration via environment:
    VAULT_ADDR: Vault server URL (e.g., https://vault.example.com:8200)
    VAULT_TOKEN: Vault authentication token
    VAULT_KV_PATH: KV v2 mount path (default: secret)
    VAULT_KV_PREFIX: Key prefix within the mount (default: workticket)
    VAULT_ENABLED: Set to 'true' to enable Vault
    VAULT_ENFORCED_SECRETS: Comma-separated list of keys that MUST come from Vault

Usage:
    from app.secrets.vault import get_secret

    stripe_key = await get_secret("stripe_secret_key")
    db_password = await get_secret("database_password")
"""

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_VAULT_ADDR = os.environ.get("VAULT_ADDR", "").rstrip("/")
_VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "")
_VAULT_KV_PATH = os.environ.get("VAULT_KV_PATH", "secret")
_VAULT_KV_PREFIX = os.environ.get("VAULT_KV_PREFIX", "workticket")
_VAULT_ENABLED = os.environ.get("VAULT_ENABLED", "").lower() in ("true", "1", "yes")
_VAULT_ENFORCED_SECRETS = {s.strip() for s in os.environ.get("VAULT_ENFORCED_SECRETS", "").split(",") if s.strip()}
_V_DEBUG = os.environ.get("DEBUG", "").lower() in ("true", "1", "yes")

# Cache: {secret_key: (value, expires_at)}
_secret_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 300  # 5 minutes
_cache_lock = asyncio.Lock()

_client: httpx.AsyncClient | None = None
_token_expires_at: float = 0.0
_token_renew_lock = asyncio.Lock()


def is_vault_available() -> bool:
    return _VAULT_ENABLED and bool(_VAULT_ADDR and _VAULT_TOKEN)


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_VAULT_ADDR,
            headers={"X-Vault-Token": _VAULT_TOKEN},
            timeout=httpx.Timeout(10.0),
        )
    return _client


async def _renew_token() -> None:
    """Renew the Vault token if it's close to expiration."""
    global _token_expires_at

    async with _token_renew_lock:
        if time.time() < _token_expires_at - 60:
            return

        try:
            client = await _get_client()
            response = await client.post("/v1/auth/token/renew-self")
            if response.status_code == 200:
                data = response.json()
                lease_duration = data.get("auth", {}).get("lease_duration", 3600)
                _token_expires_at = time.time() + lease_duration
                logger.debug("Vault token renewed, TTL: %ds", lease_duration)
            else:
                logger.warning("Vault token renewal failed: %s", response.status_code)
        except Exception as e:
            logger.debug("Vault token renewal error: %s", e)


async def get_secret(secret_key: str) -> str | None:
    """Retrieve a secret from Vault KV v2, with caching.

    Falls back to environment variable if Vault is unavailable.

    Args:
        secret_key: The key name within the configured KV prefix

    Returns:
        Secret value as string, or None if not found
    """
    # Check cache first
    async with _cache_lock:
        if secret_key in _secret_cache:
            value, expires_at = _secret_cache[secret_key]
            if time.time() < expires_at:
                return value
            del _secret_cache[secret_key]

    if not is_vault_available():
        return os.environ.get(secret_key.upper(), None)

    try:
        await _renew_token()
        client = await _get_client()

        # KV v2 path: /v1/<mount>/data/<prefix>/<key>
        path = f"/v1/{_VAULT_KV_PATH}/data/{_VAULT_KV_PREFIX}/{secret_key}"
        response = await client.get(path)

        if response.status_code == 200:
            data = response.json()
            value = data.get("data", {}).get("data", {}).get(secret_key)
            if value:
                async with _cache_lock:
                    _secret_cache[secret_key] = (str(value), time.time() + _CACHE_TTL)
                return str(value)

        if response.status_code == 404:
            # S2 FIX: Enforced secrets must exist in Vault
            if secret_key in _VAULT_ENFORCED_SECRETS:
                logger.error(
                    "VAULT_ENFORCED secret '%s' not found in Vault — refusing to start",
                    secret_key,
                )
                raise RuntimeError(
                    f"Vault-enforced secret '{secret_key}' not found in Vault. "
                    f"Add it to Vault or remove from VAULT_ENFORCED_SECRETS."
                )
            logger.debug("Secret '%s' not found in Vault", secret_key)
            return os.environ.get(secret_key.upper(), None)

        logger.warning("Vault returned %d for secret '%s'", response.status_code, secret_key)

    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Vault secret retrieval failed for '%s': %s", secret_key, e)

    # Fallback to env var (only for non-enforced secrets)
    if secret_key in _VAULT_ENFORCED_SECRETS:
        logger.error(
            "VAULT_ENFORCED secret '%s' could not be retrieved from Vault and no env var fallback allowed",
            secret_key,
        )
        raise RuntimeError(
            f"Vault-enforced secret '{secret_key}' is unavailable. "
            f"Check Vault connectivity or remove from VAULT_ENFORCED_SECRETS."
        )
    return os.environ.get(secret_key.upper(), None)


async def get_secrets_batch(secret_keys: list[str]) -> dict[str, str | None]:
    """Retrieve multiple secrets efficiently.

    Args:
        secret_keys: List of secret key names

    Returns:
        Dict mapping key names to values
    """
    if not is_vault_available():
        return {k: os.environ.get(k.upper()) for k in secret_keys}

    # Try batch retrieval first
    try:
        await _renew_token()
        client = await _get_client()

        path = f"/v1/{_VAULT_KV_PATH}/data/{_VAULT_KV_PREFIX}"
        response = await client.get(path)

        if response.status_code == 200:
            data = response.json()
            all_secrets = data.get("data", {}).get("data", {})
            result = {}
            for key in secret_keys:
                value = all_secrets.get(key)
                if value:
                    result[key] = str(value)
                    async with _cache_lock:
                        _secret_cache[key] = (str(value), time.time() + _CACHE_TTL)
                else:
                    result[key] = os.environ.get(key.upper())
            return result
    except Exception as e:
        logger.debug("Vault batch retrieval failed: %s", e)

    # Fall back to individual gets
    results = {}
    for key in secret_keys:
        results[key] = await get_secret(key)
    return results


async def health_check() -> dict:
    """Check Vault connectivity and token status."""
    if not is_vault_available():
        return {"available": False, "reason": "not_configured"}

    try:
        client = await _get_client()
        response = await client.get("/v1/sys/health")
        if response.status_code in (200, 429, 473):
            data = response.json()
            return {
                "available": True,
                "initialized": data.get("initialized", False),
                "sealed": data.get("sealed", False),
                "standby": data.get("standby", False),
            }
        return {"available": False, "reason": f"http_{response.status_code}"}
    except Exception as e:
        return {"available": False, "reason": str(e)}


async def validate_vault_startup() -> bool:
    """S2 FIX: Validate Vault connectivity and enforced secrets at startup.

    Called during app startup. In production (non-debug), refuses to start
    if Vault is enabled but unreachable, sealed, or enforced secrets are missing.

    Returns:
        True if Vault is healthy and all enforced secrets are available
    """
    if not is_vault_available():
        if _V_DEBUG:
            logger.info("Vault not configured — running in debug mode, skipping Vault validation")
            return True
        logger.warning("Vault not configured — secrets will come from environment variables")
        return True

    hc = await health_check()
    if not hc.get("available", False):
        msg = f"Vault is configured but unreachable: {hc.get('reason', 'unknown')}"
        if _V_DEBUG:
            logger.warning(msg + " — continuing in debug mode")
            return True
        logger.critical(msg)
        raise RuntimeError(f"Vault startup validation failed: {msg}")

    if hc.get("sealed", False):
        msg = "Vault is sealed — must be unsealed before app startup"
        if _V_DEBUG:
            logger.warning(msg + " — continuing in debug mode")
            return True
        logger.critical(msg)
        raise RuntimeError(f"Vault startup validation failed: {msg}")

    if _VAULT_ENFORCED_SECRETS:
        logger.info("Validating %d Vault-enforced secrets...", len(_VAULT_ENFORCED_SECRETS))
        for key in _VAULT_ENFORCED_SECRETS:
            value = await get_secret(key)
            if value is None:
                msg = f"Vault-enforced secret '{key}' not found in Vault at {_VAULT_KV_PREFIX}/{key}"
                if _V_DEBUG:
                    logger.warning(msg + " — continuing in debug mode")
                    continue
                logger.critical(msg)
                raise RuntimeError(f"Vault startup validation failed: {msg}")
        logger.info("All %d Vault-enforced secrets validated", len(_VAULT_ENFORCED_SECRETS))

    logger.info("Vault startup validation passed")
    return True

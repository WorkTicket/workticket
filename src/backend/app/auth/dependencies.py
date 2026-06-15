import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.db.tenant_context import set_current_tenant_id
from app.jobs.models import User


@dataclass(frozen=True)
class ClerkIdentity:
    user_id: str
    token_version: int

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=True)
settings = get_settings()

_cached_signing_keys: dict[str, dict[str, object]] = {}
_last_jwks_fetch = 0.0
_JWKS_CACHE_TTL = 3600
_JWKS_FETCH_TIMEOUT = 10.0
_jwks_init_done = False

_redis_jwks_prefix = "auth:jwks:"
_redis_jwks_ttl = 3600

_redis_available = False
_redis_last_health_check = 0.0
_REDIS_HEALTH_INTERVAL = 30.0
_redis_lock = asyncio.Lock()


async def _get_redis():
    global _redis_available, _redis_last_health_check
    now = time.time()
    if _redis_available and (now - _redis_last_health_check < _REDIS_HEALTH_INTERVAL):
        from app.redis import get_redis

        return await get_redis()

    async with _redis_lock:
        from app.redis import get_redis

        try:
            client = await get_redis()
            if client:
                _redis_available = True
                _redis_last_health_check = now
            else:
                _redis_available = False
            return client
        except Exception:
            _redis_available = False
            return None


async def _fetch_jwks():
    global _cached_signing_keys, _last_jwks_fetch
    jwks_url = f"{settings.clerk_jwt_issuer.rstrip('/')}/.well-known/jwks.json"
    _timeout = httpx.Timeout(_JWKS_FETCH_TIMEOUT, connect=5.0)
    async with httpx.AsyncClient(timeout=_timeout) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        data = resp.json()
        _cached_signing_keys = {k["kid"]: k for k in data.get("keys", [])}
        _last_jwks_fetch = time.time()
        return _cached_signing_keys


async def _init_jwks():
    global _jwks_init_done
    if settings.clerk_jwt_issuer:
        try:
            await _fetch_jwks()
        except Exception as e:
            logger.warning("Failed to fetch JWKS on startup: %s", e)
    _jwks_init_done = True


async def _get_signing_key_from_jwt(token: str):
    global _cached_signing_keys, _last_jwks_fetch
    unverified = jwt.decode(token, options={"verify_signature": False})
    kid = unverified.get("kid")
    if kid:
        logger.debug("Auth attempt kid=%s", kid)

    try:
        signing_key = jwt.PyJWKClient("").get_signing_key_from_jwt(token)
        if signing_key:
            return signing_key
    except Exception:
        logger.debug("PyJWKClient key resolution failed, falling back to cached JWKS")
        pass  # nosec B110

    if not kid:
        return None

    try:
        keys = _cached_signing_keys
        cache_age = time.time() - _last_jwks_fetch
        if not keys or cache_age > _JWKS_CACHE_TTL:
            await _fetch_jwks()
            keys = _cached_signing_keys
        if kid in keys:
            from jwt.algorithms import RSAAlgorithm

            key_data = keys[kid]
            public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
            return type("obj", (object,), {"key": public_key})()
    except Exception as e:
        logger.warning("JWKS fetch failed, using cache if available: %s", e)

    if _cached_signing_keys and kid and kid in _cached_signing_keys:
        from jwt.algorithms import RSAAlgorithm

        key_data = _cached_signing_keys[kid]
        public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
        return type("obj", (object,), {"key": public_key})()

    return None


async def _get_signing_key_from_redis(token: str):
    unverified = jwt.decode(token, options={"verify_signature": False})
    kid = unverified.get("kid")
    if not kid:
        return None
    try:
        r = await _get_redis()
        if r:
            cached = await r.get(f"{_redis_jwks_prefix}{kid}")
            if cached:
                from jwt.algorithms import RSAAlgorithm

                key_data = json.loads(cached)
                public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
                return type("obj", (object,), {"key": public_key})()
        return None
    except Exception:
        try:
            if kid:
                r = await _get_redis()
                if r:
                    await r.delete(f"{_redis_jwks_prefix}{kid}")
        except Exception:
            logger.debug("Failed to delete corrupted JWKS key from Redis cache")
            pass  # nosec B110
        return None


async def _cache_key_in_redis(kid: str, key_data: dict):
    try:
        r = await _get_redis()
        if r:
            await r.setex(f"{_redis_jwks_prefix}{kid}", _redis_jwks_ttl, json.dumps(key_data))
    except Exception:
        logger.debug("Failed to cache JWKS key in Redis")
        pass  # nosec B110


async def _verify_clerk_token(credentials: HTTPAuthorizationCredentials) -> ClerkIdentity:
    token = credentials.credentials

    if not settings.clerk_jwt_issuer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT verification not configured",
        )

    try:
        signing_key = await _get_signing_key_from_redis(token)
        if not signing_key:
            signing_key = await _get_signing_key_from_jwt(token)
            if signing_key and hasattr(signing_key, "key"):
                unverified = jwt.decode(token, options={"verify_signature": False})
                kid = unverified.get("kid")
                key_id = getattr(signing_key, "kid", None) or kid
                if key_id and kid and kid in _cached_signing_keys:
                    await _cache_key_in_redis(kid, _cached_signing_keys[kid])
        if not signing_key:
            raise jwt.InvalidTokenError("No signing key available")
        decode_kwargs = {
            "algorithms": ["RS256"],
            "issuer": settings.clerk_jwt_issuer.rstrip("/"),
            "audience": settings.clerk_jwt_audience,
            "options": {"verify_exp": True, "verify_nbf": True, "verify_iat": True, "verify_aud": True},
        }
        payload = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from None
    except jwt.ImmatureSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token not yet valid") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    token_version = payload.get("token_version", 0)

    r = await _get_redis()
    if r:
        try:
            blacklisted = await r.get(f"session_blacklist:{user_id}:{token_version}")
            if blacklisted:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token has been revoked. Please sign in again.",
                )
        except HTTPException:
            raise
        except Exception:
            logger.warning("Session blacklist check failed, denying authentication", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unable to verify session. Please try again.",
            ) from None
    else:
        logger.warning("Redis unavailable for session blacklist check, denying authentication")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to verify session. Please try again.",
        )

    return ClerkIdentity(user_id=user_id, token_version=token_version)


async def get_clerk_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> ClerkIdentity:
    return await _verify_clerk_token(credentials)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    identity = await _verify_clerk_token(credentials)
    user_id = identity.user_id
    token_version = identity.token_version

    result = await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account has been deactivated. Please contact your administrator.",
        )

    if token_version < user.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please sign in again.",
        )

    set_current_tenant_id(user.company_id)

    return user

import logging
import os
from threading import Lock

from app.billing.cost_estimator import PLAN_TIERS
from app.sync_redis_pool import get_sync_redis

logger = logging.getLogger(__name__)

_ESTIMATED_WORKERS = max(1, int(os.getenv("CELERY_WORKER_CONCURRENCY", "2")))

# Track which company_ids we have locked in this process (for safe release)
_locked_companies: set[str] = set()
_locked_lock = Lock()


class CompanyConcurrencyLock:
    def __init__(self):
        self._lock_timeout = 300
        self._redis_script_sha = None
        self._release_script_sha = None

    _ACQUIRE_LUA = """
        local key = KEYS[1]
        local max = tonumber(ARGV[1])
        local timeout = tonumber(ARGV[2])
        local current = redis.call("GET", key)
        current = tonumber(current) or 0
        if current >= max then
            return 0
        end
        redis.call("INCR", key)
        redis.call("EXPIRE", key, timeout)
        return 1
    """

    async def acquire(self, company_id: str, plan: str) -> bool:
        tier = PLAN_TIERS.get(plan, PLAN_TIERS["free"])
        max_concurrent = tier["concurrency_limit"]

        r = await self._get_redis()
        if r is None:
            # C-4 FIX: Local fallback divides max by estimated workers
            local_max = max(1, max_concurrent // max(1, _ESTIMATED_WORKERS))
            with _locked_lock:
                if len(_locked_companies) >= local_max:
                    logger.error("Redis unavailable for concurrency lock %s — fail-closed, denying acquire", company_id)
                    return False
                _locked_companies.add(company_id)
                return True

        key = f"conc:{company_id}"
        try:
            if self._redis_script_sha is None:
                self._redis_script_sha = r.register_script(self._ACQUIRE_LUA)
            # MED-4 FIX: Fall back to eval on NOSCRIPT error (Redis Sentinel failover)
            try:
                result = await self._redis_script_sha(keys=[key], args=[max_concurrent, self._lock_timeout])
            except Exception as _script_err:
                _noscript = "NOSCRIPT" in str(_script_err).upper() or "NOSCRIPT" in str(
                    getattr(_script_err, "message", "")
                )
                if _noscript:
                    self._redis_script_sha = None
                    result = await r.eval(self._ACQUIRE_LUA, 1, key, max_concurrent, self._lock_timeout)
                else:
                    raise
            if result == 1:
                with _locked_lock:
                    _locked_companies.add(company_id)
                return True
            else:
                return False
        except Exception as e:
            logger.error("Redis concurrency lock failed for %s: %s — fail-closed, denying acquire", company_id, e)
            return False

    _RELEASE_LUA = """
        local key = KEYS[1]
        local count = redis.call("GET", key)
        if not count then
            return -1  -- key missing, nothing to release
        end
        count = tonumber(count)
        if count <= 0 then
            redis.call("DEL", key)
            return 0
        end
        local current = tonumber(redis.call("GET", key))
        if current and current > 0 then
            redis.call("DECR", key)
        end
        local new_count = redis.call("GET", key)
        new_count = tonumber(new_count)
        if new_count <= 0 then
            redis.call("DEL", key)
            return 0
        end
        return new_count
    """

    def _validate_lua_scripts(self, r):
        """Register Lua scripts and cache their SHAs."""
        try:
            if self._redis_script_sha is None:
                self._redis_script_sha = r.register_script(self._ACQUIRE_LUA)
            if self._release_script_sha is None:
                self._release_script_sha = r.register_script(self._RELEASE_LUA)
        except Exception as e:
            logger.warning("Failed to register Lua scripts: %s", e)

    async def release(self, company_id: str):
        with _locked_lock:
            if company_id in _locked_companies:
                _locked_companies.remove(company_id)

        r = await self._get_redis()
        if r is None:
            logger.error("Redis unavailable for concurrency release — skipping release for %s", company_id)
            return

        key = f"conc:{company_id}"
        try:
            if self._release_script_sha is None:
                self._release_script_sha = r.register_script(self._RELEASE_LUA)
            # MED-4 FIX: Fall back to eval on NOSCRIPT error (Redis Sentinel failover)
            try:
                new_count = await self._release_script_sha(keys=[key])
            except Exception as _script_err:
                _noscript = "NOSCRIPT" in str(_script_err).upper() or "NOSCRIPT" in str(
                    getattr(_script_err, "message", "")
                )
                if _noscript:
                    self._release_script_sha = None
                    new_count = await r.eval(self._RELEASE_LUA, 1, key)
                else:
                    raise
            if new_count == -1:
                pass
            elif new_count == 0:
                await r.delete(key)
            try:
                from app.monitoring.prometheus import increment_counter

                if new_count is not None and new_count < 0:
                    increment_counter("workticket_concurrency_counter_negative_total", {"company_id": company_id})
            except Exception:
                logger.debug("Concurrency counter negative metric increment failed, continuing")
        pass  # nosec B110
        except Exception as e:
            logger.error("Redis concurrency release failed for %s: %s", company_id, e)

    async def active_count(self, company_id: str) -> int:
        r = await self._get_redis()
        if r is None:
            return 0
        key = f"conc:{company_id}"
        try:
            current = await r.get(key)
            return int(current) if current else 0
        except Exception:
            return 0

    async def cleanup_stale(self, max_age_seconds: int = 300):
        r = await self._get_redis()
        if r is None:
            return
        try:
            cursor = 0
            pattern = "conc:*"
            while True:
                cursor, keys = await r.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    for key in keys:
                        ttl = await r.ttl(key)
                        if ttl <= 0:
                            await r.delete(key)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("Failed to cleanup stale concurrency locks in Redis: %s", e)

    async def _get_redis(self):
        try:
            from app.ai.rate_limiter import _get_redis

            return await _get_redis()
        except Exception:
            return None

    def _get_sync_redis(self, timeout: float = 0.5):
        try:
            return get_sync_redis()
        except Exception:
            return None


company_concurrency = CompanyConcurrencyLock()

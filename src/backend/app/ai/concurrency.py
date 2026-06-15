import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_REDIS_URL = os.getenv("REDIS_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))


class ConcurrencyLimiter:
    def __init__(self, name: str = "default", max_concurrent: int = 1):
        self.name = name
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0
        self._total_queued = 0
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task | None = None
        self._redis_key = f"conc:ai:{name}"

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def available_slots(self) -> int:
        return self.max_concurrent - self._active_count

    async def _get_redis(self):
        try:
            from app.ai.rate_limiter import _get_redis

            return await _get_redis()
        except Exception:
            return None

    async def _start_heartbeat(self):
        """Refresh the Redis TTL every 30s while holding the slot."""
        while True:
            try:
                await asyncio.sleep(30)
                r = await self._get_redis()
                if r:
                    await r.expire(self._redis_key, 60)
            except Exception:
                logger.debug("AI concurrency heartbeat Redis expire failed, heartbeat will retry")
                pass  # nosec B110
            if self._active_count == 0:
                break

    async def acquire(self) -> bool:
        # Local semaphore for queueing
        await self._semaphore.acquire()
        # Redis-backed counter as source of truth
        r = await self._get_redis()
        if r:
            try:
                count = await r.incr(self._redis_key)
                await r.expire(self._redis_key, 60)
                if count > self.max_concurrent:
                    await r.decr(self._redis_key)
                    self._semaphore.release()
                    return False
            except Exception:
                logger.error("Redis error in concurrency acquire — failing closed", exc_info=True)
                self._semaphore.release()
                return False
        async with self._lock:
            self._active_count += 1
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._start_heartbeat())
        return True

    async def release(self):
        r = await self._get_redis()
        if r:
            try:
                new_count = await r.decr(self._redis_key)
                if new_count <= 0:
                    await r.delete(self._redis_key)
            except Exception:
                logger.debug("AI concurrency release Redis decrement failed, continuing")
                pass  # nosec B110
        async with self._lock:
            self._active_count -= 1
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()

    async def execute(self, coro):
        async with self:
            return await coro

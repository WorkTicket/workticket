import asyncio
import contextlib
import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_CIRCUIT_LUA_CHECK_HALF_OPEN = """
    -- HIGH-3 FIX: Globally coordinated half-open probe.
    -- Returns {open, allow_probe, cooldown_remaining, half_open}
    -- allow_probe=1: exactly 1 replica across all may probe
    local key = KEYS[1]
    local cooldown = tonumber(ARGV[1])
    local state = redis.call('HGETALL', key)
    if #state == 0 then
        return {0, 0, 0, 0}
    end
    local tbl = {}
    for i = 1, #state, 2 do
        tbl[state[i]] = state[i+1]
    end
    if tbl['open'] ~= '1' then
        return {0, 0, 0, 0}
    end
    local now = redis.call('TIME')[1]
    local last_failure = tonumber(tbl['last_failure']) or 0
    local elapsed = now - last_failure
    if elapsed <= cooldown then
        return {1, 0, cooldown - elapsed, tonumber(tbl['half_open']) or 0}
    end
    -- Cooldown expired: enter half-open globally
    local half_open = tonumber(tbl['half_open']) or 0
    local half_open_probed = tonumber(tbl['half_open_probed']) or 0
    if half_open == 0 then
        redis.call('HSET', key, 'half_open', '1')
        return {1, 1, 0, 1}
    end
    if half_open_probed == 0 then
        redis.call('HSET', key, 'half_open_probed', '1')
        return {1, 1, 0, 1}
    end
    -- Probe already used: extend cooldown
    redis.call('HSET', key, 'last_failure', now)
    return {1, 0, cooldown, 1}
"""


class CircuitBreaker:
    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        cooldown_seconds: float = 120.0,
        half_open_max_retries: int = 1,
        max_cooldown_seconds: float = 600.0,
        stability_gate: int = 3,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_retries = half_open_max_retries
        self.max_cooldown_seconds = max_cooldown_seconds
        self.stability_gate = stability_gate
        self._base_cooldown = cooldown_seconds

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._consecutive_successes = 0
        self._last_failure_time: float | None = None
        self._half_open_attempts = 0
        self._lock = asyncio.Lock()
        self._redis_prefix = f"cb:{name}"
        self._lua_sha: str | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def _redis(self):
        try:
            from app.ai.rate_limiter import _get_redis

            return await _get_redis()
        except Exception:
            return None

    async def _get_lua_sha(self, r) -> str:
        if self._lua_sha is None:
            with contextlib.suppress(Exception):
                self._lua_sha = r.script_load(_CIRCUIT_LUA_CHECK_HALF_OPEN)
        return self._lua_sha  # type: ignore[return-value]

    async def is_available(self) -> bool:
        r = await self._redis()
        if r:
            try:
                state = await r.hget(f"{self._redis_prefix}", "open")
                return state != "1"  # type: ignore[no-any-return]
            except Exception:
                pass  # nosec B110
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._last_failure_time and (time.monotonic() - self._last_failure_time) >= self.cooldown_seconds:
                    logger.info("Circuit %s transitioning OPEN -> HALF_OPEN after cooldown", self.name)
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_attempts = 0
                    return True
                return False
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_attempts < self.half_open_max_retries:
                    self._half_open_attempts += 1
                    return True
                return False
            return False

    async def record_success(self):
        async with self._lock:
            self._consecutive_successes += 1
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_attempts = 0
            if self._consecutive_successes >= self.stability_gate:
                if self._state == CircuitState.HALF_OPEN:
                    logger.info(
                        "Circuit %s recovered after %d consecutive successes: HALF_OPEN -> CLOSED",
                        self.name,
                        self._consecutive_successes,
                    )
                self._state = CircuitState.CLOSED
                if self._consecutive_successes >= 5:
                    self.cooldown_seconds = self._base_cooldown
                    logger.info(
                        "Circuit %s stable after %d consecutive successes, cooldown reset to %.0fs",
                        self.name,
                        self._consecutive_successes,
                        self._base_cooldown,
                    )
        r = await self._redis()
        if r:
            with contextlib.suppress(Exception):
                await r.hdel(f"{self._redis_prefix}", "open")
                await r.hdel(f"{self._redis_prefix}", "last_failure")
                await r.hdel(f"{self._redis_prefix}", "level")
                await r.hdel(f"{self._redis_prefix}", "half_open")
                await r.hdel(f"{self._redis_prefix}", "half_open_probed")

    async def record_failure(self):
        async with self._lock:
            self._failure_count += 1
            self._consecutive_successes = 0
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit %s half-open probe failed -> OPEN (cooldown=%.0fs)", self.name, self.cooldown_seconds
                )
                r = await self._redis()
                if r:
                    try:
                        await r.hset(f"{self._redis_prefix}", "open", "1")
                        await r.hset(f"{self._redis_prefix}", "last_failure", str(int(time.time())))
                        await r.expire(f"{self._redis_prefix}", int(self.cooldown_seconds * 2))
                    except Exception:
                        pass  # nosec B110
                return
            if self._failure_count >= self.failure_threshold:
                self.cooldown_seconds = min(self.cooldown_seconds * 2, self.max_cooldown_seconds)
                logger.warning(
                    "Circuit %s OPEN after %d consecutive failures (cooldown=%.0fs)",
                    self.name,
                    self._failure_count,
                    self.cooldown_seconds,
                )
                self._state = CircuitState.OPEN
                r = await self._redis()
                if r:
                    try:
                        await r.hset(f"{self._redis_prefix}", "open", "1")
                        await r.hset(f"{self._redis_prefix}", "level", str(self.cooldown_seconds))
                        await r.hset(f"{self._redis_prefix}", "last_failure", str(int(time.time())))
                        await r.hset(f"{self._redis_prefix}", "half_open", "0")
                        await r.hset(f"{self._redis_prefix}", "half_open_probed", "0")
                        await r.expire(f"{self._redis_prefix}", int(self.cooldown_seconds * 2))
                    except Exception:
                        pass  # nosec B110

    async def reset(self):
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_attempts = 0
            logger.info("Circuit %s manually reset to CLOSED", self.name)
        r = await self._redis()
        if r:
            with contextlib.suppress(Exception):
                await r.delete(f"{self._redis_prefix}")

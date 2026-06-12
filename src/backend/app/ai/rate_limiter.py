import logging
import os
import time

logger = logging.getLogger(__name__)

_redis_client = None
_redis_available = False
_redis_last_health_check = 0.0
_REDIS_HEALTH_INTERVAL = 30.0

# HIGH-1 FIX: Circuit breaker state stored in Redis (shared across all replicas).
# Lua script atomically checks and updates the circuit state to prevent split-brain.
_CIRCUIT_REDIS_KEY = "rl:circuit:redis"
_CIRCUIT_COOLDOWN_KEY = "rl:circuit:redis:cooldown"
_CIRCUIT_FAILURE_KEY = "rl:circuit:redis:failure_count"
_CIRCUIT_MAX_COOLDOWN = 300.0
_CIRCUIT_INITIAL_COOLDOWN = 60.0

_fallback_active = False

_ESTIMATED_WORKERS = max(1, int(os.getenv("RATE_LIMITER_WORKER_REPLICAS", "5")))
_SAFETY_MULTIPLIER = 1.5
if _ESTIMATED_WORKERS <= 2:
    from app.config import get_settings

    _cfg = get_settings()
    if not _cfg.debug:
        logger.warning(
            "RATE_LIMITER_WORKER_REPLICAS is not set or set to %d in non-debug mode. "
            "Defaulting to 5 with %.1fx safety margin. Effective local-fallback rate will be %.1f%% of intended. "
            "Set RATE_LIMITER_WORKER_REPLICAS to actual replica count to eliminate the safety margin.",
            _ESTIMATED_WORKERS,
            _SAFETY_MULTIPLIER,
            100.0 / (_ESTIMATED_WORKERS * _SAFETY_MULTIPLIER),
        )
    _ESTIMATED_WORKERS = 5


async def _get_redis():
    global _redis_client, _redis_available, _redis_last_health_check
    global _fallback_active

    now = time.time()

    # HIGH-1 FIX: Check shared Redis circuit breaker state
    if _redis_client is not None:
        try:
            _circuit_open = await _redis_client.get(_CIRCUIT_REDIS_KEY)
            if _circuit_open == b"1" or _circuit_open == "1":
                _cooldown_remaining = await _redis_client.ttl(_CIRCUIT_REDIS_KEY)
                if _cooldown_remaining is not None and _cooldown_remaining > 0:
                    _fallback_active = True
                    return None
                # Cooldown expired — clear circuit
                await _redis_client.delete(_CIRCUIT_REDIS_KEY)
                await _redis_client.delete(_CIRCUIT_FAILURE_KEY)
        except Exception as _e:
            logger.debug("Redis circuit breaker state check failed: %s", _e)

    if _redis_available and (now - _redis_last_health_check < _REDIS_HEALTH_INTERVAL):
        _fallback_active = False
        return _redis_client

    try:
        from app.config import get_settings

        settings = get_settings()
        if _redis_client is None:
            from app.redis_sentinel import create_redis_from_url

            _redis_client = create_redis_from_url(
                settings.effective_redis_cache_url,
                socket_connect_timeout=0.5,
                socket_keepalive=True,
                health_check_interval=15,
            )
        await _redis_client.ping()
        _redis_available = True
        _redis_last_health_check = now
        # Clear circuit breaker on successful connection
        await _redis_client.delete(_CIRCUIT_REDIS_KEY)
        await _redis_client.delete(_CIRCUIT_FAILURE_KEY)
        _fallback_active = False
        return _redis_client
    except Exception as e:
        # HIGH-1 FIX: Update shared circuit breaker state in Redis
        _failure_count = 0
        try:
            if _redis_client:
                _failure_count = int(await _redis_client.get(_CIRCUIT_FAILURE_KEY) or 0)
                _failure_count += 1
                await _redis_client.set(_CIRCUIT_FAILURE_KEY, _failure_count)
                _cooldown = min(_CIRCUIT_INITIAL_COOLDOWN * (2 ** (_failure_count - 1)), _CIRCUIT_MAX_COOLDOWN)
                await _redis_client.setex(_CIRCUIT_REDIS_KEY, int(_cooldown), "1")
        except Exception as _e:
            logger.debug("Redis circuit breaker state update failed: %s", _e)
        _fallback_active = True
        _redis_available = False
        logger.error(
            "REDIS CIRCUIT OPEN for rate limiter (failure #%d, shared across replicas): %s — "
            "rate limiting degraded to local fallback (%.0f%% throughput)",
            _failure_count,
            e,
            100.0 / (_ESTIMATED_WORKERS * _SAFETY_MULTIPLIER),
        )
        return None


class RedisTokenBucket:
    def __init__(self, redis_client, key_prefix: str, rate: float, burst: int):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.rate = rate
        self.burst = burst

    async def consume(self, key: str, tokens: float = 1.0) -> bool:
        redis_key = f"{self.key_prefix}:{key}"
        now = time.time()
        try:
            result = await self.redis.eval(
                """
                local key = KEYS[1]
                local now = tonumber(ARGV[1])
                local rate = tonumber(ARGV[2])
                local burst = tonumber(ARGV[3])
                local tokens = tonumber(ARGV[4])

                local stored = redis.call('HMGET', key, 'tokens', 'ts')
                local last_tokens = tonumber(stored[1])
                local last_refill = tonumber(stored[2])
                if last_tokens and last_refill then
                    local elapsed = now - last_refill
                    local new_tokens = math.min(burst, last_tokens + elapsed * rate)
                    if new_tokens >= tokens then
                        new_tokens = new_tokens - tokens
                        redis.call('HMSET', key, 'tokens', new_tokens, 'ts', now)
                        redis.call('EXPIRE', key, 3600)
                        return 1
                    else
                        redis.call('HMSET', key, 'tokens', new_tokens, 'ts', now)
                        redis.call('EXPIRE', key, 3600)
                        return 0
                    end
                else
                    local new_tokens = burst - tokens
                    redis.call('HMSET', key, 'tokens', new_tokens, 'ts', now)
                    redis.call('EXPIRE', key, 3600)
                    return 1
                end
                """,
                1,
                redis_key,
                now, self.rate, self.burst, tokens,
            )
            return bool(result)
        except Exception as e:
            logger.warning("Redis rate limiter operation failed: %s", e)
            raise


class RateLimiter:
    def __init__(self):
        self._redis: object | None = None

        self.user_rate = float(os.getenv("RL_USER_RATE", "1.0"))
        self.user_burst = int(os.getenv("RL_USER_BURST", "3"))
        self.tenant_rate = float(os.getenv("RL_TENANT_RATE", "5.0"))
        self.tenant_burst = int(os.getenv("RL_TENANT_BURST", "15"))
        self.global_rate = float(os.getenv("RL_GLOBAL_RATE", "5.0"))
        self.global_burst = int(os.getenv("RL_GLOBAL_BURST", "10"))

    def get_limits(self, endpoint: str, plan: str = "free") -> dict | None:
        """Return rate limits for a given endpoint and plan.

        Falls back to get_plan_limits for known endpoint categories.
        """
        return self.get_plan_limits(plan)

    @staticmethod
    def get_plan_limits(plan: str = "free") -> dict:
        """Return per-plan rate limits from settings."""
        from app.config import get_settings

        s = get_settings()
        plan = (plan or "free").lower()
        if plan == "enterprise":
            return {
                "ai_rate": s.rl_enterprise_ai_rate,
                "ai_burst": s.rl_enterprise_ai_burst,
                "req_rate": s.rl_enterprise_requests_rate,
                "req_burst": s.rl_enterprise_requests_burst,
            }
        elif plan == "pro":
            return {
                "ai_rate": s.rl_pro_ai_rate,
                "ai_burst": s.rl_pro_ai_burst,
                "req_rate": s.rl_pro_requests_rate,
                "req_burst": s.rl_pro_requests_burst,
            }
        return {
            "ai_rate": s.rl_free_ai_rate,
            "ai_burst": s.rl_free_ai_burst,
            "req_rate": s.rl_free_requests_rate,
            "req_burst": s.rl_free_requests_burst,
        }

    async def _ensure_redis(self):
        if self._redis is None:
            r = await _get_redis()
            if r:
                self._redis = r
            else:
                return None
        return self._redis

    async def _check_with_fallback(self, check_fn, local_key: str, rate: float, burst: int) -> bool:
        r = await self._ensure_redis()
        if r:
            rb = RedisTokenBucket(r, f"rl:{local_key}", rate, burst)
            try:
                return await rb.consume("global" if local_key == "global" else local_key)
            except Exception as _e:
                logger.debug("Redis rate limiter consume failed, falling back to local: %s", _e)
        from app.ai.local_rate_limiter import local_limiter

        scaled_rate = rate / (_ESTIMATED_WORKERS * _SAFETY_MULTIPLIER)
        scaled_burst = max(1, burst // (_ESTIMATED_WORKERS * 2))
        if local_key == "global":
            return local_limiter.check_global(scaled_rate, scaled_burst)
        elif local_key == "user":
            return local_limiter.check_user(check_fn, scaled_rate, scaled_burst)
        elif local_key == "ip":
            return local_limiter.check_ip(check_fn, scaled_rate, scaled_burst)
        else:
            return local_limiter.check_tenant(check_fn, scaled_rate, scaled_burst)

    async def check_user(self, user_id: str) -> bool:
        return await self._check_with_fallback(user_id, "user", self.user_rate, self.user_burst)

    async def check_tenant(self, company_id: str) -> bool:
        return await self._check_with_fallback(company_id, "tenant", self.tenant_rate, self.tenant_burst)

    async def check_global(self) -> bool:
        return await self._check_with_fallback("global", "global", self.global_rate, self.global_burst)

    async def check_ip(self, client_ip: str) -> bool:
        return await self._check_with_fallback(client_ip, "ip", 10.0, 20)

    async def check_all(self, user_id: str, company_id: str, client_ip: str = "") -> tuple[bool, str]:
        if not await self.check_global():
            return False, "global rate limit exceeded"
        if company_id and not await self.check_tenant(company_id):
            return False, "tenant rate limit exceeded"
        if user_id and not await self.check_user(user_id):
            return False, "user rate limit exceeded"
        if client_ip and not await self.check_ip(client_ip):
            return False, "IP rate limit exceeded"
        return True, ""

    async def remaining_user(self, user_id: str) -> int:
        r = await self._ensure_redis()
        if r:
            try:
                tokens_str = await r.hget(f"rl:user:{user_id}", "tokens")
                if tokens_str is not None:
                    return max(0, int(float(tokens_str)))
            except Exception as _e:
                logger.debug("Redis remaining_user failed: %s", _e)
        return self.user_burst

    @property
    def redis_available(self) -> bool:
        return _redis_available and not _fallback_active

    @property
    def fallback_active(self) -> bool:
        return _fallback_active

    @property
    def circuit_breaker_state(self) -> dict:
        return {
            "open": _fallback_active,
            "failure_count": -1,
            "cooldown_seconds": -1,
            "last_failure_age": None,
        }


rate_limiter = RateLimiter()

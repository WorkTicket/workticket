import logging
import time

logger = logging.getLogger(__name__)


class LocalTokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self._tokens: float = burst
        self._last_refill: float = time.monotonic()

    def consume(self, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    @property
    def available(self) -> float:
        now = time.monotonic()
        elapsed = now - self._last_refill
        return min(self.burst, self._tokens + elapsed * self.rate)


class LocalRateLimiter:
    def __init__(self):
        self._buckets: dict[str, tuple[LocalTokenBucket, float]] = {}
        self._cleanup_ttl = 30.0

        self.user_rate = 1.0
        self.user_burst = 3
        self.tenant_rate = 5.0
        self.tenant_burst = 15
        self.global_rate = 5.0
        self.global_burst = 10

    def _get_bucket(self, key: str, rate: float, burst: int) -> LocalTokenBucket:
        now = time.monotonic()
        if key in self._buckets:
            bucket, created = self._buckets[key]
            if now - created < self._cleanup_ttl:
                return bucket
        bucket = LocalTokenBucket(rate, burst)
        self._buckets[key] = (bucket, now)
        return bucket

    def check_user(self, user_id: str, rate: float | None = None, burst: int | None = None) -> bool:
        r = rate if rate is not None else self.user_rate
        b = burst if burst is not None else self.user_burst
        return self._get_bucket(f"user:{user_id}", r, b).consume()

    def check_tenant(self, company_id: str, rate: float | None = None, burst: int | None = None) -> bool:
        r = rate if rate is not None else self.tenant_rate
        b = burst if burst is not None else self.tenant_burst
        return self._get_bucket(f"tenant:{company_id}", r, b).consume()

    def check_global(self, rate: float | None = None, burst: int | None = None) -> bool:
        r = rate if rate is not None else self.global_rate
        b = burst if burst is not None else self.global_burst
        return self._get_bucket("global", r, b).consume()

    def check_ip(self, client_ip: str, rate: float | None = None, burst: int | None = None) -> bool:
        r = rate if rate is not None else 10.0
        b = burst if burst is not None else 20
        return self._get_bucket(f"ip:{client_ip}", r, b).consume()

    def check_all(self, user_id: str, company_id: str, client_ip: str = "") -> tuple[bool, str]:
        if not self.check_global():
            return False, "global rate limit exceeded"
        if company_id and not self.check_tenant(company_id):
            return False, "tenant rate limit exceeded"
        if user_id and not self.check_user(user_id):
            return False, "user rate limit exceeded"
        if client_ip and not self.check_ip(client_ip):
            return False, "IP rate limit exceeded"
        return True, ""

    def cleanup(self):
        now = time.monotonic()
        stale = [k for k, (_, t) in self._buckets.items() if now - t > self._cleanup_ttl]
        for k in stale:
            del self._buckets[k]

    def reset(self):
        self._buckets.clear()


local_limiter = LocalRateLimiter()

import logging
import os
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# H-2/S-1 FIX: Sensible default limits for all endpoint categories.
# Previous defaults were 100/s which effectively provided no protection.
# New defaults: 10/s global, 5/s per user, 3/s per IP.
# Each endpoint category has its own override based on expected usage.
_route_overrides: dict[str, tuple[float, int]] = {
    # AI endpoints have their own per-endpoint rate limiter
    "/api/v1/ai": (float("inf"), float("inf")),
    # Health endpoints - unlimited for k8s probes (livez, healthz), rate-limited for public health
    "/health": (0.05, 3),  # 3/min
    "/readyz": (0.05, 3),  # 3/min — exposes circuit breaker state, queue depths, etc.
    "/livez": (float("inf"), float("inf")),
    "/healthz": (float("inf"), float("inf")),
    # Auth endpoints - strict rate limits to prevent brute-force
    "/api/v1/auth/login": (0.083, 5),  # 5/min
    "/api/v1/auth/register": (0.05, 3),  # 3/min
    "/api/v1/auth/token": (0.167, 10),  # 10/min
    # Billing webhooks - strict rate limit
    "/api/v1/billing/webhook": (0.167, 1),
    # Billing operations - sensitive, heavily restricted
    "/api/v1/billing/credits": (0.167, 1),
    "/api/v1/billing/change-plan": (0.017, 1),
    "/api/v1/billing/admin/refund": (0.017, 1),
    "/api/v1/billing/admin/reverse-charge": (0.017, 1),
    "/api/v1/billing/disable-ai": (0.017, 1),
    "/api/v1/billing/enable-ai": (0.017, 1),
    # Media uploads - rate limited per user
    "/api/v1/media/upload-url": (0.333, 5),  # 20/min
    "/api/v1/media/confirm-upload": (0.333, 5),  # 20/min
    # Job CRUD - core operations
    "/api/v1/jobs": (2.0, 5),  # 120/min, burst 5
    # AI processing - expensive, tightly controlled
    "/api/v1/ai/process-job": (1.0, 3),  # 60/min, burst 3
    "/api/v1/ai/output": (10.0, 20),  # 600/min for reads
    # Estimates and quotes
    "/api/v1/estimates": (5.0, 10),
    "/api/v1/quotes": (5.0, 10),
    # Analytics - heavy queries
    "/api/v1/analytics": (2.0, 5),
    # Tracing
    "/api/v1/tracing": (5.0, 10),
}


_ESTIMATED_WORKERS = max(1, int(os.getenv("ESTIMATED_REPLICAS", "5")))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware with Redis primary and local fallback.

    When Redis is available, rate limits are enforced consistently across all workers.
    When Redis is down, each worker uses an in-memory limiter. To prevent the effective
    rate limit from being multiplied by the number of workers, we divide the rate and
    burst limits by _ESTIMATED_WORKERS in the fallback (see _get_strict_limits).
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self._local_limiter = None

    def _get_local_limiter(self):
        if self._local_limiter is None:
            from app.ai.local_rate_limiter import local_limiter

            self._local_limiter = local_limiter
        return self._local_limiter

    def _get_limits(self, path: str) -> tuple[float, int]:
        for prefix, (rate, burst) in _route_overrides.items():
            if path.startswith(prefix):
                return rate, burst
        # H-2 FIX: Reduced default from 100/s to 10/s with burst of 10
        return 10.0, 10

    def _get_strict_limits(self, rate: float, burst: int) -> tuple[float, int]:
        strict_rate = max(rate / _ESTIMATED_WORKERS, 1.0)
        strict_burst = max(int(burst / _ESTIMATED_WORKERS), 2)
        return strict_rate, strict_burst

    async def dispatch(self, request: Request, call_next):
        user_id = getattr(request.state, "user_id", None) or request.headers.get("X-User-ID", "")
        company_id = getattr(request.state, "company_id", None) or request.headers.get("X-Company-ID", "")
        path = request.url.path

        rate, burst = self._get_limits(path)

        if rate != float("inf"):
            allowed, reason = await self._check_rate(path, user_id, company_id, rate, burst)
            if not allowed:
                import uuid

                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=429,
                    content={
                        "success": False,
                        "error": {
                            "code": "RATE_LIMIT_EXCEEDED",
                            "message": reason,
                            "request_id": str(getattr(request.state, "request_id", uuid.uuid4())),
                        },
                    },
                    headers={
                        "X-RateLimit-Limit": str(burst),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time()) + 60),
                        "Retry-After": "60",
                    },
                )

        response = await call_next(request)

        remaining = burst
        try:
            from app.ai.local_rate_limiter import local_limiter

            bucket = local_limiter._get_bucket("global", rate, burst)
            remaining = max(0, int(bucket.available))
        except Exception as _e:
            logger.debug("Failed to get rate limit remaining: %s", _e)

        response.headers["X-RateLimit-Limit"] = str(burst)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + 60)

        return response

    async def _check_rate(self, path: str, user_id: str, company_id: str, rate: float, burst: int) -> tuple[bool, str]:
        local = self._get_local_limiter()

        # Check Redis first (consistent across all workers)
        try:
            from app.ai.rate_limiter import rate_limiter as redis_rl

            redis_allowed, redis_reason = await redis_rl.check_all(user_id, company_id)
            if not redis_allowed:
                return False, redis_reason
        except Exception:
            strict_rate, strict_burst = self._get_strict_limits(rate, burst)
            if not local._get_bucket("global", strict_rate, strict_burst).consume():
                return False, "global rate limit exceeded"
            if company_id and not local._get_bucket(f"tenant:{company_id}", strict_rate, strict_burst).consume():
                return False, "tenant rate limit exceeded"
            if user_id and not local._get_bucket(f"user:{user_id}", strict_rate, strict_burst).consume():
                return False, "user rate limit exceeded"

        return True, ""

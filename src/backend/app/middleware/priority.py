import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Priority tiers (lower number = higher priority)
_PRIORITY_TIERS = {
    "/api/v1/billing": 10,
    "/api/v1/jobs": 20,
    "/api/v1/ai": 30,
    "/api/v1/ws": 35,
    "/api/v1/analytics": 40,
    "/api/v1/admin": 50,
    "/healthz": 0,
    "/readyz": 0,
    "/livez": 0,
    "/metrics": 0,
}

# Load shedding thresholds
_LOAD_THRESHOLD_HIGH = float(os.getenv("LOAD_SHED_HIGH_THRESHOLD", "0.95"))
_LOAD_THRESHOLD_MEDIUM = float(os.getenv("LOAD_SHED_MEDIUM_THRESHOLD", "0.85"))

# Track CPU/memory proxy — DB pool utilization
_db_pool_utilization: float = 0.0
_pgbouncer_queue_depth: int = 0
_pg_latency_p99: float = 0.0


def set_db_pool_utilization(pct: float):
    global _db_pool_utilization
    _db_pool_utilization = pct


def _get_priority(request: Request) -> int:
    path = request.url.path
    for prefix, priority in _PRIORITY_TIERS.items():
        if path.startswith(prefix):
            return priority
    return 50  # default lowest priority


class LoadSheddingMiddleware(BaseHTTPMiddleware):
    """Adaptive load shedding by request priority.

    Under high load (DB pool >85%), shed lowest-priority requests first.
    Under critical load (>95%), shed all but billing and health endpoints.
    Uses PgBouncer queue depth as a proactive signal to start shedding
    earlier when the queue is building.
    """

    async def dispatch(self, request: Request, call_next):
        global _db_pool_utilization, _pgbouncer_queue_depth

        priority = _get_priority(request)

        # Always allow health checks
        if priority == 0:
            return await call_next(request)

        # Proactive shedding: start shedding at lower thresholds when
        # PgBouncer queue depth is positive or P99 latency is elevated
        active_medium_threshold = _LOAD_THRESHOLD_MEDIUM
        active_high_threshold = _LOAD_THRESHOLD_HIGH
        if _pgbouncer_queue_depth > 5:
            active_medium_threshold = 0.70
        if _pg_latency_p99 > 0.5:
            active_medium_threshold = min(active_medium_threshold, 0.60)

        # Critical load: only allow billing and billing-related
        if _db_pool_utilization >= active_high_threshold and priority > 10:
            logger.warning(
                "Load shedding (%.0f%% pool, PgBouncer queue=%d) — rejecting priority %d request: %s",
                _db_pool_utilization * 100,
                _pgbouncer_queue_depth,
                priority,
                request.url.path,
            )
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter("workticket_requests_shed_total", {"priority": str(priority)})
            except Exception:
                logger.debug("Load shedding metric increment failed, continuing")
        pass  # nosec B110
            return Response(
                content='{"error": "server_overloaded", "retry_after": 5}',
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "5"},
            )

        # Medium load: shed analytics and admin
        if _db_pool_utilization >= active_medium_threshold and priority >= 40:
            logger.warning(
                "Moderate load (%.0f%% pool, PgBouncer queue=%d) — shedding priority %d request: %s",
                _db_pool_utilization * 100,
                _pgbouncer_queue_depth,
                priority,
                request.url.path,
            )
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter("workticket_requests_shed_total", {"priority": str(priority)})
            except Exception:
                logger.debug("Load shedding metric increment failed, continuing")
        pass  # nosec B110
            return Response(
                content='{"error": "server_overloaded", "retry_after": 2}',
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "2"},
            )

        return await call_next(request)

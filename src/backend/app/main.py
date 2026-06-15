import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.ai.audit import cleanup_old_audit_logs as _cleanup_old_audit_logs
from app.ai.gateway import gateway
from app.ai.router import router as ai_router
from app.analytics.events import cleanup_old_analytics_events as _cleanup_old_analytics_events
from app.analytics.router import router as analytics_router
from app.auth.compliance import router as compliance_router
from app.auth.router import router as auth_router
from app.billing.dlq_router import router as dlq_router
from app.billing.router import router as billing_router
from app.config import FeatureFlags, get_settings
from app.estimates.router import router as estimates_router
from app.exceptions import setup_exception_handlers
from app.integrations.router import router as integrations_router
from app.jobs.router import router as jobs_router
from app.logging_config import setup_logging
from app.media.router import router as media_router
from app.middleware.tracing import tracing_middleware
from app.notifications.router import router as notifications_router
from app.quotes.router import router as quotes_router
from app.tracing.models import cleanup_old_traces as _cleanup_old_traces
from app.tracing.router import router as tracing_router

setup_logging("workticket-backend")
logger = logging.getLogger(__name__)
settings = get_settings()
feature_flags = FeatureFlags()

if not settings.debug:
    import os as _os

    if _os.environ.get("DEBUG", "").lower() in ("true", "1", "yes"):
        logger.critical("DEBUG=true detected in production mode — refusing to start")
        raise SystemExit(1)


# Global variables for tracking active connections and tasks
# C5-FIX: Use a context manager for _active_requests that guarantees
# exactly-once decrement, preventing the counter from going negative
# when middleware earlier in the chain raises before this middleware runs.
_active_requests = 0
_active_requests_lock = asyncio.Lock()
_active_websockets: set[object] = set()
_active_websockets_lock = asyncio.Lock()
_shutdown_event = asyncio.Event()
_shutting_down = False


class _ActiveRequestTracker:
    """Context manager that guarantees exactly-once increment/decrement of _active_requests.

    Prevents the race where a request fails before the middleware increment
    completes (e.g., a middleware earlier in the chain raises), which would
    cause the finally block to decrement a counter that was never incremented.
    """

    def __init__(self):
        self._token = None

    async def __aenter__(self):
        global _shutting_down, _active_requests
        async with _active_requests_lock:
            if _shutting_down:
                self._token = None
                return self
            _active_requests += 1
            self._token = 1
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        global _active_requests
        if self._token is not None:
            async with _active_requests_lock:
                _active_requests = max(0, _active_requests - 1)
            self._token = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting WorkTicket API")
    if settings.ai_disabled:
        logger.info("AI Mode: DISABLED (manual-first v1)")
    else:
        logger.info("AI Mode: LOCAL ONLY (Ollama + faster-whisper)")
        logger.info("Text model: %s | Vision model: %s", settings.ollama_text_model, settings.ollama_vision_model)
        logger.info("Whisper model: %s", settings.whisper_model_size)

    _failures = []

    async def _run_with_timeout(coro, name: str, timeout: float = 30.0):
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError:
            logger.warning("Startup cleanup %s timed out after %.0fs", name, timeout)
            _failures.append((name, TimeoutError(f"{name} timed out")))
        except Exception as e:
            logger.warning("Startup cleanup %s failed: %s", name, e)
            _failures.append((name, e))

    # MEDIUM-1 FIX: Add global timeout for all startup cleanup tasks
    # to prevent startup delays beyond 45s
    cleanup_tasks = [
        _run_with_timeout(_cleanup_old_audit_logs(), "audit_logs"),
        _run_with_timeout(_cleanup_old_analytics_events(), "analytics_events"),
        _run_with_timeout(_cleanup_old_traces(), "execution_traces"),
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*cleanup_tasks, return_exceptions=True),
            timeout=45.0,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Startup cleanup task failed: %s", r)
    except TimeoutError:
        logger.warning("Startup cleanup global timeout after 45s — proceeding with startup")

    # V2-FIX: Initialize OpenTelemetry (if configured)
    from app.database import engine
    from app.telemetry import setup_otel

    try:
        setup_otel(app=app, engine=engine)
    except Exception as e:
        logger.warning("OpenTelemetry setup failed: %s", e)

    # V2-FIX: Wait for PostgreSQL before proceeding with startup
    for i in range(30):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            break
        except Exception as e:
            logger.debug("PostgreSQL not available (attempt %d): %s", i + 1, e)
            if i == 29:
                logger.critical("PostgreSQL not available after 30 attempts")
            await asyncio.sleep(1)

    # MED-3: Validate tenant isolation table coverage at startup
    try:
        from app.database import _validate_tenant_scoped_tables

        drift_count = await asyncio.wait_for(_validate_tenant_scoped_tables(engine), timeout=15.0)
        if drift_count > 0:
            logger.critical("Tenant isolation drift detected: %d untracked tables", drift_count)
    except TimeoutError:
        logger.warning("Tenant isolation table scan timed out after 15s")
    except Exception as e:
        logger.error("Tenant isolation table scan failed: %s", e)

    # V2-FIX: Wait for Redis before proceeding with startup
    for i in range(30):
        try:
            from app.redis import get_redis

            r = await get_redis()
            if r:
                await r.ping()  # type: ignore[attr-defined]
            break
        except Exception as e:
            logger.debug("Redis not available (attempt %d): %s", i + 1, e)
            if i == 29:
                logger.critical("Redis not available after 30 attempts")
            await asyncio.sleep(1)

    for _retry in range(3):
        try:
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as cleanup_db:
                from app.billing.state_machine import cleanup_stale_reservations

                await asyncio.wait_for(cleanup_stale_reservations(cleanup_db), timeout=30.0)
            break
        except TimeoutError:
            _failures.append(("db_startup", TimeoutError("DB startup cleanup timed out")))
            if _retry < 2:
                logger.warning("Failed to cleanup stale reservations on startup (attempt %d/3): timeout", _retry + 1)
                await asyncio.sleep(1.0 * (_retry + 1))
            else:
                logger.warning("Failed to cleanup stale reservations after 3 attempts: timeout")
        except Exception as e:
            _failures.append(("db_startup", e))
            if _retry < 2:
                logger.warning("Failed to cleanup stale reservations on startup (attempt %d/3): %s", _retry + 1, e)
                await asyncio.sleep(1.0 * (_retry + 1))
            else:
                logger.warning("Failed to cleanup stale reservations after 3 attempts: %s", e)

    if not settings.ai_disabled:
        try:
            ai_health = await gateway.health()
            if ai_health.get("ollama_available"):
                logger.info("Ollama available with models: %s", ai_health["text_service"].get("models", []))
            else:
                logger.warning(
                    "Ollama not reachable at %s - AI text/vision features will use fallback", settings.ollama_base_url
                )
            if ai_health.get("whisper_available"):
                logger.info("Whisper service available: %s", ai_health["audio_service"].get("model_size", "unknown"))
            else:
                logger.warning(
                    "Whisper service not reachable at %s - audio transcription will use fallback",
                    settings.whisper_service_url,
                )
            logger.info(
                "LLM circuit: %s | Whisper circuit: %s",
                ai_health["gateway"]["llm_circuit_state"],
                ai_health["gateway"]["whisper_circuit_state"],
            )
        except Exception as e:
            _failures.append(("ai_health", e))
            logger.warning("AI health check failed: %s", e)
    else:
        logger.info("AI health check skipped — AI is disabled")

    if settings.stripe_secret_key:
        try:
            from app.billing.stripe_ips import get_cached_network_count, refresh_stripe_ips

            await asyncio.wait_for(refresh_stripe_ips(force=True), timeout=30.0)
            count = get_cached_network_count()
            logger.info("Stripe IP cache populated on startup: %d networks", count)
        except TimeoutError:
            logger.warning("Stripe IP refresh timed out after 30s on startup")
        except Exception as e:
            logger.warning("Failed to refresh Stripe IPs on startup: %s", e)

    # SCALABILITY: Initialize Redis tenant key namespace monitor
    try:
        from app.monitoring.redis_tenants import tenant_redis_monitor

        tenant_redis_monitor.scan_tenant_key_counts()
        logger.info("Redis tenant key namespace monitor initialized")
    except Exception as e:
        logger.warning("Failed to initialize Redis tenant monitor: %s", e)

    # S2 FIX: Validate Vault connectivity and enforced secrets at startup
    try:
        from app.secrets.vault import validate_vault_startup

        await asyncio.wait_for(validate_vault_startup(), timeout=15.0)
    except TimeoutError:
        logger.warning("Vault startup validation timed out after 15s")
    except RuntimeError as vault_err:
        logger.critical("Vault startup validation FAILED: %s", vault_err)
        if not settings.debug:
            raise
        logger.warning("Continuing in debug mode despite Vault validation failure")
    except Exception as e:
        logger.warning("Vault startup validation skipped: %s", e)

    if settings.sentry_dsn and settings.sentry_dsn != "__REQUIRED__":
        try:
            import sentry_sdk
            from sentry_sdk.integrations.celery import CeleryIntegration

            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                traces_sample_rate=0.1,
                integrations=[CeleryIntegration()],
            )
            logger.info("Sentry initialized with Celery integration")
        except ImportError:
            logger.warning("sentry_sdk not installed, skipping Sentry init")
        except Exception:
            logger.warning("Sentry init failed, skipping Sentry init")
    if settings.posthog_api_key:
        try:
            from posthog import Posthog

            _posthog = Posthog(settings.posthog_api_key, host=settings.posthog_host)
            _posthog.capture("server_started", distinct_id="server")
            logger.info("PostHog initialized")
        except ImportError:
            logger.warning("posthog not installed, skipping PostHog init")
    if not settings.clerk_jwt_issuer:
        logger.warning("CLERK_JWT_ISSUER not configured - authentication endpoints will return 401")
    if not settings.r2_access_key_id:
        logger.warning("R2 credentials not configured - file upload URLs will be mocked")

    cors_origins_list = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    for origin in cors_origins_list:
        if "*" in origin:
            logger.warning("CORS origin contains wildcard: %s", origin)
        url_pattern = re.compile(r"^https?://[^\s/$.?#].[^\s]*$")
        if not url_pattern.match(origin):
            logger.warning("CORS origin may be invalid URL format: %s", origin)
    if not settings.debug:
        for origin in cors_origins_list:
            if "localhost" in origin or "127.0.0.1" in origin:
                logger.debug("CORS origin contains localhost in production mode: %s", origin)

    yield

    # Graceful shutdown sequence
    logger.info("Starting graceful shutdown...")
    _shutdown_event.set()

    # V2-FIX: Wait for load balancer to drain connections (if configured)
    _lb_drain_timeout = float(os.getenv("LB_DRAIN_TIMEOUT", "5.0"))
    if _lb_drain_timeout > 0:
        logger.info("Waiting %.1fs for load balancer to drain connections...", _lb_drain_timeout)
        await asyncio.sleep(_lb_drain_timeout)

    # Wait for active requests to complete (with timeout)
    # MEDIUM-5 FIX: Increased shutdown timeout and made configurable.
    # AI tasks can take up to 240s (task_soft_time_limit), so the old
    # 15s timeout always killed long-running tasks mid-execution.
    # Default increased to 60s, configurable via SHUTDOWN_GRACE_SECONDS.
    # L-5 FIX: Default shutdown timeout increased from 60s to 240s to match
    # task_soft_time_limit, preventing premature killing of long-running AI tasks.
    shutdown_timeout = float(os.getenv("SHUTDOWN_GRACE_SECONDS", "240.0"))
    start_time = time.time()

    while True:
        async with _active_requests_lock:
            _shutting_down = True
            if _active_requests == 0:
                break

        if time.time() - start_time > shutdown_timeout:
            logger.warning(
                "Graceful shutdown timeout after %.1fs, %d requests still active", shutdown_timeout, _active_requests
            )
            break

        await asyncio.sleep(0.1)

    logger.info("All HTTP requests completed, closing WebSocket connections...")

    # Close all WebSocket connections
    async with _active_websockets_lock:
        websockets_to_close = list(_active_websockets)
        _active_websockets.clear()

    await asyncio.gather(
        *[ws.close(code=1000, reason="Server shutdown") for ws in websockets_to_close],  # type: ignore[attr-defined]
        return_exceptions=True,
    )

    # Give time for WebSocket close frames to be sent
    await asyncio.sleep(0.5)

    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0-beta-ready",
    lifespan=lifespan,
)

setup_exception_handlers(app)


@app.middleware("http")
async def tracing_middleware_layer(request: Request, call_next):
    return await tracing_middleware(request, call_next)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    global _active_requests

    # Reject new requests during shutdown (P1-C)
    if _shutdown_event.is_set():
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": {
                    "code": "SHUTTING_DOWN",
                    "message": "Server is shutting down, please retry",
                    "request_id": str(uuid.uuid4()),
                },
            },
        )

    # C5-FIX: Use _ActiveRequestTracker context manager that guarantees
    # exactly-once increment/decrement, preventing the counter from going
    # negative when middleware earlier in the chain raises.
    async with _ActiveRequestTracker():
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        correlation_id = getattr(request.state, "correlation_id", request_id)
        request.state.request_id = request_id

        logger.debug(
            "Request started: method=%s path=%s trace_id=%s",
            request.method,
            request.url.path,
            getattr(request.state, "trace_id", "N/A"),
        )

        response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = getattr(request.state, "trace_id", "")
        response.headers["X-Span-ID"] = getattr(request.state, "span_id", "")
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'self'; require-trusted-types-for 'script'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # M-1 FIX: Set Cache-Control on ALL responses, not just API v1.
        # This prevents CDNs and proxies from caching AI-generated estimates,
        # quotes, job data, or health check responses.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response


@app.middleware("http")
async def request_timeout_middleware(request: Request, call_next):
    timeout = 300.0 if request.url.path.endswith("/webhook") else 120.0
    try:
        response = await asyncio.wait_for(call_next(request), timeout=timeout)
        return response
    except TimeoutError:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": {
                    "code": "REQUEST_TIMEOUT",
                    "message": "Request timed out",
                    "request_id": request.state.request_id,
                },
            },
        )


cors_origins_list = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID", "X-Request-ID", "Idempotency-Key"],
)

from app.middleware.rate_limit import RateLimitMiddleware  # noqa: E402

app.add_middleware(RateLimitMiddleware)
from app.middleware.body_size import RequestBodySizeMiddleware  # noqa: E402

app.add_middleware(RequestBodySizeMiddleware)
from app.middleware.csrf import CSRFProtectionMiddleware  # noqa: E402

app.add_middleware(CSRFProtectionMiddleware)
from app.middleware.sanitize import AIResponseSanitizationMiddleware  # noqa: E402

app.add_middleware(AIResponseSanitizationMiddleware)

# Global RBAC enforcement based on OpenAPI route tags
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from app.auth.authorize import enforce_route_rbac  # noqa: E402
from app.auth.dependencies import get_current_user  # noqa: E402


class RBACEnforcementMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith(settings.api_v1_prefix) and not any(
            path.startswith(p) for p in ["/health", "/livez", "/readyz", "/healthz", "/docs", "/openapi.json", "/redoc"]
        ):
            try:
                user = await get_current_user(request)
                request.state.current_user = user
                await enforce_route_rbac(request, current_user=user)
            except HTTPException:
                raise
            except Exception:
                return await call_next(request)
        return await call_next(request)


app.add_middleware(RBACEnforcementMiddleware)
from app.database import _pool_utilization  # noqa: E402
from app.middleware.priority import LoadSheddingMiddleware, set_db_pool_utilization  # noqa: E402


@app.middleware("http")
async def update_load_state(request, call_next):
    set_db_pool_utilization(_pool_utilization())
    return await call_next(request)


app.add_middleware(LoadSheddingMiddleware)

from app.monitoring.latency import latency_middleware  # noqa: E402


@app.middleware("http")
async def latency_tracking_middleware(request: Request, call_next):
    return await latency_middleware(request, call_next)


from app.database import get_n1_warnings, reset_n1_counter  # noqa: E402


@app.middleware("http")
async def n1_detection_middleware(request: Request, call_next):
    reset_n1_counter()
    response = await call_next(request)
    warnings = get_n1_warnings()
    if warnings:
        logging.getLogger(__name__).warning("N+1 query patterns detected: %s", warnings)
    return response


_api_prefix = settings.api_v1_prefix
app.include_router(auth_router, prefix=f"{_api_prefix}/auth", tags=["auth", "public"])
app.include_router(jobs_router, prefix=f"{_api_prefix}/jobs", tags=["jobs", "staff"])
app.include_router(media_router, prefix=f"{_api_prefix}/media", tags=["media", "staff"])
app.include_router(ai_router, prefix=f"{_api_prefix}/ai", tags=["ai", "staff"])
app.include_router(quotes_router, prefix=f"{_api_prefix}/quotes", tags=["quotes", "staff"])
app.include_router(billing_router, prefix=f"{_api_prefix}/billing", tags=["billing", "staff"])
app.include_router(dlq_router, prefix=f"{_api_prefix}/billing", tags=["dlq", "admin"])
app.include_router(estimates_router, prefix=f"{_api_prefix}/estimates", tags=["estimates", "staff"])
app.include_router(notifications_router, prefix=f"{_api_prefix}/notifications", tags=["notifications", "staff"])
app.include_router(analytics_router, prefix=f"{_api_prefix}/analytics", tags=["analytics", "staff"])
app.include_router(tracing_router, prefix=f"{_api_prefix}/tracing", tags=["tracing", "staff"])
app.include_router(compliance_router, prefix=f"{_api_prefix}/compliance", tags=["compliance", "public"])
app.include_router(integrations_router, prefix=f"{_api_prefix}", tags=["integrations", "staff"])

# LOW-2 FIX: Health router included at both root (backward compat) and
# under api_v1_prefix so all API endpoints are version-prefixed.
health_router = APIRouter()


@health_router.get("/beta-gate")
async def beta_gate_check():
    from app.billing.beta_gate import beta_gate

    passed, results = beta_gate.can_deploy()
    return {
        "beta_ready": passed,
        "checks": results,
        "version": "1.0.0-beta-ready",
    }


@health_router.get("/livez")
async def liveness_process():
    """Lightweight liveness probe that only checks process liveness (no deps)."""
    return {"status": "alive", "timestamp": datetime.now(UTC).isoformat()}


@health_router.get("/healthz")
async def healthz():
    """Health check with Redis connectivity check.

    H5-FIX: Uses the shared Redis connection pool instead of creating a new
    connection per health check. Previously created a new Redis connection
    on every check, causing unnecessary connection churn and spiking Redis
    connection count during degradation.

    LATENCY-FIX: Now includes response time tracking for SLO monitoring
    of health endpoints.
    """
    import time as _time

    _start = _time.monotonic()
    try:
        from app.monitoring.prometheus import increment_counter

        increment_counter("workticket_healthz_requests_total", {})
    except Exception as _e:
        logger.debug("Failed to increment healthz counter: %s", _e)

    redis_ok = False
    try:
        from app.redis import get_redis

        r = await get_redis()
        if r:
            await asyncio.wait_for(r.ping(), timeout=2)  # type: ignore[attr-defined]
            redis_ok = True
    except Exception as _e:
        logger.debug("Healthz Redis check failed: %s", _e)

    db_status = "ok"
    try:
        from sqlalchemy import text

        from app.database import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.debug("Healthz DB check failed: %s", e)
        db_status = "failed"
    celery_ok = False
    try:
        from celery_app import celery_app as _celery_app

        inspector = _celery_app.control.inspect(timeout=2)
        workers = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, inspector.ping),
            timeout=3,
        )
        celery_ok = workers is not None and len(workers) > 0
    except Exception as _e:
        logger.debug("Healthz Celery check failed: %s", _e)

    overall = "ok" if redis_ok and db_status == "ok" and celery_ok else "degraded"

    try:
        from app.monitoring.prometheus import Gauge  # type: ignore[attr-defined]

        _healthz_gauge = Gauge(
            "workticket_healthz_status",
            "Health check status (1=ok, 0=degraded)",
        )
        _healthz_gauge.set(1 if overall == "ok" else 0)
    except Exception as _e:
        logger.debug("Failed to set healthz gauge: %s", _e)

    _elapsed_ms = round((_time.monotonic() - _start) * 1000, 2)

    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={
            "status": overall,
            "redis": "ok" if redis_ok else "failed",
            "database": db_status,
            "celery": "ok" if celery_ok else "degraded",
            "response_time_ms": _elapsed_ms,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


@health_router.get("/readyz")
async def readiness():
    try:
        from app.monitoring.prometheus import increment_counter

        increment_counter("workticket_readyz_requests_total", {})
    except Exception as _e:
        logger.debug("Failed to increment readyz counter: %s", _e)
    # CRITICAL-3 FIX: Check circuit breaker before any DB operations
    try:
        from app.database import _check_db_circuit

        _check_db_circuit()
    except Exception as cb_err:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": f"Circuit breaker open: {cb_err}",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    components = {}

    # Use pooled connection from engine (not opening new ones) for health checks
    db_status = "ok"
    db_pool = {}
    try:
        from app.database import _POOL_CIRCUIT_OPEN, engine, get_db_pool_metrics

        db_pool = get_db_pool_metrics()
        # Check circuit breaker first — avoids unnecessary pool contention
        if _POOL_CIRCUIT_OPEN:
            db_status = "degraded"
            db_pool["circuit_breaker"] = "open"
        else:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as e:
        db_status = "failed"
        db_pool = {"error": str(e)}

    components["database"] = {"status": db_status, "pool": db_pool}

    # CRITICAL-3 FIX: Use pooled connection from engine (not creating new ones)
    # Removed asyncpg bypass that created connections outside the pool.
    # Pgbouncer status is inferred from database pool check above.
    components["pgbouncer"] = {"status": db_status, "note": "via_pool"}

    redis_status = "ok"
    try:
        from app.redis import get_redis

        r = await get_redis()
        if r:
            await asyncio.wait_for(r.ping(), timeout=2)  # type: ignore[attr-defined]
        else:
            redis_status = "failed"
    except Exception as _e:
        logger.debug("Readyz Redis check failed: %s", _e)
        redis_status = "failed"
    components["redis"] = {"status": redis_status}

    redis_tenant_status = "ok"
    try:
        from app.monitoring.redis_tenants import tenant_redis_monitor

        tenant_redis_monitor.scan_tenant_key_counts()
        tenant_stats = tenant_redis_monitor.get_stats()
        if tenant_stats["tenants_over_limit"] > 0:
            redis_tenant_status = "degraded"
        components["redis_tenants"] = {
            "status": redis_tenant_status,
            **tenant_stats,
        }
    except Exception as _e:
        logger.debug("Readyz Redis tenant check failed: %s", _e)
        components["redis_tenants"] = {"status": "unknown"}

    sync_redis_pool_status = "ok"
    try:
        from app.sync_redis_pool import get_sync_redis_pool_stats

        pool_stats = get_sync_redis_pool_stats()
        if not pool_stats.get("available", False):
            sync_redis_pool_status = "degraded"
        components["sync_redis_pool"] = {"status": sync_redis_pool_status, "pool": pool_stats}
    except Exception as _e:
        logger.debug("Readyz sync Redis pool check failed: %s", _e)
        components["sync_redis_pool"] = {"status": "unknown"}

    ai_status = "ok"
    if settings.ai_disabled:
        components["ai_gateway"] = {"status": "ok", "note": "ai_disabled"}
    else:
        try:
            ai_health = await asyncio.wait_for(gateway.health(), timeout=5)
            if not ai_health.get("ollama_available"):
                ai_status = "degraded"
            if not ai_health.get("whisper_available"):
                ai_status = "degraded"
            components["ai_gateway"] = {
                "status": ai_status,
                "ollama": ai_health.get("ollama_available", False),
                "whisper": ai_health.get("whisper_available", False),
                "llm_circuit": ai_health.get("gateway", {}).get("llm_circuit_state", "unknown"),
                "whisper_circuit": ai_health.get("gateway", {}).get("whisper_circuit_state", "unknown"),
            }
        except TimeoutError:
            components["ai_gateway"] = {"status": "degraded", "error": "health_check_timeout"}
        except Exception as e:
            logger.debug("Readyz AI gateway check failed: %s", e)
            components["ai_gateway"] = {"status": "failed"}

    celery_status = "ok"
    celery_queue_depth = {}
    try:
        from celery_app import celery_app as _celery

        inspector = _celery.control.inspect(timeout=2)
        workers = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, inspector.ping),
            timeout=3,
        )
        if not workers or len(workers) == 0:
            celery_status = "ok"
            workers = {}
        # Queue depth via Redis
        try:
            import redis as _sync_redis

            broker_url = getattr(settings, "redis_broker_url", "") or settings.redis_url
            _r = _sync_redis.from_url(broker_url, socket_connect_timeout=1)
            for q in ("default", "ai_text", "ai_audio", "ai_image", "beat"):
                qlen = _r.llen(q)
                if qlen > 0:  # type: ignore[operator]
                    celery_queue_depth[q] = qlen
            _r.close()
        except Exception as _e:
            logger.debug("Readyz queue depth check failed: %s", _e)
    except TimeoutError:
        celery_status = "ok"
    except Exception as _e:
        logger.debug("Readyz Celery check failed: %s", _e)
        celery_status = "ok"
    components["celery"] = {"status": celery_status}
    if celery_queue_depth:
        components["celery"]["queue_depth"] = celery_queue_depth

    stripe_ip_status = "ok"
    try:
        from app.billing.stripe_ips import get_cached_network_count, get_last_fetch_age

        count = get_cached_network_count()
        age = get_last_fetch_age()
        if count == 0:
            stripe_ip_status = "degraded"
        components["stripe_ip_cache"] = {
            "status": stripe_ip_status,
            "network_count": count,
            "age_seconds": round(age, 1),
        }
    except Exception as _e:
        logger.debug("Readyz stripe_ip_cache check failed: %s", _e)
        components["stripe_ip_cache"] = {"status": "unknown"}

    rl_mode = "redis"
    try:
        from app.ai.rate_limiter import rate_limiter as _rl

        if _rl.fallback_active:
            rl_mode = "local"
        elif not _rl.redis_available:
            rl_mode = "failed"
        components["rate_limiter"] = {
            "status": "ok",
            "mode": rl_mode,
            "circuit_breaker": _rl.circuit_breaker_state,
        }
    except Exception as _e:
        logger.debug("Readyz rate_limiter check failed: %s", _e)
        components["rate_limiter"] = {"status": "unknown"}

    # Retry storm status
    try:
        from app.tasks.retry_guard import get_storm_status

        storm_status = get_storm_status()
        if storm_status.get("active_storms"):
            components["retry_storm_guard"] = {"status": "degraded", "storms": storm_status["active_storms"]}
        else:
            components["retry_storm_guard"] = {"status": "ok", "storms": []}
    except Exception as _e:
        logger.debug("Readyz retry_storm_guard check failed: %s", _e)
        components["retry_storm_guard"] = {"status": "unknown"}

    # WebSocket enabled check
    _ws_enabled = os.getenv("WS_ENABLED", "true").lower() in ("true", "1", "yes")
    components["ws_enabled"] = {"status": "ok" if _ws_enabled else "warning", "enabled": _ws_enabled}

    # Canary deployment status (P1+P2)
    try:
        from app.deploy.canary import get_canary_status

        cs = get_canary_status()
        canary_status = "ok"
        if cs.rollback_triggered:
            canary_status = "degraded"
        components["canary_deploy"] = {
            "status": canary_status,
            "enabled": cs.enabled,
            "stage": cs.stage,
            "weight_pct": cs.weight_pct,
            "version": cs.version,
            "rollback_triggered": cs.rollback_triggered,
            "rollback_reason": cs.rollback_reason,
        }
    except Exception as _e:
        logger.debug("Readyz canary deploy check failed: %s", _e)
        components["canary_deploy"] = {"status": "unknown"}

    # Beat task execution timestamp check
    try:
        from app.monitoring.prometheus import _beat_task_execution_gauge

        if _beat_task_execution_gauge:
            import time as _t

            now = _t.time()
            stale_tasks = []
            for task_name in ("reset_billing_quotas", "collect_billing_debt", "scan_for_stalled_ai_jobs"):
                try:
                    val = _beat_task_execution_gauge.labels(task=task_name)
                    if val and hasattr(val, "_value"):
                        last = float(val._value)
                        if now - last > 3600:
                            stale_tasks.append(task_name)
                except Exception:
                    logger.debug("Failed to read beat task execution gauge for %s", task_name)
                    pass  # nosec B110
            if stale_tasks:
                components["beat_tasks"] = {"status": "degraded", "stale": stale_tasks}
            else:
                components["beat_tasks"] = {"status": "ok"}
        else:
            components["beat_tasks"] = {"status": "ok", "note": "gauge_not_registered"}
    except Exception as _e:
        logger.debug("Readyz beat tasks check failed: %s", _e)
        components["beat_tasks"] = {"status": "unknown"}

    # HIGH-6 FIX: Removed custom counters from /readyz response to prevent
    # exposing operational metrics through unauthenticated endpoints.
    # Prometheus metrics are available at /admin/metrics with token auth.

    overall = all(c.get("status") == "ok" for c in components.values())
    http_status = 200 if overall else 503

    import json as _json

    def _safe_default(o):
        if isinstance(o, set):
            return list(o)
        raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

    try:
        body = _json.dumps(
            {
                "status": "ok" if overall else "degraded",
                "timestamp": datetime.now(UTC).isoformat(),
                "version": "1.0.0-beta-ready",
                "components": components,
            },
            default=_safe_default,
        ).encode("utf-8")
    except Exception as serialization_err:
        logger.error("Readyz serialization failed: %s", serialization_err)
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": f"Serialization error: {serialization_err}",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    from starlette.responses import Response

    return Response(
        content=body,
        status_code=http_status,
        media_type="application/json",
    )


@health_router.get("/health")
async def health():
    # M-2 FIX: Rate-limited by RateLimitMiddleware (3/min) to prevent abuse
    ai_health = await gateway.health()

    celery_healthy = False
    try:
        from celery_app import celery_app as _celery

        inspector = _celery.control.inspect(timeout=2)
        workers = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, inspector.ping),
            timeout=3,
        )
        celery_healthy = workers is not None and len(workers) > 0
    except Exception as _e:
        logger.debug("Health endpoint Celery check failed: %s", _e)
        celery_healthy = False

    db_healthy = False
    db_pool = {}
    # CRITICAL-3 FIX: Check circuit breaker before attempting DB connection
    try:
        from app.database import _check_db_circuit

        _check_db_circuit()
    except Exception as cb_err:
        return {
            "status": "degraded",
            "db_healthy": False,
            "reason": f"Circuit breaker open: {cb_err}",
            "timestamp": datetime.now(UTC).isoformat(),
        }
    try:
        from app.database import engine, get_db_pool_metrics

        db_pool = get_db_pool_metrics()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            db_healthy = True
    except Exception as e:
        db_pool = {"error": str(e)}

    celery_queue_depth = {}
    try:
        import redis as _sync_redis

        settings = get_settings()
        broker_url = getattr(settings, "redis_broker_url", "") or settings.redis_url
        _r = _sync_redis.from_url(broker_url, socket_connect_timeout=1)
        for q in ("default", "ai_text", "ai_audio", "ai_image", "beat"):
            qlen = _r.llen(q)
            if qlen > 0:  # type: ignore[operator]
                celery_queue_depth[q] = qlen
        _r.close()
    except Exception:
        logger.debug("Failed to read Celery queue depths from Redis for health endpoint")
        pass  # nosec B110

    status = "ok" if db_healthy else "degraded"

    return {
        "status": status,
        "db_healthy": db_healthy,
        "timestamp": datetime.now(UTC).isoformat(),
        "ai_mode": "local-only",
        "ollama_available": ai_health.get("ollama_available", False),
        "whisper_available": ai_health.get("whisper_available", False),
        "celery_worker_healthy": celery_healthy,
        "celery_queue_depth": celery_queue_depth,
        "db_pool": db_pool,
    }


@health_router.get("/slo")
async def slo_summary():
    """SLO/SLI summary endpoint for operational monitoring."""
    try:
        from app.monitoring.latency import get_slo_summary
        from app.monitoring.slo import ERROR_BUDGETS, RPO, RTO, SLIS, SLOS

        perf = get_slo_summary()
        return {
            "status": "ok",
            "timestamp": datetime.now(UTC).isoformat(),
            "performance": perf,
            "objectives": {
                "rto": {"target": RTO.target, "description": RTO.description},
                "rpo": {"target": RPO.target, "description": RPO.description},
            },
            "slis": [{"name": s.name, "target": s.target, "unit": s.unit, "window": s.window} for s in SLIS],
            "slos": [{"name": s.name, "target": s.target, "description": s.description} for s in SLOS],
            "error_budgets": ERROR_BUDGETS,
        }
    except Exception as e:
        logger.error("SLO summary failed: %s", e)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal error retrieving SLO summary"},
        )


@health_router.get("/slo/endpoints")
async def slo_endpoint_details():
    """Per-endpoint SLO details."""
    try:
        from app.monitoring.latency import get_endpoint_slo_details

        return {
            "status": "ok",
            "timestamp": datetime.now(UTC).isoformat(),
            "endpoints": get_endpoint_slo_details(),
        }
    except Exception as e:
        logger.error("SLO endpoint details failed: %s", e)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal error retrieving SLO endpoint details"},
        )


# LOW-2 FIX: Include health router at both root (for k8s probe backward compat)
# and under api_v1_prefix (for versioned API consumers).
app.include_router(health_router)
app.include_router(health_router, prefix=settings.api_v1_prefix)

try:
    from app.monitoring.prometheus import setup_prometheus

    setup_prometheus(app)
    logger.info("Prometheus metrics endpoint registered")
except Exception as e:
    logger.warning("Failed to setup Prometheus metrics: %s", e)

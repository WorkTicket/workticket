import hmac
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_meter_definitions: dict[str, Any] = {}
_registry_instrumentator = None

# --- Custom metric providers ---


def _safe_gauge(callable, default=0):
    """Wrap a gauge lambda with try/except to prevent silent metric disappearance (M10)."""

    def _wrapped():
        try:
            return callable()
        except Exception as e:
            logger.debug("Gauge lambda failed: %s", e)
            return default

    return _wrapped


_db_circuit_cooldown_gauge = None


def _register_db_pool_metrics(registry):
    """Register DB pool utilization and circuit breaker metrics."""
    global _db_circuit_cooldown_gauge
    try:
        from prometheus_client import Gauge

        from app.database import _POOL_CIRCUIT_OPEN, _pool_utilization, get_db_pool_metrics

        Gauge(
            "workticket_db_pool_size",
            "Database connection pool size",
            registry=registry,
        ).set_function(_safe_gauge(lambda: get_db_pool_metrics().get("size", 0)))

        Gauge(
            "workticket_db_pool_checkedout",
            "Database connections currently checked out",
            registry=registry,
        ).set_function(_safe_gauge(lambda: get_db_pool_metrics().get("checkedout", 0)))

        Gauge(
            "workticket_db_pool_utilization_pct",
            "Database pool utilization percentage",
            registry=registry,
        ).set_function(_safe_gauge(lambda: _pool_utilization() * 100))

        Gauge(
            "workticket_db_pool_circuit_breaker",
            "Database circuit breaker state (1=open, 0=closed)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if _POOL_CIRCUIT_OPEN else 0))

        Gauge(
            "workticket_db_pool_overflow",
            "Database pool overflow connections",
            registry=registry,
        ).set_function(_safe_gauge(lambda: get_db_pool_metrics().get("overflow", 0)))

        _db_circuit_cooldown_gauge = Gauge(
            "workticket_db_circuit_cooldown_seconds",
            "Current DB circuit breaker cooldown in seconds (0 when closed)",
            registry=registry,
        )
    except Exception as e:
        logger.warning("Failed to register DB pool metrics: %s", e)


def _register_db_index_metrics(registry):
    """Register DB index usage statistics from pg_stat_user_indexes.

    Tracks index scan counts and sizes per table to identify:
    - Unused indexes (zero scans over time)
    - Bloated indexes (large size relative to table)
    - Index efficiency (index scans vs sequential scans)
    """
    try:
        from prometheus_client import Gauge

        Gauge(
            "workticket_db_index_scans_total",
            "Total index scans per table",
            registry=registry,
            labelnames=["table_name", "index_name"],
        ).set_function(_safe_gauge(lambda: _get_db_index_metrics()))
        # The set_function approach with labels doesn't work cleanly here,
        # so we use a simpler aggregate gauge per table

        Gauge(
            "workticket_db_index_count",
            "Number of indexes per table",
            registry=registry,
            labelnames=["table_name"],
        ).set_function(_safe_gauge(lambda: _get_db_index_counts()))

        Gauge(
            "workticket_db_unused_indexes_total",
            "Number of indexes with zero scans since last stats reset",
            registry=registry,
        ).set_function(_safe_gauge(lambda: _count_unused_indexes()))
    except Exception as e:
        logger.warning("Failed to register DB index metrics: %s", e)


def _count_unused_indexes() -> int:
    """Query pg_stat_user_indexes for indexes with zero scans."""
    try:
        import asyncio

        from sqlalchemy import text

        from app.database import _get_engine

        async def _query():
            eng = _get_engine()
            async with eng.connect() as conn:
                result = await conn.execute(text("SELECT COUNT(*) FROM pg_stat_user_indexes WHERE idx_scan = 0"))
                row = result.fetchone()
                return row[0] if row else 0

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return 0
            return loop.run_until_complete(_query())
        except RuntimeError:
            return asyncio.run(_query())
    except Exception as e:
        logger.debug("Failed to count unused indexes: %s", e)
        return 0


def _get_db_index_counts() -> dict:
    """Get index counts per table for Prometheus labels."""
    try:
        import asyncio

        from sqlalchemy import text

        from app.database import _get_engine

        async def _query():
            eng = _get_engine()
            async with eng.connect() as conn:
                result = await conn.execute(
                    text("SELECT relname, COUNT(*) FROM pg_stat_user_indexes GROUP BY relname ORDER BY COUNT(*) DESC")
                )
                rows = result.fetchall()
                return {row[0]: row[1] for row in rows}

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return {}
            return loop.run_until_complete(_query())
        except RuntimeError:
            return asyncio.run(_query())
    except Exception as e:
        logger.debug("Failed to get DB index counts: %s", e)
        return {}


def _get_db_index_metrics() -> dict:
    """Get aggregate index metrics."""
    try:
        counts = _get_db_index_counts()
        return sum(counts.values()) if counts else 0
    except Exception as e:
        logger.debug("Failed to get DB index metrics: %s", e)
        return 0


def _set_db_circuit_cooldown(seconds: float):
    if _db_circuit_cooldown_gauge is not None:
        _db_circuit_cooldown_gauge.set(seconds)


def _register_rate_limiter_metrics(registry):
    """Register rate limiter circuit breaker and fallback metrics."""
    try:
        from prometheus_client import Gauge

        from app.ai.rate_limiter import rate_limiter as _rl

        Gauge(
            "workticket_rate_limiter_circuit_breaker",
            "Rate limiter circuit breaker state (1=open, 0=closed)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if _rl.circuit_breaker_state.get("open", False) else 0))

        Gauge(
            "workticket_rate_limiter_fallback_active",
            "Rate limiter fallback mode active (1=yes, 0=no)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if _rl.fallback_active else 0))

        Gauge(
            "workticket_rate_limiter_redis_available",
            "Rate limiter Redis availability (1=available, 0=unavailable)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if _rl.redis_available else 0))
    except Exception as e:
        logger.warning("Failed to register rate limiter metrics: %s", e)


def _register_ai_circuit_metrics(registry):
    """Register AI gateway circuit breaker metrics."""
    try:
        from prometheus_client import Gauge

        from app.ai.gateway import gateway

        Gauge(
            "workticket_ai_gateway_ollama_available",
            "Ollama service availability (1=up, 0=down)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if gateway._ollama_available else 0))

        Gauge(
            "workticket_ai_gateway_whisper_available",
            "Whisper service availability (1=up, 0=down)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if gateway._whisper_available else 0))

        Gauge(
            "workticket_ai_gateway_llm_circuit",
            "LLM circuit breaker state (1=open, 0=closed)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if getattr(gateway, "_llm_circuit_state", "closed") == "open" else 0))

        Gauge(
            "workticket_ai_gateway_whisper_circuit",
            "Whisper circuit breaker state (1=open, 0=closed)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: 1 if getattr(gateway, "_whisper_circuit_state", "closed") == "open" else 0))
    except Exception as e:
        logger.warning("Failed to register AI circuit metrics: %s", e)


# HIGH-6 FIX: Single shared sync Redis client singleton for Prometheus metrics.
# Created once and reused across all scrapes to prevent TCP connection exhaustion.
# Uses max_connections=10 with connection retry and backoff.
_metrics_redis_client = None
_metrics_redis_client_url = None
_metrics_redis_client_last_attempt = 0.0
_METRICS_REDIS_RETRY_INTERVAL = 15.0


def _get_metrics_redis():
    """Get or create a shared sync Redis client for Prometheus metrics.

    Uses a single module-level singleton client with max_connections=10.
    Retries connection with 15s backoff on failure.
    """
    global _metrics_redis_client, _metrics_redis_client_url, _metrics_redis_client_last_attempt
    import os
    import time as _time

    import redis as _sync_redis

    broker_url = os.getenv("REDIS_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    now = _time.time()

    if _metrics_redis_client is not None and _metrics_redis_client_url == broker_url:
        try:
            _metrics_redis_client.ping()
            return _metrics_redis_client
        except Exception as e:
            logger.debug("Metrics Redis ping check failed: %s", e)
            if now - _metrics_redis_client_last_attempt < _METRICS_REDIS_RETRY_INTERVAL:
                return None
            _metrics_redis_client = None

    if now - _metrics_redis_client_last_attempt < _METRICS_REDIS_RETRY_INTERVAL:
        return None

    _metrics_redis_client_last_attempt = now
    try:
        _pool = _sync_redis.ConnectionPool.from_url(
            broker_url,
            socket_connect_timeout=1,
            socket_timeout=2,
            max_connections=10,
            retry_on_timeout=True,
        )
        _metrics_redis_client = _sync_redis.Redis(connection_pool=_pool)
        _metrics_redis_client.ping()
        _metrics_redis_client_url = broker_url
        return _metrics_redis_client
    except Exception as e:
        logger.debug("Failed to create metrics Redis client: %s", e)
        _metrics_redis_client = None
        return None


def _register_redis_metrics(registry):
    """Register Redis broker eviction and health metrics."""
    try:
        from prometheus_client import Gauge

        Gauge(
            "workticket_redis_evicted_keys_total",
            "Total number of evicted keys from Redis broker",
            registry=registry,
        ).set_function(_safe_gauge(lambda: _redis_metric("stats", "evicted_keys", 0)))

        Gauge(
            "workticket_redis_memory_used_bytes",
            "Redis broker memory used in bytes",
            registry=registry,
        ).set_function(_safe_gauge(lambda: _redis_metric("memory", "used_memory", 0)))

        Gauge(
            "workticket_redis_memory_max_bytes",
            "Redis broker max memory in bytes",
            registry=registry,
        ).set_function(_safe_gauge(lambda: _redis_metric("memory", "maxmemory", 0)))

        Gauge(
            "workticket_redis_maxmemory_policy",
            "Redis broker maxmemory policy (1=noeviction, 0=other)",
            registry=registry,
        ).set_function(_safe_gauge(lambda: int(_redis_info_value("maxmemory_policy", "unknown") == "noeviction")))
    except Exception as e:
        logger.warning("Failed to register Redis metrics: %s", e)


def _redis_metric(section: str, key: str, default=0):
    """Fetch a value from Redis INFO with shared pool."""
    try:
        client = _get_metrics_redis()
        if client:
            info = client.info(section)
            return int(info.get(key, default))
    except Exception as e:
        logger.debug("Failed to get Redis metric %s/%s: %s", section, key, e)
    return default


def _redis_info_value(key: str, default=""):
    """Fetch a top-level Redis INFO value with shared pool."""
    try:
        client = _get_metrics_redis()
        if client:
            val = client.info(key)
            return val or default
    except Exception as e:
        logger.debug("Failed to get Redis info value %s: %s", key, e)
    return default


def _register_celery_queue_metrics(registry):
    """Register Celery queue depth metrics via Redis."""
    try:
        from prometheus_client import Gauge

        queues = ["default", "ai_text", "ai_audio", "ai_image", "beat"]

        for queue in queues:
            Gauge(
                f"workticket_celery_queue_depth_{queue}",
                f"Celery queue depth for {queue}",
                registry=registry,
            ).set_function(_safe_gauge(lambda q=queue: _llen_metric(q)))

        Gauge(
            "workticket_celery_queue_depth_total",
            "Total Celery queue depth across all queues",
            registry=registry,
        ).set_function(_safe_gauge(lambda: sum(_llen_metric(q) or 0 for q in queues)))
    except Exception as e:
        logger.warning("Failed to register Celery queue metrics: %s", e)


def _llen_metric(queue: str) -> int:
    """Get Redis LLEN for a queue using shared pool."""
    try:
        client = _get_metrics_redis()
        if client:
            return client.llen(queue) or 0
    except Exception as e:
        logger.debug("Failed to get llen for queue %s: %s", queue, e)
    return 0


def _register_stripe_ip_metrics(registry):
    """Register Stripe IP cache age metric."""
    try:
        from prometheus_client import Gauge

        from app.billing.stripe_ips import get_cached_network_count, get_last_fetch_age

        Gauge(
            "workticket_stripe_ip_cache_count",
            "Number of cached Stripe IP networks",
            registry=registry,
        ).set_function(_safe_gauge(lambda: get_cached_network_count()))

        Gauge(
            "workticket_stripe_ip_cache_age_seconds",
            "Seconds since last Stripe IP refresh",
            registry=registry,
        ).set_function(_safe_gauge(lambda: get_last_fetch_age()))
    except Exception as e:
        logger.warning("Failed to register Stripe IP metrics: %s", e)


# Module-level gauge references (set by _register_workflow_metrics, used by setter functions)
_stuck_jobs_gauge = None
_dlq_entries_gauge = None
_ws_connections_gauge = None
_acu_debt_gauge = None
_backup_timestamp_gauge = None
_dlq_retry_gauge = None
_ws_auth_cache_size_gauge = None
_dlq_fallback_file_size_gauge = None
_ghost_reservations_gauge = None
_retry_guard_depth_gauge = None
_ws_db_poll_concurrent_gauge = None
_ws_db_poll_concurrent_created = False
_r2_circuit_gauge = None


def _register_workflow_metrics(registry):
    """Register workflow-level metrics: stuck jobs, DLQ, ACU debt."""
    global _stuck_jobs_gauge, _dlq_entries_gauge, _ws_connections_gauge
    global _acu_debt_gauge, _backup_timestamp_gauge, _dlq_retry_gauge
    global _ws_auth_cache_size_gauge, _dlq_fallback_file_size_gauge
    global _ghost_reservations_gauge, _retry_guard_depth_gauge
    try:
        import os

        from prometheus_client import Gauge

        _stuck_jobs_gauge = Gauge(
            "workticket_stuck_jobs_total",
            "Total stuck jobs by state (reserved, running, queued)",
            registry=registry,
            labelnames=["state"],
        )

        _dlq_entries_gauge = Gauge(
            "workticket_dlq_entries_total",
            "Total dead letter queue entries",
            registry=registry,
        )

        _ws_connections_gauge = Gauge(
            "workticket_ws_connections_total",
            "Total active WebSocket connections",
            registry=registry,
        )

        _acu_debt_gauge = Gauge(
            "workticket_acu_debt_total",
            "Total ACU debt across all companies",
            registry=registry,
        )

        _backup_timestamp_gauge = Gauge(
            "workticket_backup_last_success_timestamp",
            "Unix timestamp of last successful database backup",
            registry=registry,
        )

        _dlq_retry_gauge = Gauge(
            "workticket_dlq_retry_attempts_total",
            "Total DLQ retry attempts by outcome",
            registry=registry,
            labelnames=["outcome"],
        )

        _ws_auth_cache_size_gauge = Gauge(
            "workticket_ws_auth_cache_size",
            "Current WebSocket auth cache entry count",
            registry=registry,
        )

        _dlq_fallback_file_size_gauge = Gauge(
            "workticket_dlq_fallback_file_size_bytes",
            "Size of DLQ JSONL fallback file in bytes",
            registry=registry,
        )

        _ghost_reservations_gauge = Gauge(
            "workticket_ghost_reservations_total",
            "Accounts with reserved_acu > 0 but no recent heartbeat",
            registry=registry,
        )

        _retry_guard_depth_gauge = Gauge(
            "workticket_retry_guard_tracked_keys",
            "Number of tracked retry guard keys",
            registry=registry,
        )

        _ws_db_poll_concurrent_gauge = Gauge(
            "workticket_ws_db_poll_concurrent",
            "Current concurrent WebSocket DB pollers (aggregated across replicas)",
            registry=registry,
        )

        _r2_circuit_gauge = Gauge(
            "workticket_r2_circuit_breaker",
            "R2/S3 circuit breaker state (1=open, 0=closed)",
            registry=registry,
        )

        Gauge(
            "workticket_ws_enabled",
            "WebSocket feature enabled (1=enabled, 0=disabled)",
            registry=registry,
        ).set_function(lambda: 1 if os.getenv("WS_ENABLED", "true").lower() in ("true", "1", "yes") else 0)
    except Exception as e:
        logger.warning("Failed to register workflow metrics: %s", e)


def set_stuck_jobs(state: str, count: int):
    if _stuck_jobs_gauge is not None:
        _stuck_jobs_gauge.labels(state=state).set(count)


def set_dlq_entries(count: int):
    if _dlq_entries_gauge is not None:
        _dlq_entries_gauge.set(count)


def set_ws_connections(count: int):
    if _ws_connections_gauge is not None:
        _ws_connections_gauge.set(count)


def set_acu_debt(total: float):
    if _acu_debt_gauge is not None:
        _acu_debt_gauge.set(total)


def set_backup_timestamp(ts: float):
    if _backup_timestamp_gauge is not None:
        _backup_timestamp_gauge.set(ts)


def inc_dlq_retry(outcome: str):
    if _dlq_retry_gauge is not None:
        _dlq_retry_gauge.labels(outcome=outcome).inc()


def set_ws_auth_cache_size(count: int):
    if _ws_auth_cache_size_gauge is not None:
        _ws_auth_cache_size_gauge.set(count)


def set_dlq_fallback_file_size(size_bytes: int):
    if _dlq_fallback_file_size_gauge is not None:
        _dlq_fallback_file_size_gauge.set(size_bytes)


def set_ghost_reservations(count: int):
    if _ghost_reservations_gauge is not None:
        _ghost_reservations_gauge.set(count)


def set_retry_guard_depth(count: int):
    if _retry_guard_depth_gauge is not None:
        _retry_guard_depth_gauge.set(count)


def set_ws_db_poll_concurrent(count: int):
    if _ws_db_poll_concurrent_gauge is not None:
        _ws_db_poll_concurrent_gauge.set(count)


def set_r2_circuit(count: int):
    if _r2_circuit_gauge is not None:
        _r2_circuit_gauge.set(count)


# --- Setup ---


def setup_prometheus(app):
    global _registry_instrumentator
    try:
        from prometheus_client import REGISTRY
        from prometheus_fastapi_instrumentator import Instrumentator

        instrumentator = Instrumentator(
            should_group_status_codes=True,
            should_ignore_untemplated=True,
        )
        instrumentator.instrument(app)
        _registry_instrumentator = instrumentator

        # Register custom metrics on the default registry
        _register_db_pool_metrics(REGISTRY)
        _register_db_index_metrics(REGISTRY)
        _register_rate_limiter_metrics(REGISTRY)
        _register_ai_circuit_metrics(REGISTRY)
        _register_redis_metrics(REGISTRY)
        _register_celery_queue_metrics(REGISTRY)
        _register_stripe_ip_metrics(REGISTRY)
        _register_workflow_metrics(REGISTRY)
        _register_billing_metrics(REGISTRY)
        _register_enhanced_metrics(REGISTRY)
        _register_phase2_metrics(REGISTRY)
        _register_phase3_metrics(REGISTRY)
        _register_redis_tenant_metrics(REGISTRY)

        from fastapi import APIRouter, Depends, HTTPException, status
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

        from app.config import get_settings

        security = HTTPBearer(auto_error=False)

        async def metrics_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
            settings = get_settings()
            metrics_token = settings.metrics_access_token
            if not metrics_token:
                if settings.debug:
                    logger.warning("Metrics auth disabled in debug mode — NOT FOR PRODUCTION")
                    return True
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Metrics authentication not configured. Set METRICS_ACCESS_TOKEN.",
                )
            if not credentials:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Metrics authentication required")
            if not hmac.compare_digest(credentials.credentials, metrics_token):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid metrics token")
            return True

        admin_router = APIRouter(dependencies=[Depends(metrics_auth)])
        instrumentator.expose(admin_router, endpoint="/metrics", include_in_schema=False)
        app.mount("/admin", admin_router)
        logger.info("Prometheus metrics available at /admin/metrics (token-protected)")
        logger.info("Custom metrics registered: db_pool, rate_limiter, ai_circuit, celery_queues, stripe_ip, workflow")
    except ImportError:
        logger.warning("prometheus-fastapi-instrumentator not installed, skipping")


_counter_registry: dict[str, int] = {}
_counter_registry_lock = threading.Lock()
_billing_underflow_counter = None

# --- Enhanced observability gauges and counters (P3-A, P3-B, P3-C) ---
_dlq_write_failures_counter = None
_celery_task_latency_histogram = None
_billing_drift_gauge = None
_billing_reconciliation_duration = None
_dlq_queue_depth_gauge = None
_ws_db_poll_counter = None
_billing_debt_threshold_counter = None
_billing_integrity_error_counter = None


def _register_enhanced_metrics(registry):
    global _dlq_write_failures_counter, _celery_task_latency_histogram
    global _billing_drift_gauge, _billing_reconciliation_duration
    global _dlq_queue_depth_gauge, _ws_db_poll_counter
    global _billing_debt_threshold_counter, _billing_integrity_error_counter
    try:
        from prometheus_client import Counter, Gauge, Histogram

        _dlq_write_failures_counter = Counter(
            "workticket_dlq_write_failures_total",
            "Total DLQ write failures by category",
            registry=registry,
            labelnames=["failure_category"],
        )

        _celery_task_latency_histogram = Histogram(
            "workticket_celery_task_latency_seconds",
            "Celery task latency in seconds (start to completion)",
            registry=registry,
            buckets=[1, 5, 10, 30, 60, 120, 180, 240, 300],
            labelnames=["queue"],
        )

        _billing_drift_gauge = Gauge(
            "workticket_billing_drift_pct",
            "Billing cost drift percentage",
            registry=registry,
        )

        _billing_reconciliation_duration = Histogram(
            "workticket_billing_reconciliation_duration_ms",
            "Billing reconciliation latency in ms",
            registry=registry,
            buckets=[10, 50, 100, 200, 500, 1000, 2000, 5000],
        )

        _dlq_queue_depth_gauge = Gauge(
            "workticket_dlq_queue_depth",
            "Number of unprocessed DLQ entries",
            registry=registry,
        )

        _ws_db_poll_counter = Counter(
            "workticket_ws_db_poll_count_total",
            "Total WebSocket DB poll count",
            registry=registry,
            labelnames=["job_id"],
        )

        _billing_debt_threshold_counter = Counter(
            "workticket_billing_debt_threshold_exceeded_total",
            "Total billing debt threshold exceeded events",
            registry=registry,
            labelnames=["company_id"],
        )

        _billing_integrity_error_counter = Counter(
            "workticket_billing_reconciliation_integrity_error_total",
            "Total billing reconciliation integrity errors",
            registry=registry,
            labelnames=["company_id"],
        )
    except Exception as e:
        logger.warning("Failed to register enhanced metrics: %s", e)


# Phase 3: Jobs created vs completed tracking
_workticket_jobs_created_total = None
_workticket_jobs_completed_total = None
_workticket_ws_messages_sent_total = None
_workticket_healthz_requests_total = None
_workticket_readyz_requests_total = None
_workticket_stuck_jobs_gauge_v2 = None
_workticket_dropped_tasks_gauge = None
_workticket_worker_crash_loops = None
_workticket_dlq_count = None
_email_circuit_gauge = None
_email_latency_histogram = None
_email_failure_counter = None
_sms_circuit_gauge = None
_sms_latency_histogram = None
_sms_failure_counter = None
_ws_latency_histogram = None

# Phase 2: Additional silent failure metrics
_unsigned_task_rejected_counter = None
_beat_lock_skipped_counter = None
_ws_pubsub_fallback_counter = None
_billing_concurrent_reset_counter = None
_dlq_duplicate_dispatch_counter = None
_compensation_race_blocked_counter = None
_orphaned_outputs_recovered_counter = None
_worker_forced_kill_counter = None
_requests_shed_counter = None
_celery_event_loop_recreated_counter = None
_ws_reauth_cache_hits_counter = None
_ws_reauth_db_hits_counter = None
_concurrency_counter_negative_counter = None
_stripe_dedup_redis_hit_counter = None
_stripe_dedup_redis_miss_counter = None
_stripe_webhook_lock_contention_counter = None
_beat_lock_ttl_renewed_counter = None
_beat_lock_contention_counter = None
_billing_reconciliation_skipped_counter = None
_ws_send_dropped_counter = None
_ws_accept_throttled_counter = None
_redis_write_failures_counter = None
_state_cycle_backoff_counter = None
_stripe_ip_cache_empty_counter = None

# Beat task execution tracking gauges
_beat_task_execution_gauge = None
_beat_task_iteration_gauge = None
_stripe_webhook_latency_histogram = None
_read_replica_lag_gauge = None


def _register_phase3_metrics(registry):
    """Register Phase 3 observability metrics."""
    global _workticket_jobs_created_total, _workticket_jobs_completed_total
    global _workticket_ws_messages_sent_total, _workticket_healthz_requests_total
    global _workticket_readyz_requests_total, _workticket_stuck_jobs_gauge_v2
    try:
        from prometheus_client import Counter, Gauge, Histogram

        _workticket_jobs_created_total = Counter(
            "workticket_jobs_created_total",
            "Total jobs created via API",
            registry=registry,
        )

        _workticket_jobs_completed_total = Counter(
            "workticket_jobs_completed_total",
            "Total jobs completed (AIOutput written)",
            registry=registry,
        )

        _workticket_ws_messages_sent_total = Counter(
            "workticket_ws_messages_sent_total",
            "WebSocket messages sent",
            registry=registry,
            labelnames=["job_id"],
        )

        _workticket_healthz_requests_total = Counter(
            "workticket_healthz_requests_total",
            "Total /healthz requests",
            registry=registry,
        )

        _workticket_readyz_requests_total = Counter(
            "workticket_readyz_requests_total",
            "Total /readyz requests",
            registry=registry,
        )

        _workticket_stuck_jobs_gauge_v2 = Gauge(
            "workticket_stuck_jobs_processing_gauge",
            "Jobs stuck in processing state >5 min",
            registry=registry,
        )

        # Silent queue death detection: tracks the gap between jobs created
        # and jobs completed over a sliding window. A persistent gap > 0
        # indicates tasks were enqueued but never completed (dropped).
        _workticket_dropped_tasks_gauge = Gauge(
            "workticket_dropped_tasks_gap",
            "Gap between jobs created and completed (dropped tasks)",
            registry=registry,
        )

        _workticket_worker_crash_loops = Gauge(
            "workticket_worker_crash_loops_detected",
            "Worker crash-loop events detected (1=active, 0=normal)",
            registry=registry,
        )

        _workticket_dlq_count = Gauge(
            "workticket_dlq_count",
            "Current dead letter queue entry count",
            registry=registry,
        )

        # Per-queue Celery task latency is tracked via the existing
        # workticket_celery_task_latency_seconds histogram — the observe
        # function now accepts an optional queue label.

        _workticket_queue_depth = Gauge(
            "workticket_queue_depth",
            "Per-queue depth",
            registry=registry,
            labelnames=["queue"],
        )

        _workticket_celery_worker_active = Gauge(
            "workticket_celery_worker_active",
            "Workers currently processing per queue",
            registry=registry,
            labelnames=["queue"],
        )

        _workticket_ws_dropped_messages_total = Counter(
            "workticket_ws_dropped_messages_total",
            "Messages dropped by WebSocket backpressure",
            registry=registry,
        )

        _workticket_stripe_circuit_state = Gauge(
            "workticket_stripe_circuit_state",
            "Stripe circuit breaker state (1=open, 0=closed)",
            registry=registry,
            labelnames=["state"],
        )

        _workticket_ai_fallback_total = Counter(
            "workticket_ai_fallback_total",
            "Count of AI fallback responses",
            registry=registry,
        )

        _workticket_email_circuit_state = Gauge(
            "workticket_email_circuit_state",
            "Email (Resend) circuit breaker state (1=open, 0=closed)",
            registry=registry,
        )

        _workticket_email_failure_rate = Gauge(
            "workticket_email_failure_rate",
            "Email (Resend) failure rate over last 5 minutes",
            registry=registry,
        )

        _workticket_sms_circuit_state = Gauge(
            "workticket_sms_circuit_state",
            "SMS (Twilio) circuit breaker state (1=open, 0=closed)",
            registry=registry,
        )

        _workticket_sms_failure_rate = Gauge(
            "workticket_sms_failure_rate",
            "SMS (Twilio) failure rate over last 5 minutes",
            registry=registry,
        )

        _workticket_migration_duration_seconds = Histogram(
            "workticket_migration_duration_seconds",
            "Migration execution time in seconds",
            registry=registry,
            buckets=[1, 5, 10, 30, 60, 120, 300],
        )

        _workticket_billing_drift_abs = Gauge(
            "workticket_billing_drift_abs",
            "Cumulative absolute billing drift in USD",
            registry=registry,
        )

        _workticket_http_request_duration_seconds = Histogram(
            "workticket_http_request_duration_seconds",
            "Per-endpoint HTTP request latency in seconds",
            registry=registry,
            labelnames=["method", "endpoint"],
            buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
        )

        global _email_latency_histogram, _ws_latency_histogram
        _email_latency_histogram = Histogram(
            "workticket_email_latency_ms",
            "Email (Resend) API call latency in milliseconds",
            registry=registry,
            buckets=[50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000],
        )

        _ws_latency_histogram = Histogram(
            "workticket_ws_message_latency_ms",
            "WebSocket message delivery latency in milliseconds",
            registry=registry,
            buckets=[10, 50, 100, 200, 500, 1000, 2000, 5000],
        )
    except Exception as e:
        logger.warning("Failed to register phase3 metrics: %s", e)


# --- Redis Tenant Key Namespace Metrics ---
_redis_tenant_key_count_gauge = None
_redis_tenants_over_limit_gauge = None
_redis_worst_tenant_keys_gauge = None


def _register_redis_tenant_metrics(registry):
    global _redis_tenant_key_count_gauge, _redis_tenants_over_limit_gauge, _redis_worst_tenant_keys_gauge
    try:
        from prometheus_client import Gauge

        _redis_tenant_key_count_gauge = Gauge(
            "workticket_redis_tenant_keys_total",
            "Total Redis keys tracked across all tenant namespaces",
            registry=registry,
        )

        _redis_tenants_over_limit_gauge = Gauge(
            "workticket_redis_tenants_over_limit",
            "Number of tenants exceeding per-tenant Redis key limit",
            registry=registry,
        )

        _redis_worst_tenant_keys_gauge = Gauge(
            "workticket_redis_worst_tenant_key_count",
            "Highest Redis key count for any single tenant",
            registry=registry,
        )
    except Exception as e:
        logger.warning("Failed to register Redis tenant metrics: %s", e)


def set_redis_tenant_key_count(count: int):
    if _redis_tenant_key_count_gauge is not None:
        _redis_tenant_key_count_gauge.set(count)


def set_redis_tenants_over_limit(count: int):
    if _redis_tenants_over_limit_gauge is not None:
        _redis_tenants_over_limit_gauge.set(count)


def set_redis_worst_tenant_keys(count: int):
    if _redis_worst_tenant_keys_gauge is not None:
        _redis_worst_tenant_keys_gauge.set(count)


def increment_jobs_created():
    if _workticket_jobs_created_total is not None:
        _workticket_jobs_created_total.inc()


def increment_jobs_completed():
    if _workticket_jobs_completed_total is not None:
        _workticket_jobs_completed_total.inc()


def increment_ws_messages_sent(job_id: str):
    if _workticket_ws_messages_sent_total is not None:
        _workticket_ws_messages_sent_total.labels(job_id=job_id).inc()


def set_stuck_jobs_processing(count: int):
    if _workticket_stuck_jobs_gauge_v2 is not None:
        _workticket_stuck_jobs_gauge_v2.set(count)


def set_dropped_tasks(gap: int):
    global _workticket_dropped_tasks_gauge
    if _workticket_dropped_tasks_gauge is not None:
        _workticket_dropped_tasks_gauge.set(gap)


def set_worker_crash_loops(detected: int):
    global _workticket_worker_crash_loops
    if _workticket_worker_crash_loops is not None:
        _workticket_worker_crash_loops.set(detected)


def set_dlq_count(count: int):
    global _workticket_dlq_count
    if _workticket_dlq_count is not None:
        _workticket_dlq_count.set(count)


def _register_phase2_metrics(registry):
    global _unsigned_task_rejected_counter, _beat_lock_skipped_counter
    global _ws_pubsub_fallback_counter, _billing_concurrent_reset_counter
    global _dlq_duplicate_dispatch_counter, _compensation_race_blocked_counter
    global _orphaned_outputs_recovered_counter, _worker_forced_kill_counter
    global _requests_shed_counter
    global _celery_event_loop_recreated_counter, _ws_reauth_cache_hits_counter
    global _ws_reauth_db_hits_counter, _concurrency_counter_negative_counter
    global _stripe_dedup_redis_hit_counter, _stripe_dedup_redis_miss_counter
    global _stripe_webhook_lock_contention_counter, _beat_lock_ttl_renewed_counter
    global _beat_lock_contention_counter, _billing_reconciliation_skipped_counter
    global _ws_send_dropped_counter, _ws_accept_throttled_counter
    global _redis_write_failures_counter, _stripe_ip_cache_empty_counter
    global _beat_task_execution_gauge, _beat_task_iteration_gauge
    global _stripe_webhook_latency_histogram, _read_replica_lag_gauge
    try:
        from prometheus_client import Counter, Gauge, Histogram

        _stripe_ip_cache_empty_counter = Counter(
            "workticket_stripe_ip_cache_empty_warning",
            "Total Stripe IP cache empty warnings",
            registry=registry,
        )

        _unsigned_task_rejected_counter = Counter(
            "workticket_unsigned_task_rejected_total",
            "Total unsigned tasks rejected (HMAC key missing in production)",
            registry=registry,
            labelnames=["task_name"],
        )

        _beat_lock_skipped_counter = Counter(
            "workticket_beat_lock_skipped_total",
            "Total beat lock skips due to Redis error",
            registry=registry,
            labelnames=["task", "reason"],
        )

        _ws_pubsub_fallback_counter = Counter(
            "workticket_ws_pubsub_fallback_total",
            "Total WebSocket PubSub fallback events",
            registry=registry,
            labelnames=["reason"],
        )

        _billing_concurrent_reset_counter = Counter(
            "workticket_billing_concurrent_reset_skipped_total",
            "Total concurrent billing reset blocks",
            registry=registry,
            labelnames=["period"],
        )

        _dlq_duplicate_dispatch_counter = Counter(
            "workticket_dlq_duplicate_dispatch_blocked_total",
            "Total DLQ duplicate dispatch blocks",
            registry=registry,
        )

        _compensation_race_blocked_counter = Counter(
            "workticket_compensation_race_blocked_total",
            "Total compensation race serializations",
            registry=registry,
            labelnames=["job_id"],
        )

        _orphaned_outputs_recovered_counter = Counter(
            "workticket_orphaned_outputs_recovered_total",
            "Total orphaned AIOutputs recovered",
            registry=registry,
        )

        _worker_forced_kill_counter = Counter(
            "workticket_worker_forced_kill_total",
            "Total worker forced kills during restart",
            registry=registry,
            labelnames=["worker"],
        )

        _requests_shed_counter = Counter(
            "workticket_requests_shed_total",
            "Total requests shed by priority tier",
            registry=registry,
            labelnames=["priority"],
        )

        _celery_event_loop_recreated_counter = Counter(
            "workticket_celery_event_loop_recreated_total",
            "Total Celery event loop recreations due to corruption",
            registry=registry,
        )

        _ws_reauth_cache_hits_counter = Counter(
            "workticket_ws_reauth_cache_hits",
            "Total WS re-auth cache hits",
            registry=registry,
            labelnames=["user_id"],
        )

        _ws_reauth_db_hits_counter = Counter(
            "workticket_ws_reauth_db_hits",
            "Total WS re-auth DB fallback hits",
            registry=registry,
            labelnames=["user_id"],
        )

        _concurrency_counter_negative_counter = Counter(
            "workticket_concurrency_counter_negative_total",
            "Total concurrency counter negative drift events (should be 0)",
            registry=registry,
            labelnames=["company_id"],
        )

        _stripe_dedup_redis_hit_counter = Counter(
            "workticket_stripe_dedup_redis_hit",
            "Total Stripe webhook Redis dedup hits",
            registry=registry,
            labelnames=["event_type"],
        )

        _stripe_dedup_redis_miss_counter = Counter(
            "workticket_stripe_dedup_redis_miss",
            "Total Stripe webhook Redis dedup misses",
            registry=registry,
            labelnames=["event_type"],
        )

        _stripe_webhook_lock_contention_counter = Counter(
            "workticket_stripe_webhook_lock_contention_total",
            "Total Stripe webhook lock contention events",
            registry=registry,
        )

        _beat_lock_ttl_renewed_counter = Counter(
            "workticket_beat_lock_ttl_renewed_total",
            "Total beat lock TTL renewals via heartbeat",
            registry=registry,
            labelnames=["task"],
        )

        _beat_lock_contention_counter = Counter(
            "workticket_beat_lock_contention_total",
            "Total beat lock contention events (acquire failed)",
            registry=registry,
            labelnames=["task"],
        )

        _billing_reconciliation_skipped_counter = Counter(
            "workticket_billing_reconciliation_skipped_total",
            "Total billing reconciliation skips due to lock contention",
            registry=registry,
        )

        _ws_send_dropped_counter = Counter(
            "workticket_ws_send_dropped_total",
            "Total WebSocket messages dropped due to backpressure",
            registry=registry,
            labelnames=["job_id"],
        )

        _ws_accept_throttled_counter = Counter(
            "workticket_ws_accept_throttled_total",
            "Total WebSocket accepts throttled by semaphore",
            registry=registry,
        )

        _redis_write_failures_counter = Counter(
            "workticket_redis_write_failures_total",
            "Total Redis write failures",
            registry=registry,
        )

        _state_cycle_backoff_counter = Counter(
            "workticket_state_cycle_backoff_total",
            "Total state cycle backoff events (exponential backoff applied)",
            registry=registry,
            labelnames=["job_id"],
        )

        _beat_task_execution_gauge = Gauge(
            "workticket_beat_task_execution_timestamp",
            "Unix timestamp of last execution per beat task",
            registry=registry,
            labelnames=["task"],
        )

        _beat_task_iteration_gauge = Gauge(
            "workticket_beat_task_iteration_count",
            "Iteration count of last execution per beat task",
            registry=registry,
            labelnames=["task"],
        )

        _stripe_webhook_latency_histogram = Histogram(
            "workticket_stripe_webhook_latency_ms",
            "Stripe webhook processing latency in milliseconds",
            registry=registry,
            buckets=[10, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000],
        )

        _read_replica_lag_gauge = Gauge(
            "workticket_read_replica_lag_seconds",
            "Read replica lag behind primary in seconds",
            registry=registry,
        )
    except Exception as e:
        logger.warning("Failed to register phase2 metrics: %s", e)


def increment_dlq_write_failure(failure_category: str):
    if _dlq_write_failures_counter is not None:
        _dlq_write_failures_counter.labels(failure_category=failure_category).inc()


def observe_celery_task_latency(seconds: float, queue: str = "default"):
    if _celery_task_latency_histogram is not None:
        # If the histogram has labelnames=["queue"], use labels; otherwise observe directly
        try:
            _celery_task_latency_histogram.labels(queue=queue).observe(seconds)
        except (TypeError, ValueError):
            _celery_task_latency_histogram.observe(seconds)


def set_billing_drift_pct(pct: float):
    if _billing_drift_gauge is not None:
        _billing_drift_gauge.set(pct)


def observe_billing_reconciliation_duration(ms: float):
    if _billing_reconciliation_duration is not None:
        _billing_reconciliation_duration.observe(ms)


def set_dlq_queue_depth(count: int):
    if _dlq_queue_depth_gauge is not None:
        _dlq_queue_depth_gauge.set(count)


def increment_ws_db_poll(job_id: str):
    if _ws_db_poll_counter is not None:
        _ws_db_poll_counter.labels(job_id=job_id).inc()


def increment_billing_debt_threshold(company_id: str):
    if _billing_debt_threshold_counter is not None:
        _billing_debt_threshold_counter.labels(company_id=company_id).inc()


def increment_billing_integrity_error(company_id: str):
    if _billing_integrity_error_counter is not None:
        _billing_integrity_error_counter.labels(company_id=company_id).inc()


def _register_billing_metrics(registry):
    """Register billing reconciliation metrics."""
    global _billing_underflow_counter
    try:
        from prometheus_client import Counter

        _billing_underflow_counter = Counter(
            "workticket_billing_reconciliation_underflow_total",
            "Total billing reconciliation underflow events (acu_debt tracked)",
            registry=registry,
            labelnames=["company_id"],
        )
    except Exception as e:
        logger.warning("Failed to register billing metrics: %s", e)


def increment_state_cycle_backoff(job_id: str):
    global _state_cycle_backoff_counter
    if _state_cycle_backoff_counter is not None:
        _state_cycle_backoff_counter.labels(job_id=job_id).inc()


def increment_counter(name: str, tags: dict | None = None):
    key = f"{name}:{tags}" if tags else name
    with _counter_registry_lock:
        _counter_registry[key] = _counter_registry.get(key, 0) + 1
    # Route to typed Prometheus counters
    if name == "billing_reconciliation_underflow_total" and _billing_underflow_counter is not None:
        company = (tags or {}).get("company_id", "unknown")
        _billing_underflow_counter.labels(company_id=company).inc()
    elif name == "workticket_unsigned_task_rejected_total" and _unsigned_task_rejected_counter is not None:
        task = (tags or {}).get("task_name", "unknown")
        _unsigned_task_rejected_counter.labels(task_name=task).inc()
    elif name == "workticket_beat_lock_skipped_total" and _beat_lock_skipped_counter is not None:
        task = (tags or {}).get("task", "unknown")
        reason = (tags or {}).get("reason", "unknown")
        _beat_lock_skipped_counter.labels(task=task, reason=reason).inc()
    elif name == "workticket_ws_pubsub_fallback_total" and _ws_pubsub_fallback_counter is not None:
        reason = (tags or {}).get("reason", "unknown")
        _ws_pubsub_fallback_counter.labels(reason=reason).inc()
    elif name == "workticket_billing_concurrent_reset_skipped_total" and _billing_concurrent_reset_counter is not None:
        period = (tags or {}).get("period", "unknown")
        _billing_concurrent_reset_counter.labels(period=period).inc()
    elif name == "workticket_dlq_duplicate_dispatch_blocked_total" and _dlq_duplicate_dispatch_counter is not None:
        _dlq_duplicate_dispatch_counter.inc()
    elif name == "workticket_compensation_race_blocked_total" and _compensation_race_blocked_counter is not None:
        job = (tags or {}).get("job_id", "unknown")
        _compensation_race_blocked_counter.labels(job_id=job).inc()
    elif name == "workticket_orphaned_outputs_recovered_total" and _orphaned_outputs_recovered_counter is not None:
        _orphaned_outputs_recovered_counter.inc()
    elif name == "workticket_worker_forced_kill_total" and _worker_forced_kill_counter is not None:
        worker = (tags or {}).get("worker", "unknown")
        _worker_forced_kill_counter.labels(worker=worker).inc()
    elif name == "workticket_requests_shed_total" and _requests_shed_counter is not None:
        priority = (tags or {}).get("priority", "unknown")
        _requests_shed_counter.labels(priority=priority).inc()
    elif name == "workticket_celery_event_loop_recreated_total" and _celery_event_loop_recreated_counter is not None:
        _celery_event_loop_recreated_counter.inc()
    elif name == "workticket_ws_reauth_cache_hits" and _ws_reauth_cache_hits_counter is not None:
        uid = (tags or {}).get("user_id", "unknown")
        _ws_reauth_cache_hits_counter.labels(user_id=uid).inc()
    elif name == "workticket_ws_reauth_db_hits" and _ws_reauth_db_hits_counter is not None:
        uid = (tags or {}).get("user_id", "unknown")
        _ws_reauth_db_hits_counter.labels(user_id=uid).inc()
    elif name == "workticket_concurrency_counter_negative_total" and _concurrency_counter_negative_counter is not None:
        cid = (tags or {}).get("company_id", "unknown")
        _concurrency_counter_negative_counter.labels(company_id=cid).inc()
    elif name == "workticket_stripe_dedup_redis_hit" and _stripe_dedup_redis_hit_counter is not None:
        et = (tags or {}).get("event_type", "unknown")
        _stripe_dedup_redis_hit_counter.labels(event_type=et).inc()
    elif name == "workticket_stripe_dedup_redis_miss" and _stripe_dedup_redis_miss_counter is not None:
        et = (tags or {}).get("event_type", "unknown")
        _stripe_dedup_redis_miss_counter.labels(event_type=et).inc()
    elif (
        name == "workticket_stripe_webhook_lock_contention_total"
        and _stripe_webhook_lock_contention_counter is not None
    ):
        _stripe_webhook_lock_contention_counter.inc()
    elif name == "workticket_beat_lock_ttl_renewed_total" and _beat_lock_ttl_renewed_counter is not None:
        t = (tags or {}).get("task", "unknown")
        _beat_lock_ttl_renewed_counter.labels(task=t).inc()
    elif name == "workticket_beat_lock_contention_total" and _beat_lock_contention_counter is not None:
        t = (tags or {}).get("task", "unknown")
        _beat_lock_contention_counter.labels(task=t).inc()
    elif (
        name == "workticket_billing_reconciliation_skipped_total"
        and _billing_reconciliation_skipped_counter is not None
    ):
        _billing_reconciliation_skipped_counter.inc()
    elif name == "workticket_ws_send_dropped_total" and _ws_send_dropped_counter is not None:
        jid = (tags or {}).get("job_id", "unknown")
        _ws_send_dropped_counter.labels(job_id=jid).inc()
    elif name == "workticket_ws_accept_throttled_total" and _ws_accept_throttled_counter is not None:
        _ws_accept_throttled_counter.inc()
    elif name == "workticket_redis_write_failures_total" and _redis_write_failures_counter is not None:
        _redis_write_failures_counter.inc()
    elif name == "workticket_state_cycle_backoff_total" and _state_cycle_backoff_counter is not None:
        jid = (tags or {}).get("job_id", "unknown")
        _state_cycle_backoff_counter.labels(job_id=jid).inc()
    elif name == "workticket_stripe_ip_cache_empty_warning" and _stripe_ip_cache_empty_counter is not None:
        _stripe_ip_cache_empty_counter.inc()


def get_counter(name: str, tags: dict | None = None) -> int:
    key = f"{name}:{tags}" if tags else name
    with _counter_registry_lock:
        return _counter_registry.get(key, 0)


def get_all_counters() -> dict[str, int]:
    """Return deep copy of all counters for /readyz."""
    with _counter_registry_lock:
        return dict(_counter_registry)


def set_email_circuit_state(state: int):
    global _email_circuit_gauge
    if _email_circuit_gauge is None:
        try:
            from prometheus_client import Gauge

            _email_circuit_gauge = Gauge(
                "workticket_email_circuit_state",
                "Email (Resend) circuit breaker state (1=open, 0=closed)",
            )
        except Exception as e:
            logger.debug("Failed to create email circuit gauge: %s", e)
            return


def observe_email_latency(ms: float):
    global _email_latency_histogram
    if _email_latency_histogram is not None:
        _email_latency_histogram.observe(ms)


def increment_email_failure():
    global _email_failure_counter
    if _email_failure_counter is None:
        try:
            from prometheus_client import Counter

            _email_failure_counter = Counter(
                "workticket_email_failures_total",
                "Total email delivery failures",
            )
        except Exception as e:
            logger.debug("Failed to create email failure counter: %s", e)
            return


_sms_circuit_gauge = None
_sms_latency_histogram = None
_sms_failure_counter = None


def set_sms_circuit_state(state: int):
    global _sms_circuit_gauge
    if _sms_circuit_gauge is None:
        try:
            from prometheus_client import Gauge

            _sms_circuit_gauge = Gauge(
                "workticket_sms_circuit_state",
                "SMS (Twilio) circuit breaker state (1=open, 0=closed)",
            )
        except Exception as e:
            logger.debug("Failed to create SMS circuit gauge: %s", e)
            return


def observe_sms_latency(ms: float):
    global _sms_latency_histogram
    if _sms_latency_histogram is None:
        try:
            from prometheus_client import Histogram

            _sms_latency_histogram = Histogram(
                "workticket_sms_latency_ms",
                "SMS (Twilio) API call latency in milliseconds",
                buckets=[50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000],
            )
        except Exception as e:
            logger.debug("Failed to create SMS latency histogram: %s", e)
            return


def increment_sms_failure():
    global _sms_failure_counter
    if _sms_failure_counter is None:
        try:
            from prometheus_client import Counter

            _sms_failure_counter = Counter(
                "workticket_sms_failures_total",
                "Total SMS delivery failures",
            )
        except Exception as e:
            logger.debug("Failed to create SMS failure counter: %s", e)
            return


def observe_ws_message_latency(ms: float):
    global _ws_latency_histogram
    if _ws_latency_histogram is not None:
        _ws_latency_histogram.observe(ms)


def set_beat_task_execution_timestamp(task: str, ts: float):
    global _beat_task_execution_gauge
    if _beat_task_execution_gauge is not None:
        _beat_task_execution_gauge.labels(task=task).set(ts)


def set_beat_task_iteration_count(task: str, count: int):
    global _beat_task_iteration_gauge
    if _beat_task_iteration_gauge is not None:
        _beat_task_iteration_gauge.labels(task=task).set(count)


def observe_stripe_webhook_latency(ms: float):
    global _stripe_webhook_latency_histogram
    if _stripe_webhook_latency_histogram is not None:
        _stripe_webhook_latency_histogram.observe(ms)


def set_read_replica_lag(seconds: float):
    global _read_replica_lag_gauge
    if _read_replica_lag_gauge is not None:
        _read_replica_lag_gauge.set(seconds)

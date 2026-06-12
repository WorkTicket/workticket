import asyncio
import logging
import os

from celery import Celery
from celery.signals import worker_ready as _worker_ready_signal
from celery.signals import worker_shutdown as _worker_shutdown_signal

logger = logging.getLogger(__name__)

from app.config import get_settings  # noqa: E402
from celery_config.broker import REDIS_BROKER_URL  # noqa: E402

_settings = get_settings()

# Build broker transport options with Sentinel support if configured
_broker_transport_options = {
    "socket_timeout": 3.0,
    "socket_connect_timeout": 2.0,
    "retry_on_timeout": True,
    "max_retries": 2,
    "visibility_timeout": 480,
}
_result_backend_transport_options = {
    "socket_timeout": 3.0,
    "socket_connect_timeout": 2.0,
    "retry_on_timeout": True,
    "max_retries": 2,
    "visibility_timeout": 480,
}
if _settings.redis_sentinel_hosts and _settings.redis_sentinel_master_name:
    _broker_transport_options["master_name"] = _settings.redis_sentinel_master_name
    _broker_transport_options["sentinel_kwargs"] = {
        "password": _settings.redis_sentinel_password or _settings.redis_password
    }
    _result_backend_transport_options["master_name"] = _settings.redis_sentinel_master_name
    _result_backend_transport_options["sentinel_kwargs"] = {
        "password": _settings.redis_sentinel_password or _settings.redis_password
    }

celery_app = Celery(
    "workticket",
    broker=REDIS_BROKER_URL,
    backend=REDIS_BROKER_URL,
)

celery_app.conf.result_expires = 3600 * 3

from celery_config.beat import get_effective_beat_schedule, task_routes  # noqa: E402

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=240,
    task_acks_late=True,
    worker_prefetch_multiplier=int(os.getenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "1")),
    task_reject_on_worker_lost=True,
    task_retry_max_retries=3,
    task_retry_backoff=True,
    task_retry_backoff_max=60,
    worker_max_tasks_per_child=int(os.getenv("CELERY_WORKER_MAX_TASKS_PER_CHILD", "1000")),
    broker_transport_options=_broker_transport_options,
    result_backend_transport_options=_result_backend_transport_options,
    task_routes=task_routes,
    beat_schedule=get_effective_beat_schedule(),
    beat_pidfile=None,
    beat_sync_every=0,
    broker_connection_retry_on_startup=True,
)

# V2-FIX: Initialize Celery OTel instrumentation after app is created
try:
    from app.telemetry import setup_celery_otel

    setup_celery_otel()
except Exception:
    logger.error("Failed to initialize Celery OTel instrumentation", exc_info=True)


@_worker_ready_signal.connect
def _validate_on_worker_ready(sender, **kwargs):
    """Validate worker configuration on startup (not import time).

    H-4 FIX: Removed hard-coded concurrency=1 enforcement. The concurrency
    is now configurable via CELERY_WORKER_CONCURRENCY (default 2).
    Use container replicas for horizontal scaling; concurrency within a
    replica allows parallel task processing for improved throughput.
    """
    import os

    concurrency = int(os.getenv("CELERY_WORKER_CONCURRENCY", "2"))
    logger.info(
        "Celery worker starting with concurrency=%d. "
        "Set CELERY_WORKER_CONCURRENCY to adjust (default=2). "
        "Use container replicas for horizontal scaling.",
        concurrency,
    )


@_worker_shutdown_signal.connect
def handle_worker_shutdown(sender, **kwargs):
    logger.warning("Worker shutting down — draining in-flight tasks")
    try:
        i = sender.control.inspect()
        active = i.active() or {}
        total_active = 0
        for worker, tasks in active.items():
            if tasks:
                total_active += len(tasks)
                logger.info("Draining %d active tasks on %s", len(tasks), worker)
        if total_active > 0:
            import time as _time

            _grace_period = max(5, min(300, total_active * 10))
            logger.warning("Waiting up to %ds for %d active tasks to complete", _grace_period, total_active)
            _time.sleep(_grace_period)
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter("workticket_worker_forced_kill_total", {"worker": sender.name or "unknown"})
            except Exception as _e:
                logger.debug("Failed to increment worker forced kill metric: %s", _e)
    except Exception as _e:
        logger.debug("Worker shutdown drain handler failed: %s", _e)


# C1-FIX: Per-task event loop execution using new_event_loop pattern.
# Replaced asyncio.run() because set_event_loop fails in nested Celery calls.
# Cannot run asyncio.run() in Celery threads where an event loop may already
# exist in the thread's TLS, causing "Event loop is closed" errors on retry.
# Each call gets a fresh loop via new_event_loop(), runs the coroutine with
# run_until_complete(), then properly cleans up with shutdown_asyncgens()
# and loop.close() in a finally block.


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.shutdown_asyncgens()
        loop.close()

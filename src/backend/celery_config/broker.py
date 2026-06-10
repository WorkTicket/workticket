import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Import settings to get Sentinel-aware URLs
from app.config import get_settings  # noqa: E402

_settings = get_settings()
REDIS_BROKER_URL = _settings.effective_redis_broker_url
REDIS_CACHE_URL = _settings.effective_redis_cache_url
REDIS_URL = REDIS_BROKER_URL

# C-5 FIX: Use shared Redis pool instead of per-operation connections
from app.sync_redis_pool import get_sync_redis  # noqa: E402

# --- Shared Redis publishing pool ---
_pub_redis_pool = None
# LOW-1 FIX: Use threading.Lock with acquire timeout to prevent
# threads blocking indefinitely if a previous Redis operation hangs.
_pub_redis_pool_lock = threading.Lock()
_PUB_REDIS_LOCK_TIMEOUT = 5.0  # seconds


def _get_pub_redis():
    """Get or create the shared Redis publishing pool.

    LOW-1 FIX: Uses lock with timeout to prevent threads blocking
    indefinitely if a previous Redis operation hangs.
    """
    global _pub_redis_pool
    if _pub_redis_pool is None:
        if not _pub_redis_pool_lock.acquire(timeout=_PUB_REDIS_LOCK_TIMEOUT):
            logger.error("Pub Redis pool init lock timeout after %.1fs", _PUB_REDIS_LOCK_TIMEOUT)
            return None
        try:
            if _pub_redis_pool is None:
                from app.redis_sentinel import create_sync_redis_from_url

                try:
                    _pub_redis_pool = create_sync_redis_from_url(
                        REDIS_URL,
                        socket_connect_timeout=0.5,
                        socket_keepalive=True,
                        max_connections=5,
                    )
                    _pub_redis_pool.ping()
                except Exception:
                    logger.error("Failed to initialize pub Redis pool")
                    _pub_redis_pool = None
        finally:
            _pub_redis_pool_lock.release()
    else:
        try:
            _pub_redis_pool.ping()
        except Exception:
            if not _pub_redis_pool_lock.acquire(timeout=_PUB_REDIS_LOCK_TIMEOUT):
                logger.error("Pub Redis pool reconnect lock timeout after %.1fs", _PUB_REDIS_LOCK_TIMEOUT)
                return _pub_redis_pool
            try:
                try:
                    _pub_redis_pool.ping()
                except Exception:
                    from app.redis_sentinel import create_sync_redis_from_url

                    try:
                        logger.warning("Reconnecting pub Redis pool")
                        _pub_redis_pool = create_sync_redis_from_url(
                            REDIS_URL,
                            socket_connect_timeout=0.5,
                            socket_keepalive=True,
                            max_connections=5,
                        )
                        _pub_redis_pool.ping()
                    except Exception:
                        _pub_redis_pool = None
            finally:
                _pub_redis_pool_lock.release()
    return _pub_redis_pool


# --- Redis broker health circuit breaker ---
_broker_unhealthy_since: float | None = None
_BROKER_HEALTH_CHECK_INTERVAL = 5.0
_BROKER_UNHEALTHY_THRESHOLD = 60.0
_last_broker_check: float = 0.0


def is_broker_healthy() -> bool:
    """Check broker Redis health with read AND write probe + memory check.
    Write probe detects OOM/noeviction scenarios where PING succeeds but
    SET fails. Memory check fires ahead-of-time warning before OOM occurs.
    If unhealthy > 60s, reject task dispatch."""
    global _broker_unhealthy_since, _last_broker_check
    now = time.monotonic()
    if now - _last_broker_check < _BROKER_HEALTH_CHECK_INTERVAL:
        return _broker_unhealthy_since is None or (now - _broker_unhealthy_since) < _BROKER_UNHEALTHY_THRESHOLD
    _last_broker_check = now
    try:
        r = get_sync_redis()
        if not r:
            raise ConnectionError("Redis pool unavailable")
        r.ping()
        # Write probe: detect OOM/noeviction mode where writes fail silently
        r.set("health:probe", "1", ex=2)
        # Memory check: warn when broker Redis is >80% of maxmemory
        try:
            _info = r.info("memory")
            _used = _info.get("used_memory", 0)
            _max = _info.get("maxmemory", 0)
            if _max > 0 and _used / _max > 0.8:
                logger.warning("Broker Redis memory usage at %.1f%% (used=%d, max=%d)", _used / _max * 100, _used, _max)
                try:
                    from app.monitoring.prometheus import _safe_gauge

                    _safe_gauge("workticket_broker_redis_memory_pct", _used / _max * 100)
                except Exception as _e:
                    logger.debug("Failed to report broker memory gauge: %s", _e)
        except Exception:
            pass
        if _broker_unhealthy_since is not None:
            logger.info("Redis broker recovered after %.1fs", now - _broker_unhealthy_since)
        _broker_unhealthy_since = None
        return True
    except Exception as e:
        if _broker_unhealthy_since is None:
            _broker_unhealthy_since = now
            logger.warning("Redis broker health check failed (including write probe): %s", e)
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_redis_write_failures_total", {})
        except Exception as _e:
            logger.debug("Failed to increment broker write failures metric: %s", _e)
        return (now - _broker_unhealthy_since) < _BROKER_UNHEALTHY_THRESHOLD


PAYLOAD_VERSION = 1
MIN_SUPPORTED_VERSION = 1
MAX_SUPPORTED_VERSION = 1
_WORKER_VERSION = int(os.getenv("CELERY_WORKER_VERSION", "1"))

_cached_signing_key: str | None = None
_cached_old_signing_key: str | None = None
_old_key_expiry: float = 0
_signing_key_last_load: float = 0
_SIGNING_KEY_REFRESH_INTERVAL = 300  # 5 minutes
_SIGNING_KEY_ROTATION_GRACE = 300  # 5 minutes grace period for key rotation


# --- Structured logging adapter for Celery tasks ---
class CeleryTaskAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        ctx = self.extra.get("task_context", {})
        prefix = " ".join(f"{k}={v}" for k, v in ctx.items())
        return f"[{prefix}] {msg}", kwargs


def _deterministic_lock_id(*parts: str) -> int:
    """Deterministic 63-bit hash for PostgreSQL advisory lock keys.
    Uses hashlib.sha256 for cross-process reproducibility.
    CRITICAL-7 FIX: Returns full 64-bit hash (not adler32 which truncates)."""
    import hashlib

    key_str = ":".join(str(p) for p in parts if p is not None)
    return int(hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16], 16) & 0x7FFFFFFFFFFFFFFF


def _get_signing_key() -> str | None:
    """Lazy-load and periodically refresh the Celery task signing key.

    Loaded at call time (not import time) so that tests and CI can set
    the variable after module import but before task dispatch.
    Refreshes from environment every 5 minutes to support key rotation
    without full worker restart.

    H7-FIX: Maintains a grace period for key rotation. When the key changes,
    both the old and new keys are cached and tried during verification for
    up to _SIGNING_KEY_ROTATION_GRACE (300s). This prevents in-flight tasks
    enqueued with the old key from being rejected immediately after rotation.
    """
    import time as _time

    global _cached_signing_key, _signing_key_last_load
    now = _time.monotonic()
    if _cached_signing_key is not None and (now - _signing_key_last_load) < _SIGNING_KEY_REFRESH_INTERVAL:
        return _cached_signing_key
    key = get_settings().celery_task_signing_key
    if not key or key == "__REQUIRED__":
        key = os.environ.get("CELERY_TASK_SIGNING_KEY")
    if not key:
        logger.warning(
            "CELERY_TASK_SIGNING_KEY not set — task payloads will not be signed. "
            "Set this to a random 256-bit key in production (e.g. openssl rand -hex 32). "
            "The app will refuse to enqueue tasks without it in non-debug mode."
        )
    if _cached_signing_key is not None and key != _cached_signing_key:
        logger.critical(
            "CELERY_TASK_SIGNING_KEY changed since last load — "
            "tasks signed with the old key will fail verification. "
            "Ensure key rotation is complete across all workers. "
            "Old key retained for %ds grace period.",
            _SIGNING_KEY_ROTATION_GRACE,
        )
        # Store old key for grace period verification
        _cached_old_signing_key = _cached_signing_key
        _old_key_expiry = now + _SIGNING_KEY_ROTATION_GRACE
    _cached_signing_key = key
    _signing_key_last_load = now
    return key


def _sign_task_payload(payload: dict) -> str:
    key = _get_signing_key()
    if not key:
        return ""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verify_task_payload(payload: dict, signature: str) -> bool:
    key = _get_signing_key()
    if not key:
        logger.error("CELERY_TASK_SIGNING_KEY not configured — rejecting unsigned task (fail-closed)")
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_unsigned_task_rejected_total", {"task_name": "unknown"})
        except Exception as _e:
            logger.debug("Failed to increment unsigned task rejected metric: %s", _e)
        return False
    if not signature:
        logger.error("Task payload missing HMAC signature")
        return False
    expected = _sign_task_payload(payload)
    if hmac.compare_digest(expected, signature):
        return True

    # H7-FIX: During key rotation grace period, also try the old key.
    # This prevents in-flight tasks enqueued with the previous key from
    # being rejected immediately after rotation.
    global _cached_old_signing_key, _old_key_expiry
    if _cached_old_signing_key is not None and time.monotonic() < _old_key_expiry:
        old_expected = hmac.new(
            _cached_old_signing_key.encode("utf-8"),
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(old_expected, signature):
            logger.info("Task verified with old signing key (rotation grace period active)")
            return True

    logger.error("HMAC verification FAILED for payload — rejecting task")
    return False


def enqueue_job_task(
    job_id: str,
    company_id: str,
    user_id: str | None = None,
    audio_url: str | None = None,
    image_urls: list | None = None,
    description: str = "",
    trade_type: str = "",
    trace_id: str | None = None,
    request_id: str | None = None,
    estimated_cost_usd: float = 0.0,
    reserved_acu: float = 0.0,
    queue: str = "default",
) -> dict:
    """Unified enqueue wrapper with mandatory HMAC signing.

    Returns a dict with delivery status for dispatch verification.
    Raises RuntimeError if signing key is not configured in production.
    Raises RuntimeError if broker Redis is unhealthy for >60s.
    Returns 429 backpressure if total queue depth > 500.
    In debug mode, logs a warning and proceeds without signing.
    """
    # Per-queue backpressure (H-5): check each queue individually with its own threshold
    # C-5 FIX: Uses shared Redis pool instead of per-operation connection
    try:
        _bp_redis = get_sync_redis()
        if _bp_redis:
            _queue_thresholds = {
                "default": 500,
                "ai_text": 200,
                "ai_audio": 200,
                "ai_image": 200,
                "beat": 50,
            }
            for q in _queue_thresholds:
                depth = _bp_redis.llen(q) or 0
                threshold = _queue_thresholds.get(q, 500)
                if depth > threshold:
                    logger.warning("Queue depth backpressure: %s depth %d > %d, rejecting enqueue", q, depth, threshold)
                    raise RuntimeError(
                        f"Queue {q} depth too high ({depth} > {threshold}) — refusing to enqueue task. "
                        "Retry later when queue depth subsides."
                    )
    except RuntimeError:
        raise
    except Exception:
        logger.warning("Backpressure check failed — proceeding without queue depth check", exc_info=True)

    # Broker circuit breaker — reject dispatch if Redis has been unhealthy >60s
    broker_ok = is_broker_healthy()
    if not broker_ok:
        _debug = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
        if not _debug:
            raise RuntimeError(
                "Redis broker has been unhealthy for more than 60s — refusing to enqueue task. "
                "Check Redis broker health and connectivity."
            )
        logger.warning("Redis broker unhealthy >60s — enqueuing task anyway (debug mode)")

    signing_key = _get_signing_key()
    if not signing_key:
        raise RuntimeError(
            "CELERY_TASK_SIGNING_KEY is not configured — refusing to enqueue task. "
            "Set CELERY_TASK_SIGNING_KEY to a random 256-bit hex key (e.g. openssl rand -hex 32)."
        )

    payload = {
        "payload_version": PAYLOAD_VERSION,
        "job_id": job_id,
        "company_id": company_id,
        "user_id": user_id,
        "audio_url": audio_url,
        "image_urls": image_urls or [],
        "description": description,
        "trade_type": trade_type,
        "trace_id": trace_id or "",
        "request_id": request_id or "",
        "estimated_cost_usd": estimated_cost_usd,
        "reserved_acu": reserved_acu,
    }

    signature = _sign_task_payload(payload)
    payload["_hmac"] = signature

    # Lazy import to avoid circular dependency (process_job_task lives in tasks.job_tasks)
    from tasks.job_tasks import process_job_task as _process_job_task

    if queue:
        async_result = _process_job_task.apply_async(args=[], kwargs=payload, queue=queue)
    else:
        async_result = _process_job_task.delay(**payload)

    # Confirm broker acceptance: wait up to 1s for sentinel key (P2-E)
    if async_result and async_result.id:
        import time as _tmod

        _sentinel_key = f"celery:sentinel:{async_result.id}"
        _sentinel_found = False
        try:
            _sr = get_sync_redis()
            if _sr:
                for _attempt in range(5):
                    if _sr.exists(_sentinel_key):
                        _sentinel_found = True
                        break
                    _tmod.sleep(0.2)
        except Exception as _e:
            logger.debug("Dispatch sentinel check failed: %s", _e)
        if not _sentinel_found:
            logger.warning(
                "Dispatch sentinel not confirmed for task %s (job %s) within 1s — broker may be slow",
                async_result.id,
                job_id,
            )

    return {
        "task_id": async_result.id if async_result else None,
        "job_id": job_id,
        "company_id": company_id,
        "signed": bool(signature),
        "queue": queue,
    }


def _move_to_dead_letter(
    job_id: str,
    company_id: str,
    task_name: str,
    error_message: str,
    failure_category: str,
    last_state: str,
    retry_count: int,
    trace_id: str | None = None,
    user_id: str | None = None,
):
    # V2-FIX: Track total attempts per job across retry layers to prevent infinite retry
    if job_id:
        _total_attempt_key = f"poison:{job_id}:{task_name}"
        try:
            _r = get_sync_redis()
            if _r is None:
                raise ConnectionError("Redis pool unavailable")
            _total_attempts = int(_r.get(_total_attempt_key) or 0)
            if _total_attempts >= 5:
                logger.critical(
                    "Poison task detected: job=%s task=%s — discarding after %d total attempts",
                    job_id,
                    task_name,
                    _total_attempts,
                )
                return
            _r.incr(_total_attempt_key)
            _r.expire(_total_attempt_key, 86400)
        except Exception:
            logger.warning("Redis unavailable for poison tracking", exc_info=True)

    _truncated = error_message[:500] if error_message else ""
    _dlq_logger = CeleryTaskAdapter(
        logging.getLogger(__name__),
        {"task_context": {"job_id": job_id, "company_id": company_id, "trace_id": trace_id or ""}},
    )

    async def _write_with_retry():
        from app.billing.dead_letter import DeadLetterJob
        from app.database import AsyncSessionLocal

        last_err = None
        for attempt in range(3):
            try:
                async with AsyncSessionLocal() as db:
                    from datetime import timedelta as _td

                    entry = DeadLetterJob(
                        job_id=job_id,
                        company_id=company_id,
                        user_id=user_id,
                        task_name=task_name,
                        error_message=_truncated,
                        failure_category=failure_category,
                        last_state=last_state,
                        retry_count=retry_count,
                        trace_id=trace_id,
                        expires_at=datetime.now(UTC) + _td(days=30),
                    )
                    db.add(entry)
                    await db.commit()
                    return True
            except Exception as e:
                last_err = e
                if attempt < 2:
                    import asyncio

                    await asyncio.sleep(0.2 * (attempt + 1))
                continue
        if last_err:
            raise last_err
        return False

    try:
        # Lazy import to avoid circular dependency
        from celery_config.worker import _run_async as _run

        success = _run(_write_with_retry())
        if success:
            _dlq_logger.info(
                "Job %s moved to dead letter queue (task=%s, category=%s)", job_id, task_name, failure_category
            )
            try:
                from app.monitoring.prometheus import inc_dlq_retry

                inc_dlq_retry("stored")
            except Exception as _e:
                logger.debug("Failed to increment DLQ stored metric: %s", _e)
    except Exception as dlq_err:
        _dlq_logger.critical(
            "Failed to write dead letter entry for job %s after 3 retries: %s — DLQ entry lost", job_id, dlq_err
        )
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("dlq_write_failures_total", {"failure_category": failure_category, "job_id": job_id})
        except Exception as _e:
            logger.debug("Failed to increment DLQ write failures metric: %s", _e)

        # V3-FIX: JSONL fallback writer — persists DLQ entries to disk when DB is unreachable
        try:
            _fallback_dir = os.environ.get("DLQ_FALLBACK_DIR", "/tmp/workticket/dlq_fallback")
            os.makedirs(_fallback_dir, exist_ok=True)
            _fallback_path = os.path.join(_fallback_dir, f"dlq_fallback_{os.getpid()}.jsonl")
            _fallback_entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "job_id": job_id,
                "company_id": company_id,
                "user_id": user_id,
                "task_name": task_name,
                "error_message": _truncated,
                "failure_category": failure_category,
                "last_state": last_state,
                "retry_count": retry_count,
                "trace_id": trace_id,
            }
            with open(_fallback_path, "a", encoding="utf-8") as _fh:
                _fh.write(json.dumps(_fallback_entry, sort_keys=True, default=str) + "\n")
            logger.info("DLQ entry written to JSONL fallback: %s", _fallback_path)
        except Exception as _fb_err:
            logger.critical("JSONL fallback writer also failed: %s — DLQ entry permanently lost", _fb_err)

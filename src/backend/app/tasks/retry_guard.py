import logging
import os
import threading
import time

from app.sync_redis_pool import get_sync_redis

logger = logging.getLogger(__name__)

_RETRY_WINDOW_SECONDS = 300
_RETRY_MAX_PER_WINDOW = 5

_local_retry_counts: dict = {}
_local_retry_last_cleanup: float = 0.0
_local_retry_lock = threading.Lock()

_REDIS_URL = os.getenv("REDIS_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _check_local(key: str) -> bool:
    global _local_retry_last_cleanup
    now = time.monotonic()
    with _local_retry_lock:
        if now - _local_retry_last_cleanup > 60:
            cutoff = now - _RETRY_WINDOW_SECONDS
            stale = [k for k, v in _local_retry_counts.items() if v[1] < cutoff]
            for k in stale:
                del _local_retry_counts[k]
            _local_retry_last_cleanup = now
        entry = _local_retry_counts.get(key)
        if entry:
            count, ts = entry
            if now - ts > _RETRY_WINDOW_SECONDS:
                _local_retry_counts[key] = [1, now]
                return True
            if count >= _RETRY_MAX_PER_WINDOW:
                logger.error("Retry storm detected locally for %s: %d retries in window", key, count)
                return False
            _local_retry_counts[key] = [count + 1, ts]
            return True
        _local_retry_counts[key] = [1, now]
        return True


def check_retry_storm(job_id: str, task_name: str) -> bool:
    """Pure-sync retry storm guard. No asyncio bridging."""
    key = f"retry:{task_name}:{job_id}"
    try:
        r = get_sync_redis()
        if r is None:
            logger.warning("Redis retry guard unavailable, using local fallback")
            return _check_local(key)
        count = r.get(key)
        count = int(count) if count else 0
        if count >= _RETRY_MAX_PER_WINDOW:
            logger.error(
                "Retry storm detected for %s (job=%s): %d retries in %ds window",
                task_name,
                job_id,
                count,
                _RETRY_WINDOW_SECONDS,
            )
            return False
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _RETRY_WINDOW_SECONDS)
        pipe.execute()
        return True
    except Exception as e:
        logger.warning("Redis retry guard failed, using local fallback: %s", e)
        return _check_local(key)


def get_storm_status() -> dict:
    """Return current retry storm state for /readyz endpoint."""
    now = time.monotonic()
    active_storms = []
    for key, (count, ts) in list(_local_retry_counts.items()):
        if now - ts <= _RETRY_WINDOW_SECONDS and count >= _RETRY_MAX_PER_WINDOW:
            active_storms.append(
                {
                    "key": key,
                    "count": count,
                    "window_seconds": _RETRY_WINDOW_SECONDS,
                }
            )
    try:
        from app.monitoring.prometheus import set_retry_guard_depth

        set_retry_guard_depth(len(_local_retry_counts))
    except Exception:
        logger.debug("Failed to set retry guard depth metric")
        pass  # nosec B110
    return {
        "active_storms": active_storms,
        "total_tracked_keys": len(_local_retry_counts),
    }

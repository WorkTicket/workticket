import atexit
import contextlib
import logging
import os
import signal
import threading

from app.sync_redis_pool import get_sync_redis

logger = logging.getLogger(__name__)

_active_locks: list = []
_shutdown_event = threading.Event()


def _cleanup_all_locks():
    """Release all active beat locks on process termination."""
    _shutdown_event.set()
    for lock in _active_locks:
        with contextlib.suppress(Exception):
            lock.release()


atexit.register(_cleanup_all_locks)

for _sig in (signal.SIGTERM, signal.SIGINT):
    with contextlib.suppress(Exception):
        signal.signal(_sig, lambda signum, frame: _cleanup_all_locks())


class RenewableBeatLock:
    """Redis-based renewable beat execution lock with heartbeat-based TTL extension.

    Acquires a lock with SET NX EX <base_ttl> and starts a background thread
    that refreshes the TTL every <base_ttl/3> seconds. On release, atomically
    DEL the key only if still owned by this worker.
    """

    def __init__(self, task_name: str, base_ttl: int = 300, redis_url: str | None = None):
        self.task_name = task_name
        self.base_ttl = base_ttl
        self.heartbeat_interval = max(5, base_ttl // 3)
        self._redis_url = redis_url or os.getenv("REDIS_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        self._worker_id = f"{os.getpid()}:{threading.get_ident()}"
        self._lock_key = f"beat:lock:{task_name}"
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_heartbeat = threading.Event()
        self._acquired = False

    def acquire(self) -> bool:
        """Acquire the lock. Returns True if acquired."""
        if self._acquired:
            return True
        try:
            r = get_sync_redis()
            if r is None:
                return False
            locked = r.set(self._lock_key, self._worker_id, nx=True, ex=self.base_ttl)
            if locked:
                self._acquired = True
                _active_locks.append(self)
                self._start_heartbeat()
                return True
            return False
        except Exception:
            logger.warning("Redis unreachable for beat lock %s — failing closed to prevent overlap", self.task_name)
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter(
                    "workticket_beat_lock_skipped_total", {"task": self.task_name, "reason": "redis_unavailable"}
                )
            except Exception:
                logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110
            return False

    def _start_heartbeat(self):
        """Start background thread to refresh TTL."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"lock-heartbeat-{self.task_name}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        """Refresh lock TTL every heartbeat_interval seconds."""
        while not self._stop_heartbeat.is_set() and not _shutdown_event.is_set():
            if self._stop_heartbeat.wait(self.heartbeat_interval):
                break
            if not self._acquired:
                break
            try:
                r = get_sync_redis()
                if r is None:
                    continue
                # Only extend TTL if we still own the lock (PEXPIRE with check)
                _owner = r.get(self._lock_key)
                if _owner == self._worker_id:
                    r.expire(self._lock_key, self.base_ttl)
                    try:
                        from app.monitoring.prometheus import increment_counter

                        increment_counter("workticket_beat_lock_ttl_renewed_total", {"task": self.task_name})
                    except Exception:
                        logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110
            except Exception:
                logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110

    def release(self):
        """Atomically release the lock only if still owned by this worker.

        Uses a Lua script to ensure atomic check-and-delete.
        """
        if not self._acquired:
            return
        self._stop_heartbeat.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        try:
            r = get_sync_redis()
            if r is None:
                return
            _release_lua = """
                if redis.call("GET", KEYS[1]) == ARGV[1] then
                    return redis.call("DEL", KEYS[1])
                end
                return 0
            """
            try:
                r.eval(_release_lua, 1, self._lock_key, self._worker_id)
            except Exception:
                r.delete(self._lock_key)
        except Exception:
            logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110
        self._acquired = False
        if self in _active_locks:
            _active_locks.remove(self)
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_beat_lock_contention_total", {"task": self.task_name})
        except Exception:
            logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def acquire_beat_lock(task_name: str, ttl: int = 300, redis_url: str | None = None) -> bool:
    """Simple non-renewable beat lock (backward compatible)."""
    try:
        r = get_sync_redis()
        if r is None:
            return False
        locked = r.set(f"beat:lock:{task_name}", "1", nx=True, ex=ttl)
        return bool(locked)
    except Exception:
        logger.warning("Redis unreachable for beat lock %s — failing closed to prevent overlap", task_name)
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_beat_lock_skipped_total", {"task": task_name})
        except Exception:
            logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110
        return False


def release_beat_lock(task_name: str, redis_url: str | None = None) -> None:
    try:
        r = get_sync_redis()
        if r is None:
            return
        r.delete(f"beat:lock:{task_name}")
    except Exception:
        logger.debug("Beat lock monitoring operation failed, continuing")
        pass  # nosec B110

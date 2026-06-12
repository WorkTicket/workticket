import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class SyncRedisPool:
    """Global synchronous Redis connection pool singleton.

    C-5 FIX: Replaces per-operation create_sync_redis_from_url() calls
    with a shared connection pool to prevent TCP connection exhaustion
    under load. Uses connection pooling with configurable max_connections.
    """

    def __init__(self):
        self.__pool: object | None = None
        self._redis_url: str | None = None
        self._max_connections = int(os.getenv("REDIS_POOL_MAX_CONNECTIONS", "50"))
        self._lock = threading.Lock()
        self._last_health_check = 0.0
        self._health_interval = 15.0
        self._available = False

    @property
    def _pool(self):
        return self.__pool

    @_pool.setter
    def _pool(self, value):
        self.__pool = value

    @_pool.deleter
    def _pool(self):
        self.__pool = None

    def _ensure_pool(self) -> bool:
        now = time.monotonic()
        if self._available and (now - self._last_health_check) < self._health_interval:
            return True
        return self._connect()

    def _connect(self) -> bool:
        try:
            url = os.getenv("REDIS_CACHE_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
            if not url:
                return False

            with self._lock:
                if self._pool is not None and url != self._redis_url:
                    try:
                        self._pool.close()  # type: ignore[attr-defined]
                    except Exception as e:
                        logger.debug("Sync Redis pool close failed: %s", e)
                    self._pool = None

                if self._pool is None:
                    from app.redis_sentinel import create_sync_redis_from_url

                    self._pool = create_sync_redis_from_url(
                        url,
                        socket_connect_timeout=2.0,
                        socket_keepalive=True,
                        max_connections=self._max_connections,
                        decode_responses=True,
                    )
                    self._redis_url = url

                try:
                    self._pool.ping()
                    self._available = True
                    self._last_health_check = time.monotonic()
                    return True
                except Exception as e:
                    logger.debug("Sync Redis pool ping failed: %s", e)
                    self._available = False
                    return False
        except Exception as e:
            self._available = False
            logger.warning("Sync Redis pool connection failed: %s", e)
            return False

    def get_client(self):
        ok = self._ensure_pool()
        if ok and self._pool:
            return self._pool
        return None

    def close(self):
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close()  # type: ignore[attr-defined]
                except Exception as e:
                    logger.debug("Sync Redis pool close failed: %s", e)
                self._pool = None
                self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def get_pool_stats(self) -> dict:
        if self._pool is None:
            return {"available": False}
        try:
            pool = self._pool.connection_pool if hasattr(self._pool, "connection_pool") else None
            return {
                "available": self._available,
                "in_use": getattr(pool, "_in_use_connections", 0) if pool else 0,
                "total": getattr(pool, "_created_connections", 0) if pool else 0,
            }
        except Exception as e:
            logger.debug("Sync Redis pool stats failed: %s", e)
            return {"available": self._available}


# Global singleton
_sync_redis_pool = SyncRedisPool()


def get_sync_redis():
    """Get a Redis client from the global shared sync pool."""
    return _sync_redis_pool.get_client()


def close_sync_redis_pool():
    """Close all connections in the global sync pool."""
    _sync_redis_pool.close()


def get_sync_redis_pool_stats() -> dict:
    return _sync_redis_pool.get_pool_stats()

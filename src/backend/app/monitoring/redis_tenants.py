import logging
import os
import time

logger = logging.getLogger(__name__)

_PER_TENANT_KEY_LIMIT = int(os.getenv("REDIS_PER_TENANT_KEY_LIMIT", "10000"))
_TENANT_KEY_SCAN_INTERVAL = int(os.getenv("REDIS_TENANT_SCAN_INTERVAL", "300"))

_TENANT_NAMESPACE_PREFIXES = {
    "spike": "spike:",
    "spend": "spend:",
    "session_blacklist": "session_blacklist:",
    "ws_conn": "ws_conn:",
    "delivery": "sms:delivery:",
    "email_delivery": "email:delivery:",
    "dlq": "sms:dlq",
    "email_dlq": "email:dlq",
    "retry": "retry:",
    "stripe_dedup": "stripe:dedup:",
    "concurrency": "concurrency:",
    "beat_lock": "beat:lock:",
    "job_lock": "job:lock:",
    "db_circuit": "db:circuit:",
}


class TenantRedisMonitor:
    def __init__(self):
        self._redis = None
        self._last_scan: float = 0
        self._tenant_key_counts: dict[str, int] = {}
        self._tenant_limits: dict[str, int] = {}
        self._total_tenants_over_limit: int = 0
        self._worst_tenant_key_count: int = 0
        self._worst_tenant_id: str = ""

    def _get_sync_redis(self):
        try:
            from app.sync_redis_pool import get_sync_redis

            return get_sync_redis()
        except Exception:
            return None

    def _get_tenant_id_from_key(self, key: str) -> str | None:
        for prefix in _TENANT_NAMESPACE_PREFIXES.values():
            if key.startswith(prefix):
                rest = key[len(prefix) :]
                if ":" in rest:
                    candidate = rest.split(":")[0]
                    if candidate and candidate != "global":
                        return candidate
                elif rest:
                    return rest
        return None

    def scan_tenant_key_counts(self):
        r = self._get_sync_redis()
        if not r:
            return

        try:
            cursor = 0
            tenant_counts: dict[str, int] = {}
            total_scanned = 0

            while True:
                cursor, keys = r.scan(cursor=cursor, count=500)
                if not keys:
                    if cursor == 0:
                        break
                    continue

                for key in keys:
                    key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                    tenant_id = self._get_tenant_id_from_key(key_str)
                    if tenant_id:
                        tenant_counts[tenant_id] = tenant_counts.get(tenant_id, 0) + 1
                    total_scanned += 1

                if cursor == 0:
                    break

            self._tenant_key_counts = tenant_counts
            self._last_scan = time.time()

            over_limit = 0
            worst_count = 0
            worst_id = ""
            for tid, count in tenant_counts.items():
                limit = self._tenant_limits.get(tid, _PER_TENANT_KEY_LIMIT)
                if count > limit:
                    over_limit += 1
                    if count > worst_count:
                        worst_count = count
                        worst_id = tid

            self._total_tenants_over_limit = over_limit
            self._worst_tenant_key_count = worst_count
            self._worst_tenant_id = worst_id

            if over_limit > 0:
                logger.warning(
                    "Redis tenant key limits: %d tenants over limit (worst: %s with %d keys)",
                    over_limit,
                    worst_id,
                    worst_count,
                )

            try:
                from app.monitoring.prometheus import (
                    set_redis_tenant_key_count,
                    set_redis_tenants_over_limit,
                    set_redis_worst_tenant_keys,
                )

                set_redis_tenant_key_count(total_scanned)
                set_redis_tenants_over_limit(over_limit)
                set_redis_worst_tenant_keys(worst_count)
            except Exception:
                logger.debug("Failed to set Redis tenant monitoring metrics")
                pass  # nosec B110

        except Exception as e:
            logger.warning("Redis tenant key scan failed: %s", e)

    def set_tenant_limit(self, tenant_id: str, limit: int):
        self._tenant_limits[tenant_id] = limit

    def get_tenant_key_count(self, tenant_id: str) -> int:
        return self._tenant_key_counts.get(tenant_id, 0)

    def get_stats(self) -> dict:
        return {
            "total_tenants_tracked": len(self._tenant_key_counts),
            "tenants_over_limit": self._total_tenants_over_limit,
            "worst_tenant_id": self._worst_tenant_id,
            "worst_tenant_key_count": self._worst_tenant_key_count,
            "per_tenant_key_limit": _PER_TENANT_KEY_LIMIT,
            "last_scan_ago_seconds": int(time.time() - self._last_scan) if self._last_scan else None,
        }

    def is_tenant_over_limit(self, tenant_id: str) -> bool:
        count = self._tenant_key_counts.get(tenant_id, 0)
        limit = self._tenant_limits.get(tenant_id, _PER_TENANT_KEY_LIMIT)
        return count > limit


tenant_redis_monitor = TenantRedisMonitor()

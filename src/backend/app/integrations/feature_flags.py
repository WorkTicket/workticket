import logging

from app.config import get_settings
from app.integrations.connectors.base import ConnectorFeatureFlag

logger = logging.getLogger(__name__)
settings = get_settings()


class IntegrationFeatureFlags:
    _redis_prefix = "integration_flag:"

    def __init__(self):
        from app.sync_redis_pool import get_sync_redis

        self._get_redis = get_sync_redis

    def _flag_key(self, provider: str, key: str) -> str:
        return f"{self._redis_prefix}{provider}:{key}"

    def is_enabled(self, provider: str) -> bool:
        try:
            r = self._get_redis()
            val = r.get(self._flag_key(provider, "enabled"))
            if val is not None:
                return val == b"1"  # type: ignore[no-any-return]
        except Exception:
            logger.debug("Integration flag Redis operation failed, using default")
        pass  # nosec B110
        return True

    def get_flag(self, provider: str) -> ConnectorFeatureFlag:
        try:
            r = self._get_redis()
            val = r.get(self._flag_key(provider, "flag"))
            if val is not None:
                return ConnectorFeatureFlag(val.decode())
        except Exception:
            logger.debug("Integration flag Redis operation failed, using default")
        pass  # nosec B110
        return ConnectorFeatureFlag.ENABLED

    def set_flag(self, provider: str, flag: ConnectorFeatureFlag):
        try:
            r = self._get_redis()
            r.setex(self._flag_key(provider, "flag"), 86400 * 30, flag.value)
        except Exception as e:
            logger.warning("Failed to set integration flag for %s: %s", provider, e)

    def enable(self, provider: str):
        self.set_flag(provider, ConnectorFeatureFlag.ENABLED)

    def disable(self, provider: str):
        self.set_flag(provider, ConnectorFeatureFlag.DISABLED)

    def set_beta(self, provider: str):
        self.set_flag(provider, ConnectorFeatureFlag.BETA)

    def set_internal(self, provider: str):
        self.set_flag(provider, ConnectorFeatureFlag.INTERNAL)


integration_flags = IntegrationFeatureFlags()

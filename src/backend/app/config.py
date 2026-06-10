import logging
import os
from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.sync_redis_pool import get_sync_redis

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_name: str = "WorkTicket API"
    debug: bool = False

    database_url: str = "__REQUIRED__"
    database_readonly_url: str = ""
    redis_url: str = "__REQUIRED__"
    redis_broker_url: str = ""
    redis_cache_url: str = ""
    redis_sentinel_master_name: str = ""
    redis_sentinel_hosts: str = ""
    redis_sentinel_password: str = ""

    clerk_secret_key: str = "__REQUIRED__"
    clerk_publishable_key: str = "__REQUIRED__"
    clerk_jwt_issuer: str = "__REQUIRED__"
    clerk_jwt_audience: str = "__REQUIRED__"

    ollama_base_url: str = "http://localhost:11434"
    ollama_text_model: str = "llama3.1:8b-q4_0"
    ollama_vision_model: str = "llama3.2-vision:11b-q4_0"
    ollama_timeout: int = 300

    whisper_model_size: str = "base"
    whisper_service_url: str = "http://localhost:8001"
    whisper_api_key: str = ""

    r2_endpoint_url: str = "__REQUIRED__"
    r2_access_key_id: str = "__REQUIRED__"
    r2_secret_access_key: str = "__REQUIRED__"
    r2_bucket_name: str = "workticket-media"
    r2_public_url: str = ""

    stripe_secret_key: str = "__REQUIRED__"
    stripe_webhook_secret: str = "__REQUIRED__"
    stripe_price_id: str = "__REQUIRED__"
    stripe_price_map: str = ""

    sentry_dsn: str = "__REQUIRED__"
    posthog_api_key: str = "__REQUIRED__"
    posthog_host: str = "https://app.posthog.com"
    metrics_access_token: str = "__REQUIRED__"

    twilio_account_sid: str = "__REQUIRED__"
    twilio_auth_token: str = "__REQUIRED__"
    twilio_from_number: str = "__REQUIRED__"

    resend_api_key: str = "__REQUIRED__"

    celery_task_signing_key: str = "__REQUIRED__"
    push_token_encryption_key: str = "__REQUIRED__"
    redis_password: str = "__REQUIRED__"

    api_v1_prefix: str = "/api/v1"

    default_hourly_rate: float = 150.0
    max_images_per_ai_job: int = 5

    allowed_hosts: str = "__REQUIRED__"
    app_base_url: str = "__REQUIRED__"
    cors_origins: str = "__REQUIRED__"
    allowed_domains: str = ""
    max_request_body_size: int = 1_048_576

    db_pool_size: int = 25
    db_max_overflow: int = 10
    db_pool_timeout: int = 30

    celery_pool_size: int = 10
    beat_pool_size: int = 5

    stripe_api_timeout: int = 30

    pgbouncer_enabled: bool = False

    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "workticket-backend"
    stripe_cache_ttl: int = 300

    max_pdf_line_items: int = 500
    default_page_size: int = 25
    max_attachment_limit: int = 10

    ai_disabled: bool = True

    # Per-plan rate limit configuration (tokens/second)
    rl_free_ai_rate: float = 0.5
    rl_free_ai_burst: int = 2
    rl_pro_ai_rate: float = 2.0
    rl_pro_ai_burst: int = 5
    rl_enterprise_ai_rate: float = 10.0
    rl_enterprise_ai_burst: int = 20

    rl_free_requests_rate: float = 2.0
    rl_free_requests_burst: int = 5
    rl_pro_requests_rate: float = 10.0
    rl_pro_requests_burst: int = 20
    rl_enterprise_requests_rate: float = 50.0
    rl_enterprise_requests_burst: int = 100

    @property
    def effective_redis_broker_url(self) -> str:
        if self.redis_broker_url:
            return self.redis_broker_url
        if self.redis_sentinel_hosts and self.redis_sentinel_master_name:
            return self._build_sentinel_url(self.redis_sentinel_master_name)
        return self.redis_url

    @property
    def effective_redis_cache_url(self) -> str:
        if self.redis_cache_url:
            return self.redis_cache_url
        if self.redis_sentinel_hosts and self.redis_sentinel_master_name:
            return self._build_sentinel_url(f"{self.redis_sentinel_master_name}-cache")
        return self.redis_url

    def _build_sentinel_url(self, master_name: str) -> str:
        password = self.redis_sentinel_password or self.redis_password or ""
        from urllib.parse import quote

        pw_part = f":{quote(password, safe='')}" if password else ""
        return f"sentinel://{pw_part}@{self.redis_sentinel_hosts}/{master_name}"

    _SECRET_SUFFIXES = ("secret", "key", "password", "token", "dsn")

    def _is_secret_field(self, field_name: str) -> bool:
        return any(suffix in field_name.lower() for suffix in self._SECRET_SUFFIXES)

    @model_validator(mode="after")
    def check_production_settings(self) -> Self:
        if not self.debug:
            required_fields = [
                "database_url",
                "redis_url",
                "redis_password",
                "clerk_secret_key",
                "clerk_publishable_key",
                "clerk_jwt_issuer",
                "clerk_jwt_audience",
                "r2_endpoint_url",
                "r2_access_key_id",
                "r2_secret_access_key",
                "r2_bucket_name",
                "stripe_secret_key",
                "stripe_webhook_secret",
                "stripe_price_id",
                "sentry_dsn",
                "metrics_access_token",
                "posthog_api_key",
                "twilio_account_sid",
                "twilio_auth_token",
                "twilio_from_number",
                "resend_api_key",
                "celery_task_signing_key",
                "push_token_encryption_key",
                "allowed_hosts",
                "app_base_url",
                "cors_origins",
            ]
            for field in required_fields:
                value = getattr(self, field)
                if not value or value == "__REQUIRED__":
                    raise ValueError(
                        f"{field} must be set in production — app refuses startup without required secrets"
                    )
        if not self.debug and not self.ai_disabled:
            for model_field, model_val in [
                ("ollama_text_model", self.ollama_text_model),
                ("ollama_vision_model", self.ollama_vision_model),
            ]:
                if ":" not in model_val:
                    logger.warning(
                        "Unpinned model %s=%s — upstream updates may change behavior. Pin a version tag (e.g. 'llama3.1:8b-q4_0').",
                        model_field,
                        model_val,
                    )
            if self.cors_origins == "__REQUIRED__":
                raise ValueError("cors_origins must be set in production — do not use wildcard CORS")
            for origin in self.cors_origins.split(","):
                origin = origin.strip()
                if origin == "*":
                    raise ValueError('CORS wildcard origin "*" is not allowed in production')
        if self.allowed_hosts in ("", "__REQUIRED__"):
            raise ValueError(
                'allowed_hosts must be set — controls SSRF protection for AI media downloads (set to "*" to allow all in debug)'
            )
        if not self.debug and self.allowed_hosts == "*":
            raise ValueError(
                'allowed_hosts must not be "*" in production — restricts AI media download targets for SSRF prevention'
            )
        if self.redis_sentinel_hosts:
            try:
                sentinel_url = self._build_sentinel_url(self.redis_sentinel_master_name or "mymaster")
                from urllib.parse import urlparse

                parsed = urlparse(sentinel_url)
                if not parsed.hostname:
                    raise ValueError(f"Invalid sentinel URL: {sentinel_url}")
            except Exception as e:
                raise ValueError(f"Failed to build valid Redis Sentinel URL: {e}") from e

        if not self.debug:
            _env_secrets_detected = []
            for field_name in self.model_fields:
                if self._is_secret_field(field_name):
                    val = getattr(self, field_name, None)
                    if val and val not in ("", "__REQUIRED__"):
                        _detected_val = os.environ.get(field_name.upper(), "")
                        if _detected_val:
                            _env_secrets_detected.append(field_name)
            if _env_secrets_detected:
                logger.warning(
                    "Secrets detected in environment variables in production mode: %s. "
                    "Use a secrets manager (Vault, AWS Secrets Manager) instead of env vars.",
                    ", ".join(_env_secrets_detected),
                )

        # H-9 FIX: Validate stripe_price_map JSON at startup.
        # Previously, malformed JSON silently fell back to the default
        # stripe_price_id, causing incorrect plan pricing.
        if self.stripe_price_map:
            try:
                import json

                parsed = json.loads(self.stripe_price_map)
                if not isinstance(parsed, dict):
                    raise ValueError("stripe_price_map must be a JSON object (dict)")
                for key, val in parsed.items():
                    if not isinstance(key, str) or not isinstance(val, str):
                        raise ValueError(
                            f"stripe_price_map entries must be string key-value pairs, got {type(key)}: {type(val)}"
                        )
                logger.info("stripe_price_map validated: %d plan mappings loaded", len(parsed))
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(f"stripe_price_map is invalid JSON: {e}") from e

        return self

    def __repr__(self) -> str:
        """Sanitize credentials in repr output."""
        safe = {}
        for k, v in self.model_dump().items():
            # L-1 FIX: Also redact __REQUIRED__ sentinel values to prevent accidental exposure
            if isinstance(v, str) and any(
                prefix in k for prefix in ("redis", "password", "secret", "key", "token", "dsn")
            ):
                safe[k] = "***REDACTED***" if v and v not in ("",) else v
            else:
                safe[k] = v
        return f"Settings({safe})"

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()


_REDIS_URL_SANITIZE_REPLACEMENT = "://:****@"


class FeatureFlags:
    """Feature flag system with Redis persistence for gradual rollout.

    MED-7 FIX: Added Redis-backed storage with in-memory cache.
    Flags persist across restarts and can be toggled at runtime via Redis.
    Falls back to in-memory only when Redis is unavailable.
    """

    def __init__(self):
        self._flags: dict[str, bool] = {
            "ai_disabled": os.environ.get("FEATURE_AI_DISABLED", "true").lower() == "true",
            "ai_celery_async": os.environ.get("FEATURE_AI_CELERY_ASYNC", "true").lower() == "true",
            "new_ws_polling": os.environ.get("FEATURE_NEW_WS_POLLING", "false").lower() == "true",
            "billing_v2": os.environ.get("FEATURE_BILLING_V2", "false").lower() == "true",
        }
        self._redis_prefix = "feature_flag:"

    def _get_redis(self):
        try:
            return get_sync_redis()
        except Exception as e:
            logger.debug("FeatureFlags: Redis unavailable: %s", e)
            return None

    def is_enabled(self, flag: str, company_id: str | None = None) -> bool:
        # Check company-specific override first
        if company_id:
            try:
                r = self._get_redis()
                if r:
                    company_val = r.get(f"{self._redis_prefix}company:{company_id}:{flag}")
                    if company_val is not None:
                        return company_val == b"1"
            except Exception as e:
                logger.debug("FeatureFlags: company override check failed: %s", e)
        try:
            r = self._get_redis()
            if r:
                val = r.get(f"{self._redis_prefix}global:{flag}")
                if val is not None:
                    return val == b"1"
        except Exception as e:
            logger.debug("FeatureFlags: global flag check failed: %s", e)
        return self._flags.get(flag, False)

    def enable(self, flag: str, company_id: str | None = None):
        key = f"{self._redis_prefix}company:{company_id}:{flag}" if company_id else f"{self._redis_prefix}global:{flag}"
        self._flags[flag] = True
        try:
            r = self._get_redis()
            if r:
                r.setex(key, 86400 * 30, "1")
        except Exception as e:
            logger.debug("FeatureFlags: enable (setex) failed: %s", e)

    def disable(self, flag: str, company_id: str | None = None):
        key = f"{self._redis_prefix}company:{company_id}:{flag}" if company_id else f"{self._redis_prefix}global:{flag}"
        self._flags[flag] = False
        try:
            r = self._get_redis()
            if r:
                r.setex(key, 86400 * 30, "0")
        except Exception as e:
            logger.debug("FeatureFlags: disable (setex) failed: %s", e)

    def set(self, flag: str, value: bool, company_id: str | None = None):
        if value:
            self.enable(flag, company_id=company_id)
        else:
            self.disable(flag, company_id=company_id)

    # Well-known feature flags
    AI_DISABLED = "ai_disabled"
    AI_CELERY_ASYNC = "ai_celery_async"
    NEW_WS_POLLING = "new_ws_polling"
    BILLING_V2 = "billing_v2"


def sanitize_redis_url(url: str) -> str:
    """Remove the password from a Redis URL for safe logging.

    'redis://:password@host:port/db' -> 'redis://:****@host:port/db'
    'redis://username:password@host:port/db' -> 'redis://:****@host:port/db'
    """
    if not url:
        return url
    try:
        idx = url.rfind("@")
        if idx == -1:
            return url
        prefix = url[:idx]
        colon = prefix.find(":")
        if colon == -1:
            return url
        return url[: colon + 1] + "****" + url[idx:]
    except Exception as e:
        logger.debug("sanitize_redis_url failed: %s", e)
        return url

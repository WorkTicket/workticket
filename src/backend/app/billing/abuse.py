import asyncio
import logging
import os
import time
from datetime import UTC
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.models import BillingAccount, UsageLedger

logger = logging.getLogger(__name__)

_SPEND_ALERT_THRESHOLD = float(os.getenv("ABUSE_SPEND_ALERT_THRESHOLD", "50.0"))


class SpendMonitor:
    def __init__(self):
        self._redis = None
        self._last_health_check: float = 0
        self._health_check_interval = 30.0

    async def _get_redis(self):
        now = time.monotonic()
        if self._redis is not None and now - self._last_health_check < self._health_check_interval:
            return self._redis
        try:
            from app.ai.rate_limiter import _get_redis

            redis_client = await _get_redis()
            if redis_client:
                await redis_client.ping()
                self._redis = redis_client
                self._last_health_check = now
                return self._redis
        except Exception:
            self._redis = None
        # Fallback: create direct connection
        try:
            import redis.asyncio as aioredis

            from app.config import get_settings

            s = get_settings()
            self._redis = aioredis.from_url(
                s.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_keepalive=True,
            )
            await self._redis.ping()
            self._last_health_check = now
            return self._redis
        except Exception:
            self._redis = None
            return None

    async def track_spend(self, company_id: str, amount: float) -> dict | None:
        r = await self._get_redis()
        if not r:
            return None
        try:
            from datetime import datetime

            daily_key = f"spend:{company_id}:{datetime.now(UTC).strftime('%Y%m%d')}"
            new_total = await r.incrbyfloat(daily_key, amount)
            await r.expire(daily_key, 172800)
            if new_total > _SPEND_ALERT_THRESHOLD:
                alert = {
                    "company_id": company_id,
                    "daily_spend": round(new_total, 2),
                    "threshold": _SPEND_ALERT_THRESHOLD,
                    "reason": "daily_spend_exceeded",
                    "severity": "high" if new_total > _SPEND_ALERT_THRESHOLD * 3 else "medium",
                }
                alert_key = f"alert:spend:{company_id}"
                await r.setex(alert_key, 21600, str(alert))
                logger.warning(
                    "Spend alert for company %s: $%.2f (threshold $%.2f)", company_id, new_total, _SPEND_ALERT_THRESHOLD
                )
                return alert
        except Exception as e:
            logger.warning("SpendMonitor.track_spend failed: %s", e)
        return None

    async def get_active_alerts(self) -> list:
        r = await self._get_redis()
        if not r:
            return []
        try:
            keys = await r.keys("alert:spend:*")
            alerts = []
            for k in keys:
                val = await r.get(k)
                if val:
                    import ast

                    alerts.append(ast.literal_eval(val))
            return alerts
        except Exception:
            return []


spend_monitor = SpendMonitor()


class AbuseDetector:
    def __init__(self):
        self._window_seconds = 60
        self._spike_threshold = 10
        self._local_spike_counts: dict = {}
        self._local_spike_cleanup: float = time.monotonic()
        self._estimated_workers: int = int(os.getenv("CELERY_WORKER_REPLICAS", "1"))
        self._spike_lock = asyncio.Lock()

    async def check_and_update_risk(
        self,
        db: AsyncSession,
        company_id: str,
    ) -> int:
        account = await self._get_account(db, company_id, for_update=True)
        if not account:
            return 0

        risk_score = account.risk_score or 0

        spike = await self._check_usage_spike(company_id)
        if spike:
            risk_score += 20

        recent_failures = await self._count_recent_failures(db, company_id)
        if recent_failures > 5:
            risk_score += 10

        if risk_score > 70:
            account.ai_disabled = True
            logger.warning("Company %s AI disabled due to risk score %d", company_id, risk_score)
        elif risk_score > 50:
            if not hasattr(account, "temp_quota_multiplier") or account.temp_quota_multiplier is None:
                account.temp_quota_multiplier = Decimal("1.0")
            account.temp_quota_multiplier = Decimal("0.5")
            logger.warning("Company %s quota temporarily halved due to risk score %d", company_id, risk_score)
        elif (
            risk_score <= 20
            and hasattr(account, "temp_quota_multiplier")
            and account.temp_quota_multiplier is not None
            and account.temp_quota_multiplier < Decimal("1.0")
        ):
            account.temp_quota_multiplier = Decimal("1.0")
            logger.info("Company %s quota restored to normal", company_id)

        account.risk_score = risk_score
        await db.flush()

        return risk_score

    async def record_request(self, company_id: str):
        r = await self._get_redis()
        if r:
            key = f"spike:{company_id}"
            now = time.time()
            try:
                await r.zadd(key, {str(now): now})
                await r.zremrangebyscore(key, 0, now - self._window_seconds)
                await r.expire(key, self._window_seconds + 10)
                return
            except Exception as e:
                logger.warning("Redis spike record failed: %s", e)
        now = time.monotonic()
        async with self._spike_lock:
            if now - self._local_spike_cleanup > 60:
                cutoff = now - self._window_seconds
                self._local_spike_counts = {
                    k: [t for t in v if t > cutoff] for k, v in self._local_spike_counts.items()
                }
                self._local_spike_cleanup = now
            if company_id not in self._local_spike_counts:
                self._local_spike_counts[company_id] = []
            self._local_spike_counts[company_id].append(now)

    async def _check_usage_spike(self, company_id: str) -> bool:
        r = await self._get_redis()
        if r:
            key = f"spike:{company_id}"
            now = time.time()
            try:
                count = await r.zcount(key, now - self._window_seconds, now)
                return count > self._spike_threshold
            except Exception as e:
                logger.error(
                    "Redis spike check failed for company %s — treating as no spike (fail-safe): %s", company_id, e
                )
                # Fall back to local state when Redis fails

        # Fallback to local state when Redis is unavailable or failed
        logger.warning(
            "Redis unavailable for spike check on company %s — using local state (conservative threshold)", company_id
        )
        now = time.monotonic()
        if now - self._local_spike_cleanup > 60:
            cutoff = now - self._window_seconds
            self._local_spike_counts = {k: [t for t in v if t > cutoff] for k, v in self._local_spike_counts.items()}
            self._local_spike_cleanup = now
        if company_id not in self._local_spike_counts:
            return False
        # Clean old entries and check threshold
        self._local_spike_counts[company_id] = [
            t for t in self._local_spike_counts[company_id] if t > now - self._window_seconds
        ]
        # Use conservative threshold: divide by estimated workers to prevent cross-worker evasion
        effective_threshold = max(2, self._spike_threshold // max(1, self._estimated_workers))
        return len(self._local_spike_counts[company_id]) > effective_threshold

    async def _get_redis(self):
        try:
            from app.ai.rate_limiter import _get_redis

            return await _get_redis()
        except Exception:
            return None

    async def _get_account(self, db: AsyncSession, company_id: str, for_update: bool = False):
        from uuid import UUID

        query = select(BillingAccount).where(BillingAccount.company_id == UUID(company_id))
        if for_update:
            query = query.with_for_update()
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def _count_recent_failures(self, db: AsyncSession, company_id: str) -> int:
        from datetime import datetime, timedelta
        from uuid import UUID

        cutoff = datetime.now(UTC) - timedelta(hours=1)
        from sqlalchemy import func as sa_func

        result = await db.execute(
            select(sa_func.count(UsageLedger.id)).where(
                UsageLedger.company_id == UUID(company_id),
                UsageLedger.created_at >= cutoff,
                UsageLedger.cost_usd == 0,
            )
        )
        return result.scalar() or 0

    async def disable_company(self, db: AsyncSession, company_id: str):
        account = await self._get_account(db, company_id)
        if account:
            account.ai_disabled = True
            await db.flush()
            logger.warning("Company %s AI manually disabled", company_id)

    async def enable_company(self, db: AsyncSession, company_id: str):
        account = await self._get_account(db, company_id)
        if account:
            account.ai_disabled = False
            account.risk_score = 0
            await db.flush()
            logger.warning("Company %s AI re-enabled", company_id)

    async def decay_risk_scores(self, db: AsyncSession, decay_rate: int = 5, min_score: int = 0):
        """Decay all risk scores by decay_rate, capping at min_score."""
        from sqlalchemy import update as sa_update

        from app.billing.models import BillingAccount

        stmt = (
            sa_update(BillingAccount)
            .where(BillingAccount.risk_score > min_score)
            .values(risk_score=BillingAccount.risk_score - decay_rate)
        )
        result = await db.execute(stmt)
        if result.rowcount > 0:
            logger.info("Decayed risk scores for %d companies by %d points", result.rowcount, decay_rate)
        await db.flush()


abuse_detector = AbuseDetector()

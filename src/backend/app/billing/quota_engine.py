import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.cost_estimator import ACU_TO_USD, PLAN_TIERS
from app.billing.models import AIJobEstimate, BillingAccount
from app.sync_redis_pool import get_sync_redis

logger = logging.getLogger(__name__)


@dataclass
class QuotaResult:
    allowed: bool
    reason: str = ""
    reserved_acu: float = 0.0


class _ConcurrencyRetryError(Exception):
    pass


class AIQuotaEngine:
    async def get_or_create_account(self, db: AsyncSession, company_id: UUID, plan: str = "free") -> BillingAccount:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        now = datetime.now(UTC)
        tier = PLAN_TIERS.get(plan, PLAN_TIERS["free"])
        stmt = (
            pg_insert(BillingAccount)
            .values(
                company_id=company_id,
                plan=plan,
                monthly_quota_acu=Decimal(str(tier["quota_acu"])),
                used_acu=Decimal("0.0"),
                reserved_acu=Decimal("0.0"),
                billing_period_start=now,
                billing_period_end=now + timedelta(days=30),
                reset_at=now + timedelta(days=30),
                temp_quota_multiplier=Decimal("1.0000"),
            )
            .on_conflict_do_nothing(constraint="billing_accounts_company_id_key")
        )
        await db.execute(stmt)
        result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
        account = result.scalar_one_or_none()
        return account

    async def check_and_reserve(
        self,
        db: AsyncSession,
        company_id: UUID,
        estimated_cost_usd: float,
        job_id: UUID,
        user_id: str | None = None,
    ) -> QuotaResult:
        cost_decimal = Decimal(str(estimated_cost_usd))
        cost_acu = cost_decimal / Decimal(str(ACU_TO_USD))
        max_retries = 3

        for attempt in range(max_retries):
            try:
                async with db.begin_nested():
                    result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
                    account = result.scalar_one_or_none()
                    if not account:
                        account = await self.get_or_create_account(db, company_id)
                    tier = PLAN_TIERS.get(account.plan, PLAN_TIERS["free"])

                    if account.ai_disabled:
                        return QuotaResult(allowed=False, reason="AI_DISABLED")

                    max_per_job = Decimal(str(tier["max_cost_per_job"]))
                    if cost_decimal > max_per_job:
                        return QuotaResult(
                            allowed=False,
                            reason=f"COST_EXCEEDS_TIER_LIMIT: {estimated_cost_usd} > {tier['max_cost_per_job']}",
                        )

                    available = account.monthly_quota_acu - account.used_acu - account.reserved_acu

                    if cost_acu > available:
                        return QuotaResult(
                            allowed=False,
                            reason=f"INSUFFICIENT_QUOTA: need {cost_acu:.4f} ACU, have {available:.4f} ACU",
                        )

                    if user_id:
                        from datetime import datetime

                        from sqlalchemy.dialects.postgresql import insert as pg_insert

                        from app.billing.user_quota import UserDailyUsage, get_user_daily_acu_limit

                        today = datetime.now(UTC).strftime("%Y-%m-%d")
                        daily_limit = get_user_daily_acu_limit(str(company_id) if company_id else None)

                        async def _try_reserve_user_quota(_date=today, _limit=daily_limit):
                            upsert_stmt = (
                                pg_insert(UserDailyUsage)
                                .values(
                                    user_id=user_id,
                                    company_id=company_id,
                                    date=_date,
                                    acu_used=Decimal(str(cost_acu)),
                                    job_count=1,
                                )
                                .on_conflict_do_update(
                                    constraint="uq_user_daily_usage",
                                    set_={
                                        "acu_used": UserDailyUsage.acu_used + Decimal(str(cost_acu)),
                                        "job_count": UserDailyUsage.job_count + 1,
                                    },
                                )
                            )
                            await db.execute(upsert_stmt)

                            result = await db.execute(
                                select(UserDailyUsage)
                                .where(
                                    UserDailyUsage.user_id == user_id,
                                    UserDailyUsage.company_id == company_id,
                                    UserDailyUsage.date == _date,
                                )
                                .with_for_update()
                            )
                            daily = result.scalar_one_or_none()
                            daily_used = float(daily.acu_used) if daily else float(cost_acu)
                            return not daily_used > _limit

                        async with db.begin_nested():
                            allowed = await _try_reserve_user_quota()
                        if not allowed:
                            return QuotaResult(
                                allowed=False,
                                reason=f"USER_DAILY_LIMIT: user {user_id} would exceed daily limit of {daily_limit} ACU",
                            )

                    new_reserved = account.reserved_acu + cost_acu
                    stmt = (
                        update(BillingAccount)
                        .where(BillingAccount.company_id == company_id)
                        .where(BillingAccount.version == account.version)
                        .values(
                            reserved_acu=new_reserved,
                            version=literal_column("version") + 1,
                        )
                    )
                    result = await db.execute(stmt)

                    if result.rowcount == 0:
                        raise _ConcurrencyRetryError()
            except _ConcurrencyRetryError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                return QuotaResult(allowed=False, reason="CONCURRENCY_LIMIT")

            # CRITICAL-4 FIX: Write Redis reservation with TTL for ghost detection.
            # C-5 FIX: Uses shared sync Redis pool instead of per-operation connection.
            if job_id:
                try:
                    _r = get_sync_redis()
                    if _r:
                        _r.setex(f"reservation:{company_id}:{job_id}", 600, str(float(cost_acu)))
                except Exception as e:
                    logger.warning("Failed to write Redis reservation for job %s: %s", job_id, e)

            billing_period = (
                account.billing_period_start.strftime("%Y-%m")
                if account.billing_period_start
                else datetime.now(UTC).strftime("%Y-%m")
            )

            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = (
                pg_insert(AIJobEstimate)
                .values(
                    company_id=company_id,
                    job_id=job_id,
                    estimated_text_cost=Decimal(str(estimated_cost_usd * 0.2)),
                    estimated_vision_cost=Decimal(str(estimated_cost_usd * 0.5 if estimated_cost_usd > 0.005 else 0)),
                    estimated_audio_cost=Decimal(str(estimated_cost_usd * 0.3 if estimated_cost_usd > 0.008 else 0)),
                    estimated_total_cost=Decimal(str(estimated_cost_usd)),
                    approved=True,
                    billing_period=billing_period,
                )
                .on_conflict_do_nothing(constraint="uq_ai_job_estimate_company_job")
            )
            await db.execute(stmt)

            return QuotaResult(
                allowed=True,
                reason="OK",
                reserved_acu=float(cost_acu),
            )

    async def finalize_usage(
        self,
        db: AsyncSession,
        company_id: UUID,
        actual_cost_usd: float,
        reserved_acu: float,
    ):
        actual_acu = Decimal(str(actual_cost_usd)) / Decimal(str(ACU_TO_USD))
        reserved = Decimal(str(reserved_acu))
        max_retries = 3

        for attempt in range(max_retries):
            result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
            account = result.scalar_one_or_none()
            if not account:
                return

            new_reserved = account.reserved_acu - reserved
            if new_reserved < Decimal("0"):
                new_reserved = Decimal("0")
            new_used = account.used_acu + actual_acu

            stmt = (
                update(BillingAccount)
                .where(BillingAccount.company_id == company_id)
                .where(BillingAccount.version == account.version)
                .values(
                    reserved_acu=new_reserved,
                    used_acu=new_used,
                    version=literal_column("version") + 1,
                )
            )
            result = await db.execute(stmt)

            if result.rowcount > 0:
                break

            if attempt < max_retries - 1:
                await asyncio.sleep(0.05 * (2**attempt))
                continue

    async def release_reserved(
        self,
        db: AsyncSession,
        company_id: UUID,
        reserved_acu: float,
        job_id: str | None = None,
    ):
        delta = Decimal(str(reserved_acu))
        max_retries = 3

        for attempt in range(max_retries):
            result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
            account = result.scalar_one_or_none()
            if not account:
                return

            new_reserved = account.reserved_acu - delta
            if new_reserved < Decimal("0"):
                new_reserved = Decimal("0")

            stmt = (
                update(BillingAccount)
                .where(BillingAccount.company_id == company_id)
                .where(BillingAccount.version == account.version)
                .values(
                    reserved_acu=new_reserved,
                    version=literal_column("version") + 1,
                )
            )
            result = await db.execute(stmt)

            if result.rowcount > 0:
                break

            if attempt < max_retries - 1:
                await asyncio.sleep(0.05 * (2**attempt))
                continue

        # CRITICAL-4 FIX: Remove Redis reservation key
        # C-5 FIX: Uses shared sync Redis pool instead of per-operation connection.
        if job_id:
            try:
                _r = get_sync_redis()
                if _r:
                    _r.delete(f"reservation:{company_id}:{job_id}")
            except Exception as e:
                logger.warning("Failed to remove Redis reservation for job %s: %s", job_id, e)

    async def check_quota(
        self,
        db: AsyncSession,
        company_id: UUID,
        estimated_cost_usd: float,
        user_id: str | None = None,
        for_update: bool = False,
    ) -> QuotaResult:
        """Read-only quota check — does NOT reserve any ACU.

        Returns the same QuotaResult as check_and_reserve, but without
        modifying the account balance. Use this in API routes before
        dispatching tasks; the actual reservation happens inside the
        Celery task body to avoid double-reservation on retry.

        When for_update=True, acquires row-level lock to prevent stale reads
        under concurrent quota checks.
        """
        query = select(BillingAccount).where(BillingAccount.company_id == company_id)
        if for_update:
            query = query.with_for_update()
        result = await db.execute(query)
        account = result.scalar_one_or_none()
        if not account:
            account = await self.get_or_create_account(db, company_id)
        tier = PLAN_TIERS.get(account.plan, PLAN_TIERS["free"])

        if account.ai_disabled:
            return QuotaResult(allowed=False, reason="AI_DISABLED")

        cost_decimal = Decimal(str(estimated_cost_usd))
        max_per_job = Decimal(str(tier["max_cost_per_job"]))
        if cost_decimal > max_per_job:
            return QuotaResult(
                allowed=False,
                reason=f"COST_EXCEEDS_TIER_LIMIT: {estimated_cost_usd} > {tier['max_cost_per_job']}",
            )

        available = account.monthly_quota_acu - account.used_acu - account.reserved_acu
        cost_acu = cost_decimal / Decimal(str(ACU_TO_USD))

        if cost_acu > available:
            return QuotaResult(
                allowed=False,
                reason=f"INSUFFICIENT_QUOTA: need {cost_acu:.4f} ACU, have {available:.4f} ACU",
            )

        return QuotaResult(
            allowed=True,
            reason="OK",
            reserved_acu=float(cost_acu),
        )

    async def check_daily_spend(
        self,
        db: AsyncSession,
        company_id: UUID,
        estimated_cost_usd: float,
    ) -> QuotaResult:
        # Read-only — no FOR UPDATE to avoid deadlock with transition_job_state (H8)
        result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
        account = result.scalar_one_or_none()
        if not account:
            account = await self.get_or_create_account(db, company_id)
        tier = PLAN_TIERS.get(account.plan, PLAN_TIERS["free"])
        daily_max = float(tier.get("max_daily_cost_usd", 0))
        if daily_max <= 0:
            return QuotaResult(allowed=True, reason="OK")
        from sqlalchemy import func as sa_func

        from app.billing.models import UsageLedger

        daily_result = await db.execute(
            select(sa_func.coalesce(sa_func.sum(UsageLedger.cost_usd), 0)).where(
                UsageLedger.company_id == company_id,
                UsageLedger.created_at >= sa_func.current_date(),
            )
        )
        daily_spent = float(daily_result.scalar() or 0)
        if daily_spent + estimated_cost_usd > daily_max:
            return QuotaResult(
                allowed=False,
                reason=f"DAILY_SPEND_LIMIT: ${daily_spent:.2f} spent today, ${estimated_cost_usd:.2f} requested, max ${daily_max:.2f}/day",
            )
        return QuotaResult(allowed=True, reason="OK")

    async def get_usage_summary(self, db: AsyncSession, company_id: UUID):
        account = await self.get_or_create_account(db, company_id)
        used = float(account.used_acu)
        reserved = float(account.reserved_acu)
        base_total = float(account.monthly_quota_acu)
        multiplier = float(account.temp_quota_multiplier) if account.temp_quota_multiplier is not None else 1.0
        effective_total = base_total * multiplier
        remaining = effective_total - used - reserved
        usage_pct = round((used / effective_total * 100), 1) if effective_total > 0 else 0

        return {
            "company_id": company_id,
            "plan": account.plan,
            "quota_total": effective_total,
            "quota_base": base_total,
            "quota_multiplier": multiplier,
            "quota_used": used,
            "quota_remaining": max(0, remaining),
            "quota_reserved": reserved,
            "usage_percent": usage_pct,
            "ai_disabled": account.ai_disabled,
            "temp_quota_multiplier": float(account.temp_quota_multiplier)
            if account.temp_quota_multiplier is not None
            else 1.0,
            "reset_at": account.reset_at,
        }


quota_engine = AIQuotaEngine()

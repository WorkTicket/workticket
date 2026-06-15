import hashlib
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, bindparam, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.metrics import increment_counter
from app.billing.cost_estimator import ACU_TO_USD
from app.billing.models import BillingAccount, UsageLedger, _utcnow

logger = logging.getLogger(__name__)


async def reconcile_cost(
    db: AsyncSession,
    company_id: UUID,
    job_id: UUID,
    estimated_cost_usd: float,
    actual_cost_usd: float,
    reserved_acu: float,
    model_used: str,
    execution_time_ms: int,
    text_units: int = 1,
    vision_units: int = 0,
    audio_units: int = 0,
    user_id: str | None = None,
) -> dict:
    # CRITICAL-2 FIX: Use advisory lock on (company_id, job_id) BEFORE BillingAccount lock.
    # This prevents two concurrent retries from both seeing existing_ledger=None.
    _lock_hash = int(hashlib.sha256(f"reconcile:{company_id}:{job_id}".encode()).hexdigest()[:15], 16)
    lock_result = await db.execute(
        sa_text("SELECT pg_try_advisory_xact_lock(:lock_key)").bindparams(
            bindparam("lock_key", type_=BigInteger),
        ),
        {"lock_key": _lock_hash},
    )
    if not lock_result.scalar():
        logger.warning("Concurrent reconciliation lock contention for job %s, company %s", job_id, company_id)
        return {"status": "lock_contention"}

    # Acquire row-level lock on BillingAccount FIRST, THEN check for
    # existing ledger entry inside the locked scope. This prevents a
    # race condition (C2) where two concurrent retries both see
    # existing_ledger=None and both modify the account balance.
    result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id).with_for_update())
    account = result.scalar_one_or_none()
    if not account:
        logger.error("No billing account found for company %s during reconciliation", company_id)
        return {"status": "no_account"}

    # existing_ledger check is inside the with_for_update() scope
    existing = await db.execute(
        select(UsageLedger).where(UsageLedger.job_id == job_id, UsageLedger.company_id == company_id)
    )
    existing_ledger = existing.scalar_one_or_none()

    actual_acu = Decimal(str(actual_cost_usd)) / Decimal(str(ACU_TO_USD))
    reserved = Decimal(str(reserved_acu))

    # Only modify account balances if this is the first reconciliation for this job
    if not existing_ledger:
        if account.reserved_acu < reserved:
            logger.critical(
                "Billing integrity error: company %s reserved_acu (%.4f) < reserved amount (%.4f) — "
                "cannot subtract, raising BillingIntegrityError to prevent silent debt accrual",
                company_id,
                float(account.reserved_acu),
                float(reserved),
            )
            increment_counter("billing_reconciliation_integrity_error_total", {"company_id": str(company_id)})
            from app.billing.state_machine import BillingIntegrityError

            raise BillingIntegrityError(
                f"reserved_acu ({float(account.reserved_acu)}) < reserved amount ({float(reserved)})",
                company_id=company_id,
            )

        account.reserved_acu -= reserved
        account.used_acu += actual_acu

        if account.reserved_acu < Decimal("0"):
            negative_amount = account.reserved_acu
            logger.critical(
                "Billing reconciliation underflow: company %s reserved_acu went to %s — tracking debt. Revenue impact possible.",
                company_id,
                account.reserved_acu,
            )
            increment_counter("billing_reconciliation_underflow_total", {"company_id": str(company_id)})
            account.acu_debt += -negative_amount
            account.reserved_acu = Decimal("0")
            max_debt = account.max_acu_debt or Decimal("500.0")
            if account.acu_debt >= max_debt:
                logger.warning(
                    "Company %s ACU debt (%.4f) exceeded max (%.4f) — disabling AI",
                    company_id,
                    account.acu_debt,
                    max_debt,
                )
                account.ai_disabled = True
                account.ai_disabled_reason = "debt_limit"

        account.last_reconciled = _utcnow()

    drift = actual_acu - reserved

    billing_period = (
        account.billing_period_start.strftime("%Y-%m") if account.billing_period_start else _utcnow().strftime("%Y-%m")
    )

    if existing_ledger:
        existing_ledger.text_units = text_units
        existing_ledger.vision_units = vision_units
        existing_ledger.audio_units = audio_units
        existing_ledger.cost_usd = Decimal(str(actual_cost_usd))
        existing_ledger.estimated_cost_usd = Decimal(str(estimated_cost_usd))
        existing_ledger.model_used = model_used
        existing_ledger.execution_time_ms = execution_time_ms
        existing_ledger.billing_period = billing_period
        if user_id:
            existing_ledger.user_id = user_id
        _usage = existing_ledger
    else:
        # CRITICAL-2 FIX: Use INSERT ... ON CONFLICT DO NOTHING for UsageLedger
        _insert_stmt = (
            pg_insert(UsageLedger)
            .values(
                company_id=company_id,
                job_id=job_id,
                text_units=text_units,
                vision_units=vision_units,
                audio_units=audio_units,
                cost_usd=Decimal(str(actual_cost_usd)),
                estimated_cost_usd=Decimal(str(estimated_cost_usd)),
                model_used=model_used,
                execution_time_ms=execution_time_ms,
                billing_period=billing_period,
                user_id=user_id,
            )
            .on_conflict_do_nothing(
                index_elements=["company_id", "job_id"],
                index_where=sa_text("job_id IS NOT NULL"),
            )
        )
        _insert_result = await db.execute(_insert_stmt)
        if getattr(_insert_result, "rowcount", 0) == 0:
            logger.warning("UsageLedger insert skipped (race detected) for job %s, company %s", job_id, company_id)
            # Fetch the existing ledger that was just inserted by the concurrent retry
            existing = await db.execute(
                select(UsageLedger).where(UsageLedger.job_id == job_id, UsageLedger.company_id == company_id)
            )
            existing_ledger = existing.scalar_one_or_none()
            _usage = existing_ledger or None
        else:
            _usage = None
    await db.flush()

    try:
        from app.monitoring.prometheus import observe_billing_reconciliation_duration, set_billing_drift_pct

        observe_billing_reconciliation_duration(execution_time_ms)
        if drift != 0:
            drift_pct = abs(reserved - actual_acu) / reserved * 100 if reserved > 0 else 0
            set_billing_drift_pct(float(drift_pct))
    except Exception:
        logger.debug("Failed to set billing reconciliation metrics")
        pass  # nosec B110

    logger.info(
        "Cost reconciliation for job %s: estimated=%.6f actual=%.6f drift=%s ACU (%.2f%%)%s",
        job_id,
        estimated_cost_usd,
        actual_cost_usd,
        str(drift),
        (float(reserved) - float(actual_acu)) / float(reserved) * 100 if reserved > 0 else 0,
        " (retry — balance already applied)" if existing_ledger else "",
    )

    return {
        "status": "reconciled",
        "estimated_cost": estimated_cost_usd,
        "actual_cost": actual_cost_usd,
        "drift_acu": round(drift, 4),
        "used_acu_after": float(account.used_acu),
    }


async def get_cost_drift(
    db: AsyncSession,
    company_id: UUID,
    hours: int = 168,
) -> dict:
    from datetime import timedelta

    from sqlalchemy import func as sa_func

    cutoff = _utcnow() - timedelta(hours=hours)
    result = await db.execute(
        select(
            sa_func.count(UsageLedger.id).label("total"),
            sa_func.sum(UsageLedger.estimated_cost_usd).label("total_estimated"),
            sa_func.sum(UsageLedger.cost_usd).label("total_actual"),
        ).where(
            UsageLedger.company_id == company_id,
            UsageLedger.created_at >= cutoff,
            UsageLedger.cost_usd > 0,
        )
    )
    row = result.one()
    total = row.total or 0
    total_est = float(row.total_estimated or 0)
    total_act = float(row.total_actual or 0)

    if total == 0:
        return {"total_jobs": 0, "drift_pct": 0.0, "avg_estimation_error_pct": 0.0}

    drift_pct = ((total_act - total_est) / total_est * 100) if total_est > 0 else 0

    avg_error = await db.execute(
        select(
            sa_func.avg(
                sa_func.abs(
                    (UsageLedger.estimated_cost_usd - UsageLedger.cost_usd)
                    / sa_func.nullif(UsageLedger.estimated_cost_usd, 0)
                )
                * 100
            )
        ).where(
            UsageLedger.company_id == company_id,
            UsageLedger.created_at >= cutoff,
            UsageLedger.estimated_cost_usd > 0,
        )
    )
    avg_pct = float(avg_error.scalar() or 0)

    return {
        "total_jobs": total,
        "total_estimated_cost": round(total_est, 6),
        "total_actual_cost": round(total_act, 6),
        "drift_pct": round(drift_pct, 2),
        "avg_estimation_error_pct": round(avg_pct, 2),
        "time_window_hours": hours,
    }

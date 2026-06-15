import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.models import AIProcessingState, Job

logger = logging.getLogger(__name__)

PROCESSING_TIMEOUT_MINUTES: int = 15


class StateTransitionError(Exception):
    def __init__(self, message: str, job_id: UUID, current: str, target: str):
        self.job_id = job_id
        self.current = current
        self.target = target
        super().__init__(message)


class BillingIntegrityError(Exception):
    def __init__(self, message: str, company_id: UUID):
        self.company_id = company_id
        super().__init__(message)


class ExecutionTransition:
    def __init__(self, from_state: AIProcessingState, to_state: AIProcessingState) -> None:
        self.from_state = from_state
        self.to_state = to_state


_ALLOWED_TRANSITIONS: dict[AIProcessingState, list[AIProcessingState]] = {
    AIProcessingState.none: [AIProcessingState.queued, AIProcessingState.failed],
    AIProcessingState.queued: [AIProcessingState.reserved, AIProcessingState.processing, AIProcessingState.failed],
    AIProcessingState.reserved: [AIProcessingState.processing, AIProcessingState.failed],
    AIProcessingState.processing: [AIProcessingState.completed, AIProcessingState.failed],
    AIProcessingState.completed: [AIProcessingState.none],
    AIProcessingState.failed: [AIProcessingState.compensated, AIProcessingState.queued, AIProcessingState.reserved],
    AIProcessingState.compensated: [AIProcessingState.none],
}

# CRITICAL-5 FIX: Replaced _STATE_CYCLE_MAX_PER_HOUR permanent block
# with exponential backoff (see transition_job_state below).


def validate_transition(current: AIProcessingState, target: AIProcessingState) -> bool:
    allowed = _ALLOWED_TRANSITIONS.get(current, [])
    return target in allowed


async def _reconcile_job_state(
    db: AsyncSession,
    job_id: UUID,
    company_id: UUID,
    expected: AIProcessingState,
    target: AIProcessingState,
) -> Literal["ok", "skip_reservation", "inconsistent", "recovered"]:
    """Safely transition the job state with self-correction.

    Reads the current state with row lock, validates the transition,
    and self-corrects if the state has already progressed past the expected state.
    """
    result = await db.execute(select(Job).where(Job.id == job_id, Job.company_id == company_id).with_for_update())
    job = result.scalar_one_or_none()
    if not job:
        logger.error("Job %s not found for state reconciliation", job_id)
        return "inconsistent"

    current = AIProcessingState(job.ai_processing_state or AIProcessingState.none.value)

    if current == target:
        return "ok"

    if current == expected or current == AIProcessingState.queued:
        if validate_transition(current, target):
            job.ai_processing_state = target.value
            job.ai_processing_updated_at = datetime.now(UTC)
            await db.flush()
            logger.info("Job %s reconciled: %s -> %s", job_id, current.value, target.value)
            return "ok"
        return "inconsistent"

    if current == AIProcessingState.processing and target == AIProcessingState.reserved:
        logger.warning("Job %s already processing — skipping reservation step", job_id)
        return "skip_reservation"

    if current == AIProcessingState.completed:
        logger.warning("Job %s already completed — skipping redundant transition", job_id)
        return "ok"

    if current == AIProcessingState.failed:
        logger.warning("Job %s is in failed state — attempting reset to queued first", job_id)
        if validate_transition(current, AIProcessingState.queued):
            job.ai_processing_state = AIProcessingState.queued.value
            job.ai_processing_updated_at = datetime.now(UTC)
            await db.flush()
            logger.info("Job %s reset from failed -> queued for retry", job_id)
            return "recovered"
        return "inconsistent"

    logger.warning(
        "Job %s in unexpected state %s (expected %s, target %s)",
        job_id,
        current.value,
        expected.value,
        target.value,
    )
    return "inconsistent"


async def _ensure_transition(
    db: AsyncSession,
    job: Job,
    company_id: UUID,
    target: AIProcessingState,
    description: str,
) -> bool:
    """Validate and execute a state transition, raising StateTransitionError on failure."""
    result = await transition_job_state(db, job.id, company_id, target)
    if not result:
        current = AIProcessingState(job.ai_processing_state or AIProcessingState.none.value)
        allowed = _ALLOWED_TRANSITIONS.get(current, [])
        raise StateTransitionError(
            f"Cannot transition job {job.id} from {current.value} to {target.value}. "
            f"Allowed: {[s.value for s in allowed]}",
            job_id=job.id,
            current=current.value,
            target=target.value,
        )
    return True


async def transition_job_state(
    db: AsyncSession,
    job_id: UUID,
    company_id: UUID,
    target_state: AIProcessingState,
    heartbeat_fn: Callable[[AsyncSession, UUID], Awaitable[None]] | None = None,
) -> bool:
    # Lock BillingAccount FIRST to prevent deadlock (HIGH-4)
    # Always lock BillingAccount before Job, matching order in quota_engine.check_and_reserve
    # Retry with backoff on 40P01 deadlock detection
    import asyncio as _asyncio

    from app.billing.models import BillingAccount

    _max_deadlock_retries = 3
    for _deadlock_attempt in range(_max_deadlock_retries):
        try:
            acct_result = await db.execute(
                select(BillingAccount).where(BillingAccount.company_id == company_id).with_for_update()
            )
            acct = acct_result.scalar_one_or_none()
            if acct:
                acct.reservation_heartbeat_at = None
            break
        except Exception as _lock_err:
            err_str = str(_lock_err)
            if ("40P01" in err_str or "deadlock" in err_str.lower()) and _deadlock_attempt < _max_deadlock_retries - 1:
                await _asyncio.sleep(0.1 * (2**_deadlock_attempt))
                continue
            logger.warning("BillingAccount lock acquisition failed (non-deadlock): %s", _lock_err)
            break

    result = await db.execute(select(Job).where(Job.id == job_id, Job.company_id == company_id).with_for_update())
    job = result.scalar_one_or_none()
    if not job:
        logger.error("Job %s not found for state transition to %s", job_id, target_state.value)
        return False

    current = AIProcessingState(job.ai_processing_state or AIProcessingState.none.value)

    # CRITICAL-5 FIX: Replace permanent block with exponential backoff.
    # Uses state_cycle_counter to compute delay between retries:
    #   delay = 60s * 2^(counter-1) capped at max_delay (3600s = 1hr)
    # The counter decays by 1 every hour when the job is not cycling.
    if target_state == AIProcessingState.completed and current == AIProcessingState.processing:
        job.state_cycle_counter = 0
        if hasattr(job, "state_cycle_reset_at"):
            job.state_cycle_reset_at = None

    if target_state == AIProcessingState.queued and current == AIProcessingState.none:
        _now_dt = datetime.now(UTC)
        _now_ts = _now_dt.timestamp()
        # Time-based decay: reduce counter by 1 for each hour since last cycle
        last_reset = getattr(job, "state_cycle_reset_at", None)
        if last_reset is None:
            last_reset_ts = _now_ts
        elif isinstance(last_reset, datetime):
            last_reset_ts = last_reset.timestamp()
        else:
            last_reset_ts = float(last_reset)
        hours_since_reset = max(0.0, (_now_ts - last_reset_ts) / 3600.0)
        effective_counter = max(0, (job.state_cycle_counter or 0) - int(hours_since_reset))
        job.state_cycle_counter = effective_counter + 1
        if hasattr(job, "state_cycle_reset_at"):
            job.state_cycle_reset_at = _now_dt

        if effective_counter > 0:
            delay_seconds = min(60 * (2 ** (effective_counter - 1)), 3600)  # type: ignore[operator]
            elapsed = _now_ts - last_reset_ts
            if elapsed < delay_seconds:
                logger.warning(
                    "Job %s state cycle backoff: cycle %d, delay=%.0fs, elapsed=%.0fs — blocking transition to %s",
                    job_id,
                    effective_counter,
                    delay_seconds,
                    elapsed,
                    target_state.value,
                )
                return False
            logger.info(
                "Job %s state cycle backoff elapsed: cycle %d, delay=%.0fs elapsed=%.0fs — allowing retry",
                job_id,
                effective_counter,
                delay_seconds,
                elapsed,
            )

    if not validate_transition(current, target_state):
        logger.warning(
            "Invalid state transition for job %s: %s -> %s",
            job_id,
            current.value,
            target_state.value,
        )
        return False

    job.ai_processing_state = target_state.value
    job.ai_processing_updated_at = datetime.now(UTC)

    await db.flush()

    if heartbeat_fn:
        await heartbeat_fn(db, company_id)

    logger.info("Job %s state: %s -> %s", job_id, current.value, target_state.value)
    return True


async def heartbeat_reservation(db: AsyncSession, company_id: UUID) -> None:
    from app.billing.models import BillingAccount

    result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
    account = result.scalar_one_or_none()
    if account:
        account.reservation_heartbeat_at = datetime.now(UTC)
        await db.flush()


async def cleanup_stale_reservations(db: AsyncSession) -> None:
    """Release stale reservations and reset associated job states.

    M4-FIX: Also resets the associated job's ai_processing_state from 'reserved'
    or 'processing' back to 'queued' so the job can be re-processed. Previously
    the reservation was freed but the job remained stuck in 'reserved'/'processing'.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=PROCESSING_TIMEOUT_MINUTES)
    from app.billing.models import BillingAccount
    from app.jobs.models import AIProcessingState, Job

    result = await db.execute(
        select(BillingAccount).where(
            BillingAccount.reservation_heartbeat_at.isnot(None),
            BillingAccount.reservation_heartbeat_at < cutoff,
            BillingAccount.reserved_acu > 0,
        )
    )
    stale_accounts = result.scalars().all()
    job_resets = 0
    for account in stale_accounts:
        logger.warning(
            "Releasing stale reservation for company %s: %.4f ACU (last heartbeat %s)",
            account.company_id,
            account.reserved_acu,
            account.reservation_heartbeat_at,
        )
        account.reserved_acu = Decimal("0.0")
        account.reservation_heartbeat_at = None
        await db.flush()

        # M4-FIX: Reset associated jobs from reserved/processing back to queued
        try:
            jobs_result = await db.execute(
                select(Job)
                .where(
                    Job.company_id == account.company_id,
                    Job.ai_processing_state.in_(
                        [
                            AIProcessingState.reserved.value,
                            AIProcessingState.processing.value,
                        ]
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            stuck_jobs = jobs_result.scalars().all()
            for job in stuck_jobs:
                job.ai_processing_state = AIProcessingState.queued.value
                job.ai_processing_updated_at = datetime.now(UTC)
                job_resets += 1
                logger.info("Reset job %s state from %s to queued (stale reservation)", job.id, job.ai_processing_state)
        except Exception as e:
            logger.warning("Failed to reset job states for company %s: %s", account.company_id, e)

    if stale_accounts or job_resets:
        await db.commit()
        logger.info("Cleaned up %d stale reservations, reset %d jobs to queued", len(stale_accounts), job_resets)

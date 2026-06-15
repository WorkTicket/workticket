import logging
from datetime import UTC, datetime, timedelta

from app.billing.state_machine import PROCESSING_TIMEOUT_MINUTES
from app.jobs.models import AIProcessingState

logger = logging.getLogger(__name__)

RESERVED_TIMEOUT_MINUTES = PROCESSING_TIMEOUT_MINUTES
RUNNING_TIMEOUT_MINUTES = 30
QUEUED_TIMEOUT_MINUTES = 60
BATCH_SIZE = 200


def cleanup_stale_jobs():
    from celery_app import _run_async

    return _run_async(_cleanup())


async def _cleanup():
    from decimal import Decimal

    from sqlalchemy import select

    from app.billing.idempotency_service import cleanup_expired_idempotency_keys
    from app.billing.models import BillingAccount
    from app.billing.quota_engine import quota_engine
    from app.billing.state_machine import heartbeat_reservation, transition_job_state
    from app.database import AsyncSessionLocal
    from app.jobs.models import Job
    from app.tracing.models import record_trace

    now = datetime.now(UTC)
    stats = {
        "stuck_reserved": 0,
        "stuck_running": 0,
        "orphaned_queued": 0,
        "orphan_reservations": 0,
        "idempotency_keys": 0,
        "errors": 0,
    }

    try:
        await cleanup_expired_idempotency_keys()
    except Exception as e:
        logger.error("Idempotency key cleanup failed: %s", e)

    async with AsyncSessionLocal() as db:
        try:
            reserved_cutoff = now - timedelta(minutes=RESERVED_TIMEOUT_MINUTES)
            result = await db.execute(
                select(Job)
                .where(
                    Job.ai_processing_state == AIProcessingState.reserved.value,
                    Job.ai_processing_updated_at < reserved_cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(BATCH_SIZE)
            )
            stuck_reserved = result.scalars().all()
            for job in stuck_reserved:
                try:
                    company_id = job.company_id
                    billing = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
                    account = billing.scalar_one_or_none()
                    if account:
                        reserved = float(account.reserved_acu)
                        if reserved > 0:
                            await quota_engine.release_reserved(db, company_id, reserved)
                    transition_ok = await transition_job_state(
                        db, job.id, company_id, AIProcessingState.failed, heartbeat_fn=heartbeat_reservation
                    )
                    if not transition_ok:
                        logger.warning("State transition to failed failed for stuck reserved job %s", job.id)
                    await record_trace(
                        str(job.id),
                        "stuck_reserved_cleanup",
                        "completed",
                        job_id=str(job.id),
                        company_id=str(company_id),
                        error_message="reservation_timeout",
                    )
                    stats["stuck_reserved"] += 1
                except Exception as e:
                    logger.error("Failed to cleanup reserved job %s: %s", job.id, e)
                    stats["errors"] += 1
            if stuck_reserved:
                await db.commit()

            running_cutoff = now - timedelta(minutes=RUNNING_TIMEOUT_MINUTES)
            result = await db.execute(
                select(Job)
                .where(
                    Job.ai_processing_state == AIProcessingState.processing.value,
                    Job.ai_processing_updated_at < running_cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(BATCH_SIZE)
            )
            stuck_running = result.scalars().all()
            for job in stuck_running:
                try:
                    company_id = job.company_id
                    billing = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
                    account = billing.scalar_one_or_none()
                    if account:
                        reserved = float(account.reserved_acu)
                        if reserved > 0:
                            await quota_engine.release_reserved(db, company_id, reserved)
                    transition_ok = await transition_job_state(
                        db, job.id, company_id, AIProcessingState.failed, heartbeat_fn=heartbeat_reservation
                    )
                    if not transition_ok:
                        logger.warning("State transition to failed failed for stuck running job %s", job.id)
                    await record_trace(
                        str(job.id),
                        "stuck_running_cleanup",
                        "completed",
                        job_id=str(job.id),
                        company_id=str(company_id),
                        error_message="execution_timeout",
                    )
                    stats["stuck_running"] += 1
                except Exception as e:
                    logger.error("Failed to cleanup running job %s: %s", job.id, e)
                    stats["errors"] += 1
            if stuck_running:
                await db.commit()

            queued_cutoff = now - timedelta(minutes=QUEUED_TIMEOUT_MINUTES)
            result = await db.execute(
                select(Job)
                .where(
                    Job.ai_processing_state == AIProcessingState.queued.value,
                    Job.ai_processing_updated_at < queued_cutoff,
                )
                .with_for_update(skip_locked=True)
                .limit(BATCH_SIZE)
            )
            orphaned_queued = result.scalars().all()
            for job in orphaned_queued:
                try:
                    company_id = job.company_id
                    billing = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
                    account = billing.scalar_one_or_none()
                    if account:
                        reserved = float(account.reserved_acu)
                        if reserved > 0:
                            await quota_engine.release_reserved(db, company_id, reserved)
                    transition_ok = await transition_job_state(
                        db, job.id, company_id, AIProcessingState.failed, heartbeat_fn=heartbeat_reservation
                    )
                    if not transition_ok:
                        logger.warning("State transition to failed failed for orphaned queued job %s", job.id)
                    await record_trace(
                        str(job.id),
                        "orphaned_queued_cleanup",
                        "completed",
                        job_id=str(job.id),
                        company_id=str(company_id),
                        error_message="queued_timeout_no_worker",
                    )
                    stats["orphaned_queued"] += 1
                except Exception as e:
                    logger.error("Failed to cleanup queued job %s: %s", job.id, e)
                    stats["errors"] += 1
            if orphaned_queued:
                await db.commit()

            orphan_cutoff = now - timedelta(minutes=RESERVED_TIMEOUT_MINUTES)
            result = await db.execute(
                select(BillingAccount).where(
                    BillingAccount.reservation_heartbeat_at.isnot(None),
                    BillingAccount.reservation_heartbeat_at < orphan_cutoff,
                    BillingAccount.reserved_acu > 0,
                )
            )
            orphan_accounts = result.scalars().all()
            for account in orphan_accounts:
                try:
                    logger.warning(
                        "Releasing orphan reservation for company %s: %.4f ACU (last heartbeat %s)",
                        account.company_id,
                        account.reserved_acu,
                        account.reservation_heartbeat_at,
                    )
                    account.reserved_acu = Decimal("0.0")
                    account.reservation_heartbeat_at = None
                    stats["orphan_reservations"] += 1
                except Exception as e:
                    logger.error("Failed to release orphan reservation for company %s: %s", account.company_id, e)
                    stats["errors"] += 1
            if orphan_accounts:
                await db.commit()

        except Exception as e:
            logger.error("Stale job cleanup failed: %s", e)
            stats["errors"] += 1

    total = stats["stuck_reserved"] + stats["stuck_running"] + stats["orphaned_queued"] + stats["orphan_reservations"]
    if total > 0:
        logger.info(
            "Stale job cleanup complete: %d resolved (%d reserved, %d running, %d queued, %d orphan_reservations, %d errors)",
            total,
            stats["stuck_reserved"],
            stats["stuck_running"],
            stats["orphaned_queued"],
            stats["orphan_reservations"],
            stats["errors"],
        )

    try:
        from app.monitoring.prometheus import set_stuck_jobs

        set_stuck_jobs("reserved", stats["stuck_reserved"])
        set_stuck_jobs("running", stats["stuck_running"])
        set_stuck_jobs("queued", stats["orphaned_queued"])
    except Exception:
        logger.debug("Failed to set stuck job metrics")
        pass  # nosec B110

    return stats

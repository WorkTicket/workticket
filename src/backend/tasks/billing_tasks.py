import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from celery_config.beat import _acquire_beat_lock
from celery_config.broker import _move_to_dead_letter, get_sync_redis
from celery_config.worker import _run_async, celery_app

logger = logging.getLogger(__name__)


def _record_beat_execution(task_name: str, iteration_count: int = 0):
    """Record beat task execution timestamp and iteration count for Prometheus."""
    import time as _time

    try:
        from app.monitoring.prometheus import set_beat_task_execution_timestamp, set_beat_task_iteration_count

        set_beat_task_execution_timestamp(task_name, _time.time())
        set_beat_task_iteration_count(task_name, iteration_count)
    except Exception:
        pass


@celery_app.task(bind=True, max_retries=5, default_retry_delay=60, queue="default")
def send_email_task(self, to: str, subject: str, body: str, company_id: str = ""):
    """Send email via Resend with exponential backoff retry and DLQ on final failure."""
    try:
        import resend

        resend.Emails.send(
            to=to,
            subject=subject,
            body=body,
        )
        logger.info("Email sent to %s (subject=%s)", to, subject)
        return {"status": "sent", "to": to}
    except Exception as e:
        logger.error("Email send failed to %s: %s", to, e)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (2**self.request.retries)) from e
        _move_to_dead_letter(
            job_id="",
            company_id=company_id or "",
            user_id="",
            task_name="send_email",
            error_message=str(e),
            failure_category="email_failure",
            last_state="failed",
            retry_count=self.request.retries,
        )
        return {"status": "failed", "error": str(e)}


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, queue="default")
def refresh_stripe_ips_task(self):
    from app.billing.stripe_ips import refresh_stripe_ips as _refresh

    try:
        _run_async(_refresh(force=True))
        logger.info("Stripe IPs refreshed successfully")
    except Exception as e:
        error_str = str(e)
        logger.error("Stripe IP refresh failed: %s", error_str)
        if self.request.retries >= (self.max_retries or 3):
            logger.critical("refresh_stripe_ips_task exhausted retries — writing to DLQ")
            _move_to_dead_letter(
                job_id="",
                company_id="",
                user_id="",
                task_name="refresh_stripe_ips_task",
                error_message=error_str,
                failure_category="stripe_ip_refresh",
                last_state="failed",
                retry_count=self.request.retries,
            )
            return {"status": "failed", "error": error_str, "failure_type": "stripe_ip_refresh"}
        raise self.retry(exc=e) from e


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, queue="beat")
def decay_risk_scores_task(self):
    if not _acquire_beat_lock(self.app, "decay_risk_scores_task", ttl=3600):
        logger.warning("decay_risk_scores_task skipped — another execution is in progress")
        return {"status": "skipped", "reason": "concurrent_execution_locked"}

    from app.billing.abuse import abuse_detector
    from app.database import AsyncSessionLocal, is_retryable_error

    try:

        async def _run():
            async with AsyncSessionLocal() as db:
                await abuse_detector.decay_risk_scores(db, decay_rate=5, min_score=0)
                await db.commit()

        _run_async(_run())
        _record_beat_execution("decay_risk_scores_task")
    except Exception as e:
        _record_beat_execution("decay_risk_scores_task", -1)
        if is_retryable_error(e) and self.request.retries < (self.max_retries or 3):
            logger.warning(
                "Serialization failure on decay_risk_scores (attempt %d/%d): %s — retrying",
                self.request.retries + 1, (self.max_retries or 3) + 1, e,
            )
            raise self.retry(exc=e, countdown=2 ** (self.request.retries + 1)) from e
        logger.error("Risk score decay task failed: %s", e)
        raise self.retry(exc=e) from e


# H-6: Removed _acquire_billing_reconciliation_lock.
# Both reset_billing_quotas and collect_billing_debt use their own
# dedicated lock keys (beat:lock:reset_billing_quotas, beat:lock:collect_billing_debt).
# The shared lock:billing:reconciliation lock was redundant and caused
# unnecessary task skipping when one task held the lock but not the other.


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, queue="default")
def reset_billing_quotas(self):
    try:
        from app.database import _check_db_circuit

        _check_db_circuit()
    except Exception as cb_err:
        logger.warning("reset_billing_quotas skipped — DB circuit breaker open: %s", cb_err)
        return {"status": "skipped", "reason": "db_circuit_open"}

    from datetime import datetime

    from sqlalchemy import or_, select, text

    from app.billing.models import BillingAccount
    from app.billing.state_machine import PROCESSING_TIMEOUT_MINUTES
    from app.database import AsyncSessionLocal, is_retryable_error

    # H-6: Execution lock to prevent overlapping beat executions
    # Removed the shared billing reconciliation lock — each task uses its own lock.
    try:
        _lock_redis = get_sync_redis()
        if _lock_redis is None:
            raise ConnectionError("Redis pool unavailable")
        from datetime import datetime as _dt

        _billing_period = _dt.now(UTC).strftime("%Y-%m")
        _lock_key = "beat:lock:reset_billing_quotas"
        _period_key = f"billing:reset:{_billing_period}"
        # HIGH-6 FIX: Single Lua script atomically acquires BOTH locks.
        # Replaces two separate SET NX calls to prevent the race where two
        # concurrent beats each succeed on execution lock but one misses the
        # period lock due to replication lag.
        _lua = """
            local exec_key = KEYS[1]
            local period_key = KEYS[2]
            local exec_ttl = tonumber(ARGV[1])
            local period_ttl = tonumber(ARGV[2])
            local exec_ok = redis.call('SET', exec_key, '1', 'NX', 'EX', exec_ttl)
            if not exec_ok then
                return {0, 0, 'concurrent_execution_locked'}
            end
            local period_ok = redis.call('SET', period_key, '1', 'NX', 'EX', period_ttl)
            if not period_ok then
                return {1, 0, 'billing_period_already_reset'}
            end
            return {1, 1, 'ok'}
        """
        _locked, _period_locked, _reason = _lock_redis.eval(_lua, 2, _lock_key, _period_key, 3600, 86400)
        if not _locked or _locked == 0:
            if _locked == 0:
                logger.warning("reset_billing_quotas skipped — %s", _reason)
                return {"status": "skipped", "reason": _reason or "concurrent_execution_locked"}
            # _locked is 1 but _period_locked is 0
            if not _period_locked or _period_locked == 0:
                logger.info("Billing period %s already reset — skipping duplicate reset", _billing_period)
                try:
                    from app.monitoring.prometheus import increment_counter

                    increment_counter("workticket_billing_concurrent_reset_skipped_total", {"period": _billing_period})
                except Exception as _e:
                    logger.debug("Failed to increment billing concurrent reset metric: %s", _e)
                return {"status": "skipped", "reason": "billing_period_already_reset"}
    except Exception as e:
        logger.error("Redis unreachable for reset_billing_quotas lock: %s", e)
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter(
                "workticket_beat_lock_skipped_total", {"task": "reset_billing_quotas", "reason": "redis_unavailable"}
            )
        except Exception as _e:
            logger.debug("Failed to increment beat lock skipped metric: %s", _e)
        if self.request.retries >= (self.max_retries or 3):
            logger.critical(
                "reset_billing_quotas exhausted retries due to Redis unavailability — billing quotas not reset"
            )
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter(
                    "workticket_beat_lock_skipped_total",
                    {"task": "reset_billing_quotas", "reason": "redis_unavailable_retries_exhausted"},
                )
            except Exception as _e:
                logger.debug("Failed to increment critical metric: %s", _e)
            _move_to_dead_letter(
                job_id="",
                company_id="",
                user_id="",
                task_name="reset_billing_quotas",
                error_message="Redis unreachable after max retries — billing quotas not reset",
                failure_category="redis_unavailable",
                last_state="failed",
                retry_count=self.request.retries,
            )
            return {"status": "failed", "error": "redis_unavailable_retries_exhausted"}
        raise self.retry(exc=e) from e

    # HIGH-5 FIX: Per-account savepoints, reduced page size, session timeout
    try:

        async def _run():
            async with AsyncSessionLocal() as db:
                # Set session-level statement_timeout (30s for the whole task)
                try:
                    await db.execute(text("SET LOCAL statement_timeout = '30000'"))
                except Exception as _e:
                    logger.debug("Failed to set statement timeout: %s", _e)

                PAGE_SIZE = 20
                total_reset = 0
                total_debt_collected = 0
                total_stale_reset = 0
                now = datetime.now(UTC)
                stale_cutoff = now - timedelta(minutes=PROCESSING_TIMEOUT_MINUTES)

                # Count due accounts without active heartbeat (skip active reservations)
                count_result = await db.execute(
                    select(BillingAccount.id).where(
                        BillingAccount.reset_at <= now,
                        or_(
                            BillingAccount.reservation_heartbeat_at.is_(None),
                            BillingAccount.reservation_heartbeat_at < stale_cutoff,
                        ),
                    )
                )
                total_count = len(count_result.scalars().all())

                # Phase 1: Reset due accounts with no recent heartbeat
                skipped_company_ids = []
                offset = 0
                while True:
                    due_page = await db.execute(
                        select(BillingAccount)
                        .where(
                            BillingAccount.reset_at <= now,
                            or_(
                                BillingAccount.reservation_heartbeat_at.is_(None),
                                BillingAccount.reservation_heartbeat_at < stale_cutoff,
                            ),
                        )
                        .order_by(BillingAccount.company_id)
                        .offset(offset)
                        .limit(PAGE_SIZE)
                        .with_for_update(nowait=True)
                    )
                    page_accounts = due_page.scalars().all()
                    if not page_accounts:
                        break

                    for account in page_accounts:
                        # HIGH-5: Use per-account savepoint for atomicity
                        try:
                            async with db.begin_nested():
                                result = await db.execute(
                                    select(BillingAccount)
                                    .where(BillingAccount.company_id == account.company_id)
                                    .with_for_update(nowait=True)
                                )
                                fresh_account = result.scalar_one_or_none()
                                if not fresh_account:
                                    continue
                                account = fresh_account
                                # Collect acu_debt before resetting (P0-D)
                                if account.acu_debt and account.acu_debt > Decimal("0.01"):
                                    monthly_quota = account.monthly_quota_acu
                                    if account.acu_debt > monthly_quota:
                                        logger.critical(
                                            "Billing debt threshold exceeded for company %s: debt=%.4f ACU > monthly_quota=%.4f — "
                                            "capping deduction",
                                            account.company_id,
                                            float(account.acu_debt),
                                            float(monthly_quota),
                                        )
                                        from app.monitoring.prometheus import increment_counter

                                        increment_counter(
                                            "billing_debt_threshold_exceeded_total",
                                            {"company_id": str(account.company_id)},
                                        )
                                        account.used_acu = account.acu_debt - monthly_quota
                                        account.acu_debt = Decimal("0")
                                    else:
                                        account.used_acu += account.acu_debt
                                        account.acu_debt = Decimal("0")
                                    total_debt_collected += 1

                                account.used_acu = Decimal("0")
                                account.reserved_acu = Decimal("0")
                                account.reservation_heartbeat_at = None
                                account.billing_period_start = now
                                account.reset_at = now + timedelta(days=30)
                                total_reset += 1
                        except Exception as lock_err:
                            skipped_company_ids.append(account.company_id)
                            logger.warning(
                                "Could not acquire lock for billing account %s (will retry next cycle): %s",
                                account.company_id,
                                lock_err,
                            )
                            continue

                    offset += PAGE_SIZE

                # H8-FIX: Retry skipped accounts up to 3 times
                if skipped_company_ids:
                    for retry_attempt in range(3):
                        still_skipped = []
                        for company_id in skipped_company_ids:
                            try:
                                async with db.begin_nested():
                                    result = await db.execute(
                                        select(BillingAccount)
                                        .where(BillingAccount.company_id == company_id)
                                        .with_for_update(nowait=True)
                                    )
                                    account = result.scalar_one_or_none()
                                    if account:
                                        if account.acu_debt and account.acu_debt > Decimal("0.01"):
                                            monthly_quota = account.monthly_quota_acu
                                            if account.acu_debt > monthly_quota:
                                                account.used_acu = account.acu_debt - monthly_quota
                                                account.acu_debt = Decimal("0")
                                            else:
                                                account.used_acu += account.acu_debt
                                                account.acu_debt = Decimal("0")
                                            total_debt_collected += 1
                                        account.used_acu = Decimal("0")
                                        account.reserved_acu = Decimal("0")
                                        account.reservation_heartbeat_at = None
                                        account.billing_period_start = now
                                        account.reset_at = now + timedelta(days=30)
                                        total_reset += 1
                            except Exception:
                                still_skipped.append(company_id)
                        skipped_company_ids = still_skipped
                        if not skipped_company_ids:
                            break
                        await asyncio.sleep(0.5 * (retry_attempt + 1))

                # Phase 2: Reset accounts with stale heartbeats unconditionally
                stale_page = await db.execute(
                    select(BillingAccount)
                    .where(
                        BillingAccount.reservation_heartbeat_at.isnot(None),
                        BillingAccount.reservation_heartbeat_at < stale_cutoff,
                        BillingAccount.reset_at <= now,
                    )
                    .with_for_update(nowait=True)
                )
                stale_accounts = stale_page.scalars().all()
                for account in stale_accounts:
                    try:
                        async with db.begin_nested():
                            account.used_acu = Decimal("0")
                            account.reserved_acu = Decimal("0")
                            account.reservation_heartbeat_at = None
                            account.billing_period_start = now
                            account.reset_at = now + timedelta(days=30)
                            total_stale_reset += 1
                    except Exception as lock_err:
                        logger.warning(
                            "Could not acquire lock for stale billing account %s: %s", account.company_id, lock_err
                        )
                        continue

                if total_reset or total_stale_reset or total_debt_collected:
                    await db.commit()
                    logger.info(
                        "Reset billing quotas: %d fresh + %d stale = %d accounts (of %d due), "
                        "collected debt from %d accounts",
                        total_reset,
                        total_stale_reset,
                        total_reset + total_stale_reset,
                        total_count,
                        total_debt_collected,
                    )

                # Report ghost reservation count
                try:
                    from app.monitoring.prometheus import set_ghost_reservations

                    ghost_count = await db.execute(
                        select(BillingAccount).where(
                            BillingAccount.reserved_acu > Decimal("0"),
                            or_(
                                BillingAccount.reservation_heartbeat_at.is_(None),
                                BillingAccount.reservation_heartbeat_at < stale_cutoff,
                            ),
                        )
                    )
                    set_ghost_reservations(len(ghost_count.scalars().all()))
                except Exception as _e:
                    logger.debug("Failed to set ghost reservations metric: %s", _e)

        _run_async(_run())
        _record_beat_execution("reset_billing_quotas")
    except Exception as e:
        _record_beat_execution("reset_billing_quotas", -1)
        if is_retryable_error(e) and self.request.retries < (self.max_retries or 3):
            logger.warning(
                "Serialization failure on reset_billing_quotas (attempt %d/%d): %s — retrying",
                self.request.retries + 1, (self.max_retries or 3) + 1, e,
            )
            raise self.retry(exc=e, countdown=2 ** (self.request.retries + 1)) from e
        logger.error("Billing quota reset failed: %s", e)
        raise self.retry(exc=e) from e


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, queue="beat")
def collect_billing_debt(self):
    """Collect acu_debt from billing accounts and apply it to used_acu."""
    from datetime import datetime

    from sqlalchemy import select

    from app.ai.failure_classifier import classify_failure
    from app.billing.models import BillingAccount
    from app.database import AsyncSessionLocal, is_retryable_error
    from app.monitoring.prometheus import set_acu_debt

    # H-6: Execution lock to prevent overlapping beat executions
    # Removed the shared billing reconciliation lock — each task uses its own lock.
    try:
        _lock_redis = get_sync_redis()
        if _lock_redis is None:
            raise ConnectionError("Redis pool unavailable")
        _lock_key = "beat:lock:collect_billing_debt"
        _locked = _lock_redis.set(_lock_key, "1", nx=True, ex=1800)
        if not _locked:
            logger.warning("collect_billing_debt skipped — another execution is in progress")
            return {"status": "skipped", "reason": "concurrent_execution_locked"}
    except Exception:
        logger.warning("Redis unreachable for collect_billing_debt lock — skipping to prevent overlap")
        return {"status": "skipped", "reason": "redis_unavailable"}

    try:

        async def _run():
            async with AsyncSessionLocal() as db:
                debtor_accounts = await db.execute(
                    select(BillingAccount)
                    .where(BillingAccount.acu_debt > Decimal("0.01"))
                    .order_by(BillingAccount.company_id)
                    .with_for_update(nowait=True)
                )
                accounts = debtor_accounts.scalars().all()
                total_debt = Decimal("0")
                debt_threshold_alerts = 0

                for account in accounts:
                    if account.acu_debt > Decimal("1.0"):
                        logger.critical(
                            "Billing debt threshold exceeded for company %s: debt=%.4f ACU (>$1)",
                            account.company_id,
                            float(account.acu_debt),
                        )
                        debt_threshold_alerts += 1

                    total_debt += account.acu_debt
                    account.used_acu = account.used_acu + account.acu_debt
                    account.acu_debt = Decimal("0")
                    account.last_reconciled = datetime.now(UTC)

                if accounts:
                    await db.commit()
                    logger.info(
                        "Collect billing debt from %d accounts (total=%.4f ACU, %d threshold alerts)",
                        len(accounts),
                        float(total_debt),
                        debt_threshold_alerts,
                    )
                    try:
                        set_acu_debt(float(total_debt))
                    except Exception as _e:
                        logger.debug("Failed to set ACU debt metric: %s", _e)

        _run_async(_run())
        _record_beat_execution("collect_billing_debt")
    except Exception as e:
        error_str = str(e)
        _record_beat_execution("collect_billing_debt", -1)
        if is_retryable_error(e) and self.request.retries < (self.max_retries or 3):
            logger.warning(
                "Serialization failure on collect_billing_debt (attempt %d/%d): %s — retrying",
                self.request.retries + 1, (self.max_retries or 3) + 1, e,
            )
            raise self.retry(exc=e, countdown=2 ** (self.request.retries + 1)) from e
        failure_cat = classify_failure(error_str)
        logger.error("Billing debt collection failed: %s (category=%s)", error_str, failure_cat.value)
        if self.request.retries >= (self.max_retries or 3):
            logger.critical("collect_billing_debt exhausted retries — writing to DLQ")
            _move_to_dead_letter(
                job_id="",
                company_id="",
                user_id="",
                task_name="collect_billing_debt",
                error_message=error_str,
                failure_category=failure_cat.value,
                last_state="failed",
                retry_count=self.request.retries,
            )
            return {"status": "failed", "error": error_str, "failure_type": failure_cat.value}
        raise self.retry(exc=e) from e


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60, queue="beat")
def purge_expired_dlq_entries(self):
    """Purge DLQ entries with expires_at in the past."""
    if not _acquire_beat_lock(self.app, "purge_expired_dlq_entries", ttl=86400):
        logger.warning("purge_expired_dlq_entries skipped — another execution is in progress")
        return {"status": "skipped", "reason": "concurrent_execution_locked"}

    from sqlalchemy import delete

    from app.billing.dead_letter import DeadLetterJob
    from app.database import AsyncSessionLocal, is_retryable_error

    try:

        async def _run():
            async with AsyncSessionLocal() as db:
                now = datetime.now(UTC)
                stmt = delete(DeadLetterJob).where(DeadLetterJob.expires_at < now)
                result = await db.execute(stmt)
                await db.commit()
                purged = result.rowcount if result.rowcount else 0
                if purged:
                    logger.info("Purged %d expired DLQ entries", purged)
                return {"purged": purged}

        _run_async(_run())
        _record_beat_execution("purge_expired_dlq_entries")
    except Exception as e:
        _record_beat_execution("purge_expired_dlq_entries", -1)
        if is_retryable_error(e) and self.request.retries < (self.max_retries or 2):
            logger.warning(
                "Serialization failure on purge_expired_dlq (attempt %d/%d): %s — retrying",
                self.request.retries + 1, (self.max_retries or 2) + 1, e,
            )
            raise self.retry(exc=e, countdown=2 ** (self.request.retries + 1)) from e
        logger.error("Expired DLQ purge failed: %s", e)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60, queue="beat")
def cleanup_dlq_fallback_files(self):
    """M-1: Periodic cleanup of stale DLQ fallback files older than 1 hour."""
    # M-7 FIX: Default to /tmp/ which is tmpfs-writable in Docker/K8s read-only root.
    # For production, set DLQ_FALLBACK_DIR to a persistent volume to prevent OOM kills.
    _DLQ_FALLBACK_DIR = os.environ.get("DLQ_FALLBACK_DIR", "/tmp/workticket/dlq_fallback")
    if not os.path.isdir(_DLQ_FALLBACK_DIR):
        return {"purged": 0, "reason": "no_fallback_dir"}
    _cutoff = time.time() - 3600
    purged = 0
    for f in os.listdir(_DLQ_FALLBACK_DIR):
        fpath = os.path.join(_DLQ_FALLBACK_DIR, f)
        if f.startswith("workticket_dlq_fallback.") and f.endswith(".jsonl"):
            try:
                if os.path.getmtime(fpath) < _cutoff:
                    os.remove(fpath)
                    purged += 1
            except Exception as _e:
                logger.debug("Failed to cleanup DLQ fallback file %s: %s", f, _e)
    if purged:
        logger.info("Cleaned up %d stale DLQ fallback files (cutoff=%ds ago)", purged, 3600)
    return {"purged": purged}

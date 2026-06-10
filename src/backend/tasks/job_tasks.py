import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

from celery_config.broker import (
    _WORKER_VERSION,
    MAX_SUPPORTED_VERSION,
    MIN_SUPPORTED_VERSION,
    PAYLOAD_VERSION,
    CeleryTaskAdapter,
    _get_pub_redis,
    _get_signing_key,
    _move_to_dead_letter,
    _verify_task_payload,
    get_sync_redis,
)
from celery_config.worker import _run_async, celery_app

logger = logging.getLogger(__name__)


async def _release_reservation_with_retry(
    company_id: str, reserved_acu: float, job_id: str, trace_id: str = "", task_logger=None
):
    """C-4 FIX: Release ACU reservation with retry logic and compensating credit entry.

    Tries to release the reservation 3 times with exponential backoff.
    If all retries fail, writes a compensating credit ledger entry so the
    customer's quota is restored even if the release operation itself fails.
    """
    log = task_logger or logger
    last_err = None
    for attempt in range(3):
        try:
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as release_db:
                from app.billing.quota_engine import quota_engine

                await quota_engine.release_reserved(release_db, company_id, reserved_acu, job_id=job_id)
                await release_db.commit()
                log.info("Released reservation %.4f ACU for job %s (attempt %d)", reserved_acu, job_id, attempt + 1)
                return
        except Exception as e:
            last_err = e
            log.warning("Reservation release attempt %d failed for job %s: %s", attempt + 1, job_id, e)
            if attempt < 2:
                import asyncio as _aio

                await _aio.sleep(0.5 * (2**attempt))
    # All retries exhausted — write compensating credit ledger entry
    log.error("All reservation release attempts failed for job %s — writing compensating credit", job_id)
    try:
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as credit_db:
            from uuid import UUID

            from app.billing.credits import grant_credit

            await grant_credit(
                db=credit_db,
                company_id=UUID(company_id) if isinstance(company_id, str) else company_id,
                job_id=UUID(job_id) if isinstance(job_id, str) else job_id,
                amount_acu=float(reserved_acu),
                reason=f"auto_compensate: reservation_release_failed after retries: {last_err}",
                granted_by="system",
            )
            await credit_db.commit()
            log.info("Compensating credit of %.4f ACU written for job %s", reserved_acu, job_id)
    except Exception as credit_err:
        log.error(
            "Failed to write compensating credit for job %s: %s — manual reconciliation required", job_id, credit_err
        )


async def _compensate_failed_job(
    job_id: str,
    company_id: str,
    failure_msg: str,
    db=None,
    trace_id: str = "",
    retry_count: int = 0,
    max_retries: int = 3,
):
    """Only compensate if no successful output exists and track compensation attempts.

    H3-FIX: Checks retry state machine position before deleting partial outputs.
    If this is the last retry (retry_count >= max_retries), compensation skips
    deleting partial outputs to avoid the scenario where a partial output with
    high confidence exists and the final retry fails on commit.
    """
    try:
        from sqlalchemy import select

        from app.jobs.models import AIOutput, Job

        # H3-FIX: If this is the final retry, check if any valid output exists.
        # If no valid output, still clean up and mark with specific failure code.
        if retry_count >= max_retries:
            try:
                from app.database import AsyncSessionLocal

                async with AsyncSessionLocal() as _validate_session:
                    _existing = await _validate_session.execute(
                        select(AIOutput).where(
                            AIOutput.job_id == job_id,
                            AIOutput.company_id == company_id,
                            AIOutput.confidence_score > 0.5,
                        )
                    )
                    if _existing.scalar_one_or_none():
                        logger.info("Final retry: valid output exists for job %s, preserving", job_id)
                        return
            except Exception as _e:
                logger.debug("Final retry validation DB check failed: %s", _e)
            logger.info("Final retry: no valid output for job %s, cleaning up partial state", job_id)
            return {"status": "no_output_on_final_retry"}

        async def _do_compensate(session):
            existing = await session.execute(
                select(AIOutput).where(
                    AIOutput.job_id == job_id,
                    AIOutput.company_id == company_id,
                    AIOutput.confidence_score > 0.5,
                )
            )
            if existing.scalar_one_or_none():
                logger.info("Skipping compensation for job %s — valid output exists (confidence > 0.5)", job_id)
                return

            job_result = await session.execute(
                select(Job).where(Job.id == job_id, Job.company_id == company_id).with_for_update()
            )
            job = job_result.scalar_one_or_none()
            if job:
                job.compensation_attempts = (job.compensation_attempts or 0) + 1

            partial = await session.execute(
                select(AIOutput).where(
                    AIOutput.job_id == job_id,
                    AIOutput.company_id == company_id,
                )
            )
            for rec in partial.scalars().all():
                await session.delete(rec)
            await session.flush()
            task_logger = CeleryTaskAdapter(
                logging.getLogger(__name__),
                {"task_context": {"job_id": job_id, "company_id": company_id, "trace_id": trace_id}},
            )
            task_logger.info(
                "Compensation: cleaned up partial AI outputs for failed job %s (attempt %s)",
                job_id,
                job.compensation_attempts if job else "?",
            )

        if db is not None:
            await _do_compensate(db)
        else:
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                await _do_compensate(session)
    except Exception as comp_err:
        logger.error("Compensation failed for job %s: %s", job_id, comp_err)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=10, queue="default", acks_late=True, reject_on_worker_lost=True
)
def process_job_task(
    self,
    job_id: str,
    company_id: str,
    user_id: str | None = None,
    audio_url: str | None = None,
    image_urls: list | None = None,
    description: str = "",
    trade_type: str = "",
    trace_id: str | None = None,
    estimated_cost_usd: float = 0.0,
    reserved_acu: float = 0.0,
    payload_version: int = PAYLOAD_VERSION,
    _hmac: str = "",
):
    # Check payload version compatibility
    if payload_version < MIN_SUPPORTED_VERSION or payload_version > MAX_SUPPORTED_VERSION:
        logger.error(
            "Payload version mismatch for job %s: got %d, supported %d-%d — sending to DLQ",
            job_id,
            payload_version,
            MIN_SUPPORTED_VERSION,
            MAX_SUPPORTED_VERSION,
        )
        _move_to_dead_letter(
            job_id=job_id,
            company_id=company_id,
            user_id=user_id,
            task_name="process_job_task",
            error_message=f"Payload version mismatch: got {payload_version}, supported {MIN_SUPPORTED_VERSION}-{MAX_SUPPORTED_VERSION}",
            failure_category="version_mismatch",
            last_state="queued",
            retry_count=self.request.retries,
            trace_id=trace_id or "",
        )
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("version_mismatch_total", {"job_id": job_id})
        except Exception as _e:
            logger.debug("Failed to increment version_mismatch metric (worker): %s", _e)
        return {
            "status": "failed",
            "job_id": job_id,
            "company_id": company_id,
            "error": "payload_version_mismatch",
            "failure_type": "version_mismatch",
            "trace_id": trace_id or "",
        }

    # V2-FIX: Worker version check — reject tasks enqueued by newer workers
    if payload_version > _WORKER_VERSION:
        logger.error(
            "Worker v%d cannot process v%d payload for job %s — sending to DLQ",
            _WORKER_VERSION,
            payload_version,
            job_id,
        )
        _move_to_dead_letter(
            job_id=job_id,
            company_id=company_id,
            user_id=user_id,
            task_name="process_job_task",
            error_message=f"Worker version mismatch: worker={_WORKER_VERSION}, payload={payload_version}",
            failure_category="version_mismatch",
            last_state="queued",
            retry_count=self.request.retries,
            trace_id=trace_id or "",
        )
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("version_mismatch_total", {"job_id": job_id})
        except Exception as _e:
            logger.debug("Failed to increment version_mismatch metric: %s", _e)
        return {
            "status": "failed",
            "job_id": job_id,
            "company_id": company_id,
            "error": "payload_version_mismatch",
            "failure_type": "version_mismatch",
            "trace_id": trace_id or "",
        }
    # Fail-closed: refuse to run without signing key
    _signing_key = _get_signing_key()
    if not _signing_key:
        logger.error("CELERY_TASK_SIGNING_KEY not configured — refusing task %s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "company_id": company_id,
            "error": "signing_key_not_configured",
            "failure_type": "security",
            "trace_id": trace_id or "",
        }

    payload = {
        "payload_version": payload_version,
        "job_id": job_id,
        "company_id": company_id,
        "user_id": user_id,
        "audio_url": audio_url,
        "image_urls": image_urls,
        "description": description,
        "trade_type": trade_type,
        "trace_id": trace_id,
        "estimated_cost_usd": estimated_cost_usd,
        "reserved_acu": reserved_acu,
    }
    # H7-FIX: HMAC verification failure now raises self.reject(requeue=False)
    # to send the task to DLQ immediately instead of silently returning it to
    # the queue. The old behavior (silent return) caused infinite redelivery
    # loops because Celery couldn't distinguish a completed task from a failed one.
    if _signing_key and not _verify_task_payload(payload, _hmac):
        logger.error("HMAC verification FAILED for job %s (company=%s) — sending to DLQ", job_id, company_id)
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_unsigned_task_rejected_total", {"task_name": "process_job_task"})
        except Exception as _e:
            logger.debug("Failed to increment unsigned task rejected metric: %s", _e)
        _move_to_dead_letter(
            job_id=job_id,
            company_id=company_id,
            user_id=user_id,
            task_name="process_job_task",
            error_message="HMAC verification failed",
            failure_category="security",
            last_state="queued",
            retry_count=self.request.retries,
            trace_id=trace_id or "",
        )
        raise self.reject(requeue=False)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.ai.failure_classifier import classify_failure, format_failure_for_trace
    from app.ai.gateway import gateway
    from app.ai.validator import validate_ai_output
    from app.analytics import EVENT_AI_OUTPUT_GENERATED, log_event
    from app.database import AsyncSessionLocal
    from app.jobs.models import AIOutput, Job
    from app.tasks.retry_guard import check_retry_storm
    from app.tracing.models import record_trace

    _trace_id = trace_id or str(uuid.uuid4())

    if self.request.retries > 0 and not check_retry_storm(job_id, "process_job_task"):
        logger.error("Retry storm blocked for job %s — not retrying further", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "company_id": company_id,
            "error": "retry_storm_blocked",
            "failure_type": "retry_storm",
            "trace_id": _trace_id,
        }

    task_logger = CeleryTaskAdapter(
        logging.getLogger(__name__),
        {"task_context": {"job_id": job_id, "company_id": company_id, "trace_id": _trace_id}},
    )

    # Fail-closed: refuse to process when AI is globally disabled
    from app.config import FeatureFlags, get_settings

    _settings = get_settings()
    _flags = FeatureFlags()
    if _settings.ai_disabled or _flags.is_enabled(FeatureFlags.AI_DISABLED):
        task_logger.info("AI disabled — refusing to process job %s", job_id)
        return {
            "status": "skipped",
            "job_id": job_id,
            "company_id": company_id,
            "error": "ai_disabled",
            "failure_type": "ai_disabled",
            "trace_id": _trace_id,
        }

    _concurrency_released = False
    _concurrency_acquired = False
    _task_start_time = time.monotonic()

    async def _phase1_pre_ai(
        db,
        step_start,
        our_estimated_cost,
        our_reserved,
    ):
        nonlocal _concurrency_released, _concurrency_acquired
        result = await db.execute(
            select(Job).where(Job.id == job_id, Job.company_id == company_id).options(selectinload(Job.media))
        )
        job = result.scalar_one_or_none()
        if not job:
            task_logger.error("Job not found for Celery task")
            raise Exception(f"Job {job_id} not found")

        from app.billing.state_machine import (
            AIProcessingState,
            StateTransitionError,
            _reconcile_job_state,
            heartbeat_reservation,
            transition_job_state,
        )

        _redis_job_lock_key = f"job:lock:{job_id}"
        _redis_job_heartbeat_key = f"job:heartbeat:{job_id}"
        _worker_id = os.getenv("WORKER_ID", os.getenv("HOSTNAME", f"worker-{uuid.uuid4().hex[:8]}"))
        _lock_value = f"{_worker_id}:{time.time()}"
        lock_acquired = False
        _redis_lock = None
        try:
            _redis_lock = get_sync_redis()
            if self.request.retries > 0:
                existing_lock = _redis_lock.get(_redis_job_lock_key)
                if existing_lock is not None:
                    _holder_heartbeat = _redis_lock.get(_redis_job_heartbeat_key)
                    if _holder_heartbeat is not None:
                        lock_ttl = _redis_lock.ttl(_redis_job_lock_key)
                        if lock_ttl > 30:
                            lock_acquired = False
                            task_logger.info(
                                "Retry %d for job %s: lock held by live worker "
                                "(TTL=%ds, heartbeat=%.0fs ago) — skipping",
                                self.request.retries,
                                job_id,
                                lock_ttl,
                                time.time()
                                - float(
                                    _holder_heartbeat.split(b":")[-1]
                                    if isinstance(_holder_heartbeat, bytes)
                                    else _holder_heartbeat.split(":")[-1]
                                ),
                            )
                        else:
                            _lua_takeover = """
                                local key = KEYS[1]
                                local hb_key = KEYS[2]
                                local new_val = ARGV[1]
                                local ttl = tonumber(ARGV[2])
                                local hb_cutoff = tonumber(ARGV[3])
                                local current = redis.call('GET', key)
                                if not current then
                                    return redis.call('SET', key, new_val, 'NX', 'EX', ttl) and 1 or 0
                                end
                                local hb = redis.call('GET', hb_key)
                                if hb then
                                    local parts = {}
                                    for part in string.gmatch(hb, '[^:]+') do
                                        table.insert(parts, part)
                                    end
                                    local hb_ts = tonumber(parts[#parts])
                                    if hb_ts and hb_ts > hb_cutoff then
                                        return 0
                                    end
                                end
                                redis.call('DEL', key)
                                return redis.call('SET', key, new_val, 'NX', 'EX', ttl) and 1 or 0
                            """
                            _acquired = _redis_lock.eval(
                                _lua_takeover,
                                2,
                                _redis_job_lock_key,
                                _redis_job_heartbeat_key,
                                _lock_value,
                                300,
                                time.time() - 30,
                            )
                            lock_acquired = bool(_acquired)
                            if lock_acquired:
                                task_logger.warning(
                                    "Retry %d for job %s: acquired stale lock via Lua takeover",
                                    self.request.retries,
                                    job_id,
                                )
                                try:
                                    from app.monitoring.prometheus import increment_counter

                                    increment_counter("workticket_stale_lock_takeover_total", {"job_id": job_id})
                                except Exception as _e:
                                    logger.debug("Failed to increment stale lock takeover metric: %s", _e)
            else:
                lock_acquired = bool(_redis_lock.set(_redis_job_lock_key, _lock_value, nx=True, ex=300))
            if lock_acquired:
                _redis_lock.setex(_redis_job_heartbeat_key, 30, _lock_value)
        except Exception as lock_err:
            task_logger.error("Redis lock acquire failed for job %s: %s", job_id, lock_err)
            raise

        if not lock_acquired:
            task_logger.info("Another worker processing job, skipping duplicate")
            await record_trace(
                _trace_id,
                "duplicate_skip",
                "completed",
                job_id=job_id,
                company_id=company_id,
                duration_ms=(time.monotonic() - step_start) * 1000,
            )
            if our_reserved > 0:
                from app.billing.quota_engine import quota_engine

                await quota_engine.release_reserved(db, company_id, our_reserved, job_id=job_id)
            return None, our_estimated_cost, our_reserved, _redis_lock, _redis_job_lock_key

        trans_result = await _reconcile_job_state(
            db,
            job.id,
            company_id,
            expected=AIProcessingState.queued,
            target=AIProcessingState.reserved,
        )
        if trans_result == "inconsistent":
            raise StateTransitionError(
                f"Cannot transition job {job.id} to reserved — inconsistent state",
                job_id=job.id,
                current=str(job.ai_processing_state),
                target="reserved",
            )
        if trans_result == "skip_reservation":
            task_logger.info("Job already processing — skipping reservation step")

        if self.request.retries > 0:
            job.retry_count = (job.retry_count or 0) + 1
            await db.flush()

        from app.billing.quota_engine import quota_engine

        if self.request.retries == 0:
            if our_reserved > 0:
                task_logger.info("Using pre-reserved quota: %.4f ACU", our_reserved)
            else:
                result = await quota_engine.check_and_reserve(
                    db,
                    company_id,
                    our_estimated_cost,
                    job_id,
                    user_id=user_id,
                )
                if not result.allowed:
                    task_logger.warning("Quota blocked on first attempt: %s", result.reason)
                    await record_trace(
                        _trace_id,
                        "quota_blocked",
                        "failed",
                        job_id=job_id,
                        company_id=company_id,
                        error_message=result.reason,
                    )
                    await transition_job_state(
                        db, job.id, company_id, AIProcessingState.failed, heartbeat_fn=heartbeat_reservation
                    )
                    return (
                        {"status": "quota_exceeded"},
                        our_estimated_cost,
                        our_reserved,
                        _redis_lock,
                        _redis_job_lock_key,
                    )
                our_reserved = result.reserved_acu
        else:
            from app.billing.cost_estimator import estimate_job_cost

            cost_est = estimate_job_cost(
                image_count=len(image_urls or []),
                has_audio=bool(audio_url),
            )
            our_estimated_cost = cost_est.total_cost
            result = await quota_engine.check_and_reserve(db, company_id, our_estimated_cost, job_id)
            if not result.allowed:
                task_logger.warning("Quota blocked on retry: %s", result.reason)
                await record_trace(
                    _trace_id,
                    "quota_blocked",
                    "failed",
                    job_id=job_id,
                    company_id=company_id,
                    error_message=result.reason,
                )
                await transition_job_state(
                    db, job.id, company_id, AIProcessingState.failed, heartbeat_fn=heartbeat_reservation
                )
                return {"status": "quota_exceeded"}, our_estimated_cost, our_reserved, _redis_lock, _redis_job_lock_key
            our_reserved = result.reserved_acu

        trans_result = await _reconcile_job_state(
            db,
            job.id,
            company_id,
            expected=AIProcessingState.reserved,
            target=AIProcessingState.processing,
        )
        if trans_result == "inconsistent":
            raise StateTransitionError(
                f"Cannot transition job {job.id} to processing — inconsistent state",
                job_id=job.id,
                current=str(job.ai_processing_state),
                target="processing",
            )

        await heartbeat_reservation(db, company_id)
        await db.commit()
        return job, our_estimated_cost, our_reserved, _redis_lock, _redis_job_lock_key

    async def _phase2_ai_gateway():
        nonlocal _concurrency_acquired
        try:
            from app.billing.concurrency import company_concurrency

            if not _concurrency_acquired:
                await company_concurrency.acquire(company_id, "default")
                _concurrency_acquired = True
        except Exception as conc_err:
            task_logger.error("Failed to acquire concurrency: %s", conc_err)

        execution_start = time.monotonic()
        output = await gateway.process_job(
            {
                "audio_url": audio_url,
                "image_urls": image_urls or [],
                "description": description,
                "trade_type": trade_type,
            },
            company_id=company_id,
            user_id=user_id,
            job_id=job_id,
            trace_id=_trace_id,
        )
        execution_ms = (time.monotonic() - execution_start) * 1000

        if getattr(output, "is_fallback", False):
            task_logger.error("AI output is fallback (AI unavailable) for job %s", job_id)
            return None, execution_ms

        validation = validate_ai_output(output, reject_on_invalid=True)
        if not validation.valid:
            ftype = validation.failure_type.value if validation.failure_type else "unknown"
            task_logger.error("AI output validation failed (%s): %s", ftype, validation.reason)
            return None, execution_ms

        return output, execution_ms

    async def _phase3_post_ai(
        job_id_str,
        job_ref,
        output,
        execution_ms,
        our_estimated_cost,
        our_reserved,
        redis_lock,
        redis_job_lock_key,
        step_start,
    ):
        from app.billing.reconciliation import reconcile_cost
        from app.billing.state_machine import AIProcessingState, _reconcile_job_state, heartbeat_reservation

        nonlocal _concurrency_released, _concurrency_acquired

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Job).where(Job.id == job_id_str, Job.company_id == company_id).options(selectinload(Job.media))
            )
            job = result.scalar_one_or_none()
            if not job:
                raise Exception(f"Job {job_id_str} not found for post-AI reconciliation")

            await heartbeat_reservation(db, company_id)

            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from app.jobs.models import AI_OUTPUT_UNIQUE_CONSTRAINT_NAME

            stmt = (
                pg_insert(AIOutput)
                .values(
                    job_id=job.id,
                    company_id=company_id,
                    output_type="job_analysis",
                    json_result=output.model_dump_json(),
                    confidence_score=output.confidence,
                    model_used="gateway:ai_gateway",
                )
                .on_conflict_do_nothing(constraint=AI_OUTPUT_UNIQUE_CONSTRAINT_NAME)
            )
            insert_result = await db.execute(stmt)
            is_retry = insert_result.rowcount == 0
            if is_retry:
                task_logger.warning("AI output already exists — proceeding with reconciliation")
                await record_trace(
                    _trace_id,
                    "duplicate_retry",
                    "in_progress",
                    job_id=job_id_str,
                    company_id=company_id,
                    duration_ms=(time.monotonic() - step_start) * 1000,
                )
                existing_output = await db.execute(
                    select(AIOutput).where(AIOutput.job_id == job.id, AIOutput.company_id == company_id)
                )
                output = existing_output.scalar_one_or_none()
                execution_ms = 0
                total_actual_cost = our_estimated_cost
            else:
                if output.partial_failure:
                    task_logger.warning("Stored with partial_failure=True (confidence=%.2f)", output.confidence)
                for media in job.media:
                    media.ai_processed = True
                from app.billing.cost_estimator import estimate_job_cost as _estimate

                _cost_est = _estimate(
                    image_count=len(image_urls or []),
                    has_audio=bool(audio_url),
                )
                total_actual_cost = _cost_est.total_cost

            try:
                await reconcile_cost(
                    db=db,
                    company_id=company_id,
                    job_id=job.id,
                    estimated_cost_usd=our_estimated_cost,
                    actual_cost_usd=total_actual_cost,
                    reserved_acu=our_reserved,
                    model_used="gateway:ai_gateway",
                    execution_time_ms=int(execution_ms),
                    user_id=user_id,
                )
            except Exception as reconcile_err:
                task_logger.error("Billing reconciliation failed: %s", reconcile_err)

            trans_result = await _reconcile_job_state(
                db,
                job.id,
                company_id,
                expected=AIProcessingState.processing,
                target=AIProcessingState.completed,
            )
            if trans_result == "inconsistent":
                task_logger.error("Failed to mark job completed — state inconsistent")
            else:
                task_logger.info("Celery AI pipeline complete (cost=$%.6f)", total_actual_cost)

            try:
                pub_redis = _get_pub_redis()
                if pub_redis:
                    pub_redis.publish(
                        f"job_status:{job_id_str}",
                        json.dumps({"status": "complete", "job_id": job_id_str, "company_id": company_id}),
                    )
                    pub_redis.xadd(
                        f"stream:job:{job_id_str}",
                        {
                            "status": "complete",
                            "job_id": job_id_str,
                            "company_id": company_id,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "system_state": "healthy",
                        },
                        maxlen=1000,
                    )
            except Exception as _e:
                logger.debug("Failed to publish completion to Redis: %s", _e)

            try:
                await record_trace(
                    _trace_id,
                    "output_stored",
                    "completed",
                    job_id=job_id_str,
                    company_id=company_id,
                    duration_ms=(time.monotonic() - step_start) * 1000,
                    metadata={
                        "confidence": output.confidence or 0,
                        "partial_failure": getattr(output, "partial_failure", False),
                        "cost": total_actual_cost,
                    },
                )
                await log_event(
                    event_name=EVENT_AI_OUTPUT_GENERATED,
                    user_id=user_id or "unknown",
                    company_id=str(company_id),
                    job_id=job_id_str,
                    metadata={
                        "confidence": output.confidence if output else 0,
                        "is_fallback": output.is_fallback if output else False,
                        "model_used": "gateway:ai_gateway",
                        "has_audio": bool(audio_url),
                        "image_count": len(image_urls or []),
                        "trace_id": _trace_id,
                        "cost_usd": total_actual_cost,
                    },
                )
            except Exception as log_err:
                task_logger.error("Failed to log completion: %s", log_err)

            try:
                from app.monitoring.prometheus import observe_celery_task_latency

                observe_celery_task_latency(time.monotonic() - _task_start_time)
            except Exception as _e:
                logger.debug("Failed to observe celery task latency: %s", _e)

            try:
                from app.billing.concurrency import company_concurrency

                if _concurrency_acquired:
                    await company_concurrency.release(company_id)
                _concurrency_released = True
            except Exception as conc_err:
                task_logger.error("Failed to release concurrency on success: %s", conc_err)

            if not is_retry and getattr(output, "is_fallback", False):
                total_actual_cost = 0.0

            try:
                if redis_lock:
                    redis_lock.delete(redis_job_lock_key)
            except Exception as _e:
                logger.debug("Failed to delete Redis job lock on success: %s", _e)

            try:
                await db.commit()
                try:
                    from app.monitoring.prometheus import increment_jobs_completed

                    increment_jobs_completed()
                except Exception as _e:
                    logger.debug("Failed to increment jobs completed metric: %s", _e)
            except Exception as commit_err:
                task_logger.error("Failed to commit transaction on success: %s", commit_err)
                raise

    async def _run():
        nonlocal _concurrency_released, _concurrency_acquired
        our_estimated_cost = estimated_cost_usd
        our_reserved = reserved_acu
        redis_lock = None
        redis_job_lock_key = None

        try:
            step_start = time.monotonic()
            await record_trace(
                _trace_id, "celery_start", job_id=job_id, company_id=company_id, metadata={"worker": "process_job_task"}
            )

            async with AsyncSessionLocal() as db:
                phase1_result = await _phase1_pre_ai(db, step_start, our_estimated_cost, our_reserved)
                if phase1_result is None:
                    return
                if isinstance(phase1_result, dict) and phase1_result.get("status") == "quota_exceeded":
                    await db.commit()
                    return phase1_result
                _job, our_estimated_cost, our_reserved, redis_lock, redis_job_lock_key = phase1_result

            output, execution_ms = await _phase2_ai_gateway()
            if output is None:
                await _release_reservation_with_retry(company_id, our_reserved, job_id, _trace_id, task_logger)
                raise Exception("NON_RECOVERABLE: AI unavailable — fallback or invalid output")

            await _phase3_post_ai(
                job_id,
                company_id,
                output,
                execution_ms,
                our_estimated_cost,
                our_reserved,
                redis_lock,
                redis_job_lock_key,
                step_start,
            )

        except Exception as e:
            failure_msg = format_failure_for_trace(str(e))
            task_logger.error("Celery AI pipeline failed: %s", failure_msg)
            await record_trace(
                _trace_id, "celery_failed", "failed", job_id=job_id, company_id=company_id, error_message=failure_msg
            )
            await _compensate_failed_job(
                job_id,
                company_id,
                failure_msg,
                db=None,
                trace_id=_trace_id,
                retry_count=self.request.retries,
                max_retries=self.max_retries or 3,
            )
            if our_reserved > 0:
                await _release_reservation_with_retry(company_id, our_reserved, job_id, _trace_id, task_logger)
            try:
                if redis_lock:
                    redis_lock.delete(redis_job_lock_key)
            except Exception as _e:
                logger.debug("Failed to delete Redis job lock on error: %s", _e)
            try:
                pub_redis = _get_pub_redis()
                if pub_redis:
                    pub_redis.publish(
                        f"job_status:{job_id}",
                        json.dumps(
                            {"status": "failed", "job_id": job_id, "company_id": company_id, "error": failure_msg[:200]}
                        ),
                    )
                    pub_redis.xadd(
                        f"stream:job:{job_id}",
                        {
                            "status": "failed",
                            "job_id": job_id,
                            "company_id": company_id,
                            "error": failure_msg[:200],
                            "timestamp": datetime.now(UTC).isoformat(),
                            "system_state": "healthy",
                        },
                        maxlen=1000,
                    )
            except Exception as _e:
                logger.debug("Failed to publish failure to Redis: %s", _e)
            raise

    try:
        _ = _run_async(_run())
    except Exception as exc:
        error_str = str(exc)
        failure_cat = classify_failure(error_str)
        from app.exceptions import ValidationError

        is_non_recoverable = isinstance(exc, (ValidationError, json.JSONDecodeError)) or "NON_RECOVERABLE" in error_str

        _is_serialization_error = any(
            code in str(getattr(exc, "orig", "")) for code in ["40001", "40P01", "serialization_failure", "deadlock"]
        )
        if _is_serialization_error and self.request.retries < (self.max_retries or 3):
            task_logger.warning(
                "Serialization failure on job %s (attempt %d/%d): %s — retrying",
                job_id,
                self.request.retries + 1,
                (self.max_retries or 3) + 1,
                exc,
            )
            raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1)) from exc

        if is_non_recoverable or self.request.retries >= (self.max_retries or 3):
            task_logger.error("Task FAILED (FINAL, sent to DLQ): %s | category=%s", exc, failure_cat.value)
            _move_to_dead_letter(
                job_id=job_id,
                company_id=company_id,
                user_id=user_id,
                task_name="process_job_task",
                error_message=error_str,
                failure_category=failure_cat.value,
                last_state="failed",
                retry_count=self.request.retries,
                trace_id=_trace_id,
            )
            if not _concurrency_released and _concurrency_acquired:
                try:
                    from app.billing.concurrency import company_concurrency

                    _run_async(company_concurrency.release(company_id))
                except Exception as _e:
                    logger.debug("Failed to release concurrency on final failure: %s", _e)
            return {
                "status": "failed",
                "job_id": job_id,
                "company_id": company_id,
                "error": str(exc),
                "failure_type": failure_cat.value,
                "trace_id": _trace_id,
                "dlq": True,
            }
        task_logger.error("Task failed, retrying...: %s", exc)
        raise self.retry(exc=exc) from exc
    finally:
        pass


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10, queue="beat")
def scan_for_stalled_ai_jobs(self):
    """Scan for jobs with confirmed media but no AI processing started.
    This addresses the issue where AI processing task dispatch fails silently.
    Also covers jobs stuck in 'queued' state where the Celery message was
    lost (e.g., after Redis OOM), using a shorter 5-minute threshold so
    they don't wait 60 minutes for cleanup_stale_jobs.
    """
    from app.config import FeatureFlags, get_settings

    _settings = get_settings()
    _flags = FeatureFlags()
    if _settings.ai_disabled or _flags.is_enabled(FeatureFlags.AI_DISABLED):
        logger.debug("AI disabled — skipping scan_for_stalled_ai_jobs")
        return {"status": "skipped", "reason": "ai_disabled"}

    from celery_config.beat import _acquire_beat_lock

    if not _acquire_beat_lock(self.app, "scan_for_stalled_ai_jobs", ttl=300):
        logger.warning("scan_for_stalled_ai_jobs skipped — another execution is in progress")
        return {"status": "skipped", "reason": "concurrent_execution_locked"}

    from datetime import datetime, timedelta

    from app.billing.state_machine import transition_job_state
    from app.database import AsyncSessionLocal
    from app.jobs.models import AIProcessingState, Job, JobMedia

    async def _scan_and_recover():
        async with AsyncSessionLocal() as db:
            try:
                # Find jobs with confirmed media but no AI processing started
                from sqlalchemy import and_, select

                queue_lost_cutoff = datetime.now(UTC) - timedelta(minutes=5)

                # Batch 1: Jobs that never got past 'none' (media was uploaded but AI never started)
                query_none = select(Job).where(
                    and_(
                        Job.ai_processing_state == AIProcessingState.none.value,
                        Job.id.in_(
                            select(JobMedia.job_id).where(JobMedia.ai_processed.is_(True))  # noqa: E712
                        ),
                    )
                )

                # Batch 2: Jobs stuck in 'queued' beyond 5 minutes (Celery message lost post-enqueue)
                query_queued = select(Job).where(
                    and_(
                        Job.ai_processing_state == AIProcessingState.queued.value,
                        Job.ai_processing_updated_at < queue_lost_cutoff,
                        Job.id.in_(
                            select(JobMedia.job_id).where(JobMedia.ai_processed.is_(True))  # noqa: E712
                        ),
                    )
                )

                from itertools import chain

                result_none = await db.execute(query_none)
                result_queued = await db.execute(query_queued)
                stalled_jobs = list(
                    chain(
                        result_none.scalars().all(),
                        result_queued.scalars().all(),
                    )
                )

                if stalled_jobs:
                    logger.info(
                        f"Found {len(stalled_jobs)} stalled AI jobs ({len(result_none)} none-state, {len(result_queued)} queued-state)"
                    )
                    # Report stuck jobs metrics
                    try:
                        from app.monitoring.prometheus import set_stuck_jobs_processing

                        set_stuck_jobs_processing(len(stalled_jobs))
                    except Exception as _e:
                        logger.debug("Failed to set stuck jobs processing metric: %s", _e)
                    _job_lock_redis = None
                    try:
                        _job_lock_redis = get_sync_redis()
                    except Exception as _e:
                        logger.debug("Failed to get sync Redis for job lock: %s", _e)

                    for job in stalled_jobs:
                        try:
                            job_lock_key = f"scan_recover:{job.id}"
                            requeue_count_key = f"scan_recover_count:{job.id}"
                            if _job_lock_redis:
                                acquired = _job_lock_redis.set(job_lock_key, "1", nx=True, ex=120)
                                if not acquired:
                                    logger.info(
                                        f"Stalled job {job.id} already being recovered by another scan, skipping"
                                    )
                                    continue
                                # Check and increment re-queue count (H9)
                                requeue_count = _job_lock_redis.incr(requeue_count_key)
                                _job_lock_redis.expire(requeue_count_key, 86400)
                                if requeue_count > 3:
                                    logger.critical(
                                        f"Stalled job {job.id} re-queued {requeue_count} times — transitioning to failed"
                                    )
                                    await transition_job_state(db, job.id, job.company_id, AIProcessingState.failed)
                                    # C-2: Commit the failed transition immediately so subsequent
                                    # stalled job processing sees a consistent DB state.
                                    await db.commit()
                                    _move_to_dead_letter(
                                        job_id=str(job.id),
                                        company_id=str(job.company_id),
                                        user_id="",
                                        task_name="scan_for_stalled_ai_jobs",
                                        error_message=f"Job re-queued {requeue_count} times without progress",
                                        failure_category="stall_exhausted",
                                        last_state=str(job.ai_processing_state),
                                        retry_count=int(requeue_count),
                                    )
                                    continue
                            else:
                                # Local in-memory tracking if Redis unavailable
                                from app.tasks.retry_guard import check_retry_storm

                                if not check_retry_storm(str(job.id), "scan_recover"):
                                    logger.critical(f"Stalled job {job.id} re-queued too many times — giving up")
                                    continue

                            await transition_job_state(db, job.id, job.company_id, AIProcessingState.queued)

                            image_urls = [m.storage_url for m in job.media if m.type in ("image", "photo")]
                            audio_url = next((m.storage_url for m in job.media if m.type == "audio"), None)

                            from celery_config.broker import enqueue_job_task

                            enqueue_job_task(
                                job_id=str(job.id),
                                company_id=str(job.company_id),
                                user_id=None,
                                audio_url=audio_url,
                                image_urls=image_urls,
                                description=job.description or "",
                                trace_id=str(uuid.uuid4()),
                            )
                            logger.info(
                                f"Re-dispatched AI processing for stalled job {job.id} (company {job.company_id})"
                            )

                        except Exception as job_err:
                            logger.error(f"Failed to recover stalled job {job.id}: {job_err}")

                    # C-2: Commit all recovered job transitions at once.
                    # Without this, the state changes are silently rolled back.
                    try:
                        await db.commit()
                    except Exception as commit_err:
                        logger.error(f"Failed to commit stalled job recovery: {commit_err}")

                else:
                    logger.debug("No stalled AI jobs found")

            except Exception as e:
                logger.error(f"Error scanning for stalled AI jobs: {e}")
                raise

    try:
        _run_async(_scan_and_recover())
    except Exception as exc:
        logger.error("Scan for stalled AI jobs failed: %s", exc)
        raise self.retry(exc=exc) from exc

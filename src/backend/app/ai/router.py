import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.audit import audit
from app.ai.business_metrics import business_metrics
from app.ai.gateway import gateway
from app.ai.rate_limiter import _get_redis, rate_limiter
from app.ai.schemas import AIOutputFeedbackRequest, AIOutputResponse, AIOutputSchema, AIProcessResponse
from app.auth.dependencies import get_current_user
from app.config import FeatureFlags, get_settings
from app.database import get_db
from app.jobs.models import AIOutput, AIOutputFeedback, Job, User
from app.pricing.engine import compute_line_item_total
from celery_app import enqueue_job_task

_flags = FeatureFlags()

_AI_DISABLED_MESSAGE = "AI features are disabled in this release. All workflows are manual-first."

# WebSocket connection tracking using Redis for global limits across replicas
_WS_CONNECT_RATE = 10
_WS_CONNECT_WINDOW = 60.0
_MAX_WS_CONNECTIONS_PER_USER = 3
_MAX_WS_CONNECTIONS_GLOBAL = int(os.getenv("MAX_WS_CONNECTIONS_GLOBAL", "500"))
_WS_ENABLED = os.getenv("WS_ENABLED", "true").lower() in ("true", "1", "yes")
_ESTIMATED_WS_WORKERS = max(1, int(os.getenv("WS_WORKER_REPLICAS", "5")))

# Local fallback for WebSocket connection tracking when Redis is unavailable
_local_ws_connections: dict = {}
_local_ws_connection_locks: dict = {}

_active_websockets: set[object] = set()
_active_websockets_lock = asyncio.Lock()


async def _increment_ws_connection(user_id: str) -> tuple[bool, int, str]:
    """Increment WebSocket connection count for user in Redis.

    C4-FIX: Uses Redis sorted set (ZADD) with score=timestamp instead of
    INCR/EXPIRE pattern. INCR/EXPIRE was inaccurate under reconnect storms
    because each reconnect reset the TTL and DECR could underflow if the
    key expired between disconnect and DECR call.

    Sorted set approach:
    - ZADD with timestamp score on connect
    - ZREMRANGEBYSCORE to remove entries older than window on each check
    - ZCARD for accurate count
    - SREM on disconnect using the unique member ID
    Returns (allowed, remaining, member_id).
    """
    try:
        redis_client = await _get_redis()
        if not redis_client:
            return _local_increment_ws(user_id)

        key = f"ws_conn:{user_id}"
        now = time.time()
        cutoff = now - _WS_CONNECT_WINDOW
        member = f"{uuid.uuid4()}:{now}"

        # Use Lua script for atomic trim + add + count
        lua_script = """
            redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
            local count = redis.call('ZCARD', KEYS[1])
            if count < tonumber(ARGV[3]) then
                redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
                redis.call('EXPIRE', KEYS[1], ARGV[5])
                return {1, count + 1, ARGV[4]}
            end
            return {0, count, ''}
        """
        result = await redis_client.eval(
            lua_script,
            1,
            key,
            cutoff,
            now,
            _MAX_WS_CONNECTIONS_PER_USER,
            member,
            int(_WS_CONNECT_WINDOW) + 1,
        )
        allowed = result[0] == 1
        remaining = max(0, _MAX_WS_CONNECTIONS_PER_USER - result[1])
        ws_member = result[2] if len(result) > 2 else member
        return allowed, remaining, ws_member
    except Exception as _e:
        logger.debug("Redis WS connection tracking error: %s", _e)
        logger.warning(
            "Redis WS connection tracking unavailable — fallback with %d estimated workers", _ESTIMATED_WS_WORKERS
        )
        return _local_increment_ws(user_id)


def _local_increment_ws(user_id: str) -> tuple[bool, int, str]:
    """Local fallback for WebSocket connection tracking.
    Divides the per-user limit by estimated replicas to prevent
    each replica from allowing the full limit during Redis outage."""
    _local_max = max(1, _MAX_WS_CONNECTIONS_PER_USER // max(1, _ESTIMATED_WS_WORKERS))
    now = time.time()
    lock = _local_ws_connection_locks.setdefault(user_id, threading.Lock())
    with lock:
        timestamps = _local_ws_connections.get(user_id, [])
        cutoff = now - _WS_CONNECT_WINDOW
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= _local_max:
            return False, 0, ""
        timestamps.append(now)
        _local_ws_connections[user_id] = timestamps
        remaining = max(0, _local_max - len(timestamps))
        return len(timestamps) <= _local_max, remaining, f"{uuid.uuid4()}:{now}"


async def _decrement_ws_connection(user_id: str, member: str = ""):
    """Decrement WebSocket connection count for user in Redis.

    C4-FIX: Uses SREM on the sorted set member instead of DECR on a string key.
    The member ID returned by _increment_ws_connection must be passed to
    ensure accurate removal. Without the member ID, DECR could underflow
    or remove the wrong entry under concurrent access.
    """
    try:
        redis_client = await _get_redis()
        if not redis_client:
            _local_decrement_ws(user_id)
            return

        key = f"ws_conn:{user_id}"
        if member:
            await redis_client.zrem(key, member)
        else:
            # Legacy fallback: pop oldest entry
            await redis_client.zpopmin(key)
    except Exception as _e:
        logger.debug("Redis WS decrement failed: %s", _e)
        _local_decrement_ws(user_id)


def _local_decrement_ws(user_id: str):
    """Local fallback for WebSocket connection decrement."""
    lock = _local_ws_connection_locks.get(user_id)
    if not lock:
        return
    with lock:
        timestamps = _local_ws_connections.get(user_id, [])
        if timestamps:
            timestamps.pop(0)
            if timestamps:
                _local_ws_connections[user_id] = timestamps
            else:
                _local_ws_connections.pop(user_id, None)
                _local_ws_connection_locks.pop(user_id, None)


# Global WebSocket connection cap (H-7): Use SADD/SREM/SCARD for accurate tracking
_ws_global_count = 0
_ws_global_count_lock = asyncio.Lock()


async def _increment_ws_global() -> tuple[bool, str]:
    """Enforce a global WebSocket connection cap across all users.
    Returns (allowed, member_id) — the member_id must be stored in the
    WebSocket handler context so it can be removed on disconnect.
    HIGH-7 FIX: In Redis fallback mode, divide max by estimated replica count."""
    member = str(uuid.uuid4())
    _local_max = max(1, _MAX_WS_CONNECTIONS_GLOBAL // max(1, _ESTIMATED_WS_WORKERS))
    try:
        redis_client = await _get_redis()
        if redis_client:
            key = "ws_global_connections"
            count = await redis_client.scard(key)
            if count >= _MAX_WS_CONNECTIONS_GLOBAL:
                return False, ""
            await redis_client.sadd(key, member)
            await redis_client.expire(key, 3600)
            return True, member
        async with _ws_global_count_lock:
            if _ws_global_count >= _local_max:
                return False, ""
            _ws_global_count += 1
            return True, member
    except Exception as _e:
        logger.debug("Redis global WS connection tracking error: %s", _e)
        logger.warning(
            "Redis global WS connection tracking unavailable — using local fallback (max %d per replica)", _local_max
        )
        async with _ws_global_count_lock:
            if _ws_global_count >= _local_max:
                return False, ""
            _ws_global_count += 1
            return True, member


async def _decrement_ws_global(member: str = ""):
    global _ws_global_count
    try:
        redis_client = await _get_redis()
        if redis_client:
            key = "ws_global_connections"
            if member:
                await redis_client.srem(key, member)
            return
        async with _ws_global_count_lock:
            _ws_global_count = max(0, _ws_global_count - 1)
    except Exception as _e:
        logger.debug("Redis global WS decrement failed: %s", _e)
        async with _ws_global_count_lock:
            _ws_global_count = max(0, _ws_global_count - 1)


logger = logging.getLogger(__name__)

router = APIRouter()


def _require_ai_enabled():
    settings = get_settings()
    if settings.ai_disabled or _flags.is_enabled(FeatureFlags.AI_DISABLED):
        raise HTTPException(status_code=503, detail=_AI_DISABLED_MESSAGE)


@router.post("/process-job/{job_id}", response_model=AIProcessResponse)
async def process_job(
    job_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    import uuid

    from app.billing.abuse import abuse_detector
    from app.billing.concurrency import company_concurrency
    from app.billing.cost_estimator import estimate_job_cost
    from app.billing.idempotency_service import (
        complete_idempotency_record,
        compute_request_hash,
        create_idempotency_record,
        extract_idempotency_key,
        get_idempotent_response,
    )
    from app.billing.quota_engine import quota_engine
    from app.billing.state_machine import AIProcessingState, transition_job_state
    from app.tracing.models import record_trace

    idempotency_key = extract_idempotency_key(request)
    if idempotency_key:
        body = await request.body()
        request_hash = compute_request_hash(body)
        cached = await get_idempotent_response(
            db, current_user.company_id, current_user.id, idempotency_key, request_hash
        )
        if cached is not None:
            logger.info("Idempotency hit: user=%s key=%s", current_user.id, idempotency_key)
            return AIProcessResponse(**cached)
        await create_idempotency_record(db, current_user.company_id, current_user.id, idempotency_key, request_hash)

    client_ip = request.client.host if request.client else ""
    allowed, reason = await rate_limiter.check_all(
        user_id=current_user.id,
        company_id=str(current_user.company_id),
        client_ip=client_ip,
    )
    if not allowed:
        logger.warning(
            "Rate limit hit: user=%s company=%s ip=%s reason=%s",
            current_user.id,
            current_user.company_id,
            client_ip,
            reason,
        )
        raise HTTPException(status_code=429, detail=reason)

    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.company_id == current_user.company_id).options(selectinload(Job.media))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.description and len(job.description) > 10000:
        raise HTTPException(status_code=400, detail="Job description exceeds maximum length of 10000 characters")

    image_urls = [m.storage_url for m in job.media if m.type == "photo"]
    settings = get_settings()
    max_images = int(getattr(settings, "max_images_per_ai_job", os.environ.get("MAX_IMAGES_PER_JOB", "5")))
    if len(image_urls) > max_images:
        logger.warning("Truncating images for job %s: %d -> %d", job_id, len(image_urls), max_images)
        image_urls = image_urls[:max_images]
    audio_url = next((m.storage_url for m in job.media if m.type == "audio"), None)

    trace_id = str(uuid.uuid4())
    await record_trace(trace_id, "api_receive", "started", job_id=str(job.id), company_id=str(current_user.company_id))

    cost_est = estimate_job_cost(
        image_count=len(image_urls),
        has_audio=bool(audio_url),
    )

    daily_check = await quota_engine.check_daily_spend(
        db=db,
        company_id=current_user.company_id,
        estimated_cost_usd=cost_est.total_cost,
    )
    if not daily_check.allowed:
        logger.warning(
            "Daily spend blocked for company %s job %s: %s", current_user.company_id, job_id, daily_check.reason
        )
        await record_trace(
            trace_id,
            "daily_spend_blocked",
            "failed",
            job_id=str(job.id),
            company_id=str(current_user.company_id),
            error_message=daily_check.reason,
        )
        raise HTTPException(status_code=402, detail=f"Daily spend limit reached: {daily_check.reason}")

    # CRITICAL-4 FIX: Remove read-only pre-flight quota check.
    # The read-only check (check_quota without FOR UPDATE) created a race condition:
    # between this pre-flight and the actual check_and_reserve in the Celery task,
    # another task could consume the remaining quota, leading to over-commit.
    # The authoritative quota check happens inside the Celery task at
    # celery_app.py:1004-1013 where check_and_reserve uses FOR UPDATE.
    # The daily spend check below is retained as it's a separate concern
    # (soft limit tracked via aggregate, not row-level reservation).

    plan = current_user.company.subscription_plan or "free"
    acquired = await company_concurrency.acquire(str(current_user.company_id), plan)
    if not acquired:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent AI jobs for your company. Please wait for existing jobs to complete.",
        )

    await abuse_detector.record_request(str(current_user.company_id))
    risk_score = await abuse_detector.check_and_update_risk(db, str(current_user.company_id))
    if risk_score > 70:
        await company_concurrency.release(str(current_user.company_id))
        raise HTTPException(
            status_code=403,
            detail="AI processing disabled for your company due to unusual activity. Please contact support.",
        )

    await transition_job_state(
        db,
        job.id,
        current_user.company_id,
        AIProcessingState.queued,
    )

    response_payload = None
    try:
        from celery_app import celery_app as _celery_app

        try:
            inspector = _celery_app.control.inspect()
            workers = inspector.ping(timeout=2)
            if not workers or len(workers) == 0:
                raise RuntimeError("No Celery workers available to accept task")
        except Exception as broker_err:
            logger.error("Broker health check failed before dispatch: %s", broker_err)
            raise RuntimeError(f"Broker unreachable: {broker_err}") from broker_err

        # V2-FIX: No reserved_acu from API — reservation happens in Celery task
        dispatch_result = enqueue_job_task(
            job_id=str(job.id),
            company_id=str(current_user.company_id),
            user_id=str(current_user.id),
            audio_url=audio_url,
            image_urls=image_urls,
            description=job.description or "",
            trace_id=trace_id,
            estimated_cost_usd=cost_est.total_cost,
            reserved_acu=0.0,
        )
        task_id = dispatch_result["task_id"]

        # Confirm broker acceptance: .delay() in enqueue_job_task returns
        # a valid AsyncResult id iff Redis accepted the message. If Redis
        # is unreachable, .delay() raises an exception caught below.
        # The scan_for_stalled_ai_jobs beat task (every 5min) recovers
        # jobs that were queued but never started.
        if not task_id:
            logger.error("Task dispatch returned no task_id for job %s", job_id)
            raise RuntimeError("Task dispatch failed to produce a task ID")

        await record_trace(
            trace_id,
            "celery_enqueue",
            "completed",
            job_id=str(job.id),
            company_id=str(current_user.company_id),
            metadata={"queue": dispatch_result["queue"], "estimated_cost": cost_est.total_cost, "task_id": task_id},
        )
        logger.info(
            "AI job %s queued for async processing (user=%s, cost=$%.6f) trace=%s task_id=%s",
            job_id,
            current_user.id,
            cost_est.total_cost,
            trace_id,
            task_id,
        )
        # Track job creation for silent failure detection
        try:
            from app.monitoring.prometheus import increment_jobs_created

            increment_jobs_created()
        except Exception as _e:
            logger.debug("Failed to increment jobs created metric: %s", _e)
        response_payload = {"job_id": str(job_id), "status": "queued", "output": None}
        # HIGH-4 FIX: Do NOT release concurrency lock after enqueue.
        # The lock must remain held until the Celery task finishes processing,
        # to prevent exceeding the company's configured concurrency limit.
        # The Celery task at celery_app.py:1046-1048 acquires its own lock
        # before AI processing and releases it in the finally block.
        # If the worker crashes, the Redis lock expires via TTL (300s).
        # Release only on error (handled in except/finally below).
        return AIProcessResponse(**response_payload)
    except Exception as e:
        await company_concurrency.release(str(current_user.company_id))
        await record_trace(
            trace_id,
            "celery_enqueue",
            "failed",
            job_id=str(job.id),
            company_id=str(current_user.company_id),
            error_message=str(e),
        )
        logger.error("Celery unavailable for job %s: %s", job_id, e)
        raise HTTPException(status_code=503, detail="AI processing temporarily unavailable. Please try again.") from e
    finally:
        if idempotency_key and response_payload is not None:
            await complete_idempotency_record(
                db, current_user.company_id, current_user.id, idempotency_key, response_payload
            )
        elif idempotency_key:
            error_payload = {"job_id": str(job_id), "status": "failed", "output": None}
            await complete_idempotency_record(
                db, current_user.company_id, current_user.id, idempotency_key, error_payload, status="failed"
            )


@router.get("/output/{job_id}", response_model=AIOutputResponse)
async def get_ai_output(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    from app.ai.gateway import gateway as ai_gateway

    result = await db.execute(
        select(AIOutput)
        .where(AIOutput.job_id == job_id, AIOutput.company_id == current_user.company_id)
        .order_by(AIOutput.created_at.desc())
    )
    ai_output = result.scalar_one_or_none()
    if not ai_output:
        return AIOutputResponse(job_id=job_id, status="pending", output=None)

    output_data = json.loads(ai_output.json_result)
    output_schema = AIOutputSchema(**output_data)

    llm_available = await ai_gateway.llm_circuit.is_available()
    whisper_available = await ai_gateway.whisper_circuit.is_available()
    system_state = "degraded" if not (llm_available and whisper_available) else "healthy"

    # CRITICAL-3 FIX: Enforce confidence threshold before populating financial fields.
    # AI outputs below _MIN_CONFIDENCE (0.7) must NOT generate cost estimates or
    # populate invoice/quote forms. The frontend will receive confidence_score so it
    # can display a warning, but cost_estimate_usd will be 0 for low-confidence outputs.
    from app.ai.validator import _MIN_CONFIDENCE

    cost_est = 0.0
    if output_schema.estimated_hours > 0 and output_schema.confidence >= _MIN_CONFIDENCE:
        cost_est = compute_line_item_total(output_schema.estimated_hours, 150.0)

    return AIOutputResponse(
        job_id=job_id,
        status="complete",
        output=output_schema,
        model_used=ai_output.model_used,
        confidence_score=ai_output.confidence_score,
        system_state=system_state,
        partial_failure=output_schema.partial_failure,
        cost_estimate_usd=cost_est,
    )


@router.post("/feedback")
async def submit_ai_feedback(
    feedback: AIOutputFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    result = await db.execute(
        select(AIOutput).where(
            AIOutput.id == feedback.ai_output_id,
            AIOutput.company_id == current_user.company_id,
        )
    )
    ai_output = result.scalar_one_or_none()
    if not ai_output:
        raise HTTPException(status_code=404, detail="AI output not found")

    feedback_record = AIOutputFeedback(
        ai_output_id=feedback.ai_output_id,
        company_id=current_user.company_id,
        user_id=current_user.user_uuid,
        action=feedback.action.value,
        modifications=feedback.modifications,
    )
    db.add(feedback_record)
    await db.commit()

    return {"status": "success"}


@router.get("/metrics/business")
async def get_business_metrics(
    minutes: int = Query(1440, ge=60, le=43200),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    return await business_metrics.get_all(minutes=minutes, company_id=str(current_user.company_id))


@router.get("/metrics/costs")
async def get_cost_metrics(
    minutes: int = Query(1440, ge=60, le=43200),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    return await business_metrics.get_cost_estimate(minutes=minutes, company_id=str(current_user.company_id))


@router.get("/metrics")
async def get_ai_metrics(
    minutes: int = Query(60, ge=1, le=1440),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    failure_rate = await audit.get_failure_rate(minutes=minutes)
    recent = await audit.get_recent(limit=20)
    return {
        "failure_rate": failure_rate,
        "recent_requests": [
            {
                "request_type": r.request_type,
                "latency_ms": r.latency_ms,
                "success": r.success,
                "circuit_state": r.circuit_state,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in recent
        ],
        "gateway": {
            "llm_circuit_state": gateway.llm_circuit.state.value,
            "whisper_circuit_state": gateway.whisper_circuit.state.value,
            "llm_active": gateway.llm_limiter.active_count,
            "whisper_active": gateway.whisper_limiter.active_count,
        },
    }


@router.get("/anomaly-check")
async def check_anomalies(
    minutes: int = Query(15, ge=5, le=1440),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    import json
    from datetime import datetime, timedelta

    from app.monitoring.anomaly import anomaly_detector

    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = await db.execute(
        select(AIOutput)
        .where(
            AIOutput.company_id == current_user.company_id,
            AIOutput.created_at >= cutoff,
            AIOutput.output_type == "job_analysis",
        )
        .order_by(AIOutput.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    outputs = rows.scalars().all()
    total = len(outputs)
    if total == 0:
        return {"total_outputs": 0, "alerts": [], "averages": {}}

    confidences = []
    fallback_count = 0
    partial_count = 0
    for o in outputs:
        try:
            data = json.loads(o.json_result)
            confidences.append(data.get("confidence", 0))
            if data.get("is_fallback"):
                fallback_count += 1
            if data.get("partial_failure"):
                partial_count += 1
        except Exception as _e:
            logger.debug("Failed to parse AI output for anomaly check: %s", _e)

    avg_conf = sum(confidences) / len(confidences) if confidences else 0
    fallback_rate = fallback_count / total if total > 0 else 0
    partial_rate = partial_count / total if total > 0 else 0

    alerts = await anomaly_detector.check_output_quality_drift(
        avg_conf,
        fallback_rate,
        partial_rate,
        company_id=str(current_user.company_id),
    )

    return {
        "time_window_minutes": minutes,
        "total_outputs": total,
        "averages": {
            "avg_confidence": round(avg_conf, 3),
            "fallback_rate": round(fallback_rate, 4),
            "partial_failure_rate": round(partial_rate, 4),
        },
        "alerts": alerts,
        "healthy": len(alerts) == 0,
    }


@router.get("/failures/classification")
async def get_failure_classification(
    minutes: int = Query(1440, ge=60, le=43200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_ai_enabled()
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.ai.failure_classifier import classify_failure
    from app.tracing.models import ExecutionTrace

    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    result = await db.execute(
        select(ExecutionTrace)
        .where(
            ExecutionTrace.step_name == "celery_failed",
            ExecutionTrace.company_id == current_user.company_id,
            ExecutionTrace.started_at >= cutoff,
        )
        .order_by(ExecutionTrace.started_at.desc())
        .limit(500)
    )
    traces = result.scalars().all()
    categories = {}
    for t in traces:
        cat = classify_failure(t.error_message or "").value
        if cat not in categories:
            categories[cat] = {"count": 0, "recent": []}
            categories[cat]["count"] += 1  # type: ignore[operator]
        if len(categories[cat]["recent"]) < 10:
            categories[cat]["recent"].append(  # type: ignore[attr-defined]
                {
                    "trace_id": str(t.trace_id),
                    "job_id": str(t.job_id) if t.job_id else None,
                    "error": t.error_message[:200] if t.error_message else "",
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                }
            )
    return {
        "time_window_minutes": minutes,
        "total_failures": len(traces),
        "categories": categories,
    }


# WebSocket connection tracking using Redis for global limits across replicas
_WS_CONNECT_RATE = 10
_WS_CONNECT_WINDOW = 60.0


# --- WebSocket DB poll rate limiter ---
class _WSDBPollLimiter:
    def __init__(self):
        self._last_poll: float = 0.0
        self._poll_timestamps: list = []
        # HIGH-2 FIX: Divide per-replica limit by estimated workers so
        # total DB poll rate across all replicas matches the global Redis limit.
        _effective_workers = max(1, _ESTIMATED_WS_WORKERS)
        self._max_polls_per_min = int(os.getenv("WS_DB_POLL_MAX_PER_MIN", "100")) // _effective_workers
        self._redis_max_polls_per_min = int(os.getenv("WS_DB_POLL_REDIS_MAX_PER_MIN", "500"))
        self._concurrent_pollers = 0

    async def can_poll_async(self) -> bool:
        """Async version of can_poll that checks Redis-based global rate limit.

        M1-FIX: Uses Redis sorted set with Lua for atomic global rate limiting
        across all replicas, preventing DB overload during Redis outage
        when all WS connections fall back to DB polling.
        HIGH-2 FIX: Reports concurrent poller count to Prometheus gauge.
        """
        now = time.monotonic()
        if now - self._last_poll < 5.0:
            return False
        self._last_poll = now

        # Check Redis-based global rate limit first
        try:
            from app.redis import get_redis

            redis_client = await get_redis()
            if redis_client and hasattr(redis_client, "eval"):
                import uuid

                member = f"poll:{uuid.uuid4()}:{now}"
                lua = """
                    local key = KEYS[1]
                    local now = tonumber(ARGV[1])
                    local window = tonumber(ARGV[2])
                    local max_count = tonumber(ARGV[3])
                    local cutoff = now - window
                    redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
                    local count = redis.call('ZCARD', key)
                    if count < max_count then
                        redis.call('ZADD', key, now, ARGV[4])
                        redis.call('EXPIRE', key, math.ceil(window * 2))
                        return {1, count + 1}
                    end
                    return {0, count}
                """
                result = await redis_client.eval(
                    lua, 1, "ws:db_poll_global", now, 60, self._redis_max_polls_per_min, member
                )
                self._concurrent_pollers = result[1]
                try:
                    from app.monitoring.prometheus import set_ws_db_poll_concurrent

                    set_ws_db_poll_concurrent(self._concurrent_pollers)
                except Exception as _e:
                    logger.debug("Failed to report Prometheus WS poll concurrent: %s", _e)
                if result[0] == 1:
                    return True
                logger.warning("Global WS DB poll rate limit exceeded (%d/min)", self._redis_max_polls_per_min)
                return False
        except Exception as _e:
            logger.debug("Redis global rate limit check failed: %s", _e)

        # HIGH-2 FIX: Process-level semaphore divided by estimated replicas
        # prevents DB thundering herd when Redis is unavailable.
        # MED-3 FIX: Ensure semaphore is released in ALL code paths.
        _sem_acquired = False
        try:
            await asyncio.wait_for(_ws_db_poll_semaphore.acquire(), timeout=2.0)
            _sem_acquired = True
        except TimeoutError:
            logger.warning(
                "WS DB poll semaphore timeout — all %d slots busy, skipping poll", _ws_db_poll_semaphore._value
            )
            return False
        try:
            cutoff = now - 60.0
            self._poll_timestamps = [t for t in self._poll_timestamps if t > cutoff]
            if len(self._poll_timestamps) >= self._max_polls_per_min:
                return False
            self._poll_timestamps.append(now)
            self._concurrent_pollers = len(self._poll_timestamps)
            try:
                from app.monitoring.prometheus import set_ws_db_poll_concurrent

                set_ws_db_poll_concurrent(self._concurrent_pollers)
            except Exception as _e:
                logger.debug("Failed to report Prometheus WS poll concurrent (fallback): %s", _e)
            return True
        finally:
            if _sem_acquired:
                _ws_db_poll_semaphore.release()

    def can_poll(self) -> bool:
        """Synchronous version (legacy, less accurate)."""
        import asyncio as _ws_aio

        try:
            loop = _ws_aio.get_running_loop()
        except RuntimeError:
            loop = _ws_aio.new_event_loop()
        return loop.run_until_complete(self.can_poll_async())


# --- WebSocket auth cache (LRU with TTL) ---
from cachetools import TTLCache  # noqa: E402

_WS_AUTH_CACHE_MAX_SIZE = int(os.getenv("WS_AUTH_CACHE_MAX_SIZE", "1000"))
_ws_auth_cache: TTLCache = TTLCache(maxsize=_WS_AUTH_CACHE_MAX_SIZE, ttl=120)
_ws_auth_cache_lock = asyncio.Lock()


async def _get_cached_ws_auth(user_id: str) -> dict | None:
    async with _ws_auth_cache_lock:
        return _ws_auth_cache.get(user_id)  # type: ignore[no-any-return]


async def _set_cached_ws_auth(user_id: str, data: dict, ttl: int = 120):
    async with _ws_auth_cache_lock:
        data["_cached_at"] = time.time()
        _ws_auth_cache[user_id] = data


async def _clear_stale_ws_auth():
    async with _ws_auth_cache_lock:
        _ws_auth_cache.expire()
    # Report cache size as Prometheus gauge
    try:
        from app.monitoring.prometheus import set_ws_auth_cache_size

        set_ws_auth_cache_size(len(_ws_auth_cache))
    except Exception as _e:
        logger.debug("Failed to report Prometheus WS auth cache size: %s", _e)


@dataclasses.dataclass
class _WSAuthUser:
    """Plain data holder for WebSocket-authenticated user.

    NOT an ORM object — prevents stale identity-map reads during re-auth.
    """

    id: str
    company_id: str
    is_active: bool
    auth_token_version: int


async def _verify_ws_token(token: str) -> _WSAuthUser:
    import jwt
    from sqlalchemy import select

    from app.auth.dependencies import _get_signing_key_from_jwt, _get_signing_key_from_redis
    from app.database import AsyncSessionLocal
    from app.jobs.models import User

    settings = get_settings()
    if not settings.clerk_jwt_issuer:
        raise HTTPException(status_code=401, detail="JWT verification not configured")

    try:
        signing_key = await _get_signing_key_from_redis(token)
        if not signing_key:
            signing_key = _get_signing_key_from_jwt(token)
        if not signing_key:
            raise jwt.InvalidTokenError("No signing key available")
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_jwt_issuer.rstrip("/"),
            audience=settings.clerk_jwt_audience,
            options={"verify_exp": True, "verify_nbf": True, "verify_iat": True, "verify_aud": True},
        )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="Token expired") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    from app.db.rls import set_rls_bypass_context

    async with AsyncSessionLocal() as db:
        await set_rls_bypass_context(db)  # Token verification requires cross-tenant User lookup
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=401, detail="Account deactivated")
        token_version = payload.get("token_version", 0)
        if token_version < user.token_version:
            raise HTTPException(status_code=401, detail="Token revoked")
        return _WSAuthUser(
            id=str(user.id),
            company_id=str(user.company_id),
            is_active=user.is_active,
            auth_token_version=user.token_version,
        )


def _ws_origin_allowed(origin: str | None) -> bool:
    """Validate WebSocket Origin against configured CORS origins.

    WebSocket connections bypass HTTP middleware CORS checks, so we
    validate the Origin header in-band before accepting the connection.
    """
    if not origin:
        logger.warning("WebSocket connection missing Origin header — rejecting")
        return False
    from app.config import get_settings as _gs

    _settings = _gs()
    allowed = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]
    if "*" in allowed:
        logger.error("WebSocket Origin validation skipped — CORS contains wildcard '*'")
        return False
    origin = origin.rstrip("/")
    for a in allowed:
        if origin == a.rstrip("/"):
            return True
    logger.warning("WebSocket Origin '%s' not in allowed list", origin)
    return False


# M-2: Derive semaphore value from settings/env var with fallback to 50
_ws_accept_semaphore = asyncio.Semaphore(int(os.getenv("WS_ACCEPT_SEMAPHORE", "50")))

# M-5: Shared semaphore for DB polling across all WS connections in this process
_ws_db_poll_semaphore = asyncio.Semaphore(int(os.getenv("WS_DB_POLL_CONCURRENCY", "5")))


async def _send_ws_json_safe(send_queue: asyncio.Queue, job_id: str, data: dict, websocket: WebSocket | None = None):
    """Send JSON via queue with backpressure. If queue is full, drop the oldest
    queued message to make room — newer status updates are more valuable.
    """
    try:
        await asyncio.wait_for(send_queue.put(data), timeout=5.0)
    except TimeoutError:
        try:
            send_queue.get_nowait()
            await asyncio.wait_for(send_queue.put(data), timeout=5.0)
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_ws_queue_dropped_old", {"job_id": job_id})
        except Exception as _e:
            logger.debug("WS send queue blocked: %s", _e)
            logger.warning("WS send queue still blocked after dropping oldest for job %s", job_id)


async def _ws_sender(websocket: WebSocket, send_queue: asyncio.Queue):
    """Background sender task that drains the send queue.

    L-3 FIX: Tracks message delivery latency for WebSocket SLO monitoring.
    """
    while True:
        try:
            msg = await send_queue.get()
            if msg is None:
                break
            _send_start = time.time()
            await websocket.send_json(msg)
            _elapsed_ms = (time.time() - _send_start) * 1000
            try:
                from app.monitoring.prometheus import observe_ws_message_latency

                observe_ws_message_latency(_elapsed_ms)
            except Exception as _e:
                logger.debug("Failed to observe ws message latency: %s", _e)
        except WebSocketDisconnect:
            break
        except Exception as _e:
            logger.debug("WS sender loop error: %s", _e)
            break


@router.websocket("/ws/job-status/{job_id}")
async def websocket_job_status(
    websocket: WebSocket,
    job_id: UUID,
    last_event_id: str = "",
):
    settings = get_settings()
    if settings.ai_disabled or _flags.is_enabled(FeatureFlags.AI_DISABLED):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="AI features are disabled")
        return
    if not _WS_ENABLED:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="WebSocket endpoint not enabled")
        return

    # Throttle concurrent WS accept with semaphore (reconnect storm mitigation)
    if not _ws_accept_semaphore.locked():
        async with _ws_accept_semaphore:
            return await _websocket_job_status_handler(websocket, job_id, last_event_id)
    else:
        # Semaphore is contended, acquire with wait
        async with _ws_accept_semaphore:
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter("workticket_ws_accept_throttled_total", {})
            except Exception as _e:
                logger.debug("Failed to increment ws accept throttled metric: %s", _e)
            return await _websocket_job_status_handler(websocket, job_id, last_event_id)


_pending_ws_tasks: dict[str, set[asyncio.Task]] = {}
_ws_tasks_lock = asyncio.Lock()

# W2: Distributed WS connection tracking with Redis heartbeat
_WORKER_ID = os.getenv("HOSTNAME", os.getenv("WORKER_ID", f"ws-{uuid.uuid4().hex[:8]}"))
_WS_REPLICA_HEARTBEAT_KEY = "ws:replicas"


async def _update_ws_replica_heartbeat():
    """Update per-replica WS connection count in Redis (60s TTL heartbeat)."""
    try:
        r = await _get_redis()
        if r:
            count = len(_active_websockets)
            await r.hset(_WS_REPLICA_HEARTBEAT_KEY, _WORKER_ID, str(count))
            await r.expire(_WS_REPLICA_HEARTBEAT_KEY, 120)
    except Exception as _e:
        logger.debug("Failed to update WS replica heartbeat: %s", _e)


async def _start_ws_heartbeat():
    """Background loop: refresh replica heartbeat every 30s."""
    while True:
        await asyncio.sleep(30)
        await _update_ws_replica_heartbeat()


async def _track_ws_task(conn_id: str, coro):
    task = asyncio.create_task(coro)
    async with _ws_tasks_lock:
        if conn_id not in _pending_ws_tasks:
            _pending_ws_tasks[conn_id] = set()
        _pending_ws_tasks[conn_id].add(task)
    task.add_done_callback(lambda t: asyncio.ensure_future(_discard_ws_task(conn_id, t)))
    return task


async def _discard_ws_task(conn_id: str, task: asyncio.Task):
    async with _ws_tasks_lock:
        if conn_id in _pending_ws_tasks:
            _pending_ws_tasks[conn_id].discard(task)
            if not _pending_ws_tasks[conn_id]:
                del _pending_ws_tasks[conn_id]


async def _cancel_all_ws_tasks(conn_id: str):
    async with _ws_tasks_lock:
        tasks = _pending_ws_tasks.pop(conn_id, set())
    for task in tasks:
        task.cancel()


async def _replay_missed_events(websocket: WebSocket, job_id: UUID, last_event_id: str, send_queue: asyncio.Queue, company_id: UUID | None = None):
    """Replay missed job status events from Redis Streams (primary) and DB (fallback).

    CRITICAL-2 FIX: Uses Redis Streams XRANGE to replay ALL missed events
    since last_event_id, not just one pending message. Falls back to DB
    polling if Redis Streams is unavailable. The event stream stores each
    status change as a stream entry with auto-generated IDs for accurate
    at-least-once replay.
    """
    if not last_event_id:
        return
    try:
        # Primary: Redis Streams replay
        redis_client = await _get_redis()
        if redis_client:
            stream_key = f"stream:job:{job_id}"
            try:
                stream_entries = await redis_client.xrange(
                    stream_key,
                    min=f"({last_event_id}",  # Exclusive: after last_event_id
                    max="+",
                    count=100,
                )
                if stream_entries:
                    for entry_id, fields in stream_entries:
                        event_data = {}
                        for i in range(0, len(fields), 2):
                            key = fields[i].decode() if isinstance(fields[i], bytes) else fields[i]
                            val = fields[i + 1].decode() if isinstance(fields[i + 1], bytes) else fields[i + 1]
                            event_data[key] = val

                        await _send_ws_json_safe(
                            send_queue,
                            str(job_id),
                            {
                                "version": 1,
                                "event_id": entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
                                "job_id": str(job_id),
                                "status": event_data.get("status", "unknown"),
                                "output_available": event_data.get("status") == "complete",
                                "system_state": event_data.get("system_state", "healthy"),
                                "timestamp": event_data.get("timestamp", datetime.now(UTC).isoformat()),
                                "replayed": True,
                            },
                            websocket=websocket,
                        )
                    return
            except Exception as _e:
                logger.debug("Redis Streams replay error: %s", _e)
                logger.debug("Redis Streams replay unavailable for job %s, falling back to DB", job_id)

        # Fallback: DB trace-based replay
        from sqlalchemy import select

        from app.database import AsyncSessionLocal
        from app.tracing.models import ExecutionTrace

        async with AsyncSessionLocal() as replay_db:
            from app.db.rls import set_rls_tenant_context
            if company_id:
                await set_rls_tenant_context(replay_db, company_id)
            result = await replay_db.execute(
                select(ExecutionTrace)
                .where(
                    ExecutionTrace.job_id == job_id,
                    ExecutionTrace.id > last_event_id,
                    ExecutionTrace.step_name.in_(["output_stored", "celery_start", "celery_failed"]),
                )
                .order_by(ExecutionTrace.id.asc())
                .limit(50)
            )
            traces = result.scalars().all()
            for trace in traces:
                status_map = {
                    "output_stored": "complete",
                    "celery_start": "processing",
                    "celery_failed": "failed",
                }
                await _send_ws_json_safe(
                    send_queue,
                    str(job_id),
                    {
                        "version": 1,
                        "event_id": str(trace.id),
                        "job_id": str(job_id),
                        "status": status_map.get(trace.step_name, "processing"),
                        "output_available": trace.step_name == "output_stored",
                        "system_state": "healthy",
                        "timestamp": trace.started_at.isoformat()
                        if hasattr(trace, "started_at") and trace.started_at
                        else datetime.now(UTC).isoformat(),
                        "replayed": True,
                    },
                    websocket=websocket,
                )
    except Exception as _e:
        logger.debug("Event replay error: %s", _e)
        logger.warning("Event replay failed for job %s (last_event_id=%s)", job_id, last_event_id)


async def _websocket_job_status_handler(
    websocket: WebSocket,
    job_id: UUID,
    last_event_id: str = "",
):
    conn_id = f"{job_id}:{id(websocket)}"
    # Validate Origin header (WebSocket connections bypass HTTP CORS middleware)
    ws_origin = websocket.headers.get("origin")
    if not _ws_origin_allowed(ws_origin):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Origin not allowed")
        return

    # H-3 FIX: Validate token BEFORE incrementing connection counters.
    # Previously, auth was checked AFTER incrementing global/per-user slots,
    # allowing attackers to exhaust WS slots by opening connections without
    # authenticating.
    token = websocket.headers.get("sec-websocket-protocol", "")
    if token and token.startswith("authorization."):
        token = token[len("authorization.") :]
        logger.warning(
            "WebSocket auth via sec-websocket-protocol header is deprecated "
            "and will be removed in v2.0. Use the first message frame "
            "({'type': 'auth', 'token': '...'}) instead."
        )

    if not token:
        try:
            # H-3 FIX: Reduced timeout from 5s to 2s for faster rejection of unauthenticated connections
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
            if msg:
                try:
                    msg_data = json.loads(msg)
                    if isinstance(msg_data, dict) and msg_data.get("type") == "auth":
                        token = msg_data.get("token", "")
                except json.JSONDecodeError:
                    token = msg
        except TimeoutError:
            pass

    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing authentication token")
        return

    current_user = None
    _ws_global_member = ""
    try:
        current_user = await _verify_ws_token(token)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication failed")
        return
    except Exception as _e:
        logger.debug("WS auth verification error: %s", _e)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Authentication error")
        return

    if current_user is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="Authentication error")
        return

    user_id = str(current_user.id)
    _client_ip = websocket.client.host if websocket.client else "unknown"

    # Check that the job exists and belongs to the user's company (without leaking which)
    from app.database import AsyncSessionLocal
    from app.jobs.models import Job

    async with AsyncSessionLocal() as db_session:
        from app.db.rls import set_rls_tenant_context
        await set_rls_tenant_context(db_session, current_user.company_id)
        job_result = await db_session.execute(
            select(Job).where(Job.id == job_id, Job.company_id == current_user.company_id)
        )
        job = job_result.scalar_one_or_none()
        if not job:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Job not found or access denied")
            return

    # H-3 FIX: Connection counters incremented AFTER successful authentication.
    # Global connection cap (H-7): Use SADD/SREM/SCARD for accurate tracking
    global_allowed, _ws_global_member = await _increment_ws_global()
    if not global_allowed:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Server at capacity")
        return

    # C4-FIX: Capture the member ID for accurate sorted-set removal on disconnect
    ws_rate_limit, _ws_remaining, _ws_conn_member = await _increment_ws_connection(user_id)
    if not ws_rate_limit:
        await _decrement_ws_global(_ws_global_member)
        effective_max = _MAX_WS_CONNECTIONS_PER_USER
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason=f"Too many WebSocket connections (max {effective_max})"
        )
        return

    await websocket.accept()

    # Backpressure: async send queue with maxsize 1024
    _send_queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
    _sender_task = await _track_ws_task(conn_id, _ws_sender(websocket, _send_queue))

    # Replay missed events after WS accept and queue setup (W1)
    await _replay_missed_events(websocket, job_id, last_event_id, send_queue=_send_queue, company_id=current_user.company_id)

    # Track active WebSocket connection for graceful shutdown
    async with _active_websockets_lock:
        _active_websockets.add(websocket)
    try:
        from app.monitoring.prometheus import set_ws_connections

        set_ws_connections(len(_active_websockets))
    except Exception as _e:
        logger.debug("Failed to report ws_connections metric after accept: %s", _e)
    # W2: Update replica heartbeat after connect
        _heartbeat_future = asyncio.ensure_future(_update_ws_replica_heartbeat())  # noqa: RUF006

    # W2: Start replica heartbeat loop if not already running
    if not hasattr(_websocket_job_status_handler, "_heartbeat_started"):
        _websocket_job_status_handler._heartbeat_started = True  # type: ignore[attr-defined]
        await _track_ws_task(conn_id, _start_ws_heartbeat())

    _ws_msg_redis_key = f"ws_msg:{user_id}:{job_id}"
    _ws_pubsub_channel = f"job_status:{job_id}"

    from app.database import AsyncSessionLocal

    # Try to subscribe to Redis pub/sub for event-driven updates
    _redis_pubsub = None
    _pubsub_broken = False
    _pubsub_error_count = 0
    try:
        redis_client = await _get_redis()
        if redis_client:
            _redis_pubsub = redis_client.pubsub()
            await _redis_pubsub.subscribe(_ws_pubsub_channel)
    except Exception as _e:
        logger.debug("Redis pubsub subscribe failed: %s", _e)

    # Capture auth token_version at connect time
    _auth_token_version = current_user.auth_token_version
    _last_auth_check = time.time()
    _reauth_interval = 60.0
    _poll_interval = 30.0
    _consecutive_no_data = 0
    _ws_poll_limiter = _WSDBPollLimiter()
    _redis_available = True
    _pubsub = _redis_pubsub
    _db_poll_interval = 30.0

    try:
        while True:
            try:
                _recv_timeout = min(_poll_interval, 5.0 if _redis_available else _db_poll_interval)
                _recv_timeout = min(_recv_timeout, 30.0)  # Cap at 30s to detect unclean disconnects
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=_recv_timeout + random.uniform(0, min(_recv_timeout, 10.0) * 0.1),  # nosec B311
                )
                now = time.time()

                # Exponential backoff on reauth: start at 60s, double on DB error, max 300s
                if now - _last_auth_check > _reauth_interval:
                    _reauth_hit_cache = False
                    _reauth_hit_db = False
                    _reauth_errored = False
                    try:
                        cached_local = await _get_cached_ws_auth(current_user.id)
                        if cached_local:
                            _reauth_hit_cache = True
                            if cached_local.get("token_version", 0) > _auth_token_version:
                                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked")
                                return
                            if not cached_local.get("is_active", True):
                                await websocket.close(
                                    code=status.WS_1008_POLICY_VIOLATION, reason="Account deactivated"
                                )
                                return
                        else:
                            reauth_key = f"ws_auth:{current_user.id}"
                            _redis_reauth = await _get_redis()
                            cached = await _redis_reauth.get(reauth_key) if _redis_reauth else None
                            if cached:
                                _reauth_hit_cache = True
                                cached_data = json.loads(cached)
                                await _set_cached_ws_auth(current_user.id, cached_data)
                                if cached_data.get("token_version", 0) > _auth_token_version:
                                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked")
                                    return
                                if not cached_data.get("is_active", True):
                                    await websocket.close(
                                        code=status.WS_1008_POLICY_VIOLATION, reason="Account deactivated"
                                    )
                                    return
                            else:
                                _reauth_hit_db = True
                                async with AsyncSessionLocal() as reauth_db:
                                    from app.db.rls import set_rls_bypass_context
                                    await set_rls_bypass_context(reauth_db)  # Re-auth User lookup
                                    recheck = await reauth_db.execute(select(User).where(User.id == current_user.id))
                                    fresh_user = recheck.scalar_one_or_none()
                                    if not fresh_user or not fresh_user.is_active:
                                        await websocket.close(
                                            code=status.WS_1008_POLICY_VIOLATION, reason="Account deactivated"
                                        )
                                        return
                                    if fresh_user.token_version > _auth_token_version:
                                        await websocket.close(
                                            code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked"
                                        )
                                        return
                                    writeback = {
                                        "token_version": fresh_user.token_version,
                                        "is_active": fresh_user.is_active,
                                        "_cached_at": time.time(),
                                    }
                                    await _set_cached_ws_auth(current_user.id, writeback)
                                    if _redis_reauth:
                                        await _redis_reauth.setex(
                                            reauth_key,
                                            120,
                                            json.dumps(
                                                {
                                                    "token_version": fresh_user.token_version,
                                                    "is_active": fresh_user.is_active,
                                                }
                                            ),
                                        )
                        try:
                            from app.monitoring.prometheus import increment_counter

                            if _reauth_hit_cache:
                                increment_counter("workticket_ws_reauth_cache_hits", {"user_id": current_user.id})
                            if _reauth_hit_db:
                                increment_counter("workticket_ws_reauth_db_hits", {"user_id": current_user.id})
                        except Exception as _e:
                            logger.debug("Failed to increment ws reauth metric: %s", _e)
                    except Exception as auth_err:
                        _reauth_errored = True
                        logger.error("WS re-auth check failed: %s", auth_err)
                    _reauth_interval = min(300.0, _reauth_interval * 2) if _reauth_errored else 60.0
                    _last_auth_check = now
                    # Periodically clear stale auth cache entries (C5)
                    await _clear_stale_ws_auth()

                # Rate limit check BEFORE ping handling (H2)
                if redis_client:
                    msg_key = f"ws_msg_rate:{user_id}:{job_id}"
                    msg_count = await redis_client.incr(msg_key)
                    if msg_count == 1:
                        await redis_client.expire(msg_key, 60)
                    if msg_count > 60:
                        ttl = await redis_client.ttl(msg_key)
                        await _send_ws_json_safe(
                            _send_queue,
                            str(job_id),
                            {
                                "type": "error",
                                "message": "Rate limit exceeded",
                                "limit": 60,
                                "remaining": 0,
                                "retry_after": max(1, ttl),
                                "reset": int(now + max(1, ttl)),
                            },
                            websocket=websocket,
                        )
                        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Rate limit exceeded")
                        return
                else:
                    if not hasattr(websocket.state, "ws_msg_timestamps"):
                        websocket.state.ws_msg_timestamps = deque(maxlen=120)
                    websocket.state.ws_msg_timestamps.append(now)
                    cutoff = now - 60.0
                    while websocket.state.ws_msg_timestamps and websocket.state.ws_msg_timestamps[0] <= cutoff:
                        websocket.state.ws_msg_timestamps.popleft()
                    if len(websocket.state.ws_msg_timestamps) > 60:
                        retry_after = (
                            int(websocket.state.ws_msg_timestamps[0] + 60.0 - now)
                            if websocket.state.ws_msg_timestamps
                            else 60
                        )
                        await _send_ws_json_safe(
                            _send_queue,
                            str(job_id),
                            {
                                "type": "error",
                                "message": "Rate limit exceeded",
                                "limit": 60,
                                "remaining": 0,
                                "retry_after": max(1, retry_after),
                                "reset": int(now + max(1, retry_after)),
                            },
                            websocket=websocket,
                        )
                        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Rate limit exceeded")
                        return

                if data == "ping":
                    await _send_ws_json_safe(_send_queue, str(job_id), {"type": "pong"}, websocket=websocket)
                    with contextlib.suppress(Exception):
                        await websocket.pong()  # type: ignore[attr-defined]
                    continue

            except TimeoutError:
                pass

            # PRIMARY PATH: Redis PubSub for event-driven updates
            job_complete = False
            if _redis_available and _pubsub and not _pubsub_broken:
                try:
                    message = await asyncio.wait_for(
                        _pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5),
                        timeout=1.0,
                    )
                    if message and message.get("data"):
                        status_data = json.loads(message["data"])
                        job_complete = status_data.get("status") == "complete"
                        if job_complete:
                            _consecutive_no_data = 0
                            _poll_interval = 30.0
                            _db_poll_interval = 30.0
                            await _send_ws_json_safe(
                                _send_queue,
                                str(job_id),
                                {
                                    "version": 1,
                                    "event_id": str(uuid.uuid4()),
                                    "job_id": str(job_id),
                                    "status": "complete",
                                    "output_available": True,
                                    "system_state": "degraded"
                                    if not (
                                        await gateway.llm_circuit.is_available()
                                        and await gateway.whisper_circuit.is_available()
                                    )
                                    else "healthy",
                                    "timestamp": datetime.now(UTC).isoformat(),
                                },
                                websocket=websocket,
                            )
                            break
                    _pubsub_error_count = 0
                except TimeoutError:
                    pass  # No message yet, continue to next iteration
                except Exception as pubsub_err:
                    _pubsub_error_count += 1
                    if _pubsub_error_count >= 3:
                        logger.warning(
                            "WebSocket pubsub broken after %d errors for job %s: %s — falling back to DB polling",
                            _pubsub_error_count,
                            job_id,
                            pubsub_err,
                        )
                        _redis_available = False
                        _pubsub_broken = True
                        try:
                            await asyncio.wait_for(_pubsub.unsubscribe(_ws_pubsub_channel), timeout=2.0)
                            await asyncio.wait_for(_pubsub.close(), timeout=2.0)
                        except Exception as _e:
                            logger.debug("Failed to unsubscribe/close broken pubsub: %s", _e)
                        _pubsub = None
                        # Track pubsub degradation in metrics
                        try:
                            from app.monitoring.prometheus import increment_counter

                            increment_counter("workticket_ws_pubsub_fallback_total", {"reason": "error"})
                        except Exception as _e:
                            logger.debug("Failed to increment pubsub fallback metric: %s", _e)

            # FALLBACK PATH: DB polling only when Redis PubSub is unavailable
            # M1-FIX: Use async version with global Redis rate limiting
            if not _redis_available and await _ws_poll_limiter.can_poll_async():
                try:
                    from app.monitoring.prometheus import increment_counter as _inc_cnt

                    _inc_cnt("ws_db_poll_count_total", {"job_id": str(job_id)})
                except Exception as _e:
                    logger.debug("Failed to increment ws db poll count metric: %s", _e)

                # M-5: Use a shared semaphore for DB polling across all WS connections
                try:
                    await asyncio.wait_for(_ws_db_poll_semaphore.acquire(), timeout=5.0)
                except TimeoutError:
                    continue
                try:
                    async with AsyncSessionLocal() as poll_db:
                        from app.db.rls import set_rls_tenant_context
                        await set_rls_tenant_context(poll_db, current_user.company_id)
                        from app.jobs.models import AIProcessingState, Job

                        job_result = await poll_db.execute(select(Job).where(Job.id == job_id))
                        current_job = job_result.scalar_one_or_none()
                        result = await poll_db.execute(
                            select(AIOutput)
                            .where(AIOutput.job_id == job_id, AIOutput.company_id == current_user.company_id)
                            .order_by(AIOutput.created_at.desc())
                        )
                        ai_output = result.scalar_one_or_none()

                        if (
                            ai_output
                            and current_job
                            and current_job.ai_processing_state == AIProcessingState.completed.value
                        ):
                            _consecutive_no_data = 0
                            _db_poll_interval = 30.0
                            await _send_ws_json_safe(
                                _send_queue,
                                str(job_id),
                                {
                                    "version": 1,
                                    "event_id": str(uuid.uuid4()),
                                    "job_id": str(job_id),
                                    "status": "complete",
                                    "output_available": True,
                                    "confidence_score": ai_output.confidence_score,
                                    "model_used": ai_output.model_used,
                                    "system_state": "degraded"
                                    if not (
                                        await gateway.llm_circuit.is_available()
                                        and await gateway.whisper_circuit.is_available()
                                    )
                                    else "healthy",
                                    "timestamp": datetime.now(UTC).isoformat(),
                                },
                                websocket=websocket,
                            )
                            try:
                                from app.monitoring.prometheus import increment_ws_messages_sent

                                increment_ws_messages_sent(str(job_id))
                            except Exception as _e:
                                logger.debug("Failed to increment ws messages sent metric: %s", _e)
                            break
                        else:
                            _consecutive_no_data += 1
                            if _consecutive_no_data > 12:
                                _db_poll_interval = min(120.0, _db_poll_interval * 1.5)
                            elif _consecutive_no_data > 6:
                                _db_poll_interval = min(60.0, _db_poll_interval * 1.2)
                            await _send_ws_json_safe(
                                _send_queue,
                                str(job_id),
                                {
                                    "version": 1,
                                    "event_id": str(uuid.uuid4()),
                                    "job_id": str(job_id),
                                    "status": "processing",
                                    "output_available": False,
                                    "system_state": "degraded"
                                    if not (
                                        await gateway.llm_circuit.is_available()
                                        and await gateway.whisper_circuit.is_available()
                                    )
                                    else "healthy",
                                    "timestamp": datetime.now(UTC).isoformat(),
                                },
                                websocket=websocket,
                            )
                except Exception as poll_err:
                    logger.error("WebSocket poll error for job %s: %s", job_id, poll_err)
                finally:
                    _ws_db_poll_semaphore.release()

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for job %s", job_id)
    except Exception as ws_err:
        logger.error("WebSocket error for job %s: %s", job_id, ws_err)
    finally:
        # Cancel ALL spawned tasks for this connection
        await _cancel_all_ws_tasks(conn_id)
        # Signal sender task to stop
        try:
            await _send_queue.put(None)
            await asyncio.wait_for(_sender_task, timeout=2.0)
        except Exception as _e:
            logger.debug("WS sender task wait failed: %s", _e)
            _sender_task.cancel()
        if _pubsub:
            try:
                await asyncio.wait_for(_pubsub.unsubscribe(_ws_pubsub_channel), timeout=2.0)
                await asyncio.wait_for(_pubsub.close(), timeout=2.0)
            except Exception as _e:
                logger.debug("WebSocket pubsub cleanup in finally failed: %s", _e)
        if current_user:
            # C4-FIX: Pass the connection member ID for accurate sorted-set removal
            await _decrement_ws_connection(str(current_user.id), _ws_conn_member if _ws_conn_member else "")
        # H-7: Pass the stored member to ensure accurate SREM
        await _decrement_ws_global(_ws_global_member)
        async with _active_websockets_lock:
            _active_websockets.discard(websocket)
        try:
            from app.monitoring.prometheus import set_ws_connections

            set_ws_connections(len(_active_websockets))
        except Exception as _e:
            logger.debug("Failed to set ws_connections metric in finally: %s", _e)
        # W2: Update replica heartbeat after disconnect
    _heartbeat_future = asyncio.ensure_future(_update_ws_replica_heartbeat())  # noqa: RUF006


@router.get("/ws-connections")
async def get_ws_connections():
    """Return active WebSocket connection count across all replicas (W2)."""
    total_local = len(_active_websockets)
    try:
        r = await _get_redis()
        if r:
            replica_data = await r.hgetall(_WS_REPLICA_HEARTBEAT_KEY)
            replica_counts = {k.decode(): int(v.decode()) for k, v in replica_data.items()}
            total_redis = sum(replica_counts.values())
            return {
                "total_active": max(total_local, total_redis),
                "local_replica": total_local,
                "per_replica": replica_counts,
                "worker_id": _WORKER_ID,
            }
    except Exception as _e:
        logger.debug("Failed to get WS connection counts from Redis: %s", _e)
    return {"total_active": total_local, "local_replica": total_local, "worker_id": _WORKER_ID}

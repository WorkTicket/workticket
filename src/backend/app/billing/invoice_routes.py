import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, BigInteger, bindparam
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.billing.cost_estimator import PLAN_TIERS
from app.billing.models import Invoice, StripeWebhookEvent
from app.billing.quota_engine import quota_engine
from app.billing.stripe_cache import (
    invalidate_subscription_cache,
    set_cached_subscription,
)
from app.config import get_settings
from app.database import get_db
from app.jobs.models import Company, User

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()
BASE_URL = settings.app_base_url or "http://localhost:3000"

stripe.api_key = settings.stripe_secret_key
stripe.api_timeout = settings.stripe_api_timeout  # type: ignore[attr-defined]
_STRIPE_TIMEOUT = settings.stripe_api_timeout

_stripe_circuit = None
_webhook_rate: OrderedDict[str, float] = OrderedDict()
_MAX_WEBHOOK_IPS = 10000
_webhook_rate_limit = 10
_webhook_window = 60.0
_WEBHOOK_GLOBAL_MAX_CONCURRENT = 5
_WEBHOOK_GLOBAL_RATE_LIMIT = 60
_WEBHOOK_GLOBAL_RATE_WINDOW = 60


_csv_control_chars = str.maketrans(
    "".join(chr(c) for c in range(0x20) if chr(c) not in ("\t", "\n", "\r")), " " * 29
)

_CSV_DANGEROUS_PATTERNS = [
    r"(?i)=HYPERLINK\s*\(",
    r"(?i)=DDE\s*\(",
    r"(?i)=CMD\s*\(",
    r"(?i)=EXEC\s*\(",
    r"(?i)=SHELL\s*\(",
    r"(?i)=MSEXCEL\|",
    r"(?i)TABLE\s+",
    r"<script[^>]*>",
    r"javascript\s*:",
]


def _sanitize_csv(val: str) -> str:
    if not val:
        return val
    val = val.replace('"', '""').translate(_csv_control_chars)
    if re.match(r"^[\s]*[=+\-@\t\n\r|%&{}]", val):
        val = "'" + val
    for pattern in _CSV_DANGEROUS_PATTERNS:
        if re.search(pattern, val):
            val = "'" + val
    return val


async def _get_stripe_circuit():
    global _stripe_circuit
    if _stripe_circuit is None:
        from app.ai.circuit_breaker import CircuitBreaker

        _stripe_circuit = CircuitBreaker(
            name="stripe-api",
            failure_threshold=3,
            cooldown_seconds=120.0,
            half_open_max_retries=1,
        )
    return _stripe_circuit


async def _acquire_webhook_slot():
    try:
        from app.ai.rate_limiter import _get_redis

        r = await _get_redis()
        if r:
            count = await r.incr("stripe:webhook:concurrent")
            await r.expire("stripe:webhook:concurrent", 30)
            if count > _WEBHOOK_GLOBAL_MAX_CONCURRENT:
                await r.decr("stripe:webhook:concurrent")
                raise HTTPException(status_code=429, detail="Too many concurrent webhook requests")
            return True
    except HTTPException:
        raise
    except Exception as _e:
        logger.debug("Webhook slot acquire failed: %s", _e)
    return True


async def _release_webhook_slot():
    try:
        from app.ai.rate_limiter import _get_redis

        r = await _get_redis()
        if r:
            _lua = """
                local key = KEYS[1]
                local val = redis.call('GET', key)
                if val and tonumber(val) > 1 then
                    return redis.call('DECR', key)
                end
                redis.call('DEL', key)
                return 0
            """
            try:
                await r.eval(_lua, 1, "stripe:webhook:concurrent")
            except Exception:
                await r.decr("stripe:webhook:concurrent")
    except Exception as _e:
        logger.debug("Webhook slot release failed: %s", _e)


async def _check_global_webhook_rate() -> None:
    try:
        from app.ai.rate_limiter import _get_redis

        redis = await _get_redis()
        if redis:
            _key = "stripe:webhook:global_rate"
            _now_ts = int(time.time())
            _window_start = _now_ts - _WEBHOOK_GLOBAL_RATE_WINDOW
            await redis.zremrangebyscore(_key, 0, _window_start)
            _count = await redis.zcard(_key)
            if _count >= _WEBHOOK_GLOBAL_RATE_LIMIT:
                raise HTTPException(status_code=429, detail="Global webhook rate limit exceeded")
            await redis.zadd(_key, {str(_now_ts): _now_ts})
            await redis.expire(_key, _WEBHOOK_GLOBAL_RATE_WINDOW + 10)
            return
    except HTTPException:
        raise
    except Exception as _e:
        logger.debug("Global webhook rate check failed: %s", _e)


async def _check_webhook_rate(ip: str) -> None:
    try:
        from app.ai.rate_limiter import _get_redis

        redis = await _get_redis()
        if redis:
            key = f"webhook_rate:{ip}"
            now = time.time()
            count = await redis.get(key)
            if count and int(count) >= _webhook_rate_limit:
                raise HTTPException(status_code=429, detail="Too many webhook requests")
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, int(_webhook_window))
            await pipe.execute()
            return
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Redis webhook rate limiter unavailable - using local fallback: %s", e)

    now = time.time()
    cutoff = now - _webhook_window
    expired = [k for k, ts in _webhook_rate.items() if ts < cutoff]
    for k in expired:
        _webhook_rate.pop(k, None)
    count = sum(1 for ts in _webhook_rate.values() if ts > cutoff)
    if count >= _webhook_rate_limit:
        raise HTTPException(status_code=429, detail="Too many webhook requests")
    _webhook_rate[ip] = now
    if len(_webhook_rate) > _MAX_WEBHOOK_IPS:
        _webhook_rate.popitem(last=False)


async def _validate_stripe_ip(client_ip: str) -> None:
    from app.billing.stripe_ips import validate_stripe_ip as _validate

    if not await _validate(client_ip):
        logger.warning("Webhook request from non-Stripe IP: %s", client_ip)
        raise HTTPException(status_code=403, detail="Webhook source IP not allowed")


@router.post("/create-checkout-session")
async def create_checkout_session(
    current_user: User = Depends(get_current_user),
):
    if not settings.stripe_secret_key:
        logger.warning("Stripe not configured, returning mock URL")
        return {"url": "https://checkout.stripe.com/mock"}

    import uuid as _uuid

    _idempotency_key = str(_uuid.uuid4())

    plan = "pro"
    price_id = settings.stripe_price_id
    if settings.stripe_price_map:
        try:
            price_map = json.loads(settings.stripe_price_map)
            price_id = price_map.get(plan, price_id)
        except json.JSONDecodeError:
            logger.warning("stripe_price_map is not valid JSON, falling back to stripe_price_id")

    stripe_circuit = await _get_stripe_circuit()
    if not await stripe_circuit.is_available():
        logger.warning("Stripe circuit breaker OPEN — rejecting checkout session creation")
        raise HTTPException(
            status_code=502, detail="Payment processing temporarily unavailable. Please try again later."
        )
    try:
        session = await asyncio.to_thread(
            lambda: stripe.checkout.Session.create(
                customer_email=current_user.email,
                mode="subscription",
                line_items=[
                    {
                        "price": price_id,
                        "quantity": 1,
                    }
                ],
                metadata={
                    "company_id": str(current_user.company_id),
                    "user_id": current_user.id,
                    "plan": plan,
                },
                success_url=f"{BASE_URL}/dashboard?checkout=success",
                cancel_url=f"{BASE_URL}/dashboard?checkout=cancelled",
                idempotency_key=_idempotency_key,
            )
        )
        await stripe_circuit.record_success()
        return {"url": session.url}
    except stripe.StripeError as e:
        await stripe_circuit.record_failure()
        logger.error("Stripe checkout failed: %s", e)
        raise HTTPException(status_code=400, detail="Failed to create checkout session") from e


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    _wh_start = time.monotonic()
    await _acquire_webhook_slot()
    try:
        client_ip = request.client.host if request.client else "unknown"
        try:
            result = await _process_webhook(request, db, client_ip)
            _wh_ms = (time.monotonic() - _wh_start) * 1000
            try:
                from app.monitoring.prometheus import observe_stripe_webhook_latency

                observe_stripe_webhook_latency(_wh_ms)
            except Exception:
                pass  # nosec B110
            return result
        except HTTPException:
            raise
        except Exception as _webhook_err:
            logger.critical("Unhandled webhook processing error: %s", _webhook_err, exc_info=True)
            raise HTTPException(status_code=503, detail="Webhook processing failed, will retry") from _webhook_err
    finally:
        await _release_webhook_slot()


async def _process_webhook(
    request: Request,
    db: AsyncSession,
    client_ip: str,
):
    await _check_global_webhook_rate()

    try:
        await _validate_stripe_ip(client_ip)
    except HTTPException:
        await _check_webhook_rate(client_ip)
        raise

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_request_body_size:
        raise HTTPException(status_code=413, detail="Request body too large")

    payload = bytearray()
    bytes_read = 0
    async for chunk in request.stream():
        payload.extend(chunk)
        bytes_read += len(chunk)
        if bytes_read > settings.max_request_body_size:
            raise HTTPException(status_code=413, detail="Request body too large")
    payload = bytes(payload)
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid payload") from e
    except stripe.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Invalid signature") from e

    event_id = event.get("id", "")
    event_type = event["type"]

    event_timestamp = event.get("created", 0)
    current_time = int(time.time())
    _skew_tolerance = int(os.getenv("STRIPE_WEBHOOK_CLOCK_SKEW_TOLERANCE", "600"))
    clock_skew = abs(event_timestamp - current_time)
    if clock_skew > 60:
        logger.warning(
            "Stripe webhook clock skew > 60s: event=%s timestamp=%s current=%s skew=%ds (tolerance=%ds)",
            event_id,
            event_timestamp,
            current_time,
            clock_skew,
            _skew_tolerance,
            extra={"event_id": event_id, "event_type": event_type, "clock_skew": clock_skew},
        )
    if clock_skew > _skew_tolerance:
        logger.error(
            "Stripe webhook replay attack detected: event=%s timestamp=%s current=%s skew=%ds exceeds tolerance=%ds",
            event_id,
            event_timestamp,
            current_time,
            clock_skew,
            _skew_tolerance,
            extra={"event_id": event_id, "event_type": event_type},
        )
        raise HTTPException(status_code=400, detail="Event timestamp outside allowed window")

    effective_event_id = event_id or hashlib.sha256(f"{event_type}:{event_timestamp}".encode()).hexdigest()

    _pg_dedup_stmt = (
        pg_insert(StripeWebhookEvent)
        .values(
            id=effective_event_id,
            event_type=event_type,
        )
        .on_conflict_do_nothing()
        .returning(StripeWebhookEvent.id)
    )
    _pg_result = await db.execute(_pg_dedup_stmt)
    _pg_inserted = _pg_result.scalar_one_or_none()
    if _pg_inserted is None:
        logger.info("Duplicate Stripe webhook event %s (PG dedup hit), skipping", effective_event_id)
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_stripe_dedup_pg_hit", {"event_type": event_type})
        except Exception as _e:
            logger.debug("Prometheus dedup PG hit increment failed: %s", _e)
        return {"received": True, "deduplicated": True}

    _redis_dedup_hit = False
    try:
        from app.ai.rate_limiter import _get_redis

        _redis_dedup = await _get_redis()
        if _redis_dedup:
            _dedup_key = f"stripe:dedup:{effective_event_id}"
            _dedup_acquired = await _redis_dedup.set(_dedup_key, "1", nx=True, ex=604800)
            if not _dedup_acquired:
                _redis_dedup_hit = True
                logger.info("Duplicate Stripe webhook event %s (Redis dedup hit), skipping", effective_event_id)
                try:
                    from app.monitoring.prometheus import increment_counter

                    increment_counter("workticket_stripe_dedup_redis_hit", {"event_type": event_type})
                except Exception as _e:
                    logger.debug("Prometheus dedup Redis hit increment failed: %s", _e)
                return {"received": True, "deduplicated": True}
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter("workticket_stripe_dedup_redis_miss", {"event_type": event_type})
            except Exception as _e:
                logger.debug("Prometheus dedup Redis miss increment failed: %s", _e)
    except Exception as _redis_err:
        logger.warning("Redis dedup check failed for event %s: %s", effective_event_id, _redis_err)

    if not _redis_dedup_hit:
        _lock_hash = int(hashlib.sha256(f"stripe:{effective_event_id}".encode()).hexdigest()[:15], 16)
        lock_result = await db.execute(
            sa_text("SELECT pg_try_advisory_xact_lock(:lock_key)").bindparams(
                bindparam("lock_key", type_=BigInteger),
            ),
            {"lock_key": _lock_hash},
        )
        if not lock_result.scalar():
            raise HTTPException(status_code=409, detail="Concurrent webhook processing in progress")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        metadata_company_id = session.get("metadata", {}).get("company_id")
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")
        plan = session.get("metadata", {}).get("plan", "pro")

        if not customer_id:
            logger.warning(
                "Stripe webhook missing customer_id - rejecting event",
                extra={"event_id": event_id, "event_type": event_type},
            )
            raise HTTPException(status_code=400, detail="Missing customer_id in Stripe event")

        if not subscription_id:
            logger.warning(
                "Stripe checkout missing subscription_id", extra={"event_id": event_id, "customer_id": customer_id}
            )
            raise HTTPException(status_code=400, detail="Missing subscription_id in Stripe session")

        stripe_circuit = await _get_stripe_circuit()
        if not await stripe_circuit.is_available():
            logger.error("Stripe circuit breaker OPEN — skipping API verification for subscription %s", subscription_id)
            raise HTTPException(status_code=502, detail="Stripe API temporarily unavailable, please try again later")
        try:
            stripe_subscription = await asyncio.to_thread(lambda: stripe.Subscription.retrieve(subscription_id))
            await stripe_circuit.record_success()
            customer = (
                stripe_subscription.customer
                if hasattr(stripe_subscription, "customer")
                else                 stripe_subscription.get("customer", "")  # type: ignore[attr-defined]
            )
            if customer != customer_id:
                logger.error(
                    "Stripe subscription/customer mismatch - potential fraud: sub=%s, sub_customer=%s, event_customer=%s",
                    subscription_id,
                    stripe_subscription.get("customer"),  # type: ignore[attr-defined]
                    customer_id,
                    extra={"event_id": event_id, "event_type": event_type},
                )
                raise HTTPException(status_code=400, detail="Subscription does not belong to the specified customer")
        except stripe.StripeError as e:
            await stripe_circuit.record_failure()
            logger.error("Failed to verify Stripe subscription %s: %s", subscription_id, e)
            raise HTTPException(status_code=502, detail="Failed to verify subscription with Stripe") from e

        event_timestamp = event.get("created", 0)
        if event_timestamp:
            event_dt = datetime.fromtimestamp(event_timestamp, tz=UTC)
            company_check = await db.execute(select(Company).where(Company.stripe_customer_id == customer_id))
            company_for_period = company_check.scalar_one_or_none()
            if company_for_period:
                account_for_period = await quota_engine.get_or_create_account(
                    db, company_for_period.id, company_for_period.subscription_plan
                )
                if (
                    account_for_period
                    and account_for_period.billing_period_start
                    and event_dt < account_for_period.billing_period_start
                ):
                    logger.warning(
                        "Stripe event %s from PRIOR billing period: event_ts=%s, billing_start=%s — rejecting",
                        event_id,
                        event_dt.isoformat(),
                        account_for_period.billing_period_start.isoformat(),
                    )
                    raise HTTPException(status_code=400, detail="Event from prior billing period")

        try:
            result = await db.execute(
                select(Company).where(Company.stripe_customer_id == customer_id).with_for_update(nowait=True)
            )
            company = result.scalar_one_or_none()
        except Exception as lock_err:
            if "could not obtain lock" in str(lock_err).lower() or "55P03" in str(lock_err):
                logger.warning(
                    "Stripe webhook lock contention for company %s (event=%s) — returning 409 for retry",
                    customer_id,
                    event_id,
                )
                raise HTTPException(
                    status_code=409, detail="Concurrent webhook processing in progress, please retry"
                ) from lock_err
            raise

        if not company:
            logger.warning(
                "Stripe webhook received for unknown customer_id: %s",
                customer_id,
                extra={"event_id": event_id, "event_type": event_type},
            )
            raise HTTPException(status_code=400, detail="Unknown customer_id in Stripe event")

        if metadata_company_id and str(company.id) != metadata_company_id:
            logger.error(
                "Stripe webhook metadata company_id mismatch - potential fraud: metadata=%s, customer_mapped=%s",
                metadata_company_id,
                str(company.id),
                extra={
                    "event_id": event_id,
                    "event_type": event_type,
                    "metadata_company_id": metadata_company_id,
                    "actual_company_id": str(company.id),
                },
            )
            raise HTTPException(
                status_code=400, detail="Stripe webhook metadata company_id mismatch - potential fraud attempt"
            )

        company.subscription_plan = plan
        company.stripe_customer_id = customer_id
        company.stripe_subscription_id = subscription_id
        await db.flush()
        account = await quota_engine.get_or_create_account(db, company.id, plan)
        tier = PLAN_TIERS.get(plan, PLAN_TIERS["pro"])
        account.plan = plan
        account.monthly_quota_acu = tier["quota_acu"]
        await db.flush()
        logger.info("Company %s upgraded to %s via Stripe (sub=%s)", company.id, plan, subscription_id)
        await set_cached_subscription(subscription_id, {"plan": plan, "status": "active", "customer_id": customer_id})
        await invalidate_subscription_cache(stripe_customer_id=customer_id)

    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer")
        subscription_id = subscription.get("id")
        metadata_company_id = subscription.get("metadata", {}).get("company_id")

        if not customer_id:
            logger.warning(
                "Stripe webhook missing customer_id - rejecting event",
                extra={"event_id": event_id, "event_type": event_type},
            )
            raise HTTPException(status_code=400, detail="Missing customer_id in Stripe event")

        if not subscription_id:
            logger.warning(
                "Stripe subscription.deleted missing subscription_id",
                extra={"event_id": event_id, "customer_id": customer_id},
            )
            raise HTTPException(status_code=400, detail="Missing subscription_id in Stripe event")

        try:
            result = await db.execute(
                select(Company).where(Company.stripe_customer_id == customer_id).with_for_update(nowait=True)
            )
            company = result.scalar_one_or_none()
        except Exception as lock_err:
            if "could not obtain lock" in str(lock_err).lower() or "55P03" in str(lock_err):
                raise HTTPException(
                    status_code=409, detail="Concurrent webhook processing in progress, please retry"
                ) from lock_err
            raise

        if not company:
            logger.warning(
                "Stripe webhook received for unknown customer_id: %s",
                customer_id,
                extra={"event_id": event_id, "event_type": event_type},
            )
            raise HTTPException(status_code=400, detail="Unknown customer_id in Stripe event")

        if metadata_company_id and str(company.id) != metadata_company_id:
            logger.error(
                "Stripe webhook metadata company_id mismatch - potential fraud: metadata=%s, customer_mapped=%s",
                metadata_company_id,
                str(company.id),
                extra={
                    "event_id": event_id,
                    "event_type": event_type,
                    "metadata_company_id": metadata_company_id,
                    "actual_company_id": str(company.id),
                },
            )
            raise HTTPException(
                status_code=400, detail="Stripe webhook metadata company_id mismatch - potential fraud attempt"
            )

        if company.stripe_subscription_id and company.stripe_subscription_id != subscription_id:
            logger.error(
                "Stripe subscription_id mismatch - potential fraud: event_sub=%s, stored_sub=%s, company=%s",
                subscription_id,
                company.stripe_subscription_id,
                company.id,
                extra={
                    "event_id": event_id,
                    "event_type": event_type,
                    "event_subscription_id": subscription_id,
                    "stored_subscription_id": company.stripe_subscription_id,
                },
            )
            raise HTTPException(
                status_code=400, detail="Stripe subscription deletion does not match stored subscription"
            )

        company.subscription_plan = "free"
        company.stripe_subscription_id = None
        await db.flush()
        account = await quota_engine.get_or_create_account(db, company.id, "free")
        tier = PLAN_TIERS["free"]
        account.plan = "free"
        account.monthly_quota_acu = tier["quota_acu"]
        await db.flush()
        logger.info("Company %s downgraded to free via Stripe (sub=%s)", company.id, subscription_id)
        await invalidate_subscription_cache(stripe_subscription_id=subscription_id, stripe_customer_id=customer_id)

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        subscription_id = invoice.get("subscription")

        if not customer_id:
            logger.warning("Invoice payment failed missing customer_id", extra={"event_id": event_id})
            raise HTTPException(status_code=400, detail="Missing customer_id in invoice event")

        try:
            result = await db.execute(
                select(Company).where(Company.stripe_customer_id == customer_id).with_for_update(nowait=True)
            )
            company = result.scalar_one_or_none()
        except Exception as lock_err:
            if "could not obtain lock" in str(lock_err).lower() or "55P03" in str(lock_err):
                raise HTTPException(
                    status_code=409, detail="Concurrent webhook processing in progress, please retry"
                ) from lock_err
            raise

        if not company:
            logger.warning(
                "Invoice payment failed for unknown customer_id: %s", customer_id, extra={"event_id": event_id}
            )
            raise HTTPException(status_code=400, detail="Unknown customer_id in invoice event")

        logger.warning(
            "Invoice payment failed for company %s: subscription=%s — disabling AI access",
            company.id,
            subscription_id,
            extra={"event_id": event_id, "company_id": str(company.id)},
        )
        account = await quota_engine.get_or_create_account(db, company.id, company.subscription_plan)
        account.ai_disabled = True
        account.ai_disabled_reason = "payment_failed"
        await db.flush()
        await invalidate_subscription_cache(stripe_subscription_id=subscription_id, stripe_customer_id=customer_id)

    elif event_type == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        subscription_id = invoice.get("subscription")

        if not customer_id:
            logger.warning("Invoice payment succeeded missing customer_id", extra={"event_id": event_id})
            raise HTTPException(status_code=400, detail="Missing customer_id in invoice event")

        try:
            result = await db.execute(
                select(Company).where(Company.stripe_customer_id == customer_id).with_for_update(nowait=True)
            )
            company = result.scalar_one_or_none()
        except Exception as lock_err:
            if "could not obtain lock" in str(lock_err).lower() or "55P03" in str(lock_err):
                raise HTTPException(
                    status_code=409, detail="Concurrent webhook processing in progress, please retry"
                ) from lock_err
            raise

        if not company:
            logger.warning(
                "Invoice payment succeeded for unknown customer_id: %s", customer_id, extra={"event_id": event_id}
            )
            raise HTTPException(status_code=400, detail="Unknown customer_id in invoice event")

        account = await quota_engine.get_or_create_account(db, company.id, company.subscription_plan)
        if account.ai_disabled and account.ai_disabled_reason == "payment_failed":
            account.ai_disabled = False
            account.ai_disabled_reason = None
            logger.info(
                "Re-enabled AI for company %s after successful payment recovery (sub=%s)",
                company.id,
                subscription_id,
                extra={"event_id": event_id, "company_id": str(company.id)},
            )
            await db.flush()
        await invalidate_subscription_cache(stripe_subscription_id=subscription_id, stripe_customer_id=customer_id)

    else:
        logger.info("Unhandled Stripe webhook event type: %s", event_type)

    return {"received": True}

@router.get("/invoices/{invoice_id}/export")
async def export_invoice(
    invoice_id: UUID,
    format: str = Query("json", pattern="^(json|csv|pdf)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        from app.ai.rate_limiter import _get_redis

        _rl_redis = await _get_redis()
        if _rl_redis:
            _rl_key = f"invoice_export_rate:{current_user.id}"
            _rl_count = await _rl_redis.incr(_rl_key)
            if _rl_count == 1:
                await _rl_redis.expire(_rl_key, 60)
            if _rl_count > 10:
                raise HTTPException(
                    status_code=429, detail="Too many invoice exports. Please wait before trying again."
                )
    except HTTPException:
        raise
    except Exception as _e:
        logger.debug("Invoice export rate limit check failed: %s", _e)

    result = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == current_user.company_id)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if format == "json":
        return {
            "id": str(invoice.id),
            "company_id": str(invoice.company_id),
            "job_id": str(invoice.job_id),
            "customer_id": str(invoice.customer_id),
            "line_items": invoice.line_items,
            "subtotal": float(invoice.subtotal),
            "tax": float(invoice.tax),
            "total": float(invoice.total),
            "status": invoice.status,
            "created_at": invoice.created_at.isoformat() if invoice.created_at else None,
            "updated_at": invoice.updated_at.isoformat() if invoice.updated_at else None,
        }
    elif format == "csv":
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_ALL)
        writer.writerow(
            [
                "id",
                "company_id",
                "job_id",
                "customer_id",
                "line_items",
                "subtotal",
                "tax",
                "total",
                "status",
                "created_at",
                "updated_at",
            ]
        )
        line_items_str = (
            json.dumps(invoice.line_items) if isinstance(invoice.line_items, (dict, list)) else str(invoice.line_items)
        )
        writer.writerow(
            [
                _sanitize_csv(str(invoice.id)),
                _sanitize_csv(str(invoice.company_id)),
                _sanitize_csv(str(invoice.job_id)),
                _sanitize_csv(str(invoice.customer_id)),
                _sanitize_csv(line_items_str),
                float(invoice.subtotal),
                float(invoice.tax),
                float(invoice.total),
                _sanitize_csv(invoice.status or ""),
                _sanitize_csv(invoice.created_at.isoformat() if invoice.created_at else ""),
                _sanitize_csv(invoice.updated_at.isoformat() if invoice.updated_at else ""),
            ]
        )
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=invoice_{invoice.id}.csv",
                "Content-Type": "text/csv; charset=utf-8",
                "X-Content-Type-Options": "nosniff",
            },
        )
    elif format == "pdf":
        _max_pdf_line_items = settings.max_pdf_line_items
        try:
            import io as pdf_io

            from reportlab.lib import colors
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

            def _escape_pdf(text: str) -> str:
                if not text:
                    return ""
                return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            buf = pdf_io.BytesIO()
            doc = SimpleDocTemplate(buf, pagesize=letter)
            styles = getSampleStyleSheet()
            story = []

            story.append(Paragraph(f"Invoice #{_escape_pdf(str(invoice.id))}", styles["Title"]))
            story.append(Spacer(1, 12))
            story.append(Paragraph(f"Status: {_escape_pdf(invoice.status or '')}", styles["Normal"]))
            story.append(Paragraph(f"Subtotal: ${float(invoice.subtotal):.2f}", styles["Normal"]))
            story.append(Paragraph(f"Tax: ${float(invoice.tax):.2f}", styles["Normal"]))
            story.append(Paragraph(f"Total: ${float(invoice.total):.2f}", styles["Normal"]))
            story.append(Spacer(1, 12))

            if invoice.line_items:
                items: dict[str, Any] = invoice.line_items if isinstance(invoice.line_items, dict) else {}
                _item_count = len(items)
                if _item_count > _max_pdf_line_items:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Invoice has too many line items ({_item_count}) for PDF export (max {_max_pdf_line_items})",
                    )
                data = [["Item", "Amount"]]
                for k, v in items.items():
                    amt = v.get("amount", 0) if isinstance(v, dict) else v
                    data.append([_escape_pdf(str(k)), f"${float(amt):.2f}"])
                if len(data) > 1:
                    table = Table(data)
                    table.setStyle(
                        TableStyle(
                            [
                                ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                            ]
                        )
                    )
                    story.append(table)

            doc.build(story)
            buf.seek(0)
            return StreamingResponse(
                iter([buf.getvalue()]),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename=invoice_{invoice.id}.pdf",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except HTTPException:
            raise
        except ImportError as e:
            raise HTTPException(status_code=501, detail="PDF export requires reportlab: pip install reportlab") from e
        except Exception as e:
            logger.error("PDF generation failed: %s", e)
            raise HTTPException(status_code=500, detail="PDF generation failed") from e
    else:
        raise HTTPException(status_code=400, detail="Invalid format specified")

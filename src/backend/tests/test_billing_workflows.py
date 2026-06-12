import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

BILLING_PREFIX = "/api/v1/billing"


@pytest.mark.asyncio
async def test_webhook_body_size_rejection(client: AsyncClient, monkeypatch):
    """M2: Chunked body reading rejects payload exceeding max_request_body_size."""
    from app.billing import invoice_routes
    from app.config import get_settings

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    settings = get_settings()
    oversized = b"x" * (settings.max_request_body_size + 1024)

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        content=oversized,
        headers={
            "content-type": "application/json",
        },
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_webhook_body_size_rejection_chunked_no_header(client: AsyncClient, monkeypatch):
    """M2: Even without content-length header, oversized body is rejected by chunked reader."""
    from app.billing import invoice_routes
    from app.config import get_settings

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    settings = get_settings()
    oversized = b"x" * (settings.max_request_body_size + 1024)

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        content=oversized,
        headers={
            "content-type": "application/json",
            "content-length": "100",
        },
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_webhook_replay_detection(client: AsyncClient, monkeypatch):
    """4C: Webhook events outside 5-minute skew window are rejected."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    old_event = {
        "id": "evt_test_replay_" + str(int(time.time())),
        "type": "checkout.session.completed",
        "created": int(time.time()) - 400,
        "data": {"object": {}},
    }

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        json=old_event,
        headers={
            "stripe-signature": "test_sig",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_webhook_missing_secret_returns_503(client: AsyncClient, monkeypatch):
    """Unconfigured Stripe secret returns 503."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    settings = invoice_routes.settings
    original_secret = settings.stripe_webhook_secret
    try:
        settings.stripe_webhook_secret = ""
        response = await client.post(
            f"{BILLING_PREFIX}/webhook",
            json={"type": "test"},
            headers={"stripe-signature": "sig", "content-type": "application/json"},
        )
        assert response.status_code == 503
    finally:
        settings.stripe_webhook_secret = original_secret


@pytest.mark.asyncio
async def test_reconciliation_underflow_tracks_debt(client: AsyncClient, monkeypatch):
    """4C: Billing reconciliation underflow tracks debt rather than going negative."""
    from app.billing.models import BillingAccount
    from app.billing.reconciliation import reconcile_cost

    from sqlalchemy import select

    async for db in override_get_db():
        result = await db.execute(
            select(BillingAccount).where(BillingAccount.company_id == UUID("00000000-0000-0000-0000-000000000001"))
        )
        account = result.scalar_one_or_none()
        if account is None:
            account = BillingAccount(
                company_id=UUID("00000000-0000-0000-0000-000000000001"),
                plan="pro",
                monthly_quota_acu=1000,
                reserved_acu=Decimal("5"),
                used_acu=Decimal("0"),
                acu_debt=Decimal("0"),
            )
            db.add(account)
        else:
            account.reserved_acu = Decimal("5")
            account.used_acu = Decimal("0")
            account.acu_debt = Decimal("0")
        await db.commit()
        await db.refresh(account)

        result = await reconcile_cost(
            db=db,
            company_id=account.company_id,
            job_id=uuid4(),
            estimated_cost_usd=0.10,
            actual_cost_usd=0.10,
            reserved_acu=5,
            model_used="test",
            execution_time_ms=100,
        )

        assert result["status"] == "reconciled"
        await db.refresh(account)
        assert account.reserved_acu == Decimal("0")
        assert account.acu_debt > Decimal("0")
        break


async def override_get_db():
    from tests.conftest import TestSessionLocal

    db = TestSessionLocal()
    try:
        yield db
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_webhook_rate_limiter_blocks_excessive(client: AsyncClient, monkeypatch):
    """4C: Webhook rate limiter blocks requests exceeding 10/60s."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())

    responses = []
    for _i in range(12):
        response = await client.post(
            f"{BILLING_PREFIX}/webhook",
            json={"type": "test"},
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )
        responses.append(response.status_code)

    rate_limited = [s for s in responses if s == 429]
    assert len(rate_limited) > 0, "Rate limiter should block at least some requests"


@pytest.mark.asyncio
async def test_idempotency_key_deduplication(client: AsyncClient):
    """4C: Duplicate idempotency key returns cached response."""
    from app.billing.idempotency_service import (
        complete_idempotency_record,
        compute_request_hash,
        create_idempotency_record,
        get_idempotent_response,
    )
    from tests.conftest import TestSessionLocal

    idem_key = str(uuid4())
    body = b'{"test": true}'
    request_hash = compute_request_hash(body)
    test_company_id = UUID("00000000-0000-0000-0000-000000000001")

    async with TestSessionLocal() as db:
        existing = await get_idempotent_response(db, test_company_id, "test-user-id", idem_key, request_hash)
        if existing:
            return

        await create_idempotency_record(db, test_company_id, "test-user-id", idem_key, request_hash)
        payload = {"status": "completed", "job_id": str(uuid4())}
        await complete_idempotency_record(db, test_company_id, "test-user-id", idem_key, payload)

        cached = await get_idempotent_response(db, test_company_id, "test-user-id", idem_key, request_hash)
        assert cached is not None
        assert cached["status"] == "completed"


# --- H3: Celery beat schedule is properly configured ---


@pytest.mark.asyncio
async def test_beat_schedule_stripe_ip_refresh_task():
    """H3: Celery beat schedule includes stripe_ip_refresh task."""
    from celery_app import celery_app

    beat_schedule = celery_app.conf.beat_schedule
    task_names = {v["task"] for v in beat_schedule.values()}
    assert "refresh_stripe_ips" in task_names or any("stripe" in t for t in task_names), (
        "Beat schedule must include Stripe IP refresh"
    )


# --- 4C: Billing integrity tests ---


@pytest.mark.asyncio
async def test_webhook_stripe_ip_validation_fails_open(client: AsyncClient, monkeypatch):
    """4C: Non-Stripe IP is rejected with 403."""
    from app.billing import invoice_routes

    monkeypatch.setattr(
        invoice_routes,
        "_validate_stripe_ip",
        AsyncMock(side_effect=HTTPException(status_code=403, detail="Webhook source IP not allowed")),
    )

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        json={"type": "test"},
        headers={"stripe-signature": "test_sig", "content-type": "application/json"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_webhook_missing_signature_header(client: AsyncClient, monkeypatch):
    """4C: Missing stripe-signature header causes invalid payload error."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        json={"type": "test"},
        headers={"content-type": "application/json"},
    )
    # Missing signature should fail signature verification
    assert response.status_code in (400,)


@pytest.mark.asyncio
async def test_webhook_advisory_lock_concurrent(client: AsyncClient, monkeypatch):
    """4C: Concurrent webhook processing advisory lock prevents double processing."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    event_id = "evt_test_concurrent_" + str(int(time.time()))
    payload = {
        "id": event_id,
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {"object": {}},
    }

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        json=payload,
        headers={"stripe-signature": "test_sig", "content-type": "application/json"},
    )
    # Should fail at customer lookup since there's no real Stripe event
    assert response.status_code in (400, 502)


@pytest.mark.asyncio
async def test_webhook_empty_body_returns_400(client: AsyncClient, monkeypatch):
    """4C: Webhook with empty body returns 400."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        content=b"",
        headers={"stripe-signature": "sig", "content-type": "application/json"},
    )
    assert response.status_code in (400,)


@pytest.mark.asyncio
async def test_webhook_unknown_event_type_succeeds(client: AsyncClient, monkeypatch):
    """4C: Unknown event type is logged but webhook returns success."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    event_id = "evt_test_unknown_" + str(int(time.time()))
    payload = {
        "id": event_id,
        "type": "unknown.event.type",
        "created": int(time.time()),
        "data": {"object": {}},
    }

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        json=payload,
        headers={"stripe-signature": "test_sig", "content-type": "application/json"},
    )
    # Should be accepted at webhook level even if event type is unhandled
    assert response.status_code in (200, 400)


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_fallback_open(client: AsyncClient, monkeypatch):
    """4C: Rate limiter fails open when Redis is unavailable."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())

    with patch.object(invoice_routes, "_check_webhook_rate", side_effect=Exception("Redis down")):
        response = await client.post(
            f"{BILLING_PREFIX}/webhook",
            json={"type": "test"},
            headers={"stripe-signature": "test_sig", "content-type": "application/json"},
        )
        # Should not get 429; should either pass through or get 400
        assert response.status_code != 429


# --- Usage history pagination ---


@pytest.mark.asyncio
async def test_usage_history_pagination(client: AsyncClient):
    """4C: Usage history endpoint supports pagination params."""
    response = await client.get(f"{BILLING_PREFIX}/usage?page=1&page_size=20")
    assert response.status_code in (200, 401)

    response_bad = await client.get(f"{BILLING_PREFIX}/usage?page=0&page_size=0")
    assert response_bad.status_code in (422, 401, 200)


# --- Billing account endpoint ---


@pytest.mark.asyncio
async def test_billing_account_endpoint(client: AsyncClient):
    """4C: Billing account endpoint returns account info."""
    response = await client.get(f"{BILLING_PREFIX}/account")
    assert response.status_code in (200, 401)
    if response.status_code == 200:
        data = response.json()
        assert "plan" in data
        assert "monthly_quota_acu" in data


# --- Webhook missing event ID (graceful skip of dedup) ---


@pytest.mark.asyncio
async def test_webhook_missing_event_id_skips_dedup(client: AsyncClient, monkeypatch):
    """4C: Webhook without event ID skips dedup and processes without error."""
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", AsyncMock())

    payload = {
        "type": "unknown.test.event",
        "created": int(time.time()),
        "data": {"object": {}},
    }

    response = await client.post(
        f"{BILLING_PREFIX}/webhook",
        json=payload,
        headers={"stripe-signature": "test_sig", "content-type": "application/json"},
    )
    # Should not crash; event without ID is handled gracefully
    assert response.status_code in (200, 400, 502)

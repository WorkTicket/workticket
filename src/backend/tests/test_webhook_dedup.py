import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select

from app.jobs.models import Company

_NOW = int(time.time())


def _make_mock_request():
    mock = MagicMock()
    mock.headers.get = lambda k, d=None: {"content-length": "500", "stripe-signature": "test_sig"}.get(k, d)

    async def _stream():
        yield b'{"test": true}'

    mock.stream = _stream
    return mock


async def _seed_company(db):
    company = (await db.execute(
        select(Company).where(Company.id == UUID("00000000-0000-0000-0000-000000000001"))
    )).scalar_one_or_none()
    if company:
        company.stripe_customer_id = "cus_test"
        await db.flush()
    return company


@pytest.fixture
def mock_redis():
    mock = AsyncMock()
    mock.set.return_value = True
    return mock


@pytest.fixture(autouse=True)
def _patch_deps():
    os.environ["STRIPE_WEBHOOK_CLOCK_SKEW_TOLERANCE"] = "999999999"
    with (
        patch("app.billing.invoice_routes._check_global_webhook_rate", AsyncMock()),
        patch("app.billing.invoice_routes._validate_stripe_ip", AsyncMock()),
        patch("app.billing.invoice_routes._check_webhook_rate", AsyncMock()),
        patch("app.billing.invoice_routes.stripe.Subscription.retrieve") as mock_sub_retrieve,
        patch("app.billing.invoice_routes.asyncio.to_thread", lambda fn, **kw: fn()),
    ):
        mock_sub = MagicMock()
        mock_sub.customer = "cus_test"
        mock_sub_retrieve.return_value = mock_sub
        yield


@pytest.mark.asyncio
async def test_duplicate_checkout_session_completed_dedup(client, mock_redis):
    """Duplicate checkout.session.completed events are deduplicated."""
    from sqlalchemy import delete

    from app.billing.invoice_routes import _process_webhook
    from app.billing.models import StripeWebhookEvent
    from app.database import get_db

    payload = {
        "id": "evt_test_checkout_1",
        "type": "checkout.session.completed",
        "created": _NOW,
        "data": {
            "object": {
                "id": "cs_test_1",
                "customer": "cus_test",
                "subscription": "sub_test",
                "metadata": {"company_id": "00000000-0000-0000-0000-000000000001", "plan": "pro"},
            }
        },
    }

    mock_request = _make_mock_request()

    with (
        patch("app.billing.invoice_routes.stripe.Webhook.construct_event", return_value=payload),
        patch("app.ai.rate_limiter._get_redis", return_value=mock_redis),
    ):
        async for db in get_db():
            await _seed_company(db)
            await db.execute(delete(StripeWebhookEvent))
            await db.commit()

            result1 = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result1.get("deduplicated") is not True, "First call should not be dedup"

            result2 = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result2.get("deduplicated") is True, "Duplicate should be deduplicated"


@pytest.mark.asyncio
async def test_duplicate_invoice_payment_failed_dedup(client, mock_redis):
    """Duplicate invoice.payment_failed events are deduplicated."""
    from sqlalchemy import delete

    from app.billing.invoice_routes import _process_webhook
    from app.billing.models import StripeWebhookEvent
    from app.database import get_db

    payload = {
        "id": "evt_test_invoice_fail_1",
        "type": "invoice.payment_failed",
        "created": _NOW,
        "data": {
            "object": {
                "id": "in_test_1",
                "customer": "cus_test",
                "subscription": "sub_test",
                "metadata": {"company_id": "00000000-0000-0000-0000-000000000001"},
            }
        },
    }

    mock_request = _make_mock_request()

    with (
        patch("app.billing.invoice_routes.stripe.Webhook.construct_event", return_value=payload),
        patch("app.ai.rate_limiter._get_redis", return_value=mock_redis),
    ):
        async for db in get_db():
            await _seed_company(db)
            await db.execute(delete(StripeWebhookEvent))
            await db.commit()

            result1 = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result1.get("deduplicated") is not True

            result2 = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result2.get("deduplicated") is True


@pytest.mark.asyncio
async def test_duplicate_customer_subscription_deleted_dedup(client, mock_redis):
    """Duplicate customer.subscription.deleted events are deduplicated."""
    from sqlalchemy import delete

    from app.billing.invoice_routes import _process_webhook
    from app.billing.models import StripeWebhookEvent
    from app.database import get_db

    payload = {
        "id": "evt_test_sub_deleted_1",
        "type": "customer.subscription.deleted",
        "created": _NOW,
        "data": {
            "object": {
                "id": "sub_test_del_1",
                "customer": "cus_test",
                "metadata": {"company_id": "00000000-0000-0000-0000-000000000001"},
            }
        },
    }

    mock_request = _make_mock_request()

    with (
        patch("app.billing.invoice_routes.stripe.Webhook.construct_event", return_value=payload),
        patch("app.ai.rate_limiter._get_redis", return_value=mock_redis),
    ):
        async for db in get_db():
            await _seed_company(db)
            await db.execute(delete(StripeWebhookEvent))
            await db.commit()

            result1 = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result1.get("deduplicated") is not True

            result2 = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result2.get("deduplicated") is True


@pytest.mark.asyncio
async def test_different_event_types_same_id_not_dedup(client, mock_redis):
    """Different event types with same ID should NOT be deduplicated."""
    from sqlalchemy import delete

    from app.billing.invoice_routes import _process_webhook
    from app.billing.models import StripeWebhookEvent
    from app.database import get_db

    shared_id = "evt_test_shared_id_1"

    payload1 = {
        "id": shared_id,
        "type": "checkout.session.completed",
        "created": _NOW,
        "data": {
            "object": {
                "id": "cs_test_shared",
                "customer": "cus_test",
                "subscription": "sub_test",
                "metadata": {"company_id": "00000000-0000-0000-0000-000000000001", "plan": "pro"},
            }
        },
    }

    payload2 = {
        "id": shared_id,
        "type": "invoice.payment_succeeded",
        "created": _NOW,
        "data": {
            "object": {
                "id": "in_test_shared",
                "customer": "cus_test",
                "subscription": "sub_test",
                "metadata": {"company_id": "00000000-0000-0000-0000-000000000001"},
            }
        },
    }

    mock_request = _make_mock_request()

    with patch("app.ai.rate_limiter._get_redis", return_value=mock_redis):
        async for db in get_db():
            await _seed_company(db)
            await db.execute(delete(StripeWebhookEvent))
            await db.commit()

            with patch("app.billing.invoice_routes.stripe.Webhook.construct_event", return_value=payload1):
                result1 = await _process_webhook(mock_request, db, "13.248.128.1")
                assert result1.get("deduplicated") is not True, "First event type should not be dedup"

            with patch("app.billing.invoice_routes.stripe.Webhook.construct_event", return_value=payload2):
                result2 = await _process_webhook(mock_request, db, "13.248.128.1")
                assert result2.get("deduplicated") is not True, "Different event type should not be dedup"


@pytest.mark.asyncio
async def test_redis_dedup_hit_returns_early(client, mock_redis):
    """When Redis already has the dedup key, it returns early."""
    from sqlalchemy import delete

    from app.billing.invoice_routes import _process_webhook
    from app.billing.models import StripeWebhookEvent
    from app.database import get_db

    payload = {
        "id": "evt_test_redis_dedup_1",
        "type": "checkout.session.completed",
        "created": _NOW,
        "data": {
            "object": {
                "id": "cs_test_redis",
                "customer": "cus_test",
                "subscription": "sub_test",
                "metadata": {"company_id": "00000000-0000-0000-0000-000000000001", "plan": "pro"},
            }
        },
    }

    mock_request = _make_mock_request()

    mock_redis_with_hit = AsyncMock()
    mock_redis_with_hit.set.return_value = False

    with (
        patch("app.billing.invoice_routes.stripe.Webhook.construct_event", return_value=payload),
        patch("app.ai.rate_limiter._get_redis", return_value=mock_redis_with_hit),
    ):
        async for db in get_db():
            await db.execute(delete(StripeWebhookEvent))
            await db.commit()

            result = await _process_webhook(mock_request, db, "13.248.128.1")
            assert result.get("deduplicated") is True, "Redis dedup hit should return early"

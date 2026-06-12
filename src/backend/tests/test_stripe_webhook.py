import time
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.main import app


@pytest.fixture
def stripe_event_payload():
    return {
        "id": "evt_test_" + str(int(time.time())),
        "type": "checkout.session.completed",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": "cs_test_" + str(int(time.time())),
                "customer": "cus_test123",
                "subscription": "sub_test123",
                "metadata": {
                    "company_id": "00000000-0000-0000-0000-000000000001",
                    "plan": "pro",
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_stripe_ip_validation():
    from app.billing.stripe_ips import refresh_stripe_ips, validate_stripe_ip

    await refresh_stripe_ips(force=True)

    result = await validate_stripe_ip("13.248.128.1")
    assert result is True, "Known Stripe IP should be valid"

    result = await validate_stripe_ip("192.0.2.1")
    assert result is False, "Non-Stripe IP should be invalid"


@pytest.mark.asyncio
async def test_stripe_webhook_ip_check_disabled(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_IP_CHECK_DISABLED", "1")
    from app.billing.stripe_ips import refresh_stripe_ips, validate_stripe_ip

    await refresh_stripe_ips(force=True)

    result = await validate_stripe_ip("192.0.2.1")
    assert result is True, "Should pass when check disabled"


@pytest.mark.asyncio
async def test_stripe_webhook_replay_detection(stripe_event_payload):
    old_ts = int(time.time()) - 600
    stripe_event_payload["created"] = old_ts

    from app.billing import invoice_routes

    invoice_routes._validate_stripe_ip = AsyncMock()

    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/billing/webhook",
            json=stripe_event_payload,
            headers={
                "stripe-signature": "test_sig",
                "content-type": "application/json",
            },
        )
        assert response.status_code in (400, 503), "Replay should be rejected"

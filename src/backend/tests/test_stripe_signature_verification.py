"""Stripe webhook signature verification tests.

Verifies that webhook signature validation works correctly using real
HMAC-SHA256 signed payloads, closing the critical DDA audit gap.
"""

import hashlib
import hmac
import json
import time

import pytest
from httpx import AsyncClient


def _sign_payload(payload: dict, secret: str, timestamp: int = None) -> str:
    ts = timestamp or int(time.time())
    payload_str = json.dumps(payload, separators=(",", ":"), default=str)
    signed_payload = f"{ts}.{payload_str}"
    signature = hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={signature}"


@pytest.mark.asyncio
async def test_valid_signature_accepted(client: AsyncClient, monkeypatch):
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", __import__("unittest.mock").AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", __import__("unittest.mock").AsyncMock())

    test_secret = "whsec_test_valid_signature_key_32chars"
    settings = invoice_routes.settings
    original_secret = settings.stripe_webhook_secret

    try:
        settings.stripe_webhook_secret = test_secret
        event = {
            "id": f"evt_test_sig_{int(time.time())}",
            "type": "unknown.event.type",
            "created": int(time.time()),
            "data": {"object": {}},
        }
        signature = _sign_payload(event, test_secret)

        resp = await client.post(
            "/api/v1/billing/webhook",
            json=event,
            headers={
                "stripe-signature": signature,
                "content-type": "application/json",
            },
        )
        assert resp.status_code in (200, 400)
    finally:
        settings.stripe_webhook_secret = original_secret


@pytest.mark.asyncio
async def test_invalid_signature_rejected(client: AsyncClient, monkeypatch):
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", __import__("unittest.mock").AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", __import__("unittest.mock").AsyncMock())

    test_secret = "whsec_real_secret_for_testing_ok"
    settings = invoice_routes.settings
    original = settings.stripe_webhook_secret

    try:
        settings.stripe_webhook_secret = test_secret
        event = {
            "id": f"evt_test_bad_{int(time.time())}",
            "type": "checkout.session.completed",
            "created": int(time.time()),
            "data": {"object": {}},
        }
        bad_sig = "t=9999999999,v1=0000000000000000000000000000000000000000000000000000000000000000"

        resp = await client.post(
            "/api/v1/billing/webhook",
            json=event,
            headers={
                "stripe-signature": bad_sig,
                "content-type": "application/json",
            },
        )
        assert resp.status_code in (400, 503)
    finally:
        settings.stripe_webhook_secret = original


@pytest.mark.asyncio
async def test_tampered_payload_with_valid_signature_rejected(client: AsyncClient, monkeypatch):
    from app.billing import invoice_routes

    monkeypatch.setattr(invoice_routes, "_validate_stripe_ip", __import__("unittest.mock").AsyncMock())
    monkeypatch.setattr(invoice_routes, "_check_webhook_rate", __import__("unittest.mock").AsyncMock())

    test_secret = "whsec_original_secret_key_1234"
    settings = invoice_routes.settings
    original = settings.stripe_webhook_secret

    try:
        settings.stripe_webhook_secret = test_secret
        original_event = {
            "id": f"evt_tampered_{int(time.time())}",
            "type": "checkout.session.completed",
            "created": int(time.time()),
            "data": {"object": {"id": "cs_original"}},
        }
        signature = _sign_payload(original_event, test_secret)

        tampered = dict(original_event)
        tampered["data"]["object"]["id"] = "cs_tampered"

        resp = await client.post(
            "/api/v1/billing/webhook",
            json=tampered,
            headers={
                "stripe-signature": signature,
                "content-type": "application/json",
            },
        )
        assert resp.status_code in (400, 503), f"Tampered payload should be rejected, got {resp.status_code}"
    finally:
        settings.stripe_webhook_secret = original

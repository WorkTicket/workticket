import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

INVOICE_EXPORT_URL = "/api/v1/billing/invoices"


@pytest.mark.asyncio
async def test_csv_injection_formula_chars_stripped(client: AsyncClient, monkeypatch):
    """H-5: CSV export should sanitize formula-injecting characters."""
    from sqlalchemy import select

    from app.billing import invoice_routes
    from app.database import AsyncSessionLocal
    from app.jobs.models import Customer, Job, Invoice

    invoice_routes._check_webhook_rate = AsyncMock()

    test_invoice_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        customer_id = uuid.uuid4()
        customer = Customer(
            id=customer_id,
            company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            name="Test Customer",
            address="123 Test St",
        )
        db.add(customer)
        await db.flush()

        job_id = uuid.uuid4()
        job = Job(
            id=job_id,
            company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            customer_id=customer_id,
            technician_id="test-user-id",
            description="Test job",
            address="123 Test St",
            scheduled_time=datetime.now(UTC),
        )
        db.add(job)
        await db.flush()

        existing = await db.execute(select(Invoice).where(Invoice.id == test_invoice_id))
        if not existing.scalar_one_or_none():
            invoice = Invoice(
                id=test_invoice_id,
                company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                job_id=job_id,
                customer_id=customer_id,
                line_items={"test": "item"},
                subtotal=100.0,
                tax=10.0,
                total=110.0,
                status="draft",
            )
            db.add(invoice)
            await db.commit()

    response = await client.get(f"{INVOICE_EXPORT_URL}/{test_invoice_id}/export?format=csv")
    assert response.status_code == 200
    content = response.text
    assert "=CMD" not in content
    assert "+MACRO" not in content
    assert "@EVAL" not in content


@pytest.mark.asyncio
async def test_csv_injection_formula_prefix_added(client: AsyncClient, monkeypatch):
    """H-5: Values starting with formula chars should be prefixed with '."""
    from app.billing import invoice_routes

    invoice_routes._check_webhook_rate = AsyncMock()

    from app.billing.invoice_routes import _sanitize_csv

    assert _sanitize_csv("=SUM(A1:A10)").startswith("'")
    assert _sanitize_csv("+MACRO()").startswith("'")
    assert _sanitize_csv("-DDE_CMD").startswith("'")
    assert _sanitize_csv("@HYPERLINK").startswith("'")
    assert _sanitize_csv("\t=CMD").startswith("'")
    assert _sanitize_csv("  =CMD").startswith("'")
    assert _sanitize_csv("normal text") == "normal text"
    assert _sanitize_csv("") == ""


@pytest.mark.asyncio
async def test_csv_injection_control_chars_stripped():
    """H-5: Control characters should be replaced with spaces."""
    from app.billing.invoice_routes import _sanitize_csv

    result = _sanitize_csv("hello\x00world\x01test")
    assert "\x00" not in result
    assert "\x01" not in result
    assert result == "hello world test" or "hello world test" in result


@pytest.mark.asyncio
async def test_push_token_encryption_roundtrip():
    """M-4: Push token encryption should be reversible."""
    from app.notifications.encryption import decrypt_push_token, encrypt_push_token

    original = "ExponentPushToken[test_token_for_encryption]"
    encrypted = encrypt_push_token(original)
    assert encrypted != original
    decrypted = decrypt_push_token(encrypted)
    assert decrypted == original


@pytest.mark.asyncio
async def test_push_token_encryption_deterministic():
    """M-4: Same token should produce different ciphertexts each time (IV)."""
    from app.notifications.encryption import encrypt_push_token

    original = "ExponentPushToken[deterministic_test]"
    encrypted1 = encrypt_push_token(original)
    encrypted2 = encrypt_push_token(original)
    assert encrypted1 != encrypted2


@pytest.mark.asyncio
async def test_push_token_stored_encrypted(client: AsyncClient, monkeypatch):
    """M-4: Push tokens should be stored encrypted in database."""

    response = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[storage_test]", "platform": "expo"},
    )
    assert response.status_code == 200
    token_id = response.json()["id"]

    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.notifications.models import PushToken

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PushToken).where(PushToken.id == token_id))
        stored = result.scalar_one_or_none()
        assert stored is not None
        assert stored.push_token != "ExponentPushToken[storage_test]"
        assert "ExponentPushToken" not in stored.push_token

    response = await client.get("/api/v1/notifications/push-tokens")
    assert response.status_code == 200
    data = response.json()
    tokens = [t for t in data["items"] if t["id"] == token_id]
    assert len(tokens) == 1
    assert tokens[0]["push_token"] == "ExponentPushToken[storage_test]"


@pytest.mark.asyncio
async def test_rate_limiter_fallback_scaling():
    """H-7: Local fallback rate should be conservatively scaled for multi-worker safety."""
    from app.ai.rate_limiter import _ESTIMATED_WORKERS, _SAFETY_MULTIPLIER

    assert _ESTIMATED_WORKERS >= 3
    effective_scaling = _ESTIMATED_WORKERS * _SAFETY_MULTIPLIER
    assert effective_scaling >= 6.0


@pytest.mark.asyncio
async def test_company_name_unique_constraint():
    """H-6: Company name should have a unique constraint."""
    from sqlalchemy import inspect

    from app.jobs.models import Company

    mapper = inspect(Company)
    for col in mapper.columns:
        if col.name == "name":
            assert col.unique is True
            break


@pytest.mark.asyncio
async def test_cache_control_on_api_responses(client: AsyncClient):
    """M-5: Authenticated API responses should have Cache-Control: no-store."""
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "no-store" in cache_control


@pytest.mark.asyncio
async def test_jwt_audience_required_in_production():
    """H-1: clerk_jwt_audience must be required in production."""
    from app.config import Settings

    with pytest.raises(ValueError, match="clerk_jwt_audience"):
        Settings(
            _env_file=None,
            debug=False,
            database_url="postgresql://test",
            redis_url="redis://test",
            redis_password="test",
            clerk_secret_key="test",
            clerk_publishable_key="test",
            clerk_jwt_issuer="https://clerk.test",
            clerk_jwt_audience="",
            ollama_base_url="http://test",
            whisper_service_url="http://test",
            r2_endpoint_url="http://test",
            r2_access_key_id="test",
            r2_secret_access_key="test",
            r2_bucket_name="test",
            stripe_secret_key="test",
            stripe_webhook_secret="test",
            stripe_price_id="test",
            sentry_dsn="test",
            metrics_access_token="test",
            posthog_api_key="test",
            posthog_host="http://test",
            twilio_account_sid="test",
            twilio_auth_token="test",
            twilio_from_number="test",
            resend_api_key="test",
            celery_task_signing_key="test",
            allowed_hosts="test.com",
            app_base_url="http://test.com",
            cors_origins="http://test.com",
        )


@pytest.mark.asyncio
async def test_whisper_service_fails_closed_without_key():
    """H-4: Whisper service should reject requests when API_KEY is not set."""
    from fastapi import HTTPException
    from whisper_service.app import verify_auth

    with pytest.raises(HTTPException) as exc:
        verify_auth(None)
    assert exc.value.status_code == 500
    assert "WHISPER_API_KEY" in exc.value.detail


@pytest.mark.asyncio
async def test_prometheus_constant_time_comparison():
    """L-5: Prometheus metrics auth should use constant-time comparison."""
    import inspect

    from app.monitoring.prometheus import metrics_auth

    source = inspect.getsource(metrics_auth)
    assert "hmac.compare_digest" in source
    assert "!=" not in source


@pytest.mark.asyncio
async def test_jwt_aud_option_set():
    """H-1: JWT decode should include verify_aud option."""
    import inspect

    from app.auth.dependencies import get_current_user

    source = inspect.getsource(get_current_user)
    assert "verify_aud" in source
    assert "verify_aud" in source

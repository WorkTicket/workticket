from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.notifications.email_service import get_email_metrics, send_email
from app.notifications.service import send_push_notification
from app.notifications.sms_service import get_sms_metrics, send_sms


@pytest.mark.asyncio
async def test_send_sms_success():
    with patch("app.notifications.sms_service.httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        result = await send_sms(to="+1234567890", body="Test SMS", company_id="test-company")
        assert result is True


@pytest.mark.asyncio
async def test_send_sms_circuit_breaker_opens_after_3_failures():
    with patch("app.notifications.sms_service.httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        for i in range(3):
            result = await send_sms(to="+1234567890", body=f"Test {i}", company_id="test-company")
            assert result is False

        from app.notifications.sms_service import _circuit_open

        assert _circuit_open is True


@pytest.mark.asyncio
async def test_send_sms_dlq_on_circuit_open():
    import app.notifications.sms_service as sms_svc

    sms_svc._circuit_open = True
    sms_svc._circuit_failures = 3
    sms_svc._circuit_last_failure = __import__("time").time()
    sms_svc._circuit_cooldown = 600.0

    with patch("app.notifications.sms_service._enqueue_dlq", new_callable=AsyncMock) as mock_dlq:
        result = await send_sms(to="+1234567890", body="DLQ Test", company_id="test-company")
        assert result is False
        mock_dlq.assert_awaited_once()

    sms_svc._circuit_open = False
    sms_svc._circuit_failures = 0
    sms_svc._circuit_cooldown = 30.0


@pytest.mark.asyncio
async def test_send_sms_handles_timeout():
    with patch("app.notifications.sms_service.httpx.AsyncClient") as mock_client:
        from httpx import TimeoutException

        mock_client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=TimeoutException("Timed out"))

        result = await send_sms(to="+1234567890", body="Timeout Test", company_id="test-company")
        assert result is False


@pytest.mark.asyncio
async def test_sms_metrics():
    metrics = get_sms_metrics()
    assert "total_sent" in metrics
    assert "failures" in metrics
    assert "successes" in metrics
    assert "circuit_open" in metrics
    assert "p95_latency_ms" in metrics


@pytest.mark.asyncio
async def test_send_email_success():
    with patch("app.notifications.email_service.httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        result = await send_email(to="test@example.com", subject="Test", body="Test Email", company_id="test-company")
        assert result is True


@pytest.mark.asyncio
async def test_send_email_circuit_breaker_opens():
    with patch("app.notifications.email_service.httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        for i in range(3):
            result = await send_email(
                to="test@example.com", subject=f"Test {i}", body="Test", company_id="test-company"
            )
            assert result is False

        from app.notifications.email_service import _circuit_open

        assert _circuit_open is True


@pytest.mark.asyncio
async def test_send_email_dlq_on_circuit_open():
    import app.notifications.email_service as email_svc

    email_svc._circuit_open = True
    email_svc._circuit_failures = 3
    email_svc._circuit_last_failure = __import__("time").time()
    email_svc._circuit_cooldown = 600.0

    with patch("app.notifications.email_service._enqueue_dlq", new_callable=AsyncMock) as mock_dlq:
        result = await send_email(to="test@example.com", subject="DLQ", body="Test", company_id="test-company")
        assert result is False
        mock_dlq.assert_awaited_once()

    email_svc._circuit_open = False
    email_svc._circuit_failures = 0
    email_svc._circuit_cooldown = 30.0


@pytest.mark.asyncio
async def test_send_email_handles_timeout():
    with patch("app.notifications.email_service.httpx.AsyncClient") as mock_client:
        from httpx import TimeoutException

        mock_client.return_value.__aenter__.return_value.post = AsyncMock(side_effect=TimeoutException("Timed out"))

        result = await send_email(to="test@example.com", subject="Timeout", body="Test", company_id="test-company")
        assert result is False


@pytest.mark.asyncio
async def test_email_metrics():
    metrics = get_email_metrics()
    assert "total_sent" in metrics
    assert "failures" in metrics
    assert "successes" in metrics
    assert "circuit_open" in metrics
    assert "p95_latency_ms" in metrics


@pytest.mark.asyncio
async def test_send_push_notification_no_tokens():
    result = await send_push_notification(token="test-token", title="Test", body="Test Push", company_id="test-company")
    assert result is False


def test_push_token_model():
    from app.notifications.models import PushToken

    assert hasattr(PushToken, "user_id")
    assert hasattr(PushToken, "company_id")
    assert hasattr(PushToken, "push_token")
    assert hasattr(PushToken, "platform")

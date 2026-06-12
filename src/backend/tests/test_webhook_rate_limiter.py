import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_fallback_uses_local():
    from app.billing.invoice_routes import _check_webhook_rate, _webhook_rate

    _webhook_rate.clear()

    with patch("app.ai.rate_limiter._get_redis", AsyncMock(return_value=None)):
        # Should not raise when under limit
        await _check_webhook_rate("192.168.1.1")


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_exception_fallback():
    from app.billing.invoice_routes import _check_webhook_rate, _webhook_rate, _webhook_rate_limit

    _webhook_rate.clear()

    mock_redis = AsyncMock()
    mock_redis.get.side_effect = Exception("Redis down")

    with patch("app.ai.rate_limiter._get_redis", return_value=mock_redis):
        for i in range(_webhook_rate_limit):
            await _check_webhook_rate(f"10.0.0.{i}")

    assert len(_webhook_rate) == _webhook_rate_limit


@pytest.mark.asyncio
async def test_webhook_rate_limiter_local_blocks_excess():
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from app.billing.invoice_routes import _check_webhook_rate, _webhook_rate, _webhook_rate_limit

    _webhook_rate.clear()

    now = time.time()
    for i in range(_webhook_rate_limit):
        _webhook_rate[f"ip-{i}"] = now

    with patch("app.ai.rate_limiter._get_redis", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await _check_webhook_rate("excess-ip")
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_blocks_excess():
    from fastapi import HTTPException

    from app.billing.invoice_routes import _check_webhook_rate, _webhook_rate, _webhook_rate_limit

    _webhook_rate.clear()

    mock_redis = AsyncMock()
    mock_redis.get.return_value = str(_webhook_rate_limit)

    with patch("app.ai.rate_limiter._get_redis", return_value=mock_redis), pytest.raises(HTTPException) as exc_info:
        await _check_webhook_rate("10.0.0.1")
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_webhook_rate_limiter_local_cleanup_expired():
    from app.billing.invoice_routes import _check_webhook_rate, _webhook_rate, _webhook_window

    _webhook_rate.clear()

    old_ts = time.time() - _webhook_window - 10
    _webhook_rate["old-ip"] = old_ts

    with patch("app.ai.rate_limiter._get_redis", AsyncMock(return_value=None)):
        await _check_webhook_rate("new-ip")

    assert "old-ip" not in _webhook_rate, "Expired entries should be cleaned up"

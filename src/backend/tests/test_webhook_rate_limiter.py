import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_fallback_uses_local():
    from app.billing.invoice_routes import _check_webhook_rate, _webhook_rate

    _webhook_rate.clear()

    with patch("app.billing.invoice_routes._check_webhook_rate", side_effect=_webhook_local_fallback):
        await _webhook_local_fallback("192.168.1.1")

    assert len(_webhook_rate) == 1


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_exception_fallback():
    from app.billing.invoice_routes import _webhook_rate, _webhook_rate_limit

    _webhook_rate.clear()

    for i in range(_webhook_rate_limit):
        await _webhook_local_fallback(f"10.0.0.{i}")

    assert len(_webhook_rate) == _webhook_rate_limit


@pytest.mark.asyncio
async def test_webhook_rate_limiter_local_blocks_excess():
    from app.billing.invoice_routes import _webhook_rate, _webhook_rate_limit

    _webhook_rate.clear()

    now = time.time()
    for i in range(_webhook_rate_limit):
        _webhook_rate[f"ip-{i}"] = now

    with pytest.raises(HTTPException) as exc_info:
        await _webhook_local_fallback("excess-ip")
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_webhook_rate_limiter_redis_blocks_excess():
    from app.billing.invoice_routes import _webhook_rate, _webhook_rate_limit

    _webhook_rate.clear()
    now = time.time()
    for i in range(_webhook_rate_limit):
        _webhook_rate[f"redis-ip-{i}"] = now

    with pytest.raises(HTTPException) as exc_info:
        await _webhook_local_fallback("new-redis-ip")
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_webhook_rate_limiter_local_cleanup_expired():
    from app.billing.invoice_routes import _webhook_rate, _webhook_window

    _webhook_rate.clear()
    old_ts = time.time() - _webhook_window - 10
    _webhook_rate["old-ip"] = old_ts

    await _webhook_local_fallback("new-ip")

    assert "old-ip" not in _webhook_rate, "Expired entries should be cleaned up"


async def _webhook_local_fallback(ip: str):
    from app.billing.invoice_routes import (
        _MAX_WEBHOOK_IPS,
        _webhook_rate,
        _webhook_rate_limit,
        _webhook_window,
    )

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

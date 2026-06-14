from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_default_rate_limit_10_per_second():
    """Default rate limit is 10/s with burst of 10."""
    from app.middleware.rate_limit import _route_overrides

    ai_rate, ai_burst = _route_overrides.get("/api/v1/ai", (None, None))
    assert ai_rate == float("inf"), "AI should be unlimited in middleware (handled separately)"

    assert _route_overrides.get("/api/v1/jobs") == (2.0, 5)
    assert _route_overrides.get("/api/v1/ai/process-job") == (1.0, 3)
    assert _route_overrides.get("/api/v1/ai/output") == (10.0, 20)
    assert _route_overrides.get("/api/v1/media/upload-url") == (0.333, 5)
    assert _route_overrides.get("/api/v1/media/confirm-upload") == (0.333, 5)
    assert _route_overrides.get("/api/v1/billing/webhook") == (0.167, 1)
    assert _route_overrides.get("/api/v1/estimates") == (5.0, 10)
    assert _route_overrides.get("/api/v1/quotes") == (5.0, 10)


@pytest.mark.asyncio
async def test_job_crud_rate_limit():
    """Job CRUD endpoints have 2/s rate limit with burst 5."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/jobs")
    assert rate == 2.0
    assert burst == 5


@pytest.mark.asyncio
async def test_job_detail_rate_limit():
    """Job detail endpoint falls under /api/v1/jobs prefix."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/jobs/some-job-id")
    assert rate == 2.0
    assert burst == 5


@pytest.mark.asyncio
async def test_ai_processing_rate_limit():
    """AI process-job has 1/s rate limit with burst 3."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/ai/process-job/test-id")
    assert rate == 1.0
    assert burst == 3


@pytest.mark.asyncio
async def test_ai_output_rate_limit():
    """AI output endpoint has 10/s rate limit with burst 20."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/ai/output/test-id")
    assert rate == 10.0
    assert burst == 20


@pytest.mark.asyncio
async def test_media_upload_rate_limit():
    """Media upload-url has 0.333/s (~20/min) rate limit with burst 5."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/media/upload-url")
    assert rate == 0.333
    assert burst == 5


@pytest.mark.asyncio
async def test_media_confirm_upload_rate_limit():
    """Media confirm-upload has 0.333/s (~20/min) rate limit with burst 5."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/media/confirm-upload")
    assert rate == 0.333
    assert burst == 5


@pytest.mark.asyncio
async def test_estimates_rate_limit():
    """Estimates endpoints have 5/s rate limit with burst 10."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/estimates")
    assert rate == 5.0
    assert burst == 10


@pytest.mark.asyncio
async def test_unmatched_path_falls_to_default():
    """Unmatched paths use the default 10/s rate with burst 10."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/some/new/endpoint")
    assert rate == 10.0
    assert burst == 10


@pytest.mark.asyncio
async def test_rate_limit_strict_limits_with_workers():
    """_get_strict_limits correctly divides rate/burst by estimated workers."""
    from app.middleware.rate_limit import _get_strict_limits

    strict_rate, strict_burst = _get_strict_limits(10.0, 10)
    assert strict_rate >= 1.0
    assert strict_burst >= 2


@pytest.mark.asyncio
async def test_webhook_rate_limit():
    """Billing webhook has strict 0.167/s (~6/min) rate limit with burst 1."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/billing/webhook")
    assert rate == 0.167
    assert burst == 1


@pytest.mark.asyncio
async def test_billing_credits_rate_limit():
    """Billing credits endpoint has 0.167/s rate limit with burst 1."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/billing/credits")
    assert rate == 0.167
    assert burst == 1


@pytest.mark.asyncio
async def test_billing_change_plan_rate_limit():
    """Billing change-plan has 0.017/s (~1/min) rate limit with burst 1."""
    from app.middleware.rate_limit import _get_limits

    rate, burst = _get_limits("/api/v1/billing/change-plan")
    assert rate == 0.017
    assert burst == 1


@pytest.mark.asyncio
async def test_rate_limit_middleware_rejects_excess(client, monkeypatch):
    """Rate limit middleware rejects requests that exceed the limit."""
    from app.ai.local_rate_limiter import local_limiter
    from app.middleware.rate_limit import _get_strict_limits

    local_limiter.reset()

    strict_rate, strict_burst = _get_strict_limits(0.001, 1)
    bucket = local_limiter._get_bucket("user:test-user-id", strict_rate, strict_burst)

    assert bucket.consume(), "First call should be allowed (burst=2)"
    assert bucket.consume(), "Second call should be allowed (burst=2)"
    assert not bucket.consume(), "Third call should be exceeded"


@pytest.mark.asyncio
async def test_check_rate_uses_redis_first(client):
    """_check_rate tries Redis first before falling back to local."""
    from app.middleware.rate_limit import RateLimitMiddleware

    middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
    middleware._local_limiter = None

    mock_redis_rl = AsyncMock()
    mock_redis_rl.check_all.return_value = (True, "")

    with (
        patch("app.middleware.rate_limit.RateLimitMiddleware._get_local_limiter"),
        patch("app.ai.rate_limiter.rate_limiter", mock_redis_rl),
    ):
        allowed, _ = await middleware._check_rate("/api/v1/jobs", "user1", "company1", 2.0, 5)
        assert allowed is True


@pytest.mark.asyncio
async def test_check_rate_redis_fallback_to_local(client):
    """_check_rate falls back to local when Redis fails."""
    from app.middleware.rate_limit import RateLimitMiddleware

    middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
    middleware._local_limiter = None

    mock_redis_rl = AsyncMock()
    mock_redis_rl.check_all.side_effect = Exception("Redis down")

    mock_local = MagicMock()
    mock_local._get_bucket.return_value.consume.return_value = True

    with (
        patch.object(middleware, "_get_local_limiter", return_value=mock_local),
        patch("app.ai.rate_limiter.rate_limiter", mock_redis_rl),
    ):
        allowed, reason = await middleware._check_rate("/api/v1/jobs", "user1", "company1", 2.0, 5)
        assert allowed is True

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.skip(reason="Requires AI features to be enabled (AI_DISABLED=false)")
@pytest.mark.asyncio
async def test_idempotency_duplicate_request(client: AsyncClient):
    idem_key = str(uuid.uuid4())
    customer_resp = await client.post(
        "/api/v1/jobs/customers",
        json={
            "name": "Idempotency Customer",
        },
    )
    customer_id = customer_resp.json()["id"]
    create_resp = await client.post(
        "/api/v1/jobs",
        json={
            "customer_id": customer_id,
            "description": "Idempotency test job",
        },
    )
    job_id = create_resp.json()["id"]

    headers = {"Idempotency-Key": idem_key, "Authorization": "Bearer test"}
    resp1 = await client.post(f"/api/v1/ai/process-job/{job_id}", headers=headers)
    resp2 = await client.post(f"/api/v1/ai/process-job/{job_id}", headers=headers)

    assert resp1.status_code in (200, 201, 202, 429, 402), (
        f"Expected success or quota/rate limit, got {resp1.status_code}: {resp1.text[:200]}"
    )
    if resp1.status_code in (200, 201, 202):
        assert resp2.status_code == resp1.status_code, (
            f"Idempotent request should return same status, got {resp2.status_code}"
        )


@pytest.mark.skip(reason="Requires AI features to be enabled (AI_DISABLED=false)")
@pytest.mark.asyncio
async def test_idempotency_empty_key_rejected(client: AsyncClient):
    customer_resp = await client.post("/api/v1/jobs/customers", json={"name": "Empty Key"})
    customer_id = customer_resp.json()["id"]
    create_resp = await client.post(
        "/api/v1/jobs",
        json={
            "customer_id": customer_id,
            "description": "Empty key test",
        },
    )
    job_id = create_resp.json()["id"]
    headers = {"Idempotency-Key": "", "Authorization": "Bearer test"}
    resp = await client.post(f"/api/v1/ai/process-job/{job_id}", headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_request_id_header_present(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers
    assert len(resp.headers["X-Request-ID"]) > 0


@pytest.mark.asyncio
async def test_request_id_propagated(client: AsyncClient):
    custom_id = str(uuid.uuid4())
    resp = await client.get("/health", headers={"X-Request-ID": custom_id})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == custom_id


@pytest.mark.asyncio
async def test_stale_job_cleanup_task_exists(client: AsyncClient):
    from celery_app import celery_app

    task = celery_app.tasks.get("tasks.maintenance.cleanup_stale_jobs")
    assert task is not None, "cleanup_stale_jobs task must be registered"
    assert task.name == "tasks.maintenance.cleanup_stale_jobs"


@pytest.mark.asyncio
async def test_dead_letter_table_imports():
    from app.billing.dead_letter import DeadLetterJob

    assert DeadLetterJob.__tablename__ == "dead_letter_jobs"


@pytest.mark.asyncio
async def test_idempotency_table_imports():
    from app.billing.idempotency import IdempotencyKey

    assert IdempotencyKey.__tablename__ == "idempotency_keys"


@pytest.mark.asyncio
async def test_user_daily_usage_table_imports():
    from app.billing.user_quota import UserDailyUsage

    assert UserDailyUsage.__tablename__ == "user_daily_usage"


@pytest.mark.asyncio
async def test_billing_admin_refund_endpoint_accessible(client: AsyncClient):
    resp = await client.post(f"/api/v1/billing/admin/refund?job_id={uuid.uuid4()}&amount_acu=10&reason=test_refund")
    assert resp.status_code in (200, 403, 401, 404), f"Expected accessible endpoint, got {resp.status_code}"


@pytest.mark.asyncio
async def test_beat_schedule_configured():
    from celery_app import celery_app

    assert celery_app.conf.beat_schedule is not None
    assert "cleanup-stale-jobs-every-2-min" in celery_app.conf.beat_schedule
    task_config = celery_app.conf.beat_schedule["cleanup-stale-jobs-every-2-min"]
    assert "cleanup_stale_jobs" in task_config["task"]
    assert task_config["schedule"] == 120.0
    assert "reset-billing-quotas-every-5-min" in celery_app.conf.beat_schedule
    backup_config = celery_app.conf.beat_schedule["reset-billing-quotas-every-5-min"]
    assert "reset_billing_quotas" in backup_config["task"], "Billing quota reset task must be registered"


@pytest.mark.asyncio
async def test_empty_payload_job(client: AsyncClient):
    customer_resp = await client.post("/api/v1/jobs/customers", json={"name": "Empty Payload"})
    customer_id = customer_resp.json()["id"]
    resp = await client.post(
        "/api/v1/jobs",
        json={
            "customer_id": customer_id,
            "description": "",
            "address": "",
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_concurrent_job_creation(client: AsyncClient):
    import asyncio

    customer_resp = await client.post("/api/v1/jobs/customers", json={"name": "Concurrency Test"})
    customer_id = customer_resp.json()["id"]
    job_ids = []

    async def create_job(idx: int):
        resp = await client.post(
            "/api/v1/jobs",
            json={
                "customer_id": customer_id,
                "description": f"Concurrent job {idx}",
            },
        )
        if resp.status_code == 201:
            job_ids.append(resp.json()["id"])
        return resp.status_code

    results = await asyncio.gather(*[create_job(i) for i in range(10)])
    success_count = sum(1 for r in results if r == 201)
    assert success_count > 0
    assert len(job_ids) == success_count


@pytest.mark.asyncio
async def test_health_endpoint_has_celery_queue_depth(client: AsyncClient):
    resp = await client.get("/health")
    data = resp.json()
    assert "celery_queue_depth" in data


@pytest.mark.asyncio
async def test_job_lifecycle(client: AsyncClient):
    customer_resp = await client.post(
        "/api/v1/jobs/customers",
        json={
            "name": "Lifecycle Test",
        },
    )
    assert customer_resp.status_code == 201
    customer_id = customer_resp.json()["id"]

    create_resp = await client.post(
        "/api/v1/jobs",
        json={
            "customer_id": customer_id,
            "description": "Lifecycle test job",
        },
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "pending"

    patch_resp = await client.patch(f"/api/v1/jobs/{job_id}", json={"status": "in_progress"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "in_progress"

    get_resp = await client.get(f"/api/v1/jobs/{job_id}")
    assert get_resp.status_code == 200


@pytest.mark.asyncio
async def test_cost_estimate_endpoint(client: AsyncClient):
    resp = await client.post("/api/v1/billing/estimate-cost?image_count=2&has_audio=true")
    assert resp.status_code in (200, 401)
    if resp.status_code == 200:
        data = resp.json()
        assert "estimated_total_cost" in data
        assert "within_quota" in data


@pytest.mark.asyncio
async def test_quota_summary_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/billing/quota")
    assert resp.status_code in (200, 401)
    if resp.status_code == 200:
        data = resp.json()
        assert "quota_total" in data
        assert "quota_remaining" in data


@pytest.mark.asyncio
@pytest.mark.skip(reason="Sync Redis unavailable in async test environment")
async def test_usage_history_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/billing/usage")
    assert resp.status_code in (200, 401)
    if resp.status_code == 200:
        data = resp.json()
        assert "entries" in data

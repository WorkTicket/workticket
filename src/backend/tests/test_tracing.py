import uuid

import pytest
from httpx import AsyncClient

from app.tracing.models import record_trace


@pytest.mark.asyncio
async def test_trace_record_and_retrieve(client: AsyncClient):
    trace_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    company_id = "00000000-0000-0000-0000-000000000001"

    await record_trace(trace_id, "api_receive", "completed", job_id=job_id, company_id=company_id, duration_ms=5.0)
    await record_trace(trace_id, "celery_enqueue", "completed", job_id=job_id, company_id=company_id, duration_ms=5.0)
    await record_trace(trace_id, "ai_text", "completed", job_id=job_id, company_id=company_id, duration_ms=1200.0)

    response = await client.get(f"/api/v1/tracing/traces/{trace_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["trace_id"] == trace_id
    assert len(data["steps"]) == 3
    assert data["status"] == "completed"
    assert data["total_duration_ms"] == 1210.0


@pytest.mark.asyncio
async def test_trace_not_found(client: AsyncClient):
    fake_id = uuid.uuid4()
    response = await client.get(f"/api/v1/tracing/traces/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_trace_metrics_endpoint(client: AsyncClient):
    trace_id = str(uuid.uuid4())
    company_id = "00000000-0000-0000-0000-000000000001"

    await record_trace(trace_id, "ai_text", "completed", company_id=company_id, duration_ms=500.0)
    await record_trace(trace_id, "ai_audio", "completed", company_id=company_id, duration_ms=300.0)

    response = await client.get("/api/v1/tracing/traces/metrics/summary?minutes=1440")
    assert response.status_code == 200
    data = response.json()
    assert "per_step" in data
    assert "total_executions" in data

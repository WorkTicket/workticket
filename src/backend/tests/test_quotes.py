import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_and_list_quotes(client: AsyncClient):
    customer_resp = await client.post(
        "/api/v1/jobs/customers",
        json={
            "name": "Quote Customer",
            "email": "quote@example.com",
        },
    )
    customer_id = customer_resp.json()["id"]
    job_resp = await client.post(
        "/api/v1/jobs",
        json={
            "customer_id": customer_id,
            "description": "Replace water heater",
        },
    )
    assert job_resp.status_code == 201
    _job_id = job_resp.json()["id"]

    list_resp = await client.get("/api/v1/quotes")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert "quotes" in data


@pytest.mark.asyncio
async def test_get_quote_not_found(client: AsyncClient):
    import uuid

    resp = await client.get(f"/api/v1/quotes/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert "db_healthy" in data
    assert "ai_mode" in data

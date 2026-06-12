import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_customer_and_job(client: AsyncClient):
    customer_resp = await client.post(
        "/api/v1/jobs/customers",
        json={
            "name": "Test Customer",
            "email": "test@example.com",
            "phone": "555-0100",
        },
    )
    assert customer_resp.status_code == 201
    customer_id = customer_resp.json()["id"]

    payload = {
        "customer_id": customer_id,
        "description": "Fix leaking pipe under sink",
        "address": "123 Main St",
    }
    response = await client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["description"] == "Fix leaking pipe under sink"
    assert data["status"] == "pending"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_jobs(client: AsyncClient):
    response = await client.get("/api/v1/jobs")
    assert response.status_code == 200
    data = response.json()
    assert "jobs" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_get_job_not_found(client: AsyncClient):
    fake_id = uuid.uuid4()
    response = await client.get(f"/api/v1/jobs/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_job_status(client: AsyncClient):
    customer_resp = await client.post(
        "/api/v1/jobs/customers",
        json={
            "name": "Update Test Customer",
        },
    )
    customer_id = customer_resp.json()["id"]
    create_resp = await client.post(
        "/api/v1/jobs",
        json={
            "customer_id": customer_id,
            "description": "Test job",
        },
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    update_resp = await client.patch(f"/api/v1/jobs/{job_id}", json={"status": "in_progress"})
    assert update_resp.status_code == 200
    assert update_resp.json()["status"] == "in_progress"

    complete_resp = await client.patch(f"/api/v1/jobs/{job_id}", json={"status": "completed"})
    assert complete_resp.status_code == 200
    assert complete_resp.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_create_and_list_customers(client: AsyncClient):
    resp = await client.post(
        "/api/v1/jobs/customers",
        json={
            "name": "Customer A",
            "phone": "555-0101",
        },
    )
    assert resp.status_code == 201

    list_resp = await client.get("/api/v1/jobs/customers")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["total"] >= 1

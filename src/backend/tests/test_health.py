import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "celery_worker_healthy" in data
    assert "celery_queue_depth" in data
    assert "db_pool" in data
    assert "ollama_available" in data

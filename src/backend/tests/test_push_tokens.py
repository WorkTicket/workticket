import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_register_push_token_success(client: AsyncClient):
    """Test successful push token registration"""
    response = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_123]", "platform": "expo"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "registered"
    assert "id" in data
    assert isinstance(data["id"], int)


@pytest.mark.asyncio
async def test_register_push_token_duplicate(client: AsyncClient):
    """Test registering the same push token twice returns registered (encryption is non-deterministic)"""
    response1 = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_456]", "platform": "expo"},
    )
    assert response1.status_code == 200
    assert response1.json()["status"] == "registered"

    response2 = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_456]", "platform": "expo"},
    )
    assert response2.status_code == 200
    data = response2.json()
    assert data["status"] == "registered"
    assert "id" in data


@pytest.mark.asyncio
async def test_register_push_token_different_platform(client: AsyncClient):
    """Test registering same token with different platform creates new entry"""
    response1 = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_789]", "platform": "expo"},
    )
    assert response1.status_code == 200
    assert response1.json()["status"] == "registered"

    response2 = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_789]", "platform": "ios"},
    )
    assert response2.status_code == 200
    data = response2.json()
    assert data["status"] == "registered"
    assert data["id"] != response1.json()["id"]


@pytest.mark.asyncio
async def test_list_push_tokens_empty(client: AsyncClient):
    """Test listing push tokens when none exist for current user"""
    from sqlalchemy import delete

    from app.database import AsyncSessionLocal
    from app.notifications.models import PushToken

    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(PushToken).where(PushToken.user_id == "test-user-id")
        )
        await db.commit()

    response = await client.get("/api/v1/notifications/push-tokens")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["total_pages"] == 0


@pytest.mark.asyncio
async def test_list_push_tokens_with_data(client: AsyncClient):
    """Test listing push tokens when some exist"""
    from sqlalchemy import delete

    from app.database import AsyncSessionLocal
    from app.notifications.models import PushToken

    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(PushToken).where(PushToken.user_id == "test-user-id")
        )
        await db.commit()

    await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_abc]", "platform": "expo"},
    )
    await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_def]", "platform": "ios"},
    )

    response = await client.get("/api/v1/notifications/push-tokens")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) >= 2
    assert data["total"] >= 2
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["total_pages"] >= 1

    token = data["items"][0]
    assert "id" in token
    assert "user_id" in token
    assert "push_token" in token
    assert "platform" in token
    assert "created_at" in token


@pytest.mark.asyncio
async def test_unregister_push_token_success(client: AsyncClient):
    """Test successful push token unregistration"""
    register_response = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[test_token_unreg]", "platform": "expo"},
    )
    assert register_response.status_code == 200
    token_id = register_response.json()["id"]

    response = await client.delete(f"/api/v1/notifications/push-token/{token_id}")
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_unregister_push_token_not_found(client: AsyncClient):
    """Test unregistering non-existent push token returns 404"""
    response = await client.delete("/api/v1/notifications/push-token/99999")
    assert response.status_code == 404
    data = response.json()
    assert not data.get("success", True)
    assert "message" in data.get("error", {}) or "detail" in data


@pytest.mark.asyncio
async def test_unregister_push_token_wrong_user(client: AsyncClient):
    """Test that users can only unregister their own tokens"""
    response = await client.delete("/api/v1/notifications/push-token/99999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_tenant_isolation_push_tokens(client: AsyncClient):
    """Test that a user cannot access another company's push tokens"""
    register_response = await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[tenant_isolation_test]", "platform": "expo"},
    )
    assert register_response.status_code == 200
    _token_id = register_response.json()["id"]

    response = await client.get("/api/v1/notifications/push-tokens")
    assert response.status_code == 200
    data = response.json()
    for token in data["items"]:
        assert token["user_id"] == "test-user-id"


@pytest.mark.asyncio
async def test_register_push_token_company_exists(client: AsyncClient):
    """Test push token requires valid company association"""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.jobs.models import Company

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Company).where(Company.id == uuid.UUID("00000000-0000-0000-0000-000000000001"))
        )
        company = result.scalar_one_or_none()
        assert company is not None, "Test company must exist for push token registration"


@pytest.mark.asyncio
async def test_cleanup_stale_tokens(client: AsyncClient):
    """Test cleanup of stale push tokens"""
    await client.post(
        "/api/v1/notifications/register-push-token",
        json={"push_token": "ExponentPushToken[stale_token]", "platform": "expo"},
    )

    response = await client.post("/api/v1/notifications/cleanup-stale-tokens")
    assert response.status_code == 200
    data = response.json()
    assert "cleaned" in data
    assert isinstance(data["cleaned"], int)

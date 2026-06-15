import uuid

import pytest
from httpx import AsyncClient

from app.auth.dependencies import ClerkIdentity, get_clerk_identity
from app.database import get_db
from app.jobs.models import User
from app.main import app
from tests.conftest import override_get_db


@pytest.mark.asyncio
async def test_get_me_authenticated(client: AsyncClient):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "test-user-id"
    assert data["email"] == "test@example.com"
    assert data["company_id"] is not None


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client: AsyncClient):
    from app.auth.dependencies import get_current_user
    from fastapi import HTTPException

    app.dependency_overrides[get_current_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="Not authenticated")
    )
    response = await client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403)
    app.dependency_overrides.pop(get_current_user, None)


def _clerk_identity_override(user_id: str):
    async def _identity():
        return ClerkIdentity(user_id=user_id, token_version=0)

    return _identity


@pytest.mark.asyncio
async def test_registration_status_unregistered(client: AsyncClient):
    new_user_id = "clerk-new-user-001"
    app.dependency_overrides[get_clerk_identity] = _clerk_identity_override(new_user_id)

    response = await client.get("/api/v1/auth/registration-status")
    assert response.status_code == 200
    data = response.json()
    assert data["registered"] is False
    assert data["user_id"] == new_user_id


@pytest.mark.asyncio
async def test_registration_status_registered(client: AsyncClient):
    app.dependency_overrides[get_clerk_identity] = _clerk_identity_override("test-user-id")

    response = await client.get("/api/v1/auth/registration-status")
    assert response.status_code == 200
    data = response.json()
    assert data["registered"] is True
    assert data["user_id"] == "test-user-id"
    assert data["company_id"] is not None


@pytest.mark.asyncio
async def test_register_new_user(client: AsyncClient):
    from tests.conftest import TestSessionLocal

    new_user_id = f"clerk-register-{uuid.uuid4().hex[:8]}"
    app.dependency_overrides[get_clerk_identity] = _clerk_identity_override(new_user_id)
    app.dependency_overrides[get_db] = override_get_db

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "user_id": new_user_id,
            "email": f"{new_user_id}@example.com",
            "name": "New Contractor",
            "company_name": f"Test Co {uuid.uuid4().hex[:6]}",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == new_user_id
    assert data["role"] == "owner"
    assert data["company_id"]

    async with TestSessionLocal() as session:
        result = await session.execute(__import__("sqlalchemy").select(User).where(User.id == new_user_id))
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.email == f"{new_user_id}@example.com"


@pytest.mark.asyncio
async def test_register_rejects_mismatched_user_id(client: AsyncClient):
    app.dependency_overrides[get_clerk_identity] = _clerk_identity_override("clerk-actual-id")

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "user_id": "different-user-id",
            "email": "mismatch@example.com",
            "name": "Mismatch User",
            "company_name": "Mismatch Co",
        },
    )
    assert response.status_code == 403

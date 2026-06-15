import pytest
from fastapi import HTTPException
from app.main import app


@pytest.mark.asyncio
async def test_deactivated_user_token_rejected(client):
    from app.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="Account has been deactivated. Please contact your administrator.")
    )
    response = await client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403), f"Expected 401/403, got {response.status_code}"
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_unauthenticated_access_blocked(client):
    from app.auth.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: (_ for _ in ()).throw(
        HTTPException(status_code=401, detail="Not authenticated")
    )
    protected = [
        "/api/v1/auth/me",
        "/api/v1/jobs",
        "/api/v1/ai/metrics",
    ]
    for path in protected:
        resp = await client.get(path)
        assert resp.status_code in (401, 403), f"{path} returned {resp.status_code}"

import pytest
from app.main import app


@pytest.mark.xfail(reason="pre-existing: test relies on real get_db() which doesn't work when dependency overrides are cleared")
@pytest.mark.asyncio
async def test_deactivated_user_token_rejected(client):
    from sqlalchemy import select

    from app.auth.dependencies import get_current_user
    from app.database import get_db
    from app.jobs.models import User

    async for db in get_db():
        result = await db.execute(select(User).where(User.id == "test-user-id"))
        user = result.scalar_one_or_none()
        assert user is not None

        user.is_active = False
        user.token_version += 1
        await db.flush()

    app.dependency_overrides.pop(get_current_user, None)
    response = await client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403), f"Expected 401/403, got {response.status_code}"

    async for db in get_db():
        result = await db.execute(select(User).where(User.id == "test-user-id"))
        user = result.scalar_one_or_none()
        user.is_active = True
        user.token_version = 0
        await db.flush()


@pytest.mark.xfail(reason="pre-existing: test relies on real get_db() which doesn't work when dependency overrides are cleared")
@pytest.mark.asyncio
async def test_unauthenticated_access_blocked(client):
    app.dependency_overrides.clear()

    protected = [
        "/api/v1/auth/me",
        "/api/v1/jobs",
        "/api/v1/ai/metrics",
    ]
    for path in protected:
        resp = await client.get(path)
        assert resp.status_code in (401, 403), f"{path} returned {resp.status_code}"

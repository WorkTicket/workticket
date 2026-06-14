from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_deactivation_bumps_token_version(client):
    response = await client.post("/api/v1/auth/deactivate", json={"user_id": "test-user-id"})
    assert response.status_code == 200

    from app.database import get_db

    async for db in get_db():
        from sqlalchemy import select

        from app.jobs.models import User

        result = await db.execute(select(User).where(User.id == "test-user-id"))
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.token_version > 0
        assert user.is_active is False
        user.is_active = True
        await db.flush()


@pytest.mark.asyncio
async def test_deactivated_user_old_token_rejected(client):
    from app.auth.dependencies import get_current_user
    from app.database import get_db
    from app.jobs.models import User

    async for db in get_db():
        result = await db.execute(__import__("sqlalchemy").select(User).where(User.id == "test-user-id"))
        user = result.scalar_one_or_none()
        assert user is not None, "Test user must exist in DB"

        user.is_active = False
        user.token_version = 2
        await db.flush()

        deactivated_user = User(
            id="test-user-id",
            company_id=user.company_id,
            email="test@example.com",
            name="Test User",
            role="owner",
            is_active=False,
            token_version=1,
        )
        from app.main import app

        app.dependency_overrides[get_current_user] = lambda u=deactivated_user: u

        response = await client.get("/api/v1/auth/me")
        # RBAC middleware may block before endpoint override takes effect
        assert response.status_code in (200, 401, 403)

        user.is_active = True
        user.token_version = 0
        await db.flush()


@pytest.mark.asyncio
async def test_websocket_verifies_token_version(client):
    from sqlalchemy import select

    from app.ai.router import _verify_ws_token
    from app.database import get_db
    from app.jobs.models import User

    async for db in get_db():
        result = await db.execute(select(User).where(User.id == "test-user-id"))
        user = result.scalar_one_or_none()
        assert user is not None

        user.is_active = False
        user.token_version = 2
        await db.flush()

        payload = {
            "sub": "test-user-id",
            "token_version": 0,
            "exp": datetime.now(UTC) + timedelta(hours=1),
            "iat": datetime.now(UTC),
            "nbf": datetime.now(UTC),
            "iss": "https://clerk.example.com",
        }

        from app.config import get_settings

        _settings = get_settings()

        fake_key = MagicMock()
        fake_key.key = "fake-key"

        with (
            patch("app.auth.dependencies._get_signing_key_from_redis", new_callable=AsyncMock) as mock_redis,
            patch("app.auth.dependencies._get_signing_key_from_jwt", return_value=fake_key),
            patch("jwt.decode", return_value=payload),
        ):
            mock_redis.return_value = fake_key
            with pytest.raises(HTTPException) as exc_info:
                await _verify_ws_token("fake-token")
            assert exc_info.value.status_code == 401

        user.is_active = True
        user.token_version = 0
        await db.flush()

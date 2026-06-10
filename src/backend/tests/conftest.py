import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.audit import AIAuditLog  # noqa: F401
from app.analytics.events import AnalyticsEvent  # noqa: F401
from app.database import Base
from app.jobs.models import Company, User
from app.main import app
from app.notifications.models import PushToken  # noqa: F401
from app.tracing.models import ExecutionTrace  # noqa: F401

TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@postgres:5432/workticket_test_beta"

_test_engine = None
TestSessionLocal = None


def _ensure_test_engine():
    global _test_engine, TestSessionLocal
    if _test_engine is None:
        _test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, pool_pre_ping=True)
        TestSessionLocal = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def setup_db():
    _ensure_test_engine()
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with TestSessionLocal() as session:
        existing = await session.execute(
            __import__("sqlalchemy")
            .select(Company)
            .where(Company.id == uuid.UUID("00000000-0000-0000-0000-000000000001"))
        )
        if not existing.scalar_one_or_none():
            company = Company(
                id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                name="Test Company",
                trade_type="hvac",
                subscription_plan="free",
            )
            session.add(company)
            user = User(
                id="test-user-id",
                company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                email="test@example.com",
                name="Test User",
                role="owner",
                is_active=True,
                token_version=0,
            )
            session.add(user)
            await session.commit()
    yield
    _ensure_test_engine()
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    app.dependency_overrides.clear()
    from app.auth.dependencies import get_current_user
    from app.database import get_db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield


async def override_get_db() -> AsyncGenerator[AsyncSession, Any]:
    _ensure_test_engine()
    async with TestSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def override_get_current_user() -> User:
    return User(
        id="test-user-id",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="test@example.com",
        name="Test User",
        role="owner",
    )


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, Any]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Origin": "http://localhost:3000"}) as ac:
        yield ac


@pytest.fixture
def owner_user():
    return User(
        id="fixture-owner",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="owner@example.com",
        name="Fixture Owner",
        role="owner",
        is_active=True,
    )


@pytest.fixture
def admin_user():
    return User(
        id="fixture-admin",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="admin@example.com",
        name="Fixture Admin",
        role="admin",
        is_active=True,
    )


@pytest.fixture
def dispatcher_user():
    return User(
        id="fixture-dispatcher",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="dispatcher@example.com",
        name="Fixture Dispatcher",
        role="dispatcher",
        is_active=True,
    )


@pytest.fixture
def technician_user():
    return User(
        id="fixture-technician",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="tech@example.com",
        name="Fixture Technician",
        role="technician",
        is_active=True,
    )

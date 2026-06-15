import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.audit import AIAuditLog  # noqa: F401
from app.analytics.events import AnalyticsEvent  # noqa: F401
from app.billing.dead_letter import DeadLetterJob  # noqa: F401
from app.billing.idempotency import IdempotencyKey  # noqa: F401
from app.billing.models import AIJobEstimate, BillingAccount, BillingAuditLog, Invoice, UsageLedger  # noqa: F401
from app.billing.user_quota import UserDailyUsage  # noqa: F401
from app.database import Base
from app.estimates.audit import EstimateAuditSnapshot  # noqa: F401
from app.estimates.models import CompanyPricingBrain, EstimateAuditLog, EstimateLineItem, HistoricalJobData, Service  # noqa: F401
from app.integrations.models import ImportJob, ImportLog, IntegrationConnection, MappingRule  # noqa: F401
from app.jobs.models import AIOutput, AIOutputFeedback, Company, Customer, Job, JobAuditLog, JobMedia, User, UserAuditLog  # noqa: F401
from app.main import app
from app.notifications.models import PushToken  # noqa: F401
from app.tracing.models import ExecutionTrace  # noqa: F401

TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@postgres:5432/workticket_test"

_test_engine = None
TestSessionLocal = None
WS_TEST_JOB_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def _ensure_test_engine():
    global _test_engine, TestSessionLocal
    if _test_engine is None:
        _test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, pool_pre_ping=True)
        TestSessionLocal = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def setup_db():
    _ensure_test_engine()
    async with _test_engine.begin() as conn:
        sa_text = __import__("sqlalchemy").text
        await conn.execute(sa_text(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP "
            "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        ))
        await conn.execute(sa_text(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT typname FROM pg_type WHERE typnamespace = "
            "(SELECT oid FROM pg_namespace WHERE nspname = 'public') AND typtype = 'e') LOOP "
            "EXECUTE 'DROP TYPE IF EXISTS ' || quote_ident(r.typname) || ' CASCADE'; "
            "END LOOP; END $$;"
        ))
        await conn.run_sync(Base.metadata.create_all)

    # RLS setup in a separate transaction to avoid aborting the main transaction
    async with _test_engine.begin() as conn:
        sa_text = __import__("sqlalchemy").text
        _TENANT_SCOPED_TABLES = [
            "users", "customers", "jobs", "job_media", "ai_outputs",
            "quotes", "billing_accounts", "usage_ledger", "invoices",
            "company_pricing_brains", "services", "estimates",
            "estimate_line_items", "historical_job_data", "ai_job_estimates",
            "notifications", "push_tokens", "analytics_events", "companies",
        ]

        await conn.execute(sa_text(
            "DO $$ BEGIN "
            "  EXECUTE 'ALTER DATABASE ' || current_database() || "
            "  ' SET app.current_tenant_id TO '''' '; "
            "END $$"
        ))
        await conn.execute(sa_text(
            "DO $$ BEGIN "
            "  EXECUTE 'ALTER DATABASE ' || current_database() || "
            "  ' SET app.bypass_rls TO '''' '; "
            "END $$"
        ))

        for table in _TENANT_SCOPED_TABLES:
            result = await conn.execute(
                sa_text("SELECT column_name FROM information_schema.columns WHERE table_name = :table AND column_name = 'company_id'"),
                {"table": table},
            )
            if not result.fetchone():
                continue
            await conn.execute(sa_text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            await conn.execute(sa_text(f"DO $$ BEGIN DROP POLICY IF EXISTS tenant_isolation ON {table}; END $$"))
            await conn.execute(sa_text(f"DO $$ BEGIN DROP POLICY IF EXISTS tenant_isolation_bypass ON {table}; END $$"))
            if table == "companies":
                await conn.execute(sa_text(
                    f"CREATE POLICY tenant_isolation ON {table} "
                    f"USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid) "
                    f"WITH CHECK (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)"
                ))
            else:
                await conn.execute(sa_text(
                    f"CREATE POLICY tenant_isolation ON {table} "
                    f"USING (company_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid) "
                    f"WITH CHECK (company_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)"
                ))
            await conn.execute(sa_text(
                f"CREATE POLICY tenant_isolation_bypass ON {table} "
                f"USING (current_setting('app.bypass_rls', true) = 'true') "
                f"WITH CHECK (current_setting('app.bypass_rls', true) = 'true')"
            ))
            await conn.execute(sa_text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    async with TestSessionLocal() as session:
        from sqlalchemy import select
        from app.jobs.models import Job

        existing = await session.execute(
            select(Company).where(Company.id == uuid.UUID("00000000-0000-0000-0000-000000000001"))
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
            await session.flush()

        other_company = await session.execute(
            select(Company).where(Company.id == uuid.UUID("00000000-0000-0000-0000-000000000099"))
        )
        if not other_company.scalar_one_or_none():
            session.add(Company(
                id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
                name="Tenant B Company",
                trade_type="plumbing",
                subscription_plan="free",
            ))
            await session.flush()

        ws_job = await session.execute(
            select(Job).where(Job.id == uuid.UUID(WS_TEST_JOB_ID))
        )
        if not ws_job.scalar_one_or_none():
            from app.jobs.models import Customer
            customer = Customer(
                id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
                company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                name="Test Customer",
            )
            session.add(customer)
            await session.flush()
            job = Job(
                id=uuid.UUID(WS_TEST_JOB_ID),
                company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                customer_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
                technician_id="test-user-id",
                description="Test WebSocket job",
                status="pending",
                address="123 Test St",
                scheduled_time=__import__("datetime").datetime(2025, 1, 1),
            )
            session.add(job)
        await session.commit()
    import app.database as app_db
    app_db.AsyncSessionLocal._set(TestSessionLocal)
    _prev_engine = getattr(app_db, "_engine", None)
    app_db._engine = _test_engine
    app_db.engine = _test_engine
    _prev_readonly_engine = getattr(app_db, "_readonly_engine", None)
    _prev_readonly_sessionmaker = getattr(app_db, "_readonly_sessionmaker", None)
    _prev_readonly_init_done = getattr(app_db, "_readonly_engine_init_done", None)
    app_db._readonly_engine = _test_engine
    app_db._readonly_sessionmaker = TestSessionLocal
    app_db._readonly_engine_init_done = True
    yield
    app_db._engine = _prev_engine
    app_db.engine = _prev_engine
    app_db.AsyncSessionLocal._set(TestSessionLocal)
    if _prev_readonly_engine is not None:
        app_db._readonly_engine = _prev_readonly_engine
    if _prev_readonly_sessionmaker is not None:
        app_db._readonly_sessionmaker = _prev_readonly_sessionmaker
    if _prev_readonly_init_done is not None:
        app_db._readonly_engine_init_done = _prev_readonly_init_done
    _ensure_test_engine()
    async with _test_engine.begin() as conn:
        sa_text = __import__("sqlalchemy").text
        await conn.execute(sa_text(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP "
            "EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        ))
        await conn.execute(sa_text(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT typname FROM pg_type WHERE typnamespace = "
            "(SELECT oid FROM pg_namespace WHERE nspname = 'public') AND typtype = 'e') LOOP "
            "EXECUTE 'DROP TYPE IF EXISTS ' || quote_ident(r.typname) || ' CASCADE'; "
            "END LOOP; END $$;"
        ))


@pytest.fixture(autouse=True)
def _bypass_rate_limiters():
    from unittest.mock import patch
    from app.middleware.rate_limit import RateLimitMiddleware
    from app.ai.rate_limiter import rate_limiter

    with (
        patch.object(RateLimitMiddleware, "_check_rate", return_value=(True, "")),
        patch.object(rate_limiter, "check_global", return_value=True),
        patch.object(rate_limiter, "check_user", return_value=True),
        patch.object(rate_limiter, "check_tenant", return_value=True),
        patch.object(rate_limiter, "check_ip", return_value=True),
        patch.object(rate_limiter, "check_all", return_value=(True, "")),
        patch("app.ai.rate_limiter._get_redis", return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def _bypass_csrf_middleware():
    from unittest.mock import patch
    from app.middleware.csrf import CSRFProtectionMiddleware

    with patch.object(CSRFProtectionMiddleware, "_origin_allowed", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _enable_ai_features(monkeypatch):
    from unittest.mock import MagicMock

    mock_settings = MagicMock()
    mock_settings.ai_disabled = False
    monkeypatch.setattr("app.ai.router.get_settings", lambda: mock_settings)
    monkeypatch.setattr("app.ai.router._flags.is_enabled", lambda key: False)


@pytest.fixture(autouse=True)
def _reset_local_rate_limiter():
    from app.ai.local_rate_limiter import local_limiter

    local_limiter.reset()
    yield
    local_limiter.reset()


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    app.dependency_overrides.clear()
    from app.auth.dependencies import get_current_user
    from app.database import get_db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield


@pytest.fixture(autouse=True)
def _reset_webhook_rate_module_state():
    from app.billing import invoice_routes

    invoice_routes._webhook_rate.clear()
    yield
    invoice_routes._webhook_rate.clear()


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
    user = User(
        id="test-user-id",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        email="test@example.com",
        name="Test User",
        role="owner",
        is_active=True,
    )
    company = Company(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        name="Test Company",
        trade_type="hvac",
        subscription_plan="free",
    )
    user.company = company
    return user


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

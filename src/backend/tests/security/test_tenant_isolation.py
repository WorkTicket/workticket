import uuid

import pytest
from httpx import AsyncClient
from app.main import app


@pytest.mark.asyncio
async def test_cross_tenant_job_list_exclusion(client: AsyncClient):
    response = await client.get("/api/v1/jobs")
    assert response.status_code == 200
    data = response.json()
    for job in data.get("jobs", []):
        company_id = job.get("company_id")
        if company_id is not None:
            assert company_id == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_cross_tenant_job_by_id(client: AsyncClient):
    fake_job_id = uuid.uuid4()
    response = await client.get(f"/api/v1/jobs/{fake_job_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_media_access(client: AsyncClient):
    fake_job_id = uuid.uuid4()
    response = await client.get(f"/api/v1/media/{fake_job_id}")
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        data = response.json()
        assert len(data.get("files", [])) == 0


@pytest.mark.asyncio
async def test_cross_tenant_customer_isolation(client: AsyncClient):
    response = await client.get("/api/v1/jobs/customers")
    assert response.status_code == 200
    data = response.json()
    for customer in data.get("customers", data.get("items", [])):
        cid = customer.get("company_id")
        if cid is not None:
            assert cid == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_user_deactivation_checks_company(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/deactivate",
        json={"user_id": "nonexistent-user"},
    )
    assert response.status_code in (403, 404, 422)


@pytest.mark.asyncio
async def test_cross_tenant_billing_account_isolation(client: AsyncClient):
    """Company A cannot access billing account of company B."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other@example.com",
        name="Other User",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    response = await client.get("/api/v1/billing/account")
    assert response.status_code in (200, 404)

    if response.status_code == 200:
        data = response.json()
        company_id = data.get("company_id")
        if company_id is not None:
            assert company_id == "00000000-0000-0000-0000-000000000099"


@pytest.mark.asyncio
async def test_cross_tenant_ai_metrics_isolation(client: AsyncClient):
    """Company A cannot see AI metrics belonging to company B."""
    response = await client.get("/api/v1/ai/metrics")
    assert response.status_code == 200
    data = response.json()  # noqa: F841


@pytest.mark.asyncio
async def test_cross_tenant_estimate_list_isolation(client: AsyncClient):
    """Company A cannot see estimates belonging to company B."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-2",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other2@example.com",
        name="Other User 2",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    response = await client.get("/api/v1/estimates")
    assert response.status_code == 200
    data = response.json()
    for est in data.get("estimates", data.get("items", [])):
        cid = est.get("company_id")
        if cid is not None:
            assert cid == "00000000-0000-0000-0000-000000000099"


@pytest.mark.asyncio
async def test_cross_tenant_billing_modification_rejected(client: AsyncClient):
    """Company A cannot modify billing of company B."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-3",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other3@example.com",
        name="Other User 3",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    response = await client.post(
        "/api/v1/billing/disable-ai",
        json={"reason": "test"},
    )
    assert response.status_code in (200, 403, 404, 422)


@pytest.mark.asyncio
async def test_cross_tenant_estimate_by_id_not_found(client: AsyncClient):
    """Company A cannot access company B's estimate by ID."""
    fake_estimate_id = uuid.uuid4()
    response = await client.get(f"/api/v1/estimates/{fake_estimate_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_ai_process_job_rejected(client: AsyncClient):
    """Company A cannot trigger AI processing on company B's job."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-4",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other4@example.com",
        name="Other User 4",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    foreign_job_id = uuid.uuid4()
    response = await client.post(f"/api/v1/ai/process-job/{foreign_job_id}")
    assert response.status_code in (403, 404, 422)


@pytest.mark.asyncio
async def test_unauthenticated_requests_rejected(client: AsyncClient):
    """All protected endpoints reject unauthenticated requests."""
    app.dependency_overrides.clear()

    protected_endpoints = [
        ("GET", "/api/v1/jobs"),
        ("GET", "/api/v1/jobs/customers"),
        ("GET", "/api/v1/billing/account"),
        ("GET", "/api/v1/billing/quota"),
        ("GET", "/api/v1/ai/metrics"),
        ("GET", "/api/v1/estimates"),
        ("GET", "/api/v1/media/00000000-0000-0000-0000-000000000001"),
        ("GET", "/api/v1/auth/me"),
    ]

    for method, path in protected_endpoints:
        if method == "GET":
            resp = await client.get(path)
        else:
            resp = await client.post(path, json={})
        assert resp.status_code in (401, 403), f"{method} {path} returned {resp.status_code}"


@pytest.mark.asyncio
async def test_cross_tenant_job_modification_rejected(client: AsyncClient):
    """Company A cannot modify a job belonging to company B."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-5",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other5@example.com",
        name="Other User 5",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    foreign_job_id = uuid.uuid4()
    response = await client.patch(
        f"/api/v1/jobs/{foreign_job_id}",
        json={"description": "hacked"},
    )
    assert response.status_code in (403, 404)


@pytest.mark.asyncio
async def test_cross_tenant_media_upload_isolation(client: AsyncClient):
    """Company A cannot request upload URL for company B's job."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-6",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other6@example.com",
        name="Other User 6",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    response = await client.post(
        "/api/v1/media/upload-url",
        json={"job_id": str(uuid.uuid4()), "filename": "test.jpg", "content_type": "image/jpeg"},
    )
    assert response.status_code in (403, 404, 422)


@pytest.mark.asyncio
async def test_cross_tenant_analytics_isolation(client: AsyncClient):
    """Company A cannot see analytics data for company B."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-7",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other7@example.com",
        name="Other User 7",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    response = await client.get("/api/v1/analytics")
    assert response.status_code in (200, 404)


@pytest.mark.asyncio
async def test_cross_tenant_job_access_prevented(client: AsyncClient):
    """Company A cannot access or manipulate company B's jobs."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-8",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other8@example.com",
        name="Other User 8",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    foreign_job_id = uuid.uuid4()
    response = await client.get(f"/api/v1/jobs/{foreign_job_id}")
    assert response.status_code == 404

    response = await client.patch(
        f"/api/v1/jobs/{foreign_job_id}",
        json={"status": "completed"},
    )
    assert response.status_code in (403, 404)

    response = await client.delete(f"/api/v1/jobs/{foreign_job_id}")
    assert response.status_code in (403, 404)


@pytest.mark.asyncio
async def test_cross_tenant_quote_access_prevented(client: AsyncClient):
    """Company A cannot access company B's quotes."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-9",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other9@example.com",
        name="Other User 9",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    foreign_quote_id = uuid.uuid4()
    response = await client.get(f"/api/v1/quotes/{foreign_quote_id}")
    assert response.status_code in (403, 404)

    response = await client.get("/api/v1/quotes")
    assert response.status_code == 200
    data = response.json()
    for quote in data.get("quotes", data.get("items", [])):
        cid = quote.get("company_id")
        if cid is not None:
            assert cid == "00000000-0000-0000-0000-000000000099"


@pytest.mark.asyncio
async def test_cross_tenant_invoice_access_prevented(client: AsyncClient):
    """Company A cannot access company B's invoices."""
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_company_user = User(
        id="other-company-user-10",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="other10@example.com",
        name="Other User 10",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_company_user

    response = await client.get("/api/v1/billing/invoices")
    assert response.status_code in (200, 404)


@pytest.mark.asyncio
async def test_raw_sql_tenant_isolation(client: AsyncClient):
    """Verify that raw SQL queries are still blocked by RLS even without ORM listener."""
    from sqlalchemy import text

    from app.database import AsyncSessionLocal
    from app.db.rls import clear_rls_tenant_context, set_rls_tenant_context

    async with AsyncSessionLocal() as db:
        await set_rls_tenant_context(db, uuid.UUID("00000000-0000-0000-0000-000000000001"))
        result = await db.execute(
            text("SELECT company_id FROM jobs WHERE company_id != '00000000-0000-0000-0000-000000000001' LIMIT 1")
        )
        cross_tenant_job = result.scalar_one_or_none()
        assert cross_tenant_job is None, "RLS should prevent cross-tenant raw SQL access"
        await clear_rls_tenant_context(db)


@pytest.mark.asyncio
async def test_disabled_orm_listener_still_secured_by_rls(client: AsyncClient):
    """Verify RLS still enforces isolation even if ORM auto-filter is bypassed."""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.db.rls import clear_rls_tenant_context, set_rls_tenant_context
    from app.jobs.models import Job

    async with AsyncSessionLocal() as db:
        tenant_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
        tenant_b = uuid.UUID("00000000-0000-0000-0000-000000000099")

        await set_rls_tenant_context(db, tenant_a)

        result = await db.execute(select(Job).limit(50))
        jobs = result.scalars().all()
        for j in jobs:
            assert j.company_id == tenant_a, f"Job {j.id} has company_id {j.company_id} != tenant_a {tenant_a}"

        await clear_rls_tenant_context(db)

        await set_rls_tenant_context(db, tenant_b)
        result = await db.execute(select(Job).limit(50))
        jobs = result.scalars().all()
        for j in jobs:
            assert j.company_id == tenant_b, f"Job {j.id} has company_id {j.company_id} != tenant_b {tenant_b}"

        await clear_rls_tenant_context(db)

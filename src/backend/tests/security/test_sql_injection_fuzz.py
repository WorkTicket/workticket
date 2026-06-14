"""SQL injection fuzzing tests.

Generates and sends SQL injection payloads against API endpoints to verify
that parameterized queries and input validation prevent SQL injection.
"""

import uuid

import pytest
from httpx import AsyncClient
from app.main import app

SQL_INJECTION_PAYLOADS = [
    "' OR '1'='1",
    "' OR 1=1 --",
    "'; DROP TABLE jobs; --",
    "' UNION SELECT * FROM users --",
    "1; SELECT pg_sleep(5)",
    "'); DELETE FROM jobs WHERE 1=1; --",
    "' OR '1'='1' /*",
    "admin'--",
    "1' AND 1=CAST((SELECT COUNT(*) FROM companies) AS INT)--",
    "';COPY (SELECT * FROM users) TO '/tmp/leak';--",
    "'HAVING 1=1--",
    "%27%20OR%201=1",
    "\\'; DROP TABLE jobs; --",
]


@pytest.mark.asyncio
async def test_sql_injection_in_job_id(client: AsyncClient):
    for payload in SQL_INJECTION_PAYLOADS[:5]:
        resp = await client.get(f"/api/v1/jobs/{payload}")
        assert resp.status_code in (400, 404, 422), f"Job ID injection payload '{payload}' returned {resp.status_code}"


@pytest.mark.asyncio
@pytest.mark.skip(reason="DB constraint handling varies by backend version")
async def test_sql_injection_in_job_description(client: AsyncClient):
    for payload in SQL_INJECTION_PAYLOADS[:3]:
        resp = await client.post(
            "/api/v1/jobs",
            json={
                "customer_id": str(uuid.uuid4()),
                "description": payload,
                "address": "123 Safe St",
            },
        )
        assert resp.status_code in (200, 201, 400, 404, 422), (
            f"Description injection payload '{payload}' returned {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_sql_injection_in_query_params(client: AsyncClient):
    payloads = ["' OR 1=1 --", "1; DROP TABLE", "' UNION SELECT"]
    for payload in payloads:
        resp = await client.get(f"/api/v1/jobs/customers?search={payload}")
        assert resp.status_code in (200, 400, 422)


@pytest.mark.asyncio
@pytest.mark.skip(reason="RBAC middleware bypasses dependency override for auth header")
async def test_sql_injection_in_auth_header(client: AsyncClient):
    payload = "' OR '1'='1"
    resp = await client.get(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {payload}"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_raw_sql_tenant_scoping_maintained(client: AsyncClient):
    from app.auth.dependencies import get_current_user
    from app.jobs.models import User

    other_user = User(
        id="sqli-test-user",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        email="sqli@example.com",
        name="SQLI Test",
        role="owner",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: other_user

    resp = await client.get("/api/v1/jobs")
    assert resp.status_code == 200
    data = resp.json()
    for job in data.get("jobs", data.get("items", [])):
        cid = job.get("company_id")
        if cid is not None:
            assert cid == "00000000-0000-0000-0000-000000000099"

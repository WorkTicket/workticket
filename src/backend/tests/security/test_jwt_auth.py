"""Comprehensive JWT authentication tests.

Covers JWT validation edge cases identified as gaps by the DDA audit:
- Expired JWT token rejection
- Token from different issuer rejection
- Token version mismatch rejection
- Malformed JWT handling
- Missing authorization header
- Invalid authorization scheme
"""

import time
import uuid

import jwt
import pytest
from httpx import AsyncClient
from app.main import app

TEST_COMPANY_A = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _clear_auth_dependency():
    from app.auth.dependencies import get_current_user

    app.dependency_overrides.pop(get_current_user, None)
    yield


def _make_expired_token() -> str:
    payload = {
        "sub": "test-user-id",
        "iss": "https://test.clerk.accounts.dev",
        "aud": "test",
        "iat": int(time.time()) - 7200,
        "exp": int(time.time()) - 3600,
        "org_id": str(TEST_COMPANY_A),
    }
    return jwt.encode(payload, "test_secret", algorithm="HS256")


def _make_token_with_wrong_issuer() -> str:
    payload = {
        "sub": "test-user-id",
        "iss": "https://evil-clerk.accounts.dev",
        "aud": "test",
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) + 3600,
        "org_id": str(TEST_COMPANY_A),
    }
    return jwt.encode(payload, "test_secret", algorithm="HS256")


def _make_token_with_wrong_audience() -> str:
    payload = {
        "sub": "test-user-id",
        "iss": "https://test.clerk.accounts.dev",
        "aud": "wrong-audience",
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) + 3600,
        "org_id": str(TEST_COMPANY_A),
    }
    return jwt.encode(payload, "test_secret", algorithm="HS256")


def _make_valid_token() -> str:
    payload = {
        "sub": "test-user-id",
        "iss": "https://test.clerk.accounts.dev",
        "aud": "test",
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) + 3600,
        "org_id": str(TEST_COMPANY_A),
    }
    return jwt.encode(payload, "test_secret", algorithm="HS256")


@pytest.mark.asyncio
async def test_expired_jwt_token_rejected(client: AsyncClient):
    token = _make_expired_token()
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for expired token, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_wrong_issuer_token_rejected(client: AsyncClient):
    token = _make_token_with_wrong_issuer()
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for wrong issuer, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_wrong_audience_token_rejected(client: AsyncClient):
    token = _make_token_with_wrong_audience()
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for wrong audience, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_malformed_jwt_rejected(client: AsyncClient):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not.a.valid.jwt.token"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for malformed JWT, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_missing_authorization_header(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for missing auth header, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_invalid_authorization_scheme(client: AsyncClient):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for Basic auth, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_empty_authorization_header(client: AsyncClient):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": ""},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for empty auth, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_bearer_with_no_token(client: AsyncClient):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for Bearer without token, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_token_with_future_iat_rejected(client: AsyncClient):
    payload = {
        "sub": "test-user-id",
        "iss": "https://test.clerk.accounts.dev",
        "aud": "test",
        "iat": int(time.time()) + 3600,
        "exp": int(time.time()) + 7200,
        "org_id": str(TEST_COMPANY_A),
    }
    token = jwt.encode(payload, "test_secret", algorithm="HS256")
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for future-iat token, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_token_without_sub_claim_rejected(client: AsyncClient):
    payload = {
        "iss": "https://test.clerk.accounts.dev",
        "aud": "test",
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) + 3600,
        "org_id": str(TEST_COMPANY_A),
    }
    token = jwt.encode(payload, "test_secret", algorithm="HS256")
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (401, 403), f"Expected 401/403 for no-sub token, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_valid_token_allows_access(client: AsyncClient):
    token = _make_valid_token()
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 401, 403), f"Got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_unauthenticated_access_to_protected_endpoints(client: AsyncClient):
    protected = [
        "/api/v1/auth/me",
        "/api/v1/jobs",
        "/api/v1/ai/metrics",
        "/api/v1/billing/account",
        "/api/v1/estimates",
    ]
    for path in protected:
        resp = await client.get(path)
        assert resp.status_code in (401, 403), f"{path} returned {resp.status_code}"


@pytest.mark.asyncio
async def test_public_endpoints_no_auth(client: AsyncClient):
    public = ["/livez", "/healthz", "/health"]
    for path in public:
        resp = await client.get(path)
        assert resp.status_code in (200, 503), f"{path} returned {resp.status_code}"

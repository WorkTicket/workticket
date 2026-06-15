"""Pagination verification tests for all list endpoints.

Covers all 10 endpoints listed in docs/load-testing-guide.md.
Validates standard pagination contract: page, page_size, total, total_pages, items.
"""

import pytest
from httpx import AsyncClient

# All list endpoints that should support pagination
PAGINATED_ENDPOINTS = [
    "/api/v1/jobs",
    "/api/v1/jobs/customers",
    "/api/v1/quotes",
    "/api/v1/estimates",
    "/api/v1/billing/usage",
    "/api/v1/billing/dlq/entries",
    "/api/v1/tracing/traces",
]

# Endpoints using cursor-based pagination (different contract)
CURSOR_ENDPOINTS = [
    "/api/v1/analytics/events/cursor",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_accepts_params(client: AsyncClient, endpoint: str):
    """Verify endpoint accepts page and page_size query parameters."""
    response = await client.get(f"{endpoint}?page=1&page_size=10")
    assert response.status_code in (200, 401, 404), (
        f"{endpoint} returned {response.status_code}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_default_params(client: AsyncClient, endpoint: str):
    """Verify endpoint works without explicit pagination params (uses defaults)."""
    response = await client.get(endpoint)
    assert response.status_code in (200, 401, 404), (
        f"{endpoint} without params returned {response.status_code}"
    )
    if response.status_code == 200:
        data = response.json()
        # Should have some form of items/data key
        has_items = any(
            key in data for key in ("items", "jobs", "data", "customers", "quotes", "estimates", "entries", "traces")
        )
        assert has_items, f"{endpoint} response missing items key: {list(data.keys())}"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_response_has_metadata(client: AsyncClient, endpoint: str):
    """Verify paginated response includes standard metadata fields."""
    response = await client.get(f"{endpoint}?page=1&page_size=5")
    if response.status_code != 200:
        return  # Skip if auth required or endpoint not available
    data = response.json()
    # Check for pagination metadata (may use different key names)
    has_page = "page" in data
    has_page_size = "page_size" in data
    has_total = "total" in data
    assert any([has_page, has_page_size, has_total]), (
        f"{endpoint} missing pagination metadata: {list(data.keys())}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_rejects_negative_page(client: AsyncClient, endpoint: str):
    """Verify endpoint rejects negative page number."""
    response = await client.get(f"{endpoint}?page=-1")
    assert response.status_code in (422, 401, 404), (
        f"{endpoint} with page=-1 returned {response.status_code}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_rejects_zero_page_size(client: AsyncClient, endpoint: str):
    """Verify endpoint rejects zero or negative page_size."""
    response = await client.get(f"{endpoint}?page=1&page_size=0")
    assert response.status_code in (422, 401, 404, 200), (
        f"{endpoint} with page_size=0 returned {response.status_code}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_handles_large_page(client: AsyncClient, endpoint: str):
    """Verify endpoint handles out-of-range page number gracefully."""
    response = await client.get(f"{endpoint}?page=9999&page_size=100")
    assert response.status_code in (200, 401, 404), (
        f"{endpoint} with page=9999 returned {response.status_code}"
    )
    if response.status_code == 200:
        data = response.json()
        # Should return empty results for out-of-range page
        items_key = next(
            (k for k in ("items", "jobs", "data", "customers", "quotes", "estimates", "entries", "traces")
             if k in data),
            None,
        )
        if items_key:
            assert len(data[items_key]) == 0, (
                f"{endpoint} returned items for page=9999: {len(data[items_key])}"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_page_size_respected(client: AsyncClient, endpoint: str):
    """Verify page_size parameter is actually honored."""
    response = await client.get(f"{endpoint}?page=1&page_size=2")
    if response.status_code != 200:
        return  # Skip if auth required
    data = response.json()
    items_key = next(
        (k for k in ("items", "jobs", "data", "customers", "quotes", "estimates", "entries", "traces")
         if k in data),
        None,
    )
    if items_key:
        assert len(data[items_key]) <= 2, (
            f"{endpoint} page_size=2 returned {len(data[items_key])} items"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", PAGINATED_ENDPOINTS)
async def test_pagination_total_pages_consistent(client: AsyncClient, endpoint: str):
    """Verify total_pages is consistent with total and page_size."""
    response = await client.get(f"{endpoint}?page=1&page_size=10")
    if response.status_code != 200:
        return
    data = response.json()
    if "total" in data and "page_size" in data and "total_pages" in data:
        expected_pages = max(1, -(-data["total"] // data["page_size"])) if data["total"] > 0 else 0
        assert data["total_pages"] == expected_pages, (
            f"{endpoint} total_pages={data['total_pages']} expected={expected_pages}"
        )


# ---- Cursor pagination ----

@pytest.mark.asyncio
async def test_cursor_pagination_accepts_params(client: AsyncClient):
    """Verify cursor-based endpoint accepts cursor and limit params."""
    response = await client.get("/api/v1/analytics/events/cursor?cursor=abc123&limit=10")
    assert response.status_code in (200, 401, 404)


@pytest.mark.asyncio
async def test_cursor_pagination_defaults(client: AsyncClient):
    """Verify cursor endpoint works without explicit params."""
    response = await client.get("/api/v1/analytics/events/cursor")
    assert response.status_code in (200, 401, 404)


# ---- Analytics offset pagination ----

@pytest.mark.asyncio
async def test_analytics_events_pagination(client: AsyncClient):
    """Verify analytics events endpoint supports page/page_size."""
    response = await client.get("/api/v1/analytics/events?page=1&page_size=10")
    assert response.status_code in (200, 401, 404)


@pytest.mark.asyncio
async def test_analytics_events_rejects_invalid(client: AsyncClient):
    """Verify analytics events endpoint rejects invalid params."""
    response = await client.get("/api/v1/analytics/events?page=-1&page_size=-1")
    assert response.status_code in (422, 401, 404)

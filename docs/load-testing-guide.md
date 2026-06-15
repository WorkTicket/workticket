# Load Testing & Pagination Verification Guide

## Overview

This document covers load testing procedures and pagination verification
for all API list endpoints. Regular load testing ensures the system meets
latency SLOs and verifies pagination correctness at scale.

## Load Testing with Locust

### Quick Start
```bash
# Install locust
pip install locust

# Run locust with the existing load test file
cd src/scripts/load_test
locust -f locustfile.py --host=https://api.workticket.app

# Run headless with 100 users, 10 spawn rate, for 5 minutes
locust -f locustfile.py --host=https://api.workticket.app \
  --users=100 --spawn-rate=10 --run-time=5m --headless
```

### Load Test Scenarios

| Scenario | Users | Spawn Rate | Duration | Endpoints |
|----------|-------|------------|----------|-----------|
| Health check | 50 | 5/s | 5m | `/health`, `/livez`, `/readyz` |
| API read | 100 | 10/s | 10m | GET jobs, customers, quotes, estimates |
| AI processing | 20 | 2/s | 10m | POST `/api/v1/ai/process` |
| Mixed workload | 200 | 20/s | 30m | All endpoints |
| Adversarial | 500 | 50/s | 15m | Burst + error scenarios |

## Pagination Verification

### All List Endpoints with Pagination

| Endpoint | Params | Verified? |
|----------|--------|-----------|
| `GET /api/v1/jobs?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/jobs/customers?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/quotes?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/estimates?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/billing/usage?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/media/{job_id}?page=&page_size=` | page, page_size | N/A |
| `GET /api/v1/analytics/events?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/analytics/events/cursor?cursor=&limit=` | cursor, limit | ✅ |
| `GET /api/v1/billing/dlq/entries?page=&page_size=` | page, page_size | ✅ |
| `GET /api/v1/tracing/traces?page=&page_size=` | page, page_size | ✅ |

### Pagination Test Script

```python
"""Verify pagination works correctly on all list endpoints."""
import asyncio
import httpx

BASE_URL = "https://api.workticket.app/api/v1"
TOKEN = "<test-jwt-token>"

ENDPOINTS = [
    "/jobs",
    "/jobs/customers",
    "/quotes",
    "/estimates",
    "/billing/usage",
    "/billing/dlq/entries",
    "/tracing/traces",
]


async def test_pagination(endpoint: str):
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}

        # Test 1: page_size defaults
        resp = await client.get(f"{BASE_URL}{endpoint}", headers=headers)
        assert resp.status_code == 200, f"{endpoint} failed: {resp.status_code}"
        data = resp.json()
        assert "page" in data, f"{endpoint} missing page"
        assert "page_size" in data, f"{endpoint} missing page_size"
        assert "total" in data, f"{endpoint} missing total"

        # Test 2: Custom page_size
        resp = await client.get(
            f"{BASE_URL}{endpoint}", headers=headers,
            params={"page": 1, "page_size": 5},
        )
        assert resp.status_code == 200
        items = resp.json().get("items") or resp.json().get("data") or resp.json().get("media") or []
        assert len(items) <= 5, f"{endpoint} page_size not respected"

        # Test 3: Page beyond total
        resp = await client.get(
            f"{BASE_URL}{endpoint}", headers=headers,
            params={"page": 9999, "page_size": 100},
        )
        assert resp.status_code == 200
        assert len(items) == 0 or resp.json().get("total", 0) == 0, \
            f"{endpoint} should return empty for out-of-range page"

        # Test 4: Negative page (should error)
        resp = await client.get(
            f"{BASE_URL}{endpoint}", headers=headers,
            params={"page": -1},
        )
        assert resp.status_code == 422, f"{endpoint} should reject negative page"

        print(f"✅ {endpoint}: pagination verified")


async def main():
    for ep in ENDPOINTS:
        try:
            await test_pagination(ep)
        except Exception as e:
            print(f"❌ {ep}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
```

## SLO Targets

| Metric | Target | Measurement |
|--------|--------|-------------|
| P50 latency | < 200ms | Prometheus histogram |
| P95 latency | < 500ms | Prometheus histogram |
| P99 latency | < 2s | Prometheus histogram |
| Error rate | < 1% | Prometheus counter |
| Pagination response | < 5s at 10K rows | Load test |

## Continuous Load Testing

### CI/CD Integration
Add to `.github/workflows/load-test.yml`:
```yaml
name: Load Test
on:
  schedule:
    - cron: '0 6 * * 1'  # Every Monday at 6 AM
jobs:
  load-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install locust
      - run: locust -f src/scripts/load_test/locustfile.py
             --host=${{ secrets.LOAD_TEST_URL }}
             --users=100 --spawn-rate=10 --run-time=10m
             --headless --csv=results
      - uses: actions/upload-artifact@v4
        with:
          name: load-test-results
          path: results*.csv
```

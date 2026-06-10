# Operations Guide — Remaining Process/Policy Items

## 1. Staging Environment Setup

### Infrastructure
Use the base `docker-compose.yml` with a `.env.staging` file for staging-specific environment variables. Below is the staging service configuration for reference:
```yaml
# docker-compose.yml (staging overrides via .env.staging)
services:
  traefik:
    ports:
      - "443:443"
    labels:
      - "traefik.http.routers.api.rule=Host(`staging.example.com`)"

  postgres:
    environment:
      POSTGRES_DB: workticket_staging
    volumes:
      - pgdata_staging:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine

  backend:
    build:
      context: ./src/backend
    environment:
      DATABASE_URL: postgresql+asyncpg://user:pass@postgres:5432/workticket_staging
      REDIS_URL: redis://redis:6379/0
      ENVIRONMENT: staging
      LOG_LEVEL: DEBUG
      # all env vars from .env.staging
    depends_on:
      - postgres
      - redis

  celery_worker:
    build:
      context: ./src/backend
    command: celery -A app.celery_app worker --loglevel=info
    environment:
      <<: *backend-environment  # same as backend

  celery_beat:
    build:
      context: ./src/backend
    command: celery -A app.celery_app beat --loglevel=info

volumes:
  pgdata_staging:
```

### CI/CD — Deploy to Staging on PR Merge to `main`
```yaml
# .github/workflows/deploy.yml
on:
  push:
    branches: [main]

jobs:
  deploy-staging:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build and deploy
        run: |
          docker compose -f docker-compose.yml --env-file .env.staging build
          docker compose -f docker-compose.yml --env-file .env.staging up -d
      - name: Run smoke tests
        run: |
          curl -f https://staging.example.com/health
          curl -f https://staging.example.com/api/v1/health
```

### Required Secrets (`gh secret set`)
| Secret | Value |
|---|---|
| `STAGING_SSH_KEY` | SSH deploy key for staging host |
| `STAGING_HOST` | IP or DNS of staging server |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook secret (test mode) |
| `POSTHOG_API_KEY` | PostHog project API key |
| `DATABASE_URL` | PostgreSQL connection string for staging |
| `REDIS_URL` | Redis connection string |
| `SECRET_KEY` | Random 256-bit key (`python -c "import secrets; print(secrets.token_hex(32))"`) |

### Setup Steps (one-time)
1. Provision VM (minimum 2 vCPU, 4 GB RAM, 50 GB SSD)
2. Install Docker + Docker Compose plugin
3. Clone repo, checkout `main`
4. Create `.env.staging` from template with real secrets
5. Start stack: `docker compose -f docker-compose.yml --env-file .env.staging up -d`
6. Run migrations: `docker compose exec backend alembic upgrade head`
7. Verify health: `curl https://staging.example.com/health`

---

## 2. API Versioning Deprecation Policy

### Strategy: URL-Prefix Versioning (`/api/v1/`, `/api/v2/`)
- **Rationale**: Simplest to route, cache, and document. Header-based versioning hides version from operational tooling.

### Policy
| Phase | Action | Duration |
|---|---|---|
| **Announce** | Blog post + email + `Deprecation: v1` response header on all v1 endpoints | Day 0 |
| **Soft deprecation** | v1 still works, `Warning: 299 - "API v1 will be removed on YYYY-MM-DD"` header added | Day 0 → Day +90 |
| **Hard deprecation** | v1 returns `410 Gone` with JSON body `{"error": "API v1 removed. Use /api/v2/..."}` | Day +90 |
| **Removal** | Delete v1 router code, remove from tests | Day +90 |

### Implementation Pattern
```python
# app/api.py
from fastapi import APIRouter

v1_router = APIRouter(prefix="/api/v1")
v2_router = APIRouter(prefix="/api/v2")

# v1 endpoints — frozen, bug fixes only
v1_router.include_router(jobs_router)
v1_router.include_router(billing_router)

# v2 endpoints — active development
v2_router.include_router(jobs_router_v2)
v2_router.include_router(billing_router_v2)
```

### Deprecation Middleware
```python
# app/middleware/deprecation.py
from datetime import date, timedelta

REMOVAL_DATE = date(2026, 9, 1)  # 90 days from soft launch

async def deprecation_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/v1/"):
        days_left = (REMOVAL_DATE - date.today()).days
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = REMOVAL_DATE.isoformat()
        if days_left <= 30:
            response.headers["Warning"] = f'299 - "API v1 removed {REMOVAL_DATE}. Migrate to /api/v2/"'
    return response
```

---

## 3. PostHog Key Provisioning for Production

### Step-by-Step
1. **Create PostHog account** at https://app.posthog.com/signup (or self-host)
2. **Create a project** named "WorkTicket (Production)"
3. **Copy the Project API Key** from Project Settings → Project API Key
4. **Set the env var** in production:
   ```bash
   POSTHOG_API_KEY=phc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
5. **Verify** — check `/health` response includes `"posthog": "connected"` (or `"disabled"`)

### Key Management
| Environment | PostHog Project | Key Type | Notes |
|---|---|---|---|
| Local dev | WorkTicket (Dev) | Local eval | Disabled by default (no key set) |
| Staging | WorkTicket (Staging) | Local eval | Test events, no billing impact |
| Production | WorkTicket (Production) | Cloud | Real user events, billable |

### Cost Estimate
- Free tier: 1M events/month
- Paid (Growth): ~$0.00045/event beyond free tier
- Expected: ~50k events/month at beta scale → **Free**

### Self-Hosted Alternative (if compliance requires)
```yaml
# docker-compose.override.yml — add PostHog services
services:
  posthog:
    image: posthog/posthog:latest
    environment:
      SECRET_KEY: ${POSTHOG_SECRET_KEY}
      POSTHOG_REDIS_HOST: redis
      POSTHOG_DB: postgres
    depends_on:
      - postgres
      - redis
```
Not recommended at current scale — use PostHog Cloud until >500k events/month.

---

## 4. Price-per-Plan Config for Each Customer

### The `stripe_price_map` Env Var
The code reads a JSON string from the `STRIPE_PRICE_MAP` env var:
```json
{
  "free":      "price_1ABC...",
  "starter":   "price_2DEF...",
  "pro":       "price_3GHI...",
  "enterprise":"price_4JKL..."
}
```

### Setup for Each Plan
1. **Log into Stripe Dashboard** → Products → Add Product
2. **Create a product** per plan (Free, Starter, Pro, Enterprise)
3. **Add a recurring price** to each product (monthly billing)
4. **Copy the Price ID** (starts with `price_`) from each product
5. **Set the env var**:
   ```bash
   STRIPE_PRICE_MAP='{"free":"price_1ABC...","starter":"price_2DEF...","pro":"price_3GHI...","enterprise":"price_4JKL..."}'
   ```

### Fallback Behavior
- If `STRIPE_PRICE_MAP` is not set, the code falls back to `STRIPE_PRICE_ID` (single price for all plans)
- If a plan name is not found in the map, checkout returns `422 Unprocessable Entity` with `{"detail": "No price configured for plan: <plan>"}`

### Testing
```bash
# Verify the map parses correctly
python -c "
import json, os
m = json.loads(os.environ['STRIPE_PRICE_MAP'])
assert 'free' in m, 'free plan missing'
assert 'starter' in m, 'starter plan missing'
assert 'pro' in m, 'pro plan missing'
assert 'enterprise' in m, 'enterprise plan missing'
for k, v in m.items():
    assert v.startswith('price_'), f'{k} has invalid price ID: {v}'
print('OK')
"
```

---

## 5. Worker Prefetch Tuning Per Deployment

### The Setting
Env var `CELERY_WORKER_PREFETCH_MULTIPLIER` — controls how many tasks a worker prefetches at once.

| Deployment | Recommended Value | Rationale |
|---|---|---|
| Local dev | 1 | Minimal concurrency, easier debugging |
| Staging | 2 | Simulate production load |
| Production (CPU-bound) | 1 | One task at a time per worker, fair scheduling |
| Production (IO-bound) | 4-8 | Maximize throughput while tasks wait for DB/API |

### How to Tune
1. **Monitor task latency** via `/health` → `celery_queue_depth`
2. **Monitor worker utilization**:
   ```bash
   celery -A app.celery_app inspect active
   celery -A app.celery_app inspect reserved
   ```
3. **Rule of thumb**:
   - If tasks are CPU-heavy (AI inference): `prefetch_multiplier = 1`
   - If tasks are IO-heavy (file uploads, notifications): `prefetch_multiplier = 4`
    - If tasks are mixed: start at `2`, increase until CPU > 70% or latency rises

### Testing in Staging
```bash
# Test different values in staging
for val in 1 2 4 6 8; do
  CELERY_WORKER_PREFETCH_MULTIPLIER=$val celery -A app.celery_app worker --loglevel=info &
  # ... run load test ...
  # Record avg task latency & throughput
  kill %1
done
```

### Per-Deployment Config Files
```yaml
# docker-compose.override.yml
services:
  celery_worker:
    environment:
      CELERY_WORKER_PREFETCH_MULTIPLIER: "2"  # staging
```

---

## 6. Deploy Sequencing

### Critical: Stop → Deploy → Migrate → Start

When deploying a new version of the application, the order of operations matters for zero-downtime:

```
1. Stop Celery Beat       → docker compose stop celery-beat
2. Stop Celery Workers    → docker compose stop celery-worker-default celery-worker-text celery-worker-image
3. Deploy API (backend)   → docker compose up -d backend
4. Run DB Migrations      → docker compose exec backend alembic upgrade head
5. Start Celery Workers   → docker compose up -d celery-worker-default celery-worker-text celery-worker-image
6. Start Celery Beat      → docker compose up -d celery-beat
```

### Why This Order
- **Stop Beat first**: Prevents beat from scheduling tasks that old workers can't process.
- **Stop Workers**: Drains in-flight tasks gracefully (due to `task_acks_late=True`). Wait for running tasks to complete or visibility timeout.
- **Deploy API**: New API code is live for HTTP requests.
- **Migrate**: Schema changes run against the database before new workers start.
- **Start Workers**: Fresh workers pick up new tasks with the new code.
- **Start Beat**: Scheduler begins scheduling tasks again.

### Version Skew Safety
If workers from different versions run simultaneously:
- Tasks have a `payload_version` field checked at runtime
- Old workers reject tasks with `payload_version > MAX_SUPPORTED_VERSION`
- New workers reject tasks with `payload_version < MIN_SUPPORTED_VERSION`
- Rejected tasks return `failure_type: "version_mismatch"` and are NOT sent to DLQ
- Tasks automatically re-queue with `self.reject()` + `self.retry(countdown=300)`

### Rollback Procedure
```bash
# 1. Tag the previous image
docker tag workticket-backend:previous workticket-backend:latest

# 2. Follow deploy sequencing above
docker compose stop celery-beat
docker compose stop celery-worker-default celery-worker-text celery-worker-image
docker compose up -d backend
docker compose exec backend alembic downgrade -1  # if schema was changed
docker compose up -d celery-worker-default celery-worker-text celery-worker-image
docker compose up -d celery-beat
```

---

## 7. Redis High-Availability with Sentinel

### Overview
Production deployments MUST use Redis Sentinel for HA. A single Redis instance is a single point of failure — if it crashes or runs out of memory, all Celery task processing, rate limiting, WebSocket pub/sub, and concurrency locking are immediately impacted.

### Architecture
```
3x Sentinel nodes (monitor, quorum=2)
    |
    +-- redis-broker (primary + 2 replicas)  ← Celery broker + result backend
    |
    +-- redis-cache  (primary + 2 replicas)  ← Rate limiting, WebSocket, concurrency
```

### Deployment
```bash
# Deploy with Redis HA override:
docker compose -f docker-compose.yml -f docker-compose.redis-ha.yml up -d

# Remove standalone redis services from base compose when using HA:
# Edit docker-compose.yml and comment out redis-broker and redis-cache services
```

### Connection URLs
| Component | Standard URL | Sentinel URL |
|-----------|-------------|--------------|
| Broker | `redis://:pass@redis-broker:6379/0` | `sentinel://:pass@sentinel-1:26379,sentinel-2:26379,sentinel-3:26379/redis-broker` |
| Cache | `redis://:pass@redis-cache:6379/0` | `sentinel://:pass@sentinel-1:26379,sentinel-2:26379,sentinel-3:26379/redis-cache` |

### Env Vars for Sentinel
```bash
REDIS_SENTINEL_MASTER_NAME=redis-broker     # Must match Sentinel config
REDIS_SENTINEL_HOSTS=sentinel-1:26379,sentinel-2:26379,sentinel-3:26379
REDIS_SENTINEL_PASSWORD=<strong-password>    # Optional, can reuse REDIS_PASSWORD
```

### Broker maxmemory Policy
```bash
# Broker Redis must use noeviction to prevent task loss:
redis-broker: --maxmemory-policy noeviction

# Cache Redis can use allkeys-lru for rate limiter state:
redis-cache:  --maxmemory-policy allkeys-lru
```

The `docker-compose.redis-ha.yml` file in `src/` configures the full Sentinel deployment.

---

## 8. Database Migration 019 — AIOutput Composite Index

### Purpose
Adds a composite index on `ai_outputs(company_id, job_id, created_at DESC)` to optimize WebSocket polling queries.

### When to Run
Migration 019 (`019_add_ai_output_company_job_created_index`) must be run during the **next deployment** after the code changes from the production readiness audit are deployed.

### Deployment Order
```
1. Deploy new backend code  (includes AIOutput index in model definition)
2. Run migration: alembic upgrade head   ← This creates the index
```

### Verification
```sql
-- Confirm the index exists
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ai_outputs';
-- Expected: ix_ai_outputs_company_job_created
```

### Rollback
```bash
docker compose exec backend alembic downgrade -1
# This drops the index (revision 018 is the parent)
```

---

## Setup Checklist Updates

### WS_ENABLED=true
WebSocket endpoint is **enabled by default** (`WS_ENABLED=true`). Ensure the env var
is not overridden to `false` in production. See `ops/runbooks/ws-disabled.md` if
WebSocket features are unavailable.

### Redis OOM Prevention
- Broker Redis maxmemory: **1GB** (was 200MB) — see `docker-compose.yml`
- Cache Redis maxmemory: 200MB (unchanged, uses allkeys-lru policy)
- Alert: `RedisMemoryPressure` fires at 80% usage
- See `ops/runbooks/redis-oom.md`

### Docker Volume Requirements
- DLQ fallback files: PID-named files in `/var/log/workticket/dlq_fallback/`
- Use a dedicated Docker volume or bind mount with at least 500MB quota
- Recommended: `tmpfs` mount with `size=500MB` for DLQ fallback
- File rotation at 100MB per file with timestamp suffix

### DB Pool Sizing
- Application pool_size: **25** (was 10), max_overflow: 10
- PgBouncer default_pool_size: **35** (was 10)
- Celery pool_size: 10, Beat pool_size: 5
- See `app/config.py` for defaults

### DB Circuit Breaker
- Exponential backoff: 30s → 60s → 120s → 300s (capped)
- Half-open state: allows exactly 1 probe request on cooldown expiry
- Jitter: base_cooldown + random(0, 0.25 * base_cooldown)
- Metric: `workticket_db_circuit_cooldown_seconds`
- See `app/database.py`

### Beat Lock TTL with Heartbeat
- Renewable beat lock in `app/tasks/beat_lock.py`
- Background thread refreshes TTL every `base_ttl/3` seconds
- Lua-based atomic release
- Metrics: `workticket_beat_lock_ttl_renewed_total`, `workticket_beat_lock_contention_total`

### Concurrency Counter (Atomic DECR)
- Lua script replaces GET+DECR pattern to prevent over-release
- Returns -1 for missing key, 0 after cleanup, or decremented count
- Alert: `ConcurrencyCounterDrift` fires if negative values detected
- See `app/billing/concurrency.py`

### Stripe Webhook Dedup
- Redis-based dedup (primary): `SET stripe:dedup:{event_id} 1 NX EX 60`
- PG dedup table (secondary audit trail): written AFTER successful processing
- Metrics: `workticket_stripe_dedup_redis_hit`, `workticket_stripe_dedup_redis_miss`
- See `app/billing/router.py`

### DLQ Fallback Files
- PID-named per-worker files: `workticket_dlq_fallback.{hostname}.{pid}.jsonl`
- Collector beat task merges PID files into main file
- File rotation at 100MB with timestamp suffix
- Alert: `DLQFallbackFileGrowing` at 50MB
- See `celery_app.py`, `app/billing/tasks.py`

---

## 10. Redis Lock Key Schema

All Redis keys used for distributed locking and coordination across replicas.

### Job Processing Lock (`C2`)
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `job:lock:{job_id}` | String (SET NX) | 300s | Prevents concurrent processing of same job across workers. Released before DB commit on success, deleted on retry. |

### WebSocket Connection Tracking (`C4`)
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `ws_conn:{user_id}` | Sorted Set (ZADD) | `_WS_CONNECT_WINDOW + 1` (default 61s) | Tracks active WS connections per user using timestamp scores. Lua trim+add+count for atomicity. Members: `{uuid}:{timestamp}` |

### DB Circuit Breaker (`H1`)
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `db:circuit:breaker` | Hash | `max_cooldown * 2` (600s) | Global circuit breaker state across all replicas. Fields: `open`, `level`, `cooldown`, `last_failure`, `half_open`, `half_open_probed` |

### WebSocket DB Poll Rate Limiter (`M1`)
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `ws:db_poll_global` | Sorted Set (ZADD) | 120s | Global rate limit for WS DB polling across all replicas. Members: `poll:{uuid}:{timestamp}` |

### Worker Crash Loop Detection
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `worker:heartbeat:{worker_name}` | String | 30s | Worker heartbeat timestamp, refreshed every beat cycle |
| `worker:crash_count` | String (INCR) | 300s | Crash event counter, reset when workers recover |

### Beat Lock Keys
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `beat:lock:{task_name}` | String (SET NX) | Per-task (120-86400s) | Prevents concurrent execution of beat tasks across replicas |

### Retry Storm Guard
| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `retry:{task_name}:{job_id}` | String (INCR) | 300s | Per-job retry count, blocks retry storms >5 in 5min window |

### Cleanup Policy
- All keys have explicit TTLs and auto-expire
- No manual cleanup needed under normal operation
- On Redis OOM with `allkeys-lru` on cache Redis, these keys are safe to evict (rate limiters, WS tracking). Job locks (`job:lock:*`) and circuit breaker (`db:circuit:breaker`) use broker Redis with `noeviction`.

---

## 11. Prometheus Alerting Rules

See `ops/prometheus_alerts.yml` for the full alert rule file. Deploy as a PrometheusRule CRD in Kubernetes or add to your Prometheus server config.

### Critical Alerts
| Alert Name | Condition | Severity | Description |
|---|---|---|---|
| `WorkTicketDroppedTasks` | `workticket_dropped_tasks_gap > 5` | critical | Jobs created but not completed — silent queue death |
| `WorkTicketNoWorkers` | `workticket_worker_crash_loops_detected == 1` | critical | All Celery workers may be crash-looping |
| `WorkTicketStuckJobs` | `workticket_stuck_jobs_processing_gauge > 0` | warning | Jobs stuck in processing for >5min |

### Queue Depth Alerts
| Alert Name | Condition | Severity | Description |
|---|---|---|---|
| `WorkTicketQueueDepthGrowing` | Rate of queue depth increase > 10/min | warning | Tasks accumulating faster than workers can process |
| `WorkTicketQueueStuck` | Queue depth unchanged for >5min with no active workers | warning | No workers consuming from queue |

### Error Rate Alerts
| Alert Name | Condition | Severity | Description |
|---|---|---|---|
| `WorkTicketDLQSpike` | Rate of DLQ writes > 5/min | warning | Sudden increase in dead letter queue entries |
| `WorkTicketStripeWebhookErrors` | Rate of 409/400 from webhooks > 3/min | warning | Stripe webhook processing issues |
| `WorkTicketHMACRejection` | `workticket_unsigned_task_rejected_total` rate > 0 | warning | Tasks being rejected due to signing key mismatch |

### Retry Storm Alerts
| Alert Name | Condition | Severity | Description |
|---|---|---|---|
| `WorkTicketRetryStorm` | `workticket_retry_guard_active_storms > 0` | warning | Active retry storms detected |

---

## 12. Multi-Worker Rate Limiter Behavior

### Architecture
The rate limiter has two layers:
1. **Redis-backed (primary)**: Uses Redis token buckets with Lua scripts for atomic operations
2. **In-memory fallback (local)**: Activates when Redis is unavailable, uses conservative per-worker limits

### Redis-Available Mode
When Redis is online, the rate limiter (`/health` shows `mode: redis`) uses a global token bucket
that is shared across all workers. Rate limits apply globally.

### Redis-Failure Mode (Local Fallback)
When Redis is unavailable, the rate limiter falls back to in-memory token buckets.
Each worker independently enforces rate limits by dividing the configured limit by
`_ESTIMATED_WORKERS` (default: 4). For example, a 10/s limit becomes 2.5/s per worker.

### Implications for Multi-Worker Deployments
| Workers | Per-Worker Limit (10/s global) | Effective Global Limit |
|---------|-------------------------------|----------------------|
| 1 | 10/s | 10/s |
| 2 | 5/s | 10/s |
| 4 | 2.5/s | 10/s |
| 8 | 1.25/s | 8/s (conservative) |

The per-worker division is intentionally conservative — when fewer workers are running than
`_ESTIMATED_WORKERS`, the effective global limit is lower than configured. This is safer than
allowing higher throughput during Redis outages.

### Configuration
```bash
# Override the estimated worker count (default: 4)
ESTIMATED_WORKERS=8
```

### Monitoring
- Prometheus gauge: `workticket_rate_limiter_fallback_active` (1=local mode, 0=redis mode)
- Health endpoint: `/health` → `rate_limiter.mode`: "redis" or "local"
- Readiness: `/readyz` → `rate_limiter.status`: degraded in fallback mode

### Alerting
- `RateLimiterFallbackActive` alert fires when local mode persists > 5 minutes
- `RateLimiterRedisUnavailable` alert fires when Redis health check fails

---

## 13. Synthetic Monitoring Probe

See `ops/synthetic_monitor.py` for the standalone probe script. Deploy as a CronJob in Kubernetes (every 5 minutes) or run as a systemd timer.

### What It Does
1. Creates a test job via the API (`POST /api/v1/jobs`)
2. Polls the job status until completed or timeout (5 min)
3. Verifies the job reached a terminal state (completed or failed)
4. Reports success/failure metrics via Prometheus push gateway or a dedicated endpoint

### Key Metrics Exported
| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `synthetic_job_creation_duration_ms` | Gauge | `status` | Time to create a job |
| `synthetic_job_completion_duration_ms` | Gauge | `status` | Time from creation to terminal state |
| `synthetic_job_success_total` | Counter | - | Total successful probe cycles |
| `synthetic_job_failure_total` | Counter | `reason` | Total failed probe cycles |
| `synthetic_api_health` | Gauge | `endpoint` | Per-endpoint health (1=ok, 0=failed) |

### Setup
```bash
# Required env vars:
export BASE_URL=http://localhost:8000
export API_KEY=<api-key-for-test-user>
export PROMETHEUS_PUSH_GATEWAY=http://pushgateway:9091  # optional

# Run once:
python ops/synthetic_monitor.py

# Run as systemd timer (every 5 min):
# See ops/systemd/synthetic-monitor.* for unit files
```

---

## 14. Rollback Drill — Audit Fixes

See `ops/rollback-drill.md` for the complete rollback procedure for the production readiness audit (fixes C1-C5, H1-H7, M1-M7).

### Quick Reference
| Fix | Rollback Action | Risk |
|-----|----------------|------|
| C1 (Event loop) | Revert `_run_async` to `asyncio.run()` | Low — old code works but may crash on retry |
| C2 (Redis lock) | Remove `job:lock:*` key and `SET NX` calls | Medium — race on retry without lock |
| H1 (Circuit breaker) | Delete `db:circuit:breaker` Redis key | Low — falls back to process-local state |
| H4 (nowait) | Revert to `skip_locked=True` | Low — returns 400 instead of 409 |
| H7 (HMAC reject) | Revert to `return {"status": "failed"}` | Low — returns to infinite redelivery |
| All Redis keys | `redis-cli KEYS 'job:lock:*' \| xargs redis-cli DEL` | Safe — deletes orphaned locks |

---

## 15. Disaster Recovery Objectives

### Recovery Time Objective (RTO)
| Tier | Target | Details |
|------|--------|---------|
| Critical (User auth, API) | < 15 minutes | Automatic failover via orchestration |
| Important (Background jobs) | < 1 hour | Worker redeployment + queue replay |
| Batch (Analytics, audit) | < 4 hours | Can be rebuilt from event logs |

### Recovery Point Objective (RPO)
| Data | Target | Mechanism |
|------|--------|-----------|
| PostgreSQL (core data) | < 5 minutes | WAL streaming + PgBouncer transaction mode |
| Redis (broker) | < 0 (at-most-once) | Tasks re-deliverable from upstream |
| Redis (cache) | < 0 (loss acceptable) | Rebuildable from DB |
| R2 (file storage) | < 0 (11x9s durability) | S3-compatible, no data loss expected |

### Cross-Region Disaster Recovery
| Scenario | Strategy | Status |
|----------|----------|--------|
| Single AZ failure | Kubernetes pod rescheduling + PVC reattach | ✅ Auto (K8s) |
| Entire region loss | Warm standby in secondary region | ⚠️ Planned — requires multi-region K8s + DB replication |
| Data corruption | Point-in-time recovery from WAL archives | ✅ `test_backup_restore.py` validates |
| R2 region failure | Automatic S3 failover via Cloudflare | ✅ Managed by Cloudflare |

### Recovery Procedures
1. **Database loss**: Restore from WAL archive using `ops/restore_from_backup.sh`
2. **Redis data loss**: Cache clears automatically; broker queue re-populates from retry
3. **Full region failover**: Update DNS, promote read replica, redeploy in secondary region (~30min RTO)

---

## 16. On-Call Procedures

### Escalation Path
| Level | Responder | Response Time | Coverage |
|-------|-----------|---------------|----------|
| L1 | Platform Engineer | < 15 min (business hours), < 1 hour (after hours) | All P0/P1 alerts |
| L2 | Backend Lead | < 30 min | L1-escalated incidents |
| L3 | Engineering Manager | < 1 hour | Cross-team incidents, customer communication |

### Incident Response Flow
```
Alert fires → L1 acknowledges (5 min) → Triage severity →
  P0: Customer-facing outage → Page L1 immediately
  P1: Degraded experience → Page L1 within 15 min
  P2: Non-critical bug → Next business day
  P3: Cosmetic / enhancement → Sprint backlog
```

### Communication Channels
| Channel | Purpose | Tool |
|---------|---------|------|
| Critical alerts | P0/P1 incident paging | PagerDuty / OpsGenie |
| Team chat | Incident coordination | Slack #workticket-incidents |
| Status page | Customer-facing status | status.workticket.app |

### Handoff Checklist
- [ ] Incident summary written in runbook
- [ ] Root cause identified and documented
- [ ] Fix deployed (or workaround in place)
- [ ] Monitoring confirms recovery
- [ ] Post-mortem scheduled (within 48 hours for P0)

---

## 17. Data Retention Policy

| Data Category | Retention | Deletion Action | Configurable |
|---------------|-----------|-----------------|--------------|
| Analytics events | 365 days | Hard delete | `ANALYTICS_RETENTION_DAYS` |
| AI audit logs | 90 days | Hard delete | `AUDIT_LOG_RETENTION_DAYS` |
| Execution traces | 30 days | Hard delete | `TRACE_RETENTION_DAYS` |
| AI outputs | 365 days | Soft delete | `AI_OUTPUT_RETENTION_DAYS` |
| Dead letter queue | 30 days | Hard delete | `DLQ_RETENTION_DAYS` |
| User accounts | Indefinite | Anonymized on GDPR delete | Not configurable |

Policies are enforced at startup via `app/db/retention.py` cleanup tasks. Override via environment variables.

---

## 18. Clerk Outage Contingency Plan

### Impact
When Clerk (external auth provider) is unavailable:
- **No new logins** — JWT verification via cached JWKS keys continues for 1 hour
- **No new registrations** — Clerk-hosted UI is unreachable
- **Existing sessions** — Continue working until JWT expires (1 hour default)

### Mitigation
1. **JWKS cache**: Signing keys are cached in Redis with 1-hour TTL; cached keys continue to verify existing JWTs even if Clerk is down
2. **Fallback mode**: Feature flag `auth_bypass` can be enabled for emergency access (requires admin intervention)
3. **Session blacklist**: Managed in Redis — unaffected by Clerk outage

### Recovery Steps
1. Verify Clerk status at https://status.clerk.com
2. If outage < 1 hour: No action needed — existing sessions continue
3. If outage > 1 hour: Consider extending `CLERK_JWKS_CACHE_TTL` or enabling fallback mode
4. **Post-incident**: Rotate Clerk webhook secrets and increment `token_version` for all users

---

## 19. Webhook Unavailability During Scheduled Maintenance

### Stripe Webhooks
Stripe retries failed webhook delivery for up to 3 days with exponential backoff:
- Retry 1: 5 minutes
- Retry 2: 15 minutes
- Retry 3: 1 hour
- Retry 4+: up to 24 hours apart

During maintenance windows, Stripe webhooks can be safely ignored — they will be retried automatically.

### Idempotency Safety
Webhook idempotency is guaranteed by a triple-layer system:
1. **Redis dedup** (`stripe:dedup:{event_id}`, 60s TTL) — prevents duplicate processing within the retry window
2. **PG dedup table** (`ON CONFLICT DO NOTHING`) — prevents duplicate writes across long windows
3. **Billing period validation** — prevents double-counting within a billing period


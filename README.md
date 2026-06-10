# WorkTicket

> Job management software for skilled trades businesses.

WorkTicket helps contractors manage the full job lifecycle — from first customer contact to final invoice. Built mobile-first for technicians in the field, with a web dashboard for office staff.

[![License](https://img.shields.io/badge/license-Proprietary-blue)](./LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0--beta.1-blue)]()
[![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688)](https://fastapi.tiangolo.com)
[![React Native](https://img.shields.io/badge/mobile-React%20Native-61DAFB)](https://reactnative.dev)
[![Next.js](https://img.shields.io/badge/dashboard-Next.js-000000)](https://nextjs.org)

---

## Overview

Most contractor software is built for office managers, not technicians. WorkTicket is designed around how trade businesses actually work in the field.

| Capability | Description |
|---|---|
| **Jobs & Customers** | Customer records, job creation, status tracking, activity history |
| **Estimates & Quotes** | Material and labor tracking, quote generation, approval workflows |
| **Media** | Photo uploads, voice recordings, organized cloud storage |
| **Billing** | Stripe Checkout, billing portal, subscription management |
| **Offline Support** | Mobile-first architecture with local persistence, auto-sync |

**v1.0.0-beta.1 is a manual-first release.** AI features are postponed and disabled by default to focus on core workflow stability, customer acquisition, and product-market fit. See [docs/future-ai/](./docs/future-ai/) for the AI roadmap.

---

## Architecture

| Layer | Technology |
|---|---|
| Mobile | React Native, Expo, Zustand, TanStack Query |
| Dashboard | Next.js, Tailwind CSS, shadcn/ui |
| Backend | FastAPI, SQLAlchemy (async), Alembic, Pydantic |
| Task Queue | Celery + Redis |
| Database | PostgreSQL 16 + pgvector |
| Auth | Clerk (JWT) |
| Storage | Cloudflare R2 |
| Payments | Stripe (Checkout, Billing Portal, Webhooks) |
| Notifications | Twilio (SMS), Resend (email) |
| Monitoring | Sentry, PostHog, Prometheus, Loki |
| Dev | Docker, Docker Compose |

---

## Getting Started

### Docker (Required)

**Prerequisites:** Docker and Docker Compose.

Docker is the sole supported development and deployment environment.

**Recommended:** Run the bootstrap script for first-time setup:
- **Windows:** `.\bootstrap.ps1`
- **Linux/Mac:** `./bootstrap.sh`

```bash
# 1. Clone the repository
git clone https://github.com/WorkTicket/workticket.git
cd workticket

# 2. Configure environment
cd src
cp .env.example .env
# Edit .env — required variables:
#   POSTGRES_PASSWORD, REDIS_PASSWORD, CELERY_TASK_SIGNING_KEY

# Generate a signing key:
openssl rand -hex 32

# 3. Start all services
docker compose up -d

# 4. Run migrations
docker compose exec backend alembic upgrade head

# 5. Verify
curl http://localhost:8000/health
# Interactive API docs → http://localhost:8000/docs
```

The web dashboard is available at `http://localhost:3000` and the Expo mobile app at `http://localhost:8081`.

---

## Core Workflow

```
Technician creates job
  → adds customer details, description, media (photos, voice notes)
  → builds estimate with materials and labor
  → generates quote for customer approval
  → sends quote via email or SMS
  → tracks job status through completion
```

All workflows are fully manual in v1. Every estimate, quote, and customer communication is created and approved by a human.

---

## Project Structure

```
WorkTicket/
├── src/
│   ├── backend/                 # FastAPI API server
│   │   ├── app/
│   │   │   ├── ai/              # AI module (disabled, preserved for future)
│   │   │   ├── auth/            # Clerk JWT auth, RBAC
│   │   │   ├── billing/         # Stripe, quotas, ACU tracking
│   │   │   ├── estimates/       # Estimate generation and pricing
│   │   │   ├── jobs/            # Job and customer CRUD
│   │   │   ├── media/           # R2 upload pipeline and thumbnails
│   │   │   ├── notifications/   # Push tokens, Expo push, email, SMS
│   │   │   └── quotes/          # Quote generation, approval, sending
│   │   ├── alembic/             # Database migrations (38 revisions)
│   │   ├── tasks/               # Celery tasks
│   │   └── tests/
│   ├── mobile-app/              # React Native (Expo) — field technician app
│   │   └── src/
│   ├── web-dashboard/           # Next.js admin dashboard
│   ├── whisper-service/         # Speech-to-text microservice (AI profile)
│   ├── nginx/                   # Reverse proxy
│   ├── k8s/                     # Kubernetes manifests
│   ├── marketing-website/       # Public marketing site (Next.js)
│   ├── scripts/                 # Dev scripts, entrypoints, load tests
│   └── docker-compose.yml       # Main deployment
├── chaos/                       # Chaos engineering tests
├── docs/
│   ├── adr/                     # Architecture Decision Records
│   ├── future-ai/               # AI documentation (postponed)
│   ├── architecture/            # Architecture docs
│   └── runbooks/                # Incident response playbooks
├── ops/                         # Operations configs
│   ├── k8s/                     # K8s operational configs
│   ├── grafana-dashboards/      # Monitoring dashboards
│   ├── prometheus-alerts/       # Alerting rules
│   └── runbooks/                # Ops-specific runbooks
├── .github/                     # CI/CD workflows, issue templates
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
├── CHANGELOG.md
├── LICENSE
└── ops-guide.md
```

---

## Documentation

- **[ops-guide.md](./ops-guide.md)** — Deploy sequencing, Redis Sentinel HA, staging, runbooks index
- **[docs/runbooks/](./docs/runbooks/)** — Incident response playbooks (16 runbooks)
- **[docs/adr/](./docs/adr/)** — Architecture Decision Records (11 records)
- **[docs/future-ai/](./docs/future-ai/)** — AI feature documentation, reactivation guide, roadmap
- **[docs/architecture/](./docs/architecture/)** — System architecture overview

---

## Known Limitations

WorkTicket is currently in **beta (v1.0.0-beta.1)**. Items tracked for GA:

| Issue | Notes |
|---|---|
| Push token `company_id` stored as `varchar` — no FK constraint | Safe for beta; tightened in next migration |
| No soft-delete — hard deletes throughout | Jobs with quotes must have quotes removed first |
| In-memory rate limiter fallback if Redis unavailable | Single-worker deployments only |
| No pagination on analytics endpoints | Acceptable at beta scale |
| No user deactivation — hard-delete only | No cascade cleanup |
| AI features disabled — postponed to post-MVP | See [docs/future-ai/](./docs/future-ai/) |

Already verified: multi-tenant isolation, JWT auth on all endpoints, idempotency on webhooks, circuit breakers, rate limiters, indexes on hot paths, comprehensive test coverage.

---

## Contributing

Please read [CONTRIBUTING.md](./CONTRIBUTING.md) for details on our code of conduct, development process, and how to get started.

---

## Security

Found a vulnerability? Please see [SECURITY.md](./SECURITY.md) for our disclosure policy.

- **Report:** security@workticket.dev
- **Response time:** Within 48 hours
- **Supported versions:** 1.0.x (beta)

---

## Changelog

See [CHANGELOG.md](./CHANGELOG.md) for the release history.

---

## License

[Proprietary](./LICENSE) — Copyright (c) 2026 WorkTicket. All rights reserved.

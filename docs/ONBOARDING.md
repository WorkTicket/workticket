# Developer Onboarding Guide

## Quick Start (First 30 Minutes)

### Prerequisites
- Docker Desktop 4.25+
- Git 2.40+
- Node.js 20 LTS
- Python 3.12+
- PowerShell 7+ (Windows) or Bash (macOS/Linux)

### Step 1: Clone & Bootstrap (10 min)

```bash
# Clone the repository
git clone https://github.com/WorkTicket/workticket.git
cd workticket

# Run the bootstrap script
# Windows:
.\bootstrap.ps1
# macOS/Linux:
./bootstrap.sh

# Copy environment templates
cp .env.dev.example .env
cp src/.env.example src/.env
cp src/backend/.env.example src/backend/.env
```

### Step 2: Start the Stack (10 min)

```bash
cd src
docker compose up -d
```

Wait for all services to be healthy (check with `docker compose ps`).

### Step 3: Verify (5 min)

```bash
# Health check
curl http://localhost:8000/health | python -m json.tool

# Run the backend test suite
cd src/backend
pip install -r requirements.txt
pytest tests/ -v

# Run the dashboard
cd src/web-dashboard
npm install
npm run dev  # Opens http://localhost:3000
```

### Step 4: Explore (5 min)

| Component | URL / Command |
|-----------|---------------|
| Backend API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| API ReDoc | http://localhost:8000/redoc |
| Web Dashboard | http://localhost:3000 |
| Mobile App (Expo) | `cd src/mobile-app && npm start` |
| Direct PostgreSQL | `psql -h localhost -U postgres -d workticket` |

## Project Structure

```
workticket/
├── src/
│   ├── backend/           # FastAPI + Celery (Python 3.12)
│   │   ├── app/           # Application modules (ai, billing, jobs, etc.)
│   │   ├── alembic/       # Database migrations
│   │   ├── tests/         # Backend tests
│   │   └── tasks/         # Celery task definitions
│   ├── web-dashboard/     # Next.js 15 (TypeScript)
│   ├── mobile-app/        # React Native / Expo (TypeScript)
│   ├── whisper-service/   # ASR service (FastAPI)
│   ├── nginx/             # Reverse proxy + TLS
│   ├── docker-compose.yml # Main compose
│   └── k8s/               # Kubernetes manifests
├── .github/               # CI/CD workflows
├── ops/                   # Operations (Terraform, ArgoCD, dashboards)
├── chaos/                 # Chaos engineering tests
└── docs/                  # Architecture docs, ADRs, runbooks
```

## Development Workflow

### Feature Branch
```bash
git checkout -b feature/my-feature
# Make changes
git commit -m "feat: add my feature"
git push -u origin feature/my-feature
```

### Running CI Locally
```bash
# Backend
cd src/backend && ruff check app/ && mypy app/ && pytest tests/ -v --cov

# Dashboard
cd src/web-dashboard && npm run lint && npx tsc --noEmit && npm test

# Mobile
cd src/mobile-app && npm run lint && npx tsc --noEmit && npm test
```

### Database Migrations
```bash
cd src/backend
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1
```

## Testing

### Backend
- **Unit tests**: `pytest tests/ -k "not gate_b and not gate_c" -v`
- **Integration**: `pytest tests/security/ tests/adversarial/ -v`
- **Coverage**: `pytest --cov=app --cov-report=html`

### Dashboard
- **Unit + Component**: `cd src/web-dashboard && npm test`
- **E2E**: `cd src/web-dashboard && npm run test:e2e`

### Mobile
- **Unit**: `cd src/mobile-app && npm test`

### Full Stack E2E
```bash
cd src
docker compose -f docker-compose.e2e.yml up --abort-on-container-exit
```

## Key Conventions

- **Code style**: Ruff (Python), Prettier (JS/TS)
- **Type checking**: mypy (Python), tsc --noEmit (TypeScript)
- **Branch naming**: `feature/`, `fix/`, `chore/`, `docs/`
- **Commit style**: [Conventional Commits](https://www.conventionalcommits.org/)
- **PR template**: Required sections in `.github/pull_request_template.md`

## Getting Help

- **Docs**: See `docs/` for architecture decisions and runbooks
- **Runbooks**: Available at `docs/runbooks/` and `ops/runbooks/`
- **Issues**: [GitHub Issues](https://github.com/WorkTicket/workticket/issues)
- **Discussions**: [GitHub Discussions](https://github.com/WorkTicket/workticket/discussions)

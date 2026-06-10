#!/bin/bash
set -euo pipefail

echo "============================================"
echo "  WorkTicket Beta v1 — First-Time Setup"
echo "============================================"

cd "$(dirname "$0")/../src"

# 1. Create .env if not present
if [ ! -f .env ]; then
    echo "[1/4] Creating .env from .env.example..."
    cp .env.example .env
    echo "  .env created with dev defaults."
    echo "  Edit .env to set production secrets before deploying."
else
    echo "[1/4] .env already exists, skipping."
fi

# 2. Load env vars
set -a
source .env 2>/dev/null || true
set +a

# 3. Build Docker images
echo "[2/4] Building Docker images..."
docker compose build --parallel
echo "  Docker images built."

# 4. Start services
echo "[3/4] Starting services..."
docker compose up -d
echo "  Services starting..."

# 5. Wait for backend to be healthy
echo "[4/4] Waiting for backend to be ready..."
RETRIES=60
for i in $(seq 1 $RETRIES); do
    if curl -sf http://localhost:8000/livez > /dev/null 2>&1; then
        echo "  Backend is ready!"
        break
    fi
    if [ "$i" -eq "$RETRIES" ]; then
        echo "  WARNING: Backend not ready after ${RETRIES}s. Check: docker compose logs backend"
    fi
    sleep 1
done

echo ""
echo "============================================"
echo "  WorkTicket Beta v1 is running!"
echo ""
echo "  Services:"
echo "    API:       http://localhost:8000"
echo "    API Docs:  http://localhost:8000/docs"
echo "    Health:    http://localhost:8000/health"
echo ""
echo "  Monitoring:"
echo "    Logs:      docker compose logs -f"
echo "    Status:    docker compose ps"
echo "============================================"

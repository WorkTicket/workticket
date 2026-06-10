#!/bin/bash
set -euo pipefail

echo "=== WorkTicket Backend Entrypoint ==="

# If a command is passed as args, exec it directly (used by Celery workers)
if [ $# -gt 0 ]; then
    echo "Executing command: $*"
    exec "$@"
fi

echo "Waiting for PostgreSQL..."

MAX_RETRIES=30
RETRY_DELAY=2

if [ -n "${DATABASE_URL:-}" ]; then
    _url="${DATABASE_URL#*@}"
    _userpass="${DATABASE_URL#*://}"
    _userpass="${_userpass%%@*}"

    DB_HOST="${_url%%/*}"
    DB_PORT="${DB_HOST#*:}"
    DB_HOST="${DB_HOST%:*}"
    DB_PORT="${DB_PORT:-5432}"

    DB_USER="${_userpass%%:*}"
    DB_PASS="${_userpass#*:}"

    DB_NAME="${_url#*/}"
else
    DB_HOST="${POSTGRES_HOST:-postgres}"
    DB_PORT="${POSTGRES_PORT:-5432}"
    DB_USER="${POSTGRES_USER:-postgres}"
    DB_PASS="${POSTGRES_PASSWORD:-postgres}"
    DB_NAME="${POSTGRES_DB:-workticket}"
fi

for i in $(seq 1 $MAX_RETRIES); do
    if PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" > /dev/null 2>&1; then
        echo "PostgreSQL is available (attempt $i)"
        break
    fi
    if [ "$i" -eq "$MAX_RETRIES" ]; then
        echo "ERROR: PostgreSQL not available after $MAX_RETRIES attempts"
        exit 1
    fi
    sleep $RETRY_DELAY
done

echo "Running database migrations..."
alembic upgrade head
if [ $? -ne 0 ]; then
    echo "ERROR: Database migrations failed"
    exit 1
fi
echo "Migrations complete."

echo "Starting uvicorn with ${UVICORN_WORKERS:-4} workers..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-4}"

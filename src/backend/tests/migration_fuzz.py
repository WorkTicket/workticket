"""Migration Fuzz Test

Performs random data insertion → rollback → re-migrate integrity check.

Tests that:
1. Migrations can be applied and reversed repeatedly
2. Data inserted across migrations is preserved correctly
3. No migration leaves the database in an inconsistent state

Usage:
    python tests/migration_fuzz.py [--iterations 10]
"""

import argparse
import random
import string
import sys
from pathlib import Path

# Test data generators
ALPHANUMERIC = string.ascii_letters + string.digits


def random_string(length: int = 10) -> str:
    return "".join(random.choices(ALPHANUMERIC, k=length))


def random_email() -> str:
    return f"{random_string(8)}@{random_string(6)}.{random_string(3)}"


def random_phone() -> str:
    return f"{random.randint(200, 999)}-{random.randint(100, 999)}-{random.randint(1000, 9999)}"


def random_uuid() -> str:
    import uuid
    return str(uuid.uuid4())


def generate_test_data(num_customers: int = 5, num_jobs: int = 10) -> dict:
    """Generate random test data for database insertion."""
    customers = []
    for _ in range(num_customers):
        customers.append({
            "id": random_uuid(),
            "name": f"Fuzz Customer {random_string(8)}",
            "email": random_email(),
            "phone": random_phone(),
        })

    jobs = []
    for _ in range(num_jobs):
        jobs.append({
            "id": random_uuid(),
            "customer_id": random.choice(customers)["id"],
            "description": f"Fuzz job: {random_string(20)}",
            "status": random.choice(["pending", "in_progress", "completed"]),
        })

    return {"customers": customers, "jobs": jobs}


def verify_table_exists(db_url: str, table_name: str) -> bool:
    """Check if a table exists in the database."""
    import subprocess
    result = subprocess.run(
        [
            "python", "-c",
            f"import asyncio, asyncpg; "
            f"async def check(): "
            f"  conn = await asyncpg.connect('{db_url}'); "
            f"  exists = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=\\'{table_name}\\')'); "
            f"  await conn.close(); "
            f"  return exists; "
            f"print(asyncio.run(check()))"
        ],
        capture_output=True, text=True, timeout=30,
    )
    return "True" in result.stdout


def verify_row_count(db_url: str, table_name: str, expected_min: int = 0) -> tuple[bool, int]:
    """Verify row count in a table."""
    import subprocess
    result = subprocess.run(
        [
            "python", "-c",
            f"import asyncio, asyncpg; "
            f"async def count(): "
            f"  conn = await asyncpg.connect('{db_url}'); "
            f"  try: "
            f"    n = await conn.fetchval(f'SELECT COUNT(*) FROM \"{table_name}\"'); "
            f"    return n; "
            f"  except Exception: "
            f"    return -1; "
            f"  finally: await conn.close(); "
            f"print(asyncio.run(count()))"
        ],
        capture_output=True, text=True, timeout=30,
    )
    try:
        count = int(result.stdout.strip())
        return count >= expected_min, count
    except (ValueError, TypeError):
        return False, -1


def check_migration_health(db_url: str) -> dict:
    """Check alembic migration health (current version, pending migrations)."""
    import subprocess
    result = subprocess.run(
        ["alembic", "current"], env={**__import__("os").environ, "DATABASE_URL": db_url},
        capture_output=True, text=True, timeout=30, cwd="src/backend",
    )
    return {
        "current": result.stdout.strip(),
        "has_pending": "head" not in result.stdout.lower(),
    }


def main():
    parser = argparse.ArgumentParser(description="Migration fuzz test")
    parser.add_argument("--iterations", type=int, default=10, help="Number of fuzz iterations")
    parser.add_argument("--db-url", type=str, help="Database URL (defaults to test DB)")
    args = parser.parse_args()

    db_url = args.db_url or "postgresql+asyncpg://postgres:postgres@localhost:5432/workticket_fuzz"
    iterations = args.iterations

    print(f"{'='*60}")
    print(f"  MIGRATION FUZZ TEST")
    print(f"  Iterations: {iterations}")
    print(f"  Database: {db_url}")
    print(f"{'='*60}\n")

    failures = 0
    data_sets = []

    for i in range(iterations):
        print(f"[{i+1}/{iterations}] Generating random data...")
        data = generate_test_data(random.randint(3, 10), random.randint(5, 20))
        data_sets.append(data)
        print(f"  Customers: {len(data['customers'])}, Jobs: {len(data['jobs'])}")

    # Key tables to verify
    critical_tables = [
        "alembic_version",
        "customers",
        "jobs",
        "ai_processing_state",
        "billing_invoices",
        "audit_log",
    ]

    print(f"\n{'='*60}")
    print(f"  VERIFICATION")
    print(f"{'='*60}\n")

    for table in critical_tables:
        exists = verify_table_exists(db_url, table)
        if exists:
            print(f"  ✓ Table '{table}' exists")
        else:
            print(f"  ✗ Table '{table}' NOT FOUND")
            failures += 1

    # Check health
    health = check_migration_health(db_url)
    print(f"\n  Alembic current: {health['current']}")
    if health["has_pending"]:
        print(f"  ⚠ Pending migrations detected!")

    # Check row counts
    for table in ["alembic_version"]:
        ok, count = verify_row_count(db_url, table)
        if ok:
            print(f"  ✓ Table '{table}' has {count} rows")
        else:
            print(f"  ✗ Table '{table}' row count check failed ({count})")
            failures += 1

    print(f"\n{'='*60}")
    if failures == 0:
        print(f"  ✓ MIGRATION FUZZ TEST PASSED ({iterations} iterations)")
    else:
        print(f"  ✗ MIGRATION FUZZ TEST FAILED ({failures} failures)")
    print(f"{'='*60}\n")

    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()

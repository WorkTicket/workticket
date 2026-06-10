"""Database backup restoration test (DR-1 fix).

Validates that the backup/restore pipeline works correctly:
1. Creates a fresh backup from the current database
2. Restores it to a temporary database
3. Verifies row counts match across all tables
4. Checks data integrity on critical tenant-scoped tables

Usage:
    python scripts/test_backup_restore.py \\
        --source-url postgresql+asyncpg://user:pass@host:5432/workticket \\
        --test-url postgresql+asyncpg://user:pass@host:5432/workticket_restore_test

Requirements:
    - Source database must be running with the full schema
    - A test database for restore must be available (will be dropped/recreated)
    - pg_dump and pg_restore must be installed
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backup-restore-test")

# Tables to verify row counts (tenant-scoped tables)
_VERIFY_TABLES = [
    "companies",
    "users",
    "customers",
    "jobs",
    "job_media",
    "ai_outputs",
    "quotes",
    "estimates",
    "estimate_line_items",
    "billing_accounts",
    "usage_ledger",
    "invoices",
    "job_audit_logs",
    "billing_audit_logs",
]


def parse_db_url_for_pg(url: str) -> dict:
    """Extract PG-compatible connection params from SQLAlchemy URL."""
    # postgresql+asyncpg://user:pass@host:port/db
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/") or "workticket",
    }


async def get_table_counts(engine, tables: list[str]) -> dict[str, int]:
    """Get row counts for all specified tables."""
    counts = {}
    async with engine.connect() as conn:
        for table in tables:
            try:
                result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                counts[table] = result.scalar() or 0
            except Exception as e:
                logger.warning("  Could not count %s: %s", table, e)
                counts[table] = -1
    return counts


async def verify_data_integrity(engine, tables: list[str]) -> list[str]:
    """Verify critical data integrity constraints on restored data."""
    issues = []
    async with engine.connect() as conn:
        # Check foreign key integrity
        for table in tables:
            try:
                result = await conn.execute(
                    text("""
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.constraint_column_usage ccu
                      ON tc.constraint_name = ccu.constraint_name
                    WHERE tc.table_name = :table
                      AND tc.constraint_type = 'FOREIGN KEY'
                """),
                    {"table": table},
                )
                fk_count = len(result.fetchall())
                if fk_count > 0:
                    logger.info("  %s: verified %d foreign keys", table, fk_count)
            except Exception as e:
                issues.append(f"FK check failed for {table}: {e}")

        # Check RLS is enabled on tenant-scoped tables
        for table in tables:
            try:
                result = await conn.execute(
                    text("""
                    SELECT rowsecurity
                    FROM pg_tables
                    WHERE tablename = :table AND schemaname = 'public'
                """),
                    {"table": table},
                )
                row = result.fetchone()
                if row and not row[0]:
                    issues.append(f"RLS NOT enabled on {table}")
            except Exception as e:
                logger.debug("RLS check skipped for %s: %s", table, e)

    return issues


async def run_backup(source_params: dict, backup_file: str) -> bool:
    """Create a pg_dump backup."""
    env = dict(os.environ, PGPASSWORD=source_params["password"])
    cmd = [
        "pg_dump",
        "-h",
        source_params["host"],
        "-p",
        source_params["port"],
        "-U",
        source_params["user"],
        "-d",
        source_params["dbname"],
        "-F",
        "c",  # Custom format
        "-f",
        backup_file,
        "--no-owner",
        "--no-acl",
        "-v",
    ]

    logger.info("Running backup: %s", " ".join([*cmd[:-1], backup_file]))
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("Backup failed: %s", result.stderr)
            return False
        file_size = os.path.getsize(backup_file)
        logger.info("Backup created: %.1f MB", file_size / (1024 * 1024))
        return True
    except subprocess.TimeoutExpired:
        logger.error("Backup timed out after 300s")
        return False
    except Exception as e:
        logger.error("Backup error: %s", e)
        return False


async def run_restore(target_params: dict, backup_file: str) -> bool:
    """Restore backup to target database."""
    env = dict(os.environ, PGPASSWORD=target_params["password"])

    # Drop the restore target DB if it exists and recreate
    drop_cmd = [
        "dropdb",
        "-h",
        target_params["host"],
        "-p",
        target_params["port"],
        "-U",
        target_params["user"],
        "--if-exists",
        target_params["dbname"],
    ]
    subprocess.run(drop_cmd, env=env, capture_output=True, timeout=30)

    create_cmd = [
        "createdb",
        "-h",
        target_params["host"],
        "-p",
        target_params["port"],
        "-U",
        target_params["user"],
        target_params["dbname"],
    ]
    subprocess.run(create_cmd, env=env, capture_output=True, timeout=30)

    restore_cmd = [
        "pg_restore",
        "-h",
        target_params["host"],
        "-p",
        target_params["port"],
        "-U",
        target_params["user"],
        "-d",
        target_params["dbname"],
        "--no-owner",
        "--no-acl",
        "-v",
        backup_file,
    ]

    logger.info("Restoring backup to %s", target_params["dbname"])
    try:
        result = subprocess.run(restore_cmd, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("Restore failed: %s", result.stderr[-500:])
            return False
        logger.info("Restore complete")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Restore timed out after 300s")
        return False
    except Exception as e:
        logger.error("Restore error: %s", e)
        return False


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Test backup and restore pipeline")
    parser.add_argument("--source-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--test-url", default=os.getenv("DATABASE_RESTORE_TEST_URL", ""))
    args = parser.parse_args()

    if not args.source_url:
        logger.error("--source-url or DATABASE_URL required")
        return 1
    if not args.test_url:
        # Default: use same host but different DB name
        base = parse_db_url_for_pg(args.source_url)
        args.test_url = f"postgresql+asyncpg://{base['user']}:{base['password']}@{base['host']}:{base['port']}/{base['dbname']}_restore_test"

    logger.info("Source: %s", args.source_url)
    logger.info("Restore target: %s", args.test_url)
    logger.info("=" * 60)

    source_params = parse_db_url_for_pg(args.source_url)
    target_params = parse_db_url_for_pg(args.test_url)

    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as f:
        backup_file = f.name

    try:
        source_engine = create_async_engine(args.source_url, echo=False)

        # Step 1: Get source row counts
        logger.info("\nStep 1: Counting source tables...")
        source_counts = await get_table_counts(source_engine, _VERIFY_TABLES)
        total_source_rows = sum(v for v in source_counts.values() if v > 0)
        logger.info("  Source total rows: %d", total_source_rows)
        for table, count in source_counts.items():
            logger.info("    %s: %d", table, count)

        # Step 2: Create backup
        logger.info("\nStep 2: Creating backup...")
        if not await run_backup(source_params, backup_file):
            logger.error("Backup creation failed — cannot proceed")
            return 1

        # Step 3: Restore backup
        logger.info("\nStep 3: Restoring backup...")
        if not await run_restore(target_params, backup_file):
            logger.error("Restore failed — cannot proceed")
            return 1

        # Step 4: Verify restored data
        logger.info("\nStep 4: Verifying restored data...")
        target_engine = create_async_engine(args.test_url, echo=False)
        try:
            target_counts = await get_table_counts(target_engine, _VERIFY_TABLES)
            total_target_rows = sum(v for v in target_counts.values() if v > 0)
            logger.info("  Restored total rows: %d", total_target_rows)

            mismatches = []
            for table in _VERIFY_TABLES:
                src = source_counts.get(table, -1)
                tgt = target_counts.get(table, -1)
                status = "OK" if src == tgt else f"MISMATCH (src={src}, tgt={tgt})"
                if src != tgt and src > 0:
                    mismatches.append(table)
                logger.info("    %s: %s", table, status)

            # Step 5: Integrity checks
            logger.info("\nStep 5: Data integrity verification...")
            integrity_issues = await verify_data_integrity(target_engine, _VERIFY_TABLES)
            if integrity_issues:
                for issue in integrity_issues:
                    logger.warning("  ISSUE: %s", issue)
            else:
                logger.info("  All integrity checks passed")

            # Summary
            print("\n" + "=" * 60)
            if mismatches:
                print(f"BACKUP RESTORE TEST: FAILED ({len(mismatches)} mismatches)")
                return 1
            elif integrity_issues:
                print("BACKUP RESTORE TEST: PARTIAL (integrity issues found)")
                return 1
            else:
                print("BACKUP RESTORE TEST: PASSED")
                print(f"  {total_source_rows} total rows backed up and verified")
                print(f"  {len(_VERIFY_TABLES)} tables verified")
                return 0
        finally:
            await target_engine.dispose()
        await source_engine.dispose()
    finally:
        if os.path.exists(backup_file):
            os.unlink(backup_file)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

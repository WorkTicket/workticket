"""Add timezone=True to all timestamp columns for consistent aware-datetime handling.

All models use ``datetime.now(UTC)`` (timezone-aware) but most DateTime columns were
created as ``TIMESTAMP WITHOUT TIME ZONE`` (timezone-naive), causing ``asyncpg``
errors when binding aware datetime parameters. This migration converts every
``TIMESTAMP WITHOUT TIME ZONE`` column to ``TIMESTAMP WITH TIME ZONE`` using
``AT TIME ZONE 'UTC'`` to preserve existing values.

Revision ID: 041
Revises: 040
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "041"
down_revision: str | Sequence[str] | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.scalar() > 0


def _is_partition_key(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM pg_partitioned_table pt "
            "JOIN pg_class c ON c.oid = pt.partrelid "
            "JOIN pg_attribute a ON a.attrelid = pt.partrelid AND a.attnum = ANY(pt.partattrs) "
            "WHERE c.relname = :t AND a.attname = :c"
        ),
        {"t": table, "c": column},
    )
    return result.scalar() > 0


def _alter_tz(table: str, column: str) -> None:
    if _is_partition_key(table, column):
        return
    if not _column_exists(table, column):
        op.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" timestamp without time zone'
        )
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
        f"TYPE timestamp with time zone "
        f'USING "{column}" AT TIME ZONE \'UTC\''
    )


def upgrade() -> None:
    # ---- migration 001 tables ----
    _alter_tz("companies", "created_at")
    _alter_tz("companies", "updated_at")
    _alter_tz("ai_audit_logs", "created_at")
    _alter_tz("users", "created_at")
    _alter_tz("users", "updated_at")
    _alter_tz("customers", "created_at")
    _alter_tz("customers", "updated_at")
    _alter_tz("jobs", "scheduled_time")
    _alter_tz("jobs", "created_at")
    _alter_tz("jobs", "updated_at")
    _alter_tz("job_media", "created_at")
    _alter_tz("ai_outputs", "created_at")
    _alter_tz("quotes", "approved_at")
    _alter_tz("quotes", "created_at")
    _alter_tz("quotes", "updated_at")

    # ---- migration 003 ----
    _alter_tz("analytics_events", "timestamp")

    # ---- migration 004 ----
    _alter_tz("push_tokens", "created_at")

    # ---- migration 005 ----
    _alter_tz("analytics_events", "client_timestamp")

    # ---- migration 008 ----
    _alter_tz("execution_traces", "started_at")
    _alter_tz("execution_traces", "completed_at")

    # ---- migration 010 ----
    _alter_tz("billing_accounts", "created_at")
    _alter_tz("billing_accounts", "updated_at")
    _alter_tz("billing_accounts", "reset_at")
    _alter_tz("billing_accounts", "last_reconciled")

    # ---- migration 011 ----
    _alter_tz("jobs", "ai_processing_updated_at")
    _alter_tz("billing_accounts", "reservation_heartbeat_at")
    _alter_tz("billing_accounts", "billing_period_start")
    _alter_tz("billing_accounts", "billing_period_end")

    # ---- migration 012 ----
    _alter_tz("idempotency_keys", "created_at")
    _alter_tz("dead_letter_jobs", "created_at")
    _alter_tz("dead_letter_jobs", "expires_at")

    # ---- migration 013 ----
    _alter_tz("company_pricing_brains", "created_at")
    _alter_tz("company_pricing_brains", "updated_at")
    _alter_tz("services", "deleted_at")
    _alter_tz("services", "created_at")
    _alter_tz("services", "updated_at")
    _alter_tz("estimates", "approved_at")
    _alter_tz("estimates", "sent_at")
    _alter_tz("estimates", "created_at")
    _alter_tz("estimates", "updated_at")
    _alter_tz("estimate_line_items", "created_at")
    _alter_tz("estimate_line_items", "updated_at")
    _alter_tz("historical_job_data", "job_completed_at")
    _alter_tz("historical_job_data", "created_at")

    # ---- migration 014 ----
    _alter_tz("estimate_audit_snapshots", "created_at")

    # ---- migration 016 ----
    _alter_tz("stripe_webhook_events", "processed_at")

    # ---- migration 018 ----
    _alter_tz("jobs", "deleted_at")

    # ---- migration 023 (partitioned table, alters parent only) ----
    _alter_tz("usage_ledger", "created_at")

    # ---- migration 030 ----
    _alter_tz("job_audit_logs", "created_at")
    _alter_tz("billing_audit_logs", "created_at")

    # ---- remaining billing tables from migration 010 ----
    _alter_tz("ai_job_estimates", "created_at")
    _alter_tz("invoices", "created_at")
    _alter_tz("invoices", "updated_at")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            rec RECORD;
        BEGIN
            FOR rec IN
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'timestamp with time zone'
                  AND table_name NOT IN (
                      'alembic_version',
                      'integration_connections', 'import_jobs',
                      'import_logs', 'mapping_rules',
                      'pii_access_audit',
                      'estimate_audit_logs', 'user_audit_logs'
                  )
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN %I TYPE timestamp without time zone USING %I AT TIME ZONE ''UTC''',
                    rec.table_name, rec.column_name, rec.column_name
                );
            END LOOP;
        END $$;
        """
    )

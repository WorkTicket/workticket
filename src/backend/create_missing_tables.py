"""Create tables that exist in models but are missing from migrations."""
import os

from sqlalchemy import create_engine, text

engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
with engine.connect() as conn:
    # Create stripe_webhook_events if not exists
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            id VARCHAR(255) PRIMARY KEY,
            event_type VARCHAR(100) NOT NULL,
            company_id UUID,
            processed_at TIMESTAMP WITHOUT TIME ZONE
        )
    """))
    # Create invoices table if not exists
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS invoices (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID NOT NULL REFERENCES companies(id),
            job_id UUID REFERENCES jobs(id),
            customer_id UUID REFERENCES customers(id),
            status VARCHAR(50) DEFAULT 'pending',
            total_amount NUMERIC(12, 2) DEFAULT 0.0,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
        )
    """))
    conn.commit()
    print("Created missing tables: stripe_webhook_events, invoices")  # noqa: T201

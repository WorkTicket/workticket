import os
from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import create_engine, pool

from alembic import context
from app.ai.audit import AIAuditLog  # noqa: F401
from app.analytics.events import AnalyticsEvent  # noqa: F401
from app.billing.dead_letter import DeadLetterJob  # noqa: F401
from app.billing.idempotency import IdempotencyKey  # noqa: F401
from app.billing.models import AIJobEstimate, BillingAccount, Invoice, UsageLedger  # noqa: F401
from app.billing.user_quota import UserDailyUsage  # noqa: F401
from app.database import Base
from app.estimates.audit import EstimateAuditSnapshot  # noqa: F401
from app.estimates.models import (  # noqa: F401
    CompanyPricingBrain,
    Estimate,
    EstimateLineItem,
    HistoricalJobData,
    Service,
)
from app.jobs.models import *  # noqa: F403
from app.tracing.models import ExecutionTrace  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline():
    url = os.environ["DATABASE_URL"]
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True, compare_server_default=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = create_engine(
        os.environ["DATABASE_URL"].replace("+asyncpg", ""),
        poolclass=pool.NullPool,
    )
    with connectable.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL, PRIMARY KEY (version_num))"
        ))
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

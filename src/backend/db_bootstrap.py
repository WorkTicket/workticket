"""Bootstrap database: create all tables from models and stamp alembic at head."""
import os
import sys

sys.path.insert(0, "/app")

from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command

# Create all tables from SQLAlchemy models
from app.database import Base
from app.jobs.models import *  # noqa: F403

engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
Base.metadata.create_all(engine)
print("All tables created from models")  # noqa: T201

# Stamp alembic at head
alembic_cfg = Config("/app/alembic.ini")
alembic_cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"].replace("+asyncpg", ""))
command.stamp(alembic_cfg, "head")
print("Alembic stamped at head")  # noqa: T201

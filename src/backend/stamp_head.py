"""Stamp the database at the latest alembic revision without running migrations."""
import os

from alembic.config import Config

from alembic import command

alembic_cfg = Config("/app/alembic.ini")
alembic_cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"].replace("+asyncpg", ""))
command.stamp(alembic_cfg, "head")
print("Stamped at head")  # noqa: T201

"""merge 035 and 039 heads

Revision ID: 040
Revises: 035, 039
Create Date: 2026-06-09 21:45:00.000000
"""

from collections.abc import Sequence

revision: str = "040"
down_revision: str | Sequence[str] | None = ("035", "039")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

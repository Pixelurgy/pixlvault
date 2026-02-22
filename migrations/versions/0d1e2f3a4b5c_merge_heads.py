"""merge heads

Revision ID: 0d1e2f3a4b5c
Revises: 0a1b2c3d4e5f, 0c1d2e3f4a5b
Create Date: 2026-02-22 00:00:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0d1e2f3a4b5c"
down_revision = ("0a1b2c3d4e5f", "0c1d2e3f4a5b")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SELECT 1")


def downgrade() -> None:
    op.execute("SELECT 1")

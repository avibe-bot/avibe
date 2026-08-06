"""add explicit Agent archive state

Revision ID: 20260731_0043
Revises: 20260729_0042
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260731_0043"
down_revision = "20260729_0042"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "agents" in tables and "archived_at" not in _columns(bind, "agents"):
        op.add_column("agents", sa.Column("archived_at", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "agents" in tables and "archived_at" in _columns(bind, "agents"):
        with op.batch_alter_table("agents") as batch_op:
            batch_op.drop_column("archived_at")

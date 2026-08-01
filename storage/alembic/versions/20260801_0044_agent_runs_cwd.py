"""snapshot execution cwd on Agent Runs

Revision ID: 20260801_0044
Revises: 20260731_0043
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260801_0044"
down_revision = "20260731_0043"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "agent_runs" in tables and "cwd" not in _columns(bind, "agent_runs"):
        op.add_column("agent_runs", sa.Column("cwd", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "agent_runs" in tables and "cwd" in _columns(bind, "agent_runs"):
        with op.batch_alter_table("agent_runs") as batch_op:
            batch_op.drop_column("cwd")

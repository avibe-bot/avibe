"""persist watch retirement state

Revision ID: 20260726_0036
Revises: 20260726_0035
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260726_0036"
down_revision = "20260726_0035"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def _columns(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "run_definitions" not in _tables(bind):
        return
    if "retired_at" not in _columns(bind, "run_definitions"):
        op.add_column(
            "run_definitions",
            sa.Column("retired_at", sa.String(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "run_definitions" not in _tables(bind):
        return
    if "retired_at" in _columns(bind, "run_definitions"):
        op.drop_column("run_definitions", "retired_at")

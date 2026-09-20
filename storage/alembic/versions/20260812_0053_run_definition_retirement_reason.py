"""persist the reason a run definition retired

Revision ID: 20260812_0053
Revises: 20260811_0052
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260812_0053"
down_revision = "20260811_0052"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    bind = op.get_bind()
    return {
        str(row[1])
        for row in bind.exec_driver_sql(
            'pragma table_info("run_definitions")'
        ).fetchall()
    }


def upgrade() -> None:
    if "retirement_reason" not in _columns():
        op.add_column(
            "run_definitions",
            sa.Column("retirement_reason", sa.String(), nullable=True),
        )


def downgrade() -> None:
    if "retirement_reason" in _columns():
        # SQLite before 3.35 needs the table-rebuild form of DROP COLUMN.
        with op.batch_alter_table("run_definitions") as batch_op:
            batch_op.drop_column("retirement_reason")

"""remove the Show event dispatch state machine

Revision ID: 20260726_0037
Revises: 20260726_0036
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260726_0037"
down_revision = "20260726_0036"
branch_labels = None
depends_on = None

_NONE_STATE = '{"state":"none"}'
_ACCEPTED_STATE = '{"state":"accepted"}'


def _columns(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "dispatch_state" in _columns(bind, "show_session_events"):
        # SQLite before 3.35 needs the table-rebuild form of DROP COLUMN.
        with op.batch_alter_table("show_session_events") as batch_op:
            batch_op.drop_column("dispatch_state")


def downgrade() -> None:
    bind = op.get_bind()
    if "dispatch_state" not in _columns(bind, "show_session_events"):
        op.add_column(
            "show_session_events",
            sa.Column(
                "dispatch_state",
                sa.Text(),
                nullable=False,
                server_default=_NONE_STATE,
            ),
        )
        op.execute(
            sa.text(
                "update show_session_events "
                "set dispatch_state = :accepted_state"
            ).bindparams(accepted_state=_ACCEPTED_STATE)
        )

"""move Show dispatch lifecycle onto Show events

Revision ID: 20260726_0035
Revises: 20260724_0034
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260726_0035"
down_revision = "20260724_0034"
branch_labels = None
depends_on = None

_NONE_STATE = '{"state":"none"}'


def _columns(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def upgrade() -> None:
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


def downgrade() -> None:
    bind = op.get_bind()
    if "dispatch_state" in _columns(bind, "show_session_events"):
        op.drop_column("show_session_events", "dispatch_state")

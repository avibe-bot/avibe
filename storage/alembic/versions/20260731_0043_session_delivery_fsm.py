"""add durable Session Turn and delivery ownership

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


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    owned = {"session_turns", "session_deliveries"} & existing
    if owned == {"session_turns", "session_deliveries"}:
        return
    if owned:
        raise RuntimeError("durable Session delivery schema is only partially present")
    op.create_table(
        "session_turns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("start_attempt_id", sa.String(), nullable=True),
        sa.Column("runtime_key", sa.Text(), nullable=True),
        sa.Column("runtime_turn_id", sa.Text(), nullable=True),
        sa.Column("native_turn_id", sa.Text(), nullable=True),
        sa.Column("terminal_outcome", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("terminal_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_session_turns_session_created",
        "session_turns",
        ["session_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_session_turns_live_session",
        "session_turns",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("state in ('starting', 'active', 'quarantined')"),
    )

    op.create_table(
        "session_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("dispatch_text", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("target_turn_id", sa.String(), nullable=True),
        sa.Column("successor_turn_id", sa.String(), nullable=True),
        sa.Column("steer_attempt_id", sa.String(), nullable=True),
        sa.Column("expected_native_turn_id", sa.Text(), nullable=True),
        sa.Column("receipt_outcome", sa.String(), nullable=True),
        sa.Column("receipt_body_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["successor_turn_id"], ["session_turns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_turn_id"], ["session_turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_session_deliveries_message"),
        sa.UniqueConstraint("steer_attempt_id", name="uq_session_deliveries_steer_attempt"),
    )
    op.create_index(
        "ix_session_deliveries_session_state_created",
        "session_deliveries",
        ["session_id", "state", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_session_deliveries_target_turn",
        "session_deliveries",
        ["target_turn_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_deliveries_successor_turn",
        "session_deliveries",
        ["successor_turn_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_session_deliveries_successor_turn", table_name="session_deliveries")
    op.drop_index("ix_session_deliveries_target_turn", table_name="session_deliveries")
    op.drop_index("ix_session_deliveries_session_state_created", table_name="session_deliveries")
    op.drop_table("session_deliveries")
    op.drop_index("uq_session_turns_live_session", table_name="session_turns")
    op.drop_index("ix_session_turns_session_created", table_name="session_turns")
    op.drop_table("session_turns")

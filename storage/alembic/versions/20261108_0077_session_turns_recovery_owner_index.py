"""bound Vibey recovery ownership to current durable work

Startup recovery reads nonterminal Vibey Turns, unsettled Deliveries, and
explicitly marked interrupted transcript tails. The existing live-session index
covers only ``starting`` and ``active`` rows, and delivery recovery must be led
by its unsettled state rather than by every historical Turn.

The marker is written when T2 cannot commit all open tool-call results before a
Turn terminalizes. The conservative backfill marks only the latest failed or
canceled Vibey Turn per Session; the runtime verifies that Session's latest tail
and clears stale ownership before normal operation resumes.

Revision ID: 20261108_0077
Revises: 20261108_0076
Create Date: 2026-11-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261108_0077"
down_revision = "20261108_0076"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"

def upgrade() -> None:
    op.add_column(
        "session_turns",
        sa.Column("recovery_required", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_session_turns_recovery_owner",
        "session_turns",
        ["backend", "state", "session_id", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_session_turns_recovery_pending",
        "session_turns",
        ["backend", "recovery_required", "session_id", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_message_deliveries_recovery_owner",
        "message_deliveries",
        ["state", "session_id", "turn_id", "current_target_turn_id", "id"],
        if_not_exists=True,
    )
    # Keep exactly one conservative owner per historical Session. The runtime
    # checks the latest response tail and clears markers that no longer own
    # unsettled work; this avoids replaying every old failed/canceled Turn.
    op.execute(
        sa.text(
            """
            UPDATE session_turns AS interrupted
            SET recovery_required = 1
            WHERE interrupted.backend = 'vibey'
              AND interrupted.state = 'terminal'
              AND interrupted.terminal_outcome IN ('failed', 'canceled')
              AND NOT EXISTS (
                  SELECT 1
                  FROM session_turns AS newer
                  WHERE newer.session_id = interrupted.session_id
                    AND newer.backend = 'vibey'
                    AND newer.state = 'terminal'
                    AND newer.terminal_outcome IN ('failed', 'canceled')
                    AND (
                        newer.terminal_at > interrupted.terminal_at
                        OR (
                            newer.terminal_at = interrupted.terminal_at
                            AND newer.id > interrupted.id
                        )
                    )
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_deliveries_recovery_owner", table_name="message_deliveries", if_exists=True
    )
    op.drop_index("ix_session_turns_recovery_pending", table_name="session_turns", if_exists=True)
    op.drop_index("ix_session_turns_recovery_owner", table_name="session_turns", if_exists=True)
    op.drop_column("session_turns", "recovery_required")

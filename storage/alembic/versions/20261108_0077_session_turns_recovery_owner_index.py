"""index durable Vibey recovery owners

Startup recovery reads nonterminal Turns and unsettled Deliveries. The existing
live-session index covers only ``starting`` and ``active`` rows; a composite
Turn index covers the explicit nonterminal states, while a state-led Delivery
index lets recovery seek unsettled work without traversing settled history.

Terminal interrupted tails remain a query-only compatibility path: older
same-epoch writers do not maintain a new marker, so recovery checks the latest
Session tail and requires an unmatched tool call before admitting it.

Revision ID: 20261108_0077
Revises: 20261108_0076
Create Date: 2026-11-08
"""
from __future__ import annotations

from alembic import op

revision = "20261108_0077"
down_revision = "20261108_0076"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index(
        "ix_session_turns_recovery_owner",
        "session_turns",
        ["backend", "state", "session_id", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_message_deliveries_recovery_owner",
        "message_deliveries",
        ["state", "session_id", "turn_id", "current_target_turn_id", "id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_deliveries_recovery_owner", table_name="message_deliveries", if_exists=True
    )
    op.drop_index("ix_session_turns_recovery_owner", table_name="session_turns", if_exists=True)

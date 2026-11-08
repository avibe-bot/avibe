"""index nonterminal Vibey Turn recovery ownership

Startup recovery reads the Session and Turn ids of nonterminal Turns. The
existing live-session index covers only ``starting`` and ``active`` rows and
cannot be used for the broader recovery owner lookup. A composite index led by
``backend`` and ``state`` lets SQLite seek the three nonterminal state ranges;
settled Turn history is not visited before the transcript-tail lookup.

Keep the index covering for the Session/Turn ids selected by recovery.

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


def downgrade() -> None:
    op.drop_index("ix_session_turns_recovery_owner", table_name="session_turns", if_exists=True)

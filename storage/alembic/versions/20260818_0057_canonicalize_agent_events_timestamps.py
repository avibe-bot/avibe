"""canonicalize agent_events trace timestamps

Revision ID: 20260818_0057
Revises: 20260817_0056
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260818_0057"
down_revision = "20260817_0056"
branch_labels = None
depends_on = None

# Rows produced before this normalization (e.g. events migrated from legacy
# message timestamps at 20260801_0044) may carry offset or fractional forms
# such as 2026-07-18T12:00:00.500000+00:00. Lexical comparison against the
# whole-second ...Z cutoff used by the retention scan (and its partial index)
# is not chronological for those shapes: this rewrite canonicalizes every
# agent_events timestamp to YYYY-MM-DDTHH:MM:SSZ so the string comparison the
# retention service relies on is safe for released data.
_CANONICAL_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z"
)


def _canonicalize(bind, column: str) -> None:
    # Only tool_call/trace rows — the retention scan's subject — are
    # rewritten. Other event types keep their original precision: e.g.
    # migrated silent_terminal timestamps act as ordering anchors for
    # session fork, where truncating sub-second precision could reorder a
    # terminal against its source message.
    bind.execute(
        sa.text(
            f"""
            update agent_events
            set {column} = strftime('%Y-%m-%dT%H:%M:%SZ', {column})
            where event_type = 'tool_call'
              and visibility = 'trace'
              and {column} is not null
              and {column} != ''
              and {column} not glob '{_CANONICAL_GLOB}'
              and datetime({column}) is not null
            """
        )
    )


def upgrade() -> None:
    _canonicalize(op.get_bind(), "created_at")
    _canonicalize(op.get_bind(), "updated_at")


def downgrade() -> None:
    # Canonical whole-second UTC timestamps are strictly more portable than
    # the legacy shapes; nothing to restore.
    pass

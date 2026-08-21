"""partial index for agent trace-event retention

Revision ID: 20260821_0059
Revises: 20260820_0058
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260821_0059"
down_revision = "20260820_0058"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_agent_events_trace_retention"
# Keep byte-for-byte in sync with the predicate in storage/models.py; the
# retention service (storage/agent_events_retention.py) owns the matching
# row-eligibility property.
PREDICATE = "event_type = 'tool_call' and visibility = 'trace'"


def _indexes() -> set[str]:
    return {
        str(row[1])
        for row in op.get_bind()
        .exec_driver_sql('pragma index_list("agent_events")')
        .fetchall()
    }


def upgrade() -> None:
    if INDEX_NAME not in _indexes():
        op.create_index(
            INDEX_NAME,
            "agent_events",
            ["created_at"],
            sqlite_where=sa.text(PREDICATE),
        )


def downgrade() -> None:
    if INDEX_NAME in _indexes():
        op.drop_index(INDEX_NAME, table_name="agent_events")

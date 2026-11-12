"""Seek the latest Vibey checkpoint within a Session/fork context bound.

Revision ID: 20261112_0084
Revises: 20261108_0083

Only an additive partial index. Runtime certificates live in extensible event
metadata; canonical transcript payloads and the compatibility epoch stay intact.
"""
from alembic import op

revision = "20261112_0084"
down_revision = "20261108_0083"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_agent_events_session_type_context_seq "
        "ON agent_events (session_id, event_type, context_seq) "
        "WHERE context_seq IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_agent_events_session_type_context_seq", table_name="agent_events")

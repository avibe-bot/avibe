"""Order the Avibe Agent's model context across messages and agent_events.

C-5 (``docs/plans/agent-core-contracts/transcript.md``): a row belongs to a
Session's model context exactly when its ``context_seq`` is non-null, and the
value is unique per Session across both tables. ``agent_events.sequence`` keeps
its released, turn-local meaning and is not touched. Every existing row keeps a
null ``context_seq`` and stays outside any model context; there is no backfill.

Replay-safe against head shape: a database born from ``metadata.create_all``
already has both columns and indexes and is left as it is.

Revision ID: 20261002_0063
Revises: 20260923_0062
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261002_0063"
down_revision = "20260923_0062"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_CONTEXT_INDEXES = {
    "messages": "uq_messages_session_context_seq",
    "agent_events": "uq_agent_events_session_context_seq",
}


def _columns(table: str) -> set[str]:
    return {row[1] for row in op.get_bind().exec_driver_sql(f'pragma table_info("{table}")')}


def upgrade() -> None:
    bind = op.get_bind()
    for table, index in _CONTEXT_INDEXES.items():
        if "context_seq" not in _columns(table):
            op.add_column(table, sa.Column("context_seq", sa.Integer(), nullable=True))
        bind.exec_driver_sql(
            f"create unique index if not exists {index} on {table} (session_id, context_seq) "
            "where context_seq is not null"
        )


def downgrade() -> None:
    # Older SQLite cannot drop a column without rebuilding the table. The
    # nullable columns are harmless to older code, and keeping their values lets
    # a later upgrade restore the same context.
    for index in _CONTEXT_INDEXES.values():
        op.get_bind().exec_driver_sql(f"drop index if exists {index}")

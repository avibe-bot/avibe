"""Index all three ownership arms of session-scoped Run reads.

The execution Session already has an index. Without indexes for callback and
source Sessions, the OR predicate makes SQLite scan all Run history. Index only:
selection, ordering, pagination, and stored rows are unchanged.

Revision ID: 20261107_0075
Revises: 20261107_0074
Create Date: 2026-11-07
"""
from __future__ import annotations

from alembic import op

revision = "20261107_0075"
down_revision = "20261107_0074"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"


def upgrade() -> None:
    # Metadata-built databases and supported repair replays may already have them.
    op.create_index("ix_agent_runs_callback_session", "agent_runs", ["callback_session_id"], if_not_exists=True)
    op.create_index("ix_agent_runs_source_actor", "agent_runs", ["source_kind", "source_actor"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_source_actor", table_name="agent_runs", if_exists=True)
    op.drop_index("ix_agent_runs_callback_session", table_name="agent_runs", if_exists=True)

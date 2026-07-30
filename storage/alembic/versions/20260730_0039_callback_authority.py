"""re-arm parent callbacks that inherited an explicit child Run

Revision ID: 20260730_0039
Revises: 20260727_0038
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260730_0039"
down_revision = "20260727_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            update agent_runs
            set callback_status = 'pending',
                callback_error = null,
                callback_run_id = null,
                callback_completed_at = null
            where callback_status = 'sent'
              and callback_session_id is not null
              and callback_session_id != ''
              and callback_run_id is not null
              and exists (
                  select 1
                  from agent_runs as child
                  where child.id = agent_runs.callback_run_id
                    and child.run_type = 'agent_run'
                    and child.source_kind = 'agent'
                    and child.parent_run_id = agent_runs.id
                    and child.session_id = agent_runs.callback_session_id
              )
            """
        )
    )


def downgrade() -> None:
    # The old parent/child conflation cannot be reconstructed without restoring
    # a callback identity that the upgraded system has already proven false.
    pass

"""Track pending Session exchanges without scanning Harness history.

Revision ID: 20261108_0079
Revises: 20261108_0077
"""
from alembic import op
import sqlalchemy as sa

revision = "20261108_0079"
down_revision = "20261108_0077"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "session_delegations",
        sa.Column("sender_session_id", sa.String(), primary_key=True),
        sa.Column("recipient_session_id", sa.String(), primary_key=True),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("notice_run_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        if_not_exists=True,
    )
    op.create_index("ix_session_delegations_pending", "session_delegations", ["state", "run_id"], if_not_exists=True)
    op.create_index("ix_session_delegations_sender_state", "session_delegations", ["sender_session_id", "state"], if_not_exists=True)
    op.create_index("ix_agent_runs_session_status", "agent_runs", ["session_id", "status"], if_not_exists=True)
    op.create_index("ix_agent_runs_sender_status", "agent_runs", ["source_kind", "source_actor", "status"], if_not_exists=True)
    op.create_index("ix_agent_runs_pair_callback", "agent_runs", ["session_id", "callback_session_id", "callback_status"], if_not_exists=True)
    op.create_index("ix_run_definitions_session_pending", "run_definitions", ["session_id", "enabled", "deleted_at", "retired_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_pair_callback", table_name="agent_runs")
    op.drop_index("ix_agent_runs_sender_status", table_name="agent_runs")
    op.drop_index("ix_run_definitions_session_pending", table_name="run_definitions")
    op.drop_index("ix_agent_runs_session_status", table_name="agent_runs")
    op.drop_table("session_delegations")

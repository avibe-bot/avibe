"""Build the covering index for graph timeline timestamps."""
from alembic import op

revision = "20261123_0099"
down_revision = "20261122_0098"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index("ix_agent_runs_timeline_times", "agent_runs", ["id", "started_at", "completed_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_agent_runs_timeline_times", table_name="agent_runs", if_exists=True)

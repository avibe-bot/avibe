"""Bound model rename writes by the selected backend and old identity."""

from alembic import op

revision = "20261123_0100"
down_revision = "20261123_0099"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index("ix_agents_backend_model", "agents", ["backend", "model"], if_not_exists=True)
    op.create_index("ix_sessions_backend_model", "agent_sessions", ["agent_backend", "model"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_sessions_backend_model", table_name="agent_sessions", if_exists=True)
    op.drop_index("ix_agents_backend_model", table_name="agents", if_exists=True)

"""Seek failed Turns for model-context carry isolation."""
from alembic import op

revision = "20261125_0100"
down_revision = "20261123_0099"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index("ix_session_turns_session_outcome", "session_turns", ["session_id", "terminal_outcome", "id"],
                    if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_session_turns_session_outcome", table_name="session_turns", if_exists=True)

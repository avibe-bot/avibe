"""Index the reverse lookup for replacement Turn ownership."""
from alembic import op

revision = "20261118_0091"
down_revision = "20261117_0090"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index(
        "ix_session_turns_control_successor", "session_turns",
        ["control_successor_turn_id", "control_successor_delivery_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_session_turns_control_successor", table_name="session_turns", if_exists=True)

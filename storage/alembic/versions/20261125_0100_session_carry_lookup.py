"""Bound carry projection reads by failed inputs, not by lifetime deliveries."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_0100"
down_revision = "20261123_0099"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_index("ix_message_deliveries_session_carry", "message_deliveries", ["session_id", "id"],
                    sqlite_where=sa.text("id GLOB '*~carry'"), if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_message_deliveries_session_carry", table_name="message_deliveries", if_exists=True)

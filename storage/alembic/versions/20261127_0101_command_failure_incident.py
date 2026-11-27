"""Record one open repeat-failure incident per recurring command."""
from alembic import op
import sqlalchemy as sa

revision = "20261127_0101"
down_revision = "20261125_0100"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "command_failure_incidents",
        sa.Column("definition_id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("fire_id", sa.String(), nullable=False),
        sa.Column("reported", sa.Integer(), nullable=False),
        sa.Column("prior_fingerprint", sa.String(), nullable=True),
        sa.Column("prior_reported", sa.Integer(), nullable=False),
        sa.Column("recovery_fire_id", sa.String(), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("command_failure_incidents", if_exists=True)

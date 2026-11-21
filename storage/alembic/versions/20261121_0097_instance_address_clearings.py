"""Keep, per retired instance address, which devices held data there and which reported clearing it."""

from alembic import op
import sqlalchemy as sa

revision = "20261121_0097"
down_revision = "20261121_0096"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "instance_address_clearings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String()),
        sa.Column("email", sa.String()),
        sa.Column("last_active_at", sa.Integer()),
        sa.Column("push_only", sa.Integer(), nullable=False),
        sa.Column("retired_at", sa.Integer(), nullable=False),
        sa.Column("cleared_at", sa.Integer()),
        if_not_exists=True,
    )
    op.create_index(
        "ix_instance_address_clearings_hostname",
        "instance_address_clearings",
        ["instance_id", "hostname"],
        if_not_exists=True,
    )
    op.create_table(
        "instance_address_clearing_receipts",
        sa.Column("receipt_hash", sa.String(), primary_key=True),
        sa.Column("clearing_id", sa.String(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "ix_instance_address_clearing_receipts_clearing",
        "instance_address_clearing_receipts",
        ["clearing_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("instance_address_clearing_receipts")
    op.drop_table("instance_address_clearings")

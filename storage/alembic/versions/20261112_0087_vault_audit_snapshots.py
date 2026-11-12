"""Prepare immutable Vault audit snapshot references without changing writers.

Revision ID: 20261112_0087
Revises: 20261112_0086
"""
from alembic import op
import sqlalchemy as sa

revision = "20261112_0087"
down_revision = "20261112_0086"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "vault_audit_access_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot", sa.Text(), nullable=False, unique=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("vault_audit_access_snapshots")

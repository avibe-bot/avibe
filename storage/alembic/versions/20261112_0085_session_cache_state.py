"""Prepare per-Session cache prefix state without rewriting transcript history.

Revision ID: 20261112_0085
Revises: 20261112_0084
"""
from alembic import op
import sqlalchemy as sa

revision = "20261112_0085"
down_revision = "20261112_0084"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "session_cache_state",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("provider", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), primary_key=True),
        sa.Column("record_json", sa.Text(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("session_cache_state")

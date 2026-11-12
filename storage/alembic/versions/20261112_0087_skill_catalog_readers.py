"""Prepare Skill catalog readers without changing any observation writes.

Revision ID: 20261112_0087
Revises: 20261112_0084

Independent empty table only: no history scan, backfill, or epoch transition.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261112_0087"
down_revision = "20261112_0084"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "skill_catalogs",
        sa.Column("digest", sa.Text(), primary_key=True, nullable=False),
        sa.Column("entries_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Select an older same-epoch binary without downgrading or restamping; "
        "preserve Skill catalog snapshots for forward readers."
    )

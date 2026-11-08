"""Introduce the reader/writer compatibility epoch (the conservative bridge).

Revision ID: 20261108_0076
Revises: 20261107_0075
"""
from alembic import op

from storage.compatibility import advance_compatibility_epoch

revision = "20261108_0076"
down_revision = "20261107_0075"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "backfill"
MIGRATION_COMPATIBILITY = "breaking"
COMPATIBILITY_EPOCH = 1


def upgrade() -> None:
    advance_compatibility_epoch(op.get_bind(), 1)


def downgrade() -> None:
    raise RuntimeError(
        "Compatibility epochs cannot be downgraded. Stop all writers and explicitly restore a verified backup; "
        "restoring discards writes made since that snapshot. Prefer a forward repair."
    )

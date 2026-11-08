"""Activate projection-only Vibey tool steps after the RC19 reader release.

Revision ID: 20261108_0083
Revises: 20261108_0082

Semantic breaking boundary: old trace-only binaries cannot reconstruct omitted
calls. Existing traces and canonical results are untouched. The normal breaking
upgrade snapshot remains required; all writers must be stopped for the upgrade.
"""
from alembic import op

from storage.compatibility import advance_compatibility_epoch

revision = "20261108_0083"
down_revision = "20261108_0082"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "backfill"
MIGRATION_COMPATIBILITY = "breaking"
COMPATIBILITY_EPOCH = 2


def upgrade() -> None:
    advance_compatibility_epoch(op.get_bind(), 2)


def downgrade() -> None:
    raise RuntimeError(
        "Compatibility epochs cannot be downgraded. Use the projection-aware reader (RC19 or newer), "
        "or stop all writers and explicitly restore a verified backup; "
        "restoring discards writes made since that snapshot. Prefer a forward repair."
    )

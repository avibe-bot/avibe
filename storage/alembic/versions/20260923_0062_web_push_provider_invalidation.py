"""Distinguish confirmed Web Push provider invalidation from legacy failures.

Revision ID: 20260923_0062
Revises: 20260907_0061
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260923_0062"
down_revision = "20260907_0061"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"


def _columns() -> set[str]:
    return {
        row[1]
        for row in op.get_bind().exec_driver_sql('pragma table_info("web_push_subscriptions")')
    }


def upgrade() -> None:
    # Older releases used last_failure_at for both transient failures and
    # disabled endpoints. Their disabled rows have no trustworthy provenance;
    # leave them opted out until the user explicitly enables notifications.
    if "provider_invalidated_at" not in _columns():
        op.add_column(
            "web_push_subscriptions",
            sa.Column("provider_invalidated_at", sa.String(), nullable=True),
        )


def downgrade() -> None:
    if "provider_invalidated_at" in _columns():
        op.drop_column("web_push_subscriptions", "provider_invalidated_at")

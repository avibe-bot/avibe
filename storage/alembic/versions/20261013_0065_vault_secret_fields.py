"""Add named extra fields to Vault secrets.

Revision ID: 20261013_0065
Revises: 20261012_0064
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261013_0065"
down_revision = "20261012_0064"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"


def _columns() -> set[str]:
    bind = op.get_bind()
    return {str(row[1]) for row in bind.exec_driver_sql("pragma table_info(vault_secrets)")}


def upgrade() -> None:
    if "fields" not in _columns():
        op.add_column("vault_secrets", sa.Column("fields", sa.Text(), nullable=True))


def downgrade() -> None:
    if "fields" in _columns():
        op.drop_column("vault_secrets", "fields")

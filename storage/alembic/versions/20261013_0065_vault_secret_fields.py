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
        # SQLite before 3.35 cannot DROP COLUMN. Batch rebuilding also preserves
        # the ordinary name constraint/index, but reflection skips expression
        # indexes, so restore the folded-name uniqueness guard explicitly.
        with op.batch_alter_table("vault_secrets") as batch:
            batch.drop_column("fields")
        op.get_bind().exec_driver_sql(
            "create unique index if not exists uq_vault_secrets_name_folded "
            "on vault_secrets (lower(name))"
        )

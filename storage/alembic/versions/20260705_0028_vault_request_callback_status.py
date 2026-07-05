"""vault request auto-resume callback status

Revision ID: 20260705_0028
Revises: 20260704_0027
Create Date: 2026-07-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260705_0028"
down_revision = "20260704_0027"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.exec_driver_sql(f'pragma table_info("{table_name}")')}


def _add_column_if_missing(table_name: str, column_name: str, column: sa.Column) -> None:
    if column_name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing("vault_requests", "callback_status", sa.Column("callback_status", sa.String(), nullable=True))
    op.create_index("ix_vault_requests_callback_status", "vault_requests", ["callback_status", "decided_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_vault_requests_callback_status", table_name="vault_requests", if_exists=True)
    # SQLite cannot drop columns without a table rebuild; keep the additive field on downgrade.
    return None

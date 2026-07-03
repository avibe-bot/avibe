"""persist vault grant purpose and selector source

Revision ID: 20260703_0026
Revises: 20260627_0025
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260703_0026"
down_revision = "20260627_0025"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first()
    return row is not None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.exec_driver_sql(f'pragma table_info("{table_name}")')}


def _add_column_if_missing(table_name: str, column_name: str, column: sa.Column) -> None:
    if column_name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if not _table_exists("vault_grants"):
        return
    _add_column_if_missing(
        "vault_grants",
        "source_selector",
        sa.Column("source_selector", sa.Text(), nullable=True),
    )
    _add_column_if_missing(
        "vault_grants",
        "purpose",
        sa.Column("purpose", sa.String(), nullable=False, server_default="run"),
    )


def downgrade() -> None:
    # SQLite cannot drop columns without rebuilding the table; keep additive fields.
    return None

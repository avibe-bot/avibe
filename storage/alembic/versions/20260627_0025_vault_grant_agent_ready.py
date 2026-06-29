"""persist resident-agent grant readiness

Revision ID: 20260627_0025
Revises: 20260626_0024
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260627_0025"
down_revision = "20260626_0024"
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
        "agent_ready",
        sa.Column("agent_ready", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_if_missing(
        "vault_grants",
        "agent_ready_at",
        sa.Column("agent_ready_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    # SQLite cannot drop columns without rebuilding the table; keep additive fields.
    return None

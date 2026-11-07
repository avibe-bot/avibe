"""Bound scheduler/supervisor cache reads to nonretired definitions.

Index only: retirement/removal facts and Run history are unchanged. Older
versions ignore the extra index; downgrade drops only the index.

Revision ID: 20261107_0074
Revises: 20261107_0073
Create Date: 2026-11-07
"""
from __future__ import annotations

from alembic import op

revision = "20261107_0074"
down_revision = "20261107_0073"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_run_definitions_resident"
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create index if not exists {_INDEX} on run_definitions (definition_type, created_at, id) "
    "where deleted_at is null and retired_at is null"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = 'run_definitions'"
    ).first():
        bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DROP_INDEX_SQL)

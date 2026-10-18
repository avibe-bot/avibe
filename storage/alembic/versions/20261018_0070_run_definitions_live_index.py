"""index the live definitions of each type

The background-work banner reads a session's Watches left running by the work it
dispatched: enabled Watches not soft-deleted, joined to their creator Run. A
removed definition keeps ``enabled = 1`` beside its ``deleted_at``, so
``ix_run_definitions_type_enabled`` walks every Watch ever removed::

    SEARCH run_definitions USING INDEX ix_run_definitions_type_enabled (definition_type=? AND enabled=?)

That grows with definition history, not with live work. A partial index over the
rows not deleted makes the same seek touch only live definitions:
``SEARCH run_definitions USING INDEX ix_run_definitions_live (definition_type=? AND enabled=?)``.

Index only: downgrade drops it, and an older build reads the same rows without
it.

Revision ID: 20261018_0070
Revises: 20261016_0069
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op

revision = "20261018_0070"
down_revision = "20261016_0069"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_run_definitions_live"
# Exported so the head-schema repair path in ``storage/migrations.py`` executes THIS
# statement rather than a retyped copy: SQLite matches a partial index's predicate
# against a query's by text.
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create index {_INDEX} on run_definitions (definition_type, enabled) "
    "where deleted_at is null"
)


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "run_definitions" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if "run_definitions" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)

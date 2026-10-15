"""index a callback child by the parent Run and actor it answers

Enqueueing a Run's callback finds an existing child for the same parent and
actor before it writes one (``SQLiteBackgroundTaskStore.enqueue_callback_run``).
No index covered ``parent_run_id``, so the lookup walked every ``agent_run`` row
through ``ix_agent_runs_type_status_created (run_type=?)`` and sorted them,
while holding SQLite's write lock (#2524)::

    SEARCH agent_runs USING INDEX ix_agent_runs_type_status_created (run_type=?)
    USE TEMP B-TREE FOR ORDER BY

On a long-running host that was 11,441 rows per callback enqueue, every other
writer waiting behind it. The partial index makes it a seek on one parent's
children, ordered as the lookup reads them: the query states both literals of
the predicate, so SQLite uses it (``SEARCH agent_runs USING INDEX
ix_agent_runs_callback_parent (parent_run_id=? AND source_actor=?)``, no temp
B-tree). Only callback children carry it, so other Runs cost it nothing.

Index only: downgrade drops it, and an older build reads the same rows without
it.

Revision ID: 20261015_0066
Revises: 20261013_0065
Create Date: 2026-10-15
"""

from __future__ import annotations

from alembic import op

revision = "20261015_0066"
down_revision = "20261013_0065"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_agent_runs_callback_parent"
# Exported so the head-schema repair path in ``storage/migrations.py`` executes THIS
# statement rather than a retyped copy.
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create index {_INDEX} on agent_runs "
    "(parent_run_id, source_actor, created_at, id) "
    "where run_type = 'agent_run' and source_kind = 'callback'"
)


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)

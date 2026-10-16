"""index a Session by its projected agent status

At every start, durable-delivery recovery clears the ``running`` projection of
Sessions whose Turns have all ended (``SessionTurnManager.project_durable_agent_status``).
No index led with ``agent_status``, so finding the projected-running Sessions
walked every active Session through ``ix_agent_sessions_status_activity
(status=?)`` and filtered each one, under SQLite's write lock (#2706)::

    SEARCH agent_sessions USING INDEX ix_agent_sessions_status_activity (status=?)

That grows with every Session ever kept, not with the work in hand. Leading
with ``agent_status`` makes it a seek on the few Sessions projected running:
``SEARCH agent_sessions USING INDEX ix_agent_sessions_agent_status
(agent_status=? AND status=?)``.

Index only: downgrade drops it, and an older build reads the same rows without
it.

Revision ID: 20261016_0068
Revises: 20261016_0067
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op

revision = "20261016_0068"
down_revision = "20261016_0067"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_agent_sessions_agent_status"
# Exported so the head-schema repair path in ``storage/migrations.py`` executes THIS
# statement rather than a retyped copy.
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = f"create index {_INDEX} on agent_sessions (agent_status, status)"


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_sessions" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_sessions" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)

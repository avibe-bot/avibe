"""index live run statuses by their definition

Task and Watch definition listings derive ``lifecycle_state`` with a correlated
``EXISTS`` over ``agent_runs``. The predicate constrains both the owning
``definition_id`` and the live status set, but the existing history indexes only
lead with ``definition_id``. SQLite therefore walks every historical Run for a
high-frequency definition before it can answer whether one live Run exists.

The plain composite index is deliberately not partial: the live status values
are bound parameters in the SQLAlchemy ``IN`` list, and SQLite cannot prove a
parameterized predicate implies a partial-index ``WHERE`` clause. A third
``created_at`` column does not improve this ``EXISTS`` shape; the status seek
already stops at the first matching row, while the existing
``(definition_id, created_at)`` indexes continue to serve ordered history
reads. It was measured against a 50,000-run definition on master.

Index only: no state is added or backfilled. An older Avibe ignores the extra
SQLite index, and downgrade removes only this index.

Revision ID: 20261107_0073
Revises: 20261030_0072
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op

revision = "20261107_0073"
down_revision = "20261030_0072"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_agent_runs_definition_status"
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = f"create index if not exists {_INDEX} on agent_runs (definition_id, status)"


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)

"""index agent_runs by definition and settlement time for derived health

The health window scans one definition's verdicts ordered by
``coalesce(completed_at, created_at) desc, id desc``. The existing
``ix_agent_runs_definition_created`` is ``(definition_id, created_at)``, which
serves the equality but NOT that ordering, so the sort was unindexed.

Index-only: this adds no state. Health stays derived from ``agent_runs`` on every
read — there is no counter, no column to keep in sync, and nothing to backfill.

Raw SQL rather than ``op.create_index`` because the second key is an expression;
SQLAlchemy cannot express it.

Revision ID: 20260728_0039
Revises: 20260727_0038
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "20260728_0039"
down_revision = "20260727_0038"
branch_labels = None
depends_on = None

_INDEX = "ix_agent_runs_definition_settled"


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(f"drop index if exists {_INDEX}")
    bind.exec_driver_sql(
        f"create index {_INDEX} on agent_runs "
        "(definition_id, coalesce(completed_at, created_at) desc, id desc)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(f"drop index if exists {_INDEX}")

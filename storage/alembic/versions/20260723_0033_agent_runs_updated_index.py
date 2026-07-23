"""index agent_runs.updated_at for the run-graph window scan

Revision ID: 20260723_0033
Revises: 20260723_0032
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op

revision = "20260723_0033"
down_revision = "20260723_0032"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def _indexes(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma index_list("{table}")').fetchall()
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    # The run-graph candidate scan filters agent_runs by a bare
    # ``updated_at >= cutoff`` range every poll/SSE refresh; without a
    # leading-timestamp index it scans the whole table (the other agent_runs
    # indexes all lead with a non-timestamp column).
    if "ix_agent_runs_updated" not in _indexes(bind, "agent_runs"):
        op.create_index("ix_agent_runs_updated", "agent_runs", ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    if "ix_agent_runs_updated" in _indexes(bind, "agent_runs"):
        op.drop_index("ix_agent_runs_updated", table_name="agent_runs")

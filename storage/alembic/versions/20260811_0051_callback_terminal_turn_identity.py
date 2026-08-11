"""deduplicate callbacks by callee terminal turn and callback session

A terminal Agent Turn is one callback event even when several accepted Agent
Runs participate in that Turn. The callback Session is the event recipient, so
the durable identity is ``(callback_terminal_turn_id, session_id)`` on callback
child Runs. Parent Runs remain separate audit records and each points to the
shared child through ``callback_run_id``.

Historical callback children are intentionally left with a null terminal Turn.
That avoids inventing provenance and lets the partial unique index coexist with
older per-Run callbacks.

Revision ID: 20260811_0051
Revises: 20260811_0050
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260811_0051"
down_revision = "20260811_0050"
branch_labels = None
depends_on = None

_INDEX = "uq_agent_runs_callback_terminal_turn_session"
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create unique index {_INDEX} on agent_runs "
    "(callback_terminal_turn_id, session_id) "
    "where run_type = 'agent_run' and source_kind = 'callback' "
    "and callback_terminal_turn_id is not null and session_id is not null"
)


def _tables(bind) -> set[str]:
    return {str(row[0]) for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")}


def _columns(bind) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns("agent_runs")}


def upgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    if "callback_terminal_turn_id" not in _columns(bind):
        op.add_column(
            "agent_runs",
            sa.Column("callback_terminal_turn_id", sa.String(), nullable=True),
        )
    bind.exec_driver_sql(DROP_INDEX_SQL)
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)
    # Keep the nullable provenance column so rollback never destroys callback
    # identity data. Older binaries ignore additive columns.

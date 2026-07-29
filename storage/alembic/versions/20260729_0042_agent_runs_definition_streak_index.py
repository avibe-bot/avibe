"""index a definition's history by its (created_at, id) position

``failure_streak`` runs once per pending owed notice, on the same two-second tick
as the eligibility lookup. It used to materialise the definition's ENTIRE settled
lifetime and slice the streak out in Python; it now finds the successes bracketing
the run with two ``LIMIT 1`` seeks and reads only the rows between them.

That rewrite needs an index this table did not have. The sequence key is
``(created_at, id)`` — ``created_at`` alone is not a position, because several
writers stamp a whole batch with one ISO string — so the seek asks for
``(created_at, id) < (?, ?)``. With ``ix_agent_runs_definition_created``
(``definition_id, created_at``) SQLite decomposes that row value into a weaker
scalar range and then sorts to recover the tie-break::

    SEARCH agent_runs USING INDEX ix_agent_runs_definition_created
        (definition_id=? AND created_at<?)
    USE TEMP B-TREE FOR LAST TERM OF ORDER BY

— and a temp sort over one definition's whole history is the unbounded read the
rewrite exists to remove, because it defeats the ``LIMIT 1``'s early exit. With
``id`` as the third key the row value becomes a real index constraint and the sort
disappears::

    SEARCH agent_runs USING INDEX ix_agent_runs_definition_streak
        (definition_id=? AND (created_at,id)<(?,?))

``ix_agent_runs_definition_created`` is left in place even though this index is a
strict superset of it: ``storage/migrations.py::_ensure_new_background_indexes``
recreates that name unconditionally for legacy databases, so dropping it here
would only make the resulting schema depend on which upgrade path a database took.

Index-only: no new state, nothing to backfill, health and streaks stay derived
from ``agent_runs`` on every read.

Raw SQL rather than ``op.create_index`` for symmetry with ``20260728_0039``.

Revision ID: 20260729_0042
Revises: 20260728_0041
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "20260729_0042"
down_revision = "20260728_0041"
branch_labels = None
depends_on = None

_INDEX = "ix_agent_runs_definition_streak"
# Exported so the head-schema repair path in ``storage/migrations.py`` executes THIS
# statement rather than a second copy of it — the same reason ``20260728_0039`` and
# ``20260728_0041`` export theirs. The note above about ``_ensure_new_background_indexes``
# recreating ``ix_agent_runs_definition_created`` unconditionally still holds; that
# helper now also installs this index, from this constant.
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = f"create index {_INDEX} on agent_runs (definition_id, created_at, id)"


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

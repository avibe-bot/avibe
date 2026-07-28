"""constrain the owed-notice backoff term in the index, not per row

``20260728_0040`` indexed the eligibility STATE expression followed by
``(created_at, id)``. That made SQLite report the index as used and skip the temp
sort — which is exactly what the query-plan test asserted — while the backoff
condition stayed unindexed, so the engine still walked EVERY pending-state entry
evaluating it. With many notices waiting on retry and none eligible, the two-second
tick was again unbounded in the number of pending notices.

The plan before this migration, with 40 backed-off notices::

    SEARCH agent_runs USING INDEX ix_agent_runs_owed_notice (<expr>=?)

— one constrained term. After, with the backoff expression as the second index
column::

    SEARCH agent_runs USING INDEX ix_agent_runs_owed_notice (<expr>=? AND <expr><?)

Measured on a 4000-row table where nothing is eligible, this is the difference
between walking all 4000 entries and ~50 VM steps.

Two things had to change together for the range term to be usable at all:

* ``next_attempt_at`` is now always written as an instant, never ``NULL``. A
  nullable column forces ``(x IS NULL OR x <= now)``, and a disjunction cannot be
  an index range constraint.
* the query orders on the index prefix, so the ordering is free and the ``LIMIT``
  still short-circuits.

The ``CASE json_valid`` guard is required on the new expression for the same
write-time reason as the state expression: an index expression is evaluated on
every INSERT/UPDATE, so a bare ``json_extract`` would make a row with an
unparseable blob unwritable.

Revision ID: 20260728_0041
Revises: 20260728_0040
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "20260728_0041"
down_revision = "20260728_0040"
branch_labels = None
depends_on = None

_INDEX = "ix_agent_runs_owed_notice"
# Unqualified column names: SQLite rejects the "." operator inside an index
# expression. Must stay byte-identical to ``OWED_NOTICE_STATE_SQL`` /
# ``OWED_NOTICE_NEXT_ATTEMPT_SQL`` in ``storage/background.py``, or the planner
# silently declines to match and the scan comes back with no signal.
_STATE_EXPR = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.owed_failure_notice.state') END"
)
# ``coalesce(..., '')`` is what makes this usable as a RANGE constraint: a null
# next-attempt means "eligible now", and expressing that as a disjunction would
# leave the term filtered per row while the plan still named the index.
_NEXT_ATTEMPT_EXPR = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN coalesce(json_extract(metadata_json, '$.owed_failure_notice.next_attempt_at'), '') END"
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
    bind.exec_driver_sql(f"drop index if exists {_INDEX}")
    bind.exec_driver_sql(
        f"create index {_INDEX} on agent_runs "
        f"({_STATE_EXPR}, {_NEXT_ATTEMPT_EXPR}, created_at, id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(f"drop index if exists {_INDEX}")
    bind.exec_driver_sql(f"create index {_INDEX} on agent_runs ({_STATE_EXPR}, created_at, id)")

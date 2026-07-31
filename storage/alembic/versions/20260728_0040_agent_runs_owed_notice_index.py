"""index the owed-failure-notice eligibility expression for the 2s drain tick

The drain runs every two seconds and asks "is any notice eligible to deliver?".
Filtering the notice state in SQL was not enough to bound that: the planner could
only seek on ``status='failed'`` and then had to evaluate the unindexed
``json_valid`` / ``json_extract`` terms on EVERY failed row before concluding
nothing was eligible. Measured before this migration::

    SEARCH agent_runs USING INDEX ix_agent_runs_status_created (status=?)
    USE TEMP B-TREE FOR LAST TERM OF ORDER BY

The temp sort is the second half of the problem: it defeats the ``LIMIT``'s early
exit, because SQLite must order the whole failed set before it can return ten
rows. So the steady-state cost of a tick grew with total failed-run history.

Indexing the eligibility EXPRESSION makes it seekable and supplies the
``(created_at, id)`` ordering, so the plan becomes a bounded seek with no temp
sort. A *partial* index (``WHERE json_extract(...) = 'pending'``) was tried first
and rejected: SQLite's partial-index implication analysis does not match these
terms, and the planner silently ignored the index — verified with EXPLAIN QUERY
PLAN on a 5000-row table both with and without ANALYZE.

The ``CASE json_valid`` guard is load-bearing at WRITE time here, which it is not
in the query. An index expression is evaluated on every INSERT and UPDATE, so a
bare ``json_extract`` would raise ``malformed JSON`` and make a row with an
unparseable blob UNWRITABLE — turning a read-side degradation into a write-side
outage. CASE short-circuits, so the extract never runs on invalid JSON.

The expression must stay byte-identical to the one
``SQLiteBackgroundTaskStore.list_owed_failure_notices`` compiles, or the planner
will not match it; ``tests/test_harness_failure_visibility.py`` asserts the query
plan names this index, so a drift fails the suite rather than silently
reintroducing the scan.

Revision ID: 20260728_0040
Revises: 20260728_0039
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "20260728_0040"
down_revision = "20260728_0039"
branch_labels = None
depends_on = None

_INDEX = "ix_agent_runs_owed_notice"
# Unqualified column names: SQLite rejects the "." operator inside an index
# expression. The planner still matches this against the query's qualified form.
_STATE_EXPR = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.owed_failure_notice.state') END"
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
        f"create index {_INDEX} on agent_runs ({_STATE_EXPR}, created_at, id)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_runs" not in _tables(bind):
        return
    bind.exec_driver_sql(f"drop index if exists {_INDEX}")

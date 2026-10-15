"""bound Vibey recovery to real unconsumed inputs

Vibey's T3 recovery looks for accepted input Messages whose ``context_seq`` is
still null.  Hidden ``agent_initiated`` harness resume rows are durable control
ownership, not model inputs, and remain null forever.  Filtering those rows in
Python made every resume walk and hydrate the whole retry history.

The partial index contains only real recovery candidates.  Its predicate is
shared with ``storage.models`` and the three recovery queries so SQLite can
seek the growing Message table by ``context_seq`` and, for a Session query, by
``session_id``.  The explicit query predicate remains necessary for databases
that have not applied this migration yet.

Revision ID: 20261016_0067
Revises: 20261015_0066
Create Date: 2026-10-16
"""

from __future__ import annotations

from alembic import op

revision = "20261016_0067"
down_revision = "20261015_0066"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_messages_vibey_recovery_input"
# Frozen migration history: do not import the runtime predicate here.  A later
# recovery-shape change must be a new migration, not a rewrite of this DDL.
_RECOVERY_INPUT_PREDICATE = (
    "context_seq is null and type in "
    "('user', 'harness', 'agent_initiated', 'annotation') "
    "and not (author is 'harness' and type is 'agent_initiated' "
    "and source is 'harness' and json_type("
    "case when json_valid(metadata_json) = 1 then metadata_json else '{}' end, "
    "'$.vibey_resume') is 'true')"
)
# Exported so a head-schema repair path can reuse the exact DDL if this index is
# later added to that repair inventory.  The predicate must not be retyped:
# SQLite's partial-index matcher is syntax-sensitive.
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create index {_INDEX} on messages (context_seq, session_id, id) "
    f"where {_RECOVERY_INPUT_PREDICATE}"
)


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if "messages" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if "messages" not in _tables(bind):
        return
    bind.exec_driver_sql(DROP_INDEX_SQL)

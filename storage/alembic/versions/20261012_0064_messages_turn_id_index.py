"""index the turn a Message row belongs to

Settling every Model Hub turn stamps its mode on the Message rows whose
``metadata_json`` names that turn (``ModelHubService.note_turn_mode``). The turn
id lives only in that JSON, so the stamp scanned every Message, overflow chains
included, while holding the database write lock. Measured on a copy of a
long-running host's database (195,468 Messages, about 405 MB)::

    SCAN messages

Indexing the turn-id EXPRESSION makes the stamp a seek on the handful of rows of
one turn. The index is partial, so the nine in ten Messages without a turn id
cost it nothing: SQLite implies the ``(expr) IS NOT NULL`` predicate from the
query's ``expr = ?`` term and uses the index (``SEARCH messages USING INDEX
ix_messages_turn_id (<expr>=?)`` on SQLite 3.50.4). Unlike the owed-notice
indexes of 0040, whose ``json_extract(...) = 'pending'`` predicate the planner
could not imply, this predicate is matched; the plan test pins that.

The ``CASE json_valid`` guard is load-bearing at WRITE time: an index expression
is evaluated on every INSERT and UPDATE, so a bare ``json_extract`` would make a
Message with an unparseable ``metadata_json`` unwritable.

The expression must stay byte-identical to ``MESSAGE_TURN_ID_SQL`` in
``storage/models.py``, which the stamp compiles, or the planner will not match
it; ``tests/test_model_hub_turn_mode_stamp.py`` asserts the plan names this index,
so a drift fails the suite rather than silently restoring the scan.

Revision ID: 20261012_0064
Revises: 20261002_0063
Create Date: 2026-10-12
"""

from __future__ import annotations

from alembic import op

revision = "20261012_0064"
down_revision = "20261002_0063"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_INDEX = "ix_messages_turn_id"
# Unqualified column name: SQLite rejects the "." operator inside an index
# expression. The planner still matches the query's unqualified twin.
_TURN_ID_EXPR = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.turn_id') END"
)
# Exported so the head-schema repair path in ``storage/migrations.py`` executes THIS
# statement rather than a retyped copy, which the planner would not match.
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create index {_INDEX} on messages ({_TURN_ID_EXPR}) where ({_TURN_ID_EXPR}) is not null"
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

"""Shared SQL for Vibey's bounded unconsumed-input recovery queries.

The predicate mirrors ``message_deliveries.is_vibey_resume_payload`` at the
materialized Message boundary.  Hidden Vibey retry rows are control ownership,
not model inputs: they remain durable rows with a null ``context_seq`` but must
not enter the growing recovery population.

Keep this expression byte-identical between the model metadata, Alembic DDL,
and recovery queries.  The literal input values and marker expression are
intentional: SQLite partial-index matching is syntax-sensitive, and a bound
``IN`` list or a Python-side marker check would either miss the index or scan
hidden retry history first.
"""

from __future__ import annotations


# Unqualified column names are required by SQLite inside a partial-index WHERE
# clause.  The same clause is used in the joined recovery SELECTs, where these
# names are unambiguous because only ``messages`` has them.
VIBEY_RECOVERY_INPUT_PREDICATE = (
    "context_seq is null and type in "
    "('user', 'harness', 'agent_initiated', 'annotation') "
    "and not (author is 'harness' and type is 'agent_initiated' "
    "and source is 'harness' and json_type("
    "case when json_valid(metadata_json) = 1 then metadata_json else '{}' end, "
    "'$.vibey_resume') is 'true')"
)

__all__ = ["VIBEY_RECOVERY_INPUT_PREDICATE"]

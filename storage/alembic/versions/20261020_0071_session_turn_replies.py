"""record each Turn's last delivered reply

A Run whose Turn ends without a reply of its own settles with the Turn's last
delivered reply (``docs/plans/reply-and-turn-end.md``, rule 5). The dispatcher
writes that reply's Run copy into ``session_turn_replies`` when it delivers the
reply and reads it back at the Turn end, so a Turn rerun after a restart, and the
terminal snapshot that reconciliation reads, settle with what the user saw.

One row per Turn, keyed by the Turn's id, written and read by primary key only. A
table of its own, so it never takes part in the Turn's compare-and-set and no
``session_turns`` read sees it. Additive: released versions never read it, and
downgrade drops it.

Revision ID: 20261020_0071
Revises: 20261018_0070
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261020_0071"
down_revision = "20261018_0070"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_TABLE = "session_turn_replies"


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def upgrade() -> None:
    tables = _tables(op.get_bind())
    if "session_turns" not in tables or _TABLE in tables:
        return
    op.create_table(
        _TABLE,
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("reply_text", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("turn_id"),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["session_turns.id"],
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
            name="fk_session_turn_replies_turn",
        ),
    )


def downgrade() -> None:
    if _TABLE in _tables(op.get_bind()):
        op.drop_table(_TABLE)

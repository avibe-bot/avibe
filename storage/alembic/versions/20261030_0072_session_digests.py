"""keep each Vibey Session's latest memory in session_digests

Other Sessions recall a Session by its latest checkpoint's mind, memo, and now
(``docs/plans/agent-core-contracts/context.md`` section 7). The transcript store
rewrites a Session's one row in the transaction that appends a
``context_compaction`` row; ``vibe data query`` reads it through its ``digests``
view, so a search does not parse every checkpoint's JSON, skills included.

The backfill gives every Session its latest checkpoint that holds any of the
three, read through ``ix_agent_events_session_type_created_id`` one Session at a
time. A ``checkpoint-v4`` row has no mind or memo: its whole text is the now. The
memo is rendered here as the harness showed it when this revision was written, so
the revision does what it did whatever the harness later becomes. Additive and
derived: released versions never read the table, and downgrade drops it.

Revision ID: 20261030_0072
Revises: 20261020_0071
Create Date: 2026-10-30
"""

from __future__ import annotations

import json
from typing import Any, Optional

import sqlalchemy as sa
from alembic import op

revision = "20261030_0072"
down_revision = "20261020_0071"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "backfill"

_TABLE = "session_digests"
_MEMO_HEADING = "# 5. The user's words"
_MEMO_CUT = "[cut short; the whole message is #{seq} in the earlier record]"


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")
    }


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _memo(value: Any) -> Optional[str]:
    if not isinstance(value, list):
        return None
    lines = [_MEMO_HEADING]
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        seq = item.get("seq")
        first, *rest = item["text"].replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines.append(f"- (#{seq}) {first}")
        lines.extend(f"  {line}" for line in rest)
        if item.get("truncated") is True:
            lines.append(f"  {_MEMO_CUT.format(seq=seq)}")
    return "\n".join(lines) if len(lines) > 1 else None


def _digest(raw: Any) -> Optional[tuple[Optional[str], Optional[str], Optional[str]]]:
    try:
        payload = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    blocks = (_text(payload.get("mind")), _memo(payload.get("memo")), _text(payload.get("checkpoint")))
    return blocks if any(blocks) else None


def _backfill(bind) -> None:
    sessions = [row[0] for row in bind.exec_driver_sql("select id from agent_sessions")]
    for session_id in sessions:
        rows = bind.exec_driver_sql(
            "select id, created_at, content_json from agent_events "
            "where session_id = ? and event_type = 'context_compaction' and visibility = 'context' "
            "order by created_at desc, id desc",
            (session_id,),
        ).fetchall()
        for compaction_id, created_at, raw in rows:
            blocks = _digest(raw)
            if blocks is None:
                continue
            bind.exec_driver_sql(
                f"insert into {_TABLE} (session_id, compaction_id, updated_at, mind, memo, now) "
                "values (?, ?, ?, ?, ?, ?)",
                (session_id, compaction_id, created_at, *blocks),
            )
            break


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "agent_sessions" not in tables or _TABLE in tables:
        return
    op.create_table(
        _TABLE,
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("compaction_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("mind", sa.Text(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("now", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
    )
    if "agent_events" in tables:
        _backfill(bind)


def downgrade() -> None:
    if _TABLE in _tables(op.get_bind()):
        op.drop_table(_TABLE)

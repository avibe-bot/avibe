"""index the exact Message transcript-entry order

Queued input enters the visible transcript when its Delivery is accepted, while
directly accepted communication enters at creation. Both timestamps are
canonicalized to fixed-width UTC microseconds, making
``coalesce(delivered_at, created_at)`` an exact, lexically sortable key. The
matching expression index lets transcript cursor reads stop at their page limit.

Revision ID: 20260804_0046
Revises: 20260802_0045
Create Date: 2026-08-04
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260804_0046"
down_revision = "20260802_0045"
branch_labels = None
depends_on = None

_INDEX = "ix_messages_session_transcript_id"
_ORDER_EXPR = "coalesce(delivered_at, created_at)"
DROP_INDEX_SQL = f"drop index if exists {_INDEX}"
CREATE_INDEX_SQL = (
    f"create index {_INDEX} on messages (session_id, {_ORDER_EXPR}, id)"
)


def _canonical_timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parseable = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        instant = datetime.fromisoformat(parseable)
    except ValueError:
        return text
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("select id, created_at, delivered_at from messages")
    ).mappings()
    updates: list[dict[str, str | None]] = []
    for row in rows:
        created_at = _canonical_timestamp(row["created_at"])
        delivered_at = _canonical_timestamp(row["delivered_at"])
        if created_at != row["created_at"] or delivered_at != row["delivered_at"]:
            updates.append(
                {
                    "id": str(row["id"]),
                    "created_at": created_at,
                    "delivered_at": delivered_at,
                }
            )
    if updates:
        bind.execute(
            sa.text(
                "update messages set created_at=:created_at, "
                "delivered_at=:delivered_at where id=:id"
            ),
            updates,
        )
    bind.exec_driver_sql(DROP_INDEX_SQL)
    bind.exec_driver_sql(CREATE_INDEX_SQL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DROP_INDEX_SQL)

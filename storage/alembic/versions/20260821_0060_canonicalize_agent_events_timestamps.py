"""canonicalize agent_events trace timestamps

Revision ID: 20260821_0060
Revises: 20260821_0059
Create Date: 2026-08-21
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "20260821_0060"
down_revision = "20260821_0059"
branch_labels = None
depends_on = None

# Rows produced before this normalization (e.g. events migrated from legacy
# message timestamps at 20260801_0044) may carry offset or fractional forms.
# Fixed-width UTC microseconds make lexical comparison chronological without
# throwing away the sub-second ordering used by activity reconstruction.


def _canonical_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonicalize(bind, column: str) -> None:
    # Only tool_call/trace rows — the retention scan's subject — are
    # rewritten. Other event types keep their original precision: e.g.
    # migrated silent_terminal timestamps act as ordering anchors for
    # session fork, where truncating sub-second precision could reorder a
    # terminal against its source message.
    rows = bind.execute(
        sa.text(
            f"""
            select id, {column}
            from agent_events
            where event_type = 'tool_call'
              and visibility = 'trace'
              and {column} is not null
              and {column} != ''
            """
        )
    ).fetchall()
    updates = []
    for event_id, value in rows:
        canonical = _canonical_timestamp(value)
        if canonical is not None and canonical != value:
            updates.append({"id": event_id, "value": canonical})
    if updates:
        bind.execute(
            sa.text(f"update agent_events set {column} = :value where id = :id"),
            updates,
        )


def upgrade() -> None:
    _canonicalize(op.get_bind(), "created_at")
    _canonicalize(op.get_bind(), "updated_at")


def downgrade() -> None:
    # Canonical UTC timestamps are not reversible to the original offset shape.
    pass

"""pause enabled scheduled Tasks whose owner is no longer available

Revision ID: 20260811_0052
Revises: 20260811_0051
Create Date: 2026-08-11
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "20260811_0052"
down_revision = "20260811_0051"
branch_labels = None
depends_on = None


_OWNER_SESSION_EXPR = (
    "CASE WHEN json_valid(run_definitions.metadata_json) = 1 THEN "
    "CASE WHEN json_type(run_definitions.metadata_json, '$.created_by.caller.session_id') = 'text' "
    "THEN nullif(trim(json_extract(run_definitions.metadata_json, '$.created_by.caller.session_id')), '') "
    "END END"
)


def upgrade() -> None:
    """Stop pre-owner-teardown Tasks that have no surviving execution target.

    Pure command and create-per-run Tasks deliberately leave ``session_id`` NULL.
    If their creating Session was removed before owner-aware teardown shipped, the
    definitions otherwise remain enabled and continue firing without belonging to
    any Session that can show them in the banner. Pausing them preserves the
    definition in the unfiltered Task list while preventing invisible execution.
    """

    bind = op.get_bind()
    now = datetime.now(timezone.utc).isoformat()
    owner_expr = _OWNER_SESSION_EXPR
    bind.execute(
        sa.text(
            "UPDATE run_definitions "
            "SET enabled = 0, "
            "metadata_json = json_set(metadata_json, '$.orphaned_task_owner', "
            "json_object('reason_code', 'task_owner_session_unavailable', "
            "'owner_session_id', (" + owner_expr + "))), "
            "updated_at = :updated_at "
            "WHERE definition_type = 'scheduled' "
            "AND enabled <> 0 "
            "AND deleted_at IS NULL "
            "AND nullif(trim(session_id), '') IS NULL "
            "AND (" + owner_expr + ") IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM agent_sessions "
            "WHERE agent_sessions.id = (" + owner_expr + ") "
            "AND agent_sessions.status <> 'archived'"
            ")"
        ),
        {"updated_at": now},
    )


def downgrade() -> None:
    # The previous enabled/metadata values are not recoverable without a separate
    # audit table; leaving reconciled definitions paused is safer than resuming
    # work with no Session owner or execution target.
    pass

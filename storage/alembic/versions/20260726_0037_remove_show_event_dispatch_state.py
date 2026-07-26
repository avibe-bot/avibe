"""remove the Show event dispatch state machine

Revision ID: 20260726_0037
Revises: 20260726_0036
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260726_0037"
down_revision = "20260726_0036"
branch_labels = None
depends_on = None

_NONE_STATE = '{"state":"none"}'
_ACCEPTED_STATE = '{"state":"accepted"}'
_PENDING_TYPE = "pending"
_HARNESS_TYPE = "harness"
_SHOW_TRIGGER_KINDS = {
    "human.annotation.created": "show_annotation",
    "human.intent.submitted": "show_intent",
}


def _columns(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def _reconcile_pending_show_messages() -> None:
    for event_type, trigger_kind in _SHOW_TRIGGER_KINDS.items():
        event_match = (
            "select 1 from show_session_events "
            "where show_session_events.message_id = messages.id "
            "and show_session_events.event_type = :event_type"
        )
        op.execute(
            sa.text(
                "update messages "
                "set author = :harness_type, "
                "source = :harness_type, "
                "author_name = :trigger_kind, "
                "author_id = ("
                "select show_session_events.id from show_session_events "
                "where show_session_events.message_id = messages.id "
                "and show_session_events.event_type = :event_type "
                "limit 1"
                ") "
                "where type = :pending_type "
                f"and exists ({event_match})"
            ).bindparams(
                event_type=event_type,
                trigger_kind=trigger_kind,
                harness_type=_HARNESS_TYPE,
                pending_type=_PENDING_TYPE,
            )
        )
        op.execute(
            sa.text(
                "update messages "
                "set type = :harness_type "
                "where type = :pending_type "
                "and exists ("
                f"{event_match} "
                "and show_session_events.dispatch_state = :accepted_state"
                ")"
            ).bindparams(
                event_type=event_type,
                accepted_state=_ACCEPTED_STATE,
                harness_type=_HARNESS_TYPE,
                pending_type=_PENDING_TYPE,
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    if "dispatch_state" in _columns(bind, "show_session_events"):
        _reconcile_pending_show_messages()
        # SQLite before 3.35 needs the table-rebuild form of DROP COLUMN.
        with op.batch_alter_table("show_session_events") as batch_op:
            batch_op.drop_column("dispatch_state")


def downgrade() -> None:
    bind = op.get_bind()
    if "dispatch_state" not in _columns(bind, "show_session_events"):
        op.add_column(
            "show_session_events",
            sa.Column(
                "dispatch_state",
                sa.Text(),
                nullable=False,
                server_default=_NONE_STATE,
            ),
        )
        op.execute(
            sa.text(
                "update show_session_events "
                "set dispatch_state = :accepted_state"
            ).bindparams(accepted_state=_ACCEPTED_STATE)
        )
        op.execute(
            sa.text(
                "update show_session_events "
                "set dispatch_state = :none_state "
                "where exists ("
                "select 1 from messages "
                "where messages.id = show_session_events.message_id "
                "and messages.type = :pending_type"
                ")"
            ).bindparams(
                none_state=_NONE_STATE,
                pending_type=_PENDING_TYPE,
            )
        )

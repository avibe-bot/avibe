"""remove the Show event dispatch state machine

Revision ID: 20260726_0037
Revises: 20260726_0036
Create Date: 2026-07-26
"""

from __future__ import annotations

import json

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


def _reconcile_show_messages() -> None:
    bind = op.get_bind()
    events = list(
        bind.execute(
            sa.text(
                "select id, event_type, message_id, payload_json, dispatch_state "
                "from show_session_events "
                "where message_id is not null"
            )
        ).mappings()
    )
    for event in events:
        trigger_kind = _SHOW_TRIGGER_KINDS.get(str(event["event_type"]))
        payload = json.loads(str(event["payload_json"]))
        if trigger_kind is None or not bool(payload.get("dispatch")):
            continue
        accepted = (
            json.loads(str(event["dispatch_state"])).get("state") == "accepted"
        )
        op.execute(
            sa.text(
                "update messages "
                "set author = :harness_type, "
                "source = :harness_type, "
                "author_name = :trigger_kind, "
                "author_id = :event_id, "
                "type = case "
                "when type != :pending_type or :accepted then :harness_type "
                "else type end "
                "where id = :message_id"
            ).bindparams(
                accepted=accepted,
                event_id=event["id"],
                trigger_kind=trigger_kind,
                harness_type=_HARNESS_TYPE,
                message_id=event["message_id"],
                pending_type=_PENDING_TYPE,
            )
        )


def _restore_legacy_show_message_identity() -> None:
    bind = op.get_bind()
    events = list(
        bind.execute(
            sa.text(
                "select event_type, message_id, payload_json "
                "from show_session_events "
                "where message_id is not null"
            )
        ).mappings()
    )
    for event in events:
        if str(event["event_type"]) not in _SHOW_TRIGGER_KINDS:
            continue
        payload = json.loads(str(event["payload_json"]))
        if not bool(payload.get("dispatch")):
            continue
        op.execute(
            sa.text(
                "update messages "
                "set author = :user_type, "
                "source = null, "
                "author_name = null, "
                "author_id = null, "
                "type = case "
                "when type = :harness_type then :user_type "
                "else type end "
                "where id = :message_id"
            ).bindparams(
                harness_type=_HARNESS_TYPE,
                message_id=event["message_id"],
                user_type="user",
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    if "dispatch_state" in _columns(bind, "show_session_events"):
        _reconcile_show_messages()
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
        _restore_legacy_show_message_identity()

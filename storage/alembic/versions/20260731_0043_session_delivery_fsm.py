"""separate submitted deliveries from accepted Session messages

Revision ID: 20260731_0043
Revises: 20260729_0042
Create Date: 2026-07-31
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20260731_0043"
down_revision = "20260729_0042"
branch_labels = None
depends_on = None

_PSEUDO_TYPES = ("queued", "pending", "draft", "harness_dedupe", "silent", "tool_call")
_OLD_INBOX_ACTIVITY_PREDICATE = (
    "session_id is not null and type not in "
    "('queued', 'draft', 'pending', 'harness_dedupe', 'silent')"
)
_INBOX_ACTIVITY_SQL = (
    "create index ix_messages_inbox_activity "
    "on messages (platform, session_id, created_at desc, id desc) "
    "where session_id is not null"
)
_INBOX_AGENT_REPLY_SQL = (
    "create index ix_messages_inbox_agent_reply "
    "on messages (platform, session_id, created_at desc, id desc) "
    "where session_id is not null and type in ('result', 'notify', 'error')"
)
_INBOX_USER_SEND_SQL = (
    "create index ix_messages_inbox_user_send "
    "on messages (platform, session_id, created_at desc, id desc) "
    "where session_id is not null and ((author = 'user' and type = 'user') "
    "or (author = 'harness' and type = 'harness') "
    "or (author = 'harness' and type = 'annotation'))"
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _message_snapshot(row: sa.RowMapping) -> dict[str, object]:
    return {
        "scope_id": row["scope_id"],
        "session_id": row["session_id"],
        "platform": row["platform"],
        "author": row["author"],
        "type": (
            "harness"
            if row["type"] == "harness_dedupe"
            else "user"
            if row["type"] in {"queued", "pending", "draft"}
            else row["type"]
        ),
        "author_id": row["author_id"],
        "author_name": row["author_name"],
        "source": row["source"],
        "native_message_id": row["native_message_id"],
        "parent_native_message_id": row["parent_native_message_id"],
        "content_text": row["content_text"],
        "content_json": row["content_json"] or "{}",
        "metadata_json": row["metadata_json"] or "{}",
        "read_at": row["read_at"],
    }


def _metadata(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _legacy_event_id(bind, message_id: str, event_type: str) -> str:
    base = f"evt_legacy_{hashlib.sha256(f'{event_type}:{message_id}'.encode()).hexdigest()[:24]}"
    candidate = base
    suffix = 0
    while bind.execute(sa.text("select 1 from agent_events where id = :id"), {"id": candidate}).first():
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


def _migrate_legacy_trace(bind, row: sa.RowMapping) -> None:
    event_type = "tool_call" if row["type"] == "tool_call" else "legacy_silent_terminal"
    existing = bind.execute(
        sa.text(
            "select id from agent_events "
            "where session_id is :session_id and event_type = :event_type "
            "and json_valid(metadata_json) = 1 "
            "and json_extract(metadata_json, '$.legacy_message_id') = :message_id limit 1"
        ),
        {
            "session_id": row["session_id"],
            "event_type": event_type,
            "message_id": row["id"],
        },
    ).first()
    if existing:
        return
    metadata = _metadata(row["metadata_json"])
    metadata.update(
        {
            "legacy_message_id": row["id"],
            "legacy_message_type": row["type"],
            "migration_revision": revision,
        }
    )
    bind.execute(
        sa.text(
            "insert into agent_events ("
            "id, scope_id, session_id, turn_id, run_id, platform, agent_name, backend, "
            "event_type, visibility, sequence, content_text, content_json, metadata_json, "
            "source, created_at, updated_at"
            ") values ("
            ":id, :scope_id, :session_id, null, null, :platform, :agent_name, null, "
            ":event_type, 'trace', null, :content_text, :content_json, :metadata_json, "
            ":source, :created_at, :updated_at)"
        ),
        {
            "id": _legacy_event_id(bind, str(row["id"]), event_type),
            "scope_id": row["scope_id"],
            "session_id": row["session_id"],
            "platform": row["platform"],
            "agent_name": row["author_name"],
            "event_type": event_type,
            "content_text": row["content_text"],
            "content_json": row["content_json"] or "{}",
            "metadata_json": _json(metadata),
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
    )


def _migrate_pseudo_messages(bind) -> None:
    rows = bind.execute(
        sa.text(
            "select id, scope_id, session_id, platform, author, type, author_id, "
            "author_name, source, native_message_id, parent_native_message_id, "
            "content_text, content_json, metadata_json, created_at, updated_at, "
            "delivered_at, read_at from messages where type in "
            "('queued','pending','draft','harness_dedupe','silent','tool_call') "
            "order by created_at, id"
        )
    ).mappings()
    latest_drafts: dict[str, sa.RowMapping] = {}
    for row in rows:
        kind = str(row["type"])
        session_id = str(row["session_id"] or "")
        if kind == "draft":
            if session_id:
                latest_drafts[session_id] = row
            continue
        if kind in {"silent", "tool_call"}:
            _migrate_legacy_trace(bind, row)
            continue
        if not session_id:
            # There is no Session queue owner to migrate to; retain an audit trace
            # and remove the pseudo communication record.
            _migrate_legacy_trace(bind, {**dict(row), "type": "tool_call"})
            continue
        snapshot = _message_snapshot(row)
        snapshot_json = _json(snapshot)
        metadata = _metadata(row["metadata_json"])
        dispatch_text = str(
            metadata.get("_queued_dispatch_text")
            or row["content_text"]
            or _metadata(row["content_json"]).get("text")
            or ""
        )
        state = {
            "queued": "queued",
            "pending": "reconciling_start",
            "harness_dedupe": "retired",
        }[kind]
        attempt_id = f"atm_migration_{row['id']}" if kind == "pending" else None
        history = {
            "version": 1,
            "events": [
                {
                    "at": row["updated_at"] or row["created_at"],
                    "kind": "migration",
                    "revision": revision,
                    "legacy_type": kind,
                    "outcome": "unknown" if kind == "pending" else "moved",
                }
            ],
        }
        bind.execute(
            sa.text(
                "insert into message_deliveries ("
                "id, session_id, message_id, priority, state, snapshot_json, snapshot_sha256, "
                "dispatch_text, dispatch_sha256, dedupe_key, accepted_turn_id, "
                "current_attempt_id, current_attempt_kind, current_target_turn_id, "
                "current_expected_native_turn_id, current_receipt_outcome, current_receipt_json, "
                "current_attempt_opened_at, delivery_history_json, version, submitted_at, "
                "updated_at, materialized_at, retired_at"
                ") values ("
                ":id, :session_id, null, 'p3', :state, :snapshot_json, :snapshot_sha256, "
                ":dispatch_text, :dispatch_sha256, :dedupe_key, null, :attempt_id, "
                ":attempt_kind, null, null, :receipt_outcome, :receipt_json, :attempt_opened_at, "
                ":history_json, 1, :submitted_at, :updated_at, null, :retired_at)"
            ),
            {
                "id": row["id"],
                "session_id": session_id,
                "state": state,
                "snapshot_json": snapshot_json,
                "snapshot_sha256": _hash(snapshot_json),
                "dispatch_text": dispatch_text,
                "dispatch_sha256": _hash(dispatch_text),
                "dedupe_key": (
                    f"legacy:{row['platform']}:{row['native_message_id']}"
                    if row["native_message_id"]
                    else None
                ),
                "attempt_id": attempt_id,
                "attempt_kind": "start" if attempt_id else None,
                "receipt_outcome": "unknown" if attempt_id else None,
                "receipt_json": _json({"reason": "legacy_pending_may_have_written"}) if attempt_id else "{}",
                "attempt_opened_at": row["created_at"] if attempt_id else None,
                "history_json": _json(history),
                "submitted_at": row["created_at"],
                "updated_at": row["updated_at"],
                "retired_at": row["updated_at"] if state == "retired" else None,
            },
        )
        bind.execute(
            sa.text(
                "update show_session_events set delivery_id = :id, message_id = null "
                "where message_id = :id"
            ),
            {"id": row["id"]},
        )
        bind.execute(
            sa.text("update media_objects set message_id = null where message_id = :id"),
            {"id": row["id"]},
        )
    for session_id, row in latest_drafts.items():
        bind.execute(
            sa.text(
                "update agent_sessions set composer_draft_text = :text, "
                "composer_draft_updated_at = :updated_at where id = :session_id"
            ),
            {
                "text": row["content_text"] or "",
                "updated_at": row["updated_at"] or row["created_at"],
                "session_id": session_id,
            },
        )
    bind.execute(
        sa.text(
            "delete from messages where type in "
            "('queued','pending','draft','harness_dedupe','silent','tool_call')"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "message_deliveries" in tables and "session_turns" in tables:
        for index_name, create_sql in (
            ("ix_messages_inbox_activity", _INBOX_ACTIVITY_SQL),
            ("ix_messages_inbox_agent_reply", _INBOX_AGENT_REPLY_SQL),
            ("ix_messages_inbox_user_send", _INBOX_USER_SEND_SQL),
        ):
            op.execute(f"drop index if exists {index_name}")
            op.execute(create_sql)
        return
    if {"message_deliveries", "session_turns"} & tables:
        raise RuntimeError("durable Message delivery schema is only partially present")

    op.add_column(
        "agent_sessions",
        sa.Column(
            "queue_hold_state",
            sa.String(),
            sa.CheckConstraint(
                "queue_hold_state in ('open', 'held')",
                name="ck_agent_sessions_queue_hold_state",
            ),
            server_default="open",
            nullable=False,
        ),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("queue_hold_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("agent_sessions", sa.Column("queue_held_at", sa.String(), nullable=True))
    op.add_column("agent_sessions", sa.Column("composer_draft_text", sa.Text(), nullable=True))
    op.add_column("agent_sessions", sa.Column("composer_draft_updated_at", sa.String(), nullable=True))
    with op.batch_alter_table("show_session_events") as batch:
        batch.add_column(sa.Column("delivery_id", sa.String(), nullable=True))

    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("snapshot_sha256", sa.String(), nullable=False),
        sa.Column("dispatch_text", sa.Text(), nullable=False),
        sa.Column("dispatch_sha256", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("accepted_turn_id", sa.String(), nullable=True),
        sa.Column("current_attempt_id", sa.String(), nullable=True),
        sa.Column("current_attempt_kind", sa.String(), nullable=True),
        sa.Column("current_target_turn_id", sa.String(), nullable=True),
        sa.Column("current_expected_native_turn_id", sa.Text(), nullable=True),
        sa.Column("current_receipt_outcome", sa.String(), nullable=True),
        sa.Column("current_receipt_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("current_attempt_opened_at", sa.String(), nullable=True),
        sa.Column(
            "delivery_history_json",
            sa.Text(),
            server_default='{"version":1,"events":[]}',
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("submitted_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("materialized_at", sa.String(), nullable=True),
        sa.Column("retired_at", sa.String(), nullable=True),
        sa.CheckConstraint("priority in ('p0','p1','p3')", name="ck_message_deliveries_priority"),
        sa.CheckConstraint(
            "state in ('reserved','queued','start_attempting','pending_steer','steering',"
            "'interrupt_waiting','reconciling_start','reconciling_steer','accepted','retired')",
            name="ck_message_deliveries_state",
        ),
        sa.CheckConstraint(
            "current_attempt_kind is null or current_attempt_kind in ('start','steer')",
            name="ck_message_deliveries_current_attempt_kind",
        ),
        sa.CheckConstraint("message_id is null or message_id = id", name="ck_message_deliveries_stable_message_id"),
        sa.CheckConstraint(
            "json_valid(delivery_history_json) = 1 "
            "and json_extract(delivery_history_json, '$.version') = 1 "
            "and json_type(delivery_history_json, '$.events') = 'array'",
            name="ck_message_deliveries_history_json",
        ),
        sa.CheckConstraint(
            "(state in ('start_attempting','reconciling_start') "
            "and current_attempt_id is not null and current_attempt_kind = 'start' "
            "and (current_target_turn_id is not null "
            "or (state = 'reconciling_start' and current_attempt_id like 'atm_migration_%'))) "
            "or (state in ('steering','reconciling_steer') "
            "and current_attempt_id is not null and current_attempt_kind = 'steer' "
            "and current_target_turn_id is not null and current_expected_native_turn_id is not null) "
            "or (state = 'pending_steer' and current_attempt_id is null "
            "and current_attempt_kind is null and current_target_turn_id is not null "
            "and current_expected_native_turn_id is null) "
            "or (state not in ('start_attempting','reconciling_start','steering',"
            "'reconciling_steer','pending_steer') and current_attempt_id is null "
            "and current_attempt_kind is null and current_target_turn_id is null "
            "and current_expected_native_turn_id is null)",
            name="ck_message_deliveries_current_attempt_shape",
        ),
        sa.CheckConstraint(
            "(state in ('reconciling_start','reconciling_steer') "
            "and current_receipt_outcome = 'unknown') "
            "or (state not in ('reconciling_start','reconciling_steer') "
            "and current_receipt_outcome is null)",
            name="ck_message_deliveries_current_receipt",
        ),
        sa.CheckConstraint(
            "(state = 'accepted' and message_id = id and accepted_turn_id is not null "
            "and materialized_at is not null and snapshot_json is null "
            "and current_attempt_id is null and current_attempt_kind is null "
            "and current_target_turn_id is null and current_expected_native_turn_id is null "
            "and current_receipt_outcome is null and current_attempt_opened_at is null) "
            "or (state <> 'accepted' and message_id is null and materialized_at is null)",
            name="ck_message_deliveries_materialization",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_turn_id"],
            ["session_turns.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["current_target_turn_id"],
            ["session_turns.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_message_deliveries_message"),
        sa.UniqueConstraint("dedupe_key", name="uq_message_deliveries_dedupe"),
        sa.UniqueConstraint("current_attempt_id", name="uq_message_deliveries_current_attempt"),
    )
    op.create_table(
        "session_turns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("initial_delivery_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("runtime_key", sa.Text(), nullable=True),
        sa.Column("runtime_turn_id", sa.Text(), nullable=True),
        sa.Column("native_turn_id", sa.Text(), nullable=True),
        sa.Column("terminal_outcome", sa.String(), nullable=True),
        sa.Column("settled_by", sa.String(), nullable=True),
        sa.Column("terminal_evidence_kind", sa.String(), nullable=True),
        sa.Column("terminal_evidence_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("control_state", sa.String(), nullable=True),
        sa.Column("control_mode", sa.String(), nullable=True),
        sa.Column("control_attempt_id", sa.String(), nullable=True),
        sa.Column("control_expected_native_turn_id", sa.Text(), nullable=True),
        sa.Column("control_receipt_outcome", sa.String(), nullable=True),
        sa.Column("control_receipt_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("control_successor_delivery_id", sa.String(), nullable=True),
        sa.Column("control_successor_turn_id", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("terminal_at", sa.String(), nullable=True),
        sa.CheckConstraint("state in ('waiting','starting','active','terminal')", name="ck_session_turns_state"),
        sa.CheckConstraint(
            "terminal_outcome is null or terminal_outcome in ('completed','failed','canceled','not_written')",
            name="ck_session_turns_terminal_outcome",
        ),
        sa.CheckConstraint(
            "control_state is null or control_state in "
            "('pending','interrupting','waiting_terminal','reconciling','refused','settled')",
            name="ck_session_turns_control_state",
        ),
        sa.CheckConstraint(
            "control_mode is null or control_mode in ('stop_only','replace')",
            name="ck_session_turns_control_mode",
        ),
        sa.CheckConstraint(
            "(state = 'terminal' and terminal_outcome is not null and terminal_at is not null) "
            "or (state <> 'terminal' and terminal_outcome is null and terminal_at is null)",
            name="ck_session_turns_terminal_shape",
        ),
        sa.CheckConstraint(
            "(control_mode = 'replace' and control_successor_delivery_id is not null "
            "and control_successor_turn_id is not null) "
            "or (control_mode = 'stop_only' and control_successor_delivery_id is null "
            "and control_successor_turn_id is null) "
            "or (control_mode is null and control_successor_delivery_id is null "
            "and control_successor_turn_id is null)",
            name="ck_session_turns_control_shape",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["initial_delivery_id"],
            ["message_deliveries.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["control_successor_delivery_id"],
            ["message_deliveries.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["control_successor_turn_id"],
            ["session_turns.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # SQLite permits the forward references above; add the Show Delivery FK only
    # after both operational tables exist.
    with op.batch_alter_table("show_session_events") as batch:
        batch.create_foreign_key(
            "fk_show_session_events_delivery",
            "message_deliveries",
            ["delivery_id"],
            ["id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        )

    op.create_index(
        "ix_message_deliveries_session_order",
        "message_deliveries",
        ["session_id", "submitted_at", "id"],
        sqlite_where=sa.text(
            "state in ('queued','pending_steer','steering','reconciling_start','reconciling_steer')"
        ),
    )
    op.create_index(
        "ix_message_deliveries_session_state",
        "message_deliveries",
        ["session_id", "state", "submitted_at", "id"],
    )
    op.create_index("ix_message_deliveries_accepted_turn", "message_deliveries", ["accepted_turn_id"])
    op.create_index(
        "ix_message_deliveries_current_target_turn",
        "message_deliveries",
        ["current_target_turn_id"],
    )
    op.create_index("ix_session_turns_session_created", "session_turns", ["session_id", "created_at", "id"])
    op.create_index(
        "uq_session_turns_live_session",
        "session_turns",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("state in ('starting','active')"),
    )
    op.create_index(
        "uq_session_turns_message_written_attempt",
        "session_turns",
        ["initial_delivery_id"],
        unique=True,
        sqlite_where=sa.text("terminal_outcome is null or terminal_outcome <> 'not_written'"),
    )
    op.create_index(
        "uq_session_turns_waiting_successor",
        "session_turns",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("state = 'waiting'"),
    )
    op.create_index(
        "uq_session_turns_control_attempt",
        "session_turns",
        ["control_attempt_id"],
        unique=True,
        sqlite_where=sa.text("control_attempt_id is not null"),
    )
    _migrate_pseudo_messages(bind)
    # SQLite rebuilds ``messages`` to add the CHECK.  Its SET NULL dependents
    # observe the transient DROP even though their accepted Message survives,
    # so retain and restore those audit links around the batch operation.
    bind.execute(
        sa.text(
            "create temporary table _0043_show_message_refs as "
            "select id, message_id from show_session_events where message_id is not null"
        )
    )
    bind.execute(
        sa.text(
            "create temporary table _0043_media_message_refs as "
            "select token, message_id from media_objects where message_id is not null"
        )
    )
    with op.batch_alter_table("messages") as batch:
        batch.create_check_constraint(
            "ck_messages_communication_type",
            "type not in ('queued','pending','draft','harness_dedupe','silent','tool_call')",
        )
    bind.execute(
        sa.text(
            "update show_session_events set message_id = ("
            "select message_id from _0043_show_message_refs refs "
            "where refs.id = show_session_events.id) "
            "where id in (select id from _0043_show_message_refs)"
        )
    )
    bind.execute(
        sa.text(
            "update media_objects set message_id = ("
            "select message_id from _0043_media_message_refs refs "
            "where refs.token = media_objects.token) "
            "where token in (select token from _0043_media_message_refs)"
        )
    )
    bind.execute(sa.text("drop table _0043_show_message_refs"))
    bind.execute(sa.text("drop table _0043_media_message_refs"))
    for index_name, create_sql in (
        ("ix_messages_inbox_activity", _INBOX_ACTIVITY_SQL),
        ("ix_messages_inbox_agent_reply", _INBOX_AGENT_REPLY_SQL),
        ("ix_messages_inbox_user_send", _INBOX_USER_SEND_SQL),
    ):
        op.drop_index(index_name, table_name="messages")
        op.execute(create_sql)


def _restore_legacy_messages(bind) -> None:
    rows = bind.execute(
        sa.text(
            "select * from message_deliveries where message_id is null "
            "and json_valid(delivery_history_json) = 1 "
            "and json_array_length(json_extract(delivery_history_json, '$.events')) = 1 "
            "and json_extract(delivery_history_json, '$.events[0].kind') = 'migration'"
        )
    ).mappings()
    for row in rows:
        snapshot = json.loads(row["snapshot_json"] or "{}")
        legacy_type = bind.execute(
            sa.text(
                "select json_extract(:history, '$.events[0].legacy_type')"
            ),
            {"history": row["delivery_history_json"]},
        ).scalar_one()
        if legacy_type not in {"queued", "pending", "harness_dedupe"}:
            raise RuntimeError("0043 downgrade cannot represent this Delivery safely")
        bind.execute(
            sa.text(
                "insert into messages ("
                "id, scope_id, session_id, platform, author, type, author_id, author_name, source, "
                "native_message_id, parent_native_message_id, content_text, content_json, metadata_json, "
                "created_at, updated_at, delivered_at, read_at"
                ") values ("
                ":id, :scope_id, :session_id, :platform, :author, :type, :author_id, :author_name, "
                ":source, :native_message_id, :parent_native_message_id, :content_text, :content_json, "
                ":metadata_json, :created_at, :updated_at, null, :read_at)"
            ),
            {
                "id": row["id"],
                **snapshot,
                "type": legacy_type,
                "created_at": row["submitted_at"],
                "updated_at": row["updated_at"],
            },
        )
        bind.execute(
            sa.text(
                "update show_session_events set message_id = :id, delivery_id = null "
                "where delivery_id = :id"
            ),
            {"id": row["id"]},
        )


def downgrade() -> None:
    bind = op.get_bind()
    unsafe = bind.execute(
        sa.text(
            "select count(*) from message_deliveries where not ("
            "message_id is null and json_valid(delivery_history_json) = 1 "
            "and json_array_length(json_extract(delivery_history_json, '$.events')) = 1 "
            "and json_extract(delivery_history_json, '$.events[0].kind') = 'migration' "
            "and json_extract(delivery_history_json, '$.events[0].legacy_type') <> 'pending')"
        )
    ).scalar_one()
    live_turns = bind.execute(
        sa.text("select count(*) from session_turns where state <> 'terminal'")
    ).scalar_one()
    held = bind.execute(
        sa.text("select count(*) from agent_sessions where queue_hold_state = 'held'")
    ).scalar_one()
    if unsafe or live_turns or held:
        raise RuntimeError(
            "0043 downgrade refused: live, accepted, or ambiguous Delivery state cannot be represented without replay risk"
        )
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("ck_messages_communication_type", type_="check")
    _restore_legacy_messages(bind)
    op.drop_index("ix_messages_inbox_activity", table_name="messages")
    op.create_index(
        "ix_messages_inbox_activity",
        "messages",
        ["platform", "session_id", sa.text("created_at desc"), sa.text("id desc")],
        sqlite_where=sa.text(_OLD_INBOX_ACTIVITY_PREDICATE),
    )
    with op.batch_alter_table("show_session_events") as batch:
        batch.drop_constraint("fk_show_session_events_delivery", type_="foreignkey")
        batch.drop_column("delivery_id")
    op.drop_index("uq_session_turns_control_attempt", table_name="session_turns")
    op.drop_index("uq_session_turns_waiting_successor", table_name="session_turns")
    op.drop_index("uq_session_turns_message_written_attempt", table_name="session_turns")
    op.drop_index("uq_session_turns_live_session", table_name="session_turns")
    op.drop_index("ix_session_turns_session_created", table_name="session_turns")
    op.drop_table("session_turns")
    op.drop_index("ix_message_deliveries_current_target_turn", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_accepted_turn", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_session_state", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_session_order", table_name="message_deliveries")
    op.drop_table("message_deliveries")
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_constraint("ck_agent_sessions_queue_hold_state", type_="check")
        batch.drop_column("composer_draft_updated_at")
        batch.drop_column("composer_draft_text")
        batch.drop_column("queue_held_at")
        batch.drop_column("queue_hold_version")
        batch.drop_column("queue_hold_state")

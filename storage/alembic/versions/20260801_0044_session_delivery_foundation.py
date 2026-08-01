"""separate submitted deliveries from accepted Session messages

Revision ID: 20260801_0044
Revises: 20260731_0043
Create Date: 2026-07-31
"""

from __future__ import annotations

import hashlib
import json
from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "20260801_0044"
down_revision = "20260731_0043"
branch_labels = None
depends_on = None

_AGENT_RUN_INDEX_MODULES = (
    "storage.alembic.versions.20260728_0039_agent_runs_settled_at_index",
    "storage.alembic.versions.20260728_0041_agent_runs_owed_notice_backoff_index",
    "storage.alembic.versions.20260729_0042_agent_runs_definition_streak_index",
)

_OLD_INBOX_ACTIVITY_PREDICATE = (
    "session_id is not null and type not in "
    "('queued', 'draft', 'pending', 'harness_dedupe', 'silent')"
)
_INBOX_ACTIVITY_SQL = (
    "create index ix_messages_inbox_activity "
    "on messages (platform, session_id, created_at desc, id desc) "
    "where session_id is not null and type in "
    "('user', 'harness', 'annotation', 'result', 'notify', 'error', 'assistant')"
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


def _restore_agent_run_expression_indexes(bind) -> None:
    # SQLite batch-alter rebuilds the table and cannot reflect expression indexes.
    for module_name in _AGENT_RUN_INDEX_MODULES:
        revision_module = import_module(module_name)
        bind.exec_driver_sql(revision_module.DROP_INDEX_SQL)
        bind.exec_driver_sql(revision_module.CREATE_INDEX_SQL)


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


def _pending_target_type(row: sa.RowMapping) -> str:
    if (
        row["author"] == "harness"
        and row["source"] == "harness"
        and row["author_name"] == "show_annotation"
    ):
        return "annotation"
    if "harness" in {row["author"], row["source"]}:
        return "harness"
    return "user"


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
    event_type = "tool_call" if row["type"] == "tool_call" else "silent_terminal"
    metadata = _metadata(row["metadata_json"])
    metadata.update(
        {
            "legacy_message_id": row["id"],
            "legacy_message_type": row["type"],
            "legacy_message_snapshot": {
                **_message_snapshot(row),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "delivered_at": row["delivered_at"],
            },
            "migration_revision": revision,
        }
    )
    existing = bind.execute(
        sa.text(
            "select id, metadata_json from agent_events "
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
        previous_metadata_json = str(existing[1] or "{}")
        existing_metadata = _metadata(existing[1])
        existing_metadata.update(
            {
                **metadata,
                "migration_event_created": False,
                "migration_previous_metadata_json": previous_metadata_json,
            }
        )
        bind.execute(
            sa.text(
                "update agent_events set metadata_json = :metadata_json "
                "where id = :id"
            ),
            {"id": existing[0], "metadata_json": _json(existing_metadata)},
        )
        return
    metadata["migration_event_created"] = True
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
    seen_queued_sessions: set[str] = set()
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
        session_status = bind.execute(
            sa.text("select status from agent_sessions where id = :session_id"),
            {"session_id": session_id},
        ).scalar_one_or_none()
        if session_status is None:
            _migrate_legacy_trace(bind, {**dict(row), "type": "tool_call"})
            continue
        pending_target = _pending_target_type(row) if kind == "pending" else None
        snapshot = _message_snapshot(row)
        if pending_target is not None:
            snapshot["type"] = pending_target
            snapshot["author"] = "user" if pending_target == "user" else "harness"
        snapshot_json = _json(snapshot)
        metadata = _metadata(row["metadata_json"])
        provenance = metadata.get("scheduled_provenance")
        provenance_spec = (
            provenance.get("platform_specific")
            if isinstance(provenance, dict)
            else None
        )
        owned_agent_run = kind == "queued" and (
            str(row["native_message_id"] or "").startswith("agent_run:")
            or (
                isinstance(provenance_spec, dict)
                and provenance_spec.get("task_trigger_kind") == "agent_run"
            )
        )
        dispatch_text = str(
            metadata.get("_queued_dispatch_text")
            or row["content_text"]
            or _metadata(row["content_json"]).get("text")
            or ""
        )
        state = {
            "queued": "queued",
            "pending": "retired",
            "harness_dedupe": "retired",
        }[kind]
        if session_status != "active":
            state = "retired"
        if (
            kind == "queued"
            and session_status == "active"
            and session_id not in seen_queued_sessions
        ):
            seen_queued_sessions.add(session_id)
            if not owned_agent_run:
                # Legacy startup resumed only an Agent-Run-owned queue head.
                # Keep an ordinary head held so upgrade cannot dispatch it.
                bind.execute(
                    sa.text(
                        "update agent_sessions set queue_hold_state = 'held', "
                        "queue_hold_version = queue_hold_version + 1, "
                        "queue_held_at = :held_at where id = :session_id"
                    ),
                    {
                        "held_at": row["updated_at"] or row["created_at"],
                        "session_id": session_id,
                    },
                )
        history = {
            "version": 1,
            "events": [
                {
                    "at": row["updated_at"] or row["created_at"],
                    "kind": "migration",
                    "revision": revision,
                    "legacy_type": kind,
                    "outcome": "unknown_not_replayed" if kind == "pending" else "moved",
                }
            ],
        }
        delivery_values = {
            "id": row["id"],
            "session_id": session_id,
            "state": state,
            "snapshot_json": snapshot_json,
            "snapshot_sha256": _hash(snapshot_json),
            "dispatch_text": dispatch_text,
            "dispatch_sha256": _hash(dispatch_text),
            "dedupe_key": (
                (
                    f"{row['platform']}:{row['native_message_id']}"
                    if kind == "harness_dedupe" or owned_agent_run
                    else f"legacy:{row['platform']}:{row['native_message_id']}"
                )
                if row["native_message_id"]
                else None
            ),
            "attempt_id": None,
            "attempt_kind": None,
            "receipt_outcome": None,
            "attempt_opened_at": None,
            "history_json": _json(history),
            "submitted_at": row["created_at"],
            "updated_at": row["updated_at"],
            "retired_at": row["updated_at"] if state == "retired" else None,
        }
        bind.execute(
            sa.text(
                "insert or ignore into message_deliveries ("
                "id, session_id, message_id, priority, state, snapshot_json, snapshot_sha256, "
                "dispatch_text, dispatch_sha256, dedupe_key, turn_id, turn_role, turn_position, "
                "current_attempt_id, current_attempt_kind, current_target_turn_id, "
                "current_expected_native_turn_id, current_receipt_outcome, current_receipt_json, "
                "current_attempt_opened_at, delivery_history_json, version, submitted_at, "
                "updated_at, materialized_at, retired_at"
                ") values ("
                ":id, :session_id, null, 'p3', :state, :snapshot_json, :snapshot_sha256, "
                ":dispatch_text, :dispatch_sha256, :dedupe_key, null, null, null, "
                ":attempt_id, :attempt_kind, null, null, :receipt_outcome, '{}', :attempt_opened_at, "
                ":history_json, 1, :submitted_at, :updated_at, null, :retired_at)"
            ),
            delivery_values,
        )
        persisted = bind.execute(
            sa.text(
                "select session_id, snapshot_sha256, submitted_at from message_deliveries "
                "where id = :id"
            ),
            {"id": row["id"]},
        ).one()
        if tuple(persisted) != (
            session_id,
            delivery_values["snapshot_sha256"],
            delivery_values["submitted_at"],
        ):
            raise RuntimeError(
                f"0044 migration Delivery {row['id']} conflicts with its Message snapshot"
            )
        bind.execute(
            sa.text(
                "update show_session_events set delivery_id = :id, message_id = null "
                "where message_id = :id"
            ),
            {"id": row["id"]},
        )
        if owned_agent_run:
            run_id = str(
                (provenance_spec or {}).get("task_execution_id")
                or str(row["native_message_id"] or "").removeprefix("agent_run:")
            ).strip()
            if run_id:
                bind.execute(
                    sa.text(
                        "update agent_runs set delivery_id = :delivery_id "
                        "where id = :run_id and delivery_id is null"
                    ),
                    {"delivery_id": row["id"], "run_id": run_id},
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


def _precreated_schema_gaps(bind) -> list[str]:
    inspector = sa.inspect(bind)
    gaps: list[str] = []
    required_columns = {
        "message_deliveries": {
            "id", "session_id", "message_id", "priority", "state",
            "snapshot_json", "snapshot_sha256", "dispatch_text",
            "dispatch_sha256", "dedupe_key", "turn_id", "turn_role",
            "turn_position", "current_attempt_id", "current_attempt_kind",
            "current_target_turn_id", "current_expected_native_turn_id",
            "current_receipt_outcome", "current_receipt_json",
            "current_attempt_opened_at", "delivery_history_json", "version",
            "submitted_at", "updated_at", "materialized_at", "retired_at",
        },
        "session_turns": {
            "id", "session_id", "initial_delivery_id", "state", "backend",
            "runtime_key", "runtime_turn_id", "native_turn_id",
            "start_attempt_id", "start_receipt_outcome", "start_receipt_json",
            "dispatch_text", "dispatch_sha256", "terminal_outcome", "settled_by",
            "terminal_evidence_kind", "terminal_evidence_json", "control_state",
            "control_mode", "control_attempt_id", "control_expected_native_turn_id",
            "control_receipt_outcome", "control_receipt_json",
            "control_successor_delivery_id", "control_successor_turn_id", "version",
            "created_at", "updated_at", "started_at", "terminal_at",
        },
    }
    for table_name, expected in required_columns.items():
        present = {column["name"] for column in inspector.get_columns(table_name)}
        for name in sorted(expected - present):
            gaps.append(f"{table_name}.{name}")

    required_checks = {
        "message_deliveries": {
            "ck_message_deliveries_priority",
            "ck_message_deliveries_state",
            "ck_message_deliveries_current_attempt_kind",
            "ck_message_deliveries_current_attempt_shape",
            "ck_message_deliveries_current_receipt",
            "ck_message_deliveries_history_json",
            "ck_message_deliveries_materialization",
            "ck_message_deliveries_turn_membership",
        },
        "session_turns": {
            "ck_session_turns_state",
            "ck_session_turns_terminal_outcome",
            "ck_session_turns_start_receipt_outcome",
            "ck_session_turns_start_shape",
            "ck_session_turns_control_state",
            "ck_session_turns_control_mode",
            "ck_session_turns_terminal_shape",
            "ck_session_turns_control_shape",
        },
    }
    for table_name, expected in required_checks.items():
        present = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        }
        for name in sorted(expected - present):
            gaps.append(f"constraint {name}")

    required_foreign_keys = {
        "message_deliveries": {
            (("session_id",), "agent_sessions"),
            (("message_id",), "messages"),
            (("turn_id",), "session_turns"),
            (("current_target_turn_id",), "session_turns"),
        },
        "session_turns": {
            (("session_id",), "agent_sessions"),
            (("initial_delivery_id",), "message_deliveries"),
            (("control_successor_delivery_id",), "message_deliveries"),
            (("control_successor_turn_id",), "session_turns"),
        },
    }
    for table_name, expected in required_foreign_keys.items():
        present = {
            (tuple(foreign_key["constrained_columns"]), foreign_key["referred_table"])
            for foreign_key in inspector.get_foreign_keys(table_name)
            if foreign_key.get("options", {}).get("deferrable") is True
            and str(foreign_key.get("options", {}).get("initially") or "").upper()
            == "DEFERRED"
        }
        for columns, target in sorted(expected - present):
            gaps.append(f"deferred FK {table_name}.{','.join(columns)}->{target}")

    unique_names = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("message_deliveries")
    }
    if "uq_message_deliveries_dedupe" not in unique_names:
        gaps.append("constraint uq_message_deliveries_dedupe")
    return gaps


def _column_names(bind, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _index_names(bind, table_name: str) -> set[str]:
    return {
        str(name)
        for name in bind.execute(
            sa.text(
                "select name from sqlite_master "
                "where type = 'index' and tbl_name = :table_name"
            ),
            {"table_name": table_name},
        ).scalars()
    }


def _has_deferred_fk(bind, table_name: str, column: str, target: str) -> bool:
    return any(
        foreign_key["constrained_columns"] == [column]
        and foreign_key["referred_table"] == target
        and foreign_key.get("options", {}).get("deferrable") is True
        and str(foreign_key.get("options", {}).get("initially") or "").upper()
        == "DEFERRED"
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
    )


def _add_precreated_columns(bind) -> None:
    session_columns = _column_names(bind, "agent_sessions")
    if "queue_hold_state" not in session_columns:
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
    if "queue_hold_version" not in session_columns:
        op.add_column(
            "agent_sessions",
            sa.Column("queue_hold_version", sa.Integer(), server_default="1", nullable=False),
        )
    for name, column_type in (
        ("queue_held_at", sa.String()),
        ("composer_draft_text", sa.Text()),
        ("composer_draft_updated_at", sa.String()),
    ):
        if name not in session_columns:
            op.add_column("agent_sessions", sa.Column(name, column_type, nullable=True))

    if "delivery_id" not in _column_names(bind, "show_session_events"):
        with op.batch_alter_table("show_session_events") as batch:
            batch.add_column(sa.Column("delivery_id", sa.String(), nullable=True))
    if "delivery_id" not in _column_names(bind, "agent_runs"):
        with op.batch_alter_table("agent_runs") as batch:
            batch.add_column(sa.Column("delivery_id", sa.String(), nullable=True))


def _finish_upgrade(bind) -> None:
    _add_precreated_columns(bind)
    session_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("agent_sessions")
    }
    if "ck_agent_sessions_queue_hold_state" not in session_checks:
        raise RuntimeError(
            "pre-existing agent_sessions queue hold columns lack their 0044 constraint"
        )
    if not _has_deferred_fk(
        bind, "show_session_events", "delivery_id", "message_deliveries"
    ):
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
    if not _has_deferred_fk(bind, "agent_runs", "delivery_id", "message_deliveries"):
        with op.batch_alter_table("agent_runs") as batch:
            batch.create_foreign_key(
                "fk_agent_runs_delivery",
                "message_deliveries",
                ["delivery_id"],
                ["id"],
                ondelete="NO ACTION",
                deferrable=True,
                initially="DEFERRED",
            )
    _restore_agent_run_expression_indexes(bind)

    delivery_indexes = _index_names(bind, "message_deliveries")
    for name, columns, unique, where in (
        (
            "ix_message_deliveries_session_order",
            ["session_id", "submitted_at", "id"],
            False,
            "state in ('reserved','queued','claimed','pending_steer','steering','reconciling_steer')",
        ),
        ("ix_message_deliveries_session_state", ["session_id", "state", "submitted_at", "id"], False, None),
        ("ix_message_deliveries_turn", ["turn_id", "turn_position"], False, None),
        ("uq_message_deliveries_turn_position", ["turn_id", "turn_position"], True, "turn_id is not null"),
        ("ix_message_deliveries_current_attempt", ["current_attempt_id"], False, None),
        ("ix_message_deliveries_current_target_turn", ["current_target_turn_id"], False, None),
    ):
        if name not in delivery_indexes:
            op.create_index(
                name,
                "message_deliveries",
                columns,
                unique=unique,
                **({"sqlite_where": sa.text(where)} if where else {}),
            )

    turn_indexes = _index_names(bind, "session_turns")
    for name, columns, unique, where in (
        ("ix_session_turns_session_created", ["session_id", "created_at", "id"], False, None),
        ("uq_session_turns_live_session", ["session_id"], True, "state in ('starting','active')"),
        (
            "uq_session_turns_message_written_attempt",
            ["initial_delivery_id"],
            True,
            "state <> 'terminal' or start_receipt_outcome = 'accepted'",
        ),
        ("uq_session_turns_waiting_successor", ["session_id"], True, "state = 'waiting'"),
        ("uq_session_turns_control_attempt", ["control_attempt_id"], True, "control_attempt_id is not null"),
        ("uq_session_turns_start_attempt", ["start_attempt_id"], True, "start_attempt_id is not null"),
    ):
        if name not in turn_indexes:
            op.create_index(
                name,
                "session_turns",
                columns,
                unique=unique,
                **({"sqlite_where": sa.text(where)} if where else {}),
            )
    if "uq_agent_runs_delivery" not in _index_names(bind, "agent_runs"):
        op.create_index(
            "uq_agent_runs_delivery",
            "agent_runs",
            ["delivery_id"],
            unique=True,
            sqlite_where=sa.text("delivery_id is not null"),
        )

    _migrate_pseudo_messages(bind)
    message_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("messages")
    }
    messages_complete = (
        "ck_messages_communication_type" in message_checks
        and _has_deferred_fk(bind, "messages", "session_id", "agent_sessions")
    )
    if not messages_complete:
        _snapshot_message_references(bind)
        with op.batch_alter_table(
            "messages",
            naming_convention={
                "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
            },
        ) as batch:
            batch.drop_constraint(
                "fk_messages_session_id_agent_sessions",
                type_="foreignkey",
            )
            batch.create_foreign_key(
                "fk_messages_session_id_agent_sessions",
                "agent_sessions",
                ["session_id"],
                ["id"],
                ondelete="NO ACTION",
                deferrable=True,
                initially="DEFERRED",
            )
            batch.create_check_constraint(
                "ck_messages_communication_type",
                "type not in ('queued','pending','draft','harness_dedupe','silent','tool_call')",
            )
        _restore_message_references(bind)
    for index_name, create_sql in (
        ("ix_messages_inbox_activity", _INBOX_ACTIVITY_SQL),
        ("ix_messages_inbox_agent_reply", _INBOX_AGENT_REPLY_SQL),
        ("ix_messages_inbox_user_send", _INBOX_USER_SEND_SQL),
    ):
        op.execute(f"drop index if exists {index_name}")
        op.execute(create_sql)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    operational_tables = {"message_deliveries", "session_turns"}
    if operational_tables.issubset(tables):
        gaps = _precreated_schema_gaps(bind)
        if gaps:
            raise RuntimeError(
                "pre-existing durable Message delivery tables indicate an interrupted "
                f"0044 migration; incomplete: {', '.join(gaps)}"
            )
        _finish_upgrade(bind)
        return
    if operational_tables & tables:
        raise RuntimeError(
            "pre-existing durable Message delivery tables indicate an interrupted "
            "0044 migration; incomplete: one operational table is missing"
        )

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
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("delivery_id", sa.String(), nullable=True))
    _restore_agent_run_expression_indexes(op.get_bind())

    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("snapshot_sha256", sa.String(), nullable=False),
        sa.Column("dispatch_text", sa.Text(), nullable=True),
        sa.Column("dispatch_sha256", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("turn_role", sa.String(), nullable=True),
        sa.Column("turn_position", sa.Integer(), nullable=True),
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
            "state in ('reserved','queued','claimed','pending_steer','steering',"
            "'interrupt_waiting','reconciling_steer',"
            "'accepted','retired')",
            name="ck_message_deliveries_state",
        ),
        sa.CheckConstraint(
            "current_attempt_kind is null or current_attempt_kind = 'steer'",
            name="ck_message_deliveries_current_attempt_kind",
        ),
        sa.CheckConstraint(
            "json_valid(delivery_history_json) = 1 "
            "and json_extract(delivery_history_json, '$.version') = 1 "
            "and json_type(delivery_history_json, '$.events') = 'array'",
            name="ck_message_deliveries_history_json",
        ),
        sa.CheckConstraint(
            "(state in ('steering','reconciling_steer') "
            "and current_attempt_id is not null and current_attempt_kind = 'steer' "
            "and current_target_turn_id is not null and current_expected_native_turn_id is not null) "
            "or (state = 'pending_steer' and current_attempt_id is not null "
            "and current_attempt_kind = 'steer' and current_target_turn_id is not null "
            "and current_expected_native_turn_id is null) "
            "or (state not in ('steering','reconciling_steer','pending_steer') "
            "and current_attempt_id is null "
            "and current_attempt_kind is null and current_target_turn_id is null "
            "and current_expected_native_turn_id is null)",
            name="ck_message_deliveries_current_attempt_shape",
        ),
        sa.CheckConstraint(
            "(state = 'reconciling_steer' "
            "and current_receipt_outcome = 'unknown') "
            "or (state <> 'reconciling_steer' "
            "and current_receipt_outcome is null)",
            name="ck_message_deliveries_current_receipt",
        ),
        sa.CheckConstraint(
            "(state = 'accepted' and message_id is not null and turn_id is not null "
            "and turn_role in ('initial','steer') and turn_position is not null "
            "and materialized_at is not null and snapshot_json is null and dispatch_text is null "
            "and current_attempt_id is null and current_attempt_kind is null "
            "and current_target_turn_id is null and current_expected_native_turn_id is null "
            "and current_receipt_outcome is null and current_attempt_opened_at is null) "
            "or (state <> 'accepted' and message_id is null and materialized_at is null)",
            name="ck_message_deliveries_materialization",
        ),
        sa.CheckConstraint(
            "(state in ('claimed','interrupt_waiting') and turn_id is not null "
            "and turn_role = 'initial' and turn_position is not null) "
            "or (state = 'accepted' and turn_id is not null and turn_role is not null "
            "and turn_position is not null) "
            "or (state not in ('claimed','interrupt_waiting','accepted') "
            "and turn_id is null and turn_role is null and turn_position is null)",
            name="ck_message_deliveries_turn_membership",
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
            ["turn_id"],
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
        sa.UniqueConstraint("dedupe_key", name="uq_message_deliveries_dedupe"),
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
        sa.Column("start_attempt_id", sa.String(), nullable=True),
        sa.Column("start_receipt_outcome", sa.String(), nullable=True),
        sa.Column("start_receipt_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("dispatch_text", sa.Text(), nullable=True),
        sa.Column("dispatch_sha256", sa.String(), nullable=True),
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
            "start_receipt_outcome is null or start_receipt_outcome in "
            "('accepted','not_written','unknown')",
            name="ck_session_turns_start_receipt_outcome",
        ),
        sa.CheckConstraint(
            "(state = 'waiting' and start_attempt_id is null and dispatch_text is null "
            "and dispatch_sha256 is null and start_receipt_outcome is null) "
            "or (state = 'starting' and start_attempt_id is not null "
            "and dispatch_text is not null and dispatch_sha256 is not null "
            "and (start_receipt_outcome is null or start_receipt_outcome = 'unknown')) "
            "or (state = 'active' and start_attempt_id is not null "
            "and dispatch_text is not null and dispatch_sha256 is not null "
            "and start_receipt_outcome = 'accepted') "
            "or (state = 'terminal' and (((terminal_outcome <> 'not_written' "
            "and start_attempt_id is not null and dispatch_text is not null "
            "and dispatch_sha256 is not null and start_receipt_outcome = 'accepted') "
            "or (terminal_outcome = 'failed' and start_attempt_id is not null "
            "and dispatch_text is not null and dispatch_sha256 is not null "
            "and start_receipt_outcome = 'unknown')) "
            "or (terminal_outcome = 'not_written' and start_attempt_id is not null "
            "and dispatch_text is not null and dispatch_sha256 is not null "
            "and start_receipt_outcome = 'not_written') "
            "or (terminal_outcome = 'not_written' and start_attempt_id is null "
            "and dispatch_text is null and dispatch_sha256 is null)))",
            name="ck_session_turns_start_shape",
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
    _finish_upgrade(bind)


def _snapshot_message_references(bind) -> None:
    bind.execute(
        sa.text(
            "create temporary table _0044_show_message_refs as "
            "select id, message_id from show_session_events where message_id is not null"
        )
    )
    bind.execute(
        sa.text(
            "create temporary table _0044_media_message_refs as "
            "select token, message_id from media_objects where message_id is not null"
        )
    )


def _restore_message_references(bind) -> None:
    bind.execute(
        sa.text(
            "update show_session_events set message_id = ("
            "select message_id from _0044_show_message_refs refs "
            "where refs.id = show_session_events.id) "
            "where id in (select id from _0044_show_message_refs)"
        )
    )
    bind.execute(
        sa.text(
            "update media_objects set message_id = ("
            "select message_id from _0044_media_message_refs refs "
            "where refs.token = media_objects.token) "
            "where token in (select token from _0044_media_message_refs)"
        )
    )
    bind.execute(sa.text("drop table _0044_show_message_refs"))
    bind.execute(sa.text("drop table _0044_media_message_refs"))


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
            raise RuntimeError("0044 downgrade cannot represent this Delivery safely")
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
        bind.execute(
            sa.text(
                "insert into _0044_message_session_refs (message_id, session_id) "
                "values (:message_id, :session_id)"
            ),
            {"message_id": row["id"], "session_id": row["session_id"]},
        )


def _restore_legacy_drafts(bind) -> None:
    rows = bind.execute(
        sa.text(
            "select id, scope_id, composer_draft_text, composer_draft_updated_at, "
            "updated_at from agent_sessions where composer_draft_text is not null"
        )
    ).mappings()
    for row in rows:
        session_id = str(row["id"])
        base_id = (
            "msg_legacy_draft_"
            + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        )
        message_id = base_id
        suffix = 0
        while bind.execute(
            sa.text("select 1 from messages where id = :id"),
            {"id": message_id},
        ).first():
            suffix += 1
            message_id = f"{base_id}_{suffix}"
        text = str(row["composer_draft_text"])
        timestamp = row["composer_draft_updated_at"] or row["updated_at"]
        bind.execute(
            sa.text(
                "insert into messages ("
                "id, scope_id, session_id, platform, author, type, author_id, author_name, "
                "source, native_message_id, parent_native_message_id, content_text, "
                "content_json, metadata_json, created_at, updated_at, delivered_at, read_at"
                ") values ("
                ":id, :scope_id, :session_id, 'avibe', 'user', 'draft', null, null, "
                "'user', null, null, :content_text, :content_json, '{}', :created_at, "
                ":updated_at, null, null)"
            ),
            {
                "id": message_id,
                "scope_id": row["scope_id"],
                "session_id": session_id,
                "content_text": text,
                "content_json": _json({"text": text}),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        bind.execute(
            sa.text(
                "insert into _0044_message_session_refs (message_id, session_id) "
                "values (:message_id, :session_id)"
            ),
            {"message_id": message_id, "session_id": session_id},
        )


def _restore_legacy_trace_messages(bind) -> None:
    rows = bind.execute(
        sa.text(
            "select id, metadata_json from agent_events "
            "where event_type in ('tool_call', 'silent_terminal') "
            "and json_valid(metadata_json) = 1 "
            "and json_extract(metadata_json, '$.migration_revision') = :revision"
        ),
        {"revision": revision},
    ).mappings()
    for row in rows:
        metadata = _metadata(row["metadata_json"])
        legacy_type = metadata.get("legacy_message_type")
        snapshot = metadata.get("legacy_message_snapshot")
        if legacy_type not in {"tool_call", "silent"} or not isinstance(
            snapshot, dict
        ):
            raise RuntimeError(
                "0044 downgrade cannot restore migrated trace Message safely"
            )
        message_id = str(metadata.get("legacy_message_id") or "")
        if not message_id or bind.execute(
            sa.text("select 1 from messages where id = :id"),
            {"id": message_id},
        ).first():
            raise RuntimeError(
                "0044 downgrade cannot restore migrated trace Message identity"
            )
        bind.execute(
            sa.text(
                "insert into messages ("
                "id, scope_id, session_id, platform, author, type, author_id, "
                "author_name, source, native_message_id, parent_native_message_id, "
                "content_text, content_json, metadata_json, created_at, updated_at, "
                "delivered_at, read_at"
                ") values ("
                ":id, :scope_id, :session_id, :platform, :author, :type, :author_id, "
                ":author_name, :source, :native_message_id, :parent_native_message_id, "
                ":content_text, :content_json, :metadata_json, :created_at, :updated_at, "
                ":delivered_at, :read_at)"
            ),
            {"id": message_id, **snapshot, "type": legacy_type},
        )
        session_id = str(snapshot.get("session_id") or "")
        if session_id:
            bind.execute(
                sa.text(
                    "insert into _0044_message_session_refs (message_id, session_id) "
                    "values (:message_id, :session_id)"
                ),
                {"message_id": message_id, "session_id": session_id},
            )
        if metadata.get("migration_event_created") is True:
            bind.execute(
                sa.text(
                    "delete from agent_events where id = :id "
                    "and json_valid(metadata_json) = 1 "
                    "and json_extract(metadata_json, '$.migration_revision') = :revision "
                    "and json_extract(metadata_json, '$.legacy_message_id') = :message_id"
                ),
                {
                    "id": row["id"],
                    "revision": revision,
                    "message_id": message_id,
                },
            )
        else:
            previous_metadata_json = metadata.get(
                "migration_previous_metadata_json"
            )
            if not isinstance(previous_metadata_json, str):
                raise RuntimeError(
                    "0044 downgrade cannot restore pre-existing trace event metadata"
                )
            bind.execute(
                sa.text("update agent_events set metadata_json = :metadata_json where id = :id"),
                {"id": row["id"], "metadata_json": previous_metadata_json},
            )


def downgrade() -> None:
    bind = op.get_bind()
    unsafe = bind.execute(
        sa.text(
            "select count(*) from message_deliveries where not ("
            "message_id is null and json_valid(delivery_history_json) = 1 "
            "and json_array_length(json_extract(delivery_history_json, '$.events')) = 1 "
            "and json_extract(delivery_history_json, '$.events[0].kind') = 'migration' and ("
            "(json_extract(delivery_history_json, '$.events[0].legacy_type') = 'queued' "
            "and state in ('queued','retired')) or "
            "(json_extract(delivery_history_json, '$.events[0].legacy_type') = 'pending' "
            "and state = 'retired') or "
            "(json_extract(delivery_history_json, '$.events[0].legacy_type') = 'harness_dedupe' "
            "and state = 'retired')))"
        )
    ).scalar_one()
    live_turns = bind.execute(
        sa.text("select count(*) from session_turns where state <> 'terminal'")
    ).scalar_one()
    held = bind.execute(
        sa.text(
            "select count(*) from agent_sessions sessions "
            "where sessions.queue_hold_state = 'held' and not exists ("
            "select 1 from message_deliveries deliveries "
            "where deliveries.session_id = sessions.id "
            "and deliveries.message_id is null "
            "and deliveries.state in ('queued','retired') "
            "and json_valid(deliveries.delivery_history_json) = 1 "
            "and json_array_length(json_extract(deliveries.delivery_history_json, '$.events')) = 1 "
            "and json_extract(deliveries.delivery_history_json, '$.events[0].kind') = 'migration' "
            "and json_extract(deliveries.delivery_history_json, '$.events[0].legacy_type') = 'queued'"
            ")"
        )
    ).scalar_one()
    if unsafe or live_turns or held:
        raise RuntimeError(
            "0044 downgrade refused: live, accepted, or ambiguous Delivery state cannot be represented without replay risk"
        )
    _snapshot_message_references(bind)
    with op.batch_alter_table(
        "messages",
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
        },
    ) as batch:
        batch.drop_constraint("ck_messages_communication_type", type_="check")
        batch.drop_constraint(
            "fk_messages_session_id_agent_sessions",
            type_="foreignkey",
        )
        batch.create_foreign_key(
            "fk_messages_session_id_agent_sessions",
            "agent_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )
    _restore_message_references(bind)
    bind.execute(
        sa.text(
            "create temporary table _0044_message_session_refs ("
            "message_id text primary key, session_id text not null)"
        )
    )
    _restore_legacy_messages(bind)
    _restore_legacy_drafts(bind)
    _restore_legacy_trace_messages(bind)
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
    op.drop_index("uq_agent_runs_delivery", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_constraint("fk_agent_runs_delivery", type_="foreignkey")
        batch.drop_column("delivery_id")
    _restore_agent_run_expression_indexes(op.get_bind())
    op.drop_index("uq_session_turns_start_attempt", table_name="session_turns")
    op.drop_index("uq_session_turns_control_attempt", table_name="session_turns")
    op.drop_index("uq_session_turns_waiting_successor", table_name="session_turns")
    op.drop_index("uq_session_turns_message_written_attempt", table_name="session_turns")
    op.drop_index("uq_session_turns_live_session", table_name="session_turns")
    op.drop_index("ix_session_turns_session_created", table_name="session_turns")
    op.drop_table("session_turns")
    op.drop_index("ix_message_deliveries_current_attempt", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_current_target_turn", table_name="message_deliveries")
    op.drop_index("uq_message_deliveries_turn_position", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_turn", table_name="message_deliveries")
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
    bind.execute(
        sa.text(
            "update messages set session_id = ("
            "select session_id from _0044_message_session_refs refs "
            "where refs.message_id = messages.id) "
            "where id in (select message_id from _0044_message_session_refs)"
        )
    )
    bind.execute(sa.text("drop table _0044_message_session_refs"))

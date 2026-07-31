from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from storage.agent_session_rows import reserve_write_lock
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, message_deliveries, messages, metadata, session_turns


REPO_ROOT = Path(__file__).resolve().parents[1]
PSEUDO_MESSAGE_TYPES = {
    "queued",
    "pending",
    "draft",
    "harness_dedupe",
    "silent",
    "tool_call",
}


def _seed_session(conn, session_id: str = "ses_authority") -> None:
    now = "2026-08-01T00:00:00+00:00"
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=None,
            agent_id=None,
            agent_name="codex",
            agent_backend="codex",
            agent_variant="codex",
            model=None,
            reasoning_effort=None,
            session_anchor=session_id,
            workdir="/tmp",
            native_session_id="",
            title=None,
            status="active",
            visibility="foreground",
            pinned=0,
            agent_status="idle",
            queue_hold_state="open",
            queue_hold_version=1,
            queue_held_at=None,
            composer_draft_text=None,
            composer_draft_updated_at=None,
            metadata_json="{}",
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
    )


def test_delivery_schema_has_one_queue_owner_and_no_turn_start_attempt() -> None:
    assert not (REPO_ROOT / "storage" / "session_deliveries.py").exists()
    assert message_deliveries.name == "message_deliveries"
    assert "start_attempt_id" not in session_turns.c
    assert {
        "snapshot_json",
        "snapshot_sha256",
        "dispatch_text",
        "dispatch_sha256",
        "current_attempt_id",
        "current_attempt_kind",
        "current_target_turn_id",
        "current_expected_native_turn_id",
        "current_receipt_outcome",
        "delivery_history_json",
        "version",
    } <= set(message_deliveries.c.keys())


def test_unaccepted_submission_has_no_message_and_materializes_once(tmp_path: Path) -> None:
    from storage import message_deliveries as delivery_store

    engine = create_sqlite_engine(tmp_path / "state.sqlite")
    metadata.create_all(engine)
    snapshot = delivery_store.message_snapshot(
        scope_id=None,
        session_id="ses_authority",
        platform="avibe",
        author="user",
        source="user",
        message_type="user",
        text="display text",
        metadata={"origin": "test"},
    )
    with engine.begin() as conn:
        _seed_session(conn)
        delivery_store.insert_delivery(
            conn,
            delivery_id="msg_submission",
            session_id="ses_authority",
            priority="p3",
            state="start_attempting",
            snapshot=snapshot,
            dispatch_text="exact agent prompt",
            current_attempt_id="atm_start",
            current_attempt_kind="start",
            current_target_turn_id="turn_initial",
        )
        delivery_store.insert_turn(
            conn,
            turn_id="turn_initial",
            session_id="ses_authority",
            initial_delivery_id="msg_submission",
            state="starting",
            backend="codex",
        )
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.id)).all() == []

    with engine.begin() as conn:
        materialized = delivery_store.materialize_acceptance(
            conn,
            delivery_id="msg_submission",
            expected_attempt_id="atm_start",
            accepted_turn_id="turn_initial",
            evidence={"kind": "native_start"},
        )
        duplicate = delivery_store.materialize_acceptance(
            conn,
            delivery_id="msg_submission",
            expected_attempt_id=None,
            accepted_turn_id="turn_initial",
            evidence={"kind": "native_start_replay"},
        )
    assert materialized is not None
    assert duplicate is not None
    with engine.connect() as conn:
        message_rows = conn.execute(select(messages)).mappings().all()
        delivery = delivery_store.get_delivery(conn, "msg_submission")
    assert [row["id"] for row in message_rows] == ["msg_submission"]
    assert message_rows[0]["content_text"] == "display text"
    assert delivery is not None
    assert delivery["state"] == "accepted"
    assert delivery["message_id"] == "msg_submission"
    assert delivery["snapshot_json"] is None
    assert delivery["dispatch_text"] == "exact agent prompt"
    assert delivery["current_attempt_id"] is None


def test_production_message_writers_do_not_create_operational_pseudo_types() -> None:
    offenders: list[str] = []
    for root_name in ("core", "modules", "storage", "vibe"):
        for source_path in sorted((REPO_ROOT / root_name).rglob("*.py")):
            if "alembic/versions" in source_path.as_posix():
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "message_type" or not isinstance(keyword.value, ast.Constant):
                        continue
                    if keyword.value.value in PSEUDO_MESSAGE_TYPES:
                        offenders.append(f"{source_path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_legacy_queue_authority_symbols_have_no_production_call_sites() -> None:
    forbidden = {
        "session_deliveries",
        "QUEUED_TYPE",
        "PENDING_TYPE",
        "HARNESS_DEDUPE_TYPE",
        "SILENT_TYPE",
        "QUEUED_DISPATCH_TEXT_KEY",
        "messages_service.clear_queued",
        "messages_service.clear_pending",
        "messages_service.delete_queued",
        "messages_service.remove_queued",
    }
    offenders: list[str] = []
    for root_name in ("core", "modules", "storage", "vibe"):
        for source_path in sorted((REPO_ROOT / root_name).rglob("*.py")):
            if "alembic/versions" in source_path.as_posix():
                continue
            source = source_path.read_text(encoding="utf-8")
            found = sorted(symbol for symbol in forbidden if symbol in source)
            if found:
                offenders.append(f"{source_path.relative_to(REPO_ROOT)}: {', '.join(found)}")
    assert offenders == []


def test_session_turn_storage_mutations_have_one_module_owner() -> None:
    offenders: list[str] = []
    for root_name in ("core", "modules", "storage", "vibe"):
        for source_path in sorted((REPO_ROOT / root_name).rglob("*.py")):
            relative = source_path.relative_to(REPO_ROOT).as_posix()
            if relative == "storage/message_deliveries.py" or "alembic/versions" in relative:
                continue
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if (
                    isinstance(function, ast.Name)
                    and function.id in {"update", "delete", "insert"}
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "session_turns"
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr in {"update", "delete", "insert"}
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "session_turns"
                ):
                    offenders.append(f"{relative}:{node.lineno}")
    assert offenders == []


def test_normal_product_paths_do_not_physically_delete_session_messages() -> None:
    offenders: list[str] = []
    for root_name in ("core", "modules", "storage", "vibe"):
        for source_path in sorted((REPO_ROOT / root_name).rglob("*.py")):
            relative = source_path.relative_to(REPO_ROOT).as_posix()
            if "alembic/versions" in relative or relative == "storage/migrations.py":
                continue
            normalized = " ".join(source_path.read_text(encoding="utf-8").lower().split())
            if "delete from messages" in normalized or "delete(messages)" in normalized or "messages.delete(" in normalized:
                offenders.append(relative)
    assert offenders == []


def test_operational_graph_foreign_keys_are_deferred_no_action() -> None:
    operational_tables = {"agent_sessions", "message_deliveries", "messages", "session_turns"}
    inspected = 0
    for table in (message_deliveries, session_turns):
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table.name not in operational_tables:
                continue
            inspected += 1
            assert constraint.ondelete == "NO ACTION"
            assert constraint.deferrable is True
            assert constraint.initially == "DEFERRED"
    assert inspected == 8


def test_materialization_cas_loss_cannot_publish_a_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storage import message_deliveries as delivery_store

    engine = create_sqlite_engine(tmp_path / "state.sqlite")
    metadata.create_all(engine)
    with engine.begin() as conn:
        _seed_session(conn)
        delivery_store.insert_delivery(
            conn,
            delivery_id="msg_cas_loser",
            session_id="ses_authority",
            priority="p1",
            state="steering",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_authority",
                platform="avibe",
                author="user",
                source="user",
                text="must stay private",
            ),
            dispatch_text="must stay private",
            current_attempt_id="atm_cas_loser",
            current_attempt_kind="steer",
            current_target_turn_id="turn_target",
            current_expected_native_turn_id="native_target",
        )
        delivery_store.insert_delivery(
            conn,
            delivery_id="msg_target",
            session_id="ses_authority",
            priority="p3",
            state="reserved",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_authority",
                platform="avibe",
                author="user",
                source="user",
                text="target",
            ),
            dispatch_text="target",
        )
        delivery_store.insert_turn(
            conn,
            turn_id="turn_target",
            session_id="ses_authority",
            initial_delivery_id="msg_target",
            state="active",
            backend="codex",
        )

    monkeypatch.setattr(delivery_store, "cas_delivery", lambda *args, **kwargs: None)
    with engine.begin() as conn:
        assert delivery_store.materialize_acceptance(
            conn,
            delivery_id="msg_cas_loser",
            expected_attempt_id="atm_cas_loser",
            accepted_turn_id="turn_target",
            evidence={"kind": "accepted_receipt"},
        ) is None
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.id).where(messages.c.id == "msg_cas_loser")).first() is None


def test_delivery_history_is_valid_versioned_json(tmp_path: Path) -> None:
    from storage import message_deliveries as delivery_store

    engine = create_sqlite_engine(tmp_path / "state.sqlite")
    metadata.create_all(engine)
    with engine.begin() as conn:
        _seed_session(conn)
        delivery_store.insert_delivery(
            conn,
            delivery_id="retired_submission",
            session_id="ses_authority",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_authority",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="remove me",
            ),
            dispatch_text="remove me",
        )
        assert delivery_store.retire_queued(conn, "ses_authority", "retired_submission")
    with engine.connect() as conn:
        row = delivery_store.get_delivery(conn, "retired_submission")
        assert conn.execute(select(messages.c.id)).all() == []
    history = json.loads(str(row["delivery_history_json"]))
    assert history["version"] == 1
    assert history["events"][-1]["kind"] == "retire"

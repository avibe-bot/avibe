from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from core.services.session_fork import (
    SESSION_AGENT_UNAVAILABLE_CODE,
    SessionForkError,
    SourceMessageAnchor,
    fork_anchor_is_terminal_agent_output,
    fork_metadata_from_session_metadata,
    fork_source_has_agent_output_after_anchor,
    fork_source_state,
    pending_native_fork,
    pending_native_fork_source,
    reserve_forked_session,
)
from core.scheduled_tasks import resolve_session_id_target
from core.vibe_agents import VibeAgentStore
from modules.im import MessageContext
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage import message_deliveries, messages_service
from storage.models import agent_events, agent_runs, agent_sessions, messages, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope


def _seed_source_session(db_path: Path, tmp_path: Path, *, backend: str = "codex") -> str:
    SQLiteSessionsService(db_path).close()
    store = VibeAgentStore(db_path)
    try:
        worker = store.create(name="worker", backend=backend)
    finally:
        store.close()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="avibe",
                scope_type="project",
                native_id="proj_fork",
                now="2026-06-16T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name="worker",
                    agent_backend=backend,
                    agent_variant=backend,
                    model="gpt-5",
                    reasoning_effort="medium",
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-16T00:00:00Z",
                    updated_at="2026-06-16T00:00:00Z",
                )
            )
            return create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor=None,
                agent_backend=backend,
                agent_variant=backend,
                agent_id=worker.id,
                agent_name="worker",
                model="gpt-5",
                reasoning_effort="medium",
                workdir=str(tmp_path),
                native_session_id="thread-source",
                title="Source",
                metadata={"created_via": "test"},
            )
    finally:
        engine.dispose()


def _seed_started_delivery(conn, *, scope_id: str, session_id: str, text: str) -> str:
    delivery_id = message_deliveries.new_delivery_id()
    turn_id = message_deliveries.new_turn_id()
    attempt_id = message_deliveries.new_attempt_id()
    delivery = message_deliveries.insert_delivery(
        conn,
        delivery_id=delivery_id,
        session_id=session_id,
        priority="p3",
        state="reserved",
        snapshot=message_deliveries.message_snapshot(
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            source="user",
            message_type="user",
            text=text,
        ),
        dispatch_text=text,
    )
    claimed = message_deliveries.claim_start_batch(
        conn,
        turn_id=turn_id,
        session_id=session_id,
        backend="codex",
        deliveries=[delivery],
        dispatch_text=text,
        attempt_id=attempt_id,
    )
    assert message_deliveries.bind_native_start(
        conn,
        turn_id,
        expected_version=int(claimed["turn"]["version"]),
        runtime_key=f"runtime:{turn_id}",
        runtime_turn_id=f"runtime-turn:{turn_id}",
        native_turn_id=f"native:{turn_id}",
    ) is not None
    assert message_deliveries.materialize_start_acceptance(
        conn,
        turn_id=turn_id,
        evidence={"kind": "test_native_acceptance"},
    )
    return turn_id


def test_reserve_forked_session_copies_row_and_applies_overrides(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            source_row = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).mappings().one()
            visible_message = messages_service.append(
                conn,
                scope_id=source_row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="source answer",
            )
            messages_service.append(
                conn,
                scope_id=source_row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="assistant",
                text="hidden process log",
            )
    finally:
        engine.dispose()
    store = VibeAgentStore(db_path)
    try:
        store.create(name="reviewer", backend="codex", model="gpt-5.1", reasoning_effort="high")
    finally:
        store.close()

    result = reserve_forked_session(
        source_session_id=source_id,
        agent_name="reviewer",
        model="gpt-5.2",
        reasoning_effort="low",
        db_path=db_path,
    )

    assert result.session_id != source_id
    assert result.fork.source_native_session_id == "thread-source"
    assert result.fork.source_message_id == visible_message["id"]
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(row["metadata_json"])
    assert row["agent_name"] == "reviewer"
    assert row["agent_backend"] == "codex"
    assert row["agent_variant"] == "codex"
    assert row["model"] == "gpt-5.2"
    assert row["reasoning_effort"] == "low"
    assert row["workdir"] == str(tmp_path)
    assert row["native_session_id"] == ""
    assert row["session_anchor"] == result.session_id
    assert row["title"] == "Fork Source"
    assert metadata["fork_source_message_id"] == visible_message["id"]
    assert metadata["fork_source_session_title"] == "Source"
    assert metadata["fork_trim_latest_running_turn"] is False


def test_reserve_forked_codex_running_fork_marks_trim(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)

    result = reserve_forked_session(
        source_session_id=source_id,
        trim_latest_running_turn=True,
        native_turn_started=True,
        db_path=db_path,
    )

    assert result.fork.source_backend == "codex"
    assert result.fork.trim_latest_running_turn is True
    assert result.fork.native_turn_started is True
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(row["metadata_json"])
    assert metadata["fork_trim_latest_running_turn"] is True
    assert metadata["fork_native_turn_started"] is True


@pytest.mark.parametrize(
    ("author", "message_type"),
    [("user", "user"), ("harness", messages_service.HARNESS_TYPE)],
)
def test_reserve_forked_session_infers_running_input_anchor_without_live_hint(
    tmp_path: Path,
    author: str,
    message_type: str,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).mappings().one()
            running_input = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author=author,
                message_type=message_type,
                text="long running request",
                source="harness" if author == "harness" else None,
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.source_message_id == running_input["id"]
    assert result.fork.trim_latest_running_turn is True
    assert result.fork.native_turn_started is False
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            forked = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(forked["metadata_json"])
    assert metadata["fork_source_message_id"] == running_input["id"]
    assert metadata["fork_trim_latest_running_turn"] is True
    assert metadata["fork_native_turn_started"] is False


def test_reserve_forked_session_does_not_infer_trim_for_claude(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path, backend="claude")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).mappings().one()
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(
                    agent_backend="claude",
                    agent_variant="claude",
                    native_session_id="claude-source",
                )
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="active claude request",
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.source_backend == "claude"
    assert result.fork.trim_latest_running_turn is False
    assert result.fork.native_turn_started is False


def _seed_opencode_messages(
    xdg_home: Path,
    native_session_id: str,
    roles: list[str],
    *,
    completed_assistant: bool = True,
) -> None:
    db_path = xdg_home / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT, time_created INTEGER, data TEXT)"
        )
        for index, role in enumerate(roles, start=1):
            message_id = f"oc-msg-{index}"
            conn.execute(
                "INSERT INTO message (id, data) VALUES (?, ?)",
                (
                    message_id,
                    json.dumps(
                        {
                            "role": role,
                            "time": {"completed": index} if role == "assistant" and completed_assistant else {},
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO part (id, session_id, message_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
                (f"part-{index}", native_session_id, message_id, index, json.dumps({"type": "text"})),
            )


def test_reserve_forked_opencode_running_fork_records_frozen_native_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "oc-source", ["user", "assistant", "user"])
    source_id = _seed_source_session(db_path, tmp_path, backend="opencode")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).mappings().one()
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(
                    agent_backend="opencode",
                    agent_variant="opencode",
                    native_session_id="oc-source",
                )
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="completed answer",
                native_message_id="oc-msg-prev",
            )
            latest_user = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="do the long task",
                native_message_id="oc-msg-user",
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(
        source_session_id=source_id,
        trim_latest_running_turn=True,
        native_turn_started=True,
        db_path=db_path,
    )

    assert result.fork.source_message_id == latest_user["id"]
    assert result.fork.trim_latest_running_turn is True
    assert result.fork.native_turn_started is True
    assert result.fork.opencode_fork_message_id == "oc-msg-3"
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            forked = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(forked["metadata_json"])
    assert metadata["fork_source_message_id"] == latest_user["id"]
    assert metadata["fork_opencode_message_id"] == "oc-msg-3"
    assert metadata["fork_trim_latest_running_turn"] is True
    assert metadata["fork_native_turn_started"] is True
    assert "fork_opencode_boundary_from_active_run" not in metadata


def test_reserve_forked_opencode_active_run_freezes_native_boundary_without_live_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "oc-source", ["user", "assistant", "user"])
    source_id = _seed_source_session(db_path, tmp_path, backend="opencode")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(
                    agent_backend="opencode",
                    agent_variant="opencode",
                    native_session_id="oc-source",
                )
            )
            conn.execute(
                agent_runs.insert().values(
                    id="run-active-source",
                    definition_id=None,
                    run_type="agent",
                    status="running",
                    source_kind="cli",
                    source_actor=None,
                    parent_run_id=None,
                    agent_name="worker",
                    agent_id="agent-worker",
                    agent_backend="opencode",
                    model=None,
                    reasoning_effort=None,
                    session_policy="resume",
                    session_id=source_id,
                    legacy_session_key=None,
                    post_to=None,
                    deliver_key=None,
                    prompt="active source prompt",
                    message="active source prompt",
                    message_payload_json="{}",
                    result_text=None,
                    result_payload_json=None,
                    message_ids_json=None,
                    callback_session_id=None,
                    callback_status=None,
                    callback_error=None,
                    callback_run_id=None,
                    callback_completed_at=None,
                    cancel_requested=0,
                    cancel_requested_at=None,
                    pid=None,
                    exit_code=None,
                    error=None,
                    stdout=None,
                    stderr=None,
                    created_at="2026-06-16T00:00:01Z",
                    started_at="2026-06-16T00:00:02Z",
                    completed_at=None,
                    updated_at="2026-06-16T00:00:02Z",
                    metadata_json="{}",
                )
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.trim_latest_running_turn is True
    assert result.fork.native_turn_started is True
    assert result.fork.opencode_fork_message_id == "oc-msg-3"
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            forked = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(forked["metadata_json"])
    assert metadata["fork_trim_latest_running_turn"] is True
    assert metadata["fork_native_turn_started"] is True
    assert metadata["fork_opencode_message_id"] == "oc-msg-3"
    assert metadata["fork_opencode_boundary_from_active_run"] is True


def test_reserve_forked_opencode_running_first_turn_records_user_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "oc-source", ["user"])
    source_id = _seed_source_session(db_path, tmp_path, backend="opencode")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(
                    agent_backend="opencode",
                    agent_variant="opencode",
                    native_session_id="oc-source",
                )
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(
        source_session_id=source_id,
        trim_latest_running_turn=True,
        native_turn_started=True,
        db_path=db_path,
    )

    assert result.fork.trim_latest_running_turn is True
    assert result.fork.native_turn_started is True
    assert result.fork.opencode_fork_message_id == "oc-msg-1"
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            forked = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(forked["metadata_json"])
    assert metadata["fork_opencode_message_id"] == "oc-msg-1"
    assert "fork_opencode_fork_empty_history" not in metadata


def test_reserve_forked_session_clears_stale_opencode_active_run_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "oc-source", ["user"])
    source_id = _seed_source_session(db_path, tmp_path, backend="opencode")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(
                    agent_backend="opencode",
                    agent_variant="opencode",
                    native_session_id="oc-source",
                    metadata_json=json.dumps(
                        {
                            "created_via": "session_fork",
                            "fork_opencode_message_id": "stale-oc-msg",
                            "fork_opencode_fork_empty_history": True,
                            "fork_opencode_boundary_from_active_run": True,
                        }
                    ),
                )
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            forked = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(forked["metadata_json"])
    assert "fork_opencode_message_id" not in metadata
    assert "fork_opencode_fork_empty_history" not in metadata
    assert "fork_opencode_boundary_from_active_run" not in metadata


def test_reserve_forked_opencode_missing_boundary_preserves_trim_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    xdg_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    _seed_opencode_messages(xdg_home, "oc-source", [])
    source_id = _seed_source_session(db_path, tmp_path, backend="opencode")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(
                    agent_backend="opencode",
                    agent_variant="opencode",
                    native_session_id="oc-source",
                )
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(
        source_session_id=source_id,
        trim_latest_running_turn=True,
        native_turn_started=False,
        db_path=db_path,
    )

    assert result.fork.trim_latest_running_turn is True
    assert result.fork.native_turn_started is False


def test_reserve_forked_session_uses_generic_title_for_untitled_source(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(title=None)
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    metadata = json.loads(row["metadata_json"])
    assert row["title"] == "Fork"
    assert metadata["fork_source_session_title"] == ""


def test_reserve_forked_session_keeps_im_anchor_and_resets_variant_for_agent_override(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    SQLiteSessionsService(db_path).close()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="slack",
                scope_type="channel",
                native_id="C123",
                now="2026-06-16T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name="worker",
                    agent_backend="codex",
                    agent_variant="reviewer",
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-16T00:00:00Z",
                    updated_at="2026-06-16T00:00:00Z",
                )
            )
            source_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_171717.123",
                agent_backend="codex",
                agent_variant="reviewer",
                agent_id="agent-worker",
                agent_name="worker",
                workdir=str(tmp_path),
                native_session_id="thread-source",
            )
    finally:
        engine.dispose()
    store = VibeAgentStore(db_path)
    try:
        store.create(name="auditor", backend="codex")
    finally:
        store.close()

    result = reserve_forked_session(
        source_session_id=source_id,
        agent_name="auditor",
        db_path=db_path,
    )

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    assert row["agent_name"] == "auditor"
    assert row["agent_variant"] == "codex"
    assert row["session_anchor"].startswith("slack_171717.123:fork_")

    resolved = resolve_session_id_target(result.session_id, db_path=db_path)
    assert resolved.session_key.to_key() == "slack::channel::C123::thread::171717.123"


def test_reserve_forked_session_reanchors_when_moved_to_new_im_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    SQLiteSessionsService(db_path).close()
    store = VibeAgentStore(db_path)
    try:
        worker = store.create(name="worker", backend="codex")
    finally:
        store.close()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            source_scope_id = upsert_scope(
                conn,
                platform="slack",
                scope_type="channel",
                native_id="C123",
                now="2026-06-16T00:00:00Z",
            )
            target_scope_id = upsert_scope(
                conn,
                platform="slack",
                scope_type="channel",
                native_id="C999",
                now="2026-06-16T00:00:00Z",
            )
            for scope_id in (source_scope_id, target_scope_id):
                conn.execute(
                    scope_settings.insert().values(
                        scope_id=scope_id,
                        enabled=1,
                        role=None,
                        workdir=str(tmp_path),
                        agent_name="worker",
                        agent_backend="codex",
                        agent_variant="codex",
                        model=None,
                        reasoning_effort=None,
                        require_mention=None,
                        settings_version=1,
                        settings_json="{}",
                        created_at="2026-06-16T00:00:00Z",
                        updated_at="2026-06-16T00:00:00Z",
                    )
                )
            source_id = create_agent_session_row(
                conn,
                scope_id=source_scope_id,
                session_anchor="slack_171717.123",
                agent_backend="codex",
                agent_variant="codex",
                agent_id=worker.id,
                agent_name="worker",
                workdir=str(tmp_path),
                native_session_id="thread-source",
                metadata={
                    "legacy_scope_key": source_scope_id,
                    "private_agent_run": True,
                    "no_delivery": True,
                },
            )
    finally:
        engine.dispose()

    first_result = reserve_forked_session(
        source_session_id=source_id,
        scope_id=target_scope_id,
        db_path=db_path,
    )
    second_result = reserve_forked_session(
        source_session_id=source_id,
        scope_id=target_scope_id,
        db_path=db_path,
    )

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_sessions)
                    .where(agent_sessions.c.id.in_([first_result.session_id, second_result.session_id]))
                    .order_by(agent_sessions.c.id)
                ).mappings()
            )
    finally:
        engine.dispose()

    assert len(rows) == 2
    row = rows[0]
    metadata = json.loads(row["metadata_json"])
    assert row["scope_id"] == target_scope_id
    assert row["session_anchor"].startswith("slack_C999:fork_")
    assert metadata["fork_target_scope_id"] == target_scope_id
    assert metadata["legacy_scope_key"] == target_scope_id
    assert metadata["private_agent_run"] is True
    assert metadata["no_delivery"] is True
    assert rows[0]["session_anchor"] != rows[1]["session_anchor"]

    resolved = resolve_session_id_target(first_result.session_id, db_path=db_path)
    assert resolved.session_key.to_key() == "slack::channel::C999"
    assert resolved.session_key.thread_id is None
    assert resolved.session_anchor.startswith("slack_C999:fork_")
    assert resolved.suppress_delivery is False


def test_reserve_forked_session_reanchors_explicit_parent_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    SQLiteSessionsService(db_path).close()
    store = VibeAgentStore(db_path)
    try:
        worker = store.create(name="worker", backend="codex")
    finally:
        store.close()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform="slack",
                scope_type="channel",
                native_id="C123",
                now="2026-06-16T00:00:00Z",
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name="worker",
                    agent_backend="codex",
                    agent_variant="codex",
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-06-16T00:00:00Z",
                    updated_at="2026-06-16T00:00:00Z",
                )
            )
            source_id = create_agent_session_row(
                conn,
                scope_id=scope_id,
                session_anchor="slack_171717.123",
                agent_backend="codex",
                agent_variant="codex",
                agent_id=worker.id,
                agent_name="worker",
                workdir=str(tmp_path),
                native_session_id="thread-source",
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(
        source_session_id=source_id,
        scope_id=scope_id,
        db_path=db_path,
    )

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id).limit(1)
            ).mappings().one()
    finally:
        engine.dispose()

    assert row["scope_id"] == scope_id
    assert row["session_anchor"].startswith("slack_C123:fork_")

    resolved = resolve_session_id_target(result.session_id, db_path=db_path)
    assert resolved.session_key.to_key() == "slack::channel::C123"
    assert resolved.session_key.thread_id is None
    assert resolved.session_anchor.startswith("slack_C123:fork_")


def test_reserve_forked_session_rejects_backend_change(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    store = VibeAgentStore(db_path)
    try:
        store.create(name="claude-worker", backend="claude")
    finally:
        store.close()

    with pytest.raises(SessionForkError, match="backend"):
        reserve_forked_session(
            source_session_id=source_id,
            agent_name="claude-worker",
            db_path=db_path,
        )


def test_reserve_forked_session_rejects_archived_inherited_agent(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    store = VibeAgentStore(db_path)
    try:
        replacement = store.create(name="reviewer", backend="codex")
        archived = store.archive("worker")
        assert archived is not None
    finally:
        store.close()

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            session_count = conn.execute(
                select(func.count()).select_from(agent_sessions)
            ).scalar_one()
    finally:
        engine.dispose()

    with pytest.raises(SessionForkError, match="source session Agent is unavailable") as exc_info:
        reserve_forked_session(source_session_id=source_id, db_path=db_path)
    assert exc_info.value.code == SESSION_AGENT_UNAVAILABLE_CODE
    assert exc_info.value.details == {"source_session_id": source_id}

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(select(func.count()).select_from(agent_sessions)).scalar_one()
                == session_count
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(
        source_session_id=source_id,
        agent_name=replacement.name,
        db_path=db_path,
    )
    assert result.agent_id == replacement.id
    assert result.agent_name == replacement.name


def test_reserve_forked_session_canonicalizes_legacy_inherited_agent_name(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_id)
                .values(agent_name="WORKER")
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.agent_name == "worker"
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                select(agent_sessions.c.agent_name).where(
                    agent_sessions.c.id == result.session_id
                )
            ).scalar_one() == "worker"
    finally:
        engine.dispose()


def test_reserve_forked_session_agent_override_keeps_source_model_when_not_overridden(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    store = VibeAgentStore(db_path)
    try:
        store.create(name="reviewer", backend="codex", model="agent-model", reasoning_effort="agent-effort")
    finally:
        store.close()

    result = reserve_forked_session(
        source_session_id=source_id,
        agent_name="reviewer",
        db_path=db_path,
    )

    assert result.agent_name == "reviewer"
    assert result.model == "gpt-5"
    assert result.reasoning_effort == "medium"


def test_pending_native_fork_source_requires_empty_target_native() -> None:
    ctx = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform_specific={
            "agent_session_target": {
                "id": "ses-target",
                "agent_backend": "codex",
                "native_session_id": "",
                "native_session_fork": {
                    "source_session_id": "ses-source",
                    "source_native_session_id": "thread-source",
                    "source_backend": "codex",
                },
            }
        },
    )

    assert pending_native_fork_source(ctx, "codex") == "thread-source"
    assert pending_native_fork_source(ctx, "claude") is None
    ctx.platform_specific["agent_session_target"]["agent_backend"] = "claude"
    assert pending_native_fork_source(ctx, "codex") is None
    ctx.platform_specific["agent_session_target"]["agent_backend"] = "codex"
    ctx.platform_specific["agent_session_target"]["native_session_id"] = "thread-existing"
    assert pending_native_fork_source(ctx, "codex") is None


def test_pending_native_fork_preserves_trim_metadata() -> None:
    ctx = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform_specific={
            "agent_session_target": {
                "id": "ses-target",
                "agent_backend": "opencode",
                "native_session_id": "",
                "native_session_fork": {
                    "source_session_id": "ses-source",
                    "source_native_session_id": "oc-source",
                    "source_backend": "opencode",
                    "source_message_id": "msg-avibe",
                    "trim_latest_running_turn": True,
                    "native_turn_started": True,
                    "opencode_fork_message_id": "oc-msg-2",
                },
            }
        },
    )

    assert pending_native_fork(ctx, "opencode") == {
        "source_session_id": "ses-source",
        "source_native_session_id": "oc-source",
        "source_backend": "opencode",
        "source_message_id": "msg-avibe",
        "trim_latest_running_turn": True,
        "native_turn_started": True,
        "opencode_fork_message_id": "oc-msg-2",
    }


def test_pending_native_fork_source_uses_target_session_metadata() -> None:
    ctx = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform_specific={
            "agent_session_target": {
                "id": "ses-target",
                "agent_backend": "codex",
                "native_session_id": "",
                "metadata": {
                    "created_via": "session_fork",
                    "fork_source_session_id": "ses-source",
                    "fork_source_native_session_id": "thread-source",
                    "fork_source_backend": "codex",
                },
            }
        },
    )

    assert pending_native_fork_source(ctx, "codex") == "thread-source"


def test_fork_source_has_agent_output_after_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            user = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="do the long task",
            )

        assert fork_source_has_agent_output_after_anchor(
            {"source_session_id": source_id, "source_message_id": user["id"]}
        ) is False

        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="done",
            )

        assert fork_source_has_agent_output_after_anchor(
            {"source_session_id": source_id, "source_message_id": user["id"]}
        ) is True
    finally:
        engine.dispose()


def test_fork_source_state_identifies_completed_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            result = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="done",
            )

        fork = {"source_session_id": source_id, "source_message_id": result["id"]}
        state = fork_source_state(fork)

        assert state.anchor_is_terminal_agent_output is True
        assert state.has_messages_after_anchor is False
        assert state.has_terminal_agent_output_after_anchor is False
        assert fork_anchor_is_terminal_agent_output(fork) is True
    finally:
        engine.dispose()


def test_fork_source_state_tracks_nonterminal_messages_after_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            user = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="do the long task",
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="assistant",
                text="thinking",
            )

        state = fork_source_state({"source_session_id": source_id, "source_message_id": user["id"]})

        assert state.anchor_is_terminal_agent_output is False
        assert state.latest_after_anchor_author == "agent"
        assert state.latest_after_anchor_type == "assistant"
        assert state.has_messages_after_anchor is True
        assert state.has_terminal_agent_output_after_anchor is False
    finally:
        engine.dispose()


def test_fork_source_state_uses_delivery_acceptance_for_anchor_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == source_id)
            ).mappings().one()
            conn.execute(
                messages.insert(),
                [
                    {
                        "id": "msg_prior_result",
                        "scope_id": row["scope_id"],
                        "session_id": source_id,
                        "platform": "avibe",
                        "author": "agent",
                        "type": "result",
                        "source": None,
                        "content_text": "prior result",
                        "content_json": "{}",
                        "metadata_json": "{}",
                        "created_at": "2026-08-04T00:00:02.000000Z",
                        "updated_at": "2026-08-04T00:00:02.000000Z",
                        "delivered_at": None,
                    },
                    {
                        "id": "msg_queued_anchor",
                        "scope_id": row["scope_id"],
                        "session_id": source_id,
                        "platform": "avibe",
                        "author": "user",
                        "type": "user",
                        "source": "user",
                        "content_text": "queued prompt",
                        "content_json": "{}",
                        "metadata_json": "{}",
                        "created_at": "2026-08-04T00:00:01.000000Z",
                        "updated_at": "2026-08-04T00:00:03.000000Z",
                        "delivered_at": "2026-08-04T00:00:03.000000Z",
                    },
                ],
            )

        state = fork_source_state(
            {
                "source_session_id": source_id,
                "source_message_id": "msg_queued_anchor",
            }
        )

        assert state.has_messages_after_anchor is False
        assert state.latest_after_anchor_author is None
        assert state.has_terminal_agent_output_after_anchor is False
    finally:
        engine.dispose()


def test_fork_source_state_ignores_notify_as_terminal_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            notify = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="notify",
                text="still working",
            )

        fork = {"source_session_id": source_id, "source_message_id": notify["id"]}
        state = fork_source_state(fork)

        assert state.anchor_is_terminal_agent_output is False
        assert state.latest_after_anchor_author is None
        assert state.latest_after_anchor_type is None
        assert state.has_messages_after_anchor is False
        assert state.has_terminal_agent_output_after_anchor is False
        assert fork_anchor_is_terminal_agent_output(fork) is False
    finally:
        engine.dispose()


def test_fork_source_state_treats_backend_failure_notify_anchor_as_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            notify = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="notify",
                text="Codex backend failed",
                metadata={"event": "backend_failure", "failure_id": "failure_1"},
            )

        fork = {"source_session_id": source_id, "source_message_id": notify["id"]}
        state = fork_source_state(fork)

        assert state.anchor_is_terminal_agent_output is True
        assert state.has_messages_after_anchor is False
        assert fork_anchor_is_terminal_agent_output(fork) is True
    finally:
        engine.dispose()


def test_fork_source_state_keeps_detached_failure_outside_current_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            notify = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="notify",
                text="Background run failed",
                metadata={
                    "event": "backend_failure",
                    "failure_id": "failure_detached",
                    "detached": True,
                },
            )

        fork = {"source_session_id": source_id, "source_message_id": notify["id"]}
        state = fork_source_state(fork)

        assert state.anchor_is_terminal_agent_output is False
        assert fork_anchor_is_terminal_agent_output(fork) is False
    finally:
        engine.dispose()


def test_fork_source_state_treats_backend_failure_notify_after_anchor_as_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            user = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="Do the task",
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="notify",
                text="Codex backend failed",
                metadata={"event": "backend_failure", "failure_id": "failure_1"},
            )

        state = fork_source_state({"source_session_id": source_id, "source_message_id": user["id"]})

        assert state.latest_after_anchor_author == "agent"
        assert state.latest_after_anchor_type == "notify"
        assert state.has_messages_after_anchor is True
        assert state.has_terminal_agent_output_after_anchor is True
    finally:
        engine.dispose()


def test_fork_source_state_ignores_operational_rows_after_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            user = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="do the long task",
            )
            message_deliveries.enqueue_queued(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                text="queued",
            )
            message_deliveries.set_draft(conn, source_id, "draft")
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="notify",
                text="notify",
            )

        state = fork_source_state({"source_session_id": source_id, "source_message_id": user["id"]})

        assert state.anchor_is_terminal_agent_output is False
        assert state.latest_after_anchor_author is None
        assert state.latest_after_anchor_type is None
        assert state.has_messages_after_anchor is False
        assert state.has_terminal_agent_output_after_anchor is False
    finally:
        engine.dispose()


def test_fork_source_state_uses_latest_progress_after_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(agent_sessions).where(agent_sessions.c.id == source_id)).mappings().one()
            anchor = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="first task",
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="first done",
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="second task",
            )

        state = fork_source_state({"source_session_id": source_id, "source_message_id": anchor["id"]})

        assert state.latest_after_anchor_author == "user"
        assert state.latest_after_anchor_type == "user"
        assert state.has_messages_after_anchor is True
        assert state.has_terminal_agent_output_after_anchor is False
        assert state.has_input_turn_after_anchor is True
    finally:
        engine.dispose()


def test_harness_message_is_an_input_turn() -> None:
    assert SourceMessageAnchor(
        author="harness",
        message_type="harness",
    ).is_running_input_turn is True


def test_dispatching_annotation_is_an_input_turn() -> None:
    assert SourceMessageAnchor(
        author="harness",
        message_type=messages_service.ANNOTATION_TYPE,
    ).is_running_input_turn is True
    assert SourceMessageAnchor(
        author="user",
        message_type=messages_service.ANNOTATION_TYPE,
    ).is_running_input_turn is False


def test_fork_source_state_tracks_harness_turn_after_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import paths
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    db_path = paths.get_sqlite_state_path()
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == source_id)
            ).mappings().one()
            anchor = messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="previous result",
            )
            messages_service.append(
                conn,
                scope_id=row["scope_id"],
                session_id=source_id,
                platform="avibe",
                author="harness",
                message_type=messages_service.HARNESS_TYPE,
                text="automated follow-up",
                source="harness",
            )

        state = fork_source_state(
            {"source_session_id": source_id, "source_message_id": anchor["id"]}
        )

        assert state.latest_after_anchor_author == "harness"
        assert state.latest_after_anchor_type == messages_service.HARNESS_TYPE
        assert state.has_messages_after_anchor is True
        assert state.has_terminal_agent_output_after_anchor is False
        assert state.has_input_turn_after_anchor is True
    finally:
        engine.dispose()


def test_fork_metadata_from_session_metadata_uses_pending_row_fields() -> None:
    metadata = {
        "created_via": "session_fork",
        "fork_source_session_id": "ses-source",
        "fork_source_native_session_id": "thread-source",
        "fork_source_backend": "codex",
    }

    assert fork_metadata_from_session_metadata(metadata) == {
        "source_session_id": "ses-source",
        "source_native_session_id": "thread-source",
        "source_backend": "codex",
    }
    assert fork_metadata_from_session_metadata({"created_via": "session_fork"}) is None


def test_fork_metadata_from_session_metadata_preserves_trim_fields() -> None:
    metadata = {
        "created_via": "session_fork",
        "fork_source_session_id": "ses-source",
        "fork_source_native_session_id": "oc-source",
        "fork_source_backend": "opencode",
        "fork_source_message_id": "msg-avibe",
        "fork_trim_latest_running_turn": True,
        "fork_native_turn_started": True,
        "fork_opencode_message_id": "oc-msg-2",
    }

    assert fork_metadata_from_session_metadata(metadata) == {
        "source_session_id": "ses-source",
        "source_native_session_id": "oc-source",
        "source_backend": "opencode",
        "source_message_id": "msg-avibe",
        "trim_latest_running_turn": True,
        "native_turn_started": True,
        "opencode_fork_message_id": "oc-msg-2",
    }


def test_reserve_forked_session_silent_completion_is_terminal_no_trim(tmp_path: Path) -> None:
    """A reply-less durable terminal snapshot prevents running-Turn trimming."""
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)  # agent_backend='codex'
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).mappings().one()["scope_id"]
            turn_id = _seed_started_delivery(
                conn,
                scope_id=scope_id,
                session_id=source_id,
                text="do the thing",
            )
            messages_service.append(
                conn, scope_id=scope_id, session_id=source_id, platform="avibe",
                author="agent", message_type="assistant", text="working",
            )
            message_deliveries.terminalize_turn(
                conn,
                turn_id,
                outcome="completed",
                settled_by="terminal_result",
                evidence_kind="test_replyless_completion",
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)
    # A terminal exists after the input anchor → not a running turn → no trim.
    assert result.fork.trim_latest_running_turn is False


def test_not_written_successor_does_not_mask_accepted_source_turn(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).scalar_one()
            turn_id = _seed_started_delivery(
                conn,
                scope_id=scope_id,
                session_id=source_id,
                text="keep working",
            )
            successor_id = message_deliveries.new_delivery_id()
            successor_turn_id = message_deliveries.new_turn_id()
            successor = message_deliveries.insert_delivery(
                conn,
                delivery_id=successor_id,
                session_id=source_id,
                priority="p0",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=scope_id,
                    session_id=source_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type="user",
                    text="replacement",
                ),
                dispatch_text="replacement",
            )
            message_deliveries.insert_turn(
                conn,
                turn_id=successor_turn_id,
                session_id=source_id,
                initial_delivery_id=successor_id,
                state="waiting",
                backend="codex",
            )
            assert message_deliveries.cas_delivery(
                conn,
                successor_id,
                expected_version=int(successor["version"]),
                expected_states=("reserved",),
                values={
                    "state": "interrupt_waiting",
                    "turn_id": successor_turn_id,
                    "turn_role": "initial",
                    "turn_position": 0,
                },
            ) is not None
            message_deliveries.terminalize_turn(
                conn,
                successor_turn_id,
                outcome="not_written",
                settled_by="definitive_stop_receipt",
                evidence_kind="stop_refused",
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.trim_latest_running_turn is True

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            message_deliveries.terminalize_turn(
                conn,
                turn_id,
                outcome="completed",
                settled_by="terminal_result",
                evidence_kind="replyless_completion",
            )
    finally:
        engine.dispose()

    completed = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert completed.fork.trim_latest_running_turn is False


def test_fork_anchor_uses_active_turn_initial_delivery_before_transcript_order(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).scalar_one()
            previous_result = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="previous result persisted after the queued submission",
            )
            conn.execute(
                messages.update()
                .where(messages.c.id == previous_result["id"])
                .values(
                    created_at="2026-08-01T00:02:00Z",
                    updated_at="2026-08-01T00:02:00Z",
                )
            )
            delivery_id = message_deliveries.new_delivery_id()
            turn_id = message_deliveries.new_turn_id()
            attempt_id = message_deliveries.new_attempt_id()
            delivery = message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=source_id,
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=scope_id,
                    session_id=source_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type="user",
                    text="queued before the previous result",
                ),
                dispatch_text="queued before the previous result",
                now="2026-08-01T00:01:00Z",
            )
            claimed = message_deliveries.claim_start_batch(
                conn,
                turn_id=turn_id,
                session_id=source_id,
                backend="codex",
                deliveries=[delivery],
                dispatch_text="queued before the previous result",
                attempt_id=attempt_id,
            )
            assert message_deliveries.bind_native_start(
                conn,
                turn_id,
                expected_version=int(claimed["turn"]["version"]),
                runtime_key="runtime",
                runtime_turn_id="runtime-turn",
                native_turn_id="native-turn",
            ) is not None
            assert message_deliveries.materialize_start_acceptance(
                conn,
                turn_id=turn_id,
                evidence={"kind": "delayed_start_acceptance"},
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.source_message_id == delivery_id
    assert result.fork.trim_latest_running_turn is True


@pytest.mark.parametrize("start_receipt", ["unwritten", "unknown"])
def test_fork_anchor_treats_pre_materialization_start_as_running(
    tmp_path: Path,
    start_receipt: str,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).scalar_one()
            previous_result = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=source_id,
                platform="avibe",
                author="agent",
                message_type="result",
                text="previous terminal boundary",
            )
            delivery_id = message_deliveries.new_delivery_id()
            turn_id = message_deliveries.new_turn_id()
            attempt_id = message_deliveries.new_attempt_id()
            delivery = message_deliveries.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=source_id,
                priority="p3",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=scope_id,
                    session_id=source_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type="user",
                    text="possibly written native input",
                ),
                dispatch_text="possibly written native input",
            )
            claimed = message_deliveries.claim_start_batch(
                conn,
                turn_id=turn_id,
                session_id=source_id,
                backend="codex",
                deliveries=[delivery],
                dispatch_text="possibly written native input",
                attempt_id=attempt_id,
            )
            if start_receipt == "unknown":
                assert message_deliveries.mark_start_unknown(
                    conn,
                    turn_id,
                    expected_version=int(claimed["turn"]["version"]),
                    receipt={"reason": "restart_without_native_evidence"},
                ) is not None
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.source_message_id == previous_result["id"]
    assert result.fork.trim_latest_running_turn is True


def test_reserve_forked_session_migrated_silent_completion_is_terminal_no_trim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migrated legacy silent event remains a terminal fork boundary."""

    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).scalar_one()
            message_micros = int(
                datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1_000_000
            )
            monkeypatch.setattr(
                messages_service,
                "_new_message_id",
                lambda: f"msg_{message_micros:015x}{'0' * 8}",
            )
            message = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=source_id,
                platform="avibe",
                author="user",
                message_type="user",
                text="latest completed input",
            )
            conn.execute(
                messages.update()
                .where(messages.c.id == message["id"])
                .values(
                    created_at="2026-08-01T00:00:00Z",
                    updated_at="2026-08-01T00:00:00Z",
                )
            )
            conn.execute(
                agent_events.insert().values(
                    id="evt_legacy_silent_fork_boundary",
                    scope_id=scope_id,
                    session_id=source_id,
                    turn_id=None,
                    run_id=None,
                    platform="avibe",
                    agent_name="worker",
                    backend="codex",
                    event_type="silent_terminal",
                    visibility="trace",
                    sequence=None,
                    content_text=None,
                    content_json="{}",
                    metadata_json=json.dumps(
                        {
                            "legacy_message_id": "msg_legacy_silent",
                            "migration_revision": "20260731_0043",
                        }
                    ),
                    source="agent",
                    created_at="2026-08-01T00:00:01Z",
                    updated_at="2026-08-01T00:00:01Z",
                )
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.trim_latest_running_turn is False


def test_earlier_same_second_silent_terminal_does_not_close_latest_input(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)
    engine = create_sqlite_engine(db_path)
    base = 1_800_000_000_000_000
    timestamp = "2026-08-01T00:00:00Z"
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(
                    agent_sessions.c.id == source_id
                )
            ).scalar_one()
            conn.execute(
                agent_events.insert().values(
                    id=f"evt_{base + 1_000:015x}{'0' * 8}",
                    scope_id=scope_id,
                    session_id=source_id,
                    platform="avibe",
                    event_type="silent_terminal",
                    visibility="trace",
                    content_json="{}",
                    metadata_json="{}",
                    source="agent",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            conn.execute(
                messages.insert().values(
                    id=f"msg_{base + 2_000:015x}{'0' * 8}",
                    scope_id=scope_id,
                    session_id=source_id,
                    platform="avibe",
                    author="user",
                    type="user",
                    source="user",
                    content_text="new still-running input",
                    content_json="{}",
                    metadata_json="{}",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)

    assert result.fork.trim_latest_running_turn is True


def test_forking_an_inherited_null_session_keeps_its_explicit_pins(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-248 — an OMITTED fork override COPIES the column, so it copies the pin.

    ``reserve_forked_session`` resolves ``target_model = _clean_optional(model) if
    model is not None else row["model"]``: when the caller passes no ``model`` /
    ``reasoning_effort``, the fork's column is the SOURCE's column, verbatim. The
    source's ``explicit_setting_overrides`` marker is a claim about that same
    value, so for a copied field the claim is still true and must survive the fork.

    The first version of this guard reconciled it exactly backwards — it cleared
    the marker for the fields the fork had NOT supplied, i.e. precisely the copied
    ones. Forking an explicit-null session (the user pinned "no model, no effort"
    on purpose, or a preserved ``create_once`` rebind did, HFR-244) then produced a
    fork that merely *looked* like it inherited nulls. Nothing is visible until the
    Agent's defaults next move: the marker is gone, so turn-start materialization
    (HFR-249) pins today's Agent default onto the fork and dispatch runs it with a
    model and a reasoning effort the source had deliberately pinned away.

    Asserted on the real ``AgentRequest``, because that is the only place the
    difference is observable — a NULL session column means "inherit from the Agent"
    to every other session, so the marker is the ONLY thing standing between the
    fork's nulls and the Agent's live defaults.
    """
    import asyncio

    from core.scheduled_tasks import ScheduledTaskStore
    from modules.agents.base import AgentRequest
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY
    from storage.sessions_service import resolve_scope_from_legacy_key
    from tests.test_scheduled_tasks import _binding_env, _dispatching_binding_service

    db_path = _binding_env(tmp_path, monkeypatch, backends=("claude", "codex"), default="codex")

    agent_store = VibeAgentStore(db_path)
    try:
        # The Agent model is explicit by invariant; the session below pins it away.
        source_agent = agent_store.create(name="nightly", backend="claude")
    finally:
        agent_store.close()
    assert source_agent.model == "claude-opus-5"
    assert source_agent.reasoning_effort is None

    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        scope_id = resolve_scope_from_legacy_key(
            conn, "slack::channel::C123", now="2026-07-28T00:00:00Z"
        )
        assert scope_id is not None
        source_id = create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor="slack_C123:definition_abc",
            agent_backend="claude",
            agent_variant="claude",
            agent_id=source_agent.id,
            agent_name=source_agent.name,
            # NULL columns + the marker naming them: this session pins "nothing",
            # it does not inherit.
            model=None,
            reasoning_effort=None,
            native_session_id="native-1",
            workdir=str(tmp_path),
            title="Source",
            metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["model", "reasoning_effort"]},
        )

    # The fork supplies NEITHER setting: both columns are copied from the source.
    forked = reserve_forked_session(source_session_id=source_id, db_path=db_path)
    assert forked.model is None and forked.reasoning_effort is None

    # The Agent gains defaults AFTER the fork — an ordinary Agent Settings edit,
    # with no way to know a fork of an explicit-null session points at it.
    agent_store = VibeAgentStore(db_path)
    try:
        edited_agent = agent_store.update("nightly", model="claude-opus-4-6", reasoning_effort="high")
    finally:
        agent_store.close()
    # Proves the request's nulls below are a real pin, not an empty fixture.
    assert edited_agent.model == "claude-opus-4-6"
    assert edited_agent.reasoning_effort == "high"

    # A turn on the FORKED session, through the real MessageHandler dispatch path.
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=forked.session_id,
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    asyncio.run(service._execute_task(task, execution_id="exec-1", disable_one_shot=False))

    assert len(dispatched) == 1, "the turn on the forked session never reached the backend"
    backend_name, request = dispatched[0]
    assert isinstance(request, AgentRequest), "the captured request is not the production type"
    assert backend_name == edited_agent.backend
    assert request.vibe_agent_name == "nightly", (
        "precondition: the fork still runs as the source's Agent, only its settings moved"
    )
    assert request.vibe_agent_model is None, (
        f"dispatch handed the backend model={request.vibe_agent_model!r} from the Agent's "
        "CURRENT settings; the forked session pinned none and the fork copied that pin"
    )
    assert request.vibe_agent_reasoning_effort is None, (
        f"dispatch handed the backend reasoning_effort="
        f"{request.vibe_agent_reasoning_effort!r} the forked session never had"
    )

    # ...and the durable record must agree. Read AFTER dispatch on purpose: the
    # turn-start route materialization is what converts a dropped marker into a
    # PERMANENT change (HFR-249), so an unmarked fork does not just mis-route this
    # run -- the Agent's current default becomes the fork's pinned model forever.
    with engine.connect() as conn:
        forked_row = conn.execute(
            select(
                agent_sessions.c.model,
                agent_sessions.c.reasoning_effort,
                agent_sessions.c.metadata_json,
            ).where(agent_sessions.c.id == forked.session_id)
        ).one()
    forked_metadata = json.loads(forked_row.metadata_json or "{}")
    assert set(forked_metadata.get(SESSION_SETTINGS_OVERRIDE_KEY) or ()) == {
        "model",
        "reasoning_effort",
    }, (
        "the fork copied the source's NULL model / reasoning_effort but dropped their "
        f"explicit-override marker (metadata marker={forked_metadata.get(SESSION_SETTINGS_OVERRIDE_KEY)!r}); "
        "the copied nulls now read as 'inherit from the Agent' and the next Agent "
        "default change silently gives the fork settings the source pinned away"
    )
    assert forked_row.model is None, (
        f"the forked session acquired model={forked_row.model!r} from the Agent's CURRENT "
        "settings; the source pinned none and the fork copied that pin"
    )
    assert forked_row.reasoning_effort is None, (
        f"the forked session acquired reasoning_effort={forked_row.reasoning_effort!r} it never had"
    )

    # The inverse half, so "preserve the marker" cannot degenerate into "always
    # preserve it": a fork that SUPPLIES a concrete model owns that setting. Its
    # column is non-NULL, dispatch reads it directly, and a marker entry claiming
    # an explicit pin on a value the fork replaced would be stale on the next edit.
    respecified = reserve_forked_session(
        source_session_id=source_id, model="claude-sonnet-4-9", db_path=db_path
    )
    assert respecified.model == "claude-sonnet-4-9"
    with engine.connect() as conn:
        respecified_metadata = json.loads(
            conn.execute(
                select(agent_sessions.c.metadata_json).where(
                    agent_sessions.c.id == respecified.session_id
                )
            ).scalar_one()
            or "{}"
        )
    marked = set(respecified_metadata.get(SESSION_SETTINGS_OVERRIDE_KEY) or ())
    assert "model" not in marked, (
        f"the fork replaced model with a concrete value but kept it marked explicit "
        f"(marker={sorted(marked)}), so a later edit of that column keeps routing the "
        "value the fork was given"
    )
    # The field the fork still did NOT supply is still copied, so still pinned.
    assert "reasoning_effort" in marked, (
        f"supplying model dropped the untouched reasoning_effort pin too (marker={sorted(marked)})"
    )

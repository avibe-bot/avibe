from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from core.services.session_fork import (
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
from storage import messages_service
from storage.models import agent_runs, agent_sessions, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope


def _seed_source_session(db_path: Path, tmp_path: Path) -> str:
    SQLiteSessionsService(db_path).close()
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
                    agent_backend="codex",
                    agent_variant="codex",
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
                agent_backend="codex",
                agent_variant="codex",
                agent_id="agent-worker",
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
    source_id = _seed_source_session(db_path, tmp_path)
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
    source_id = _seed_source_session(db_path, tmp_path)
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
    source_id = _seed_source_session(db_path, tmp_path)
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
    source_id = _seed_source_session(db_path, tmp_path)
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
    source_id = _seed_source_session(db_path, tmp_path)
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
    source_id = _seed_source_session(db_path, tmp_path)
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
                agent_id="agent-worker",
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
    assert "private_agent_run" not in metadata
    assert "no_delivery" not in metadata
    assert rows[0]["session_anchor"] != rows[1]["session_anchor"]

    resolved = resolve_session_id_target(first_result.session_id, db_path=db_path)
    assert resolved.session_key.to_key() == "slack::channel::C999"
    assert resolved.session_key.thread_id is None
    assert resolved.session_anchor.startswith("slack_C999:fork_")
    assert resolved.suppress_delivery is False


def test_reserve_forked_session_reanchors_explicit_parent_scope(tmp_path: Path) -> None:
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
                agent_id="agent-worker",
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
            for message_type in ("queued", "pending", "draft", "notify"):
                messages_service.append(
                    conn,
                    scope_id=row["scope_id"],
                    session_id=source_id,
                    platform="avibe",
                    author="agent",
                    message_type=message_type,
                    text=message_type,
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
    """A codex/opencode source whose latest turn completed SILENTLY (the invisible
    ``silent`` marker follows its input + activity) is TERMINAL — the fork must not
    trim/roll back the completed turn as if it were still running."""
    db_path = tmp_path / "vibe.sqlite"
    source_id = _seed_source_session(db_path, tmp_path)  # agent_backend='codex'
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = conn.execute(
                select(agent_sessions.c.scope_id).where(agent_sessions.c.id == source_id)
            ).mappings().one()["scope_id"]
            messages_service.append(
                conn, scope_id=scope_id, session_id=source_id, platform="avibe",
                author="user", message_type="user", text="do the thing",
            )
            messages_service.append(
                conn, scope_id=scope_id, session_id=source_id, platform="avibe",
                author="agent", message_type="assistant", text="working",
            )
            messages_service.append(
                conn, scope_id=scope_id, session_id=source_id, platform="avibe",
                author="agent", message_type=messages_service.SILENT_TYPE, text="",
            )
    finally:
        engine.dispose()

    result = reserve_forked_session(source_session_id=source_id, db_path=db_path)
    # A terminal exists after the input anchor → not a running turn → no trim.
    assert result.fork.trim_latest_running_turn is False

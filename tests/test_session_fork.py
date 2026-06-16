from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from core.services.session_fork import (
    SessionForkError,
    fork_metadata_from_session_metadata,
    pending_native_fork_source,
    reserve_forked_session,
)
from core.scheduled_tasks import resolve_session_id_target
from core.vibe_agents import VibeAgentStore
from modules.im import MessageContext
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, scope_settings
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
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == result.session_id)
            ).mappings().one()
    finally:
        engine.dispose()

    assert row["agent_name"] == "reviewer"
    assert row["agent_backend"] == "codex"
    assert row["agent_variant"] == "codex"
    assert row["model"] == "gpt-5.2"
    assert row["reasoning_effort"] == "low"
    assert row["workdir"] == str(tmp_path)
    assert row["native_session_id"] == ""
    assert row["session_anchor"] == result.session_id
    assert row["title"] == "Source"


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

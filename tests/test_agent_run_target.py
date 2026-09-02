from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from core.services import sessions as sessions_service
from avibe_memory.store import derive_project_id
from core.services.agent_run_target import (
    resolve_agent_run_target,
    resolve_default_agent_workdir,
)
from modules.im import MessageContext
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope
from config.v2_settings import make_thread_native_id


def _controller(tmp_path):
    ensure_sqlite_state()
    default_agent = SimpleNamespace(
        id="agent-codex-default",
        name="codex",
        backend="codex",
        model=None,
        reasoning_effort=None,
    )
    return SimpleNamespace(
        sqlite_engine=create_sqlite_engine(),
        primary_platform="slack",
        config=SimpleNamespace(platform="slack", claude=SimpleNamespace(cwd=None), default_backend="codex"),
        agent_router=SimpleNamespace(resolve=lambda _platform, _settings_key: "codex", global_default="codex"),
        resolve_vibe_agent_for_context=lambda _context, required=False: default_agent,
    )


def _seed_scope_settings(
    conn,
    scope_id: str,
    *,
    workdir: str,
    agent_name: str | None = None,
    agent_backend: str | None = None,
    agent_variant: str | None = None,
    routing: dict | None = None,
) -> None:
    conn.execute(
        scope_settings.insert().values(
            scope_id=scope_id,
            enabled=1,
            role=None,
            workdir=workdir,
            agent_name=agent_name,
            agent_backend=agent_backend,
            agent_variant=agent_variant,
            model=None,
            reasoning_effort=None,
            require_mention=None,
            settings_version=1,
            settings_json=json.dumps({"routing": routing}) if routing is not None else "{}",
            created_at="2026-06-04T05:00:00Z",
            updated_at="2026-06-04T05:00:00Z",
        )
    )


def test_workbench_reserved_session_workdir_wins_over_process_cwd(tmp_path, monkeypatch):
    project_workdir = tmp_path / "vibe-remote-project"
    monkeypatch.chdir(tmp_path)
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_vibe_remote",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(project_workdir))
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="codex",
        )

    ctx = MessageContext(
        user_id="workbench",
        channel_id=session["id"],
        platform="avibe",
        platform_specific={
            "agent_session_id": session["id"],
            "agent_session_target": {
                "id": session["id"],
                "workdir": session["workdir"],
                "session_anchor": session["session_anchor"],
            },
        },
    )

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id=session["id"],
    )

    assert target.workdir == str(project_workdir)
    assert target.project_base == str(project_workdir)
    assert target.agent_session_id == session["id"]
    assert ctx.platform_specific["agent_run_target"]["workdir"] == str(project_workdir)
    assert ctx.platform_specific["agent_run_target"]["project_base"] == str(project_workdir)


def test_existing_workbench_session_retains_non_git_project_base(tmp_path):
    project = tmp_path / "project"
    child = project / "packages" / "app"
    child.mkdir(parents=True)
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_non_git",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(project))
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="codex",
        )
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == session["id"])
            .values(
                workdir=str(child),
                metadata_json=json.dumps({"created_via": "workbench"}),
            )
        )
        other_scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_other",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, other_scope_id, workdir=str(tmp_path / "other"))
        sessions_service.update_session(
            conn,
            session["id"],
            scope_id=other_scope_id,
        )
        session = {**session, "workdir": str(child)}

    context = MessageContext(
        user_id="workbench",
        channel_id=session["id"],
        platform="avibe",
        platform_specific={
            "agent_session_id": session["id"],
            "agent_session_target": {
                "id": session["id"],
                "workdir": session["workdir"],
                "session_anchor": session["session_anchor"],
            },
        },
    )

    target = resolve_agent_run_target(
        context,
        controller=controller,
        base_session_id=session["id"],
    )

    assert target.workdir == str(child)
    assert target.project_base == str(project)


def test_legacy_workbench_session_uses_current_scope_as_project_base(tmp_path):
    project = tmp_path / "project"
    child = project / "packages" / "app"
    child.mkdir(parents=True)
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_legacy_non_git",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(project))
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="codex",
        )
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == session["id"])
            .values(
                workdir=str(child),
                metadata_json=json.dumps({"created_via": "workbench"}),
            )
        )

    context = MessageContext(
        user_id="workbench",
        channel_id=session["id"],
        platform="avibe",
        platform_specific={"agent_session_id": session["id"]},
    )

    target = resolve_agent_run_target(
        context,
        controller=controller,
        base_session_id=session["id"],
    )

    assert target.workdir == str(child)
    assert target.project_base == str(project)


def test_im_channel_scope_workdir_creates_session_snapshot(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.scope_id == "slack::channel::C123"
    assert target.workdir == str(workdir)
    assert target.session_anchor == "slack_171717.123"
    assert target.agent_id == "agent-codex-default"
    assert target.agent_name == "codex"
    assert target.agent_backend == "codex"
    assert target.agent_variant == "codex"
    assert target.agent_session_id
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["workdir"] == str(workdir)
    assert session["agent_id"] == "agent-codex-default"
    assert session["agent_name"] == "codex"
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "codex"


def test_telegram_topic_scope_workdir_wins_over_group(tmp_path):
    # Scenario: TELEGRAM-TOPIC-003
    controller = _controller(tmp_path)
    group_workdir = tmp_path / "group"
    topic_workdir = tmp_path / "topic"
    with controller.sqlite_engine.begin() as conn:
        group_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="-1001",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, group_scope_id, workdir=str(group_workdir))
        topic_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="thread",
            native_id=make_thread_native_id("-1001", "42"),
            parent_scope_id=group_scope_id,
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, topic_scope_id, workdir=str(topic_workdir))

    ctx = MessageContext(
        user_id="7",
        channel_id="-1001",
        thread_id="42",
        platform="telegram",
        platform_specific={"is_forum": True, "is_topic_message": True},
    )
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="telegram_topic_42")

    assert target.scope_id == "telegram::thread::-1001/42"
    assert target.workdir == str(topic_workdir)
    assert target.agent_session_id
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["workdir"] == str(topic_workdir)
    assert session["agent_id"] == "agent-codex-default"
    assert session["agent_name"] == "codex"
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "codex"


def test_telegram_topic_override_reuses_parent_scoped_session(tmp_path):
    # Scenario: TELEGRAM-TOPIC-003
    controller = _controller(tmp_path)
    group_workdir = tmp_path / "group"
    topic_workdir = tmp_path / "topic"
    with controller.sqlite_engine.begin() as conn:
        group_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="-1001",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, group_scope_id, workdir=str(group_workdir))
        topic_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="thread",
            native_id=make_thread_native_id("-1001", "42"),
            parent_scope_id=group_scope_id,
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, topic_scope_id, workdir=str(topic_workdir))
        session_id = create_agent_session_row(
            conn,
            scope_id=group_scope_id,
            agent_backend="codex",
            agent_variant="codex",
            session_anchor="telegram_topic_42",
            native_session_id="native-topic-session",
            workdir=str(group_workdir),
        )

    ctx = MessageContext(
        user_id="7",
        channel_id="-1001",
        thread_id="42",
        platform="telegram",
        platform_specific={"is_forum": True, "is_topic_message": True},
    )
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="telegram_topic_42")

    assert target.agent_session_id == session_id
    assert target.scope_id == "telegram::channel::-1001"
    assert target.native_session_id == "native-topic-session"
    assert target.workdir == str(group_workdir)


def test_telegram_topic_override_removal_reuses_topic_scoped_session(tmp_path):
    # Scenario: TELEGRAM-TOPIC-003
    controller = _controller(tmp_path)
    group_workdir = tmp_path / "group"
    topic_workdir = tmp_path / "topic"
    with controller.sqlite_engine.begin() as conn:
        group_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="-1001",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, group_scope_id, workdir=str(group_workdir))
        topic_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="thread",
            native_id=make_thread_native_id("-1001", "42"),
            parent_scope_id=group_scope_id,
            now="2026-06-04T05:00:00Z",
        )
        session_id = create_agent_session_row(
            conn,
            scope_id=topic_scope_id,
            agent_backend="codex",
            agent_variant="codex",
            session_anchor="telegram_topic_42",
            native_session_id="native-topic-session",
            workdir=str(topic_workdir),
        )

    ctx = MessageContext(
        user_id="7",
        channel_id="-1001",
        thread_id="42",
        platform="telegram",
        platform_specific={"is_forum": True, "is_topic_message": True},
    )
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="telegram_topic_42")

    assert target.agent_session_id == session_id
    assert target.scope_id == "telegram::thread::-1001/42"
    assert target.native_session_id == "native-topic-session"
    assert target.workdir == str(topic_workdir)


def test_telegram_general_topic_fallback_anchor_reuses_session(tmp_path):
    # Scenario: TELEGRAM-TOPIC-003
    controller = _controller(tmp_path)
    group_workdir = tmp_path / "group"
    with controller.sqlite_engine.begin() as conn:
        group_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="-1001",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, group_scope_id, workdir=str(group_workdir))

    def general_context(message_id: str) -> MessageContext:
        return MessageContext(
            user_id="7",
            channel_id="-1001",
            message_id=message_id,
            platform="telegram",
            platform_specific={"is_forum": True, "is_topic_message": True},
        )

    first = resolve_agent_run_target(general_context("100"), controller=controller)
    follow_up = resolve_agent_run_target(general_context("101"), controller=controller)

    assert first.session_anchor == "telegram_-1001_1"
    assert follow_up.session_anchor == "telegram_-1001_1"
    assert follow_up.agent_session_id == first.agent_session_id


def test_telegram_topic_migrates_legacy_anchor_without_losing_native_session(tmp_path):
    controller = _controller(tmp_path)
    group_workdir = tmp_path / "group"
    with controller.sqlite_engine.begin() as conn:
        group_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="-1001",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, group_scope_id, workdir=str(group_workdir))
        session_id = create_agent_session_row(
            conn,
            scope_id=group_scope_id,
            agent_backend="codex",
            agent_variant="codex",
            session_anchor="telegram_42",
            native_session_id="native-topic-session",
            workdir=str(group_workdir),
        )

    context = MessageContext(
        user_id="7",
        channel_id="-1001",
        thread_id="42",
        platform="telegram",
        platform_specific={"is_forum": True, "is_topic_message": True},
    )
    target = resolve_agent_run_target(
        context,
        controller=controller,
        base_session_id="telegram_-1001_42",
    )

    assert target.agent_session_id == session_id
    assert target.session_anchor == "telegram_-1001_42"
    assert target.native_session_id == "native-topic-session"
    with controller.sqlite_engine.connect() as conn:
        migrated_anchor = conn.execute(
            select(agent_sessions.c.session_anchor).where(agent_sessions.c.id == session_id)
        ).scalar_one()
    assert migrated_anchor == "telegram_-1001_42"


def test_existing_background_im_target_carries_visibility(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))
        session_id = create_agent_session_row(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_variant="codex",
            session_anchor="slack_171717.123",
            native_session_id="native-codex",
            workdir=str(workdir),
            visibility="background",
        )

    ctx = MessageContext(
        user_id="U1",
        channel_id="C123",
        platform="slack",
        thread_id="171717.123",
    )
    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_session_id == session_id
    assert target.visibility == "background"
    assert ctx.platform_specific["agent_run_target"]["visibility"] == "background"


def test_new_im_session_uses_resolved_vibe_agent(tmp_path):
    workdir = tmp_path / "channel"
    agent = SimpleNamespace(
        id="agent-reviewer",
        name="reviewer",
        backend="codex",
        model="gpt-5.5",
        reasoning_effort="high",
    )
    controller = _controller(tmp_path)
    controller.resolve_vibe_agent_for_context = lambda _context, required=False: agent
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_id == "agent-reviewer"
    assert target.agent_name == "reviewer"
    assert target.agent_backend == "codex"
    assert target.agent_variant == "codex"
    assert target.model == "gpt-5.5"
    assert target.reasoning_effort == "high"
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["agent_id"] == "agent-reviewer"
    assert session["agent_name"] == "reviewer"
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "codex"
    assert session["model"] == "gpt-5.5"
    assert session["reasoning_effort"] == "high"


def test_telegram_dm_new_session_ignores_legacy_channel_scope_session(tmp_path):
    workdir = tmp_path / "telegram-dm"
    claude_agent = SimpleNamespace(
        id="agent-claude",
        name="claude",
        backend="claude",
        model="claude-opus-4-8",
        reasoning_effort=None,
    )
    controller = _controller(tmp_path)
    controller.primary_platform = "telegram"
    controller.config.platform = "telegram"
    controller.resolve_vibe_agent_for_context = (
        lambda _context, override_agent_name=None, required=False: claude_agent
    )
    with controller.sqlite_engine.begin() as conn:
        user_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="user",
            native_id="58181121",
            now="2026-06-19T07:30:00Z",
        )
        _seed_scope_settings(
            conn,
            user_scope_id,
            workdir=str(workdir),
            agent_name="claude",
            routing={"agent_name": "claude", "claude_model": "claude-opus-4-8"},
        )
        legacy_channel_scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="58181121",
            now="2026-06-19T07:30:00Z",
        )
        create_agent_session_row(
            conn,
            scope_id=legacy_channel_scope_id,
            agent_backend="opencode",
            agent_variant="opencode",
            agent_name="opencode",
            session_anchor="telegram_58181121",
            native_session_id="oc-native",
            workdir=str(workdir),
        )

    ctx = MessageContext(
        user_id="58181121",
        channel_id="58181121",
        message_id="100",
        platform="telegram",
        platform_specific={"platform": "telegram", "is_dm": True},
    )

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="telegram_58181121",
    )

    assert target.scope_id == "telegram::user::58181121"
    assert target.session_key == "telegram::user::58181121"
    assert target.agent_backend == "claude"
    assert target.agent_name == "claude"
    assert target.model == "claude-opus-4-8"
    assert target.agent_session_id is not None
    with controller.sqlite_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "select scope_id, agent_backend, session_anchor, metadata_json from agent_sessions order by scope_id"
        ).all()
    assert [(row.scope_id, row.agent_backend, row.session_anchor) for row in rows] == [
        ("telegram::channel::58181121", "opencode", "telegram_58181121"),
        ("telegram::user::58181121", "claude", "telegram_58181121"),
    ]
    assert json.loads(rows[1].metadata_json)["legacy_scope_key"] == "telegram::user::58181121"


def test_new_im_session_ignores_legacy_scope_backend(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(
            conn,
            scope_id,
            workdir=str(workdir),
            agent_backend="opencode",
            agent_variant="reviewer",
        )

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_id == "agent-codex-default"
    assert target.agent_name == "codex"
    assert target.agent_backend == "codex"
    assert target.agent_variant == "codex"
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["agent_id"] == "agent-codex-default"
    assert session["agent_name"] == "codex"
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "codex"


def test_new_im_session_falls_back_to_default_vibe_agent(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    del controller.agent_router
    controller.config = SimpleNamespace(
        platform="slack",
        claude=SimpleNamespace(cwd=None),
        agents=SimpleNamespace(default_backend="claude"),
    )
    default_agent = SimpleNamespace(
        id="agent-codex-default",
        name="codex",
        backend="codex",
        model=None,
        reasoning_effort=None,
    )
    controller.resolve_vibe_agent_for_context = lambda _context, required=False: default_agent
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_backend == "codex"
    assert target.agent_variant == "codex"
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "codex"


def test_new_im_session_without_scope_settings_snapshots_default_cwd(tmp_path):
    default_cwd = tmp_path / "default"
    controller = _controller(tmp_path)
    controller.config.claude.cwd = str(default_cwd)
    with controller.sqlite_engine.begin() as conn:
        upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.scope_id == "slack::channel::C123"
    assert target.workdir == str(default_cwd)
    assert target.agent_session_id
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["workdir"] == str(default_cwd)
    assert default_cwd.is_dir()

    ui_workdir = resolve_default_agent_workdir(
        controller,
        platform="avibe",
        settings_key="memory-ui",
        session_key="memory-ui",
    )
    scope_key = bytes.fromhex("11" * 32)
    assert ui_workdir == target.workdir
    assert derive_project_id(scope_key, ui_workdir) == derive_project_id(
        scope_key,
        target.workdir,
    )


def test_opencode_bind_reuses_scoped_agent_variant_session(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    opencode_agent = SimpleNamespace(
        id="agent-opencode-reviewer",
        name="Code Reviewer",
        backend="opencode",
        model=None,
        reasoning_effort=None,
    )

    default_resolver = controller.resolve_vibe_agent_for_context
    controller.resolve_vibe_agent_for_context = lambda _context, override_agent_name=None, required=False: (
        opencode_agent if override_agent_name == "Code Reviewer" else default_resolver(_context, required=required)
    )
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(
            conn,
            scope_id,
            workdir=str(workdir),
            agent_name="Code Reviewer",
            agent_backend="opencode",
            agent_variant="reviewer",
            routing={"opencode_agent": "reviewer"},
        )

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_session_id is not None
    assert target.agent_backend == "opencode"
    assert target.agent_variant == "reviewer"

    service = SQLiteSessionsService(Path(controller.sqlite_engine.url.database))
    try:
        bound_id = service.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="opencode",
            session_anchor="slack_171717.123",
            native_session_id="oc-native",
            workdir=str(workdir),
        )
    finally:
        service.close()

    assert bound_id == target.agent_session_id
    with controller.sqlite_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "select agent_backend, agent_variant, native_session_id from agent_sessions"
        ).all()
    assert rows == [("opencode", "reviewer", "oc-native")]


def test_new_im_session_uses_scope_agent_variant_column_when_json_missing(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    scoped_agent = SimpleNamespace(
        id="agent-reviewer",
        name="Code Reviewer",
        backend="codex",
        model=None,
        reasoning_effort=None,
    )

    default_resolver = controller.resolve_vibe_agent_for_context
    controller.resolve_vibe_agent_for_context = lambda _context, override_agent_name=None, required=False: (
        scoped_agent if override_agent_name == "Code Reviewer" else default_resolver(_context, required=required)
    )
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(
            conn,
            scope_id,
            workdir=str(workdir),
            agent_name="Code Reviewer",
            agent_backend="codex",
            agent_variant="reviewer-sub",
            routing={},
        )

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_name == "Code Reviewer"
    assert target.agent_backend == "codex"
    assert target.agent_variant == "reviewer-sub"
    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["agent_name"] == "Code Reviewer"
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "reviewer-sub"


def test_readonly_cwd_lookup_does_not_create_session(tmp_path):
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    readonly = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
        create_session=False,
    )

    assert readonly.workdir == str(workdir)
    assert readonly.agent_session_id is None
    with controller.sqlite_engine.connect() as conn:
        count = conn.exec_driver_sql("select count(*) from agent_sessions").scalar_one()
    assert count == 0

    persisted = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )
    assert persisted.agent_session_id
    with controller.sqlite_engine.connect() as conn:
        count = conn.exec_driver_sql("select count(*) from agent_sessions").scalar_one()
    assert count == 1


def test_scope_workdir_is_created_before_return(tmp_path):
    missing = tmp_path / "new-project"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(missing))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.workdir == str(missing)
    assert missing.is_dir()


def test_missing_explicit_session_target_is_rejected(tmp_path):
    controller = _controller(tmp_path)
    ctx = MessageContext(
        user_id="U1",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "agent_session_id": "ses_missing",
            "agent_session_target": {
                "id": "ses_missing",
                "session_anchor": "slack_payload",
                "workdir": str(tmp_path / "payload-project"),
            },
        },
    )

    import pytest

    with pytest.raises(LookupError):
        resolve_agent_run_target(
            ctx,
            controller=controller,
            base_session_id="slack_payload",
        )

def test_uncreatable_scope_workdir_falls_back_to_config_default(tmp_path):
    blocked_parent = tmp_path / "not-a-dir"
    blocked_parent.write_text("blocked", encoding="utf-8")
    default_cwd = tmp_path / "default-cwd"
    controller = _controller(tmp_path)
    controller.config.claude.cwd = str(default_cwd)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(blocked_parent / "child"))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.workdir == str(default_cwd)
    assert default_cwd.is_dir()


def test_existing_im_session_workdir_wins_over_scope_change(tmp_path):
    original_workdir = tmp_path / "original"
    changed_workdir = tmp_path / "changed"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(original_workdir))
    service = SQLiteSessionsService(Path(controller.sqlite_engine.url.database))
    try:
        session_id = service.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_171717.123",
            agent_name="codex",
        )
        assert session_id is not None
        with controller.sqlite_engine.begin() as conn:
            conn.execute(
                scope_settings.update()
                .where(scope_settings.c.scope_id == scope_id)
                .values(workdir=str(changed_workdir))
            )
        service.bind_agent_session_by_id(session_id=session_id, native_session_id="codex-native")
    finally:
        service.close()

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")

    target = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert target.agent_session_id == session_id
    assert target.workdir == str(original_workdir)


def test_existing_session_workdir_does_not_read_anchor_suffix(tmp_path, monkeypatch):
    workdir = tmp_path / "channel"
    deleted_cwd = tmp_path / "deleted-cwd"
    deleted_cwd.mkdir()
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))
    service = SQLiteSessionsService(Path(controller.sqlite_engine.url.database))
    try:
        session_id = service.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_scheduled:legacy-suffix",
            agent_name="codex",
            workdir=str(workdir),
        )
        assert session_id is not None
    finally:
        service.close()

    ctx = MessageContext(
        user_id="U1",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "agent_session_id": session_id,
            "agent_session_target": {
                "id": session_id,
                "session_anchor": "slack_scheduled:legacy-suffix",
            },
        },
    )

    monkeypatch.chdir(deleted_cwd)
    deleted_cwd.rmdir()

    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="slack_scheduled:legacy-suffix")

    assert target.agent_session_id == session_id
    assert target.session_anchor == "slack_scheduled:legacy-suffix"
    assert target.workdir == str(workdir)


def test_existing_session_missing_workdir_is_rejected(tmp_path, monkeypatch):
    deleted_cwd = tmp_path / "deleted-cwd"
    deleted_cwd.mkdir()
    workdir = tmp_path / "scope"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))
        session_id = create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor="slack_171717.123",
            agent_backend="codex",
            agent_variant="codex",
            agent_name="codex",
            workdir=None,
            require_workdir=False,
        )
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == session_id).values(workdir=None))

    ctx = MessageContext(
        user_id="U1",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "agent_session_id": session_id,
            "agent_session_target": {"id": session_id},
        },
    )

    monkeypatch.chdir(deleted_cwd)
    deleted_cwd.rmdir()

    with pytest.raises(RuntimeError, match="missing workdir"):
        resolve_agent_run_target(ctx, controller=controller, base_session_id="slack_171717.123")

    assert not workdir.exists()


def test_new_im_session_bind_snapshots_scope_workdir(tmp_path):
    original_workdir = tmp_path / "original"
    changed_workdir = tmp_path / "changed"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(original_workdir))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")
    first = resolve_agent_run_target(
        ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )
    assert first.workdir == str(original_workdir)

    session_id = first.agent_session_id
    assert session_id is not None

    with controller.sqlite_engine.begin() as conn:
        conn.execute(
            scope_settings.update()
            .where(scope_settings.c.scope_id == scope_id)
            .values(workdir=str(changed_workdir))
        )

    next_ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")
    second = resolve_agent_run_target(
        next_ctx,
        controller=controller,
        base_session_id="slack_171717.123",
    )

    assert second.agent_session_id == session_id
    assert second.workdir == str(original_workdir)


def test_native_bind_does_not_change_session_workdir(tmp_path):
    original_workdir = tmp_path / "original"
    requested_workdir = tmp_path / "requested"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(original_workdir))

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="slack_171717.123")
    assert target.agent_session_id is not None

    service = SQLiteSessionsService(Path(controller.sqlite_engine.url.database))
    try:
        service.bind_agent_session_by_id(
            session_id=target.agent_session_id,
            native_session_id="native-1",
            workdir=str(requested_workdir),
        )
    finally:
        service.close()

    with controller.sqlite_engine.connect() as conn:
        session = sessions_service.get_session(conn, target.agent_session_id)
    assert session["workdir"] == str(original_workdir)


#: ``_row_for_scope_anchor``'s read. It runs TWICE on the create path: once unlocked on the
#: hot path, and again after ``reserve_write_lock`` has taken the write lock. Landing a
#: competing commit after the FIRST one is the whole window that remains (HFR-262), and the
#: reserved re-read is what has to see it.
_ANCHOR_DECISION_SELECT = (
    "SELECT agent_sessions.id, agent_sessions.agent_backend, agent_sessions.agent_variant, "
    "agent_sessions.native_session_id FROM agent_sessions WHERE agent_sessions.scope_id = ? "
    "AND agent_sessions.session_anchor = ? AND agent_sessions.status != ? "
    "ORDER BY agent_sessions.last_active_at DESC, agent_sessions.id DESC LIMIT ? OFFSET ?"
)


def test_resolve_agent_run_target_tolerates_concurrent_row_insert(tmp_path):
    """HFR-053 — IM inbound find-then-create is a race, not a guarantee.

    ``resolve_agent_run_target`` selected and then inserted with no ``IntegrityError``
    catch; SQLite takes no write lock at a SELECT, so a competing writer can win the
    ``(scope_id, session_anchor)`` slot in between. The loser must re-read the winner's row
    instead of surfacing a 500.

    WHERE THE COMPETITOR COMMITS, and why it moved. This used to wrap
    ``create_agent_session_row`` and commit the winner from inside the create call. That
    interleaving no longer exists: HFR-262 made the create path take the write lock BEFORE
    the reads its INSERT decides from, so a competing connection reaching that point is
    refused the lock rather than allowed to commit — and a test that stages an impossible
    interleaving is proving nothing about production. The window that DOES remain is
    earlier and deliberate: ``get_or_create_agent_session_row`` keeps its first anchor read
    unlocked (it is the hot path every message takes), so a winner can still commit between
    that read and the reservation. The competitor is landed exactly there, from a real
    second connection, which is the production race this scenario is about.

    So the loser still has to answer with the winner's row — now by re-deciding under the
    write lock rather than by catching the UNIQUE violation. A create that trusted its
    unlocked read would insert a second row for a thread that already has one, and this
    test fails on it.
    """
    controller = _controller(tmp_path)
    db_path = Path(controller.sqlite_engine.url.database)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C777",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(tmp_path / "wd"))

    competitor: dict[str, str] = {}

    @event.listens_for(controller.sqlite_engine, "after_cursor_execute")
    def _win_the_anchor(conn, cursor, statement, parameters, context, executemany):
        if competitor or " ".join(statement.split()) != _ANCHOR_DECISION_SELECT:
            return
        other = create_sqlite_engine(db_path)
        try:
            with other.begin() as other_conn:
                competitor["id"] = create_agent_session_row(
                    other_conn,
                    scope_id=scope_id,
                    session_anchor="slack_171717.777",
                    agent_backend="codex",
                    workdir=str(tmp_path / "wd"),
                )
        finally:
            other.dispose()

    ctx = MessageContext(user_id="U1", channel_id="C777", platform="slack", thread_id="171717.777")
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="slack_171717.777")

    assert competitor, (
        "the competing insert never landed between the unlocked read and the reservation, "
        "so this test proved nothing — the keyed read is no longer the SQL the code emits"
    )
    assert target.agent_session_id == competitor["id"]
    with controller.sqlite_engine.connect() as conn:
        ids = list(conn.execute(select(agent_sessions.c.id)).scalars().all())
    assert ids == [competitor["id"]], (
        f"agent_sessions holds {ids!r}; the loser must join the winner's row, never insert "
        "a second one for the same thread"
    )


def test_resolve_agent_run_target_tolerates_no_usable_session(tmp_path, monkeypatch):
    """HFR-253, third caller — "no usable session" must not be a 500 on inbound.

    ``get_or_create_agent_session_row`` answers ``(None, created)`` when it resolved
    onto an existing row for this anchor, tried to claim it for this turn's backend,
    and lost that race to a writer that ARCHIVED the row: an archive is terminal, so
    there is no id to hand back. The interleaving that produces it is proven against
    the storage boundary by
    ``tests/test_sqlite_sessions_store.py::test_ensure_agent_session_id_cannot_relabel_a_row_archived_inside_its_window``;
    what is pinned HERE is how this third caller consumes that answer.

    It used to consume it by crashing. The row read after the get-or-create is a
    ``.one()`` keyed on the returned id, so ``id IS NULL`` matched nothing and
    ``NoResultFound`` propagated out of inbound message handling — a user-visible
    failure on an ordinary channel turn, caused by another session being archived.
    The turn must instead run on the UNPERSISTED target, which is the answer this
    function already gives whenever there is no scope to persist against: a resolved
    workdir and Agent route, and no ``agent_session_id``.
    """
    import core.services.agent_run_target as art

    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C778",
            now="2026-07-28T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(tmp_path / "wd"))

    monkeypatch.setattr(art, "get_or_create_agent_session_row", lambda conn, **kwargs: (None, False))

    ctx = MessageContext(user_id="U1", channel_id="C778", platform="slack", thread_id="171717.778")
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="slack_171717.778")

    assert target is not None
    assert target.agent_session_id is None, (
        f"the turn was pointed at {target.agent_session_id!r} after the keyed "
        "get-or-create reported no usable session"
    )
    # The rest of the turn still has everything it needs.
    assert target.workdir == str(tmp_path / "wd")
    assert target.session_anchor == "slack_171717.778"
    assert target.agent_backend == "codex"
    with controller.sqlite_engine.connect() as conn:
        assert (
            conn.execute(
                select(agent_sessions.c.id).where(agent_sessions.c.scope_id == scope_id)
            ).first()
            is None
        ), "the degrade minted a row for the anchor the archive just vacated"


#: The last read the first-turn INSERT in ``get_or_create_agent_session_row`` decides
#: from: ``new_session_id``'s scan of every existing session id.
_SESSION_ID_SCAN_SELECT = "SELECT agent_sessions.id FROM agent_sessions"

#: The two spellings ``reserve_write_lock`` uses to become the writer before that read.
_WRITE_LOCK_RESERVATIONS = ("BEGIN IMMEDIATE", "UPDATE agent_sessions SET id = id WHERE 1 = 0")


def test_resolve_agent_run_target_reserves_the_write_lock_for_its_first_turn_insert(tmp_path):
    """HFR-262 — the third production path into the contested first-turn INSERT.

    ``resolve_agent_run_target`` reaches the same shared get-or-create as
    ``SQLiteSessionsService.ensure_agent_session_id`` / ``bind_agent_session`` (pinned in
    ``tests/test_sqlite_sessions_store.py``), and it gets there after nothing but SELECTs
    -- so before the fix its INSERT decided from a WAL read snapshot no write lock stood
    behind, and a turn arriving on the same brand-new thread could make that snapshot
    stale. SQLite answers that with ``SQLITE_BUSY_SNAPSHOT``, which ``busy_timeout`` never
    retries and which cannot even be IDENTIFIED on Python 3.10 (``sqlite_errorcode`` is
    3.11+), so the fix removes the window rather than detecting the failure.

    One path holding the write lock is not the fix; the INSERT is shared, so this asserts
    the guarantee here too, and asserts it as a fact rather than an argument: a competing
    writer given ``busy_timeout = 0`` must be REFUSED at the last read before the INSERT.
    That observation does not depend on the interpreter version.
    """
    workdir = tmp_path / "channel"
    controller = _controller(tmp_path)
    with controller.sqlite_engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C123",
            now="2026-06-04T05:00:00Z",
        )
        _seed_scope_settings(conn, scope_id, workdir=str(workdir))

    statements: list[str] = []
    race = {"fired": 0, "refused": [], "committed": 0}

    @event.listens_for(controller.sqlite_engine, "after_cursor_execute")
    def _observe(conn, cursor, statement, parameters, context, executemany):
        normalised = " ".join(statement.split())
        statements.append(normalised)
        if race["fired"] or normalised != _SESSION_ID_SCAN_SELECT:
            return
        race["fired"] += 1
        other = create_sqlite_engine()
        try:
            with other.connect() as other_conn:
                # Zero patience instead of the engine's 5s pragma, so "the writer slot is
                # taken" is an immediate refusal rather than a five-second wait.
                other_conn.exec_driver_sql("PRAGMA busy_timeout = 0")
                try:
                    create_agent_session_row(
                        other_conn,
                        scope_id=scope_id,
                        session_id="seswinner001",
                        session_anchor="slack_171717.123",
                        agent_backend="claude",
                        agent_variant="claude",
                        native_session_id="",
                        workdir=str(workdir),
                        now="2026-06-04T05:00:01Z",
                    )
                    other_conn.commit()
                except OperationalError as exc:
                    race["refused"].append(str(exc))
                else:
                    race["committed"] += 1
        finally:
            other.dispose()

    ctx = MessageContext(user_id="U1", channel_id="C123", platform="slack", thread_id="171717.123")
    target = resolve_agent_run_target(ctx, controller=controller, base_session_id="slack_171717.123")

    assert target.agent_session_id, "the first turn produced no session at all"
    assert _SESSION_ID_SCAN_SELECT in statements, (
        "the id scan the INSERT decides from is no longer emitted, so neither check below "
        "means anything"
    )
    scan_at = statements.index(_SESSION_ID_SCAN_SELECT)
    reserved_at = [index for index, sql in enumerate(statements) if sql in _WRITE_LOCK_RESERVATIONS]
    assert reserved_at and min(reserved_at) < scan_at, (
        f"no write-lock reservation precedes the id scan: {statements!r}"
    )
    assert race["fired"] == 1, "the competing insert never ran inside the window"
    assert race["committed"] == 0, (
        f"a competing connection took the write lock while resolve_agent_run_target was "
        f"reading the state its INSERT decides from: {race!r}"
    )
    assert len(race["refused"]) == 1, f"the competing writer was neither refused nor committed: {race!r}"

    with controller.sqlite_engine.connect() as conn:
        ids = list(conn.execute(select(agent_sessions.c.id)).scalars().all())
    assert ids == [target.agent_session_id], (
        f"agent_sessions holds {ids!r}; exactly ONE row may exist for a thread whose first "
        "turn was contested"
    )

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import sessions as sessions_service
from core.services.agent_run_target import resolve_agent_run_target
from modules.im import MessageContext
from storage.agent_session_rows import create_agent_session_row
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, scope_settings
from storage.sessions_service import SQLiteSessionsService
from storage.settings_service import upsert_scope


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
    assert target.agent_session_id == session["id"]
    assert ctx.platform_specific["agent_run_target"]["workdir"] == str(project_workdir)


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

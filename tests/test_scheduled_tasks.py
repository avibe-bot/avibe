from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from core.session_activities import SessionActivityRegistry
from core.scheduled_tasks import (
    ParsedSessionKey,
    ScheduledTaskService,
    ScheduledTaskStore,
    TaskExecutionRequest,
    TaskExecutionStore,
    _agent_run_message_for_request,
    build_session_key_for_context,
    parse_session_key,
    resolve_session_id_target,
    session_anchor_for_target,
)
from modules.im import MessageContext
from storage.db import create_sqlite_engine
from storage.background import SQLiteBackgroundTaskStore
from storage.pagination import PageRequest
from storage.session_activities import SQLiteSessionActivityStore


class _StubScheduler:
    def __init__(self) -> None:
        self.jobs = {}
        self.started = False
        self.shutdown_calls = 0

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = False) -> None:
        self.shutdown_calls += 1

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, trigger, id, replace_existing, coalesce, max_instances, args):
        self.jobs[id] = SimpleNamespace(id=id, trigger=trigger, args=args)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def get_jobs(self):
        return list(self.jobs.values())


def test_parse_session_key_accepts_channel_and_thread() -> None:
    parsed = parse_session_key("slack::channel::C123::thread::171717.123")

    assert parsed.platform == "slack"
    assert parsed.scope_type == "channel"
    assert parsed.scope_id == "C123"
    assert parsed.thread_id == "171717.123"


def test_session_anchor_for_target_uses_scope_until_thread_is_explicit() -> None:
    channel = parse_session_key("slack::channel::C123")
    thread = parse_session_key("slack::channel::C123::thread::171717.123")

    assert session_anchor_for_target(channel) == "slack_C123"
    assert session_anchor_for_target(thread) == "slack_171717.123"


def test_resolve_session_id_target_keeps_scope_anchor_threadless(tmp_path: Path) -> None:
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    target = parse_session_key("slack::channel::C123")
    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_agent_session(
            scope_key=target.session_scope,
            agent_backend="codex",
            session_anchor=session_anchor_for_target(target),
        )
    finally:
        service.close()

    assert session_id is not None
    resolved = resolve_session_id_target(session_id, db_path=db_path)

    assert resolved.session_key.to_key() == "slack::channel::C123"
    assert resolved.session_key.thread_id is None


def test_resolve_session_id_target_preserves_reserved_user_scope(tmp_path: Path) -> None:
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    target = parse_session_key("discord::user::123456789")
    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.reserve_agent_session(
            scope_key=target.session_scope,
            agent_backend="codex",
            session_anchor=session_anchor_for_target(target),
        )
    finally:
        service.close()

    assert session_id is not None
    resolved = resolve_session_id_target(session_id, db_path=db_path)

    assert resolved.session_key.to_key() == "discord::user::123456789"
    assert resolved.session_key.is_dm is True


def test_resolve_session_id_target_accepts_avibe_project_session(tmp_path: Path) -> None:
    """avibe workbench sessions live under ``avibe::project::proj_<hex>``. A
    ``--session-id`` task target must resolve them (the dispatch binds the reply
    to the session via ``agent_session_target``); rejecting the project scope made
    scheduled tasks unusable on the workbench."""
    from storage.db import create_sqlite_engine
    from storage.models import scope_settings
    from storage.sessions_service import SQLiteSessionsService
    from storage.settings_service import upsert_scope
    from storage import workbench_sessions_service

    db_path = tmp_path / "vibe.sqlite"
    # Build + migrate the schema, then seed an avibe project scope + session row.
    SQLiteSessionsService(db_path).close()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn, platform="avibe", scope_type="project", native_id="proj_test", now="2026-05-31T00:00:00Z"
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=1,
                    role=None,
                    workdir=str(tmp_path),
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-05-31T00:00:00Z",
                    updated_at="2026-05-31T00:00:00Z",
                )
            )
            session = workbench_sessions_service.create_session(
                conn, scope_id=scope_id, agent_backend="claude", agent_name="default"
            )
    finally:
        engine.dispose()

    resolved = resolve_session_id_target(session["id"], db_path=db_path)

    assert resolved.session_id == session["id"]
    assert resolved.session_key.platform == "avibe"
    assert resolved.session_key.scope_type == "project"
    assert resolved.session_key.scope_id == "proj_test"
    assert resolved.agent_backend == "claude"


def test_parse_session_key_rejects_invalid_scope_type() -> None:
    try:
        parse_session_key("slack::room::C123")
    except ValueError as exc:
        assert "scope type" in str(exc)
    else:
        raise AssertionError("expected invalid scope type to raise ValueError")


def test_build_session_key_for_context_defaults_to_threadless_scope() -> None:
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform="slack",
        thread_id="171717.123",
        platform_specific={"is_dm": False},
    )

    parsed = build_session_key_for_context(context)

    assert parsed.to_key(include_thread=False) == "slack::channel::C123"
    assert parsed.thread_id is None


def test_build_session_key_for_context_uses_fallback_platform() -> None:
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        thread_id="171717.123",
        platform_specific={"is_dm": False},
    )

    parsed = build_session_key_for_context(context, fallback_platform="slack")

    assert parsed.to_key(include_thread=False) == "slack::channel::C123"


def test_build_session_key_for_context_uses_platform_specific_platform() -> None:
    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        thread_id="171717.123",
        platform_specific={"platform": "telegram", "is_dm": False},
    )

    parsed = build_session_key_for_context(context, fallback_platform="slack")

    assert parsed.to_key(include_thread=False) == "telegram::channel::C123"


def test_scheduled_task_store_uses_sqlite_when_path_is_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ScheduledTaskStore()
    task = store.add_task(
        name="Hourly summary",
        session_key="slack::channel::C123",
        session_id="sesk8m4q2p7x",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )

    reloaded = ScheduledTaskStore()
    saved = reloaded.get_task(task.id)
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")

    assert not (tmp_path / "state" / "scheduled_tasks.json").exists()
    assert saved is not None
    assert saved.session_id == "sesk8m4q2p7x"
    assert sqlite.get_scheduled_task(task.id)["prompt"] == "hello"


def test_sqlite_update_task_persists_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ScheduledTaskStore()
    task = store.add_task(
        name="Hourly summary",
        session_key="slack::channel::C123",
        session_id="sesk8m4q2p7x",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )

    store.update_task(
        task.id,
        name="Morning summary",
        session_key="slack::channel::C456",
        session_id=None,
        prompt="updated",
        schedule_type="cron",
        post_to=None,
        deliver_key=None,
        cron="*/30 * * * *",
        run_at=None,
        timezone_name="Asia/Shanghai",
    )
    reloaded = ScheduledTaskStore()
    saved = reloaded.get_task(task.id)

    assert saved is not None
    assert saved.name == "Morning summary"
    assert saved.session_id is None
    assert saved.session_key == "slack::channel::C456"
    assert saved.prompt == "updated"
    assert saved.cron == "*/30 * * * *"


def test_task_execution_store_uses_sqlite_runs_when_root_is_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = TaskExecutionStore()
    request = store.enqueue_hook_send(
        session_key="slack::channel::C123",
        session_id="sesk8m4q2p7x",
        prompt="hello",
    )

    claimed = store.claim(request.id)
    assert claimed is not None
    store.complete(claimed, ok=True, session_key="slack::channel::C123", session_id="sesk8m4q2p7x")

    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    saved = sqlite.get_run(request.id)
    assert not (tmp_path / "state" / "task_requests").exists()
    assert saved["status"] == "succeeded"
    assert saved["session_id"] == "sesk8m4q2p7x"
    assert saved["session_key"] == "slack::channel::C123"


def test_sqlite_complete_persists_resolved_run_target(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    store = TaskExecutionStore(tmp_path / "task_requests")
    store._sqlite = sqlite
    request = store.enqueue_hook_send(
        session_key="slack::channel::C123",
        session_id=None,
        prompt="hello",
    )

    claimed = store.claim(request.id)
    assert claimed is not None
    store.complete(
        claimed,
        ok=True,
        task_id="task-1",
        session_key="slack::channel::C456",
        session_id="sesk8m4q2p7x",
    )

    saved = sqlite.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "succeeded"
    assert saved["task_id"] == "task-1"
    assert saved["session_key"] == "slack::channel::C456"
    assert saved["session_id"] == "sesk8m4q2p7x"


def test_sqlite_claim_only_claims_pending_runs_once(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    first_store = TaskExecutionStore(tmp_path / "task_requests")
    second_store = TaskExecutionStore(tmp_path / "task_requests-other")
    first_store._sqlite = sqlite
    second_store._sqlite = sqlite
    request = first_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="hello",
    )

    first_claim = first_store.claim(request.id)
    second_claim = second_store.claim(request.id)

    assert first_claim is not None
    assert first_claim.request_type == "hook_send"
    assert second_claim is None
    assert sqlite.get_run(request.id)["status"] == "running"


def test_sqlite_cancel_pending_run_marks_canceled(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    store = TaskExecutionStore(tmp_path / "task_requests")
    store._sqlite = sqlite
    request = store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="hello",
        agent_name="default",
    )

    assert store.cancel_run(request.id) is True

    saved = sqlite.get_run(request.id)
    assert saved["status"] == "canceled"
    assert saved["cancel_requested"] is True
    assert store.claim(request.id) is None


def test_file_backend_cancel_pending_run_marks_canceled(tmp_path: Path) -> None:
    store = TaskExecutionStore(tmp_path / "task_requests")
    request = store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="hello",
        agent_name="default",
    )

    assert store.cancel_run(request.id) is True

    saved = store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["cancel_requested"] is True
    assert [item["id"] for item in store.list_runs(status="canceled")] == [request.id]
    assert not (store.pending_dir / f"{request.id}.json").exists()
    assert (store.completed_dir / f"{request.id}.json").exists()
    assert store.claim(request.id) is None


def test_file_backend_cancel_running_run_sets_cancel_requested(tmp_path: Path) -> None:
    store = TaskExecutionStore(tmp_path / "task_requests")
    request = store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="hello",
        agent_name="default",
    )
    claimed = store.claim(request.id)
    assert claimed is not None

    assert store.cancel_run(request.id) is True

    saved = store.get_run(request.id)
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["cancel_requested"] is True
    assert (store.processing_dir / f"{request.id}.json").exists()


def test_store_round_trip_persists_task(tmp_path: Path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        name="Digest",
        session_key="discord::channel::123",
        post_to="channel",
        deliver_key="discord::channel::456",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )

    reloaded = ScheduledTaskStore(store.path)
    payload = json.loads(store.path.read_text(encoding="utf-8"))

    assert payload["tasks"][0]["id"] == task.id
    assert reloaded.get_task(task.id) is not None
    assert reloaded.get_task(task.id).name == "Digest"
    assert reloaded.get_task(task.id).session_key == "discord::channel::123"
    assert reloaded.get_task(task.id).post_to == "channel"
    assert reloaded.get_task(task.id).deliver_key == "discord::channel::456"


def test_update_task_preserves_id_and_overwrites_selected_fields(tmp_path: Path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="hello",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )

    updated = store.update_task(
        task.id,
        name="Morning summary",
        session_key="slack::channel::C123::thread::171717.123",
        prompt="updated",
        schedule_type="at",
        post_to="channel",
        deliver_key=None,
        cron=None,
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="UTC",
    )

    assert updated.id == task.id
    assert updated.name == "Morning summary"
    assert updated.session_key == "slack::channel::C123::thread::171717.123"
    assert updated.prompt == "updated"
    assert updated.schedule_type == "at"
    assert updated.post_to == "channel"
    assert updated.cron is None
    assert updated.run_at == "2026-03-31T09:00:00+08:00"
    assert updated.timezone == "UTC"


def test_store_reload_detects_deleted_task_file(tmp_path: Path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )

    assert store.list_tasks()
    store.path.unlink()

    assert store.maybe_reload() is True
    assert store.list_tasks() == []


def test_mark_task_result_skips_deleted_task_after_reload(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.json"
    writer = ScheduledTaskStore(path)
    task = writer.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    remover = ScheduledTaskStore(path)
    assert remover.remove_task(task.id) is True

    updated = writer.mark_task_result(task.id, error="boom")
    reloaded = ScheduledTaskStore(path)

    assert updated is False
    assert reloaded.get_task(task.id) is None


def test_sqlite_remove_task_soft_deletes_task_but_keeps_runs(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    task = store.add_task(
        session_key="slack::channel::C123",
        session_id="sesk8m4q2p7x",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    sqlite.enqueue_run(
        {
            "id": "run-1",
            "request_type": "scheduled",
            "status": "succeeded",
            "task_id": task.id,
            "session_id": "sesk8m4q2p7x",
            "created_at": "2026-05-15T00:00:00+00:00",
            "updated_at": "2026-05-15T00:00:00+00:00",
            "completed_at": "2026-05-15T00:01:00+00:00",
        }
    )

    assert store.remove_task(task.id) is True

    reloaded = ScheduledTaskStore(tmp_path / "scheduled_tasks-reloaded.json")
    reloaded._sqlite = sqlite
    reloaded.load()

    assert reloaded.get_task(task.id) is None
    assert sqlite.get_scheduled_task(task.id) is None
    assert sqlite.get_run("run-1")["task_id"] == task.id


def test_store_reload_uses_size_when_mtime_does_not_change(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.json"
    writer = ScheduledTaskStore(path)
    task = writer.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    before = path.stat()

    remover = ScheduledTaskStore(path)
    assert remover.remove_task(task.id) is True

    after = path.stat()
    writer._signature = (after.st_mtime_ns, before.st_size, after.st_ino)

    assert writer.maybe_reload() is True
    assert writer.get_task(task.id) is None


def test_service_rejects_unsupported_platform_at_runtime() -> None:
    controller = SimpleNamespace(platform_settings_managers={"slack": object()})
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))

    try:
        service.validate_platform("foo")
    except ValueError as exc:
        assert "unsupported task platform" in str(exc)
    else:
        raise AssertionError("expected unsupported platform to raise ValueError")


def test_build_context_assigns_unique_scheduled_message_ids() -> None:
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    target = parse_session_key("slack::channel::C123")

    first = asyncio.run(service._build_context(target, execution_id="exec-1", task_id="task-1"))
    second = asyncio.run(service._build_context(target, execution_id="exec-2", task_id="task-1"))

    assert first.message_id.startswith("scheduled:task-1:")
    assert second.message_id.startswith("scheduled:task-1:")
    assert first.message_id != second.message_id


def test_build_context_assigns_hook_message_id() -> None:
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    target = parse_session_key("slack::channel::C123")

    context = asyncio.run(service._build_context(target, execution_id="exec-hook", trigger_kind="hook"))

    assert context.message_id == "hook:exec-hook"
    assert context.platform_specific["task_trigger_kind"] == "hook"


def test_build_context_separates_delivery_target_from_session_target() -> None:
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    session_target = parse_session_key("slack::channel::C123::thread::171717.123")
    delivery_target = parse_session_key("slack::channel::C123")

    context = asyncio.run(
        service._build_context(
            session_target,
            delivery_target=delivery_target,
            execution_id="exec-1",
            task_id="task-1",
        )
    )

    assert context.thread_id == "171717.123"
    assert context.platform_specific["delivery_override"]["thread_id"] is None
    assert context.platform_specific["delivery_scope_session_key"] == "slack::channel::C123"
    assert context.platform_specific["scheduled_delivery_alias"]["mode"] == "sent_message"
    assert context.platform_specific["scheduled_delivery_alias"]["clear_source"] is False


def test_build_context_avibe_keys_on_session_id_not_project() -> None:
    # An avibe project holds many independent sessions. The scheduled context's
    # identity (channel_id) must be the concrete session, not the project scope,
    # so two concurrent runs in the same project don't collide on _get_session_key
    # / consolidated-log grouping and edit each other's visible log message.
    controller = SimpleNamespace(
        platform_settings_managers={},
        im_clients={"avibe": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    target = ParsedSessionKey(platform="avibe", scope_type="project", scope_id="proj_890721e64fc8")

    context = asyncio.run(
        service._build_context(
            target,
            execution_id="exec-1",
            task_id="task-1",
            session_id="ses3chKBjP5hy",
        )
    )

    # Context identity is the session, not the project.
    assert context.channel_id == "ses3chKBjP5hy"
    assert context.platform_specific["agent_session_id"] == "ses3chKBjP5hy"
    # The project scope is still carried for persistence/routing.
    assert context.platform_specific["session_key_external"] == "avibe::project::proj_890721e64fc8"


def test_build_context_carries_pending_native_fork_metadata() -> None:
    controller = SimpleNamespace(
        platform_settings_managers={},
        im_clients={"avibe": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    target = ParsedSessionKey(platform="avibe", scope_type="project", scope_id="proj_890721e64fc8")
    target_info = SimpleNamespace(
        session_id="ses-target",
        agent_id="agent-1",
        agent_name="worker",
        agent_backend="codex",
        agent_variant="codex",
        model="gpt-5",
        reasoning_effort="high",
        native_session_id="",
        workdir="/tmp/work",
        session_anchor="ses-target",
        suppress_delivery=False,
    )

    context = asyncio.run(
        service._build_context(
            target,
            execution_id="exec-1",
            trigger_kind="agent_run",
            session_id="ses-target",
            agent_name="worker",
            target_info=target_info,
            metadata={
                "session_fork": {
                    "source_session_id": "ses-source",
                    "source_native_session_id": "thread-source",
                    "source_backend": "codex",
                }
            },
        )
    )

    session_target = context.platform_specific["agent_session_target"]
    assert session_target["native_session_id"] == ""
    assert session_target["metadata"] == {}
    assert session_target["native_session_fork"] == {
        "source_session_id": "ses-source",
        "source_native_session_id": "thread-source",
        "source_backend": "codex",
    }


def test_build_context_restores_pending_fork_from_session_metadata_when_run_metadata_missing() -> None:
    controller = SimpleNamespace(
        platform_settings_managers={},
        im_clients={"avibe": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    target = ParsedSessionKey(platform="avibe", scope_type="project", scope_id="proj_890721e64fc8")
    target_info = SimpleNamespace(
        session_id="ses-target",
        agent_id="agent-1",
        agent_name="worker",
        agent_backend="codex",
        agent_variant="codex",
        model="gpt-5",
        reasoning_effort="high",
        native_session_id="",
        workdir="/tmp/work",
        session_anchor="ses-target",
        metadata={
            "created_via": "session_fork",
            "fork_source_session_id": "ses-source",
            "fork_source_native_session_id": "thread-source",
            "fork_source_backend": "codex",
        },
        suppress_delivery=False,
    )

    context = asyncio.run(
        service._build_context(
            target,
            execution_id="exec-1",
            trigger_kind="agent_run",
            session_id="ses-target",
            agent_name="worker",
            target_info=target_info,
            metadata={},
        )
    )

    session_target = context.platform_specific["agent_session_target"]
    assert session_target["metadata"]["fork_source_native_session_id"] == "thread-source"
    assert session_target["native_session_fork"] == {
        "source_session_id": "ses-source",
        "source_native_session_id": "thread-source",
        "source_backend": "codex",
    }


def test_build_context_clears_provisional_anchor_for_cross_scope_delivery() -> None:
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    service = ScheduledTaskService(controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")))
    session_target = parse_session_key("slack::channel::C123")
    delivery_target = parse_session_key("slack::channel::C999")

    context = asyncio.run(
        service._build_context(
            session_target,
            delivery_target=delivery_target,
            execution_id="exec-1",
            task_id="task-1",
        )
    )

    assert context.thread_id is None
    assert context.platform_specific["delivery_override"]["channel_id"] == "C999"
    assert context.platform_specific["scheduled_delivery_alias"]["mode"] == "sent_message"
    assert context.platform_specific["scheduled_delivery_alias"]["clear_source"] is True


def test_run_task_records_scheduled_handler_error(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.json"
    store = ScheduledTaskStore(path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        return "scheduled turn failed"

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(controller=controller, store=store)

    asyncio.run(service._run_task(task.id))
    reloaded = ScheduledTaskStore(path)
    updated = reloaded.get_task(task.id)

    assert updated is not None
    assert updated.last_error == "scheduled turn failed"
    assert updated.enabled is False


def test_run_task_stays_queued_until_target_transport_is_ready(tmp_path: Path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="discord::channel::C123",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    controller = SimpleNamespace(
        platform_settings_managers={},
        is_im_transport_ready=lambda _platform: False,
    )
    service = ScheduledTaskService(controller=controller, store=store, request_store=request_store)

    asyncio.run(service._run_task(task.id))
    restarted = ScheduledTaskService(controller=controller, store=store, request_store=request_store)
    asyncio.run(restarted._run_task(task.id))

    pending = request_store.list_pending()
    assert len(pending) == 1
    assert pending[0].task_id == task.id
    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.last_run_at is None
    assert updated.enabled is True


def test_reconcile_jobs_skips_invalid_tasks_and_keeps_valid_jobs(tmp_path: Path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    valid = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="Asia/Shanghai",
    )
    invalid = store.add_task(
        session_key="slack::channel::C123",
        prompt="broken digest",
        schedule_type="cron",
        cron="not-a-cron",
        timezone_name="Asia/Shanghai",
    )
    controller = SimpleNamespace(platform_settings_managers={})
    service = ScheduledTaskService(controller=controller, store=store)
    service.scheduler = _StubScheduler()

    service.reconcile_jobs()

    assert valid.id in service.scheduler.jobs
    assert invalid.id not in service.scheduler.jobs


def test_reconcile_jobs_stops_after_service_lease_loss(tmp_path: Path, monkeypatch) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    controller = SimpleNamespace(platform_settings_managers={"slack": object()}, im_clients={})
    service = ScheduledTaskService(controller=controller, store=store)
    service.scheduler = _StubScheduler()
    service._running = True
    service._requires_service_lease = True
    monkeypatch.setattr("core.scheduled_tasks.runtime.current_process_owns_service_instance", lambda: False)

    service.reconcile_jobs()

    assert task.id not in service.scheduler.jobs
    assert service._running is False
    assert service.scheduler.shutdown_calls == 1


def test_request_store_enqueue_claim_and_complete(tmp_path: Path) -> None:
    store = TaskExecutionStore(tmp_path / "task_requests")

    request = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="hello")
    pending = store.list_pending()
    claimed = store.claim(request.id)

    assert [item.id for item in pending] == [request.id]
    assert claimed is not None
    assert claimed.request_type == "hook_send"

    store.complete(claimed, ok=True, session_key="slack::channel::C123")
    completed_path = store.completed_dir / f"{request.id}.json"
    payload = json.loads(completed_path.read_text(encoding="utf-8"))

    assert payload["ok"] is True
    assert payload["session_key"] == "slack::channel::C123"
    assert not (store.processing_dir / f"{request.id}.json").exists()


def test_request_store_file_backend_reload_detects_queue_changes(tmp_path: Path) -> None:
    root = tmp_path / "task_requests"
    reader = TaskExecutionStore(root)
    writer = TaskExecutionStore(root)

    assert reader.maybe_reload() is False
    request = writer.enqueue_hook_send(session_key="slack::channel::C123", prompt="hello")

    assert reader.maybe_reload() is True
    assert reader.maybe_reload() is False

    assert writer.claim(request.id) is not None

    assert reader.maybe_reload() is True
    assert reader.maybe_reload() is False


def test_request_store_file_backend_filters_public_run_statuses(tmp_path: Path) -> None:
    store = TaskExecutionStore(tmp_path / "task_requests")
    queued = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="queued")
    running = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="running")
    failed = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="failed")
    succeeded = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="succeeded")

    claimed_running = store.claim(running.id)
    claimed_failed = store.claim(failed.id)
    claimed_succeeded = store.claim(succeeded.id)
    assert claimed_running is not None
    assert claimed_failed is not None
    assert claimed_succeeded is not None
    store.complete(claimed_failed, ok=False, error="boom")
    store.complete(claimed_succeeded, ok=True)

    assert [item["id"] for item in store.list_runs(status="queued")] == [queued.id]
    assert [item["id"] for item in store.list_runs(status="running")] == [running.id]
    assert [item["id"] for item in store.list_runs(status="failed")] == [failed.id]
    assert [item["id"] for item in store.list_runs(status="succeeded")] == [succeeded.id]
    assert [item["id"] for item in store.list_runs(status="pending")] == [queued.id]
    assert [item["id"] for item in store.list_runs(status="processing")] == [running.id]
    assert [item["id"] for item in store.list_runs(status="completed")] == [succeeded.id]


def test_sqlite_run_listing_pages_and_filters(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    try:
        for index in range(25):
            sqlite.enqueue_run(
                {
                    "id": f"run-{index:02d}",
                    "request_type": "agent_run" if index % 2 == 0 else "hook_send",
                    "status": "succeeded",
                    "agent_name": "helper" if index % 2 == 0 else "ops",
                    "agent_backend": "codex",
                    "session_id": "ses-alpha" if index < 20 else "ses-beta",
                    "message": f"message {index}",
                    "created_at": f"2026-05-25T00:{index:02d}:00+00:00",
                    "updated_at": f"2026-05-25T00:{index:02d}:00+00:00",
                }
            )

        first_page = sqlite.list_runs_page(page_request=PageRequest(page=1, limit=20))
        second_page = sqlite.list_runs_page(page_request=PageRequest(page=2, limit=20))
        filtered = sqlite.list_runs_page(
            agent_name="helper",
            session_id="ses-beta",
            created_after="2026-05-25T00:20:00+00:00",
            query="message 24",
            page_request=PageRequest(page=1, limit=20),
        )

        assert first_page.has_more is True
        assert [item["id"] for item in first_page.items[:2]] == ["run-24", "run-23"]
        assert second_page.has_more is False
        assert [item["id"] for item in second_page.items] == ["run-04", "run-03", "run-02", "run-01", "run-00"]
        assert [item["id"] for item in filtered.items] == ["run-24"]
    finally:
        sqlite.close()


def test_sqlite_definition_listing_pages_filter_and_count_without_loading_all(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    try:
        for index in range(5):
            sqlite.upsert_scheduled_task(
                {
                    "id": f"task-{index}",
                    "name": f"Nightly task {index}",
                    "prompt": "run it",
                    "schedule_type": "cron",
                    "cron": "0 * * * *",
                    "enabled": index % 2 == 0,
                    "created_at": f"2026-05-25T00:0{index}:00+00:00",
                    "updated_at": f"2026-05-25T00:0{index}:00+00:00",
                }
            )
        for index in range(6):
            sqlite.upsert_watch(
                {
                    "id": f"watch-{index}",
                    "name": f"Deploy watch {index}",
                    "shell_command": f"tail deploy-{index}.log",
                    "enabled": index < 2,
                    "created_at": f"2026-05-25T00:1{index}:00+00:00",
                    "updated_at": f"2026-05-25T00:1{index}:00+00:00",
                }
            )

        enabled_tasks = sqlite.list_scheduled_tasks_page(
            status="enabled",
            page_request=PageRequest(page=1, limit=2),
        )
        disabled_watches = sqlite.list_watches_page(
            status="disabled",
            query="deploy",
            page_request=PageRequest(page=1, limit=3),
        )

        assert [item["id"] for item in enabled_tasks.items] == ["task-4", "task-2"]
        assert enabled_tasks.has_more is True
        assert sqlite.count_scheduled_tasks() == {"all": 5, "enabled": 3, "disabled": 2}
        assert [item["id"] for item in disabled_watches.items] == ["watch-5", "watch-4", "watch-3"]
        assert disabled_watches.has_more is True
        assert sqlite.count_watches(query="deploy") == {"all": 6, "enabled": 2, "disabled": 4}
    finally:
        sqlite.close()


def test_sqlite_run_counts_respect_filters_and_public_status_aliases(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    try:
        for run_id, status, run_type in [
            ("run-queued", "pending", "watch"),
            ("run-running", "processing", "watch"),
            ("run-succeeded", "completed", "watch"),
            ("run-failed", "failed", "scheduled"),
            ("run-other", "completed", "hook_send"),
        ]:
            sqlite.enqueue_run(
                {
                    "id": run_id,
                    "request_type": run_type,
                    "status": status,
                    "message": "deploy status",
                    "created_at": "2026-05-25T00:00:00+00:00",
                    "updated_at": "2026-05-25T00:00:00+00:00",
                }
            )

        assert sqlite.count_runs(status="succeeded", run_type="watch", query="deploy") == 1
        assert sqlite.count_runs_by_status(run_type="watch", query="deploy") == {
            "all": 3,
            "queued": 1,
            "running": 1,
            "succeeded": 1,
            "failed": 0,
            "canceled": 0,
        }
    finally:
        sqlite.close()


def test_sqlite_run_query_filter_treats_like_wildcards_as_literals(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    try:
        for run_id, message in [
            ("run-underscore", "foo_bar"),
            ("run-letter", "fooxbar"),
            ("run-percent", "100% done"),
            ("run-plain", "1000 done"),
        ]:
            sqlite.enqueue_run(
                {
                    "id": run_id,
                    "request_type": "agent_run",
                    "status": "succeeded",
                    "message": message,
                    "created_at": "2026-05-25T00:00:00+00:00",
                    "updated_at": "2026-05-25T00:00:00+00:00",
                }
            )

        underscore = sqlite.list_runs_page(query="foo_", page_request=PageRequest(page=1, limit=20))
        percent = sqlite.list_runs_page(query="100%", page_request=PageRequest(page=1, limit=20))

        assert [item["id"] for item in underscore.items] == ["run-underscore"]
        assert [item["id"] for item in percent.items] == ["run-percent"]
    finally:
        sqlite.close()


def test_runtime_session_reservation_uses_canonicalized_scope_agent(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    monkeypatch.setattr(paths, "get_state_dir", lambda: db_path.parent)
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    agent_store = VibeAgentStore(db_path)
    try:
        default_agent = agent_store.ensure_default_agent(backend="claude")
    finally:
        agent_store.close()

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )

    controller = SimpleNamespace(agent_router=SimpleNamespace(global_default="claude"))
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    session_id = service._reserve_runtime_session(agent_name=None, deliver_key="slack::channel::C123")
    target = resolve_session_id_target(session_id, db_path=db_path)

    assert target.agent_backend == default_agent.backend
    assert target.agent_name == default_agent.name
    assert target.agent_id


def test_runtime_session_reservation_ignores_unresolved_legacy_scope_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    monkeypatch.setattr(paths, "get_state_dir", lambda: db_path.parent)
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    agent_store = VibeAgentStore(db_path)
    try:
        default_agent = agent_store.ensure_default_agent(backend="claude")
        agent_store.create(name="codex", backend="opencode")
    finally:
        agent_store.close()

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    with create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C123", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend="codex",
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({"routing": {"agent_backend": "codex"}}),
                created_at=now,
                updated_at=now,
            )
        )

    controller = SimpleNamespace(agent_router=SimpleNamespace(global_default="claude"))
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    session_id = service._reserve_runtime_session(agent_name=None, deliver_key="slack::channel::C123")
    target = resolve_session_id_target(session_id, db_path=db_path)

    assert target.agent_backend == default_agent.backend
    assert target.agent_name == default_agent.name


def test_runtime_session_reservation_uses_default_agent_without_scope_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    monkeypatch.setattr(paths, "get_state_dir", lambda: db_path.parent)
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.ensure_builtin_default_agents(["opencode", "codex"])
        agent_store.set_default_agent_name("codex")
    finally:
        agent_store.close()

    with create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C456", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({}),
                created_at=now,
                updated_at=now,
            )
        )

    controller = SimpleNamespace(agent_router=SimpleNamespace(global_default="opencode"))
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    session_id = service._reserve_runtime_session(agent_name=None, deliver_key="slack::channel::C456")
    target = resolve_session_id_target(session_id, db_path=db_path)

    assert target.agent_backend == "codex"
    assert target.agent_name == "codex"


def test_runtime_session_reservation_uses_unique_anchors_for_reused_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    monkeypatch.setattr(paths, "get_state_dir", lambda: db_path.parent)
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, scope_settings
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.ensure_builtin_default_agents(["codex"])
        agent_store.set_default_agent_name("codex")
    finally:
        agent_store.close()

    with create_sqlite_engine(db_path).begin() as conn:
        now = "2026-05-22T00:00:00+00:00"
        scope_id = upsert_scope(conn, "slack", "channel", "C789", now=now)
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=None,
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps({}),
                created_at=now,
                updated_at=now,
            )
        )

    controller = SimpleNamespace(agent_router=SimpleNamespace(global_default="codex"))
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    first_session_id = service._reserve_runtime_session(agent_name=None, deliver_key="slack::channel::C789")
    second_session_id = service._reserve_runtime_session(agent_name=None, deliver_key="slack::channel::C789")

    with create_sqlite_engine(db_path).connect() as conn:
        rows = list(
            conn.execute(
                select(agent_sessions.c.id, agent_sessions.c.session_anchor)
                .where(agent_sessions.c.id.in_([first_session_id, second_session_id]))
                .order_by(agent_sessions.c.id)
            ).mappings()
        )

    anchors = {row["session_anchor"] for row in rows}
    assert len(rows) == 2
    assert len(anchors) == 2
    assert all(anchor.startswith("slack_C789:runtime_") for anchor in anchors)
    assert resolve_session_id_target(first_session_id, db_path=db_path).session_key.to_key() == "slack::channel::C789"
    assert resolve_session_id_target(second_session_id, db_path=db_path).session_key.to_key() == "slack::channel::C789"


def test_request_store_constructor_does_not_requeue_processing_files(tmp_path: Path) -> None:
    root = tmp_path / "task_requests"
    store = TaskExecutionStore(root)
    request = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="hello")
    claimed = store.claim(request.id)

    assert claimed is not None
    assert (store.processing_dir / f"{request.id}.json").exists()

    producer_view = TaskExecutionStore(root)

    assert not (producer_view.pending_dir / f"{request.id}.json").exists()
    assert (producer_view.processing_dir / f"{request.id}.json").exists()


def test_request_store_lists_pending_in_created_order(tmp_path: Path) -> None:
    store = TaskExecutionStore(tmp_path / "task_requests")
    first = TaskExecutionRequest(
        id="zzzz",
        request_type="hook_send",
        created_at="2026-03-31T01:00:00+00:00",
        session_key="slack::channel::C123",
        prompt="first",
    )
    second = TaskExecutionRequest(
        id="aaaa",
        request_type="hook_send",
        created_at="2026-03-31T02:00:00+00:00",
        session_key="slack::channel::C123",
        prompt="second",
    )
    store.enqueue(second)
    store.enqueue(first)

    pending = store.list_pending()

    assert [item.id for item in pending] == ["zzzz", "aaaa"]


def test_recover_processing_drops_completed_requests(tmp_path: Path) -> None:
    root = tmp_path / "task_requests"
    store = TaskExecutionStore(root)
    request = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="hello")
    claimed = store.claim(request.id)

    assert claimed is not None
    store.complete(claimed, ok=True, session_key="slack::channel::C123")
    stale_processing = store.processing_dir / f"{request.id}.json"
    stale_processing.write_text(json.dumps(claimed.to_dict(), indent=2), encoding="utf-8")

    store.recover_processing()

    assert (store.completed_dir / f"{request.id}.json").exists()
    assert not stale_processing.exists()
    assert not (store.pending_dir / f"{request.id}.json").exists()


def test_drain_requests_requeues_cancelled_task_run(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.json"
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    store = ScheduledTaskStore(path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    request = request_store.enqueue_task_run(task.id)
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        raise asyncio.CancelledError()

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(controller=controller, store=store, request_store=request_store)

    async def _exercise() -> None:
        # The drain now dispatches concurrently and returns immediately, so
        # the CancelledError surfaces on the spawned execution task rather
        # than out of _drain_requests itself. Awaiting it lets the requeue
        # path (in _execute_claimed_request) run.
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        try:
            await execution
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError on the execution task")

    asyncio.run(_exercise())

    reloaded = ScheduledTaskStore(path)
    updated = reloaded.get_task(task.id)
    assert updated is not None
    assert updated.last_run_at is None
    assert updated.enabled is True
    assert (request_store.pending_dir / f"{request.id}.json").exists()
    assert not (request_store.processing_dir / f"{request.id}.json").exists()
    assert not (request_store.completed_dir / f"{request.id}.json").exists()


def test_service_lease_loss_cancels_inflight_execution(tmp_path: Path, monkeypatch) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    request = request_store.enqueue_hook_send(session_key="slack::channel::C123", prompt="send digest")
    controller = SimpleNamespace(platform_settings_managers={"slack": object()}, im_clients={})
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    service._running = True
    service._requires_service_lease = True
    owner_state = {"owns": True}
    started = asyncio.Event()

    async def fake_execute(claimed):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "core.scheduled_tasks.runtime.current_process_owns_service_instance",
        lambda: owner_state["owns"],
    )
    service._execute_claimed_request = fake_execute  # type: ignore[assignment]

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await started.wait()
        owner_state["owns"] = False
        assert service._owns_service_instance() is False
        with pytest.raises(asyncio.CancelledError):
            await execution

    asyncio.run(_exercise())

    assert service._running is False


def test_run_task_uses_tracked_execution_for_lease_loss(tmp_path: Path, monkeypatch) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    controller = SimpleNamespace(platform_settings_managers={"slack": object()}, im_clients={})
    service = ScheduledTaskService(controller=controller, store=store, request_store=request_store)
    service._running = True
    service._requires_service_lease = True
    owner_state = {"owns": True}
    started = asyncio.Event()

    async def fake_execute(claimed):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "core.scheduled_tasks.runtime.current_process_owns_service_instance",
        lambda: owner_state["owns"],
    )
    service._execute_claimed_request = fake_execute  # type: ignore[assignment]

    async def _exercise() -> None:
        run_task = asyncio.create_task(service._run_task(task.id))
        await started.wait()
        assert len(service._inflight_executions) == 1
        execution = next(iter(service._inflight_executions.values()))
        owner_state["owns"] = False
        assert service._owns_service_instance() is False
        with pytest.raises(asyncio.CancelledError):
            await execution
        with pytest.raises(asyncio.CancelledError):
            await run_task

    asyncio.run(_exercise())

    assert service._running is False


def test_drain_requests_executes_hook_send(tmp_path: Path) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    request = request_store.enqueue_hook_send(
        session_key="slack::channel::C123::thread::171717.123",
        post_to="channel",
        prompt="ship it",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    calls = []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        calls.append((context, message, parsed_session_key))
        return None

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_requests())

    assert len(calls) == 1
    context, message, parsed = calls[0]
    assert message == "ship it"
    assert parsed.to_key() == "slack::channel::C123::thread::171717.123"
    assert context.message_id == f"hook:{request.id}"
    assert context.thread_id == "171717.123"
    assert context.platform_specific["delivery_override"]["thread_id"] is None
    payload = json.loads((request_store.completed_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True


def test_agent_run_stays_running_until_terminal_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="make an image",
        agent_name="codex",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    terminal_event: asyncio.Event | None = None

    class _Controller:
        platform_settings_managers = {"slack": settings_manager}

        def __init__(self) -> None:
            self.active_turn_sinks: dict[str, dict] = {}
            self.message_handler = SimpleNamespace(handle_scheduled_message=self._handle_scheduled_message)

        def get_im_client_for_context(self, _context):
            return SimpleNamespace(
                should_use_thread_for_reply=lambda: True,
                should_use_thread_for_dm_session=lambda: False,
            )

        def _get_session_key(self, context):
            return f"{context.platform}:{context.channel_id}:{context.thread_id or ''}"

        def get_turn_sink(self, session_key):
            return self.active_turn_sinks.get(session_key)

        def register_turn_sink(self, session_key, *, on_chunk, done_event, turn_token=None, context=None):
            self.active_turn_sinks[session_key] = {
                "on_chunk": on_chunk,
                "done_event": done_event,
                "turn_token": turn_token,
            }

        def pop_turn_sink(self, session_key, done_event=None):
            self.active_turn_sinks.pop(session_key, None)

        async def _handle_scheduled_message(self, context, message, parsed_session_key=None):
            async def _finish_later() -> None:
                assert terminal_event is not None
                await terminal_event.wait()
                sink = self.get_turn_sink(self._get_session_key(context))
                assert sink is not None
                store = SQLiteBackgroundTaskStore()
                try:
                    store.record_run_message(
                        request.id,
                        text="final image result",
                        message_id=f"suppressed:{request.id}",
                        terminal_status="succeeded",
                    )
                finally:
                    store.close()
                sink["done_event"].set()

            asyncio.create_task(_finish_later())
            return None

    async def _exercise() -> None:
        nonlocal terminal_event
        terminal_event = asyncio.Event()
        controller = _Controller()
        service = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=request_store,
        )

        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None

        await asyncio.sleep(0.01)
        running = request_store.get_run(request.id)
        assert running is not None
        assert running["status"] == "running"
        assert running.get("completed_at") is None

        terminal_event.set()
        await execution

    asyncio.run(_exercise())

    completed = request_store.get_run(request.id)
    assert completed is not None
    assert completed["status"] == "succeeded"
    assert completed["completed_at"] is not None
    assert completed["result_text"] == "final image result"


def test_agent_run_preserves_failed_terminal_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="make an image",
        agent_name="codex",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))

    class _Controller:
        platform_settings_managers = {"slack": settings_manager}

        def __init__(self) -> None:
            self.active_turn_sinks: dict[str, dict] = {}
            self.message_handler = SimpleNamespace(handle_scheduled_message=self._handle_scheduled_message)

        def get_im_client_for_context(self, _context):
            return SimpleNamespace(
                should_use_thread_for_reply=lambda: True,
                should_use_thread_for_dm_session=lambda: False,
            )

        def _get_session_key(self, context):
            return f"{context.platform}:{context.channel_id}:{context.thread_id or ''}"

        def get_turn_sink(self, session_key):
            return self.active_turn_sinks.get(session_key)

        def register_turn_sink(self, session_key, *, on_chunk, done_event, turn_token=None, context=None):
            self.active_turn_sinks[session_key] = {
                "on_chunk": on_chunk,
                "done_event": done_event,
                "turn_token": turn_token,
            }

        def pop_turn_sink(self, session_key, done_event=None):
            self.active_turn_sinks.pop(session_key, None)

        async def _handle_scheduled_message(self, context, message, parsed_session_key=None):
            sink = self.get_turn_sink(self._get_session_key(context))
            assert sink is not None
            store = SQLiteBackgroundTaskStore()
            try:
                store.record_run_message(
                    request.id,
                    text="terminal failed",
                    message_id=f"suppressed:{request.id}",
                    terminal_status="failed",
                )
            finally:
                store.close()
            sink["done_event"].set()
            return None

    controller = _Controller()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        if execution is not None:
            await execution

    asyncio.run(_exercise())

    completed = request_store.get_run(request.id)
    assert completed is not None
    assert completed["status"] == "failed"
    assert completed["completed_at"] is not None
    assert completed["result_text"] == "terminal failed"


def test_duplicate_recovered_coalesced_agent_run_settles_held_children(tmp_path: Path, monkeypatch) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
            metadata={"workbench_queue_holds_run": True},
        )
        run_ids.append(request.id)

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    request_store.recover_processing()

    async def _submit_scheduled(_sid, _ctx, _text):
        return "duplicate"

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        return None

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_requests())
    stored = {run_id: request_store.get_run(run_id) for run_id in run_ids}

    assert stored[run_ids[0]]["status"] == "succeeded"
    assert stored[run_ids[1]]["status"] == "succeeded"
    assert stored[run_ids[0]]["completed_at"] is not None
    assert stored[run_ids[1]]["completed_at"] is not None


def test_recover_processing_rebuilds_coalesced_metadata_without_queue_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
            metadata={"workbench_queue_holds_run": True},
        )
        run_ids.append(request.id)

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids

    request_store.recover_processing()
    pending = request_store.list_pending()

    assert [request.id for request in pending] == [run_ids[0]]
    assert pending[0].metadata["coalesced_queue"]["execution_ids"] == run_ids
    assert _agent_run_message_for_request(pending[0]) == (
        "coalesced prompt 1\n\n---\n\ncoalesced prompt 2\n\n---\n\ncoalesced prompt 3"
    )


def test_recovered_agent_run_retires_stale_queued_native_rows_before_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
            metadata={"workbench_queue_holds_run": True},
        )
        run_ids.append(request.id)

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    request_store.recover_processing()

    from storage import messages_service
    from storage.models import agent_sessions, messages

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id).limit(1)
        ).mappings().first()
        assert session is not None
        scope_id = session["scope_id"]
        for run_id in run_ids:
            messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author="harness",
                source="harness",
                message_type=messages_service.QUEUED_TYPE,
                text=f"stale queued row for {run_id}",
                native_message_id=f"agent_run:{run_id}",
            )

    submitted: list[str] = []

    async def _submit_scheduled(_sid, ctx, text):
        with engine.connect() as conn:
            if messages_service.native_message_exists(
                conn,
                platform="avibe",
                native_message_id=ctx.message_id,
            ):
                return "duplicate"
        submitted.append(text)
        return "ran"

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        return None

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(run_ids[0])
        if execution is not None:
            await execution

    asyncio.run(_exercise())

    assert len(submitted) == 1
    assert "coalesced prompt 1" in submitted[0]
    assert "coalesced prompt 2" in submitted[0]
    with engine.connect() as conn:
        assert messages_service.list_queued(conn, session_id) == []
        dedupe_rows = conn.execute(
            select(messages.c.native_message_id, messages.c.type)
            .where(messages.c.session_id == session_id)
            .where(messages.c.type == messages_service.HARNESS_DEDUPE_TYPE)
        ).all()
    assert {row.native_message_id for row in dedupe_rows} == {f"agent_run:{run_ids[1]}"}
    stored = {run_id: request_store.get_run(run_id) for run_id in run_ids}
    assert stored[run_ids[0]]["status"] == "running"
    assert stored[run_ids[1]]["status"] == "queued"


def test_recovered_coalesced_agent_run_early_failure_settles_children(tmp_path: Path, monkeypatch) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
            metadata={"workbench_queue_holds_run": True},
        )
        run_ids.append(request.id)

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    request_store.recover_processing()
    claimed = request_store.claim(run_ids[0])
    assert claimed is not None

    async def _submit_scheduled(_sid, _ctx, _text):
        raise AssertionError("the direct execution path should be patched below")

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        return None

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _raise_early(**_kwargs):
        raise RuntimeError("target session vanished")

    service._execute_agent_run = _raise_early

    asyncio.run(service._execute_claimed_request(claimed))
    stored = {run_id: request_store.get_run(run_id) for run_id in run_ids}

    assert stored[run_ids[0]]["status"] == "failed"
    assert stored[run_ids[1]]["status"] == "failed"
    assert stored[run_ids[0]]["completed_at"] is not None
    assert stored[run_ids[1]]["completed_at"] is not None
    assert stored[run_ids[0]]["error"] == "target session vanished"
    assert stored[run_ids[1]]["error"] == "target session vanished"


def test_agent_run_callback_enqueues_only_result_to_caller_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="codex",
        callback_session_id=caller_session_id,
    )
    request_store.complete(request, ok=True, session_id="target-session")
    store = SQLiteBackgroundTaskStore()
    try:
        store.record_run_message(
            request.id,
            text="complete delegated result",
            message_id=f"suppressed:{request.id}",
            terminal_status="succeeded",
        )
    finally:
        store.close()

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        return None

    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=_handle_scheduled_message,
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["callback_status"] == "sent"
    callback_run_id = original["callback_run_id"]
    assert callback_run_id
    callback_run = request_store.get_run(callback_run_id)
    assert callback_run is not None
    assert callback_run["session_id"] == caller_session_id
    assert callback_run["source_kind"] == "callback"
    assert callback_run["parent_run_id"] == request.id
    assert callback_run["message"] == "complete delegated result"


def test_agent_run_forwards_multiple_outputs_and_completes_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="codex",
        callback_session_id=caller_session_id,
    )
    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    first = sqlite_store.record_run_output(
        request.id,
        output_id="output-1",
        text="first callback output",
        sequence=1,
        provenance={"run_id": request.id},
    )
    service.forward_run_outputs([request.id])
    running = request_store.get_run(request.id)

    assert first["recorded"] is True
    assert first["terminal_transition"] is False
    assert running is not None
    assert running["status"] == "running"
    assert running["callback_status"] == "pending"
    assert running["result_payload"]["deferred_terminal_status"] == "succeeded"

    second = sqlite_store.record_run_output(
        request.id,
        output_id="output-2",
        text="second callback output",
        sequence=2,
        provenance={"run_id": request.id},
        terminal_status="succeeded",
    )
    service.forward_run_outputs([request.id])
    terminal = request_store.get_run(request.id)
    assert terminal is not None
    completed_at = terminal["completed_at"]

    duplicate = sqlite_store.record_run_output(
        request.id,
        output_id="output-2",
        text="second callback output",
        sequence=2,
        provenance={"run_id": request.id},
        terminal_status="succeeded",
    )
    service.forward_run_outputs([request.id])
    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert second["recorded"] is True
    assert second["terminal_transition"] is True
    assert duplicate["recorded"] is False
    assert duplicate["terminal_transition"] is False
    assert original["status"] == "succeeded"
    assert original["completed_at"] == completed_at
    assert original["callback_status"] == "sent"
    assert "deferred_terminal_status" not in original["result_payload"]
    assert original["result_text"] == "first callback output\n\nsecond callback output"
    assert [item["id"] for item in original["result_payload"]["outputs"]] == [
        "output-1",
        "output-2",
    ]

    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback" and run.get("parent_run_id") == request.id
    ]
    assert [run["message"] for run in callback_runs] == [
        "first callback output",
        "second callback output",
    ]
    assert {run["metadata"]["callback_output_id"] for run in callback_runs} == {
        "output-1",
        "output-2",
    }


@pytest.mark.parametrize(
    ("terminal_status", "expected_message"),
    [
        ("failed", "Error: backend disconnected"),
        ("canceled", "The run was canceled before producing a result."),
    ],
)
def test_agent_run_forwards_terminal_status_after_partial_output(
    tmp_path: Path,
    monkeypatch,
    terminal_status: str,
    expected_message: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="codex",
        callback_session_id=caller_session_id,
    )
    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    recorded = sqlite_store.record_run_output(
        request.id,
        output_id="output-1",
        text="partial callback output",
        sequence=1,
        provenance={"run_id": request.id},
    )
    assert recorded["recorded"] is True
    service.forward_run_outputs([request.id])
    if terminal_status == "failed":
        request_store.complete(request, ok=False, error="backend disconnected")
    else:
        assert request_store.mark_run_canceled(request.id) is True

    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["status"] == terminal_status
    assert original["callback_status"] == "sent"
    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback" and run.get("parent_run_id") == request.id
    ]
    assert [run["message"] for run in callback_runs] == [
        "partial callback output",
        expected_message,
    ]
    assert callback_runs[1]["source_actor"] == f"{request.id}:terminal:{terminal_status}"
    assert original["callback_run_id"] == callback_runs[1]["id"]


def test_duplicate_terminal_output_does_not_append_result_text_again(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    first = sqlite_store.record_run_output(
        request.id,
        output_id="output-1",
        text="callback output",
    )
    assert first["recorded"] is True
    assert sqlite_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    duplicate = sqlite_store.record_run_output(
        request.id,
        output_id="output-1",
        text="callback output",
        terminal_status="succeeded",
    )

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert duplicate["recorded"] is False
    assert duplicate["terminal_transition"] is True
    assert terminal["result_text"] == "callback output"
    assert [item["id"] for item in terminal["result_payload"]["outputs"]] == [
        "output-1",
    ]
    assert "deferred_terminal_status" not in terminal["result_payload"]


def test_failed_run_records_error_and_enqueues_one_terminal_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="codex",
        callback_session_id=caller_session_id,
    )
    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None

    first = sqlite_store.record_run_output(
        request.id,
        output_id="terminal",
        text="",
        terminal_status="failed",
        error="provider unavailable",
    )
    duplicate = sqlite_store.record_run_output(
        request.id,
        output_id="terminal",
        text="",
        terminal_status="failed",
        error="provider unavailable",
    )
    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert first["terminal_transition"] is True
    assert duplicate["terminal_transition"] is False
    assert original["status"] == "failed"
    assert original["error"] == "provider unavailable"
    assert not original["result_text"]
    assert original["callback_status"] == "sent"
    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback" and run.get("parent_run_id") == request.id
    ]
    assert [run["message"] for run in callback_runs] == ["Error: provider unavailable"]
    assert callback_runs[0]["source_actor"] == f"{request.id}:terminal:failed"


def test_deferred_failure_preserves_error_through_later_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None

    assert sqlite_store.defer_run_terminal(
        request.id,
        terminal_status="failed",
        error="provider unavailable",
    ) is True
    running = request_store.get_run(request.id)
    assert running is not None
    assert running["result_payload"]["deferred_terminal_error"] == "provider unavailable"

    terminal = sqlite_store.record_run_output(
        request.id,
        output_id="activity-output",
        text="background output",
        terminal_status="succeeded",
    )

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert terminal["terminal_transition"] is True
    assert settled["status"] == "failed"
    assert settled["error"] == "provider unavailable"
    assert "deferred_terminal_status" not in settled["result_payload"]
    assert "deferred_terminal_error" not in settled["result_payload"]


@pytest.mark.parametrize(
    ("activity_status", "expected_run_status"),
    [
        ("failed", "failed"),
        ("stopped", "canceled"),
        ("killed", "canceled"),
    ],
)
def test_terminal_owned_activity_settles_deferred_run_once(
    tmp_path: Path,
    monkeypatch,
    activity_status: str,
    expected_run_status: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=lambda *_args, **_kwargs: None,
    )
    registry = SessionActivityRegistry()
    controller.agent_service = SimpleNamespace(activities=registry)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="target-session",
        activity_id="task-failed",
        kind="background_task",
        run_id=request.id,
    )
    activity = registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-failed",
        status=activity_status,
    )
    assert activity is not None

    assert service.settle_activity_runs(activity) == [request.id]
    terminal = request_store.get_run(request.id)
    assert terminal is not None
    completed_at = terminal["completed_at"]
    assert terminal["status"] == expected_run_status
    assert terminal["error"] == f"Background Activity task-failed {activity_status}"
    assert "deferred_terminal_status" not in terminal["result_payload"]

    assert service.settle_activity_runs(activity) == []
    assert request_store.get_run(request.id)["completed_at"] == completed_at


def test_failed_activity_intent_survives_until_last_owned_activity_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=lambda *_args, **_kwargs: None,
    )
    registry = SessionActivityRegistry()
    controller.agent_service = SimpleNamespace(activities=registry)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    assert request_store.claim(request.id) is not None
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True
    for activity_id in ("task-failed", "task-running"):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="target-session",
            activity_id=activity_id,
            kind="background_task",
            run_id=request.id,
        )

    failed = registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-failed",
        status="failed",
    )
    assert failed is not None
    assert service.settle_activity_runs(failed) == []
    running = request_store.get_run(request.id)
    assert running is not None
    assert running["status"] == "running"
    assert running["result_payload"]["deferred_terminal_status"] == "failed"

    completed = registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-running",
        status="completed",
    )
    assert completed is not None
    assert service.settle_activity_runs(completed) == []
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    result = sqlite_store.record_run_output(
        request.id,
        output_id="task-running:completion",
        text="The other task completed",
        terminal_status="succeeded",
    )

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert result["terminal_transition"] is True
    assert terminal["status"] == "failed"
    assert "deferred_terminal_status" not in terminal["result_payload"]


def test_restart_delivers_persisted_activity_summary_and_settles_run_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    first_registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id=session_id,
        activity_id="task-complete",
        kind="background_task",
        run_id=request.id,
    )
    first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-complete",
        status="completed",
        metadata={"summary": "Recovered task result"},
        expects_output=True,
    )
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    recovered_registry = SessionActivityRegistry(activity_store)
    emitted: list[tuple[str, bool, bool]] = []

    async def emit_agent_message(context, message_type, text, *, output, **_kwargs):
        emitted.append((text, output.detached, output.completes_turn))
        result = sqlite_store.record_run_output(
            request.id,
            output_id=str(output.idempotency_key),
            text=text,
            terminal_status="succeeded" if output.settles_run else None,
            provenance=output.provenance(context),
        )
        assert result["terminal_transition"] is True
        return "recovered-message"

    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=lambda *_args, **_kwargs: None,
    )
    controller.agent_service = SimpleNamespace(activities=recovered_registry)
    controller.emit_agent_message = emit_agent_message
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    running = request_store.get_run(request.id)
    assert running is not None
    assert running["status"] == "running"
    asyncio.run(service._drain_recovered_activity_outputs())

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert terminal["status"] == "succeeded"
    assert emitted == [("Recovered task result", True, False)]
    assert activity_store.list_activities() == []

    asyncio.run(service._drain_recovered_activity_outputs())
    assert emitted == [("Recovered task result", True, False)]


def test_recovered_terminal_waits_for_pending_activity_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    for activity_id in ("task-failed", "task-output"):
        first_registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id=session_id,
            activity_id=activity_id,
            kind="background_task",
            run_id=request.id,
        )
    failed = first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-failed",
        status="failed",
        retain_terminal_snapshot=True,
    )
    assert failed is not None
    output = first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-output",
        status="completed",
        metadata={"summary": "Recovered output before failure callback"},
        expects_output=True,
    )
    assert output is not None
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    recovered_registry = SessionActivityRegistry(activity_store)
    statuses_during_delivery: list[str] = []

    async def emit_agent_message(context, _message_type, text, *, output, **_kwargs):
        current = request_store.get_run(request.id)
        assert current is not None
        statuses_during_delivery.append(current["status"])
        result = sqlite_store.record_run_output(
            request.id,
            output_id=str(output.idempotency_key),
            text=text,
            terminal_status="succeeded" if output.settles_run else None,
            provenance=output.provenance(context),
        )
        assert result["terminal_transition"] is True
        return "recovered-message"

    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=lambda *_args, **_kwargs: None,
    )
    controller.agent_service = SimpleNamespace(activities=recovered_registry)
    controller.emit_agent_message = emit_agent_message
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    before_output = request_store.get_run(request.id)
    assert before_output is not None
    assert before_output["status"] == "running"
    assert before_output["result_payload"]["deferred_terminal_status"] == "failed"
    assert len(activity_store.list_activities()) == 2

    asyncio.run(service._drain_recovered_activity_outputs())

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert statuses_during_delivery == ["running"]
    assert terminal["status"] == "failed"
    assert terminal["result_text"] == "Recovered output before failure callback"
    assert activity_store.list_activities() == []


def test_recovered_terminal_settlement_failure_does_not_abort_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    first_registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="target-session",
        activity_id="task-failed",
        kind="background_task",
        run_id=request.id,
    )
    failed = first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-failed",
        status="failed",
        retain_terminal_snapshot=True,
    )
    assert failed is not None

    recovered_registry = SessionActivityRegistry(activity_store)
    original_defer = request_store.defer_run_terminal
    attempts = 0

    def transient_defer_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database is locked")
        return original_defer(*args, **kwargs)

    request_store.defer_run_terminal = transient_defer_failure
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(activities=recovered_registry),
    )

    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    assert attempts == 1
    assert len(service._pending_recovered_activity_terminals) == 1
    assert len(activity_store.list_activities()) == 1

    asyncio.run(service._drain_recovered_activity_outputs())

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert attempts == 2
    assert terminal["status"] == "failed"
    assert terminal["error"] == "Background Activity task-failed failed"
    assert service._pending_recovered_activity_terminals == []
    assert activity_store.list_activities() == []


def test_restart_preserves_activity_delivery_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage.importer import ensure_sqlite_state
    from storage.sessions_service import SQLiteSessionsService

    ensure_sqlite_state()
    session_target = parse_session_key(
        "slack::channel::C-SOURCE::thread::171717.123"
    )
    sessions = SQLiteSessionsService(paths.get_sqlite_state_path())
    try:
        session_id = sessions.reserve_agent_session(
            scope_key=session_target.session_scope,
            agent_backend="claude",
            session_anchor=session_anchor_for_target(session_target),
        )
    finally:
        sessions.close()
    assert session_id is not None

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        session_key=session_target.to_key(),
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    first_registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id=session_id,
        activity_id="task-routed",
        kind="background_task",
        run_id=request.id,
        metadata={"delivery_key_external": "slack::channel::C-DESTINATION"},
    )
    first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-routed",
        status="completed",
        metadata={"summary": "Recovered routed result"},
        expects_output=True,
    )
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    recovered_registry = SessionActivityRegistry(activity_store)
    emitted_contexts: list[MessageContext] = []

    async def emit_agent_message(context, _message_type, text, *, output, **_kwargs):
        emitted_contexts.append(context)
        result = sqlite_store.record_run_output(
            request.id,
            output_id=str(output.idempotency_key),
            text=text,
            terminal_status="succeeded" if output.settles_run else None,
            provenance=output.provenance(context),
        )
        assert result["terminal_transition"] is True
        return "recovered-routed-message"

    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None)
    )
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        im_clients={},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        agent_service=SimpleNamespace(activities=recovered_registry),
        emit_agent_message=emit_agent_message,
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_recovered_activity_outputs())

    assert len(emitted_contexts) == 1
    context = emitted_contexts[0]
    assert context.thread_id == "171717.123"
    assert context.platform_specific["delivery_key_external"] == (
        "slack::channel::C-DESTINATION"
    )
    assert context.platform_specific["delivery_override"]["channel_id"] == (
        "C-DESTINATION"
    )
    assert context.platform_specific["delivery_override"]["thread_id"] is None
    assert request_store.get_run(request.id)["status"] == "succeeded"
    assert activity_store.list_activities() == []


def test_recovered_silent_directive_activity_settles_without_emit() -> None:
    activity = SimpleNamespace(
        id="task-silent",
        session_id="session-1",
        metadata={"summary": "<silent>internal completion</silent>"},
    )
    registry = SimpleNamespace(ack_completed_output=Mock())
    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.controller = SimpleNamespace(
        agent_service=SimpleNamespace(activities=registry),
        emit_agent_message=AsyncMock(),
    )
    service._settle_activity_without_output = Mock()

    asyncio.run(service._deliver_recovered_activity_output(activity))

    service._settle_activity_without_output.assert_called_once_with(activity)
    registry.ack_completed_output.assert_called_once_with(activity)
    service.controller.emit_agent_message.assert_not_awaited()


def test_restart_no_delivery_activity_settles_real_run_without_emit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        metadata={"no_delivery": True},
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    first_registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id=session_id,
        activity_id="task-private",
        kind="background_task",
        run_id=request.id,
    )
    first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-private",
        status="completed",
        metadata={"summary": "Private task result"},
        expects_output=True,
    )
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    recovered_registry = SessionActivityRegistry(activity_store)
    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=lambda *_args, **_kwargs: None,
    )
    controller.agent_service = SimpleNamespace(activities=recovered_registry)
    controller.emit_agent_message = AsyncMock()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_recovered_activity_outputs())

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert terminal["status"] == "succeeded"
    assert terminal["result_text"] in {None, ""}
    assert not terminal["result_payload"].get("outputs")
    controller.emit_agent_message.assert_not_awaited()
    assert activity_store.list_activities() == []


def test_restart_settles_terminal_activity_without_inventing_visible_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    first_registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="target-session",
        activity_id="task-silent",
        kind="background_task",
        run_id=request.id,
    )
    first_registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-silent",
        status="completed",
        expects_output=True,
    )
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
    ) is True

    recovered_registry = SessionActivityRegistry(activity_store)
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(activities=recovered_registry),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_recovered_activity_outputs())

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert terminal["status"] == "succeeded"
    assert terminal["result_text"] in {None, ""}
    assert activity_store.list_activities() == []


def test_restart_marks_live_activity_disconnected_and_cancels_owned_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    first_registry = SessionActivityRegistry(activity_store)
    first_registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="target-session",
        activity_id="task-live",
        kind="background_task",
        run_id=request.id,
    )

    recovered_registry = SessionActivityRegistry(activity_store)
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(activities=recovered_registry),
    )
    ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert terminal["status"] == "canceled"
    assert terminal["error"] == "Background Activity task-live disconnected"
    assert activity_store.list_activities() == []


def test_agent_run_callback_builds_failure_message_without_result_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="codex",
        callback_session_id=caller_session_id,
    )
    request_store.complete(request, ok=False, error="agent crashed", session_id="target-session")
    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["callback_status"] == "sent"
    callback_run = request_store.get_run(original["callback_run_id"])
    assert callback_run is not None
    assert callback_run["message"] == "Error: agent crashed"


def test_agent_run_synchronous_dispatch_error_marks_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="use missing agent",
        agent_name="missing",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))

    class _Controller:
        platform_settings_managers = {"slack": settings_manager}

        def __init__(self) -> None:
            self.active_turn_sinks: dict[str, dict] = {}
            self.message_handler = SimpleNamespace(handle_scheduled_message=self._handle_scheduled_message)

        def get_im_client_for_context(self, _context):
            return SimpleNamespace(
                should_use_thread_for_reply=lambda: True,
                should_use_thread_for_dm_session=lambda: False,
            )

        def _get_session_key(self, context):
            return f"{context.platform}:{context.channel_id}:{context.thread_id or ''}"

        def get_turn_sink(self, session_key):
            return self.active_turn_sinks.get(session_key)

        def register_turn_sink(self, session_key, *, on_chunk, done_event, turn_token=None, context=None):
            self.active_turn_sinks[session_key] = {
                "on_chunk": on_chunk,
                "done_event": done_event,
                "turn_token": turn_token,
            }

        def pop_turn_sink(self, session_key, done_event=None):
            self.active_turn_sinks.pop(session_key, None)

        async def _handle_scheduled_message(self, context, message, parsed_session_key=None):
            sink = self.get_turn_sink(self._get_session_key(context))
            assert sink is not None
            sink["done_event"].set()
            return "agent 'missing' is not available"

    controller = _Controller()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        if execution is not None:
            await execution

    asyncio.run(_exercise())

    completed = request_store.get_run(request.id)
    assert completed is not None
    assert completed["status"] == "failed"
    assert completed["completed_at"] is not None
    assert completed["error"] == "agent 'missing' is not available"


def test_avibe_agent_run_routes_through_gate_without_completing_early(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="run in workbench session",
        agent_name="codex",
    )
    submitted: list[tuple] = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text):
        submitted.append((sid, text, ctx.platform, ctx.platform_specific.get("task_execution_id")))
        return "ran"

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        handler_calls.append(message)
        return None

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await execution

    asyncio.run(_exercise())

    run = request_store.get_run(request.id)
    assert run is not None
    assert run["status"] == "running"
    assert run.get("completed_at") is None
    assert submitted == [(session_id, "run in workbench session", "avibe", request.id)]
    assert handler_calls == []


def test_busy_avibe_agent_run_returns_to_queued_and_is_held_by_workbench_queue(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="run behind active workbench turn",
        agent_name="codex",
    )
    submitted: list[tuple] = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text):
        submitted.append((sid, text, ctx.platform_specific.get("task_execution_id")))
        return "enqueued"

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        handler_calls.append(message)
        return None

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={session_id: object()})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await execution
        await service._drain_requests()

    asyncio.run(_exercise())

    run = request_store.get_run(request.id)
    assert run is not None
    assert run["status"] == "queued"
    assert run.get("started_at") is None
    assert run.get("completed_at") is None
    assert (run.get("metadata") or {}).get("workbench_queue_holds_run") is True
    assert submitted == [(session_id, "run behind active workbench turn", request.id)]
    assert handler_calls == []


def test_busy_avibe_agent_run_requeue_preserves_session_fork_metadata(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="run behind active workbench turn",
        agent_name="codex",
        metadata={
            "session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "thread-source",
                "source_backend": "codex",
            }
        },
    )
    submitted: list[tuple] = []

    async def _submit_scheduled(sid, ctx, text):
        submitted.append((sid, text, ctx.platform_specific["agent_session_target"]["native_session_fork"]))
        return "enqueued"

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        raise AssertionError("busy workbench runs should not dispatch directly")

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={session_id: object()})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await execution

    asyncio.run(_exercise())

    run = request_store.get_run(request.id)
    assert run is not None
    assert run["metadata"]["session_fork"]["source_native_session_id"] == "thread-source"
    assert run["metadata"]["workbench_queue_holds_run"] is True
    assert submitted[0][2]["source_native_session_id"] == "thread-source"


def test_workbench_queue_flush_recovery_preserves_session_fork_metadata(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="recover fork after queue flush",
        agent_name="codex",
        metadata={
            "session_fork": {
                "source_session_id": "ses-source",
                "source_native_session_id": "thread-source",
                "source_backend": "codex",
            },
            "workbench_queue_holds_run": True,
        },
    )

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_run_for_workbench(request.id) is True

    flushed = request_store.get_run(request.id)
    assert flushed is not None
    assert flushed["status"] == "running"
    assert flushed["metadata"]["workbench_queue_holds_run"] is False
    assert flushed["metadata"]["session_fork"]["source_native_session_id"] == "thread-source"

    request_store.recover_processing()
    claimed = request_store.claim(request.id)

    assert claimed is not None
    assert claimed.metadata["workbench_queue_holds_run"] is False
    assert claimed.metadata["session_fork"]["source_native_session_id"] == "thread-source"


def test_recover_processing_keeps_coalesced_agent_run_children_held(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
            metadata={"workbench_queue_holds_run": True},
        )
        run_ids.append(request.id)

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids

    request_store.recover_processing()
    pending = request_store.list_pending()

    assert [request.id for request in pending] == [run_ids[0]]
    primary = request_store.get_run(run_ids[0])
    child = request_store.get_run(run_ids[1])
    assert primary is not None
    assert child is not None
    assert primary["status"] == "queued"
    assert child["status"] == "queued"
    assert primary["metadata"]["workbench_queue_holds_run"] is False
    assert primary["metadata"]["coalesced_queue"]["execution_ids"] == run_ids
    assert primary["metadata"]["coalesced_queue"]["messages"] == [
        {"execution_id": run_ids[0], "message": "coalesced prompt 1"},
        {"execution_id": run_ids[1], "message": "coalesced prompt 2"},
        {"execution_id": run_ids[2], "message": "coalesced prompt 3"},
    ]
    assert child["metadata"]["workbench_queue_holds_run"] is True
    assert child["metadata"]["coalesced_into_run_id"] == run_ids[0]

    claimed = request_store.claim(run_ids[0])
    assert claimed is not None
    recovered_message = _agent_run_message_for_request(claimed)
    assert "coalesced prompt 1" in recovered_message
    assert "coalesced prompt 2" in recovered_message
    assert "coalesced prompt 3" in recovered_message


def test_recovered_coalesced_agent_run_message_filters_cancelled_child(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(3):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
            metadata={"workbench_queue_holds_run": True},
        )
        run_ids.append(request.id)

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    request_store.recover_processing()
    assert sqlite_store.cancel_run(run_ids[1]) is True

    claimed = request_store.claim(run_ids[0])
    assert claimed is not None
    recovered_message = _agent_run_message_for_request(claimed)

    assert "coalesced prompt 1" in recovered_message
    assert "coalesced prompt 2" not in recovered_message
    assert "coalesced prompt 3" in recovered_message


def test_inspect_queued_runs_finalizes_cancel_requested_queued_agent_run(monkeypatch, tmp_path) -> None:
    _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="placeholder",
        message="queued cancel request",
        agent_name="codex",
        metadata={"workbench_queue_holds_run": True},
    )
    bg = request_store._sqlite
    assert bg is not None
    bg.update_run_status(
        request.id,
        status="queued",
        updated_at="2026-06-22T00:00:00Z",
        cancel_requested=True,
        cancel_requested_at="2026-06-22T00:00:01Z",
    )

    queued_run_ids, stale_run_ids = bg.inspect_queued_runs_for_workbench([request.id])

    assert queued_run_ids == []
    assert stale_run_ids == [request.id]
    stored = bg.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["completed_at"] is not None


def test_claim_queued_runs_publishes_after_commit(monkeypatch, tmp_path) -> None:
    import storage.background as background_module

    _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="placeholder",
        message="queued run",
        agent_name="codex",
        metadata={"workbench_queue_holds_run": True},
    )
    bg = request_store._sqlite
    assert bg is not None

    observed_statuses: list[str | None] = []

    def capture_publish(_rows):
        stored = bg.get_run(request.id)
        observed_statuses.append(stored["status"] if stored else None)

    monkeypatch.setattr(background_module, "_publish_run_rows_updated", capture_publish)

    assert bg.claim_queued_runs_for_workbench([request.id]) == [request.id]
    assert observed_statuses == ["running"]


def test_drain_requests_reserves_watch_create_per_run_before_session_validation(tmp_path: Path) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    request = request_store.enqueue_definition_run(
        definition_id="watch-1",
        run_type="watch",
        source_kind="watch",
        session_key="",
        session_id=None,
        post_to=None,
        deliver_key="slack::channel::C123",
        prompt="summarize waiter output",
        agent_name="release-reviewer",
        session_policy="create_per_run",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    calls = []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        calls.append((context, message, parsed_session_key))
        return None

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    service._reserve_runtime_session = lambda **_kwargs: "ses-created"  # type: ignore[method-assign]

    async def _execute_request(**kwargs):
        calls.append(kwargs)
        return None

    service._execute_request = _execute_request  # type: ignore[method-assign]

    asyncio.run(service._drain_requests())

    assert calls == [
        {
            "session_key": "",
            "session_id": "ses-created",
            "post_to": None,
            "deliver_key": "slack::channel::C123",
            "prompt": "summarize waiter output",
            "execution_id": request.id,
            "task_id": "watch-1",
            "trigger_kind": "watch",
            "agent_name": "release-reviewer",
        }
    ]
    payload = json.loads((request_store.completed_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["session_id"] == "ses-created"
    assert payload["session_key"] == ""


def test_drain_requests_records_scheduled_create_per_run_reserved_session(tmp_path: Path) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id=None,
        prompt="daily review",
        schedule_type="cron",
        cron="0 9 * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        agent_name="release-reviewer",
        session_policy="create_per_run",
    )
    request = request_store.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    calls = []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        calls.append((context, message, parsed_session_key))
        return None

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(controller=controller, store=store, request_store=request_store)
    service._reserve_runtime_session = lambda **_kwargs: "ses-created"  # type: ignore[method-assign]

    async def _execute_request(**kwargs):
        calls.append(kwargs)
        return None

    service._execute_request = _execute_request  # type: ignore[method-assign]

    asyncio.run(service._drain_requests())

    assert calls == [
        {
            "session_key": "",
            "session_id": "ses-created",
            "post_to": None,
            "deliver_key": "slack::channel::C123",
            "prompt": "daily review",
            "execution_id": request.id,
            "task_id": task.id,
            "trigger_kind": "scheduled",
            "agent_name": "release-reviewer",
        }
    ]
    payload = json.loads((request_store.completed_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["session_id"] == "ses-created"
    assert payload["session_key"] == ""


def test_drain_requests_agent_run_passes_agent_name(tmp_path: Path) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="review build",
        agent_name="release-reviewer",
    )
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    calls = []

    class _Controller:
        platform_settings_managers = {"slack": settings_manager}

        def __init__(self) -> None:
            self.active_turn_sinks: dict[str, dict] = {}
            self.message_handler = SimpleNamespace(handle_scheduled_message=self._handle_scheduled_message)

        def get_im_client_for_context(self, _context):
            return SimpleNamespace(
                should_use_thread_for_reply=lambda: True,
                should_use_thread_for_dm_session=lambda: False,
            )

        def _get_session_key(self, context):
            return f"{context.platform}:{context.channel_id}:{context.thread_id or ''}"

        def get_turn_sink(self, session_key):
            return self.active_turn_sinks.get(session_key)

        def register_turn_sink(self, session_key, *, on_chunk, done_event, turn_token=None, context=None):
            self.active_turn_sinks[session_key] = {
                "on_chunk": on_chunk,
                "done_event": done_event,
                "turn_token": turn_token,
            }

        def pop_turn_sink(self, session_key, done_event=None):
            self.active_turn_sinks.pop(session_key, None)

        async def _handle_scheduled_message(self, context, message, parsed_session_key=None):
            calls.append((context, message, parsed_session_key))
            sink = self.get_turn_sink(self._get_session_key(context))
            assert sink is not None
            sink["done_event"].set()
            return None

    controller = _Controller()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_requests())

    assert len(calls) == 1
    context, message, parsed = calls[0]
    assert message == "review build"
    assert parsed is None
    assert context.platform == "slack"
    assert context.channel_id == "C123"
    assert context.message_id == f"agent_run:{request.id}"
    assert context.platform_specific["vibe_agent_name"] == "release-reviewer"
    payload = json.loads((request_store.processing_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["request_type"] == "agent_run"


def test_run_task_request_does_not_disable_one_shot(tmp_path: Path) -> None:
    path = tmp_path / "scheduled_tasks.json"
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    store = ScheduledTaskStore(path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    request_store.enqueue_task_run(task.id)
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        return None

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(controller=controller, store=store, request_store=request_store)

    asyncio.run(service._drain_requests())

    reloaded = ScheduledTaskStore(path)
    updated = reloaded.get_task(task.id)
    assert updated is not None
    assert updated.enabled is True
    assert updated.last_run_at is not None


def test_start_keeps_watcher_alive_after_initial_reconcile_failure(tmp_path: Path) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    controller = SimpleNamespace(platform_settings_managers={})
    service = ScheduledTaskService(controller=controller, store=store)
    service.scheduler = _StubScheduler()

    async def _watch_store():
        await asyncio.Event().wait()

    def _fail_once():
        raise ValueError("bad trigger")

    service._watch_store = _watch_store  # type: ignore[method-assign]
    service.reconcile_jobs = _fail_once  # type: ignore[method-assign]

    async def _exercise():
        service.start()
        assert service._running is True
        assert service._reconcile_task is not None
        service._reconcile_task.cancel()
        try:
            await service._reconcile_task
        except asyncio.CancelledError:
            pass
        await service.stop()

    asyncio.run(_exercise())


def test_watch_store_respawns_after_unexpected_cancellation(tmp_path: Path) -> None:
    """A spurious CancelledError must not silently kill the drain loop."""
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    controller = SimpleNamespace(platform_settings_managers={})
    service = ScheduledTaskService(controller=controller, store=store)
    service.scheduler = _StubScheduler()

    started = asyncio.Event()

    async def _watch_store():
        started.set()
        await asyncio.Event().wait()

    service._watch_store = _watch_store  # type: ignore[method-assign]

    async def _exercise():
        service.start()
        first_task = service._reconcile_task
        assert first_task is not None
        await asyncio.wait_for(started.wait(), timeout=1)

        started.clear()
        first_task.cancel()
        for _ in range(50):
            await asyncio.sleep(0)
            if service._reconcile_task is not None and service._reconcile_task is not first_task:
                break
        assert service._reconcile_task is not None
        assert service._reconcile_task is not first_task
        assert service._watch_store_restart_count == 1

        await asyncio.wait_for(started.wait(), timeout=1)
        await service.stop()

    asyncio.run(_exercise())


def test_watch_store_respawns_after_unexpected_exception(tmp_path: Path) -> None:
    """If the watch coroutine crashes with a non-Cancelled exception it must respawn."""
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    controller = SimpleNamespace(platform_settings_managers={})
    service = ScheduledTaskService(controller=controller, store=store)
    service.scheduler = _StubScheduler()

    invocations: list[int] = []

    async def _watch_store():
        invocations.append(1)
        if len(invocations) == 1:
            raise RuntimeError("boom")
        await asyncio.Event().wait()

    service._watch_store = _watch_store  # type: ignore[method-assign]

    async def _exercise():
        service.start()
        for _ in range(50):
            await asyncio.sleep(0)
            if len(invocations) >= 2:
                break
        assert len(invocations) >= 2
        assert service._watch_store_restart_count == 1
        await service.stop()

    asyncio.run(_exercise())


def test_watch_store_does_not_respawn_after_stop(tmp_path: Path) -> None:
    """stop() cancels the task and must not trigger a respawn."""
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    controller = SimpleNamespace(platform_settings_managers={})
    service = ScheduledTaskService(controller=controller, store=store)
    service.scheduler = _StubScheduler()

    async def _watch_store():
        await asyncio.Event().wait()

    service._watch_store = _watch_store  # type: ignore[method-assign]

    async def _exercise():
        service.start()
        first_task = service._reconcile_task
        assert first_task is not None
        await service.stop()
        assert service._reconcile_task is None
        assert service._watch_store_restart_count == 0
        assert first_task.cancelled() or first_task.done()

    asyncio.run(_exercise())


def test_scheduled_task_service_idle_tick_does_not_drain_empty_queues(tmp_path: Path, monkeypatch) -> None:
    original_sleep = asyncio.sleep

    class IdleTaskStore(ScheduledTaskStore):
        def __init__(self, path: Path):
            super().__init__(path)
            self.reloads = 0
            self.list_calls = 0

        def maybe_reload(self) -> bool:
            self.reloads += 1
            return False

        def list_tasks(self):
            self.list_calls += 1
            return super().list_tasks()

    class IdleRequestStore(TaskExecutionStore):
        def __init__(self, root: Path):
            super().__init__(root)
            self.reloads = 0
            self.pending_calls = 0
            self.callback_calls = 0

        def maybe_reload(self) -> bool:
            self.reloads += 1
            return False

        def list_pending(self):
            self.pending_calls += 1
            return super().list_pending()

        def list_pending_callbacks(self, *, limit: int = 20):
            self.callback_calls += 1
            return super().list_pending_callbacks(limit=limit)

    store = IdleTaskStore(tmp_path / "scheduled_tasks.json")
    request_store = IdleRequestStore(tmp_path / "task_requests")
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=request_store,
    )
    service.scheduler = _StubScheduler()
    service._running = True
    service._drain_dirty = False
    ticks = 0

    async def _stop_after_first_sleep(_seconds):
        nonlocal ticks
        ticks += 1
        service._running = False
        await original_sleep(0)

    monkeypatch.setattr("core.scheduled_tasks.asyncio.sleep", _stop_after_first_sleep)

    asyncio.run(service._watch_store())

    assert ticks == 1
    assert store.reloads == 1
    assert request_store.reloads == 1
    assert store.list_calls == 0
    assert request_store.pending_calls == 0
    assert request_store.callback_calls == 0


def test_scheduled_task_service_dirty_tick_drains_without_store_reload(tmp_path: Path, monkeypatch) -> None:
    original_sleep = asyncio.sleep

    class IdleTaskStore(ScheduledTaskStore):
        def maybe_reload(self) -> bool:
            return False

    class IdleRequestStore(TaskExecutionStore):
        def maybe_reload(self) -> bool:
            return False

    store = IdleTaskStore(tmp_path / "scheduled_tasks.json")
    request_store = IdleRequestStore(tmp_path / "task_requests")
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=request_store,
    )
    service.scheduler = _StubScheduler()
    service._running = True
    service._drain_dirty = True
    request_drains = 0
    callback_drains = 0

    async def _drain_requests() -> None:
        nonlocal request_drains
        request_drains += 1

    async def _drain_callbacks() -> None:
        nonlocal callback_drains
        callback_drains += 1

    async def _stop_after_first_sleep(_seconds):
        service._running = False
        await original_sleep(0)

    service._drain_requests = _drain_requests
    service._drain_callbacks = _drain_callbacks
    monkeypatch.setattr("core.scheduled_tasks.asyncio.sleep", _stop_after_first_sleep)

    asyncio.run(service._watch_store())

    assert request_drains == 1
    assert callback_drains == 1
    assert service._drain_dirty is False


def test_scheduled_task_service_rearms_after_skipped_and_failed_callback_batches(
    tmp_path: Path, monkeypatch
) -> None:
    original_sleep = asyncio.sleep

    class IdleTaskStore(ScheduledTaskStore):
        def maybe_reload(self) -> bool:
            return False

    class CallbackRequestStore(TaskExecutionStore):
        def __init__(self, root: Path):
            super().__init__(root)
            self.callback_calls = 0
            self.status_updates: list[tuple[str, str]] = []

        def maybe_reload(self) -> bool:
            return False

        def list_pending_callbacks(self, *, limit: int = 20):
            self.callback_calls += 1
            if self.callback_calls == 1:
                return [{"id": "run-1", "callback_session_id": "ses-callback"}]
            if self.callback_calls == 2:
                return [{"id": "run-2", "callback_session_id": "ses-callback"}]
            return []

        def update_callback_status(
            self,
            run_id: str,
            *,
            status: str,
            error: str | None = None,
            callback_run_id: str | None = None,
        ) -> None:
            self.status_updates.append((run_id, status))

    store = IdleTaskStore(tmp_path / "scheduled_tasks.json")
    request_store = CallbackRequestStore(tmp_path / "task_requests")
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=request_store,
    )
    service.scheduler = _StubScheduler()
    service._running = True
    service._drain_dirty = True
    ticks = 0

    async def _drain_requests() -> None:
        return None

    def _enqueue_callback_run(run: dict):
        if run["id"] == "run-1":
            return None
        raise RuntimeError("callback boom")

    async def _stop_after_two_sleeps(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks >= 2:
            service._running = False
        await original_sleep(0)

    service._drain_requests = _drain_requests
    service._enqueue_callback_run = _enqueue_callback_run
    monkeypatch.setattr("core.scheduled_tasks.asyncio.sleep", _stop_after_two_sleeps)

    asyncio.run(service._watch_store())

    assert request_store.status_updates == [("run-1", "skipped"), ("run-2", "failed")]
    assert request_store.callback_calls == 2
    assert service._drain_dirty is True


def test_drain_does_not_block_on_hung_execution(tmp_path: Path) -> None:
    """A turn that never returns must not stall delivery of other sessions.

    Regression for watch follow-up runs piling up in ``queued`` after one
    execution hung: the drain loop used to await each execution inline.
    """

    async def _exercise() -> None:
        store = TaskExecutionStore(tmp_path / "reqs")
        hung = store.enqueue_hook_send(session_key="slack::channel::A", prompt="hangs")
        fast = store.enqueue_hook_send(session_key="slack::channel::B", prompt="fast")

        controller = SimpleNamespace(platform_settings_managers={})
        service = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=store,
        )

        started: list[str] = []
        never = asyncio.Event()

        async def fake_execute(request):
            started.append(request.id)
            if request.id == hung.id:
                await never.wait()  # simulate an agent turn that never returns
                return
            service.request_store.complete(request, ok=True)

        service._execute_claimed_request = fake_execute  # type: ignore[assignment]

        # Should return promptly even though one execution hangs forever.
        await asyncio.wait_for(service._drain_requests(), timeout=1.0)
        # Let the fast execution finish.
        await asyncio.sleep(0.05)

        assert hung.id in started and fast.id in started
        # Fast session delivered despite the hung one still in flight.
        assert [item["id"] for item in store.list_runs(status="succeeded")] == [fast.id]
        assert hung.id in service._inflight_executions
        assert "key:slack::channel::A" in service._inflight_sessions
        assert "key:slack::channel::B" not in service._inflight_sessions

        # Cleanup: release the hung task.
        never.set()
        hung_task = service._inflight_executions.get(hung.id)
        if hung_task is not None:
            await hung_task

    asyncio.run(_exercise())


def test_drain_defers_im_runs_until_transport_ready_without_blocking_workbench(tmp_path: Path) -> None:
    async def _exercise() -> None:
        store = TaskExecutionStore(tmp_path / "reqs")
        workbench = store.enqueue_hook_send(session_key="avibe::project::proj_test", prompt="local")
        discord = store.enqueue_hook_send(session_key="discord::channel::C123", prompt="remote")
        ready_platforms = {"avibe"}
        controller = SimpleNamespace(
            platform_settings_managers={},
            is_im_transport_ready=lambda platform: platform in ready_platforms,
        )
        service = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=store,
        )
        started: list[str] = []

        async def fake_execute(request):
            started.append(request.id)
            service.request_store.complete(request, ok=True)

        service._execute_claimed_request = fake_execute  # type: ignore[assignment]

        await service._drain_requests()
        await asyncio.sleep(0)

        assert started == [workbench.id]
        assert [item.id for item in store.list_pending()] == [discord.id]

        ready_platforms.add("discord")
        service.notify_transport_ready("discord")
        assert service._drain_dirty is True
        await service._drain_requests()
        await asyncio.sleep(0)

        assert started == [workbench.id, discord.id]
        assert store.list_pending() == []

    asyncio.run(_exercise())


def test_drain_serializes_executions_per_session(tmp_path: Path) -> None:
    """Two requests for the same session never run concurrently; the second
    stays queued until the first finishes."""

    async def _exercise() -> None:
        store = TaskExecutionStore(tmp_path / "reqs")
        first = store.enqueue_hook_send(session_key="slack::channel::A", prompt="first")
        second = store.enqueue_hook_send(session_key="slack::channel::A", prompt="second")

        controller = SimpleNamespace(platform_settings_managers={})
        service = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=store,
        )

        started: list[str] = []
        gate = asyncio.Event()

        async def fake_execute(request):
            started.append(request.id)
            await gate.wait()
            service.request_store.complete(request, ok=True)

        service._execute_claimed_request = fake_execute  # type: ignore[assignment]

        await asyncio.wait_for(service._drain_requests(), timeout=1.0)
        await asyncio.sleep(0.05)

        # Only the first claimed; the second stays queued behind the same session.
        assert started == [first.id]
        assert [item["id"] for item in store.list_runs(status="queued")] == [second.id]

        # Release the first; a second drain now picks up the queued one.
        gate.set()
        first_task = service._inflight_executions.get(first.id)
        if first_task is not None:
            await first_task
        await asyncio.wait_for(service._drain_requests(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert started == [first.id, second.id]
        second_task = service._inflight_executions.get(second.id)
        if second_task is not None:
            await second_task

    asyncio.run(_exercise())


def test_drain_serializes_session_id_against_matching_session_key(tmp_path: Path, monkeypatch) -> None:
    """A session_id-only run must serialize against a key-only run for the
    same conversation: the session id is resolved to its canonical key before
    gating (otherwise the disjoint identifiers would run concurrently)."""

    from core.scheduled_tasks import ParsedSessionKey

    def fake_resolve(session_id, *, db_path=None):
        # Both runs resolve to the same canonical session key.
        return SimpleNamespace(
            session_key=ParsedSessionKey(platform="slack", scope_type="channel", scope_id="C123")
        )

    monkeypatch.setattr("core.scheduled_tasks.resolve_session_id_target", fake_resolve)

    async def _exercise() -> None:
        store = TaskExecutionStore(tmp_path / "reqs")
        by_id = store.enqueue_hook_send(session_key="", session_id="sesX", prompt="id only")
        by_key = store.enqueue_hook_send(session_key="slack::channel::C123", prompt="key only")

        controller = SimpleNamespace(platform_settings_managers={})
        service = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
            request_store=store,
        )

        started: list[str] = []
        gate = asyncio.Event()

        async def fake_execute(request):
            started.append(request.id)
            await gate.wait()
            service.request_store.complete(request, ok=True)

        service._execute_claimed_request = fake_execute  # type: ignore[assignment]

        await asyncio.wait_for(service._drain_requests(), timeout=1.0)
        await asyncio.sleep(0.05)

        # session_id run resolves to slack::channel::C123 — same as the key-only
        # run — so the second is held behind the shared canonical key.
        assert started == [by_id.id]
        assert [item["id"] for item in store.list_runs(status="queued")] == [by_key.id]

        gate.set()
        for run_id in (by_id.id, by_key.id):
            task = service._inflight_executions.get(run_id)
            if task is not None:
                await task

    asyncio.run(_exercise())


def test_drain_serializes_task_only_scheduled_runs(tmp_path: Path) -> None:
    """Scheduled runs that carry only a task_id resolve their target off the
    task definition before gating, so two runs for the same task/session do
    not run concurrently."""

    async def _exercise() -> None:
        task_store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
        task = task_store.add_task(
            session_key="slack::channel::D",
            prompt="digest",
            schedule_type="cron",
            cron="0 * * * *",
            timezone_name="UTC",
        )
        store = TaskExecutionStore(tmp_path / "reqs")
        # Task-only requests: no session_id/session_key, just the task_id.
        first = store.enqueue_task_run(task.id)
        second = store.enqueue_task_run(task.id)

        controller = SimpleNamespace(platform_settings_managers={})
        service = ScheduledTaskService(controller=controller, store=task_store, request_store=store)

        started: list[str] = []
        gate = asyncio.Event()

        async def fake_execute(request):
            started.append(request.id)
            await gate.wait()
            service.request_store.complete(request, ok=True)

        service._execute_claimed_request = fake_execute  # type: ignore[assignment]

        await asyncio.wait_for(service._drain_requests(), timeout=1.0)
        await asyncio.sleep(0.05)

        assert started == [first.id]
        assert [item["id"] for item in store.list_runs(status="queued")] == [second.id]

        gate.set()
        for run_id in (first.id, second.id):
            t = service._inflight_executions.get(run_id)
            if t is not None:
                await t

    asyncio.run(_exercise())


# ---------------------------------------------------------------------
# avibe scheduled runs route through the per-session turn gate
# ---------------------------------------------------------------------


def _avibe_controller_double(*, gate, handle_scheduled_message):
    """A controller double sufficient for ``_execute_request`` → ``_build_context``
    on an avibe target: a virtual ``avibe`` IM client (so ``validate_platform``
    passes) plus the thread-policy hooks ``_build_context`` consults."""
    return SimpleNamespace(
        platform_settings_managers={},
        im_clients={"avibe": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        session_turn_gate=gate,
        message_handler=SimpleNamespace(handle_scheduled_message=handle_scheduled_message),
    )


def _make_avibe_session(
    monkeypatch,
    tmp_path,
    *,
    metadata: dict | None = None,
) -> str:
    """Create a real avibe workbench session so ``resolve_session_id_target``
    resolves it to ``platform='avibe'`` (the gate trigger)."""
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn, platform="avibe", scope_type="project", native_id="proj_gate_exec", now="2026-05-31T00:00:00Z"
        )
        conn.execute(
            scope_settings.insert().values(
                scope_id=scope_id,
                enabled=1,
                role=None,
                workdir=str(tmp_path),
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json="{}",
                created_at="2026-05-31T00:00:00Z",
                updated_at="2026-05-31T00:00:00Z",
            )
        )
        session = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="claude",
            agent_name="worker",
            metadata=metadata,
        )
    return session["id"]


def test_execute_request_avibe_routes_through_gate(monkeypatch, tmp_path) -> None:
    """An avibe scheduled run is dispatched via ``session_turn_gate.submit_scheduled``
    (so it queues behind an active Chat turn + gets the turn lifecycle) and does
    NOT call ``handle_scheduled_message`` directly. It returns ``None`` so the
    caller's ``ok = not error`` stays true — the run's own outcome surfaces via
    the outbound terminal result + sidebar dot."""
    session_id = _make_avibe_session(monkeypatch, tmp_path)

    submitted: list[tuple] = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text):
        submitted.append((sid, text, getattr(ctx, "platform", None)))

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        handler_calls.append(message)
        return "should not be called"

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(gate=gate, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    )

    error = asyncio.run(
        service._execute_request(
            session_key=None,
            post_to=None,
            deliver_key=None,
            prompt="run the digest",
            execution_id="exec-gate-1",
            trigger_kind="scheduled",
            session_id=session_id,
        )
    )

    assert error is None, "dispatched-success returns None so ok=not error stays true"
    assert submitted == [(session_id, "run the digest", "avibe")], "routed through the turn gate"
    assert handler_calls == [], "the direct handle_scheduled_message path is bypassed for avibe"


def test_execute_request_im_bypasses_gate(monkeypatch, tmp_path) -> None:
    """An IM (slack/discord/...) scheduled run NEVER touches the gate — it keeps
    the direct ``handle_scheduled_message`` path byte-for-byte, even when a gate
    is present on the controller."""
    submitted: list = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text):
        submitted.append((sid, text))

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        handler_calls.append((message, context.platform))
        return None

    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_k: None))
    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        session_turn_gate=gate,
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(
        controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    )

    error = asyncio.run(
        service._execute_request(
            session_key="slack::channel::C123",
            post_to=None,
            deliver_key=None,
            prompt="send digest",
            execution_id="exec-im-1",
            trigger_kind="scheduled",
        )
    )

    assert error is None
    assert submitted == [], "IM runs must never reach the turn gate"
    assert handler_calls == [("send digest", "slack")], "IM keeps the direct scheduled path"


def test_execute_request_avibe_falls_back_when_no_gate(monkeypatch, tmp_path) -> None:
    """When the internal server hasn't published the gate yet
    (``session_turn_gate is None``), an avibe scheduled run falls back to the
    direct ``handle_scheduled_message`` path instead of crashing."""
    session_id = _make_avibe_session(monkeypatch, tmp_path)

    handler_calls: list = []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        handler_calls.append((message, context.platform))
        return None

    controller = _avibe_controller_double(gate=None, handle_scheduled_message=_handle_scheduled_message)
    service = ScheduledTaskService(
        controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    )

    error = asyncio.run(
        service._execute_request(
            session_key=None,
            post_to=None,
            deliver_key=None,
            prompt="run the digest",
            execution_id="exec-gate-fallback",
            trigger_kind="scheduled",
            session_id=session_id,
        )
    )

    assert error is None
    assert handler_calls == [("run the digest", "avibe")], "no gate → direct scheduled path"

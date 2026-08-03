from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.scheduled_tasks as scheduled_tasks
from config import paths
from config.v2_settings import make_thread_native_id
from core.controller import Controller
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_mirror import mirror_harness_inbound
from core.message_output import MessageOutput, stop_output_for
from core.run_settlement import (
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
    SETTLED_BY_TURN_ONLY_RESULT,
)
from core.services.dispatch import SOURCE_SCHEDULED, TurnDispatchOutcome
from core.session_activities import SessionActivityRegistry
from core.session_turns import SessionTurnManager
from core.scheduled_tasks import (
    BINDING_FOLLOWS_SESSION_METADATA_KEY,
    BINDING_RECOVERY_METADATA_KEY,
    ParsedSessionKey,
    ScheduledTaskService,
    ScheduledTaskStore,
    SessionBindingChange,
    TaskDispatchResult,
    TaskExecutionRequest,
    TaskExecutionStore,
    _TASK_RESULT_NOT_RECORDED_ERROR,
    _agent_run_message_for_request,
    build_session_key_for_context,
    normalize_agent_run_delivery_intent,
    parse_session_key,
    resolve_session_id_target,
    session_anchor_for_target,
)
from modules.im import MessageContext
from storage import message_deliveries
from storage.db import create_sqlite_engine
from storage.background import SQLiteBackgroundTaskStore
from storage.models import (
    agent_events,
    agent_runs,
    agent_sessions,
    messages,
    run_definitions,
)
from storage.pagination import PageRequest
from storage.session_activities import SQLiteSessionActivityStore
from storage.agent_session_rows import create_agent_session_row
from storage.settings_service import upsert_scope


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


def test_session_anchor_for_telegram_topic_includes_chat_id() -> None:
    first = parse_session_key("telegram::channel::-1001::thread::42")
    second = parse_session_key("telegram::channel::-1002::thread::42")

    assert session_anchor_for_target(first) == "telegram_-1001_42"
    assert session_anchor_for_target(second) == "telegram_-1002_42"


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


def test_superseding_a_bound_row_keeps_its_thread_routing(tmp_path: Path) -> None:
    """HFR-057. Superseding must not silently unroute a bound session's definitions.

    When another backend claims an anchor whose row already has a native id, the
    row is kept and its anchor moved to ``superseded:<id>``. Pinned tasks and
    watches stay attached to that row, but ``resolve_session_id_target`` derives
    the thread solely from ``session_anchor`` -- and ``superseded:<id>`` splits to
    the base ``superseded``, which matches no platform prefix. The thread is lost,
    so ``--post-to thread`` definitions deliver to the channel root forever. The
    row still exists, so the unresolvable-binding recovery never fires either.
    """
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    target = parse_session_key("telegram::channel::-1001::thread::42")
    scope_key = target.session_scope
    session_anchor = session_anchor_for_target(target)

    service = SQLiteSessionsService(db_path)
    try:
        session_id = service.bind_agent_session(
            scope_key=scope_key,
            agent_name="codex",
            session_anchor=session_anchor,
            native_session_id="codex-native-1",
        )
    finally:
        service.close()
    assert session_id is not None

    before = resolve_session_id_target(session_id, db_path=db_path)
    assert before.session_key.thread_id == "42", "precondition: thread routing resolves"

    # Another backend claims the same anchor; the bound row is superseded.
    service = SQLiteSessionsService(db_path)
    try:
        other = service.ensure_agent_session_id(
            scope_key=scope_key,
            agent_name="claude",
            session_anchor=session_anchor,
        )
    finally:
        service.close()
    assert other != session_id, "precondition: a fresh row took the freed anchor"

    after = resolve_session_id_target(session_id, db_path=db_path)
    assert after.session_key.thread_id == "42", (
        "superseded row lost its thread routing: definitions pinned to it now "
        f"deliver to the channel root (thread_id={after.session_key.thread_id!r})"
    )


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


@pytest.mark.parametrize("anchor", ["telegram_-1001_42", "telegram_42"])
def test_resolve_session_id_target_maps_telegram_topic_scope_to_delivery_key(
    tmp_path: Path,
    anchor: str,
) -> None:
    from storage.sessions_service import SQLiteSessionsService

    db_path = tmp_path / "vibe.sqlite"
    SQLiteSessionsService(db_path).close()
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            group_scope_id = upsert_scope(
                conn,
                platform="telegram",
                scope_type="channel",
                native_id="-1001",
                now="2026-05-31T00:00:00Z",
            )
            topic_scope_id = upsert_scope(
                conn,
                platform="telegram",
                scope_type="thread",
                native_id=make_thread_native_id("-1001", "42"),
                parent_scope_id=group_scope_id,
                now="2026-05-31T00:00:00Z",
            )
            session_id = create_agent_session_row(
                conn,
                scope_id=topic_scope_id,
                agent_backend="codex",
                agent_variant="codex",
                session_anchor=anchor,
                native_session_id="native-topic-session",
                workdir=str(tmp_path),
            )
    finally:
        engine.dispose()

    resolved = resolve_session_id_target(session_id, db_path=db_path)

    assert resolved.scope_id == topic_scope_id
    assert resolved.session_key.to_key() == "telegram::channel::-1001::thread::42"


def test_build_session_key_for_general_topic_uses_canonical_thread_id() -> None:
    context = MessageContext(
        user_id="7",
        channel_id="-1001",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True},
    )

    parsed = build_session_key_for_context(context, include_thread=True)

    assert parsed.to_key() == "telegram::channel::-1001::thread::1"


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


def test_telegram_delivery_alias_includes_chat_id() -> None:
    service = ScheduledTaskService(
        controller=SimpleNamespace(),
        store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json")),
    )
    session_target = parse_session_key("telegram::channel::-100123::thread::42")
    delivery_target = parse_session_key("telegram::channel::-100456::thread::42")

    strategy = service._build_delivery_alias_strategy(
        session_target=session_target,
        delivery_target=delivery_target,
        session_context={"channel_id": "-100123"},
        delivery_context={"channel_id": "-100456"},
    )

    assert strategy["mode"] == "fixed_base"
    assert strategy["base_session_id"] == "telegram_-100456_42"


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


def test_request_store_file_backend_reload_detects_queue_changes(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "task_requests"
    state_dirs = {root / "pending", root / "processing", root / "completed"}
    path_signature = scheduled_tasks._path_signature

    def fixed_directory_signature(path: Path):
        if path in state_dirs:
            return (1, 1, 1)
        return path_signature(path)

    monkeypatch.setattr(scheduled_tasks, "_path_signature", fixed_directory_signature)
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

        waiting_tasks = sqlite.list_scheduled_tasks_page(
            status="waiting",
            page_request=PageRequest(page=1, limit=2),
        )
        paused_watches = sqlite.list_watches_page(
            status="paused",
            query="deploy",
            page_request=PageRequest(page=1, limit=3),
        )

        assert [item["id"] for item in waiting_tasks.items] == ["task-4", "task-2"]
        assert waiting_tasks.has_more is True
        # Nothing here has ever run, so nothing is finished: a switched-off cron
        # task and a never-started watch are both someone having paused them.
        assert sqlite.count_scheduled_tasks() == {
            "total": 5,
            "running": 0,
            "waiting": 3,
            "paused": 2,
            "finished": 0,
        }
        assert [item["id"] for item in paused_watches.items] == ["watch-5", "watch-4", "watch-3"]
        assert paused_watches.has_more is True
        assert sqlite.count_watches(query="deploy") == {
            "total": 6,
            "running": 0,
            "waiting": 2,
            "paused": 4,
            "finished": 0,
        }
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

    session_id = service._reserve_runtime_session(
        agent_name=None,
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    target = resolve_session_id_target(session_id, db_path=db_path)

    assert target.agent_backend == default_agent.backend
    assert target.agent_name == default_agent.name
    assert target.agent_id


def test_runtime_session_reservation_without_scope_creates_background_standalone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AVIBE_HOME", str(home))

    from core.vibe_agents import VibeAgentStore
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions

    agent_store = VibeAgentStore()
    try:
        agent_store.ensure_builtin_default_agents(["codex"])
        agent_store.set_default_agent_name("codex")
    finally:
        agent_store.close()

    service = ScheduledTaskService(
        controller=SimpleNamespace(agent_router=SimpleNamespace(global_default="codex")),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    session_id = service._reserve_runtime_session(agent_name=None, deliver_key=None)

    with create_sqlite_engine().connect() as conn:
        row = conn.execute(
            select(
                agent_sessions.c.scope_id,
                agent_sessions.c.visibility,
                agent_sessions.c.workdir,
            ).where(agent_sessions.c.id == session_id)
        ).one()
    expected_workdir = home / "show" / session_id
    assert row.scope_id is None
    assert row.visibility == "background"
    assert row.workdir == str(expected_workdir)
    assert expected_workdir.is_dir()


def test_runtime_session_reservation_preserves_legacy_deliver_key_scope(
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
    assert target.scope_id == "slack::channel::C123"
    assert target.visibility == "background"


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

    session_id = service._reserve_runtime_session(
        agent_name=None,
        deliver_key="slack::channel::C456",
        metadata={"session_scope_id": "slack::channel::C456"},
    )
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

    reservation = {
        "agent_name": None,
        "deliver_key": "slack::channel::C789",
        "metadata": {"session_scope_id": "slack::channel::C789"},
    }
    first_session_id = service._reserve_runtime_session(**reservation)
    second_session_id = service._reserve_runtime_session(**reservation)

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
    """HFR-003: cancellation requeues the claim and releases its Session slot."""
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
        await asyncio.sleep(0)
        assert request.id not in service._inflight_executions
        assert "key:slack::channel::C123" not in service._inflight_sessions

    asyncio.run(_exercise())

    reloaded = ScheduledTaskStore(path)
    updated = reloaded.get_task(task.id)
    assert updated is not None
    assert updated.last_run_at is None
    assert updated.enabled is True
    assert (request_store.pending_dir / f"{request.id}.json").exists()
    assert not (request_store.processing_dir / f"{request.id}.json").exists()
    assert not (request_store.completed_dir / f"{request.id}.json").exists()


def test_restart_recovers_running_row_and_preserves_same_session_fifo(monkeypatch, tmp_path) -> None:
    """HFR-004: a crash-held row restarts queued and each successor runs once."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    first = request_store.enqueue_agent_run(
        session_id="ses-restart",
        session_key="avibe::project::proj-restart",
        message="first",
        agent_name="codex",
    )
    second = request_store.enqueue_agent_run(
        session_id="ses-restart",
        session_key="avibe::project::proj-restart",
        message="second",
        agent_name="codex",
    )
    assert request_store.claim(first.id) is not None
    assert request_store.get_run(first.id)["status"] == "running"

    restarted_store = TaskExecutionStore()
    controller = SimpleNamespace(platform_settings_managers={})
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=restarted_store,
    )
    assert [request.id for request in restarted_store.list_pending()] == [first.id, second.id]

    async def _exercise() -> None:
        started: list[str] = []
        release_first = asyncio.Event()

        async def _execute(request):
            started.append(request.id)
            if request.id == first.id:
                await release_first.wait()
            restarted_store.complete(request, ok=True)

        service._execute_claimed_request = _execute  # type: ignore[method-assign]
        await service._drain_requests()
        await asyncio.sleep(0)
        assert started == [first.id]
        assert restarted_store.get_run(second.id)["status"] == "queued"

        release_first.set()
        first_task = service._inflight_executions[first.id]
        await first_task
        await asyncio.sleep(0)
        await service._drain_requests()
        second_task = service._inflight_executions[second.id]
        await second_task
        assert started == [first.id, second.id]

    asyncio.run(_exercise())
    assert restarted_store.get_run(first.id)["status"] == "succeeded"
    assert restarted_store.get_run(second.id)["status"] == "succeeded"


def test_restart_recovers_delivery_owned_run_without_unrelated_run_arbitration(
    monkeypatch,
    tmp_path,
) -> None:
    """Only the Delivery FIFO can block recovery; an unlinked Run cannot."""

    from core.session_turns import (
        SCHEDULED_PROVENANCE_KEY,
        SessionTurnManager,
        capture_scheduled_provenance,
    )
    from storage.background import attach_agent_run_delivery_in_connection
    from storage.models import agent_sessions

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    older = request_store.enqueue_agent_run(
        session_id=session_id,
        message="older recovered owner",
        agent_name="codex",
    )
    successor = request_store.enqueue_agent_run(
        session_id=session_id,
        message="persisted successor",
        agent_name="codex",
    )
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None

    queued_context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{successor.id}",
        platform_specific={
            "task_execution_id": successor.id,
            "task_trigger_kind": "agent_run",
            "vibe_agent_name": "codex",
            "source_kind": "cli",
        },
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            author="harness",
            source="harness",
            message_type="harness",
            text="persisted successor",
            metadata={
                SCHEDULED_PROVENANCE_KEY: capture_scheduled_provenance(
                    queued_context
                )
            },
            native_message_id=f"agent_run:{successor.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            successor.id,
            session_id=session_id,
            delivery_id=str(delivery["id"]),
        )

    class _Controller:
        def __init__(self) -> None:
            self.session_turns = SessionTurnManager(
                self,
                build_context=self._build_context,
            )
            self.statuses: list[tuple[str, str]] = []

        @staticmethod
        def _build_context(target_session_id):
            assert target_session_id == session_id
            return MessageContext(
                user_id="scheduled",
                channel_id=session_id,
                platform="avibe",
                platform_specific={
                    "agent_session_id": session_id,
                    "agent_session_target": {"agent_backend": "codex"},
                },
            )

        def set_agent_status(self, target_session_id, status) -> None:
            self.statuses.append((target_session_id, status))

    controller = _Controller()
    started: list[str] = []

    async def _dispatch(_controller, context, _text, **_kwargs):
        run_id = str((context.platform_specific or {}).get("task_execution_id") or "")
        started.append(run_id)
        sqlite_store.record_run_output(
            run_id,
            output_id="terminal",
            text="recovered once",
            terminal_status="succeeded",
        )
        # The honest path: a real terminal result, so the manager leaves the row to
        # the out-of-band writer above instead of settling it itself.
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TERMINAL_RESULT)

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", _dispatch)

    async def _exercise() -> None:
        assert await controller.session_turns.recover_persisted_agent_run_queue(
            session_id
        ) == [session_id]
        for _ in range(100):
            if session_id not in controller.session_turns.in_flight:
                break
            await asyncio.sleep(0.005)

        assert started == [successor.id]
        assert request_store.get_run(older.id)["status"] == "queued"
        assert request_store.get_run(successor.id)["status"] == "succeeded"
        with engine.connect() as conn:
            assert message_deliveries.list_queued(conn, session_id) == []

        assert await controller.session_turns.recover_persisted_agent_run_queue(
            session_id
        ) == []
        assert started == [successor.id]

    asyncio.run(_exercise())
    engine.dispose()


def test_restart_does_not_auto_send_pure_user_queue(monkeypatch, tmp_path) -> None:
    """HFR-004 safety: startup recovery is limited to durable Agent Run rows."""

    from core.session_turns import SessionTurnManager
    from storage import messages_service
    from storage.models import agent_sessions

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="kept after explicit stop",
        )

    controller = SimpleNamespace()
    manager = SessionTurnManager(
        controller,
        build_context=lambda _session_id: MessageContext(
            user_id="workbench",
            channel_id=session_id,
            platform="avibe",
        ),
    )
    controller.session_turns = manager

    assert asyncio.run(manager.recover_persisted_agent_run_queue()) == []
    with engine.connect() as conn:
        assert [row["text"] for row in message_deliveries.list_queued(conn, session_id)] == [
            "kept after explicit stop"
        ]
    engine.dispose()


def test_restart_does_not_flush_user_queue_ahead_of_held_agent_run(
    monkeypatch,
    tmp_path,
) -> None:
    """HFR-004 safety: recovery cannot bypass a stopped user-owned queue head."""

    from core.session_turns import (
        SCHEDULED_PROVENANCE_KEY,
        SessionTurnManager,
        capture_scheduled_provenance,
    )
    from storage import messages_service
    from storage.models import agent_sessions

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    successor = request_store.enqueue_agent_run(
        session_id=session_id,
        message="held successor",
        agent_name="codex",
        metadata={"workbench_queue_holds_run": True},
    )
    queued_context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{successor.id}",
        platform_specific={
            "task_execution_id": successor.id,
            "task_trigger_kind": "agent_run",
            "vibe_agent_name": "codex",
            "source_kind": "cli",
        },
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            text="kept after explicit stop",
        )
        message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            author="harness",
            source="harness",
            message_type="harness",
            text="held successor",
            metadata={
                SCHEDULED_PROVENANCE_KEY: capture_scheduled_provenance(
                    queued_context
                )
            },
            native_message_id=f"agent_run:{successor.id}",
        )

    controller = SimpleNamespace()
    manager = SessionTurnManager(
        controller,
        build_context=lambda _session_id: MessageContext(
            user_id="workbench",
            channel_id=session_id,
            platform="avibe",
        ),
    )
    controller.session_turns = manager

    assert asyncio.run(manager.recover_persisted_agent_run_queue()) == []
    assert session_id not in manager.in_flight
    assert request_store.get_run(successor.id)["status"] == "queued"
    with engine.connect() as conn:
        assert [
            row["text"]
            for row in message_deliveries.list_queued(conn, session_id)
        ] == ["kept after explicit stop", "held successor"]
    engine.dispose()


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


class _SettlementControllerDouble:
    """Controller double for the Gap-A settlement cases.

    Same surface as the ``terminal_result`` doubles above, but the fake turn
    releases the sink the way a turn that never produced a result does. The session
    key is a constant so a test can pre-register a live sink for it and drive the
    concurrent-turn refusal deterministically.
    """

    SESSION_KEY = "settlement-session"

    def __init__(self, *, on_turn=None) -> None:
        settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *a, **k: None))
        self.platform_settings_managers = {"slack": settings_manager}
        self.active_turn_sinks: dict[str, dict] = {}
        self._on_turn = on_turn
        self.turns: list[str] = []
        self.message_handler = SimpleNamespace(handle_scheduled_message=self._handle_scheduled_message)

    def _t(self, key: str, **_kwargs) -> str:
        return key

    def get_im_client_for_context(self, _context):
        return SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        )

    def _get_session_key(self, _context):
        return self.SESSION_KEY

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
        self.turns.append(message)
        if self._on_turn is not None:
            await self._on_turn(self, context, message)
        # Release the waiter WITHOUT recording a terminal result — exactly what
        # ``Controller.mark_turn_complete`` does for a turn that never dispatched.
        sink = self.get_turn_sink(self._get_session_key(context))
        assert sink is not None
        sink["done_event"].set()
        return None


def _run_single_request(service: ScheduledTaskService, request_id: str) -> None:
    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request_id)
        if execution is not None:
            await execution

    asyncio.run(_exercise())


def test_agent_run_settles_failed_when_sink_released_without_terminal_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-009: a released waiter with no terminal result must not strand the run.

    Nothing else will ever write this row — the out-of-band terminal writer only
    runs on a real backend result — so leaving it ``running`` is the zombie.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )
    controller = _SettlementControllerDouble()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "failed"
    assert settled["completed_at"] is not None
    assert settled["error"]
    assert settled["metadata"]["interrupt_reason"] == "no_terminal_result"


def test_drain_lane_leaves_a_run_whose_turn_released_without_claiming_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-032: the drain lane must allow-list the settlements, not exclude one value.

    A released waiter usually means "no result is coming", but not always: the Claude
    Activity delivery-failure path closes its origin turn with ``turn_only_result``
    while the requeued Activity keeps the run. Testing "anything but
    ``terminal_result`` is a zombie" settled that live run ``failed`` and fired its
    terminal callback before the retry ran, so the lane tests membership in
    ``SETTLEMENTS_WITHOUT_RESULT`` instead.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="delivery failed, activity requeued",
        agent_name="claude",
    )

    async def _keep_the_run(controller, context, _message) -> None:
        sink = controller.get_turn_sink(controller._get_session_key(context))
        assert sink is not None
        sink["settled_by"] = SETTLED_BY_TURN_ONLY_RESULT

    controller = _SettlementControllerDouble(on_turn=_keep_the_run)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    kept = request_store.get_run(request.id)
    assert kept is not None
    assert kept["status"] == "running"
    assert kept["completed_at"] is None
    assert not kept["error"]
    assert not (kept["metadata"] or {}).get("interrupt_reason")


def test_a_stopped_run_settles_canceled_not_succeeded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-037: pressing End on a live run must report ``canceled``, not success.

    The backend answers an acknowledged stop with an empty silent ``result``. Sent
    with the terminal-turn default that output claimed the run and recorded the empty
    body as ``succeeded``; since it writes before the stop's own guarded write,
    first-writer-wins made every normally-stopped run read as a success and left
    round 5's ``canceled`` mapping unreachable on the path that actually runs.

    The stamp here is derived from the production helpers rather than written as a
    literal, so changing either the stop output's lifecycle or the release-reason
    rule fails this test instead of silently reverting the behavior.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="a long build the user gives up on",
        agent_name="codex",
    )

    stop_semantics = stop_output_for(None)
    # A stop must not own the run's terminal state...
    assert stop_semantics.settles_run is False
    # ...but must still end the turn, so the dot settles and the SSE waiter closes.
    assert stop_semantics.completes_turn is True

    async def _stop_the_turn(controller, context, _message) -> None:
        sink = controller.get_turn_sink(controller._get_session_key(context))
        assert sink is not None
        sink["settled_by"] = ConsolidatedMessageDispatcher._turn_release_settlement(
            stop_semantics
        )

    controller = _SettlementControllerDouble(on_turn=_stop_the_turn)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    stopped = request_store.get_run(request.id)
    assert stopped is not None
    # Called off, not broken, and not a success: the closed vocabulary's ``canceled``.
    assert stopped["status"] == "canceled"
    assert stopped["completed_at"] is not None
    assert stopped["error"]
    assert stopped["metadata"]["interrupt_reason"] == SETTLED_BY_STOPPED


def test_agent_run_settles_when_dispatch_refuses_a_concurrent_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-010: the refusal returns before any sink exists, so the sink cannot carry it.

    ``dispatch_turn`` refuses a second streaming turn for a session that already has
    one in flight. That happens BEFORE ``register_turn_sink``, so an earlier design
    that only inspected the sink saw ``settled_by=None`` and kept the run open.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )
    controller = _SettlementControllerDouble()
    # A turn is already streaming for this session.
    controller.active_turn_sinks[_SettlementControllerDouble.SESSION_KEY] = {
        "on_chunk": AsyncMock(),
        "done_event": asyncio.Event(),
        "turn_token": "live",
    }
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    assert controller.turns == [], "the refused turn must never reach the handler"
    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "failed"
    assert settled["completed_at"] is not None
    assert settled["metadata"]["interrupt_reason"] == "refused_concurrent_turn"


def test_agent_run_cancel_racing_settlement_keeps_canceled(tmp_path: Path, monkeypatch) -> None:
    """HFR-011: a cancel landing mid-settlement must win (§3.3.1 TOCTOU).

    The cancel is applied between the executor deciding to settle and the write
    itself — the exact window an unguarded ``UPDATE`` would clobber. A fixture that
    cancels up front would pass even with the unguarded writer, so this interleaves
    for real.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )
    controller = _SettlementControllerDouble()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    original_settle = request_store.settle_without_result
    raced: list[str] = []

    def _settle_after_cancel(run_id: str, **kwargs):
        # The user cancels the still-``running`` row right before our write lands,
        # and the cancel path terminalizes it.
        sqlite_store = request_store._sqlite
        assert sqlite_store is not None
        sqlite_store.cancel_run(run_id)
        assert (
            sqlite_store.settle_run_terminal(run_id, terminal_status="canceled") == "canceled"
        )
        raced.append(run_id)
        return original_settle(run_id, **kwargs)

    monkeypatch.setattr(request_store, "settle_without_result", _settle_after_cancel)

    _run_single_request(service, request.id)

    assert raced == [request.id]
    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "canceled", "the guarded writer must not clobber a settled row"
    assert settled["metadata"].get("interrupt_reason") is None


def test_agent_run_cancel_requested_settles_canceled_not_failed(tmp_path: Path, monkeypatch) -> None:
    """A run the user asked to cancel reports ``canceled``, not ``failed``.

    ``cancel_run`` on a ``running`` row only records the request; the terminal write
    is ours, and it must honor that request in the same transaction.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )

    async def _cancel_mid_turn(_controller, _context, _message) -> None:
        sqlite_store = request_store._sqlite
        assert sqlite_store is not None
        assert sqlite_store.cancel_run(request.id) is True

    controller = _SettlementControllerDouble(on_turn=_cancel_mid_turn)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "canceled"
    assert settled["completed_at"] is not None
    assert settled["metadata"]["interrupt_reason"] == "no_terminal_result"


class _StopSinkSettler:
    """Drives a stop through the REAL ``settle_bound_turn_sink``.

    Hand-stamping ``settled_by="stopped"`` would test the string, not the stamp
    site. Borrowing the actual methods keeps the ``done.is_set()`` bail-out and the
    object identity guard under test, which is what decides the ordering against a
    real terminal result.
    """

    bind_context_to_turn_sink = SessionTurnManager.bind_context_to_turn_sink
    settle_bound_turn_sink = SessionTurnManager.settle_bound_turn_sink
    # Re-wrap: the real one is a staticmethod, and a bare function assigned to a
    # class attribute would bind ``self`` as its first argument.
    _sink_identity_matches = staticmethod(SessionTurnManager._sink_identity_matches)

    def __init__(self, controller) -> None:
        self.controller = controller
        self.active_turn_sinks = controller.active_turn_sinks


def test_agent_run_stopped_by_user_settles_canceled(tmp_path: Path, monkeypatch) -> None:
    """HFR-012: running-tab End on an agent run terminalizes it as ``canceled``.

    The backend was interrupted without emitting a terminal result, so nothing else
    will ever write this row. ``canceled`` (not ``failed``) is the honest status:
    the run did not break, the user called it off.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )

    async def _stop_mid_turn(controller, context, _message) -> None:
        settler = _StopSinkSettler(controller)
        binding = settler.bind_context_to_turn_sink(context)
        assert binding is not None
        assert settler.settle_bound_turn_sink(binding) is True

    controller = _SettlementControllerDouble(on_turn=_stop_mid_turn)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "canceled", "an explicit stop is a cancellation, not a failure"
    assert settled["completed_at"] is not None
    assert settled["metadata"]["interrupt_reason"] == "stopped"


def test_stop_defers_to_a_terminal_result_that_already_landed(tmp_path: Path, monkeypatch) -> None:
    """A stop racing a terminal result that arrived FIRST must not steal the run.

    This is the safe half of the stop race: the backend emitted its result (setting
    the done event and stamping the honest settlement) before the stop fallback ran.
    ``settle_bound_turn_sink`` must decline, leaving the out-of-band terminal writer
    the owner — otherwise a run that really finished would report ``canceled``.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )
    declined: list[bool] = []

    async def _terminal_then_stop(controller, context, _message) -> None:
        session_key = controller._get_session_key(context)
        # The real terminal emit: stamp the honest settlement and release the waiter.
        sink = controller.active_turn_sinks[session_key]
        sink["settled_by"] = SETTLED_BY_TERMINAL_RESULT
        sink["done_event"].set()
        # The stop fallback fires afterwards and must be a no-op.
        settler = _StopSinkSettler(controller)
        binding = settler.bind_context_to_turn_sink(context)
        declined.append(settler.settle_bound_turn_sink(binding) is False)
        assert sink["settled_by"] == SETTLED_BY_TERMINAL_RESULT

    controller = _SettlementControllerDouble(on_turn=_terminal_then_stop)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    assert declined == [True], "a stop after the result landed must not re-settle the sink"
    still_open = request_store.get_run(request.id)
    assert still_open is not None
    assert still_open["status"] == "running", "the terminal result's own writer owns this row"
    assert still_open["metadata"].get("interrupt_reason") is None


def test_late_terminal_result_cannot_reopen_a_stopped_run(tmp_path: Path, monkeypatch) -> None:
    """The lossy half of the stop race, pinned as deliberate precedence.

    If the backend's terminal result lands only AFTER the stop was acknowledged and
    the run settled, the terminal write loses — both writers are scoped to
    ``queued|running``. The row stays ``canceled``, which is still true of a run the
    user stopped, and the late text is still appended to the run's outputs rather
    than dropped. Pinned so a future change to either writer's guard is a visible
    test failure, not a silent flip in who wins.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )

    async def _stop_mid_turn(controller, context, _message) -> None:
        settler = _StopSinkSettler(controller)
        assert settler.settle_bound_turn_sink(settler.bind_context_to_turn_sink(context)) is True

    controller = _SettlementControllerDouble(on_turn=_stop_mid_turn)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)
    assert request_store.get_run(request.id)["status"] == "canceled"

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    recorded = sqlite_store.record_run_output(
        request.id,
        output_id="late-terminal",
        text="the answer that arrived too late",
        terminal_status="succeeded",
    )

    assert recorded["recorded"] is True, "the late text is still appended, not dropped"
    assert recorded["terminal_transition"] is False, "but it must not re-terminalize the row"
    final = request_store.get_run(request.id)
    assert final is not None
    assert final["status"] == "canceled", "a stop already settled this run; the late result loses"
    assert final["metadata"]["interrupt_reason"] == "stopped"


# ---------------------------------------------------------------------------
# Gap B: the staleness sweep (docs/plans/agent-run-zombie-settlement.md §4)
#
# Gap A settles a run whose turn REPORTED that it produced nothing. These cases
# cover the runs nobody will ever report on: the owner vanished without reporting,
# the transport never came back, the queue gate never reopened. The sweep is the
# only thing that can close them, which also makes it the only thing that can
# WRONGLY close a healthy run — so the negative cases matter as much as the positive.
#
# Note on staging: ``ScheduledTaskService.__init__`` runs ``recover_processing()``,
# which requeues every ``running`` row (that is how a restart's in-flight runs get
# retried, and why the orphan class here is about owners lost WITHIN a live process).
# So these tests build the service first and stage the stale row afterwards.
# ---------------------------------------------------------------------------


class _SweepControllerDouble:
    """The controller surface the sweep reads: timing knobs plus the ownership lane.

    ``session_turns`` is injectable so a test can supply the real
    ``SessionTurnManager`` (the workbench ownership lane) or a broken provider (the
    fail-closed case) rather than a hand-written answer.
    """

    def __init__(self, *, session_turns: Any = None, transport_ready: bool = True, **timings) -> None:
        runtime = {
            "harness_run_sweep_interval_seconds": 60,
            "harness_run_orphan_grace_seconds": 120,
            "harness_run_queued_ttl_seconds": 1800,
            "harness_run_hold_ttl_seconds": 3600,
        }
        runtime.update(timings)
        self.config = SimpleNamespace(language="en", runtime=SimpleNamespace(**runtime))
        self.session_turns = SessionTurnManager(self) if session_turns is None else session_turns
        self._transport_ready = transport_ready

    def is_im_transport_ready(self, _platform: str) -> bool:
        return self._transport_ready


def _sweep_service(
    tmp_path: Path,
    request_store: TaskExecutionStore,
    controller: Any = None,
) -> ScheduledTaskService:
    return ScheduledTaskService(
        controller=controller if controller is not None else _SweepControllerDouble(),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _force_run_columns(request_store: TaskExecutionStore, run_id: str, **columns) -> None:
    """Write raw ``agent_runs`` columns to stage a state that takes real time to reach.

    Aging a row by hand is the only way to exercise a TTL without sleeping through
    it, and the sweep classifies purely on stored columns, so a staged row is
    indistinguishable from one that got there naturally.
    """

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    with sqlite_store.engine.begin() as conn:
        conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**columns))


def _stage_orphan_run(
    request_store: TaskExecutionStore,
    *,
    message: str = "summarize the build",
    age_seconds: int = 900,
) -> str:
    """A ``running`` agent run whose executor is gone — the post-restart zombie."""

    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message=message,
        agent_name="codex",
    )
    _force_run_columns(
        request_store,
        request.id,
        status="running",
        started_at=_ago(age_seconds),
        created_at=_ago(age_seconds),
    )
    return request.id


def test_sweep_terminalizes_orphaned_running_run(tmp_path: Path, monkeypatch) -> None:
    """HFR-013: a ``running`` row with no live owner is the zombie Gap A cannot reach.

    Gap A only fires when a turn in this process reports back. A turn that was taken
    over out of band and then lost — no sink, no execution task, no settlement — never
    reports, so without the sweep the row stays ``running`` forever: it blocks its
    session, shows as active in the UI, and never notifies anyone.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = _stage_orphan_run(request_store)

    service._sweep_stale_runs()

    swept = request_store.get_run(run_id)
    assert swept is not None
    assert swept["status"] == "failed"
    assert swept["completed_at"] is not None
    assert swept["metadata"]["interrupt_reason"] == "orphaned"
    # The resolved translation, not a raw dotted key: this column is shown verbatim
    # in the Runs UI and in the callback message.
    assert "Avibe Harness" in swept["error"]
    assert "harness.run.interrupted" not in swept["error"]


def test_sweep_respects_the_orphan_grace_period(tmp_path: Path, monkeypatch) -> None:
    """A run that just started has no owner YET; the grace period is what protects it.

    Ownership registration and the run row are written by different steps, so a
    freshly claimed run is briefly visible as unowned. Sweeping on that window would
    fail healthy runs at the moment they start.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = _stage_orphan_run(request_store, age_seconds=5)

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "running"


def test_sweep_skips_running_run_owned_by_inflight_execution(tmp_path: Path, monkeypatch) -> None:
    """The drain lane: a claimed request whose execution task is still alive.

    A hung backend looks exactly like an orphan in the database. The difference is
    only visible in memory, and terminalizing it here would settle a run that is
    still streaming — and then the real result would have nowhere to land.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = _stage_orphan_run(request_store)
    # Only membership is read; a real ``asyncio.Task`` would need a running loop.
    service._inflight_executions[run_id] = Mock(name="live-execution-task")

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "running"


def test_sweep_skips_running_run_owned_by_workbench_turn(tmp_path: Path, monkeypatch) -> None:
    """HFR-014: the second ownership lane, and the trap: it never enters ``_inflight_executions``.

    A workbench/web turn takes the run over out of band, so the drain lane knows
    nothing about it. A sweep that consulted only ``_inflight_executions`` would look
    correct in every drain-lane test and still fail live workbench runs. Uses the real
    ``register_turn_sink`` so the attribution path itself is under test.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    session_turns = SessionTurnManager(controller=None)
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(session_turns=session_turns)
    )
    run_id = _stage_orphan_run(request_store)
    sibling_id = _stage_orphan_run(request_store, message="and the sibling callback")

    context = MessageContext(
        user_id="U123",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "task_execution_id": run_id,
            "accepted_agent_run_ids": [run_id, sibling_id],
        },
    )
    session_turns.register_turn_sink(
        "slack::channel::C123",
        on_chunk=AsyncMock(),
        done_event=asyncio.Event(),
        context=context,
    )
    assert service._inflight_executions == {}, "the workbench lane registers nothing here"

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "running"
    assert request_store.get_run(sibling_id)["status"] == "running"


@pytest.mark.parametrize(
    "session_turns",
    [
        pytest.param(SimpleNamespace(), id="provider-missing"),
        pytest.param(
            SimpleNamespace(owned_agent_run_ids=Mock(side_effect=RuntimeError("turn state gone"))),
            id="provider-raises",
        ),
    ],
)
def test_sweep_fails_closed_when_ownership_is_unknown(
    tmp_path: Path, monkeypatch, session_turns: Any
) -> None:
    """HFR-015: "Nobody owns this run" and "I cannot tell" are opposite answers.

    Both failures degrade to an empty owner set, which reads as "sweep everything".
    The sweep must refuse to run instead: leaving a zombie for one more interval is
    recoverable, failing every live run is not.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(session_turns=session_turns)
    )
    run_id = _stage_orphan_run(request_store)

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "running"


def _stage_queued_run(
    request_store: TaskExecutionStore,
    *,
    metadata: Optional[dict] = None,
    created_age_seconds: int = 0,
    updated_age_seconds: Optional[int] = None,
) -> str:
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
    )
    columns: dict[str, Any] = {}
    if created_age_seconds:
        columns["created_at"] = _ago(created_age_seconds)
    if updated_age_seconds is not None:
        columns["updated_at"] = _ago(updated_age_seconds)
    if metadata is not None:
        existing = request_store.get_run(request.id)["metadata"] or {}
        columns["metadata_json"] = json.dumps({**existing, **metadata})
    if columns:
        _force_run_columns(request_store, request.id, **columns)
    return request.id


def test_sweep_terminalizes_queued_run_stranded_by_a_dead_transport(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-016: a queued run whose platform never reconnected is undeliverable, not pending.

    Left alone it waits forever with no user-visible explanation. Two independent facts
    have to agree before it is failed: the reason the drain recorded, and a live check
    saying the platform is *still* undeliverable. Here both hold.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        metadata={"last_skip_reason": "transport_unavailable", "last_skip_at": _ago(1900)},
        created_age_seconds=1900,
    )
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(transport_ready=False)
    )

    service._sweep_stale_runs()

    swept = request_store.get_run(run_id)
    assert swept["status"] == "failed"
    assert swept["metadata"]["interrupt_reason"] == "transport_unavailable"
    assert "Avibe Harness" in swept["error"]


def test_sweep_spares_a_stale_transport_stamp_once_the_platform_is_back(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-021: the recorded reason alone must not fail a run that is deliverable now.

    The drain ``break``s at its concurrency cap without examining the rest of the
    queue, so a row below the cut keeps its old ``transport_unavailable`` stamp long
    after its platform reconnected — nothing re-derives it. Sweeping on the stamp
    alone would fail a run that is merely waiting for a free slot (Codex P1). The live
    second opinion is what distinguishes the two.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        metadata={"last_skip_reason": "transport_unavailable", "last_skip_at": _ago(1900)},
        created_age_seconds=1900,
    )
    # transport_ready=True: Slack is back. The drain cannot say so, because it is at
    # capacity and never reaches this row to refresh the stamp.
    service = _sweep_service(tmp_path, request_store)
    for index in range(service._MAX_CONCURRENT_EXECUTIONS):
        service._inflight_executions[f"busy{index:08d}"] = Mock(name="live-execution-task")

    asyncio.run(service._drain_requests())
    assert (
        request_store.get_run(run_id)["metadata"]["last_skip_reason"] == "transport_unavailable"
    ), "the stale stamp survives, which is exactly why the sweep needs a second opinion"

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued"


def test_sweep_grants_a_second_outage_its_own_ttl_after_a_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-033: proven recovery retires the evidence, so the NEXT outage ages from itself.

    Deliverability only exempts a row while the transport is up. Since the drain
    ``break``s at its concurrency cap it never re-stamps a row below the cut, so a stale
    ``last_skip_at`` survived the recovery — and the moment the platform dropped again
    the sweep read one continuous outage and failed the run instantly, skipping the whole
    configured reconnect window (Codex P2). Observing the recovery is the only chance to
    retire it, so the sweep does that when it sees both halves of the evidence disagree.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        metadata={"last_skip_reason": "transport_unavailable", "last_skip_at": _ago(1900)},
        created_age_seconds=1900,
    )
    # Slack is back, and the drain is at capacity so it can never say so itself.
    controller = _SweepControllerDouble()
    service = _sweep_service(tmp_path, request_store, controller)
    for index in range(service._MAX_CONCURRENT_EXECUTIONS):
        service._inflight_executions[f"busy{index:08d}"] = Mock(name="live-execution-task")

    service._sweep_stale_runs()

    recovered = request_store.get_run(run_id)
    assert recovered["status"] == "queued", "deliverable => never swept (HFR-021)"
    assert "last_skip_reason" not in (recovered["metadata"] or {}), "the ended outage is forgotten"
    assert "last_skip_at" not in (recovered["metadata"] or {})

    # A NEW outage, recorded by the drain now that a slot is free.
    service._inflight_executions.clear()
    controller._transport_ready = False
    asyncio.run(service._drain_requests())
    restamped = request_store.get_run(run_id)["metadata"]
    assert restamped["last_skip_reason"] == "transport_unavailable"

    service._last_sweep_at = None  # the rate limiter is not what is under test
    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued", (
        "the second outage gets its own full TTL, not the first one's leftover age"
    )


def test_sweep_ages_a_transport_failure_from_when_it_started(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-024: the TTL is a reconnect window, so it runs from the outage, not the enqueue.

    A run can legitimately sit in ``queued`` far longer than the TTL for reasons that
    are progress — capacity, a busy session. If its transport then blinks, aging from
    ``created_at`` would make it sweepable on the very next tick and skip the entire
    configured reconnect window. ``last_skip_at`` is when the reason started (the stamp
    is transition-triggered), which is the only clock that means "how long has this been
    undeliverable".
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        # Two hours in the queue, but the transport only just went down.
        metadata={"last_skip_reason": "transport_unavailable", "last_skip_at": _ago(30)},
        created_age_seconds=7200,
    )
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(transport_ready=False)
    )

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued"


def test_sweep_never_terminalizes_a_transport_reason_with_no_timestamp(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-025: a reason without its timestamp is unrecognized evidence, not old evidence.

    ``record_run_skip_reason`` writes the reason and ``last_skip_at`` in one statement,
    so a row carrying one without the other did not come from that writer. Falling back
    to ``created_at`` there would quietly reintroduce the bug HFR-024 pins, so the row is
    left alone instead.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        metadata={"last_skip_reason": "transport_unavailable"},
        created_age_seconds=7200,
    )
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(transport_ready=False)
    )

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued"


def test_sweep_skips_transport_class_when_deliverability_is_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-022: if the live deliverability check breaks, the whole class is disabled.

    Same fail-closed posture as unknown ownership: a sweep that cannot prove a run is
    undeliverable must not fail it. The other classes keep working — only the transport
    class is suppressed for this tick.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        metadata={"last_skip_reason": "transport_unavailable", "last_skip_at": _ago(1900)},
        created_age_seconds=1900,
    )
    controller = _SweepControllerDouble(transport_ready=False)
    service = _sweep_service(tmp_path, request_store, controller)
    orphan_id = _stage_orphan_run(request_store)

    def _broken(_platform: str) -> bool:
        raise RuntimeError("transport registry unavailable")

    controller.is_im_transport_ready = _broken

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued", "unprovable => untouched"
    assert request_store.get_run(orphan_id)["status"] == "failed", "other classes still sweep"


def test_sweep_leaves_a_queued_run_whose_session_is_merely_busy(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-017: ``session_busy`` is progress, and it must be able to clear a stale transport reason.

    A run blocked behind its own session's active turn will run the moment that turn
    ends. Without the drain overwriting the older ``transport_unavailable`` reason, an
    aged row that is now making progress would still read as sweepable.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(
        request_store,
        metadata={"last_skip_reason": "transport_unavailable", "last_skip_at": _ago(1900)},
        created_age_seconds=1900,
    )
    service = _sweep_service(tmp_path, request_store)
    # The session is busy: a turn for this conversation holds the lock.
    lock_key = service._execution_lock_key(request_store.list_pending()[0])
    assert lock_key is not None
    service._inflight_sessions.add(lock_key)
    service._session_lock_owners[lock_key] = "otherrun0001"
    service._inflight_executions["otherrun0001"] = Mock(name="live-execution-task")

    asyncio.run(service._drain_requests())
    assert request_store.get_run(run_id)["metadata"]["last_skip_reason"] == "session_busy"

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued"


def test_sweep_ignores_queued_run_skipped_only_for_capacity(tmp_path: Path, monkeypatch) -> None:
    """A row the drain never even looked at must never be swept.

    At capacity the drain ``break``s without examining the rest of the queue, so those
    rows carry no skip reason. Requiring recorded evidence is what makes that silence
    safe — a busy service must not look like a broken one.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(request_store, created_age_seconds=7200)
    service = _sweep_service(tmp_path, request_store)
    for index in range(service._MAX_CONCURRENT_EXECUTIONS):
        service._inflight_executions[f"busy{index:08d}"] = Mock(name="live-execution-task")

    asyncio.run(service._drain_requests())
    assert request_store.get_run(run_id)["metadata"].get("last_skip_reason") is None

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "queued"


def test_legacy_queue_hold_metadata_has_no_sweep_authority(
    tmp_path: Path, monkeypatch
) -> None:
    """Old metadata cannot recreate the removed Run-owned queue lifecycle."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    recovering = _stage_queued_run(
        request_store, metadata={"workbench_queue_holds_run": True}, updated_age_seconds=1800
    )
    abandoned = _stage_queued_run(
        request_store, metadata={"workbench_queue_holds_run": True}, updated_age_seconds=7200
    )
    service = _sweep_service(tmp_path, request_store)

    service._sweep_stale_runs()

    assert request_store.get_run(recovering)["status"] == "queued"
    assert request_store.get_run(abandoned)["status"] == "queued"


def test_legacy_hold_metadata_does_not_change_turn_or_run_ownership(
    tmp_path: Path, monkeypatch
) -> None:
    """Only explicit Turn attribution owns running work; legacy flags own nothing."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    session_turns = SessionTurnManager(controller=None)
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(session_turns=session_turns)
    )
    primary = _stage_orphan_run(request_store)
    held = _stage_queued_run(
        request_store, metadata={"workbench_queue_holds_run": True}, updated_age_seconds=7200
    )
    unowned = _stage_queued_run(
        request_store, metadata={"workbench_queue_holds_run": True}, updated_age_seconds=7200
    )

    session_turns.register_turn_sink(
        "slack::channel::C123",
        on_chunk=AsyncMock(),
        done_event=asyncio.Event(),
        context=MessageContext(
            user_id="U123",
            channel_id="C123",
            platform="slack",
            platform_specific={
                "task_execution_id": primary,
                "accepted_agent_run_ids": [primary],
            },
        ),
    )

    service._sweep_stale_runs()

    assert request_store.get_run(held)["status"] == "queued"
    assert request_store.get_run(unowned)["status"] == "queued"


def test_workbench_turn_settles_its_agent_run_when_no_result_arrives(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-027: on the gate lane the TURN settles the run, because the harness cannot.

    ``_execute_agent_run`` hands an avibe-targeted run to
    ``session_turn_gate.submit_scheduled`` and returns while the turn is still
    running, so the outcome that turn eventually produces is never seen there. When
    the turn ends without a terminal result — a stop the backend answered without
    emitting one — the turn lane is the only place left that can settle the row.
    Without this the run stays ``running`` until the sweep relabels it ``orphaned``,
    or forever when the sweep is disabled. A coalesced turn settles every id it owns.
    """

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    primary = request_store.enqueue_agent_run(
        session_id=session_id, message="stop me", agent_name="codex"
    ).id
    sibling = request_store.enqueue_agent_run(
        session_id=session_id, message="and the coalesced sibling", agent_name="codex"
    ).id
    for run_id in (primary, sibling):
        _force_run_columns(request_store, run_id, status="running", started_at=_ago(30))

    async def _stopped_without_result(_controller, _context, _text, **_kwargs):
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_STOPPED)

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", _stopped_without_result)

    manager = SessionTurnManager(controller=None)
    manager.controller = SimpleNamespace(
        scheduled_task_service=service,
        session_turns=manager,
        set_agent_status=lambda *_args, **_kwargs: None,
        _get_session_key=lambda ctx: f"avibe::{ctx.channel_id}",
    )
    context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "task_execution_id": primary,
            "task_trigger_kind": "agent_run",
            "accepted_agent_run_ids": [primary, sibling],
        },
    )

    async def _exercise() -> None:
        assert (
            await manager.submit(session_id, context, "stop me", source=SOURCE_SCHEDULED)
        ).route == "ran"
        for _ in range(400):
            if request_store.get_run(primary)["status"] != "running":
                break
            await asyncio.sleep(0.005)

    asyncio.run(_exercise())

    for run_id in (primary, sibling):
        settled = request_store.get_run(run_id)
        # ``stopped`` is user intent, so ``canceled`` — not ``failed`` — is the honest
        # terminal, exactly as on the drain lane (HFR-012).
        assert settled["status"] == "canceled", run_id
        assert settled["metadata"]["interrupt_reason"] == "stopped", run_id
        assert settled["error"], run_id


def test_backend_refresh_settles_its_run_as_a_refresh_not_a_user_stop(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-029: a runtime refresh is not a user Stop, and must not be reported as one.

    ``release_for_backend_refresh`` cancels every in-flight turn of a backend whose
    cached process state is about to disappear — which is what an ``agents.*`` save's
    rolling reconciliation does. That arrives in ``_run`` as a bare
    ``CancelledError``, indistinguishable from the Stop button unless the canceller
    says so. Reading every cancellation as a stop made a run killed by routine
    configuration reconciliation settle ``canceled`` with the user-stop explanation,
    so the callback told the user they had stopped a run they never touched and the
    failure accounting saw deliberate intent instead of an infrastructure fault.
    """

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = request_store.enqueue_agent_run(
        session_id=session_id, message="interrupted by a config save", agent_name="codex"
    ).id
    _force_run_columns(request_store, run_id, status="running", started_at=_ago(30))

    dispatch_started = asyncio.Event()

    async def _never_returns(_controller, _context, _text, **_kwargs):
        dispatch_started.set()
        await asyncio.Event().wait()  # held open until the refresh cancels the turn
        raise AssertionError("unreachable")

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", _never_returns)

    manager = SessionTurnManager(controller=None)
    manager.controller = SimpleNamespace(
        scheduled_task_service=service,
        session_turns=manager,
        set_agent_status=lambda *_args, **_kwargs: None,
        _get_session_key=lambda ctx: f"avibe::{ctx.channel_id}",
    )
    context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "task_execution_id": run_id,
            "task_trigger_kind": "agent_run",
            "agent_session_target": {"agent_backend": "codex"},
        },
    )

    async def _exercise() -> None:
        assert (
            await manager.submit(session_id, context, "interrupted by a config save", source=SOURCE_SCHEDULED)
        ).route == "ran"
        await asyncio.wait_for(dispatch_started.wait(), timeout=5)
        released = await manager.release_for_backend_refresh(
            backend="codex", base_session_ids={session_id}
        )
        assert released == 1
        for _ in range(400):
            if request_store.get_run(run_id)["status"] != "running":
                break
            await asyncio.sleep(0.005)

    asyncio.run(_exercise())

    settled = request_store.get_run(run_id)
    # An infrastructure fault with no user intent behind it, so ``failed`` — it stays
    # visible to a failure counter — and the reason names the refresh, not a stop.
    assert settled["status"] == "failed"
    assert settled["metadata"]["interrupt_reason"] == SETTLED_BY_BACKEND_REFRESH
    assert settled["error"]
    assert "stop" not in settled["error"].lower()


def test_backend_refresh_settles_restored_durable_agent_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from storage.background import attach_agent_run_delivery_in_connection

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = request_store.enqueue_agent_run(
        session_id=session_id,
        message="restored before backend refresh",
        agent_name="codex",
    ).id
    _force_run_columns(request_store, run_id, status="running", started_at=_ago(30))
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = conn.execute(
            select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id)
        ).scalar_one()
        delivery_id = message_deliveries.new_delivery_id()
        turn_id = message_deliveries.new_turn_id()
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
                author="harness",
                source="harness",
                message_type="harness",
                text="restored before backend refresh",
                native_message_id=f"agent_run:{run_id}",
                metadata={
                    "scheduled_provenance": {
                        "task_execution_id": run_id,
                        "platform_specific": {
                            "task_trigger_kind": "agent_run",
                            "task_execution_id": run_id,
                        },
                    }
                },
            ),
            dispatch_text="restored before backend refresh",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            run_id,
            session_id=session_id,
            delivery_id=delivery_id,
        )
        message_deliveries.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id=session_id,
            backend="codex",
            deliveries=[delivery],
            dispatch_text="restored before backend refresh",
        )
        turn = message_deliveries.get_turn(conn, turn_id)
        assert turn is not None
        assert message_deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            runtime_key="restored-runtime",
            runtime_turn_id="restored-turn",
            native_turn_id="restored-native",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "restored_native_acceptance"},
        )

    manager = SessionTurnManager(controller=None)
    manager._engine = engine
    manager.controller = SimpleNamespace(
        scheduled_task_service=service,
        set_agent_status=lambda *_args, **_kwargs: None,
    )
    assert run_id in manager.owned_agent_run_ids()

    released = asyncio.run(
        manager.release_for_backend_refresh(
            backend="codex",
            base_session_ids={session_id},
        )
    )

    assert released == 1
    settled = request_store.get_run(run_id)
    assert settled["status"] == "failed"
    assert settled["metadata"]["interrupt_reason"] == SETTLED_BY_BACKEND_REFRESH


def test_turn_only_result_leaves_an_activity_owned_run_alone(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-031: "the turn ended" is not "the run ended" — leave a run somebody still owns.

    The Claude Activity delivery-failure path closes its origin turn with a silent
    terminal ``result`` carrying ``completes_run=False`` while the REQUEUED Activity
    keeps the run and retries. That release stamps ``turn_only_result``, which is
    deliberately NOT in ``SETTLEMENTS_WITHOUT_RESULT``: settling here would fail a
    live Activity-owned run and fire its terminal callback before the retry ran.
    Both lanes therefore allow-list the settlements that mean "no result is coming"
    instead of treating everything but ``terminal_result`` as a zombie.
    """

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = request_store.enqueue_agent_run(
        session_id=session_id, message="delivery failed, activity requeued", agent_name="claude"
    ).id
    _force_run_columns(request_store, run_id, status="running", started_at=_ago(30))

    async def _turn_closed_run_kept(_controller, _context, _text, **_kwargs):
        return TurnDispatchOutcome(error=None, settled_by=SETTLED_BY_TURN_ONLY_RESULT)

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", _turn_closed_run_kept)

    manager = SessionTurnManager(controller=None)
    manager.controller = SimpleNamespace(
        scheduled_task_service=service,
        session_turns=manager,
        set_agent_status=lambda *_args, **_kwargs: None,
        _get_session_key=lambda ctx: f"avibe::{ctx.channel_id}",
    )
    context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "task_execution_id": run_id,
            "task_trigger_kind": "agent_run",
        },
    )

    async def _exercise() -> None:
        assert (
            await manager.submit(
                session_id, context, "delivery failed, activity requeued", source=SOURCE_SCHEDULED
            )
        ).route == "ran"
        for _ in range(400):
            if session_id not in manager.in_flight:
                break
            await asyncio.sleep(0.005)
        assert session_id not in manager.in_flight

    asyncio.run(_exercise())

    kept = request_store.get_run(run_id)
    # Still owned, still running: the retry gets to write the honest terminal state.
    assert kept["status"] == "running"
    assert not (kept["metadata"] or {}).get("interrupt_reason")
    assert not kept["error"]


def test_sweep_skips_hold_class_when_live_session_turns_are_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-035: an unanswerable "is this session busy?" disables the hold class.

    Same fail-closed posture as ownership and deliverability: "no session is busy" and
    "I could not look" are opposite answers, and acting on the second fails a run the
    gate is about to flush. Only the hold class is suppressed for this tick.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    manager = SessionTurnManager(controller=None)

    def _broken() -> set[str]:
        raise RuntimeError("turn manager unavailable")

    manager.busy_session_ids = _broken
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(session_turns=manager)
    )
    held = _stage_queued_run(
        request_store, metadata={"workbench_queue_holds_run": True}, updated_age_seconds=7200
    )
    orphan_id = _stage_orphan_run(request_store)

    service._sweep_stale_runs()

    assert request_store.get_run(held)["status"] == "queued", "unprovable => untouched"
    assert request_store.get_run(orphan_id)["status"] == "failed", "other classes still sweep"


def test_sweep_spares_a_hold_parked_behind_a_live_session_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-034: a hold with a live turn to wait for is not abandoned (Codex P2).

    The gate answers ``enqueued`` when a run arrives at a session that already has a
    turn in flight, and the run is requeued with ``workbench_queue_holds_run``. Nobody
    reports it as owned — the live turn owns only the ids it is executing itself — so a
    legitimate Workbench turn outliving ``harness_run_hold_ttl_seconds`` had its own
    queued follower failed underneath it, even though ``flush_queue`` would have picked
    it up on completion. Ownership cannot express this; live session occupancy can. The
    control row proves the class still works: same flag, same age, no live turn.
    """

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    manager = SessionTurnManager(controller=None)
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(session_turns=manager)
    )
    manager.controller = SimpleNamespace(
        scheduled_task_service=service,
        session_turns=manager,
        set_agent_status=lambda *_args, **_kwargs: None,
        _get_session_key=lambda ctx: f"avibe::{ctx.channel_id}",
    )
    live = request_store.enqueue_agent_run(
        session_id=session_id, message="the long legitimate turn", agent_name="codex"
    ).id
    _force_run_columns(request_store, live, status="running", started_at=_ago(30))
    held = request_store.enqueue_agent_run(
        session_id=session_id, message="parked behind that turn", agent_name="codex"
    ).id
    _force_run_columns(
        request_store,
        held,
        updated_at=_ago(7200),
        metadata_json=json.dumps({"workbench_queue_holds_run": True}),
    )
    abandoned = _stage_queued_run(
        request_store, metadata={"workbench_queue_holds_run": True}, updated_age_seconds=7200
    )

    dispatch_started = asyncio.Event()

    async def _never_returns(_controller, _context, _text, **_kwargs):
        dispatch_started.set()
        await asyncio.Event().wait()  # the long turn, still going
        raise AssertionError("unreachable")

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", _never_returns)

    context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        platform_specific={"task_execution_id": live, "task_trigger_kind": "agent_run"},
    )

    async def _exercise() -> None:
        assert (
            await manager.submit(
                session_id, context, "the long legitimate turn", source=SOURCE_SCHEDULED
            )
        ).route == "ran"
        await asyncio.wait_for(dispatch_started.wait(), timeout=5)
        assert manager.busy_session_ids() == {session_id}

        service._sweep_stale_runs()

        assert request_store.get_run(held)["status"] == "queued", "the gate will flush it"
        assert request_store.get_run(live)["status"] == "running", "owned, so never swept"
        assert request_store.get_run(abandoned)["status"] == "queued"

        turn = manager.in_flight.get(session_id)
        assert turn is not None
        turn.task.cancel()
        with suppress(asyncio.CancelledError):
            await turn.task

    asyncio.run(_exercise())


def test_canceling_a_delivery_owned_run_retires_its_exact_queue_row(
    tmp_path: Path, monkeypatch
) -> None:
    """Run cancellation and exact Delivery retirement share one transaction."""

    from storage.background import attach_agent_run_delivery_in_connection
    from storage.models import agent_sessions

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run = request_store.enqueue_agent_run(
        session_id=session_id,
        message="cancel this queued input",
        agent_name="codex",
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            author="harness",
            source="harness",
            message_type="harness",
            text="cancel this queued input",
            native_message_id=f"agent_run:{run.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            run.id,
            session_id=session_id,
            delivery_id=str(delivery["id"]),
        )
        assert len(message_deliveries.list_queued(conn, session_id)) == 1

    assert request_store.cancel_run(run.id) is True
    assert request_store.get_run(run.id)["status"] == "canceled"
    with create_sqlite_engine().connect() as conn:
        assert message_deliveries.list_queued(conn, session_id) == []
        retired = message_deliveries.get_delivery(conn, str(delivery["id"]))
    assert retired is not None and retired["state"] == "retired"


def test_sweep_leaves_watch_runtime_and_deferred_rows_alone(tmp_path: Path, monkeypatch) -> None:
    """Two row classes look stale and are not: neither is ours to settle.

    ``watch_runtime`` is a singleton bookkeeping row that is ``running`` by design, and
    a deferred terminal belongs to the Activity lifecycle. Terminalizing either would
    corrupt state the sweep does not own.

    Both are defended twice on purpose. Mutation testing shows the deferred row
    survives even with the sweep's own candidate check removed, because
    ``settle_run_terminal`` refuses a deferred row as well — so read the candidate
    check as belt-and-braces, not as the load-bearing guard. The watch_runtime row
    needs both the query filter and the ``agent_run`` restriction gone before it is
    touched.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    watch_runtime = _stage_orphan_run(request_store, message="watch runtime bookkeeping")
    _force_run_columns(request_store, watch_runtime, run_type="watch_runtime")
    deferred = _stage_orphan_run(request_store, message="activity-owned run")
    _force_run_columns(
        request_store,
        deferred,
        result_payload_json=json.dumps({"deferred_terminal_status": "succeeded"}),
    )

    service._sweep_stale_runs()

    assert request_store.get_run(watch_runtime)["status"] == "running"
    assert request_store.get_run(deferred)["status"] == "running"


def test_sweep_releases_a_leaked_session_lock(tmp_path: Path, monkeypatch) -> None:
    """HFR-018: an honest row is only half the repair; the wedge is in memory.

    ``_inflight_sessions`` gates dispatch for the whole conversation, so a lock that
    outlived its execution keeps the session undispatchable no matter how the run row
    reads. A lock a LIVE execution holds must survive the same pass — freeing that one
    would let two turns run at once in one session.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    service._inflight_sessions.update({"key:leaked", "key:live"})
    service._session_lock_owners.update({"key:leaked": "deadrun00001", "key:live": "liverun00001"})
    service._inflight_executions["liverun00001"] = Mock(name="live-execution-task")
    service._drain_dirty = False

    service._sweep_stale_runs()

    assert service._inflight_sessions == {"key:live"}
    assert service._session_lock_owners == {"key:live": "liverun00001"}
    assert service._drain_dirty is True, "the freed session must be re-checked immediately"


def test_execution_completion_does_not_steal_a_later_lock_owner(tmp_path: Path, monkeypatch) -> None:
    """HFR-019: the owner map must not be clobbered by a finishing predecessor.

    Two executions can reuse one lock key in sequence. If the first one's completion
    callback removed the owner entry the second one wrote, the sweep would read the
    live lock as leaked and free it mid-turn.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    service._inflight_sessions.add("key:shared")
    service._session_lock_owners["key:shared"] = "secondrun001"
    service._inflight_executions["secondrun001"] = Mock(name="live-execution-task")

    service._on_execution_done("firstrun0001", "key:shared", Mock(cancelled=lambda: True))

    assert service._session_lock_owners == {"key:shared": "secondrun001"}
    service._sweep_stale_runs()
    assert service._session_lock_owners == {"key:shared": "secondrun001"}


def test_stranded_queued_run_does_not_trigger_repeated_metadata_writes(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-020: the skip stamp must be transition-only, or it becomes a self-feeding hot loop.

    Every write bumps the store's invalidation probe, which is what wakes the drain —
    so a per-tick stamp would make a permanently-down transport spin the service
    forever. It must also leave ``updated_at`` alone: the hold TTL reads that column,
    and bumping it would keep any hold permanently fresh.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    run_id = _stage_queued_run(request_store)
    before = request_store.get_run(run_id)["updated_at"]
    service = _sweep_service(tmp_path, request_store, _SweepControllerDouble(transport_ready=False))

    wrote: list[bool] = []
    original = request_store.record_skip_reason

    def _record(target_id: str, *, reason: str) -> bool:
        result = original(target_id, reason=reason)
        wrote.append(result)
        return result

    monkeypatch.setattr(request_store, "record_skip_reason", _record)

    for _ in range(4):
        asyncio.run(service._drain_requests())

    assert wrote == [True, False, False, False], "the reason is stamped once, not once per tick"
    after = request_store.get_run(run_id)
    assert after["metadata"]["last_skip_reason"] == "transport_unavailable"
    assert after["updated_at"] == before, "stamping a skip must not refresh the hold TTL"


def test_swept_run_notifies_the_session_that_launched_it(tmp_path: Path, monkeypatch) -> None:
    """An honest row nobody is told about is still a silent failure.

    A delegated run (``vibe agent run``) reports back to its caller's session when it
    reaches a terminal state. The sweep goes through the same guarded writer, so the
    callback becomes owed automatically — this pins that, because the whole point of
    settling a zombie is that the waiting side stops waiting.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message="summarize the build",
        agent_name="codex",
        callback_session_id="ses_parent",
    )
    _force_run_columns(
        request_store, request.id, status="running", started_at=_ago(900), created_at=_ago(900)
    )
    assert request_store.list_pending_callbacks() == [], "nothing is owed while it runs"

    service._sweep_stale_runs()

    owed = request_store.list_pending_callbacks()
    assert [run["id"] for run in owed] == [request.id]
    assert owed[0]["status"] == "failed"
    assert owed[0]["callback_session_id"] == "ses_parent"


def test_sweep_publishes_a_run_update_event(tmp_path: Path, monkeypatch) -> None:
    """The Runs UI is SSE-driven, so a swept row must announce itself.

    Without the event the run keeps rendering as active until something else happens
    to refresh, which looks exactly like the bug the sweep exists to fix.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from core import inbox_events

    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    run_id = _stage_orphan_run(request_store)

    published: list[tuple[str, dict]] = []
    # The bus is process-global and this subscription is not one-shot, so it has to be
    # removed again or it leaks into every later test in this process.
    sub_id = inbox_events.bus.subscribe_callback(
        lambda event_type, data: published.append((event_type, data))
    )
    try:
        service._sweep_stale_runs()
    finally:
        inbox_events.bus.unsubscribe(sub_id)

    assert published[-1][0] == "runs.updated"
    assert published[-1][1]["run_id"] == run_id
    assert published[-1][1]["status"] == "failed"


def test_sweep_is_rate_limited_to_the_configured_interval(tmp_path: Path, monkeypatch) -> None:
    """The sweep rides a 2 s tick, so its own interval is the only thing bounding cost.

    Without the guard this becomes a full scan of every open run twice a second.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(tmp_path, request_store)
    first = _stage_orphan_run(request_store)

    service._sweep_stale_runs()
    assert request_store.get_run(first)["status"] == "failed"

    second = _stage_orphan_run(request_store, message="a later orphan")
    service._sweep_stale_runs()
    assert request_store.get_run(second)["status"] == "running", "still inside the interval"

    # Rewind the last-sweep stamp instead of the clock: same effect, no time travel.
    service._last_sweep_at -= 61
    service._sweep_stale_runs()
    assert request_store.get_run(second)["status"] == "failed"


def test_sweep_is_disabled_by_a_zero_interval(tmp_path: Path, monkeypatch) -> None:
    """A zero interval is the documented off switch — an operator must be able to stop it."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    service = _sweep_service(
        tmp_path, request_store, _SweepControllerDouble(harness_run_sweep_interval_seconds=0)
    )
    run_id = _stage_orphan_run(request_store)

    service._sweep_stale_runs()

    assert request_store.get_run(run_id)["status"] == "running"


def test_agent_run_with_blank_message_fails_instead_of_hanging(tmp_path: Path, monkeypatch) -> None:
    """A whitespace-only prompt is rejected at the door instead of hanging.

    ``MessageHandler`` returns early for a blank prompt without dispatching an agent,
    so such a run could never receive a terminal result. A row enqueued before this
    guard existed (bypassing ``enqueue_agent_run``) must still terminalize.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()

    with pytest.raises(ValueError):
        request_store.enqueue_agent_run(session_key="slack::channel::C123", message="   \n ")

    legacy = request_store.enqueue(
        TaskExecutionRequest(
            id="blankrun0001",
            request_type="agent_run",
            session_key="slack::channel::C123",
            message="   \n ",
            prompt="   \n ",
            source_kind="cli",
        )
    )
    controller = _SettlementControllerDouble()
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, legacy.id)

    assert controller.turns == []
    settled = request_store.get_run(legacy.id)
    assert settled is not None
    assert settled["status"] == "failed"
    assert settled["completed_at"] is not None


def test_duplicate_result_settles_only_the_exact_agent_run(tmp_path: Path, monkeypatch) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced prompt {index + 1}",
            agent_name="codex",
        )
        run_ids.append(request.id)

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

    claimed = request_store.claim(run_ids[0])
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))
    stored = {run_id: request_store.get_run(run_id) for run_id in run_ids}

    assert stored[run_ids[0]]["status"] == "succeeded"
    assert stored[run_ids[1]]["status"] == "queued"
    assert stored[run_ids[0]]["completed_at"] is not None
    assert stored[run_ids[1]]["completed_at"] is None


def test_recover_processing_preserves_independent_agent_runs(
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
        )
        run_ids.append(request.id)

    for run_id in run_ids:
        assert request_store.claim(run_id) is not None

    request_store.recover_processing()
    pending = request_store.list_pending()

    assert [request.id for request in pending] == run_ids
    assert [_agent_run_message_for_request(request) for request in pending] == [
        "coalesced prompt 1",
        "coalesced prompt 2",
        "coalesced prompt 3",
    ]
    assert all("coalesced_queue" not in request.metadata for request in pending)


def test_recovery_preserves_delivery_owned_agent_run_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from storage.background import (
        attach_agent_run_delivery_in_connection,
        claim_agent_runs_for_turn_in_connection,
    )

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"batched prompt {index + 1}",
            agent_name="codex",
        )
        run_ids.append(request.id)

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id).limit(1)
        ).mappings().first()
        assert session is not None
        deliveries = []
        for run_id in run_ids:
            delivery = message_deliveries.enqueue_queued(
                conn,
                scope_id=session["scope_id"],
                session_id=session_id,
                author="harness",
                source="harness",
                message_type="harness",
                text=f"batched prompt {len(deliveries) + 1}",
                native_message_id=f"agent_run:{run_id}",
            )
            assert attach_agent_run_delivery_in_connection(
                conn,
                run_id,
                session_id=session_id,
                delivery_id=str(delivery["id"]),
            )
            deliveries.append(delivery)
        assert claim_agent_runs_for_turn_in_connection(conn, run_ids) == run_ids
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id=message_deliveries.new_turn_id(),
            session_id=session_id,
            backend="codex",
            deliveries=deliveries,
            dispatch_text="batched prompt 1\n\nbatched prompt 2",
        )

    request_store.recover_processing()

    assert request_store.list_pending() == []
    stored = [request_store.get_run(run_id) for run_id in run_ids]
    assert [row["status"] for row in stored] == ["running", "running"]
    assert [row["delivery_id"] for row in stored] == [
        delivery["id"] for delivery in deliveries
    ]
    with engine.connect() as conn:
        retained = [
            message_deliveries.get_delivery(conn, delivery["id"])
            for delivery in deliveries
        ]
    assert all(row is not None and row["state"] == "claimed" for row in retained)
    assert len({row["turn_id"] for row in retained}) == 1
    assert retained[0]["turn_id"] == claimed["turn"]["id"]


def test_recovered_agent_run_resubmits_through_real_session_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core import internal_server, session_turns

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="recover through the durable gate",
        agent_name="codex",
    )
    engine = create_sqlite_engine()

    sinks: dict[str, dict] = {}

    def _session_key(context):
        return f"avibe::{(context.platform_specific or {}).get('agent_session_id')}"

    def _register_sink(
        key,
        *,
        on_chunk,
        done_event,
        turn_token=None,
        context=None,
    ):
        sinks[key] = {
            "on_chunk": on_chunk,
            "done_event": done_event,
            "turn_token": turn_token,
            "context": context,
        }

    controller = SimpleNamespace(
        platform_settings_managers={},
        im_clients={"avibe": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        message_handler=SimpleNamespace(handle_scheduled_message=AsyncMock()),
        command_handler=SimpleNamespace(handle_stop=AsyncMock(return_value=True)),
        agent_service=SimpleNamespace(default_agent="codex", agents={}, _turn_gates={}),
        config=SimpleNamespace(language="en"),
        _get_lang=lambda: "en",
        _get_session_key=_session_key,
        register_turn_sink=_register_sink,
        get_turn_sink=lambda key: sinks.get(key),
        pop_turn_sink=lambda key, done_event=None: sinks.pop(key, None),
        _session_id_from_context=lambda context: (
            context.platform_specific or {}
        ).get("agent_session_id"),
        resolve_agent_for_context=lambda _context: "codex",
        set_agent_status=lambda *_args: None,
        emit_agent_message=AsyncMock(),
    )
    internal_server.create_app(controller)
    dispatched: list[str] = []

    async def _dispatch(_controller, _context, text, **_kwargs):
        dispatched.append(text)
        logical_turn_id = str(
            (_context.platform_specific or {}).get("turn_token") or ""
        )
        assert logical_turn_id
        _controller.session_turns.on_native_start(
            _context,
            backend="codex",
            runtime_key=f"runtime:{logical_turn_id}",
            runtime_turn_id=f"runtime-turn:{logical_turn_id}",
        )
        return TurnDispatchOutcome(
            error=None,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
        )

    monkeypatch.setattr(session_turns, "dispatch_turn_with_outcome", _dispatch)
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    controller.scheduled_task_service = service

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await execution
        for _ in range(100):
            if not controller.session_turns.in_flight:
                break
            await asyncio.sleep(0)

    asyncio.run(_exercise())

    assert dispatched == ["recover through the durable gate"]
    with engine.connect() as conn:
        stored_run = conn.execute(
            select(agent_runs).where(agent_runs.c.id == request.id)
        ).mappings().one()
        owner = message_deliveries.get_delivery(conn, stored_run["delivery_id"])
        accepted_message = conn.execute(
            select(messages).where(messages.c.id == stored_run["delivery_id"])
        ).mappings().one()
    assert owner is not None
    assert owner["state"] == "accepted"
    assert owner["id"] == accepted_message["id"] == stored_run["delivery_id"]


def test_agent_run_early_failure_settles_only_the_exact_run(tmp_path: Path, monkeypatch) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    run_ids: list[str] = []
    for index in range(2):
        request = request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"independent prompt {index + 1}",
            agent_name="codex",
        )
        run_ids.append(request.id)
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
    assert stored[run_ids[1]]["status"] == "queued"
    assert stored[run_ids[0]]["completed_at"] is not None
    assert stored[run_ids[1]]["completed_at"] is None
    assert stored[run_ids[0]]["error"] == "target session vanished"


def _harness_turn_context(
    *,
    session_id: str,
    run_id: str,
    backend: str,
) -> MessageContext:
    return MessageContext(
        user_id="harness",
        channel_id=session_id,
        platform="avibe",
        message_id=f"agent_run:{run_id}",
        platform_specific={
            "agent_session_id": session_id,
            "task_execution_id": run_id,
            "task_trigger_kind": "agent_run",
            "suppress_delivery": True,
            "vibe_agent_name": "worker",
            "vibe_agent_backend": backend,
        },
    )


def _harness_dispatcher() -> ConsolidatedMessageDispatcher:
    settings_manager = SimpleNamespace(
        _canonicalize_message_type=lambda value: value,
    )
    im_client = SimpleNamespace(
        should_use_thread_for_reply=lambda: False,
        should_use_thread_for_dm_session=lambda: False,
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(
            platform="avibe",
            language="en",
            reply_enhancements=False,
        ),
        get_settings_manager_for_context=lambda _context: settings_manager,
        get_im_client_for_context=lambda _context: im_client,
        _get_settings_key=lambda context: context.channel_id,
        _get_session_key=lambda context: f"avibe::{context.channel_id}",
        agent_service=SimpleNamespace(
            emit_matches_runtime_turn=lambda _context: True,
            release_runtime_turn=lambda _context: None,
        ),
        session_turns=SimpleNamespace(
            on_terminal_result=lambda _context, is_error=False, **_kwargs: None,
        ),
        mark_turn_complete=lambda _context, **_kwargs: None,
    )
    return ConsolidatedMessageDispatcher(controller)


async def _persist_harness_turn(
    *,
    dispatcher: ConsolidatedMessageDispatcher,
    context: MessageContext,
    prompt: str,
    preamble: str,
    terminal: str,
) -> tuple[MessageOutput, MessageOutput, MessageOutput]:
    mirror_harness_inbound(context, prompt)
    preamble_output = MessageOutput(
        completes_turn=False,
        completes_run=False,
        idempotency_key="intermediate-preamble",
    )
    tool_output = MessageOutput(
        completes_turn=False,
        completes_run=False,
        idempotency_key="tool-boundary",
    )
    terminal_output = MessageOutput(
        completes_turn=True,
        completes_run=True,
        idempotency_key="terminal-result",
    )
    await dispatcher.emit_agent_message(
        context,
        "assistant",
        preamble,
        output=preamble_output,
    )
    await dispatcher.emit_agent_message(
        context,
        "toolcall",
        "Tool: vibe data query",
        output=tool_output,
    )
    await dispatcher.emit_agent_message(
        context,
        "result",
        terminal,
        output=terminal_output,
    )
    return preamble_output, tool_output, terminal_output


def _callback_service(
    *,
    tmp_path: Path,
    request_store: TaskExecutionStore,
) -> ScheduledTaskService:
    return ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(
                submit_scheduled=lambda *_args, **_kwargs: None,
                in_flight={},
            ),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )


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


def test_one_terminal_turn_fans_out_each_accepted_run_callback_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Scenario: MESSAGE-DELIVERY-008 closed-loop subscriber fan-out."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-fanout-caller",
    )
    target_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-fanout-target",
    )
    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(
            session_id=target_session_id,
            message=message,
            agent_name="codex",
            callback_session_id=caller_session_id,
        )
        for message in ("participant one", "participant two")
    ]
    for request in requests:
        assert request_store.claim(request.id) is not None

    service = _callback_service(tmp_path=tmp_path, request_store=request_store)
    service.settle_agent_runs_from_terminal_turn(
        [request.id for request in requests],
        turn_id="turn-shared-by-two-runs",
        outcome="completed",
        settled_by="terminal_result",
        evidence_kind="terminal_result",
        evidence={
            "settles_run": True,
            "result_text": "shared immutable terminal result",
        },
    )

    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    originals = [request_store.get_run(request.id) for request in requests]
    assert all(row is not None and row["callback_status"] == "sent" for row in originals)
    callbacks = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback"
    ]
    assert len(callbacks) == 2
    assert {run["parent_run_id"] for run in callbacks} == {
        request.id for request in requests
    }
    assert {run["message"] for run in callbacks} == {
        "shared immutable terminal result"
    }
    assert {
        run["metadata"]["delivery_intent"] for run in callbacks
    } == {"steer"}


def test_historical_conflated_sent_callback_stays_inert_on_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Startup must not replay a historical directed child as a callback."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    parent = request_store.enqueue_agent_run(
        session_id="historical-target",
        message="historical delegated work",
        agent_name="codex",
        callback_session_id="historical-caller",
    )
    directed = request_store.enqueue_agent_run(
        session_id="historical-caller",
        message="historical directed report",
        agent_name="codex",
        source_kind="agent",
        source_actor="historical-target",
        parent_run_id=parent.id,
    )
    sqlite_store = SQLiteBackgroundTaskStore()
    try:
        sqlite_store.record_run_message(
            parent.id,
            text="stale automatic terminal",
            terminal_status="succeeded",
        )
    finally:
        sqlite_store.close()

    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == parent.id)
                .values(
                    callback_status="sent",
                    callback_error="historical conflation",
                    callback_run_id=directed.id,
                    callback_completed_at="2026-07-30T00:00:00Z",
                )
            )
    finally:
        engine.dispose()
    assert request_store.sqlite_backend is not None
    request_store.sqlite_backend.close()

    restarted_store = TaskExecutionStore()
    service = _callback_service(
        tmp_path=tmp_path,
        request_store=restarted_store,
    )

    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    stored_parent = restarted_store.get_run(parent.id)
    stored_directed = restarted_store.get_run(directed.id)
    runs = restarted_store.list_runs()
    assert stored_parent is not None
    assert stored_parent["callback_status"] == "sent"
    assert stored_parent["callback_error"] == "historical conflation"
    assert stored_parent["callback_run_id"] == directed.id
    assert stored_directed is not None
    assert stored_directed["source_kind"] == "agent"
    assert stored_directed["status"] == "queued"
    assert len(runs) == 2
    assert not any(run.get("source_kind") == "callback" for run in runs)
    assert not any(run.get("message") == "stale automatic terminal" for run in runs)
    assert restarted_store.sqlite_backend is not None
    restarted_store.sqlite_backend.close()


def test_directed_run_and_callback_remain_distinct_while_target_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Scenario: MESSAGE-DELIVERY-008."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-caller",
    )
    target_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-target",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=target_session_id,
        message="delegated work",
        agent_name="codex",
        callback_session_id=caller_session_id,
    )
    explicit = request_store.enqueue_agent_run(
        session_id=caller_session_id,
        message="explicit final report",
        agent_name="codex",
        source_kind="agent",
        source_actor=target_session_id,
        parent_run_id=request.id,
        callback_session_id=target_session_id,
    )
    store = SQLiteBackgroundTaskStore()
    try:
        store.record_run_message(
            request.id,
            text="automatic terminal result",
            terminal_status="succeeded",
        )
    finally:
        store.close()
    service = _callback_service(
        tmp_path=tmp_path,
        request_store=request_store,
    )

    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["callback_status"] == "sent"
    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback" and run.get("parent_run_id") == request.id
    ]
    assert len(callback_runs) == 1
    callback = callback_runs[0]
    assert original["callback_run_id"] == callback["id"]
    assert callback["id"] != explicit.id
    assert callback["session_id"] == caller_session_id
    assert callback["status"] == "queued"
    assert callback["started_at"] is None
    assert callback["message"] == "automatic terminal result"
    assert callback["source_actor"] == request.id
    assert callback["callback_session_id"] is None
    assert explicit.callback_session_id == target_session_id
    stored_explicit = request_store.get_run(explicit.id)
    assert stored_explicit is not None
    assert stored_explicit["status"] == "queued"
    assert stored_explicit["started_at"] is None
    assert stored_explicit["message"] == "explicit final report"
    assert stored_explicit["source_kind"] == "agent"
    assert stored_explicit["parent_run_id"] == request.id
    assert stored_explicit["callback_status"] == "pending"
    assert stored_explicit["created_at"] <= callback["created_at"]
    assert [
        run["id"]
        for run in request_store.list_runs(status="queued")
        if run.get("session_id") == caller_session_id
    ] == [explicit.id, callback["id"]]


def test_silent_terminal_skips_callback_and_keeps_directed_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Scenario: MESSAGE-DELIVERY-008."""

    target_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="silent-target",
        agent_backend="claude",
    )
    caller_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="silent-caller",
        agent_backend="codex",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=target_session_id,
        message="delegated work",
        agent_name="worker",
        agent_backend="claude",
        callback_session_id=caller_session_id,
    )
    assert request_store.claim(request.id) is not None
    explicit = request_store.enqueue_agent_run(
        session_id=caller_session_id,
        message="完整定向裁决：精确内容",
        agent_name="worker",
        agent_backend="codex",
        source_kind="agent",
        source_actor=target_session_id,
        parent_run_id=request.id,
    )
    context = _harness_turn_context(
        session_id=target_session_id,
        run_id=request.id,
        backend="claude",
    )
    preamble_output, _tool_output, _terminal_output = asyncio.run(
        _persist_harness_turn(
            dispatcher=_harness_dispatcher(),
            context=context,
            prompt="escalation prompt",
            preamble="裁决如下：",
            terminal="",
        )
    )
    service = _callback_service(
        tmp_path=tmp_path,
        request_store=request_store,
    )

    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["status"] == "succeeded"
    assert original["result_text"] == ""
    assert original["callback_status"] == "skipped"
    assert original["callback_run_id"] is None
    assert [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback"
        and run.get("parent_run_id") == request.id
    ] == []

    stored_explicit = request_store.get_run(explicit.id)
    assert stored_explicit is not None
    assert stored_explicit["status"] == "queued"
    assert stored_explicit["started_at"] is None
    assert stored_explicit["message"] == "完整定向裁决：精确内容"
    assert stored_explicit["source_kind"] == "agent"
    assert stored_explicit["parent_run_id"] == request.id

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        target_messages = conn.execute(
            select(
                messages.c.author,
                messages.c.type,
                messages.c.source,
                messages.c.native_message_id,
                messages.c.content_text,
            )
            .where(messages.c.session_id == target_session_id)
            .order_by(messages.c.created_at, messages.c.id)
        ).mappings().all()
        tool_events = conn.execute(
            select(
                agent_events.c.event_type,
                agent_events.c.source,
                agent_events.c.run_id,
                agent_events.c.content_text,
            ).where(agent_events.c.session_id == target_session_id)
        ).mappings().all()
    assert [row["author"] for row in target_messages] == ["harness", "agent"]
    assert [row["type"] for row in target_messages] == [
        "harness",
        "assistant",
    ]
    assert [row["source"] for row in target_messages] == [
        "harness",
        "agent",
    ]
    assert [row["native_message_id"] for row in target_messages] == [
        f"agent_run:{request.id}",
        preamble_output.native_message_id(context),
    ]
    assert [row["content_text"] for row in target_messages] == [
        "escalation prompt",
        "裁决如下：",
    ]
    assert all(row["author"] != "user" and row["type"] != "user" for row in target_messages)
    assert tool_events == [
        {
            "event_type": "tool_call",
            "source": "agent",
            "run_id": request.id,
            "content_text": "Tool: vibe data query",
        }
    ]


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_callback_consumes_only_full_terminal_once_across_backends(
    tmp_path: Path,
    monkeypatch,
    backend: str,
) -> None:
    """Scenario: MESSAGE-DELIVERY-008."""

    target_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id=f"{backend}-target",
        agent_backend=backend,
    )
    caller_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id=f"{backend}-caller",
        agent_backend=backend,
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=target_session_id,
        message="delegated work",
        agent_name="worker",
        agent_backend=backend,
        callback_session_id=caller_session_id,
    )
    assert request_store.claim(request.id) is not None
    context = _harness_turn_context(
        session_id=target_session_id,
        run_id=request.id,
        backend=backend,
    )
    preamble_output, _tool_output, terminal_output = asyncio.run(
        _persist_harness_turn(
            dispatcher=_harness_dispatcher(),
            context=context,
            prompt="delegated work",
            preamble="The complete ruling follows:",
            terminal="exact terminal body\nlocale.en=Ready\nlocale.zh=就绪",
        )
    )
    service = _callback_service(
        tmp_path=tmp_path,
        request_store=request_store,
    )

    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["status"] == "succeeded"
    assert original["result_text"] == "exact terminal body\nlocale.en=Ready\nlocale.zh=就绪"
    assert original["callback_status"] == "sent"
    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback"
        and run.get("parent_run_id") == request.id
    ]
    assert len(callback_runs) == 1
    callback = callback_runs[0]
    assert callback["id"] == original["callback_run_id"]
    assert callback["session_id"] == caller_session_id
    assert callback["source_actor"] == request.id
    assert callback["message"] == "exact terminal body\nlocale.en=Ready\nlocale.zh=就绪"
    assert callback["status"] == "queued"
    assert callback["started_at"] is None

    claimed_callback = request_store.claim(callback["id"])
    assert claimed_callback is not None
    callback_context = _harness_turn_context(
        session_id=caller_session_id,
        run_id=callback["id"],
        backend=backend,
    )
    mirror_harness_inbound(callback_context, claimed_callback.message)
    mirror_harness_inbound(callback_context, claimed_callback.message)

    engine = create_sqlite_engine()
    with engine.connect() as conn:
        target_messages = conn.execute(
            select(
                messages.c.author,
                messages.c.type,
                messages.c.source,
                messages.c.native_message_id,
                messages.c.content_text,
            )
            .where(messages.c.session_id == target_session_id)
            .order_by(messages.c.created_at, messages.c.id)
        ).mappings().all()
        caller_messages = conn.execute(
            select(
                messages.c.author,
                messages.c.type,
                messages.c.source,
                messages.c.native_message_id,
                messages.c.content_text,
            )
            .where(messages.c.session_id == caller_session_id)
            .order_by(messages.c.created_at, messages.c.id)
        ).mappings().all()
        target_events = conn.execute(
            select(
                agent_events.c.event_type,
                agent_events.c.source,
                agent_events.c.run_id,
                agent_events.c.content_text,
            ).where(agent_events.c.session_id == target_session_id)
        ).mappings().all()
    assert [row["type"] for row in target_messages] == [
        "harness",
        "assistant",
        "result",
    ]
    assert [row["source"] for row in target_messages] == [
        "harness",
        "agent",
        "agent",
    ]
    assert [row["native_message_id"] for row in target_messages] == [
        f"agent_run:{request.id}",
        preamble_output.native_message_id(context),
        terminal_output.native_message_id(context),
    ]
    assert caller_messages == [
        {
            "author": "harness",
            "type": "harness",
            "source": "harness",
            "native_message_id": f"agent_run:{callback['id']}",
            "content_text": "exact terminal body\nlocale.en=Ready\nlocale.zh=就绪",
        }
    ]
    assert all(row["author"] != "user" and row["type"] != "user" for row in caller_messages)
    assert target_events == [
        {
            "event_type": "tool_call",
            "source": "agent",
            "run_id": request.id,
            "content_text": "Tool: vibe data query",
        }
    ]


def test_agent_run_keeps_output_ledger_but_callbacks_only_terminal_result(
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
    assert sqlite_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
        result_text="terminal delegated result",
    ) is True

    first = sqlite_store.record_run_output(
        request.id,
        output_id="terminal-output",
        text="terminal delegated result",
        sequence=1,
        provenance={"run_id": request.id},
    )
    running = request_store.get_run(request.id)

    assert first["recorded"] is True
    assert first["terminal_transition"] is False
    assert running is not None
    assert running["status"] == "running"
    assert not running["result_text"]
    assert running["callback_status"] == "pending"
    assert running["result_payload"]["deferred_terminal_status"] == "succeeded"

    second = sqlite_store.record_run_output(
        request.id,
        output_id="activity-output",
        text="background activity completion",
        sequence=2,
        provenance={"run_id": request.id},
        terminal_status="succeeded",
    )
    terminal = request_store.get_run(request.id)
    assert terminal is not None
    completed_at = terminal["completed_at"]

    duplicate = sqlite_store.record_run_output(
        request.id,
        output_id="activity-output",
        text="background activity completion",
        sequence=2,
        provenance={"run_id": request.id},
        terminal_status="succeeded",
    )
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
    assert "deferred_terminal_result_text" not in original["result_payload"]
    assert original["result_text"] == "terminal delegated result"
    assert [item["id"] for item in original["result_payload"]["outputs"]] == [
        "terminal-output",
        "activity-output",
    ]

    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback" and run.get("parent_run_id") == request.id
    ]
    assert [run["message"] for run in callback_runs] == ["terminal delegated result"]
    assert callback_runs[0]["source_actor"] == request.id


def test_base_agent_terminal_markdown_example_persists_complete_run_and_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Scenario: MESSAGE-DELIVERY-007."""
    from core.services import sessions as sessions_service
    from modules.agents.base import BaseAgent
    from modules.im.formatters.slack_formatter import SlackFormatter

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    callback_session_id = _make_avibe_session(monkeypatch, tmp_path)
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        callback_session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == callback_session_id)
        ).mappings().one()
        target_session = sessions_service.create_session(
            conn,
            scope_id=callback_session["scope_id"],
            agent_backend="codex",
            agent_name="codex",
            visibility="background",
        )

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=target_session["id"],
        message="inspect the callback regression",
        agent_name="codex",
        callback_session_id=callback_session_id,
    )
    assert request_store.claim(request.id) is not None

    class _IMClient:
        formatter = SlackFormatter()

        @staticmethod
        def should_use_thread_for_reply() -> bool:
            return False

        @staticmethod
        async def send_message(context, text, parse_mode=None, reply_to=None):
            return "persisted-result-message"

        @staticmethod
        async def send_message_with_buttons(context, text, keyboard, parse_mode=None):
            return "persisted-result-message"

    class _SettingsManager:
        @staticmethod
        def _canonicalize_message_type(message_type: str) -> str:
            return message_type

        @staticmethod
        def is_message_type_hidden(settings_key: str, message_type: str) -> bool:
            return False

    class _Controller:
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                platform="avibe",
                reply_enhancements=True,
                show_duration=False,
            )
            self.im_client = _IMClient()
            self.settings_manager = _SettingsManager()
            self.session_handler = SimpleNamespace(
                finalize_scheduled_delivery=lambda *_args, **_kwargs: None
            )
            self.dispatcher = ConsolidatedMessageDispatcher(self)

        @staticmethod
        def _get_settings_key(context) -> str:
            return context.channel_id

        @staticmethod
        def _get_session_key(context) -> str:
            return f"avibe::{context.channel_id}"

        @staticmethod
        def get_settings_manager_for_context(context):
            return _SettingsManager()

        def get_im_client_for_context(self, context):
            return self.im_client

        async def emit_agent_message(self, context, message_type, text, **kwargs):
            return await self.dispatcher.emit_agent_message(
                context,
                message_type,
                text,
                **kwargs,
            )

    class _CodexBaseAgent(BaseAgent):
        name = "codex"

        async def handle_message(self, request) -> None:
            return None

    specimen = (
        "Intermediate assistant text must not leave its Session; "
        "the literal directive is `<silent>`.\n\n"
        "2. This substantial trailing section must survive parser cleanup.\n"
        "3. The persisted message, output ledger, and callback must be identical.\n"
        "4. This line proves the live 339-character truncation cannot recur."
    )
    context = MessageContext(
        user_id="scheduled",
        channel_id=target_session["id"],
        platform="avibe",
        platform_specific={
            "agent_session_id": target_session["id"],
            "task_trigger_kind": "agent_run",
            "task_execution_id": request.id,
        },
    )

    asyncio.run(
        _CodexBaseAgent(_Controller()).emit_result_message(
            context,
            specimen,
            duration_ms=0,
        )
    )

    stored_run = request_store.get_run(request.id)
    assert stored_run is not None
    assert stored_run["status"] == "succeeded"
    assert stored_run["result_text"] == specimen
    assert [item["text"] for item in stored_run["result_payload"]["outputs"]] == [
        specimen
    ]
    with engine.connect() as conn:
        persisted_messages = conn.execute(
            select(messages.c.content_text)
            .where(messages.c.session_id == target_session["id"])
            .where(messages.c.type == "result")
        ).scalars().all()
    assert persisted_messages == [specimen]

    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(
                submit_scheduled=lambda *_args, **_kwargs: None,
                in_flight={},
            ),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    asyncio.run(service._drain_callbacks())

    completed_run = request_store.get_run(request.id)
    assert completed_run is not None
    assert completed_run["callback_status"] == "sent"
    callback_run = request_store.get_run(completed_run["callback_run_id"])
    assert callback_run is not None
    assert callback_run["source_kind"] == "callback"
    assert callback_run["parent_run_id"] == request.id
    assert callback_run["message"] == specimen


def test_settled_deferred_run_delivers_saved_terminal_result(
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
    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="succeeded",
        result_text="terminal delegated result",
    ) is True

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    sqlite_store.record_run_output(
        request.id,
        output_id="activity-output",
        text="intermediate activity output",
    )
    assert request_store.settle_deferred_run(request.id) is True
    asyncio.run(service._drain_callbacks())

    original = request_store.get_run(request.id)
    assert original is not None
    assert original["result_text"] == "terminal delegated result"
    assert "deferred_terminal_result_text" not in original["result_payload"]
    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback" and run.get("parent_run_id") == request.id
    ]
    assert [run["message"] for run in callback_runs] == ["terminal delegated result"]


@pytest.mark.parametrize(
    ("terminal_status", "expected_message"),
    [
        ("failed", "Error: backend disconnected"),
        ("canceled", "The run was canceled before producing a result."),
    ],
)
def test_agent_run_callbacks_only_terminal_status_after_partial_output(
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
        text="intermediate activity output",
        sequence=1,
        provenance={"run_id": request.id},
    )
    assert recorded["recorded"] is True
    assert not request_store.get_run(request.id)["result_text"]
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
    assert [run["message"] for run in callback_runs] == [expected_message]
    assert callback_runs[0]["source_actor"] == f"{request.id}:terminal:{terminal_status}"
    assert original["callback_run_id"] == callback_runs[0]["id"]


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


def test_claude_terminal_output_records_assistant_text_not_tool_description(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id="target-session",
        message="delegated work",
        agent_name="claude",
        callback_session_id=caller_session_id,
    )
    assert request_store.claim(request.id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    assistant_text = (
        "Two clean commits. Let me push the branch and confirm the repo/remote "
        "for the PR."
    )

    recorded = sqlite_store.record_run_output(
        request.id,
        output_id="terminal",
        text=assistant_text,
        provenance={"backend": "claude", "run_id": request.id},
        terminal_status="succeeded",
    )

    terminal = request_store.get_run(request.id)
    assert terminal is not None
    assert recorded["terminal_transition"] is True
    assert terminal["result_text"] == assistant_text
    assert [item["text"] for item in terminal["result_payload"]["outputs"]] == [
        assistant_text
    ]
    assert "Push branch, confirm repo" not in terminal["result_text"]

    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=SimpleNamespace(
                submit_scheduled=lambda *_args, **_kwargs: None,
                in_flight={},
            ),
            handle_scheduled_message=lambda *_args, **_kwargs: None,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    asyncio.run(service._drain_callbacks())
    asyncio.run(service._drain_callbacks())

    callback_runs = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback"
        and run.get("parent_run_id") == request.id
    ]
    assert [run["message"] for run in callback_runs] == [assistant_text]


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


def test_restart_background_activity_persists_without_outward_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        visibility="background",
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
    async def emit_background(context, *_args, **_kwargs):
        assert context.platform_specific["suppress_delivery"] is True
        assert request_store.settle_deferred_run(request.id) is True
        return "msg-background"

    controller.emit_agent_message = AsyncMock(side_effect=emit_background)
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
    controller.emit_agent_message.assert_awaited_once()
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
    sqlite_backend = request_store.sqlite_backend
    assert sqlite_backend is not None
    with sqlite_backend.engine.begin() as conn:
        metadata = json.loads(
            conn.execute(
                select(agent_runs.c.metadata_json).where(agent_runs.c.id == request.id)
            ).scalar_one()
        )
        metadata["delivery_intent"] = "queue"
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == request.id)
            .values(metadata_json=json.dumps(metadata))
        )
    submitted: list[tuple] = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="steer"):
        submitted.append(
            (
                sid,
                text,
                ctx.platform,
                ctx.platform_specific.get("task_execution_id"),
                delivery_intent,
            )
        )
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
    assert submitted == [
        (session_id, "run in workbench session", "avibe", request.id, "queue")
    ]
    assert handler_calls == []


def test_explicit_queue_delivery_intent_remains_queued() -> None:
    assert normalize_agent_run_delivery_intent("queue") == "queue"


def test_busy_avibe_agent_run_send_now_keeps_transferred_owner_running(monkeypatch, tmp_path) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="run behind active workbench turn",
        agent_name="codex",
        delivery_intent="send_now",
    )
    submitted: list[tuple] = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="queue"):
        from core.session_turns import TurnSubmissionResult

        submitted.append(
            (
                sid,
                text,
                ctx.platform_specific.get("task_execution_id"),
                delivery_intent,
            )
        )
        return TurnSubmissionResult(
            route="enqueued",
            queue_persisted=True,
            target_was_busy=True,
            delivery_status="interrupted",
            delivery_owner_transferred=True,
        )

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
    assert run["status"] == "running"
    assert run.get("started_at") is not None
    assert run.get("completed_at") is None
    assert "workbench_queue_holds_run" not in (run.get("metadata") or {})
    assert run["metadata"]["delivery_intent"] == "send_now"
    assert submitted == [
        (session_id, "run behind active workbench turn", request.id, "send_now")
    ]
    assert handler_calls == []


def test_idle_send_now_flush_failure_invokes_queue_recovery(monkeypatch, tmp_path) -> None:
    from core.session_turns import TurnSubmissionResult

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="recover the held queue",
        agent_name="codex",
        delivery_intent="send_now",
    )
    recovered: list[str] = []

    async def _submit_scheduled(_sid, _ctx, _text, *, delivery_intent="queue"):
        request_store.requeue(
            request.id,
            metadata={
                "workbench_queue_holds_run": True,
                "delivery_outcome": {
                    "intent": "send_now",
                    "status": "flush_failed",
                    "target_was_busy": False,
                },
            },
        )
        return TurnSubmissionResult(
            route="enqueued",
            queue_persisted=True,
            target_was_busy=False,
            delivery_status="flush_failed",
            delivery_owner_transferred=True,
        )

    async def _recover_queue(sid):
        recovered.append(sid)
        return []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        raise AssertionError("send-now must not use direct IM dispatch")

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(
        gate=gate,
        handle_scheduled_message=_handle_scheduled_message,
    )
    controller.session_turns = SimpleNamespace(
        recover_persisted_agent_run_queue=_recover_queue,
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "queued"
    assert stored["metadata"]["workbench_queue_holds_run"] is True
    assert stored["metadata"]["delivery_outcome"]["status"] == "flush_failed"
    assert recovered == [session_id]


def test_send_now_runtime_rejects_non_workbench_session(monkeypatch, tmp_path) -> None:
    from core.services import sessions as sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import scope_settings
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state(primary_platform="slack")
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="slack",
            scope_type="channel",
            native_id="C_SEND_NOW",
            now="2026-07-30T00:00:00Z",
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
                created_at="2026-07-30T00:00:00Z",
                updated_at="2026-07-30T00:00:00Z",
            )
        )
        session_id = sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="worker",
        )["id"]

    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="do not silently downgrade",
        agent_name="worker",
        delivery_intent="send_now",
    )
    direct_calls: list[str] = []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        direct_calls.append(message)

    controller = SimpleNamespace(
        platform_settings_managers={"slack": object()},
        im_clients={"slack": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        session_turn_gate=SimpleNamespace(submit_scheduled=AsyncMock(), in_flight={}),
        message_handler=SimpleNamespace(
            handle_scheduled_message=_handle_scheduled_message,
        ),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "failed"
    assert "Web/Workbench Agent Session" in stored["error"]
    assert stored["metadata"]["delivery_outcome"] == {
        "intent": "send_now",
        "status": "unsupported_target",
        "target_was_busy": False,
    }
    assert direct_calls == []
    controller.session_turn_gate.submit_scheduled.assert_not_awaited()


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
    assert "workbench_queue_holds_run" not in run["metadata"]
    assert submitted[0][2]["source_native_session_id"] == "thread-source"


def test_delivery_claim_preserves_session_fork_metadata(monkeypatch, tmp_path) -> None:
    from storage.background import attach_agent_run_delivery_in_connection

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
        },
    )

    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            author="harness",
            source="harness",
            message_type="harness",
            text="recover fork after queue flush",
            native_message_id=f"agent_run:{request.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            request.id,
            session_id=session_id,
            delivery_id=str(delivery["id"]),
        )
    assert sqlite_store.claim_agent_run_for_turn(request.id) is True

    flushed = request_store.get_run(request.id)
    assert flushed is not None
    assert flushed["status"] == "running"
    assert flushed["delivery_id"] == delivery["id"]
    assert "workbench_queue_holds_run" not in flushed["metadata"]
    assert flushed["metadata"]["session_fork"]["source_native_session_id"] == "thread-source"

    request_store.recover_processing()
    recovered = request_store.get_run(request.id)
    assert recovered is not None
    assert recovered["status"] == "running"
    assert recovered["delivery_id"] == delivery["id"]
    assert recovered["metadata"]["session_fork"]["source_native_session_id"] == "thread-source"


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

    queued_run_ids, stale_run_ids = bg.inspect_agent_runs_for_turn([request.id])

    assert queued_run_ids == []
    assert stale_run_ids == [request.id]
    stored = bg.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["completed_at"] is not None


def test_claim_queued_runs_publishes_after_commit(monkeypatch, tmp_path) -> None:
    import storage.background as background_module
    from storage.background import attach_agent_run_delivery_in_connection

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="queued run",
        agent_name="codex",
    )
    bg = request_store._sqlite
    assert bg is not None
    with create_sqlite_engine().begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        delivery = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            author="harness",
            source="harness",
            message_type="harness",
            text="queued run",
            native_message_id=f"agent_run:{request.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            request.id,
            session_id=session_id,
            delivery_id=str(delivery["id"]),
        )

    observed_statuses: list[str | None] = []

    def capture_publish(_rows):
        stored = bg.get_run(request.id)
        observed_statuses.append(stored["status"] if stored else None)

    monkeypatch.setattr(background_module, "_publish_run_rows_updated", capture_publish)

    assert bg.claim_agent_runs_for_turn([request.id]) == [request.id]
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
            "_capture_dispatch_result": True,
        }
    ]
    payload = json.loads((request_store.completed_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["session_id"] == "ses-created"
    assert payload["session_key"] == ""


def test_claimed_watch_stays_nonterminal_after_delivery_ownership_transfer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_hook_send(
        session_key="",
        session_id=session_id,
        prompt="watch result",
        agent_name="release-reviewer",
        run_type="watch",
    )
    submitted: list[tuple[str, str, str]] = []

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="steer"):
        from core.session_turns import TurnSubmissionResult

        submitted.append(
            (
                sid,
                text,
                str(ctx.platform_specific.get("task_execution_id") or ""),
                delivery_intent,
            )
        )
        return TurnSubmissionResult(
            route="enqueued",
            queue_persisted=True,
            target_was_busy=True,
            delivery_owner_transferred=True,
        )

    async def _handle_scheduled_message(*_args, **_kwargs):
        raise AssertionError("Workbench Watch input must use the Delivery gate")

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    service = ScheduledTaskService(
        controller=_avibe_controller_double(
            gate=gate,
            handle_scheduled_message=_handle_scheduled_message,
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await execution

    asyncio.run(_exercise())

    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored.get("completed_at") is None
    assert submitted == [(session_id, "watch result", request.id, "steer")]


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
            "_capture_dispatch_result": True,
        }
    ]
    payload = json.loads((request_store.completed_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["session_id"] == "ses-created"
    assert payload["session_key"] == ""


def test_claimed_request_refreshes_agent_name_after_archive(monkeypatch, tmp_path: Path) -> None:
    from core.vibe_agents import VibeAgentStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    agent_store = VibeAgentStore()
    try:
        agent = agent_store.create(name="pm", backend="claude")
        agent_store.create(name="zz-fallback", backend="claude")
        request_store = TaskExecutionStore()
        request = request_store.enqueue_hook_send(
            session_key="slack::channel::C123",
            prompt="continue",
            agent_name=agent.name,
        )
        claimed = request_store.claim(request.id)
        assert claimed is not None
        assert claimed.agent_name == "pm"

        archived = agent_store.archive("pm")
        assert archived is not None
        calls: list[dict[str, Any]] = []
        service = ScheduledTaskService(
            controller=SimpleNamespace(),
            store=ScheduledTaskStore(),
            request_store=request_store,
        )

        async def _execute_request(**kwargs):
            calls.append(kwargs)
            return None

        service._execute_request = _execute_request  # type: ignore[method-assign]
        asyncio.run(service._execute_claimed_request(claimed))

        assert len(calls) == 1
        assert calls[0]["agent_name"] == archived.archived_name
    finally:
        agent_store.close()


def test_claimed_request_keeps_agent_identity_when_archive_lands_after_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    from core.vibe_agents import VibeAgentStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    agent_store = VibeAgentStore()
    try:
        agent = agent_store.create(name="pm", backend="claude")
        agent_store.create(name="zz-fallback", backend="claude")
        request_store = TaskExecutionStore()
        request = request_store.enqueue_hook_send(
            session_key="slack::channel::C123",
            prompt="continue",
            agent_name=agent.name,
        )
        claimed = request_store.claim(request.id)
        assert claimed is not None

        original_refresh = request_store.refresh_claimed_request
        archived_result = None

        def _refresh_then_archive(item):
            nonlocal archived_result
            refreshed = original_refresh(item)
            assert refreshed.agent_id == agent.id
            archived_result = agent_store.archive(agent.name)
            return refreshed

        request_store.refresh_claimed_request = _refresh_then_archive  # type: ignore[method-assign]
        calls: list[dict[str, Any]] = []
        service = ScheduledTaskService(
            controller=SimpleNamespace(),
            store=ScheduledTaskStore(),
            request_store=request_store,
        )

        async def _execute_request(**kwargs):
            calls.append(kwargs)
            return None

        service._execute_request = _execute_request  # type: ignore[method-assign]
        asyncio.run(service._execute_claimed_request(claimed))

        assert archived_result is not None
        assert calls == [
            {
                "session_key": "slack::channel::C123",
                "session_id": None,
                "post_to": None,
                "deliver_key": None,
                "prompt": "continue",
                "execution_id": request.id,
                "task_id": None,
                "trigger_kind": "hook",
                "agent_name": "pm",
                "_capture_dispatch_result": True,
                "agent_id": agent.id,
            }
        ]
        assert agent_store.require_reference_by_id(agent.id).name == archived_result.archived_name
    finally:
        agent_store.close()


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
    # The fake handler releases the turn sink without emitting a terminal result, so
    # no out-of-band writer will ever settle this run. The legacy file store has no
    # guarded writer, so the run is completed here rather than left in ``processing``
    # forever. See docs/plans/agent-run-zombie-settlement.md.
    assert not (request_store.processing_dir / f"{request.id}.json").exists()
    payload = json.loads((request_store.completed_dir / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["request_type"] == "agent_run"
    assert payload["ok"] is False
    assert "Agent producing a result" in payload["error"]


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
    visibility: str = "foreground",
    scope_native_id: str = "proj_gate_exec",
    platform: str = "avibe",
    scope_type: str = "project",
    agent_backend: str = "claude",
    agent_name: str = "worker",
) -> str:
    """Create a real persisted Session target for delivery-gate tests."""
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
            conn,
            platform=platform,
            scope_type=scope_type,
            native_id=scope_native_id,
            now="2026-05-31T00:00:00Z",
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
            agent_backend=agent_backend,
            agent_name=agent_name,
            visibility=visibility,
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

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="steer"):
        submitted.append((sid, text, getattr(ctx, "platform", None), delivery_intent))

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
    assert submitted == [
        (session_id, "run the digest", "avibe", "queue")
    ], "routed through the turn gate"
    assert handler_calls == [], "the direct handle_scheduled_message path is bypassed for avibe"


def test_execute_request_im_watch_steers_through_delivery_owner(
    monkeypatch,
    tmp_path,
) -> None:
    """An IM Watch uses the same durable P1 owner as every Session target."""
    session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        platform="slack",
        scope_type="channel",
        scope_native_id="C123",
    )
    submitted: list = []
    handler_calls: list = []

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="steer"):
        submitted.append((sid, text, delivery_intent))

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
            trigger_kind="watch",
            session_id=session_id,
        )
    )

    assert error is None
    assert submitted == [(session_id, "send digest", "steer")]
    assert handler_calls == []


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


def test_dead_accepted_owner_converges_run_session_and_persisted_fifo(
    monkeypatch,
    tmp_path,
) -> None:
    """HFR-002: one dead accepted owner releases every shared ownership layer."""

    from core.internal_server import create_app
    from core.message_output import terminal_output_for
    from core.session_turns import SessionTurnManager, emit_matches_active_turn
    from modules.agents.base import AgentRequest
    from modules.agents.service import AgentService

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    first = request_store.enqueue_agent_run(
        session_id=session_id,
        message="first",
        agent_name="worker",
    )
    second = request_store.enqueue_agent_run(
        session_id=session_id,
        message="second",
        agent_name="worker",
    )
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    engine = create_sqlite_engine()

    def _delivery_state(delivery_id: str) -> str | None:
        with engine.connect() as conn:
            delivery = message_deliveries.get_delivery(conn, delivery_id)
        return str(delivery["state"]) if delivery is not None else None

    class _Controller:
        primary_platform = "avibe"

        def __init__(self) -> None:
            self.session_turns = SessionTurnManager(self)
            self.session_turn_gate = None
            self.agent_service = AgentService(self)
            self.platform_settings_managers = {}
            self.im_clients = {"avibe": SimpleNamespace()}
            self.statuses: list[tuple[str, str]] = []
            self.terminal_transitions: list[str] = []

        @staticmethod
        def _get_session_key(context) -> str:
            return f"avibe::{context.channel_id}"

        @staticmethod
        def _session_id_from_context(context) -> str | None:
            payload = getattr(context, "platform_specific", None) or {}
            value = payload.get("agent_session_id")
            return str(value) if value else None

        def get_turn_sink(self, session_key):
            return self.session_turns.get_turn_sink(session_key)

        def register_turn_sink(self, session_key, **kwargs) -> None:
            self.session_turns.register_turn_sink(session_key, **kwargs)

        def pop_turn_sink(self, session_key, done_event=None) -> None:
            self.session_turns.pop_turn_sink(session_key, done_event)

        def mark_turn_complete(self, context) -> None:
            sink = self.get_turn_sink(self._get_session_key(context))
            if sink is not None and emit_matches_active_turn(sink, context):
                sink["done_event"].set()

        def set_agent_status(self, target_session_id, status) -> None:
            self.statuses.append((target_session_id, status))

        def backend_alive(self, context):
            return self.agent_service.backend_alive(context)

        def get_im_client_for_context(self, _context):
            return SimpleNamespace(
                should_use_thread_for_reply=lambda: True,
                should_use_thread_for_dm_session=lambda: False,
            )

        async def emit_agent_message(
            self,
            context,
            message_type,
            text,
            *,
            output,
            is_error=False,
            terminal_error=None,
            **_kwargs,
        ):
            assert message_type == "result"
            if not self.agent_service.emit_matches_runtime_turn(context):
                return None
            self.session_turns.on_terminal_result(context, is_error=is_error)
            run_id = str((context.platform_specific or {}).get("task_execution_id") or "")
            result = sqlite_store.record_run_output(
                run_id,
                output_id="terminal",
                text=text,
                terminal_status=("failed" if is_error else "succeeded") if output.settles_run else None,
                error=terminal_error,
                provenance=output.provenance(context),
            )
            if result["terminal_transition"]:
                self.terminal_transitions.append(run_id)
            self.mark_turn_complete(context)
            self.agent_service.release_runtime_turn(context)
            self.session_turns.on_terminal_delivery_complete(context)
            return None

    controller = _Controller()

    class _AcceptedBackend:
        name = "claude"

        def __init__(self) -> None:
            self.first_alive = True
            self.started: list[str] = []

        @staticmethod
        def runtime_turn_key(request) -> str:
            return request.composite_session_id

        def backend_alive(self, context):
            run_id = str((context.platform_specific or {}).get("task_execution_id") or "")
            return self.first_alive if run_id == first.id else True

        async def handle_message(self, request) -> None:
            run_id = str((request.context.platform_specific or {}).get("task_execution_id") or "")
            self.started.append(run_id)
            controller.agent_service.mark_runtime_turn_started(request.context)
            if run_id == second.id:
                await controller.emit_agent_message(
                    request.context,
                    "result",
                    "second completed",
                    output=terminal_output_for(request),
                )

    backend = _AcceptedBackend()
    controller.agent_service.register(backend)
    controller.agent_service._liveness_probe_interval_seconds = 0.005
    controller.agent_service._liveness_failure_grace_seconds = 0.005

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        del parsed_session_key
        await controller.agent_service.handle_message(
            "claude",
            AgentRequest(
                context=context,
                message=message,
                user_message=message,
                working_path=str(tmp_path),
                base_session_id=session_id,
                composite_session_id=f"{session_id}:{tmp_path}",
                session_key=controller._get_session_key(context),
            ),
        )

    controller.message_handler = SimpleNamespace(
        handle_scheduled_message=_handle_scheduled_message,
    )
    create_app(controller)
    scheduled = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _wait_for(predicate, *, timeout=1.0) -> None:
        async def _poll() -> None:
            while not predicate():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(_poll(), timeout=timeout)

    async def _exercise() -> None:
        await scheduled._drain_requests()
        await _wait_for(lambda: backend.started == [first.id])
        await _wait_for(lambda: first.id not in scheduled._inflight_executions)

        await scheduled._drain_requests()
        await _wait_for(
            lambda: request_store.get_run(second.id).get("delivery_id") is not None
        )
        second_delivery_id = request_store.get_run(second.id)["delivery_id"]
        await _wait_for(
            lambda: _delivery_state(second_delivery_id) == "pending_steer"
        )
        state = controller.session_turns.turn_state(session_id)
        assert state["owner"]["run_id"] == first.id
        assert state["pending_input_count"] == 0
        assert request_store.get_run(first.id)["status"] == "running"
        assert request_store.get_run(second.id)["status"] == "running"

        backend.first_alive = False
        await _wait_for(lambda: request_store.get_run(second.id)["status"] == "succeeded")
        await _wait_for(lambda: session_id not in controller.session_turns.in_flight)

        assert backend.started == [first.id, second.id]
        assert controller.terminal_transitions == [first.id, second.id]
        assert request_store.get_run(first.id)["status"] == "failed"
        assert request_store.get_run(first.id)["error"] == (
            "backend_runtime_exited_before_terminal"
        )
        assert request_store.get_run(second.id)["result_text"] == "second completed"
        assert controller.session_turns.turn_state(session_id)["pending_input_count"] == 0

    asyncio.run(_exercise())


# --- P5 (PR5): a pinned session binding must not break permanently ---
# Scenario IDs: HFR-054 (auto-pause backstop) / HFR-055 (create_once rebind)
# / HFR-056 (D3 settings preservation).


def _binding_env(tmp_path: Path, monkeypatch, *, backends=("claude", "codex"), default="codex") -> Path:
    """Migrated DB + enabled Agents + a slack channel scope, for binding tests."""
    db_path = tmp_path / "state" / "vibe.sqlite"
    monkeypatch.setattr(paths, "get_state_dir", lambda: db_path.parent)
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.ensure_builtin_default_agents(list(backends))
        agent_store.set_default_agent_name(default)
    finally:
        agent_store.close()
    with create_sqlite_engine(db_path).begin() as conn:
        upsert_scope(conn, "slack", "channel", "C123", now="2026-07-27T00:00:00Z")
    return db_path


def _binding_service(
    tmp_path: Path,
    store: ScheduledTaskStore,
    calls: list,
    *,
    language: str = "en",
) -> ScheduledTaskService:
    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        calls.append(message)
        return None

    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_kw: None))
    controller = SimpleNamespace(
        config=SimpleNamespace(language=language),
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )
    service.scheduler = _StubScheduler()
    return service


#: A system prompt no default fixture would produce, so "the fallback Agent's
#: prompt reached the request" cannot pass on a None == None comparison.
_FALLBACK_AGENT_SYSTEM_PROMPT = "You are the scope default Agent. Answer tersely."


class _DispatchIMClient:
    """The minimum IM surface ``MessageHandler`` touches on a scheduled turn."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.formatter = SimpleNamespace(format_error=lambda text: text)

    def should_use_thread_for_reply(self) -> bool:
        return True

    def should_use_thread_for_dm_session(self) -> bool:
        return False

    def should_use_message_id_for_channel_session(self, _context=None) -> bool:
        return True

    async def prepare_turn_context(self, context, source):
        return context

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        self.sent.append(text)
        return "msg-1"


class _CapturingAgentService:
    """Stands in for the backend registry and records what dispatch received.

    The recorded ``request`` is the REAL ``modules.agents.base.AgentRequest`` the
    real ``MessageHandler`` built — the values under test (Agent identity, model,
    reasoning effort, system prompt) are re-derived there, downstream of both the
    stored session row and the scheduler's context payload.
    """

    def __init__(self) -> None:
        self.default_agent = "codex"
        self.agents: dict = {}
        self.dispatched: list = []

    async def handle_message(self, agent_name, request):
        self.dispatched.append((agent_name, request))
        return None


class _DispatchSessionHandler:
    def __init__(self, working_path: str) -> None:
        self.working_path = working_path

    def get_session_info(self, context, source="human"):
        base = "slack_C123"
        return (base, self.working_path, f"{base}:{self.working_path}")

    @staticmethod
    def should_allocate_scheduled_anchor(context, source="human") -> bool:
        return False

    @staticmethod
    def alias_session_base(context, *, source_base_session_id, alias_base_session_id, clear_source=False):
        return False


class _DispatchController:
    """Controller double wired for the REAL ``MessageHandler`` dispatch path.

    Only the collaborators ``MessageHandler`` actually reaches on a scheduled
    turn are doubled. Agent identity resolution deliberately is NOT: it runs
    against the real :class:`~core.vibe_agents.VibeAgentStore` on the test DB,
    because which VibeAgent the turn lands on is exactly what is under test.
    """

    def __init__(self, db_path: Path, working_path: Path) -> None:
        from core.processing_indicator import ProcessingIndicatorService
        from storage.sessions_service import SQLiteSessionsService

        self.db_path = db_path
        self.config = SimpleNamespace(
            platform="slack",
            ack_mode="reaction",
            include_time_info=False,
            include_user_info=False,
            language="en",
        )
        self.im_client = _DispatchIMClient()
        self.sessions = SQLiteSessionsService(db_path)
        self.settings_manager = SimpleNamespace(
            sessions=self.sessions,
            get_channel_routing=lambda _settings_key: None,
            get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_kw: None),
        )
        self.platform_settings_managers = {"slack": self.settings_manager}
        self.session_manager = SimpleNamespace()
        self.receiver_tasks: dict = {}
        self.agent_service = _CapturingAgentService()
        # Reached only on HUMAN turns (the Workbench dispatch path); a bare
        # namespace makes the handler's ``getattr`` probes miss and move on.
        self.agent_auth_service = SimpleNamespace()
        self.primary_platform = "slack"
        from core.vibe_agents import VibeAgentStore

        self.vibe_agent_store = VibeAgentStore(db_path)
        self.processing_indicator = ProcessingIndicatorService(self)
        self.completed_turns: list = []

    # -- VibeAgent resolution: the REAL controller methods, not a mirror --------
    #
    # Bound straight off ``Controller`` rather than reimplemented here. The
    # precedence between an override name, ``agent_run_target`` /
    # ``agent_session_target`` and channel routing IS the thing under test, so a
    # hand-written copy could agree with the test and disagree with production --
    # the proxy pattern these regressions exist to close. Only the collaborators
    # they read (`vibe_agent_store`, `_get_settings_key`,
    # `get_settings_manager_for_context`, `primary_platform`) are doubled, and
    # the store underneath is the real one on the test DB.
    resolve_vibe_agent_for_context = Controller.resolve_vibe_agent_for_context
    resolve_agent_for_context = Controller.resolve_agent_for_context
    # Re-wrapped: accessing it off ``Controller`` resolves the descriptor to a
    # plain function, which would rebind as an instance method here.
    _agent_run_target_payload = staticmethod(Controller._agent_run_target_payload)

    # -- misc controller surface ----------------------------------------------

    def get_im_client_for_context(self, context):
        return self.im_client

    def get_settings_manager_for_context(self, context):
        return self.settings_manager

    def _get_settings_key(self, context) -> str:
        from core.message_context import resolve_context_scope_settings_key

        return resolve_context_scope_settings_key(context)

    def _get_session_key(self, context) -> str:
        from core.message_context import build_context_session_key, resolve_context_settings_key

        platform = context.platform or (context.platform_specific or {}).get("platform") or "slack"
        return build_context_session_key(
            context, platform=platform, settings_key=resolve_context_settings_key(context)
        )

    def _get_lang(self) -> str:
        return "en"

    def update_thread_message_id(self, context):
        return None

    def mark_turn_complete(self, context) -> None:
        self.completed_turns.append(context)

    async def emit_agent_message(self, context, message_type, text, parse_mode="markdown", **_kwargs):
        return None


def _dispatching_binding_service(
    tmp_path: Path, store: ScheduledTaskStore, *, db_path: Path
) -> ScheduledTaskService:
    """``_binding_service`` with the PRODUCTION dispatch path attached.

    ``_binding_service`` replaces ``handle_scheduled_message`` with a double that
    records the prompt string. That proves a run fired, but every value the
    binding-recovery tests care about — which VibeAgent the turn runs as, its
    backend, its system prompt, its model / reasoning effort — is re-derived
    INSIDE ``MessageHandler`` from the session row and the scheduler's context
    payload, i.e. strictly downstream of anything the prompt-recording double can
    observe. So the real handler is wired in here and the assertions move to the
    ``AgentRequest`` it hands to ``AgentService.handle_message``.
    """
    from core.handlers.message_handler import MessageHandler

    working_path = tmp_path / "workdir"
    working_path.mkdir(parents=True, exist_ok=True)
    controller = _DispatchController(db_path, working_path)
    handler = MessageHandler(controller)
    handler.set_session_handler(_DispatchSessionHandler(str(working_path)))
    controller.message_handler = handler
    controller.session_handler = handler.session_handler

    service = ScheduledTaskService(
        controller=controller,
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )
    service.scheduler = _StubScheduler()
    return service


def _spy_binding_notices(service: ScheduledTaskService) -> list:
    notices: list = []
    original = service._notify_binding_change

    async def _spy(task, change):
        notices.append(change)
        return await original(task, change)

    service._notify_binding_change = _spy  # type: ignore[method-assign]
    return notices


def test_execute_task_notifies_then_pauses_after_three_unresolvable_failures(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-054 — an unresolvable pinned session must not fire forever.

    ``resolve_session_id_target`` raises, ``_execute_task`` records the error and
    leaves ``enabled=1``, so the first failure must be visible while the stable
    failure code drives the three-consecutive-failure auto-pause policy.
    """
    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
    )
    calls: list = []
    service = _binding_service(tmp_path, store, calls)
    notices = _spy_binding_notices(service)

    for attempt in range(1, 4):
        asyncio.run(service._run_task(task.id))
        current = store.get_task(task.id)
        assert current is not None
        assert current.enabled is (attempt < 3)

    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.enabled is False, "a permanently broken binding kept firing"
    assert updated.last_error
    assert "sesdoesnotexist" in updated.last_error
    assert not calls
    assert [notice.action for notice in notices] == ["failing", "paused"]
    failed_runs = service.request_store.list_runs(status="failed")
    assert len(failed_runs) == 3
    assert {(run.get("metadata") or {}).get("failure_code") for run in failed_runs} == {"unresolvable_target"}


@pytest.mark.parametrize(
    ("language", "retry_copy", "paused_copy"),
    [
        ("en", "cannot accept a turn (1/3)", "paused: pinned agent session"),
        ("zh", "无法接收请求(1/3)", "已暂停:绑定的 Agent 会话"),
    ],
)
def test_unresolvable_target_errors_follow_the_configured_language(
    tmp_path: Path,
    monkeypatch,
    language: str,
    retry_copy: str,
    paused_copy: str,
) -> None:
    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
    )
    service = _binding_service(tmp_path, store, [], language=language)

    asyncio.run(service._run_task(task.id))
    retry_error = store.get_task(task.id).last_error
    assert retry_copy in retry_error
    assert "sesdoesnotexist" in retry_error
    assert f"vibe task update {task.id} --session-id <id>" in retry_error

    asyncio.run(service._run_task(task.id))
    asyncio.run(service._run_task(task.id))
    paused_error = store.get_task(task.id).last_error
    assert paused_copy in paused_error
    assert "sesdoesnotexist" in paused_error
    assert f"vibe task resume {task.id}" in paused_error


def test_existing_policy_never_rebinds(tmp_path: Path, monkeypatch) -> None:
    """HFR-054 — ``existing`` is user-pinned: pause and notify, never re-point.

    Silently reserving a different session for a user-pinned task would lose the
    continuity the pin exists to guarantee.
    """
    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    service = _binding_service(tmp_path, store, [])

    for _ in range(3):
        asyncio.run(service._run_task(task.id))

    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.session_id == "sesdoesnotexist"
    assert updated.enabled is False


def test_create_once_rebinds_when_session_deleted(tmp_path: Path, monkeypatch) -> None:
    """HFR-055 — ``create_once`` reserved its own session, so it may re-reserve.

    ``/new`` hard-deletes the session a ``create_once`` definition reserved at
    definition time and nothing updates ``run_definitions.session_id``. The
    definition re-reserves, keeps running, and always says so.
    """
    db_path = _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    calls: list = []
    service = _binding_service(tmp_path, store, calls)
    notices = _spy_binding_notices(service)

    asyncio.run(service._run_task(task.id))

    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.enabled is True, "a rebindable definition was paused"
    assert updated.session_id and updated.session_id != "sesdoesnotexist"
    resolve_session_id_target(updated.session_id, db_path=db_path)
    assert calls == ["send digest"], "the rebound run never executed"
    assert len(notices) == 1
    assert notices[0].action == "rebound"


def test_repeated_binding_failures_notify_only_on_state_transitions(tmp_path: Path, monkeypatch) -> None:
    """HFR-054 — one broken binding is not one notification per fire.

    The first failing transition and the eventual pause are distinct; the middle
    identical failure is deduplicated.
    """
    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
    )
    service = _binding_service(tmp_path, store, [])
    notices = _spy_binding_notices(service)

    for _ in range(3):
        asyncio.run(service._run_task(task.id))

    assert [notice.action for notice in notices] == ["failing", "paused"]


def test_transient_resolver_errors_do_not_auto_pause_a_definition(tmp_path: Path, monkeypatch) -> None:
    """Only the persisted unresolvable-target code drives auto-pause."""

    from sqlalchemy.exc import OperationalError

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
    )
    service = _binding_service(tmp_path, store, [])

    async def _transient_failure(**_kwargs):
        raise OperationalError(
            "SELECT agent_sessions.id ...",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    service._execute_request = _transient_failure  # type: ignore[method-assign]
    for _ in range(3):
        asyncio.run(service._run_task(task.id))

    saved = store.get_task(task.id)
    assert saved is not None and saved.enabled is True
    assert all(
        not (run.get("metadata") or {}).get("failure_code") for run in service.request_store.list_runs(status="failed")
    )


def test_a_success_resets_the_unresolvable_target_auto_pause_streak(
    tmp_path: Path, monkeypatch
) -> None:
    """The policy counts consecutive classified failures, not lifetime failures."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
    )
    service = _binding_service(tmp_path, store, [])

    for _ in range(2):
        asyncio.run(service._run_task(task.id))

    current = store.get_task(task.id)
    assert current is not None
    queued = service.request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=current,
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    service.request_store.complete(claimed, ok=True, task_id=task.id)

    asyncio.run(service._run_task(task.id))

    saved = store.get_task(task.id)
    assert saved is not None and saved.enabled is True
    latest = service.request_store.list_runs(status="failed")[-1]
    assert "(1/3)" in str(latest.get("error") or "")


def test_rebind_preserves_model_of_the_deleted_session(tmp_path: Path, monkeypatch) -> None:
    """HFR-056 — the executable form of D3.

    ``run_definitions`` has no ``model``/``reasoning_effort`` column and the session
    row is hard-deleted, so without the reclaim snapshot ``_reserve_runtime_session``
    re-resolves the CURRENT scope Agent and silently changes the task's settings.
    Here the deleted session ran on ``claude`` with an explicit model while the
    default Agent is ``codex``: the rebind must keep the old settings.
    """
    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned is not None
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == pinned)
            .values(model="legacy-model", reasoning_effort="high", agent_name="claude")
        )

    # The SQLite-backed store is the one the reclaim snapshot is written into.
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # ``/new`` in that channel.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()

    # The scheduler process that fires this definition next reads it fresh; the
    # reclaim has already paused it, so resume it the way a user would.
    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    service = _binding_service(tmp_path, store, [])
    reloaded = store.get_task(task.id)
    assert reloaded is not None
    assert reloaded.metadata.get("session_settings_snapshot"), "reclaim wrote no settings snapshot"
    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    rebound = store.get_task(task.id)
    assert rebound is not None
    assert rebound.session_id and rebound.session_id != pinned
    with engine.connect() as conn:
        row = conn.execute(
            select(
                agent_sessions.c.model,
                agent_sessions.c.reasoning_effort,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == rebound.session_id)
        ).one()
    assert row.model == "legacy-model"
    assert row.reasoning_effort == "high"
    assert row.agent_backend == "claude"


def test_rebind_falls_back_to_scope_defaults_when_the_snapshot_agent_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-243 — the non-preserving rebind must not retry the Agent that just failed.

    ``_rebind_create_once_session`` tries the snapshot's settings first and, when
    that reservation fails, falls back to a reset one whose whole purpose is to
    degrade to scope defaults -- its own notice says "settings could not be
    recovered, so scope defaults were used". But the fallback re-sent
    ``task.agent_name``, and for a ``create_once`` definition that is the SAME
    name the snapshot carries. A deleted or disabled Agent therefore failed
    ``require_enabled`` twice, both attempts were exhausted, and the definition
    was paused -- the permanent failure the fallback exists to prevent.
    """
    from core.vibe_agents import VibeAgentStore
    from modules.agents.base import AgentRequest
    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.create(name="nightly", backend="claude", model="legacy-model")
        default_agent = agent_store.get_default_agent()
        assert default_agent is not None and default_agent.name != "nightly"
        # A distinctive prompt so "the fallback Agent's system prompt reached the
        # request" cannot pass vacuously on a None-vs-None comparison.
        default_agent = agent_store.update(
            default_agent.name, system_prompt=_FALLBACK_AGENT_SYSTEM_PROMPT
        )
    finally:
        agent_store.close()
    assert default_agent.system_prompt == _FALLBACK_AGENT_SYSTEM_PROMPT

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned is not None
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == pinned)
            .values(agent_name="nightly", model="legacy-model")
        )

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        # The definition was pinned to the same Agent the session ran on, which is
        # what makes the fallback's re-send a repeat of the failure.
        agent_name="nightly",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # `/new` in that channel deletes the row and writes the settings snapshot.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()

    # ...and the Agent the snapshot names is then deleted.
    agent_store = VibeAgentStore(db_path)
    try:
        assert agent_store.remove("nightly")
    finally:
        agent_store.close()

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    notices = _spy_binding_notices(service)
    reloaded = store.get_task(task.id)
    assert reloaded is not None
    assert reloaded.metadata.get("session_settings_snapshot"), "reclaim wrote no settings snapshot"

    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    rebound = store.get_task(task.id)
    assert rebound is not None
    assert rebound.enabled is True, (
        "the definition was paused because the fallback retried the deleted Agent; "
        "the reset attempt is supposed to degrade to scope defaults"
    )
    # Reserving a session is not the deliverable. A rebind that produces a row but
    # never fires the run leaves the user with a definition that is enabled,
    # looks healthy, and silently does nothing -- so the run itself is asserted,
    # not just its binding.
    assert len(dispatched) == 1, "the definition rebound but the run never reached the backend"
    assert rebound.session_id and rebound.session_id != pinned, "no replacement session was reserved"
    with engine.connect() as conn:
        row = conn.execute(
            select(
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == rebound.session_id)
        ).one()
    assert row.agent_name == default_agent.name, "the rebind did not land on the scope/default Agent"
    assert row.agent_backend == default_agent.backend
    assert len(notices) == 1
    assert notices[0].action == "rebound"
    assert notices[0].settings_preserved is False, (
        "a rebind that could not use the snapshot must say so, or the user reads a "
        "settings reset as a settings-preserving recovery"
    )

    # The rebound ROW is not the deliverable either: ``MessageHandler`` re-derives
    # the turn's Agent identity from the definition's own pin FIRST, so a row that
    # says "default" and a pin that still says "nightly" dispatch under different
    # Agents. Assert on the request the backend was actually handed.
    retry_backend, retry_request = dispatched[0]
    assert retry_request.message == "send digest", "the definition's prompt was not the turn input"
    assert retry_backend == default_agent.backend
    assert retry_request.vibe_agent_name == default_agent.name, (
        "the retry reached the backend under the wrong Agent identity"
    )
    assert retry_request.vibe_agent_backend == default_agent.backend
    assert retry_request.vibe_agent_system_prompt == _FALLBACK_AGENT_SYSTEM_PROMPT, (
        "the fallback Agent's system prompt never reached the request, so the turn "
        "ran with different instructions than the Agent it claims to run as"
    )

    # ...and it must still be true on a LATER, SEPARATE fire. The retry could be
    # right by accident (in-memory task object) while the persisted definition
    # still pins the dead Agent, in which case tomorrow's cron minute regresses.
    # Re-read through a fresh store, exactly like the next scheduler tick does.
    next_fire_store = ScheduledTaskStore()
    next_fire_task = next_fire_store.get_task(task.id)
    assert next_fire_task is not None
    asyncio.run(
        service._execute_task(next_fire_task, execution_id="exec-2", disable_one_shot=False)
    )

    # The durable half, asserted at the persistence layer as well as through
    # dispatch: a retry-only fix (pass the fallback Agent to this one
    # ``_execute_request`` and leave the definition alone) satisfies exec-1 and
    # fails here, which is exactly the shape this pair exists to catch.
    assert next_fire_task.agent_name is None, (
        "the definition still pins the deleted Agent, so every future fire re-sends it "
        "as vibe_agent_name and dispatches under an Agent that cannot be resolved"
    )
    assert len(dispatched) == 2, "the second fire never reached the backend"
    later_backend, later_request = dispatched[1]
    assert later_backend == default_agent.backend
    assert later_request.vibe_agent_name == default_agent.name, (
        "the fallback Agent did not survive to the next fire; the definition still "
        "pins the deleted Agent durably"
    )
    assert later_request.vibe_agent_backend == default_agent.backend
    assert later_request.vibe_agent_system_prompt == _FALLBACK_AGENT_SYSTEM_PROMPT
    assert "nightly" not in {request.vibe_agent_name for _backend, request in dispatched}, (
        "a dispatched turn still identified as the deleted Agent"
    )
    for _backend, request in dispatched:
        assert isinstance(request, AgentRequest), "the captured request is not the production type"


def test_rebind_keeps_a_snapshot_null_model_instead_of_adopting_the_agents(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-244 — a snapshot's NULL model is a pinned value, not a missing one.

    ``_reserve_runtime_session`` read ``model is not None`` as "an override was
    supplied", so a snapshot recording ``model=NULL`` -- the session pinned
    nothing and inherited whatever its Agent had at the time -- was
    indistinguishable from passing no override at all. If the Agent is edited
    between the reclaim and the rebind, the replacement session silently acquires
    a model the original never had, while the recovery is still recorded as
    settings-preserving. The record says preserved; the session is not.
    """
    from core.vibe_agents import VibeAgentStore
    from modules.agents.base import AgentRequest
    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    agent_store = VibeAgentStore(db_path)
    try:
        # Pins neither a model nor a reasoning effort, so neither does the session.
        agent_store.create(name="nightly", backend="claude")
    finally:
        agent_store.close()

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned is not None
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == pinned)
            .values(agent_name="nightly", model=None, reasoning_effort=None)
        )

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # `/new` in that channel: the snapshot records model=NULL.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()

    # The Agent is edited AFTER the session was reclaimed -- an ordinary Agent
    # Settings edit, with no way to know a reclaimed session points at it.
    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.update("nightly", model="claude-opus-4-6", reasoning_effort="high")
    finally:
        agent_store.close()

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    notices = _spy_binding_notices(service)
    reloaded = store.get_task(task.id)
    assert reloaded is not None
    snapshot = reloaded.metadata.get("session_settings_snapshot")
    assert snapshot, "reclaim wrote no settings snapshot"
    assert snapshot.get("model") is None, "precondition: the snapshot pinned no model"

    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    rebound = store.get_task(task.id)
    assert rebound is not None
    assert rebound.session_id and rebound.session_id != pinned

    # What the backend was ACTUALLY handed. A NULL in the session row is only half
    # the guarantee: ``MessageHandler`` reads a NULL session column as "inherit
    # from the Agent at dispatch time" -- correct for every other session, and
    # exactly the value D3 says must NOT be adopted here -- so the preserved nulls
    # have to survive into the request, which is the only thing the agent sees.
    agent_store = VibeAgentStore(db_path)
    try:
        edited_agent = agent_store.require_enabled("nightly")
    finally:
        agent_store.close()
    # Proves the request's nulls are a real override, not an empty fixture.
    assert edited_agent.model == "claude-opus-4-6"
    assert edited_agent.reasoning_effort == "high"

    assert len(dispatched) == 1, "the rebound definition never reached the backend"
    backend_name, request = dispatched[0]
    assert isinstance(request, AgentRequest), "the captured request is not the production type"
    assert backend_name == edited_agent.backend
    assert request.vibe_agent_name == "nightly", (
        "precondition: the turn still runs as the same Agent, only its settings differ"
    )
    assert request.vibe_agent_model is None, (
        f"dispatch handed the backend model={request.vibe_agent_model!r} from the Agent's "
        "CURRENT settings; the snapshot pinned none and D3 says preserve that"
    )
    assert request.vibe_agent_reasoning_effort is None, (
        f"dispatch handed the backend reasoning_effort="
        f"{request.vibe_agent_reasoning_effort!r} the session never had"
    )

    # ...and the durable record must agree. Read AFTER dispatch on purpose: the
    # turn-start route materialization writes the resolved model back onto empty
    # session columns, so an adopted model does not just mis-route this run, it
    # becomes the session's pinned model for every run after it.
    with engine.connect() as conn:
        row = conn.execute(
            select(
                agent_sessions.c.model,
                agent_sessions.c.reasoning_effort,
            ).where(agent_sessions.c.id == rebound.session_id)
        ).one()
    assert row.model is None, (
        f"the rebound session acquired model={row.model!r} from the Agent's CURRENT "
        "settings; the snapshot pinned none and D3 says preserve it"
    )
    assert row.reasoning_effort is None, (
        f"the rebound session acquired reasoning_effort={row.reasoning_effort!r} it never had"
    )
    assert len(notices) == 1
    assert notices[0].settings_preserved is True, (
        "this rebind DID use the snapshot, so it must not be reported as a reset"
    )


#: The system prompt of the Agent the reset rebind lands on, and of the Agent that
#: later becomes the default. Distinct strings so "the turn ran as the Agent it
#: claims to" is asserted on content, not on a None == None comparison.
_REBOUND_AGENT_SYSTEM_PROMPT = "You are the rebound session's Agent. Answer tersely."
_SUCCESSOR_AGENT_SYSTEM_PROMPT = "You are the NEW scope default. Answer at length."


def test_unrelated_task_update_keeps_the_rebound_sessions_agent_authority(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """HFR-245 — "follow the rebound Session's Agent" must survive ordinary edits.

    A reset rebind clears ``task.agent_name`` because the Agent the definition
    pinned is the one that was just found unusable; authority moves to the session
    the rebind reserved. But an absent ``agent_name`` is ALSO what "never pinned
    one" looks like, and ``vibe task update`` re-resolves an omitted Agent for
    every non-``existing`` policy and writes it back. So a later
    ``vibe task update <id> --name ...`` -- an edit about the name and nothing else
    -- silently re-pinned whatever Agent the scope resolved to at that moment, and
    because the definition's pin outranks the session row at dispatch, every future
    fire moved onto that Agent (and its system prompt) instead of the session's.
    The user changes a label and their nightly task quietly changes personality.
    """
    from core.vibe_agents import VibeAgentStore
    from modules.agents.base import AgentRequest
    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService
    from unittest.mock import patch

    from vibe import cli

    db_path = _binding_env(tmp_path, monkeypatch)

    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.create(name="nightly", backend="claude", model="legacy-model")
        rebound_agent = agent_store.get_default_agent()
        assert rebound_agent is not None and rebound_agent.name != "nightly"
        rebound_agent = agent_store.update(
            rebound_agent.name, system_prompt=_REBOUND_AGENT_SYSTEM_PROMPT
        )
    finally:
        agent_store.close()

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned is not None
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == pinned)
            .values(agent_name="nightly", model="legacy-model")
        )

    store = ScheduledTaskStore()
    task = store.add_task(
        name="digest",
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        agent_name="nightly",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # ``/new`` in that channel, then the snapshot's Agent is deleted: the rebind
    # cannot preserve the settings and resets to the scope/default Agent.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()
    agent_store = VibeAgentStore(db_path)
    try:
        assert agent_store.remove("nightly")
    finally:
        agent_store.close()

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    reloaded = store.get_task(task.id)
    assert reloaded is not None
    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    rebound = ScheduledTaskStore().get_task(task.id)
    assert rebound is not None
    assert rebound.session_id and rebound.session_id != pinned
    assert rebound.agent_name is None
    assert rebound.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "the reset rebind dropped the Agent pin but recorded nothing durable, so the "
        "cleared state cannot be told apart from 'the user never pinned an Agent'"
    )

    def _run_update(*argv: str) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["task", "update", task.id, *argv])
        cli_store = ScheduledTaskStore()
        cli_agent_store = VibeAgentStore(db_path)
        try:
            with (
                patch("vibe.cli._ensure_config", return_value=None),
                patch("vibe.cli._task_store", return_value=cli_store),
                patch("vibe.cli._agent_store", return_value=cli_agent_store),
            ):
                assert cli.cmd_task_update(args) == 0, capsys.readouterr().err
        finally:
            cli_agent_store.close()
        capsys.readouterr()

    # The scope's default Agent is then changed -- an ordinary Agent Settings edit,
    # with no way to know a rebound definition points at the previous default. It
    # happens BEFORE the unrelated edits below, which is what makes the difference
    # observable: re-resolving an omitted Agent inside ``task update`` resolves
    # TODAY's default, not the Agent the rebound session actually carries.
    agent_store = VibeAgentStore(db_path)
    try:
        successor = agent_store.create(
            name="successor", backend="claude", system_prompt=_SUCCESSOR_AGENT_SYSTEM_PROMPT
        )
        agent_store.set_default_agent_name(successor.name)
    finally:
        agent_store.close()
    assert successor.name != rebound_agent.name

    # The REAL update command, on an edit that has nothing to do with the Agent.
    _run_update("--name", "renamed digest")
    after_rename = ScheduledTaskStore().get_task(task.id)
    assert after_rename is not None
    assert after_rename.name == "renamed digest"
    assert after_rename.agent_name is None, (
        f"a --name-only update re-pinned agent_name={after_rename.agent_name!r}; the "
        "rebound session's Agent no longer governs the definition"
    )
    assert after_rename.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "an unrelated update dropped the follow-the-session state"
    )

    # The next fire must still run as the REBOUND SESSION's Agent, with that
    # Agent's system prompt -- read through a fresh store, exactly like the next
    # scheduler tick does.
    next_fire_task = ScheduledTaskStore().get_task(task.id)
    assert next_fire_task is not None
    asyncio.run(
        service._execute_task(next_fire_task, execution_id="exec-2", disable_one_shot=False)
    )
    assert len(dispatched) == 2, "the later fire never reached the backend"
    later_backend, later_request = dispatched[1]
    assert isinstance(later_request, AgentRequest), "the captured request is not the production type"
    assert later_request.vibe_agent_name == rebound_agent.name, (
        "the fire dispatched under the new scope default instead of the Agent the "
        "rebound session carries"
    )
    assert later_backend == rebound_agent.backend
    assert later_request.vibe_agent_system_prompt == _REBOUND_AGENT_SYSTEM_PROMPT, (
        "the turn ran with the new default Agent's instructions while the session "
        "says it runs as its own Agent"
    )
    assert successor.name not in {request.vibe_agent_name for _backend, request in dispatched}

    # A second unrelated edit: the state has to survive repeated edits, not just
    # the first one.
    _run_update("--name", "renamed digest again")
    after_second_rename = ScheduledTaskStore().get_task(task.id)
    assert after_second_rename is not None
    assert after_second_rename.agent_name is None, (
        f"the second update re-pinned agent_name={after_second_rename.agent_name!r} -- "
        "today's scope default, not the Agent the rebound session actually runs as"
    )
    assert after_second_rename.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True

    # An EXPLICIT Agent is the user pinning again: the follow state ends.
    _run_update("--agent", rebound_agent.name)
    repinned = ScheduledTaskStore().get_task(task.id)
    assert repinned is not None
    assert repinned.agent_name == rebound_agent.name
    assert BINDING_FOLLOWS_SESSION_METADATA_KEY not in repinned.metadata, (
        "an explicit --agent must clear the follow-the-session state, otherwise the "
        "user's new pin is treated as accidental on the next edit"
    )


def test_unrelated_task_update_keeps_the_follow_state_before_the_default_moves(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """HFR-245 (literal order) — the persisted follow-the-session state is the defect.

    Same regression as ``test_unrelated_task_update_keeps_the_rebound_sessions_agent
    _authority``, in the order the report describes it: rebind, then an unrelated
    ``vibe task update --name``, then the scope default moves, then a later fire.

    In THIS order the load-bearing assertion is step 3 -- the persisted state right
    after the unrelated edit: ``agent_name`` must still be unpinned and the durable
    follow-the-session marker must still be there. On the pre-fix code the edit
    re-resolves an omitted Agent and writes back whatever the scope resolves to at
    that moment, which is the CURRENT default, which is still the very Agent the
    rebound session carries. So the definition acquires a hard Agent pin whose value
    happens to be right, and every downstream assertion agrees with reality.

    That is why the dispatch assertions at the end are GUARDS here, not the proof:
    they only diverge once the default moves AFTER the pin was written, which is the
    other test's ordering. Read together, the two orderings say the pin must not be
    created (this test) and must not be honoured over the session if it somehow is
    (the other one). Neither ordering alone covers both.
    """
    from core.vibe_agents import VibeAgentStore
    from modules.agents.base import AgentRequest
    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService
    from unittest.mock import patch

    from vibe import cli

    db_path = _binding_env(tmp_path, monkeypatch)

    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.create(name="nightly", backend="claude", model="legacy-model")
        rebound_agent = agent_store.get_default_agent()
        assert rebound_agent is not None and rebound_agent.name != "nightly"
        rebound_agent = agent_store.update(
            rebound_agent.name, system_prompt=_REBOUND_AGENT_SYSTEM_PROMPT
        )
    finally:
        agent_store.close()

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned is not None
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == pinned)
            .values(agent_name="nightly", model="legacy-model")
        )

    store = ScheduledTaskStore()
    task = store.add_task(
        name="digest",
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        agent_name="nightly",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # 1. ``/new`` in that channel, then the snapshot's Agent is deleted: the rebind
    #    cannot preserve the settings, so it resets to the scope/default Agent and
    #    hands Agent authority to the session it just reserved.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()
    agent_store = VibeAgentStore(db_path)
    try:
        assert agent_store.remove("nightly")
    finally:
        agent_store.close()

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    reloaded = store.get_task(task.id)
    assert reloaded is not None
    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    rebound = ScheduledTaskStore().get_task(task.id)
    assert rebound is not None
    assert rebound.session_id and rebound.session_id != pinned
    assert rebound.agent_name is None
    assert rebound.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "the reset rebind dropped the Agent pin but recorded nothing durable, so the "
        "cleared state cannot be told apart from 'the user never pinned an Agent'"
    )
    rebound_session_id = rebound.session_id

    # 2. An unrelated edit through the REAL command: nothing about the Agent.
    parser = cli.build_parser()
    args = parser.parse_args(["task", "update", task.id, "--name", "renamed digest"])
    cli_store = ScheduledTaskStore()
    cli_agent_store = VibeAgentStore(db_path)
    try:
        with (
            patch("vibe.cli._ensure_config", return_value=None),
            patch("vibe.cli._task_store", return_value=cli_store),
            patch("vibe.cli._agent_store", return_value=cli_agent_store),
        ):
            assert cli.cmd_task_update(args) == 0, capsys.readouterr().err
    finally:
        cli_agent_store.close()
    capsys.readouterr()

    # 3. THE load-bearing assertion. Nothing observable has changed yet -- the
    #    default Agent has not moved -- so this is the only place the defect exists
    #    right now. The edit must not have converted "follow the bound Session's
    #    Agent" into a hard pin, however harmless today's resolved value looks.
    after_rename = ScheduledTaskStore().get_task(task.id)
    assert after_rename is not None
    assert after_rename.name == "renamed digest"
    assert after_rename.agent_name is None, (
        f"a --name-only update re-pinned agent_name={after_rename.agent_name!r}. It "
        "equals today's default, so nothing looks wrong yet -- but the definition now "
        "carries a hard Agent pin that outranks its bound Session at dispatch, and it "
        "will keep pointing here after the default moves"
    )
    assert after_rename.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "an unrelated update dropped the durable follow-the-session state, so the "
        "next edit cannot tell the cleared Agent from 'never pinned one'"
    )
    assert after_rename.session_id == rebound_session_id, (
        "precondition: the unrelated edit must not have re-bound the Session either"
    )

    # 4. NOW the scope default moves -- an ordinary Agent Settings edit, with no way
    #    to know a rebound definition points at the previous default.
    agent_store = VibeAgentStore(db_path)
    try:
        successor = agent_store.create(
            name="successor", backend="claude", system_prompt=_SUCCESSOR_AGENT_SYSTEM_PROMPT
        )
        agent_store.set_default_agent_name(successor.name)
    finally:
        agent_store.close()
    assert successor.name != rebound_agent.name

    # 5. Guards, not the proof (see the docstring): a later real fire must still run
    #    as the REBOUND SESSION's Agent, with that Agent's system prompt -- read
    #    through a fresh store, exactly like the next scheduler tick does.
    next_fire_task = ScheduledTaskStore().get_task(task.id)
    assert next_fire_task is not None
    asyncio.run(
        service._execute_task(next_fire_task, execution_id="exec-2", disable_one_shot=False)
    )
    assert len(dispatched) == 2, "the later fire never reached the backend"
    later_backend, later_request = dispatched[1]
    assert isinstance(later_request, AgentRequest), "the captured request is not the production type"
    assert later_request.vibe_agent_name == rebound_agent.name, (
        "the fire dispatched under the new scope default instead of the Agent the "
        "rebound session carries"
    )
    assert later_backend == rebound_agent.backend
    assert later_request.vibe_agent_system_prompt == _REBOUND_AGENT_SYSTEM_PROMPT, (
        "the turn ran with the new default Agent's instructions while the session "
        "says it runs as its own Agent"
    )
    assert successor.name not in {request.vibe_agent_name for _backend, request in dispatched}


#: The Agent the bound watch Session runs as. Distinctive so "the session's Agent
#: reached the request" cannot pass on a None == None comparison.
_WATCH_SESSION_AGENT_SYSTEM_PROMPT = "You are the watch session's Agent. Answer tersely."


def test_unrelated_watch_update_keeps_the_follow_session_agent_authority(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """HFR-256 — ``vibe watch update`` had no follow-the-session logic at all.

    ``vibe task update`` keeps three durable Agent-authority states (HFR-245..247,
    HFR-255): ``--clear-agent`` (or an already-rebound definition) hands Agent
    authority to the bound Session and records it durably; an unrelated edit
    preserves that; an explicit ``--agent`` ends it. ``cmd_watch_update`` is the
    sibling definition command over the same ``run_definitions`` rows and had NONE
    of it: ``--clear-agent`` / ``--agent`` were a silent ``if``/``elif``, and the
    command then ran the same ``agent_name is None and session_policy != 'existing'``
    re-resolution, writing today's scope / default Agent straight back as a HARD PIN.
    So ``--clear-agent`` was silently undone, and any unrelated edit re-pinned a
    watch whose Agent authority belonged to its Session.

    The proof is NOT the CLI payload. After the unrelated edit the scope default is
    moved and the watch is FIRED through its production path -- the real
    ``ManagedWatchService._run_watch`` reads the STORED definition, ``_enqueue_hook``
    turns it into a queued ``watch`` request carrying ``agent_name``, and the real
    ``ScheduledTaskService`` claim/execute path hands it to the real
    ``MessageHandler``. The assertions are on the ``AgentRequest`` that handler built,
    i.e. the Agent identity, backend and system prompt the turn actually runs as.
    Only the waiter subprocess is stubbed (its stdout has no bearing on Agent
    identity).
    """
    from unittest.mock import patch

    from core.vibe_agents import VibeAgentStore
    from core.watches import (
        ManagedWatchService,
        ManagedWatchStore,
        WatchRuntimeStateStore,
        _CycleResult,
    )
    from modules.agents.base import AgentRequest

    from vibe import cli

    db_path = _binding_env(tmp_path, monkeypatch)

    # The Agent the bound Session runs as ("claude"), and a DIFFERENT current default
    # ("codex"). The gap between them is what makes a re-pin observable at all.
    agent_store = VibeAgentStore(db_path)
    try:
        session_agent = agent_store.update("claude", system_prompt=_WATCH_SESSION_AGENT_SYSTEM_PROMPT)
        original_default = agent_store.get_default_agent()
        assert original_default is not None and original_default.name != session_agent.name
    finally:
        agent_store.close()

    # The Session a ``create_once`` definition reserves, through the production
    # helper ``vibe watch add --create-session`` uses -- so the row carries its own
    # Agent identity exactly as a real one does.
    with patch("vibe.cli._ensure_config", return_value=None):
        pinned = cli._reserve_definition_session(
            agent_name=session_agent.name,
            deliver_key="slack::channel::C123",
            help_command="vibe watch add --help",
        )
    assert resolve_session_id_target(pinned).agent_name == session_agent.name

    watch = ManagedWatchStore().add_watch(
        name="ci watch",
        session_key="",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix="CI finished.",
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key="slack::channel::C123",
        session_id=pinned,
        agent_name=session_agent.name,
        session_policy="create_once",
        message="CI finished.",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    def _run_update(*argv: str) -> None:
        """The REAL ``vibe watch update`` command, on a fresh store each time."""
        parser = cli.build_parser()
        args = parser.parse_args(["watch", "update", watch.id, *argv])
        cli_agent_store = VibeAgentStore(db_path)
        try:
            with (
                patch("vibe.cli._ensure_config", return_value=None),
                patch("vibe.cli._watch_store", return_value=ManagedWatchStore()),
                patch("vibe.cli._agent_store", return_value=cli_agent_store),
            ):
                assert cli.cmd_watch_update(args) == 0, capsys.readouterr().err
        finally:
            cli_agent_store.close()
        capsys.readouterr()

    def _fire_watch(execution_label: str) -> None:
        """Fire the watch through its PRODUCTION path, reading the stored definition.

        A fresh ``ManagedWatchStore`` / ``ManagedWatchService`` per fire, exactly like
        the next service tick. Only ``_run_cycle`` (the waiter subprocess) is stubbed.
        """
        fire_store = ManagedWatchStore()
        fire_store.set_enabled(watch.id, True)
        watch_service = ManagedWatchService(
            controller=SimpleNamespace(),
            store=fire_store,
            request_store=service.request_store,
            runtime_store=WatchRuntimeStateStore(),
        )
        watch_service._running = True
        watch_service._requires_service_lease = False

        async def _fake_cycle(*_args, **_kwargs):
            return _CycleResult(exit_code=0, stdout=f"ci is green ({execution_label})", stderr="", timed_out=False)

        watch_service._run_cycle = _fake_cycle  # type: ignore[method-assign]
        asyncio.run(watch_service._run_watch(watch.id))

        pending = [item for item in service.request_store.list_pending() if item.task_id == watch.id]
        assert len(pending) == 1, f"the watch fire enqueued {len(pending)} request(s), expected exactly one"
        assert pending[0].request_type == "watch"
        claimed = service.request_store.claim(pending[0].id)
        assert claimed is not None
        asyncio.run(service._execute_claimed_request(claimed))

    service = _dispatching_binding_service(tmp_path, ScheduledTaskStore(), db_path=db_path)
    dispatched = service.controller.agent_service.dispatched

    # 1. ``--clear-agent`` hands Agent authority back to the bound Session. Both
    #    halves are load-bearing: the cleared pin, and the DURABLE record of why it
    #    is cleared -- without the marker the state cannot be told apart from "the
    #    user never pinned an Agent", which is what the re-resolution acts on.
    _run_update("--clear-agent")
    after_clear = ManagedWatchStore().get_watch(watch.id)
    assert after_clear is not None
    assert after_clear.agent_name is None, (
        f"--clear-agent was silently undone: the command re-resolved and re-pinned "
        f"agent_name={after_clear.agent_name!r} (today's scope/default Agent), so the "
        "bound Session never gets Agent authority"
    )
    assert after_clear.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "--clear-agent recorded nothing durable, so the next unrelated edit cannot "
        "tell the cleared Agent from 'never pinned one' and will re-pin it"
    )

    # 2. An unrelated edit -- nothing about the Agent -- must preserve BOTH.
    _run_update("--name", "renamed ci watch")
    after_rename = ManagedWatchStore().get_watch(watch.id)
    assert after_rename is not None
    assert after_rename.name == "renamed ci watch"
    assert after_rename.agent_name is None, (
        f"a --name-only update re-pinned agent_name={after_rename.agent_name!r}; the "
        "bound Session's Agent no longer governs the watch"
    )
    assert after_rename.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True, (
        "an unrelated update dropped the durable follow-the-session state"
    )
    assert after_rename.session_id == pinned, (
        "precondition: the unrelated edit must not have re-bound the Session either"
    )

    # 3. The scope default now MOVES -- an ordinary Agent Settings edit, with no way
    #    to know a watch points at the previous default. This is what makes the pin
    #    the earlier edits would have written observably wrong, and it happens before
    #    one more unrelated edit so the wrong value is the one that gets written.
    agent_store = VibeAgentStore(db_path)
    try:
        successor = agent_store.create(
            name="successor", backend="claude", system_prompt=_SUCCESSOR_AGENT_SYSTEM_PROMPT
        )
        agent_store.set_default_agent_name(successor.name)
    finally:
        agent_store.close()
    assert successor.name != session_agent.name

    _run_update("--name", "renamed ci watch again")
    after_second_rename = ManagedWatchStore().get_watch(watch.id)
    assert after_second_rename is not None
    assert after_second_rename.agent_name is None, (
        f"the second update re-pinned agent_name={after_second_rename.agent_name!r} -- "
        "today's default, not the Agent the bound Session actually runs as"
    )
    assert after_second_rename.metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY) is True

    # 4. THE PROOF: fire the watch for real. The Agent identity the turn runs as is
    #    re-derived inside the real ``MessageHandler``, strictly downstream of the
    #    stored definition and of anything the CLI printed.
    _fire_watch("first")
    assert len(dispatched) == 1, "the watch fire never reached the backend"
    backend, request = dispatched[0]
    assert isinstance(request, AgentRequest), "the captured request is not the production type"
    assert request.vibe_agent_name == session_agent.name, (
        f"the watch hook dispatched as {request.vibe_agent_name!r} instead of the Agent "
        "the bound Session carries -- the stored definition carries a pin the unrelated "
        "edit wrote, and that pin outranks the Session row at dispatch"
    )
    assert backend == session_agent.backend
    assert request.vibe_agent_system_prompt == _WATCH_SESSION_AGENT_SYSTEM_PROMPT, (
        "the turn ran with another Agent's instructions while the Session says it runs "
        "as its own Agent"
    )
    assert successor.name not in {item.vibe_agent_name for _backend, item in dispatched}

    # 5. An EXPLICIT ``--agent`` is the user pinning again: the follow state ends.
    #    The state is seeded straight onto the stored row first, so this step tests
    #    the EXIT independently of how the definition entered the state -- a reset
    #    rebind writes exactly this shape onto a create_once definition.
    seed_store = ManagedWatchStore()
    seeded = seed_store.get_watch(watch.id)
    assert seeded is not None
    seeded.agent_name = None
    seeded.metadata = {**(seeded.metadata or {}), BINDING_FOLLOWS_SESSION_METADATA_KEY: True}
    seed_store.upsert_watch(seeded)

    _run_update("--agent", session_agent.name)
    repinned = ManagedWatchStore().get_watch(watch.id)
    assert repinned is not None
    assert repinned.agent_name == session_agent.name
    assert BINDING_FOLLOWS_SESSION_METADATA_KEY not in repinned.metadata, (
        "an explicit --agent must clear the follow-the-session state, otherwise the "
        "user's new pin is treated as accidental on the next edit"
    )

    # A guard, not the proof: with the pin restored the fire must still run as that
    # Agent and never as the new default. It agrees with the follow-the-session
    # reading here (the pin names the Session's own Agent, which is the only value
    # ``_resolve_agent_for_target`` accepts for a Session-bound definition), so it
    # cannot by itself distinguish pin from follow -- the durable assertions above do.
    _fire_watch("second")
    assert len(dispatched) == 2, "the later fire never reached the backend"
    later_backend, later_request = dispatched[1]
    assert isinstance(later_request, AgentRequest)
    assert later_request.vibe_agent_name == session_agent.name
    assert later_backend == session_agent.backend
    assert later_request.vibe_agent_system_prompt == _WATCH_SESSION_AGENT_SYSTEM_PROMPT


def _reclaim_now(session_id: str, *, mode: str, reason: str) -> dict[str, int]:
    """Run the shared teardown reclaim against the isolated state database."""
    from storage.session_reclaim import reclaim_bound_definitions

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            return reclaim_bound_definitions(conn, session_id, mode=mode, reason=reason)
    finally:
        engine.dispose()


def _bare_session_row(*, workdir: Path, anchor: str) -> str:
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            return create_agent_session_row(
                conn,
                scope_id=None,
                session_anchor=anchor,
                agent_backend="codex",
                agent_variant="codex",
                model="gpt-5.5-codex",
                native_session_id="codex-native",
                workdir=str(workdir),
                require_workdir=False,
            )
    finally:
        engine.dispose()


def _stored_definition_row(definition_id: str) -> dict[str, Any]:
    from storage.models import run_definitions

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            row = (
                conn.execute(select(run_definitions).where(run_definitions.c.id == definition_id))
                .mappings()
                .first()
            )
    finally:
        engine.dispose()
    assert row is not None
    return dict(row)


def test_task_result_stamp_cannot_resurrect_a_definition_the_archive_deleted(tmp_path: Path) -> None:
    """HFR-261, ``deleted_at`` half — nothing in memory even carries the column.

    THE PRODUCTION STORY. A one-shot task fires. While its run is in flight the user
    archives the bound Session, and ``reclaim_bound_definitions(mode='delete')``
    soft-deletes the definition: archive is terminal, so a paused definition could
    otherwise be re-enabled onto a dead session later. The run then finishes and the
    scheduler stamps its result.

    THE DEFECT IS THE COLUMN THAT IS NOT THERE. ``ScheduledTask`` has no
    ``deleted_at`` field at all -- the store only ever lists live rows -- so
    ``_scheduled_task_values`` wrote ``deleted_at=NULL`` unconditionally. A run-result
    stamp keyed on ``id`` alone therefore UN-DELETED the definition, restored its
    ``enabled`` switch and replaced the reclaim's settings snapshot: the task came back
    from an archive the user confirmed, in a list the archive dialog had already
    reported as cleaned up.

    ``mark_task_result`` is a best-effort runtime stamp, so the refusal is reported by
    its return value (its caller already treats ``False`` as "nothing recorded")
    rather than by an exception through the fire path.
    """
    from storage.session_reclaim import RECLAIM_DELETE, SESSION_SETTINGS_SNAPSHOT_KEY

    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="slack_C1")
    task = store.add_task(
        session_key="",
        session_id=session_id,
        session_policy="existing",
        prompt="run once",
        schedule_type="at",
        run_at="2026-07-28T09:00:00+00:00",
        timezone_name="UTC",
        metadata={"origin": "cli"},
    )

    summary = _reclaim_now(session_id, mode=RECLAIM_DELETE, reason="the session was archived")
    assert summary == {"paused": 0, "deleted": 1, "snapshotted": 1}, (
        f"the archive reclaim itself did not land ({summary!r})"
    )

    # The in-flight run finishing, from the mirror the fire was decided from.
    recorded = store.mark_task_result(task.id, error=None)

    row = _stored_definition_row(task.id)
    assert row["deleted_at"] is not None, (
        "the run-result stamp resurrected a definition the archive soft-deleted; it is "
        "back in every task list, bound to a session the user archived"
    )
    assert row["last_run_at"] is None, (
        "the refused write partially landed — a lost compare-and-set must change "
        "NOTHING, not just the guarded columns"
    )
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert SESSION_SETTINGS_SNAPSHOT_KEY in stored_metadata, (
        "the stale write replaced the reclaim's settings snapshot with the "
        "pre-teardown metadata"
    )
    assert recorded is False, (
        "the store reported the run result as recorded while the write was refused; a "
        "lost write must be reported to nobody"
    )
    assert ScheduledTaskStore().get_task(task.id) is None, (
        "the deleted definition is being served again"
    )
    # HFR-271's rule: the fresh store above cannot see the mirror ``store`` mutated
    # before the refusal, and that mirror is what ``reconcile_jobs`` schedules from.
    assert store.get_task(task.id) is None, (
        "the live store still serves the definition the archive deleted, so the "
        "scheduler keeps firing it until the process restarts"
    )


def test_cycle_result_cannot_restore_the_metadata_a_snapshot_refresh_replaced(tmp_path: Path) -> None:
    """HFR-261, snapshot-marker half — the reclaim shape the other guards miss.

    THE THIRD RECLAIM SHAPE. For a definition that is ALREADY paused,
    ``reclaim_bound_definitions(mode='pause')`` changes neither ``enabled`` nor
    ``deleted_at`` nor ``session_id``: it only refreshes
    ``session_settings_snapshot``, because that snapshot is the ONLY copy of the dying
    session's workdir / agent / model and a later ``create_once`` rebind reads it to
    carry them forward (D3). So the three lifecycle predicates all match, and a stale
    full-row write would still restore the pre-teardown metadata and send the
    definition back on the wrong route.

    That is why the guard also re-asserts the snapshot's ``captured_at``: it is the
    state this reclaim actually owns. Driven here through ``mark_cycle_result`` -- a
    cycle landing after a manual pause, which the store explicitly supports -- so the
    proof does not depend on the CLI.
    """
    from core.watches import ManagedWatchStore
    from storage.session_reclaim import RECLAIM_PAUSE, SESSION_SETTINGS_SNAPSHOT_KEY

    store = ManagedWatchStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="slack_C2")
    watch = store.add_watch(
        name="Watch CI",
        session_key="",
        session_id=session_id,
        session_policy="existing",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=600,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
        metadata={"origin": "cli"},
    )
    store.set_enabled(watch.id, False)

    summary = _reclaim_now(session_id, mode=RECLAIM_PAUSE, reason="the bound agent session was cleared")
    assert summary == {"paused": 0, "deleted": 0, "snapshotted": 1}, (
        f"this test needs the SNAPSHOT-ONLY reclaim shape, and got {summary!r}: an "
        "already-paused definition is neither paused again nor deleted"
    )
    reclaimed_snapshot = json.loads(_stored_definition_row(watch.id)["metadata_json"])[
        SESSION_SETTINGS_SNAPSHOT_KEY
    ]

    recorded = store.mark_cycle_result(watch.id, exit_code=0, error=None, event_detected=True)

    stored_metadata = json.loads(_stored_definition_row(watch.id)["metadata_json"] or "{}")
    assert stored_metadata.get(SESSION_SETTINGS_SNAPSHOT_KEY) == reclaimed_snapshot, (
        "the stale cycle-result write restored the pre-teardown metadata over the "
        "reclaim's settings snapshot; that snapshot is the only record of the dying "
        "session's workdir/agent/model, so a later create_once rebind would resolve "
        f"today's defaults instead. Stored: {stored_metadata!r}"
    )
    assert recorded is False, (
        "the store reported the cycle result as recorded while the write was refused"
    )
    # HFR-271's rule: ``store`` is the object that mutated its cached ManagedWatch before
    # the refusal, so a refusal is only proven once IT agrees with the row above.
    live = store.get_watch(watch.id)
    assert live is not None and live.metadata.get(SESSION_SETTINGS_SNAPSHOT_KEY) == reclaimed_snapshot, (
        "the write was refused and the live store kept the pre-teardown metadata: "
        f"{None if live is None else live.metadata!r}"
    )


#: The ``existing`` probe ``_upsert_definition`` runs before its guarded UPDATE.
#: Committing the competing teardown when THIS read completes puts it exactly
#: inside the window the guard exists for: after the caller decided what to write,
#: before the write takes the lock.
_DEFINITION_EXISTS_SELECT = (
    "SELECT run_definitions.id FROM run_definitions WHERE run_definitions.id = ? LIMIT ? OFFSET ?"
)


def _commit_reclaim_after(engine, session_id: str, *, read: str, mode: str, reason: str) -> dict:
    """Commit the REAL teardown reclaim from a genuinely separate connection.

    The task-side twin of ``_commit_competing_bind_after`` in
    ``tests/test_sqlite_sessions_store.py``: hooks ``after_cursor_execute`` on the
    engine the code under test uses, and when ``read`` completes opens its own
    engine, runs ``reclaim_bound_definitions`` and COMMITS. Control returns to the
    caller mid-write, so its next statement runs against a database another writer
    has already changed. Fires once; the returned dict records it, so a rendered-SQL
    drift shows up as "never raced" instead of a vacuous pass.
    """
    from sqlalchemy import event

    from storage.session_reclaim import reclaim_bound_definitions

    state: dict = {"fired": 0, "summary": None}

    @event.listens_for(engine, "after_cursor_execute")
    def _race(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) != read:
            return
        state["fired"] += 1
        other = create_sqlite_engine(paths.get_sqlite_state_path())
        try:
            with other.begin() as other_conn:
                state["summary"] = reclaim_bound_definitions(
                    other_conn, session_id, mode=mode, reason=reason
                )
        finally:
            other.dispose()

    return state


def _scheduled_service_with_ledger(
    tmp_path: Path, store: ScheduledTaskStore, calls: list
) -> ScheduledTaskService:
    """``_binding_service``, but on the SQLite request store — the real run ledger.

    ``_binding_service`` uses a file-backed ``TaskExecutionStore``; the run ledger
    HFR-264 is about is ``agent_runs``, which only the SQLite backend writes, and
    ``get_run`` is how the CLI and the Harness read a run's terminal state.
    """

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        calls.append(message)
        return None

    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_kw: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    return service


def test_a_refused_result_stamp_cannot_complete_the_run_ok(tmp_path: Path, monkeypatch) -> None:
    """HFR-264 — the consuming end of ``mark_task_result``'s refusal.

    THE PRODUCTION STORY. A one-shot task fires and its turn succeeds. While the run
    is in flight the user archives the bound Session, and
    ``reclaim_bound_definitions(mode='delete')`` soft-deletes the definition. The fire
    then stamps its terminal result from the pre-teardown mirror, and HFR-261's guard
    correctly REFUSES it — ``last_run_at``, ``last_error`` and the one-shot disable are
    not stored.

    THE DEFECT WAS DOWNSTREAM OF THE GUARD. ``_execute_task`` discarded that ``False``
    and returned a ``TaskExecutionResult`` with ``error=None``, so
    ``_execute_claimed_request`` completed the run ``ok=True``: the database refused
    the stale stamp and BOTH the caller and the run ledger reported success. The user
    sees a green run for a task whose stored state never moved, and an ``at`` task that
    was never disabled can fire again.

    Driven through the REAL claimed-request path with the archive committed from a
    second connection INSIDE the write window, and asserted on the run ledger
    ``agent_runs`` row — which is what ``vibe task runs`` and the Harness detail pane
    read.
    """
    from storage.session_reclaim import RECLAIM_DELETE

    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    assert store._sqlite is not None, "this test needs the SQLite-backed store; the guard lives there"
    sessions = SQLiteSessionsService(db_path)
    try:
        session_id = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert session_id is not None
    task = store.add_task(
        session_key="",
        session_id=session_id,
        session_policy="existing",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-07-28T09:00:00+00:00",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"origin": "cli"},
    )

    calls: list = []
    service = _scheduled_service_with_ledger(tmp_path, store, calls)
    queued = service.request_store.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None

    race = _commit_reclaim_after(
        store._sqlite.engine,
        session_id,
        read=_DEFINITION_EXISTS_SELECT,
        mode=RECLAIM_DELETE,
        reason="the session was archived",
    )

    asyncio.run(service._execute_claimed_request(claimed))

    assert race["fired"] == 1, (
        "the competing archive never landed inside the write window, so this test "
        "proved nothing; the rendered SQL of the guarded upsert's existence probe drifted"
    )
    assert race["summary"] == {"paused": 0, "deleted": 1, "snapshotted": 1}, (
        f"the archive reclaim itself did not land ({race['summary']!r})"
    )
    assert calls, "the turn never ran, so this is not the success-shaped fire under test"

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert run["status"] == "failed", (
        "the run ledger recorded a success for a fire whose terminal stamp the "
        f"database refused (status={run['status']!r}); the stored task never moved"
    )
    from core.scheduled_tasks import _TASK_RESULT_NOT_RECORDED_ERROR

    assert run["error"] == _TASK_RESULT_NOT_RECORDED_ERROR, (
        f"the refusal reached the ledger without saying why: {run['error']!r}"
    )

    row = _stored_definition_row(task.id)
    assert row["deleted_at"] is not None, (
        "the result stamp resurrected a definition the archive soft-deleted"
    )
    assert row["last_run_at"] is None and row["last_error"] is None, (
        "the refused write partially landed — a lost compare-and-set must change NOTHING"
    )


def test_refused_task_stamp_fails_durable_run_and_reconciles_its_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_refusal")
    task = store.add_task(
        session_key="",
        session_id=session_id,
        session_policy="existing",
        prompt="send digest",
        schedule_type="at",
        run_at="2026-07-28T09:00:00+00:00",
        timezone_name="UTC",
        metadata={"origin": "test"},
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    reconciled: list[tuple[str, str]] = []

    async def _execute_request(**_kwargs):
        return TaskDispatchResult(error=None, complete_on_return=False)

    async def _reconcile(run_id: str, *, session_id: str):
        reconciled.append((run_id, session_id))
        return {"changed": True, "state": "retired"}

    service._execute_request = _execute_request
    service.controller.session_turns = SimpleNamespace(
        reconcile_terminal_run_delivery=_reconcile
    )
    monkeypatch.setattr(store, "mark_task_result", lambda *_args, **_kwargs: False)
    queued = service.request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None

    asyncio.run(service._execute_claimed_request(claimed))

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] == _TASK_RESULT_NOT_RECORDED_ERROR
    assert reconciled == [(queued.id, session_id)]


def test_a_refused_recovery_record_does_not_notify_a_transition_it_cannot_dedup(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-266 — ``record_binding_recovery``'s refusal was discarded too.

    ``_emit_binding_change`` promises "once per binding transition, never once per
    fire", and the ONLY thing that makes it once is the durable
    ``metadata.binding_recovery`` marker it writes first. That write is guarded
    (HFR-261) and refuses when the definition was reclaimed, repointed or removed in
    the window — and its ``False`` was ignored, so the notice went out with nothing
    behind it: a daily cron re-notifies every day, about a recovery the stored
    definition no longer reflects.
    """
    from storage.session_reclaim import RECLAIM_DELETE

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    assert store._sqlite is not None
    session_id = _bare_session_row(workdir=tmp_path, anchor="slack_C123")
    task = store.add_task(
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"origin": "cli"},
    )
    # Bind it to the real session AFTER creation so the reclaim below has a row to
    # reclaim, without the fire being able to resolve it.
    with create_sqlite_engine(paths.get_sqlite_state_path()).begin() as conn:
        conn.execute(
            update(run_definitions).where(run_definitions.c.id == task.id).values(session_id=session_id)
        )
    store.load()

    service = _scheduled_service_with_ledger(tmp_path, store, [])
    notices = _spy_binding_notices(service)
    change = SessionBindingChange(
        action="paused",
        task_id=task.id,
        reason="missing",
        previous_session_id=session_id,
        detail="paused: the bound agent session no longer exists.",
    )

    race = _commit_reclaim_after(
        store._sqlite.engine,
        session_id,
        read=_DEFINITION_EXISTS_SELECT,
        mode=RECLAIM_DELETE,
        reason="the session was archived",
    )

    asyncio.run(service._emit_binding_change(change))

    assert race["fired"] == 1, "the competing archive never landed inside the write window"
    assert notices == [], (
        "a binding-change notice was delivered while its dedup marker was refused; "
        "'once per transition' becomes once per fire, forever"
    )
    row = _stored_definition_row(task.id)
    stored_metadata = json.loads(row["metadata_json"] or "{}")
    assert BINDING_RECOVERY_METADATA_KEY not in stored_metadata, (
        "the refused write partially landed"
    )


def test_rebind_propagates_an_operational_fault_instead_of_resetting_the_route(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-265 — a transient fault must not be read as "that Agent is gone".

    THE FALLBACK'S JOB (HFR-243) is narrow: the snapshot names an Agent the user has
    since deleted or disabled, so the reset attempt degrades to scope defaults and
    says so. It decided that from a BROAD ``except Exception``, which cannot tell a
    settled catalog fact from SQLite contention, a migration failure or a filesystem
    error. On any of those the retry SUCCEEDED against scope defaults,
    ``_persist_task_session_id`` wrote the reset route, ``agent_name`` was dropped and
    ``binding_follows_session`` was stamped — so a momentary database fault
    PERMANENTLY cost the task the Agent and model the snapshot was holding for it,
    under a notice claiming the settings "could not be recovered".

    The condition now has a type (``AgentUnavailableError``) raised by the
    Agent-resolution layer and caught narrowly. Everything else propagates, with the
    definition's route and lifecycle untouched and NO fallback reservation.
    """
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from core.vibe_agents import VibeAgentStore
    from storage.models import agent_sessions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.create(name="nightly", backend="claude", model="legacy-model")
    finally:
        agent_store.close()

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="claude",
            session_anchor="slack_C123:definition_abc",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned is not None
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == pinned)
            .values(agent_name="nightly", model="legacy-model")
        )

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        agent_name="nightly",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # `/new` deletes the session row and writes the settings snapshot the preserved
    # rebind reads.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    before = _stored_definition_row(task.id)

    calls: list = []
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    notices = _spy_binding_notices(service)
    original_reserve = service._reserve_runtime_session

    def _reserve(**kwargs):
        calls.append(kwargs)
        return original_reserve(**kwargs)

    service._reserve_runtime_session = _reserve  # type: ignore[method-assign]

    # THE OPERATIONAL FAULT, at the Agent-resolution layer the real rebind reaches.
    def _contended(self, name):  # noqa: ANN001
        raise OperationalError("SELECT agents.id ...", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(VibeAgentStore, "require_reference", _contended)

    reloaded = store.get_task(task.id)
    assert reloaded is not None
    assert reloaded.metadata.get("session_settings_snapshot"), "reclaim wrote no settings snapshot"

    with pytest.raises(OperationalError):
        asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    assert len(calls) == 1, (
        f"the reset attempt ran after an operational fault ({len(calls)} reservations); "
        "a transient error was treated as a deleted Agent and the task's route was reset"
    )
    assert calls[0]["agent_name"] == "nightly", "the ONE attempt was not the preserving one"

    after = _stored_definition_row(task.id)
    assert after["session_id"] == before["session_id"], (
        "the definition was repointed after an operational fault; the snapshot it "
        "needed for a later preserved rebind is gone with it"
    )
    assert after["agent_name"] == "nightly", (
        "the definition lost its Agent pin to a transient database error"
    )
    assert after["enabled"] == before["enabled"], "the lifecycle moved on an operational fault"
    assert after["metadata_json"] == before["metadata_json"], (
        "the definition's durable metadata changed — a reset rebind stamped "
        "binding_follows_session, or the snapshot was replaced"
    )
    assert notices == [], "an operational fault was reported to the user as a binding recovery"


def test_agent_resolution_types_the_deleted_or_disabled_condition(tmp_path: Path, monkeypatch) -> None:
    """HFR-265, the contract half — the type the narrow catch is written against.

    The rebind fallback must degrade for exactly two facts (the Agent was deleted, the
    Agent was disabled) and for nothing else. Inferring them from ``except Exception``
    is what let an infrastructure fault cost a task its route, so the Agent-resolution
    layer now says which it is. ``AgentUnavailableError`` subclasses ``ValueError`` and
    keeps the old messages, so every existing ``except ValueError`` caller — the CLI,
    the UI server, the controller's route resolution — is unchanged.
    """
    from core.vibe_agents import AgentUnavailableError, VibeAgentStore

    db_path = _binding_env(tmp_path, monkeypatch)
    agent_store = VibeAgentStore(db_path)
    try:
        agent_store.create(name="nightly", backend="claude", enabled=False)
        with pytest.raises(AgentUnavailableError) as missing:
            agent_store.require_enabled("does-not-exist")
        with pytest.raises(AgentUnavailableError) as disabled:
            agent_store.require_enabled("nightly")
    finally:
        agent_store.close()

    assert missing.value.reason == "missing"
    assert missing.value.agent_name == "does-not-exist"
    assert str(missing.value) == "agent 'does-not-exist' not found"
    assert disabled.value.reason == "disabled"
    assert disabled.value.agent_name == "nightly"
    assert str(disabled.value) == "agent 'nightly' is disabled"
    assert isinstance(missing.value, ValueError) and isinstance(disabled.value, ValueError), (
        "the typed contract must stay a ValueError, or every existing caller changes behaviour"
    )


def test_execute_task_does_not_dispatch_when_the_rebind_persist_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-268 — a rebind the guard refused must not fire the turn it was for.

    THE PRODUCTION STORY. A ``create_once`` task is pinned to a Session. ``/new``
    deletes that session, the reclaim pauses the task and writes its settings
    snapshot, the user resumes it, and the schedule fires. The fire finds the pinned
    session unresolvable, so ``_recover_pinned_session_binding`` reserves a
    replacement and persists the rebind. While that is happening a SECOND teardown
    lands — another ``/new``, or the archive dialog — and pauses the definition again.

    THE GUARD WAS ASKED, AND ITS ANSWER WAS DROPPED. ``_write_task`` is guarded
    (HFR-261) and correctly refuses to restore the binding and the enabled state the
    teardown just cleared. But ``_persist_task_session_id`` swallowed the
    ``DefinitionWriteConflict`` and returned ``None``, so ``action`` stayed
    ``"rebound"`` and ``_execute_task`` went on to run the prompt and post the reply
    into the freshly reserved session — a real agent turn, delivered to the user's
    channel, for a definition the database had just torn down. Same class as HFR-267:
    the effect outlived the refusal. The refusal also covers the soft-delete shape
    (``expect.deleted_at`` is ``None``), so "the rebind stands for THIS fire only"
    was not a safe reading of it.

    Driven through the REAL ``_execute_task`` with the REAL production dispatch path
    attached, and the competing reclaim committed from a genuinely separate engine.
    """
    from storage.session_reclaim import RECLAIM_PAUSE, reclaim_bound_definitions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_hfr268",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # `/new`: the session row goes, the reclaim pauses the task and snapshots its
    # settings so a rebind can preserve them.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()

    # The user resumes it and the schedule fires. THIS is the read the fire acts
    # from; nothing reloads it again before the rebind is written.
    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    reloaded = store.get_task(task.id)
    assert reloaded is not None and reloaded.enabled is True
    assert reloaded.metadata.get("session_settings_snapshot"), "the reclaim wrote no settings snapshot"

    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    notices = _spy_binding_notices(service)

    # THE COMPETING TEARDOWN, committed from its own engine after that read.
    reason = "the bound agent session was cleared"
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            summary = reclaim_bound_definitions(conn, pinned, mode=RECLAIM_PAUSE, reason=reason)
    finally:
        engine.dispose()
    assert summary["paused"] == 1, f"the competing teardown never landed ({summary!r}), so this test proves nothing"

    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    assert dispatched == [], (
        "the fire dispatched an agent turn on a rebind the store REFUSED; the prompt "
        "runs and its reply is posted for a definition the teardown just paused"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.session_id == pinned, (
        "the refused rebind was stored anyway, overwriting the binding the teardown cleared"
    )
    assert stored.enabled is False, "the refused rebind re-enabled the definition the teardown paused"
    assert [notice.action for notice in notices] == ["reclaimed"], (
        f"the user was notified of a rebind that was never stored: {[n.action for n in notices]}"
    )
    assert "not rebound" in (notices[0].detail or ""), (
        f"the notice does not say the rebind was refused: {notices[0].detail!r}"
    )
    assert stored.last_error and "not rebound" in stored.last_error, (
        f"the durable error does not record the refused rebind: {stored.last_error!r}"
    )


def _spy_reserved_sessions(service: ScheduledTaskService) -> list[str]:
    """Record every session id ``_reserve_runtime_session`` actually handed back.

    The id is the only handle on the row the reservation created: it is random, it is
    never written to the definition on the refused path, and diffing the table would
    also catch rows a concurrent writer created. Spying on the return value names
    exactly the row THIS call is responsible for.
    """

    reserved: list[str] = []
    original = service._reserve_runtime_session

    def _spy(**kwargs):
        session_id = original(**kwargs)
        reserved.append(session_id)
        return session_id

    service._reserve_runtime_session = _spy  # type: ignore[method-assign]
    return reserved


@pytest.mark.parametrize("placement", ["scoped", "standalone"])
def test_a_refused_rebind_reclaims_the_session_and_workspace_it_reserved(
    tmp_path: Path, monkeypatch, placement: str
) -> None:
    """HFR-270 — a rebind the guard refused must not leave its replacement behind.

    THE PRODUCTION STORY, one step past HFR-268. The same race: a ``create_once``
    definition's pinned session is gone, the fire reserves a replacement, and a second
    teardown pauses the definition before the rebind can be stored. HFR-268 made the
    refusal stop the dispatch. It did not undo the reservation.

    ``_rebind_create_once_session`` COMMITS the replacement row before
    ``_persist_task_session_id`` is ever called -- a separate service, a separate
    transaction, and for the standalone placement a ``mkdir`` of a Show Page workspace
    as well. When the guard then refuses, nothing points at that row: the definition
    still names the session the teardown cleared, the notice says "not rebound", and
    the row survives as a live, unreferenced background session, with its workspace, for
    as long as the database does. Every subsequent fire that loses the same race leaks
    another one.

    Two placements because the reservation has two shapes and only one of them creates
    a directory: a definition whose deliver key resolves to a Scope reserves inside that
    Scope and INHERITS a shared workdir (which the reclaim must NOT delete -- it belongs
    to the Scope, not to this row), while one whose deliver key names no Scope reserves
    standalone and gets a Show Page workspace of its own (which the reclaim MUST remove,
    because this reservation is the only thing that ever created it).

    The concurrent winner in the fixture is the load-bearing negative: a reclaim written
    as "delete the background sessions that nothing references" would take it too. Only
    the row this call created may go.
    """
    from storage.session_reclaim import RECLAIM_PAUSE, reclaim_bound_definitions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    if placement == "scoped":
        deliver_key = "slack::channel::C123"
        task_metadata: dict = {"session_scope_id": "slack::channel::C123"}
    else:
        # A deliver key that names no Scope: neither ``parse_scope_id`` nor
        # ``parse_session_key`` accepts it, so the reservation goes down the
        # standalone branch and mkdirs a Show Page workspace of its own.
        deliver_key = "web::show::hfr270"
        task_metadata = {}

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_hfr270",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key=deliver_key,
        metadata=task_metadata,
    )

    # `/new`: the pinned session goes and the reclaim pauses the definition.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
        # THE CONCURRENT WINNER: a sibling definition's rebind that DID land, reserved
        # after the same teardown and through the SAME branch as the one under test, so
        # it is indistinguishable from the loser by age, visibility, anchor shape or
        # workdir. For the scoped placement that workdir is the SHARED one the loser also
        # got, which is what makes "the reclaim removed a directory it did not create" a
        # failing assertion rather than a hypothetical.
        if placement == "scoped":
            winner = sessions.reserve_agent_session(
                scope_key="slack::channel::C123",
                agent_backend="codex",
                session_anchor="slack_C123:runtime_winner",
                agent_name="codex",
            )
        else:
            winner = sessions.reserve_standalone_agent_session(
                agent_backend="codex",
                session_anchor="standalone_hfr270_winner",
                agent_name="codex",
            )
        reserved_winner = sessions.get_agent_session_by_id(str(winner))
    finally:
        sessions.close()
    assert winner and reserved_winner
    winner_workdir = Path(str(reserved_winner["workdir"]))
    # The standalone branch mkdirs its own workspace; a scoped reservation only records
    # the Scope's shared path, which exists in production because turns run in it.
    winner_workdir.mkdir(parents=True, exist_ok=True)

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    reloaded = store.get_task(task.id)
    assert reloaded is not None and reloaded.enabled is True

    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    reserved = _spy_reserved_sessions(service)
    notices = _spy_binding_notices(service)

    # THE COMPETING TEARDOWN, from its own engine, after the read the fire acts from.
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            summary = reclaim_bound_definitions(
                conn, pinned, mode=RECLAIM_PAUSE, reason="the bound agent session was cleared"
            )
    finally:
        engine.dispose()
    assert summary["paused"] == 1, (
        f"the competing teardown never landed ({summary!r}), so this test proves nothing"
    )

    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    assert [notice.action for notice in notices] == ["reclaimed"], (
        f"the rebind was not refused, so the leak this test is about never happened: "
        f"{[n.action for n in notices]}"
    )
    assert dispatched == [], "HFR-268 regressed: a refused rebind dispatched its turn"
    assert len(reserved) == 1, (
        f"expected exactly one reservation on the refused path, got {reserved!r}"
    )
    orphan = reserved[0]

    sessions = SQLiteSessionsService(db_path)
    try:
        orphan_row = sessions.get_agent_session_by_id(orphan)
        winner_row = sessions.get_agent_session_by_id(winner)
    finally:
        sessions.close()

    assert orphan_row is None, (
        f"the refused rebind left its replacement session {orphan} behind "
        f"(workdir={None if orphan_row is None else orphan_row.get('workdir')!r}): a live, "
        "unreferenced background session that nothing will ever run, delete or show, and "
        "one more of them for every fire that loses this race"
    )
    orphan_workspace = Path(paths.get_show_page_dir(orphan))
    assert not orphan_workspace.exists(), (
        f"the refused rebind left the Show Page workspace {orphan_workspace} it created"
    )
    if placement == "standalone":
        assert Path(paths.get_show_pages_dir()).is_dir(), (
            "the standalone placement never reached the workspace branch, so the "
            "directory half of this test proves nothing"
        )

    assert winner_row is not None, (
        "the reclaim took a session this call did not reserve: the concurrent winner's "
        "row is gone, so a rebind that DID land has just been orphaned by one that did not"
    )
    assert winner_workdir.is_dir(), (
        f"the reclaim removed {winner_workdir}, which the released reservation did not "
        "create: for the scoped placement that directory is the Scope's, shared with "
        "every session in it"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.session_id == pinned, (
        "the refused rebind was stored anyway"
    )
    live = store.get_task(task.id)
    assert live is not None and live.session_id == pinned and live.enabled is False, (
        "the live store mirror still shows the rebind the database refused "
        f"(session_id={None if live is None else live.session_id!r}, "
        f"enabled={None if live is None else live.enabled!r})"
    )


#: Spelled out rather than imported from ``core.scheduled_tasks``: this is a DURABLE
#: definition-metadata key, so the name itself is the contract a later fire (and any
#: operator reading ``run_definitions.metadata_json``) depends on.
_ORPHAN_RESERVATIONS_KEY = "orphaned_reservations"


def test_a_release_that_fails_records_the_orphan_instead_of_reporting_a_reclaim(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-276 — the consuming end of HFR-270's own cleanup.

    THE PRODUCTION STORY, one layer inside the fix. Same race as HFR-268/HFR-270: a
    ``create_once`` definition's pinned Session is gone, the fire reserves a
    replacement, a second teardown pauses the definition, and the guarded rebind is
    refused. HFR-270 then gives the replacement back. That release is deliberately
    NEVER FATAL -- it runs on a path that is already reporting a failure to the user
    and must not raise a second exception on top of it -- so ``_release_reserved_session``
    catches everything and returns ``False``.

    THE CALLER IGNORED THE ANSWER. ``_recover_pinned_session_binding`` emitted
    ``action="reclaimed"`` unconditionally, so a locked database or an I/O fault during
    the release produced exactly the orphan HFR-270 promises cannot remain, told the
    user the opposite, and lost the only handle on it: the reserved session's id is
    random, it is never written to the definition, and nothing else ever knew it. The
    next fire that loses the same race reserves and leaks another one, untracked.

    THE REQUIREMENT HAS TWO CLAUSES and a truthful terminal outcome only satisfies the
    first. The losing path must not report a completed reclaim, AND it must not keep
    accumulating UNTRACKED reservations -- so the id is recorded durably on the
    definition (``metadata.orphaned_reservations``, the same ``run_definitions``
    metadata the binding-recovery record already uses) and a later fire retries the
    release from it.

    THE FAULT IS INJECTED INSIDE THE RELEASE, which is the only moment that satisfies
    both preconditions at once: the replacement row already exists (the reservation
    committed) and the rebind CAS has already lost (the definition still names the
    session the teardown cleared). The injection asserts both, so a refactor that moved
    the release earlier could not leave this test passing vacuously.
    """
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from storage.session_reclaim import RECLAIM_PAUSE, reclaim_bound_definitions
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_hfr276",
            native_session_id="native-1",
        )
    finally:
        sessions.close()
    assert pinned

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # `/new`: the pinned session goes and the reclaim pauses the definition.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()

    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    reloaded = store.get_task(task.id)
    assert reloaded is not None and reloaded.enabled is True

    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    reserved = _spy_reserved_sessions(service)
    notices = _spy_binding_notices(service)

    # THE COMPETING TEARDOWN, from its own engine, after the read the fire acts from.
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            summary = reclaim_bound_definitions(
                conn, pinned, mode=RECLAIM_PAUSE, reason="the bound agent session was cleared"
            )
    finally:
        engine.dispose()
    assert summary["paused"] == 1, (
        f"the competing teardown never landed ({summary!r}), so this test proves nothing"
    )

    # THE OPERATIONAL FAULT, inside the release itself: the shape a locked database
    # takes on the DELETE, which ``_release_reserved_session`` is required to swallow.
    observed: dict[str, Any] = {"calls": 0, "fail": True}
    original_release = SQLiteSessionsService.release_reserved_agent_session

    def _failing_release(self, session_id, *, reason):  # noqa: ANN001, ANN202
        observed["calls"] += 1
        observed["session_id"] = str(session_id)
        observed["row_existed"] = self.get_agent_session_by_id(str(session_id)) is not None
        observed["stored_session_id"] = _stored_definition_row(task.id)["session_id"]
        if not observed["fail"]:
            return original_release(self, session_id, reason=reason)
        raise OperationalError(
            "DELETE FROM agent_sessions ...", {}, sqlite3.OperationalError("database is locked")
        )

    monkeypatch.setattr(
        SQLiteSessionsService, "release_reserved_agent_session", _failing_release
    )

    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    assert observed["calls"] == 1, (
        f"the release was not attempted exactly once ({observed!r}), so the failure this "
        "test injects never reached the branch under test"
    )
    assert observed["row_existed"] is True, (
        "the replacement session did not exist when the release ran, so the fault was "
        "injected before the reservation committed and no orphan is possible"
    )
    assert observed["stored_session_id"] == pinned, (
        "the definition had already been repointed when the release ran, so the rebind "
        f"CAS did NOT lose ({observed['stored_session_id']!r}) and this is a different case"
    )
    assert dispatched == [], "HFR-268 regressed: a refused rebind dispatched its turn"
    assert len(reserved) == 1, f"expected exactly one reservation, got {reserved!r}"
    orphan = reserved[0]
    assert observed["session_id"] == orphan, (
        "the release was attempted against a session this fire did not reserve"
    )

    sessions = SQLiteSessionsService(db_path)
    try:
        orphan_row = sessions.get_agent_session_by_id(orphan)
    finally:
        sessions.close()
    assert orphan_row is not None, (
        "the injected fault did not actually prevent the release, so there is no orphan "
        "and the reporting/tracking assertions below prove nothing"
    )

    # CLAUSE ONE: the outcome must not claim the session was reclaimed. ``action`` is
    # what the notice, the durable ``binding_recovery`` record and every future
    # consumer key off, so "reclaimed" here is the lie, not a wording preference.
    assert [notice.action for notice in notices] == ["orphaned"], (
        "the failed cleanup was reported as a completed reclaim: the user is told the "
        f"replacement was given back while it is still live ({[n.action for n in notices]})"
    )
    notice = notices[0]
    assert notice.orphaned_session_id == orphan and notice.orphan_tracked is True
    assert orphan in (notice.detail or "") and "could NOT be given back" in (notice.detail or ""), (
        f"the notice does not name the session that leaked: {notice.detail!r}"
    )

    # CLAUSE TWO: the reservation must not be UNTRACKED. Durable row and live mirror
    # both, because a fact that only one of them holds is not a fact the next fire can
    # act on (the round-18 rule applied to a record instead of a rollback).
    durable_metadata = json.loads(_stored_definition_row(task.id)["metadata_json"] or "{}")
    durable_entries = durable_metadata.get(_ORPHAN_RESERVATIONS_KEY) or []
    assert [entry.get("session_id") for entry in durable_entries] == [orphan], (
        f"the orphaned reservation {orphan} was not durably recorded on the definition "
        f"({durable_entries!r}): its id is random and nothing else ever knew it, so the "
        "leak is untracked and no later attempt can find it"
    )
    live = store.get_task(task.id)
    assert live is not None
    live_entries = (live.metadata or {}).get(_ORPHAN_RESERVATIONS_KEY) or []
    assert [entry.get("session_id") for entry in live_entries] == [orphan], (
        f"the live store mirror does not carry the orphan record ({live_entries!r}), so "
        "the next fire reads a definition that has forgotten it"
    )
    assert live.session_id == pinned and live.enabled is False, (
        "recording the orphan restored the binding or the enabled state the teardown "
        f"cleared (session_id={live.session_id!r}, enabled={live.enabled!r})"
    )
    assert _stored_definition_row(task.id)["session_id"] == pinned, (
        "recording the orphan wrote the refused rebind through after all"
    )
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.last_error and orphan in stored.last_error, (
        f"the durable error does not name the leaked session: {stored.last_error!r}"
    )

    # AND THE RECORD IS ACTIONABLE, THROUGH THE FIRE PATH: with the database healthy
    # again, the NEXT FIRE of the same definition -- ``_execute_task``, not a helper
    # called by hand -- reads the record, finishes the release and drops the entry.
    # That is what makes the accumulation tracked-and-retryable rather than merely
    # written down, and it is why the record lives on the definition that reserved it.
    observed["fail"] = False
    next_fire = ScheduledTaskStore()
    refire = next_fire.get_task(task.id)
    assert refire is not None
    assert (refire.metadata or {}).get(_ORPHAN_RESERVATIONS_KEY), (
        "the re-read definition carries no orphan record, so the retry has nothing to act on"
    )
    later = _dispatching_binding_service(tmp_path, next_fire, db_path=db_path)

    asyncio.run(later._execute_task(refire, execution_id="exec-2", disable_one_shot=False))

    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.get_agent_session_by_id(orphan) is None, (
            "the next fire did not release the recorded orphan, so the durable fact leads "
            "nowhere and the reservation accumulates anyway"
        )
    finally:
        sessions.close()
    durable_after = json.loads(_stored_definition_row(task.id)["metadata_json"] or "{}")
    assert not durable_after.get(_ORPHAN_RESERVATIONS_KEY), (
        "the released orphan is still recorded durably, so every later fire retries a "
        f"session that is already gone ({durable_after.get(_ORPHAN_RESERVATIONS_KEY)!r})"
    )
    live_after = next_fire.get_task(task.id)
    assert live_after is not None and not (live_after.metadata or {}).get(
        _ORPHAN_RESERVATIONS_KEY
    ), "the live mirror still records an orphan the retry released"


def test_an_adopted_reservation_is_dropped_from_the_retry_record_without_being_touched(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-279 — the retry record must not hold an adopted winner hostage.

    ONE LAYER INSIDE HFR-276's OWN FIX. ``release_reserved_agent_session`` answers
    ``False`` for three different facts -- gone, adopted, faulted -- and the retry's
    absence-only probe resolved only the first. A tracked reservation that was since
    ADOPTED (a native session dispatched into it, or a definition pointing at it) is
    not absent and can never be released, so its entry stayed on
    ``orphaned_reservations`` forever and every later fire of the original definition
    re-took SQLite's write lock (the release's ``BEGIN IMMEDIATE``) to retry a cleanup
    that cannot succeed.

    THE CONTRACT, all four verdicts of the classification the fix introduces:

    * absent row -- resolved, dropped from the record;
    * native-bound or definition-referenced row -- an adopted winner: resolved and
      dropped WITHOUT being read under the write lock, let alone mutated;
    * still empty-native and unreferenced -- a genuine orphan: released;
    * a classification that cannot read -- kept (its own test below).

    Both adoption arms are exercised, and the winner invariance is the FULL row (the
    HFR-251 lesson): route/anchor, workdir, pins and metadata markers, visibility and
    every timestamp, byte-for-byte -- plus the adopting definition's full row. The
    release spy pins the mechanism itself: no release attempt is made for either
    adopted row or for the absent id, so the winner never pays the loser's lock again.
    """
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_hfr279",
            native_session_id="native-hfr279-pinned",
        )
        adopted_native = sessions.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_C123:runtime_hfr279_native",
        )
        adopted_definition = sessions.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_C123:runtime_hfr279_def",
        )
        genuine = sessions.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_C123:runtime_hfr279_orphan",
        )
        assert pinned and adopted_native and adopted_definition and genuine
        # The first adoption arm: a turn was dispatched into the row after it was
        # recorded as orphaned, so it has a transcript and is a reservation no more.
        bound = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:runtime_hfr279_native",
            native_session_id="native-hfr279-winner",
        )
        assert bound == adopted_native, f"the native bind created a new row ({bound!r})"
    finally:
        sessions.close()

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    # The second adoption arm: another definition adopted the tracked reservation
    # after all, so the row is its live binding and run_definitions points at it.
    winner = store.add_task(
        session_key="",
        session_id=adopted_definition,
        session_policy="create_once",
        prompt="the winner's digest",
        schedule_type="cron",
        cron="30 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    assert store.record_orphaned_reservations(
        task.id,
        [
            {"session_id": adopted_native, "reason": "hfr279 native arm", "at": "2026-07-29T00:00:00Z"},
            {"session_id": adopted_definition, "reason": "hfr279 definition arm", "at": "2026-07-29T00:00:00Z"},
            {"session_id": genuine, "reason": "hfr279 genuine orphan", "at": "2026-07-29T00:00:00Z"},
            {"session_id": "sess-gone-hfr279", "reason": "hfr279 absent id", "at": "2026-07-29T00:00:00Z"},
        ],
    )

    sessions = SQLiteSessionsService(db_path)
    try:
        native_winner_before = sessions.get_agent_session_by_id(adopted_native)
        definition_winner_before = sessions.get_agent_session_by_id(adopted_definition)
    finally:
        sessions.close()
    assert native_winner_before is not None and definition_winner_before is not None
    winner_definition_before = _stored_definition_row(winner.id)

    calls: list = []
    service = _binding_service(tmp_path, store, calls)
    release_attempts: list[str] = []
    original_release = ScheduledTaskService._release_reserved_session

    def _spy_release(self, session_id, *, reason):  # noqa: ANN001, ANN202
        release_attempts.append(str(session_id))
        return original_release(self, session_id, reason=reason)

    monkeypatch.setattr(ScheduledTaskService, "_release_reserved_session", _spy_release)

    fire = store.get_task(task.id)
    assert fire is not None
    asyncio.run(service._execute_task(fire, execution_id="exec-hfr279", disable_one_shot=False))

    # THE MECHANISM: only the genuine orphan was worth a release attempt. Neither
    # adopted row -- nor the absent id -- was made to pay the release's write lock.
    assert release_attempts == [genuine], (
        f"the retry attempted releases against {release_attempts!r}; an adopted winner "
        "or an absent id reaching the guarded release means the classification did not "
        "run, or did not run first"
    )

    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.get_agent_session_by_id(genuine) is None, (
            "the genuine orphan was not released, so the classification resolved the "
            "wrong verdict for the one entry that IS still this definition's to clean"
        )
        native_winner_after = sessions.get_agent_session_by_id(adopted_native)
        definition_winner_after = sessions.get_agent_session_by_id(adopted_definition)
    finally:
        sessions.close()
    assert native_winner_after == native_winner_before, (
        "the natively-bound winner's row changed while its entry was being resolved: "
        f"{native_winner_before!r} -> {native_winner_after!r}"
    )
    assert definition_winner_after == definition_winner_before, (
        "the definition-adopted winner's row changed while its entry was being resolved: "
        f"{definition_winner_before!r} -> {definition_winner_after!r}"
    )
    assert _stored_definition_row(winner.id) == winner_definition_before, (
        "resolving the loser's record mutated the ADOPTING definition's row"
    )

    durable_after = json.loads(_stored_definition_row(task.id)["metadata_json"] or "{}")
    assert not durable_after.get(_ORPHAN_RESERVATIONS_KEY), (
        "the retry record still holds resolved entries "
        f"({durable_after.get(_ORPHAN_RESERVATIONS_KEY)!r}): an adopted or absent id "
        "kept there is retried -- and re-locked -- on every later fire, forever"
    )
    live_after = store.get_task(task.id)
    assert live_after is not None and not (live_after.metadata or {}).get(
        _ORPHAN_RESERVATIONS_KEY
    ), "the live mirror still carries entries the durable record dropped"


def test_a_retry_classification_that_cannot_read_keeps_the_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-279, the fault verdict — a probe that could not read must keep the entry.

    The classification exists to take entries OFF the record, which makes its own
    failure mode the dangerous one: reading "could not classify" as "resolved" would
    drop a genuine orphan on the very fault -- a locked or unavailable database --
    that produced it. The contract is the conservative fourth verdict: keep the
    entry, touch nothing, do not guess. The release spy pins that no release is
    attempted either: a row whose state is unknown is not this fire's to delete.
    """
    from storage.sessions_service import SQLiteSessionsService

    db_path = _binding_env(tmp_path, monkeypatch)

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_hfr279_fault",
            native_session_id="native-hfr279-fault",
        )
        genuine = sessions.reserve_agent_session(
            scope_key="slack::channel::C123",
            agent_backend="codex",
            session_anchor="slack_C123:runtime_hfr279_fault",
        )
    finally:
        sessions.close()
    assert pinned and genuine

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    entry = {"session_id": genuine, "reason": "hfr279 fault arm", "at": "2026-07-29T00:00:00Z"}
    assert store.record_orphaned_reservations(task.id, [entry])

    def _unreadable(self, session_id):  # noqa: ANN001, ANN202
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(
        SQLiteSessionsService, "classify_reserved_agent_session", _unreadable
    )
    release_attempts: list[str] = []
    original_release = ScheduledTaskService._release_reserved_session

    def _spy_release(self, session_id, *, reason):  # noqa: ANN001, ANN202
        release_attempts.append(str(session_id))
        return original_release(self, session_id, reason=reason)

    monkeypatch.setattr(ScheduledTaskService, "_release_reserved_session", _spy_release)

    calls: list = []
    service = _binding_service(tmp_path, store, calls)
    fire = store.get_task(task.id)
    assert fire is not None
    asyncio.run(
        service._execute_task(fire, execution_id="exec-hfr279-fault", disable_one_shot=False)
    )

    assert release_attempts == [], (
        f"a release was attempted ({release_attempts!r}) for an entry whose state the "
        "classification could not establish"
    )
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.get_agent_session_by_id(genuine) is not None, (
            "the unreadable entry's row was deleted anyway"
        )
    finally:
        sessions.close()
    durable_after = json.loads(_stored_definition_row(task.id)["metadata_json"] or "{}")
    kept = durable_after.get(_ORPHAN_RESERVATIONS_KEY) or []
    assert [item.get("session_id") for item in kept] == [genuine], (
        f"the entry did not survive the unreadable classification ({kept!r}); dropping "
        "it loses the only recorded handle on a still-live reservation"
    )


def test_a_locked_database_that_refuses_release_and_record_still_recovers_the_orphan(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-276, the shared-fault half — the durable handle must survive the fault itself.

    THE HOLE THE FIRST HFR-276 REGRESSION LEFT. It monkeypatched the release to raise
    while leaving SQLite healthy, so the ``orphaned_reservations`` record that
    immediately follows always landed. In the stated production cases -- a locked or
    unavailable database, an I/O fault -- both writes go through the SAME database, so
    the fault that refused the release refuses the record too: the code then reported
    ``action="orphaned"`` truthfully, but the id was in a log line and nowhere else,
    untracked and unretryable.

    THE FAULT HERE IS REAL. A second connection takes ``BEGIN IMMEDIATE`` on the same
    database file the instant the release is entered and holds it across the record
    write, so both fail with SQLite's own ``database is locked`` -- the release inside
    its guarded transaction, the record inside ``_write_task``, which RAISES on a
    faulted write (HFR-272) rather than returning ``False``. That raise is itself part
    of the regression: the first fix only ever consumed the boolean, so the production
    fault unwound past the notice branch the monkeypatched test appeared to cover.

    THE DURABLE HANDLE IS THE RESERVATION ROW ITSELF. The row committed before the
    fault -- that is what makes it an orphan -- and it carries the reserving
    definition's id in its own metadata, stamped inside the reservation's transaction:
    if the reservation exists, so does the stamp, no matter what later writes were
    refused. The fault then ends the way a transient fault ends -- by going away,
    writing nothing -- and the NEXT FIRE, from freshly constructed stores (the restart
    shape), recovers the id from the stamp, releases the row, and rebinds. A third
    fire pins the sweep's own safety: the rebound session is stamped AND adopted, and
    must never be swept.
    """
    import sqlite3 as sqlite3_module

    from storage.session_reclaim import RECLAIM_PAUSE, reclaim_bound_definitions
    from storage.sessions_service import (
        RESERVED_BY_DEFINITION_METADATA_KEY,
        SQLiteSessionsService,
    )

    db_path = _binding_env(tmp_path, monkeypatch)

    sessions = SQLiteSessionsService(db_path)
    try:
        pinned = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:definition_hfr276_fault",
            native_session_id="native-hfr276-fault",
        )
    finally:
        sessions.close()
    assert pinned

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        session_id=pinned,
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    # `/new`: the pinned session goes and the reclaim pauses the definition.
    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.delete_agent_sessions(
            scope_key="slack::channel::C123",
            session_anchor_prefix="slack_C123",
        )
    finally:
        sessions.close()
    store = ScheduledTaskStore()
    store.set_enabled(task.id, True)
    reloaded = store.get_task(task.id)
    assert reloaded is not None and reloaded.enabled is True

    service = _dispatching_binding_service(tmp_path, store, db_path=db_path)
    dispatched = service.controller.agent_service.dispatched
    reserved = _spy_reserved_sessions(service)
    notices = _spy_binding_notices(service)

    # THE COMPETING TEARDOWN, from its own engine, after the read the fire acts from:
    # what makes the guarded rebind lose, exactly as in the first HFR-276 regression.
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            summary = reclaim_bound_definitions(
                conn, pinned, mode=RECLAIM_PAUSE, reason="the bound agent session was cleared"
            )
    finally:
        engine.dispose()
    assert summary["paused"] == 1, (
        f"the competing teardown never landed ({summary!r}), so this test proves nothing"
    )

    # THE SHARED FAULT: a real write lock on the real database file, taken the moment
    # the release is entered and lifted only after the record write has failed against
    # it. Nothing about the release or the record is stubbed; both hit SQLite and get
    # SQLite's answer.
    holder = sqlite3_module.connect(str(db_path))
    fault = {"armed": True, "held": False, "release_calls": 0, "record_failures": 0}
    original_release = SQLiteSessionsService.release_reserved_agent_session
    original_record = ScheduledTaskStore.record_orphaned_reservations

    def _release_under_fault(self, session_id, *, reason):  # noqa: ANN001, ANN202
        if fault["armed"]:
            fault["armed"] = False
            holder.execute("BEGIN IMMEDIATE")
            fault["held"] = True
            fault["release_calls"] += 1
        return original_release(self, session_id, reason=reason)

    def _record_under_fault(self, task_id, entries):  # noqa: ANN001, ANN202
        try:
            return original_record(self, task_id, entries)
        except Exception:
            if fault["held"]:
                fault["record_failures"] += 1
            raise
        finally:
            if fault["held"]:
                holder.rollback()
                fault["held"] = False

    monkeypatch.setattr(
        SQLiteSessionsService, "release_reserved_agent_session", _release_under_fault
    )
    monkeypatch.setattr(
        ScheduledTaskStore, "record_orphaned_reservations", _record_under_fault
    )

    asyncio.run(service._execute_task(reloaded, execution_id="exec-1", disable_one_shot=False))

    assert fault["release_calls"] == 1, (
        f"the fault was never armed around a release ({fault!r}), so nothing below "
        "exercises the shared failure"
    )
    assert fault["record_failures"] == 1, (
        f"the record write did not fail under the same lock that refused the release "
        f"({fault!r}); the shared-fault claim is exactly that both fail for one reason"
    )
    assert dispatched == [], "HFR-268 regressed: a refused rebind dispatched its turn"
    assert len(reserved) == 1, f"expected exactly one reservation, got {reserved!r}"
    orphan = reserved[0]

    sessions = SQLiteSessionsService(db_path)
    try:
        orphan_row = sessions.get_agent_session_by_id(orphan)
    finally:
        sessions.close()
    assert orphan_row is not None, (
        "the locked database did not actually prevent the release, so there is no "
        "orphan and nothing below proves recovery"
    )
    orphan_stamp = json.loads(orphan_row.get("metadata_json") or "{}").get(
        RESERVED_BY_DEFINITION_METADATA_KEY
    )
    assert orphan_stamp == task.id, (
        f"the reservation does not name its definition ({orphan_stamp!r}); with the "
        "record refused, that stamp is the only durable handle on the id"
    )

    durable = json.loads(_stored_definition_row(task.id)["metadata_json"] or "{}")
    assert not durable.get(_ORPHAN_RESERVATIONS_KEY), (
        "the orphan record landed despite the lock, so this test degenerated into the "
        "healthy-database case the first regression already covers"
    )
    assert [notice.action for notice in notices] == ["orphaned"], (
        f"the truthful terminal outcome regressed under the real fault "
        f"({[n.action for n in notices]})"
    )
    notice = notices[0]
    assert notice.orphaned_session_id == orphan and notice.orphan_tracked is True, (
        "the stamped orphan was reported as untracked: the row itself durably names "
        f"this definition (tracked={notice.orphan_tracked!r})"
    )
    assert "stamp" in (notice.detail or ""), (
        f"the notice does not say how the next run finds the id: {notice.detail!r}"
    )

    # THE FAULT IS GONE AND THE PROCESS RESTARTED: fresh store, fresh service, nothing
    # in memory. The only handles that survive are the database rows themselves.
    next_fire = ScheduledTaskStore()
    refire = next_fire.get_task(task.id)
    assert refire is not None
    assert not (refire.metadata or {}).get(_ORPHAN_RESERVATIONS_KEY), (
        "the restarted store carries an orphan record the locked database supposedly "
        "refused; the fault injection above did not do what it claims"
    )
    later = _dispatching_binding_service(tmp_path, next_fire, db_path=db_path)
    asyncio.run(later._execute_task(refire, execution_id="exec-2", disable_one_shot=False))

    sessions = SQLiteSessionsService(db_path)
    try:
        assert sessions.get_agent_session_by_id(orphan) is None, (
            "the next fire did not recover the orphan from its stamp: with the record "
            "refused, the leak is permanent and HFR-276's durability claim is false"
        )
    finally:
        sessions.close()

    # THE FIRE ALSO REBOUND, and the rebound session is stamped AND adopted: the sweep
    # must classify it as a winner and never touch it (HFR-279 guarding HFR-276).
    rebound = next_fire.get_task(task.id)
    assert rebound is not None and rebound.session_id and rebound.session_id != pinned, (
        f"the recovery fire did not rebind (session_id={None if rebound is None else rebound.session_id!r})"
    )
    sessions = SQLiteSessionsService(db_path)
    try:
        adopted_row_before = sessions.get_agent_session_by_id(rebound.session_id)
    finally:
        sessions.close()
    assert adopted_row_before is not None
    assert json.loads(adopted_row_before.get("metadata_json") or "{}").get(
        RESERVED_BY_DEFINITION_METADATA_KEY
    ) == task.id, "the rebound session is not stamped; the durable handle is not being written"

    third_fire = ScheduledTaskStore()
    third = third_fire.get_task(task.id)
    assert third is not None
    third_service = _dispatching_binding_service(tmp_path, third_fire, db_path=db_path)
    asyncio.run(third_service._execute_task(third, execution_id="exec-3", disable_one_shot=False))

    sessions = SQLiteSessionsService(db_path)
    try:
        adopted_row_after = sessions.get_agent_session_by_id(rebound.session_id)
    finally:
        sessions.close()
    assert adopted_row_after is not None, (
        "a later fire swept the definition's OWN adopted session: the stamp made the "
        "live binding look like an orphan and the sweep destroyed it"
    )
    still_bound = third_fire.get_task(task.id)
    assert still_bound is not None and still_bound.session_id == rebound.session_id, (
        f"the third fire re-pointed the definition (session_id="
        f"{None if still_bound is None else still_bound.session_id!r})"
    )


#: Every guarded writer that mutates the cached ``ScheduledTask`` before persisting it.
_TASK_MIRROR_WRITERS = {
    "set_enabled": lambda store, task_id: store.set_enabled(task_id, False),
    "mark_task_result": lambda store, task_id: store.mark_task_result(task_id, error="boom"),
    "record_binding_recovery": lambda store, task_id: store.record_binding_recovery(
        task_id, {"signature": "sig", "action": "paused"}
    ),
    "update_task": lambda store, task_id: store.update_task(
        task_id,
        **{**_TASK_FIXTURE_PAYLOAD, "name": "renamed"},
    ),
}

#: Values a round trip through ``run_definitions`` returns unchanged, so a baseline
#: mismatch cannot be mistaken for a mirror the failed write left ahead.
_TASK_FIXTURE_PAYLOAD = {
    "name": "original",
    "session_key": "slack::channel::C1",
    "prompt": "send digest",
    "schedule_type": "cron",
    "post_to": None,
    "deliver_key": None,
    "cron": "0 * * * *",
    "run_at": None,
    "timezone_name": "UTC",
}


def _fail_the_definition_write(engine) -> dict:
    """Make the ``run_definitions`` write itself fail, the way a real fault would."""
    from sqlalchemy import event

    state: dict = {"fired": 0}

    def _boom(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        normalized = " ".join(statement.split()).upper()
        if not normalized.startswith(("UPDATE RUN_DEFINITIONS", "INSERT INTO RUN_DEFINITIONS")):
            return
        state["fired"] += 1
        raise RuntimeError("definition write failed: disk I/O error")

    event.listens_for(engine, "before_cursor_execute")(_boom)
    return state


@pytest.mark.parametrize("writer", list(_TASK_MIRROR_WRITERS))
def test_a_definition_write_that_raises_leaves_no_live_task_mirror_ahead_of_the_database(
    writer: str,
) -> None:
    """HFR-272 — the task store's twin of HFR-271.

    ``ScheduledTaskStore`` is the same write-through cache with the same choke point:
    every writer edits the cached ``ScheduledTask`` and hands the whole row to
    ``_write_task``, and ``_write_task`` reloaded only when the compare-and-set RETURNED
    ``False``. A raised write -- a full disk, a locked database, a fault between the
    statement and the commit -- rolls the transaction back just as completely and used to
    leave the mutation in the mirror, where ``reconcile_jobs`` schedules from it and
    ``_read_state`` derives the NEXT compare-and-set's expectation from it. Found by
    applying HFR-271's rule (a rollback is proven only when the durable row and the live
    object agree) to the sibling store rather than by a second production report.
    """
    store = ScheduledTaskStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    task = store.add_task(**_TASK_FIXTURE_PAYLOAD)

    durable_before = ScheduledTaskStore().get_task(task.id)
    live_before = store.get_task(task.id)
    assert durable_before is not None and live_before is not None
    assert live_before.to_dict() == durable_before.to_dict(), (
        "the fixture starts with the mirror already disagreeing with the row, so the "
        "assertion below would pass or fail for the wrong reason"
    )
    boom = _fail_the_definition_write(store._sqlite.engine)

    with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
        _TASK_MIRROR_WRITERS[writer](store, task.id)

    assert boom["fired"] >= 1, (
        f"{writer} never wrote a run_definitions row, so no fault was injected and this "
        "test proves nothing"
    )

    durable_after = ScheduledTaskStore().get_task(task.id)
    assert durable_after is not None
    assert durable_after.to_dict() == durable_before.to_dict(), (
        f"{writer} committed something despite the injected fault; this test can no "
        "longer tell a rolled-back mirror from a written one"
    )

    live = store.get_task(task.id)
    assert live is not None, f"{writer} dropped the task from the live store"
    assert live.to_dict() == durable_after.to_dict(), (
        f"{writer} left the live mirror ahead of the database. The transaction rolled "
        "back; the cached ScheduledTask kept the edit. Differing fields: "
        + repr(
            {
                key: (value, durable_after.to_dict().get(key))
                for key, value in live.to_dict().items()
                if durable_after.to_dict().get(key) != value
            }
        )
    )


def test_a_failed_create_leaves_no_phantom_task_in_the_live_store() -> None:
    """HFR-275 — the task store's twin: the create entry point rolls back too.

    ``upsert_task`` is the one writer that can put an id in the mirror the database has
    NEVER seen. The caller is told the task could not be created, while ``reconcile_jobs``
    -- which schedules out of exactly this dict -- fires its prompt into the channel on
    the next tick, with no durable row to stop it and nothing that will reload it away.
    """
    store = ScheduledTaskStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    before = {task.id for task in store.list_tasks()}
    boom = _fail_the_definition_write(store._sqlite.engine)

    with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
        store.add_task(**_TASK_FIXTURE_PAYLOAD)

    assert boom["fired"] >= 1, "no run_definitions write was attempted, so nothing failed"
    phantom = {task.id for task in store.list_tasks()} - before
    assert not phantom, (
        f"the failed create left {sorted(phantom)} in the live store: a task the database "
        "never accepted, that reconcile_jobs will schedule and fire anyway"
    )
    assert not {task.id for task in ScheduledTaskStore().list_tasks()} - before, (
        "the create committed despite the injected fault, so this test proves nothing"
    )


def test_a_failed_delete_does_not_stop_a_task_the_database_still_has() -> None:
    """HFR-275 — the delete entry point, the same class in the safer direction.

    ``remove_task`` drops the entry before the soft delete. Absent reads as "gone" and
    stops the schedule, which is the conservative direction, but it is silent and does
    NOT heal: the row is still there and UNCHANGED, so ``maybe_reload`` sees no external
    write and the task the user was told could not be deleted simply stops firing until
    the process restarts.
    """
    store = ScheduledTaskStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    task = store.add_task(**_TASK_FIXTURE_PAYLOAD)
    boom = _fail_the_definition_write(store._sqlite.engine)

    with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
        store.remove_task(task.id)

    assert boom["fired"] >= 1, "no run_definitions write was attempted, so nothing failed"
    durable = ScheduledTaskStore().get_task(task.id)
    assert durable is not None, (
        "the delete committed despite the injected fault, so this test proves nothing"
    )
    live = store.get_task(task.id)
    assert live is not None, (
        f"the failed delete dropped task {task.id} from the live store while the database "
        "still has it: it stops firing, silently, until the process restarts"
    )
    assert live.to_dict() == durable.to_dict()


#: The two statement shapes the fault below intercepts. The list read is the one
#: ``ScheduledTaskStore.load`` issues (it is the only ``run_definitions`` SELECT that
#: filters by ``definition_type``), so the guard's own ``SELECT id ... LIMIT 1`` still
#: runs and the write is genuinely ATTEMPTED before it fails.
_DEFINITION_LIST_READ_MARKERS = ("FROM RUN_DEFINITIONS", "DEFINITION_TYPE = ?")


def _fail_the_definition_write_and_the_reload(engine) -> dict:
    """A transient fault that takes out the guarded write AND the recovery read.

    The real shape of HFR-277: whatever broke the write (a locked database, a full
    disk, an I/O error) is still there a millisecond later when the store tries to
    reload, so the recovery ``load`` fails too and the entry is dropped. Flip
    ``state["live"]`` to ``False`` to end the fault WITHOUT committing anything --
    which is the whole point, because a commit would bump ``PRAGMA data_version`` and
    heal the mirror for a reason that has nothing to do with the fix.
    """
    from sqlalchemy import event

    state: dict = {"writes": 0, "reads": 0, "live": True}

    def _boom(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if not state["live"]:
            return
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(("UPDATE RUN_DEFINITIONS", "INSERT INTO RUN_DEFINITIONS")):
            state["writes"] += 1
            raise RuntimeError("definition write failed: disk I/O error")
        if all(marker in normalized for marker in _DEFINITION_LIST_READ_MARKERS):
            state["reads"] += 1
            raise RuntimeError("definition read failed: disk I/O error")

    event.listens_for(engine, "before_cursor_execute")(_boom)
    return state


def test_a_dropped_task_mirror_recovers_with_no_unrelated_commit_to_wake_it() -> None:
    """HFR-277 — the consuming end of HFR-271/272's own recovery path.

    THE DEFECT, and it is one WE introduced. When a guarded write fails and the
    immediate recovery ``load`` fails too, ``_reload_after_lost_write`` drops the entry
    from the live map -- deliberately, because an absent definition is safer to act on
    than a mutated one the database never accepted -- and promises that ``maybe_reload``
    will bring it back "once the database is reachable again". It did not. The only thing
    ``maybe_reload`` consulted was ``SqliteInvalidationProbe``, i.e. ``PRAGMA
    data_version``, which moves only when another connection COMMITS. The failed write
    ROLLED BACK, so data_version never moved: every later tick answered "nothing
    changed", and the task stayed durably enabled in SQLite and invisible in-process
    until the service restarted or some unrelated write happened to bump the counter.
    ``reconcile_jobs`` schedules out of exactly this dict, so a cron task simply stopped
    firing, with the row still saying it is enabled.

    THE CLAUSE THAT IS THE TEST: nothing commits between the failure and the recovery.
    A witness probe on its own connection asserts data_version is unchanged at that
    instant, so a mirror that comes back can only have come back because the store
    remembered it had to reload -- not because another writer woke it up. The fault ends
    the way a transient fault does, by going away, not by writing anything.
    """
    from storage.db import SqliteInvalidationProbe, create_sqlite_engine

    store = ScheduledTaskStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    task = store.add_task(**_TASK_FIXTURE_PAYLOAD)
    durable_before = ScheduledTaskStore().get_task(task.id)
    assert durable_before is not None and durable_before.enabled, (
        "the fixture must start from a definition the database has, and has enabled"
    )

    # Settle the store's own probe on the fixture's commits first: a pending
    # data_version bump would reload the mirror below for the wrong reason.
    store.maybe_reload()
    assert store.maybe_reload() is False, "the store's probe is not settled"
    witness_engine = create_sqlite_engine(store._sqlite.db_path)
    witness = SqliteInvalidationProbe(witness_engine)
    witness.has_external_write()
    assert witness.has_external_write() is False, "the witness probe is not settled"

    fault = _fail_the_definition_write_and_the_reload(store._sqlite.engine)
    try:
        with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
            store.mark_task_result(task.id, error="boom")

        assert fault["writes"] >= 1, "no run_definitions write was attempted"
        assert fault["reads"] >= 1, (
            "the recovery reload was never attempted, so the entry was not dropped for "
            "the reason this test is about"
        )
        assert store.get_task(task.id) is None, (
            "the failed write did not drop the mirror entry, so there is nothing for "
            "maybe_reload to recover and this test proves nothing"
        )
        assert [item.id for item in store.list_tasks()] == [], (
            "the dropped entry is still listed; the precondition is a mirror that has "
            "LOST the definition"
        )

        # The fault clears the way a transient one does: nothing is written.
        fault["live"] = False
        assert witness.has_external_write() is False, (
            "something COMMITTED between the failed write and the reload below. A "
            "data_version bump heals the mirror on its own, so this test would pass "
            "without the fix"
        )

        assert store.maybe_reload() is True, (
            "maybe_reload reported 'nothing changed' for a mirror the store itself knows "
            "is incomplete. data_version cannot see a rolled-back write, so the dropped "
            "task stays invisible to reconcile_jobs until the process restarts"
        )
        live = store.get_task(task.id)
        assert live is not None, (
            f"task {task.id} is still missing from the live store after a reload; it is "
            "enabled in SQLite and will never be scheduled again"
        )
        assert live.to_dict() == durable_before.to_dict(), (
            "the recovered entry does not match the durable row. Differing fields: "
            + repr(
                {
                    key: (value, durable_before.to_dict().get(key))
                    for key, value in live.to_dict().items()
                    if durable_before.to_dict().get(key) != value
                }
            )
        )
        assert [item.id for item in store.list_tasks()] == [task.id]
        assert store.maybe_reload() is False, (
            "the store keeps reloading unconditionally; the flag must be cleared by the "
            "reload that repaired the mirror"
        )
    finally:
        witness.close()
        witness_engine.dispose()

    durable_after = ScheduledTaskStore().get_task(task.id)
    assert durable_after is not None and durable_after.to_dict() == durable_before.to_dict(), (
        "the durable row changed, so the failed write committed something and the "
        "recovery above was reading a different definition than the one that was dropped"
    )

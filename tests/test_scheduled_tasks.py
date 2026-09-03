from __future__ import annotations

import asyncio
import ast
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
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
from core import command_runner
from config import paths
from config.v2_settings import make_thread_native_id
from core.controller import Controller
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.message_mirror import mirror_harness_inbound
from core.message_output import MessageOutput, stop_output_for
import core.process_isolation as process_isolation
from core.process_isolation import (
    capture_spawned_process_identity,
    fingerprint_process_marker,
    isolated_subprocess_kwargs,
    process_identity_subprocess_env,
    serialize_process_identity,
)
from core.run_settlement import (
    RUN_INTERRUPTION_REASONS,
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_RESTARTED,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
    SETTLED_BY_TURN_ONLY_RESULT,
    SETTLEMENT_I18N_KEYS,
    SETTLEMENT_TERMINAL_STATUS,
    SETTLEMENTS_WITHOUT_RESULT,
    TEARDOWN_SETTLEMENT_ENTRY_POINTS,
    TEARDOWN_SETTLEMENT_MATRIX,
    TEARDOWN_SETTLEMENT_SURFACES,
)
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_work import (
    RuntimeWorkLane,
    RuntimeWorkRegistrationToken,
    RuntimeWorkSupervisor,
)
from core.services.dispatch import SOURCE_SCHEDULED, TurnDispatchOutcome
from core.session_activities import SessionActivityRegistry
from core.session_turns import SessionTurnManager
from core.scheduled_tasks import (
    BINDING_FOLLOWS_SESSION_METADATA_KEY,
    BINDING_RECOVERY_METADATA_KEY,
    FAILURE_CODE_SESSION_TURN_GATE_UNAVAILABLE,
    ParsedSessionKey,
    SESSION_TURN_GATE_UNAVAILABLE_I18N_KEY,
    ScheduledTask,
    ScheduledTaskService,
    ScheduledTaskStore,
    SessionBindingChange,
    TaskDispatchResult,
    TaskExecutionResult,
    TaskExecutionRequest,
    TaskExecutionStore,
    _TASK_RESULT_NOT_RECORDED_I18N_KEY,
    _agent_run_message_for_request,
    build_session_key_for_context,
    normalize_agent_run_delivery_intent,
    parse_session_key,
    resolve_session_id_target,
    session_anchor_for_target,
)
from core.watch_worker import WATCH_WORKER_ERROR_PREFIX
from vibe.i18n import t as i18n_t
from modules.im import MessageContext
from storage import message_deliveries
from storage.db import create_sqlite_engine
from storage.background import (
    COMMAND_SNAPSHOT_METADATA_KEY,
    COMMAND_TIMED_OUT_METADATA_KEY,
    DefinitionWriteConflict,
    SQLiteBackgroundTaskStore,
    TASK_SCHEDULE_CONSUMED_METADATA_KEY,
    TASK_LAST_RESULT_STATUS_METADATA_KEY,
    definition_lifecycle_detail,
    resolve_run_at,
    task_schedule_generation,
)
from storage.models import (
    agent_events,
    agent_runs,
    agent_sessions,
    metadata,
    messages,
    run_definitions,
    vault_requests,
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
        # ``func`` is retained so a test can fire a registered job exactly the way
        # APScheduler does: ``await job.func(*job.args)``.
        self.jobs[id] = SimpleNamespace(id=id, func=func, trigger=trigger, args=args)

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def get_jobs(self):
        return list(self.jobs.values())


async def _fire_and_finish_scheduled_task(
    service: ScheduledTaskService,
    task_id: str,
) -> None:
    """Fire through the scheduler producer, then let the request owner consume it."""

    await service._run_task(task_id)
    pending = [
        request
        for request in service.request_store.list_pending()
        if request.task_id == task_id and request.source_kind == "scheduler"
    ]
    for request in pending:
        await service._process_pending_request(request)
    executions = tuple(service._inflight_executions.values())
    if executions:
        await asyncio.gather(*executions)


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


def test_hfr_172_task_definition_wake_follows_commit_and_not_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import inbox_events

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    events: list[tuple[str, dict[str, Any]]] = []
    subscription = inbox_events.bus.subscribe_callback(
        lambda event_type, payload: events.append((event_type, payload))
    )
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", True)
    original_save = store._save
    entered = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def blocking_save() -> None:
        entered.set()
        assert release.wait(timeout=1)
        original_save()

    def add_task() -> None:
        try:
            store.add_task(
                name="Wake contract",
                session_key="avibe::agent::default",
                prompt="hello",
                schedule_type="at",
                run_at="2026-08-04T01:00:00+00:00",
                timezone_name="UTC",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failure.append(exc)

    monkeypatch.setattr(store, "_save", blocking_save)
    writer = threading.Thread(target=add_task)
    writer.start()
    assert entered.wait(timeout=1)
    assert events == []
    release.set()
    writer.join(timeout=1)
    assert not writer.is_alive()
    assert failure == []
    assert events == [
        (inbox_events.DEFINITIONS_UPDATED_EVENT, {"definition_type": "scheduled"})
    ]

    events.clear()
    task = store.list_tasks()[0]
    monkeypatch.setattr(store, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.set_enabled(task.id, False)
    assert events == []
    inbox_events.bus.unsubscribe(subscription)


def test_hfr_172_losing_task_definition_cas_emits_no_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import inbox_events

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", True)
    store = ScheduledTaskStore()
    task = store.add_task(
        name="CAS wake contract",
        session_key="avibe::agent::default",
        prompt="hello",
        schedule_type="at",
        run_at="2026-08-04T01:00:00+00:00",
        timezone_name="UTC",
    )
    events: list[tuple[str, dict[str, Any]]] = []
    subscription = inbox_events.bus.subscribe_callback(
        lambda event_type, payload: events.append((event_type, payload))
    )
    assert store.sqlite_backend is not None
    monkeypatch.setattr(store.sqlite_backend, "upsert_scheduled_task", lambda *args, **kwargs: False)

    with pytest.raises(DefinitionWriteConflict):
        store.set_enabled(task.id, False)
    assert events == []
    inbox_events.bus.unsubscribe(subscription)


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


def _sqlite_backed_task_store(tmp_path: Path) -> tuple[ScheduledTaskStore, SQLiteBackgroundTaskStore]:
    """A scheduled-task store on its own SQLite file under ``tmp_path``."""

    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    return store, sqlite


def _reloaded_task_store(tmp_path: Path, sqlite: SQLiteBackgroundTaskStore) -> ScheduledTaskStore:
    reloaded = ScheduledTaskStore(tmp_path / "scheduled_tasks-reloaded.json")
    reloaded._sqlite = sqlite
    reloaded.load()
    return reloaded


def _definition_row(sqlite: SQLiteBackgroundTaskStore, definition_id: str) -> dict[str, Any]:
    with sqlite.engine.connect() as conn:
        row = (
            conn.execute(select(run_definitions).where(run_definitions.c.id == definition_id))
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


def test_store_round_trip_persists_shell_command_task(tmp_path: Path) -> None:
    store, sqlite = _sqlite_backed_task_store(tmp_path)
    task = store.add_task(
        name="Nightly sync",
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="0 3 * * *",
        timezone_name="UTC",
        shell_command="./sync.sh; exit 0",
        timeout_seconds=300.0,
        metadata={"on_failure": "agent"},
    )

    saved = _reloaded_task_store(tmp_path, sqlite).get_task(task.id)

    assert saved is not None
    assert saved.shell_command == "./sync.sh; exit 0"
    assert saved.command is None
    assert saved.timeout_seconds == 300.0
    assert saved.last_exit_code is None
    assert saved.has_command is True
    assert saved.on_failure == "agent"
    # A command task needs no session, and the inference must not invent one.
    assert saved.session_policy is None


def test_store_round_trip_persists_argv_command_task(tmp_path: Path) -> None:
    store, sqlite = _sqlite_backed_task_store(tmp_path)
    task = store.add_task(
        name="Echo",
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="*/5 * * * *",
        timezone_name="UTC",
        command=["/bin/echo", "hi"],
    )

    saved = _reloaded_task_store(tmp_path, sqlite).get_task(task.id)

    assert saved is not None
    assert saved.command == ["/bin/echo", "hi"]
    assert saved.shell_command is None
    assert saved.timeout_seconds is None
    assert saved.has_command is True
    assert saved.on_failure == "none"


def test_message_task_keeps_command_columns_null(tmp_path: Path) -> None:
    store, sqlite = _sqlite_backed_task_store(tmp_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )

    row = _definition_row(sqlite, task.id)
    saved = _reloaded_task_store(tmp_path, sqlite).get_task(task.id)

    assert row["command_json"] is None
    assert row["shell_command"] is None
    assert row["timeout_seconds"] is None
    assert row["last_exit_code"] is None
    assert saved is not None
    assert saved.shell_command is None
    assert saved.command is None
    assert saved.timeout_seconds is None
    assert saved.last_exit_code is None
    assert saved.has_command is False


def test_from_dict_defaults_command_fields_for_legacy_payloads() -> None:
    task = ScheduledTask.from_dict(
        {
            "id": "task-legacy",
            "session_key": "slack::channel::C123",
            "prompt": "send digest",
            "schedule_type": "cron",
            "cron": "0 * * * *",
        }
    )

    assert task.shell_command is None
    assert task.command is None
    assert task.timeout_seconds is None
    assert task.last_exit_code is None
    assert task.has_command is False
    assert task.on_failure == "none"
    assert task.to_dict()["shell_command"] is None


def test_update_task_only_rewrites_command_fields_when_gated(tmp_path: Path) -> None:
    store, sqlite = _sqlite_backed_task_store(tmp_path)
    task = store.add_task(
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="0 3 * * *",
        timezone_name="UTC",
        shell_command="./sync.sh",
        timeout_seconds=120.0,
    )

    preserved = store.update_task(
        task.id,
        name="Renamed",
        session_key="",
        prompt="",
        schedule_type="cron",
        post_to=None,
        deliver_key=None,
        cron="0 4 * * *",
        run_at=None,
        timezone_name="UTC",
    )

    assert preserved.shell_command == "./sync.sh"
    assert preserved.timeout_seconds == 120.0
    assert _reloaded_task_store(tmp_path, sqlite).get_task(task.id).shell_command == "./sync.sh"

    replaced = store.update_task(
        task.id,
        name="Renamed",
        session_key="",
        prompt="",
        schedule_type="cron",
        post_to=None,
        deliver_key=None,
        cron="0 4 * * *",
        run_at=None,
        timezone_name="UTC",
        command=["/bin/echo", "hi"],
        timeout_seconds=45.0,
        update_command_fields=True,
    )

    reloaded = _reloaded_task_store(tmp_path, sqlite).get_task(task.id)
    assert replaced.shell_command is None
    assert replaced.command == ["/bin/echo", "hi"]
    assert reloaded.command == ["/bin/echo", "hi"]
    assert reloaded.shell_command is None
    assert reloaded.timeout_seconds == 45.0


def test_last_exit_code_survives_an_unrelated_definition_write(tmp_path: Path) -> None:
    """A definition edit must not wipe the exit code a command run recorded.

    Every scheduled-task write is a FULL-ROW upsert, so a hardcoded ``None`` in the
    column mapping would clear the exit code on the next rename or ``mark_task_result``.
    """

    store, sqlite = _sqlite_backed_task_store(tmp_path)
    task = store.add_task(
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="0 3 * * *",
        timezone_name="UTC",
        shell_command="./sync.sh",
    )
    sqlite.upsert_scheduled_task({**task.to_dict(), "last_exit_code": 3})
    store.load()

    assert store.get_task(task.id).last_exit_code == 3

    store.update_task(
        task.id,
        name="Renamed",
        session_key="",
        prompt="",
        schedule_type="cron",
        post_to=None,
        deliver_key=None,
        cron="0 4 * * *",
        run_at=None,
        timezone_name="UTC",
    )
    assert store.mark_task_result(task.id, error=None) is True

    assert _definition_row(sqlite, task.id)["last_exit_code"] == 3
    assert _reloaded_task_store(tmp_path, sqlite).get_task(task.id).last_exit_code == 3


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


def test_build_context_forwards_vault_callback_outcome_metadata() -> None:
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

    context = asyncio.run(
        service._build_context(
            target,
            execution_id="exec-vault",
            trigger_kind="agent_run",
            metadata={
                "source_kind": "callback",
                "source_actor": "vault:vrq_1",
                "vault_request_type": "access",
                "vault_request_status": "denied",
            },
        )
    )

    assert context.platform_specific["vault_request_type"] == "access"
    assert context.platform_specific["vault_request_status"] == "denied"


def test_build_context_forwards_callback_source_session_id() -> None:
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

    context = asyncio.run(
        service._build_context(
            target,
            execution_id="exec-callback-source",
            trigger_kind="agent_run",
            metadata={
                "source_kind": "callback",
                "source_actor": "run-parent",
                "source_session_id": "ses-source",
            },
        )
    )

    assert context.platform_specific["source_session_id"] == "ses-source"


def test_build_context_carries_the_definition_name_for_display() -> None:
    """The prompt echo names what fired, so the label cannot be an opaque id."""

    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    store = ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    service = ScheduledTaskService(controller=controller, store=store)
    target = parse_session_key("slack::channel::C123")

    store.get_task = (
        lambda task_id: SimpleNamespace(name="Daily digest", prompt="summarize open PRs")
        if task_id == "task-1"
        else None
    )
    store.get_watch_definition = (
        lambda definition_id: {"name": "Deploy watch", "message": "check the deploy"}
        if definition_id == "watch-1"
        else None
    )

    task_context = asyncio.run(service._build_context(target, execution_id="exec-1", task_id="task-1"))
    watch_context = asyncio.run(
        service._build_context(target, execution_id="exec-2", task_id="watch-1", trigger_kind="watch")
    )
    unknown_context = asyncio.run(service._build_context(target, execution_id="exec-3", task_id="gone"))
    anonymous_context = asyncio.run(service._build_context(target, execution_id="exec-4", trigger_kind="agent_run"))

    assert task_context.platform_specific["task_definition_name"] == "Daily digest"
    assert watch_context.platform_specific["task_definition_name"] == "Deploy watch"
    assert unknown_context.platform_specific["task_definition_name"] is None
    assert anonymous_context.platform_specific["task_definition_name"] is None

    # The definition's STORED instruction travels next to the name: a watch / hook /
    # webhook prompt appends machine-generated evidence (waiter stdout, a failure
    # report) that the outward echo must never publish, so it echoes this instead.
    assert task_context.platform_specific["harness_display_prompt"] == "summarize open PRs"
    assert watch_context.platform_specific["harness_display_prompt"] == "check the deploy"
    assert unknown_context.platform_specific["harness_display_prompt"] is None
    assert anonymous_context.platform_specific["harness_display_prompt"] is None


def test_build_context_prefers_an_explicit_display_prompt_from_the_request() -> None:
    """A producer that composes the prompt itself can stamp the user-authored part.

    The definition lookup covers task/watch rows; a request whose prompt was composed
    somewhere else (a hook send with no definition row) can pass the instruction
    through its metadata instead, and that wins over the stored value.
    """

    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    store = ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    service = ScheduledTaskService(controller=controller, store=store)
    store.get_task = lambda task_id: SimpleNamespace(name="Deploy watch", prompt="stored instruction")
    target = parse_session_key("slack::channel::C123")

    context = asyncio.run(
        service._build_context(
            target,
            execution_id="exec-1",
            task_id="task-1",
            trigger_kind="hook",
            metadata={"harness_display_prompt": "explicit instruction"},
        )
    )

    assert context.platform_specific["harness_display_prompt"] == "explicit instruction"


def test_build_context_survives_a_store_that_cannot_name_the_definition() -> None:
    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_args, **_kwargs: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
    )
    store = ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    service = ScheduledTaskService(controller=controller, store=store)

    def _boom(_identifier):
        raise RuntimeError("store unavailable")

    store.get_task = _boom
    target = parse_session_key("slack::channel::C123")

    context = asyncio.run(service._build_context(target, execution_id="exec-1", task_id="task-1"))

    # Display copy must never be able to fail the run it describes.
    assert context.platform_specific["task_definition_name"] is None
    assert context.platform_specific["harness_display_prompt"] is None
    assert context.platform_specific["task_definition_id"] == "task-1"


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

    asyncio.run(_fire_and_finish_scheduled_task(service, task.id))
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


def test_hfr_161_scheduler_only_enqueues_one_successor_behind_active_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    request_store = TaskExecutionStore()
    notifications: list[tuple[RuntimeWorkLane, ...]] = []
    controller = SimpleNamespace(
        platform_settings_managers={"slack": object()},
        runtime_work_supervisor=SimpleNamespace(
            notify=lambda *lanes: notifications.append(lanes)
        ),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=store,
        request_store=request_store,
    )

    async def _exercise() -> None:
        await service._run_task(task.id)
        first = request_store.list_pending()
        assert len(first) == 1
        claimed = request_store.claim(first[0].id)
        assert claimed is not None

        await service._run_task(task.id)
        assert request_store.list_pending() == []

        assert request_store.mark_execution_started(claimed.id)

        await asyncio.gather(
            service._run_task(task.id),
            service._run_task(task.id),
            service._run_task(task.id),
        )

    asyncio.run(_exercise())

    queued = request_store.list_pending()
    assert len(queued) == 1
    assert queued[0].task_id == task.id
    assert len(notifications) == 2
    assert all(lanes == (RuntimeWorkLane.REQUESTS,) for lanes in notifications)


def test_hfr_161_file_scheduler_fence_includes_claimed_pre_execution_run(
    tmp_path: Path,
) -> None:
    task_store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = task_store.add_task(
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    request_store = TaskExecutionStore(tmp_path / "task_requests")

    first = request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        suppress_scheduler_successor=True,
    )
    assert first is not None
    assert request_store.claim(first.id) is not None

    assert request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        suppress_scheduler_successor=True,
    ) is None

    assert request_store.mark_execution_started(first.id)
    successor = request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        suppress_scheduler_successor=True,
    )
    assert successor is not None
    assert request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        suppress_scheduler_successor=True,
    ) is None


def test_hfr_158_request_scan_fills_page_across_recovery_and_queued_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    recovering = request_store.enqueue_hook_send(
        session_key="slack::channel::recovering",
        prompt="recover",
    )
    assert request_store.claim(recovering.id) is not None
    queued = request_store.enqueue_hook_send(
        session_key="slack::channel::recovering",
        prompt="queued",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        request_store=request_store,
    )
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.REQUESTS,
    )

    items, _has_more = handler.scan(
        limit=2,
        occupied=frozenset(),
        cursor=None,
    )

    assert [item.observation[0] for item in items] == ["fallback", "queued"]
    assert items[0].partition_key == "key:slack::channel::recovering"
    assert items[1].partition_key == items[0].partition_key
    assert items[1].observation[1].id == queued.id


def test_hfr_166_activity_scan_never_sleeps_ahead_of_due_runtime() -> None:
    scheduled: list[tuple[RuntimeWorkLane, float]] = []

    class _Registry:
        @staticmethod
        def scan_recovered_output_runtimes(
            *,
            limit,
            cursor,
            grace_seconds,
        ):  # noqa: ANN001, ANN202
            del limit, cursor
            assert grace_seconds("claude") == 10.0
            return [("claude", "b-due")], False, 30.0, "claude\x1fb-due"

    service = SimpleNamespace(
        _activity_registry=lambda: _Registry(),
        _activity_output_grace_seconds=lambda _backend: 10.0,
        _schedule_runtime_work_wake=(
            lambda lane, delay: scheduled.append((lane, delay))
        ),
    )
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.ACTIVITY_OUTPUTS,
    )

    items, has_more = handler.scan(
        limit=1,
        occupied=frozenset(),
        cursor=None,
    )

    assert [item.partition_key for item in items] == ["claude\x1fb-due"]
    assert has_more is False
    assert scheduled == [(RuntimeWorkLane.ACTIVITY_OUTPUTS, 30.0)]


@pytest.mark.anyio
async def test_hfr_166_recovered_runtime_rewinds_until_every_batch_is_drained() -> None:
    activities = [SimpleNamespace(id="activity-a"), SimpleNamespace(id="activity-b")]

    class _Registry:
        def claim_completed_output(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return activities.pop(0) if activities else None

        @staticmethod
        def has_completed_output(*_args):  # noqa: ANN002, ANN205
            return bool(activities)

        @staticmethod
        def has_recovered_output(*_args):  # noqa: ANN002, ANN205
            return bool(activities)

    registry = _Registry()
    wake = Mock()
    service = SimpleNamespace(
        _activity_registry=lambda: registry,
        _deliver_recovered_activity_output=AsyncMock(),
        _settle_pending_recovered_activity_terminals=Mock(),
        _wake_runtime_work=wake,
    )

    assert await ScheduledTaskService._process_recovered_activity_output(
        service,
        "claude",
        "runtime-a",
    )
    wake.assert_called_once_with(
        RuntimeWorkLane.ACTIVITY_OUTPUTS,
        reset_cursor=True,
    )
    assert [activity.id for activity in activities] == ["activity-b"]

    assert await ScheduledTaskService._process_recovered_activity_output(
        service,
        "claude",
        "runtime-a",
    )
    assert activities == []
    assert wake.call_count == 1


def test_hfr_168_stale_lane_arms_its_configured_remaining_interval() -> None:
    scheduled: list[tuple[RuntimeWorkLane, float]] = []
    service = SimpleNamespace(
        _stale_run_sweep_delay_seconds=lambda: 5.0,
        _schedule_runtime_work_wake=(
            lambda lane, delay: scheduled.append((lane, delay))
        ),
    )
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.STALE_RUNS,
    )

    items, has_more = handler.scan(limit=1, occupied=frozenset(), cursor=None)
    assert [item.partition_key for item in items] == ["run-cancellations"]
    assert has_more is False
    assert scheduled == [(RuntimeWorkLane.STALE_RUNS, 5.0)]


def test_task_reload_and_scheduler_snapshot_share_one_mirror_lock(
    tmp_path: Path,
) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    enabled = ScheduledTask(
        id="task-a",
        name=None,
        session_key="slack::channel::C123",
        prompt="run",
        schedule_type="cron",
        cron="* * * * *",
    )
    disabled = ScheduledTask.from_dict({**enabled.to_dict(), "enabled": False})
    store._tasks = {enabled.id: enabled}
    load_started = threading.Event()
    release_load = threading.Event()

    class _SQLite:
        probes = 0

        def maybe_reload(self) -> bool:
            self.probes += 1
            return self.probes == 1

        @staticmethod
        def list_scheduled_tasks():
            load_started.set()
            assert release_load.wait(timeout=1)
            return [disabled.to_dict()]

    store._sqlite = _SQLite()  # type: ignore[assignment]
    refresh_entered = threading.Event()

    def refresh() -> ScheduledTask | None:
        refresh_entered.set()
        return store.refresh_task(enabled.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reload_future = executor.submit(store.maybe_reload)
        assert load_started.wait(timeout=1)
        refresh_future = executor.submit(refresh)
        assert refresh_entered.wait(timeout=1)
        try:
            with pytest.raises(FutureTimeoutError):
                refresh_future.result(timeout=0.05)
        finally:
            release_load.set()

        assert reload_future.result(timeout=1) is True
        refreshed = refresh_future.result(timeout=1)

    assert refreshed is not None
    assert refreshed.enabled is False


def test_hfr_282_task_reload_waits_for_result_stamp_mirror_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="run once",
        schedule_type="at",
        run_at="2030-01-01T00:00:00+00:00",
        timezone_name="UTC",
    )
    write_entered = threading.Event()
    release_write = threading.Event()
    load_entered = threading.Event()
    original_save = store._save
    original_load = store._load_unlocked

    def blocked_save() -> None:
        write_entered.set()
        assert release_write.wait(timeout=1)
        original_save()

    def observed_load() -> None:
        load_entered.set()
        original_load()

    monkeypatch.setattr(store, "_save", blocked_save)
    monkeypatch.setattr(store, "_load_unlocked", observed_load)

    with ThreadPoolExecutor(max_workers=2) as executor:
        stamp_future = executor.submit(store.mark_task_result, task.id, error=None)
        assert write_entered.wait(timeout=1)
        reload_future = executor.submit(store.load)
        try:
            with pytest.raises(FutureTimeoutError):
                reload_future.result(timeout=0.05)
            assert not load_entered.is_set()
        finally:
            release_write.set()

        assert stamp_future.result(timeout=1) is True
        reload_future.result(timeout=1)

    reloaded = store.get_task(task.id)
    assert reloaded is not None
    assert reloaded.enabled is False


def test_hfr_165_vault_scan_arms_exact_pending_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    engine = create_sqlite_engine()
    metadata.create_all(engine)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=20)
    with engine.begin() as conn:
        conn.execute(
            vault_requests.insert().values(
                id="vrq_future",
                request_type="sign",
                status="pending",
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=expires_at.isoformat(),
            )
        )

    scheduled: list[tuple[RuntimeWorkLane, float]] = []
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )
    service._schedule_runtime_work_wake = (  # type: ignore[method-assign]
        lambda lane, delay: scheduled.append((lane, delay))
    )
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.VAULT_CALLBACKS,
    )

    items, has_more = handler.scan(
        limit=1,
        occupied=frozenset(),
        cursor=None,
    )

    assert items == []
    assert has_more is False
    assert len(scheduled) == 1
    lane, delay = scheduled[0]
    assert lane is RuntimeWorkLane.VAULT_CALLBACKS
    assert 0 < delay <= 20


@pytest.mark.anyio
async def test_hfr_166_activity_lane_lives_for_the_controller_generation(
    tmp_path: Path,
) -> None:
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    controller = SimpleNamespace(
        runtime_work_supervisor=supervisor,
        platform_settings_managers={},
        agent_service=SimpleNamespace(activities=None, agents={}),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
    )

    [token] = service.register_controller_runtime_work_lanes()
    assert token.lane is RuntimeWorkLane.ACTIVITY_OUTPUTS
    await supervisor.activate()
    assert await supervisor.run_in_partition(
        RuntimeWorkLane.ACTIVITY_OUTPUTS,
        "claude\x1fruntime-a",
        lambda: asyncio.sleep(0, result="delivered"),
    ) == "delivered"

    assert service._begin_runtime_work_unregistration() is None
    registration = supervisor._registrations[RuntimeWorkLane.ACTIVITY_OUTPUTS]
    assert registration.live is True
    await supervisor.stop()


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


def test_direct_inflight_cancellation_terminalizes_the_exact_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-100: a canceled claimed execution releases ownership and fails its Run."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    path = tmp_path / "scheduled_tasks.json"
    request_store = TaskExecutionStore()
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
    started = asyncio.Event()

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        started.set()
        await asyncio.Event().wait()

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(controller=controller, store=store, request_store=request_store)

    async def _exercise() -> None:
        await service._drain_requests()
        execution = service._inflight_executions.get(request.id)
        assert execution is not None
        await asyncio.wait_for(started.wait(), timeout=1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        await asyncio.sleep(0)
        assert request.id not in service._inflight_executions
        assert "key:slack::channel::C123" not in service._inflight_sessions
        assert "key:slack::channel::C123" not in service._session_lock_owners

    asyncio.run(_exercise())

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "failed"
    assert settled["metadata"]["interrupt_reason"] == "interrupted"
    assert settled["error"]
    assert request_store.list_pending() == []


def test_service_stop_terminalizes_inflight_run_without_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-101: shutdown records the interruption and restart never repeats the prompt."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_path = tmp_path / "scheduled_tasks.json"
    store = ScheduledTaskStore(task_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="interrupted prompt",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="test-one-shot",
    )
    successor = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="queued successor",
    )
    started = asyncio.Event()
    prompts: list[str] = []
    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(
            get_user=lambda *_args, **_kwargs: None,
        )
    )

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        prompts.append(message)
        if message == "interrupted prompt":
            started.set()
            await asyncio.Event().wait()

    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(handle_scheduled_message=_handle_scheduled_message),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=store,
        request_store=request_store,
    )

    async def _stop_in_flight() -> None:
        await service._drain_requests()
        await asyncio.wait_for(started.wait(), timeout=1)
        await service.stop()
        assert prompts == ["interrupted prompt"]

    asyncio.run(_stop_in_flight())

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "failed"
    assert settled["metadata"]["interrupt_reason"] == "restarted"
    assert settled["error"]
    queued = request_store.get_run(successor.id)
    assert queued is not None
    assert queued["status"] == "queued"
    retired = ScheduledTaskStore(task_path).get_task(task.id)
    assert retired is not None
    assert retired.enabled is False
    assert retired.last_error == settled["error"]

    restarted_store = TaskExecutionStore()
    restarted = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(task_path),
        request_store=restarted_store,
    )
    restarted.reconcile_jobs()
    assert restarted.scheduler.get_job(task.id) is None

    async def _restart_and_drain_successor() -> None:
        await restarted._drain_requests()
        successor_task = restarted._inflight_executions.get(successor.id)
        assert successor_task is not None
        await successor_task

    asyncio.run(_restart_and_drain_successor())

    assert prompts == ["interrupted prompt", "queued successor"]
    successor_result = restarted_store.get_run(successor.id)
    assert successor_result is not None
    assert successor_result["status"] == "succeeded"
    assert restarted_store.list_pending() == []
    assert not (request_store.processing_dir / f"{request.id}.json").exists()
    assert not (request_store.completed_dir / f"{request.id}.json").exists()


def test_service_stop_preserves_non_durable_user_stop_cause(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-106: user Stop wins when graceful shutdown cancels the same Run."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="stop before shutdown",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
    )
    started = asyncio.Event()

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        started.set()
        await asyncio.Event().wait()

    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(
            get_user=lambda *_args, **_kwargs: None,
        )
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(
            platform_settings_managers={"slack": settings_manager},
            message_handler=SimpleNamespace(
                handle_scheduled_message=_handle_scheduled_message,
            ),
        ),
        store=store,
        request_store=request_store,
    )

    async def _stop_after_user() -> None:
        await service._drain_requests()
        await asyncio.wait_for(started.wait(), timeout=1)
        assert request_store.cancel_run(request.id)
        await service.stop()

    asyncio.run(_stop_after_user())

    settled = request_store.get_run(request.id)
    projected = ScheduledTaskStore().get_task(task.id)
    assert settled is not None
    assert settled["status"] == "canceled"
    assert settled["metadata"]["interrupt_reason"] == SETTLED_BY_STOPPED
    assert settled["error"] == service._t(
        SETTLEMENT_I18N_KEYS[SETTLED_BY_STOPPED]
    )
    assert projected is not None
    assert projected.enabled is True
    assert projected.last_error == settled["error"]
    assert request_store.sqlite_backend is not None
    assert request_store.sqlite_backend.owed_failure_notice(request.id) is None


def test_service_stop_keeps_claim_cancelled_before_execution_starts_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A bare claim stays queued when teardown wins before coroutine entry."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_path = tmp_path / "scheduled_tasks.json"
    store = ScheduledTaskStore(task_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="never dispatched",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="test-one-shot",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=request_store,
    )

    async def _stop_before_start() -> None:
        claimed = request_store.claim(request.id)
        assert claimed is not None
        service._spawn_execution(claimed, "key:slack::channel::C123")
        await service.stop()

    asyncio.run(_stop_before_start())

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "queued"
    assert "interrupt_reason" not in settled["metadata"]
    preserved = ScheduledTaskStore(task_path).get_task(task.id)
    assert preserved is not None
    assert preserved.enabled is True
    assert preserved.last_run_at is None
    assert preserved.last_error is None


@pytest.mark.parametrize(
    ("schedule_type", "source_kind", "expected_enabled"),
    [
        ("cron", "scheduler", True),
        ("at", "cli", True),
        ("at", "scheduler", False),
    ],
)
def test_canceled_task_execution_projects_every_result_and_only_retires_scheduler_one_shots(
    tmp_path: Path,
    monkeypatch,
    schedule_type: str,
    source_kind: str,
    expected_enabled: bool,
) -> None:
    """HFR-102: every interrupted fire records a result; only owned one-shots retire."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_path = tmp_path / "scheduled_tasks.json"
    store = ScheduledTaskStore(task_path)
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="record this interruption",
        schedule_type=schedule_type,
        cron="0 * * * *" if schedule_type == "cron" else None,
        run_at=(
            "2026-03-31T09:00:00+08:00"
            if schedule_type == "at"
            else None
        ),
        timezone_name="Asia/Shanghai",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_task_run(
        task.id,
        source_kind=source_kind,
        task=task,
    )
    started = asyncio.Event()

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        started.set()
        await asyncio.Event().wait()

    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(
            get_user=lambda *_args, **_kwargs: None,
        )
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(
            platform_settings_managers={"slack": settings_manager},
            message_handler=SimpleNamespace(
                handle_scheduled_message=_handle_scheduled_message,
            ),
        ),
        store=store,
        request_store=request_store,
    )
    service.scheduler = _StubScheduler()
    service.scheduler.jobs[task.id] = SimpleNamespace(id=task.id)

    async def _cancel() -> None:
        await service._drain_requests()
        execution = service._inflight_executions[request.id]
        await asyncio.wait_for(started.wait(), timeout=1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        await asyncio.sleep(0)

    asyncio.run(_cancel())

    settled = request_store.get_run(request.id)
    projected = ScheduledTaskStore(task_path).get_task(task.id)
    assert settled is not None and settled["status"] == "failed"
    assert projected is not None
    assert projected.enabled is expected_enabled
    assert projected.last_run_at is not None
    assert projected.last_error == settled["error"]
    has_job = any(
        job_id == task.id or job_id.startswith(f"{task.id}:at:")
        for job_id in service.scheduler.jobs
    )
    assert has_job is expected_enabled


@pytest.mark.parametrize("cancellation_entrypoint", ["direct", "service_stop", "lease_loss"])
@pytest.mark.parametrize("settlement_winner", ["interruption", "natural_terminal", "user_stop"])
def test_task_definition_projection_follows_the_exact_terminal_cas_winner(
    tmp_path: Path,
    monkeypatch,
    cancellation_entrypoint: str,
    settlement_winner: str,
) -> None:
    """HFR-102: definition projection follows the exact terminal CAS winner."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="slack::channel::C123",
        prompt="settle this exact fire",
        schedule_type="at",
        run_at="2026-08-04T00:00:00+00:00",
        timezone_name="UTC",
    )
    request_store = TaskExecutionStore()
    request = request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="test-one-shot",
    )
    started = asyncio.Event()

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        started.set()
        await asyncio.Event().wait()

    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(
            get_user=lambda *_args, **_kwargs: None,
        )
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(
            platform_settings_managers={"slack": settings_manager},
            message_handler=SimpleNamespace(
                handle_scheduled_message=_handle_scheduled_message,
            ),
        ),
        store=store,
        request_store=request_store,
    )
    service.scheduler = _StubScheduler()
    service.scheduler.jobs[task.id] = SimpleNamespace(id=task.id)

    async def _race_terminal_with_cancellation() -> None:
        await service._drain_requests()
        execution = service._inflight_executions[request.id]
        await asyncio.wait_for(started.wait(), timeout=1)
        if settlement_winner == "natural_terminal":
            assert store.mark_task_result(
                task.id,
                error=None,
                disable_one_shot=True,
            )
            assert request_store.complete(request, ok=True) == "succeeded"
            service.reconcile_jobs()
        elif settlement_winner == "user_stop":
            assert request_store.cancel_run(request.id)
        if cancellation_entrypoint == "direct":
            if settlement_winner == "user_stop":
                service._inflight_cancellation_causes[request.id] = SETTLED_BY_STOPPED
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        elif cancellation_entrypoint == "service_stop":
            await service.stop()
        else:
            service._running = True
            service._requires_service_lease = True
            monkeypatch.setattr(
                "core.scheduled_tasks.runtime.current_process_owns_service_instance",
                lambda: False,
            )
            assert service._owns_service_instance() is False
            teardown = service._service_teardown_task
            assert teardown is not None
            await teardown
            await service.stop()
        await asyncio.sleep(0)

    asyncio.run(_race_terminal_with_cancellation())

    settled = request_store.get_run(request.id)
    projected = ScheduledTaskStore().get_task(task.id)
    assert settled is not None
    expected_status = {
        "interruption": "failed",
        "natural_terminal": "succeeded",
        "user_stop": "canceled",
    }[settlement_winner]
    assert settled["status"] == expected_status
    assert projected is not None
    assert projected.enabled is False
    assert projected.last_run_at is not None
    assert projected.last_error == settled["error"]
    assert task.id not in service.scheduler.jobs
    if settlement_winner == "natural_terminal":
        assert "interrupt_reason" not in settled["metadata"]
        assert TASK_LAST_RESULT_STATUS_METADATA_KEY not in (projected.metadata or {})
    elif settlement_winner == "user_stop":
        assert settled["metadata"]["interrupt_reason"] == SETTLED_BY_STOPPED
        assert projected.metadata[TASK_LAST_RESULT_STATUS_METADATA_KEY] == "canceled"
    else:
        assert projected.metadata[TASK_LAST_RESULT_STATUS_METADATA_KEY] == "failed"


@pytest.mark.parametrize("shutdown_entrypoint", ["stop", "lease_loss"])
@pytest.mark.parametrize("user_stopped", [False, True])
def test_service_teardown_terminalizes_transferred_turn_without_starting_queued_work(
    tmp_path: Path,
    monkeypatch,
    shutdown_entrypoint: str,
    user_stopped: bool,
) -> None:
    """HFR-103: full teardown settles Runs; lease loss first stops the controller."""
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _make_avibe_session(monkeypatch, tmp_path, agent_backend="codex")
    definition_store = ScheduledTaskStore()
    definition = definition_store.add_task(
        session_key="",
        session_id=session_id,
        session_policy="existing",
        prompt="durable prompt interrupted by shutdown",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    request_store = TaskExecutionStore()
    run_id = request_store.enqueue_task_run(
        definition.id,
        source_kind="scheduler",
        task=definition,
    ).id
    _force_run_columns(
        request_store,
        run_id,
        status="running",
        started_at=_ago(30),
        pid=1234,
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == session_id)
        ).mappings().one()
        delivery_id = message_deliveries.new_delivery_id()
        turn_id = message_deliveries.new_turn_id()
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=session_id,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=session["scope_id"],
                session_id=session_id,
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="durable prompt interrupted by shutdown",
                native_message_id=f"agent_run:{run_id}",
                metadata={},
            ),
            dispatch_text="durable prompt interrupted by shutdown",
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
            dispatch_text="durable prompt interrupted by shutdown",
        )
        turn = message_deliveries.get_turn(conn, turn_id)
        assert turn is not None
        assert message_deliveries.bind_native_start(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            runtime_key="runtime-key",
            runtime_turn_id="runtime-turn",
            native_turn_id="native-turn",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "native_acceptance"},
        )
        queued = message_deliveries.enqueue_queued(
            conn,
            scope_id=session["scope_id"],
            session_id=session_id,
            author="user",
            source="user",
            message_type="user",
            text="keep me queued",
            native_message_id="held-after-shutdown",
        )
        successor_delivery_id = None
        successor_turn_id = None
        if not user_stopped:
            successor_delivery_id = message_deliveries.new_delivery_id()
            successor_turn_id = message_deliveries.new_turn_id()
            successor_delivery = message_deliveries.insert_delivery(
                conn,
                delivery_id=successor_delivery_id,
                session_id=session_id,
                priority="p0",
                state="reserved",
                snapshot=message_deliveries.message_snapshot(
                    scope_id=session["scope_id"],
                    session_id=session_id,
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type="user",
                    text="do not start this replacement",
                    native_message_id="replacement-after-shutdown",
                    metadata={},
                ),
                dispatch_text="do not start this replacement",
            )
            message_deliveries.insert_turn(
                conn,
                turn_id=successor_turn_id,
                session_id=session_id,
                initial_delivery_id=successor_delivery_id,
                state="waiting",
                backend="codex",
            )
            assert message_deliveries.cas_delivery(
                conn,
                successor_delivery_id,
                expected_version=int(successor_delivery["version"]),
                expected_states=("reserved",),
                values={
                    "state": "interrupt_waiting",
                    "turn_id": successor_turn_id,
                    "turn_role": "initial",
                    "turn_position": 0,
                },
            ) is not None
            active = message_deliveries.get_turn(conn, turn_id)
            assert active is not None
            assert message_deliveries.cas_turn(
                conn,
                turn_id,
                expected_version=int(active["version"]),
                expected_states=("active",),
                values={
                    "control_state": "interrupting",
                    "control_mode": "replace",
                    "control_successor_delivery_id": successor_delivery_id,
                    "control_successor_turn_id": successor_turn_id,
                },
            ) is not None
        if user_stopped:
            active = message_deliveries.get_turn(conn, turn_id)
            assert active is not None
            assert message_deliveries.cas_turn(
                conn,
                turn_id,
                expected_version=int(active["version"]),
                expected_states=("active",),
                values={"control_mode": "stop_only"},
            ) is not None
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(cancel_requested=1)
            )

    dispatch_started = asyncio.Event()
    prompts: list[str] = []

    async def _never_returns(_controller, _context, text, **_kwargs):
        prompts.append(text)
        dispatch_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", _never_returns)
    shutdown_reasons: list[str] = []
    controller = SimpleNamespace(
        config=SimpleNamespace(language="en"),
        set_agent_status=lambda *_args, **_kwargs: None,
        _get_session_key=lambda ctx: f"avibe::{ctx.channel_id}",
        request_shutdown=shutdown_reasons.append,
    )
    service = ScheduledTaskService(
        controller=controller,
        store=definition_store,
        request_store=request_store,
    )
    service.scheduler = _StubScheduler()
    manager = SessionTurnManager(controller)
    manager._engine = engine
    manager._resume_post_terminal = AsyncMock()
    controller.session_turns = manager
    controller.scheduled_task_service = service
    context = MessageContext(
        user_id="scheduled",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "task_execution_id": run_id,
            "task_definition_id": definition.id,
            "task_trigger_kind": "scheduled",
            "accepted_agent_run_ids": [run_id],
            "agent_session_target": {"agent_backend": "codex"},
        },
    )

    async def _stop() -> None:
        await manager._run(
            session_id,
            context,
            "durable prompt interrupted by shutdown",
            source=SOURCE_SCHEDULED,
            logical_turn_id=turn_id,
            delivery_id=delivery_id,
            durable_preallocated=True,
        )
        await asyncio.wait_for(dispatch_started.wait(), timeout=1)
        if shutdown_entrypoint == "stop":
            await service.stop()
            return
        service._running = True
        service._requires_service_lease = True
        monkeypatch.setattr(
            "core.scheduled_tasks.runtime.current_process_owns_service_instance",
            lambda: False,
        )
        assert service._owns_service_instance() is False
        assert shutdown_reasons == ["service lease lost"]
        assert service._service_teardown_task is None
        assert service._owns_service_instance() is False
        assert shutdown_reasons == ["service lease lost", "service lease lost"]
        assert service._service_teardown_task is None
        live_run = request_store.get_run(run_id)
        assert live_run is not None
        assert live_run["status"] == "running"
        with engine.connect() as conn:
            live_turn = message_deliveries.get_turn(conn, turn_id)
        assert live_turn is not None
        assert live_turn["state"] == "active"
        assert session_id in manager.in_flight
        assert not manager.in_flight[session_id].task.done()
        await service.stop()
        assert service._service_teardown_task is not None

    asyncio.run(_stop())

    settled = request_store.get_run(run_id)
    assert settled is not None
    expected_status = "canceled" if user_stopped else "failed"
    expected_reason = SETTLED_BY_STOPPED if user_stopped else SETTLED_BY_RESTARTED
    assert settled["status"] == expected_status
    assert settled["metadata"]["interrupt_reason"] == expected_reason
    projected_definition = ScheduledTaskStore().get_task(definition.id)
    assert projected_definition is not None
    assert projected_definition.enabled is True
    assert projected_definition.last_run_at is not None
    assert projected_definition.last_error == settled["error"]
    with engine.connect() as conn:
        terminal_turn = message_deliveries.get_turn(conn, turn_id)
        accepted = message_deliveries.get_delivery(conn, delivery_id)
        held = message_deliveries.get_delivery(conn, queued["id"])
        successor_turn = (
            message_deliveries.get_turn(conn, successor_turn_id)
            if successor_turn_id
            else None
        )
        successor_delivery = (
            message_deliveries.get_delivery(conn, successor_delivery_id)
            if successor_delivery_id
            else None
        )
    assert terminal_turn is not None
    assert terminal_turn["state"] == "terminal"
    assert terminal_turn["terminal_outcome"] == expected_status
    assert terminal_turn["settled_by"] == expected_reason
    assert terminal_turn["terminal_evidence_kind"] == (
        "service_shutdown_after_user_stop"
        if user_stopped
        else "service_shutdown"
    )
    assert accepted is not None and accepted["state"] == "accepted"
    assert held is not None and held["state"] == "queued"
    if successor_turn_id:
        assert successor_turn is not None
        assert successor_turn["state"] == "terminal"
        assert successor_turn["terminal_outcome"] == "not_written"
        assert successor_delivery is not None
        assert successor_delivery["state"] == "queued"
        assert successor_delivery["priority"] == "p3"
        assert successor_delivery["turn_id"] is None
    assert manager.in_flight == {}
    manager._resume_post_terminal.assert_not_awaited()
    assert prompts == ["durable prompt interrupted by shutdown"]


def test_service_stop_preserves_terminal_run_that_won_the_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A recorded terminal outcome wins over the teardown fallback."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="finish before shutdown",
    )
    started = asyncio.Event()

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        started.set()
        await asyncio.Event().wait()

    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(
            get_user=lambda *_args, **_kwargs: None,
        )
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(
            platform_settings_managers={"slack": settings_manager},
            message_handler=SimpleNamespace(
                handle_scheduled_message=_handle_scheduled_message,
            ),
        ),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    async def _settle_then_stop() -> None:
        await service._drain_requests()
        await asyncio.wait_for(started.wait(), timeout=1)
        request_store.complete(request, ok=True)
        await service.stop()

    asyncio.run(_settle_then_stop())

    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "succeeded"
    assert "interrupt_reason" not in settled["metadata"]


def test_restart_recovery_terminalizes_started_rows_and_preserves_other_owners(
    monkeypatch,
    tmp_path,
) -> None:
    """HFR-104: startup fails executed Runs while preserving every exempt owner."""
    from storage.background import attach_agent_run_delivery_in_connection

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    definition_store = ScheduledTaskStore()
    scheduler_at = definition_store.add_task(
        session_key="slack::channel::C123",
        prompt="scheduler at",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    scheduler_cron = definition_store.add_task(
        session_key="slack::channel::C123",
        prompt="scheduler cron",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )
    manual_at = definition_store.add_task(
        session_key="slack::channel::C123",
        prompt="manual at",
        schedule_type="at",
        run_at="2026-03-31T09:00:00+08:00",
        timezone_name="Asia/Shanghai",
    )
    request_store = TaskExecutionStore()

    started_runs: dict[str, str] = {}
    for label, task, source_kind in (
        ("scheduler_at", scheduler_at, "scheduler"),
        ("scheduler_cron", scheduler_cron, "scheduler"),
        ("manual_at", manual_at, "cli"),
    ):
        request = request_store.enqueue_task_run(
            task.id,
            source_kind=source_kind,
            task=task,
            expected_run_at=(
                task.run_at
                if source_kind == "scheduler" and task.schedule_type == "at"
                else None
            ),
            expected_timezone=(
                task.timezone
                if source_kind == "scheduler" and task.schedule_type == "at"
                else None
            ),
            expected_job_id=(
                f"test:{task.id}"
                if source_kind == "scheduler" and task.schedule_type == "at"
                else None
            ),
        )
        assert request_store.claim(request.id) is not None
        _force_run_columns(request_store, request.id, pid=4321)
        started_runs[label] = request.id

    pre_execution = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="claimed but coroutine never entered",
    )
    assert request_store.claim(pre_execution.id) is not None

    watch_runtime = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="watch runtime bookkeeping",
    )
    assert request_store.claim(watch_runtime.id) is not None
    _force_run_columns(
        request_store,
        watch_runtime.id,
        run_type="watch_runtime",
        pid=4321,
    )

    deferred = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="activity owns terminal settlement",
    )
    assert request_store.claim(deferred.id) is not None
    _force_run_columns(
        request_store,
        deferred.id,
        pid=4321,
        result_payload_json=json.dumps({"deferred_terminal_status": "succeeded"}),
    )

    stopped = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="user stopped before restart",
    )
    assert request_store.claim(stopped.id) is not None
    _force_run_columns(request_store, stopped.id, pid=4321)
    assert request_store.cancel_run(stopped.id)

    delivery_owned = request_store.enqueue_agent_run(
        session_id=session_id,
        message="durable Turn owns this row",
        agent_name="codex",
    )
    assert request_store.claim(delivery_owned.id) is not None
    _force_run_columns(request_store, delivery_owned.id, pid=4321)
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
            text="durable Turn owns this row",
            native_message_id=f"agent_run:{delivery_owned.id}",
        )
        assert attach_agent_run_delivery_in_connection(
            conn,
            delivery_owned.id,
            session_id=session_id,
            delivery_id=str(delivery["id"]),
        )

    natural = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="natural terminal wins",
    )
    claimed_natural = request_store.claim(natural.id)
    assert claimed_natural is not None
    request_store.complete(claimed_natural, ok=True)

    held_queued = request_store.enqueue_agent_run(
        session_id=session_id,
        message="held queue stays queued",
        agent_name="codex",
        metadata={"workbench_queue_holds_run": True},
    )

    restarted_store = TaskExecutionStore()
    restarted_store.recover_processing()

    for run_id in started_runs.values():
        settled = restarted_store.get_run(run_id)
        assert settled is not None
        assert settled["status"] == "failed"
        assert settled["metadata"]["interrupt_reason"] == "restarted"
        assert settled["error"]
        assert settled["completed_at"] is not None

    assert restarted_store.get_run(pre_execution.id)["status"] == "queued"
    assert restarted_store.get_run(watch_runtime.id)["status"] == "running"
    assert restarted_store.get_run(deferred.id)["status"] == "running"
    assert restarted_store.get_run(delivery_owned.id)["status"] == "running"
    assert restarted_store.get_run(natural.id)["status"] == "succeeded"
    assert restarted_store.get_run(held_queued.id)["status"] == "queued"
    recovered_stopped = restarted_store.get_run(stopped.id)
    assert recovered_stopped["status"] == "canceled"
    assert recovered_stopped["metadata"]["interrupt_reason"] == SETTLED_BY_STOPPED

    projected = ScheduledTaskStore()
    recovered_scheduler_at = projected.get_task(scheduler_at.id)
    recovered_scheduler_cron = projected.get_task(scheduler_cron.id)
    recovered_manual_at = projected.get_task(manual_at.id)
    assert recovered_scheduler_at is not None
    assert recovered_scheduler_cron is not None
    assert recovered_manual_at is not None
    assert recovered_scheduler_at.enabled is False
    assert recovered_scheduler_cron.enabled is True
    assert recovered_manual_at.enabled is True
    for definition in (
        recovered_scheduler_at,
        recovered_scheduler_cron,
        recovered_manual_at,
    ):
        assert definition.last_run_at is not None
        assert definition.last_error


_TEARDOWN_SETTLEMENT_CELLS = [
    (entry_point, surface, TEARDOWN_SETTLEMENT_MATRIX[entry_point][surface])
    for entry_point in TEARDOWN_SETTLEMENT_ENTRY_POINTS
    for surface in TEARDOWN_SETTLEMENT_SURFACES
]


@pytest.mark.parametrize(
    ("entry_point", "surface", "proof"),
    _TEARDOWN_SETTLEMENT_CELLS,
    ids=[
        f"{entry_point}-{surface}"
        for entry_point, surface, _proof in _TEARDOWN_SETTLEMENT_CELLS
    ],
)
def test_every_teardown_settlement_surface_is_reached_or_explicitly_owned_elsewhere(
    entry_point: str,
    surface: str,
    proof: tuple[str, str],
) -> None:
    """HFR-105: every entry point x surface cell has durable evidence or an owner."""
    assert set(TEARDOWN_SETTLEMENT_MATRIX) == set(
        TEARDOWN_SETTLEMENT_ENTRY_POINTS
    )
    assert set(TEARDOWN_SETTLEMENT_MATRIX[entry_point]) == set(
        TEARDOWN_SETTLEMENT_SURFACES
    )
    proof_kind, detail = proof
    assert proof_kind in {"covered", "N/A"}
    assert detail.strip()
    if proof_kind == "covered":
        test_path, *node_parts = detail.split("::")
        assert test_path.startswith("tests/")
        assert node_parts
        test_name = node_parts[-1]
        tree = ast.parse(Path(test_path).read_text(encoding="utf-8"))
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == test_name
            for node in ast.walk(tree)
        ), detail

    assert set(SETTLEMENTS_WITHOUT_RESULT) == set(SETTLEMENT_I18N_KEYS)
    assert set(SETTLEMENTS_WITHOUT_RESULT) == set(SETTLEMENT_TERMINAL_STATUS)
    assert SETTLED_BY_RESTARTED in RUN_INTERRUPTION_REASONS

    from storage.background import RUN_INTERRUPTION_REASONS as storage_reasons

    assert storage_reasons == RUN_INTERRUPTION_REASONS


def test_file_recovery_distinguishes_bare_started_and_user_stopped_claims(
    tmp_path: Path,
) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")

    bare = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="claim only",
    )
    assert request_store.claim(bare.id) is not None

    started = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="started",
    )
    assert request_store.claim(started.id) is not None
    assert request_store.mark_execution_started(started.id)

    stopped = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="stopped",
    )
    assert request_store.claim(stopped.id) is not None
    assert request_store.mark_execution_started(stopped.id)
    assert request_store.cancel_run(stopped.id)

    request_store.recover_processing()

    assert request_store.get_run(bare.id)["status"] == "queued"
    recovered_started = request_store.get_run(started.id)
    assert recovered_started["status"] == "failed"
    assert recovered_started["metadata"]["interrupt_reason"] == SETTLED_BY_RESTARTED
    recovered_stopped = request_store.get_run(stopped.id)
    assert recovered_stopped["status"] == "canceled"
    assert recovered_stopped["metadata"]["interrupt_reason"] == SETTLED_BY_STOPPED


def test_restart_recovers_running_row_and_preserves_same_session_fifo(monkeypatch, tmp_path) -> None:
    """A bare-claimed row restarts queued and each successor runs once."""

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
    assert restarted_store.get_run(first.id)["status"] == "running"
    service.recover_processing_requests()
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
        assert request_store.mark_execution_started(claimed.id)
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
    settled = request_store.get_run(request.id)
    assert settled is not None
    assert settled["status"] == "failed"
    assert settled["metadata"]["interrupt_reason"] == "restarted"


def test_execute_claimed_request_checks_backend_before_marking_started(
    tmp_path: Path,
) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    wait_observations: list[object] = []

    def is_backend_ready(_backend: str) -> bool:
        wait_observations.append(request_store.get_run(claimed.id).get("pid"))
        return True

    controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            is_backend_ready=Mock(side_effect=is_backend_ready),
            runtime_activation_identity_for_request=Mock(return_value=None),
            activation_registry=None,
        )
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    claimed = request_store.enqueue_agent_run(
        message="run",
        agent_backend="codex",
        session_id="ses-runtime",
    )
    claimed = request_store.claim(claimed.id)
    assert claimed is not None

    async def fake_execute_agent_run(**_kwargs):
        assert request_store.get_run(claimed.id).get("pid") is not None
        return scheduled_tasks.AgentRunExecutionResult(
            error=None,
            complete_on_return=False,
        )

    service._execute_agent_run = AsyncMock(side_effect=fake_execute_agent_run)  # type: ignore[assignment]

    asyncio.run(service._execute_claimed_request(claimed))

    assert wait_observations == [None]
    service._execute_agent_run.assert_awaited_once()
    assert request_store.get_run(claimed.id)["pid"] is not None


def test_execute_claimed_request_skips_retired_runtime_generation(
    tmp_path: Path,
) -> None:
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    registry = RuntimeActivationRegistry()
    identity = registry.attach("codex", "/repo")
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            is_backend_ready=Mock(return_value=True),
            runtime_activation_identity_for_request=Mock(return_value=identity),
            activation_registry=registry,
        )
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    claimed = request_store.enqueue_agent_run(
        message="run",
        agent_backend="codex",
        session_id="ses-runtime",
    )
    claimed = request_store.claim(claimed.id)
    assert claimed is not None
    assert registry.retire_if_current(identity, lambda: True) is True
    service._execute_agent_run = AsyncMock()  # type: ignore[assignment]

    asyncio.run(service._execute_claimed_request(claimed))

    service._execute_agent_run.assert_not_awaited()
    row = request_store.get_run(claimed.id)
    assert row["status"] == "queued"
    assert row.get("pid") is None


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
        await service._run_task(task.id)
        pending = service.request_store.list_pending()
        assert len(pending) == 1
        assert await service._process_pending_request(pending[0]) is True
        await started.wait()
        assert len(service._inflight_executions) == 1
        execution = next(iter(service._inflight_executions.values()))
        owner_state["owns"] = False
        assert service._owns_service_instance() is False
        with pytest.raises(asyncio.CancelledError):
            await execution

    asyncio.run(_exercise())

    assert service._running is False


def test_request_partitions_use_the_canonical_session_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from storage.sessions_service import SQLiteSessionsService

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    target = parse_session_key("slack::channel::C123")
    sessions = SQLiteSessionsService(paths.get_sqlite_state_path())
    try:
        session_id = sessions.reserve_agent_session(
            scope_key=target.session_scope,
            agent_backend="codex",
            session_anchor=session_anchor_for_target(target),
        )
    finally:
        sessions.close()
    assert session_id is not None

    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "requests"),
    )
    by_id = TaskExecutionRequest(
        id="by-session-id",
        request_type="hook_send",
        session_id=session_id,
    )
    by_key = TaskExecutionRequest(
        id="by-session-key",
        request_type="hook_send",
        session_key=target.to_key(),
    )

    assert service._request_partition_key(by_id) == service._request_partition_key(
        by_key
    )


def test_request_capacity_is_reserved_before_claim(tmp_path: Path) -> None:
    request_store = TaskExecutionStore(tmp_path / "requests")
    first = request_store.enqueue_hook_send(
        session_key="slack::channel::A",
        prompt="first",
    )
    second = request_store.enqueue_hook_send(
        session_key="slack::channel::B",
        prompt="second",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=request_store,
    )
    service._running = True
    for index in range(service._MAX_CONCURRENT_EXECUTIONS - 1):
        service._inflight_executions[f"busy-{index}"] = Mock()
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()
    claims: list[str] = []
    spawned: list[str] = []

    async def _run_sync(operation, /, *args, **kwargs):  # noqa: ANN001, ANN202
        if operation == service._claim_pending_request:
            claims.append(args[0].id)
            claim_started.set()
            await release_claim.wait()
        return operation(*args, **kwargs)

    service._run_runtime_sync = _run_sync  # type: ignore[method-assign]
    service._spawn_execution = (  # type: ignore[method-assign]
        lambda request, _lock_key: spawned.append(request.id)
    )

    async def _exercise() -> None:
        first_task = asyncio.create_task(service._process_pending_request(first))
        await claim_started.wait()
        assert await service._process_pending_request(second) is False
        release_claim.set()
        assert await first_task is True

    asyncio.run(_exercise())

    assert claims == [first.id]
    assert spawned == [first.id]
    assert service._request_capacity_reservations == set()


def test_claimed_request_is_requeued_when_its_lane_generation_stops(
    tmp_path: Path,
) -> None:
    request_store = TaskExecutionStore(tmp_path / "requests")
    pending = request_store.enqueue_hook_send(
        session_key="slack::channel::A",
        prompt="run after restart",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=request_store,
    )
    service._running = True
    token = RuntimeWorkRegistrationToken(RuntimeWorkLane.REQUESTS, 1)
    service._runtime_work_tokens[RuntimeWorkLane.REQUESTS] = token
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()
    spawned: list[str] = []

    async def _run_sync(operation, /, *args, **kwargs):  # noqa: ANN001, ANN202
        if operation == service._claim_pending_request:
            claim_started.set()
            await release_claim.wait()
        return operation(*args, **kwargs)

    service._run_runtime_sync = _run_sync  # type: ignore[method-assign]
    service._spawn_execution = (  # type: ignore[method-assign]
        lambda request, _lock_key: spawned.append(request.id)
    )

    async def _exercise() -> None:
        processing = asyncio.create_task(service._process_pending_request(pending))
        await claim_started.wait()
        service._running = False
        service._runtime_work_tokens.pop(RuntimeWorkLane.REQUESTS)
        release_claim.set()
        assert await processing is True

    asyncio.run(_exercise())

    assert spawned == []
    assert request_store.get_run(pending.id)["status"] == "queued"
    assert service._request_capacity_reservations == set()


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
# Note on staging: startup owner recovery requeues bare claims and terminalizes
# executions whose coroutine had started. These tests build the service first and
# stage the live-process orphan after it.
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
    claimed = request_store.claim(run_ids[0])
    assert claimed is not None

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
    assert callback_run["metadata"]["source_session_id"] == "target-session"


def test_one_terminal_turn_callbacks_once_per_callback_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Scenario: MESSAGE-DELIVERY-314 terminal Turn callback identity."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_ids = [
        _make_avibe_session(
            monkeypatch,
            tmp_path,
            scope_native_id=f"callback-turn-caller-{index}",
        )
        for index in range(2)
    ]
    target_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-turn-target",
    )
    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(
            session_id=target_session_id,
            message=message,
            agent_name="codex",
            callback_session_id=callback_session_id,
        )
        for message, callback_session_id in (
            ("participant one", caller_session_ids[0]),
            ("participant two", caller_session_ids[0]),
            ("participant three", caller_session_ids[1]),
        )
    ]
    for request in requests:
        assert request_store.claim(request.id) is not None

    service = _callback_service(tmp_path=tmp_path, request_store=request_store)
    service.settle_agent_runs_from_terminal_turn(
        [request.id for request in requests],
        turn_id="turn-shared-by-three-runs",
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
    assert {run["session_id"] for run in callbacks} == set(caller_session_ids)
    assert {run["message"] for run in callbacks} == {
        "shared immutable terminal result"
    }
    assert {
        run["metadata"]["delivery_intent"] for run in callbacks
    } == {"steer"}
    assert {
        run["metadata"]["callback_terminal_turn_id"] for run in callbacks
    } == {"turn-shared-by-three-runs"}

    callback_ids_by_parent = {
        str(row["id"]): str(row["callback_run_id"]) for row in originals
    }
    assert callback_ids_by_parent[requests[0].id] == callback_ids_by_parent[requests[1].id]
    assert callback_ids_by_parent[requests[2].id] != callback_ids_by_parent[requests[0].id]


def test_distinct_terminal_turns_callback_same_session_independently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    caller_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-distinct-turn-caller",
    )
    target_session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        scope_native_id="callback-distinct-turn-target",
    )
    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(
            session_id=target_session_id,
            message=f"participant {index}",
            agent_name="codex",
            callback_session_id=caller_session_id,
        )
        for index in range(2)
    ]
    service = _callback_service(tmp_path=tmp_path, request_store=request_store)

    for index, request in enumerate(requests):
        assert request_store.claim(request.id) is not None
        service.settle_agent_runs_from_terminal_turn(
            [request.id],
            turn_id=f"turn-distinct-{index}",
            outcome="completed",
            settled_by="terminal_result",
            evidence_kind="terminal_result",
            evidence={
                "settles_run": True,
                "result_text": f"terminal result {index}",
            },
        )

    asyncio.run(service._drain_callbacks())
    callbacks = [
        run
        for run in request_store.list_runs()
        if run.get("source_kind") == "callback"
    ]

    assert len(callbacks) == 2
    assert {run["session_id"] for run in callbacks} == {caller_session_id}
    assert {run["message"] for run in callbacks} == {
        "terminal result 0",
        "terminal result 1",
    }
    assert {
        run["metadata"]["callback_terminal_turn_id"] for run in callbacks
    } == {"turn-distinct-0", "turn-distinct-1"}


def test_hfr_439_turn_failure_metadata_reaches_every_linked_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The failed transition and its Turn notification evidence commit together."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(message=message, agent_name="codex")
        for message in ("participant one", "participant two")
    ]
    for request in requests:
        assert request_store.claim(request.id) is not None

    service = _callback_service(tmp_path=tmp_path, request_store=request_store)
    service.settle_agent_runs_from_terminal_turn(
        [request.id for request in requests],
        turn_id="turn-one-error",
        outcome="failed",
        settled_by="terminal_result",
        evidence_kind="terminal_result",
        evidence={
            "settles_run": True,
            "terminal_error": "stream disconnected",
            "output_provenance": {
                "turn_failure_notification": {
                    "failure_id": "turn:turn-one-error",
                    "ack_evidence": "receipt",
                    "delivered": True,
                }
            },
        },
    )

    expected_owner = min(request.id for request in requests)
    for request in requests:
        row = request_store.get_run(request.id)
        assert row is not None and row["status"] == "failed"
        assert row["metadata"]["turn_id"] == "turn-one-error"
        notice = request_store.sqlite_backend.owed_failure_notice(request.id)
        assert notice["failure_id"] == "turn:turn-one-error"
        assert notice["turn_id"] == "turn-one-error"
        assert notice["turn_notification_delivered"] is True
        assert notice["turn_notification_ack_evidence"] == "receipt"
        assert notice["turn_fallback_run_id"] == expected_owner


def test_hfr_474_resultless_turn_settlement_creates_one_restart_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A current release never creates the legacy per-Run restart shape."""

    from core import failure_notices
    from core.run_settlement import SETTLED_BY_RESTARTED

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(message=message, agent_name="codex")
        for message in ("participant one", "participant two", "participant three")
    ]
    for request in requests:
        assert request_store.claim(request.id) is not None

    service = _callback_service(tmp_path=tmp_path, request_store=request_store)
    service.settle_agent_runs_from_terminal_turn(
        [request.id for request in requests],
        turn_id="turn-resultless-restart",
        outcome="failed",
        settled_by=SETTLED_BY_RESTARTED,
        evidence_kind="service_shutdown",
        evidence={"reason": "scheduled_service_shutdown"},
    )

    expected_owner = min(request.id for request in requests)
    deliverable = []
    for request in requests:
        row = request_store.get_run(request.id)
        assert row is not None and row["status"] == "failed"
        notice = request_store.sqlite_backend.owed_failure_notice(request.id)
        assert notice is not None
        assert notice["failure_id"] == "turn:turn-resultless-restart"
        assert notice["turn_id"] == "turn-resultless-restart"
        assert notice["turn_fallback_run_id"] == expected_owner
        if failure_notices.decide(
            run_id=request.id,
            definition_id=None,
            notice=notice,
            streak_facts=None,
            earlier_unsettled=None,
        ).action == failure_notices.ACTION_DELIVER:
            deliverable.append(request.id)
    assert deliverable == [expected_owner]


def test_turn_fallback_owner_excludes_a_canceled_participant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A canceled Run keeps its audit state but cannot own the only fallback."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    requests = [
        request_store.enqueue_agent_run(message=message, agent_name="codex")
        for message in ("participant one", "participant two")
    ]
    for request in requests:
        assert request_store.claim(request.id) is not None
    canceled_id = min(request.id for request in requests)
    assert request_store.cancel_run(canceled_id)

    service = _callback_service(tmp_path=tmp_path, request_store=request_store)
    service.settle_agent_runs_from_terminal_turn(
        [request.id for request in requests],
        turn_id="turn-canceled-owner",
        outcome="failed",
        settled_by="terminal_result",
        evidence_kind="terminal_result",
        evidence={
            "settles_run": True,
            "terminal_error": "stream disconnected",
            "output_provenance": {
                "turn_failure_notification": {
                    "failure_id": "turn:turn-canceled-owner",
                    "delivered": False,
                }
            },
        },
    )

    eligible_id = next(request.id for request in requests if request.id != canceled_id)
    assert request_store.get_run(canceled_id)["status"] == "canceled"
    notice = request_store.sqlite_backend.owed_failure_notice(eligible_id)
    assert notice["turn_fallback_run_id"] == eligible_id


def test_late_turn_settlement_reuses_all_durable_participants(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A late accepted Run cannot elect a second owner from only its subset."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    settle = Mock()
    manager = SessionTurnManager(
        SimpleNamespace(
            scheduled_task_service=SimpleNamespace(
                settle_agent_runs_from_terminal_turn=settle
            )
        )
    )
    manager.accepted_agent_run_ids_for_turn = lambda _turn_id: [
        "run-initial",
        "run-late",
    ]
    turn = {
        "id": "turn-late-owner",
        "terminal_outcome": "failed",
        "settled_by": "terminal_result",
        "terminal_evidence_kind": "terminal_result",
        "terminal_evidence_json": json.dumps(
            {
                "settles_run": True,
                "output_provenance": {
                    "turn_failure_notification": {
                        "failure_id": "turn:turn-late-owner",
                        "fallback_run_id": "run-initial",
                    }
                },
            }
        ),
    }

    manager._settle_agent_run_ids_from_terminal_turn(["run-late"], turn)

    settle.assert_called_once()
    assert settle.call_args.args[0] == ["run-initial", "run-late"]


def test_hfr_439_deferred_run_preserves_turn_failure_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An Activity delay cannot turn a shared Turn failure into a new Run failure."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(message="participant", agent_name="codex")
    assert request_store.claim(request.id) is not None
    metadata = {
        "turn_id": "turn-deferred-error",
        "turn_failure_notification": {
            "failure_id": "turn:turn-deferred-error",
            "ack_evidence": "receipt",
            "delivered": True,
            "fallback_run_id": request.id,
        },
    }

    assert request_store.defer_run_terminal(
        request.id,
        terminal_status="failed",
        error="stream disconnected",
        metadata=metadata,
    )
    assert request_store.sqlite_backend.settle_deferred_run(request.id)

    notice = request_store.sqlite_backend.owed_failure_notice(request.id)
    assert notice["failure_id"] == "turn:turn-deferred-error"
    assert notice["turn_id"] == "turn-deferred-error"
    assert notice["turn_notification_delivered"] is True
    assert notice["turn_fallback_run_id"] == request.id


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
    assert callback_runs[0]["metadata"]["source_session_id"] == "target-session"
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


def test_hfr_169_recovered_activity_without_run_reaches_its_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    sqlite_store = request_store.sqlite_backend
    assert sqlite_store is not None
    activity_store = SQLiteSessionActivityStore(sqlite_store.engine)
    original = SessionActivityRegistry(activity_store)
    original.start(
        backend="claude",
        runtime_key="runtime-without-run",
        session_id=session_id,
        activity_id="task-without-run",
        kind="background_task",
    )
    original.complete(
        backend="claude",
        runtime_key="runtime-without-run",
        activity_id="task-without-run",
        status="completed",
        metadata={"summary": "Recovered without a Run row"},
        expects_output=True,
    )

    recovered = SessionActivityRegistry(activity_store)
    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=lambda *_args, **_kwargs: None, in_flight={}),
        handle_scheduled_message=lambda *_args, **_kwargs: None,
    )
    controller.agent_service = SimpleNamespace(activities=recovered)
    delivered: list[tuple[str, str]] = []

    async def emit(context, _kind, text, **_kwargs):  # noqa: ANN001, ANN202
        delivered.append((context.platform_specific["agent_session_id"], text))
        return "message-without-run"

    controller.emit_agent_message = emit
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    asyncio.run(service._drain_recovered_activity_outputs())

    assert delivered == [(session_id, "Recovered without a Run row")]
    assert activity_store.list_activities() == []


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


def test_legacy_send_now_intent_reaches_the_shared_gate(monkeypatch, tmp_path) -> None:
    """An upgrade preserves the raw alias long enough to recover old P3 admission."""

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="recover legacy admission",
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
        metadata["delivery_intent"] = "send_now"
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == request.id)
            .values(metadata_json=json.dumps(metadata))
        )

    submitted: list[str] = []

    async def _submit_scheduled(_sid, _ctx, _text, *, delivery_intent="steer"):
        submitted.append(delivery_intent)
        return "ran"

    async def _handle_scheduled_message(_context, _message, parsed_session_key=None):
        raise AssertionError("a Session-bound Agent Run must use the shared gate")

    controller = _avibe_controller_double(
        gate=SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={}),
        handle_scheduled_message=_handle_scheduled_message,
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    assert submitted == ["send_now"]
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "running"


def test_explicit_queue_delivery_intent_remains_queued() -> None:
    assert normalize_agent_run_delivery_intent("queue") == "queue"
    assert normalize_agent_run_delivery_intent("send_now") == "steer"


@pytest.mark.parametrize("delivery_intent", ["steer", "queue", "send_now"])
def test_session_agent_run_fails_closed_without_turn_gate(
    monkeypatch,
    tmp_path,
    delivery_intent,
) -> None:
    """No Session-bound delivery may bypass its durable turn owner at startup."""

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="must stay behind the shared owner",
        agent_name="codex",
        delivery_intent=delivery_intent,
    )
    direct_calls: list[str] = []

    async def _handle_scheduled_message(_context, message, parsed_session_key=None):
        direct_calls.append(message)

    controller = _avibe_controller_double(
        gate=None,
        handle_scheduled_message=_handle_scheduled_message,
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
    assert stored["error"] == i18n_t(SESSION_TURN_GATE_UNAVAILABLE_I18N_KEY, "en")
    assert (
        stored["metadata"]["failure_code"]
        == FAILURE_CODE_SESSION_TURN_GATE_UNAVAILABLE
    )
    assert direct_calls == []


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

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="steer"):
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
            route="ran",
            queue_persisted=True,
            target_was_busy=True,
            delivery_status="accepted",
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
    assert run["metadata"]["delivery_intent"] == "steer"
    assert submitted == [
        (session_id, "run behind active workbench turn", request.id, "steer")
    ]
    assert handler_calls == []


def test_send_now_refusal_keeps_transferred_delivery_running(monkeypatch, tmp_path) -> None:
    from core.session_turns import TurnSubmissionResult

    session_id = _make_avibe_session(monkeypatch, tmp_path)
    request_store = TaskExecutionStore()
    request = request_store.enqueue_agent_run(
        session_id=session_id,
        message="keep this queued after refusal",
        agent_name="codex",
        delivery_intent="send_now",
    )
    async def _submit_scheduled(_sid, _ctx, _text, *, delivery_intent="steer"):
        return TurnSubmissionResult(
            route="enqueued",
            queue_persisted=True,
            target_was_busy=True,
            delivery_status="queued",
            delivery_owner_transferred=True,
        )

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        raise AssertionError("send-now must not use direct IM dispatch")

    gate = SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={})
    controller = _avibe_controller_double(
        gate=gate,
        handle_scheduled_message=_handle_scheduled_message,
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    _run_single_request(service, request.id)

    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "running"
    assert "workbench_queue_holds_run" not in stored["metadata"]


def test_send_now_runtime_uses_shared_delivery_for_im_session(monkeypatch, tmp_path) -> None:
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
        message="steer through the shared owner",
        agent_name="worker",
        delivery_intent="send_now",
    )
    direct_calls: list[str] = []

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        direct_calls.append(message)

    from core.session_turns import TurnSubmissionResult

    submit_scheduled = AsyncMock(
        return_value=TurnSubmissionResult(
            route="enqueued",
            queue_persisted=True,
            target_was_busy=True,
            delivery_status="queued",
            delivery_owner_transferred=True,
        )
    )
    controller = SimpleNamespace(
        platform_settings_managers={"slack": object()},
        im_clients={"slack": SimpleNamespace()},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        session_turn_gate=SimpleNamespace(submit_scheduled=submit_scheduled, in_flight={}),
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
    assert stored["status"] == "running"
    assert stored["error"] is None
    assert direct_calls == []
    submit_scheduled.assert_awaited_once()
    assert submit_scheduled.await_args.kwargs == {}


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
            "metadata": {},
            "user_context": None,
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
            "metadata": {},
            "user_context": None,
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
        calls: list[dict[str, Any]] = []
        service = ScheduledTaskService(
            controller=SimpleNamespace(),
            store=ScheduledTaskStore(),
            request_store=request_store,
        )
        claimed = request_store.claim(request.id)
        assert claimed is not None
        assert claimed.agent_name == "pm"

        archived = agent_store.archive("pm")
        assert archived is not None

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
        calls: list[dict[str, Any]] = []
        service = ScheduledTaskService(
            controller=SimpleNamespace(),
            store=ScheduledTaskStore(),
            request_store=request_store,
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
                "metadata": {},
                "user_context": None,
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


def test_hfr_477_scheduler_consumes_one_shot_atomically_but_manual_run_does_not(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- only the scheduler may consume a one-shot definition."""

    db_path = _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
        metadata={TASK_SCHEDULE_CONSUMED_METADATA_KEY: True},
    )

    manual = requests.enqueue_task_run(task.id, source_kind="cli", task=task)
    assert manual is not None
    store.load()
    armed = store.get_task(task.id)
    assert armed is not None
    assert (armed.enabled, armed.retired_at, armed.retirement_reason) == (
        True,
        None,
        None,
    )

    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=requests,
    )
    service._job_ids[task.id] = "test-one-shot"
    asyncio.run(
        service._run_task(
            task.id,
            task.run_at,
            task.timezone,
            task.updated_at,
            "test-one-shot",
        )
    )

    stored = requests._sqlite.get_scheduled_task(task.id)
    assert stored is not None
    assert stored["enabled"] is False
    assert stored["retired_at"] is not None
    assert stored["retirement_reason"] == "schedule_consumed"
    runs = [
        row
        for row in requests._sqlite.list_runs()
        if row["definition_id"] == task.id
    ]
    assert {row["source_kind"] for row in runs} == {"cli", "scheduler"}
    by_source = {row["source_kind"]: row for row in runs}
    assert TASK_SCHEDULE_CONSUMED_METADATA_KEY not in by_source["cli"]["metadata"]
    generation = by_source["scheduler"]["metadata"][
        TASK_SCHEDULE_CONSUMED_METADATA_KEY
    ]
    assert generation["job_id"] == "test-one-shot"
    assert generation["run_at"] == task.run_at
    assert stored["last_run_id"] == by_source["scheduler"]["id"]


def test_hfr_477_only_a_consumed_one_shot_forces_the_executor_mirror_reload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- the terminal transition, not scheduler provenance, owns reload."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    one_shot = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    cron = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    queued_one_shot = requests.enqueue_task_run(
        one_shot.id,
        source_kind="scheduler",
        task=one_shot,
        expected_run_at=one_shot.run_at,
        expected_timezone=one_shot.timezone,
        expected_job_id="test-one-shot",
    )
    queued_cron = requests.enqueue_task_run(
        cron.id,
        source_kind="scheduler",
        task=cron,
    )
    assert queued_one_shot is not None and queued_cron is not None

    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=requests,
    )
    reloads: list[str] = []
    real_load = store.load
    real_maybe_reload = store.maybe_reload

    def _load() -> None:
        reloads.append("load")
        real_load()

    def _maybe_reload() -> bool:
        reloads.append("maybe_reload")
        return real_maybe_reload()

    async def _execute(task, **_kwargs):
        return TaskExecutionResult(
            error=None,
            session_key=task.session_key,
            session_id=task.session_id,
        )

    monkeypatch.setattr(store, "load", _load)
    monkeypatch.setattr(store, "maybe_reload", _maybe_reload)
    monkeypatch.setattr(service, "_execute_task", _execute)

    claimed_one_shot = requests.claim(queued_one_shot.id)
    assert claimed_one_shot is not None
    asyncio.run(service._execute_claimed_request(claimed_one_shot))
    assert reloads == ["load"]

    reloads.clear()
    claimed_cron = requests.claim(queued_cron.id)
    assert claimed_cron is not None
    asyncio.run(service._execute_claimed_request(claimed_cron))
    assert reloads == ["maybe_reload"]


def test_hfr_477_late_consumed_run_cannot_retire_replacement_schedule(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- only the exact scheduler consumption owns retirement."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    first_run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    replacement_run_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=first_run_at,
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=requests,
    )
    service._job_ids[task.id] = "test-one-shot"
    asyncio.run(
        service._run_task(
            task.id,
            first_run_at,
            "UTC",
            task.updated_at,
            "test-one-shot",
        )
    )

    consumed = store.refresh_task(task.id)
    assert consumed is not None and consumed.retired_at is not None
    store.update_task(
        task.id,
        name=consumed.name,
        session_key=consumed.session_key,
        session_id=consumed.session_id,
        prompt=consumed.prompt,
        schedule_type="at",
        post_to=consumed.post_to,
        deliver_key=consumed.deliver_key,
        cron=None,
        run_at=replacement_run_at,
        timezone_name="UTC",
        agent_name=consumed.agent_name,
        session_policy=consumed.session_policy,
    )
    store.set_enabled(task.id, True)

    consumed_run = next(
        row
        for row in requests._sqlite.list_runs()
        if row["definition_id"] == task.id and row["source_kind"] == "scheduler"
    )
    generation = consumed_run["metadata"][TASK_SCHEDULE_CONSUMED_METADATA_KEY]
    assert not store.mark_task_result(
        task.id,
        error=None,
        disable_one_shot=True,
        expected_schedule_generation=generation,
        expected_terminal_run_id=consumed_run["id"],
    )

    replacement = store.refresh_task(task.id)
    assert replacement is not None
    assert (replacement.enabled, replacement.run_at) == (True, replacement_run_at)
    assert (replacement.retired_at, replacement.retirement_reason) == (None, None)
    assert replacement.last_run_at is None


def test_hfr_477_manual_rerun_of_retired_one_shot_preserves_terminal_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- a manual rerun adds history without reviving the schedule."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    consumed = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="generation-a",
    )
    assert consumed is not None
    store.load()
    retired = store.refresh_task(task.id)
    assert retired is not None
    terminal = (retired.retired_at, retired.retirement_reason, retired.last_run_id)

    manual = requests.enqueue_task_run(task.id, source_kind="cli", task=retired)

    assert manual is not None
    assert TASK_SCHEDULE_CONSUMED_METADATA_KEY not in manual.metadata
    current = store.refresh_task(task.id)
    assert current is not None
    assert current.enabled is False
    assert (current.retired_at, current.retirement_reason, current.last_run_id) == terminal


@pytest.mark.parametrize("retirement_reason", ["schedule_missed", "schedule_consumed"])
def test_hfr_477_non_owner_manual_failure_preserves_retired_one_shot_projection(
    tmp_path: Path,
    monkeypatch,
    retirement_reason: str,
) -> None:
    """HFR-477/HFR-478 -- a manual Run owns history, never retired definition facts."""

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = _add_command_task(
        store,
        shell_command="echo manual failure >&2; exit 7",
        cwd=str(tmp_path),
        schedule_type="at",
    )
    if retirement_reason == "schedule_consumed":
        owner = requests.enqueue_task_run(
            task.id,
            source_kind="scheduler",
            task=task,
            expected_run_at=task.run_at,
            expected_timezone=task.timezone,
            expected_job_id="generation-a",
        )
        assert owner is not None
    else:
        assert store.sqlite_backend is not None
        assert store.sqlite_backend.retire_missed_one_shot(
            task.id,
            expected_run_at=str(task.run_at),
            expected_timezone=task.timezone,
            expected_updated_at=task.updated_at,
            retired_at="2026-07-28T09:00:01+00:00",
        )
    store.load()
    retired = store.get_task(task.id)
    assert retired is not None
    assert retired.retirement_reason == retirement_reason
    definition_before = _stored_definition_row(task.id)

    manual = requests.enqueue_task_run(task.id, source_kind="cli", task=retired)
    assert manual is not None
    claimed = requests.claim(manual.id)
    assert claimed is not None
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    asyncio.run(service._execute_claimed_request(claimed))

    definition_after = _stored_definition_row(task.id)
    assert definition_after == definition_before
    run = requests.get_run(manual.id)
    assert run is not None
    assert (run["status"], run["exit_code"]) == ("failed", 7)
    assert "manual failure" in str(run["error"])
    assert TASK_SCHEDULE_CONSUMED_METADATA_KEY not in (run["metadata"] or {})
    notice = requests.sqlite_backend.owed_failure_notice(manual.id)
    assert notice is not None and notice["state"] == "pending"


@pytest.mark.parametrize("replacement_schedule", ["at", "cron"])
def test_hfr_477_retired_manual_escalation_preserves_replacement_schedule(
    tmp_path: Path,
    monkeypatch,
    replacement_schedule: str,
) -> None:
    """HFR-477 -- an old manual Run may enqueue, but never rewrite its replacement."""

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(
        workdir=tmp_path,
        anchor=f"avibe_retired_escalation_replacement_{replacement_schedule}",
    )
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo old manual failure >&2; exit 7",
        schedule_type="at",
        session_id=session_id,
        agent_name="codex",
    )
    assert store.sqlite_backend is not None
    assert store.sqlite_backend.retire_missed_one_shot(
        task.id,
        expected_run_at=str(task.run_at),
        expected_timezone=task.timezone,
        expected_updated_at=task.updated_at,
        retired_at="2026-07-28T09:00:01+00:00",
    )
    store.load()
    retired = store.get_task(task.id)
    assert retired is not None and retired.retired_at is not None

    requests = TaskExecutionStore()
    manual = requests.enqueue_task_run(task.id, source_kind="cli", task=retired)
    assert manual is not None
    claimed = requests.claim(manual.id)
    assert claimed is not None
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    replacement_row: dict[str, Any] = {}
    real_runner = scheduled_tasks.run_supervised_command

    async def _replace_while_running(**kwargs):
        writer = ScheduledTaskStore()
        current = writer.get_task(task.id)
        assert current is not None
        replacement_run_at = (
            "2026-07-29T10:30:00+00:00"
            if replacement_schedule == "at"
            else None
        )
        writer.update_task(
            task.id,
            name=current.name,
            session_key=current.session_key,
            session_id=current.session_id,
            prompt="replacement definition",
            schedule_type=replacement_schedule,
            post_to=current.post_to,
            deliver_key=current.deliver_key,
            cron="30 10 * * *" if replacement_schedule == "cron" else None,
            run_at=replacement_run_at,
            timezone_name="UTC",
            agent_name=current.agent_name,
            session_policy=current.session_policy,
        )
        replacement_row.update(_stored_definition_row(task.id))
        return await real_runner(**kwargs)

    monkeypatch.setattr(
        scheduled_tasks,
        "run_supervised_command",
        _replace_while_running,
    )

    asyncio.run(service._execute_claimed_request(claimed))

    assert _stored_definition_row(task.id) == replacement_row
    run = requests.get_run(manual.id)
    assert run is not None and (run["status"], run["exit_code"]) == ("failed", 7)
    escalations = _escalation_runs(store)
    assert len(escalations) == 1
    assert escalations[0]["parent_run_id"] == manual.id
    assert escalations[0]["agent_name"] == "codex"
    assert requests.sqlite_backend.owed_failure_notice(manual.id) is None


@pytest.mark.parametrize("authority_loss", ["deleted", "reclaimed", "canceled"])
def test_hfr_477_retired_manual_escalation_refuses_lost_authority(
    tmp_path: Path,
    monkeypatch,
    authority_loss: str,
) -> None:
    """HFR-477 -- outbox enqueue rechecks deletion, reclaim, and cancellation."""

    from storage.session_reclaim import RECLAIM_PAUSE, reclaim_bound_definitions

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(
        workdir=tmp_path,
        anchor=f"avibe_retired_escalation_refusal_{authority_loss}",
    )
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo refused escalation >&2; exit 7",
        schedule_type="at",
        session_id=session_id,
        agent_name="codex",
    )
    assert store.sqlite_backend is not None
    assert store.sqlite_backend.retire_missed_one_shot(
        task.id,
        expected_run_at=str(task.run_at),
        expected_timezone=task.timezone,
        expected_updated_at=task.updated_at,
        retired_at="2026-07-28T09:00:01+00:00",
    )
    store.load()
    retired = store.get_task(task.id)
    assert retired is not None and retired.retired_at is not None

    requests = TaskExecutionStore()
    manual = requests.enqueue_task_run(task.id, source_kind="cli", task=retired)
    assert manual is not None
    claimed = requests.claim(manual.id)
    assert claimed is not None
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    original_enqueue = (
        scheduled_tasks.SQLiteBackgroundTaskStore
        .enqueue_task_escalation_without_definition_write
    )

    def _lose_authority_before_transaction(backend, definition_id, **kwargs):
        if authority_loss == "deleted":
            assert backend.remove_task(definition_id)
        elif authority_loss == "reclaimed":
            with backend.engine.begin() as conn:
                summary = reclaim_bound_definitions(
                    conn,
                    session_id,
                    mode=RECLAIM_PAUSE,
                    reason="the bound session was replaced",
                )
            assert summary["paused"] == 1
        else:
            assert backend.cancel_run(manual.id)
        return original_enqueue(backend, definition_id, **kwargs)

    monkeypatch.setattr(
        scheduled_tasks.SQLiteBackgroundTaskStore,
        "enqueue_task_escalation_without_definition_write",
        _lose_authority_before_transaction,
    )

    asyncio.run(service._execute_claimed_request(claimed))

    run = requests.get_run(manual.id)
    assert run is not None
    assert _escalation_runs(store) == []
    assert run["metadata"].get("escalation_run_id") is None
    if authority_loss == "canceled":
        assert run["status"] == "canceled"
    else:
        assert run["status"] == "failed"
        notice = requests.sqlite_backend.owed_failure_notice(manual.id)
        assert notice is not None and notice["state"] == "pending"


def test_hfr_477_old_queued_run_does_not_suppress_replacement_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- successor suppression is scoped to one exact DateTrigger."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="generation A",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    first = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        suppress_scheduler_successor=True,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="generation-a",
    )
    assert first is not None
    store.load()
    retired = store.refresh_task(task.id)
    replacement_run_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    replacement = store.update_task(
        task.id,
        name=retired.name,
        session_key=retired.session_key,
        session_id=retired.session_id,
        prompt="generation B",
        schedule_type="at",
        post_to=retired.post_to,
        deliver_key=retired.deliver_key,
        cron=None,
        run_at=replacement_run_at,
        timezone_name="UTC",
        agent_name=retired.agent_name,
        session_policy=retired.session_policy,
    )
    store.set_enabled(task.id, True)
    replacement = store.refresh_task(task.id)
    second = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=replacement,
        suppress_scheduler_successor=True,
        expected_run_at=replacement.run_at,
        expected_timezone=replacement.timezone,
        expected_job_id="generation-b",
    )

    assert second is not None
    assert second.id != first.id
    assert requests.get_run(first.id)["status"] == "queued"
    current = store.refresh_task(task.id)
    assert current is not None and current.last_run_id == second.id


def test_hfr_477_old_queued_run_cannot_execute_replacement_definition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- a claimed old fire keeps its history but cannot run generation B."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="generation A",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    queued = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="generation-a",
    )
    assert queued is not None
    store.load()
    retired = store.get_task(task.id)
    store.update_task(
        task.id,
        name=retired.name,
        session_key=retired.session_key,
        session_id=retired.session_id,
        prompt="generation B",
        schedule_type="cron",
        post_to=retired.post_to,
        deliver_key=retired.deliver_key,
        cron="0 * * * *",
        run_at=None,
        timezone_name="UTC",
        agent_name=retired.agent_name,
        session_policy=retired.session_policy,
    )
    store.set_enabled(task.id, True)
    dispatched: list[str] = []
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=requests,
    )

    async def _execute_request(**kwargs):
        dispatched.append(kwargs["prompt"])
        return None

    service._execute_request = _execute_request
    claimed = requests.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    assert dispatched == []
    old_run = requests.get_run(queued.id)
    assert old_run is not None and old_run["status"] == "failed"
    replacement = store.refresh_task(task.id)
    assert replacement is not None
    assert (replacement.enabled, replacement.schedule_type, replacement.prompt) == (
        True,
        "cron",
        "generation B",
    )
    assert replacement.last_run_at is None


def test_hfr_477_result_cas_rejects_replacement_after_mirror_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- replacement in the result read/write window wins atomically."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="generation A",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    queued = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="generation-a",
    )
    assert queued is not None
    generation = task_schedule_generation(queued.metadata)
    assert generation is not None
    store.load()
    real_upsert = store.sqlite_backend.upsert_scheduled_task

    def _replace_then_write(payload, **kwargs):
        writer = ScheduledTaskStore()
        current = writer.get_task(task.id)
        writer.update_task(
            task.id,
            name=current.name,
            session_key=current.session_key,
            session_id=current.session_id,
            prompt="generation B",
            schedule_type="cron",
            post_to=current.post_to,
            deliver_key=current.deliver_key,
            cron="0 * * * *",
            run_at=None,
            timezone_name="UTC",
            agent_name=current.agent_name,
            session_policy=current.session_policy,
        )
        writer.set_enabled(task.id, True)
        return real_upsert(payload, **kwargs)

    monkeypatch.setattr(store.sqlite_backend, "upsert_scheduled_task", _replace_then_write)
    landed = store.mark_task_result(
        task.id,
        error=None,
        expected_schedule_generation=generation,
        expected_terminal_run_id=queued.id,
    )

    assert landed is False
    replacement = store.refresh_task(task.id)
    assert replacement is not None
    assert (replacement.enabled, replacement.schedule_type, replacement.prompt) == (
        True,
        "cron",
        "generation B",
    )
    assert replacement.last_run_at is None


def test_hfr_477_consumed_result_survives_unrelated_definition_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- mutable copy edits do not replace the consumed generation."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        name="generation A",
        session_key="slack::channel::C123",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="existing",
    )
    queued = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="generation-a",
    )
    assert queued is not None
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=requests,
    )
    assert store.maybe_reload() is False

    async def _edit_while_running(**_kwargs):
        writer = ScheduledTaskStore()
        current = writer.get_task(task.id)
        assert current is not None
        writer.update_task(
            task.id,
            name="renamed while running",
            session_key=current.session_key,
            session_id=current.session_id,
            prompt="edited prompt",
            schedule_type=current.schedule_type,
            post_to=current.post_to,
            deliver_key=current.deliver_key,
            cron=current.cron,
            run_at=current.run_at,
            timezone_name=current.timezone,
            agent_name=current.agent_name,
            session_policy=current.session_policy,
        )
        return TaskDispatchResult(error=None)

    service._execute_request = _edit_while_running
    claimed = requests.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    settled = requests.get_run(queued.id)
    assert settled is not None and settled["status"] == "succeeded"
    current = store.refresh_task(task.id)
    assert current is not None
    assert (current.name, current.prompt) == ("renamed while running", "edited prompt")
    assert current.last_run_id == queued.id
    assert current.retired_at == task_schedule_generation(queued.metadata)["retired_at"]
    assert current.last_run_at is not None
    assert current.last_error is None


def test_hfr_477_enqueue_exception_records_failed_terminal_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- a DateTrigger callback cannot disappear before enqueue."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=requests,
    )
    service._job_ids[task.id] = "generation-a"
    real_enqueue = requests._sqlite.enqueue_definition_run

    def _fail_enqueue(payload, **kwargs):
        if kwargs.get("terminal_error") is None:
            raise RuntimeError("queue unavailable")
        return real_enqueue(payload, **kwargs)

    monkeypatch.setattr(requests._sqlite, "enqueue_definition_run", _fail_enqueue)
    asyncio.run(
        service._run_task(
            task.id,
            task.run_at,
            task.timezone,
            task.updated_at,
            "generation-a",
        )
    )

    current = requests._sqlite.get_scheduled_task(task.id)
    assert current is not None
    assert current["lifecycle_state"] == "finished"
    assert current["lifecycle_detail"] == "error"
    owner = requests.get_run(current["last_run_id"])
    assert owner is not None and owner["status"] == "failed"
    assert "queue unavailable" in owner["error"]
    assert task_schedule_generation(owner["metadata"])["job_id"] == "generation-a"
    notice = requests._sqlite.owed_failure_notice(owner["id"])
    assert notice is not None and notice["state"] == "pending"
    assert [row["id"] for row in requests._sqlite.list_owed_failure_notices()] == [
        owner["id"]
    ]
    compact = requests._sqlite.list_scheduled_tasks_page(
        page_request=PageRequest(limit=20),
        include_successful_finished=False,
    )
    assert task.id in {item["id"] for item in compact.items}

    emitted: list[str] = []

    async def _emit(run, _notice, evidence):
        emitted.append(run["id"])
        evidence.delivered_id = "notice-1"
        evidence.persisted_row = {"id": "notice-1"}
        evidence.send_returned = True
        return True

    service._emit_failure_notice = _emit
    service._owns_service_instance = lambda: True
    asyncio.run(service._drain_failure_notices())

    assert emitted == [owner["id"]]
    sent = requests._sqlite.owed_failure_notice(owner["id"])
    assert sent is not None and sent["state"] == "sent"


def test_hfr_477_job_error_event_recovers_only_registered_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- EVENT_JOB_ERROR carries the same exact DateTrigger owner."""

    from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    job_id = service._job_ids[task.id]
    event = JobExecutionEvent(
        EVENT_JOB_ERROR,
        job_id,
        "default",
        resolve_run_at(task.run_at, task.timezone),
        exception=RuntimeError("callback failed"),
    )

    service._on_scheduler_event(event)

    current = service.request_store._sqlite.get_scheduled_task(task.id)
    assert current is not None and current["lifecycle_detail"] == "error"
    owner = service.request_store.get_run(current["last_run_id"])
    assert owner is not None and owner["status"] == "failed"
    generation = task_schedule_generation(owner["metadata"])
    assert generation is not None and generation["job_id"] == job_id


def test_hfr_478_misfire_retires_only_the_schedule_apscheduler_observed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-478 -- missed recovery records evidence and rejects stale events."""

    from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    original_instant = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(
        microsecond=0
    )
    original_run_at = original_instant.isoformat()
    replacement_run_at = original_instant.astimezone(
        timezone(timedelta(hours=8))
    ).isoformat()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=original_run_at,
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    original_job_id = service._job_ids[task.id]
    stale_event = JobExecutionEvent(
        EVENT_JOB_MISSED,
        original_job_id,
        "default",
        resolve_run_at(original_run_at, "UTC"),
    )

    store.update_task(
        task.id,
        name=task.name,
        session_key=task.session_key,
        session_id=task.session_id,
        prompt=task.prompt,
        schedule_type="at",
        post_to=task.post_to,
        deliver_key=task.deliver_key,
        cron=None,
        run_at=replacement_run_at,
        timezone_name="Asia/Shanghai",
        agent_name=task.agent_name,
        session_policy=task.session_policy,
    )
    service.reconcile_jobs()
    replacement_job_id = service._job_ids[task.id]
    assert replacement_job_id != original_job_id
    service._on_scheduler_event(stale_event)
    current = store.refresh_task(task.id)
    assert current is not None
    assert (current.enabled, current.retired_at, current.retirement_reason) == (
        True,
        None,
        None,
    )

    missed_event = JobExecutionEvent(
        EVENT_JOB_MISSED,
        replacement_job_id,
        "default",
        resolve_run_at(replacement_run_at, "Asia/Shanghai"),
    )
    service.scheduler.remove_job(replacement_job_id)
    service._on_scheduler_event(missed_event)
    missed = store.refresh_task(task.id)
    assert missed is not None
    assert missed.enabled is False
    assert missed.retired_at is not None
    assert missed.retirement_reason == "schedule_missed"
    assert service.scheduler.get_jobs() == []
    assert task.id not in service._job_ids
    assert [
        row
        for row in service.request_store._sqlite.list_runs()
        if row["definition_id"] == task.id
    ] == []


@pytest.mark.parametrize("replacement_schedule", ["cron", "at"])
@pytest.mark.parametrize("event_code", ["missed", "error"])
def test_hfr_478_stale_event_reconciles_the_current_replacement_schedule(
    tmp_path: Path,
    monkeypatch,
    replacement_schedule: str,
    event_code: str,
) -> None:
    """HFR-478 -- rejecting a removed DateTrigger restores current intent."""

    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    original_run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    task = store.add_task(
        session_key="",
        prompt="original",
        schedule_type="at",
        run_at=original_run_at,
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    original_job_id = service._job_ids[task.id]
    original_identity = service._one_shot_job_identities[original_job_id]
    assert store.maybe_reload() is False

    writer = ScheduledTaskStore()
    current = writer.get_task(task.id)
    assert current is not None
    replacement_run_at = (
        (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        if replacement_schedule == "at"
        else None
    )
    replacement = writer.update_task(
        task.id,
        name=current.name,
        session_key=current.session_key,
        session_id=current.session_id,
        prompt="replacement",
        schedule_type=replacement_schedule,
        post_to=current.post_to,
        deliver_key=current.deliver_key,
        cron="0 * * * *" if replacement_schedule == "cron" else None,
        run_at=replacement_run_at,
        timezone_name="UTC",
        agent_name=current.agent_name,
        session_policy=current.session_policy,
    )

    # APScheduler removes a DateTrigger before publishing its terminal event.
    service.scheduler.remove_job(original_job_id)
    code = EVENT_JOB_ERROR if event_code == "error" else EVENT_JOB_MISSED
    event = JobExecutionEvent(
        code,
        original_job_id,
        "default",
        resolve_run_at(original_identity[1], original_identity[2]),
        exception=RuntimeError("stale callback") if event_code == "error" else None,
    )
    service._on_scheduler_event(event)

    refreshed = store.refresh_task(task.id)
    assert refreshed is not None
    assert (refreshed.enabled, refreshed.retired_at, refreshed.schedule_type) == (
        True,
        None,
        replacement_schedule,
    )
    assert refreshed.updated_at == replacement.updated_at
    assert len(service.scheduler.get_jobs()) == 1
    replacement_job_id = service._job_ids[task.id]
    replacement_job = service.scheduler.get_job(replacement_job_id)
    assert replacement_job is not None
    assert replacement_job.args[0] == task.id
    if replacement_schedule == "at":
        assert replacement_job_id != original_job_id
        assert tuple(replacement_job.args[1:4]) == (
            replacement_run_at,
            "UTC",
            replacement.updated_at,
        )
    else:
        assert replacement_job_id == task.id
        assert tuple(replacement_job.args[1:4]) == (None, None, None)
    assert original_job_id not in service._one_shot_job_identities


@pytest.mark.parametrize("replacement_schedule", ["cron", "at"])
def test_hfr_477_normal_stale_callback_reconciles_the_current_replacement_schedule(
    tmp_path: Path,
    monkeypatch,
    replacement_schedule: str,
) -> None:
    """HFR-477 -- a rejected normal DateTrigger returns current intent to its owner."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="original",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    original_job_id = service._job_ids[task.id]
    original_identity = service._one_shot_job_identities[original_job_id]

    writer = ScheduledTaskStore()
    current = writer.get_task(task.id)
    assert current is not None
    replacement_run_at = (
        (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        if replacement_schedule == "at"
        else None
    )
    replacement = writer.update_task(
        task.id,
        name=current.name,
        session_key=current.session_key,
        session_id=current.session_id,
        prompt="replacement",
        schedule_type=replacement_schedule,
        post_to=current.post_to,
        deliver_key=current.deliver_key,
        cron="0 * * * *" if replacement_schedule == "cron" else None,
        run_at=replacement_run_at,
        timezone_name="UTC",
        agent_name=current.agent_name,
        session_policy=current.session_policy,
    )
    service.scheduler.remove_job(original_job_id)

    asyncio.run(service._run_task(task.id, *original_identity[1:], original_job_id))

    assert service.request_store.list_pending() == []
    jobs = service.scheduler.get_jobs()
    assert len(jobs) == 1
    replacement_job = jobs[0]
    assert replacement_job.args[0] == task.id
    assert service._job_ids[task.id] == replacement_job.id
    if replacement_schedule == "at":
        assert replacement_job.id != original_job_id
        assert tuple(replacement_job.args[1:4]) == (
            replacement_run_at,
            "UTC",
            replacement.updated_at,
        )
    else:
        assert replacement_job.id == task.id
        assert tuple(replacement_job.args[1:4]) == (None, None, None)


def test_hfr_477_enqueue_race_reconciles_the_current_replacement_schedule(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- losing the atomic enqueue CAS still restores current intent."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="original",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    original_job_id = service._job_ids[task.id]
    original_identity = service._one_shot_job_identities[original_job_id]
    replacement_run_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    def _replace_before_atomic_enqueue(*_args, **_kwargs):
        writer = ScheduledTaskStore()
        current = writer.get_task(task.id)
        assert current is not None
        writer.update_task(
            task.id,
            name=current.name,
            session_key=current.session_key,
            session_id=current.session_id,
            prompt="replacement",
            schedule_type="at",
            post_to=current.post_to,
            deliver_key=current.deliver_key,
            cron=None,
            run_at=replacement_run_at,
            timezone_name="UTC",
            agent_name=current.agent_name,
            session_policy=current.session_policy,
        )
        return None

    monkeypatch.setattr(
        service.request_store,
        "enqueue_task_run",
        _replace_before_atomic_enqueue,
    )
    service.scheduler.remove_job(original_job_id)

    asyncio.run(service._run_task(task.id, *original_identity[1:], original_job_id))

    replacement = store.refresh_task(task.id)
    assert replacement is not None and replacement.run_at == replacement_run_at
    jobs = service.scheduler.get_jobs()
    assert len(jobs) == 1
    replacement_job = jobs[0]
    assert replacement_job.id != original_job_id
    assert tuple(replacement_job.args[1:4]) == (
        replacement_run_at,
        "UTC",
        replacement.updated_at,
    )


def test_hfr_477_stale_scheduler_enqueue_cannot_consume_a_replacement_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- the storage CAS rejects stale and non-``at`` callbacks."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    task = store.add_task(
        session_key="",
        prompt="original",
        schedule_type="at",
        run_at=run_at,
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    original_updated_at = task.updated_at

    replacement = store.update_task(
        task.id,
        name=task.name,
        session_key=task.session_key,
        session_id=task.session_id,
        prompt="replacement",
        schedule_type="at",
        post_to=task.post_to,
        deliver_key=task.deliver_key,
        cron=None,
        run_at=run_at,
        timezone_name="UTC",
        agent_name=task.agent_name,
        session_policy=task.session_policy,
    )
    stale = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=run_at,
        expected_timezone="UTC",
        expected_updated_at=original_updated_at,
        expected_job_id="stale-job",
    )
    assert stale is None
    assert store.refresh_task(task.id).enabled is True

    cron = store.update_task(
        task.id,
        name=replacement.name,
        session_key=replacement.session_key,
        session_id=replacement.session_id,
        prompt=replacement.prompt,
        schedule_type="cron",
        post_to=replacement.post_to,
        deliver_key=replacement.deliver_key,
        cron="0 * * * *",
        run_at=None,
        timezone_name="UTC",
        agent_name=replacement.agent_name,
        session_policy=replacement.session_policy,
    )
    stale_after_cron = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=cron,
        expected_run_at=run_at,
        expected_timezone="UTC",
        expected_updated_at=replacement.updated_at,
        expected_job_id="stale-job",
    )
    assert stale_after_cron is None
    assert store.refresh_task(task.id).schedule_type == "cron"


@pytest.mark.parametrize("schedule", ["cron", "at"])
def test_hfr_483_registered_job_fires_through_its_own_scheduler_arguments(
    tmp_path: Path,
    monkeypatch,
    schedule: str,
) -> None:
    """HFR-483 -- every registered schedule enqueues from the args it registered.

    ``reconcile_jobs`` gives every job its APScheduler job id so the callback can
    reject a stale generation, but only an ``at`` job carries a run_at. Firing the
    registered arguments -- rather than a hand-built call -- is what proves a cron
    job never presents the job id as half of a one-shot schedule identity.
    """

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    run_at = (
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        if schedule == "at"
        else None
    )
    task = store.add_task(
        session_key="",
        prompt="daily digest",
        schedule_type=schedule,
        cron="0 11 * * *" if schedule == "cron" else None,
        run_at=run_at,
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()

    jobs = service.scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.args[0] == task.id
    assert job.args[4] == job.id
    if schedule == "cron":
        assert job.id == task.id
        assert tuple(job.args[1:4]) == (None, None, None)
    else:
        assert tuple(job.args[1:4]) == (run_at, "UTC", task.updated_at)

    asyncio.run(job.func(*job.args))

    pending = service.request_store.list_pending()
    assert [(request.task_id, request.source_kind) for request in pending] == [
        (task.id, "scheduler")
    ]
    refreshed = store.refresh_task(task.id)
    assert refreshed is not None
    if schedule == "cron":
        # A recurring definition survives its own fire; only a one-shot is consumed.
        assert refreshed.enabled is True
        assert refreshed.retired_at is None
    else:
        assert refreshed.enabled is False
        assert refreshed.retired_at is not None


@pytest.mark.parametrize("mirror", ["fresh", "stale"])
def test_hfr_484_in_flight_cron_fire_cannot_spend_a_replacement_one_shot(
    tmp_path: Path,
    monkeypatch,
    mirror: str,
) -> None:
    """HFR-484 -- a cron callback that races an edit to ``at`` enqueues nothing.

    A cron registration carries no schedule identity, so nothing downstream could
    retire the replacement. Enqueueing would spend a fire the new run_at has not
    reached and leave that one-shot still armed to run a second time.

    Both layers are exercised because ``refresh_task`` is a mirror read and can
    legitimately lag a writer on another connection (HFR-277). ``fresh`` rejects
    in the callback; ``stale`` falls through to the storage CAS, which is the
    real authority. Both must end with the replacement schedule registered.
    """

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="daily digest",
        schedule_type="cron",
        cron="0 11 * * *",
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    cron_job = service.scheduler.get_job(task.id)
    assert cron_job is not None

    writer = store if mirror == "fresh" else ScheduledTaskStore()
    run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    replacement = writer.update_task(
        task.id,
        name=task.name,
        session_key=task.session_key,
        session_id=task.session_id,
        prompt=task.prompt,
        schedule_type="at",
        post_to=task.post_to,
        deliver_key=task.deliver_key,
        cron=None,
        run_at=run_at,
        timezone_name="UTC",
        agent_name=task.agent_name,
        session_policy=task.session_policy,
    )

    # The already-dispatched cron callback still carries the cron registration.
    asyncio.run(cron_job.func(*cron_job.args))

    assert service.request_store.list_pending() == []
    refreshed = ScheduledTaskStore().get_task(task.id)
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.retired_at is None
    assert refreshed.run_at == run_at
    # Either way the rejected fire hands the replacement schedule back to the
    # scheduler. Nothing else can: the cron job that keeps firing IS the stale
    # generation, so a rejection that left it installed would strand the
    # one-shot -- unregistered, and never fired.
    jobs = service.scheduler.get_jobs()
    assert len(jobs) == 1
    assert tuple(jobs[0].args[1:4]) == (run_at, "UTC", replacement.updated_at)


def test_hfr_484_cron_fire_runs_the_definition_current_at_fire_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-484 -- a cron fire racing a benign edit runs the refreshed definition.

    ``refresh_task`` is deliberate: a recurring schedule has no fire to spend, so
    the useful reading of "run this definition now" is the definition as it stands
    at fire time. This is the counterpart to the rejection above -- an edit that
    keeps the definition recurring must not silently drop the fire.
    """

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="original prompt",
        schedule_type="cron",
        cron="0 11 * * *",
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    cron_job = service.scheduler.get_job(task.id)
    assert cron_job is not None

    writer = ScheduledTaskStore()
    writer.update_task(
        task.id,
        name=task.name,
        session_key=task.session_key,
        session_id=task.session_id,
        prompt="edited prompt",
        schedule_type="cron",
        post_to=task.post_to,
        deliver_key=task.deliver_key,
        cron="0 11 * * *",
        run_at=None,
        timezone_name="UTC",
        agent_name=task.agent_name,
        session_policy=task.session_policy,
    )

    asyncio.run(cron_job.func(*cron_job.args))

    pending = service.request_store.list_pending()
    assert [(request.task_id, request.prompt) for request in pending] == [
        (task.id, "edited prompt")
    ]
    refreshed = store.refresh_task(task.id)
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.retired_at is None


def test_hfr_484_backlogged_cron_fire_leaves_its_own_registration_alone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-484 -- a refused cron fire only reconciles when the schedule changed.

    Successor suppression refuses a fire whose predecessor has not started yet,
    and that refusal looks exactly like the stale-generation one at the callback.
    Reconciling on every refusal would re-register a live cron job -- and reset
    its next fire -- each time the queue is merely backed up, so the reconcile
    is gated on the reloaded definition no longer being that cron.
    """

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="daily digest",
        schedule_type="cron",
        cron="0 11 * * *",
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=store,
        request_store=TaskExecutionStore(),
    )
    service.scheduler = _StubScheduler()
    service.reconcile_jobs()
    cron_job = service.scheduler.get_job(task.id)
    assert cron_job is not None

    # The predecessor fire is queued and unstarted, so the next one is refused.
    service.request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        suppress_scheduler_successor=True,
    )
    reconciled = 0

    def _count_reconcile() -> None:
        nonlocal reconciled
        reconciled += 1

    monkeypatch.setattr(service, "reconcile_jobs", _count_reconcile)

    asyncio.run(cron_job.func(*cron_job.args))

    assert reconciled == 0
    assert len(service.request_store.list_pending()) == 1


def test_hfr_477_consumed_terminal_outcome_belongs_to_the_consuming_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-477 -- prior manual history cannot hide a canceled consumed fire."""

    _binding_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    requests = TaskExecutionStore()
    task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="at",
        run_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        timezone_name="UTC",
        session_policy="create_per_run",
    )
    manual = requests.enqueue_task_run(task.id, source_kind="cli", task=task)
    assert manual is not None
    claimed = requests.claim(manual.id)
    assert claimed is not None
    assert requests.complete(claimed, ok=True) == "succeeded"
    manual_finished_at = requests.get_run(manual.id)["completed_at"]

    consumed = requests.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="test-one-shot",
    )
    assert consumed is not None
    assert requests.cancel_run(consumed.id)

    row = requests._sqlite.get_scheduled_task(task.id)
    terminal_run = requests.get_run(consumed.id)
    assert row is not None and terminal_run is not None
    assert row["last_run_id"] == consumed.id
    assert row["lifecycle_state"] == "finished"
    assert row["lifecycle_detail"] == "canceled"
    assert row["lifecycle_finished_at"] == terminal_run["completed_at"]
    assert row["last_run_at"] == terminal_run["completed_at"]
    assert row["last_run_at"] != manual_finished_at
    compact = requests._sqlite.list_scheduled_tasks_page(
        page_request=PageRequest(limit=20),
        include_successful_finished=False,
    )
    assert task.id in {item["id"] for item in compact.items}


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


def test_hfr_164_hfr_165_service_restart_joins_only_its_harness_lane_generations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    class _Supervisor:
        def __init__(self) -> None:
            self.generations: dict[RuntimeWorkLane, int] = {}
            self.current: dict[RuntimeWorkLane, RuntimeWorkRegistrationToken] = {}
            self.unregistered: list[RuntimeWorkLane] = []

        def register(self, lane, _handler):  # noqa: ANN001, ANN202
            assert lane not in self.current
            generation = self.generations.get(lane, 0) + 1
            self.generations[lane] = generation
            token = RuntimeWorkRegistrationToken(lane=lane, generation=generation)
            self.current[lane] = token
            return token

        def begin_unregister(self, token):  # noqa: ANN001, ANN202
            assert self.current.get(token.lane) == token
            self.current.pop(token.lane)
            self.unregistered.append(token.lane)

            async def _joined() -> None:
                return None

            return asyncio.create_task(_joined())

        def notify(self, *_lanes) -> None:  # noqa: ANN002
            return None

    async def _exercise() -> None:
        supervisor = _Supervisor()
        supervisor.register(RuntimeWorkLane.SESSION_DELIVERIES, object())
        controller = SimpleNamespace(
            platform_settings_managers={},
            runtime_work_supervisor=supervisor,
        )
        service = ScheduledTaskService(controller=controller)
        service.scheduler = _StubScheduler()
        service.start()
        expected_service_lanes = {
            RuntimeWorkLane.TASK_DEFINITIONS,
            RuntimeWorkLane.REQUESTS,
            RuntimeWorkLane.RUN_CALLBACKS,
            RuntimeWorkLane.VAULT_CALLBACKS,
            RuntimeWorkLane.FAILURE_NOTICES,
            RuntimeWorkLane.STALE_RUNS,
        }
        expected_controller_lanes = {
            RuntimeWorkLane.SESSION_DELIVERIES,
            RuntimeWorkLane.ACTIVITY_OUTPUTS,
        }
        assert set(supervisor.current) == {
            *expected_controller_lanes,
            *expected_service_lanes,
        }
        assert service._reconcile_task is None

        await service.stop()
        assert set(supervisor.current) == expected_controller_lanes
        assert set(supervisor.unregistered) == expected_service_lanes

        for expected_generation in (2, 3):
            service.scheduler = _StubScheduler()
            service.start()
            assert set(supervisor.current) == {
                *expected_controller_lanes,
                *expected_service_lanes,
            }
            assert all(
                supervisor.generations[lane] == expected_generation
                for lane in expected_service_lanes
            )
            await service.stop()
            assert set(supervisor.current) == expected_controller_lanes

    asyncio.run(_exercise())


def test_hfr_176_legacy_file_probes_wake_only_their_mapped_lanes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    notifications: list[tuple[RuntimeWorkLane, ...]] = []
    service = ScheduledTaskService(
        controller=SimpleNamespace(
            platform_settings_managers={},
            runtime_work_supervisor=SimpleNamespace(
                notify=lambda *lanes: notifications.append(lanes)
            ),
        ),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "requests"),
    )
    service._running = True
    service.request_store._ensure_dirs()
    service._legacy_request_signature = service.request_store._state_signature()

    async def _one_probe(delay: float) -> None:
        assert delay == 2
        (service.request_store.pending_dir / "edge.json").write_text(
            "{}",
            encoding="utf-8",
        )
        service._running = False

    monkeypatch.setattr(scheduled_tasks.asyncio, "sleep", _one_probe)
    asyncio.run(service._legacy_request_directory_probe())

    assert notifications == [
        (
            RuntimeWorkLane.REQUESTS,
            RuntimeWorkLane.RUN_CALLBACKS,
            RuntimeWorkLane.STALE_RUNS,
        )
    ]

    notifications.clear()
    service._running = True
    service._legacy_task_signature = scheduled_tasks._path_signature(
        service.store.path
    )

    async def _task_probe(delay: float) -> None:
        assert delay == 2
        service.store.path.write_text('{"tasks": []}', encoding="utf-8")
        service._running = False

    monkeypatch.setattr(scheduled_tasks.asyncio, "sleep", _task_probe)
    asyncio.run(service._legacy_task_definition_probe())
    assert notifications == [(RuntimeWorkLane.TASK_DEFINITIONS,)]


def test_failure_notice_scan_arms_the_earliest_retry_deadline() -> None:
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat()
    sqlite_store = SimpleNamespace(
        list_owed_failure_notices=lambda **_kwargs: [],
        next_owed_failure_notice_at=lambda: retry_at,
    )
    schedule_wake = Mock()
    service = SimpleNamespace(
        request_store=SimpleNamespace(sqlite_backend=sqlite_store),
        _schedule_runtime_work_wake=schedule_wake,
    )
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.FAILURE_NOTICES,
    )

    items, has_more = handler.scan(limit=10, occupied=frozenset(), cursor=None)

    assert items == []
    assert has_more is False
    lane, delay = schedule_wake.call_args.args
    assert lane is RuntimeWorkLane.FAILURE_NOTICES
    assert 0 < delay <= 8


def test_stale_lane_applies_leaked_lock_cleanup_on_the_controller_loop(
    tmp_path: Path,
) -> None:
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "requests"),
    )
    sweep_threads: list[int] = []
    cleanup_threads: list[int] = []
    service._propagate_requested_cancellations_async = AsyncMock()  # type: ignore[method-assign]

    def _sweep(*, release_leaked_locks: bool = True) -> None:
        assert release_leaked_locks is False
        sweep_threads.append(threading.get_ident())

    service._sweep_stale_runs = _sweep  # type: ignore[method-assign]
    service._release_leaked_session_locks = (  # type: ignore[method-assign]
        lambda: cleanup_threads.append(threading.get_ident()) or set()
    )
    scheduled = Mock()
    service._stale_run_sweep_delay_seconds = lambda: 7.0  # type: ignore[method-assign]
    service._schedule_runtime_work_wake = scheduled  # type: ignore[method-assign]
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.STALE_RUNS,
    )

    async def _exercise() -> int:
        loop_thread = threading.get_ident()
        assert await handler.process(
            scheduled_tasks.RuntimeWorkItem("stale-runs", None)
        )
        return loop_thread

    loop_thread = asyncio.run(_exercise())

    assert sweep_threads and sweep_threads[0] != loop_thread
    assert cleanup_threads == [loop_thread]
    scheduled.assert_called_once_with(RuntimeWorkLane.STALE_RUNS, 7.0)


def test_hfr_286_cancellation_wake_is_not_gated_by_stale_sweep_cadence(
    tmp_path: Path,
) -> None:
    service = ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=ScheduledTaskStore(tmp_path / "tasks.json"),
        request_store=TaskExecutionStore(tmp_path / "requests"),
    )
    cancellations = AsyncMock()
    sweep = Mock()
    scheduled = Mock()
    service._propagate_requested_cancellations_async = cancellations  # type: ignore[method-assign]
    service._sweep_stale_runs = sweep  # type: ignore[method-assign]
    service._stale_run_sweep_delay_seconds = lambda: 29.0  # type: ignore[method-assign]
    service._schedule_runtime_work_wake = scheduled  # type: ignore[method-assign]
    handler = scheduled_tasks._ScheduledRuntimeWorkHandler(
        service,
        RuntimeWorkLane.STALE_RUNS,
    )

    items, has_more = handler.scan(limit=4, occupied=frozenset(), cursor=None)

    assert [item.partition_key for item in items] == ["run-cancellations"]
    assert has_more is False
    scheduled.assert_called_once_with(RuntimeWorkLane.STALE_RUNS, 29.0)
    asyncio.run(handler.process(items[0]))
    cancellations.assert_awaited_once_with()
    sweep.assert_not_called()


@pytest.mark.parametrize("stop_entrypoint", ["stop", "lease_loss"])
def test_request_recovery_registration_is_owned_by_exact_service_generation(
    tmp_path: Path,
    monkeypatch,
    stop_entrypoint: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    class BarrierSupervisor:
        def __init__(self) -> None:
            self.generation = 0
            self.current: RuntimeWorkRegistrationToken | None = None
            self.handlers: list[Any] = []
            self.unregister_calls: list[RuntimeWorkRegistrationToken] = []
            self.unregister_entered = asyncio.Event()
            self.allow_unregister = asyncio.Event()

        def register(self, lane, handler):  # noqa: ANN001, ANN202
            assert lane is RuntimeWorkLane.REQUESTS
            assert self.current is None, "a replacement overlapped the prior generation"
            self.generation += 1
            token = RuntimeWorkRegistrationToken(lane=lane, generation=self.generation)
            self.current = token
            self.handlers.append(handler)
            return token

        def begin_unregister(
            self,
            token: RuntimeWorkRegistrationToken,
        ) -> asyncio.Task[None]:
            self.unregister_calls.append(token)
            assert self.current == token
            self.unregister_entered.set()

            async def _join() -> None:
                await self.allow_unregister.wait()
                if self.current == token:
                    self.current = None

            return asyncio.create_task(_join())

    async def _exercise() -> None:
        supervisor = BarrierSupervisor()
        controller = SimpleNamespace(
            platform_settings_managers={},
            runtime_work_supervisor=supervisor,
        )
        request_store = TaskExecutionStore()
        service = ScheduledTaskService(
            controller=controller,
            store=ScheduledTaskStore(),
            request_store=request_store,
        )
        service.scheduler = _StubScheduler()

        async def _watch_store() -> None:
            await asyncio.Event().wait()

        service._watch_store = _watch_store  # type: ignore[method-assign]
        service.start()
        first_token = service._request_recovery_token
        assert first_token is not None
        assert supervisor.handlers[-1].store is request_store.sqlite_backend

        if stop_entrypoint == "stop":
            stop_task = asyncio.create_task(service.stop())
        else:
            service._requires_service_lease = True
            monkeypatch.setattr(
                "core.scheduled_tasks.runtime.current_process_owns_service_instance",
                lambda: False,
            )
            assert service._owns_service_instance() is False
            stop_task = asyncio.create_task(service.stop())

        await asyncio.wait_for(supervisor.unregister_entered.wait(), timeout=1)
        assert service._request_recovery_token is None
        assert supervisor.unregister_calls == [first_token]
        with pytest.raises(RuntimeError, match="generation is still stopping"):
            service.start()

        supervisor.allow_unregister.set()
        await asyncio.wait_for(stop_task, timeout=1)
        assert supervisor.current is None

        service._requires_service_lease = False
        service.scheduler = _StubScheduler()
        service.start()
        second_token = service._request_recovery_token
        assert second_token is not None
        assert second_token.generation == first_token.generation + 1
        assert supervisor.current == second_token
        await service.stop()
        assert supervisor.unregister_calls == [first_token, second_token]

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


def test_hfr_177_transport_ready_wakes_requests_and_resumes_skipped_run(
    tmp_path: Path,
) -> None:
    async def _exercise() -> None:
        store = TaskExecutionStore(tmp_path / "reqs")
        workbench = store.enqueue_hook_send(session_key="avibe::project::proj_test", prompt="local")
        discord = store.enqueue_hook_send(session_key="discord::channel::C123", prompt="remote")
        ready_platforms = {"avibe"}
        notifications: list[tuple[tuple[RuntimeWorkLane, ...], bool]] = []
        controller = SimpleNamespace(
            platform_settings_managers={},
            is_im_transport_ready=lambda platform: platform in ready_platforms,
            runtime_work_supervisor=SimpleNamespace(
                notify=lambda *lanes, reset_cursor=False: notifications.append(
                    (lanes, reset_cursor)
                )
            ),
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
        assert notifications == [((RuntimeWorkLane.REQUESTS,), True)]
        await service._drain_requests()
        await asyncio.sleep(0)

        assert started == [workbench.id, discord.id]
        assert store.list_pending() == []

    asyncio.run(_exercise())


def test_drain_serializes_executions_per_session(tmp_path: Path) -> None:
    """HFR-003: an unstarted same-session request stays queued until its turn."""

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
            session_key=ParsedSessionKey(platform="slack", scope_type="channel", scope_id="C123"),
            agent_backend="",
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


def test_execute_request_forwards_request_metadata_into_the_context(
    monkeypatch,
    tmp_path,
) -> None:
    """A composed hook/watch/webhook/escalation prompt carries its user-authored part
    in the request metadata, and ``_build_context`` is the only reader — so
    ``_execute_request`` has to hand it over, exactly as the ``agent_run`` path already
    does. Without the forward the IM echo has no instruction to show and stays silent
    for every enqueued composed request (Codex P2)."""
    session_id = _make_avibe_session(
        monkeypatch,
        tmp_path,
        platform="slack",
        scope_type="channel",
        scope_native_id="C123",
    )
    contexts: list = []

    async def _submit_scheduled(sid, ctx, text, *, delivery_intent="steer"):
        contexts.append(ctx)

    settings_manager = SimpleNamespace(get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_k: None))
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        get_im_client_for_context=lambda _context: SimpleNamespace(
            should_use_thread_for_reply=lambda: True,
            should_use_thread_for_dm_session=lambda: False,
        ),
        session_turn_gate=SimpleNamespace(submit_scheduled=_submit_scheduled, in_flight={}),
        message_handler=SimpleNamespace(),
    )
    service = ScheduledTaskService(
        controller=controller, store=ScheduledTaskStore(Path("/tmp/nonexistent-scheduled.json"))
    )

    error = asyncio.run(
        service._execute_request(
            session_key="slack::channel::C123",
            post_to=None,
            deliver_key=None,
            prompt="check the deploy\n\nwaiter said: token=ghp_SECRET",
            execution_id="exec-metadata-1",
            trigger_kind="watch",
            session_id=session_id,
            metadata={"harness_display_prompt": "check the deploy"},
        )
    )

    assert error is None
    assert contexts[0].platform_specific["harness_display_prompt"] == "check the deploy"


def test_claimed_request_hands_its_metadata_to_the_execution(monkeypatch, tmp_path: Path) -> None:
    """The enqueued-request lane is where hook / watch / webhook / escalation runs are
    executed, so the request's own metadata must reach ``_execute_request`` there."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    request_store = TaskExecutionStore()
    request = request_store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="check the deploy\n\nwaiter said: rows=42",
        metadata={"harness_display_prompt": "check the deploy"},
    )
    calls: list[dict[str, Any]] = []
    service = ScheduledTaskService(
        controller=SimpleNamespace(),
        store=ScheduledTaskStore(),
        request_store=request_store,
    )
    claimed = request_store.claim(request.id)
    assert claimed is not None

    async def _execute_request(**kwargs):
        calls.append(kwargs)
        return None

    service._execute_request = _execute_request  # type: ignore[method-assign]
    asyncio.run(service._execute_claimed_request(claimed))

    assert len(calls) == 1
    assert calls[0]["metadata"] == {"harness_display_prompt": "check the deploy"}


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
    from core.session_turns import (
        SessionTurnManager,
        emit_matches_active_turn,
    )
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
        assert "_turn_lifecycle_admission" not in (
            context.platform_specific or {}
        )
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
        from core.memory_adapter import DisabledMemoryAdapter
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
        self.memory_adapter = DisabledMemoryAdapter()
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
        asyncio.run(_fire_and_finish_scheduled_task(service, task.id))
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

    asyncio.run(_fire_and_finish_scheduled_task(service, task.id))
    retry_error = store.get_task(task.id).last_error
    assert retry_copy in retry_error
    assert "sesdoesnotexist" in retry_error
    assert f"vibe task update {task.id} --session-id <id>" in retry_error

    asyncio.run(_fire_and_finish_scheduled_task(service, task.id))
    asyncio.run(_fire_and_finish_scheduled_task(service, task.id))
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
        asyncio.run(_fire_and_finish_scheduled_task(service, task.id))

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

    asyncio.run(_fire_and_finish_scheduled_task(service, task.id))

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
        asyncio.run(_fire_and_finish_scheduled_task(service, task.id))

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
        asyncio.run(_fire_and_finish_scheduled_task(service, task.id))

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
        asyncio.run(_fire_and_finish_scheduled_task(service, task.id))

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

    asyncio.run(_fire_and_finish_scheduled_task(service, task.id))

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
    queued = service.request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="test-one-shot",
    )
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
    assert run["error"] == i18n_t(_TASK_RESULT_NOT_RECORDED_I18N_KEY, "en"), (
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
        expected_run_at=task.run_at,
        expected_timezone=task.timezone,
        expected_job_id="test-one-shot",
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None

    asyncio.run(service._execute_claimed_request(claimed))

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["error"] == i18n_t(_TASK_RESULT_NOT_RECORDED_I18N_KEY, "en")
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


# --- Command tasks: a definition that runs a subprocess instead of an Agent turn ---


def _command_task_env(tmp_path: Path, monkeypatch) -> Path:
    """``_binding_env`` plus a test-owned fallback spawn cwd.

    A command definition with no ``cwd`` spawns in ``paths.get_vibe_remote_dir()``,
    which is the REAL ``~/.avibe`` in an unpatched process. Redirected here so no
    command test can spawn a child inside the user's live product state.
    """

    db_path = _binding_env(tmp_path, monkeypatch)
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / "avibe_home")
    return db_path


def _fire_command_task(
    service: ScheduledTaskService, task: ScheduledTask
) -> dict[str, Any]:
    """Fire one definition through the REAL claimed-request path; return its run row."""

    queued = service.request_store.enqueue_task_run(
        task.id,
        source_kind="scheduler",
        task=task,
        expected_run_at=task.run_at if task.schedule_type == "at" else None,
        expected_timezone=task.timezone if task.schedule_type == "at" else None,
        expected_job_id=(f"test:{task.id}" if task.schedule_type == "at" else None),
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))
    run = service.request_store.get_run(queued.id)
    assert run is not None
    return run


def _add_command_task(
    store: ScheduledTaskStore,
    *,
    shell_command: str,
    cwd: Optional[str],
    schedule_type: str = "cron",
    timeout_seconds: Optional[float] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> ScheduledTask:
    return store.add_task(
        session_key="",
        prompt="",
        schedule_type=schedule_type,
        cron="0 * * * *" if schedule_type == "cron" else None,
        run_at="2026-07-28T09:00:00+00:00" if schedule_type == "at" else None,
        timezone_name="UTC",
        cwd=cwd,
        shell_command=shell_command,
        timeout_seconds=timeout_seconds,
        metadata=dict(metadata or {"origin": "cli"}),
    )


def test_command_task_fire_records_a_successful_run_and_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    """A zero-exit command run is a SUCCEEDED run with its output on the row.

    The whole point of a command task: the outcome the user reads is the exit code
    and the captured output, not an Agent reply. Driven through the real
    claimed-request path so the assertions are on ``agent_runs`` -- what
    ``vibe task runs`` and the Harness detail pane actually show.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="echo hi; exit 0", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded", f"a zero-exit command run failed: {run['error']!r}"
    assert run["error"] in (None, "")
    assert run["exit_code"] == 0, f"the run row lost the exit code: {run['exit_code']!r}"
    assert "hi" in (run["stdout"] or ""), f"stdout was not persisted: {run['stdout']!r}"
    assert run["run_type"] == "scheduled", (
        f"a command fire changed the run type to {run['run_type']!r}; the CLI and Harness "
        "filter scheduled fires on it"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code == 0, (
        f"the definition did not record the exit code: {stored.last_exit_code!r}"
    )
    assert stored.last_error is None
    assert stored.last_run_at is not None


def test_command_task_failure_records_the_exit_code_and_stderr(
    tmp_path: Path, monkeypatch
) -> None:
    """A nonzero exit fails the run and keeps the cause where a reader can find it."""

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(
        store, shell_command="echo boom >&2; exit 7", cwd=str(tmp_path)
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", "a command that exited 7 was recorded as a success"
    assert "status 7" in (run["error"] or ""), (
        f"the run error does not name the exit status: {run['error']!r}"
    )
    assert "boom" in (run["error"] or ""), (
        "the last stderr line -- the only hint the list view shows -- was dropped from "
        f"the error text: {run['error']!r}"
    )
    assert "boom" in (run["stderr"] or ""), f"stderr was not persisted: {run['stderr']!r}"
    assert run["exit_code"] == 7

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code == 7
    assert "status 7" in (stored.last_error or "")


def test_command_failure_text_is_written_in_the_configured_language(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-022 -- the outcome a command fire generates is user-visible copy.

    ``last_error`` is rendered verbatim inside the failure notice, ``vibe task list``
    and the Workbench, all of which are otherwise translated. Generating it in English
    put an English sentence in the middle of a Chinese notice.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    service.controller.config = SimpleNamespace(language="zh")

    exited = _fire_command_task(
        service,
        _add_command_task(store, shell_command="echo boom >&2; exit 7", cwd=str(tmp_path)),
    )
    assert "命令退出" in (exited["error"] or ""), (
        f"a Chinese install got an English command outcome: {exited['error']!r}"
    )
    assert "boom" in (exited["error"] or ""), "the stderr detail was lost in translation"

    missing = tmp_path / "gone"
    no_spawn = _fire_command_task(
        service, _add_command_task(store, shell_command="true", cwd=str(missing))
    )
    assert "工作目录不存在" in (no_spawn["error"] or ""), (
        f"the no-spawn outcome stayed English: {no_spawn['error']!r}"
    )
    assert str(missing) in (no_spawn["error"] or ""), "the translation dropped the path"


def test_command_task_timeout_fails_the_run_with_the_timeout_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    """A command that outlives its timeout is killed and reported as a timeout.

    ``124`` is the runner's timeout code, and it must reach both the run row and the
    definition: a wedged command that merely stopped being awaited would leave the
    run ``running`` forever with nothing naming the cause.

    SCT-024 rides along on the same fire, because both halves are about the sentence
    the user reads. ``--timeout`` is a FLOAT, so the limit is stated as the user wrote
    it: ``int()`` was not rounding a sub-second limit, it was deleting it, and "timed
    out after 0 second(s)" describes an impossible event while hiding the setting that
    has to change. And the definition records that the SCHEDULER is why the command
    stopped, which nothing else can say -- ``timeout 5 ...`` inside a ``--shell``
    script exits 124 all by itself.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(
        store, shell_command="sleep 30", cwd=str(tmp_path), timeout_seconds=0.5
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed"
    assert run["exit_code"] == 124, (
        f"the timeout exit code did not reach the run row: {run['exit_code']!r}"
    )
    assert "timed out" in (run["error"] or ""), (
        f"the error text does not say the command timed out: {run['error']!r}"
    )
    assert "0.5 second" in (run["error"] or ""), (
        "the fractional limit was truncated away, so the text names a limit that was "
        f"never configured: {run['error']!r}"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code == 124
    assert (stored.metadata or {}).get(COMMAND_TIMED_OUT_METADATA_KEY) is True, (
        "the definition did not record that the scheduler is what stopped this fire, "
        f"so 124 is all the row has to go on: {stored.metadata!r}"
    )

    # And a real 124 from the command itself POSITIVELY clears the claim, rather than
    # merely failing to make it: the same definition, the next fire.
    _edit_definition_command(task.id, "exit 124")
    reread = ScheduledTaskStore().get_task(task.id)
    assert reread is not None
    _fire_command_task(service, reread)

    settled = ScheduledTaskStore().get_task(task.id)
    assert settled is not None and settled.last_exit_code == 124
    assert (settled.metadata or {}).get(COMMAND_TIMED_OUT_METADATA_KEY) is False, (
        "a command that chose its own 124 still reads as a scheduler timeout: "
        f"{settled.metadata!r}"
    )
    assert (
        definition_lifecycle_detail(
            lifecycle_state="finished",
            last_run_at=settled.last_run_at,
            last_exit_code=settled.last_exit_code,
            last_error=settled.last_error,
            timed_out=(settled.metadata or {}).get(COMMAND_TIMED_OUT_METADATA_KEY),
        )
        == "error"
    ), "the UI would tell the user to raise a limit that was never reached"


def test_command_task_with_a_missing_cwd_fails_without_spawning(
    tmp_path: Path, monkeypatch
) -> None:
    """A configured working directory that is gone must be named, not guessed at.

    Spawning anyway raises a bare ``NotADirectoryError`` from deep inside asyncio
    whose text never mentions the directory the user configured -- and there is no
    exit code to report, because nothing ran.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    missing = tmp_path / "gone"
    task = _add_command_task(store, shell_command="echo hi", cwd=str(missing))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    spawned: list[dict[str, Any]] = []

    async def _never(**kwargs):
        spawned.append(kwargs)
        raise AssertionError("a command was spawned into a directory that does not exist")

    monkeypatch.setattr(scheduled_tasks, "run_supervised_command", _never)

    run = _fire_command_task(service, task)

    assert spawned == []
    assert run["status"] == "failed"
    assert str(missing) in (run["error"] or ""), (
        f"the error does not name the missing directory: {run['error']!r}"
    )
    assert run["exit_code"] is None, (
        f"a run that never spawned reported an exit code: {run['exit_code']!r}"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code is None
    assert str(missing) in (stored.last_error or "")


def test_command_task_reports_a_supervisor_startup_failure_verbatim(
    tmp_path: Path, monkeypatch
) -> None:
    """``SupervisedCommandStartupError`` must be caught BEFORE the broad handler.

    It subclasses ``RuntimeError``, so a single ``except Exception`` would swallow it
    and report the worker's own startup detail as an ordinary command error --
    losing ``exc.detail``, the only text that says what the worker rejected.
    """

    from core.command_runner import SupervisedCommandStartupError

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="echo hi", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    async def _startup_failure(**_kwargs):
        raise SupervisedCommandStartupError("bad spec")

    monkeypatch.setattr(scheduled_tasks, "run_supervised_command", _startup_failure)

    run = _fire_command_task(service, task)

    assert run["status"] == "failed"
    assert run["error"] == "bad spec", (
        f"the supervisor's own startup detail was rewritten: {run['error']!r}"
    )
    assert run["exit_code"] is None

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_error == "bad spec"


def test_a_command_whose_executable_is_missing_reads_as_a_sentence(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-025 -- the worker's machine-readable error line is not user-facing text.

    ``SupervisedCommandStartupError`` is raised ONLY when the stdin handshake breaks,
    so the ordinary failure -- an ``argv`` whose executable does not exist -- takes the
    other route entirely: the worker accepts the spec, fails to spawn, and exits 1
    with ``AVIBE_WATCH_WORKER_ERROR:{...}`` on stderr. The watch lane decodes that; the
    scheduled lane did not, so the raw JSON became the definition's ``last_error``, the
    text of the failure notice, and the body of the Agent escalation prompt.

    Driven through the REAL worker, because the wire format is the thing under test:
    a hand-written stderr string would keep passing if the encoder changed.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        cwd=str(tmp_path),
        command=[str(tmp_path / "no-such-executable"), "--now"],
        metadata={"origin": "cli"},
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the spawn failed ({run['error']!r})"
    for field, value in (
        ("error", run["error"]),
        ("stderr", run["stderr"]),
    ):
        assert WATCH_WORKER_ERROR_PREFIX not in (value or ""), (
            f"the run's {field} is raw worker protocol JSON, not a sentence: {value!r}"
        )
    assert "supervisor failed" in (run["error"] or "").lower(), (
        f"the decoded text does not say what went wrong: {run['error']!r}"
    )
    assert "no-such-executable" in (run["error"] or ""), (
        f"the decoded text dropped the detail naming the cause: {run['error']!r}"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert WATCH_WORKER_ERROR_PREFIX not in (stored.last_error or ""), (
        f"the definition stored the raw protocol line: {stored.last_error!r}"
    )


def test_a_supervisor_failure_records_no_command_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-039 -- the supervisor's exit status is not the command's.

    SCT-025 decoded the worker's error LINE and left its exit STATUS misattributed.
    ``core/watch_worker.py``'s ``main()`` returns 1 for both of its own failures, so a
    spawn the worker could not perform arrived as ``exit_code == 1`` and was published as
    a fact about the user's command: ``Exit code: 1`` in the failure notice and the
    escalation prompt, ``last_exit_code = 1`` on the definition, an ``exit_code`` in the
    run row ``vibe runs show`` prints -- for a command that never ran, and next to
    "Command exited with status 1" contradicting the supervisor sentence beside it.

    The negative half is the point of the discriminator: a command that DID run and
    exited 1 must keep saying so, or this fix would blind every failing cron job.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        cwd=str(tmp_path),
        command=[str(tmp_path / "no-such-executable"), "--now"],
        metadata={"origin": "cli"},
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the spawn failed ({run['error']!r})"
    assert run["exit_code"] is None, (
        "the run row published the SUPERVISOR's status as the command's exit code "
        f"({run['exit_code']!r}); the command never started"
    )
    assert "supervisor failed" in (run["error"] or "").lower(), (
        f"the failure stopped naming the supervisor: {run['error']!r}"
    )
    assert "exited with status" not in (run["error"] or "").lower(), (
        "one message claims the command exited AND that the supervisor failed: "
        f"{run['error']!r}"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code is None, (
        "the definition's lifecycle claims a command exited: "
        f"last_exit_code={stored.last_exit_code!r}"
    )

    # The negative half, through the same real worker: a command that ran and failed.
    ran = _add_command_task(
        store, shell_command="exit 3", cwd=str(tmp_path), timeout_seconds=30.0
    )
    ran_run = _fire_command_task(service, ran)
    assert ran_run["status"] == "failed"
    assert ran_run["exit_code"] == 3, (
        "a real command's exit status was blanked along with the supervisor's: "
        f"{ran_run['exit_code']!r}"
    )
    assert "3" in (ran_run["error"] or ""), (
        f"the failure no longer reports the status the command exited with: {ran_run['error']!r}"
    )


def test_command_run_is_not_gated_on_im_transport_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    """A command run needs no IM transport, so a down adapter must not hold it queued.

    The regression guard is the second half: the SAME setup on a MESSAGE definition
    must still be gated, because that fire really does owe a reply to a platform.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    metadata = {"origin": "cli", "session_scope_id": "slack::channel::C123"}
    command_task = _add_command_task(
        store, shell_command="echo hi", cwd=str(tmp_path), metadata=metadata
    )
    message_task = store.add_task(
        session_key="",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        metadata=dict(metadata),
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    service.controller.is_im_transport_ready = lambda _platform: False

    command_request = TaskExecutionRequest(
        id="run-command",
        request_type="scheduled",
        task_id=command_task.id,
        metadata=dict(metadata),
    )
    message_request = TaskExecutionRequest(
        id="run-message",
        request_type="scheduled",
        task_id=message_task.id,
        metadata=dict(metadata),
    )

    assert service._transport_ready_for_request(command_request) is True, (
        "a command run was held queued behind an IM adapter it never needs"
    )
    assert service._transport_ready_for_request(message_request) is False, (
        "the short-circuit leaked to message tasks; a reply-owing fire must stay gated "
        "until its platform can deliver"
    )


def test_one_shot_command_task_is_disabled_after_it_fires(
    tmp_path: Path, monkeypatch
) -> None:
    """An ``at`` command definition must not be able to fire twice."""

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(
        store, shell_command="echo hi", cwd=str(tmp_path), schedule_type="at"
    )
    assert task.enabled is True
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded"
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.enabled is False, "a one-shot command definition stayed enabled"


def test_command_task_never_dispatches_an_agent_turn(tmp_path: Path, monkeypatch) -> None:
    """A command definition must run its subprocess INSTEAD of prompting an Agent.

    Not a stylistic check: dispatching as well would post an unasked-for reply into
    the bound conversation and bill an Agent turn for every scheduled backup.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="echo hi", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    dispatched: list[dict[str, Any]] = []

    async def _spy(**kwargs):
        dispatched.append(kwargs)
        return TaskDispatchResult(error=None)

    service._execute_request = _spy  # type: ignore[method-assign]

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded"
    assert dispatched == [], (
        f"a command task also dispatched an Agent turn: {dispatched!r}"
    )


def test_mark_task_result_stamps_an_exit_code_only_when_one_is_given(
    tmp_path: Path, monkeypatch
) -> None:
    """``exit_code=None`` means "this fire had none", never "clear the stored one".

    A message fire of a definition that also stores a command must not blank the last
    exit code a command fire recorded.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="echo hi", cwd=str(tmp_path))

    assert store.mark_task_result(task.id, error="boom", exit_code=7) is True
    assert ScheduledTaskStore().get_task(task.id).last_exit_code == 7

    assert store.mark_task_result(task.id, error=None) is True
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code == 7, (
        f"a result with no exit code cleared the stored one: {stored.last_exit_code!r}"
    )
    assert stored.last_error is None

    assert store.mark_task_result(task.id, error=None, exit_code=0) is True
    assert ScheduledTaskStore().get_task(task.id).last_exit_code == 0


def test_canceling_a_command_fire_propagates_and_stamps_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """Cancellation must escape the command path un-swallowed.

    ``_execute_claimed_request``'s own ``except asyncio.CancelledError`` is what
    settles the run row as an interruption, so absorbing the cancellation here would
    record a service stop as an ordinary failed command AND leave a stale
    ``last_error`` on the definition for a fire whose outcome is unknown.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(
        store, shell_command="sleep 30", cwd=str(tmp_path), timeout_seconds=0.0
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    stamped: list[dict[str, Any]] = []
    monkeypatch.setattr(
        store,
        "mark_task_result",
        lambda *args, **kwargs: stamped.append(kwargs) or True,
    )

    real_runner = scheduled_tasks.run_supervised_command
    entered = asyncio.Event()

    async def _tracking_runner(**kwargs):
        entered.set()
        return await real_runner(**kwargs)

    monkeypatch.setattr(scheduled_tasks, "run_supervised_command", _tracking_runner)

    # A real claimed run, because the execution id is not decoration: the fire records
    # its worker onto that ``running`` row before it will run the user's command
    # (SCT-037), and a fabricated id would be refused at the spawn -- leaving this test
    # cancelling a fire that had already failed.
    request = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert request is not None

    async def _exercise() -> None:
        fire = asyncio.ensure_future(
            service._execute_command_task(
                task, execution_id=request.id, disable_one_shot=False
            )
        )
        # Cancel only once the child is genuinely being awaited: a cancel that landed
        # before the spawn would prove nothing about the running path.
        await asyncio.wait_for(entered.wait(), timeout=3)
        await asyncio.sleep(0.2)
        fire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fire

    asyncio.run(_exercise())

    assert entered.is_set(), "the command never started, so nothing was cancelled"
    assert stamped == [], (
        f"a cancelled command fire stamped a terminal result anyway: {stamped!r}"
    )


# --- ``--on-failure agent``: the escalation turn a failed command fire owes -------
#
# A command definition never prompts an Agent, so a failure it should ACT on has to
# queue that turn itself. The turn is what the result stamp AUTHORISES, so the two are
# ONE transaction (HFR-269, ``core/watches.py``: "TWO COMMITS ARE NOT ONE DECISION").
# Split in two, a teardown between the commits queues an escalation under a definition
# that no longer authorises it, and an exception between them disables a failed one-shot
# while LOSING the report that explains it.


def _escalation_command_task(
    store: ScheduledTaskStore,
    tmp_path: Path,
    *,
    shell_command: str,
    schedule_type: str = "cron",
    session_id: Optional[str] = None,
    prompt: str = "",
    agent_name: Optional[str] = None,
) -> ScheduledTask:
    """A command definition configured with ``--on-failure agent`` and a binding.

    The CLI guarantees such a definition has a session policy and a binding, because
    the escalation is a real Agent turn that has to land in a real conversation.
    """

    return store.add_task(
        session_key="",
        session_id=session_id,
        session_policy="existing" if session_id else None,
        agent_name=agent_name,
        prompt=prompt,
        schedule_type=schedule_type,
        cron="0 * * * *" if schedule_type == "cron" else None,
        run_at="2026-07-28T09:00:00+00:00" if schedule_type == "at" else None,
        timezone_name="UTC",
        cwd=str(tmp_path),
        deliver_key="slack::channel::C123",
        shell_command=shell_command,
        metadata={"origin": "cli", "on_failure": "agent"},
    )


def _queued_runs(store: ScheduledTaskStore) -> list[dict[str, Any]]:
    assert store._sqlite is not None
    return [run for run in store._sqlite.list_runs() if run["status"] == "queued"]


def _escalation_runs(store: ScheduledTaskStore) -> list[dict[str, Any]]:
    return [run for run in _queued_runs(store) if run["run_type"] == "task_escalation"]


def _escalation_run_payload(task: ScheduledTask, *, run_id: str = "esc-1") -> dict[str, Any]:
    """The ``agent_runs`` outbox payload an escalation stamp would carry."""

    return {
        "id": run_id,
        "request_type": "task_escalation",
        "task_id": task.id,
        "session_key": "",
        "session_id": task.session_id,
        "prompt": "the report",
        "message": "the report",
        "source_kind": "scheduler",
        "parent_run_id": "parent-run",
        "status": "queued",
        "created_at": "2026-07-28T09:00:00+00:00",
        "updated_at": "2026-07-28T09:00:00+00:00",
    }


def test_the_atomic_task_stamp_and_its_queued_run_both_land(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-269 control — with nothing racing it, ONE transaction carries both halves.

    A combined write that simply refused everything would satisfy the refusal test
    below vacuously ("no run row, definition untouched" is exactly what a method that
    never commits produces), so the positive half needs its own test: the definition's
    transition is readable AND the outbox row is durable, from one call.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    assert store.sqlite_backend is not None, (
        "this test needs the SQLite backend; a shared transaction is the whole fix"
    )
    task = _escalation_command_task(store, tmp_path, shell_command="exit 7")
    expect = store._read_state(task)
    task.last_error = "command exited with status 7"
    task.last_exit_code = 7

    landed = store.sqlite_backend.upsert_scheduled_task_with_queued_run(
        task.to_dict(), expect=expect, run_payload=_escalation_run_payload(task)
    )

    assert landed is True
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.last_exit_code == 7, (
        f"the definition's transition did not commit: {stored and stored.last_exit_code!r}"
    )
    runs = _escalation_runs(store)
    assert len(runs) == 1, f"expected exactly one queued escalation, got {runs!r}"
    assert runs[0]["id"] == "esc-1" and runs[0]["task_id"] == task.id


def test_a_refused_atomic_task_stamp_writes_neither_half(
    tmp_path: Path, monkeypatch
) -> None:
    """A refused compare-and-set must leave NO run row and an untouched definition.

    The inverse of the control above, and the reason the two are one transaction: an
    escalation queued against a definition whose stamp was refused is a durable turn
    nothing authorises.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    assert store.sqlite_backend is not None
    task = _escalation_command_task(store, tmp_path, shell_command="exit 7")
    before = _stored_definition_row(task.id)
    # A STALE expectation: the payload claims it was read from a paused definition,
    # which is what a teardown committed after the read would have left behind.
    stale = scheduled_tasks.DefinitionWriteExpectation.from_read(
        session_id=task.session_id,
        enabled=False,
        deleted_at=None,
        metadata=task.metadata,
    )
    task.last_error = "command exited with status 7"

    landed = store.sqlite_backend.upsert_scheduled_task_with_queued_run(
        task.to_dict(), expect=stale, run_payload=_escalation_run_payload(task)
    )

    assert landed is False, "a stale expectation must be refused"
    assert _escalation_runs(store) == [], (
        "the refused stamp still queued its escalation; the two are not one transaction"
    )
    assert _stored_definition_row(task.id)["last_error"] == before["last_error"], (
        "a refused compare-and-set partially landed"
    )


def test_mark_task_result_stamps_and_enqueues_in_one_call(
    tmp_path: Path, monkeypatch
) -> None:
    """``queued_run`` rides the stamp: one call, one transaction, both effects."""

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _escalation_command_task(store, tmp_path, shell_command="exit 7")

    stamped = store.mark_task_result(
        task.id,
        error="command exited with status 7",
        exit_code=7,
        queued_run=_escalation_run_payload(task),
    )

    assert stamped is True
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.last_exit_code == 7
    assert [run["id"] for run in _escalation_runs(store)] == ["esc-1"]


def test_a_refused_mark_task_result_queues_no_run(tmp_path: Path, monkeypatch) -> None:
    """The refusal path: an unmatched binding expectation writes neither half."""

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _escalation_command_task(store, tmp_path, shell_command="exit 7")

    stamped = store.mark_task_result(
        task.id,
        error="command exited with status 7",
        exit_code=7,
        # The binding this fire started against, as the caller remembers it — and it
        # is not what the definition says, so the stamp must refuse.
        expected_binding=("some-other-session", "", "cron"),
        queued_run=_escalation_run_payload(task),
    )

    assert stamped is False
    assert _escalation_runs(store) == []
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.last_exit_code is None, (
        "a refused stamp recorded the exit code anyway"
    )


def test_an_agent_renamed_during_the_command_still_gets_the_escalation(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-030 -- the queued turn must name an Agent the catalog still has.

    The escalation payload is composed from the definition as the fire CLAIMED it,
    which for a command task is however long the command ran. A rename committed in
    that window rewrites the definition and every ACTIVE run, but this run does not
    exist yet, so it escaped the rewrite and was inserted naming an Agent nobody can
    resolve: the turn carrying the failure report could never be claimed, while the
    notice its own stamp suppressed was already gone -- neither report, which is the
    one outcome the two-report invariant exists to rule out.

    The narrower race (a rename the mirror has NOT yet seen) needs no fix: the payload
    then carries the pre-rename binding revision and the compare-and-set refuses both
    halves, leaving the fire to the notice ladder exactly like the reclaim in SCT-004.
    What survives that guard is the rename the mirror ABSORBED -- and it absorbs one
    whenever ``PRAGMA data_version`` reports another connection's commit, which during
    a command long enough to matter is the normal case, not the exotic one. The reload
    is forced here because that probe is timing-dependent and the defect is not.
    """

    _command_task_env(tmp_path, monkeypatch)
    from core.vibe_agents import VibeAgentStore

    agent_store = VibeAgentStore(paths.get_sqlite_state_path())
    try:
        # A user Agent, because a built-in one cannot be renamed at all.
        agent_store.create(name="ops", backend="claude")
    finally:
        agent_store.close()

    store = ScheduledTaskStore()
    task = _escalation_command_task(
        store, tmp_path, shell_command="exit 7", agent_name="ops"
    )
    assert task.agent_name == "ops"
    # What ``build_hook_send`` puts on the payload: the name, resolved by the executor
    # before the command started. There is no ``agent_id`` to carry -- ``run_definitions``
    # stores only the spelling.
    run_payload = _escalation_run_payload(task)
    run_payload["agent_name"] = task.agent_name

    agent_store = VibeAgentStore(paths.get_sqlite_state_path())
    try:
        renamed = agent_store.rename("ops", "night-shift")
    finally:
        agent_store.close()
    # The executor's mirror catching up mid-command, which is what makes the stamp's
    # expectation current and the pre-composed run row the only stale thing left.
    store.load()
    assert store.get_task(task.id).agent_name == "night-shift"

    stamped = store.mark_task_result(
        task.id,
        error="command exited with status 7",
        exit_code=7,
        records_command_outcome=True,
        queued_run=run_payload,
    )

    assert stamped is True, "the rename was absorbed by the reload, so the stamp must land"
    runs = _escalation_runs(store)
    assert len(runs) == 1, f"expected exactly one queued escalation, got {runs!r}"
    queued = runs[0]
    assert queued["agent_name"] == "night-shift", (
        "the escalation was queued against the pre-rename name, which resolves to no "
        f"Agent at claim time: {queued['agent_name']!r}"
    )
    assert queued["agent_id"] == renamed.id, (
        "the escalation was not pinned to the Agent's durable identity, so the NEXT "
        f"rename loses it again: {queued['agent_id']!r}"
    )


def test_an_archived_agent_keeps_the_escalations_own_spelling(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-030, negative half -- a REMOVED Agent must not be papered over.

    Resolution failing is not always a rename. If the user archived or deleted the
    Agent the definition names, there is no identity to pin and inventing one would
    bind the report to a different Agent than the definition asks for. The row keeps
    the definition's own spelling and lets the claim report the truth.
    """

    _command_task_env(tmp_path, monkeypatch)
    from core.vibe_agents import VibeAgentStore

    agent_store = VibeAgentStore(paths.get_sqlite_state_path())
    try:
        agent_store.create(name="ops", backend="claude")
    finally:
        agent_store.close()

    store = ScheduledTaskStore()
    task = _escalation_command_task(
        store, tmp_path, shell_command="exit 7", agent_name="ops"
    )
    run_payload = _escalation_run_payload(task)
    run_payload["agent_name"] = task.agent_name
    _delete_agent_row(paths.get_sqlite_state_path(), "ops")

    stamped = store.mark_task_result(
        task.id,
        error="command exited with status 7",
        exit_code=7,
        records_command_outcome=True,
        queued_run=run_payload,
    )

    assert stamped is True
    runs = _escalation_runs(store)
    assert len(runs) == 1
    assert runs[0]["agent_name"] == "ops", (
        f"the definition's own spelling was dropped: {runs[0]['agent_name']!r}"
    )
    assert not (runs[0]["agent_id"] or ""), (
        f"an identity was invented for an Agent that no longer exists: {runs[0]['agent_id']!r}"
    )


def _delete_agent_row(db_path: Path, name: str) -> None:
    """Remove an Agent from the catalog outright, as a delete leaves it."""

    from storage.models import agents as agents_table

    with create_sqlite_engine(db_path).begin() as conn:
        conn.execute(agents_table.delete().where(agents_table.c.name == name))


def test_a_file_backed_task_store_refuses_a_queued_run(tmp_path: Path) -> None:
    """The watch store's answer, replicated: REFUSE rather than save-then-enqueue.

    A file-backed store cannot make the two writes one decision at all, so a
    best-effort second write would be exactly the two-commits bug HFR-269 removed.
    Passing one here is a caller bug and says so.
    """

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    assert store.sqlite_backend is None
    task = store.add_task(
        session_key="",
        prompt="",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        shell_command="exit 7",
        metadata={"on_failure": "agent"},
    )

    with pytest.raises(ValueError):
        store.mark_task_result(
            task.id, error="boom", queued_run=_escalation_run_payload(task)
        )


def test_a_failed_on_failure_agent_command_fire_queues_exactly_one_escalation(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-003 — a failed ``--on-failure agent`` fire reports through ONE Agent turn.

    The whole point of the mode: instead of a notification the user has to act on, the
    failure becomes a turn the Agent can act on. So all four halves are asserted from a
    REAL fire through the claimed-request path:

    * the parent run is ``failed`` and carries the ``escalation_run_id`` marker,
    * exactly one queued ``task_escalation`` row exists, pointing back at the fire and
      at the definition,
    * its prompt carries the command and the outcome the Agent has to act on, and
    * the parent owes NO failure notice -- the escalation IS the report, and both would
      be the same failure told twice.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_escalation")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo boom >&2; exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the fire failed ({run['error']!r})"
    escalations = _escalation_runs(store)
    assert len(escalations) == 1, (
        f"expected exactly one queued escalation for one failed fire: {escalations!r}"
    )
    escalation = escalations[0]
    assert run["metadata"].get("escalation_run_id") == escalation["id"], (
        "the failed run does not point at the turn that reports it: "
        f"{run['metadata'].get('escalation_run_id')!r} != {escalation['id']!r}"
    )
    assert escalation["parent_run_id"] == run["id"], (
        f"the escalation lost its parent fire: {escalation['parent_run_id']!r}"
    )
    assert escalation["task_id"] == task.id, (
        f"the escalation lost its definition: {escalation['task_id']!r}"
    )
    assert escalation["session_id"] == session_id
    assert "echo boom >&2; exit 7" in (escalation["prompt"] or ""), (
        f"the escalation must name the command that failed: {escalation['prompt']!r}"
    )
    assert "status 7" in (escalation["prompt"] or ""), (
        f"and the outcome it failed with: {escalation['prompt']!r}"
    )
    assert store._sqlite.owed_failure_notice(run["id"]) is None, (
        "an escalated failure also owes a notice; the same failure would be reported "
        "twice, once as a turn and once as an alert"
    )


def test_archiving_the_session_gives_a_killed_escalation_its_notice_back(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-005 -- teardown must not turn an escalated failure into a silent one.

    A failed ``--on-failure agent`` fire suppresses its own failure notice because
    the escalation turn IS the report. Session teardown then cancels every queued run
    bound to that session -- the escalation among them -- and a ``canceled`` run owes
    nothing. So the failure ended up with NO turn and NO notice, which is the one
    direction this design refuses: the accepted crash window biases towards BOTH,
    never towards neither.

    The failure must fall back to the notice ladder, which does not need the
    torn-down session: a notice is delivered to the scope, not into a conversation.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_archive_esc")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the fire failed ({run['error']!r})"
    escalations = _escalation_runs(store)
    assert len(escalations) == 1, f"the premise: one escalation is queued ({escalations!r})"
    assert store._sqlite.owed_failure_notice(run["id"]) is None, (
        "the premise: the escalation suppressed the notice"
    )

    from storage.workbench_sessions_service import archive_session

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            archive_session(conn, session_id)
    finally:
        engine.dispose()

    assert _escalation_runs(store) == [], (
        "the premise: archiving the session cancels the queued escalation"
    )
    notice = store._sqlite.owed_failure_notice(run["id"])
    assert notice is not None, (
        "the escalation was canceled and no notice replaced it, so this failure is "
        "reported by nothing at all"
    )
    assert notice["state"] == "pending"


def _start_the_escalation(escalation_id: str) -> None:
    """Move an escalation to ``running``, the way the executor does when it claims it."""

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == escalation_id)
                .values(status="running", started_at="2026-07-28T09:00:01+00:00")
            )
    finally:
        engine.dispose()


def test_archiving_the_session_gives_an_already_running_escalation_its_notice_back(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-009 -- an escalation teardown kills MID-TURN owes the same fallback.

    Teardown treats the two halves of "not yet terminal" differently: it flags all four
    active statuses ``cancel_requested`` and lets the executor honour it, but only
    ``pending``/``queued`` are terminalized on the spot. So an escalation the executor
    had already claimed is not canceled by this transaction -- it is canceled a moment
    later, out here, by the executor.

    That is the same silence as the queued case and it is harder to see. The re-arm ran
    only for ``pending``/``queued``, so an escalation cancelled at ``running`` settled
    ``canceled`` (owing nothing, as a cancel does) while the parent's notice stayed
    suppressed by ``escalation_run_id`` on the promise of a turn that was killed before
    it reported anything. What the re-arm must key on is "teardown has condemned this
    escalation", which is exactly the set teardown cancel-requests -- not the narrower
    set it happens to terminalize in the same statement.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_running_esc")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the fire failed ({run['error']!r})"
    escalations = _escalation_runs(store)
    assert len(escalations) == 1, f"the premise: one escalation is queued ({escalations!r})"
    assert store._sqlite.owed_failure_notice(run["id"]) is None, (
        "the premise: the escalation suppressed the notice"
    )

    # The window: the executor claims the turn, then teardown lands.
    _start_the_escalation(escalations[0]["id"])

    from storage.workbench_sessions_service import archive_session

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            archive_session(conn, session_id)
        with engine.begin() as conn:
            condemned = (
                conn.execute(
                    select(agent_runs.c.cancel_requested).where(
                        agent_runs.c.id == escalations[0]["id"]
                    )
                )
                .scalars()
                .first()
            )
    finally:
        engine.dispose()

    assert condemned, (
        "the premise: teardown condemns a running escalation too, it just leaves the "
        "terminalizing to the executor"
    )
    notice = store._sqlite.owed_failure_notice(run["id"])
    assert notice is not None, (
        "the turn was killed before it could report anything and no notice replaced "
        "it, so this failure is reported by nothing at all"
    )
    assert notice["state"] == "pending"


def test_hard_deleting_the_session_gives_a_killed_escalation_its_notice_back(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-005, other half -- ``/new`` owes the same fallback, and owes it EARLIER.

    ``_delete_agent_session_rows`` splits on retained history: a Session with Messages
    or Deliveries is archived and re-anchored (and cancels its queued runs on the way),
    while an EMPTY one is deleted outright. The empty branch is the one that bites,
    because it is both reachable and quieter: a Session created for a command task has
    no Messages until a turn actually delivers, so the first thing that ever happens in
    it can be a queued escalation -- and deleting the row leaves that escalation queued
    against a Session that no longer exists. It is not cancelled, so no cancel-shaped
    guard can see it; it simply never runs, and the notice it suppressed never came.

    So the re-arm belongs BEFORE the branch, not inside the archival half: the failure
    must fall back to the notice ladder whichever way the row goes.
    """

    from storage.models import agent_sessions
    from storage.session_reclaim import RECLAIM_PAUSE
    from storage.sessions_service import _delete_agent_session_rows

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_delete_esc")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the fire failed ({run['error']!r})"
    assert len(_escalation_runs(store)) == 1, "the premise: one escalation is queued"
    assert store._sqlite.owed_failure_notice(run["id"]) is None, (
        "the premise: the escalation suppressed the notice"
    )

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            deleted = _delete_agent_session_rows(
                conn,
                select(agent_sessions.c.id).where(agent_sessions.c.id == session_id),
                reclaim_mode=RECLAIM_PAUSE,
                reclaim_reason="new_session",
            )
    finally:
        engine.dispose()

    assert deleted == 1, "the premise: the empty Session row was torn down"
    notice = store._sqlite.owed_failure_notice(run["id"])
    assert notice is not None, (
        "the Session the escalation was queued against is gone, so the turn can never "
        "run and no notice replaced it: this failure is reported by nothing at all"
    )
    assert notice["state"] == "pending"


def test_hard_deleting_the_session_also_cancels_the_runs_bound_to_it(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-010 -- a deleted Session must not leave claimable runs behind it.

    ``agent_runs.session_id`` carries no foreign key, so deleting the Session row does
    not touch the runs bound to it: they stay ``queued``, and the executor will happily
    claim one against a Session that no longer exists. The archival half of this same
    function cancels them; the delete half only stopped mattering less because the row
    it removes hid the problem.

    For a command-task escalation the cost is precise. The re-arm above has already
    handed the failure back to the notice ladder on the grounds that the turn can never
    run -- so if the turn is nonetheless claimed and dies on the missing Session, that
    death reports the SAME failure a second time, from the lane meant to replace it.
    Terminalizing here is what makes the re-arm's premise true.
    """

    from storage.models import agent_sessions
    from storage.session_reclaim import RECLAIM_PAUSE
    from storage.sessions_service import _delete_agent_session_rows

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_delete_orphan")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed", f"the premise: the fire failed ({run['error']!r})"
    escalations = _escalation_runs(store)
    assert len(escalations) == 1, f"the premise: one escalation is queued ({escalations!r})"

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            deleted = _delete_agent_session_rows(
                conn,
                select(agent_sessions.c.id).where(agent_sessions.c.id == session_id),
                reclaim_mode=RECLAIM_PAUSE,
                reclaim_reason="new_session",
            )
        with engine.begin() as conn:
            settled = (
                conn.execute(
                    select(agent_runs.c.status, agent_runs.c.cancel_requested).where(
                        agent_runs.c.id == escalations[0]["id"]
                    )
                )
                .mappings()
                .first()
            )
    finally:
        engine.dispose()

    assert deleted == 1, "the premise: the empty Session row was torn down"
    assert settled is not None, "the escalation row itself should survive as history"
    assert settled["status"] == "canceled", (
        "the escalation is still claimable against a Session that no longer exists: "
        f"{settled['status']!r}"
    )
    assert settled["cancel_requested"], (
        "and nothing recorded that the teardown is what ended it"
    )


def _edit_definition_command(definition_id: str, shell_command: str) -> None:
    """Commit a command edit from another connection, the way ``vibe task update`` does."""

    from storage.models import run_definitions

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            conn.execute(
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .values(shell_command=shell_command)
            )
    finally:
        engine.dispose()


def _repoint_definition_session(definition_id: str, session_id: str) -> None:
    """Commit a binding change from another connection, the way ``/new`` does."""

    from storage.models import run_definitions

    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            conn.execute(
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .values(session_id=session_id)
            )
    finally:
        engine.dispose()


def test_a_reclaim_during_the_command_refuses_the_stamp_and_escalates_nowhere(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-004 -- a binding that moved mid-fire must not receive the escalation.

    ``mark_task_result`` reloads the mirror and derives its compare-and-set
    expectation FROM THE RELOADED ROW, so a ``/new`` reclaim that commits while the
    command is running becomes the expectation the stamp compares against -- it
    matches, and the stamp lands. That was survivable while the stamp only wrote
    bookkeeping. It is not survivable now that the same stamp QUEUES AN AGENT TURN:
    the escalation is composed from the pre-execution task object, so it would be
    durably queued against the session the reclaim just tore down, and the notice
    would be suppressed in favour of a turn that can never be delivered there.

    The fire must instead be refused whole: no escalation row, and the failure
    reported on the run so the owed-notice ladder still owns it.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_reclaim_before")
    reclaimed_session_id = _bare_session_row(
        workdir=tmp_path, anchor="avibe_task_reclaim_after"
    )
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    real_runner = scheduled_tasks.run_supervised_command

    async def _reclaim_then_fail(**kwargs):
        # The reclaim commits WHILE the command runs, which is the only ordering that
        # reaches the window: before the fire there is nothing to stamp, and after the
        # stamp the transaction has already closed.
        result = await real_runner(**kwargs)
        _repoint_definition_session(task.id, reclaimed_session_id)
        return result

    monkeypatch.setattr(scheduled_tasks, "run_supervised_command", _reclaim_then_fail)

    run = _fire_command_task(service, task)

    assert _escalation_runs(store) == [], (
        "an escalation was queued against a binding the definition no longer has"
    )
    assert run["status"] == "failed", (
        f"a fire whose stamp was refused must not report success: {run['status']!r}"
    )
    assert run["metadata"].get("escalation_run_id") in (None, ""), (
        "the run claims an escalation that was never queued, which suppresses the "
        f"notice too: {run['metadata'].get('escalation_run_id')!r}"
    )


def test_a_cancel_during_the_command_refuses_the_escalation_and_settles_canceled(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-026 -- a stopped job must not answer by starting an Agent.

    The reclaim twin above guards the BINDING; this guards the STOP, and the window is
    narrower than the one SCT-013 covers. There the flag is observed by the tick, which
    cancels the awaiting coroutine and the fire never reaches its stamp. Here the
    command has already exited, so nothing is left to interrupt: ``mark_task_result``
    stamps the failure and durably queues the escalation, and only afterwards does
    ``complete`` read ``cancel_requested`` and settle the run ``canceled``.

    Two commits are not one decision. The turn the stamp authorises is durable by then,
    so the user stops a job and the job answers by starting an Agent. The stop has to be
    re-read SERVER-SIDE inside the stamp's own transaction, which is the only place it
    can still roll the escalation back with it.

    The definition must still end up describing this fire rather than the one before it
    -- that is the terminal projection's job, and a refused stamp is exactly when it is
    needed.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_cancel_stamp")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo boom >&2; exit 7",
        session_id=session_id,
    )
    # The premise for the projection assertion: a PREVIOUS fire left an outcome behind.
    store.mark_task_result(task.id, error=None, exit_code=3, records_command_outcome=True)

    service = _scheduled_service_with_ledger(tmp_path, store, [])
    queued = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    real_runner = scheduled_tasks.run_supervised_command

    async def _stop_it_as_it_exits(**kwargs):
        result = await real_runner(**kwargs)
        # The only ordering that reaches the window: after the command is done, so the
        # tick has nothing left to interrupt, and before the stamp opens its transaction.
        service.request_store.cancel_run(queued.id)
        return result

    monkeypatch.setattr(scheduled_tasks, "run_supervised_command", _stop_it_as_it_exits)

    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert run["status"] == "canceled", (
        f"the premise: the stop won the settlement race ({run['status']!r})"
    )
    assert _escalation_runs(store) == [], (
        "the user stopped this run and it answered by durably queueing an Agent turn"
    )
    assert run["metadata"].get("escalation_run_id") in (None, ""), (
        "the run claims an escalation that was rolled back with the stamp: "
        f"{run['metadata'].get('escalation_run_id')!r}"
    )

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    # 7, not the 3 the previous fire left: the stamp was refused, so the terminal
    # projection is what describes this fire -- and the fire did exit 7, which the run
    # row carries. The refusal rolls back the ESCALATION, not the record of the outcome.
    assert stored.last_exit_code == 7, (
        "the refused stamp left the definition reporting the exit code of the fire "
        f"BEFORE this one: {stored.last_exit_code!r}"
    )
    assert (stored.metadata or {}).get(COMMAND_TIMED_OUT_METADATA_KEY) is False, (
        "the projection left the timeout claim of an earlier fire standing beside a "
        f"code this one chose for itself: {stored.metadata!r}"
    )


def test_a_stop_that_lands_after_the_stamp_retracts_the_escalation(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-033 -- a compare-and-set can only refuse the stops it can already see.

    SCT-026 closes the window where the cancel is visible when the stamp commits: the
    transaction re-reads it server-side and rolls the escalation back with the stamp.
    One step further along the same coroutine the guard has already run. ``cancel_run``
    on a running fire writes only a flag, so the flag can land AFTER
    ``mark_task_result`` returns -- and ``settle_run_terminal`` then re-reads it and
    normalizes this fire to ``canceled`` while the escalation it just committed sits
    queued and claimable. The user stops a job and the job answers by starting an Agent,
    which is SCT-026's defect surviving one lane later.

    Retraction rather than a wider transaction: the enqueue must commit with the STAMP
    (that is what authorises the turn), and this settlement happens afterwards in
    another lane, so no single transaction holds both without reopening HFR-269.

    NEITHER report is owed. ``canceled`` is written only for a stop the user asked for,
    and a stopped fire owes no notice -- SCT-027's rule. Alerting instead would report a
    failure for a job they had just stopped. The definition still describes the failure,
    because this stamp -- unlike SCT-026's -- landed.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_late_stop")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo boom >&2; exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    queued = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    real_mark = service.store.mark_task_result

    def _stop_it_after_the_stamp(*args, **kwargs):
        stamped = real_mark(*args, **kwargs)
        # THE window: the guarded transaction has committed, so the escalation is
        # durable and no compare-and-set is left to consult.
        service.request_store.cancel_run(queued.id)
        return stamped

    monkeypatch.setattr(service.store, "mark_task_result", _stop_it_after_the_stamp)

    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert run["status"] == "canceled", (
        f"the premise: the stop won the settlement race ({run['status']!r})"
    )
    assert run["metadata"].get("escalation_run_id"), (
        "the premise: this stamp LANDED, so the run names the turn it authorised"
    )

    escalation_id = str(run["metadata"]["escalation_run_id"])
    escalation = service.request_store.get_run(escalation_id)
    assert escalation is not None, "the escalation row vanished rather than settling"
    assert escalation["status"] == "canceled", (
        "the user stopped this command and the Agent turn it queued is still claimable: "
        f"{escalation['status']!r}"
    )
    assert _escalation_runs(store) == [], (
        "a queued escalation survived the stop of the fire that queued it"
    )

    # The failure itself is not lost: this stamp landed, unlike SCT-026's.
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.last_exit_code == 7


def test_claiming_the_escalation_of_a_stopped_fire_is_refused_at_the_door(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-034 -- retraction is too late once the dispatch loop has claimed the turn.

    SCT-033 takes the escalation back from whoever settles the fire ``canceled``, and
    that works for exactly as long as the row is still queued. The dispatch loop runs on
    the same loop as the fire's own coroutine, and the escalation becomes claimable the
    moment the stamp commits -- well before this settlement reaches it. A claim is the
    one transition retraction cannot undo: ``cancel_run`` on a running Agent Run writes
    a flag, and the turn lane does not watch that flag, so the stopped job would launch
    an Agent and run it to completion.

    So the CLAIM answers the question, from the parent row, inside the transaction that
    would otherwise start the work -- and then it no longer matters which of the two
    lanes gets there first. Keyed on the fire's ``cancel_requested``, which is set the
    instant the user asks, rather than on any status a claim would have to wait for.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_claimed_stop")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo boom >&2; exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    queued = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    real_mark = service.store.mark_task_result
    claim_attempt: dict[str, Any] = {}

    def _stop_it_then_claim_the_escalation(*args, **kwargs):
        stamped = real_mark(*args, **kwargs)
        if claim_attempt:
            # The terminal projection stamps the definition again on its way out; only
            # the FIRST stamp is the one that queued the escalation.
            return stamped
        # THE ordering: the escalation is durable, the user's stop lands, and the
        # dispatch loop claims the escalation before the fire's settlement gets to it.
        service.request_store.cancel_run(queued.id)
        escalation = service.request_store.find_escalation_run(parent_run_id=queued.id)
        assert escalation is not None, "the premise: the stamp queued an escalation"
        claim_attempt["id"] = str(escalation["id"])
        claim_attempt["claimed"] = service.request_store.claim(claim_attempt["id"])
        return stamped

    monkeypatch.setattr(
        service.store, "mark_task_result", _stop_it_then_claim_the_escalation
    )

    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert run["status"] == "canceled", (
        f"the premise: the stop won the settlement race ({run['status']!r})"
    )
    assert claim_attempt.get("claimed") is None, (
        "the dispatch loop was handed the escalation of a fire the user had already "
        f"stopped: {claim_attempt.get('claimed')!r}"
    )

    escalation = service.request_store.get_run(str(claim_attempt["id"]))
    assert escalation is not None, "the escalation row vanished rather than settling"
    assert escalation["status"] == "canceled", (
        "the refused claim left the escalation of a stopped fire non-terminal, so a "
        f"later pass can start it: {escalation['status']!r}"
    )
    assert _escalation_runs(store) == [], (
        "a claimable escalation survived the stop of the fire that queued it"
    )

    # The failure is still recorded on the definition; only the Agent turn is withdrawn.
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.last_exit_code == 7


def test_teardown_between_the_stamp_and_the_settle_cancels_the_fire_itself(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-027 -- why the settle needs no second check on the escalation it names.

    SCT-005 and SCT-009 both let the parent settle FIRST, so
    ``rearm_notices_for_escalations_canceled_with_session`` finds a ``failed`` row and
    can hand it a notice. The window in between looks like it breaks that: teardown
    lands after the atomic stamp+enqueue and before ``complete``, so the re-arm sees a
    parent that is still ``running`` and owes nothing yet, and ``complete`` then writes
    ``escalation_run_id`` -- apparently suppressing a notice in favour of a turn that
    was cancelled a moment earlier, leaving the failure reported by nothing.

    IT DOES NOT, and this test is the reason a status check inside the settle would be
    dead code. The teardown that cancels the escalation cannot cancel only the
    escalation: ``archive_session`` flags EVERY non-terminal run of that session, and
    this fire is one of them. So the settle normalizes the parent to ``canceled``, and
    ``canceled`` owes no notice by design -- the run is recorded as stopped, not as a
    failure whose report went missing. Nothing is suppressed because nothing was owed.
    """

    from storage.workbench_sessions_service import archive_session

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_stamp_teardown")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    real_mark = store.mark_task_result

    def _stamp_then_tear_down(*args, **kwargs):
        stamped = real_mark(*args, **kwargs)
        engine = create_sqlite_engine(paths.get_sqlite_state_path())
        try:
            with engine.begin() as conn:
                archive_session(conn, session_id)
        finally:
            engine.dispose()
        return stamped

    monkeypatch.setattr(store, "mark_task_result", _stamp_then_tear_down)

    run = _fire_command_task(service, task)

    assert run["status"] == "canceled", (
        "the teardown that cancelled the escalation left this fire settling as a "
        f"failure, so the settle really can suppress a notice for a dead turn: {run['status']!r}"
    )
    assert _escalation_runs(store) == [], (
        "the premise: teardown cancelled the escalation this fire queued"
    )
    assert store._sqlite.owed_failure_notice(run["id"]) is None, (
        "a cancelled fire stamped a failure notice, which is the noise "
        "``_owed_failure_notice_for_transition`` exists to refuse"
    )


def test_the_escalation_prompt_prepends_the_stored_message_and_names_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    """The user's standing instruction first, then the machine-generated evidence.

    ``vibe task add --shell ... --on-failure agent --message "open a ticket"`` stores
    that message as the definition's prompt: it is the instruction, and the report is
    what it operates on. The report also has to name the run, or the Agent cannot look
    up the full output the one-line tails were cut from.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="echo boom >&2; exit 7",
        prompt="Open a ticket if the nightly sync breaks.",
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    prompt = service._escalation_prompt(
        task,
        run_id="run-abc123",
        error="command exited with status 7: boom",
        exit_code=7,
        stdout="starting\nsyncing\n",
        stderr="warning: slow\nboom\n",
    )

    assert prompt.startswith("Open a ticket if the nightly sync breaks."), (
        f"the stored instruction must come first: {prompt!r}"
    )
    assert "echo boom >&2; exit 7" in prompt, f"the command must be named: {prompt!r}"
    assert "command exited with status 7: boom" in prompt
    assert "Exit code: 7" in prompt
    assert "boom" in prompt.split("Last stderr line:")[1].splitlines()[0], (
        f"the stderr tail is where the cause usually is: {prompt!r}"
    )
    assert "syncing" in prompt.split("Last stdout line:")[1].splitlines()[0], (
        f"the stdout tail was dropped: {prompt!r}"
    )
    assert "run-abc123" in prompt and "vibe runs show run-abc123" in prompt, (
        f"the report must say how to read the full output: {prompt!r}"
    )


def test_a_stored_cwd_beats_the_bound_sessions_workdir(tmp_path: Path, monkeypatch) -> None:
    """SCT-050 -- an explicit ``--cwd`` has to actually win, or accepting the flag means nothing.

    The bound Session's workdir is read LIVE at fire time, by design, so a command with
    no directory of its own follows a Session that moves. That is the behaviour the
    flag exists to opt out of: a scheduled job should not relocate because someone ran
    ``vibe session update --cwd`` on an unrelated conversation. Both directories exist
    here and hold different files, so the assertion cannot pass by falling back.
    """

    _command_task_env(tmp_path, monkeypatch)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "marker").write_text("session-workdir\n", encoding="utf-8")
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    (pinned / "marker").write_text("pinned-workdir\n", encoding="utf-8")

    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=session_dir, anchor="avibe_task_pinned_cwd")
    task = store.add_task(
        session_key="",
        session_id=session_id,
        session_policy="existing",
        prompt="",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        cwd=str(pinned),
        deliver_key="slack::channel::C123",
        shell_command="cat marker",
        metadata={"origin": "cli", "on_failure": "agent"},
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded", run["error"]
    assert "pinned-workdir" in (run["stdout"] or ""), (
        "the definition's own cwd lost to the Session it only escalates into, so "
        f"--cwd cannot pin a scheduled command at all: {run['stdout']!r}"
    )


def test_an_escalating_command_runs_in_its_bound_sessions_workdir(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-008 -- the relative commands the docs promise have to resolve somewhere.

    A definition bound to an existing Session stores ``cwd=None`` unless its command
    was given one: ``--cwd`` is still refused for a MESSAGE task, on the rule that the
    Session owns its working directory (`cwd_with_existing_session`), and for a message
    task that is complete -- the Agent turn starts in the Session's workdir. A command
    task has no Agent turn to inherit it from, so the stored ``None`` fell through to
    the ``~/.avibe`` fallback, meaning ``--shell './scripts/sync.sh'``, the form the
    docs use, ran from the product state directory.

    A command task can now name its own directory
    (``test_task_add_escalating_command_task_accepts_an_explicit_cwd``). This is the
    other half: what happens when it does not. The binding supplies the answer, and
    goes on supplying it, so omitting the flag keeps the behaviour every task created
    before it relies on.

    The lookup opens a store per FIRE, and each one carries an engine and an
    invalidation probe, so the second assertion is that it does not outlive the read:
    a cron command running every minute would otherwise leak a connection a minute for
    the life of the service.
    """

    import storage.sessions_service as sessions_service

    _command_task_env(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / "marker").write_text("bound-session-workdir\n", encoding="utf-8")

    opened: list[str] = []
    closed: list[str] = []

    class _CountedSessionsService(sessions_service.SQLiteSessionsService):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(str(id(self)))

        def close(self) -> None:
            closed.append(str(id(self)))
            super().close()

    monkeypatch.setattr(
        sessions_service, "SQLiteSessionsService", _CountedSessionsService
    )

    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=project, anchor="avibe_task_cwd")
    task = store.add_task(
        session_key="",
        session_id=session_id,
        # The exact shape ``vibe task add --shell ... --on-failure agent
        # --session-id ...`` stores: an existing binding, and NO cwd.
        session_policy="existing",
        prompt="",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        cwd=None,
        deliver_key="slack::channel::C123",
        shell_command="cat marker",
        metadata={"origin": "cli", "on_failure": "agent"},
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded", (
        "the command could not read a file sitting in its own Session's workdir, so it "
        f"ran somewhere else: {run['error']!r}"
    )
    assert _escalation_runs(store) == [], "a succeeded fire must not escalate"
    assert opened, "the premise: the fire resolved its workdir through a Session store"
    assert closed == opened, (
        "the workdir lookup left its Session store open, so every fire of this cron "
        f"leaks an engine and its invalidation probe: opened {opened!r}, closed {closed!r}"
    )


def test_a_command_with_nothing_to_inherit_runs_where_agent_work_runs(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-048 -- the product state directory is the wrong last resort.

    SCT-008 gave a Session-bound command its binding's workdir. A definition can still
    reach the fire with neither: a per-run binding whose Session does not exist yet
    (created before the CLI recorded the invocation directory for it), or one written
    straight through the API. Those fell through to ``paths.get_vibe_remote_dir()`` --
    ``~/.avibe`` -- so a relative command ran against persisted product state, where its
    files are missing and its writes land among the store's.

    ``runtime.default_cwd`` is where this install's Agent turns already run, so it is
    both a real directory and the same answer the escalation Session would get.
    """

    _command_task_env(tmp_path, monkeypatch)
    work = tmp_path / "agent-work"
    work.mkdir()
    (work / "marker").write_text("runtime-default-workdir\n", encoding="utf-8")

    store = ScheduledTaskStore()
    task = store.add_task(
        session_key="",
        # The shape ``--create-session-per-run`` stores: no cwd, and no Session to read
        # one from until escalation creates it.
        session_policy="create_per_run",
        prompt="",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        cwd=None,
        shell_command="cat marker",
        metadata={"origin": "cli", "on_failure": "agent"},
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    service.controller.config = SimpleNamespace(
        language="en", runtime=SimpleNamespace(default_cwd=str(work))
    )

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded", (
        "the command could not read a file sitting in the configured runtime workdir, "
        f"so it ran in the product state directory instead: {run['error']!r}"
    )
    assert "runtime-default-workdir" in (run["stdout"] or "")


def test_the_run_snapshot_names_the_command_the_fire_actually_ran(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-028 -- the snapshot is only worth having if it is the executed copy.

    The enqueue stamps the definition as it stood THEN, but the executor re-reads the
    definition after claiming the run -- so an edit committed in that window left the
    run naming command A while command B ran, and the notice, the escalation prompt and
    the Workbench run detail all repeated that wrong answer with a snapshot's authority.
    Worse than no snapshot: it is the record readers were told to trust precisely
    because the definition is mutable.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(
        store, shell_command="echo original", cwd=str(tmp_path)
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    queued = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    predicted = service.request_store.get_run(queued.id)
    assert predicted is not None
    assert predicted["metadata"].get(COMMAND_SNAPSHOT_METADATA_KEY) == {
        "shell": "echo original",
        "argv": [],
    }, f"the premise: the enqueue predicted a command ({predicted['metadata']!r})"

    # ``SqliteInvalidationProbe`` reports "changed" only by COMPARING two readings of
    # ``PRAGMA data_version``, so its very first call always answers no. A live service
    # has ticked long before a fire claims anything; a test has to prime it, or the
    # mirror below never reloads and the executor cannot see the edit at all.
    service.store.maybe_reload()

    # The window: the user rewrites the command after it was queued and before it runs.
    _edit_definition_command(task.id, "echo edited")

    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    run = service.request_store.get_run(queued.id)
    assert run is not None
    assert "edited" in (run["stdout"] or ""), (
        f"the premise: the executor ran the edited command ({run['stdout']!r})"
    )
    assert run["metadata"].get(COMMAND_SNAPSHOT_METADATA_KEY) == {
        "shell": "echo edited",
        "argv": [],
    }, (
        "the run's immutable record of what it executed names a command it did not "
        f"execute: {run['metadata'].get(COMMAND_SNAPSHOT_METADATA_KEY)!r}"
    )


def test_a_running_command_fire_does_not_hold_its_conversations_turn_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-012 -- a six-hour backup command must not mute the conversation for six hours.

    The per-session lock exists to stop TWO AGENT TURNS running in one conversation at
    once. A command fire is not a turn: it talks to no Agent, writes no message, and
    holds no native session. But it is bound to a Session (``--on-failure agent`` needs
    one), so it inherited that Session's lock key -- and with ``--timeout`` defaulting
    to six hours, one long command left every queued turn in the conversation skipped as
    ``session_busy`` for as long as it ran.

    The escalation it may queue is the part that IS a turn, and it is a separate run
    that takes the lock in the ordinary way. So the fire keys on the definition instead:
    still serialized against itself, no longer against the user.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_lock")
    task = _escalation_command_task(
        store, tmp_path, shell_command="sleep 3600", session_id=session_id
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    fire = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert fire is not None
    fire_lock = service._execution_lock_key(fire)
    session_lock = service._canonical_session_lock(session_id, None)
    assert fire_lock == f"task:{task.id}", (
        "the command fire took a lock keyed on something other than its own definition"
    )
    assert fire_lock != session_lock, "the fire is holding the conversation's turn lock"

    # And the consequence, through the real drain: the escalation turn this very
    # definition would queue has to be dispatchable while the command is still running.
    service._inflight_sessions.add(fire_lock)
    escalation = service.request_store.enqueue_hook_send(
        session_key="",
        session_id=session_id,
        prompt="the report",
        run_type="task_escalation",
        definition_id=task.id,
        source_kind="scheduler",
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        service, "_spawn_execution", lambda request, lock_key: dispatched.append(request.id)
    )

    asyncio.run(service._drain_requests())

    assert dispatched == [escalation.id], (
        "the escalation was held behind the command fire: "
        f"{service.request_store.get_run(escalation.id)['metadata']}"
    )


def test_canceling_a_running_command_run_actually_stops_the_command(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-013 -- ``vibe runs cancel`` has to mean the work stops, not just the row.

    For every other run type cancellation reaches the work: a turn is interrupted, a
    queued claim is terminalized at the door. A command fire had NOTHING watching the
    flag, so ``cancel_requested`` was only read at settlement -- which for a command
    that hangs is up to ``--timeout`` (six hours by default) away. Until then the child
    kept running, holding whatever it was holding, and the row the user was shown said
    the run was already canceled.

    The runner already owns the kill: cancelling the awaiting coroutine tears down the
    process tree. What was missing was somebody to observe the flag and pull it, so the
    scheduler tick does -- the same ``_inflight_cancellation_causes`` + ``task.cancel()``
    pair service shutdown uses, only with the user named as the cause.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_cancel")
    started = tmp_path / "command-started"
    task = _escalation_command_task(
        store,
        tmp_path,
        # Announces itself, then hangs for far longer than this test may wait.
        shell_command=f"touch {started.name}; sleep 3600",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    async def _cancel_it_mid_flight() -> None:
        request = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert request is not None
        service._spawn_execution(request, service._execution_lock_key(request))
        execution = service._inflight_executions[request.id]
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "the command never started, so nothing was cancelled"

        service.request_store.cancel_run(request.id)
        service._propagate_requested_cancellations()
        # Ten seconds, against a command that sleeps for an hour: the point of the
        # assertion is that the wait ends because the command was killed.
        await asyncio.wait_for(
            asyncio.gather(execution, return_exceptions=True), timeout=10
        )

    try:
        asyncio.run(_cancel_it_mid_flight())
    except asyncio.TimeoutError:  # pragma: no cover - the defect this test reproduces
        pytest.fail("cancelling the run left the command running")

    run = next(
        row
        for row in store._sqlite.list_runs()
        if row["run_type"] not in {"task_escalation"}
    )
    assert run["status"] == "canceled", f"a cancelled fire settled as {run['status']!r}"
    assert _escalation_runs(store) == [], (
        "a fire the user cancelled escalated to an Agent as though the command had failed"
    )
    stored = ScheduledTaskStore().get_task(task.id)
    # The stop landed before the result stamp, which is what keeps the definition free
    # of a fabricated outcome: a command killed mid-flight has no exit status, and the
    # cancellation is recorded as the stop it was (``last_error``), not as a command that
    # reported one.
    assert stored is not None and stored.last_exit_code is None, (
        "a cancelled fire stamped an exit code the command never produced"
    )


def test_canceling_a_run_whose_task_was_removed_still_stops_the_command(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-020 -- removing the task must not make its running command unkillable.

    ``remove_task`` drops the definition immediately and leaves the in-flight Run
    alive, which is the ONLY way a user can react to a command that is misbehaving
    right now. Deciding command-ness by asking the live store therefore answered
    "not a command" exactly when the answer mattered, the cancellation was ignored,
    and the child ran on to its ``--timeout`` -- six hours by default. The run
    carries an immutable snapshot of what it launched; that is what decides.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    started = tmp_path / "orphan-started"
    task = _add_command_task(
        store,
        shell_command=f"touch {started.name}; sleep 3600",
        cwd=str(tmp_path),
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    async def _remove_then_cancel() -> None:
        request = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert request is not None
        service._spawn_execution(request, service._execution_lock_key(request))
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "the command never started, so nothing was cancelled"

        assert store.remove_task(task.id), "the definition was not removed"
        assert store.get_task(task.id) is None
        service.request_store.cancel_run(request.id)
        service._propagate_requested_cancellations()
        await asyncio.wait_for(
            asyncio.gather(service._inflight_executions[request.id], return_exceptions=True),
            timeout=10,
        )

    try:
        asyncio.run(_remove_then_cancel())
    except asyncio.TimeoutError:  # pragma: no cover - the defect this test reproduces
        pytest.fail("removing the task left its cancelled command running")


def test_a_crashed_service_reaps_the_command_worker_it_left_running(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-023 -- a service that dies without unwinding must not leak its command.

    Every ORDERLY stop reaches the child through the awaiting coroutine's
    ``CancelledError``. A SIGKILL or a crash delivers none: the isolated supervisor
    and the backup, deployment or migration under it keep running, unowned, and the
    next cron fire starts a SECOND one beside it. Nothing on disk named that child,
    so the restart could not have found it even in principle.

    The crash is simulated by abandoning the in-flight coroutine and constructing a
    fresh service over the same state, which is what a restart does; the assertion
    is that the abandoned fire then ends on its own, because its worker was killed.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    started = tmp_path / "worker-started"
    task = _add_command_task(
        store,
        shell_command=f"touch {started.name}; sleep 3600",
        cwd=str(tmp_path),
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    async def _crash_then_restart() -> None:
        request = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert request is not None
        service._spawn_execution(request, service._execution_lock_key(request))
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "the command never started, so nothing was orphaned"

        workers = service.request_store.list_running_command_workers()
        assert workers, "the fire recorded no worker, so a restart cannot find it"
        assert workers[0]["run_id"] == request.id

        # The controller invokes recovery only after backend/Turn recovery has
        # reconstructed exact owners. Command reaping remains the first step of
        # that recovery pass, before the row carrying its identity can settle.
        restarted = _scheduled_service_with_ledger(tmp_path, store, [])
        restarted.recover_processing_requests()

        await asyncio.wait_for(
            asyncio.gather(
                service._inflight_executions[request.id], return_exceptions=True
            ),
            timeout=15,
        )

    try:
        asyncio.run(_crash_then_restart())
    except asyncio.TimeoutError:  # pragma: no cover - the defect this test reproduces
        pytest.fail("the restart left the orphaned command worker running")


def test_a_worker_that_survives_the_reap_is_kept_for_the_next_start(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-029 -- an unconfirmed kill must not throw away the only handle on the child.

    SCT-023 records the worker's identity so a restart can find it. But the reap
    cleared that record unconditionally, INCLUDING when the teardown came back
    unconfirmed and the process was still there -- a task wedged in uninterruptible
    I/O outlives even ``SIGKILL``. Since the run is the only place the identity is
    written, and ``recover_processing`` settles the run moments later, no later pass
    could ever find that backup or migration again: it ran to completion unowned.

    So the record survives and the row stays ``running`` for the next start to retry.
    The cap is the other half: retrying forever would hold a run ``running`` for the
    life of the install whenever the process genuinely never exits.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    started = tmp_path / "unkillable-started"
    task = _add_command_task(
        store,
        shell_command=f"touch {started.name}; sleep 3600",
        cwd=str(tmp_path),
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    attempts: list[int] = []

    def _refuse_to_confirm(pid, *args, **kwargs):
        """A teardown that reports "not confirmed" while the process keeps running."""

        attempts.append(pid)
        return False

    def _worker_record() -> Optional[dict[str, Any]]:
        workers = service.request_store.list_running_command_workers()
        return workers[0]["identity"] if workers else None

    def _run_row(run_id: str) -> dict[str, Any]:
        row = service.request_store.get_run(run_id)
        assert row is not None
        return row

    async def _restart_until_it_gives_up() -> None:
        request = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert request is not None
        service._spawn_execution(request, service._execution_lock_key(request))
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "the command never started, so nothing was orphaned"
        assert _worker_record(), "the fire recorded no worker"

        real_terminate = process_isolation.terminate_process_tree_by_pid
        monkeypatch.setattr(
            process_isolation, "terminate_process_tree_by_pid", _refuse_to_confirm
        )
        cap = ScheduledTaskService._MAX_COMMAND_WORKER_REAP_ATTEMPTS
        # Every start before the cap: the kill is attempted, comes back unconfirmed,
        # and the record is kept with its attempt count so the NEXT one can try.
        for attempt in range(1, cap):
            restarted = _scheduled_service_with_ledger(tmp_path, store, [])
            restarted.recover_processing_requests()
            assert len(attempts) == attempt, (
                f"start {attempt} did not attempt the kill: {attempts!r}"
            )
            identity = _worker_record()
            assert identity is not None, (
                f"start {attempt} discarded a live worker's identity, so no later start "
                "can ever find it"
            )
            assert identity.get("reap_attempts") == attempt, (
                f"the attempt count did not advance: {identity!r}"
            )
            assert _run_row(request.id)["status"] == "running", (
                "the run was settled while its worker was still alive; "
                "``list_running_command_workers`` will not see it again"
            )

        # The cap. The process is still there and still unkillable, but the run stops
        # being held open on its behalf.
        restarted = _scheduled_service_with_ledger(tmp_path, store, [])
        restarted.recover_processing_requests()
        assert _worker_record() is None, "the reap never gave up; this run stays running forever"
        assert _run_row(request.id)["status"] != "running", (
            "the spent run was left ``running`` with nothing tracking it"
        )

        # Cleanup: the abandoned coroutine still owns the child, and its cancellation
        # is the orderly teardown every non-crash stop uses. Restored by name, never
        # ``monkeypatch.undo()`` -- that would also put back the real ``paths``, and a
        # teardown writing to the user's live ``~/.avibe`` is exactly what
        # ``_command_task_env`` exists to prevent.
        monkeypatch.setattr(
            process_isolation, "terminate_process_tree_by_pid", real_terminate
        )
        execution = service._inflight_executions[request.id]
        execution.cancel()
        await asyncio.wait_for(
            asyncio.gather(execution, return_exceptions=True), timeout=15
        )

    try:
        asyncio.run(_restart_until_it_gives_up())
    except asyncio.TimeoutError:  # pragma: no cover - cleanup only
        pytest.fail("the cancelled command was left running")


_SUPERVISOR_KILLED_MID_COMMAND = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
    "sys.stdout.write(f'{child.pid}\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(0.5)\n"
    "os._exit(0)\n"
)


def test_a_restart_reaps_the_group_a_dead_supervisor_left_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-032 -- an empty supervisor pid is not an empty tree.

    The worker is spawned into its own session, so it LEADS the process group
    ``pgid == pid``, and on POSIX that group outlives it. Kill the supervisor alone --
    the OOM killer picking the parent, a fault in the supervisor's own code -- and the
    backup or migration underneath keeps running in a group nothing else names.

    Reading the free pid as proof the tree was reaped is the whole defect: the reap
    stopped there, cleared ``command_worker``, and let ``recover_processing`` settle
    the row moments later, so the survivor ran to completion unowned and the next
    cron fire started a second one beside it. The watch lane already walked the
    surviving group; this asserts the command lane does too, on a real one.
    """

    if os.name == "nt":
        pytest.skip("a process group outliving its leader is POSIX-specific")

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="sleep 3600", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    request = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert request is not None

    marker = process_isolation.new_process_identity_marker()
    leader = subprocess.Popen(  # noqa: S603 - fixed argv, test-owned
        [os.path.abspath(sys.executable), "-c", _SUPERVISOR_KILLED_MID_COMMAND],
        stdout=subprocess.PIPE,
        text=True,
        env=process_isolation.process_identity_subprocess_env(marker),
        **process_isolation.isolated_subprocess_kwargs(),
    )
    child_pid: Optional[int] = None
    try:
        identity = process_isolation.capture_spawned_process_identity(leader.pid, marker)
        assert identity is not None
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().strip())
        assert leader.wait(timeout=15) == 0, "the supervisor did not die on its own"
        assert process_isolation.probe_process_liveness(leader.pid) == "gone"
        assert os.getpgid(child_pid) == leader.pid, (
            "the command did not inherit the supervisor's process group, so this is "
            "not the state the defect needs"
        )

        assert service.request_store.record_command_worker(
            request.id, process_isolation.serialize_process_identity(identity)
        )
        assert service.request_store.list_running_command_workers(), (
            "the fixture did not leave the run in the state the startup reap scans"
        )

        # Recovery reaps before settling the row that stores the identity.
        restarted = _scheduled_service_with_ledger(tmp_path, store, [])
        restarted.recover_processing_requests()

        deadline = 15.0
        while deadline > 0 and process_isolation.probe_process_liveness(child_pid) != "gone":
            time.sleep(0.1)
            deadline -= 0.1
        assert process_isolation.probe_process_liveness(child_pid) == "gone", (
            "the restart read the dead supervisor as a reaped tree and left the "
            "command running, unowned, with nothing on disk naming it"
        )
        assert not service.request_store.list_running_command_workers(), (
            "a proven death must retire the record; keeping it would pin the run "
            "``running`` and retry a kill on a pid that is free"
        )
        row = service.request_store.get_run(request.id)
        assert row is not None and row["status"] != "running"
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, process_isolation.KILL_SIGNAL)
        with suppress(Exception):
            leader.kill()
        with suppress(Exception):
            leader.wait(timeout=5)
        if leader.stdout is not None:
            leader.stdout.close()


def test_a_probe_that_cannot_read_the_process_keeps_its_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-031 -- "I could not look" is not "it is gone", and only gone retires a record.

    SCT-029 keeps the handle when the KILL comes back unconfirmed. The liveness
    question in front of that kill had the same two answers folded into one:
    ``inspect_process_identity`` returns ``None`` both for an empty pid and for a
    probe that could not read the process table at all -- an exhausted fd table, a
    ``/proc`` read losing a race, a platform refusing ``create_time``. Treating the
    second as absence skipped the kill AND cleared the record, so a backup still
    writing became unfindable by every later start too, and the next fire could run
    beside it.

    Reads as a restart, because that is the only caller: the reap runs from
    ``__init__``, before ``recover_processing`` settles the row the identity lives on.
    The cap applies here as well -- a probe that never recovers must not hold a run
    ``running`` for the life of the install.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="sleep 3600", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])
    request = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert request is not None

    # A worker record for a process that is never spawned: the probe is what is
    # under test, so the pid only has to survive ``process_identity_from_payload``.
    assert service.request_store.record_command_worker(
        request.id,
        {
            "pid": 424242,
            "create_time": 1.0,
            "worker_fingerprint": fingerprint_process_marker("orphaned-worker"),
        },
    )
    assert service.request_store.list_running_command_workers(), (
        "the fixture did not leave the run in the state the startup reap scans"
    )

    signalled: list[int] = []

    def _record_kill(pid, *_args, **_kwargs) -> bool:
        signalled.append(pid)
        return True

    monkeypatch.setattr(
        process_isolation, "probe_process_liveness", lambda _pid: "unknown"
    )
    monkeypatch.setattr(
        process_isolation, "terminate_process_tree_by_pid", _record_kill
    )

    def _worker_record() -> Optional[dict[str, Any]]:
        workers = service.request_store.list_running_command_workers()
        return workers[0]["identity"] if workers else None

    cap = ScheduledTaskService._MAX_COMMAND_WORKER_REAP_ATTEMPTS
    for attempt in range(1, cap):
        restarted = _scheduled_service_with_ledger(tmp_path, store, [])
        restarted.recover_processing_requests()
        identity = _worker_record()
        assert identity is not None, (
            f"start {attempt} read the process as gone because it could not read it at "
            "all, and threw away the only handle on it"
        )
        assert identity.get("reap_attempts") == attempt, (
            f"the attempt count did not advance: {identity!r}"
        )
        row = service.request_store.get_run(request.id)
        assert row is not None and row["status"] == "running", (
            "the run was settled while its worker was unaccounted for; "
            "``list_running_command_workers`` will not see it again"
        )

    assert signalled == [], (
        "the reap signalled a pid whose identity it could not read; that is the "
        "coin flip on an unrelated process the identity check exists to refuse"
    )

    # The cap. Still unreadable, but the run stops being held open on its behalf.
    restarted = _scheduled_service_with_ledger(tmp_path, store, [])
    restarted.recover_processing_requests()
    assert _worker_record() is None, "the reap never gave up; this run stays running forever"
    row = service.request_store.get_run(request.id)
    assert row is not None and row["status"] != "running", (
        "the spent run was left ``running`` with nothing tracking it"
    )


def test_a_command_whose_worker_cannot_be_recorded_is_never_started(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-037 -- the handshake is the one moment a nameless worker can be refused.

    Every other guard in this family protects a record that already exists. If the
    stamp itself does not land, the fire runs a backup or deployment with nothing on
    disk naming it, and a crash mid-command leaves the same unfindable orphan
    SCT-023/029/031/032 exist to prevent -- reached before the record was ever written.

    Refusing costs almost nothing HERE and only here: the supervisor is blocked reading
    its spec off stdin, so the user's command has not started, and the runner reaps the
    tree on the way out. So a captured identity that cannot be persisted fails the fire.

    The negative half is the same judgement from the other side: an identity that could
    not be CAPTURED does not refuse. Its dominant cause is a supervisor that already
    exited, where there is no process to name, and no amount of persisting makes an
    uncapturable identity trustworthy -- every kill path refuses a record it cannot
    vouch for. Turning that into a refusal would stop honest commands from running.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    ran = tmp_path / "command-ran"
    task = _add_command_task(
        store, shell_command=f"touch {ran.name}", cwd=str(tmp_path)
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    supervisors: list[int] = []
    real_capture = command_runner.capture_spawned_process_identity

    def _remember_the_supervisor(pid, marker):
        supervisors.append(pid)
        return real_capture(pid, marker)

    monkeypatch.setattr(
        command_runner, "capture_spawned_process_identity", _remember_the_supervisor
    )

    real_record = type(service.request_store).record_command_worker

    def _cannot_write(self, run_id, identity):
        if identity is not None:
            raise RuntimeError("database is locked")
        return real_record(self, run_id, identity)

    monkeypatch.setattr(
        type(service.request_store), "record_command_worker", _cannot_write
    )

    queued = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    assert not ran.exists(), (
        "the command ran with nothing on disk naming the process running it; a crash "
        "here leaves an orphan no later start can find"
    )
    run = service.request_store.get_run(queued.id)
    assert run is not None and run["status"] == "failed", (
        f"the refusal was not reported as a failed fire: {run and run['status']!r}"
    )
    assert "could not be recorded" in (run["error"] or ""), (
        f"the user is not told why the command did not run: {run and run['error']!r}"
    )
    assert supervisors, "the supervisor never spawned, so nothing was refused"
    for _ in range(100):
        if process_isolation.probe_process_liveness(supervisors[0]) == "gone":
            break
        time.sleep(0.05)
    assert process_isolation.probe_process_liveness(supervisors[0]) == "gone", (
        "the refusal left the supervisor running -- the very leak it exists to prevent"
    )

    # The negative half: nothing to name is not the same as failing to name it.
    monkeypatch.setattr(
        type(service.request_store), "record_command_worker", real_record
    )
    monkeypatch.setattr(
        command_runner, "capture_spawned_process_identity", lambda _pid, _marker: None
    )
    second = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    claimed = service.request_store.claim(second.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    assert ran.exists(), (
        "a command whose identity could not be captured was refused; the usual cause "
        "is a supervisor that already exited, and the honest ones must still run"
    )
    run = service.request_store.get_run(second.id)
    assert run is not None and run["status"] == "succeeded", (
        f"the uncaptured identity failed an otherwise fine fire: {run and run['status']!r}"
    )


def test_an_enumeration_that_fails_leaves_the_records_it_could_not_read(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-036 -- "I could not enumerate" is not "there is nothing to protect".

    SCT-031 fixed that category error one layer down, on a single process. The same
    mistake sat one layer up, on the whole set: the startup reap answered a failed
    ``list_running_command_workers`` with an empty skip set, and the ``recover_processing``
    on the very next line then settled EVERY ``running`` command fire -- because only
    ``running`` rows carry ``command_worker``, one momentary read failure permanently
    unnamed every live backup and deployment on the host, and the next fire ran beside
    them.

    So the settle pass no longer trusts a set handed to it. It reads the record off the
    row it is about to settle, inside the same transaction, and the reap communicates by
    WRITING that record: cleared once the process is proven dead or the retries are
    spent, left in place on every maybe -- including the maybe where it never looked.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="sleep 3600", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    def _claimed_fire() -> str:
        request = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert request is not None
        return request.id

    # Two interrupted fires, differing only in whether a worker record survived: one
    # whose process is unaccounted for, and one the last life already proved dead.
    with_worker = _claimed_fire()
    without_worker = _claimed_fire()
    assert service.request_store.record_command_worker(
        with_worker,
        {
            "pid": 424242,
            "create_time": 1.0,
            "worker_fingerprint": fingerprint_process_marker("orphaned-worker"),
        },
    )

    real_list = type(service.request_store).list_running_command_workers

    def _cannot_read(_self):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        type(service.request_store), "list_running_command_workers", _cannot_read
    )

    # The recovery pass, with enumeration failing exactly as a momentary lock would.
    restarted = _scheduled_service_with_ledger(tmp_path, store, [])
    restarted.recover_processing_requests()

    monkeypatch.setattr(
        type(service.request_store), "list_running_command_workers", real_list
    )

    workers = {
        worker["run_id"]: worker["identity"]
        for worker in service.request_store.list_running_command_workers()
    }
    assert with_worker in workers, (
        "a start that could not enumerate the workers settled the run anyway, and the "
        "identity lives on that row -- no later start can find that process again"
    )
    assert workers[with_worker].get("reap_attempts") is None, (
        "no kill was attempted, so nothing may be charged against the attempt cap"
    )
    row = service.request_store.get_run(with_worker)
    assert row is not None and row["status"] == "running", (
        "the row must stay ``running`` for the next start's enumeration to see it"
    )

    # The other half: preservation keys on the surviving record, not on the run type,
    # so a fire with no worker to protect is settled like any other interrupted run.
    row = service.request_store.get_run(without_worker)
    assert row is not None and row["status"] != "running", (
        "a fire carrying no worker record was held open by a read failure that had "
        "nothing to do with it"
    )


def test_a_definition_does_not_fire_while_its_own_worker_may_be_alive(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-040 -- single flight per definition must survive the restart that breaks it.

    ``_execution_lock_key`` keys a command fire on its definition "so a fire is still
    serialized against itself", but ``self._inflight_sessions`` is memory: the rule
    lapses at exactly the moment a survivor is most likely. The service dies, the
    supervisor's tree keeps running, the startup reap RETAINS its record because it
    could not prove it dead -- and then the next tick of the same cron starts a second
    copy of that backup over the same dataset.

    So a command fire consults the durable record too, by identity, and only for its
    own definition: another definition's live worker is not its business. It looks and
    never signals -- the retained record exists so a later start can kill that tree, and
    a fire that reaped it to make room would destroy the work it was scheduled to
    protect. Proof of death releases the definition here rather than a restart later.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    second_copy = tmp_path / "second-copy-ran"
    other_ran = tmp_path / "other-definition-ran"
    task = _add_command_task(
        store, shell_command=f"touch {second_copy}", cwd=str(tmp_path)
    )
    other = _add_command_task(store, shell_command=f"touch {other_ran}", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    async def _drain_and_wait() -> None:
        await service._drain_requests()
        for execution in list(service._inflight_executions.values()):
            await execution

    marker = "surviving-command-worker"
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        env=process_identity_subprocess_env(marker),
        **isolated_subprocess_kwargs(),
    )
    try:
        identity = capture_spawned_process_identity(survivor.pid, marker)
        assert identity is not None
        # A fire from the previous life: claimed, ``running``, still naming the process
        # that outlived the service -- what the startup reap leaves behind on a maybe.
        orphan = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert orphan is not None
        assert service.request_store.record_command_worker(
            orphan.id, serialize_process_identity(identity)
        )

        asyncio.run(service._run_task(task.id))
        assert not second_copy.exists(), (
            "the cron tick started a second copy of a command whose first copy is still "
            "running -- two writers over one dataset"
        )
        deferred = [
            request
            for request in service.request_store.list_pending()
            if request.task_id == task.id
        ]
        assert deferred, (
            "the deferred fire was dropped rather than requeued; it must run once the "
            "earlier worker is shown gone"
        )

        # The drain reaches the same row on its own tick and must answer it the same way,
        # recording the skip so an indefinitely deferred fire never looks sweepable.
        asyncio.run(_drain_and_wait())
        assert not second_copy.exists(), (
            "the drain ran the second copy the scheduler had just refused to run"
        )
        row = service.request_store.get_run(deferred[0].id)
        assert row is not None and (row["metadata"] or {}).get(
            "last_skip_reason"
        ) == "session_busy", (
            "a fire deferred by a live worker recorded no skip reason, so the stale-run "
            f"sweep sees an idle queued row: {row and row['metadata']!r}"
        )

        assert any(
            worker["run_id"] == orphan.id
            for worker in service.request_store.list_running_command_workers()
        ), "the fire cleared the live worker's record, the only handle on that process"
        assert survivor.poll() is None, (
            "the fire SIGNALLED the earlier worker to make room for itself"
        )

        # Scoped to the definition: another definition's fire is not held up by it.
        asyncio.run(_fire_and_finish_scheduled_task(service, other.id))
        assert other_ran.exists(), (
            "one definition's live worker blocked every other definition's fire"
        )
    finally:
        survivor.kill()
        survivor.wait()

    # Proven dead now, so the record is retired and the definition fires again.
    asyncio.run(_drain_and_wait())
    assert second_copy.exists(), (
        "the deferred fire never ran after its blocking worker exited; a dead worker's "
        "record would hold its own schedule shut until the next restart"
    )
    assert not [
        worker
        for worker in service.request_store.list_running_command_workers()
        if worker["run_id"] == orphan.id
    ], "the proven-dead worker's record was left to block later fires forever"


def test_both_run_stores_name_the_definition_a_command_worker_belongs_to(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-042 -- a worker record nobody can attribute defers nothing.

    SCT-040's guard filters the recorded workers by definition, because "some command
    is running" is not a reason to skip a different one. The file backend omitted the
    field: the SQLite column was renamed ``definition_id`` while the file payload still
    spells the association ``task_id``, so its entries matched no definition at all --
    which does not read as "no worker", it reads as "no worker for every definition at
    once", and single flight silently stopped holding on that backend.

    Asserted on both backends together, since the field is a store contract and a
    reader cannot tell which one it was handed.
    """

    db_path = _command_task_env(tmp_path, monkeypatch)
    assert db_path is not None
    task_store = ScheduledTaskStore()
    task = _add_command_task(task_store, shell_command="sleep 3600", cwd=str(tmp_path))
    identity = {
        "pid": 424242,
        "create_time": 1.0,
        "worker_fingerprint": fingerprint_process_marker("attributed-worker"),
    }

    for label, store in (
        ("sqlite", TaskExecutionStore()),
        ("file", TaskExecutionStore(tmp_path / "task_requests")),
    ):
        claimed = store.claim(
            store.enqueue_task_run(task.id, source_kind="scheduler", task=task).id
        )
        assert claimed is not None, f"{label}: could not claim the fire"
        assert store.record_command_worker(claimed.id, identity), (
            f"{label}: the worker record did not land"
        )
        workers = [
            worker
            for worker in store.list_running_command_workers()
            if worker["run_id"] == claimed.id
        ]
        assert workers, f"{label}: the recorded worker was not listed"
        assert workers[0].get("definition_id") == task.id, (
            f"{label}: the worker is not attributed to its definition "
            f"({workers[0].get('definition_id')!r}), so a fire of that definition "
            "cannot tell this worker apart from another definition's and starts a "
            "second copy of the command"
        )


def test_both_run_stores_settle_a_fire_with_the_command_it_ran(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-049 -- the executed snapshot has to survive the completion that publishes it.

    SCT-028 re-stamps the run with the command the executor really spawned, and every
    reader of that record -- the failure notice, the escalation prompt, the Workbench run
    detail -- reads it off the TERMINAL row. The file backend composed that row from the
    ``TaskExecutionRequest`` the caller still held, which is the enqueue-time copy, so the
    re-stamp was overwritten at the last step and the settled run named the command as it
    stood before the edit. SQLite updates the row the metadata already lives on and keeps
    it for free; the gap was invisible from there.

    Asserted on both backends together, like SCT-042: what a settled run remembers is a
    store contract, and its readers cannot tell which store answered them.
    """

    db_path = _command_task_env(tmp_path, monkeypatch)
    assert db_path is not None
    task_store = ScheduledTaskStore()
    task = _add_command_task(
        task_store, shell_command="echo original", cwd=str(tmp_path)
    )

    for label, store in (
        ("sqlite", TaskExecutionStore()),
        ("file", TaskExecutionStore(tmp_path / "task_requests")),
    ):
        queued = store.enqueue_task_run(task.id, source_kind="scheduler", task=task)
        predicted = store.get_run(queued.id)
        assert predicted is not None
        assert predicted["metadata"].get(COMMAND_SNAPSHOT_METADATA_KEY) == {
            "shell": "echo original",
            "argv": [],
        }, f"{label}: the premise -- the enqueue predicted a command"

        claimed = store.claim(queued.id)
        assert claimed is not None, f"{label}: could not claim the fire"
        # The executor's re-stamp: the definition was edited between enqueue and claim.
        assert store.record_command_snapshot(
            claimed.id, {"shell": "echo edited", "argv": []}
        ), f"{label}: the executed snapshot did not land on the in-flight row"

        assert (
            store.complete(claimed, ok=True, exit_code=0, stdout="edited\n")
            == "succeeded"
        ), f"{label}: the fire did not settle"

        settled = store.get_run(queued.id)
        assert settled is not None, f"{label}: the settled run is unreadable"
        assert settled["metadata"].get(COMMAND_SNAPSHOT_METADATA_KEY) == {
            "shell": "echo edited",
            "argv": [],
        }, (
            f"{label}: the settled run's immutable record of what it executed names a "
            "command it did not execute "
            f"({settled['metadata'].get(COMMAND_SNAPSHOT_METADATA_KEY)!r}), so the "
            "notice and the escalation prompt report the wrong command with a "
            "snapshot's authority"
        )


def test_a_failed_retention_write_keeps_the_stored_worker_record(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-041 -- the last write in the retain path is not allowed to lose the record.

    SCT-036 established the invariant one call up: every maybe keeps the record, a
    failure to look included. The retain path itself then broke it at the end -- a
    locked database while re-stamping the attempt count answered "did not retain", and
    the caller's next line clears the record. A store hiccup, which says nothing at all
    about the process, thereby did the one thing the whole path exists to prevent.

    The stored record survives a failed write untouched, so the honest answer is that it
    was kept: it simply keeps the attempt count it had, this pass is not charged against
    the cap, and the next start looks again.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="sleep 3600", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    orphan = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert orphan is not None
    identity = {
        "pid": 424242,
        "create_time": 1.0,
        "worker_fingerprint": fingerprint_process_marker("unreapable-worker"),
    }
    assert service.request_store.record_command_worker(orphan.id, identity)

    # A worker that could not be shown dead, and a re-stamp that fails the way a
    # momentary lock does. Neither is a fact about the process.
    monkeypatch.setattr(
        scheduled_tasks,
        "reap_orphaned_process_tree",
        lambda *_a, **_kw: "unconfirmed",
    )
    real_record = type(service.request_store).record_command_worker

    def _cannot_write(self, run_id, identity):
        if identity is None:
            # Only the re-stamp fails. The clear on the caller's next line must still
            # work, or this test cannot tell a kept record from an undeletable one.
            return real_record(self, run_id, identity)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        type(service.request_store), "record_command_worker", _cannot_write
    )

    service._reap_orphaned_command_workers()

    monkeypatch.setattr(
        type(service.request_store), "record_command_worker", real_record
    )

    workers = {
        worker["run_id"]: worker["identity"]
        for worker in service.request_store.list_running_command_workers()
    }
    assert orphan.id in workers, (
        "a failed attempt-count write discarded the identity of a worker that could not "
        "be shown dead -- that row was the only place it was written"
    )
    assert workers[orphan.id].get("reap_attempts") is None, (
        "the write failed, so nothing was stored and nothing may be charged against the "
        "attempt cap"
    )
    row = service.request_store.get_run(orphan.id)
    assert row is not None and row["status"] == "running", (
        "the row must stay ``running`` for the next start's enumeration to see it"
    )


def test_a_recovered_fire_is_settled_when_its_worker_is_finally_shown_gone(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-044 -- releasing the definition is only half of closing the old fire.

    SCT-040 made a proven death release the definition immediately instead of waiting
    for a restart, which left the interrupted run behind: NO OTHER PASS COMES BACK FOR
    IT. ``recover_processing`` runs once at startup and steps over any row still naming
    a worker -- this row's whole reason for surviving -- and ``sweep_stale_runs`` only
    classifies orphans of ``run_type == "agent_run"``. So the run stayed ``running``
    until the next restart while the backup it described was over, the definition read
    as still executing, and the user was never told the fire had been interrupted at all.

    Settled the same way the startup pass would have settled it, ``restarted``, since
    that is what happened -- one interruption should not be told two ways depending on
    which pass got to prove the death.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="sleep 3600", cwd=str(tmp_path))
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    # A worker whose death is PROVABLE: spawned, named, then reaped, so its recorded
    # identity points at a pid the OS has fully released.
    marker = "worker-that-died-while-the-service-was-down"
    dead = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        env=process_identity_subprocess_env(marker),
        **isolated_subprocess_kwargs(),
    )
    identity = capture_spawned_process_identity(dead.pid, marker)
    dead.wait()
    assert identity is not None

    orphan = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert orphan is not None
    assert service.request_store.record_command_worker(
        orphan.id, serialize_process_identity(identity)
    )

    assert service._command_definition_has_live_worker(task.id) is False, (
        "a worker proven dead must not defer the next fire"
    )

    row = service.request_store.get_run(orphan.id)
    assert row is not None
    assert row["status"] == "failed", (
        "the recovered fire was released but never closed, so it reads as still running "
        f"a command that ended before the service came back: {row['status']!r}"
    )
    assert row["completed_at"], "a terminal run with no completion time"
    assert row["error"], (
        "the interrupted fire settled with no explanation for the user to read"
    )
    assert (row["metadata"] or {}).get("interrupt_reason") == "restarted", (
        "the interruption was recorded under a different cause than the startup pass "
        f"uses for the same event: {row['metadata']!r}"
    )
    assert not [
        worker
        for worker in service.request_store.list_running_command_workers()
        if worker["run_id"] == orphan.id
    ], "the dead worker's record outlived the run it named"
    settled = store.get_task(task.id)
    assert settled is not None and settled.last_error, (
        "the definition still shows no failure for a fire that was interrupted, so the "
        "Harness lists it as healthy and the user never learns the command was cut off"
    )


def test_a_recovered_fire_is_settled_on_the_file_store_too(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-046 -- the legacy store must not release a fire it never closed.

    SCT-044 settles the released fire through the guarded per-run writer, which only
    SQLite has. On the file store that check answered "unsupported" and the method
    returned having done nothing but clear the worker record: the definition was
    released, the next fire ran, and the interrupted run's file sat in ``processing``
    reading ``running`` until some later restart -- the exact leak SCT-044 removed,
    still open one backend over.

    Both backends are asked the same question here because both are read the same way:
    ``vibe runs`` and the Harness show whatever the store says, and a run that says
    ``running`` describes a backup that is still going.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    task = _add_command_task(store, shell_command="sleep 3600", cwd=str(tmp_path))
    service = _binding_service(tmp_path, store, [])
    assert service.request_store.supports_guarded_settlement() is False, (
        "this test is about the store WITHOUT the guarded writer"
    )

    marker = "file-store-worker-that-died-while-the-service-was-down"
    dead = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        env=process_identity_subprocess_env(marker),
        **isolated_subprocess_kwargs(),
    )
    identity = capture_spawned_process_identity(dead.pid, marker)
    dead.wait()
    assert identity is not None

    orphan = service.request_store.claim(
        service.request_store.enqueue_task_run(
            task.id, source_kind="scheduler", task=task
        ).id
    )
    assert orphan is not None
    assert service.request_store.mark_execution_started(orphan.id)
    assert service.request_store.record_command_worker(
        orphan.id, serialize_process_identity(identity)
    )

    assert service._command_definition_has_live_worker(task.id) is False

    row = service.request_store.get_run(orphan.id)
    assert row is not None
    assert row["status"] == "failed", (
        "the file-backed fire was released but never closed, so it still reads as "
        f"running a command that ended before the service came back: {row['status']!r}"
    )
    assert row["error"], "the interrupted fire settled with no explanation"
    assert (row["metadata"] or {}).get("interrupt_reason") == "restarted"
    assert not (tmp_path / "task_requests" / "processing" / f"{orphan.id}.json").exists(), (
        "the run was reported terminal while its processing file still claimed it"
    )
    assert not [
        worker
        for worker in service.request_store.list_running_command_workers()
        if worker["run_id"] == orphan.id
    ], "the dead worker's record outlived the run it named"


def test_an_interrupted_command_fire_clears_the_previous_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-021 -- an interrupted fire is still a fire with no status of its own.

    SCT-017 gave the command fire's own stamp authority over ``last_exit_code``,
    ``None`` included. A cancelled or shutdown-interrupted fire never reaches that
    stamp: ``run_supervised_command`` raises, and settlement projects the run through
    the generic lane, which by design leaves the column alone so a message task cannot
    blank a command's code. The result was "exited 7" on the row beside a run the user
    had just stopped -- the same fabricated fact SCT-017 removed, one lane over.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    started = tmp_path / "interrupted-started"
    task = _add_command_task(
        store,
        shell_command=f"touch {started.name}; sleep 3600",
        cwd=str(tmp_path),
    )
    # What a previous fire of this definition left behind.
    assert store.mark_task_result(
        task.id, error="exited 7", exit_code=7, records_command_outcome=True
    )
    assert ScheduledTaskStore().get_task(task.id).last_exit_code == 7
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    async def _cancel_mid_flight() -> None:
        request = service.request_store.claim(
            service.request_store.enqueue_task_run(
                task.id, source_kind="scheduler", task=task
            ).id
        )
        assert request is not None
        service._spawn_execution(request, service._execution_lock_key(request))
        for _ in range(200):
            if started.exists():
                break
            await asyncio.sleep(0.05)
        assert started.exists(), "the command never started"
        service.request_store.cancel_run(request.id)
        service._propagate_requested_cancellations()
        await asyncio.wait_for(
            asyncio.gather(service._inflight_executions[request.id], return_exceptions=True),
            timeout=10,
        )

    asyncio.run(_cancel_mid_flight())

    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None
    assert stored.last_exit_code is None, (
        "an interrupted command fire kept the previous fire's exit code: "
        f"{stored.last_exit_code!r}"
    )
    assert stored.last_error, "the interruption was not recorded on the definition"


def test_a_failed_one_shot_command_task_is_disabled_and_escalates_together(
    tmp_path: Path, monkeypatch
) -> None:
    """The trap this design avoids: the stamp that authorises the turn also disables.

    ``enqueue_definition_run`` re-reads the definition server-side and RAISES on a
    disabled one -- and a failed ``at`` task is disabled by the very stamp that
    authorises its escalation. Routing the turn through that writer would mean a
    one-shot command task NEVER escalates, which is the case that needs it most: there
    is no next fire to notice the failure.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_oneshot")
    task = _escalation_command_task(
        store,
        tmp_path,
        shell_command="exit 7",
        schedule_type="at",
        session_id=session_id,
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "failed"
    stored = ScheduledTaskStore().get_task(task.id)
    assert stored is not None and stored.enabled is False, (
        "a failed one-shot command definition stayed enabled"
    )
    assert len(_escalation_runs(store)) == 1, (
        "the one-shot was disabled without queueing the report that explains why"
    )


def test_a_refused_result_stamp_queues_no_escalation_and_leaves_a_notice(
    tmp_path: Path, monkeypatch
) -> None:
    """Stamp refused -> zero escalation rows AND a notice: never silent.

    THE PRODUCTION STORY (HFR-261/HFR-264 applied to this lane). A command task fires
    and fails. While the fire is running the user archives the bound Session, and
    ``reclaim_bound_definitions(mode='delete')`` soft-deletes the definition. The
    guarded stamp then correctly REFUSES -- and the escalation rolls back with it,
    because both were the same transaction. The failure must still be visible, so the
    owed-notice path takes it over.
    """

    from storage.session_reclaim import RECLAIM_DELETE

    from storage.sessions_service import SQLiteSessionsService

    _binding_env(tmp_path, monkeypatch)
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / "avibe_home")
    store = ScheduledTaskStore()
    assert store._sqlite is not None
    sessions = SQLiteSessionsService(paths.get_sqlite_state_path())
    try:
        session_id = sessions.bind_agent_session(
            scope_key="slack::channel::C123",
            agent_name="codex",
            session_anchor="slack_C123:escalation_refusal",
            native_session_id="native-esc",
        )
    finally:
        sessions.close()
    assert session_id is not None
    task = _escalation_command_task(
        store, tmp_path, shell_command="exit 7", session_id=session_id
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    race = _commit_reclaim_after(
        store._sqlite.engine,
        session_id,
        read=_DEFINITION_EXISTS_SELECT,
        mode=RECLAIM_DELETE,
        reason="the session was archived",
    )

    run = _fire_command_task(service, task)

    assert race["fired"] == 1, (
        "the competing archive never landed inside the write window, so this test "
        "proved nothing; the rendered SQL of the guarded upsert's existence probe drifted"
    )
    assert run["status"] == "failed"
    assert _escalation_runs(store) == [], (
        "an escalation survived a refused stamp: a durable turn nothing authorises"
    )
    assert run["metadata"].get("escalation_run_id") is None, (
        "the run claims an escalation the transaction rolled back"
    )
    notice = store._sqlite.owed_failure_notice(run["id"])
    assert notice is not None and notice.get("state"), (
        "no escalation AND no notice: the failure is silent, which is the one outcome "
        "this design must never produce"
    )


def test_claiming_an_escalation_dispatches_it_as_a_hook_send(
    tmp_path: Path, monkeypatch
) -> None:
    """The executor has to accept the new request type and route it somewhere real.

    ``delivery_intent_for_trigger`` has a CLOSED vocabulary and would RAISE on an
    unmapped trigger, so an escalation takes the hook intent -- it IS an out-of-band
    turn a definition queued. Provenance is not lost: the row still carries
    ``run_type="task_escalation"`` and its parent fire.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_escalation_exec")
    task = _escalation_command_task(
        store, tmp_path, shell_command="exit 7", session_id=session_id
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    dispatched: list[dict[str, Any]] = []

    async def _spy(**kwargs):
        dispatched.append(kwargs)
        return TaskDispatchResult(error=None)

    service._execute_request = _spy  # type: ignore[method-assign]

    escalation = service.request_store.build_hook_send(
        session_key="",
        session_id=session_id,
        prompt="A scheduled command task failed: nightly sync",
        deliver_key=task.deliver_key,
        session_policy=task.session_policy,
        run_type="task_escalation",
        definition_id=task.id,
        source_kind="scheduler",
        parent_run_id="parent-run",
    )
    service.request_store.enqueue(escalation)
    assert [run["id"] for run in _escalation_runs(store)] == [escalation.id], (
        "the escalation is not visible as a queued run at all"
    )
    assert [
        pending.id
        for pending in service.request_store.list_pending()
        if pending.request_type == "task_escalation"
    ] == [escalation.id], (
        "the drain loop's pending list filters the escalation out, so a durable "
        "escalation would never be executed"
    )
    claimed = service.request_store.claim(escalation.id)
    assert claimed is not None

    asyncio.run(service._execute_claimed_request(claimed))

    assert len(dispatched) == 1, f"the escalation was not dispatched: {dispatched!r}"
    assert dispatched[0]["trigger_kind"] == "hook", (
        f"an escalation must take the hook delivery intent: {dispatched[0]['trigger_kind']!r}"
    )
    assert dispatched[0]["prompt"] == escalation.prompt, (
        "the dispatched turn is not carrying the composed report"
    )
    settled = service.request_store.get_run(escalation.id)
    assert settled is not None and settled["status"] == "succeeded"
    assert settled["run_type"] == "task_escalation", (
        f"the escalation's provenance was rewritten to {settled['run_type']!r}"
    )


def test_a_successful_on_failure_agent_command_fire_escalates_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """``--on-failure agent`` is a FAILURE policy; success stays silent.

    Otherwise a minute-ly health check would bill an Agent turn every minute and post
    a reply nobody asked for.
    """

    _command_task_env(tmp_path, monkeypatch)
    store = ScheduledTaskStore()
    session_id = _bare_session_row(workdir=tmp_path, anchor="avibe_task_escalation_ok")
    task = _escalation_command_task(
        store, tmp_path, shell_command="echo fine", session_id=session_id
    )
    service = _scheduled_service_with_ledger(tmp_path, store, [])

    run = _fire_command_task(service, task)

    assert run["status"] == "succeeded", f"the premise: the fire succeeded ({run['error']!r})"
    assert _escalation_runs(store) == [], "a successful fire escalated anyway"
    assert run["metadata"].get("escalation_run_id") is None
    assert store._sqlite.owed_failure_notice(run["id"]) is None, (
        "a succeeded run must owe no notice either"
    )


def test_complete_with_an_escalation_marker_stamps_no_owed_notice(
    tmp_path: Path, monkeypatch
) -> None:
    """The suppression is decided from the metadata the SAME statement writes.

    ``_merge_owed_failure_notice`` applies ``extra_metadata`` before
    ``_owed_failure_notice_for_transition`` reads it, which is what makes the marker
    visible to the notice decision. The regression half is the second run: without the
    kwarg the identical failure still owes its notice, so this cannot silence anything
    it was not asked to.
    """

    _command_task_env(tmp_path, monkeypatch)
    requests = TaskExecutionStore()
    assert requests.sqlite_backend is not None

    escalated = requests.enqueue_hook_send(session_key="slack::channel::C123", prompt="hi")
    claimed = requests.claim(escalated.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="boom", escalation_run_id="esc-9")

    row = requests.get_run(escalated.id)
    assert row is not None and row["status"] == "failed"
    assert row["metadata"].get("escalation_run_id") == "esc-9", (
        f"the marker never reached the run row: {row['metadata']!r}"
    )
    assert requests.sqlite_backend.owed_failure_notice(escalated.id) is None, (
        "an escalated failure stamped a notice as well"
    )

    plain = requests.enqueue_hook_send(session_key="slack::channel::C123", prompt="hi")
    plain_claimed = requests.claim(plain.id)
    assert plain_claimed is not None
    requests.complete(plain_claimed, ok=False, error="boom")

    notice = requests.sqlite_backend.owed_failure_notice(plain.id)
    assert notice is not None and notice.get("state"), (
        "the suppression leaked to ordinary failures; every failed run would go silent"
    )

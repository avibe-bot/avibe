"""Contracts for the shared, per-user Memory capture boundary."""

from __future__ import annotations

import ast
import asyncio
import gc
import json
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig
from core.controller import Controller
from core.handlers.message_handler import MessageHandler
from core.memory import CaptureAccepted, CaptureDuplicate, CaptureRequest, CaptureSkipped
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.everos import FakeMemoryProvider
from core.memory.runtime import MemoryRuntime
from core.memory.store import MemoryStore
from modules.im.base import FileAttachment, MessageContext
from modules.im.message_facts import (
    is_ordinary_discord_text,
    is_ordinary_feishu_text,
    is_ordinary_slack_attachment,
    is_ordinary_slack_text,
    is_ordinary_telegram_text,
    is_ordinary_wechat_text,
)


PROJECT = "default"
REPO_ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self, user) -> None:
        self.user = user

    def maybe_reload(self) -> None:
        return None

    def get_user(self, _user_id: str, *, platform: str):
        return self.user


class _Manager:
    def __init__(self, user) -> None:
        self.store = _Store(user)

    def get_store(self):
        return self.store


class _Runtime:
    """Stand in for MemoryRuntime, which owns the capture module."""

    def __init__(self, module) -> None:
        self.module = module
        self.available = True
        self.retired = False
        self.attachment_status = "ready"
        self.final_flush_calls: list[dict[str, object]] = []
        self.final_flush_result = True
        self.final_flush_error: Exception | None = None
        self.session_lifecycle_calls: list[dict[str, object]] = []
        self.session_lifecycle_error: Exception | None = None
        self.recovered_scope: tuple[str, str] | None = None
        self.recovered_scopes: tuple[tuple[str, str], ...] = ()
        self.scope_recovery_calls: list[str] = []
        self.scopes_recovery_calls: list[str] = []

    def principal_for_user_key(self, user_key: str) -> str:
        suffix = "1" if user_key.endswith("user-1") else "2"
        return f"u-{suffix * 32}"

    def project_for_workdir(self, workdir: str) -> str:
        assert workdir == "/tmp/project"
        return PROJECT

    async def attachment_capture_status(self) -> str:
        return self.attachment_status

    async def resolve_current_session_scope(self, raw_session_id: str) -> tuple[str, str] | None:
        self.scope_recovery_calls.append(raw_session_id)
        return self.recovered_scope

    async def resolve_current_session_scopes(
        self,
        raw_session_id: str,
    ) -> tuple[tuple[str, str], ...]:
        self.scopes_recovery_calls.append(raw_session_id)
        return self.recovered_scopes

    async def final_flush(self, **kwargs) -> bool:
        self.final_flush_calls.append(kwargs)
        if self.final_flush_error is not None:
            raise self.final_flush_error
        return self.final_flush_result

    async def run_session_lifecycle(self, **kwargs):
        operation = kwargs.pop("operation")
        self.session_lifecycle_calls.append(kwargs)
        if self.session_lifecycle_error is not None:
            raise self.session_lifecycle_error
        return await operation()

    async def run_session_scopes_lifecycle(self, **kwargs):
        operation = kwargs.pop("operation")
        self.session_lifecycle_calls.append(kwargs)
        if self.session_lifecycle_error is not None:
            raise self.session_lifecycle_error
        return await operation()


class _CaptureModule:
    def __init__(self) -> None:
        self.accepted = []
        self.seen: set[str] = set()

    async def capture(self, request):
        if request.source_message_id in self.seen:
            return CaptureDuplicate()
        self.seen.add(request.source_message_id)
        self.accepted.append(request)
        return CaptureAccepted()


def _controller(*, user=None):
    user = user or SimpleNamespace(enabled=True, is_admin=False)
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    controller.platform_settings_managers = {
        platform: _Manager(user)
        for platform in ("slack", "discord", "telegram", "feishu", "wechat", "lark")
    }
    controller.memory_module = _CaptureModule()
    controller.memory_runtime = _Runtime(controller.memory_module)
    controller.get_cwd = lambda _context: "/tmp/project"
    return controller


def _context(platform: str, *, user_id: str = "user-1", ordinary=True, **payload) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        channel_id="dm-1",
        platform=platform,
        message_id="native-1",
        platform_specific={"platform": platform, "is_dm": True, **payload},
        files=[],
        is_ordinary_text=ordinary,
    )


def test_message_handler_retains_memory_capture_task_until_completion() -> None:
    handler = MessageHandler.__new__(MessageHandler)
    handler._memory_capture_tasks = set()

    async def run() -> None:
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        reference = weakref.ref(task)
        handler._track_memory_capture_task(task)
        del task
        gc.collect()

        assert reference() is not None
        assert len(handler._memory_capture_tasks) == 1

        release.set()
        await reference()
        await asyncio.sleep(0)
        assert handler._memory_capture_tasks == set()

    asyncio.run(run())


def test_message_handler_drains_memory_capture_tasks() -> None:
    handler = MessageHandler.__new__(MessageHandler)
    handler._memory_capture_tasks = set()
    completed = False

    async def run() -> None:
        nonlocal completed
        release = asyncio.Event()

        async def capture() -> None:
            nonlocal completed
            await release.wait()
            completed = True

        task = asyncio.create_task(capture())
        handler._track_memory_capture_task(task)
        drain = asyncio.create_task(handler.drain_memory_capture_tasks())
        await asyncio.sleep(0)
        assert not drain.done()

        release.set()
        await drain
        assert completed is True
        assert handler._memory_capture_tasks == set()

    asyncio.run(run())


@pytest.mark.parametrize("platform", ["slack", "discord", "telegram", "feishu", "wechat"])
def test_capture_admits_every_enabled_bound_dm_user(platform: str) -> None:
    controller = _controller(user=SimpleNamespace(enabled=True, is_admin=False))

    assert controller.memory_capture_admitted(_context(platform)) is True
    assert controller.memory_capture_admitted(_context(platform, is_dm=False)) is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [("ready", True), ("not_configured", False), ("unavailable", False)],
)
def test_slack_memory_lease_retention_requires_current_attachment_readiness(
    status: str,
    expected: bool,
) -> None:
    """Scenarios: MEMORY-IM-ATTACH-001, MEMORY-IM-ATTACH-003."""

    controller = _controller()
    controller.memory_runtime.attachment_status = status
    context = _context("slack", ordinary=False)
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            url="https://files.slack.test/private",
        )
    ]
    context.is_ordinary_attachment = True

    assert (
        asyncio.run(
            controller.memory_attachment_capture_admitted(
                context,
                "stable-session",
            )
        )
        is expected
    )


@pytest.mark.parametrize(
    "context,text,enabled",
    [
        (_context("slack", is_dm=False), "normal", True),
        (_context("slack", ordinary=False), "normal", True),
        (_context("slack", user_id=""), "normal", True),
        (_context("avibe", user_id="workbench"), "normal", True),
        (_context("slack"), "normal", False),
    ],
)
def test_capture_skips_ineligible_human_turns(context, text, enabled) -> None:
    controller = _controller()
    controller.config.memory.enabled = enabled

    asyncio.run(controller.capture_user_memory(context, text, "stable-session"))

    assert controller.memory_module.accepted == []


def test_capture_stamps_user_principal_provenance_and_native_dedup_key() -> None:
    controller = _controller()
    context = _context("telegram")
    other_user = _context("telegram", user_id="user-2")

    asyncio.run(controller.capture_user_memory(context, "/memory status", "stable-session"))
    asyncio.run(controller.capture_user_memory(context, "/memory status", "stable-session"))
    asyncio.run(controller.capture_user_memory(other_user, "/memory status", "stable-session"))

    assert len(controller.memory_module.accepted) == 2
    request = controller.memory_module.accepted[0]
    assert request.source_message_id == f"im:telegram:u-{'1' * 32}:native-1"
    assert request.session_id == "stable-session"
    assert request.principal_id == "u-" + ("1" * 32)
    assert request.project_id == PROJECT
    assert request.provenance == "user_input"
    assert request.text == "/memory status"
    assert controller.memory_module.accepted[1].source_message_id == f"im:telegram:u-{'2' * 32}:native-1"


def test_capture_user_memory_rejects_fresh_runtime_after_reset_gate() -> None:
    controller = _controller()
    gate = controller._memory_replacement_lock()

    async def run() -> None:
        await gate.acquire()
        capture = controller.capture_user_memory(
            _context("telegram"),
            "remember this",
            "stable-session",
        )

        fresh_module = _CaptureModule()
        controller.memory_runtime = _Runtime(fresh_module)
        queued = asyncio.create_task(capture)
        await asyncio.sleep(0)
        gate.release()

        await queued
        assert fresh_module.accepted == []

    asyncio.run(run())


def test_capture_paths_reject_a_settled_factory_reset_marker() -> None:
    controller = _controller()
    controller.config.memory.recovery_intent = "factory_reset"
    controller.memory_runtime._config = SimpleNamespace(recovery_intent="factory_reset")
    controller.memory_runtime._restart_config = SimpleNamespace(recovery_intent="factory_reset")

    direct = asyncio.run(
        controller.capture_memory(
            CaptureRequest(
                source_message_id="direct-source",
                session_id="stable-session",
                principal_id="u-" + ("1" * 32),
                project_id=PROJECT,
                provenance="agent",
                text="remember this",
                occurred_at_ms=1,
            )
        )
    )
    asyncio.run(controller.capture_user_memory(_context("telegram"), "remember this", "stable-session"))

    assert isinstance(direct, CaptureSkipped)
    assert direct.reason == "memory_operation_in_progress"
    assert controller.memory_module.accepted == []


def test_final_flush_memory_session_reuses_capture_scope_and_raw_anchor() -> None:
    controller = _controller()

    result = asyncio.run(controller.final_flush_memory_session(_context("telegram"), "telegram_dm-1"))

    assert result is True
    assert controller.memory_runtime.final_flush_calls == [
        {
            "principal_id": "u-" + ("1" * 32),
            "project_id": PROJECT,
            "raw_session_id": "telegram_dm-1",
            "deadline_seconds": 5.0,
        }
    ]


@pytest.mark.parametrize("enabled,admitted", [(False, True), (True, False)])
def test_final_flush_memory_session_fails_closed_without_admitted_scope(enabled: bool, admitted: bool) -> None:
    controller = _controller(user=SimpleNamespace(enabled=admitted, is_admin=False))
    controller.config.memory.enabled = enabled

    result = asyncio.run(controller.final_flush_memory_session(_context("slack"), "slack_dm-1"))

    assert result is False
    assert controller.memory_runtime.final_flush_calls == []


def test_final_flush_memory_session_swallows_runtime_failure() -> None:
    controller = _controller()
    controller.memory_runtime.final_flush_error = RuntimeError("provider unavailable")

    result = asyncio.run(controller.final_flush_memory_session(_context("wechat"), "wechat_dm-1"))

    assert result is False
    assert len(controller.memory_runtime.final_flush_calls) == 1


def test_memory_session_lifecycle_reuses_capture_scope_and_raw_anchor() -> None:
    controller = _controller()
    operation_calls = []

    async def reset_session() -> str:
        operation_calls.append("reset")
        return "reset-complete"

    result = asyncio.run(
        controller.run_memory_session_lifecycle(
            _context("wechat"),
            "wechat_dm-1",
            reset_session,
            deadline_seconds=4.0,
        )
    )

    assert result == "reset-complete"
    assert operation_calls == ["reset"]
    assert controller.memory_runtime.session_lifecycle_calls == [
        {
            "principal_id": "u-" + ("1" * 32),
            "project_id": PROJECT,
            "raw_session_id": "wechat_dm-1",
            "deadline_seconds": 4.0,
        }
    ]


def test_memory_session_lifecycle_does_not_reset_without_a_fence() -> None:
    controller = _controller()
    controller.memory_runtime.session_lifecycle_error = RuntimeError("fence unavailable")
    operation_calls = []

    async def reset_session() -> str:
        operation_calls.append("reset")
        return "reset-complete"

    with pytest.raises(RuntimeError, match="fence unavailable"):
        asyncio.run(
            controller.run_memory_session_lifecycle(
                _context("slack"),
                "slack_dm-1",
                reset_session,
            )
        )

    assert operation_calls == []


def test_memory_session_lifecycle_resets_without_guessing_an_ineligible_scope() -> None:
    controller = _controller(user=SimpleNamespace(enabled=False, is_admin=False))
    operation_calls = []

    async def reset_session() -> str:
        operation_calls.append("reset")
        return "reset-complete"

    result = asyncio.run(
        controller.run_memory_session_lifecycle(
            _context("slack"),
            "slack_dm-1",
            reset_session,
        )
    )

    assert result == "reset-complete"
    assert operation_calls == ["reset"]
    assert controller.memory_runtime.session_lifecycle_calls == []


def test_memory_session_lifecycle_does_not_repeat_failed_reset() -> None:
    controller = _controller()
    operation_calls = []

    async def reset_session() -> None:
        operation_calls.append("reset")
        raise RuntimeError("reset failed")

    with pytest.raises(RuntimeError, match="reset failed"):
        asyncio.run(
            controller.run_memory_session_lifecycle(
                _context("telegram"),
                "telegram_dm-1",
                reset_session,
            )
        )

    assert operation_calls == ["reset"]


def test_final_flush_memory_cli_session_uses_trusted_stored_scope() -> None:
    controller = _controller()
    principal_id = "u-" + ("2" * 32)
    controller._memory_scopes_by_session = {
        "ses-workbench": (principal_id, PROJECT),
    }
    controller._memory_cli_facts_by_session = {}

    result = asyncio.run(
        controller.final_flush_memory_cli_session(
            "ses-workbench",
            deadline_seconds=4.0,
        )
    )

    assert result is True
    assert controller.memory_runtime.session_lifecycle_calls == [
        {
            "scopes": ((principal_id, PROJECT),),
            "raw_session_id": "ses-workbench",
            "deadline_seconds": 4.0,
        }
    ]


def test_final_flush_memory_cli_session_swallows_runtime_failure() -> None:
    controller = _controller()
    controller.memory_runtime.session_lifecycle_error = RuntimeError("provider unavailable")
    controller._memory_scopes_by_session = {
        "ses-workbench": ("u-" + ("2" * 32), PROJECT),
    }
    controller._memory_cli_facts_by_session = {}

    result = asyncio.run(
        controller.final_flush_memory_cli_session(
            "ses-workbench",
        )
    )

    assert result is False
    assert len(controller.memory_runtime.session_lifecycle_calls) == 1


def test_final_flush_memory_cli_session_recovers_scope_after_controller_restart() -> None:
    controller = _controller()
    principal_id = "u-" + ("2" * 32)
    controller._memory_scopes_by_session = {}
    controller.memory_runtime.recovered_scopes = ((principal_id, PROJECT),)

    result = asyncio.run(controller.final_flush_memory_cli_session("ses-workbench"))

    assert result is True
    assert controller.memory_runtime.scopes_recovery_calls == ["ses-workbench"]
    assert controller.memory_runtime.session_lifecycle_calls == [
        {
            "scopes": ((principal_id, PROJECT),),
            "raw_session_id": "ses-workbench",
            "deadline_seconds": 5.0,
        }
    ]


def test_final_flush_memory_cli_session_skips_without_stored_scope() -> None:
    controller = _controller()
    controller._memory_scopes_by_session = {}

    result = asyncio.run(controller.final_flush_memory_cli_session("ses-absent"))

    assert result is False
    assert controller.memory_runtime.scopes_recovery_calls == ["ses-absent"]
    assert controller.memory_runtime.final_flush_calls == []


def test_archive_memory_cli_session_holds_capture_fence_through_db_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.services import sessions as sessions_service
    from storage import workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        session_id = workbench_sessions_service.create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend="claude",
            title="Archive me",
        )["id"]

    principal_id = "u-" + ("2" * 32)
    admission_lock = asyncio.Lock()
    capture_may_enter = asyncio.Event()
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()
    flush_entered = asyncio.Event()
    release_flush = asyncio.Event()
    archive_lock_states: list[bool] = []

    class LifecycleRuntime(_Runtime):
        async def run_session_scopes_lifecycle(self, **kwargs):
            operation = kwargs.pop("operation")
            self.session_lifecycle_calls.append(kwargs)
            async with admission_lock:
                flush_entered.set()
                await release_flush.wait()
                return await operation()

    controller = _controller()
    controller.memory_runtime = LifecycleRuntime(controller.memory_module)
    from core.session_turns import SessionTurnManager

    controller.session_turns = SessionTurnManager()
    controller._memory_scopes_by_session = {
        session_id: (principal_id, PROJECT),
    }
    controller._memory_cli_facts_by_session = {}

    original_archive = sessions_service.archive_session

    def archive_under_test(conn, actual_session_id):
        archive_lock_states.append(admission_lock.locked())
        return original_archive(conn, actual_session_id)

    monkeypatch.setattr(sessions_service, "archive_session", archive_under_test)

    async def run() -> None:
        turn_admission = await controller.session_turns.acquire_lifecycle_admission(
            session_id
        )

        async def old_capture() -> None:
            await capture_may_enter.wait()
            async with admission_lock:
                capture_entered.set()
                await release_capture.wait()
            turn_admission.release()

        capture = asyncio.create_task(old_capture())
        archive = asyncio.create_task(
            controller.archive_memory_cli_session(
                session_id,
                deadline_seconds=2.0,
            )
        )
        await asyncio.sleep(0)

        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "active"
        assert not archive.done()
        assert not flush_entered.is_set()

        # This turn was admitted before archive but had not reached capture yet.
        # Archive must not take Memory admission ahead of it.
        capture_may_enter.set()
        await asyncio.wait_for(capture_entered.wait(), timeout=1.0)
        assert not flush_entered.is_set()

        release_capture.set()
        await capture
        await asyncio.wait_for(flush_entered.wait(), timeout=1.0)

        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "active"
        assert not archive.done()

        release_flush.set()
        result = await archive

        assert result["status"] == "archived"
        assert archive_lock_states == [True]
        assert controller.memory_runtime.session_lifecycle_calls == [
            {
                "scopes": ((principal_id, PROJECT),),
                "raw_session_id": session_id,
                "deadline_seconds": 2.0,
            }
        ]
        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "archived"

    try:
        asyncio.run(run())
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["missing", "reserved", "archived"])
def test_archive_memory_cli_session_preflight_skips_memory_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    from storage import workbench_sessions_service
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    session_id = "ses-missing"
    if state == "reserved":
        session_id = WORKSPACE_NOTICE_SESSION_ID
    elif state == "archived":
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        with engine.begin() as conn:
            project = create_project(conn, str(project_dir), display_name="Project")
            session_id = workbench_sessions_service.create_session(
                conn,
                scope_id=project["scope_id"],
                agent_backend="claude",
            )["id"]
            workbench_sessions_service.archive_session(conn, session_id)

    controller = _controller()
    controller._memory_scopes_by_session = {
        session_id: ("u-" + ("2" * 32), PROJECT),
    }
    controller._memory_cli_facts_by_session = {}

    async def archive() -> dict[str, object]:
        return await controller.archive_memory_cli_session(session_id)

    try:
        if state == "missing":
            with pytest.raises(LookupError):
                asyncio.run(archive())
        elif state == "reserved":
            with pytest.raises(PermissionError):
                asyncio.run(archive())
        else:
            assert asyncio.run(archive())["status"] == "archived"
    finally:
        engine.dispose()

    assert controller.memory_runtime.session_lifecycle_calls == []
    assert controller.memory_runtime.scope_recovery_calls == []


@pytest.mark.parametrize("restarted", [False, True])
def test_archive_memory_cli_session_flushes_every_cwd_scope_deterministically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    restarted: bool,
) -> None:
    from storage import workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        session_id = workbench_sessions_service.create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend="claude",
            title="Archive every Memory project",
        )["id"]

    first_scope = (
        "u-11111111111111111111111111111111",
        "p-11111111111111111111111111111111",
    )
    second_scope = (
        "u-22222222222222222222222222222222",
        "p-22222222222222222222222222222222",
    )
    controller = _controller()
    controller._memory_scopes_by_session = (
        {} if restarted else {session_id: second_scope}
    )
    controller._memory_cli_facts_by_session = {}
    controller.memory_runtime.recovered_scopes = (second_scope, first_scope)

    try:
        result = asyncio.run(
            controller.archive_memory_cli_session(
                session_id,
                deadline_seconds=3.0,
            )
        )
    finally:
        engine.dispose()

    assert result["status"] == "archived"
    assert controller.memory_runtime.scopes_recovery_calls == [session_id]
    assert controller.memory_runtime.session_lifecycle_calls == [
        {
            "scopes": (first_scope, second_scope),
            "raw_session_id": session_id,
            "deadline_seconds": 3.0,
        }
    ]


def test_archive_memory_cli_session_skips_flush_when_memory_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core.session_turns import SessionTurnManager
    from storage import workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")
        session_id = workbench_sessions_service.create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend="claude",
            title="Archive with Memory disabled",
        )["id"]

    principal_id = "u-" + ("2" * 32)
    store = MemoryStore(tmp_path / "memory.sqlite")
    accepted = store.enqueue_request(
        source_message_id="persisted-scope",
        session_id=session_id,
        principal_id=principal_id,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="persist this scope",
        occurred_at_ms=1_725_000_001_234,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert accepted.row is not None

    disabled = MemoryConfig(enabled=False)
    runtime = MemoryRuntime(
        disabled,
        store=store,
        artifact_manager=FakeMemoryArtifactManager(python=Path(sys.executable)),
        effective_home=tmp_path / "runtime",
    )
    assert runtime.available is True
    assert runtime._maintenance_open() is False
    provider = FakeMemoryProvider()
    runtime.module.replace_provider(provider)
    controller = _controller()
    controller.config.memory = disabled
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    controller.session_turns = SessionTurnManager(controller)
    controller._memory_scopes_by_session = {}
    controller._memory_cli_facts_by_session = {}

    async def run() -> dict[str, object]:
        try:
            return await controller.archive_memory_cli_session(session_id)
        finally:
            await runtime.close()

    try:
        result = asyncio.run(run())
        assert result["status"] == "archived"
        assert store.resolve_current_session_scopes(session_id) == (
            (principal_id, PROJECT),
        )
        assert provider.flushes == []
        assert store.list_queue_rows()[0].state == "pending"
        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "archived"
    finally:
        engine.dispose()


def test_removed_memory_runtime_lifecycle_symbols_have_no_callers() -> None:
    """Keep callers on MemoryModule after lifecycle ownership moves there."""

    module_internal = "_final_flush_" + "under_admission"
    removed = {
        module_internal,
        "_final_flush_" + "timeout",
        "_replace_" + "provider",
    }
    module_path = REPO_ROOT / "core" / "memory" / "module.py"
    offenders: list[str] = []
    for source_path in sorted(REPO_ROOT.rglob("*.py")):
        if any(part in {".git", ".runtime", ".venv"} for part in source_path.parts):
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            symbol: str | None = None
            if isinstance(node, ast.Attribute):
                symbol = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Name)):
                symbol = node.name if hasattr(node, "name") else node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                symbol = node.value
            if symbol not in removed:
                continue
            if source_path == module_path and symbol == module_internal:
                continue
            offenders.append(f"{source_path.relative_to(REPO_ROOT)}:{node.lineno}: {symbol}")

    assert offenders == []


def test_workbench_capture_requires_resolved_identity_and_uses_row_id() -> None:
    controller = _controller()
    context = _context("avibe", user_id="local")

    asyncio.run(controller.capture_user_memory(context, "ordinary text", "stable-session"))

    request = controller.memory_module.accepted[0]
    assert request.source_message_id == f"workbench:u-{'2' * 32}:native-1"
    assert request.principal_id == "u-" + ("2" * 32)
    assert request.project_id == PROJECT


def test_workbench_capture_converts_owned_attachment_without_text(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    attachment_path = tmp_path / "attachments" / "avibe" / "receipt.pdf"
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_bytes(b"pdf")
    controller = _controller()
    context = _context("avibe", user_id="local")
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            local_path=str(attachment_path),
        )
    ]

    asyncio.run(controller.capture_user_memory(context, "", "stable-session"))

    request = controller.memory_module.accepted[0]
    assert request.text == ""
    assert request.attachments[0].kind == "pdf"
    assert request.attachments[0].name == "receipt.pdf"
    assert request.attachments[0].uri == attachment_path.as_uri()


def test_im_attachments_remain_out_of_scope() -> None:
    controller = _controller()
    context = _context("slack")
    context.files = [object()]

    asyncio.run(controller.capture_user_memory(context, "ordinary text", "stable-session"))

    assert controller.memory_module.accepted == []


def test_im_adapters_normalize_native_ordinary_text_facts() -> None:
    discord_message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        edited_at=None,
        attachments=[],
        embeds=[],
        flags=SimpleNamespace(forwarded=False),
        message_snapshots=(),
        is_system=lambda: False,
    )
    assert is_ordinary_discord_text(discord_message, None) is True
    discord_message.flags.forwarded = True
    assert is_ordinary_discord_text(discord_message, None) is False

    assert is_ordinary_slack_text({"text": "hello"}, None) is True
    assert is_ordinary_slack_text({"text": "hello", "subtype": "message_changed"}, None) is False

    assert is_ordinary_telegram_text({"from": {"is_bot": False}, "text": "hello"}, []) is True
    assert is_ordinary_telegram_text({"from": {"is_bot": False}, "forward_origin": {"type": "user"}}, []) is False

    feishu_event = {"sender": {"sender_type": "user"}, "message": {"message_type": "text"}}
    assert is_ordinary_feishu_text(feishu_event, None, shared_text=None) is True
    feishu_event["message"]["message_type"] = "post"
    assert is_ordinary_feishu_text(feishu_event, None, shared_text=None) is False

    assert is_ordinary_wechat_text({"item_list": [{"type": "TEXT"}]}, None) is True
    assert is_ordinary_wechat_text({"item_list": [{"type": 1}, {"type": 2}]}, None) is False
    assert is_ordinary_wechat_text({"item_list": [{"type": 1, "ref_msg": {"title": "quoted"}}]}, None) is False


def _slack_dm_event(**overrides) -> dict:
    """Return a real-shaped Slack ``message`` event for a DM typed in a client.

    Modern Slack clients always attach the composer's ``rich_text`` block, so a
    payload without ``blocks`` does not represent what production delivers.
    """

    event = {
        "client_msg_id": "3d0a24a2-1c1a-4b6f-9f43-8f9d0d9a1111",
        "type": "message",
        "text": "ship the memory fix today",
        "user": "U04ABCDEF",
        "ts": "1753420800.123456",
        "team": "T04ABCDEF",
        "channel": "D04ABCDEF",
        "channel_type": "im",
        "event_ts": "1753420800.123456",
        "blocks": [
            {
                "type": "rich_text",
                "block_id": "Xq2",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "ship the memory fix today"}],
                    }
                ],
            }
        ],
    }
    event.update(overrides)
    return event


def test_slack_composer_rich_text_dm_is_ordinary_human_text() -> None:
    assert is_ordinary_slack_text(_slack_dm_event(), None) is True

    # Mentions, links, emoji, styled runs, lists, quotes, and code blocks are all
    # plain composer output for a human-typed DM.
    decorated = _slack_dm_event(
        text="<@U04TEAMMATE> see <https://example.com|docs> :tada:",
        blocks=[
            {
                "type": "rich_text",
                "block_id": "d1F",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U04TEAMMATE"},
                            {"type": "text", "text": " see "},
                            {"type": "link", "url": "https://example.com", "text": "docs"},
                            {"type": "text", "text": " now", "style": {"bold": True}},
                            {"type": "emoji", "name": "tada", "unicode": "1f389"},
                        ],
                    },
                    {
                        "type": "rich_text_list",
                        "style": "bullet",
                        "indent": 0,
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "first"}],
                            }
                        ],
                    },
                    {
                        "type": "rich_text_quote",
                        "elements": [{"type": "text", "text": "quoted line"}],
                    },
                    {
                        "type": "rich_text_preformatted",
                        "elements": [{"type": "text", "text": "uv run pytest"}],
                    },
                ],
            }
        ],
    )
    assert is_ordinary_slack_text(decorated, None) is True


def test_slack_non_text_block_payloads_are_not_ordinary() -> None:
    # Image upload in a DM: Slack sends ``file_share`` with a files array.
    upload = _slack_dm_event(
        subtype="file_share",
        text="look at this",
        upload=False,
        display_as_bot=False,
        files=[
            {
                "id": "F04FILEID",
                "name": "screenshot.png",
                "mimetype": "image/png",
                "filetype": "png",
                "url_private_download": "https://files.slack.com/files-pri/T04-F04/screenshot.png",
            }
        ],
    )
    assert is_ordinary_slack_text(upload, None) is False
    extracted_upload = [
        FileAttachment(
            name="screenshot.png",
            mimetype="image/png",
            url="https://files.slack.com/files-pri/T04-F04/screenshot.png",
        )
    ]
    assert is_ordinary_slack_attachment(upload, extracted_upload) is True

    # Forwarded / shared message: composer rich text PLUS a share attachment.
    forwarded = _slack_dm_event(
        text="fyi",
        attachments=[
            {
                "id": 1,
                "is_share": True,
                "author_name": "Teammate",
                "channel_id": "C04SOURCE",
                "ts": "1753410000.000100",
                "text": "the original message",
            }
        ],
    )
    assert is_ordinary_slack_text(forwarded, None) is False
    assert is_ordinary_slack_attachment(forwarded, extracted_upload) is False

    # App-authored layout blocks are not composer output, even without ``bot_id``.
    app_blocks = _slack_dm_event(
        text="Deployment finished",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "Deployment finished"}},
            {"type": "image", "image_url": "https://example.com/chart.png", "alt_text": "chart"},
        ],
    )
    assert is_ordinary_slack_text(app_blocks, None) is False
    assert is_ordinary_slack_attachment(app_blocks, extracted_upload) is False

    # An unrecognized node inside rich text fails closed.
    unknown_element = _slack_dm_event(
        blocks=[
            {
                "type": "rich_text",
                "block_id": "u1",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "see "},
                            {"type": "image", "image_url": "https://example.com/inline.png"},
                        ],
                    }
                ],
            }
        ],
    )
    assert is_ordinary_slack_text(unknown_element, None) is False

    # Only an absent blocks value or a real empty list is the legacy plain-text
    # shape. Falsy values of any other type are malformed and fail closed.
    assert is_ordinary_slack_text(_slack_dm_event(blocks=[]), None) is True
    for malformed_blocks in ({}, "", 0, False):
        assert is_ordinary_slack_text(_slack_dm_event(blocks=malformed_blocks), None) is False

    # Edits and bot/self events stay excluded regardless of block content.
    assert is_ordinary_slack_text(_slack_dm_event(subtype="message_changed"), None) is False
    assert is_ordinary_slack_text(_slack_dm_event(subtype="future_system_event"), None) is False
    assert (
        is_ordinary_slack_text(
            _slack_dm_event(edited={"user": "U04ABCDEF", "ts": "1753420900.000000"}),
            None,
        )
        is False
    )
    assert is_ordinary_slack_text(_slack_dm_event(bot_id="B04BOTID"), None) is False
    assert (
        is_ordinary_slack_text(
            _slack_dm_event(),
            [
                FileAttachment(
                    name="notes.txt",
                    mimetype="text/plain",
                    url="https://files.slack.com/notes.txt",
                )
            ],
        )
        is False
    )


def test_slack_manifest_has_no_native_memory_command() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "vibe" / "templates" / "slack_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = manifest["features"].get("slash_commands", [])
    assert all(command.get("command") != "/memory" for command in commands)


def _memory_config(
    *,
    enabled: bool = True,
    llm_model: str = "chat",
    embedding_model: str = "embed",
    recovery_intent: str | None = None,
) -> MemoryConfig:
    return MemoryConfig(
        enabled=enabled,
        recovery_intent=recovery_intent,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                base_url="https://llm.example.test/v1",
                model=llm_model,
                api_key="llm-key",
            ),
            embedding=MemoryEndpointConfig(
                base_url="https://embed.example.test/v1",
                model=embedding_model,
                api_key="embed-key",
            ),
        ),
    )


class _FailingReconcileRuntime:
    """A runtime whose reconciliation always fails, as when the provider is down."""

    def __init__(self) -> None:
        self.calls = 0
        self.module = object()

    async def reconcile(self, _config):
        self.calls += 1
        return {"ok": False, "error": "memory_sidecar_unavailable"}


def _reconcile_controller(current: MemoryConfig) -> Controller:
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=current)
    controller.memory_runtime = _FailingReconcileRuntime()
    controller.memory_module = None
    return controller


def test_a_failed_memory_reconciliation_does_not_adopt_the_candidate_config() -> None:
    controller = _reconcile_controller(_memory_config())

    result = asyncio.run(controller.reconcile_memory(_memory_config(enabled=False)))

    assert result["ok"] is False
    assert controller.memory_runtime.calls == 1
    assert controller.config.memory.enabled is True


def test_controller_adopts_an_independent_settled_memory_snapshot() -> None:
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=_memory_config(recovery_intent="rebuild"))
    settled = _memory_config(enabled=False)

    controller._adopt_settled_memory_config(settled)

    assert controller.config.memory == settled
    assert controller.config.memory is not settled


def test_settling_a_pending_embedding_change_compares_embedding_identity():
    """Settlement ignores its marker but never a vector-space identity change."""

    from core.memory.runtime import _same_embedding_identity

    persisted = _memory_config(recovery_intent="rebuild")
    candidate = _memory_config()

    assert _same_embedding_identity(persisted, candidate) is True
    assert _same_embedding_identity(persisted, _memory_config(llm_model="chat-2")) is True
    assert _same_embedding_identity(
        persisted,
        _memory_config(embedding_model="embed-v2"),
    ) is False

"""Contracts for the shared, per-user Memory capture boundary."""

from __future__ import annotations

import ast
import asyncio
import gc
import json
import threading
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig
from core.controller import Controller
from avibe_memory import (
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureRequest,
    CaptureSkipped,
    MemoryModule,
)
from avibe_memory.admission import InboundTurnFacts
from avibe_memory.everos import FakeMemoryProvider
from avibe_memory.store import MemoryStore
from core.memory_adapter import EnabledMemoryAdapter, TurnAccepted
from modules.im.base import FileAttachment, MessageContext
from modules.im.message_facts import (
    is_original_human_discord_attachment,
    is_original_human_discord_text,
    is_original_human_feishu_attachment,
    is_original_human_feishu_text,
    is_original_human_slack_attachment,
    is_original_human_slack_text,
    is_original_human_telegram_attachment,
    is_original_human_telegram_text,
    is_original_human_wechat_attachment,
    is_original_human_wechat_text,
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
        self.attachment_generation: int | None = 1
        self.barrier_offers = 0
        self.barrier_sessions: list[str | None] = []
        self.barrier_error: Exception | None = None

    def principal_for_user_key(self, user_key: str) -> str:
        suffix = "1" if user_key.endswith("user-1") else "2"
        return f"u-{suffix * 32}"

    def project_for_workdir(self, workdir: str) -> str:
        assert workdir == "/tmp/project"
        return PROJECT

    async def attachment_capture_status(self) -> str:
        return self.attachment_status

    def attachment_capture_config_generation(self) -> int | None:
        return self.attachment_generation

    def offer_barrier(self, raw_session_id: str) -> str:
        self.barrier_offers += 1
        self.barrier_sessions.append(raw_session_id)
        if self.barrier_error is not None:
            raise self.barrier_error
        return "queued"


class _CaptureModule:
    def __init__(self) -> None:
        self.accepted = []
        self.seen: set[str] = set()
        self.reservations: list[object] = []

    def reserve_capture_admission(self, **_scope):
        reservation = object()
        self.reservations.append(reservation)
        return reservation

    def cancel_capture_reservation(self, _reservation):
        return None

    @asynccontextmanager
    async def capture_admission(self, **_scope):
        yield object()

    async def capture(self, request, **_options):
        if request.source_message_id in self.seen:
            return CaptureDuplicate()
        self.seen.add(request.source_message_id)
        self.accepted.append(request)
        return CaptureAccepted(
            captured_attachment_count=len(request.attachments)
        )


def _controller(*, user=None):
    user = user or SimpleNamespace(enabled=True, is_admin=False)
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(
        memory=SimpleNamespace(
            enabled=True,
            processing=MemoryProcessingConfig(
                multimodal=MemoryEndpointConfig(
                    base_url="https://multimodal.test/v1",
                    model="vision-test",
                    api_key="secret",
                )
            ),
        )
    )
    controller.platform_settings_managers = {
        platform: _Manager(user)
        for platform in ("slack", "discord", "telegram", "feishu", "wechat", "lark")
    }
    controller.memory_module = _CaptureModule()
    controller.memory_runtime = _Runtime(controller.memory_module)
    controller.memory_adapter = EnabledMemoryAdapter(controller)
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
        is_original_human_text=ordinary,
    )


def test_enabled_adapter_retains_memory_capture_task_until_completion() -> None:
    async def run() -> None:
        release = asyncio.Event()
        controller = SimpleNamespace(
            capture_user_memory=lambda *_args, **_kwargs: release.wait(),
        )
        adapter = EnabledMemoryAdapter(controller)
        adapter.offer(TurnAccepted(_context("slack"), "remember", "session", 0))
        (task,) = adapter.capture_tasks
        reference = weakref.ref(task)
        del task
        gc.collect()

        assert reference() is not None
        assert len(adapter.capture_tasks) == 1

        release.set()
        await reference()
        await asyncio.sleep(0)
        assert adapter.capture_tasks == set()

    asyncio.run(run())


def test_enabled_adapter_cancels_and_joins_memory_capture_tasks() -> None:
    async def run() -> None:
        started = asyncio.Event()
        retained_lease = Mock()
        attachment_lease = Mock()
        attachment_lease.retain.return_value = retained_lease

        async def capture(*_args, **_kwargs) -> None:
            started.set()
            await asyncio.Event().wait()

        controller = SimpleNamespace(
            capture_user_memory=capture,
            reserve_memory_attachment_capture=lambda *_args: SimpleNamespace(
                config_generation=1,
                release=Mock(),
            ),
        )
        adapter = EnabledMemoryAdapter(controller)
        adapter.offer(
            TurnAccepted(
                _context("slack"),
                "remember",
                "session",
                0,
                attachment_lease,
            )
        )
        (task,) = adapter.capture_tasks
        await started.wait()

        await adapter.cancel_memory_capture_tasks()

        assert task.cancelled()
        assert adapter.capture_tasks == set()
        retained_lease.release.assert_called_once_with()

    asyncio.run(run())


@pytest.mark.parametrize("platform", ["slack", "discord", "telegram", "feishu", "wechat"])
def test_capture_admits_every_enabled_bound_dm_user(platform: str) -> None:
    controller = _controller(user=SimpleNamespace(enabled=True, is_admin=False))

    assert controller.memory_capture_admitted(_context(platform)) is True
    assert controller.memory_capture_admitted(_context(platform, is_dm=False)) is False


@pytest.mark.parametrize("status", ["ready", "unavailable"])
def test_slack_memory_lease_retention_is_local_and_ignores_runtime_health(
    status: str,
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
    context.is_original_human_attachment = True

    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )

    assert reservation is not None
    assert reservation.config_generation == 1


@pytest.mark.parametrize("generation", [None, True, -1, "1"])
def test_slack_memory_reservation_normalizes_invalid_multimodal_generation(
    generation: object,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    controller = _controller()
    controller.memory_runtime.attachment_generation = generation
    context = _context("slack", ordinary=False)
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            url="https://files.slack.test/private",
        )
    ]
    context.is_original_human_attachment = True

    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )

    assert reservation is not None
    assert reservation.config_generation is None


def test_attachment_capacity_is_reserved_before_session_or_lease_work() -> None:
    controller = _controller()
    reserve_admission = Mock()
    controller.memory_module.reserve_capture_capacity = lambda: "full"
    controller.memory_module.reserve_capture_admission = reserve_admission
    context = _context("slack", ordinary=False)
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            url="https://files.slack.test/private",
        )
    ]
    context.is_original_human_attachment = True

    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )

    assert reservation is not None
    assert reservation.capacity_full is True
    reserve_admission.assert_not_called()
    reservation.release()


@pytest.mark.parametrize("outcome", ["unavailable", "disabled"])
def test_capture_capacity_terminal_outcomes_are_propagated(outcome: str) -> None:
    controller = _controller()
    controller.memory_module.reserve_capture_capacity = lambda: outcome

    reservation = controller.reserve_memory_capture_capacity(
        _context("slack"),
        "remember this",
        "stable-session",
    )

    assert reservation is not None
    assert reservation.capacity_outcome == outcome
    assert reservation.capacity_blocked is True
    reservation.release()


def test_attachment_reservation_failure_preserves_caption_as_text_only() -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    controller = _controller()
    controller.memory_module.reserve_capture_admission = Mock(
        side_effect=RuntimeError("reservation unavailable")
    )
    context = _context("slack", ordinary=False)
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            url="https://files.slack.test/private",
        )
    ]
    context.is_original_human_attachment = True

    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )
    assert reservation is None

    asyncio.run(
        controller.capture_user_memory(
            context,
            "keep the caption",
            "stable-session",
            attachment_reservation=reservation,
        )
    )

    assert len(controller.memory_module.accepted) == 1
    request = controller.memory_module.accepted[0]
    assert request.text == "keep the caption"
    assert request.attachments == ()


def test_retained_lease_failure_preserves_reserved_caption_as_text_only() -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    controller = _controller()
    context = _context("slack", ordinary=False)
    context.files = [
        FileAttachment(
            name="receipt.pdf",
            mimetype="application/pdf",
            url="https://files.slack.test/private",
        )
    ]
    context.is_original_human_attachment = True
    reservation = controller.reserve_memory_attachment_capture(
        context,
        "stable-session",
    )
    assert reservation is not None

    asyncio.run(
        controller.capture_user_memory(
            context,
            "keep the caption",
            "stable-session",
            attachment_reservation=reservation,
            attachment_text_only=True,
        )
    )

    assert len(controller.memory_module.accepted) == 1
    request = controller.memory_module.accepted[0]
    assert request.text == "keep the caption"
    assert request.attachments == ()


def test_slack_without_multimodal_opt_in_skips_live_health_read() -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    async def run() -> None:
        controller = _controller()
        store = MemoryStore()
        provider = FakeMemoryProvider()
        module = MemoryModule(store, provider, enabled=True)
        controller.memory_module = module
        controller.memory_runtime = _Runtime(module)
        controller.memory_runtime.attachment_generation = None
        health_reads = 0

        async def attachment_capture_status() -> str:
            nonlocal health_reads
            health_reads += 1
            await asyncio.Event().wait()
            return "ready"

        controller.memory_runtime.attachment_capture_status = attachment_capture_status
        context = _context("slack", ordinary=False)
        context.files = [
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
            )
        ]
        context.is_original_human_attachment = True
        reservation = controller.reserve_memory_attachment_capture(
            context,
            "stable-session",
        )
        assert reservation is not None
        assert reservation.config_generation is None

        await asyncio.wait_for(
            controller.capture_user_memory(
                context,
                "keep the caption",
                "stable-session",
                attachment_reservation=reservation,
            ),
            timeout=1.0,
        )

        assert health_reads == 0
        await module.wait_writer_idle_for_tests()
        assert len(provider.captures) == 1
        assert provider.captures[0].text == "keep the caption"
        assert provider.captures[0].attachments == ()

        reset_ran = False

        async def reset() -> None:
            nonlocal reset_ran
            reset_ran = True

        module.offer_barrier("stable-session")
        await asyncio.wait_for(reset(), timeout=1.5)
        assert reset_ran is True
        await module.close_writer()

    asyncio.run(run())


def test_configured_attachment_capture_skips_live_health_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-001."""

    async def run() -> None:
        controller = _controller()
        health_reads = 0

        async def attachment_capture_status() -> str:
            nonlocal health_reads
            health_reads += 1
            await asyncio.Event().wait()
            return "ready"

        controller.memory_runtime.attachment_capture_status = attachment_capture_status
        attachment = CaptureAttachment(
            kind="pdf",
            name="receipt.pdf",
            uri="file:///leased/receipt.pdf",
            ext="pdf",
        )
        monkeypatch.setattr(
            "avibe_memory.admission.select_memory_attachments",
            lambda _lease: SimpleNamespace(attachments=(attachment,), skipped=()),
        )
        context = _context("slack", ordinary=False)
        context.files = [
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
            )
        ]
        context.is_original_human_attachment = True
        reservation = controller.reserve_memory_attachment_capture(
            context,
            "stable-session",
        )
        assert reservation is not None
        assert reservation.config_generation == 1

        await asyncio.wait_for(
            controller.capture_user_memory(
                context,
                "remember this",
                "stable-session",
                attachment_lease=object(),
                attachment_reservation=reservation,
            ),
            timeout=1.0,
        )

        assert health_reads == 0
        assert len(controller.memory_module.accepted) == 1
        assert controller.memory_module.accepted[0].attachments == (attachment,)

    asyncio.run(run())


def test_configured_attachment_capture_does_not_block_session_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    async def run() -> None:
        controller = _controller()
        store = MemoryStore()
        module = MemoryModule(store, FakeMemoryProvider(), enabled=True)
        controller.memory_module = module
        controller.memory_runtime = _Runtime(module)
        health_reads = 0

        async def attachment_capture_status() -> str:
            nonlocal health_reads
            health_reads += 1
            await asyncio.Event().wait()
            return "ready"

        controller.memory_runtime.attachment_capture_status = attachment_capture_status
        monkeypatch.setattr(
            "avibe_memory.admission.select_memory_attachments",
            lambda _lease: SimpleNamespace(attachments=(), skipped=()),
        )
        context = _context("slack", ordinary=False)
        context.files = [
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
            )
        ]
        context.is_original_human_attachment = True
        reservation = controller.reserve_memory_attachment_capture(
            context,
            "stable-session",
        )
        assert reservation is not None
        capture = asyncio.create_task(
            controller.capture_user_memory(
                context,
                "keep the caption",
                "stable-session",
                attachment_lease=object(),
                attachment_reservation=reservation,
            )
        )
        reset_ran = False

        async def reset() -> None:
            nonlocal reset_ran
            reset_ran = True

        module.offer_barrier("stable-session")
        await asyncio.wait_for(reset(), timeout=0.75)
        await asyncio.wait_for(capture, timeout=1.0)

        assert reset_ran is True
        assert health_reads == 0

    asyncio.run(run())


def test_attachment_selection_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-001."""

    async def run() -> None:
        controller = _controller()
        event_loop_thread = threading.get_ident()
        binding_threads: list[int] = []
        settings_store = controller.platform_settings_managers["slack"].get_store()
        original_maybe_reload = settings_store.maybe_reload

        def observe_settings_access() -> None:
            binding_threads.append(threading.get_ident())
            original_maybe_reload()

        monkeypatch.setattr(settings_store, "maybe_reload", observe_settings_access)
        attachment = CaptureAttachment(
            kind="pdf",
            name="receipt.pdf",
            uri="file:///leased/receipt.pdf",
            ext="pdf",
        )
        selector_threads: list[int] = []
        release_selection = threading.Event()
        released_by_event_loop: list[bool] = []

        def select_attachments(_lease):
            selector_threads.append(threading.get_ident())
            released_by_event_loop.append(release_selection.wait(timeout=1.0))
            return SimpleNamespace(attachments=(attachment,), skipped=())

        monkeypatch.setattr(
            "avibe_memory.admission.select_memory_attachments",
            select_attachments,
        )
        context = _context("slack", ordinary=False)
        context.files = [
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
            )
        ]
        context.is_original_human_attachment = True
        reservation = controller.reserve_memory_attachment_capture(
            context,
            "stable-session",
        )
        assert reservation is not None

        asyncio.get_running_loop().call_later(0.05, release_selection.set)
        await asyncio.wait_for(
            controller.capture_user_memory(
                context,
                "remember this",
                "stable-session",
                attachment_lease=object(),
                attachment_reservation=reservation,
            ),
            timeout=2.0,
        )

        assert binding_threads and set(binding_threads) == {event_loop_thread}
        assert selector_threads and selector_threads[0] != event_loop_thread
        assert released_by_event_loop == [True]

    asyncio.run(run())


def test_attachment_capture_fails_closed_when_config_generation_changes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    caplog.set_level("INFO", logger="avibe_memory.admission")

    async def run() -> None:
        controller = _controller()
        attachment = CaptureAttachment(
            kind="pdf",
            name="receipt.pdf",
            uri="file:///leased/receipt.pdf",
            ext="pdf",
        )
        def select_attachments(_lease):
            controller.memory_runtime.attachment_generation = 2
            return SimpleNamespace(attachments=(attachment,), skipped=())

        monkeypatch.setattr(
            "avibe_memory.admission.select_memory_attachments",
            select_attachments,
        )
        context = _context("slack", ordinary=False)
        context.files = [
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
            )
        ]
        context.is_original_human_attachment = True
        reservation = controller.reserve_memory_attachment_capture(
            context,
            "stable-session",
        )
        assert reservation is not None

        await controller.capture_user_memory(
            context,
            "keep the caption",
            "stable-session",
            attachment_lease=object(),
            attachment_reservation=reservation,
        )

        assert len(controller.memory_module.accepted) == 1
        request = controller.memory_module.accepted[0]
        assert request.text == "keep the caption"
        assert request.attachments == ()
        assert request.attachment_config_generation is None

    asyncio.run(run())
    records = [
        record
        for record in caplog.records
        if record.message.startswith("memory_attachment_capture ")
    ]
    assert len(records) == 1
    assert "platform=slack total=1 captured=0 dropped=1" in records[0].getMessage()


def test_downstream_capture_unavailability_does_not_change_ingress_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-003."""

    async def run() -> None:
        controller = _controller()
        attachment = CaptureAttachment(
            kind="pdf",
            name="receipt.pdf",
            uri="file:///leased/receipt.pdf",
            ext="pdf",
        )
        monkeypatch.setattr(
            "avibe_memory.admission.select_memory_attachments",
            lambda _lease: SimpleNamespace(attachments=(attachment,), skipped=()),
        )
        captured_requests: list[CaptureRequest] = []

        async def unavailable_capture(request, **_options):
            captured_requests.append(request)
            return CaptureSkipped(reason="memory_operation_in_progress")

        controller.memory_module.capture = unavailable_capture
        controller.memory_runtime.attachment_status = "unavailable"
        context = _context("slack", ordinary=False)
        context.files = [
            FileAttachment(
                name="receipt.pdf",
                mimetype="application/pdf",
                url="https://files.slack.test/private",
            )
        ]
        context.is_original_human_attachment = True
        reservation = controller.reserve_memory_attachment_capture(
            context,
            "stable-session",
        )
        assert reservation is not None

        await controller.capture_user_memory(
            context,
            "keep the caption",
            "stable-session",
            attachment_lease=object(),
            attachment_reservation=reservation,
        )

        assert len(captured_requests) == 1
        assert captured_requests[0].attachments == (attachment,)

    asyncio.run(run())


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
    """MEMORY-SEARCH-015: capture identity stays caller-scoped and deduplicated."""

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


def test_archive_session_commits_then_offers_session_archived(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scenario: MEMORY-INDEP-007."""

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
    barrier_started_after_archive: list[bool] = []

    class RecordingAdapter:
        def offer(self, event: object) -> None:
            with engine.connect() as conn:
                barrier_started_after_archive.append(
                    workbench_sessions_service.get_session(conn, session_id)["status"]
                    == "archived"
                )
            assert type(event).__name__ == "SessionArchived"
            assert getattr(event, "session_id") == session_id

    controller = _controller()
    controller.memory_adapter = RecordingAdapter()
    from core.session_turns import SessionTurnManager

    controller.session_turns = SessionTurnManager()
    controller._memory_scopes_by_session = {
        session_id: (principal_id, PROJECT),
    }
    controller._memory_cli_facts_by_session = {
        session_id: InboundTurnFacts(
            platform="avibe",
            user_id="workbench-user",
            message_id="message-1",
            session_id=session_id,
            memory_enabled=True,
        )
    }
    async def run() -> None:
        result = await controller.archive_session(
            session_id,
            deadline_seconds=2.0,
        )
        assert result["status"] == "archived"
        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "archived"
        await asyncio.sleep(0)
        assert barrier_started_after_archive == [True]

    try:
        asyncio.run(run())
    finally:
        engine.dispose()


def test_archive_memory_barrier_failure_still_releases_authorization() -> None:
    """Scenario: MEMORY-INDEP-007."""

    session_id = "ses-archived-with-barrier-failure"
    controller = _controller()
    controller.memory_runtime.barrier_error = RuntimeError("writer unavailable")
    controller._memory_scopes_by_session = {
        session_id: ("u-" + ("2" * 32), PROJECT),
    }
    controller._memory_cli_facts_by_session = {session_id: object()}

    controller._offer_best_effort_session_archived(session_id)

    assert controller.memory_runtime.barrier_offers == 1
    assert session_id not in controller._memory_scopes_by_session
    assert session_id not in controller._memory_cli_facts_by_session


def test_archive_session_offers_barrier_when_commit_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            title="Archive through cancellation",
        )["id"]

    barrier_offered = asyncio.Event()
    commit_entered = threading.Event()
    release_commit = threading.Event()

    class LifecycleRuntime(_Runtime):
        def offer_barrier(self, raw_session_id: str) -> str:
            barrier_offered.set()
            return super().offer_barrier(raw_session_id)

    from core.services import sessions as sessions_service

    original_archive = sessions_service.archive_session

    def archive_under_test(conn, actual_session_id):
        commit_entered.set()
        assert release_commit.wait(timeout=2.0)
        return original_archive(conn, actual_session_id)

    monkeypatch.setattr(sessions_service, "archive_session", archive_under_test)

    controller = _controller()
    controller.memory_runtime = LifecycleRuntime(controller.memory_module)
    controller._memory_scopes_by_session = {
        session_id: ("u-" + ("2" * 32), PROJECT),
    }
    controller._memory_cli_facts_by_session = {}

    async def run() -> None:
        archive = asyncio.create_task(controller.archive_session(session_id))
        deadline = asyncio.get_running_loop().time() + 2.0
        while not commit_entered.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("archive commit did not start")
            await asyncio.sleep(0.01)
        archive.cancel()
        await asyncio.sleep(0)
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await archive
        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "archived"
        await asyncio.wait_for(barrier_offered.wait(), timeout=1.0)
        assert controller.memory_runtime.barrier_offers == 1
        assert session_id not in controller._memory_scopes_by_session

    try:
        asyncio.run(run())
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["missing", "reserved", "archived"])
def test_archive_session_preflight_skips_lifecycle_event(
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
        return await controller.archive_session(session_id)

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

    assert controller.memory_runtime.barrier_sessions == []
    assert controller.memory_runtime.barrier_offers == 0


def test_archive_session_commits_when_barrier_offer_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            title="Archive despite Memory maintenance",
        )["id"]

    controller = _controller()
    controller.memory_runtime.barrier_error = RuntimeError("writer unavailable")
    controller._memory_scopes_by_session = {}
    controller._memory_cli_facts_by_session = {}

    async def run() -> dict[str, object]:
        result = await controller.archive_session(session_id)
        await asyncio.sleep(0)
        return result

    try:
        result = asyncio.run(run())
        assert result["status"] == "archived"
        with engine.connect() as conn:
            assert workbench_sessions_service.get_session(conn, session_id)["status"] == "archived"
        assert controller.memory_runtime.barrier_offers == 1
    finally:
        engine.dispose()


def test_retired_memory_delivery_symbols_have_no_product_callers() -> None:
    """Keep the removed durable-delivery surface out of production code."""

    removed = {
        "final_" + "flush",
        "drain_" + "memory_capture_tasks",
        "enqueue_" + "request",
        "list_" + "queue_rows",
        "resolve_current_session_" + "scope",
        "resolve_current_session_" + "scopes",
    }
    offenders: list[str] = []
    for root_name in ("core", "modules", "vibe"):
        for source_path in sorted((REPO_ROOT / root_name).rglob("*.py")):
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
                offenders.append(f"{source_path.relative_to(REPO_ROOT)}:{node.lineno}: {symbol}")

    assert offenders == []


def test_workbench_capture_requires_resolved_identity_and_uses_row_id() -> None:
    controller = _controller()
    context = _context("avibe", user_id="local", author_id="local")

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
    context = _context("avibe", user_id="local", author_id="local")
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
    assert is_original_human_discord_text(discord_message, None) is True
    discord_message.flags.forwarded = True
    assert is_original_human_discord_text(discord_message, None) is False

    assert is_original_human_slack_text({"text": "hello"}, None) is True
    assert is_original_human_slack_text({"text": "hello", "subtype": "message_changed"}, None) is False

    assert is_original_human_telegram_text({"from": {"is_bot": False}, "text": "hello"}, []) is True
    assert is_original_human_telegram_text({"from": {"is_bot": False}, "forward_origin": {"type": "user"}}, []) is False

    feishu_event = {"sender": {"sender_type": "user"}, "message": {"message_type": "text"}}
    assert is_original_human_feishu_text(feishu_event, None, shared_text=None) is True
    feishu_event["message"]["message_type"] = "post"
    assert is_original_human_feishu_text(feishu_event, None, shared_text=None) is False

    assert is_original_human_wechat_text({"item_list": [{"type": "TEXT"}]}, None) is True
    assert is_original_human_wechat_text({"item_list": [{"type": 1}, {"type": 2}]}, None) is False
    assert is_original_human_wechat_text({"item_list": [{"type": 1, "ref_msg": {"title": "quoted"}}]}, None) is False


def test_im_adapters_normalize_native_ordinary_attachment_facts() -> None:
    """MEMORY-IM-ATTACH-005..008: adapters publish only native ordinary shares."""

    discord_file = FileAttachment(
        name="diagram.png",
        mimetype="image/png",
        url="https://cdn.discord.test/diagram.png",
    )
    discord_message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        edited_at=None,
        attachments=[object()],
        embeds=[],
        components=[],
        stickers=[],
        sticker_items=[],
        webhook_id=None,
        flags=SimpleNamespace(forwarded=False),
        message_snapshots=(),
        is_system=lambda: False,
    )
    assert is_original_human_discord_attachment(discord_message, [discord_file]) is True
    for field, value in (
        ("embeds", [object()]),
        ("components", [object()]),
        ("sticker_items", [object()]),
        ("webhook_id", "webhook-1"),
        ("message_snapshots", [object()]),
    ):
        setattr(discord_message, field, value)
        assert is_original_human_discord_attachment(discord_message, [discord_file]) is False
        setattr(discord_message, field, None)
    discord_message.author.bot = True
    assert is_original_human_discord_attachment(discord_message, [discord_file]) is False

    telegram_file = FileAttachment(
        name="telegram-file.bin",
        mimetype="application/octet-stream",
        url="native-file",
    )
    for native_field, native_value in (
        ("document", {"file_id": "document"}),
        ("photo", [{"file_id": "small"}, {"file_id": "large"}]),
        ("voice", {"file_id": "voice"}),
        ("audio", {"file_id": "audio"}),
    ):
        telegram_message = {
            "from": {"id": 42, "is_bot": False},
            "media_group_id": "album-1",
            native_field: native_value,
        }
        assert is_original_human_telegram_attachment(telegram_message, [telegram_file]) is True
    for rejected_field in ("video", "animation", "sticker"):
        telegram_message = {
            "from": {"id": 42, "is_bot": False},
            "document": {"file_id": "document"},
            rejected_field: {"file_id": "decorated"},
        }
        assert is_original_human_telegram_attachment(telegram_message, [telegram_file]) is False
    telegram_message = {
        "from": {"id": 42, "is_bot": False},
        "document": {"file_id": "document"},
        "forward_origin": {"type": "user"},
    }
    assert is_original_human_telegram_attachment(telegram_message, [telegram_file]) is False

    feishu_file = FileAttachment(
        name="report.pdf",
        mimetype="application/octet-stream",
        url="https://open.feishu.test/report.pdf",
    )
    feishu_event = {
        "sender": {"sender_type": "user"},
        "message": {"message_type": "file"},
    }
    assert (
        is_original_human_feishu_attachment(
            feishu_event,
            {"file_key": "file-key", "file_name": "report.pdf"},
            [feishu_file],
            shared_text=None,
        )
        is True
    )
    assert (
        is_original_human_feishu_attachment(
            feishu_event,
            {"file_key": ""},
            [feishu_file],
            shared_text=None,
        )
        is False
    )
    feishu_event["message"]["message_type"] = "image"
    assert (
        is_original_human_feishu_attachment(
            feishu_event,
            {"image_key": "image-key"},
            [feishu_file],
            shared_text=None,
        )
        is True
    )
    feishu_event["message"]["message_type"] = "media"
    assert (
        is_original_human_feishu_attachment(
            feishu_event,
            {"file_key": "media-key"},
            [feishu_file],
            shared_text=None,
        )
        is False
    )

    wechat_files = [
        FileAttachment(name="voice.silk", mimetype="audio/silk", url="voice-query"),
        FileAttachment(name="video.mp4", mimetype="video/mp4", url="video-query"),
    ]
    wechat_message = {
        "from_user_id": "user-1",
        "item_list": [
            {"type": 1, "text_item": {"text": "remember these"}},
            {
                "type": 3,
                "voice_item": {"media": {"encrypt_query_param": "voice-query"}},
            },
            {
                "type": 5,
                "video_item": {"media": {"encrypt_query_param": "video-query"}},
            },
        ],
    }
    assert is_original_human_wechat_attachment(wechat_message, wechat_files) is True
    wechat_message["item_list"][0]["ref_msg"] = {"title": "forwarded"}
    assert is_original_human_wechat_attachment(wechat_message, wechat_files) is False
    for item_type, media_field in (
        (2, "image_item"),
        (3, "voice_item"),
        (4, "file_item"),
        (5, "video_item"),
    ):
        direct_message = {
            "item_list": [
                {
                    "type": item_type,
                    media_field: {
                        "media": {"encrypt_query_param": "native-media"}
                    },
                }
            ]
        }
        assert is_original_human_wechat_attachment(direct_message, wechat_files) is True
        direct_message["item_list"][0][media_field]["media"][
            "encrypt_query_param"
        ] = ""
        assert is_original_human_wechat_attachment(direct_message, wechat_files) is False


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
    assert is_original_human_slack_text(_slack_dm_event(), None) is True

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
    assert is_original_human_slack_text(decorated, None) is True


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
    assert is_original_human_slack_text(upload, None) is False
    extracted_upload = [
        FileAttachment(
            name="screenshot.png",
            mimetype="image/png",
            url="https://files.slack.com/files-pri/T04-F04/screenshot.png",
        )
    ]
    assert is_original_human_slack_attachment(upload, extracted_upload) is True

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
    assert is_original_human_slack_text(forwarded, None) is False
    assert is_original_human_slack_attachment(forwarded, extracted_upload) is False

    # App-authored layout blocks are not composer output, even without ``bot_id``.
    app_blocks = _slack_dm_event(
        text="Deployment finished",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "Deployment finished"}},
            {"type": "image", "image_url": "https://example.com/chart.png", "alt_text": "chart"},
        ],
    )
    assert is_original_human_slack_text(app_blocks, None) is False
    assert is_original_human_slack_attachment(app_blocks, extracted_upload) is False

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
    assert is_original_human_slack_text(unknown_element, None) is False

    # Only an absent blocks value or a real empty list is the legacy plain-text
    # shape. Falsy values of any other type are malformed and fail closed.
    assert is_original_human_slack_text(_slack_dm_event(blocks=[]), None) is True
    for malformed_blocks in ({}, "", 0, False):
        assert is_original_human_slack_text(_slack_dm_event(blocks=malformed_blocks), None) is False

    # Edits and bot/self events stay excluded regardless of block content.
    assert is_original_human_slack_text(_slack_dm_event(subtype="message_changed"), None) is False
    assert is_original_human_slack_text(_slack_dm_event(subtype="future_system_event"), None) is False
    assert (
        is_original_human_slack_text(
            _slack_dm_event(edited={"user": "U04ABCDEF", "ts": "1753420900.000000"}),
            None,
        )
        is False
    )
    assert is_original_human_slack_text(_slack_dm_event(bot_id="B04BOTID"), None) is False
    assert (
        is_original_human_slack_text(
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
) -> MemoryConfig:
    return MemoryConfig(
        enabled=enabled,
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
    controller.config = SimpleNamespace(memory=_memory_config())
    settled = _memory_config(enabled=False)

    controller._adopt_settled_memory_config(settled)

    assert controller.config.memory == settled
    assert controller.config.memory is not settled

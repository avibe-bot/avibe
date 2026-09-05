from __future__ import annotations

import asyncio
import builtins
import os
import socket
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import avibe_memory.capture_adapter as capture_adapter_module
from avibe_memory.capture_adapter import EnabledMemoryAdapter
from avibe_memory.types import CaptureAccepted, CaptureAttachment
from core.memory_adapter import (
    DisabledMemoryAdapter,
    MemoryFile,
    SessionArchived,
    SessionReset,
    TurnAccepted,
    normalize_memory_sender_name,
)
from core.handlers.message_handler import memory_turn_event
from modules.im.base import FileAttachment, MessageContext


PRINCIPAL = "u-" + ("1" * 32)


class _Lease:
    def __init__(self) -> None:
        self.retained = 0
        self.released = 0

    def retain(self) -> _LeaseReference:
        self.retained += 1
        return _LeaseReference(self)


class _LeaseReference:
    def __init__(self, owner: _Lease) -> None:
        self.owner = owner
        self.active = True

    def release(self) -> None:
        if self.active:
            self.active = False
            self.owner.released += 1


class _Capacity:
    def __init__(self) -> None:
        self.active = True


class _Module:
    def __init__(self) -> None:
        self.capacity_outcome: object = None
        self.capacities: list[_Capacity] = []
        self.reservations: list[object] = []
        self.captures: list[object] = []
        self.barriers: list[str] = []
        self.capture_error: BaseException | None = None
        self.capture_started = asyncio.Event()
        self.capture_continue = asyncio.Event()
        self.capture_continue.set()

    def reserve_capture_capacity(self) -> object:
        if isinstance(self.capacity_outcome, str):
            return self.capacity_outcome
        capacity = _Capacity()
        self.capacities.append(capacity)
        return capacity

    def release_capture_capacity(self, reservation: object) -> None:
        if isinstance(reservation, _Capacity):
            reservation.active = False

    def reserve_capture_admission(self, **_scope: object) -> object:
        reservation = SimpleNamespace(active=True)
        self.reservations.append(reservation)
        return reservation

    def cancel_capture_reservation(self, reservation: object) -> None:
        reservation.active = False

    @asynccontextmanager
    async def capture_admission(self, **_options: object):
        admission = SimpleNamespace(release=Mock())
        yield admission

    async def capture(self, request, **_options: object) -> object:
        self.capture_started.set()
        await self.capture_continue.wait()
        if self.capture_error is not None:
            raise self.capture_error
        self.captures.append(request)
        return SimpleNamespace(status="accepted")

    def offer_barrier(self, session_id: str) -> object:
        self.barriers.append(session_id)
        return "queued"

    async def wait_writer_idle_for_tests(self, **_options: object) -> None:
        return None


class _Principals:
    def __init__(self, forbidden: bool = False) -> None:
        self.forbidden = forbidden
        self.calls = 0

    def principal_for_user_key(self, _user_key: str) -> str:
        self.calls += 1
        if self.forbidden:
            raise AssertionError("offer reached SQLite/store principal derivation")
        return PRINCIPAL

class _Bindings:
    def __init__(self, forbidden: bool = False, enabled: bool = True) -> None:
        self.forbidden = forbidden
        self.enabled = enabled
        self.calls = 0

    def is_enabled_user(self, _platform: str, _user_id: str) -> bool:
        self.calls += 1
        if self.forbidden:
            raise AssertionError("offer reached settings/binding lookup")
        return self.enabled


class _Lifecycle:
    def __init__(self) -> None:
        self.matches = True
        self.match_calls = 0
        self.acquired = 0
        self.released = 0

    def snapshot_matches(self, _session_id: str, _snapshot: object) -> bool:
        self.match_calls += 1
        return self.matches

    async def acquire(self, _session_id: str) -> object:
        self.acquired += 1
        lifecycle = self

        class Admission:
            def release(self) -> None:
                lifecycle.released += 1

        return Admission()


def _event(
    *,
    text: str = "remember this",
    lease: object = None,
    files: tuple[MemoryFile, ...] = (),
    snapshot: object = 1,
) -> TurnAccepted:
    return TurnAccepted(
        platform="slack",
        user_id="user-1",
        message_id="native-1",
        session_id="session-1",
        text=text,
        files=files,
        is_dm=True,
        is_ordinary_text=not files,
        is_ordinary_attachment=bool(files),
        lifecycle_snapshot=snapshot,
        attachment_lease=lease,
    )


def _adapter(
    module: _Module,
    *,
    lifecycle: _Lifecycle | None = None,
    principals: _Principals | None = None,
    bindings: _Bindings | None = None,
    selector=None,
    status_reader=None,
    generation_reader=None,
    task_factory=asyncio.create_task,
    max_pending_events: int = 256,
) -> tuple[EnabledMemoryAdapter, _Lifecycle]:
    lifecycle = lifecycle or _Lifecycle()
    options = {}
    if selector is not None:
        options["attachment_selector"] = selector
    adapter = EnabledMemoryAdapter(
        module=module,
        principals=principals or _Principals(),
        is_enabled_user=(bindings or _Bindings()).is_enabled_user,
        lifecycle_snapshot_matches=lifecycle.snapshot_matches,
        acquire_lifecycle_admission=lifecycle.acquire,
        attachment_capture_status=(
            status_reader or (lambda: asyncio.sleep(0, result="ready"))
        ),
        attachment_config_generation=generation_reader or (lambda: 7),
        max_pending_events=max_pending_events,
        **options,
    )
    assert adapter.start(task_factory=task_factory)
    return adapter, lifecycle


async def _settle(adapter: EnabledMemoryAdapter) -> None:
    await adapter.wait_idle_for_tests()
    await asyncio.sleep(0)


def test_non_running_controller_loop_can_schedule_dispatcher() -> None:
    from core.controller import Controller

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        assert loop.is_running() is False
        module = _Module()
        lifecycle = _Lifecycle()
        adapter = EnabledMemoryAdapter(
            module=module,
            principals=_Principals(),
            is_enabled_user=_Bindings().is_enabled_user,
            lifecycle_snapshot_matches=lifecycle.snapshot_matches,
            acquire_lifecycle_admission=lifecycle.acquire,
            attachment_capture_status=lambda: asyncio.sleep(0, result="ready"),
            attachment_config_generation=lambda: 7,
        )

        class Runtime:
            def start_capture_adapter(self, *, task_factory) -> bool:
                return adapter.start(task_factory=task_factory)

        controller = Controller.__new__(Controller)
        controller._loop = loop
        assert controller._start_memory_capture_adapter(Runtime())
        assert adapter._worker_task is not None
        assert adapter._worker_task.get_loop() is loop
        loop.run_until_complete(adapter.cancel_memory_capture_tasks())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_disabled_adapter_has_zero_side_effects() -> None:
    """Scenarios: MEMORY-INDEP-001, MEMORY-INDEP-003, MEMORY-INDEP-004."""

    lease = Mock()
    DisabledMemoryAdapter().offer(_event(lease=lease))
    lease.retain.assert_not_called()


def test_host_event_is_closed_and_uses_authenticated_workbench_author() -> None:
    """Scenario: MEMORY-INDEP-016."""

    file = FileAttachment(
        name="原始.pdf",
        mimetype="application/pdf",
        local_path="/private/original.pdf",
    )
    context = MessageContext(
        user_id="routing-user",
        channel_id="session-1",
        platform="avibe",
        message_id="native-1",
        platform_specific={"author_id": "authenticated-author", "is_dm": False},
        files=[file],
        is_original_human_text=True,
        is_original_human_attachment=True,
    )

    event = memory_turn_event(context, "记住原件", "session-1", (3, 4), sender_name="用户")
    file.name = "mutated.pdf"
    context.user_id = "mutated-user"
    context.files.clear()

    assert event.user_id == "authenticated-author"
    assert event.sender_name == "用户"
    assert event.text == "记住原件"
    assert event.files == (
        MemoryFile(
            name="原始.pdf",
            mimetype="application/pdf",
            local_path="/private/original.pdf",
        ),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (123, None),
        (" \n\x00\ud800\u202e ", None),
        ("  小王 Élodie 🌱\n\u202e ", "小王 Élodie 🌱"),
        ("👩\u200d💻", "👩\u200d💻"),
        ("名" * 140, "名" * 128),
    ],
)
def test_sender_name_normalization_preserves_bounded_unicode(raw, expected) -> None:
    assert normalize_memory_sender_name(raw) == expected


def _sender_name_controller(*, enabled=True, language="en", user=None):
    from core.controller import Controller

    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=enabled), language=language)
    store = Mock()
    store.get_user.return_value = user
    manager = Mock()
    manager.get_store.return_value = store
    controller.platform_settings_managers = {"slack": manager}
    return controller, store, manager


@pytest.mark.parametrize("platform", ["slack", "avibe"])
@pytest.mark.asyncio
async def test_sender_name_uses_only_bound_records_not_browser_claims(platform) -> None:
    user = SimpleNamespace(display_name="  小王\n ", enabled=True)
    controller, store, manager = _sender_name_controller(user=user)
    context = MessageContext(
        user_id="native-1", channel_id="channel-1", platform=platform,
        platform_specific={"author_id": "remote:subject", "author_name": "Untrusted"},
    )
    assert await controller.memory_sender_name_for_context(context) == ("小王" if platform == "slack" else "User")
    if platform == "slack":
        store.get_user.assert_called_once_with("native-1", platform="slack")
    else:
        manager.get_store.assert_not_called()


@pytest.mark.asyncio
async def test_sender_name_lookup_failure_and_disabled_capture_are_best_effort() -> None:
    controller, store, manager = _sender_name_controller(language="zh")
    context = MessageContext(user_id="raw-id", channel_id="channel", platform="slack")
    store.maybe_reload.side_effect = OSError("settings unavailable")
    assert await controller.memory_sender_name_for_context(context) == "用户"
    manager.reset_mock()
    controller.config.memory.enabled = False
    assert await controller.memory_sender_name_for_context(context) is None
    manager.get_store.assert_not_called()


@pytest.mark.parametrize("name", [None, "", " \x00 "])
@pytest.mark.asyncio
async def test_missing_bound_name_falls_back_without_using_native_id(name) -> None:
    controller, _store, _manager = _sender_name_controller(
        user=SimpleNamespace(display_name=name, enabled=True)
    )
    context = MessageContext(user_id="raw-id", channel_id="channel", platform="slack")
    assert await controller.memory_sender_name_for_context(context) == "User"


@pytest.mark.asyncio
async def test_sender_name_lookup_wait_does_not_block_event_loop() -> None:
    import threading

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    lookup_threads = []
    controller, store, _manager = _sender_name_controller(
        user=SimpleNamespace(display_name="小王", enabled=True)
    )

    def blocked_reload():
        lookup_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=3), "event loop could not release the lookup"

    store.maybe_reload.side_effect = blocked_reload
    context = MessageContext(user_id="user-1", channel_id="channel", platform="slack")
    task = asyncio.create_task(controller.memory_sender_name_for_context(context))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert lookup_threads and lookup_threads[0] != loop_thread
        assert not task.done()
        # A separate loop callback must progress while the settings read waits.
        heartbeat = asyncio.Event()
        loop.call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=1)
    finally:
        release.set()
        name = await task
    assert name == "小王"
    store.maybe_reload.assert_called_once_with()


@pytest.mark.asyncio
async def test_sender_name_snapshot_survives_rename_before_async_admission() -> None:
    """MEMORY-SEARCH-020: a queued capture keeps its accepted name snapshot."""
    user = SimpleNamespace(display_name="  小王 Élodie 🌱 ", enabled=True)
    controller, _store, _manager = _sender_name_controller(user=user)
    context = MessageContext(
        user_id="user-1", channel_id="channel", platform="slack", message_id="message-1",
        platform_specific={"is_dm": True}, is_original_human_text=True,
    )
    text = "原文\n`u-synthetic` https://example.invalid/u-synthetic"
    name = await controller.memory_sender_name_for_context(context)
    event = memory_turn_event(context, text, "session-1", 1, sender_name=name)
    module = _Module()
    adapter, _lifecycle = _adapter(module)
    try:
        adapter.offer(event)
        user.display_name = "Renamed"
        await _settle(adapter)
        assert len(module.captures) == 1
        captured = module.captures[0]
        assert captured.sender_name == "小王 Élodie 🌱"
        assert captured.text == text
        assert captured.principal_id == PRINCIPAL
        assert captured.provenance == "user_input"
        assert event.user_id == "user-1"
    finally:
        await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_offer_hot_path_only_retains_reserves_and_puts_nowait(monkeypatch) -> None:
    """Scenario: MEMORY-INDEP-001; synchronous offer is a strict sentinel."""

    module = _Module()
    principals = _Principals(forbidden=True)
    bindings = _Bindings(forbidden=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("offer reached filesystem/SQLite/provider/subprocess work")

    created_tasks: list[str] = []
    create_task = asyncio.create_task

    def task_factory(coroutine, *, name: str):
        created_tasks.append(name)
        return create_task(coroutine, name=name)

    adapter, lifecycle = _adapter(
        module,
        principals=principals,
        bindings=bindings,
        selector=forbidden,
        status_reader=forbidden,
        generation_reader=forbidden,
        task_factory=task_factory,
    )
    lease = _Lease()
    files = (MemoryFile("账单.pdf", "application/pdf"),)

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "stat", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    monkeypatch.setattr(module, "reserve_capture_admission", forbidden)
    monkeypatch.setattr(module, "capture_admission", forbidden)
    monkeypatch.setattr(module, "capture", forbidden)
    monkeypatch.setattr(module, "offer_barrier", forbidden)

    adapter.offer(_event(text="记住账单", files=files, lease=lease))

    assert lease.retained == 1
    assert lease.released == 0
    assert principals.calls == 0
    assert bindings.calls == 0
    assert lifecycle.match_calls == lifecycle.acquired == 0
    assert module.captures == []
    assert created_tasks == ["memory-capture-dispatcher"]
    monkeypatch.undo()
    await adapter.cancel_memory_capture_tasks()
    assert lease.released == 1


@pytest.mark.asyncio
async def test_success_releases_one_retained_lease_and_forwards_capture() -> None:
    """Scenarios: MEMORY-INDEP-001, MEMORY-IM-ATTACH-001."""

    module = _Module()
    attachment = CaptureAttachment(
        kind="pdf",
        name="账单.pdf",
        uri="file:///private/账单.pdf",
        ext="pdf",
    )
    selector = lambda _lease: SimpleNamespace(attachments=(attachment,), skipped=())
    adapter, lifecycle = _adapter(module, selector=selector)
    lease = _Lease()

    adapter.offer(
        _event(
            text="请记住这张账单",
            files=(MemoryFile("账单.pdf", "application/pdf"),),
            lease=lease,
        )
    )
    await _settle(adapter)

    assert [request.text for request in module.captures] == ["请记住这张账单"]
    assert module.captures[0].attachments == (attachment,)
    assert lease.retained == lease.released == 1
    assert lifecycle.acquired == lifecycle.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_attachment_capture_telemetry_accounts_each_terminal_path_once(
    monkeypatch,
) -> None:
    records: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        capture_adapter_module,
        "log_attachment_capture",
        lambda platform, total, captured: records.append(
            (platform, total, captured)
        ),
    )
    files = (MemoryFile("receipt.pdf", "application/pdf"),)

    capacity_module = _Module()
    capacity_module.capacity_outcome = "full"
    capacity_adapter, _ = _adapter(capacity_module)
    capacity_adapter.offer(_event(files=files))
    await capacity_adapter.cancel_memory_capture_tasks()

    denied_module = _Module()
    denied_adapter, _ = _adapter(
        denied_module,
        bindings=_Bindings(enabled=False),
    )
    denied_adapter.offer(_event(files=files))
    await _settle(denied_adapter)
    await denied_adapter.cancel_memory_capture_tasks()

    stale_module = _Module()
    stale_lifecycle = _Lifecycle()
    stale_lifecycle.matches = False
    stale_adapter, _ = _adapter(stale_module, lifecycle=stale_lifecycle)
    stale_adapter.offer(_event(files=files))
    await _settle(stale_adapter)
    await stale_adapter.cancel_memory_capture_tasks()

    accepted_module = _Module()

    async def accept_capture(*_args, **_kwargs):
        return CaptureAccepted(captured_attachment_count=1)

    accepted_module.capture = accept_capture
    accepted_adapter, _ = _adapter(
        accepted_module,
        selector=lambda _lease: SimpleNamespace(
            attachments=(
                CaptureAttachment(
                    kind="pdf",
                    name="receipt.pdf",
                    uri="file:///private/receipt.pdf",
                    ext="pdf",
                ),
            ),
            skipped=(),
        ),
    )
    accepted_adapter.offer(_event(files=files, lease=_Lease()))
    await _settle(accepted_adapter)
    await accepted_adapter.cancel_memory_capture_tasks()

    assert records == [
        ("slack", 1, 0),
        ("slack", 1, 0),
        ("slack", 1, 0),
        ("slack", 1, 1),
    ]


@pytest.mark.asyncio
async def test_denied_attachment_is_rejected_before_any_preparation() -> None:
    module = _Module()
    bindings = _Bindings(enabled=False)
    generation = Mock(side_effect=AssertionError("generation read before authorization"))
    status = Mock(side_effect=AssertionError("status read before authorization"))
    selector = Mock(side_effect=AssertionError("selection ran before authorization"))
    adapter, _ = _adapter(
        module,
        bindings=bindings,
        generation_reader=generation,
        status_reader=status,
        selector=selector,
    )
    lease = _Lease()

    adapter.offer(
        _event(
            text="private caption",
            files=(MemoryFile("private.pdf", "application/pdf"),),
            lease=lease,
        )
    )
    await _settle(adapter)

    assert bindings.calls == 1
    generation.assert_not_called()
    status.assert_not_called()
    selector.assert_not_called()
    assert module.captures == []
    assert lease.retained == lease.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_missing_generation_skips_provider_probe_and_degrades_caption() -> None:
    module = _Module()
    status = Mock(side_effect=AssertionError("provider probed without opt-in"))
    selector = Mock(side_effect=AssertionError("selection ran without opt-in"))
    adapter, _ = _adapter(
        module,
        generation_reader=lambda: None,
        status_reader=status,
        selector=selector,
    )
    lease = _Lease()

    adapter.offer(
        _event(
            text="caption survives",
            files=(MemoryFile("receipt.pdf", "application/pdf"),),
            lease=lease,
        )
    )
    await _settle(adapter)

    status.assert_not_called()
    selector.assert_not_called()
    assert len(module.captures) == 1
    assert module.captures[0].attachments == ()
    assert lease.retained == lease.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_queue_full_releases_rejected_and_shutdown_releases_queued_lease() -> None:
    module = _Module()
    adapter, _ = _adapter(module, max_pending_events=1)
    first = _Lease()
    rejected = _Lease()

    adapter.offer(_event(lease=first))
    adapter.offer(_event(lease=rejected))

    assert rejected.retained == rejected.released == 1
    await adapter.cancel_memory_capture_tasks()
    assert first.retained == first.released == 1


@pytest.mark.asyncio
async def test_capture_scheduling_failure_releases_retained_lease() -> None:
    """Scenario: MEMORY-INDEP-006."""
    module = _Module()

    def task_factory(coroutine, *, name: str):
        if name == "memory-capture":
            raise RuntimeError("registration failed")
        return asyncio.create_task(coroutine, name=name)

    adapter, _ = _adapter(module, task_factory=task_factory)
    lease = _Lease()
    adapter.offer(_event(lease=lease))
    await _settle(adapter)

    assert lease.retained == lease.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_capture_registration_failure_joins_task_before_releasing_lease() -> None:
    """Scenario: MEMORY-INDEP-006."""

    module = _Module()
    adapter, _ = _adapter(module)
    adapter._track_capture_task = Mock(side_effect=RuntimeError("registration failed"))
    lease = _Lease()
    adapter.offer(_event(lease=lease))
    await _settle(adapter)

    assert lease.retained == lease.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_pre_first_step_cancellation_releases_all_ownership() -> None:
    module = _Module()
    adapter, _ = _adapter(module)
    original_track = adapter._track_capture_task

    def track_then_cancel(task, session_id, ownership):
        original_track(task, session_id, ownership)
        task.cancel()

    adapter._track_capture_task = track_then_cancel
    lease = _Lease()
    adapter.offer(_event(lease=lease))
    await _settle(adapter)

    assert module.captures == []
    assert lease.retained == lease.released == 1
    assert module.capacities and all(not item.active for item in module.capacities)
    assert module.reservations and all(
        not item.active for item in module.reservations
    )
    assert adapter.capture_tasks == set()
    await adapter.cancel_memory_capture_tasks()


def test_controller_passes_only_fresh_binding_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.controller as controller_module
    from config.v2_config import MemoryConfig
    from core.controller import Controller

    calls: list[str] = []
    passed: dict[str, object] = {}

    class Store:
        def maybe_reload(self) -> None:
            calls.append("reload")

        def get_user(self, user_id: str, *, platform: str):
            calls.append(f"lookup:{platform}:{user_id}")
            return SimpleNamespace(enabled=False)

    def load_runtime(_config, **kwargs):
        passed.update(kwargs)
        return object()

    monkeypatch.setattr(controller_module, "load_memory_runtime", load_runtime)
    controller = Controller.__new__(Controller)
    controller.platform_settings_managers = {
        "slack": SimpleNamespace(get_store=lambda: Store())
    }
    controller.session_turns = SimpleNamespace(
        session_lifecycle_snapshot_matches=lambda *_args: True,
        acquire_lifecycle_admission=lambda *_args: None,
    )
    controller._log_memory_processing_event = lambda *_args: None
    controller._adopt_settled_memory_config = lambda _config: None

    assert controller._create_memory_runtime(MemoryConfig(enabled=True)) is not None
    assert "platform_settings_managers" not in passed
    is_enabled_user = passed["is_enabled_user"]
    assert callable(is_enabled_user)
    assert is_enabled_user("slack", "user-1") is False
    assert calls == ["reload", "lookup:slack:user-1"]


@pytest.mark.asyncio
async def test_worker_failure_and_stale_lifecycle_each_release_exactly_once() -> None:
    """Scenario: MEMORY-INDEP-002."""
    module = _Module()
    module.capture_error = RuntimeError("worker failed")
    adapter, lifecycle = _adapter(module)
    failed = _Lease()
    adapter.offer(_event(lease=failed))
    await _settle(adapter)
    assert failed.retained == failed.released == 1

    lifecycle.matches = False
    stale = _Lease()
    adapter.offer(_event(lease=stale, snapshot=2))
    await _settle(adapter)
    assert stale.retained == stale.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_session_and_shutdown_cancellation_release_active_leases() -> None:
    """Scenarios: MEMORY-INDEP-001, MEMORY-INDEP-003, MEMORY-INDEP-008."""
    module = _Module()
    module.capture_continue.clear()
    adapter, _ = _adapter(module)
    abandoned = _Lease()
    adapter.offer(_event(lease=abandoned))
    await module.capture_started.wait()
    adapter.abandon_memory_captures_for_session("session-1")
    await _settle(adapter)
    assert abandoned.retained == abandoned.released == 1

    module.capture_started.clear()
    shutdown = _Lease()
    adapter.offer(_event(lease=shutdown))
    await module.capture_started.wait()
    await adapter.cancel_memory_capture_tasks()
    assert shutdown.retained == shutdown.released == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected_captures",
    [("caption survives", 1), ("", 0)],
)
async def test_unavailable_attachment_preparation_degrades_or_fails_closed(
    text: str,
    expected_captures: int,
) -> None:
    """Scenarios: MEMORY-IM-ATTACH-003, MEMORY-IM-ATTACH-004."""

    module = _Module()
    adapter, _ = _adapter(module, selector=lambda _lease: None)
    lease = _Lease()
    adapter.offer(
        _event(
            text=text,
            files=(MemoryFile("receipt.pdf", "application/pdf"),),
            lease=lease,
        )
    )
    await _settle(adapter)

    assert len(module.captures) == expected_captures
    if module.captures:
        assert module.captures[0].attachments == ()
    assert lease.retained == lease.released == 1
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.asyncio
async def test_reset_and_archive_events_preserve_barriers() -> None:
    """Scenarios: MEMORY-INDEP-005, MEMORY-INDEP-010."""

    module = _Module()
    first_started = asyncio.Event()
    first_continue = asyncio.Event()
    second_started = asyncio.Event()

    async def capture(request, **_options: object) -> object:
        if request.text == "first capture":
            first_started.set()
            await first_continue.wait()
        else:
            second_started.set()
        module.captures.append(request)
        return SimpleNamespace(status="accepted")

    module.capture = capture
    adapter, _ = _adapter(module)

    first_lease = _Lease()
    second_lease = _Lease()
    adapter.offer(_event(text="first capture", lease=first_lease))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    adapter.offer(_event(text="second capture", lease=second_lease))
    adapter.offer(SessionReset("session-reset"))
    adapter.offer(SessionArchived("session-archived"))
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    async def wait_for_barriers() -> None:
        while len(module.barriers) < 2:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_barriers(), timeout=1.0)

    assert module.barriers == ["session-reset", "session-archived"]
    assert [request.text for request in module.captures] == ["second capture"]
    assert first_lease.retained == 1
    assert first_lease.released == 0
    assert second_lease.retained == second_lease.released == 1

    first_continue.set()
    await _settle(adapter)
    assert first_lease.retained == first_lease.released == 1
    await adapter.cancel_memory_capture_tasks()

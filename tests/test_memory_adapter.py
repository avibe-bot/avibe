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

from avibe_memory.capture_adapter import EnabledMemoryAdapter
from avibe_memory.types import CaptureAttachment
from core.memory_adapter import (
    DisabledMemoryAdapter,
    MemoryFile,
    SessionArchived,
    SessionReset,
    TurnAccepted,
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
        return SimpleNamespace(active=True)

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

    def project_for_workdir(self, _workdir: str) -> str:
        raise AssertionError("capture must not derive a project from workdir")


class _Bindings:
    def __init__(self, forbidden: bool = False) -> None:
        self.forbidden = forbidden
        self.calls = 0

    def is_enabled_user(self, _platform: str, _user_id: str) -> bool:
        self.calls += 1
        if self.forbidden:
            raise AssertionError("offer reached settings/binding lookup")
        return True


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
        bindings=bindings or _Bindings(),
        lifecycle_snapshot_matches=lifecycle.snapshot_matches,
        acquire_lifecycle_admission=lifecycle.acquire,
        attachment_capture_status=(
            status_reader or (lambda: asyncio.sleep(0, result="ready"))
        ),
        attachment_config_generation=generation_reader or (lambda: 7),
        task_factory=task_factory,
        max_pending_events=max_pending_events,
        **options,
    )
    assert adapter.start()
    return adapter, lifecycle


async def _settle(adapter: EnabledMemoryAdapter) -> None:
    await adapter.wait_idle_for_tests()
    await asyncio.sleep(0)


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

    event = memory_turn_event(context, "记住原件", "session-1", (3, 4))
    file.name = "mutated.pdf"
    context.user_id = "mutated-user"
    context.files.clear()

    assert event.user_id == "authenticated-author"
    assert event.text == "记住原件"
    assert event.files == (
        MemoryFile(
            name="原始.pdf",
            mimetype="application/pdf",
            local_path="/private/original.pdf",
        ),
    )


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
    adapter, _ = _adapter(module)
    adapter.offer(SessionReset("session-reset"))
    adapter.offer(SessionArchived("session-archived"))
    await _settle(adapter)

    assert module.barriers == ["session-reset", "session-archived"]
    await adapter.cancel_memory_capture_tasks()

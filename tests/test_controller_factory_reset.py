from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.controller import Controller
from core.memory.operation_lock import MemoryOperationLease


class _Runtime:
    def __init__(self, effective_home: Path, *, artifact_admitted: bool = True) -> None:
        self.effective_home = effective_home
        self._artifact_admitted = artifact_admitted
        self.retired = False

    def artifact_admitted(self) -> bool:
        return self._artifact_admitted

    def adopt_recovery_intent(self, _candidate: object) -> None:
        return None

    def retire(self) -> None:
        self.retired = True

    async def close(self) -> None:
        raise RuntimeError("retirement failed")


def _controller(runtime: _Runtime) -> Controller:
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    return controller


def _create_roots(home: Path) -> None:
    home.chmod(0o700)
    (home / "memory").mkdir(mode=0o700)
    (home / "state" / "memory").mkdir(mode=0o700, parents=True)
    (home / "state").chmod(0o700)


def test_delete_memory_roots_reports_lstat_failure_per_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.memory import factory_reset

    _create_roots(tmp_path)
    real_lstat = factory_reset.os.lstat

    def lstat(path: Path):
        if Path(path).as_posix().endswith("state/memory"):
            raise OSError("unreadable root")
        return real_lstat(path)

    monkeypatch.setattr(factory_reset.os, "lstat", lstat)
    result = factory_reset.delete_memory_roots(tmp_path)

    assert len(result.roots) == 2
    assert result.roots[0].relative_path == "memory"
    assert result.roots[0].deleted is True
    assert result.roots[1].relative_path == "state/memory"
    assert result.roots[1].deleted is False
    assert result.roots[1].error == "OSError"


def test_delete_memory_roots_reports_absent_roots_as_not_deleted(tmp_path: Path) -> None:
    from core.memory import factory_reset

    result = factory_reset.delete_memory_roots(tmp_path)

    assert [(root.existed, root.deleted) for root in result.roots] == [(False, False), (False, False)]
    assert result.data_deleted is False
    assert result.data_remaining is False


@pytest.mark.asyncio
async def test_factory_reset_artifact_invalid_returns_closed_unchanged_result(
    tmp_path: Path,
) -> None:
    _create_roots(tmp_path)
    result = await Controller._factory_reset_memory_once(_controller(_Runtime(tmp_path, artifact_admitted=False)))

    assert result == {
        "ok": False,
        "error": "memory_factory_reset_failed",
        "result": "failed",
        "reason": "artifact_repair_required",
        "data_deleted": False,
        "data_remaining": True,
        "roots": [
            {"path": "memory", "existed": True, "deleted": False},
            {"path": "state/memory", "existed": True, "deleted": False},
        ],
    }


@pytest.mark.asyncio
async def test_factory_reset_retirement_failure_returns_closed_unchanged_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_roots(tmp_path)
    controller = _controller(_Runtime(tmp_path))
    monkeypatch.setattr(
        controller,
        "_mark_factory_reset_intent",
        lambda: SimpleNamespace(recovery_intent="factory_reset"),
    )

    result = await controller._factory_reset_memory_once()

    assert result["result"] == "failed"
    assert result["reason"] == "runtime_retirement_failed"
    assert result["data_deleted"] is False
    assert result["data_remaining"] is True
    assert result["roots"] == [
        {"path": "memory", "existed": True, "deleted": False},
        {"path": "state/memory", "existed": True, "deleted": False},
    ]


@pytest.mark.asyncio
async def test_factory_reset_returns_conflict_when_operation_lease_is_held(tmp_path: Path) -> None:
    """Factory reset must not retire a runtime behind another Memory operation."""

    _create_roots(tmp_path)
    runtime = _Runtime(tmp_path)
    controller = _controller(runtime)
    controller._mark_factory_reset_intent = lambda: pytest.fail(
        "busy reset must not persist durable intent"
    )
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    try:
        result = await Controller._factory_reset_memory_once(controller)
    finally:
        lease.release()

    assert result == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }
    assert runtime.retired is False


def test_factory_reset_deletion_syncs_each_anchored_parent_after_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root deletion is durable before recovery intent may be cleared."""

    from core.memory import confined_filesystem

    _create_roots(tmp_path)
    real_fsync = confined_filesystem.os.fsync
    real_rmdir = confined_filesystem.os.rmdir
    home_identity = os.stat(tmp_path).st_ino
    state_identity = os.stat(tmp_path / "state").st_ino
    events: list[tuple[str, object]] = []

    def observe_fsync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        assert stat.S_ISDIR(info.st_mode)
        events.append(("fsync", info.st_ino))
        real_fsync(descriptor)

    def observe_rmdir(path: str, *, dir_fd: int | None = None) -> None:
        events.append(("rmdir", path))
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(confined_filesystem.os, "fsync", observe_fsync)
    monkeypatch.setattr(confined_filesystem.os, "rmdir", observe_rmdir)

    confined_filesystem.remove_confined_path(
        tmp_path,
        tmp_path / "state" / "memory",
    )

    removed_at = events.index(("rmdir", "memory"))
    assert events[removed_at + 1 :] == [
        ("fsync", state_identity),
        ("fsync", home_identity),
    ]


def test_factory_reset_fsync_failure_stays_pending_and_retry_syncs_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-unlink fsync fault remains retryable instead of clearing intent."""

    from core.memory import confined_filesystem, factory_reset

    _create_roots(tmp_path)
    real_fsync = confined_filesystem.os.fsync
    state_identity = os.stat(tmp_path / "state").st_ino
    failed = False

    def fail_state_parent_once(descriptor: int) -> None:
        nonlocal failed
        if os.fstat(descriptor).st_ino == state_identity and not failed:
            failed = True
            raise OSError("simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(confined_filesystem.os, "fsync", fail_state_parent_once)
    first = factory_reset.delete_memory_roots(tmp_path)

    assert [(root.deleted, root.error) for root in first.roots] == [
        (True, None),
        (False, "ConfinedFilesystemError"),
    ]
    assert first.data_remaining is True
    assert not (tmp_path / "state" / "memory").exists()

    monkeypatch.setattr(confined_filesystem.os, "fsync", real_fsync)
    retry = factory_reset.delete_memory_roots(tmp_path)

    assert [(root.existed, root.deleted, root.error) for root in retry.roots] == [
        (False, False, None),
        (False, False, None),
    ]
    assert retry.data_remaining is False


@pytest.mark.asyncio
async def test_factory_reset_does_not_clear_intent_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controller cutover cannot outrun uncertain deletion durability."""

    from core.memory import confined_filesystem

    class _ClosableRuntime(_Runtime):
        closed = False

        async def close(self) -> None:
            self.closed = True

    _create_roots(tmp_path)
    runtime = _ClosableRuntime(tmp_path)
    controller = _controller(runtime)
    controller._mark_factory_reset_intent = lambda: SimpleNamespace(
        recovery_intent="factory_reset"
    )
    controller._clear_factory_reset_intent = lambda: pytest.fail(
        "uncertain deletion must retain recovery intent"
    )
    real_fsync = confined_filesystem.os.fsync
    state_identity = os.stat(tmp_path / "state").st_ino
    failed = False

    def fail_state_parent_once(descriptor: int) -> None:
        nonlocal failed
        if os.fstat(descriptor).st_ino == state_identity and not failed:
            failed = True
            raise OSError("simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(confined_filesystem.os, "fsync", fail_state_parent_once)

    result = await controller._factory_reset_memory_once()

    assert result["result"] == "partial"
    assert result["data_remaining"] is True
    assert result["roots"][1]["error"] == "ConfinedFilesystemError"
    assert runtime.retired is True

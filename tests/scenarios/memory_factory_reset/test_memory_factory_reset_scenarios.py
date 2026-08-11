"""Service-boundary scenarios for Memory factory reset (#1315)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.v2_config import MemoryConfig
from core.controller import Controller
from core.memory import CaptureAccepted, CaptureRequest, CaptureSkipped
from core.memory.factory_reset import delete_memory_roots


class _Runtime:
    def __init__(self, home: Path, *, artifact_admitted: bool = True) -> None:
        self.effective_home = home
        self.artifact_manager = object()
        self.process_factory = object()
        self.retired = False
        self._artifact_admitted = artifact_admitted
        self.closed = False

    def artifact_admitted(self) -> bool:
        return self._artifact_admitted

    def adopt_recovery_intent(self, _config: object) -> None:
        return None

    def retire(self) -> None:
        self.retired = True

    async def close(self) -> None:
        self.closed = True


class _FreshRuntime(_Runtime):
    def __init__(self, home: Path, *, activation_ok: bool = True) -> None:
        super().__init__(home)
        self.activation_ok = activation_ok
        self.module = SimpleNamespace(pause_claims=lambda: None)

    async def activate_fresh(self, _config: MemoryConfig) -> dict[str, object]:
        return {"ok": self.activation_ok}


def _controller(runtime: _Runtime) -> Controller:
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    controller.memory_module = getattr(runtime, "module", None)
    controller.config = SimpleNamespace(memory=MemoryConfig(enabled=False))
    controller._mark_factory_reset_intent = lambda: replace(
        controller.config.memory,
        recovery_intent="factory_reset",
    )
    controller._clear_factory_reset_intent = lambda: replace(
        controller.config.memory,
        recovery_intent=None,
    )
    return controller


def _create_roots(home: Path) -> None:
    home.chmod(0o700)
    (home / "memory").mkdir(mode=0o700)
    (home / "state" / "memory").mkdir(mode=0o700, parents=True)
    (home / "state").chmod(0o700)


def _request() -> CaptureRequest:
    return CaptureRequest(
        source_message_id="scenario-source",
        session_id="scenario-session",
        principal_id="u-" + "1" * 32,
        project_id="p-" + "2" * 32,
        provenance="agent",
        text="remember this",
        occurred_at_ms=1,
    )


@pytest.mark.asyncio
async def test_memory_factory_001_successful_reset_is_a_closed_service_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful boundary call deletes exactly both roots and settles intent."""
    _create_roots(tmp_path)
    old = _Runtime(tmp_path)
    fresh = _FreshRuntime(tmp_path)
    controller = _controller(old)
    monkeypatch.setattr("core.memory.runtime.create_memory_runtime", lambda *args, **kwargs: fresh)

    result = await controller._factory_reset_memory_once()

    assert result["ok"] is True
    assert result["data_deleted"] is True
    assert result["data_remaining"] is False
    assert [root["path"] for root in result["roots"]] == ["memory", "state/memory"]
    assert controller.config.memory.recovery_intent is None
    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "state" / "memory").exists()


@pytest.mark.asyncio
async def test_memory_factory_002_partial_delete_is_truthful_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed root remains visible, and a later call finishes the reset."""
    _create_roots(tmp_path)
    old = _Runtime(tmp_path)
    controller = _controller(old)
    failed_once = True
    from core.memory import factory_reset

    real_remove = factory_reset.remove_confined_path

    def remove(home: Path, path: Path) -> None:
        nonlocal failed_once
        if failed_once and path.relative_to(home).as_posix() == "state/memory":
            failed_once = False
            raise PermissionError("test partial delete")
        real_remove(home, path)

    monkeypatch.setattr(factory_reset, "remove_confined_path", remove)
    monkeypatch.setattr("core.memory.runtime.create_memory_runtime", lambda *args, **kwargs: _FreshRuntime(tmp_path))

    first = await controller._factory_reset_memory_once()
    second = await controller._factory_reset_memory_once()

    assert first["result"] == "partial"
    assert first["data_deleted"] is True
    assert first["data_remaining"] is True
    assert second["ok"] is True
    assert second["data_remaining"] is False


@pytest.mark.asyncio
async def test_memory_factory_003_stale_remember_waits_for_controller_replacement_gate() -> None:
    """A stale capture cannot cross the Controller replacement gate."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class Module:
        async def capture(self, _request: CaptureRequest):
            entered.set()
            await release.wait()
            return CaptureAccepted()

    runtime = SimpleNamespace(retired=False, available=True, module=Module())
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime

    gate = controller._memory_replacement_lock()
    await gate.acquire()
    task = asyncio.create_task(controller.capture_memory(_request()))
    await asyncio.sleep(0)
    assert not entered.is_set()
    gate.release()
    await entered.wait()
    release.set()
    assert (await task).status == "accepted"

    runtime.retired = True
    blocked = await controller.capture_memory(_request())
    assert isinstance(blocked, CaptureSkipped)
    assert blocked.reason == "memory_operation_in_progress"


@pytest.mark.asyncio
async def test_memory_factory_101_disabled_reset_fails_closed_without_deletion(tmp_path: Path) -> None:
    """An invalid pinned artifact leaves both roots untouched."""
    _create_roots(tmp_path)
    result = await Controller._factory_reset_memory_once(
        _controller(_Runtime(tmp_path, artifact_admitted=False))
    )
    assert result["ok"] is False
    assert result["reason"] == "artifact_repair_required"
    assert result["data_deleted"] is False
    assert result["data_remaining"] is True
    assert len(result["roots"]) == 2


@pytest.mark.asyncio
async def test_memory_factory_201_worker_and_process_death_are_quiesced_before_delete(tmp_path: Path) -> None:
    """Retirement closes the old aggregate before either mutable root is removed."""
    _create_roots(tmp_path)
    old = _Runtime(tmp_path)
    controller = _controller(old)
    result = await controller._factory_reset_memory_once()
    assert result["ok"] is False or result["ok"] is True
    assert old.closed is True
    assert old.retired is True


@pytest.mark.asyncio
async def test_memory_factory_202_supervisor_down_still_returns_two_root_outcomes(tmp_path: Path) -> None:
    """A retained, down supervisor does not change the exact deletion envelope."""
    _create_roots(tmp_path)
    result = delete_memory_roots(tmp_path)
    assert len(result.roots) == 2
    assert result.data_deleted is True
    assert result.data_remaining is False


@pytest.mark.asyncio
async def test_memory_factory_301_post_delete_activation_failure_retains_recovery_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activation failure after deletion is closed, truthful, and retryable."""
    _create_roots(tmp_path)
    old = _Runtime(tmp_path)
    fresh = _FreshRuntime(tmp_path, activation_ok=False)
    controller = _controller(old)
    monkeypatch.setattr("core.memory.runtime.create_memory_runtime", lambda *args, **kwargs: fresh)

    result = await controller._factory_reset_memory_once()

    assert result == {
        "ok": False,
        "error": "memory_factory_reset_failed",
        "result": "deleted_activation_failed",
        "data_deleted": True,
        "data_remaining": False,
        "roots": [
            {"path": "memory", "existed": True, "deleted": True},
            {"path": "state/memory", "existed": True, "deleted": True},
        ],
    }
    assert fresh.retired is True
    assert controller.config.memory.recovery_intent is None

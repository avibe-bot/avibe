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
        self.worker_running = True
        self.process_running = True
        self.reap_calls = 0
        self.module = SimpleNamespace(
            claims_paused=False,
            claims_resumed=False,
            retired=False,
            pause_claims=lambda: None,
            resume_claims=lambda: None,
            retire=lambda: None,
        )

    def artifact_admitted(self) -> bool:
        return self._artifact_admitted

    async def _reap_recorded_sidecar_if_unowned(self, *, fail_closed: bool = False) -> bool:
        assert fail_closed is True
        self.reap_calls += 1
        return True

    def adopt_recovery_intent(self, _config: object) -> None:
        if getattr(_config, "recovery_intent", None) is not None:
            self.module.claims_paused = True
        return None

    def retire(self) -> None:
        self.retired = True
        self.module.retired = True
        self.module.claims_paused = True

    async def close(self) -> None:
        self.worker_running = False
        self.process_running = False
        self.closed = True


class _FreshRuntime(_Runtime):
    def __init__(self, home: Path, *, activation_ok: bool = True) -> None:
        super().__init__(home)
        self.activation_ok = activation_ok
        self.module.claims_paused = True

        def resume_claims() -> None:
            self.module.claims_paused = False
            self.module.claims_resumed = True

        self.module.resume_claims = resume_claims

    async def activate_fresh(self, _config: MemoryConfig) -> dict[str, object]:
        if self.activation_ok:
            self.module.resume_claims()
        return {"ok": self.activation_ok}


def _controller(runtime: _Runtime) -> Controller:
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    controller.memory_module = getattr(runtime, "module", None)
    controller.config = SimpleNamespace(memory=MemoryConfig(enabled=False))
    def mark_intent() -> MemoryConfig:
        candidate = replace(controller.config.memory, recovery_intent="factory_reset")
        controller.config.memory = candidate
        return candidate

    def clear_intent() -> MemoryConfig:
        settled = replace(controller.config.memory, recovery_intent=None)
        controller.config.memory = settled
        return settled

    controller._mark_factory_reset_intent = mark_intent
    controller._clear_factory_reset_intent = clear_intent
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
    """MEMORY-FACTORY-001: delete both roots and settle durable intent."""
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
    assert fresh.module.claims_resumed is True
    assert fresh.module.claims_paused is False
    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "state" / "memory").exists()


@pytest.mark.asyncio
async def test_memory_factory_002_partial_delete_is_truthful_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-FACTORY-002: report partial deletion and complete on retry."""
    _create_roots(tmp_path)
    old = _Runtime(tmp_path)
    controller = _controller(old)
    failed_once = True
    from core.memory import factory_reset

    real_remove = factory_reset.remove_confined_path

    def remove(home: Path, path: Path, **kwargs: object) -> None:
        nonlocal failed_once
        if failed_once and path.relative_to(home).as_posix() == "state/memory":
            failed_once = False
            raise PermissionError("test partial delete")
        real_remove(home, path, **kwargs)

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
    """MEMORY-FACTORY-003: retirement fences a stale capture at the gate."""
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
    runtime.retired = True
    gate.release()
    blocked = await controller.capture_memory(_request())
    assert isinstance(blocked, CaptureSkipped)
    assert blocked.reason == "memory_operation_in_progress"
    assert isinstance(await task, CaptureSkipped)
    assert not entered.is_set()
    release.set()


@pytest.mark.asyncio
async def test_memory_factory_004_capture_queued_for_reset_cannot_use_fresh_runtime() -> None:
    """MEMORY-FACTORY-004: a pre-reset capture is fenced at generation change."""

    class Module:
        def __init__(self) -> None:
            self.calls = 0

        async def capture(self, _request: CaptureRequest):
            self.calls += 1
            return CaptureAccepted()

    old_module = Module()
    fresh_module = Module()
    old_runtime = SimpleNamespace(retired=False, available=True, module=old_module)
    fresh_runtime = SimpleNamespace(retired=False, available=True, module=fresh_module)
    controller = Controller.__new__(Controller)
    controller.memory_runtime = old_runtime

    gate = controller._memory_replacement_lock()
    await gate.acquire()
    queued = asyncio.create_task(controller.capture_memory(_request()))
    await asyncio.sleep(0)

    controller.memory_runtime = fresh_runtime
    gate.release()

    result = await queued

    assert isinstance(result, CaptureSkipped)
    assert result.reason == "memory_operation_in_progress"
    assert old_module.calls == 0
    assert fresh_module.calls == 0


@pytest.mark.asyncio
async def test_memory_factory_005_retry_deletes_owned_permissive_child_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-FACTORY-005: retry hardens and deletes a real permissive tree."""
    _create_roots(tmp_path)
    local = tmp_path / "memory" / ".child-home" / ".local"
    (local / "share").mkdir(mode=0o700, parents=True)
    (local / "share" / "cache.db").write_bytes(b"memory")
    local.chmod(0o777)
    old = _Runtime(tmp_path)
    controller = _controller(old)
    controller.config.memory = replace(
        controller.config.memory,
        recovery_intent="factory_reset",
    )
    fresh = _FreshRuntime(tmp_path)
    monkeypatch.setattr(
        "core.memory.runtime.create_memory_runtime",
        lambda *args, **kwargs: fresh,
    )

    result = await controller._factory_reset_memory_once()

    assert result["ok"] is True
    assert result["data_deleted"] is True
    assert result["data_remaining"] is False
    assert controller.config.memory.recovery_intent is None
    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "state" / "memory").exists()


@pytest.mark.asyncio
async def test_memory_factory_101_disabled_reset_fails_closed_without_deletion(tmp_path: Path) -> None:
    """MEMORY-FACTORY-101: an invalid artifact leaves both roots untouched."""
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
async def test_memory_factory_201_worker_and_process_death_are_quiesced_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-FACTORY-201: quiesce worker and process before root deletion."""
    _create_roots(tmp_path)
    old = _Runtime(tmp_path)
    fresh = _FreshRuntime(tmp_path)
    controller = _controller(old)
    monkeypatch.setattr("core.memory.runtime.create_memory_runtime", lambda *args, **kwargs: fresh)
    result = await controller._factory_reset_memory_once()
    assert result["ok"] is True
    assert result["data_remaining"] is False
    assert old.closed is True
    assert old.retired is True
    assert old.worker_running is False
    assert old.process_running is False
    assert fresh.module.claims_resumed is True
    assert fresh.module.claims_paused is False


@pytest.mark.asyncio
async def test_memory_factory_202_supervisor_down_still_returns_two_root_outcomes(tmp_path: Path) -> None:
    """MEMORY-FACTORY-202: a down supervisor keeps exact root outcomes."""
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
    """MEMORY-FACTORY-301: failed activation preserves durable retry intent."""
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
    assert controller.memory_runtime is fresh
    assert fresh.retired is False
    assert fresh.closed is False
    assert fresh.module.claims_paused is True
    assert controller.config.memory.recovery_intent == "factory_reset"


@pytest.mark.asyncio
async def test_memory_factory_302_durable_reset_marker_blocks_capture_after_preflight() -> None:
    """MEMORY-FACTORY-302: a settled pending marker fences direct captures."""

    class Module:
        def __init__(self) -> None:
            self.calls = 0

        async def capture(self, _request: CaptureRequest):
            self.calls += 1
            return CaptureAccepted()

    runtime = SimpleNamespace(
        retired=False,
        available=True,
        module=Module(),
        _config=SimpleNamespace(recovery_intent="factory_reset"),
        _restart_config=SimpleNamespace(recovery_intent="factory_reset"),
    )
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    controller.config = SimpleNamespace(memory=MemoryConfig(enabled=True))

    result = await controller.capture_memory(_request())

    assert isinstance(result, CaptureSkipped)
    assert result.reason == "memory_operation_in_progress"
    assert runtime.module.calls == 0

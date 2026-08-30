from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager
from copy import deepcopy
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.v2_config import (
    MemoryCloudConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
)
from core.controller import Controller
from avibe_memory.data_reset import reset_memory_data_roots
from core.memory_adapter import DisabledMemoryAdapter
from vibe.memory_contract import MemoryPluginUnavailableError


class _Runtime:
    def __init__(
        self,
        home: Path,
        *,
        needs_repair: bool = False,
        close_proved: bool = True,
        wake_result: dict[str, object] | None = None,
    ) -> None:
        self.effective_home = home
        self.needs_repair = needs_repair
        self.closed = False
        self.retired = False
        self.module = object()
        self.capture_adapter = DisabledMemoryAdapter()
        self._artifact_installing = False
        self._close_proved = close_proved
        self._wake_result = wake_result or {"ok": True, "state": "running"}
        self.events: list[str] = []
        self.marked_reason: str | None = None
        self.replacement_runtime: _Runtime | None = None
        self.replacement_configs: list[MemoryConfig] = []

    async def prepare_data_reset(self) -> None:
        self.events.append("reap")

    def start_capture_adapter(self, **_options: object) -> bool:
        return True

    def replacement(self, config: MemoryConfig) -> _Runtime:
        self.replacement_configs.append(config)
        if self.replacement_runtime is not None:
            return self.replacement_runtime
        return _Runtime(
            self.effective_home,
            needs_repair=config.legacy_needs_repair,
        )

    def retire(self) -> None:
        self.events.append("retire")
        self.retired = True

    async def close(self) -> None:
        self.events.append("close")
        self.closed = self._close_proved

    async def wake(self, *, operation_lease_held: bool) -> dict[str, object]:
        assert operation_lease_held is True
        self.events.append("wake")
        return self._wake_result

    async def settle_after_data_loss(self) -> None:
        assert self.closed is True
        self.events.append("settle")

    def reset_mutable_data(self):
        assert self.closed is True
        self.events.append("delete")
        return reset_memory_data_roots(self.effective_home)

    def mark_needs_repair(self, reason: str) -> None:
        self.marked_reason = reason
        self.needs_repair = True


def _controller(runtime: _Runtime, *, enabled: bool = True) -> Controller:
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    controller.config = SimpleNamespace(memory=MemoryConfig(enabled=enabled))
    return controller


def _roots(home: Path) -> None:
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    (home / "memory").mkdir(mode=0o700)
    (home / "state").mkdir(mode=0o700)
    (home / "state" / "memory").mkdir(mode=0o700)


def _persist(monkeypatch: pytest.MonkeyPatch, controller: Controller) -> None:
    def update(transform):
        return SimpleNamespace(memory=transform(controller.config.memory))

    monkeypatch.setattr("core.controller.atomic_update_memory", update)


@pytest.mark.asyncio
async def test_reset_reaches_plugin_boundary_before_importing_reset_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = Controller.__new__(Controller)

    @asynccontextmanager
    async def unavailable_runtime():
        raise MemoryPluginUnavailableError("implementation missing")
        yield

    controller._destructive_memory_runtime = unavailable_runtime
    real_import = builtins.__import__

    def block_reset_helper(name, *args, **kwargs):
        if name == "avibe_memory.data_reset":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_reset_helper)

    with pytest.raises(MemoryPluginUnavailableError, match="implementation missing"):
        await controller._reset_memory_data_transaction(operation="delete_data")


def test_reset_memory_data_roots_deletes_only_exact_confined_roots(tmp_path: Path) -> None:
    _roots(tmp_path)
    memory_state = tmp_path / "state" / "memory" / "memory.sqlite"
    memory_state.write_text("stable-memory-identity", encoding="utf-8")
    stable = tmp_path / "state" / "identity.json"
    stable.write_text("stable", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    result = reset_memory_data_roots(tmp_path)

    assert result.data_deleted is True
    assert result.data_remaining is False
    assert not (tmp_path / "memory").exists()
    assert memory_state.read_text(encoding="utf-8") == "stable-memory-identity"
    assert stable.read_text(encoding="utf-8") == "stable"
    assert outside.read_text(encoding="utf-8") == "keep"


def test_reset_memory_data_roots_removes_only_named_retired_recovery_residue(
    tmp_path: Path,
) -> None:
    _roots(tmp_path)
    retired = tmp_path / "state" / "memory" / "clear-intent.json"
    retired.write_text("{}", encoding="utf-8")
    unrelated = tmp_path / "state" / "memory" / "operator-note.txt"
    unrelated.write_text("keep", encoding="utf-8")

    result = reset_memory_data_roots(tmp_path)

    assert result.data_remaining is False
    assert not retired.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_reset_memory_data_roots_fails_closed_on_special_entry(tmp_path: Path) -> None:
    """MEMORY-DELETE-DATA-002: unsafe contents are never deleted recursively."""

    _roots(tmp_path)
    unsafe = tmp_path / "memory" / "pipe"
    os.mkfifo(unsafe, mode=0o600)

    result = reset_memory_data_roots(tmp_path)

    primary = result.roots[0]
    assert primary.error == "ConfinedFilesystemError"
    assert result.data_remaining is True
    assert stat.S_ISFIFO(os.lstat(unsafe).st_mode)


@pytest.mark.asyncio
async def test_repair_requires_exact_loss_confirmation(tmp_path: Path) -> None:
    """MEMORY-REPAIR-201: Repair requires exact accepted-loss authority."""

    runtime = _Runtime(tmp_path, needs_repair=True)
    controller = _controller(runtime)

    result = await controller.repair_memory(confirm_loss=False)

    assert result == {
        "ok": False,
        "operation": "repair",
        "error": "memory_loss_confirmation_required",
        "result": "unchanged",
    }
    assert runtime.events == []


@pytest.mark.asyncio
async def test_repair_is_not_offered_for_degraded_runtime(tmp_path: Path) -> None:
    """MEMORY-REPAIR-201: degraded state grants no Repair authority."""

    runtime = _Runtime(tmp_path, needs_repair=False)
    controller = _controller(runtime)

    result = await controller.repair_memory(confirm_loss=True)

    assert result["error"] == "memory_repair_not_required"
    assert runtime.events == []


@pytest.mark.asyncio
async def test_repair_stops_owned_runtime_before_delete_and_proves_native_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-REPAIR-202: Repair proves stop, reset, and native readiness."""

    _roots(tmp_path)
    runtime = _Runtime(tmp_path, needs_repair=True)
    fresh = _Runtime(tmp_path)
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    _persist(monkeypatch, controller)
    events = runtime.events

    result = await controller.repair_memory(confirm_loss=True)

    assert result["ok"] is True
    assert result["state"] == "running"
    assert events == ["reap", "retire", "close", "settle", "delete"]
    assert fresh.events == ["wake"]
    assert controller.memory_runtime is fresh


@pytest.mark.asyncio
async def test_repair_deletes_nothing_when_termination_is_unproved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-REPAIR-203: failed termination proof grants no deletion."""

    _roots(tmp_path)
    runtime = _Runtime(tmp_path, needs_repair=True, close_proved=False)
    controller = _controller(runtime)
    _persist(monkeypatch, controller)
    result = await controller.repair_memory(confirm_loss=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_termination_unproved"
    assert result["state"] == "needs_repair"
    assert result["data_deleted"] is False
    assert "delete" not in runtime.events
    assert runtime.marked_reason == "memory_repair_failed"
    assert (tmp_path / "memory").is_dir()


@pytest.mark.asyncio
async def test_external_post_reset_wake_failure_remains_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-REPAIR-205: external activation failures remain degraded."""

    _roots(tmp_path)
    runtime = _Runtime(tmp_path, needs_repair=True)
    fresh = _Runtime(
        tmp_path,
        wake_result={
            "ok": False,
            "state": "degraded",
            "error": "memory_permission_denied",
        },
    )
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    _persist(monkeypatch, controller)
    result = await controller.delete_memory_data(confirm_loss=True)

    assert result["ok"] is False
    assert result["result"] == "deleted_readiness_failed"
    assert result["state"] == "degraded"
    assert result["data_deleted"] is True
    assert fresh.marked_reason is None
    assert await controller.repair_memory(confirm_loss=True) == {
        "ok": False,
        "operation": "repair",
        "error": "memory_repair_not_required",
        "result": "unchanged",
    }


@pytest.mark.asyncio
async def test_local_post_reset_wake_failure_remains_needs_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-REPAIR-204: local post-reset failure remains repairable."""

    _roots(tmp_path)
    runtime = _Runtime(tmp_path, needs_repair=True)
    fresh = _Runtime(
        tmp_path,
        needs_repair=True,
        wake_result={
            "ok": False,
            "state": "needs_repair",
            "error": "memory_local_data_unusable",
        },
    )
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    _persist(monkeypatch, controller)
    result = await controller.repair_memory(confirm_loss=True)

    assert result["ok"] is False
    assert result["state"] == "needs_repair"
    assert result["error"] == "memory_local_data_unusable"
    assert fresh.marked_reason is None


@pytest.mark.asyncio
async def test_delete_data_has_distinct_intent_but_reuses_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-DELETE-DATA-001: explicit deletion uses the shared reset."""

    _roots(tmp_path)
    runtime = _Runtime(tmp_path)
    fresh = _Runtime(tmp_path)
    runtime.replacement_runtime = fresh
    controller = _controller(runtime, enabled=False)
    _persist(monkeypatch, controller)
    result = await controller.delete_memory_data(confirm_loss=True)

    assert result["ok"] is True
    assert result["operation"] == "delete_data"
    assert result["state"] == "disabled"
    assert result["data_deleted"] is True
    assert fresh.events == []


@pytest.mark.asyncio
async def test_delete_data_lazily_constructs_runtime_after_disabled_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roots(tmp_path)
    runtime = _Runtime(tmp_path)
    controller = _controller(runtime, enabled=False)
    controller.memory_runtime = None
    controller.memory_module = None
    controller.memory_adapter = DisabledMemoryAdapter()
    created: list[MemoryConfig] = []
    loader_flags: list[bool] = []

    def create_memory_runtime(
        config: MemoryConfig,
        **kwargs: object,
    ) -> _Runtime:
        created.append(config)
        loader_flags.append(bool(kwargs["allow_disabled"]))
        return runtime

    monkeypatch.setattr(controller, "_create_memory_runtime", create_memory_runtime)
    _persist(monkeypatch, controller)

    result = await controller.delete_memory_data(confirm_loss=True)

    assert result["ok"] is True
    assert result["state"] == "disabled"
    assert result["data_deleted"] is True
    assert created == [controller.config.memory]
    assert loader_flags == [True]
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert runtime.replacement_configs == []


@pytest.mark.asyncio
async def test_disable_vs_delete_race_closes_temporary_runtime_on_early_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued delete decides temporary ownership only after taking the gate."""

    old_runtime = _Runtime(tmp_path)
    controller = _controller(old_runtime)
    controller.memory_adapter = None
    controller._memory_reconcile_task = None
    temporary = _Runtime(tmp_path)
    created: list[MemoryConfig] = []

    def create_memory_runtime(
        config: MemoryConfig,
        **_kwargs: object,
    ) -> _Runtime:
        created.append(config)
        return temporary

    class _BusyLease:
        def __init__(self, _home: Path) -> None:
            pass

        def acquire(self) -> None:
            from config.memory_operation_lock import MemoryOperationBusy

            raise MemoryOperationBusy("busy")

        def release(self) -> None:
            raise AssertionError("an unacquired lease must not be released")

    monkeypatch.setattr(controller, "_create_memory_runtime", create_memory_runtime)
    monkeypatch.setattr("core.controller.MemoryOperationLease", _BusyLease)

    gate = controller._memory_replacement_lock()
    await gate.acquire()
    operation = asyncio.create_task(
        controller.delete_memory_data(confirm_loss=True)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    disabled = MemoryConfig(enabled=False)
    controller.config.memory = disabled
    controller.memory_runtime = None
    controller.memory_module = None
    controller.memory_adapter = DisabledMemoryAdapter()
    gate.release()

    result = await operation

    assert result == {
        "ok": False,
        "operation": "delete_data",
        "error": "memory_operation_in_progress",
        "result": "unchanged",
    }
    assert created == [disabled]
    assert temporary.events == ["retire", "close"]
    assert temporary.closed is True
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_partial_deletion_persists_repair_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roots(tmp_path)
    os.mkfifo(tmp_path / "memory" / "pipe", mode=0o600)
    runtime = _Runtime(tmp_path)
    controller = _controller(runtime, enabled=False)
    _persist(monkeypatch, controller)
    result = await controller.delete_memory_data(confirm_loss=True)

    assert result["result"] == "partial"
    assert result["state"] == "needs_repair"
    assert controller.config.memory.legacy_needs_repair is True
    assert runtime.replacement_configs == []
    assert controller.memory_runtime is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)


@pytest.mark.asyncio
async def test_reconfigure_keeps_confirmed_identity_when_readiness_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roots(tmp_path)
    runtime = _Runtime(tmp_path)
    fresh = _Runtime(
        tmp_path,
        wake_result={"ok": False, "state": "degraded", "error": "memory_wake_failed"},
    )
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    target = MemoryConfig(enabled=True)
    target.processing.embedding = MemoryEndpointConfig(
        base_url="https://embedding.example.test/v1",
        model="embed-v2",
        api_key="secret",
    )
    persisted: list[MemoryConfig] = []

    def update(transform):
        value = transform(controller.config.memory)
        persisted.append(value)
        runtime.events.append(
            "persist" if value.runtime_embedding_identity() == target.runtime_embedding_identity() else "verify"
        )
        return SimpleNamespace(memory=value)

    monkeypatch.setattr("core.controller.atomic_update_memory", update)
    result = await controller.reconfigure_memory(
        target,
        expected_config=controller.config.memory,
        confirm_loss=True,
    )

    assert result["ok"] is False
    assert result["state"] == "degraded"
    assert persisted == [
        MemoryConfig(enabled=True, legacy_needs_repair=True),
        target,
    ]
    assert runtime.events.index("delete") < runtime.events.index("persist")
    assert controller.config.memory == target
    assert fresh.marked_reason is None


@pytest.mark.asyncio
async def test_reconfigure_accepts_scope_release_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig(
            "https://llm.example.test/v1",
            "chat-v1",
            "llm-secret",
        ),
        embedding=MemoryEndpointConfig(
            "https://embedding.example.test/v1",
            "embedding-v1",
            "embedding-secret",
        ),
    )
    expected = MemoryConfig(
        enabled=True,
        mode="custom",
        processing=processing,
        cloud=MemoryCloudConfig(
            scope="platform",
            transition_notice_pending=True,
            applied_embedding_identity="emb-org",
        ),
    )
    candidate = deepcopy(expected)
    candidate.cloud.transition_notice_pending = False
    candidate.cloud.applied_embedding_identity = "emb-platform"
    controller = _controller(runtime)
    controller.config.memory = expected
    calls: list[dict[str, object]] = []

    async def reset(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "operation": "reconfigure"}

    monkeypatch.setattr(controller, "_reset_memory_data", reset)

    result = await controller.reconfigure_memory(
        candidate,
        expected_config=expected,
        confirm_loss=True,
    )

    assert result == {"ok": True, "operation": "reconfigure"}
    assert calls == [
        {
            "operation": "reconfigure",
            "target_config": candidate,
            "expected_config": expected,
        }
    ]


@pytest.mark.asyncio
async def test_reconfigure_rejects_a_stale_memory_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    controller = _controller(runtime)
    expected = controller.config.memory
    target = MemoryConfig(enabled=True)
    target.processing.embedding = MemoryEndpointConfig(
        base_url="https://embedding.example.test/v1",
        model="embed-v2",
        api_key="secret",
    )

    def update(transform):
        concurrent = MemoryConfig(enabled=False)
        return SimpleNamespace(memory=transform(concurrent))

    monkeypatch.setattr("core.controller.atomic_update_memory", update)

    result = await controller.reconfigure_memory(
        target,
        expected_config=expected,
        confirm_loss=True,
    )

    assert result == {
        "ok": False,
        "operation": "reconfigure",
        "error": "memory_operation_in_progress",
        "result": "unchanged",
    }
    assert runtime.events == ["reap"]


@pytest.mark.asyncio
async def test_reconfigure_does_not_overwrite_a_change_racing_with_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roots(tmp_path)
    runtime = _Runtime(tmp_path)
    fresh = _Runtime(tmp_path)
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    expected = controller.config.memory
    target = MemoryConfig(enabled=True)
    target.processing.embedding = MemoryEndpointConfig(
        base_url="https://embedding.example.test/v1",
        model="embed-v2",
        api_key="secret",
    )
    concurrent = MemoryConfig(enabled=False)
    updates = 0

    def update(transform):
        nonlocal updates
        updates += 1
        runtime.events.append("verify" if updates == 1 else "persist")
        current = expected if updates == 1 else concurrent
        return SimpleNamespace(memory=transform(current))

    monkeypatch.setattr("core.controller.atomic_update_memory", update)
    monkeypatch.setattr(
        "core.controller.V2Config.load",
        classmethod(lambda cls: SimpleNamespace(memory=concurrent)),
    )
    result = await controller.reconfigure_memory(
        target,
        expected_config=expected,
        confirm_loss=True,
    )

    assert result["ok"] is False
    assert result["error"] == "memory_operation_in_progress"
    assert result["result"] == "deleted_config_not_applied"
    assert result["data_deleted"] is True
    assert controller.config.memory == concurrent
    assert runtime.events.index("delete") < runtime.events.index("persist")
    assert fresh.events == []


@pytest.mark.asyncio
async def test_cancelled_reset_joins_deletion_before_releasing_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roots(tmp_path)
    runtime = _Runtime(tmp_path)
    fresh = _Runtime(tmp_path)
    runtime.replacement_runtime = fresh
    controller = _controller(runtime, enabled=False)
    _persist(monkeypatch, controller)
    deletion_started = threading.Event()
    allow_deletion_to_finish = threading.Event()
    lease_events: list[str] = []

    class Lease:
        def __init__(self, _home: Path) -> None:
            pass

        def acquire(self) -> None:
            lease_events.append("acquire")

        def release(self) -> None:
            lease_events.append("release")

    def blocking_reset():
        runtime.events.append("delete")
        deletion_started.set()
        allow_deletion_to_finish.wait(timeout=5)
        return reset_memory_data_roots(tmp_path)

    runtime.reset_mutable_data = blocking_reset
    monkeypatch.setattr("core.controller.MemoryOperationLease", Lease)
    task = asyncio.create_task(controller.delete_memory_data(confirm_loss=True))
    assert await asyncio.to_thread(deletion_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert lease_events == ["acquire"]
    assert len(controller._memory_destructive_tasks) == 1

    allow_deletion_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease_events == ["acquire", "release"]
    assert controller._memory_destructive_tasks == set()
    assert runtime.events == ["reap", "retire", "close", "settle", "delete"]
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert runtime.replacement_configs == []


@pytest.mark.asyncio
async def test_shutdown_join_settles_accepted_destructive_transactions() -> None:
    controller = Controller.__new__(Controller)
    controller._memory_destructive_quiescing = False
    controller._memory_destructive_tasks = set()
    controller._shutdown_tainted = False
    started = asyncio.Event()
    release = asyncio.Event()

    async def transaction() -> dict[str, object]:
        started.set()
        await release.wait()
        return {"ok": True}

    task = asyncio.create_task(transaction())
    controller._memory_destructive_tasks.add(task)
    joining = asyncio.create_task(controller._join_memory_destructive_transactions())
    await started.wait()
    await asyncio.sleep(0)

    assert joining.done() is False
    assert controller._memory_destructive_quiescing is True

    release.set()
    await joining

    assert task.done() is True
    assert controller._memory_destructive_tasks == set()
    assert controller._shutdown_tainted is False


@pytest.mark.asyncio
async def test_shutdown_quiesce_rejects_new_destructive_transactions(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path)
    controller = _controller(runtime)
    controller._memory_destructive_quiescing = True

    result = await controller.delete_memory_data(confirm_loss=True)

    assert result == {
        "ok": False,
        "operation": "delete_data",
        "error": "memory_operation_in_progress",
        "result": "unchanged",
    }
    assert runtime.events == []

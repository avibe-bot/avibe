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
from config.memory_operation_lock import MemoryOperationLease
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
        fail_stage: str | None = None,
        wake_result: dict[str, object] | None = None,
    ) -> None:
        self.effective_home = home
        self.needs_repair = needs_repair
        self.close_completed = False
        self.closing = False
        self.module = object()
        self.capture_adapter = DisabledMemoryAdapter()
        self._artifact_installing = False
        self._fail_stage = fail_stage
        self._wake_result = wake_result or {"ok": True, "state": "running"}
        self.events: list[str] = []
        self.marked_reason: str | None = None
        self.replacement_runtime: _Runtime | None = None
        self.replacement_configs: list[MemoryConfig] = []
        self.root_ownership = object()
        self.root_handoffs: list[object] = []
        self.root_released = False

    async def prepare_data_reset(self) -> None:
        self.events.append("reap")

    def _fail_once(self, stage: str) -> None:
        if self._fail_stage == stage:
            self._fail_stage = None
            raise RuntimeError(f"{stage} failed")

    def start_capture_adapter(self, **_options: object) -> bool:
        return True

    def replacement(self, config: MemoryConfig, root_ownership: object) -> _Runtime:
        assert root_ownership is self.root_ownership
        self.replacement_configs.append(config)
        if self.replacement_runtime is not None:
            return self.replacement_runtime
        return _Runtime(
            self.effective_home,
            needs_repair=config.legacy_needs_repair,
        )

    def begin_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.events.append("begin_close")

    def begin_root_ownership_handoff(self) -> object:
        self.begin_close()
        self.root_handoffs.append(self.root_ownership)
        return self.root_ownership

    def accept_root_ownership(self) -> None:
        return None

    async def close(
        self,
        *,
        root_ownership: object | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        del timeout_seconds
        if root_ownership is not None:
            assert root_ownership is self.root_ownership
        self.events.append("close")
        self._fail_once("close")
        self.close_completed = True

    async def wake(self, *, operation_lease_held: bool) -> dict[str, object]:
        assert operation_lease_held is True
        self.events.append("wake")
        return self._wake_result

    async def settle_after_data_loss(self, root_ownership: object) -> None:
        assert root_ownership is self.root_ownership
        assert self.close_completed is True
        self._fail_once("settle")
        self.events.append("settle")

    def reset_mutable_data(self, root_ownership: object):
        assert root_ownership is self.root_ownership
        assert self.close_completed is True
        self._fail_once("delete")
        self.events.append("delete")
        return reset_memory_data_roots(self.effective_home)

    def release_root_ownership(self, root_ownership: object) -> None:
        assert root_ownership is self.root_ownership
        self.root_released = True

    def release_retained_root_ownership(self) -> None:
        self.root_released = True

    def mark_needs_repair(self, reason: str) -> None:
        self.marked_reason = reason
        self.needs_repair = True


def _controller(runtime: _Runtime | None, *, enabled: bool = True) -> Controller:
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module if runtime is not None else None
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

    controller._memory_runtime_for_data_reset = unavailable_runtime
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


@pytest.mark.parametrize(
    ("needs_repair", "confirm_loss", "error"),
    (
        (True, False, "memory_loss_confirmation_required"),
        (False, True, "memory_repair_not_required"),
    ),
)
@pytest.mark.asyncio
async def test_repair_requires_exact_loss_and_repair_authority(
    needs_repair: bool,
    confirm_loss: bool,
    error: str,
    tmp_path: Path,
) -> None:
    """MEMORY-REPAIR-201: Repair requires loss and needs-repair authority."""

    runtime = _Runtime(tmp_path, needs_repair=needs_repair)
    controller = _controller(runtime)

    result = await controller.repair_memory(confirm_loss=confirm_loss)

    assert result["error"] == error
    assert runtime.events == []


@pytest.mark.parametrize("failure_stage", (None, "close", "settle", "delete"))
@pytest.mark.asyncio
async def test_repair_reuses_retained_owner_until_reset_proves(
    failure_stage: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-REPAIR-202/203: failed reset phases retain one retryable owner."""

    _roots(tmp_path)
    runtime = _Runtime(
        tmp_path,
        needs_repair=True,
        fail_stage=failure_stage,
    )
    fresh = _Runtime(tmp_path)
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    _persist(monkeypatch, controller)
    if failure_stage == "delete":
        with pytest.raises(RuntimeError, match="delete failed"):
            await controller.repair_memory(confirm_loss=True)
    else:
        first = await controller.repair_memory(confirm_loss=True)
        if failure_stage is None:
            result = first
        else:
            assert first["ok"] is False
            assert first["state"] == "needs_repair"
    if failure_stage is not None:
        assert controller.memory_runtime is runtime
        assert runtime.root_released is False
        assert fresh.events == []
        result = await controller.repair_memory(confirm_loss=True)

    assert result["ok"] is True
    assert result["state"] == "running"
    assert all(token is runtime.root_ownership for token in runtime.root_handoffs)
    assert len(runtime.root_handoffs) == (1 if failure_stage is None else 2)
    assert fresh.events == ["wake"]
    assert controller.memory_runtime is fresh


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
    assert result["operation"] == "delete_data"
    assert result["state"] == "disabled"
    assert result["data_deleted"] is True
    assert created == [controller.config.memory]
    assert loader_flags == [True]
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert runtime.replacement_configs == []


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


@pytest.mark.parametrize("write_failure", ("stale", "error"))
@pytest.mark.asyncio
async def test_reconfigure_fence_failure_restores_exact_enabled_runtime(
    write_failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    def update(transform):
        if write_failure == "error":
            raise OSError("write failed")
        concurrent = MemoryConfig(enabled=False)
        return SimpleNamespace(memory=transform(concurrent))

    monkeypatch.setattr("core.controller.atomic_update_memory", update)
    monkeypatch.setattr(
        "core.controller.V2Config.load",
        classmethod(lambda cls: SimpleNamespace(memory=expected)),
    )

    result = await controller.reconfigure_memory(
        target,
        expected_config=expected,
        confirm_loss=True,
    )

    assert result["ok"] is False
    assert result["error"] == (
        "memory_operation_in_progress"
        if write_failure == "stale"
        else "memory_reconfigure_failed"
    )
    assert result["state"] == "running"
    assert runtime.events == ["reap", "begin_close", "close"]
    assert runtime.replacement_configs == [expected]
    assert fresh.events == ["wake"]
    assert controller.memory_runtime is fresh


@pytest.mark.asyncio
async def test_unpublished_reset_blocks_concurrent_enable_before_construction(
    tmp_path: Path,
) -> None:
    controller = _controller(None, enabled=False)
    controller.memory_adapter = DisabledMemoryAdapter()
    controller._memory_plugin_error = None
    runtime = _Runtime(tmp_path)
    reset_started = asyncio.Event()
    reset_release = asyncio.Event()
    lease = threading.Lock()
    construction_calls: list[object] = []

    async def try_lease(_effective_home=None):
        if not lease.acquire(blocking=False):
            return None
        return SimpleNamespace(release=lease.release)

    async def prepare_data_reset() -> None:
        reset_started.set()
        await reset_release.wait()

    runtime.prepare_data_reset = prepare_data_reset
    controller._try_memory_operation_lease = try_lease
    controller._create_memory_runtime = lambda config, **_kwargs: (
        construction_calls.append(config) or runtime
    )
    reset = asyncio.create_task(
        controller._reset_memory_data_transaction(operation="delete_data")
    )
    await reset_started.wait()

    assert await controller.reconcile_memory(MemoryConfig(enabled=True)) == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    assert construction_calls == [controller.config.memory]
    assert controller.memory_runtime is None

    reset.cancel()
    reset_release.set()
    with pytest.raises(asyncio.CancelledError):
        await reset
    assert runtime.close_completed is True


@pytest.mark.asyncio
async def test_reset_lost_runtime_writes_no_fence_and_leaves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(tmp_path)
    replacement = _Runtime(tmp_path)
    controller = _controller(runtime)
    replacement_adapter = object()

    async def lose_expected_runtime() -> None:
        runtime.events.append("reap")
        controller.memory_runtime = replacement
        controller.memory_module = replacement.module
        controller.memory_adapter = replacement_adapter

    runtime.prepare_data_reset = lose_expected_runtime
    monkeypatch.setattr(
        "core.controller.atomic_update_memory",
        lambda _transform: pytest.fail("lost ownership must not persist a fence"),
    )

    result = await controller.delete_memory_data(confirm_loss=True)

    assert result == {
        "ok": False,
        "operation": "delete_data",
        "error": "memory_operation_in_progress",
        "result": "unchanged",
    }
    assert runtime.events == ["reap"]
    assert replacement.events == []
    assert controller.memory_runtime is replacement
    assert controller.memory_module is replacement.module
    assert controller.memory_adapter is replacement_adapter


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
async def test_reset_disable_and_cancellation_retain_owner_until_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _roots(tmp_path)
    runtime = _Runtime(tmp_path)
    fresh = _Runtime(tmp_path)
    runtime.replacement_runtime = fresh
    controller = _controller(runtime)
    _persist(monkeypatch, controller)
    deletion_started = threading.Event()
    allow_deletion_to_finish = threading.Event()

    def blocking_reset(_root_ownership: object):
        runtime.events.append("delete")
        deletion_started.set()
        allow_deletion_to_finish.wait(timeout=5)
        return reset_memory_data_roots(tmp_path)

    runtime.reset_mutable_data = blocking_reset
    task = asyncio.create_task(controller.delete_memory_data(confirm_loss=True))
    assert await asyncio.to_thread(deletion_started.wait, 2)
    disabled = MemoryConfig(enabled=False)
    assert await controller.reconcile_memory(disabled) == {
        "ok": False,
        "state": "disabled",
        "error": "memory_operation_in_progress",
    }
    assert controller.memory_runtime is runtime
    assert controller.config.memory == disabled
    task.cancel()
    await asyncio.sleep(0.05)

    assert task.done() is False
    assert len(controller._memory_destructive_tasks) == 1

    allow_deletion_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    lease.release()
    assert controller._memory_destructive_tasks == set()
    assert runtime.events == ["reap", "begin_close", "close", "settle", "delete"]
    assert controller.memory_runtime is None
    assert controller.memory_module is None
    assert controller.config.memory == disabled
    assert isinstance(controller.memory_adapter, DisabledMemoryAdapter)
    assert runtime.replacement_configs == []
    assert runtime.root_released is True


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

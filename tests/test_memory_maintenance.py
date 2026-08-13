from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import stat
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import threading

import pytest

import core.memory.snapshot as snapshot_module
from config.v2_config import MemoryConfig
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.clear_journal import MemoryClearJournal
from core.memory.clear_snapshot_storage import MemoryClearSnapshotStorage
from core.memory.everos import FakeMemoryProvider
from core.memory.process import SidecarOwnership
from core.memory.maintenance import (
    ClearRecoveryResult,
    ClearResult,
    MaintenanceObservation,
    MemoryMaintenance,
)
from core.memory.runtime import MemoryRuntime
from core.memory.snapshot import MemorySnapshotManager
from core.memory.store import AmbiguousAdd, MemoryStore
from core.memory.types import (
    CaptureAccepted,
    CaptureAttachment,
    CaptureRequest,
    CaptureSkipped,
)


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "default"


def _maintenance(runtime: MemoryRuntime) -> MemoryMaintenance:
    maintenance = runtime._maintenance
    assert maintenance is not None
    return maintenance


def _replace_runtime_port(runtime: MemoryRuntime, **changes) -> None:
    maintenance = _maintenance(runtime)
    maintenance._runtime = replace(maintenance._runtime, **changes)


def _clear_payload(result: ClearResult) -> dict:
    if result.status == "failed":
        recovery = result.recovery
        return {
            "status": "failed",
            "error": result.error,
            "recovery": (
                None
                if recovery is None
                else {
                    "state": recovery.state,
                    "operation_id": recovery.operation_id,
                    "occurred_at": recovery.occurred_at,
                    "error_code": recovery.error_code,
                    "can_resume": recovery.can_resume,
                    "can_abort": recovery.can_abort,
                }
            ),
        }
    return {
        "status": result.status,
        "operation_id": result.operation_id,
        "epoch": result.epoch,
    }


async def _clear(runtime: MemoryRuntime, *, operator_ref: str) -> dict:
    return _clear_payload(
        await _maintenance(runtime).clear(operator_ref=operator_ref)
    )


async def _resume_clear(
    runtime: MemoryRuntime,
    operation_id: str,
    *,
    operator_ref: str,
) -> dict:
    return _clear_payload(
        await _maintenance(runtime).resume_clear(
            operation_id,
            operator_ref=operator_ref,
        )
    )


async def _abort_clear(
    runtime: MemoryRuntime,
    operation_id: str,
    *,
    operator_ref: str,
) -> dict:
    return _clear_payload(
        await _maintenance(runtime).abort_clear(
            operation_id,
            operator_ref=operator_ref,
        )
    )


async def test_runtime_preserves_public_clear_payloads_when_delegating(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    results = iter(
        (
            ClearResult(status="completed", operation_id="clear-1", epoch=3),
            ClearResult(
                status="failed",
                error="memory_clear_failed",
                recovery=ClearRecoveryResult(
                    state="recovery_needed",
                    operation_id="clear-2",
                    occurred_at="2026-01-01T00:00:00.000Z",
                    error_code="memory_clear_failed",
                    can_resume=True,
                    can_abort=False,
                ),
            ),
        )
    )

    async def delegated_clear(*, operator_ref: str) -> ClearResult:
        assert operator_ref == "user:owner"
        return next(results)

    monkeypatch.setattr(_maintenance(runtime), "clear", delegated_clear)

    assert await runtime.clear(operator_ref="user:owner") == {
        "status": "completed",
        "operation_id": "clear-1",
        "epoch": 3,
    }
    assert await runtime.clear(operator_ref="user:owner") == {
        "status": "failed",
        "error": "memory_clear_failed",
        "recovery": {
            "state": "recovery_needed",
            "operation_id": "clear-2",
            "occurred_at": "2026-01-01T00:00:00.000Z",
            "error_code": "memory_clear_failed",
            "can_resume": True,
            "can_abort": False,
        },
    }
    await runtime.close()


@pytest.mark.parametrize(
    "failed_dependency",
    ("MemoryClearJournal", "MemoryBackupRestoreJournal"),
)
async def test_maintenance_refuses_clear_when_a_journal_cannot_initialize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_dependency: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def fail_initialization(*_args, **_kwargs):
        raise OSError("injected journal initialization failure")

    monkeypatch.setattr(
        f"core.memory.maintenance.{failed_dependency}",
        fail_initialization,
    )
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    assert runtime.available is True
    assert (await runtime.maintenance_payload())["can_clear"] is False
    await runtime.close()


async def test_maintenance_advertises_clear_only_for_a_healthy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    assert runtime.available is True
    assert (await runtime.maintenance_payload())["can_clear"] is True
    await runtime.close()


async def test_maintenance_observes_clear_recovery_with_one_journal_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    maintenance = _maintenance(runtime)
    journal = maintenance._clear_journal
    assert journal is not None
    operation = journal.start(
        operation_id="single-observation",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None

    connection_count = 0
    original_connect = journal._connect

    def observed_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    monkeypatch.setattr(journal, "_connect", observed_connect)

    observation = await maintenance.observe(operator_ref="user:owner")

    assert observation.clear_recovery == ClearRecoveryResult(
        state="recovery_needed",
        operation_id=operation.operation_id,
        occurred_at=recovery.updated_at,
        error_code="memory_clear_failed",
        can_resume=True,
        can_abort=False,
    )
    assert connection_count == 1
    await runtime.close()


@pytest.mark.parametrize("failure_point", ("snapshot_manager", "boot_marker"))
async def test_partial_maintenance_initialization_still_projects_clear_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    journal = MemoryClearJournal(tmp_path)
    operation = journal.start(
        operation_id=f"clear-before-{failure_point}-failure",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    recovery = journal.mark_boot_recovery_needed()
    assert recovery is not None

    def fail_initialization(*_args, **_kwargs):
        raise OSError(f"injected {failure_point} initialization failure")

    if failure_point == "snapshot_manager":
        monkeypatch.setattr(
            "core.memory.maintenance.MemorySnapshotManager",
            fail_initialization,
        )
    else:
        monkeypatch.setattr(
            "core.memory.maintenance.MemoryBackupRestoreJournal."
            "mark_boot_recovery_needed",
            fail_initialization,
        )

    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    maintenance = _maintenance(runtime)
    assert maintenance.ready is False

    observation = await maintenance.observe(operator_ref="user:owner")
    assert observation.block_reason == "memory_store_unavailable"
    assert observation.can_clear is False
    assert observation.clear_recovery == ClearRecoveryResult(
        state="recovery_needed",
        operation_id=operation.operation_id,
        occurred_at=recovery.updated_at,
        error_code="memory_clear_failed",
        can_resume=False,
        can_abort=False,
    )

    payload = await runtime.maintenance_payload(operator_ref="user:owner")
    assert payload["can_clear"] is False
    assert payload["clear_recovery"] == {
        "state": "recovery_needed",
        "operation_id": operation.operation_id,
        "occurred_at": recovery.updated_at,
        "error_code": "memory_clear_failed",
        "can_resume": False,
        "can_abort": False,
    }
    await runtime.close()


async def test_maintenance_revalidates_journals_after_store_metadata_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    maintenance = runtime._maintenance
    assert maintenance is not None
    initial = MaintenanceObservation(None, None, True)
    latest = MaintenanceObservation("busy", None, False)
    observations = iter((initial, latest))

    async def observe(*, operator_ref: str | None = None) -> MaintenanceObservation:
        del operator_ref
        return next(observations)

    monkeypatch.setattr(maintenance, "observe", observe)

    result = await maintenance.maintenance_payload(operator_ref="user:owner")

    assert result.can_clear is False
    assert result.clear_recovery is latest.clear_recovery
    await runtime.close()


async def test_maintenance_drops_old_recovery_when_freshness_check_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    maintenance = runtime._maintenance
    assert maintenance is not None
    recovery = ClearRecoveryResult(
        state="recovery_needed",
        operation_id="clear-stale",
        occurred_at="2026-08-10T00:00:00.000Z",
        error_code="memory_clear_failed",
        can_resume=True,
        can_abort=True,
    )
    initial = MaintenanceObservation("memory_clear_failed", recovery, False)

    async def failed_observe(*, operator_ref: str | None = None) -> MaintenanceObservation:
        del operator_ref
        raise OSError("journal unavailable")

    monkeypatch.setattr(maintenance, "observe", failed_observe)

    result = await maintenance.maintenance_payload(
        operator_ref="user:owner",
        observation=initial,
    )

    assert result.can_clear is False
    assert result.clear_recovery is None
    assert result.error == "memory_store_unavailable"
    await runtime.close()


async def test_maintenance_refuses_clear_when_the_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def fail_initialization(*_args, **_kwargs):
        raise OSError("injected store initialization failure")

    monkeypatch.setattr("core.memory.runtime.MemoryStore", fail_initialization)
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    assert runtime.available is False
    assert (await runtime.maintenance_payload())["can_clear"] is False
    await runtime.close()


async def test_unavailable_store_still_projects_durable_clear_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    operation = MemoryClearJournal(tmp_path).start(
        operation_id="clear-before-store-failure",
        operator_ref="user:owner",
        pre_epoch=2,
        target_epoch=3,
    )

    def fail_initialization(*_args, **_kwargs):
        raise OSError("injected store initialization failure")

    monkeypatch.setattr("core.memory.runtime.MemoryStore", fail_initialization)
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)

    fence_entered = False

    @asynccontextmanager
    async def observed_fence():
        nonlocal fence_entered
        fence_entered = True
        yield

    _replace_runtime_port(runtime, exclusive_fence=observed_fence)
    before = await runtime.maintenance_payload(operator_ref="user:owner")
    assert before["status"] == "ok"
    assert before["data_exists"] is True
    assert before["can_clear"] is False
    assert before["clear_recovery"] == {
        "state": "recovery_needed",
        "operation_id": operation.operation_id,
        "occurred_at": before["clear_recovery"]["occurred_at"],
        "error_code": "memory_clear_failed",
        "can_resume": True,
        "can_abort": False,
    }

    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert _maintenance(runtime)._backup_stage_reconcile_task is None
    await asyncio.sleep(0)
    assert fence_entered is False
    assert await runtime.maintenance_payload(operator_ref="user:owner") == before
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("failure_point", ("provider_root", "module"))
async def test_supplied_store_attaches_only_after_module_initialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    operation = MemoryClearJournal(tmp_path).start(
        operation_id=f"clear-before-{failure_point}-failure",
        operator_ref="user:owner",
        pre_epoch=4,
        target_epoch=5,
    )
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite")
    artifact = FakeMemoryArtifactManager()

    def fail_initialization(*_args, **_kwargs):
        raise OSError(f"injected {failure_point} initialization failure")

    if failure_point == "provider_root":
        monkeypatch.setattr(artifact, "set_provider_root", fail_initialization)
    else:
        monkeypatch.setattr("core.memory.runtime.MemoryModule", fail_initialization)

    runtime = memory_runtime_factory(
        MemoryConfig(),
        store=store,
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    maintenance = _maintenance(runtime)
    assert runtime.available is False
    assert maintenance._store is None

    fence_entered = False

    @asynccontextmanager
    async def observed_fence():
        nonlocal fence_entered
        fence_entered = True
        yield

    _replace_runtime_port(runtime, exclusive_fence=observed_fence)
    before = await runtime.maintenance_payload(operator_ref="user:owner")
    assert before["clear_recovery"]["operation_id"] == operation.operation_id

    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert maintenance._backup_stage_reconcile_task is None
    await asyncio.sleep(0)
    assert fence_entered is False
    assert await runtime.maintenance_payload(operator_ref="user:owner") == before
    await memory_runtime_factory.close(runtime)


def _enqueue(runtime: MemoryRuntime, source: str) -> None:
    result = runtime._store.enqueue_request(
        source_message_id=source,
        session_id="session",
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="private payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
    )
    assert result.outcome == "accepted"


async def test_clear_converges_for_provider_tree_deeper_than_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    manager = _maintenance(runtime)._snapshot_manager
    assert manager is not None
    provider_root = tmp_path / "memory/everos-root"
    runtime._provider_root_owner.ensure(
        runtime._store.ensure_meta(),
        runtime._active_provider_root_metadata(),
    )
    path_max = os.pathconf(tmp_path, "PC_PATH_MAX")
    snapshot_prefix = manager.snapshot_root / (
        f".{('x' * 32)}.tmp/payload/memory/everos-root"
    )
    longest_prefix = max(
        len(os.fsencode(provider_root)),
        len(os.fsencode(snapshot_prefix)),
    )
    depth = min(500, (path_max - longest_prefix - len("/leaf") - 16) // 2)
    recursion_limit = min(sys.getrecursionlimit(), depth - 32)
    assert recursion_limit > 200
    assert depth > recursion_limit

    leaf_parent = provider_root
    for _ in range(depth):
        leaf_parent /= "d"
        leaf_parent.mkdir(mode=0o700)
    leaf = leaf_parent / "leaf"
    leaf.write_bytes(b"provider bytes")
    leaf.chmod(0o600)
    assert len(os.fsencode(leaf)) < path_max

    previous_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(recursion_limit)
        result = await _clear(runtime, operator_ref="user:owner")
        gc_task = _maintenance(runtime)._terminal_snapshot_gc_task
        if gc_task is not None:
            await gc_task
    finally:
        sys.setrecursionlimit(previous_recursion_limit)

    assert result["status"] == "completed"
    assert _maintenance(runtime)._clear_journal is not None
    assert _maintenance(runtime)._clear_journal.get_open_operation() is None
    assert not leaf.exists()
    assert not manager.snapshot_path(result["operation_id"]).exists()
    await memory_runtime_factory.close(runtime)


async def test_clear_discards_manual_required_fence_and_allows_new_delivery(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """MEMORY-CLEAR-201: Clear settles an unknown add without replaying it."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(enabled=True), effective_home=tmp_path)
    provider_timeout = asyncio.Event()

    async def time_out_add(_capture) -> None:
        await provider_timeout.wait()

    provider = FakeMemoryProvider(add_hook=time_out_add)
    runtime.module.replace_provider(provider)
    runtime.module._worker.coordinator._add_timeout_seconds = 0.001
    source_root = tmp_path / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700)
    source_root.chmod(0o700)
    attachment_path = source_root / "evidence.txt"
    attachment_path.write_bytes(b"retained ambiguous attachment")
    attachment_path.chmod(0o600)
    first = CaptureRequest(
        source_message_id="timed-out-add",
        session_id="session",
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        provenance="user_input",
        text="ambiguous payload",
        occurred_at_ms=1,
        attachments=(
            CaptureAttachment(
                kind="doc",
                name=attachment_path.name,
                uri=attachment_path.as_uri(),
                ext="txt",
            ),
        ),
    )
    assert await runtime.module.capture(first) == CaptureAccepted()
    assert await runtime.module.drain() == 1

    ambiguous = runtime._store.list_queue_rows()
    assert len(ambiguous) == 1
    assert ambiguous[0].state == "manual_required"
    assert ambiguous[0].payload_text == "ambiguous payload"
    assert ambiguous[0].attachment_bundle_id is not None
    attachment_bundle_id = ambiguous[0].attachment_bundle_id
    assert runtime._store.has_manual_required_fence() is True
    session = runtime._store.get_session_flush_state(ambiguous[0].provider_session_ref)
    assert session is not None and session.state == "manual_required"
    assert (await runtime.maintenance_payload())["can_clear"] is True

    async def resume_without_sidecar() -> None:
        runtime.module.resume_claims()

    _replace_runtime_port(runtime, resume=resume_without_sidecar)
    result = await _clear(runtime, operator_ref="user:owner")

    assert result["status"] == "completed"
    assert runtime._store.list_queue_rows() == ()
    assert runtime._store.has_manual_required_fence() is False
    assert not (
        tmp_path / "memory" / "attachments" / "bundles" / attachment_bundle_id
    ).exists()
    assert attachment_path.read_bytes() == b"retained ambiguous attachment"

    provider.add_hook = None
    second = CaptureRequest(
        source_message_id="after-clear",
        session_id="session",
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        provenance="user_input",
        text="deliver after clear",
        occurred_at_ms=2,
    )
    assert await runtime.module.capture(second) == CaptureAccepted()
    assert await runtime.module.drain() == 1
    delivered = runtime._store.list_queue_rows()
    assert len(delivered) == 1 and delivered[0].state == "delivered"
    assert delivered[0].provider_session_ref.epoch == result["epoch"]
    assert [capture.text for capture in provider.captures] == [
        "ambiguous payload",
        "deliver after clear",
    ]
    await memory_runtime_factory.close(runtime)


async def test_interrupted_queue_delete_requires_explicit_resume_at_target_epoch(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    owner = runtime.principal_for_user_key("avibe:local")
    other_operator = runtime.principal_for_user_key("avibe:remote:other-subject")
    _enqueue(runtime, "resume-source")
    pre_epoch = runtime._store.ensure_meta().epoch
    journal = _maintenance(runtime)._clear_journal
    assert journal is not None
    original_record = journal.record_surface_deleted
    interrupted = False

    def interrupt_queue_receipt(operation_id, surface, **kwargs):
        nonlocal interrupted
        if surface == "queue" and not interrupted:
            interrupted = True
            raise OSError("injected receipt failure")
        return original_record(operation_id, surface, **kwargs)

    monkeypatch.setattr(journal, "record_surface_deleted", interrupt_queue_receipt)

    failed = await _clear(runtime, operator_ref=owner)

    assert failed["status"] == "failed"
    recovery = journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "deleting"
    assert failed["recovery"]["can_abort"] is True
    assert runtime._store.ensure_meta().epoch == pre_epoch + 1
    assert runtime._store.list_queue_rows() == ()

    owner_maintenance = await runtime.maintenance_payload(operator_ref=owner)
    other_maintenance = await runtime.maintenance_payload(
        operator_ref=other_operator
    )
    owner_failures = await runtime.failure_log_payload(operator_ref=owner)
    other_failures = await runtime.failure_log_payload(operator_ref=other_operator)
    assert owner_maintenance["clear_recovery"]["can_resume"] is True
    assert owner_maintenance["clear_recovery"]["can_abort"] is True
    assert owner_failures["recovery"]["can_resume"] is True
    assert owner_failures["recovery"]["can_abort"] is True
    assert other_maintenance["clear_recovery"]["can_resume"] is False
    assert other_maintenance["clear_recovery"]["can_abort"] is False
    assert other_failures["recovery"]["can_resume"] is False
    assert other_failures["recovery"]["can_abort"] is False
    assert "operator_ref" not in owner_maintenance["clear_recovery"]
    assert owner not in repr(owner_maintenance)

    rejected = await _resume_clear(runtime,
        recovery.operation_id,
        operator_ref=other_operator,
    )
    assert rejected["status"] == "failed"
    assert rejected["recovery"]["can_resume"] is False
    assert journal.get_open_operation() == recovery

    monkeypatch.setattr(journal, "record_surface_deleted", original_record)
    completed = await _resume_clear(runtime,
        recovery.operation_id,
        operator_ref=owner,
    )

    assert completed["status"] == "completed"
    assert completed["epoch"] == pre_epoch + 1
    assert runtime._store.ensure_meta().epoch == pre_epoch + 1
    assert journal.get_open_operation() is None
    await memory_runtime_factory.close(runtime)


async def test_attachment_clear_failure_does_not_record_surface_deleted(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    attachment_store = runtime.module._attachment_store
    outside = tmp_path / "outside-attachments"
    outside.mkdir(mode=0o700)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside must remain")
    unsafe = attachment_store._bundles / "malformed-bundle"
    original_clear_all = attachment_store.clear_all

    def inject_unsafe_entry_then_clear() -> None:
        unsafe.symlink_to(outside, target_is_directory=True)
        original_clear_all()

    monkeypatch.setattr(attachment_store, "clear_all", inject_unsafe_entry_then_clear)

    failed = await _clear(runtime, operator_ref="user:owner")

    assert failed["status"] == "failed"
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()
    assert recovery is not None and recovery.state == "recovery_needed"
    surfaces = {
        surface.surface: surface.state
        for surface in _maintenance(runtime)._clear_journal.get_surfaces(recovery.operation_id)
    }
    assert surfaces["attachments"] == "snapshotted"
    assert unsafe.is_symlink()
    assert sentinel.read_bytes() == b"outside must remain"
    await memory_runtime_factory.close(runtime)


async def test_recovery_payload_disables_abort_before_initial_snapshot(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    journal = _maintenance(runtime)._clear_journal
    assert journal is not None
    operation = journal.start(
        operation_id="incomplete-snapshot",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    recovery = journal.mark_boot_recovery_needed()

    assert recovery is not None
    maintenance = await runtime.maintenance_payload(operator_ref="user:owner")
    assert maintenance["clear_recovery"] == {
        "state": "recovery_needed",
        "operation_id": operation.operation_id,
        "occurred_at": recovery.updated_at,
        "error_code": "memory_clear_failed",
        "can_resume": True,
        "can_abort": False,
    }
    await memory_runtime_factory.close(runtime)


async def test_cancelled_clear_start_and_resume_claim_settle_before_recovery(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    journal = _maintenance(runtime)._clear_journal
    assert journal is not None
    start_entered = threading.Event()
    start_release = threading.Event()
    original_start = journal.start

    def blocking_start(**kwargs):
        operation = original_start(**kwargs)
        start_entered.set()
        assert start_release.wait(2)
        return operation

    monkeypatch.setattr(journal, "start", blocking_start)
    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    assert await asyncio.to_thread(start_entered.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    start_release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    recovery = journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "preparing"
    assert recovery.execution_token is None
    assert runtime._store.ensure_meta().clear_in_progress is False

    claim_entered = threading.Event()
    claim_release = threading.Event()
    original_claim_resume = journal.claim_resume

    def blocking_claim_resume(operation_id: str, **kwargs):
        operation = original_claim_resume(operation_id, **kwargs)
        claim_entered.set()
        assert claim_release.wait(2)
        return operation

    monkeypatch.setattr(journal, "claim_resume", blocking_claim_resume)
    resuming = asyncio.create_task(
        _resume_clear(runtime, recovery.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(claim_entered.wait, 1)

    resuming.cancel()
    await asyncio.sleep(0)

    assert resuming.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    claim_release.set()
    with pytest.raises(asyncio.CancelledError):
        await resuming

    claimed_recovery = journal.get_open_operation()
    assert claimed_recovery is not None
    assert claimed_recovery.state == "recovery_needed"
    assert claimed_recovery.recovery_from_state == "preparing"
    assert claimed_recovery.resolution == "resume"
    assert claimed_recovery.execution_token is None

    monkeypatch.setattr(journal, "claim_resume", original_claim_resume)
    completed = await _resume_clear(runtime,
        claimed_recovery.operation_id,
        operator_ref="user:owner",
    )
    assert completed["status"] == "completed"
    assert journal.get_open_operation() is None
    await memory_runtime_factory.close(runtime)


async def test_cancelled_completed_clear_resumes_runtime_once(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    journal = _maintenance(runtime)._clear_journal
    assert journal is not None
    completed_entered = threading.Event()
    completed_release = threading.Event()
    completed_ids: list[str] = []
    original_mark_completed = journal.mark_completed

    def blocking_mark_completed(operation_id: str, **kwargs):
        operation = original_mark_completed(operation_id, **kwargs)
        completed_ids.append(operation.operation_id)
        completed_entered.set()
        assert completed_release.wait(2)
        return operation

    resume_calls = 0
    original_resume = _maintenance(runtime)._runtime.resume

    async def counted_resume() -> None:
        nonlocal resume_calls
        resume_calls += 1
        await original_resume()

    monkeypatch.setattr(journal, "mark_completed", blocking_mark_completed)
    _replace_runtime_port(runtime, resume=counted_resume)
    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    assert await asyncio.to_thread(completed_entered.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    completed_release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert completed_ids
    completed = journal.get_operation(completed_ids[0])
    assert completed is not None
    assert completed.state == "completed"
    assert journal.get_open_operation() is None
    assert resume_calls == 1
    await memory_runtime_factory.close(runtime)


async def test_cancelled_post_terminal_resume_holds_lifecycle_fences(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(enabled=True), effective_home=tmp_path)
    resume_entered = asyncio.Event()
    resume_release = asyncio.Event()

    async def blocking_reconcile(*_args, **_kwargs):
        resume_entered.set()
        await resume_release.wait()
        runtime.module._worker.resume_claims()
        return {"ok": True, "state": "running"}

    monkeypatch.setattr(runtime, "_reconcile_locked", blocking_reconcile)
    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    await asyncio.wait_for(resume_entered.wait(), timeout=5)

    assert _maintenance(runtime)._clear_journal.get_open_operation() is None
    assert runtime.module._worker._claims_paused is True
    assert runtime.module._worker._coordinator._paused is True
    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    resume_release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert runtime.module._worker._claims_paused is False
    assert runtime.module._worker._coordinator._paused is False
    assert runtime._reconcile_lock.locked() is False
    assert runtime.module._lifecycle_lock.locked() is False
    assert runtime.module._root_lifecycle_lock().locked() is False
    await memory_runtime_factory.close(runtime)


async def test_abort_restores_all_surfaces_after_destructive_work(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "abort-source")
    pre_meta = runtime._store.ensure_meta()
    original_delete = _maintenance(runtime)._runtime.delete_surface

    async def interrupt_provider(surface, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch)

    _replace_runtime_port(runtime, delete_surface=interrupt_provider)
    failed = await _clear(runtime, operator_ref="user:owner")
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()

    assert failed["status"] == "failed"
    assert recovery is not None and recovery.destructive_started
    assert runtime._store.list_queue_rows() == ()

    _replace_runtime_port(runtime, delete_surface=original_delete)
    aborted = await _abort_clear(runtime,
        recovery.operation_id,
        operator_ref="user:owner",
    )

    assert aborted["status"] == "aborted"
    restored_meta = runtime._store.ensure_meta()
    assert restored_meta.epoch == pre_meta.epoch
    assert restored_meta.clear_in_progress is False
    assert len(runtime._store.list_queue_rows()) == 1
    assert _maintenance(runtime)._clear_journal.get_open_operation() is None
    operation = _maintenance(runtime)._clear_journal.get_operation(recovery.operation_id)
    assert operation is not None and operation.state == "aborted"
    assert _maintenance(runtime)._snapshot_manager is not None
    assert not _maintenance(runtime)._snapshot_manager.snapshot_path(recovery.operation_id).exists()
    await memory_runtime_factory.close(runtime)


async def test_clear_accepts_corrupt_diagnostic_call_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    call_log = tmp_path / "memory/call-log/call-log.db"
    call_log.parent.mkdir(parents=True, mode=0o700)
    call_log.parent.chmod(0o700)
    files = {
        call_log: b"corrupt-call-log-database",
        call_log.with_name(f"{call_log.name}-wal"): b"corrupt-call-log-wal",
        call_log.with_name(f"{call_log.name}-shm"): b"corrupt-call-log-shm",
        call_log.with_name(f"{call_log.name}-journal"): b"corrupt-call-log-journal",
    }
    for path, payload in files.items():
        path.write_bytes(payload)
        path.chmod(0o600)

    result = await _clear(runtime, operator_ref="user:owner")

    assert result["status"] == "completed"
    assert all(not path.exists() for path in files)
    assert _maintenance(runtime)._clear_journal.get_open_operation() is None
    await memory_runtime_factory.close(runtime)


async def test_abort_restores_exact_corrupt_call_log_files_after_deletion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    call_log = tmp_path / "memory/call-log/call-log.db"
    call_log.parent.mkdir(parents=True, mode=0o700)
    call_log.parent.chmod(0o700)
    files = {
        call_log: b"corrupt-call-log-database",
        call_log.with_name(f"{call_log.name}-wal"): b"corrupt-call-log-wal",
        call_log.with_name(f"{call_log.name}-shm"): b"corrupt-call-log-shm",
        call_log.with_name(f"{call_log.name}-journal"): b"corrupt-call-log-journal",
    }
    for path, payload in files.items():
        path.write_bytes(payload)
        path.chmod(0o600)
    before = {
        path: (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in files
    }
    original_delete = _maintenance(runtime)._runtime.delete_surface

    async def fail_after_call_log(surface, target_epoch):
        if surface.surface == "attachments":
            raise OSError("injected failure after call-log deletion")
        await original_delete(surface, target_epoch)

    _replace_runtime_port(runtime, delete_surface=fail_after_call_log)
    failed = await _clear(runtime, operator_ref="user:owner")
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()

    assert failed["status"] == "failed"
    assert recovery is not None and recovery.destructive_started
    assert all(not path.exists() for path in files)

    _replace_runtime_port(runtime, delete_surface=original_delete)
    aborted = await _abort_clear(runtime,
        recovery.operation_id,
        operator_ref="user:owner",
    )

    assert aborted["status"] == "aborted"
    for path, (expected_bytes, expected_mode, expected_digest) in before.items():
        restored_bytes = path.read_bytes()
        assert restored_bytes == expected_bytes
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
        assert hashlib.sha256(restored_bytes).hexdigest() == expected_digest
    assert _maintenance(runtime)._clear_journal.get_open_operation() is None
    await memory_runtime_factory.close(runtime)


async def test_completed_clear_snapshot_removal_retries_on_reconcile_and_restart(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    storage = _maintenance(runtime)._clear_snapshot_storage
    manager = _maintenance(runtime)._snapshot_manager
    assert storage is not None
    assert manager is not None
    original_remove = storage.remove_terminal_snapshot
    removal_attempts: list[str] = []

    def fail_removal(operation_id: str) -> None:
        if manager.snapshot_path(operation_id).exists():
            removal_attempts.append(operation_id)
            raise OSError("injected snapshot removal failure")
        original_remove(operation_id)

    monkeypatch.setattr(storage, "remove_terminal_snapshot", fail_removal)
    completed = await _clear(runtime, operator_ref="user:owner")
    snapshot_path = manager.snapshot_path(completed["operation_id"])

    assert completed["status"] == "completed"
    assert removal_attempts == [completed["operation_id"]]
    assert snapshot_path.is_dir()

    monkeypatch.setattr(storage, "remove_terminal_snapshot", original_remove)
    assert await runtime.reconcile(MemoryConfig()) == {"ok": True, "state": "disabled"}
    reconcile_gc = _maintenance(runtime)._terminal_snapshot_gc_task
    assert reconcile_gc is not None
    await reconcile_gc
    assert not snapshot_path.exists()

    monkeypatch.setattr(storage, "remove_terminal_snapshot", fail_removal)
    completed_before_restart = await _clear(runtime, operator_ref="user:owner")
    restart_snapshot_path = manager.snapshot_path(completed_before_restart["operation_id"])
    assert restart_snapshot_path.is_dir()
    await memory_runtime_factory.close(runtime)

    restarted = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)

    assert restart_snapshot_path.is_dir()
    assert _maintenance(restarted)._clear_journal is not None
    assert _maintenance(restarted)._clear_journal.get_open_operation() is None
    assert await restarted.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    restart_gc = _maintenance(restarted)._terminal_snapshot_gc_task
    assert restart_gc is not None
    await restart_gc
    assert not restart_snapshot_path.exists()
    await memory_runtime_factory.close(restarted)


async def _retain_terminal_clear_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    memory_runtime_factory,
) -> Path:
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=home)
    storage = _maintenance(runtime)._clear_snapshot_storage
    manager = _maintenance(runtime)._snapshot_manager
    assert storage is not None
    assert manager is not None

    def retain(_operation_id: str) -> None:
        raise OSError("retain terminal snapshot for startup GC")

    monkeypatch.setattr(storage, "remove_terminal_snapshot", retain)
    completed = await _clear(runtime, operator_ref="user:owner")
    snapshot_path = manager.snapshot_path(completed["operation_id"])
    assert completed["status"] == "completed"
    assert snapshot_path.is_dir()
    await memory_runtime_factory.close(runtime)
    return snapshot_path


async def test_terminal_snapshot_gc_is_scheduled_after_lifecycle_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original_remove = MemoryClearSnapshotStorage.remove_terminal_snapshot
    snapshot_path = await _retain_terminal_clear_snapshot(
        monkeypatch,
        tmp_path,
        memory_runtime_factory,
    )

    def blocking_remove(
        storage: MemoryClearSnapshotStorage,
        operation_id: str,
    ) -> None:
        entered.set()
        assert release.wait(timeout=2)
        original_remove(storage, operation_id)

    monkeypatch.setattr(
        MemoryClearSnapshotStorage,
        "remove_terminal_snapshot",
        blocking_remove,
    )
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)

    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert entered.is_set() is False
    gc_task = _maintenance(runtime)._terminal_snapshot_gc_task
    assert gc_task is not None
    assert await asyncio.to_thread(entered.wait, 1)

    release.set()
    await asyncio.wait_for(asyncio.shield(gc_task), timeout=1)
    assert not snapshot_path.exists()
    await memory_runtime_factory.close(runtime)


async def test_terminal_snapshot_gc_shutdown_joins_cancelled_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_remove = MemoryClearSnapshotStorage.remove_terminal_snapshot
    snapshot_path = await _retain_terminal_clear_snapshot(
        monkeypatch,
        tmp_path,
        memory_runtime_factory,
    )

    def blocking_remove(
        storage: MemoryClearSnapshotStorage,
        operation_id: str,
    ) -> None:
        entered.set()
        assert release.wait(timeout=2)
        try:
            original_remove(storage, operation_id)
        finally:
            finished.set()

    monkeypatch.setattr(
        MemoryClearSnapshotStorage,
        "remove_terminal_snapshot",
        blocking_remove,
    )
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    await runtime.reconcile(MemoryConfig())
    gc_task = _maintenance(runtime)._terminal_snapshot_gc_task
    assert gc_task is not None
    assert await asyncio.to_thread(entered.wait, 1)

    closing = asyncio.create_task(_maintenance(runtime).close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)
    assert finished.is_set()
    assert gc_task.done()
    assert _maintenance(runtime)._terminal_snapshot_gc_task is None
    assert not snapshot_path.exists()


async def test_aborted_clear_snapshot_removal_retries_on_reconcile_and_restart(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    journal = _maintenance(runtime)._clear_journal
    storage = _maintenance(runtime)._clear_snapshot_storage
    manager = _maintenance(runtime)._snapshot_manager
    assert journal is not None
    assert storage is not None
    assert manager is not None
    original_delete = _maintenance(runtime)._runtime.delete_surface
    original_remove = storage.remove_terminal_snapshot
    removal_attempts: list[str] = []

    async def interrupt_provider(surface, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch)

    def fail_removal(operation_id: str) -> None:
        if manager.snapshot_path(operation_id).exists():
            removal_attempts.append(operation_id)
            raise OSError("injected snapshot removal failure")
        original_remove(operation_id)

    async def abort_after_interrupted_clear() -> dict:
        _replace_runtime_port(runtime, delete_surface=interrupt_provider)
        failed = await _clear(runtime, operator_ref="user:owner")
        recovery = journal.get_open_operation()
        assert failed["status"] == "failed"
        assert recovery is not None
        _replace_runtime_port(runtime, delete_surface=original_delete)
        return await _abort_clear(runtime,
            recovery.operation_id,
            operator_ref="user:owner",
        )

    monkeypatch.setattr(storage, "remove_terminal_snapshot", fail_removal)
    aborted = await abort_after_interrupted_clear()
    snapshot_path = manager.snapshot_path(aborted["operation_id"])

    assert aborted["status"] == "aborted"
    assert removal_attempts == [aborted["operation_id"]]
    assert snapshot_path.is_dir()
    terminal = journal.get_operation(aborted["operation_id"])
    assert terminal is not None and terminal.state == "aborted"

    monkeypatch.setattr(storage, "remove_terminal_snapshot", original_remove)
    assert await runtime.reconcile(MemoryConfig()) == {"ok": True, "state": "disabled"}
    reconcile_gc = _maintenance(runtime)._terminal_snapshot_gc_task
    assert reconcile_gc is not None
    await reconcile_gc
    assert not snapshot_path.exists()

    monkeypatch.setattr(storage, "remove_terminal_snapshot", fail_removal)
    aborted_before_restart = await abort_after_interrupted_clear()
    restart_snapshot_path = manager.snapshot_path(aborted_before_restart["operation_id"])
    assert restart_snapshot_path.is_dir()
    await memory_runtime_factory.close(runtime)

    restarted = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)

    assert restart_snapshot_path.is_dir()
    assert _maintenance(restarted)._clear_journal is not None
    assert _maintenance(restarted)._clear_journal.get_open_operation() is None
    assert await restarted.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    restart_gc = _maintenance(restarted)._terminal_snapshot_gc_task
    assert restart_gc is not None
    await restart_gc
    assert not restart_snapshot_path.exists()
    await memory_runtime_factory.close(restarted)


async def test_cancelled_clear_waits_for_snapshot_creation_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_create = MemorySnapshotManager.create

    def blocking_create(manager: MemorySnapshotManager, snapshot_id: str | None = None):
        if manager is _maintenance(runtime)._snapshot_manager:
            started.set()
            assert release.wait(2)
        try:
            return original_create(manager, snapshot_id)
        finally:
            if manager is _maintenance(runtime)._snapshot_manager:
                finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "create", blocking_create)
    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    assert await asyncio.to_thread(started.wait, 1)
    operation = _maintenance(runtime)._clear_journal.get_open_operation()
    assert operation is not None

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    resuming = asyncio.create_task(
        _resume_clear(runtime, operation.operation_id, operator_ref="user:owner")
    )
    await asyncio.sleep(0)
    assert resuming.done() is False
    resuming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resuming

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert finished.is_set()
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "preparing"

    discard_started = threading.Event()
    discard_release = threading.Event()
    discard_finished = threading.Event()
    original_discard = (
        MemoryClearSnapshotStorage.discard_unrecorded_preparing_snapshot
    )

    def blocking_discard(
        storage: MemoryClearSnapshotStorage,
        operation_id: str,
        **kwargs,
    ):
        if storage is _maintenance(runtime)._clear_snapshot_storage:
            discard_started.set()
            assert discard_release.wait(2)
        try:
            return original_discard(storage, operation_id, **kwargs)
        finally:
            if storage is _maintenance(runtime)._clear_snapshot_storage:
                discard_finished.set()

    monkeypatch.setattr(
        MemoryClearSnapshotStorage,
        "discard_unrecorded_preparing_snapshot",
        blocking_discard,
    )
    resuming = asyncio.create_task(
        _resume_clear(runtime, recovery.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(discard_started.wait, 1)
    resuming.cancel()
    await asyncio.sleep(0)
    assert resuming.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()

    discard_release.set()
    with pytest.raises(asyncio.CancelledError):
        await resuming

    assert discard_finished.is_set()
    pending = _maintenance(runtime)._clear_journal.get_open_operation()
    assert pending is not None
    assert pending.state == "recovery_needed"
    assert pending.resolution == "resume"

    monkeypatch.setattr(
        MemoryClearSnapshotStorage,
        "discard_unrecorded_preparing_snapshot",
        original_discard,
    )
    completed = await _resume_clear(runtime, pending.operation_id, operator_ref="user:owner")
    assert completed["status"] == "completed"
    await memory_runtime_factory.close(runtime)


async def test_cancelled_clear_waits_for_snapshot_verification_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_verify = MemorySnapshotManager.verify

    def blocking_verify(manager: MemorySnapshotManager, snapshot_id: str, **kwargs):
        if manager is _maintenance(runtime)._snapshot_manager:
            started.set()
            assert release.wait(2)
        try:
            return original_verify(manager, snapshot_id, **kwargs)
        finally:
            if manager is _maintenance(runtime)._snapshot_manager:
                finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "verify", blocking_verify)
    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    assert await asyncio.to_thread(started.wait, 1)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert finished.is_set()
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "prepared"

    monkeypatch.setattr(MemorySnapshotManager, "verify", original_verify)
    completed = await _resume_clear(runtime, recovery.operation_id, operator_ref="user:owner")
    assert completed["status"] == "completed"
    await memory_runtime_factory.close(runtime)


async def test_cancelled_clear_waits_for_provider_delete_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    runtime._provider_root_owner.ensure(
        runtime._store.ensure_meta(),
        runtime._active_provider_root_metadata(),
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_recreate = runtime._provider_root_owner.recreate_empty

    def blocking_recreate(meta, metadata) -> None:
        started.set()
        assert release.wait(2)
        try:
            original_recreate(meta, metadata)
        finally:
            finished.set()

    monkeypatch.setattr(
        runtime._provider_root_owner,
        "recreate_empty",
        blocking_recreate,
    )
    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    assert await asyncio.to_thread(started.wait, 5)

    clearing.cancel()
    await asyncio.sleep(0)

    assert clearing.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert finished.is_set()
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "deleting"

    monkeypatch.setattr(
        runtime._provider_root_owner,
        "recreate_empty",
        original_recreate,
    )
    completed = await _resume_clear(runtime,
        recovery.operation_id,
        operator_ref="user:owner",
    )
    assert completed["status"] == "completed"
    await memory_runtime_factory.close(runtime)


async def test_cancelled_abort_waits_for_restore_before_releasing_fences(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "cancel-abort-source")
    original_delete = _maintenance(runtime)._runtime.delete_surface

    async def interrupt_provider(surface, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch)

    _replace_runtime_port(runtime, delete_surface=interrupt_provider)
    failed = await _clear(runtime, operator_ref="user:owner")
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()
    assert failed["status"] == "failed"
    assert recovery is not None
    _replace_runtime_port(runtime, delete_surface=original_delete)

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_restore = MemorySnapshotManager.restore

    def blocking_restore(manager: MemorySnapshotManager, snapshot_id: str, **kwargs):
        if manager is _maintenance(runtime)._snapshot_manager:
            started.set()
            assert release.wait(2)
        try:
            return original_restore(manager, snapshot_id, **kwargs)
        finally:
            if manager is _maintenance(runtime)._snapshot_manager:
                finished.set()

    monkeypatch.setattr(MemorySnapshotManager, "restore", blocking_restore)
    aborting = asyncio.create_task(
        _abort_clear(runtime, recovery.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(started.wait, 1)

    aborting.cancel()
    await asyncio.sleep(0)

    assert aborting.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await aborting

    assert finished.is_set()
    pending = _maintenance(runtime)._clear_journal.get_open_operation()
    assert pending is not None
    assert pending.state == "recovery_needed"
    assert pending.resolution == "abort"
    assert pending.execution_token is None
    assert _maintenance(runtime).recovery().can_resume is False

    monkeypatch.setattr(MemorySnapshotManager, "restore", original_restore)
    journal = _maintenance(runtime)._clear_journal
    terminal_entered = threading.Event()
    terminal_release = threading.Event()
    original_mark_aborted = journal.mark_aborted

    def blocking_mark_aborted(operation_id: str, **kwargs):
        operation = original_mark_aborted(operation_id, **kwargs)
        terminal_entered.set()
        assert terminal_release.wait(2)
        return operation

    resume_calls = 0
    original_resume = _maintenance(runtime)._runtime.resume

    async def counted_resume() -> None:
        nonlocal resume_calls
        resume_calls += 1
        await original_resume()

    monkeypatch.setattr(journal, "mark_aborted", blocking_mark_aborted)
    _replace_runtime_port(runtime, resume=counted_resume)
    aborting = asyncio.create_task(
        _abort_clear(runtime, pending.operation_id, operator_ref="user:owner")
    )
    assert await asyncio.to_thread(terminal_entered.wait, 1)

    aborting.cancel()
    await asyncio.sleep(0)

    assert aborting.done() is False
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()
    terminal_release.set()
    with pytest.raises(asyncio.CancelledError):
        await aborting

    aborted = journal.get_operation(pending.operation_id)
    assert aborted is not None
    assert aborted.state == "aborted"
    assert _maintenance(runtime)._clear_journal.get_open_operation() is None
    assert resume_calls == 1
    await memory_runtime_factory.close(runtime)


async def test_maintenance_backup_fences_capture_and_clear_for_the_full_copy(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(enabled=True), effective_home=tmp_path)
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    original_create = MemorySnapshotManager.create
    original_enqueue = runtime._store.enqueue_request

    def blocking_enqueue(**kwargs):
        enqueue_entered.set()
        assert release_enqueue.wait(2)
        return original_enqueue(**kwargs)

    def blocking_create(manager: MemorySnapshotManager, snapshot_id: str | None = None):
        if manager.snapshot_root == tmp_path / "state" / "memory" / "backups":
            entered.set()
            assert release.wait(2)
        return original_create(manager, snapshot_id)

    async def resume_without_sidecar() -> None:
        runtime.module._worker.resume_claims()

    monkeypatch.setattr(MemorySnapshotManager, "create", blocking_create)
    monkeypatch.setattr(runtime._store, "enqueue_request", blocking_enqueue)
    _replace_runtime_port(runtime, resume=resume_without_sidecar)

    capturing = asyncio.create_task(
        runtime.module.capture(
            CaptureRequest(
                source_message_id="before-backup",
                session_id="session",
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provenance="user_input",
                text="must be included",
                occurred_at_ms=1,
            )
        )
    )
    assert await asyncio.to_thread(enqueue_entered.wait, 1)
    creating = asyncio.create_task(_maintenance(runtime).create_backup("runtime-fence"))
    await asyncio.sleep(0)
    assert entered.is_set() is False
    release_enqueue.set()
    assert await capturing == CaptureAccepted()
    assert await asyncio.to_thread(entered.wait, 1)
    assert _maintenance(runtime).is_open() is True
    receipt = await runtime.module.capture(
        CaptureRequest(
            source_message_id="blocked-capture",
            session_id="session",
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provenance="user_input",
            text="must wait for backup",
            occurred_at_ms=2,
        )
    )
    assert receipt == CaptureSkipped(reason="memory_clear_failed")

    clearing = asyncio.create_task(_clear(runtime, operator_ref="user:owner"))
    await asyncio.sleep(0)
    assert clearing.done() is False
    assert _maintenance(runtime)._clear_journal.get_open_operation() is None

    clearing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await clearing
    creating.cancel()
    await asyncio.sleep(0)
    assert creating.done() is False
    assert _maintenance(runtime).is_open() is True
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert (tmp_path / "state" / "memory" / "backups" / "runtime-fence").is_dir()
    assert _maintenance(runtime).is_open() is False
    await memory_runtime_factory.close(runtime)


async def test_reconcile_schedules_auto_id_backup_stage_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    manager = _maintenance(runtime)._backup_manager
    assert manager is not None
    stage = manager.snapshot_root / f".{('a' * 32)}.tmp"
    stage.mkdir(parents=True, mode=0o700)
    manager.snapshot_root.chmod(0o700)
    stage.chmod(0o700)
    (stage / "partial.bin").write_bytes(b"partial")
    (stage / "partial.bin").chmod(0o600)

    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    task = _maintenance(runtime)._backup_stage_reconcile_task
    if task is not None:
        await task

    assert not stage.exists()
    await memory_runtime_factory.close(runtime)


async def test_backup_stage_cleanup_waits_for_terminal_snapshot_gc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    storage = _maintenance(runtime)._clear_snapshot_storage
    manager = _maintenance(runtime)._backup_manager
    assert storage is not None
    assert manager is not None
    terminal_entered = threading.Event()
    terminal_release = threading.Event()
    backup_entered = threading.Event()
    original_ids = storage.eligible_terminal_snapshot_ids

    def blocking_ids():
        terminal_entered.set()
        assert terminal_release.wait(2)
        return original_ids()

    def observe_backup_cleanup() -> tuple[str, ...]:
        backup_entered.set()
        return ()

    monkeypatch.setattr(storage, "eligible_terminal_snapshot_ids", blocking_ids)
    monkeypatch.setattr(
        manager,
        "reconcile_unpublished_backup_stages",
        observe_backup_cleanup,
    )

    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert await asyncio.to_thread(terminal_entered.wait, 1)
    await asyncio.sleep(0)
    assert backup_entered.is_set() is False

    terminal_release.set()
    terminal_gc = _maintenance(runtime)._terminal_snapshot_gc_task
    backup_reconcile = _maintenance(runtime)._backup_stage_reconcile_task
    assert terminal_gc is not None
    assert backup_reconcile is not None
    await terminal_gc
    await backup_reconcile

    assert backup_entered.is_set() is True
    await memory_runtime_factory.close(runtime)


async def test_backup_stage_cleanup_cancellation_joins_filesystem_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    manager = _maintenance(runtime)._backup_manager
    assert manager is not None
    entered = threading.Event()
    release = threading.Event()

    def blocking_reconcile() -> tuple[str, ...]:
        entered.set()
        assert release.wait(2)
        return ()

    monkeypatch.setattr(manager, "reconcile_unpublished_backup_stages", blocking_reconcile)
    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert await asyncio.to_thread(entered.wait, 1)
    assert _maintenance(runtime).is_open() is False

    maintenance_close_entered = asyncio.Event()
    maintenance = _maintenance(runtime)
    original_maintenance_close = maintenance.close

    async def observed_maintenance_close() -> None:
        maintenance_close_entered.set()
        await original_maintenance_close()

    monkeypatch.setattr(maintenance, "close", observed_maintenance_close)
    closing = asyncio.create_task(memory_runtime_factory.close(runtime))
    close_results: list[object] = []
    try:
        await asyncio.wait_for(maintenance_close_entered.wait(), timeout=1.0)
        assert closing.done() is False
    finally:
        release.set()
        close_results.extend(
            await asyncio.gather(closing, return_exceptions=True)
        )
    assert close_results == [None]
    assert _maintenance(runtime)._backup_stage_reconcile_task is None


async def test_backup_stage_cleanup_queues_capture_without_global_maintenance_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(enabled=True), effective_home=tmp_path)
    manager = _maintenance(runtime)._backup_manager
    assert manager is not None
    entered = threading.Event()
    release = threading.Event()

    def blocking_reconcile() -> tuple[str, ...]:
        entered.set()
        assert release.wait(2)
        return ()

    monkeypatch.setattr(manager, "reconcile_unpublished_backup_stages", blocking_reconcile)
    cleanup = asyncio.create_task(_maintenance(runtime)._run_backup_stage_reconcile())
    assert await asyncio.to_thread(entered.wait, 1)
    assert runtime._reconcile_lock.locked()
    assert runtime.module._lifecycle_lock.locked()
    assert runtime.module._root_lifecycle_lock().locked()

    capturing = asyncio.create_task(
        runtime.module.capture(
            CaptureRequest(
                source_message_id="during-backup-stage-cleanup",
                session_id="session",
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provenance="user_input",
                text="must be captured after cleanup",
                occurred_at_ms=1,
            )
        )
    )
    await asyncio.sleep(0)
    completed_during_cleanup = capturing.done()
    release.set()

    await cleanup
    assert completed_during_cleanup is False
    assert await capturing == CaptureAccepted()
    assert [row.payload_text for row in runtime._store.list_queue_rows()] == [
        "must be captured after cleanup"
    ]
    await memory_runtime_factory.close(runtime)


async def test_queued_backup_stage_reconcile_rechecks_artifact_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    install_entered = threading.Event()
    release_install = threading.Event()

    class BlockingArtifact(FakeMemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict:
            self.ensure_calls.append(force)
            install_entered.set()
            release_install.wait(timeout=2)
            return dict(self.ensure_payload)

    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=BlockingArtifact(python=Path(sys.executable)),
        effective_home=tmp_path,
    )
    terminal_gc_entered = asyncio.Event()
    release_terminal_gc = asyncio.Event()
    cleanup_finished = threading.Event()
    cleanup_calls: list[bool] = []
    maintenance = _maintenance(runtime)

    async def blocked_terminal_gc() -> None:
        terminal_gc_entered.set()
        await release_terminal_gc.wait()

    manager = maintenance._backup_manager
    assert manager is not None
    monkeypatch.setattr(maintenance, "_run_terminal_snapshot_gc", blocked_terminal_gc)
    def reconcile_unpublished_backup_stages() -> None:
        cleanup_calls.append(runtime._artifact_installing)
        cleanup_finished.set()

    monkeypatch.setattr(manager, "reconcile_unpublished_backup_stages", reconcile_unpublished_backup_stages)

    maintenance.ensure_housekeeping()
    await terminal_gc_entered.wait()
    queued_reconcile = maintenance._backup_stage_reconcile_task
    assert queued_reconcile is not None

    installing = asyncio.create_task(runtime.install_artifact())
    assert await asyncio.to_thread(install_entered.wait, 1)
    release_terminal_gc.set()
    await queued_reconcile
    assert cleanup_calls == []

    release_install.set()
    assert (await installing)["ok"] is True
    assert await asyncio.to_thread(cleanup_finished.wait, 2)
    deferred_reconcile = maintenance._backup_stage_reconcile_task
    # The completion callback may clear a task before this assertion runs on a
    # fast event loop. Observe the cleanup contract instead of that transient
    # implementation detail, while still awaiting a task if it remains held.
    if deferred_reconcile is not None:
        assert deferred_reconcile is not queued_reconcile
        await deferred_reconcile
    assert cleanup_calls == [False]
    await memory_runtime_factory.close(runtime)


async def test_maintenance_backup_restore_round_trips_queue_state(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "backup-source")
    restored_call_log: list[None] = []
    _replace_runtime_port(
        runtime,
        restore_completed=lambda: restored_call_log.append(None),
    )

    backup = await _maintenance(runtime).create_backup("runtime-round-trip")
    meta = runtime._store.ensure_meta()
    runtime._store.reset_for_clear(target_epoch=meta.epoch + 1)
    assert runtime._store.list_queue_rows() == ()

    restored = await _maintenance(runtime).restore_backup(
        backup.snapshot_id,
        expected_manifest_sha256=backup.manifest_sha256,
        expected_surface_digests=backup.surface_digests(),
    )

    assert restored == backup
    assert restored_call_log == [None]
    rows = runtime._store.list_queue_rows()
    assert len(rows) == 1
    assert rows[0].source_message_digest
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize(
    ("crash_surface", "crash_target"),
    (
        ("queue", "state/memory/memory.sqlite"),
        ("provider", "memory/everos-root"),
        ("call_log", "memory/call-log/call-log.db"),
        ("attachments", "memory/attachments"),
    ),
)
async def test_backup_restore_crash_is_fenced_and_boot_converges_one_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_surface: str,
    crash_target: str,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "backup-queue")

    provider_marker = tmp_path / "memory/everos-root/generation.txt"
    attachment_marker = tmp_path / "memory/attachments/generation.txt"
    call_log = tmp_path / "memory/call-log/call-log.db"
    for marker in (provider_marker, attachment_marker):
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.parent.chmod(0o700)
        marker.write_text("backup")
        marker.chmod(0o600)
    call_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    call_log.parent.chmod(0o700)
    with sqlite3.connect(call_log) as connection:
        connection.execute("CREATE TABLE generation (value TEXT NOT NULL)")
        connection.execute("INSERT INTO generation VALUES ('backup')")
    call_log.chmod(0o600)

    backup = await _maintenance(runtime).create_backup("crash-safe-restore")
    _enqueue(runtime, "live-queue")
    provider_marker.write_text("live")
    attachment_marker.write_text("live")
    with sqlite3.connect(call_log) as connection:
        connection.execute("UPDATE generation SET value = 'live'")

    target = tmp_path / crash_target
    real_replace = snapshot_module.os.replace
    crashed = False
    intent_visible_before_replace = False

    class InjectedProcessDeath(BaseException):
        pass

    def crash_after_surface_install(source, destination, *args, **kwargs):
        nonlocal crashed, intent_visible_before_replace
        if (
            not crashed
            and Path(destination).name == target.name
            and f".restore-{backup.snapshot_id}-" in Path(source).name
            and kwargs.get("src_dir_fd") is not None
            and kwargs.get("dst_dir_fd") is not None
        ):
            open_operation = _maintenance(runtime)._backup_restore_journal.get_open_operation()
            assert open_operation is not None
            assert open_operation.state == "restoring"
            intent_visible_before_replace = True
            real_replace(source, destination, *args, **kwargs)
            crashed = True
            raise InjectedProcessDeath(crash_surface)
        return real_replace(source, destination, *args, **kwargs)

    async def process_died_before_cleanup(_operation):
        return None

    monkeypatch.setattr(snapshot_module.os, "replace", crash_after_surface_install)
    monkeypatch.setattr(
        _maintenance(runtime),
        "_mark_backup_restore_recovery",
        process_died_before_cleanup,
    )
    with pytest.raises(InjectedProcessDeath):
        await _maintenance(runtime).restore_backup(
            backup.snapshot_id,
            expected_manifest_sha256=backup.manifest_sha256,
            expected_surface_digests=backup.surface_digests(),
        )
    monkeypatch.setattr(snapshot_module.os, "replace", real_replace)

    assert crashed is True
    assert intent_visible_before_replace is True
    interrupted = _maintenance(runtime)._backup_restore_journal.get_open_operation()
    assert interrupted is not None
    assert interrupted.state == "restoring"
    assert [
        event.event
        for event in _maintenance(runtime)._backup_restore_journal.get_events(
            interrupted.operation_id
        )
    ] == ["started"]

    restarted = memory_runtime_factory(
        MemoryConfig(enabled=True),
        effective_home=tmp_path,
    )
    restored_call_log: list[None] = []
    _replace_runtime_port(
        restarted,
        restore_completed=lambda: restored_call_log.append(None),
    )
    recovery = _maintenance(restarted)._backup_restore_journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert _maintenance(restarted).is_open() is True
    assert restarted.module._worker._claims_paused is True
    assert restarted._worker_task is None
    assert restarted._process is None
    blocked = await restarted.module.capture(
        CaptureRequest(
            source_message_id="blocked-before-restore-recovery",
            session_id="session",
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provenance="user_input",
            text="must remain fenced",
            occurred_at_ms=2,
        )
    )
    assert blocked == CaptureSkipped(reason="memory_clear_failed")

    if crash_surface == "queue":
        original_reap = SidecarOwnership.reap

        async def fail_reap(_ownership) -> None:
            raise OSError("recorded sidecar is still live")

        monkeypatch.setattr(SidecarOwnership, "reap", fail_reap)
        assert await restarted.reconcile(MemoryConfig()) == {
            "ok": False,
            "error": "memory_clear_failed",
        }
        still_fenced = _maintenance(restarted)._backup_restore_journal.get_open_operation()
        assert still_fenced == recovery
        assert restarted.module._worker._claims_paused is True
        assert restored_call_log == []
        monkeypatch.setattr(SidecarOwnership, "reap", original_reap)

    assert await restarted.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert len(restarted._store.list_queue_rows()) == 1
    assert provider_marker.read_text() == "backup"
    assert attachment_marker.read_text() == "backup"
    with sqlite3.connect(call_log) as connection:
        assert connection.execute("SELECT value FROM generation").fetchone() == (
            "backup",
        )

    completed = _maintenance(restarted)._backup_restore_journal.get_operation(
        recovery.operation_id
    )
    assert completed is not None
    assert completed.state == "completed"
    assert completed.attempt_count == 2
    assert _maintenance(restarted)._backup_restore_journal.get_open_operation() is None
    assert [
        event.event
        for event in _maintenance(restarted)._backup_restore_journal.get_events(
            completed.operation_id
        )
    ] == ["started", "recovery_needed", "retry_started", "completed"]
    assert _maintenance(restarted).is_open() is False
    assert restored_call_log == [None]

    await memory_runtime_factory.close(runtime)
    await memory_runtime_factory.close(restarted)

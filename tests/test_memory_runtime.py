from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from core.managed_runtime import ManagedRuntimeArchive, ManagedRuntimeManifest
import core.memory.artifact as memory_artifact
import core.memory.confined_filesystem as confined_filesystem
import core.memory.module as memory_module
from core.memory.artifact import (
    FakeMemoryArtifactManager,
    MemoryArtifactCandidate,
    MemoryArtifactManager,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
)
from core.memory.clear_journal import MemoryClearJournal
from core.memory.maintenance import MemoryMaintenance
from core.memory.attachments import attachment_pin_root
from core.memory.everos import (
    AddAck,
    FakeMemoryProvider,
    FlushSucceeded,
    ProviderCapture,
    ProviderHealthSnapshot,
)
import core.memory.runtime as memory_runtime
from core.memory.process import (
    EverOSProcess,
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
    RebuildProcessResult,
)
from core.memory.runtime import (
    MemoryRuntime,
    MemorySessionLifecycleBusyError,
    MemoryStoreUnavailableError,
    create_memory_runtime,
)
from core.memory.everos_insight.recorder import initialize_call_log
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
from core.memory.store import MemoryStore
from core.memory.types import (
    CaptureAccepted,
    MemoryItem,
    MemoryItems,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    CaptureAttachment,
    CaptureRequest,
    ProviderSessionRef,
)
from config.v2_config import (
    AgentsConfig,
    MEMORY_RECOVERY_INTENTS,
    MemoryConfig,
    MemoryDiagnosticsConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
    atomic_update_memory,
)


PROJECT = "p-22222222222222222222222222222222"


def _maintenance(runtime: MemoryRuntime) -> MemoryMaintenance:
    maintenance = runtime._maintenance
    assert maintenance is not None
    return maintenance


def _installed_artifact(**overrides) -> FakeMemoryArtifactManager:
    """A verified, installed EverOS artifact — the common runtime-test baseline."""

    defaults: dict = {
        "python": Path(sys.executable),
        "root_format": "everos-1.2.3",
        "fingerprint": "test-artifact",
        "status_payload": {"reason": None},
    }
    defaults.update(overrides)
    return FakeMemoryArtifactManager(**defaults)


@pytest.fixture(autouse=True)
def _preflight_passes_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep runtime lifecycle tests hermetic unless they exercise preflight."""

    async def preflight(self: MemoryRuntime, config: MemoryConfig | None = None):
        return {"ok": True}

    monkeypatch.setattr(MemoryRuntime, "preflight", preflight)


async def test_memory_drain_task_reactivates_recovery_after_an_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
        effective_home=tmp_path,
    )
    drain_calls = 0

    async def drain() -> int:
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            raise RuntimeError("transient drain failure")
        runtime._config = replace(runtime._config, enabled=False)
        return 0

    async def no_wait(_seconds: float) -> None:
        return None

    activations: list[bool] = []
    begin_activation = runtime.module.begin_activation

    def track_activation(*, new_lease: bool = False) -> None:
        activations.append(new_lease)
        begin_activation(new_lease=new_lease)

    monkeypatch.setattr(runtime.module, "begin_activation", track_activation)
    monkeypatch.setattr(runtime.module, "drain", drain)
    monkeypatch.setattr("core.memory.runtime.asyncio.sleep", no_wait)

    runtime._ensure_worker()
    assert runtime._worker_task is not None
    await runtime._worker_task
    assert drain_calls == 2
    assert activations == [False, True]


async def test_memory_drain_recovery_waits_for_scheduled_flush_settlement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
        effective_home=tmp_path,
    )
    store = runtime._store
    assert store is not None
    accepted = store.enqueue_request(
        source_message_id="flush-before-transient-drain-failure",
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="settle this flush exactly once",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert accepted.row is not None
    claimed = store.claim_due(
        lease_owner="setup",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    assert store.settle_add_ack(
        claimed,
        AddAck(request_id="setup-add", status="accumulated"),
        lease_owner="setup",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        idle_timeout=timedelta(0),
    ).settled

    loop = asyncio.get_running_loop()
    flush_entered = threading.Event()
    flush_finished = threading.Event()
    release_flush = asyncio.Event()

    async def block_flush(_session_ref: ProviderSessionRef) -> None:
        flush_entered.set()
        await release_flush.wait()
        flush_finished.set()

    provider = FakeMemoryProvider(
        flush_results=deque(
            (FlushSucceeded(request_id="exact-flush", status="extracted"),)
        ),
        flush_hook=block_flush,
    )
    runtime._provider = provider
    runtime.module.replace_provider(provider)
    worker = runtime.module._worker
    coordinator = worker.coordinator

    original_claim_due = store.claim_due
    claim_failures = 0

    def fail_foreground_claim_once(*, lease_owner: str, now: str):
        nonlocal claim_failures
        claim_failures += 1
        if claim_failures == 1:
            assert flush_entered.wait(timeout=1.0)
            raise OSError("transient foreground store failure")
        return original_claim_due(lease_owner=lease_owner, now=now)

    monkeypatch.setattr(store, "claim_due", fail_foreground_claim_once)

    original_recovery = store.recover_after_boot
    recovery_calls = 0
    recovery_overlapped_flush: list[bool] = []

    def observe_recovery(*, lease_owner: str, clock):
        nonlocal recovery_calls
        recovery_calls += 1
        result = original_recovery(lease_owner=lease_owner, clock=clock)
        if recovery_calls == 2:
            recovery_overlapped_flush.append(not flush_finished.is_set())
            runtime._config = replace(runtime._config, enabled=False)
            loop.call_soon_threadsafe(release_flush.set)
        return result

    monkeypatch.setattr(store, "recover_after_boot", observe_recovery)

    original_pause_and_wait = worker.pause_and_wait

    async def release_during_quiesce(*, timeout_seconds: float = 30.0) -> bool:
        release_flush.set()
        return await original_pause_and_wait(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(worker, "pause_and_wait", release_during_quiesce)

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr("core.memory.runtime.asyncio.sleep", no_wait)
    runtime._ensure_worker()
    assert runtime._worker_task is not None
    await asyncio.wait_for(runtime._worker_task, timeout=2.0)
    assert await coordinator.pause_and_wait(timeout_seconds=1.0)

    state = store.get_session_flush_state(accepted.row.provider_session_ref)
    assert recovery_calls == 2
    assert recovery_overlapped_flush == [False]
    assert provider.flushes == [accepted.row.provider_session_ref]
    assert state is not None
    assert (state.state, state.unflushed_count) == ("idle", 0)
    assert store.has_manual_required_fence() is False
    await memory_runtime_factory.close(runtime)


def _settings() -> EverOSProcessSettings:
    return EverOSProcessSettings(
        llm_base_url="https://llm.example.test/v1",
        llm_model="chat",
        llm_api_key="llm-secret",
        embedding_base_url="https://embed.example.test/v1",
        embedding_model="embed",
        embedding_api_key="embedding-secret",
    )


def test_memory_runtime_factory_degrades_when_private_modes_cannot_be_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store_dir = tmp_path / "state" / "memory"
    store_dir.mkdir(parents=True, mode=0o755)
    os.chmod(store_dir, 0o755)
    monkeypatch.setattr("core.memory.store.os.chmod", lambda *_args, **_kwargs: None)

    runtime = create_memory_runtime(MemoryConfig(enabled=True))

    assert runtime.available is False
    assert asyncio.run(runtime.status_payload()) == {
        "status": "ok",
        "source": {
            "status": "unavailable",
            "observed_at": None,
            "reason": "memory_sidecar_unavailable",
        },
        "health": None,
    }
    assert asyncio.run(runtime.maintenance_payload()) == {
        "status": "ok",
        "data_exists": True,
        "can_clear": False,
        "clear_recovery": None,
    }


async def test_memory_runtime_reopens_the_store_after_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """An unavailable runtime is a state, not a permanent identity.

    The previous shape returned a distinct UnavailableMemoryRuntime class that
    built a delegate on the next reconciliation. Recovery must still work now
    that one class owns both states.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    repaired = tmp_path / "repaired" / "memory.sqlite"
    open_attempts = 0

    def open_store(*_args: object, **_kwargs: object) -> MemoryStore:
        nonlocal open_attempts
        open_attempts += 1
        if open_attempts == 1:
            raise OSError("temporary store initialization failure")
        return MemoryStore(repaired)

    monkeypatch.setattr("core.memory.runtime.MemoryStore", open_store)

    runtime = memory_runtime_factory.register(
        create_memory_runtime(
            MemoryConfig(enabled=True),
            artifact_manager=_installed_artifact(
                python=None,
                status_payload={"reason": "memory_runtime_missing"},
            ),
        )
    )
    assert runtime.available is False
    # Reads stay closed, and capture is absorbed rather than raising.
    assert await runtime.profile_payload("u-" + "0" * 32, PROJECT) == {
        "status": "failed",
        "error": "memory_store_unavailable",
    }
    with pytest.raises(MemoryStoreUnavailableError):
        runtime.principal_for_user_key("avibe:local")

    # An enabled reconciliation reopens it; the runtime is the same object.
    result = await runtime.reconcile(MemoryConfig(enabled=True))
    assert runtime.available is True
    # The artifact is still missing, so enablement fails for that reason, not
    # for the store.
    assert result["ok"] is False
    assert result["error"] != "memory_store_unavailable"
    assert runtime.principal_for_user_key("avibe:local").startswith("u-")
    await memory_runtime_factory.close(runtime)




def test_memory_artifact_uses_shared_manager_status_shape(tmp_path: Path) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    status = manager.status()

    assert status["id"] == "memory-runtime"
    assert status["status"] == "missing"
    assert status["reason"] == "memory_runtime_unpublished"
    assert manager.provider_root_format() is None
    assert manager.artifact_fingerprint() is None


def test_memory_artifact_requires_exact_python_lock_and_builder_provenance(tmp_path: Path) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    payload = {
        "release_state": "published",
        "python_version": memory_artifact.EMBEDDED_PYTHON_VERSION,
        "lock_sha256": memory_artifact.PACKAGE_LOCK_SHA256,
        "lock_id": f"uv-lock-sha256:{memory_artifact.PACKAGE_LOCK_SHA256}",
        "uv_version": memory_artifact.RUNTIME_BUILDER_UV_VERSION,
        "provider_root_format": "everos-1.2.3",
        "compatible_provider_root_formats": [],
    }
    manifest = ManagedRuntimeManifest(
        schema_version=1,
        runtime_version=memory_artifact.EVEROS_VERSION,
        source="test",
        source_url=None,
        archives={},
        digest="a" * 64,
        loaded_from="test",
        payload=payload,
    )

    assert manager._manifest_installable(manifest) is True
    for key in ("python_version", "lock_sha256", "lock_id", "uv_version"):
        changed = dict(payload)
        changed[key] = "wrong"
        invalid = replace(manifest, payload=changed)
        assert manager._manifest_installable(invalid) is False
        assert manager._install_reason == "memory_runtime_manifest_invalid"


def test_memory_artifact_uses_configured_dev_runtime_without_managed_archive(
    monkeypatch, caplog, tmp_path: Path
) -> None:
    dev_python = tmp_path / "dev-venv" / "bin" / "python"
    dev_python.parent.mkdir(parents=True)
    dev_python.write_text("#!/bin/sh\n", encoding="utf-8")
    dev_python.chmod(0o755)
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(dev_python))
    calls: list[list[str]] = []

    def smoke_succeeds(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="1.2.3\n3.12.12\n", stderr="")

    monkeypatch.setattr(memory_artifact.subprocess, "run", smoke_succeeds)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    def unexpected_manifest_load(*_args, **_kwargs) -> None:
        raise AssertionError("development runtime must not load the managed manifest")

    monkeypatch.setattr(manager, "_load_manifest", unexpected_manifest_load)

    status = manager.status()
    ensured = manager.ensure(force=True)

    assert manager.resolve_binary() == dev_python
    assert manager.resolve_python() == dev_python
    assert status["installed"] is True
    assert status["status"] == "ready"
    assert status["path"] == str(dev_python)
    assert status["reason"] is None
    assert ensured["ok"] is True
    assert ensured["changed"] is False
    assert manager.provider_root_format() == "everos-1.2.3"
    assert manager.compatible_provider_root_formats() == frozenset({"everos-1.2.3"})
    assert manager.artifact_fingerprint() == "dev-everos-1.2.3"
    assert len(calls) == 2
    assert "DEV RUNTIME bypass active - not for production" in caplog.text


def test_memory_artifact_smoke_requires_pinned_cli_cascade_module(
    monkeypatch, tmp_path: Path
) -> None:
    dev_python = tmp_path / "dev-venv" / "bin" / "python"
    dev_python.parent.mkdir(parents=True)
    dev_python.write_text("#!/bin/sh\n", encoding="utf-8")
    dev_python.chmod(0o755)
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(dev_python))
    commands: list[list[str]] = []

    def smoke_succeeds(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="1.2.3\n3.12.12\n",
            stderr="",
        )

    monkeypatch.setattr(memory_artifact.subprocess, "run", smoke_succeeds)

    assert MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True).resolve_python() == dev_python
    assert "everos.entrypoints.cli.main" in commands[0][-1]
    assert "everos.memory.cascade" in commands[0][-1]
    assert "everalgo.types.modality" in commands[0][-1]


def test_memory_artifact_rejects_scrubber_incompatibility_as_repairable_dependency_failure(
    monkeypatch, tmp_path: Path
) -> None:
    dev_python = tmp_path / "dev-venv" / "bin" / "python"
    dev_python.parent.mkdir(parents=True)
    dev_python.write_text("#!/bin/sh\n", encoding="utf-8")
    dev_python.chmod(0o755)
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(dev_python))

    def scrubber_fails(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if "-I" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="1.2.3\n3.12.12\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="incompatible patch")

    monkeypatch.setattr(memory_artifact.subprocess, "run", scrubber_fails)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    assert manager.resolve_python() is None
    assert manager.status()["reason"] == "memory_runtime_install_failed"


def test_memory_artifact_prepare_maps_scrubber_rejection_to_public_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "python"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    def smoke_succeeds(
        command: list[str],
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="1.2.3\n3.12.12\n",
            stderr="",
        )

    monkeypatch.setattr(memory_artifact.subprocess, "run", smoke_succeeds)
    monkeypatch.setattr(manager, "_admit_error_scrubbers", lambda _binary: False)

    assert manager._prepare_binary(binary) == {
        "ok": False,
        "reason": "memory_runtime_install_failed",
    }


@pytest.mark.parametrize(
    "failure",
    [
        OSError("cannot execute"),
        subprocess.CompletedProcess(["python"], 1, stdout="", stderr="incompatible"),
    ],
)
def test_memory_artifact_prepare_maps_smoke_rejection_to_public_install_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException | subprocess.CompletedProcess[str],
) -> None:
    binary = tmp_path / "python"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    def reject_smoke(
        _command: list[str],
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(memory_artifact.subprocess, "run", reject_smoke)

    assert manager._prepare_binary(binary) == {
        "ok": False,
        "reason": "memory_runtime_install_failed",
    }


def test_memory_artifact_refuses_dev_runtime_without_importable_everos(monkeypatch, caplog, tmp_path: Path) -> None:
    dev_python = tmp_path / "dev-venv" / "bin" / "python"
    dev_python.parent.mkdir(parents=True)
    dev_python.write_text("#!/bin/sh\n", encoding="utf-8")
    dev_python.chmod(0o755)
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(dev_python))

    def smoke_fails(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="ModuleNotFoundError")

    monkeypatch.setattr(memory_artifact.subprocess, "run", smoke_fails)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    status = manager.status()
    ensured = manager.ensure(force=True)

    assert manager.resolve_binary() is None
    assert manager.resolve_python() is None
    assert status["installed"] is False
    assert status["status"] == "error"
    assert status["reason"] == "memory_runtime_install_failed"
    assert ensured == {
        "ok": False,
        "reason": "memory_runtime_install_failed",
        "download_error": None,
    }
    assert "refusing DEV RUNTIME bypass" in caplog.text


def test_memory_artifact_dev_runtime_retries_after_failure_at_same_path(monkeypatch, tmp_path: Path) -> None:
    """A failed dev-runtime probe must not be cached as a permanent failure.

    A developer who starts Vibe before everos is importable, then installs/fixes
    everos at the same path and hits Repair, must see it resolve without a restart
    or env-string change. Only successful probes are cached.
    """
    dev_python = tmp_path / "dev-venv" / "bin" / "python"
    dev_python.parent.mkdir(parents=True)
    dev_python.write_text("#!/bin/sh\n", encoding="utf-8")
    dev_python.chmod(0o755)
    monkeypatch.setenv("AVIBE_MEMORY_DEV_RUNTIME", str(dev_python))

    # The developer hasn't installed everos yet; once they do (flipped to True),
    # every subsequent probe at the same path succeeds.
    everos_installed = {"installed": False}

    def smoke_then_succeeds(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if not everos_installed["installed"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="ModuleNotFoundError")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{memory_artifact.EVEROS_VERSION}\n{memory_artifact.EMBEDDED_PYTHON_VERSION}\n",
            stderr="",
        )

    monkeypatch.setattr(memory_artifact.subprocess, "run", smoke_then_succeeds)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    # First probe fails (everos not yet importable at this path).
    assert manager.resolve_binary() is None
    assert manager.status()["status"] == "error"

    # Developer installs everos at the same path, then hits Repair.
    everos_installed["installed"] = True

    # Same path, same env value — developer fixed everos and hit Repair.
    # The failed probe must NOT be cached; this call retries and succeeds.
    resolved = manager.resolve_binary()
    assert resolved is not None
    assert resolved == dev_python
    assert manager.status()["installed"] is True


def test_memory_artifact_dev_runtime_bypass_is_off_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    status = manager.status()
    ensured = manager.ensure(force=True)

    assert manager.resolve_python() is None
    assert status["installed"] is False
    assert status["status"] == "missing"
    assert status["reason"] == "memory_runtime_unpublished"
    assert ensured["ok"] is False
    assert ensured["reason"] == "memory_runtime_unpublished"


@pytest.mark.parametrize("pointer_contents", [b"not-json", b"[]"])
def test_memory_artifact_status_marks_unreadable_active_pointer_as_error(tmp_path: Path, pointer_contents: bytes) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    manager.runtime_dir.mkdir(parents=True)
    (manager.runtime_dir / "current.json").write_bytes(pointer_contents)

    status = manager.status()

    assert status["installed"] is False
    assert status["status"] == "error"
    assert status["reason"] == "memory_runtime_install_failed"
    assert stat.S_IMODE(manager.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((manager.runtime_dir / "current.json").stat().st_mode) == 0o600


def test_memory_artifact_hardens_a_valid_legacy_active_pointer(tmp_path: Path) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    manager.runtime_dir.mkdir(parents=True, mode=0o755)
    manager.runtime_dir.chmod(0o755)
    current = manager.runtime_dir / "current.json"
    current.write_text(json.dumps({"legacy": True}), encoding="utf-8")
    current.chmod(0o644)

    assert manager._read_active_pointer() == ({"legacy": True}, False)
    assert stat.S_IMODE(manager.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(current.stat().st_mode) == 0o600


def test_memory_artifact_refuses_to_harden_a_hardlinked_legacy_pointer(
    tmp_path: Path,
) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    manager.runtime_dir.mkdir(parents=True, mode=0o755)
    manager.runtime_dir.chmod(0o755)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"outside": True}), encoding="utf-8")
    outside.chmod(0o644)
    os.link(outside, manager.runtime_dir / "current.json")

    assert manager._read_active_pointer() == (None, True)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert json.loads(outside.read_text(encoding="utf-8")) == {"outside": True}


@pytest.mark.parametrize("binary_state", ["missing", "tampered"])
def test_memory_artifact_status_marks_broken_active_binary_as_error(tmp_path: Path, binary_state: str) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    install_dir = manager.runtime_dir / "versions" / "old"
    binary = install_dir / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": str(install_dir),
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
    }
    (install_dir / manager.spec.metadata_filename).write_text(
        json.dumps(
            {
                **pointer,
                "binary_sha256": binary_sha256,
            }
        ),
        encoding="utf-8",
    )
    manager._restore_current_pointer(pointer)
    if binary_state == "missing":
        binary.unlink()
    else:
        binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    status = manager.status()

    assert status["installed"] is False
    assert status["status"] == "error"
    assert status["reason"] == "memory_runtime_install_failed"


@pytest.mark.parametrize(
    "mismatch",
    [
        {"platform": "plan9-vax"},
        {"runtime_version": "0.0.1"},
    ],
)
def test_memory_artifact_rejects_an_active_pointer_built_for_another_target(
    tmp_path: Path,
    mismatch: dict,
) -> None:
    """Well-formed is not usable.

    Installation rejects a manifest whose runtime_version is not
    EVEROS_VERSION, but the active pointer outlives that check: a ``~/.avibe``
    copied between architectures, or an upgrade that moves EVEROS_VERSION,
    would otherwise keep resolving an executable this build cannot run, and
    Dependencies would report ready until the sidecar failed much later.
    """

    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    install_dir = manager.runtime_dir / "versions" / "old"
    binary = install_dir / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": str(install_dir),
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
        **mismatch,
    }
    (install_dir / manager.spec.metadata_filename).write_text(
        json.dumps({**pointer, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    manager._restore_current_pointer(pointer)

    # The binary itself is intact and its metadata agrees with the pointer, so
    # only the target check can reject this.
    assert manager.resolve_python() is None


@pytest.mark.parametrize("admitted", [False, True])
def test_memory_artifact_readmits_active_pointer_after_contract_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    admitted: bool,
) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    install_dir = manager.runtime_dir / "versions" / "old"
    binary = install_dir / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": str(install_dir),
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
    }
    (install_dir / manager.spec.metadata_filename).write_text(
        json.dumps(
            {
                **pointer,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    manager._restore_current_pointer(pointer)
    admissions: list[Path] = []

    def admit(candidate: Path) -> dict[str, object]:
        admissions.append(candidate)
        return {
            "ok": admitted,
            "reason": None if admitted else "memory_runtime_install_failed",
        }

    monkeypatch.setattr(manager, "_prepare_binary", admit)

    resolved = manager.resolve_python()

    assert resolved == (binary if admitted else None)
    assert admissions == [binary]
    active = manager._active_pointer()
    assert active is not None
    if admitted:
        assert active["admission_revision"] == memory_artifact.ARTIFACT_ADMISSION_REVISION
        assert active["admission_ok"] is True
        assert manager.resolve_python() == binary
        assert admissions == [binary]
    else:
        assert active["admission_revision"] == memory_artifact.ARTIFACT_ADMISSION_REVISION
        assert active["admission_ok"] is False
        assert manager.resolve_python() is None
        assert manager.status()["reason"] == "memory_runtime_install_failed"
        assert admissions == [binary]


@pytest.mark.parametrize("admitted", [False, True])
def test_memory_artifact_readmits_existing_install_before_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    admitted: bool,
) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)
    binary_contents = b"#!/bin/sh\nexit 0\n"
    binary_sha256 = hashlib.sha256(binary_contents).hexdigest()
    archive = ManagedRuntimeArchive(
        platform=memory_artifact.runtime_platform_tag(),
        name="memory-runtime.tar.gz",
        url="file:///memory-runtime.tar.gz",
        sha256="d" * 64,
        binary_sha256=binary_sha256,
        size=1,
        bin_path="bin/python",
    )
    manifest = ManagedRuntimeManifest(
        schema_version=1,
        runtime_version=memory_artifact.EVEROS_VERSION,
        source="test",
        source_url=None,
        archives={archive.platform: archive},
        digest="c" * 64,
        loaded_from="test",
        payload={
            "release_state": "published",
            "python_version": memory_artifact.EMBEDDED_PYTHON_VERSION,
            "lock_sha256": memory_artifact.PACKAGE_LOCK_SHA256,
            "lock_id": f"uv-lock-sha256:{memory_artifact.PACKAGE_LOCK_SHA256}",
            "uv_version": memory_artifact.RUNTIME_BUILDER_UV_VERSION,
            "provider_root_format": "everos-1.2.3",
            "compatible_provider_root_formats": [],
        },
    )
    install_dir = manager._manifest_install_dir(manifest, archive)
    binary = install_dir / archive.bin_path
    binary.parent.mkdir(parents=True)
    binary.write_bytes(binary_contents)
    binary.chmod(0o755)
    manager._write_manifest_install_metadata(
        install_dir,
        manifest,
        archive,
        binary_sha256=binary_sha256,
    )
    manager._restore_current_pointer(
        {
            "provider": "manifest",
            "runtime_id": manager.spec.runtime_id,
            "runtime_version": manifest.runtime_version,
            "platform": archive.platform,
            "install_dir": str(install_dir),
            "manifest_sha256": manifest.digest,
            "archive_sha256": archive.sha256,
            "bin_path": archive.bin_path,
        }
    )
    monkeypatch.setattr(manager, "_load_manifest", lambda *, allow_network: manifest)
    admissions: list[Path] = []

    def admit(candidate: Path) -> dict[str, object]:
        admissions.append(candidate)
        return {
            "ok": admitted,
            "reason": None if admitted else "memory_runtime_install_failed",
        }

    monkeypatch.setattr(manager, "_prepare_binary", admit)

    result = manager.ensure(force=False)

    assert result["ok"] is admitted
    assert result.get("reason") is (None if admitted else "memory_runtime_install_failed")
    assert admissions == [binary]
    active = manager._active_pointer()
    assert active is not None
    assert active["admission_revision"] == memory_artifact.ARTIFACT_ADMISSION_REVISION
    assert active["admission_ok"] is admitted
    assert manager.resolve_python() == (binary if admitted else None)
    if not admitted:
        assert manager.status()["reason"] == "memory_runtime_install_failed"
    assert admissions == [binary]


def test_memory_artifact_coordinator_rolls_back_the_active_pointer(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
    provider_root.parent.chmod(0o700)
    sentinel = provider_root / ".avibe-memory-root.json"
    sentinel.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider_root_id": "root-id",
                "provider_id": "everos",
                "provider_root_format": "everos-1.0",
                "created_by_artifact_fingerprint": "old-artifact",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(sentinel, 0o600)
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )
    manager.runtime_dir.mkdir(parents=True)
    manager.runtime_dir.chmod(0o700)
    previous_pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": "/runtime/old",
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
        "admission_revision": memory_artifact.ARTIFACT_ADMISSION_REVISION,
        "admission_ok": True,
        "provider_root_format": "everos-1.0",
        "compatible_provider_root_formats": [],
        "artifact_fingerprint": "old-artifact",
    }
    current_pointer = manager.runtime_dir / "current.json"
    current_pointer.write_text(json.dumps(previous_pointer), encoding="utf-8")
    current_pointer.chmod(0o600)
    calls: list[tuple[str, object]] = []

    def coordinate(candidate, root_state, commit, rollback) -> None:
        calls.append(("candidate", candidate.provider_root_format))
        calls.append(("root", (root_state.provider_root_format, root_state.empty)))
        commit()
        assert (
            manager._active_pointer()["admission_revision"]
            == memory_artifact.ARTIFACT_ADMISSION_REVISION
        )
        assert manager._active_pointer()["admission_ok"] is True
        calls.append(("active", manager.provider_root_format()))
        rollback()

    manager.set_activation_coordinator(coordinate)
    manager._write_current_pointer(
        tmp_path / "runtime" / "versions" / "candidate",
        _artifact_manifest("everos-2.0", compatible_formats=["everos-1.0"]),
        _artifact_archive(),
    )

    assert calls == [
        ("candidate", "everos-2.0"),
        ("root", ("everos-1.0", True)),
        ("active", "everos-2.0"),
    ]
    assert json.loads((manager.runtime_dir / "current.json").read_text(encoding="utf-8")) == previous_pointer
    assert manager.provider_root_format() == "everos-1.0"


def test_memory_artifact_pending_reset_can_commit_when_old_root_is_incompatible(
    tmp_path: Path,
) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
    provider_root.parent.chmod(0o700)
    (provider_root / "data").write_text("old", encoding="utf-8")
    (provider_root / "data").chmod(0o600)
    (provider_root / ".avibe-memory-root.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider_root_id": "root-id",
                "provider_id": "everos",
                "provider_root_format": "everos-1.0",
                "created_by_artifact_fingerprint": "old-artifact",
            }
        ),
        encoding="utf-8",
    )
    (provider_root / ".avibe-memory-root.json").chmod(0o600)
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )
    manager.runtime_dir.mkdir(parents=True, mode=0o700)
    calls: list[object] = []

    def defer_pointer(_candidate, root_state, commit, _rollback) -> None:
        calls.append(root_state)
        assert root_state is None
        commit()

    manager.set_activation_coordinator(defer_pointer)
    manager._write_current_pointer(
        manager.runtime_dir / "versions" / "candidate",
        _artifact_manifest("everos-2.0", compatible_formats=[]),
        _artifact_archive(),
    )

    assert calls == [None]
    assert manager._active_pointer() is not None


def test_memory_artifact_first_activation_rollback_does_not_need_temp_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )
    connect_calls = 0

    def fail_connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        raise sqlite3.OperationalError("temporary ordering unavailable")

    def reject_candidate(_candidate, root_state, commit, rollback) -> None:
        assert root_state.exists is False
        commit()
        assert (manager.runtime_dir / "current.json").is_file()
        rollback()
        raise MemoryRuntimeActivationError("candidate rejected")

    monkeypatch.setattr(confined_filesystem.sqlite3, "connect", fail_connect)
    manager.set_activation_coordinator(reject_candidate)

    with pytest.raises(MemoryRuntimeActivationError, match="candidate rejected"):
        manager._write_current_pointer(
            manager.runtime_dir / "versions" / "candidate",
            _artifact_manifest("everos-2.0", compatible_formats=[]),
            _artifact_archive(),
        )

    assert connect_calls == 0
    assert not (manager.runtime_dir / "current.json").exists()
    assert not provider_root.exists()


async def test_memory_artifact_rollback_resolves_old_active_binary(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=tmp_path / "memory" / "everos-root",
    )
    old_install_dir = manager.runtime_dir / "versions" / "old"
    old_binary = old_install_dir / "bin" / "python"
    old_binary.parent.mkdir(parents=True)
    old_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    old_binary.chmod(0o755)
    binary_sha256 = hashlib.sha256(old_binary.read_bytes()).hexdigest()
    old_pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": str(old_install_dir),
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
        "admission_revision": memory_artifact.ARTIFACT_ADMISSION_REVISION,
        "admission_ok": True,
        "provider_root_format": "everos-1.0",
        "compatible_provider_root_formats": [],
        "artifact_fingerprint": "old-artifact",
    }
    (old_install_dir / manager.spec.metadata_filename).write_text(
        json.dumps(
            {
                "provider": "manifest",
                "runtime_id": "memory-runtime",
                "runtime_version": memory_artifact.EVEROS_VERSION,
                "platform": memory_artifact.runtime_platform_tag(),
                "manifest_sha256": "a" * 64,
                "archive_sha256": "b" * 64,
                "binary_sha256": binary_sha256,
                "bin_path": "bin/python",
            }
        ),
        encoding="utf-8",
    )
    manager._restore_current_pointer(old_pointer)

    def candidate_fails(_candidate, _root_state, commit, rollback) -> None:
        commit()
        rollback()

    manager.set_activation_coordinator(candidate_fails)
    manager._write_current_pointer(
        manager.runtime_dir / "versions" / "candidate",
        _artifact_manifest("everos-2.0", compatible_formats=["everos-1.0"]),
        _artifact_archive(),
    )

    assert manager._active_pointer() == old_pointer
    assert manager.resolve_python() == old_binary

    factory = FakeEverOSProcessFactory()

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=manager,
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(runtime._config))["ok"] is True
    assert [process.python for process in factory.supervised] == [old_binary]
    await memory_runtime_factory.close(runtime)


async def test_runtime_controller_port_never_copies_processing_credentials(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-secret"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embedding-secret"),
    )
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
    )

    assert runtime._provider._llm_api_key is None
    assert runtime._provider._embedding_api_key is None

    assert await runtime.reconcile(MemoryConfig(enabled=False, processing=processing)) == {
        "ok": True,
        "state": "disabled",
    }
    assert runtime._provider._llm_api_key is None
    assert runtime._provider._embedding_api_key is None
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize(
    "journal_attribute",
    ["_clear_journal", "_backup_restore_journal"],
)
async def test_disable_stops_live_runtime_when_maintenance_journal_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    journal_attribute: str,
    memory_runtime_factory,
) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig(
            "https://embed.example.test/v1",
            "embed",
            "embed-key",
        ),
    )
    enabled = MemoryConfig(enabled=True, processing=processing)
    disabled = replace(enabled, enabled=False)
    factory = FakeEverOSProcessFactory()

    runtime = memory_runtime_factory(
        enabled,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert await runtime.reconcile(enabled) == {"ok": True, "state": "ready"}
    sidecar = factory.supervised[0]
    assert runtime._worker_task is not None
    assert runtime._process_records_calls is True

    terminal_gc = _maintenance(runtime)._terminal_snapshot_gc_task
    backup_reconcile = _maintenance(runtime)._backup_stage_reconcile_task
    if terminal_gc is not None:
        await terminal_gc
    if backup_reconcile is not None:
        await backup_reconcile

    journal = getattr(_maintenance(runtime), journal_attribute)
    assert journal is not None

    def unreadable_journal() -> None:
        raise OSError("maintenance journal is unreadable")

    async def recovery_must_not_run() -> bool:
        raise AssertionError("disable must not mutate an unreadable journal")

    monkeypatch.setattr(journal, "get_open_operation", unreadable_journal)
    monkeypatch.setattr(_maintenance(runtime), "recover_boot", recovery_must_not_run)

    changed = replace(
        enabled,
        processing=replace(
            processing,
            llm=replace(processing.llm, model="changed-while-unreadable"),
        ),
    )
    assert await runtime.reconcile(changed) == {
        "ok": False,
        "error": "memory_clear_failed",
    }
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_clear_failed",
    }
    assert runtime._worker_task is not None
    assert sidecar.stopped is False

    from core.controller import Controller

    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=enabled)
    controller.memory_runtime = runtime
    controller.memory_module = runtime.module
    assert await controller.reconcile_memory(disabled) == {
        "ok": True,
        "state": "disabled",
    }
    assert controller.config.memory is disabled
    assert runtime._config is disabled
    assert runtime._restart_config == disabled
    assert runtime.module._worker._claims_paused is True
    assert runtime._worker_task is None
    assert runtime._process is None
    assert runtime._process_records_calls is False
    assert sidecar.stopped is True
    assert sidecar.running is False
    assert len(factory.supervised) == 1
    await memory_runtime_factory.close(runtime)


async def test_reconcile_never_downloads_a_missing_runtime(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    def _artifact() -> FakeMemoryArtifactManager:
        return FakeMemoryArtifactManager(
            python=None,
            status_payload={"reason": "memory_runtime_missing"},
            root_format=None,
            fingerprint=None,
            compatible_formats=frozenset(),
            ensure_failure=AssertionError("startup reconciliation must not download Memory"),
        )

    artifact = _artifact()
    disabled = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=artifact,
        effective_home=tmp_path / "disabled",
    )
    enabled = memory_runtime_factory(
        MemoryConfig(enabled=True),
        artifact_manager=artifact,
        effective_home=tmp_path / "enabled",
    )

    assert await disabled.reconcile(MemoryConfig(enabled=False)) == {
        "ok": True,
        "state": "disabled",
    }
    assert await enabled.reconcile(MemoryConfig(enabled=True)) == {
        "ok": False,
        "error": "memory_runtime_missing",
    }
    await memory_runtime_factory.close(disabled)
    await memory_runtime_factory.close(enabled)


def _recording_ownership(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: BaseException | None = None,
) -> list[dict[str, Path]]:
    """Replace the runtime's locked recovery process with a reap recorder."""

    reaps: list[dict[str, Path]] = []

    class _Recovery:
        def __init__(
            self,
            python,
            *,
            effective_home: Path,
            provider_root: Path,
            **_kwargs,
        ) -> None:
            assert python is None
            self._inputs = {
                "record_path": effective_home / "memory" / ".rt" / "everos.sidecar.json",
                "socket_path": effective_home / "memory" / ".rt" / "everos.sock",
                "provider_root": provider_root,
            }

        async def reconcile_orphan(self) -> None:
            reaps.append(self._inputs)
            if failure is not None:
                raise failure

    monkeypatch.setattr(memory_runtime, "EverOSRebuildProcess", _Recovery)
    return reaps


async def test_recorded_orphan_recovery_can_fail_closed_for_root_deletion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    reaps = _recording_ownership(monkeypatch, failure=RuntimeError("orphan still serving"))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )

    with pytest.raises(RuntimeError, match="orphan still serving"):
        await runtime._reap_recorded_sidecar_if_unowned(fail_closed=True)
    assert len(reaps) == 1
    await memory_runtime_factory.close(runtime)


async def test_recorded_orphan_recovery_routes_through_provider_root_exclusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    recoveries: list[tuple[Path | None, Path]] = []

    class _LockedRecovery:
        def __init__(
            self,
            python,
            *,
            effective_home: Path,
            provider_root: Path,
            **_kwargs,
        ) -> None:
            assert python is None
            self._inputs = (effective_home, provider_root)

        async def reconcile_orphan(self) -> None:
            recoveries.append(self._inputs)

    class _UnlockedOwnership:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("runtime recovery bypassed provider-root exclusion")

    monkeypatch.setattr(
        memory_runtime,
        "EverOSRebuildProcess",
        _LockedRecovery,
        raising=False,
    )
    monkeypatch.setattr(
        memory_runtime,
        "SidecarOwnership",
        _UnlockedOwnership,
        raising=False,
    )
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )

    assert await runtime.reconcile(MemoryConfig(enabled=False)) == {
        "ok": True,
        "state": "disabled",
    }
    assert recoveries == [(tmp_path, tmp_path / "memory" / "everos-root")]
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("boot", ["disabled", "runtime_missing", "store_unavailable"])
async def test_recorded_orphan_recovery_runs_on_boots_that_never_launch_a_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boot: str,
    memory_runtime_factory,
) -> None:
    """The reap used to live only on the way to spawning a replacement.

    A boot that never spawns one never reached it, so an orphan from the previous
    run kept serving the socket and holding the provider root indefinitely: a
    settings save that persisted ``enabled = false`` before reconciliation could
    stop the child, a runtime artifact that will not resolve, or a store that
    will not open. Recovery now runs before all three early returns.
    """

    reaps = _recording_ownership(monkeypatch)
    home = tmp_path / boot
    artifact = (
        FakeMemoryArtifactManager(python=None, status_payload={"reason": "memory_runtime_missing"})
        if boot == "runtime_missing"
        else _installed_artifact()
    )
    config = MemoryConfig(enabled=boot == "runtime_missing")
    runtime = memory_runtime_factory(config, artifact_manager=artifact, effective_home=home)
    if boot == "store_unavailable":
        # The store never opened, which returns before the reconcile lock.
        runtime._module = None

    result = await runtime.reconcile(config)

    expected = (
        {"ok": False, "error": "memory_runtime_missing"}
        if boot == "runtime_missing"
        else {"ok": True, "state": "disabled"}
    )
    assert result == expected
    # Recovery ran, and against this home's own record, socket, and root -- the
    # three inputs every ownership claim is decided against.
    assert reaps == [
        {
            "record_path": home / "memory" / ".rt" / "everos.sidecar.json",
            "socket_path": home / "memory" / ".rt" / "everos.sock",
            "provider_root": home / "memory" / "everos-root",
        }
    ]
    await memory_runtime_factory.close(runtime)


async def test_store_reopen_failure_maintains_call_log_after_orphan_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    maintained = threading.Event()

    def maintain(path: Path) -> None:
        assert path == db_path
        maintained.set()

    monkeypatch.setattr(memory_runtime, "maintain_call_log", maintain)
    _recording_ownership(monkeypatch)
    config = MemoryConfig(enabled=True)

    runtime = memory_runtime_factory(config, effective_home=tmp_path)
    runtime._module = None
    monkeypatch.setattr(runtime, "_open_store", lambda: False)

    assert await runtime.reconcile(config) == {
        "ok": False,
        "error": "memory_store_unavailable",
    }
    assert await asyncio.to_thread(maintained.wait, 1)
    await memory_runtime_factory.close(runtime)


async def test_store_reopen_failure_does_not_maintain_call_log_beside_unreaped_orphan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    _recording_ownership(monkeypatch, failure=RuntimeError("orphan still owns call log"))
    config = MemoryConfig(enabled=True)

    runtime = memory_runtime_factory(config, effective_home=tmp_path)
    runtime._module = None
    monkeypatch.setattr(runtime, "_open_store", lambda: False)

    assert await runtime.reconcile(config) == {
        "ok": False,
        "error": "memory_store_unavailable",
    }
    assert runtime._call_log_retention_task is None
    await memory_runtime_factory.close(runtime)


async def test_disabled_boot_retires_the_record_of_a_sidecar_that_is_already_gone(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """The same recovery, observed through its effect rather than a stub.

    A boot that finds Memory disabled used to leave the previous run's ownership
    record untouched, because nothing on that path ever read it. Here the sidecar
    it names has already exited and its group is empty, so the recovery has
    nothing to signal and simply retires the record -- proving the disabled path
    reaches the recovery at all.
    """

    record_path = tmp_path / "memory" / ".rt" / "everos.sidecar.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    # A pid no process holds, so the reap is a pure record decision.
    record_path.write_text(
        json.dumps(
            {
                "pid": _ORPHAN_PID,
                "create_time": _ORPHAN_CREATE_TIME,
                "process_group": _ORPHAN_PID,
                "socket_path": str(tmp_path / "memory" / ".rt" / "everos.sock"),
                "provider_root": str(tmp_path / "memory" / "everos-root"),
            }
        ),
        encoding="utf-8",
    )
    config = MemoryConfig(enabled=False)
    runtime = memory_runtime_factory(config, artifact_manager=_installed_artifact(), effective_home=tmp_path)

    assert await runtime.reconcile(config) == {"ok": True, "state": "disabled"}

    assert not record_path.exists()
    await memory_runtime_factory.close(runtime)


async def test_recorded_orphan_recovery_never_reaps_a_child_this_runtime_owns(
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    """The one way this fix could be worse than the bug it closes.

    After a successful launch the record names our own live sidecar, so a
    recovery that ran on every settings save would classify that child as ours
    and kill it. ``self._process is None`` is the guard: the supervisor is
    assigned before ``start`` writes the record, so a runtime holding a child
    never reaches the reap.
    """

    reaps = _recording_ownership(monkeypatch)
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
        ),
    )

    runtime = memory_runtime_factory(config, artifact_manager=_installed_artifact(), process_factory=factory)
    # First reconciliation: no child exists yet, so recovery is free to run.
    assert (await runtime.reconcile(config))["ok"] is True
    assert len(reaps) == 1
    assert runtime._process is not None

    # Every later settings save finds a child of ours, and must leave it be.
    for _ in range(2):
        assert (await runtime.reconcile(config))["ok"] is True
    assert len(reaps) == 1
    assert factory.supervised[-1].stopped is False
    await memory_runtime_factory.close(runtime)


async def test_recorded_orphan_recovery_cannot_overlap_a_concurrent_launch(
    monkeypatch: pytest.MonkeyPatch,
    memory_runtime_factory,
) -> None:
    """A reap in flight must not be able to retire a record a launch just wrote.

    The reap runs for up to two stop-timeout rounds and retires the record when
    it finishes. Unserialized, a reconciliation that launched a child during
    those rounds would have its fresh record deleted by the finishing reap,
    leaving a live sidecar no boot could find -- the state an unwritable record
    is already made to fail a start over. Sharing the reconcile lock is what
    makes a launch and a reap mutually exclusive.
    """

    started = asyncio.Event()
    release = asyncio.Event()
    factory = FakeEverOSProcessFactory()
    reaps = 0

    class _Recovery:
        def __init__(self, _python, **_kwargs) -> None:
            return None

        async def reconcile_orphan(self) -> None:
            nonlocal reaps
            reaps += 1
            if reaps == 1:
                # Stand in for the signal rounds of a genuinely live orphan.
                started.set()
                await release.wait()

    monkeypatch.setattr(memory_runtime, "EverOSRebuildProcess", _Recovery)
    config = MemoryConfig(
        enabled=True,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
        ),
    )

    runtime = memory_runtime_factory(config, artifact_manager=_installed_artifact(), process_factory=factory)
    try:
        reaping = asyncio.create_task(runtime.reconcile(config))
        await started.wait()
        launching = asyncio.create_task(runtime.reconcile(config))
        # Real time, not bare yields: a reconciliation reaches its launch
        # through `asyncio.to_thread` hops, which bare yields never let
        # finish, so yielding alone would hold whether or not the two are
        # serialized. Breaks early on the failure, so only the passing case
        # waits out the whole window.
        for _ in range(40):
            await asyncio.sleep(0.01)
            if factory.supervised:
                break
        overlapped = list(factory.supervised)
        release.set()
        results = list(await asyncio.gather(reaping, launching))
    finally:
        # Nothing is asserted while those tasks are in flight: a failure has
        # to leave a closed runtime behind, not a hung event loop.
        release.set()
        await memory_runtime_factory.close(runtime)

    assert overlapped == [], "a launch overlapped a reap that was still running"
    assert [result["ok"] for result in results] == [True, True]
    # Both reconciliations still completed, one after the other.
    assert len(factory.supervised) == 2


async def test_recorded_orphan_recovery_failure_still_applies_a_disable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog,
    memory_runtime_factory,
) -> None:
    """A reap that will not finish must not block a disable the user saved.

    Nothing will spawn a replacement on this path, so there is no second sidecar
    to protect the provider root from, and the user cannot act on the failure
    anyway. The record is kept, so the enabled path still fails closed on it.
    """

    reaps = _recording_ownership(
        monkeypatch,
        failure=RuntimeError(f"orphaned sidecar did not exit (pid {_ORPHAN_PID}, record /x/everos.sidecar.json)"),
    )
    config = MemoryConfig(enabled=False)
    runtime = memory_runtime_factory(config, artifact_manager=_installed_artifact(), effective_home=tmp_path)

    with caplog.at_level(logging.WARNING, logger=memory_runtime.logger.name):
        result = await runtime.reconcile(config)

    assert result == {"ok": True, "state": "disabled"}
    assert len(reaps) == 1
    assert "Recorded EverOS sidecar recovery did not finish" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text
    await memory_runtime_factory.close(runtime)




async def test_final_flush_fences_capture_before_queue_visibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("AVIBE_HOME", str(runtime_home))
    store = MemoryStore()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=store,
        artifact_manager=_installed_artifact(),
        effective_home=runtime_home,
    )
    flush_entered = asyncio.Event()
    release_flush = asyncio.Event()

    async def block_flush(_session_ref: ProviderSessionRef) -> None:
        flush_entered.set()
        await release_flush.wait()

    provider = FakeMemoryProvider(
        flush_results=deque(
            (FlushSucceeded(request_id="old-session-flush", status="extracted"),)
        ),
        flush_hook=block_flush,
    )
    runtime._provider = provider
    runtime.module.replace_provider(provider)

    source_root = runtime_home / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700)
    source = source_root / "old.txt"
    source.write_bytes(b"old session attachment")
    source.chmod(0o600)
    old_request = CaptureRequest(
        source_message_id="old-source",
        session_id="shared-session",
        principal_id="u-11111111111111111111111111111111",
        project_id=PROJECT,
        provenance="user_input",
        text="old session message",
        occurred_at_ms=1_725_000_001_234,
        attachments=(
            CaptureAttachment(
                kind="doc",
                name=source.name,
                uri=source.as_uri(),
                ext="txt",
            ),
        ),
    )
    later_request = replace(
        old_request,
        source_message_id="later-source",
        text="later session message",
        occurred_at_ms=1_725_000_001_235,
        attachments=(),
    )

    pin_entered = asyncio.Event()
    release_pin = asyncio.Event()
    handshake_timeout_seconds = 5.0
    original_pin = runtime.module._attachment_store.pin

    async def gate_attachment_pin(operation, /, *args, **kwargs):
        if operation == original_pin:
            pin_entered.set()
            await asyncio.wait_for(
                release_pin.wait(), timeout=handshake_timeout_seconds
            )
            return operation(*args, **kwargs)
        return await original_run_blocking(operation, *args, **kwargs)

    original_run_blocking = memory_module.run_blocking
    monkeypatch.setattr(memory_module, "run_blocking", gate_attachment_pin)

    class ObservedAdmissionLock:
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._acquisitions = 0
            self.final_flush_waiting = asyncio.Event()
            self.later_capture_waiting = asyncio.Event()

        async def acquire(self) -> bool:
            self._acquisitions += 1
            if self._acquisitions == 2:
                self.final_flush_waiting.set()
            elif self._acquisitions == 3:
                self.later_capture_waiting.set()
            return await self._lock.acquire()

        def locked(self) -> bool:
            return self._lock.locked()

        def release(self) -> None:
            self._lock.release()

        async def __aenter__(self):
            await self.acquire()
            return self

        async def __aexit__(self, exc_type, exc_value, traceback) -> None:
            del exc_type, exc_value, traceback
            self.release()

    admission_lock = ObservedAdmissionLock()
    runtime.module._capture_admission_locks[
        (
            old_request.principal_id,
            old_request.project_id,
            old_request.session_id,
        )
    ] = admission_lock

    try:
        old_capture = asyncio.create_task(runtime.module.capture(old_request))
        await asyncio.wait_for(pin_entered.wait(), timeout=handshake_timeout_seconds)

        final_flush = asyncio.create_task(
            runtime.final_flush(
                principal_id=old_request.principal_id,
                project_id=old_request.project_id,
                raw_session_id=old_request.session_id,
                deadline_seconds=2.0,
            )
        )
        await asyncio.wait_for(
            admission_lock.final_flush_waiting.wait(),
            timeout=handshake_timeout_seconds,
        )
        later_capture = asyncio.create_task(runtime.module.capture(later_request))
        await asyncio.wait_for(
            admission_lock.later_capture_waiting.wait(),
            timeout=handshake_timeout_seconds,
        )
        assert admission_lock.locked()
        assert not final_flush.done()
        assert not later_capture.done()

        release_pin.set()
        assert isinstance(await old_capture, CaptureAccepted)

        await asyncio.wait_for(flush_entered.wait(), timeout=handshake_timeout_seconds)
        assert [capture.text for capture in provider.captures] == [
            "old session message"
        ]
        assert len(provider.flushes) == 1
        assert not final_flush.done()
        assert not later_capture.done()
        assert all(
            row.payload_text != "later session message"
            for row in store.list_queue_rows()
        )

        release_flush.set()
        assert await final_flush

        assert isinstance(await later_capture, CaptureAccepted)
        later_rows = [
            row
            for row in store.list_queue_rows()
            if row.payload_text == "later session message"
        ]
        assert len(later_rows) == 1
        assert later_rows[0].state == "pending"
    finally:
        release_pin.set()
        release_flush.set()
        await memory_runtime_factory.close(runtime)


async def test_session_lifecycle_fences_capture_through_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = MemoryStore()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=store,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    provider = FakeMemoryProvider()
    runtime._provider = provider
    runtime.module.replace_provider(provider)
    principal_id = "u-11111111111111111111111111111111"
    session_id = "lifecycle-session"
    request = CaptureRequest(
        source_message_id="after-reset-source",
        session_id=session_id,
        principal_id=principal_id,
        project_id=PROJECT,
        provenance="user_input",
        text="message after reset",
        occurred_at_ms=1_725_000_001_234,
        attachments=(),
    )

    reset_entered = asyncio.Event()
    release_reset = asyncio.Event()
    order: list[str] = []

    async def reset_session() -> str:
        order.append("reset-entered")
        reset_entered.set()
        await release_reset.wait()
        order.append("reset-committed")
        return "reset-complete"

    lifecycle = asyncio.create_task(
        runtime.run_session_lifecycle(
            principal_id=principal_id,
            project_id=PROJECT,
            raw_session_id=session_id,
            operation=reset_session,
            deadline_seconds=2.0,
        )
    )
    await asyncio.wait_for(reset_entered.wait(), timeout=1.0)
    capture = asyncio.create_task(runtime.module.capture(request))
    await asyncio.sleep(0)

    assert not capture.done()
    assert all(
        row.payload_text != request.text
        for row in store.list_queue_rows()
    )

    release_reset.set()
    assert await lifecycle == "reset-complete"
    assert isinstance(await capture, CaptureAccepted)
    order.append("capture-enqueued")
    assert order == [
        "reset-entered",
        "reset-committed",
        "capture-enqueued",
    ]
    await memory_runtime_factory.close(runtime)


async def test_multi_scope_session_lifecycle_holds_every_fence_through_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    first_scope = (
        "u-11111111111111111111111111111111",
        "p-11111111111111111111111111111111",
    )
    second_scope = (
        "u-22222222222222222222222222222222",
        "p-22222222222222222222222222222222",
    )
    session_id = "multi-scope-lifecycle-session"
    operation_entered = asyncio.Event()
    release_operation = asyncio.Event()

    async def archive_session() -> str:
        operation_entered.set()
        await release_operation.wait()
        return "archived"

    lifecycle = asyncio.create_task(
        runtime.run_session_scopes_lifecycle(
            scopes=(second_scope, first_scope, second_scope),
            raw_session_id=session_id,
            operation=archive_session,
            deadline_seconds=2.0,
        )
    )
    captures: list[asyncio.Task[object]] = []
    try:
        await asyncio.wait_for(operation_entered.wait(), timeout=1.0)
        captures = [
            asyncio.create_task(
                runtime.module.capture(
                    CaptureRequest(
                        source_message_id=f"multi-scope-{index}",
                        session_id=session_id,
                        principal_id=principal_id,
                        project_id=project_id,
                        provenance="user_input",
                        text=f"scope {index} after lifecycle",
                        occurred_at_ms=1_725_000_001_234 + index,
                        attachments=(),
                    )
                )
            )
            for index, (principal_id, project_id) in enumerate(
                (first_scope, second_scope),
                start=1,
            )
        ]
        await asyncio.sleep(0)
        assert all(not capture.done() for capture in captures)

        release_operation.set()
        assert await lifecycle == "archived"
        capture_results = await asyncio.gather(*captures)
        assert all(isinstance(result, CaptureAccepted) for result in capture_results)
    finally:
        release_operation.set()
        await asyncio.gather(lifecycle, *captures, return_exceptions=True)
        await memory_runtime_factory.close(runtime)


async def test_cancelled_multi_scope_lifecycle_releases_partially_acquired_fences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    scopes = (
        (
            "u-11111111111111111111111111111111",
            "p-11111111111111111111111111111111",
        ),
        (
            "u-22222222222222222222222222222222",
            "p-22222222222222222222222222222222",
        ),
    )
    session_id = "cancelled-multi-scope-lifecycle"

    first_lock, second_lock = [
        runtime.module._capture_admission_lock(
            principal_id=principal_id,
            project_id=project_id,
            session_id=session_id,
        )
        for principal_id, project_id in scopes
    ]
    await second_lock.acquire()

    async def archive_session() -> None:
        raise AssertionError("archive must not run before every fence is held")

    lifecycle = asyncio.create_task(
        runtime.run_session_scopes_lifecycle(
            scopes=scopes,
            raw_session_id=session_id,
            operation=archive_session,
            deadline_seconds=2.0,
        )
    )
    for _ in range(20):
        if first_lock.locked():
            break
        await asyncio.sleep(0)
    assert first_lock.locked()
    # Let ``wait_for`` return the acquired first lock and enter the wait for
    # the already-held second lock before cancelling the outer lifecycle.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not lifecycle.done()

    lifecycle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lifecycle
    assert not first_lock.locked()
    second_lock.release()
    await memory_runtime_factory.close(runtime)


async def test_session_lifecycle_does_not_reset_when_capture_fence_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    principal_id = "u-11111111111111111111111111111111"
    session_id = "busy-lifecycle-session"

    admission_lock = runtime.module._capture_admission_lock(
        principal_id=principal_id,
        project_id=PROJECT,
        session_id=session_id,
    )
    await admission_lock.acquire()
    operation_called = False

    async def reset_session() -> None:
        nonlocal operation_called
        operation_called = True

    try:
        with pytest.raises(
            MemorySessionLifecycleBusyError,
            match="did not quiesce",
        ):
            await runtime.run_session_lifecycle(
                principal_id=principal_id,
                project_id=PROJECT,
                raw_session_id=session_id,
                operation=reset_session,
                deadline_seconds=0.01,
            )
        assert operation_called is False
    finally:
        admission_lock.release()
        await memory_runtime_factory.close(runtime)


async def test_retired_close_aborts_when_claim_quiescence_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    original_quiesce_claims = runtime.module.quiesce_claims_for_clear
    original_prepare_shutdown = runtime.module.prepare_shutdown

    async def refuse_quiescence(*_args, **_kwargs) -> bool:
        return False

    async def shutdown_must_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("shutdown must not run after a failed claim drain")

    monkeypatch.setattr(runtime.module, "quiesce_claims_for_clear", refuse_quiescence)
    monkeypatch.setattr(runtime.module, "prepare_shutdown", shutdown_must_not_run)
    runtime.retire()
    with pytest.raises(RuntimeError, match="did not quiesce"):
        await runtime.close()
    assert runtime.closed is False

    monkeypatch.setattr(
        runtime.module,
        "quiesce_claims_for_clear",
        original_quiesce_claims,
    )
    monkeypatch.setattr(runtime.module, "prepare_shutdown", original_prepare_shutdown)
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("outcome", ["exception", "cancellation"])
async def test_session_lifecycle_releases_capture_fence_after_aborted_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    provider = FakeMemoryProvider()
    runtime._provider = provider
    runtime.module.replace_provider(provider)
    principal_id = "u-11111111111111111111111111111111"
    session_id = f"aborted-{outcome}-session"
    request = CaptureRequest(
        source_message_id=f"after-{outcome}-source",
        session_id=session_id,
        principal_id=principal_id,
        project_id=PROJECT,
        provenance="user_input",
        text=f"message after {outcome}",
        occurred_at_ms=1_725_000_001_234,
        attachments=(),
    )

    reset_entered = asyncio.Event()
    keep_reset_open = asyncio.Event()

    async def abort_reset() -> None:
        reset_entered.set()
        if outcome == "exception":
            raise RuntimeError("reset failed")
        await keep_reset_open.wait()

    lifecycle = asyncio.create_task(
        runtime.run_session_lifecycle(
            principal_id=principal_id,
            project_id=PROJECT,
            raw_session_id=session_id,
            operation=abort_reset,
            deadline_seconds=2.0,
        )
    )
    await asyncio.wait_for(reset_entered.wait(), timeout=1.0)
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="reset failed"):
            await lifecycle
    else:
        lifecycle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lifecycle

    receipt = await asyncio.wait_for(runtime.module.capture(request), timeout=1.0)
    assert isinstance(receipt, CaptureAccepted)
    await memory_runtime_factory.close(runtime)


async def test_final_flush_deadline_includes_capture_admission_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    principal_id = "u-11111111111111111111111111111111"
    session_id = "deadline-session"

    admission_lock = runtime.module._capture_admission_lock(
        principal_id=principal_id,
        project_id=PROJECT,
        session_id=session_id,
    )
    await admission_lock.acquire()
    try:
        started = asyncio.get_running_loop().time()
        assert not await runtime.final_flush(
            principal_id=principal_id,
            project_id=PROJECT,
            raw_session_id=session_id,
            deadline_seconds=0.02,
        )
        assert asyncio.get_running_loop().time() - started < 0.5
    finally:
        admission_lock.release()
        await memory_runtime_factory.close(runtime)




async def test_runtime_exposes_interrupted_clear_without_starting_sidecar(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    started: list[object] = []

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    factory = FakeEverOSProcessFactory()
    started = factory.created

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    journal = MemoryClearJournal(tmp_path)
    journal.start(
        operation_id="interrupted-clear",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )

    assert await runtime.reconcile(runtime._config) == {"ok": False, "error": "memory_clear_failed"}
    assert started == []
    recovery = _maintenance(runtime)._clear_journal.get_open_operation()
    assert recovery is not None
    assert recovery.operation_id == "interrupted-clear"
    assert recovery.state == "recovery_needed"
    await memory_runtime_factory.close(runtime)


async def test_runtime_install_artifact_uses_controller_owned_manager(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    calls: list[bool] = []

    artifact = FakeMemoryArtifactManager(
        root_format=None,
        fingerprint=None,
        compatible_formats=frozenset(),
        ensure_payload={"ok": False, "reason": "memory_runtime_unpublished", "download_error": None},
    )
    calls = artifact.ensure_calls
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )

    assert await runtime.install_artifact() == {
        "ok": False,
        "reason": "memory_runtime_unpublished",
        "download_error": None,
    }
    assert callable(artifact.activation_coordinator)
    assert calls == [True]
    assert runtime._config.enabled is False
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("recovery_intent", sorted(MEMORY_RECOVERY_INTENTS))
async def test_runtime_repairs_artifact_without_activating_pending_recovery(
    tmp_path: Path,
    memory_runtime_factory,
    monkeypatch: pytest.MonkeyPatch,
    recovery_intent: str,
) -> None:
    """Repair admits only the pointer while every durable recovery fence remains set."""

    class _RepairArtifact(FakeMemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict:
            self.ensure_calls.append(force)
            assert self.activation_coordinator is not None
            self.activation_coordinator(
                MemoryArtifactCandidate(
                    provider_root_format="everos-1.2.3",
                    compatible_provider_root_formats=frozenset({"everos-1.2.3"}),
                    artifact_fingerprint="repaired-artifact",
                ),
                MemoryProviderRootState(exists=True, empty=False),
                commit,
                rollback,
            )
            return dict(self.ensure_payload)

    committed: list[bool] = []
    rolled_back: list[bool] = []

    def commit() -> None:
        committed.append(True)

    def rollback() -> None:
        rolled_back.append(True)

    artifact = _RepairArtifact(python=Path(sys.executable))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False, recovery_intent=recovery_intent),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    assert runtime.module._worker._claims_paused is True
    monkeypatch.setattr(
        runtime,
        "_activate_artifact_candidate",
        lambda *_args: pytest.fail("pending recovery repair must not activate Runtime"),
    )

    result = await runtime.install_artifact()

    assert result == {"ok": True, "reason": None, "download_error": None}
    assert artifact.ensure_calls == [True]
    assert committed == [True]
    assert rolled_back == []
    assert runtime._config.recovery_intent == recovery_intent
    assert runtime._restart_config.recovery_intent == recovery_intent
    await memory_runtime_factory.close(runtime)


async def test_failed_fresh_runtime_stays_available_for_pending_reset_repair(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    pending = replace(runtime._config, recovery_intent="factory_reset")

    await runtime.retain_factory_reset_recovery(pending)

    assert runtime.available is True
    assert runtime.retired is False
    assert runtime.closed is False
    assert runtime.factory_reset_pending is True
    assert runtime.module._worker._claims_paused is True
    assert await runtime.install_artifact() == {
        "ok": True,
        "reason": None,
        "download_error": None,
    }

    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("recovery_intent", sorted(MEMORY_RECOVERY_INTENTS))
async def test_pending_recovery_repair_reports_pointer_commit_failure(
    tmp_path: Path,
    memory_runtime_factory,
    monkeypatch: pytest.MonkeyPatch,
    recovery_intent: str,
) -> None:
    """A failed admission commit remains a failed Repair with the fence intact."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    class _RepairArtifact(FakeMemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict:
            self.ensure_calls.append(force)
            assert self.activation_coordinator is not None
            self.activation_coordinator(
                MemoryArtifactCandidate(
                    provider_root_format="everos-1.2.3",
                    compatible_provider_root_formats=frozenset({"everos-1.2.3"}),
                    artifact_fingerprint="repaired-artifact",
                ),
                MemoryProviderRootState(exists=True, empty=False),
                commit,
                lambda: pytest.fail("an atomic commit failure has nothing to roll back"),
            )
            return dict(self.ensure_payload)

    def commit() -> None:
        raise OSError("simulated pointer commit failure")

    artifact = _RepairArtifact(python=Path(sys.executable))
    pending = MemoryConfig(enabled=False, recovery_intent=recovery_intent)
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=pending,
    ).save()
    runtime = memory_runtime_factory(
        pending,
        artifact_manager=artifact,
        effective_home=tmp_path,
    )

    assert await runtime.install_artifact() == {
        "ok": False,
        "reason": "memory_runtime_install_failed",
        "download_error": None,
    }
    assert artifact.ensure_calls == [True]
    assert runtime._config.recovery_intent == recovery_intent
    assert runtime._restart_config.recovery_intent == recovery_intent
    assert V2Config.load().memory.recovery_intent == recovery_intent
    await memory_runtime_factory.close(runtime)


async def test_pending_rebuild_install_publishes_artifact_for_explicit_retry(
    tmp_path: Path,
    memory_runtime_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-REBUILD-202"""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    manifest = _artifact_manifest("everos-1.2.3", compatible_formats=[])
    binary_contents = b"#!/bin/sh\nexit 0\n"
    archive = ManagedRuntimeArchive(
        platform=memory_artifact.runtime_platform_tag(),
        name="memory-runtime.tar.gz",
        url="file:///memory-runtime.tar.gz",
        sha256="d" * 64,
        binary_sha256=hashlib.sha256(binary_contents).hexdigest(),
        size=1,
        bin_path="bin/python",
    )

    class _MissingArtifact(MemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict:
            assert force is True
            self._write_current_pointer(install_dir, manifest, archive)
            return {"ok": True, "reason": None, "download_error": None}

    pending = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=pending,
    ).save()
    artifact = _MissingArtifact(
        runtime_dir=tmp_path / "runtime" / "memory-runtime",
        provider_root=tmp_path / "memory" / "everos-root",
        offline=True,
    )
    install_dir = artifact._manifest_install_dir(manifest, archive)
    binary = install_dir / archive.bin_path
    binary.parent.mkdir(parents=True)
    binary.write_bytes(binary_contents)
    binary.chmod(0o755)
    artifact._write_manifest_install_metadata(
        install_dir,
        manifest,
        archive,
        binary_sha256=archive.binary_sha256,
    )
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        pending,
        artifact_manager=artifact,
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert runtime.module._worker._claims_paused is True

    assert await runtime.install_artifact() == {
        "ok": True,
        "reason": None,
        "download_error": None,
    }
    pointer = artifact._active_pointer()
    assert pointer is not None
    assert pointer["install_dir"] == str(install_dir)
    assert pointer["admission_ok"] is True
    assert artifact.resolve_python() == binary
    assert factory.created == []
    assert runtime.module._worker._claims_paused is True
    assert V2Config.load().memory.recovery_intent == "rebuild"

    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)
    monkeypatch.setattr(runtime, "_probe_processing", lambda *_args: asyncio.sleep(0, result=True))
    assert await runtime.rebuild() == {
        "ok": True,
        "result": "completed_empty",
        "state": "ready",
    }
    assert V2Config.load().memory.recovery_intent is None
    assert runtime._restart_config.recovery_intent is None
    assert len(factory.supervised) == 1
    await memory_runtime_factory.close(runtime)


async def test_artifact_repair_rejects_uninspectable_root_without_pending_recovery(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """Pointer-only admission is exclusive to a supported durable recovery fence."""

    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=FakeMemoryArtifactManager(python=Path(sys.executable)),
        effective_home=tmp_path,
    )

    with pytest.raises(
        MemoryRuntimeActivationError,
        match="provider root could not be inspected",
    ):
        runtime._coordinate_artifact_activation(
            MemoryArtifactCandidate(
                provider_root_format="everos-1.2.3",
                compatible_provider_root_formats=frozenset({"everos-1.2.3"}),
                artifact_fingerprint="candidate",
            ),
            None,
            lambda: pytest.fail("ordinary repair must not commit"),
            lambda: pytest.fail("nothing was committed to roll back"),
        )

    await memory_runtime_factory.close(runtime)


async def test_runtime_install_artifact_converts_background_ensure_exception(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    artifact = _installed_artifact(ensure_failure=RuntimeError("install failed"))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=artifact,
        effective_home=tmp_path,
    )
    assert await runtime.install_artifact() == {
        "ok": False,
        "reason": "memory_runtime_install_failed",
        "download_error": None,
    }
    await memory_runtime_factory.close(runtime)


def test_distribution_metadata_bundles_only_the_memory_runtime_manifest() -> None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        import tomli as tomllib

    project_root = Path(__file__).resolve().parents[1]
    build_config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"][
        "build"
    ]
    memory_entries = [
        entry
        for entry in (*build_config["artifacts"], *build_config["targets"]["sdist"]["include"])
        if "memory_runtime" in entry
    ]

    assert memory_entries == [
        "vibe/memory_runtime_manifest.json",
        "vibe/memory_runtime_manifest.json",
    ]
    assert not list((project_root / "vibe").glob("memory_runtime*.tar.gz"))
    assert not list((project_root / "vibe").glob("memory_runtime*.tgz"))
    assert not list((project_root / "vibe").glob("memory_runtime*.zip"))


async def test_runtime_repair_stops_retained_down_supervisor_before_replacing_artifact(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    events: list[str] = []

    class _Artifact(FakeMemoryArtifactManager):
        """Assert the retained supervisor is already gone when ensure runs."""

        def ensure(self, *, force: bool = False) -> dict:
            assert force is True
            assert runtime._process is None
            events.append("ensure")
            return super().ensure(force=force)

    class _DownProcess:
        # A failed supervisor retains its retry task even after its child exits.
        running = False
        consecutive_failures = 5
        down = True

        async def stop(self) -> None:
            events.append("stop")

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(root_format=None, fingerprint=None),
        effective_home=tmp_path,
    )
    runtime._process = _DownProcess()

    async def pause_and_wait() -> bool:
        events.append("pause")
        return True

    monkeypatch.setattr(runtime.module, "quiesce_claims", pause_and_wait)

    assert await runtime.install_artifact() == {
        "ok": True,
        "reason": None,
        "download_error": None,
    }
    assert events == ["pause", "stop", "ensure"]
    assert runtime._process is None
    await memory_runtime_factory.close(runtime)


async def test_runtime_repair_rejects_healthy_running_sidecar(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """A healthy running sidecar must not be force-stopped/replaced via Repair.

    Only a retained down supervisor (no live child) may be stopped for Repair; a
    live sidecar requires a coordinated disable first.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    events: list[str] = []

    class _Artifact(FakeMemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict:
            events.append("ensure")
            return super().ensure(force=force)

    class _LiveProcess:
        running = True  # healthy sidecar with a live child
        consecutive_failures = 0
        down = False

        async def stop(self) -> None:
            events.append("stop")  # must NOT be called

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(root_format=None, fingerprint=None),
        effective_home=tmp_path,
    )
    runtime._process = _LiveProcess()

    result = await runtime.install_artifact()
    assert result == {
        "ok": False,
        "reason": "memory_runtime_install_requires_disabled_memory",
        "download_error": None,
    }
    # The healthy sidecar was neither stopped nor replaced.
    assert events == []
    assert runtime._process is not None
    await memory_runtime_factory.close(runtime)


async def test_runtime_activation_timeout_cancels_and_settles_submitted_coroutine(tmp_path: Path, monkeypatch) -> None:
    cleanup_started = asyncio.Event()
    allow_cleanup_to_finish = asyncio.Event()
    runtime = MemoryRuntime(
        MemoryConfig(enabled=False),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
        effective_home=tmp_path,
    )
    runtime._activation_loop = asyncio.get_running_loop()

    async def activation_with_slow_cancellation(*_args) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup_to_finish.wait()
            raise

    monkeypatch.setattr(runtime, "_activate_artifact_candidate", activation_with_slow_cancellation)
    monkeypatch.setattr("core.memory.runtime.ARTIFACT_ACTIVATION_TIMEOUT_SECONDS", 0.01)

    coordinate = asyncio.create_task(
        asyncio.to_thread(
            runtime._coordinate_artifact_activation,
            MemoryArtifactCandidate(
                provider_root_format="everos-1.2.3",
                compatible_provider_root_formats=frozenset({"everos-1.2.3"}),
                artifact_fingerprint="candidate-artifact",
            ),
            MemoryProviderRootState(exists=False),
            lambda: None,
            lambda: None,
        )
    )

    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    await asyncio.sleep(0.02)
    assert coordinate.done() is False

    allow_cleanup_to_finish.set()
    with pytest.raises(MemoryRuntimeActivationError, match="timed out"):
        await asyncio.wait_for(coordinate, timeout=1.0)


async def test_runtime_rejects_embedding_change_when_root_inspection_fails_under_lifecycle_lock(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    instances: list[object] = []

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    factory = FakeEverOSProcessFactory()
    instances = factory.supervised

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)

    runtime = memory_runtime_factory(
        initial,
        artifact_manager=_Artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(initial))["ok"] is True
    lifecycle_lock_states: list[bool] = []

    def inspection_failure() -> bool:
        lifecycle_lock_states.append(runtime.module._lifecycle_lock.locked())
        raise OSError("root inspection unavailable")

    monkeypatch.setattr(runtime, "_provider_data_exists_strict", inspection_failure, raising=False)
    updated = replace(
        initial,
        processing=replace(
            processing,
            embedding=replace(processing.embedding, model="embed-v2"),
        ),
    )

    assert await runtime.reconcile(updated) == {"ok": False, "error": "memory_clear_failed"}
    assert lifecycle_lock_states == [True]
    assert len(instances) == 1
    assert instances[0].stopped is False
    assert runtime._config is initial
    assert runtime.module._worker._claims_paused is False
    await memory_runtime_factory.close(runtime)


async def test_runtime_restart_rechecks_persisted_embedding_candidate(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v2", "embed-key"),
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=False,
            processing=processing,
            recovery_intent="rebuild",
        ),
    ).save()
    restarted = V2Config.load().memory
    inspected: list[bool] = []
    factory = FakeEverOSProcessFactory()

    runtime = memory_runtime_factory(
        restarted,
        artifact_manager=_Artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )

    def existing_vectors() -> bool:
        inspected.append(runtime.module._lifecycle_lock.locked())
        return True

    monkeypatch.setattr(runtime, "_provider_data_exists_strict", existing_vectors, raising=False)
    assert await runtime.reconcile(restarted) == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    assert runtime._config == restarted
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)
    assert inspected == []
    # The pending marker must fence without launching any child.
    assert factory.created == []


async def test_runtime_boot_with_pending_empty_candidate_stays_fenced_and_down(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    factory = FakeEverOSProcessFactory()

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed-v2", "embed-key"),
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=True,
            processing=processing,
            recovery_intent="rebuild",
        ),
    ).save()
    restarted = V2Config.load().memory

    runtime = memory_runtime_factory(
        restarted,
        artifact_manager=_Artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False, raising=False)
    assert await runtime.reconcile(restarted) == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    assert runtime._config.recovery_intent == "rebuild"
    assert runtime._restart_config.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    assert old.stopped is True
    assert factory.created == []
    assert V2Config.load().memory.recovery_intent == "rebuild"
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("durable_pending", [False, True])
async def test_runtime_reconcile_prefers_durable_marker_only_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
    durable_pending: bool,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    settled = MemoryConfig(enabled=False, processing=_processing_config())
    pending = replace(settled, recovery_intent="rebuild")
    durable = pending if durable_pending else settled
    stale = settled if durable_pending else pending
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=durable,
    ).save()
    runtime = memory_runtime_factory(
        stale,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )

    result = await runtime.reconcile(stale)

    if durable_pending:
        assert result == {
            "ok": False,
            "error": "memory_embedding_rebuild_required",
        }
        assert runtime.module._worker._claims_paused is True
    else:
        assert result == {"ok": True, "state": "disabled"}
    assert runtime._config.recovery_intent == durable.recovery_intent
    assert runtime._restart_config.recovery_intent == durable.recovery_intent
    await memory_runtime_factory.close(runtime)


async def test_runtime_artifact_activation_rolls_back_root_and_sidecar(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    instances: list[object] = []
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=tmp_path / "memory" / "everos-root",
    )
    manager.runtime_dir.mkdir(parents=True)
    previous_pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": str(manager.runtime_dir / "versions" / "old"),
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
        "provider_root_format": "everos-1.0",
        "compatible_provider_root_formats": [],
        "artifact_fingerprint": "old-artifact",
    }
    manager._restore_current_pointer(previous_pointer)
    monkeypatch.setattr(manager, "resolve_python", lambda: Path(sys.executable))
    monkeypatch.setattr(manager, "status", lambda: {"reason": None})

    class _Process(FakeEverOSProcess):
        """Refuse to boot against the incompatible activated root format."""

        async def start(self) -> bool:
            if manager.provider_root_format() == "everos-2.0":
                self.starts += 1
                return False
            return await super().start()

    factory = FakeEverOSProcessFactory(template=_Process)
    instances = factory.supervised

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    runtime = memory_runtime_factory(
        initial,
        store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
        artifact_manager=manager,
        process_factory=factory,
    )
    assert (await runtime.reconcile(initial))["ok"] is True

    with pytest.raises(MemoryRuntimeActivationError):
        await asyncio.to_thread(
            manager._write_current_pointer,
            manager.runtime_dir / "versions" / "candidate",
            _artifact_manifest("everos-2.0", compatible_formats=[]),
            _artifact_archive(),
        )

    assert manager._active_pointer() == previous_pointer
    assert manager.provider_root_format() == "everos-1.0"
    assert len(instances) == 3
    assert instances[0].stopped is True
    assert instances[1].stopped is True
    assert instances[2].stopped is False
    assert runtime.module._worker._claims_paused is False
    await memory_runtime_factory.close(runtime)


async def test_runtime_artifact_activation_switches_incompatible_empty_root(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    instances: list[object] = []
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=tmp_path / "memory" / "everos-root",
    )
    manager.runtime_dir.mkdir(parents=True)
    manager._restore_current_pointer(
        {
            "provider": "manifest",
            "runtime_id": "memory-runtime",
            "runtime_version": "1.0",
            "platform": "darwin-arm64",
            "install_dir": str(manager.runtime_dir / "versions" / "old"),
            "manifest_sha256": "a" * 64,
            "archive_sha256": "b" * 64,
            "bin_path": "bin/python",
            "provider_root_format": "everos-1.0",
            "compatible_provider_root_formats": [],
            "artifact_fingerprint": "old-artifact",
        }
    )
    monkeypatch.setattr(manager, "resolve_python", lambda: Path(sys.executable))
    monkeypatch.setattr(manager, "status", lambda: {"reason": None})

    factory = FakeEverOSProcessFactory()
    instances = factory.supervised

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    runtime = memory_runtime_factory(
        initial,
        store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
        artifact_manager=manager,
        process_factory=factory,
    )
    assert (await runtime.reconcile(initial))["ok"] is True

    await asyncio.to_thread(
        manager._write_current_pointer,
        manager.runtime_dir / "versions" / "candidate",
        _artifact_manifest("everos-2.0", compatible_formats=[]),
        _artifact_archive(),
    )

    assert manager.provider_root_format() == "everos-2.0"
    assert len(instances) == 2
    assert instances[0].stopped is True
    assert instances[1].stopped is False
    assert runtime.module._worker._claims_paused is False
    await memory_runtime_factory.close(runtime)


async def test_runtime_reconciliation_restarts_sidecar_with_fresh_child_settings(
    monkeypatch,
    memory_runtime_factory,
) -> None:
    instances: list[object] = []

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    factory = FakeEverOSProcessFactory()
    instances = factory.supervised

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)

    runtime = memory_runtime_factory(initial, artifact_manager=_Artifact(), process_factory=factory)
    assert (await runtime.reconcile(initial))["ok"] is True
    updated = replace(
        initial,
        processing=replace(
            processing,
            llm=replace(processing.llm, model="chat-v2"),
        ),
    )
    assert (await runtime.reconcile(updated))["ok"] is True
    assert len(instances) == 2
    assert instances[0].stopped is True
    assert runtime.module._worker._provider is runtime._provider
    await memory_runtime_factory.close(runtime)


async def test_runtime_preflight_failure_keeps_existing_sidecar_running(
    monkeypatch,
    memory_runtime_factory,
) -> None:
    instances: list[object] = []

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    class _Process(FakeEverOSProcess):
        """Report health from the child settings this sidecar was launched with."""

        async def processing_healthy(self) -> bool:
            return self.settings is not None and self.settings.llm_model != "unhealthy"

    factory = FakeEverOSProcessFactory(template=_Process)
    instances = factory.supervised

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)

    runtime = memory_runtime_factory(initial, artifact_manager=_Artifact(), process_factory=factory)
    assert (await runtime.reconcile(initial))["ok"] is True
    rejected = replace(
        initial,
        processing=replace(processing, llm=replace(processing.llm, model="unhealthy")),
    )
    assert await runtime.reconcile(rejected) == {"ok": False, "error": "memory_processing_failed"}
    assert len(instances) == 1
    assert instances[0].stopped is False
    assert runtime._config is initial
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_preflight_failure_keeps_candidate_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    active = MemoryConfig(enabled=True, processing=_processing_config())
    candidate = replace(
        active,
        processing=replace(
            active.processing,
            embedding=replace(active.processing.embedding, model="embed-v2"),
        ),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        active,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old

    async def reject_preflight(_config: MemoryConfig | None = None) -> dict[str, object]:
        return {"ok": False, "error": "memory_embedding_unavailable"}

    monkeypatch.setattr(runtime, "preflight", reject_preflight)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_embedding_unavailable",
        "result": "failed",
    }
    assert runtime._config == candidate
    assert runtime._restart_config == candidate
    assert runtime._process is old
    assert runtime.module._worker._claims_paused is True
    assert runtime._insight_reader is not None
    assert runtime._insight_reader._provider_base_urls == (
        active.processing.llm.base_url,
        active.processing.embedding.base_url,
    )
    assert V2Config.load().memory == candidate

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_embedding_unavailable",
        "result": "failed",
    }
    assert runtime._insight_reader is not None
    assert runtime._insight_reader._provider_base_urls == (
        active.processing.llm.base_url,
        active.processing.embedding.base_url,
    )
    await memory_runtime_factory.close(runtime)


async def test_runtime_passes_call_log_only_to_the_supervised_recorder_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(config))["ok"] is True

    assert len(factory.created) == 2
    probe, supervised = factory.created
    assert probe.settings.call_log_db_path is None
    assert supervised.settings.call_log_db_path == (
        tmp_path / "memory" / "call-log" / "call-log.db"
    )
    await memory_runtime_factory.close(runtime)


async def test_legacy_disabled_diagnostics_still_records_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=False),
    )

    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(config))["ok"] is True
    expected = tmp_path / "memory" / "call-log" / "call-log.db"
    assert factory.supervised[-1].settings.call_log_db_path == expected

    assert await runtime.restart() == {"ok": True, "state": "ready"}
    assert factory.supervised[-1].settings.call_log_db_path == expected
    await memory_runtime_factory.close(runtime)


async def test_status_preserves_everos_disabled_recorder_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(config))["ok"] is True

    snapshot = ProviderHealthSnapshot(
        status="ok",
        version="1.2.3",
        capabilities={
            "llm": True,
            "embed": True,
            "rerank": True,
            "multimodal_llm": True,
            "parser": True,
        },
        disabled_features=(),
        cascade=None,
        recorder={"state": "disabled", "reason": None},
    )

    async def health_snapshot() -> ProviderHealthSnapshot:
        return snapshot

    monkeypatch.setattr(runtime._provider, "health_snapshot", health_snapshot)
    assert (await runtime.status_payload())["health"]["recorder"] == {
        "state": "disabled",
        "reason": None,
    }
    await memory_runtime_factory.close(runtime)


async def test_recorder_reap_hands_call_log_to_host_until_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    """A crashed recorder has no DB-owner overlap with its supervised restart."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    maintenance_entered = threading.Event()
    maintenance_release = threading.Event()
    maintenance_finished = threading.Event()

    def maintain(path: Path) -> None:
        assert path == db_path
        maintenance_entered.set()
        maintenance_release.wait(timeout=2)
        maintenance_finished.set()
        return None

    monkeypatch.setattr(memory_runtime, "maintain_call_log", maintain)
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(config))["ok"] is True
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    recorder = factory.supervised[0]
    assert recorder.on_reaped is not None

    # This is the supervisor's post-reap notification at the beginning of
    # its crash-backoff window, before it schedules the restart attempt.
    recorder._running = False
    result = recorder.on_reaped()
    if inspect.isawaitable(result):
        await result
    assert await asyncio.to_thread(maintenance_entered.wait, 1)
    assert runtime._process_records_calls is False

    restarting = asyncio.create_task(recorder.start())
    await asyncio.sleep(0)
    assert not restarting.done()
    assert not maintenance_finished.is_set()

    maintenance_release.set()
    assert await asyncio.wait_for(restarting, timeout=1)
    assert maintenance_finished.is_set()
    async with asyncio.timeout(1):
        while not runtime._process_records_calls:
            await asyncio.sleep(0)
    assert runtime._process_records_calls is True
    assert runtime._call_log_retention_task is None
    await memory_runtime_factory.close(runtime)


async def test_stale_recorder_supervisor_cannot_release_host_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(config))["ok"] is True
    stale = factory.supervised[0]
    assert stale.before_start is not None
    runtime._process = FakeEverOSProcess()
    with pytest.raises(RuntimeError, match="stale EverOS recorder supervisor"):
        await stale.before_start()
    await memory_runtime_factory.close(runtime)


async def test_disabled_runtime_maintains_retained_call_log_and_reports_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    maintained = threading.Event()
    release_maintenance = threading.Event()
    maintenance_calls = 0

    def maintain(path: Path) -> str:
        nonlocal maintenance_calls
        assert path == db_path
        maintenance_calls += 1
        maintained.set()
        assert release_maintenance.wait(timeout=2)
        return "call_log_corrupt"

    monkeypatch.setattr(memory_runtime, "maintain_call_log", maintain)

    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    assert await runtime.reconcile(MemoryConfig()) == {
        "ok": True,
        "state": "disabled",
    }
    assert await asyncio.to_thread(maintained.wait, 1)
    task = runtime._call_log_retention_task
    assert task is not None
    assert not task.done()
    release_maintenance.set()
    for _ in range(20):
        if runtime._recorder_health["reason"] == "call_log_corrupt":
            break
        await asyncio.sleep(0)
    payload = await runtime.status_payload()
    assert payload["health"] is None
    assert payload["source"]["reason"] == "memory_disabled"
    await asyncio.wait_for(asyncio.shield(task), timeout=1)
    assert maintenance_calls == 1
    assert runtime._call_log_retention_task is None
    runtime._ensure_call_log_retention()
    assert runtime._call_log_retention_task is None
    assert maintenance_calls == 1
    await memory_runtime_factory.close(runtime)
    assert task.done()


async def test_runtime_close_waits_for_active_call_log_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    entered = threading.Event()
    release = threading.Event()

    def maintain(_path: Path) -> None:
        entered.set()
        release.wait(timeout=2)
        return None

    monkeypatch.setattr(memory_runtime, "maintain_call_log", maintain)

    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    await runtime.reconcile(MemoryConfig())
    assert await asyncio.to_thread(entered.wait, 1)

    retention_stop_entered = asyncio.Event()
    original_stop_retention = runtime._stop_call_log_retention

    async def observed_stop_retention() -> None:
        retention_stop_entered.set()
        await original_stop_retention()

    monkeypatch.setattr(runtime, "_stop_call_log_retention", observed_stop_retention)
    closing = asyncio.create_task(memory_runtime_factory.close(runtime))
    close_results: list[object] = []
    try:
        await asyncio.wait_for(retention_stop_entered.wait(), timeout=1.0)
        assert not closing.done()
    finally:
        release.set()
        close_results.extend(
            await asyncio.gather(closing, return_exceptions=True)
        )
    assert close_results == [None]


async def test_clear_quiesce_does_not_delete_call_log_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    unexpected = db_path.parent / "keep.txt"
    unexpected.write_text("keep", encoding="utf-8")
    os.chmod(unexpected, 0o600)

    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    await runtime._stop_sidecar_for_clear()

    assert db_path.exists()
    assert unexpected.read_text(encoding="utf-8") == "keep"
    assert runtime._recorder_health == {"state": "disabled", "reason": None}
    await memory_runtime_factory.close(runtime)




def _pid_exists(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _artifact_manifest(provider_root_format: str, *, compatible_formats: list[str]) -> ManagedRuntimeManifest:
    return ManagedRuntimeManifest(
        schema_version=1,
        runtime_version="1.2.3",
        source="test",
        source_url=None,
        archives={},
        digest="c" * 64,
        loaded_from="test",
        payload={
            "provider_root_format": provider_root_format,
            "compatible_provider_root_formats": compatible_formats,
        },
    )


def _artifact_archive() -> ManagedRuntimeArchive:
    return ManagedRuntimeArchive(
        platform="darwin-arm64",
        name="memory-runtime.tar.gz",
        url="file:///memory-runtime.tar.gz",
        sha256="d" * 64,
        binary_sha256="e" * 64,
        size=1,
        bin_path="bin/python",
    )


async def test_profile_payload_reports_only_its_own_principal_emptiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two principals reading concurrently must not decide each other's warning.

    The warning used to live on the shared ``EverOSPort``: whichever profile
    read finished last overwrote it, and ``profile_payload`` sampled that field
    after its own await. An authenticated remote or IM read landing between the
    two could tell the local UI its profile was empty, or hide that it was.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = create_memory_runtime(MemoryConfig(enabled=True), artifact_manager=_installed_artifact())

    empty_principal = "u-" + "a" * 32
    populated_principal = "u-" + "b" * 32
    released = asyncio.Event()

    class _InterleavingModule:
        async def profile(self, *, principal_id: str, project_id: str) -> MemoryItems:
            assert project_id == PROJECT
            if principal_id == populated_principal:
                # Finish only after the other read has entered its own await, so
                # a shared last-writer-wins field would be this call's value.
                await released.wait()
                return MemoryItems(items=(MemoryItem(kind="profile", text="{}"),))
            released.set()
            return MemoryItems(items=())

    runtime._module = _InterleavingModule()

    populated, empty = await asyncio.gather(
        runtime.profile_payload(populated_principal, PROJECT),
        runtime.profile_payload(empty_principal, PROJECT),
    )

    assert populated["profile_warning"] is None
    assert empty["profile_warning"] == "empty"


def test_profile_payload_serializes_structured_profile_without_widening_legacy_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = create_memory_runtime(MemoryConfig(enabled=True), artifact_manager=_installed_artifact())
    structured = MemoryItem(
        kind="profile",
        text="{}",
        profile=MemoryProfile(
            summary="Concise updates.",
            explicit_info=(MemoryProfileExplicitInfo(description="Uses Python."),),
        ),
    )

    class _ProfileModule:
        async def profile(self, *, principal_id: str, project_id: str) -> MemoryItems:
            del principal_id, project_id
            return MemoryItems(items=(structured, MemoryItem(kind="fact", text="Legacy")))

    runtime._module = _ProfileModule()
    payload = asyncio.run(runtime.profile_payload("u-" + "a" * 32, PROJECT))

    assert payload["items"] == [
        {
            "kind": "profile",
            "text": "{}",
            "date": None,
            "profile": {
                "summary": "Concise updates.",
                "explicit_info": [
                    {"description": "Uses Python.", "category": None, "evidence": None}
                ],
                "implicit_traits": [],
                "updated_at": None,
            },
        },
        {"kind": "fact", "text": "Legacy", "date": None},
    ]


def test_status_payload_carries_no_principal_scoped_profile_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = create_memory_runtime(MemoryConfig(enabled=True), artifact_manager=_installed_artifact())

    payload = asyncio.run(runtime.status_payload())

    # Status is not scoped to a principal, so it must not expose a field whose
    # only possible value is some other principal's last profile read.
    assert "profile_warning" not in payload


async def test_maintenance_data_exists_ignores_an_empty_diagnostic_call_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = memory_runtime_factory(MemoryConfig(), effective_home=tmp_path)
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)

    assert (await runtime.maintenance_payload())["data_exists"] is False
    assert "data_exists" not in await runtime.status_payload()
    await memory_runtime_factory.close(runtime)


def test_runtime_builds_insight_reader_from_injected_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite")
    config = MemoryConfig(
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                base_url="https://llm.example.test/v1",
                api_key="opaque-llm-key",
            ),
            embedding=MemoryEndpointConfig(
                base_url="https://embed.example.test/v1",
                api_key="opaque-embedding-key",
            ),
        )
    )

    runtime = MemoryRuntime(config, store=store, effective_home=tmp_path)

    assert runtime._insight_reader is not None
    assert runtime._insight_reader._paths.everos_root == tmp_path / "memory" / "everos-root"
    assert runtime._insight_reader._paths.capture_db_path == store.path
    assert runtime._insight_reader._paths.call_log_db_path == (
        tmp_path / "memory" / "call-log" / "call-log.db"
    )
    assert runtime._insight_reader._provider_base_urls == (
        "https://llm.example.test/v1",
        "https://embed.example.test/v1",
    )
    assert set(runtime._insight_reader._exact_redaction_values) == {
        "opaque-llm-key",
        "opaque-embedding-key",
    }


async def test_cancelled_insight_read_keeps_lifecycle_lock_until_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    principal_id = "u-11111111111111111111111111111111"
    started = threading.Event()
    release = threading.Event()

    class BlockingReader:
        def list_entries(self, scope, cursor, limit):
            assert scope == (principal_id, PROJECT)
            assert cursor == "cursor"
            assert limit == 7
            started.set()
            assert release.wait(2)
            return {"status": "ok", "entries": [], "next_cursor": None}

    runtime = memory_runtime_factory(
        MemoryConfig(),
        store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
        effective_home=tmp_path,
        insight_reader=BlockingReader(),
    )

    reading = asyncio.create_task(
        runtime.log_entries_payload(principal_id, PROJECT, "cursor", 7)
    )
    assert await asyncio.to_thread(started.wait, 2)
    reading.cancel()
    await asyncio.sleep(0)
    assert runtime.module._lifecycle_lock.locked()
    acquired = asyncio.Event()

    async def wait_for_lifecycle() -> None:
        async with runtime.module._lifecycle_lock:
            acquired.set()

    waiter = asyncio.create_task(wait_for_lifecycle())
    await asyncio.sleep(0)
    assert not acquired.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await asyncio.wait_for(acquired.wait(), timeout=1)
    await waiter
    await memory_runtime_factory.close(runtime)


def test_runtime_forwards_scoped_insight_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    principal_id = "u-11111111111111111111111111111111"
    calls: list[tuple[tuple[str, str], str]] = []

    class Reader:
        def entry_detail(self, scope, memcell_id):
            calls.append((scope, memcell_id))
            return {"status": "ok", "entry": {"memcell_id": memcell_id}}

    runtime = MemoryRuntime(
        MemoryConfig(),
        store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
        effective_home=tmp_path,
        insight_reader=Reader(),
    )

    payload = asyncio.run(runtime.log_entry_payload(principal_id, PROJECT, "mc_1"))

    assert payload == {"status": "ok", "entry": {"memcell_id": "mc_1"}}
    assert calls == [((principal_id, PROJECT), "mc_1")]


def test_runtime_forwards_admin_insight_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    calls: list[tuple[object, ...]] = []

    class Reader:
        def list_admin_entries(self, cursor, limit):
            calls.append(("list", cursor, limit))
            return {"status": "ok", "entries": [], "next_cursor": None}

        def admin_entry_detail(self, memcell_id):
            calls.append(("detail", memcell_id))
            return {"status": "ok", "entry": {"memcell_id": memcell_id}}

    runtime = MemoryRuntime(
        MemoryConfig(),
        store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
        effective_home=tmp_path,
        insight_reader=Reader(),
    )

    listed = asyncio.run(runtime.admin_log_entries_payload("cursor", 7))
    detail = asyncio.run(runtime.admin_log_entry_payload("mc_1"))

    assert listed == {"status": "ok", "entries": [], "next_cursor": None}
    assert detail == {"status": "ok", "entry": {"memcell_id": "mc_1"}}
    assert calls == [("list", "cursor", 7), ("detail", "mc_1")]


def _processing_config() -> MemoryProcessingConfig:
    return MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-secret"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embedding-secret"),
    )


async def test_runtime_restart_replaces_process_without_processing_preflight(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess()
    runtime._process = old

    assert await runtime.restart() == {"ok": True, "state": "ready"}
    assert old.stops == 1
    assert len(factory.created) == 1
    assert factory.supervised[0].starts == 1
    await memory_runtime_factory.close(runtime)


async def test_runtime_restart_preserves_admitted_recorder_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    runtime._process = FakeEverOSProcess()

    async def disabled_recorder_health() -> dict[str, str | None]:
        return {"state": "disabled", "reason": "writer_failures"}

    monkeypatch.setattr(
        runtime._sidecar,
        "_read_recorder_health",
        disabled_recorder_health,
    )

    assert await runtime.restart() == {"ok": True, "state": "ready"}
    assert runtime._recorder_health == {
        "state": "disabled",
        "reason": "writer_failures",
    }
    assert runtime._sidecar.snapshot().records_calls is False
    await memory_runtime_factory.close(runtime)


async def test_runtime_discards_recorder_health_from_prior_launch_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(enabled=True, processing=_processing_config())
    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    assert (await runtime.reconcile(config))["ok"] is True
    supervised = factory.supervised[0]
    assert supervised.before_start is not None
    health = ProviderHealthSnapshot(
        status="ok",
        version="1.2.3",
        capabilities={},
        disabled_features=(),
        cascade=None,
        recorder={"state": "disabled", "reason": "writer_failures"},
    )

    async def stale_health() -> ProviderHealthSnapshot:
        entered.set()
        await release.wait()
        return health

    monkeypatch.setattr(runtime._provider, "health_snapshot", stale_health)
    observation = asyncio.create_task(runtime._processing_record_health(None))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    await supervised.before_start()
    release.set()
    result = await observation

    assert result.snapshot is None
    assert result.unavailable_reason == "memory_sidecar_unavailable"
    assert runtime._process_records_calls is True
    assert runtime._call_log_retention_task is None
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("force_pending_assertion_failure", [False, True])
async def test_runtime_restart_is_retained_when_one_caller_is_cancelled(
    tmp_path: Path,
    memory_runtime_factory,
    force_pending_assertion_failure: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingStop(FakeEverOSProcess):
        async def stop(self) -> None:
            self.stops += 1
            entered.set()
            await release.wait()
            self.stopped = True
            self._running = False

    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = BlockingStop()
    runtime._process = old

    detached = asyncio.create_task(runtime.restart())
    await entered.wait()
    detached.cancel()
    with pytest.raises(asyncio.CancelledError):
        await detached

    joined = asyncio.create_task(runtime.restart())
    restart_results: list[object] = []

    async def verify_joined_restart_is_retained() -> None:
        try:
            await asyncio.sleep(0)
            joined_done = joined.done()
            if force_pending_assertion_failure:
                joined_done = True
            assert joined_done is False
        finally:
            release.set()
            restart_results.extend(
                await asyncio.gather(
                    detached,
                    joined,
                    return_exceptions=True,
                )
            )

    if force_pending_assertion_failure:
        with pytest.raises(AssertionError):
            await verify_joined_restart_is_retained()
    else:
        await verify_joined_restart_is_retained()

    assert isinstance(restart_results[0], asyncio.CancelledError)
    assert restart_results[1] == {"ok": True, "state": "ready"}
    assert old.stops == 1
    assert len(factory.created) == 1
    await memory_runtime_factory.close(runtime)


async def test_stop_worker_supports_python_310_task_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )

    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    runtime._worker_task = task
    await started.wait()

    class Python310Task:
        """Python 3.10 tasks do not expose ``Task.cancelling()``."""

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(memory_runtime.asyncio, "current_task", lambda: Python310Task())
            await runtime._stop_worker()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert runtime._worker_task is None
    await memory_runtime_factory.close(runtime)


async def test_stop_worker_settles_worker_before_propagating_caller_cancellation(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )

    started = asyncio.Event()
    cancellation_started = asyncio.Event()
    release_cancellation = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await release_cancellation.wait()
            raise

    worker_task = asyncio.create_task(worker())
    runtime._worker_task = worker_task
    await started.wait()

    stopping = asyncio.create_task(runtime._stop_worker())
    try:
        await asyncio.wait_for(cancellation_started.wait(), timeout=1.0)
        stopping.cancel()
        await asyncio.sleep(0)

        assert stopping.done() is False
        assert runtime._worker_task is worker_task
        release_cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    finally:
        release_cancellation.set()
        if not stopping.done():
            await asyncio.gather(stopping, return_exceptions=True)

    assert worker_task.cancelled()
    assert runtime._worker_task is None
    await memory_runtime_factory.close(runtime)


async def test_stop_worker_propagates_worker_cleanup_failure(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )

    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("worker cleanup failed")

    worker_task = asyncio.create_task(worker())
    runtime._worker_task = worker_task
    await started.wait()

    with pytest.raises(RuntimeError, match="worker cleanup failed"):
        await runtime._stop_worker()

    assert runtime._worker_task is None
    await memory_runtime_factory.close(runtime)


async def test_restart_aborts_when_worker_cannot_pause_before_process_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess()
    runtime._process = old
    old_provider = runtime._provider
    store = runtime._store
    assert store is not None
    worker = runtime.module._worker
    old_lease = worker._boot_id
    accepted = store.enqueue_request(
        source_message_id="old-lease-row",
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="recover this row",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert accepted.row is not None
    claimed = store.claim_due(
        lease_owner=old_lease,
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None
    recover_after_boot = store.recover_after_boot

    def blocking_recovery(*, lease_owner: str, clock):
        entered.set()
        release.wait(timeout=2.0)
        return recover_after_boot(lease_owner=lease_owner, clock=clock)

    monkeypatch.setattr(store, "recover_after_boot", blocking_recovery)

    async def no_grace_wait(*, timeout_seconds: float) -> bool:
        del timeout_seconds
        worker.pause_claims()
        return False

    monkeypatch.setattr(worker, "pause_and_wait", no_grace_wait)
    monkeypatch.setattr(runtime, "_ensure_worker", lambda: None)

    runtime._worker_task = asyncio.create_task(worker.drain_once())
    assert await asyncio.to_thread(entered.wait, 1.0)
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_restart_failed",
    }
    assert old.stops == 0
    assert runtime._process is old
    assert runtime._provider is old_provider
    assert factory.created == []
    assert worker._claims_paused is False

    release.set()
    assert await runtime._worker_task == 0
    assert old.stops == 0
    assert factory.created == []
    assert worker._boot_id == old_lease
    assert store.list_queue_rows()[0].state == "processing"
    await memory_runtime_factory.close(runtime)


async def test_runtime_restart_preserves_drain_completed_inside_grace_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    add_entered = asyncio.Event()
    release_add = asyncio.Event()
    pause_timeouts: list[float] = []

    async def block_add(_capture: ProviderCapture) -> None:
        add_entered.set()
        await release_add.wait()

    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess()
    runtime._process = old
    provider = FakeMemoryProvider(add_hook=block_add)
    runtime._provider = provider
    runtime.module.replace_provider(provider)
    store = runtime._store
    assert store is not None
    accepted = store.enqueue_request(
        source_message_id="graceful-row",
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="finish before replacement",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert accepted.row is not None
    worker = runtime.module._worker
    pause_and_wait = worker.pause_and_wait

    async def tracked_pause(*, timeout_seconds: float) -> bool:
        pause_timeouts.append(timeout_seconds)
        return await pause_and_wait(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(worker, "pause_and_wait", tracked_pause)

    runtime._worker_task = asyncio.create_task(worker.drain_once())
    await asyncio.wait_for(add_entered.wait(), timeout=1.0)
    restarting = asyncio.create_task(runtime.restart())
    restart_results: list[object] = []
    try:
        await asyncio.sleep(0)
        assert old.stops == 0
    finally:
        release_add.set()
        restart_results.extend(
            await asyncio.gather(restarting, return_exceptions=True)
        )

    assert restart_results == [{"ok": True, "state": "ready"}]
    assert pause_timeouts == [5.0]
    assert old.stops == 1
    row = store.list_queue_rows()[0]
    assert row.state == "delivered"
    assert row.payload_text is None
    await memory_runtime_factory.close(runtime)


async def test_runtime_restart_returns_closed_errors_when_not_eligible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    disabled_home = tmp_path / "disabled"
    monkeypatch.setenv("AVIBE_HOME", str(disabled_home))
    disabled = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=_installed_artifact(),
        effective_home=disabled_home,
    )

    def unavailable_store(*_args, **_kwargs):
        raise OSError("store unavailable")

    monkeypatch.setattr(memory_runtime, "MemoryStore", unavailable_store)
    unavailable = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path / "unavailable",
    )

    assert await disabled.restart() == {
        "ok": False,
        "error": "memory_disabled",
    }
    assert await unavailable.restart() == {
        "ok": False,
        "error": "memory_store_unavailable",
    }
    await memory_runtime_factory.close(disabled)
    await memory_runtime_factory.close(unavailable)


async def test_runtime_restart_replays_last_success_after_failed_candidate(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    class ConfigAwareProcess(FakeEverOSProcess):
        async def processing_healthy(self) -> bool:
            return self.settings is not None and self.settings.llm_model != "rejected"

    factory = FakeEverOSProcessFactory(template=ConfigAwareProcess)
    applied = MemoryConfig(enabled=True, processing=_processing_config())
    rejected = replace(
        applied,
        processing=replace(
            applied.processing,
            llm=replace(applied.processing.llm, model="rejected"),
        ),
    )
    runtime = memory_runtime_factory(
        applied,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )

    assert await runtime.reconcile(applied) == {"ok": True, "state": "ready"}
    assert await runtime.reconcile(rejected) == {
        "ok": False,
        "error": "memory_processing_failed",
    }
    assert await runtime.restart() == {"ok": True, "state": "ready"}
    assert factory.supervised[-1].settings is not None
    assert factory.supervised[-1].settings.llm_model == "chat"
    await memory_runtime_factory.close(runtime)


async def test_rebuild_settlement_survives_candidate_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    startup = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    candidate = replace(
        startup,
        processing=replace(
            startup.processing,
            llm=replace(startup.processing.llm, model="rejected"),
            embedding=replace(startup.processing.embedding, model="embed-v2"),
        ),
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    factory = FakeEverOSProcessFactory(
        template=lambda: FakeEverOSProcess(start_results=deque((False,)))
    )
    runtime = memory_runtime_factory(
        startup,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)
    monkeypatch.setattr(
        runtime,
        "preflight",
        lambda config=None: asyncio.sleep(0, result={"ok": True}),
    )

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_sidecar_unavailable",
        "result": "completed_empty",
    }
    settled = V2Config.load().memory
    assert settled.recovery_intent is None
    assert settled.processing.llm.model == "rejected"
    assert runtime._restart_config == settled
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)


async def test_runtime_restart_stop_failure_retains_old_process_and_paused_claims(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(stop_failure=RuntimeError("still owned"))
    runtime._process = old

    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_restart_failed",
    }
    assert runtime._process is old
    assert factory.created == []
    assert runtime.module._worker._claims_paused is True
    old.stop_failure = None
    await memory_runtime_factory.close(runtime)


async def test_runtime_restart_fails_closed_before_launch_for_marker_or_clear_recovery(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    marked_factory = FakeEverOSProcessFactory()
    marked = memory_runtime_factory(
        MemoryConfig(
            enabled=True,
            processing=_processing_config(),
            recovery_intent="rebuild",
        ),
        artifact_manager=_installed_artifact(),
        process_factory=marked_factory,
        effective_home=tmp_path / "marked",
    )
    recovery_factory = FakeEverOSProcessFactory()
    recovering = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=recovery_factory,
        effective_home=tmp_path / "recovery",
    )

    meta = recovering._store.ensure_meta()
    _maintenance(recovering)._clear_journal.start(
        operation_id="restart-recovery",
        operator_ref="user:owner",
        pre_epoch=meta.epoch,
        target_epoch=meta.epoch + 1,
    )

    assert await marked.restart() == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    assert await recovering.restart() == {
        "ok": False,
        "error": "memory_clear_failed",
    }
    assert marked_factory.created == []
    assert recovery_factory.created == []
    await memory_runtime_factory.close(marked)
    await memory_runtime_factory.close(recovering)


async def test_ready_callback_activates_only_the_current_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    starts = deque((False, True))

    def process_template() -> FakeEverOSProcess:
        return FakeEverOSProcess(start_results=deque((starts.popleft(),)))

    factory = FakeEverOSProcessFactory(template=process_template)
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    activations: list[None] = []
    activated = asyncio.Event()

    def record_activation() -> None:
        activations.append(None)
        activated.set()

    monkeypatch.setattr(runtime, "_ensure_worker", record_activation)

    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_sidecar_unavailable",
    }
    recovering = factory.supervised[0]
    recovering._running = True
    await recovering.ready()
    await asyncio.wait_for(activated.wait(), timeout=1.0)
    assert len(activations) == 1
    assert runtime.module._worker._claims_paused is False

    activated.clear()
    assert await runtime.restart() == {"ok": True, "state": "ready"}
    assert len(activations) == 2
    recovering._running = True
    await recovering.ready()
    await asyncio.sleep(0.05)
    assert len(activations) == 2
    await memory_runtime_factory.close(runtime)


async def test_ready_callback_survives_rejected_artifact_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    config = MemoryConfig(enabled=True, processing=_processing_config())
    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    process = FakeEverOSProcess()
    runtime._process = process
    activated = asyncio.Event()
    monkeypatch.setattr(runtime, "_ensure_worker", activated.set)

    runtime._activation_loop = asyncio.get_running_loop()
    process.on_ready = lambda: runtime._schedule_sidecar_ready(process)

    await process.ready()
    assert await runtime.install_artifact() == {
        "ok": False,
        "reason": "memory_runtime_install_requires_disabled_memory",
        "download_error": None,
    }

    await asyncio.wait_for(activated.wait(), timeout=1.0)
    assert runtime.module._worker._claims_paused is False
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("lifecycle", ["clear", "reconcile", "artifact"])
async def test_ready_callback_waits_for_runtime_lifecycle_and_revalidates_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lifecycle: str,
    memory_runtime_factory,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    config = MemoryConfig(enabled=True, processing=_processing_config())
    runtime = memory_runtime_factory(
        config,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path / lifecycle,
    )
    process = FakeEverOSProcess()
    runtime._process = process
    activations: list[None] = []
    monkeypatch.setattr(runtime, "_ensure_worker", lambda: activations.append(None))

    runtime._activation_loop = asyncio.get_running_loop()
    process.on_ready = lambda: runtime._schedule_sidecar_ready(process)

    if lifecycle == "clear":
        async def blocked_prepare(_operation, **_kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("injected clear pause")

        monkeypatch.setattr(_maintenance(runtime), "_prepare_clear", blocked_prepare)
        operation = asyncio.create_task(runtime.clear(operator_ref="user:owner"))
    elif lifecycle == "reconcile":
        async def blocked_reconcile(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"ok": False, "error": "memory_processing_failed"}

        monkeypatch.setattr(runtime, "_reconcile_locked", blocked_reconcile)
        operation = asyncio.create_task(runtime.reconcile(config))
    else:
        worker = runtime.module._worker

        async def blocked_pause(**_kwargs):
            worker.pause_claims()
            entered.set()
            await release.wait()
            return True

        async def accepted_reconcile(*_args, **_kwargs):
            return {"ok": True, "state": "ready"}

        monkeypatch.setattr(worker, "pause_and_wait", blocked_pause)
        monkeypatch.setattr(runtime, "_reconcile_locked", accepted_reconcile)
        operation = asyncio.create_task(
            runtime._activate_artifact_candidate(
                MemoryArtifactCandidate(
                    provider_root_format="everos-test-next",
                    compatible_provider_root_formats=frozenset(),
                    artifact_fingerprint="next-artifact",
                ),
                MemoryProviderRootState(exists=False),
                lambda: None,
                lambda: None,
            )
        )

    await entered.wait()
    await process.ready()
    await asyncio.sleep(0)
    assert activations == []
    release.set()
    await operation
    ready_task = runtime._ready_activation_task
    if ready_task is not None:
        await asyncio.wait_for(asyncio.shield(ready_task), timeout=1.0)
    assert activations == ([] if lifecycle in {"clear", "artifact"} else [None])
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("unexpected_activation", [False, True])
async def test_runtime_close_rejects_ready_callback_during_process_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
    unexpected_activation: bool,
) -> None:
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class BlockingStop(FakeEverOSProcess):
        async def stop(self) -> None:
            self.stops += 1
            stop_entered.set()
            await release_stop.wait()
            self.stopped = True
            self._running = False

    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    process = BlockingStop()
    runtime._process = process
    activations: list[None] = []
    monkeypatch.setattr(runtime, "_ensure_worker", lambda: activations.append(None))

    runtime._activation_loop = asyncio.get_running_loop()
    process.on_ready = lambda: runtime._schedule_sidecar_ready(process)
    closing = asyncio.create_task(memory_runtime_factory.close(runtime))
    close_results: list[object] = []

    async def verify_readiness_stays_closed() -> None:
        try:
            await asyncio.wait_for(stop_entered.wait(), timeout=1.0)
            await process.ready()
            await asyncio.sleep(0.01)
            if unexpected_activation:
                activations.append(None)
            assert activations == []
        finally:
            release_stop.set()
            close_results.extend(
                await asyncio.gather(closing, return_exceptions=True)
            )

    if unexpected_activation:
        with pytest.raises(AssertionError):
            await verify_readiness_stays_closed()
    else:
        await verify_readiness_stays_closed()
        assert activations == []
    assert close_results == [None]
    assert closing.done()
    assert process.stops == 1


async def test_cancelled_artifact_install_owns_ensure_until_restart_can_enter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingArtifact(FakeMemoryArtifactManager):
        def ensure(self, *, force: bool = False) -> dict:
            self.ensure_calls.append(force)
            entered.set()
            release.wait(timeout=2.0)
            return dict(self.ensure_payload)

    artifact = BlockingArtifact(python=Path(sys.executable))
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=artifact,
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    activations: list[None] = []
    monkeypatch.setattr(runtime, "_ensure_worker", lambda: activations.append(None))
    installing = asyncio.create_task(runtime.install_artifact())
    assert await asyncio.to_thread(entered.wait, 1.0)
    competing = MemoryOperationLease(tmp_path)
    with pytest.raises(MemoryOperationBusy):
        competing.acquire()
    from core.controller import Controller

    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    assert await controller._factory_reset_memory_once() == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }
    assert runtime.retired is False
    candidate = FakeEverOSProcess()
    candidate.on_ready = lambda: runtime._schedule_sidecar_ready(candidate)
    runtime._process = candidate
    await candidate.ready()
    await asyncio.sleep(0)
    assert activations == []

    installing.cancel()
    await asyncio.sleep(0)
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    await asyncio.sleep(0)
    assert installing.done() is False
    installing.cancel()
    await asyncio.sleep(0)
    assert installing.done() is False
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await installing
    competing.acquire()
    competing.release()
    assert await runtime.restart() == {"ok": True, "state": "ready"}
    assert activations == [None]
    await memory_runtime_factory.close(runtime)


class _BlockingProbeProcess:
    """A sidecar fake whose processing probe never finishes on its own.

    Stands in for the real probe child, which can run for
    ``_PROCESSING_PROBE_TIMEOUT_SECONDS`` plus reaping. ``probe_calls`` is what
    makes single-flight observable without timing assumptions.
    """

    running = True
    starting = False

    def __init__(self, *, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release
        self.probe_calls = 0

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        return None

    async def processing_healthy(self) -> bool:
        self.probe_calls += 1
        self._entered.set()
        await self._release.wait()
        return True


async def test_drain_health_gate_never_waits_on_an_in_flight_reconcile_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A settings save must not be able to stall the drain loop's health gate.

    Both used to take one controller-wide probe lock, so a reconcile probe held
    it for the length of a child process while the drain gate — running inside
    the worker's drain lock — waited. That pushed a single drain tick past the
    five-second fence Clear and clear recovery use.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    reconcile_probe = _BlockingProbeProcess(entered=entered, release=release)
    supervised = FakeEverOSProcess()

    def factory(python, **kwargs):
        # The runtime builds the reconcile probe without a readiness callback.
        return supervised if kwargs.get("on_ready") is not None else reconcile_probe

    config = MemoryConfig(enabled=True, processing=_processing_config())
    runtime = MemoryRuntime(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    runtime._process = supervised

    probe = asyncio.create_task(runtime._probe_processing(Path(sys.executable), config))
    await entered.wait()
    assert await asyncio.wait_for(runtime._processing_healthy(), timeout=2.0) is True
    release.set()
    assert await probe is True


async def test_reconcile_probe_never_waits_on_an_in_flight_drain_health_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The other half of the former cycle: reconcile waited with no bound at all.

    ``_probe_processing`` acquired the shared lock while holding both the
    reconcile lock and the module lifecycle lock, so a drain-side probe blocked
    every status read, search, Clear and clear recovery behind it.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    supervised = _BlockingProbeProcess(entered=entered, release=release)
    reconcile_probe = FakeEverOSProcess()

    def factory(python, **kwargs):
        return reconcile_probe

    config = MemoryConfig(enabled=True, processing=_processing_config())
    runtime = MemoryRuntime(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    runtime._process = supervised

    gate = asyncio.create_task(runtime._processing_healthy())
    await entered.wait()
    probed = await asyncio.wait_for(
        runtime._probe_processing(Path(sys.executable), config),
        timeout=2.0,
    )
    assert probed is True
    release.set()
    assert await gate is True


async def test_drain_health_gate_reuses_the_last_verdict_instead_of_queueing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Single-flight is kept, but a second caller reads instead of waiting."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()
    blocking = _BlockingProbeProcess(entered=entered, release=release)
    config = MemoryConfig(enabled=True, processing=_processing_config())
    runtime = MemoryRuntime(
        config,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    runtime._process = FakeEverOSProcess()

    assert await runtime._processing_healthy() is True
    runtime._process = blocking
    first = asyncio.create_task(runtime._processing_healthy())
    await entered.wait()
    assert await asyncio.wait_for(runtime._processing_healthy(), timeout=2.0) is True
    # The second caller answered from the published verdict rather than
    # starting — or waiting for — a second child probe.
    assert blocking.probe_calls == 1
    release.set()
    assert await first is True


@pytest.mark.parametrize("changes_embedding", [False, True])
async def test_reconcile_releases_the_claim_fence_when_the_worker_pause_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changes_embedding: bool,
    memory_runtime_factory,
) -> None:
    """A failed settings save must never leave the drain loop fenced forever.

    ``pause_and_wait`` fences claims before it waits, so returning on its
    timeout without resuming left ``MemoryWorker.drain`` returning at its
    ``_claims_paused`` check — no claims, and no health probing — until the
    service restarted.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    worker = runtime.module._worker

    async def pause_and_wait(**_kwargs) -> bool:
        worker.pause_claims()
        return False

    monkeypatch.setattr(worker, "pause_and_wait", pause_and_wait)

    candidate_processing = processing
    if changes_embedding:
        candidate_processing = replace(
            processing,
            embedding=MemoryEndpointConfig("https://embed.example.test/v2", "embed", "embedding-secret"),
        )
    candidate = MemoryConfig(enabled=True, processing=candidate_processing)

    result = await runtime.reconcile(candidate)

    assert result == {"ok": False, "error": "memory_clear_failed"}
    assert worker._claims_paused is False
    await memory_runtime_factory.close(runtime)


_ORPHAN_PID = 424_242
_ORPHAN_DESCENDANT_PID = 424_243
_ORPHAN_GROUP_MEMBER_PID = 424_244
_ORPHAN_GROUP_HELPER_PID = 424_245
_FOREIGN_GROUP_PID = 424_246
_FOREIGN_UID_GROUP_PID = 424_247
_ORPHAN_CREATE_TIME = 1_700_000_000.5


async def test_runtime_effective_home_owns_the_attachment_pipeline(
    monkeypatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    global_home = tmp_path / "global-home"
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("AVIBE_HOME", str(runtime_home))
    store = MemoryStore()
    monkeypatch.setenv("AVIBE_HOME", str(global_home))
    source_root = runtime_home / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700)
    source = source_root / "notes.txt"
    source.write_bytes(b"runtime-owned attachment")
    source.chmod(0o600)

    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True),
        store=store,
        artifact_manager=_installed_artifact(),
        effective_home=runtime_home,
    )
    expected_pin_root = attachment_pin_root(runtime_home)
    assert runtime.module._effective_home == runtime_home
    assert runtime.module._attachment_store._effective_home == runtime_home
    assert runtime.module._attachment_store._root == expected_pin_root
    assert runtime.module._attachment_store._source_root == source_root
    manager = _maintenance(runtime)._snapshot_manager
    assert manager is not None
    assert manager.effective_home == runtime_home
    assert any(
        surface.path == "memory/attachments"
        for surface in manager.surfaces
    )

    process = EverOSProcess(
        sys.executable,
        effective_home=runtime_home,
        settings=_settings(),
    )
    assert process._child_environment()["AVIBE_MEMORY_ATTACHMENTS_ROOT"] == str(
        expected_pin_root
    )

    try:
        result = await runtime.module.capture(
            CaptureRequest(
                source_message_id="source-1",
                session_id="session-1",
                principal_id="u-11111111111111111111111111111111",
                project_id=PROJECT,
                provenance="user_input",
                text="remember the notes",
                occurred_at_ms=1_725_000_001_234,
                attachments=(
                    CaptureAttachment(
                        kind="doc",
                        name=source.name,
                        uri=source.as_uri(),
                        ext="txt",
                    ),
                ),
            )
        )
    finally:
        await memory_runtime_factory.close(runtime)
    assert isinstance(result, CaptureAccepted)
    pinned_files = tuple((expected_pin_root / "bundles").glob("*/*"))
    assert len(pinned_files) == 1
    assert pinned_files[0].read_bytes() == b"runtime-owned attachment"
    assert not attachment_pin_root(global_home).exists()


async def test_runtime_rebuild_validates_before_destructive_stop(
    monkeypatch,
    tmp_path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=True,
            processing=processing,
            recovery_intent="rebuild",
        ),
    ).save()
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        V2Config.load().memory,
        artifact_manager=_installed_artifact(
            python=None,
            status_payload={"reason": "missing"},
        ),
        process_factory=factory,
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old
    runtime.module.resume_claims()
    result = await runtime.rebuild()
    assert result["ok"] is False
    assert result["result"] == "failed"
    assert old.stopped is False
    assert factory.created == []
    assert V2Config.load().memory.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_durable_candidate_load_failure_never_uses_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old
    monkeypatch.setattr(
        V2Config,
        "load",
        classmethod(
            lambda cls, config_path=None: (_ for _ in ()).throw(
                ValueError("durable config unavailable")
            )
        ),
    )

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    assert old.stopped is False
    assert runtime._process is old
    assert runtime.module._worker._claims_paused is True
    assert runtime._restart_config.recovery_intent == "rebuild"
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("failure", [False, RuntimeError("quiesce failed")])
async def test_runtime_rebuild_quiesce_failure_does_not_stop_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
    failure: bool | RuntimeError,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old

    async def fail_quiesce(*_args, **_kwargs) -> bool:
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(runtime.module, "quiesce_claims", fail_quiesce)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    assert old.stopped is False
    assert runtime._process is old
    assert V2Config.load().memory.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize("preflight", ["indeterminate", "incomplete_endpoint"])
async def test_runtime_rebuild_data_preflight_fails_before_destructive_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
    preflight: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    persisted_candidate = MemoryConfig(
        enabled=True,
        processing=processing,
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=persisted_candidate,
    ).save()
    if preflight == "incomplete_endpoint":
        processing = replace(
            processing,
            embedding=replace(processing.embedding, api_key=None),
        )
    candidate = MemoryConfig(
        enabled=True,
        processing=processing,
        recovery_intent="rebuild",
    )
    if preflight == "incomplete_endpoint":
        monkeypatch.setattr(V2Config, "load", lambda: SimpleNamespace(memory=candidate))
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old

    def inspect_data() -> bool:
        if preflight == "indeterminate":
            raise RuntimeError("root state unavailable")
        return True

    monkeypatch.setattr(runtime, "_provider_data_exists_strict", inspect_data)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    assert old.stopped is False
    assert runtime._process is old
    assert V2Config.load().memory.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_probes_candidate_before_destructive_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)
    probe_calls: list[MemoryConfig] = []

    async def reject_candidate(_python: Path, config: MemoryConfig) -> bool:
        probe_calls.append(config)
        assert old.stopped is False
        return False

    monkeypatch.setattr(runtime, "_probe_processing", reject_candidate)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    assert probe_calls == [candidate]
    assert old.stopped is False
    assert runtime._process is old
    assert V2Config.load().memory.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_embedding_rebuild_required",
    }
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize(
    ("inspections", "expected_result", "expected_child_calls"),
    [
        ((False, True), "completed", 1),
        ((True, False), "completed_empty", 0),
    ],
)
async def test_runtime_rebuild_reinspects_after_proven_sidecar_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
    inspections: tuple[bool, bool],
    expected_result: str,
    expected_child_calls: int,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    old = FakeEverOSProcess(settings=_settings())
    runtime._process = old
    remaining = deque(inspections)
    stop_observations: list[bool] = []
    child_calls = 0

    def inspect_data() -> bool:
        stop_observations.append(old.stopped)
        return remaining.popleft()

    class _CompletedRebuild:
        def __init__(self, *_args, **_kwargs):
            nonlocal child_calls
            child_calls += 1

        async def run(self) -> RebuildProcessResult:
            return RebuildProcessResult.COMPLETED

    monkeypatch.setattr(runtime, "_provider_data_exists_strict", inspect_data)
    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _CompletedRebuild)

    assert await runtime.rebuild() == {
        "ok": True,
        "result": expected_result,
        "state": "ready",
    }
    assert stop_observations == [False, True]
    assert child_calls == expected_child_calls
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize(("enabled", "state"), [(True, "ready"), (False, "disabled")])
async def test_runtime_rebuild_empty_root_settles_without_child(
    monkeypatch,
    tmp_path,
    memory_runtime_factory,
    enabled: bool,
    state: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=enabled,
            processing=processing,
            recovery_intent="rebuild",
        ),
    ).save()
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        V2Config.load().memory,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    if not enabled:
        runtime._recorder_health = {"state": "degraded", "reason": "writer_failures"}
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False, raising=False)
    rebuild_calls = []

    class _NoChild:
        def __init__(self, *args, **kwargs):
            rebuild_calls.append(True)

        async def run(self):
            raise AssertionError("empty root must not launch cascade rebuild")

    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _NoChild)
    result = await runtime.rebuild()
    assert result == {"ok": True, "result": "completed_empty", "state": state}
    assert rebuild_calls == []
    assert len(factory.supervised) == int(enabled)
    if not enabled:
        assert runtime._recorder_health == {"state": "disabled", "reason": None}
    assert V2Config.load().memory.recovery_intent is None
    assert runtime._restart_config.recovery_intent is None
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_reads_key_correction_after_admission_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    from config.v2_config import memory_config_to_payload
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True)
    acquire_entered = threading.Event()
    allow_acquire = threading.Event()
    observed_keys: list[str | None] = []

    class _GatedLease(MemoryOperationLease):
        def acquire(self) -> None:
            acquire_entered.set()
            assert allow_acquire.wait(10)
            super().acquire()

    class _CompletedRebuild:
        def __init__(self, *_args, settings, **_kwargs):
            observed_keys.append(settings.embedding_api_key)

        async def run(self) -> RebuildProcessResult:
            return RebuildProcessResult.COMPLETED

    monkeypatch.setattr(memory_runtime, "MemoryOperationLease", _GatedLease)
    monkeypatch.setattr(memory_runtime, "EverOSRebuildProcess", _CompletedRebuild)
    rebuilding = asyncio.create_task(runtime.rebuild())
    assert await asyncio.to_thread(acquire_entered.wait, 10)

    current = V2Config.load().memory
    payload = memory_config_to_payload(current, include_secrets=True)
    payload["processing"]["embedding"]["api_key"] = "corrected-key"
    api.save_memory_config(
        payload,
        recovery_intent="rebuild",
        expected=current,
    )
    allow_acquire.set()

    assert await rebuilding == {
        "ok": True,
        "result": "completed",
        "state": "ready",
    }
    assert observed_keys == ["corrected-key"]
    assert runtime._restart_config.processing.embedding.api_key == "corrected-key"
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_maps_root_busy_without_settling(
    monkeypatch,
    tmp_path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=True,
            processing=processing,
            recovery_intent="rebuild",
        ),
    ).save()
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        V2Config.load().memory,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True, raising=False)

    class _BusyRebuild:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return RebuildProcessResult.ROOT_BUSY

    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _BusyRebuild)
    result = await runtime.rebuild()
    assert result == {"ok": False, "error": "memory_rebuild_root_busy", "result": "root_busy"}
    assert "factory" not in str(result).lower()
    assert V2Config.load().memory.recovery_intent == "rebuild"
    assert runtime._restart_config.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)


@pytest.mark.parametrize(
    ("child_result", "public_result"),
    [
        (RebuildProcessResult.INTERRUPTED, "interrupted"),
        (RebuildProcessResult.TIMED_OUT, "timed_out"),
        (RebuildProcessResult.FAILED, "failed"),
    ],
)
async def test_runtime_rebuild_maps_failed_child_without_settling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
    child_result: RebuildProcessResult,
    public_result: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True)

    class _FailedRebuild:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self) -> RebuildProcessResult:
            return child_result

    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _FailedRebuild)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": public_result,
    }
    assert V2Config.load().memory.recovery_intent == "rebuild"
    assert runtime._restart_config.recovery_intent == "rebuild"
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_rejects_stale_settlement_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True)

    class _SupersededRebuild:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self) -> RebuildProcessResult:
            atomic_update_memory(
                lambda current: replace(
                    current,
                    processing=replace(
                        current.processing,
                        embedding=replace(current.processing.embedding, model="embed-v3"),
                    ),
                )
            )
            return RebuildProcessResult.COMPLETED

    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _SupersededRebuild)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    persisted = V2Config.load().memory
    assert persisted.recovery_intent == "rebuild"
    assert persisted.processing.embedding.model == "embed-v3"
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)

async def test_runtime_rebuild_refreshes_restart_snapshot_before_activation(
    monkeypatch,
    tmp_path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=MemoryConfig(
            enabled=True,
            processing=processing,
            recovery_intent="rebuild",
        ),
    ).save()
    observed = []

    class _CompletedRebuild:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            return RebuildProcessResult.COMPLETED

    class _Process(FakeEverOSProcess):
        async def start(self) -> bool:
            observed.append(
                (
                    runtime._restart_config.recovery_intent,
                    V2Config.load().memory.recovery_intent,
                )
            )
            return await super().start()

    factory = FakeEverOSProcessFactory(template=_Process)
    settled_callbacks: list[MemoryConfig] = []

    def on_config_settled(settled: MemoryConfig) -> None:
        assert runtime._config == settled
        assert runtime._restart_config == settled
        settled_callbacks.append(settled)

    runtime = memory_runtime_factory(
        V2Config.load().memory,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
        on_config_settled=on_config_settled,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True, raising=False)
    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _CompletedRebuild)
    result = await runtime.rebuild()
    assert result == {"ok": True, "result": "completed", "state": "ready"}
    assert observed == [(None, None)]
    assert settled_callbacks == [V2Config.load().memory]
    assert runtime._restart_config.recovery_intent is None
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_callback_failure_blocks_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    factory = FakeEverOSProcessFactory()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=factory,
        effective_home=tmp_path,
        on_config_settled=lambda _settled: (_ for _ in ()).throw(
            RuntimeError("controller snapshot unavailable")
        ),
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_restart_failed",
        "result": "completed_empty",
    }
    assert V2Config.load().memory.recovery_intent is None
    assert runtime._restart_config.recovery_intent is None
    assert factory.supervised == []
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)


async def test_cancelled_rebuild_caller_still_refreshes_settled_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    child_entered = asyncio.Event()
    child_release = asyncio.Event()
    settled_seen = asyncio.Event()
    callbacks: list[MemoryConfig] = []

    class _BlockingRebuild:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self) -> RebuildProcessResult:
            child_entered.set()
            await child_release.wait()
            return RebuildProcessResult.COMPLETED

    def on_config_settled(settled: MemoryConfig) -> None:
        callbacks.append(settled)
        settled_seen.set()

    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
        on_config_settled=on_config_settled,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True)
    monkeypatch.setattr("core.memory.runtime.EverOSRebuildProcess", _BlockingRebuild)
    caller = asyncio.create_task(runtime.rebuild())
    await child_entered.wait()
    retained = runtime._rebuild_task
    assert retained is not None

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    child_release.set()
    await settled_seen.wait()

    assert await retained == {"ok": True, "result": "completed", "state": "ready"}
    assert callbacks == [V2Config.load().memory]
    assert callbacks[0].recovery_intent is None
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_keeps_completed_result_when_root_activation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        recovery_intent="rebuild",
    )
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=candidate,
    ).save()
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_installed_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)
    monkeypatch.setattr(
        runtime._store,
        "ensure_meta",
        lambda: (_ for _ in ()).throw(RuntimeError("meta unavailable")),
    )

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_restart_failed",
        "result": "completed_empty",
    }
    assert V2Config.load().memory.recovery_intent is None
    assert runtime._restart_config.recovery_intent is None
    assert runtime.module._worker._claims_paused is True
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_is_retained_joined_and_gates_other_mutations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_rebuild() -> dict[str, object]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"ok": False, "error": "memory_rebuild_failed", "result": "failed"}

    monkeypatch.setattr(runtime, "_rebuild_locked", blocked_rebuild)
    detached = asyncio.create_task(runtime.rebuild())
    await entered.wait()
    joined = asyncio.create_task(runtime.rebuild())
    await asyncio.sleep(0)
    detached.cancel()

    assert await runtime.restart() == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    assert await runtime.reconcile(runtime._config) == {
        "ok": False,
        "error": "memory_operation_in_progress",
    }
    assert await runtime.clear(operator_ref="user:owner") == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }
    assert await runtime.install_artifact() == {
        "ok": False,
        "reason": "memory_operation_in_progress",
        "download_error": None,
    }

    release.set()
    results = await asyncio.gather(detached, joined, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    assert calls == 1
    await memory_runtime_factory.close(runtime)


async def test_runtime_rebuild_lease_rejects_a_second_controller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    first = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    second = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_rebuild() -> dict[str, object]:
        entered.set()
        await release.wait()
        return {"ok": False, "error": "memory_rebuild_failed", "result": "failed"}

    async def unexpected_rebuild() -> dict[str, object]:
        raise AssertionError("busy controller must not enter rebuild")

    monkeypatch.setattr(first, "_rebuild_locked", blocked_rebuild)
    monkeypatch.setattr(second, "_rebuild_locked", unexpected_rebuild)
    rebuilding = asyncio.create_task(first.rebuild())
    await entered.wait()

    assert await second.rebuild() == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }
    assert await second.clear(operator_ref="user:owner") == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }
    assert await second.resume_clear("clear-1", operator_ref="user:owner") == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }
    assert await second.abort_clear("clear-1", operator_ref="user:owner") == {
        "status": "failed",
        "error": "memory_operation_in_progress",
    }

    release.set()
    assert await rebuilding == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    await memory_runtime_factory.close(first)
    await memory_runtime_factory.close(second)


async def test_cancelled_rebuild_keeps_lease_until_blocking_work_settles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_work() -> None:
        entered.set()
        assert release.wait(10)

    async def blocked_rebuild() -> dict[str, object]:
        await memory_runtime.run_blocking(blocking_work)
        return {"ok": False, "error": "memory_rebuild_failed", "result": "failed"}

    monkeypatch.setattr(runtime, "_rebuild_locked", blocked_rebuild)
    caller = asyncio.create_task(runtime.rebuild())
    assert await asyncio.to_thread(entered.wait, 10)
    retained = runtime._rebuild_task
    assert retained is not None
    retained.cancel()
    await asyncio.sleep(0)

    competing = MemoryOperationLease(tmp_path)
    with pytest.raises(MemoryOperationBusy):
        competing.acquire()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller

    competing.acquire()
    competing.release()
    await memory_runtime_factory.close(runtime)


async def test_runtime_close_cancels_and_joins_retained_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    entered = asyncio.Event()
    interrupted = asyncio.Event()

    async def blocked_rebuild() -> dict[str, object]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            return {
                "ok": False,
                "error": "memory_rebuild_failed",
                "result": "interrupted",
            }

    monkeypatch.setattr(runtime, "_rebuild_locked", blocked_rebuild)
    rebuilding = asyncio.create_task(runtime.rebuild())
    await entered.wait()
    closing = asyncio.create_task(memory_runtime_factory.close(runtime))
    await interrupted.wait()
    assert await rebuilding == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "interrupted",
    }
    await closing


async def test_runtime_rebuild_rejects_admission_after_close(
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=True, processing=_processing_config()),
        artifact_manager=_installed_artifact(),
        effective_home=tmp_path,
    )
    await memory_runtime_factory.close(runtime)

    assert await runtime.rebuild() == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }
    assert runtime._rebuild_task is None

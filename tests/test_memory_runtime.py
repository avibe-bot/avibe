from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from core.managed_runtime import ManagedRuntimeArchive, ManagedRuntimeManifest
import core.memory.artifact as memory_artifact
from core.memory.artifact import (
    FakeMemoryArtifactManager,
    MemoryArtifactCandidate,
    MemoryArtifactManager,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
)
import core.memory.process as memory_process
import core.memory.runtime as memory_runtime
from core.memory.process import (
    _SIDECAR_ENTRYPOINT_MODULE,
    EverOSProcess,
    EverOSProcessSettings,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
    _ProcessIdentity,
    _RecordedSidecar,
    _classify_recorded_sidecar,
    _live_owned_processes,
    _signal_owned_group_or_process,
    _signal_owned_processes,
    _snapshot_owned_processes,
)
from core.memory.runtime import MemoryRuntime, MemoryStoreUnavailableError, create_memory_runtime
from core.memory.everos_insight.recorder import initialize_call_log
from core.memory.store import MemoryStore
from core.memory.types import (
    MemoryItem,
    MemoryItems,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    OperationFailed,
)
from config.v2_config import (
    AgentsConfig,
    MemoryConfig,
    MemoryDiagnosticsConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)


PROJECT = "p-22222222222222222222222222222222"


def _installed_artifact(**overrides) -> FakeMemoryArtifactManager:
    """A verified, installed EverOS artifact — the common runtime-test baseline."""

    defaults: dict = {
        "python": Path(sys.executable),
        "root_format": "everos-1.2.1",
        "fingerprint": "test-artifact",
        "status_payload": {"reason": None},
    }
    defaults.update(overrides)
    return FakeMemoryArtifactManager(**defaults)


def test_memory_drain_task_reactivates_recovery_after_an_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        store=MemoryStore(),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
        effective_home=tmp_path,
    )
    activation_calls = 0
    drain_calls = 0

    def begin_activation() -> None:
        nonlocal activation_calls
        activation_calls += 1

    async def drain() -> int:
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            raise RuntimeError("transient drain failure")
        runtime._config = replace(runtime._config, enabled=False)
        return 0

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runtime.module._worker, "begin_activation", begin_activation)
    monkeypatch.setattr(runtime.module._worker, "drain", drain)
    monkeypatch.setattr("core.memory.runtime.asyncio.sleep", no_wait)

    async def run() -> None:
        runtime._ensure_worker()
        assert runtime._worker_task is not None
        await runtime._worker_task

    asyncio.run(run())
    assert drain_calls == 2
    assert activation_calls == 2


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
    assert asyncio.run(runtime.status_payload())["error"] == "memory_store_unavailable"


def test_memory_runtime_reopens_the_store_after_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    runtime = create_memory_runtime(
        MemoryConfig(enabled=True),
        artifact_manager=_installed_artifact(python=None, status_payload={"reason": "memory_runtime_missing"}),
    )
    assert runtime.available is False
    # Reads stay closed, and capture is absorbed rather than raising.
    assert asyncio.run(runtime.profile_payload("u-" + "0" * 32, PROJECT)) == {
        "status": "failed",
        "error": "memory_store_unavailable",
    }
    with pytest.raises(MemoryStoreUnavailableError):
        runtime.principal_for_user_key("avibe:local")

    # An enabled reconciliation reopens it; the runtime is the same object.
    result = asyncio.run(runtime.reconcile(MemoryConfig(enabled=True)))
    assert runtime.available is True
    # The artifact is still missing, so enablement fails for that reason, not
    # for the store.
    assert result["ok"] is False
    assert result["error"] != "memory_store_unavailable"
    assert runtime.principal_for_user_key("avibe:local").startswith("u-")


def test_refresh_owned_processes_uses_one_snapshot_and_prunes_dead_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    process._process_group = 42_424
    process._owned_processes = {100: 1.0, 200: 2.0}
    snapshots: list[tuple[int, int | None]] = []

    def snapshot(pid: int, process_group: int | None) -> dict[int, float]:
        snapshots.append((pid, process_group))
        return {100: 1.0, 300: 3.0}

    monkeypatch.setattr(memory_process, "_snapshot_owned_processes", snapshot)
    monkeypatch.setattr(
        memory_process,
        "_live_owned_processes",
        lambda identities: {pid: created for pid, created in identities.items() if pid != 200},
    )

    refreshed = process._refresh_owned_processes(100)

    assert snapshots == [(100, 42_424)]
    assert refreshed == {100: 1.0, 300: 3.0}
    assert process._owned_processes == refreshed


def test_refresh_owned_processes_retains_unverifiable_identity_sentinels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    process._owned_processes = {100: 1.0, 200: -1.0}
    monkeypatch.setattr(memory_process, "_snapshot_owned_processes", lambda *_args: {100: 1.0})
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda _identities: {100: 1.0})

    assert process._refresh_owned_processes(100) == {100: 1.0, 200: -1.0}


def test_tcp_listener_check_reuses_refreshed_owned_processes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    inspected: list[int] = []

    class _Process:
        def __init__(self, process_id: int) -> None:
            self.process_id = process_id

        def net_connections(self, *, kind: str):
            assert kind == "inet"
            inspected.append(self.process_id)
            return []

    monkeypatch.setattr(memory_process.psutil, "Process", _Process)
    monkeypatch.setattr(
        memory_process,
        "_snapshot_owned_processes",
        lambda *_args: (_ for _ in ()).throw(AssertionError("listener check repeated the process-tree snapshot")),
    )

    process._assert_no_tcp_listener(100, owned_processes={100: 1.0, 300: 3.0})

    assert inspected == [100, 300]


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
        "provider_root_format": "everos-1.2.1",
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
        return subprocess.CompletedProcess(command, 0, stdout="1.2.1\n3.12.12\n", stderr="")

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
    assert manager.provider_root_format() == "everos-1.2.1"
    assert manager.compatible_provider_root_formats() == frozenset({"everos-1.2.1"})
    assert manager.artifact_fingerprint() == "dev-everos-1.2.1"
    assert len(calls) == 1
    assert "DEV RUNTIME bypass active - not for production" in caplog.text


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


def test_memory_artifact_rejects_incompatible_nonempty_root_before_pointer_activation(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
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
    (provider_root / "vector-data").write_text("data", encoding="utf-8")

    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )

    with pytest.raises(MemoryRuntimeActivationError):
        manager._write_current_pointer(
            tmp_path / "runtime" / "versions" / "candidate",
            _artifact_manifest("everos-2.0", compatible_formats=[]),
            _artifact_archive(),
        )

    assert not (tmp_path / "runtime" / "current.json").exists()


def test_memory_artifact_routes_incompatible_empty_root_through_activation_coordinator(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
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
    observed: list[MemoryProviderRootState] = []

    def coordinate(candidate, root_state, commit, _rollback) -> None:
        assert candidate.provider_root_format == "everos-2.0"
        observed.append(root_state)
        commit()

    manager.set_activation_coordinator(coordinate)
    manager._write_current_pointer(
        tmp_path / "runtime" / "versions" / "candidate",
        _artifact_manifest("everos-2.0", compatible_formats=[]),
        _artifact_archive(),
    )

    assert observed == [MemoryProviderRootState(exists=True, provider_root_format="everos-1.0", empty=True)]
    assert manager.provider_root_format() == "everos-2.0"


def test_memory_artifact_treats_generated_control_files_as_an_empty_root(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
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
    # Written into the root by every sidecar start, so they must not make an
    # otherwise empty root look like provider data.
    (provider_root / "everos.toml").write_text("[memory]\n", encoding="utf-8")
    (provider_root / "ome.toml").write_text("[strategies]\n", encoding="utf-8")
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )
    observed: list[MemoryProviderRootState] = []

    def coordinate(_candidate, root_state, commit, _rollback) -> None:
        observed.append(root_state)
        commit()

    manager.set_activation_coordinator(coordinate)
    manager._write_current_pointer(
        tmp_path / "runtime" / "versions" / "candidate",
        _artifact_manifest("everos-2.0", compatible_formats=[]),
        _artifact_archive(),
    )

    assert observed == [MemoryProviderRootState(exists=True, provider_root_format="everos-1.0", empty=True)]
    assert manager.provider_root_format() == "everos-2.0"


def test_memory_provider_root_control_files_match_the_runtime_data_check() -> None:
    """The two emptiness checks must not diverge again."""

    from core.memory import module as memory_module
    from core.memory import runtime as memory_runtime

    assert memory_runtime.PROVIDER_ROOT_CONTROL_FILES == memory_artifact.PROVIDER_ROOT_CONTROL_FILES
    assert memory_module.PROVIDER_ROOT_CONTROL_FILES == memory_artifact.PROVIDER_ROOT_CONTROL_FILES


def test_memory_artifact_accepts_declared_compatible_nonempty_root(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
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
    (provider_root / "vector-data").write_text("data", encoding="utf-8")

    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )

    state = manager._inspect_provider_root(
        manager._candidate_from_manifest(_artifact_manifest("everos-2.0", compatible_formats=["everos-1.0"]))
    )

    assert state == MemoryProviderRootState(exists=True, provider_root_format="everos-1.0", empty=False)


def test_memory_artifact_rejects_malformed_existing_root_sentinel(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
    sentinel = provider_root / ".avibe-memory-root.json"
    sentinel.write_text(json.dumps({"provider_root_format": "everos-1.0"}), encoding="utf-8")
    os.chmod(sentinel, 0o600)
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        offline=True,
        provider_root=provider_root,
    )

    with pytest.raises(MemoryRuntimeActivationError):
        manager._inspect_provider_root(
            manager._candidate_from_manifest(_artifact_manifest("everos-2.0", compatible_formats=["everos-1.0"]))
        )


def test_memory_artifact_coordinator_rolls_back_the_active_pointer(tmp_path: Path) -> None:
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True, mode=0o700)
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
    previous_pointer = {
        "provider": "manifest",
        "runtime_id": "memory-runtime",
        "runtime_version": memory_artifact.EVEROS_VERSION,
        "platform": memory_artifact.runtime_platform_tag(),
        "install_dir": "/runtime/old",
        "manifest_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
        "bin_path": "bin/python",
        "provider_root_format": "everos-1.0",
        "compatible_provider_root_formats": [],
        "artifact_fingerprint": "old-artifact",
    }
    (manager.runtime_dir / "current.json").write_text(json.dumps(previous_pointer), encoding="utf-8")
    calls: list[tuple[str, object]] = []

    def coordinate(candidate, root_state, commit, rollback) -> None:
        calls.append(("candidate", candidate.provider_root_format))
        calls.append(("root", (root_state.provider_root_format, root_state.empty)))
        commit()
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


def test_memory_artifact_rollback_resolves_old_active_binary(monkeypatch, tmp_path: Path) -> None:
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

    async def run() -> None:
        runtime = MemoryRuntime(
            MemoryConfig(enabled=True, processing=processing),
            artifact_manager=manager,
            process_factory=factory,
            effective_home=tmp_path,
        )
        assert (await runtime.reconcile(runtime._config))["ok"] is True
        assert [process.python for process in factory.supervised] == [old_binary]
        await runtime.close()

    asyncio.run(run())


def test_runtime_controller_port_never_copies_processing_credentials(tmp_path: Path) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-secret"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embedding-secret"),
    )
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
    )

    assert runtime._provider._llm_api_key is None
    assert runtime._provider._embedding_api_key is None

    async def run() -> None:
        assert await runtime.reconcile(MemoryConfig(enabled=False, processing=processing)) == {
            "ok": True,
            "state": "disabled",
        }

    asyncio.run(run())
    assert runtime._provider._llm_api_key is None
    assert runtime._provider._embedding_api_key is None


def test_reconcile_never_downloads_a_missing_runtime(tmp_path: Path) -> None:
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
    disabled = MemoryRuntime(
        MemoryConfig(enabled=False),
        artifact_manager=artifact,
        effective_home=tmp_path / "disabled",
    )
    enabled = MemoryRuntime(
        MemoryConfig(enabled=True),
        artifact_manager=artifact,
        effective_home=tmp_path / "enabled",
    )

    async def run() -> None:
        assert await disabled.reconcile(MemoryConfig(enabled=False)) == {
            "ok": True,
            "state": "disabled",
        }
        assert await enabled.reconcile(MemoryConfig(enabled=True)) == {
            "ok": False,
            "error": "memory_runtime_missing",
        }

    asyncio.run(run())


def _recording_ownership(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: BaseException | None = None,
) -> list[dict[str, Path]]:
    """Replace the runtime's ``SidecarOwnership`` with a recorder of reap calls."""

    reaps: list[dict[str, Path]] = []

    class _Ownership:
        def __init__(self, *, record_path: Path, socket_path: Path, provider_root: Path, **_kwargs) -> None:
            self._inputs = {
                "record_path": record_path,
                "socket_path": socket_path,
                "provider_root": provider_root,
            }

        async def reap(self) -> None:
            reaps.append(self._inputs)
            if failure is not None:
                raise failure

    monkeypatch.setattr(memory_runtime, "SidecarOwnership", _Ownership)
    return reaps


@pytest.mark.parametrize("boot", ["disabled", "runtime_missing", "store_unavailable"])
def test_recorded_orphan_recovery_runs_on_boots_that_never_launch_a_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boot: str,
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
    runtime = MemoryRuntime(config, artifact_manager=artifact, effective_home=home)
    if boot == "store_unavailable":
        # The store never opened, which returns before the reconcile lock.
        runtime._module = None

    result = asyncio.run(runtime.reconcile(config))

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


def test_store_reopen_failure_maintains_call_log_after_orphan_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    async def run() -> None:
        runtime = MemoryRuntime(config, effective_home=tmp_path)
        runtime._module = None
        monkeypatch.setattr(runtime, "_open_store", lambda: False)

        assert await runtime.reconcile(config) == {
            "ok": False,
            "error": "memory_store_unavailable",
        }
        assert await asyncio.to_thread(maintained.wait, 1)
        await runtime.close()

    asyncio.run(run())


def test_store_reopen_failure_does_not_maintain_call_log_beside_unreaped_orphan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    _recording_ownership(monkeypatch, failure=RuntimeError("orphan still owns call log"))
    config = MemoryConfig(enabled=True)

    async def run() -> None:
        runtime = MemoryRuntime(config, effective_home=tmp_path)
        runtime._module = None
        monkeypatch.setattr(runtime, "_open_store", lambda: False)

        assert await runtime.reconcile(config) == {
            "ok": False,
            "error": "memory_store_unavailable",
        }
        assert runtime._call_log_retention_task is None
        await runtime.close()

    asyncio.run(run())


def test_disabled_boot_retires_the_record_of_a_sidecar_that_is_already_gone(
    tmp_path: Path,
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
    runtime = MemoryRuntime(config, artifact_manager=_installed_artifact(), effective_home=tmp_path)

    assert asyncio.run(runtime.reconcile(config)) == {"ok": True, "state": "disabled"}

    assert not record_path.exists()


def test_recorded_orphan_recovery_never_reaps_a_child_this_runtime_owns(
    monkeypatch: pytest.MonkeyPatch,
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

    async def run() -> None:
        runtime = MemoryRuntime(config, artifact_manager=_installed_artifact(), process_factory=factory)
        # First reconciliation: no child exists yet, so recovery is free to run.
        assert (await runtime.reconcile(config))["ok"] is True
        assert len(reaps) == 1
        assert runtime._process is not None

        # Every later settings save finds a child of ours, and must leave it be.
        for _ in range(2):
            assert (await runtime.reconcile(config))["ok"] is True
        assert len(reaps) == 1
        assert factory.supervised[-1].stopped is False
        await runtime.close()

    asyncio.run(run())


def test_recorded_orphan_recovery_cannot_overlap_a_concurrent_launch(
    monkeypatch: pytest.MonkeyPatch,
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

    class _Ownership:
        def __init__(self, **_kwargs) -> None:
            return None

        async def reap(self) -> None:
            nonlocal reaps
            reaps += 1
            if reaps == 1:
                # Stand in for the signal rounds of a genuinely live orphan.
                started.set()
                await release.wait()

    monkeypatch.setattr(memory_runtime, "SidecarOwnership", _Ownership)
    config = MemoryConfig(
        enabled=True,
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
            embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
        ),
    )

    async def run() -> tuple[list[object], list[dict]]:
        runtime = MemoryRuntime(config, artifact_manager=_installed_artifact(), process_factory=factory)
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
            await runtime.close()
        return overlapped, results

    overlapped, results = asyncio.run(run())

    assert overlapped == [], "a launch overlapped a reap that was still running"
    assert [result["ok"] for result in results] == [True, True]
    # Both reconciliations still completed, one after the other.
    assert len(factory.supervised) == 2


def test_recorded_orphan_recovery_failure_still_applies_a_disable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog,
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
    runtime = MemoryRuntime(config, artifact_manager=_installed_artifact(), effective_home=tmp_path)

    with caplog.at_level(logging.WARNING, logger=memory_runtime.logger.name):
        result = asyncio.run(runtime.reconcile(config))

    assert result == {"ok": True, "state": "disabled"}
    assert len(reaps) == 1
    assert "Recorded EverOS sidecar recovery did not finish" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text


def test_sidecar_child_environment_is_allowlisted_and_generated_config_has_no_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/override.pem")
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._prepare_owned_directories()
    process._write_generated_config()
    environment = process._child_environment()
    generated = (tmp_path / "memory" / "generated" / "everos.toml").read_text(encoding="utf-8")

    assert environment["EVEROS_LLM__API_KEY"] == "llm-secret"
    assert environment["EVEROS_MULTIMODAL__BASE_URL"] == environment["EVEROS_LLM__BASE_URL"]
    assert environment["EVEROS_MULTIMODAL__MODEL"] == environment["EVEROS_LLM__MODEL"]
    assert environment["EVEROS_MULTIMODAL__API_KEY"] == "llm-secret"
    assert environment["AVIBE_MEMORY_ATTACHMENTS_ROOT"] == str(tmp_path / "attachments" / "avibe")
    assert environment["EVEROS_EMBEDDING__API_KEY"] == "embedding-secret"
    assert "HTTP_PROXY" not in environment
    assert "SSL_CERT_FILE" not in environment
    assert "llm-secret" not in generated
    assert "embedding-secret" not in generated
    assert "rerank" in generated
    assert str(tmp_path / "attachments" / "avibe") in generated
    assert "AVIBE_MEMORY_CALL_LOG_DB" not in environment


def test_sidecar_child_environment_includes_only_the_configured_call_log(tmp_path: Path) -> None:
    call_log = tmp_path / "memory" / "call-log" / "call-log.db"
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=replace(_settings(), call_log_db_path=call_log),
    )

    process._prepare_owned_directories()
    environment = process._child_environment()

    assert environment["AVIBE_MEMORY_CALL_LOG_DB"] == str(call_log)
    assert stat.S_IMODE(call_log.parent.stat().st_mode) == 0o700


def test_sidecar_rejects_sun_path_overflow_without_launching_child(tmp_path: Path) -> None:
    socket_path = tmp_path / ("a" * 180) / "everos.sock"
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        socket_path=socket_path,
        settings=_settings(),
    )

    async def run() -> None:
        assert await process.start() is False
        assert process.last_error == "memory_sidecar_unavailable"
        assert process.consecutive_failures == 1
        await process.stop()

    asyncio.run(run())


def test_sidecar_start_failure_never_relaunches_beside_an_unreaped_child(monkeypatch, tmp_path: Path) -> None:
    class _Child:
        pid = 999_999
        returncode = None

        async def wait(self) -> None:
            return None

        def send_signal(self, _signum) -> None:
            return None

    launches: list[_Child] = []

    async def spawn(*_args, **_kwargs) -> _Child:
        child = _Child()
        launches.append(child)
        return child

    async def readiness_failure(_process) -> None:
        raise RuntimeError("readiness failed")

    async def cleanup_failure(*_args, **_kwargs) -> None:
        raise RuntimeError("child tree still alive")

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        socket_path=Path(f"/tmp/everos-{os.getpid()}.sock"),
        settings=_settings(),
    )
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(process, "_prepare_owned_directories", lambda: None)
    monkeypatch.setattr(process, "_write_generated_config", lambda: None)
    monkeypatch.setattr(process, "_remove_owned_socket", lambda: None)
    monkeypatch.setattr(process, "_wait_for_ready", readiness_failure)
    monkeypatch.setattr(process, "_terminate_owned_tree", cleanup_failure)

    async def run() -> None:
        assert await process.start() is False
        assert process.down is True
        assert process._process is launches[0]
        assert process._restart_task is None
        assert await process.start() is False
        assert len(launches) == 1

    asyncio.run(run())


def test_processing_probe_reaps_child_when_its_caller_is_cancelled(monkeypatch, tmp_path: Path) -> None:
    started = asyncio.Event()
    cleanup_calls: list[object] = []

    class _Probe:
        pid = 999_999
        returncode = None

        async def wait(self) -> None:
            started.set()
            await asyncio.Event().wait()

    async def spawn(*_args, **_kwargs) -> _Probe:
        return _Probe()

    async def cleanup(*_args, **_kwargs) -> None:
        cleanup_calls.append(object())

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(process, "_terminate_owned_tree", cleanup)

    async def run() -> None:
        task = asyncio.create_task(process.processing_healthy())
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("processing probe cancellation was swallowed")

    asyncio.run(run())
    assert cleanup_calls


def test_sidecar_stop_signals_isolated_child_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); time.sleep(60)"
    )

    async def run() -> tuple[int, int]:
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", script, start_new_session=True)
        deadline = time.monotonic() + 3
        while not child_pid_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert child_pid_path.exists()
        descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))
        process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
        process._process_group = os.getpgid(child.pid)
        owned_processes = _snapshot_owned_processes(child.pid, process._process_group)
        await process._terminate_owned_tree(
            child,
            process_group=process._process_group,
            owned_processes=owned_processes,
        )
        return child.pid, descendant_pid

    parent_pid, descendant_pid = asyncio.run(run())
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(descendant_pid)


def test_sidecar_safety_monitor_ignores_expected_shutdown(monkeypatch, tmp_path: Path, caplog) -> None:
    class _Child:
        pid = 999_999
        returncode = None

    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    child = _Child()
    process._process = child
    process._desired_running = False
    monkeypatch.setattr(memory_process, "_owned_process_identity_is_live", lambda *_args: False)

    asyncio.run(process._monitor_child(child))

    assert "safety monitor rejected" not in caplog.text
    assert process._process is child


def test_sidecar_stop_reaps_a_descendant_that_leaves_the_child_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached-child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); time.sleep(60)"
    )

    async def run() -> tuple[int, int]:
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", script, start_new_session=True)
        deadline = time.monotonic() + 3
        while not child_pid_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert child_pid_path.exists()
        descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))
        process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
        process._process_group = os.getpgid(child.pid)
        owned_processes = _snapshot_owned_processes(child.pid, process._process_group)
        await process._terminate_owned_tree(
            child,
            process_group=process._process_group,
            owned_processes=owned_processes,
        )
        return child.pid, descendant_pid

    parent_pid, descendant_pid = asyncio.run(run())
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(descendant_pid)


def test_sidecar_cleanup_skips_a_reused_pid_identity(monkeypatch, tmp_path: Path) -> None:
    signals: list[int] = []

    class _Child:
        pid = 42_424
        returncode = None

        async def wait(self) -> None:
            return None

        def send_signal(self, signum: int) -> None:
            signals.append(signum)

    async def reaped(*_args, **_kwargs) -> bool:
        return True

    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    monkeypatch.setattr("core.memory.process._snapshot_owned_processes", lambda *_args: {42_424: 22.0})
    monkeypatch.setattr("core.memory.process._wait_for_owned_exit", reaped)

    asyncio.run(
        process._terminate_owned_tree(
            _Child(),
            process_group=None,
            owned_processes={42_424: 11.0},
        )
    )

    assert signals == []


def test_sidecar_cleanup_never_signals_spawned_pid_after_identity_changes(monkeypatch, tmp_path: Path) -> None:
    signals: list[tuple[str, int]] = []

    class _TrackedChild:
        returncode = None

        def __init__(self, pid: int) -> None:
            self.pid = pid

        def send_signal(self, signum: int) -> None:
            signals.append(("child", signum))

    async def run() -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        try:
            identities = _snapshot_owned_processes(child.pid, None)
            captured_at = identities[child.pid]
            original_process = memory_process.psutil.Process

            class _ReusedProcess:
                def __init__(self, process_id: int) -> None:
                    assert process_id == child.pid
                    self.pid = process_id

                def create_time(self) -> float:
                    return captured_at + 1.0

                def status(self) -> str:
                    return psutil.STATUS_SLEEPING

                def send_signal(self, signum: int) -> None:
                    signals.append(("psutil", signum))

            monkeypatch.setattr(memory_process.psutil, "Process", _ReusedProcess)
            try:
                _signal_owned_processes(identities, signal.SIGTERM)
                _signal_owned_group_or_process(_TrackedChild(child.pid), None, identities, signal.SIGTERM)
            finally:
                monkeypatch.setattr(memory_process.psutil, "Process", original_process)

            assert signals == []
        finally:
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), timeout=3.0)
                except TimeoutError:
                    child.kill()
                    await child.wait()

    asyncio.run(run())


def test_sidecar_cleanup_does_not_group_signal_an_unconfirmed_member(monkeypatch) -> None:
    group_signals: list[tuple[int, int]] = []
    child_signals: list[int] = []

    class _TrackedChild:
        pid = 42_424
        returncode = None

        def send_signal(self, signum: int) -> None:
            child_signals.append(signum)

    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: {42_424: 11.0, 42_425: 12.0})
    monkeypatch.setattr(memory_process, "_confirmed_owned_processes", lambda _identities: {42_424: 11.0})
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))

    _signal_owned_group_or_process(
        _TrackedChild(),
        42_424,
        {42_424: 11.0, 42_425: 12.0},
        signal.SIGTERM,
    )

    assert group_signals == []
    assert child_signals == [signal.SIGTERM]


def test_sidecar_group_snapshot_fails_closed_for_an_inaccessible_member(monkeypatch) -> None:
    parent_id = 42_424
    child_id = 42_425
    group_id = 42_424
    group_signals: list[tuple[int, int]] = []
    child_signals: list[int] = []

    class _GroupMember:
        def __init__(self, process_id: int, created_at: float | None) -> None:
            self.pid = process_id
            self._created_at = created_at

        def create_time(self) -> float:
            if self._created_at is None:
                raise psutil.AccessDenied(pid=self.pid)
            return self._created_at

    class _TrackedChild:
        pid = parent_id
        returncode = None

        def send_signal(self, signum: int) -> None:
            child_signals.append(signum)

    monkeypatch.setattr(
        memory_process.psutil,
        "process_iter",
        lambda: [_GroupMember(parent_id, 11.0), _GroupMember(child_id, None)],
    )
    monkeypatch.setattr(memory_process.os, "getpgid", lambda _process_id: group_id)
    monkeypatch.setattr(memory_process, "_confirmed_owned_processes", lambda _identities: {parent_id: 11.0})
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))

    snapshot = memory_process._snapshot_process_group(group_id)
    declared_safe = memory_process._group_contains_only_confirmed_owned_processes(
        group_id,
        {parent_id: 11.0, child_id: 12.0},
    )
    _signal_owned_group_or_process(
        _TrackedChild(),
        group_id,
        {parent_id: 11.0, child_id: 12.0},
        signal.SIGTERM,
    )

    assert snapshot == {parent_id: 11.0, child_id: -1.0}
    assert declared_safe is False
    assert group_signals == []
    assert child_signals == [signal.SIGTERM]


def test_sidecar_cleanup_keeps_access_denied_identity_live_without_signaling(monkeypatch) -> None:
    process_id = 42_425

    class _InaccessibleProcess:
        def __init__(self, _process_id: int) -> None:
            raise psutil.AccessDenied(pid=process_id)

    monkeypatch.setattr(memory_process.psutil, "Process", _InaccessibleProcess)
    identities = {process_id: 11.0}

    assert _live_owned_processes(identities) == identities
    _signal_owned_processes(identities, signal.SIGTERM)


def test_sidecar_crash_counter_resets_only_after_observed_healthy_window(tmp_path: Path) -> None:
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    process._consecutive_failures = 4

    process._record_health_observation(True, observed_at=10.0)
    process._record_health_observation(False, observed_at=310.0)
    process._record_health_observation(True, observed_at=311.0)
    process._record_health_observation(True, observed_at=610.0)

    assert process.consecutive_failures == 4

    process._record_health_observation(True, observed_at=611.0)

    assert process.consecutive_failures == 0


def test_explicit_sidecar_retry_keeps_crash_budget_until_observed_health(monkeypatch, tmp_path: Path) -> None:
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    process._down = True
    process._consecutive_failures = 5

    async def start_stub() -> bool:
        return True

    monkeypatch.setattr(process, "_start_locked", start_stub)

    assert asyncio.run(process.start()) is True
    assert process.down is False
    assert process.consecutive_failures == 5


def test_runtime_recovers_interrupted_clear_before_starting_sidecar(monkeypatch, tmp_path: Path) -> None:
    started: list[object] = []

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    factory = FakeEverOSProcessFactory()
    started = factory.created

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(),
        process_factory=factory,
        effective_home=tmp_path,
    )

    async def interrupted_clear() -> OperationFailed:
        return OperationFailed(error="memory_clear_failed")

    monkeypatch.setattr(runtime.module, "_recover_interrupted_clear", interrupted_clear)

    async def run() -> None:
        assert await runtime.reconcile(runtime._config) == {"ok": False, "error": "memory_clear_failed"}

    asyncio.run(run())
    assert started == []


def test_runtime_install_artifact_uses_controller_owned_manager(tmp_path: Path) -> None:
    calls: list[bool] = []

    artifact = FakeMemoryArtifactManager(
        root_format=None,
        fingerprint=None,
        compatible_formats=frozenset(),
        ensure_payload={"ok": False, "reason": "memory_runtime_unpublished", "download_error": None},
    )
    calls = artifact.ensure_calls
    runtime = MemoryRuntime(MemoryConfig(enabled=False), artifact_manager=artifact, effective_home=tmp_path)

    async def run() -> None:
        assert await runtime.install_artifact() == {
            "ok": False,
            "reason": "memory_runtime_unpublished",
            "download_error": None,
        }

    asyncio.run(run())
    assert callable(artifact.activation_coordinator)
    assert calls == [True]
    assert runtime._config.enabled is False


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


def test_runtime_repair_stops_retained_down_supervisor_before_replacing_artifact(monkeypatch, tmp_path: Path) -> None:
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
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(root_format=None, fingerprint=None),
        effective_home=tmp_path,
    )
    runtime._process = _DownProcess()

    async def pause_and_wait() -> bool:
        events.append("pause")
        return True

    monkeypatch.setattr(runtime.module._worker, "pause_and_wait", pause_and_wait)

    assert asyncio.run(runtime.install_artifact()) == {
        "ok": True,
        "reason": None,
        "download_error": None,
    }
    assert events == ["pause", "stop", "ensure"]
    assert runtime._process is None


def test_runtime_repair_rejects_healthy_running_sidecar(monkeypatch, tmp_path: Path) -> None:
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
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(root_format=None, fingerprint=None),
        effective_home=tmp_path,
    )
    runtime._process = _LiveProcess()

    result = asyncio.run(runtime.install_artifact())
    assert result == {
        "ok": False,
        "reason": "memory_runtime_install_requires_disabled_memory",
        "download_error": None,
    }
    # The healthy sidecar was neither stopped nor replaced.
    assert events == []
    assert runtime._process is not None


def test_runtime_activation_timeout_cancels_and_settles_submitted_coroutine(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
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
                    provider_root_format="everos-1.2.1",
                    compatible_provider_root_formats=frozenset({"everos-1.2.1"}),
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

    asyncio.run(run())


def test_runtime_rejects_embedding_change_when_root_inspection_fails_under_lifecycle_lock(monkeypatch, tmp_path: Path) -> None:
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

    async def run() -> None:
        runtime = MemoryRuntime(
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
        await runtime.close()

    asyncio.run(run())


def test_runtime_restart_rechecks_persisted_embedding_candidate(monkeypatch, tmp_path: Path) -> None:
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
            embedding_change_pending=True,
        ),
    ).save()
    restarted = V2Config.load().memory
    inspected: list[bool] = []
    factory = FakeEverOSProcessFactory()

    async def run() -> None:
        runtime = MemoryRuntime(
            restarted,
            artifact_manager=_Artifact(),
            process_factory=factory,
            effective_home=tmp_path,
        )

        def existing_vectors() -> bool:
            inspected.append(runtime.module._lifecycle_lock.locked())
            return True

        monkeypatch.setattr(runtime, "_provider_data_exists_strict", existing_vectors, raising=False)
        assert await runtime.reconcile(restarted) == {"ok": False, "error": "memory_clear_failed"}
        assert runtime._config is restarted
        assert runtime.module._worker._claims_paused is False
        await runtime.close()

    asyncio.run(run())
    assert inspected == [True]
    # The rejection must land before any child is launched.
    assert factory.created == []


def test_runtime_settles_embedding_candidate_before_resuming_claims(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def _Artifact() -> FakeMemoryArtifactManager:
        return _installed_artifact()

    observed_before_ready: list[tuple[bool, bool]] = []
    runtime: MemoryRuntime | None = None

    class _Process(FakeEverOSProcess):
        """Snapshot persisted candidate state and claim fencing before readiness."""

        async def start(self) -> bool:
            assert runtime is not None
            observed_before_ready.append(
                (
                    V2Config.load().memory.embedding_change_pending,
                    runtime.module._worker._claims_paused,
                )
            )
            return await super().start()

    factory = FakeEverOSProcessFactory(template=_Process)

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
            embedding_change_pending=True,
        ),
    ).save()
    restarted = V2Config.load().memory

    async def run() -> None:
        nonlocal runtime
        runtime = MemoryRuntime(
            restarted,
            artifact_manager=_Artifact(),
            process_factory=factory,
            effective_home=tmp_path,
        )
        monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False, raising=False)
        assert (await runtime.reconcile(restarted))["ok"] is True
        assert runtime._config.embedding_change_pending is False
        assert runtime.module._worker._claims_paused is False
        await runtime.close()

    asyncio.run(run())
    assert observed_before_ready == [(False, True)]
    assert V2Config.load().memory.embedding_change_pending is False


def test_runtime_artifact_activation_rolls_back_root_and_sidecar(monkeypatch, tmp_path: Path) -> None:
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

    async def run() -> None:
        runtime = MemoryRuntime(
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

        sentinel = json.loads((tmp_path / "memory" / "everos-root" / ".avibe-memory-root.json").read_text())
        assert sentinel["provider_root_format"] == "everos-1.0"
        assert sentinel["created_by_artifact_fingerprint"] == "old-artifact"
        assert manager._active_pointer() == previous_pointer
        assert manager.provider_root_format() == "everos-1.0"
        assert runtime.module._provider_root_format == "everos-1.0"
        assert runtime.module._artifact_fingerprint == "old-artifact"
        assert len(instances) == 3
        assert instances[0].stopped is True
        assert instances[1].stopped is True
        assert instances[2].stopped is False
        assert runtime.module._worker._claims_paused is False
        await runtime.close()

    asyncio.run(run())


def test_runtime_artifact_activation_switches_incompatible_empty_root(monkeypatch, tmp_path: Path) -> None:
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

    async def run() -> None:
        runtime = MemoryRuntime(
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

        sentinel = json.loads((tmp_path / "memory" / "everos-root" / ".avibe-memory-root.json").read_text())
        assert sentinel["provider_root_format"] == "everos-2.0"
        assert manager.provider_root_format() == "everos-2.0"
        assert runtime.module._provider_root_format == "everos-2.0"
        assert len(instances) == 2
        assert instances[0].stopped is True
        assert instances[1].stopped is False
        assert runtime.module._worker._claims_paused is False
        await runtime.close()

    asyncio.run(run())


def test_runtime_artifact_activation_rolls_back_after_sentinel_postwrite_failure(
    monkeypatch,
    tmp_path: Path,
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

    factory = FakeEverOSProcessFactory()
    instances = factory.supervised

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    async def run() -> None:
        runtime = MemoryRuntime(
            initial,
            store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
            artifact_manager=manager,
            process_factory=factory,
        )
        assert (await runtime.reconcile(initial))["ok"] is True
        original_verify = runtime.module._verify_owned_provider_root
        failed_once = False

        def fail_candidate_postwrite(meta, *, require_empty, allow_format_mismatch=False):
            nonlocal failed_once
            original_verify(
                meta,
                require_empty=require_empty,
                allow_format_mismatch=allow_format_mismatch,
            )
            sentinel = json.loads(
                (tmp_path / "memory" / "everos-root" / ".avibe-memory-root.json").read_text()
            )
            if sentinel["provider_root_format"] == "everos-2.0" and not failed_once:
                failed_once = True
                raise RuntimeError("injected post-write verification failure")

        monkeypatch.setattr(runtime.module, "_verify_owned_provider_root", fail_candidate_postwrite)

        with pytest.raises(MemoryRuntimeActivationError):
            await asyncio.to_thread(
                manager._write_current_pointer,
                manager.runtime_dir / "versions" / "candidate",
                _artifact_manifest("everos-2.0", compatible_formats=[]),
                _artifact_archive(),
            )

        sentinel = json.loads((tmp_path / "memory" / "everos-root" / ".avibe-memory-root.json").read_text())
        assert failed_once is True
        assert sentinel["provider_root_format"] == "everos-1.0"
        assert sentinel["created_by_artifact_fingerprint"] == "old-artifact"
        assert manager._active_pointer() == previous_pointer
        assert runtime.module._provider_root_format == "everos-1.0"
        assert runtime.module._artifact_fingerprint == "old-artifact"
        assert len(instances) == 2
        assert instances[0].stopped is True
        assert instances[1].stopped is False
        assert runtime.module._worker._claims_paused is False
        await runtime.close()

    asyncio.run(run())


def test_runtime_reconciliation_restarts_sidecar_with_fresh_child_settings(monkeypatch) -> None:
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

    async def run() -> None:
        runtime = MemoryRuntime(initial, artifact_manager=_Artifact(), process_factory=factory)
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
        await runtime.close()

    asyncio.run(run())


def test_runtime_preflight_failure_keeps_existing_sidecar_running(monkeypatch) -> None:
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

    async def run() -> None:
        runtime = MemoryRuntime(initial, artifact_manager=_Artifact(), process_factory=factory)
        assert (await runtime.reconcile(initial))["ok"] is True
        rejected = replace(
            initial,
            processing=replace(processing, llm=replace(processing.llm, model="unhealthy")),
        )
        assert await runtime.reconcile(rejected) == {"ok": False, "error": "memory_processing_failed"}
        assert len(instances) == 1
        assert instances[0].stopped is False
        assert runtime._config is initial
        await runtime.close()

    asyncio.run(run())


def test_runtime_passes_call_log_only_to_the_supervised_recorder_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    async def run() -> None:
        runtime = MemoryRuntime(
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
        await runtime.close()

    asyncio.run(run())


def test_disabling_diagnostics_stops_recorder_before_fallible_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    class _Process(FakeEverOSProcess):
        async def processing_healthy(self) -> bool:
            return self.settings is not None and self.settings.llm_model != "unhealthy"

    factory = FakeEverOSProcessFactory(template=_Process)
    initial = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )
    rejected = replace(
        initial,
        processing=replace(
            initial.processing,
            llm=replace(initial.processing.llm, model="unhealthy"),
        ),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=False),
    )

    async def run() -> None:
        runtime = MemoryRuntime(
            initial,
            artifact_manager=_installed_artifact(),
            process_factory=factory,
            effective_home=tmp_path,
        )
        assert (await runtime.reconcile(initial))["ok"] is True
        recorder_child = factory.supervised[0]

        assert await runtime.reconcile(rejected) == {
            "ok": False,
            "error": "memory_processing_failed",
        }
        assert recorder_child.stopped is True
        assert runtime._process is None
        assert runtime._config.processing == initial.processing
        assert runtime._config.diagnostics.log_provider_calls is False
        assert factory.created[-1].settings.call_log_db_path is None
        await runtime.close()

    asyncio.run(run())


def test_recorder_corruption_stays_degraded_until_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    async def run() -> None:
        runtime = MemoryRuntime(
            config,
            artifact_manager=_installed_artifact(),
            process_factory=FakeEverOSProcessFactory(),
            effective_home=tmp_path,
        )
        assert (await runtime.reconcile(config))["ok"] is True
        calls = 0

        async def recorder_health() -> dict[str, str | None]:
            nonlocal calls
            calls += 1
            return (
                {"state": "degraded", "reason": "call_log_corrupt"}
                if calls == 1
                else {"state": "active", "reason": None}
            )

        async def ready_status():
            return memory_runtime.MemoryStatus(state="ready")

        monkeypatch.setattr(runtime._provider, "recorder_health", recorder_health)
        monkeypatch.setattr(runtime.module, "status", ready_status)

        first = await runtime.status_payload()
        second = await runtime.status_payload()

        assert first["recorder"] == {
            "state": "degraded",
            "reason": "call_log_corrupt",
        }
        assert second["recorder"] == first["recorder"]
        assert calls == 1
        await runtime._stop_sidecar_for_clear()
        assert runtime._recorder_health == {"state": "disabled", "reason": None}
        await runtime.close()

    asyncio.run(run())


def test_live_sidecar_with_disabled_recorder_is_reported_as_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    async def run() -> None:
        runtime = MemoryRuntime(
            config,
            artifact_manager=_installed_artifact(),
            process_factory=FakeEverOSProcessFactory(),
            effective_home=tmp_path,
        )
        assert (await runtime.reconcile(config))["ok"] is True

        async def recorder_health() -> dict[str, str | None]:
            return {"state": "disabled", "reason": None}

        async def ready_status() -> memory_runtime.MemoryStatus:
            return memory_runtime.MemoryStatus(state="ready")

        monkeypatch.setattr(runtime._provider, "recorder_health", recorder_health)
        monkeypatch.setattr(runtime.module, "status", ready_status)
        assert (await runtime.status_payload())["recorder"] == {
            "state": "degraded",
            "reason": "writer_failures",
        }
        await runtime.close()

    asyncio.run(run())


def test_recorder_reap_hands_call_log_to_host_until_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    async def run() -> None:
        runtime = MemoryRuntime(
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
        assert runtime._process_records_calls is True
        assert runtime._call_log_retention_task is None
        await runtime.close()

    asyncio.run(run())


def test_stale_recorder_supervisor_cannot_release_host_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    factory = FakeEverOSProcessFactory()
    config = MemoryConfig(
        enabled=True,
        processing=_processing_config(),
        diagnostics=MemoryDiagnosticsConfig(log_provider_calls=True),
    )

    async def run() -> None:
        runtime = MemoryRuntime(
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
        await runtime.close()

    asyncio.run(run())


def test_disabled_runtime_maintains_retained_call_log_and_reports_corruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    maintained = threading.Event()
    maintenance_calls = 0

    def maintain(path: Path) -> str:
        nonlocal maintenance_calls
        assert path == db_path
        maintenance_calls += 1
        maintained.set()
        return "call_log_corrupt"

    monkeypatch.setattr(memory_runtime, "maintain_call_log", maintain)

    async def run() -> None:
        runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
        assert await runtime.reconcile(MemoryConfig()) == {
            "ok": True,
            "state": "disabled",
        }
        assert await asyncio.to_thread(maintained.wait, 1)
        task = runtime._call_log_retention_task
        assert task is not None
        for _ in range(20):
            if runtime._recorder_health["reason"] == "call_log_corrupt":
                break
            await asyncio.sleep(0)
        payload = await runtime.status_payload()
        assert payload["recorder"] == {
            "state": "degraded",
            "reason": "call_log_corrupt",
        }
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
        assert maintenance_calls == 1
        assert runtime._call_log_retention_task is None
        runtime._ensure_call_log_retention()
        assert runtime._call_log_retention_task is None
        assert maintenance_calls == 1
        await runtime.close()
        assert task.done()

    asyncio.run(run())


def test_runtime_close_waits_for_active_call_log_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    async def run() -> None:
        runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
        await runtime.reconcile(MemoryConfig())
        assert await asyncio.to_thread(entered.wait, 1)

        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, timeout=1)

    asyncio.run(run())


def test_clear_stops_call_log_maintenance_and_removes_only_owned_database_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    unexpected = db_path.parent / "keep.txt"
    unexpected.write_text("keep", encoding="utf-8")
    os.chmod(unexpected, 0o600)

    async def run() -> None:
        runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
        await runtime._stop_sidecar_for_clear()

        assert not db_path.exists()
        assert unexpected.read_text(encoding="utf-8") == "keep"
        assert runtime._recorder_health == {"state": "disabled", "reason": None}
        await runtime.close()

    asyncio.run(run())


def test_generated_timezone_stays_with_existing_provider_root(tmp_path: Path) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._prepare_owned_directories()
    (tmp_path / "memory" / "everos-root" / "everos.toml").write_text(
        "[memory]\ntimezone = \"Asia/Shanghai\"\n",
        encoding="utf-8",
    )

    process._write_generated_config()

    contents = (tmp_path / "memory" / "everos-root" / "everos.toml").read_text(encoding="utf-8")
    assert 'timezone = "Asia/Shanghai"' in contents


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
        runtime_version="1.2.1",
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


def test_profile_payload_reports_only_its_own_principal_emptiness(
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

    async def run():
        return await asyncio.gather(
            runtime.profile_payload(populated_principal, PROJECT),
            runtime.profile_payload(empty_principal, PROJECT),
        )

    populated, empty = asyncio.run(run())

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


def test_status_data_exists_ignores_an_empty_diagnostic_call_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    db_path = tmp_path / "memory" / "call-log" / "call-log.db"
    db_path.parent.mkdir(parents=True, mode=0o700)
    initialize_call_log(db_path)
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: False)

    async def run() -> None:
        assert (await runtime.status_payload())["data_exists"] is False
        await runtime.close()

    asyncio.run(run())


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


def test_cancelled_insight_read_keeps_lifecycle_lock_until_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    runtime = MemoryRuntime(
        MemoryConfig(),
        store=MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite"),
        effective_home=tmp_path,
        insight_reader=BlockingReader(),
    )

    async def run() -> None:
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
        await runtime.close()

    asyncio.run(run())


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


def _processing_config() -> MemoryProcessingConfig:
    return MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-secret"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embedding-secret"),
    )


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


def test_drain_health_gate_never_waits_on_an_in_flight_reconcile_probe(
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

    async def run() -> None:
        probe = asyncio.create_task(runtime._probe_processing(Path(sys.executable), config))
        await entered.wait()
        assert await asyncio.wait_for(runtime._processing_healthy(), timeout=2.0) is True
        release.set()
        assert await probe is True

    asyncio.run(run())


def test_reconcile_probe_never_waits_on_an_in_flight_drain_health_gate(
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

    async def run() -> None:
        gate = asyncio.create_task(runtime._processing_healthy())
        await entered.wait()
        probed = await asyncio.wait_for(
            runtime._probe_processing(Path(sys.executable), config),
            timeout=2.0,
        )
        assert probed is True
        release.set()
        assert await gate is True

    asyncio.run(run())


def test_drain_health_gate_reuses_the_last_verdict_instead_of_queueing(
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

    async def run() -> None:
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

    asyncio.run(run())


@pytest.mark.parametrize("changes_embedding", [False, True])
def test_reconcile_releases_the_claim_fence_when_the_worker_pause_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changes_embedding: bool,
) -> None:
    """A failed settings save must never leave the drain loop fenced forever.

    ``pause_and_wait`` fences claims before it waits, so returning on its
    timeout without resuming left ``MemoryWorker.drain`` returning at its
    ``_claims_paused`` check — no claims, and no health probing — until the
    service restarted.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    processing = _processing_config()
    runtime = MemoryRuntime(
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

    result = asyncio.run(runtime.reconcile(candidate))

    assert result == {"ok": False, "error": "memory_clear_failed"}
    assert worker._claims_paused is False


_ORPHAN_PID = 424_242
_ORPHAN_DESCENDANT_PID = 424_243
_ORPHAN_GROUP_MEMBER_PID = 424_244
_ORPHAN_GROUP_HELPER_PID = 424_245
_FOREIGN_GROUP_PID = 424_246
_FOREIGN_UID_GROUP_PID = 424_247
_ORPHAN_CREATE_TIME = 1_700_000_000.5


def _orphan_process(tmp_path: Path, **overrides) -> EverOSProcess:
    return EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        **overrides,
    )


@pytest.fixture
def short_socket_path() -> Iterator[Path]:
    """A socket path that fits ``sun_path``, for tests that call ``start``.

    ``tmp_path`` alone is already past the 104-byte macOS limit, so a launch rooted
    there fails ``_validate_launch_inputs`` before it ever reaches the orphan
    check — every assertion after it would hold vacuously.
    """

    directory = Path(tempfile.mkdtemp(prefix="avibe-"))
    socket_path = directory / "everos.sock"
    assert len(os.fsencode(socket_path)) + 1 <= 104, socket_path
    try:
        yield socket_path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _orphan_record(process: EverOSProcess, **overrides) -> dict:
    record = {
        "pid": _ORPHAN_PID,
        "create_time": _ORPHAN_CREATE_TIME,
        # ``start_new_session=True`` makes the sidecar lead a group of its own
        # number, which is what identifies its helpers once the leader is gone.
        "process_group": _ORPHAN_PID,
        "socket_path": str(process.socket_path),
        "provider_root": str(process.provider_root),
    }
    record.update(overrides)
    return record


def _orphan_identity(process: EverOSProcess, **overrides) -> _ProcessIdentity:
    fields = {
        "create_time": _ORPHAN_CREATE_TIME,
        "cmdline": (
            sys.executable,
            "-m",
            _SIDECAR_ENTRYPOINT_MODULE,
            "--uds",
            str(process.socket_path),
        ),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }
    fields.update(overrides)
    return _ProcessIdentity(**fields)


def _write_orphan_record(process: EverOSProcess, record: dict) -> Path:
    path = process._ownership.record_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_recorded_sidecar_identity_accepts_only_a_provably_owned_orphan(tmp_path: Path) -> None:
    """The decision that gates a kill signal, in one place.

    Every ``NOT_OURS`` case below is a process Avibe must leave alone and a record
    it may retire: a recycled pid, a sidecar from another home, another user's
    process, or something that is not our entrypoint at all. ``UNVERIFIABLE`` is
    the separate case of a live pid whose deciding facts were never disclosed --
    it must not be confused with "gone", because that starts a second sidecar.
    """

    process = _orphan_process(tmp_path)
    own_uid = os.getuid() if hasattr(os, "getuid") else None

    def verdict(record: dict, identity: _ProcessIdentity | None) -> _RecordedSidecar:
        return _classify_recorded_sidecar(
            record,
            identity,
            socket_path=process.socket_path,
            provider_root=process.provider_root,
        )

    assert verdict(_orphan_record(process), _orphan_identity(process)) is _RecordedSidecar.OURS

    not_ours: list[tuple[dict, _ProcessIdentity | None]] = [
        # The process is confirmed gone.
        (_orphan_record(process), None),
        # The pid was recycled: same number, different process.
        (_orphan_record(process), _orphan_identity(process, create_time=_ORPHAN_CREATE_TIME + 1)),
        # Not our entrypoint.
        (_orphan_record(process), _orphan_identity(process, cmdline=(sys.executable, "-m", "http.server"))),
        # Our entrypoint name, but serving a different socket.
        (
            _orphan_record(process),
            _orphan_identity(
                process,
                cmdline=(sys.executable, "-m", _SIDECAR_ENTRYPOINT_MODULE, "--uds", "/tmp/other.sock"),
            ),
        ),
        # Another user's process.
        (_orphan_record(process), _orphan_identity(process, uid=(own_uid or 0) + 1)),
        # A recycled pid owned by another user that will not disclose its cmdline:
        # the readable uid alone is enough to rule it out, so startup continues.
        (
            _orphan_record(process),
            _orphan_identity(process, uid=(own_uid or 0) + 1, cmdline=None),
        ),
        # A record written for a different provider root or socket.
        (_orphan_record(process, provider_root="/tmp/other-root"), _orphan_identity(process)),
        (_orphan_record(process, socket_path="/tmp/other.sock"), _orphan_identity(process)),
        # A malformed creation time can never be matched.
        (_orphan_record(process, create_time="1700000000.5"), _orphan_identity(process)),
        (_orphan_record(process, create_time=True), _orphan_identity(process)),
    ]
    for record, identity in not_ours:
        assert verdict(record, identity) is _RecordedSidecar.NOT_OURS, (record, identity)

    unverifiable: list[tuple[dict, _ProcessIdentity]] = [
        # Live, matching uid and creation time, but the cmdline is withheld —
        # nothing here excludes our own sidecar.
        (_orphan_record(process), _orphan_identity(process, cmdline=None)),
        # A live pid that disclosed nothing at all.
        (_orphan_record(process), _orphan_identity(process, create_time=None, cmdline=None, uid=None)),
        # The creation time alone is withheld, so pid reuse cannot be ruled out.
        (_orphan_record(process), _orphan_identity(process, create_time=None)),
    ]
    for record, identity in unverifiable:
        assert verdict(record, identity) is _RecordedSidecar.UNVERIFIABLE, (record, identity)

    if own_uid is not None:
        # An unreadable uid is not an exclusion either.
        assert verdict(_orphan_record(process), _orphan_identity(process, uid=None)) is (
            _RecordedSidecar.UNVERIFIABLE
        )


def _guarded_process_class(
    *,
    create_time: float | None = _ORPHAN_CREATE_TIME,
    uid: int | None = None,
    cmdline: tuple[str, ...] | None = None,
):
    """A ``psutil.Process`` stand-in that withholds every field left at ``None``.

    Models what a real OS does: macOS discloses ``create_time`` and ``uids`` for
    any pid but refuses ``cmdline`` outside the caller's own uid.
    """

    class _Guarded:
        def __init__(self, process_id: int) -> None:
            self.pid = process_id

        def status(self) -> str:
            return psutil.STATUS_SLEEPING

        def create_time(self) -> float:
            if create_time is None:
                raise psutil.AccessDenied(pid=self.pid)
            return create_time

        def cmdline(self) -> list[str]:
            if cmdline is None:
                raise psutil.AccessDenied(pid=self.pid)
            return list(cmdline)

        def uids(self):
            if uid is None:
                raise psutil.AccessDenied(pid=self.pid)
            return SimpleNamespace(real=uid, effective=uid, saved=uid)

    return _Guarded


def test_process_identity_reports_undisclosed_fields_instead_of_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A withheld field must not read as "this pid is not running".

    Collapsing every ``psutil.Error`` to ``None`` made a live sidecar whose
    cmdline the OS refuses to disclose indistinguishable from a reaped one.
    """

    guarded = _guarded_process_class(uid=4_242)
    monkeypatch.setattr(memory_process.psutil, "Process", guarded)

    identity = memory_process._inspect_process_identity(_ORPHAN_PID)

    assert identity == _ProcessIdentity(create_time=_ORPHAN_CREATE_TIME, cmdline=None, uid=4_242)

    class _Zombie(guarded):
        def status(self) -> str:
            return psutil.STATUS_ZOMBIE

    class _Gone(guarded):
        def __init__(self, process_id: int) -> None:
            raise psutil.NoSuchProcess(pid=process_id)

    class _ExitsMidRead(guarded):
        def cmdline(self) -> list[str]:
            raise psutil.NoSuchProcess(pid=self.pid)

    for stub in (_Zombie, _Gone, _ExitsMidRead):
        monkeypatch.setattr(memory_process.psutil, "Process", stub)
        assert memory_process._inspect_process_identity(_ORPHAN_PID) is None

    class _NoUidsPlatform(guarded):
        def uids(self):
            # ``psutil`` declares ``uids`` everywhere but only implements it on
            # POSIX, so on Windows the call itself raises.
            raise AttributeError("uids")

    monkeypatch.setattr(memory_process.psutil, "Process", _NoUidsPlatform)

    assert memory_process._inspect_process_identity(_ORPHAN_PID) == _ProcessIdentity(
        create_time=_ORPHAN_CREATE_TIME,
        cmdline=None,
        uid=None,
    )


def test_sidecar_launch_reaps_a_recorded_orphan_from_a_previous_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A crashed service leaves its ``start_new_session`` child running.

    Boot used to spawn a second sidecar beside it, so the orphan kept serving the
    socket and holding handles on provider data until it was killed by hand.
    """

    process = _orphan_process(tmp_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    live = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    signalled: list[int] = []

    def signal_processes(identities, signum) -> None:
        signalled.append(signum)
        assert identities == {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
        live.clear()

    monkeypatch.setattr(
        memory_process,
        "_inspect_process_identity",
        lambda pid: _orphan_identity(process) if pid == _ORPHAN_PID else None,
    )
    monkeypatch.setattr(memory_process, "_signal_owned_processes", signal_processes)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda _identities: dict(live))

    asyncio.run(process._ownership.reap())

    assert signalled == [signal.SIGTERM]
    assert not record_path.exists()


def test_sidecar_launch_never_signals_a_pid_it_cannot_identify(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recycled pid retires the record instead of killing its new owner."""

    process = _orphan_process(tmp_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    signalled: list[int] = []

    monkeypatch.setattr(
        memory_process,
        "_inspect_process_identity",
        lambda _pid: _orphan_identity(process, create_time=_ORPHAN_CREATE_TIME + 10),
    )
    monkeypatch.setattr(
        memory_process,
        "_signal_owned_processes",
        lambda *_args: signalled.append(object()),
    )

    asyncio.run(process._ownership.reap())

    assert signalled == []
    assert not record_path.exists()


def test_sidecar_launch_refuses_to_start_beside_an_unreapable_orphan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """Fail closed, exactly as ``start`` already does for an unreaped child."""

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    _write_orphan_record(process, _orphan_record(process))
    spawns: list[tuple] = []

    async def spawn(*args, **_kwargs):
        # Recorded rather than asserted: ``_start_locked`` catches every exception,
        # so a raised ``AssertionError`` here would be swallowed into a plain
        # "start failed" and the test would pass for the wrong reason.
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    monkeypatch.setattr(
        memory_process,
        "_inspect_process_identity",
        lambda _pid: _orphan_identity(process),
    )
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: None)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda identities: dict(identities))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    assert asyncio.run(process.start()) is False
    assert process.last_error == "memory_sidecar_unavailable"
    assert spawns == []
    assert process._ownership.record_path.exists()


def test_sidecar_launch_reaps_the_whole_orphan_tree_not_just_the_recorded_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An orphan's helpers hold the provider root just as its root process does.

    Signalling only the recorded pid left same-group helpers running against the
    root while a replacement sidecar started, recreating the overlap this reap
    exists to prevent. Discovery must match the normal stop path: descendants plus
    every member of the isolated process group.
    """

    process = _orphan_process(tmp_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    tree = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
        _ORPHAN_GROUP_MEMBER_PID: _ORPHAN_CREATE_TIME + 2,
    }
    live = dict(tree)
    snapshots: list[tuple[int, int | None]] = []
    group_signals: list[tuple[int, int]] = []
    signalled: list[dict[int, float]] = []

    def snapshot(pid: int, process_group: int | None) -> dict[int, float]:
        snapshots.append((pid, process_group))
        return dict(tree)

    def signal_processes(identities, signum) -> None:
        del signum
        signalled.append(dict(identities))
        live.clear()

    monkeypatch.setattr(
        memory_process,
        "_inspect_process_identity",
        lambda pid: _orphan_identity(process) if pid == _ORPHAN_PID else None,
    )
    # ``start_new_session=True`` makes the sidecar its own process group leader.
    monkeypatch.setattr(memory_process, "_isolated_process_group", lambda pid: pid)
    monkeypatch.setattr(memory_process, "_snapshot_owned_processes", snapshot)
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: dict(tree))
    monkeypatch.setattr(memory_process, "_confirmed_owned_processes", lambda identities: dict(identities))
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))
    monkeypatch.setattr(memory_process, "_signal_owned_processes", signal_processes)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda _identities: dict(live))

    asyncio.run(process._ownership.reap())

    assert snapshots == [(_ORPHAN_PID, _ORPHAN_PID)]
    assert group_signals == [(_ORPHAN_PID, signal.SIGTERM)]
    assert signalled == [tree]
    assert not record_path.exists()


def test_sidecar_orphan_reap_refuses_a_group_signal_for_an_unverifiable_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Widening discovery must not widen the blast radius.

    A group holding a member carrying the ``AccessDenied`` sentinel is never
    signaled group-wide, and a member that cannot be proven reaped still fails the
    launch instead of being written off.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    discovered = {_ORPHAN_PID: _ORPHAN_CREATE_TIME, _ORPHAN_GROUP_MEMBER_PID: -1.0}
    live = dict(discovered)
    group_signals: list[tuple[int, int]] = []
    signalled: list[int] = []

    def signal_processes(identities, signum) -> None:
        del identities
        signalled.append(signum)
        # The confirmed root exits; the unverifiable member cannot be proven gone.
        live.pop(_ORPHAN_PID, None)

    monkeypatch.setattr(
        memory_process,
        "_inspect_process_identity",
        lambda _pid: _orphan_identity(process),
    )
    monkeypatch.setattr(memory_process, "_isolated_process_group", lambda pid: pid)
    monkeypatch.setattr(memory_process, "_snapshot_owned_processes", lambda _pid, _group: dict(discovered))
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: dict(discovered))
    monkeypatch.setattr(
        memory_process,
        "_confirmed_owned_processes",
        lambda identities: {pid: created_at for pid, created_at in identities.items() if created_at >= 0},
    )
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))
    monkeypatch.setattr(memory_process, "_signal_owned_processes", signal_processes)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda _identities: dict(live))

    with pytest.raises(RuntimeError, match="orphaned sidecar did not exit"):
        asyncio.run(process._ownership.reap())

    assert group_signals == []
    assert signalled == [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)]
    assert record_path.exists()


def test_sidecar_launch_fails_closed_on_a_live_pid_it_cannot_describe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
    caplog,
) -> None:
    """A live pid that cannot be excluded is not the same thing as a gone one.

    Its record used to be retired on any unreadable identity, so a replacement
    sidecar started beside a process that may still have been serving the socket.
    No later attempt can clear this by itself, so the log has to name the pid that
    is blocking the launch and the record file that points at it -- ``last_error``
    alone only ever says ``memory_sidecar_unavailable``.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    signalled: list[object] = []
    spawns: list[tuple] = []

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    # Our uid and the recorded creation time, but the cmdline is withheld: nothing
    # observable rules this pid out as the sidecar the record names.
    monkeypatch.setattr(
        memory_process.psutil,
        "Process",
        _guarded_process_class(uid=os.getuid() if hasattr(os, "getuid") else None),
    )
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: signalled.append(object()))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    assert signalled == []
    assert spawns == []
    assert record_path.exists()
    assert "recorded sidecar identity could not be verified" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text
    assert str(record_path) in caplog.text
    # `logger.exception`, so the traceback reaches the log too.
    assert "Traceback (most recent call last)" in caplog.text


def test_sidecar_launch_proceeds_past_a_recycled_pid_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """Failing closed must not turn into a permanent brick.

    A pid recycled by another user's process is provably not our sidecar even when
    that process withholds its cmdline, so the record is retired and the launch
    continues rather than requiring manual intervention.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    signalled: list[object] = []
    spawns: list[tuple] = []
    foreign_uid = (os.getuid() if hasattr(os, "getuid") else 0) + 1

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    # The recorded creation time still matches, so only the foreign uid rules this
    # pid out — exactly the fact macOS and Linux both disclose for any process.
    monkeypatch.setattr(memory_process.psutil, "Process", _guarded_process_class(uid=foreign_uid))
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: signalled.append(object()))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    assert asyncio.run(process.start()) is False
    assert signalled == []
    # The launch reached the spawn, so an unreadable stranger cannot wedge startup.
    assert spawns
    assert not record_path.exists()


def test_sidecar_records_a_verified_launch_identity_privately(tmp_path: Path) -> None:
    """The record must be owner-only, and an unverifiable identity is not recorded."""

    process = _orphan_process(tmp_path)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)

    process._ownership.record_launch(_ORPHAN_PID, _ORPHAN_CREATE_TIME, _ORPHAN_PID)
    recorded = json.loads(process._ownership.record_path.read_text(encoding="utf-8"))

    assert recorded == _orphan_record(process)
    assert stat.S_IMODE(process._ownership.record_path.lstat().st_mode) == 0o600

    process._ownership.record_path.unlink()
    # An AccessDenied group member carries a negative sentinel instead of a
    # creation time. Recording it would produce a record nothing can match, and
    # skipping the write would launch a child no later boot can identify, so the
    # launch has to fail instead.
    with pytest.raises(RuntimeError, match="could not verify the sidecar creation time"):
        process._ownership.record_launch(_ORPHAN_PID, -1.0, _ORPHAN_PID)

    assert not process._ownership.record_path.exists()


def _group_member_process_class(disclosures: dict[int, dict]):
    """A ``psutil.Process`` stand-in for the members of a recorded process group.

    Each entry lists what the OS discloses about that pid; anything left out is
    withheld the way a real refusal is, and an unlisted pid is gone.
    """

    class _Member:
        def __init__(self, process_id: int) -> None:
            if process_id not in disclosures:
                raise psutil.NoSuchProcess(pid=process_id)
            self.pid = process_id
            self._facts = disclosures[process_id]

        def _disclosed(self, name: str):
            value = self._facts.get(name)
            if value is None:
                raise psutil.AccessDenied(pid=self.pid)
            return value

        def status(self) -> str:
            return psutil.STATUS_SLEEPING

        def create_time(self) -> float:
            return float(self._disclosed("create_time"))

        def cmdline(self) -> list[str]:
            return list(self._disclosed("cmdline"))

        def environ(self) -> dict[str, str]:
            return dict(self._disclosed("environ"))

        def uids(self):
            uid = self._disclosed("uid")
            return SimpleNamespace(real=uid, effective=uid, saved=uid)

    return _Member


def _own_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _recorded_group_disclosures(process: EverOSProcess) -> dict[int, dict]:
    """Four live members of a dead leader's group: two ours, two to leave alone."""

    return {
        # Re-exec'd sidecar entrypoint: its command line names our socket.
        _ORPHAN_DESCENDANT_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 1,
            "uid": _own_uid(),
            "cmdline": (sys.executable, "-m", _SIDECAR_ENTRYPOINT_MODULE, "--uds", str(process.socket_path)),
        },
        # A helper EverOS spawned: nothing in its command line, but it inherited
        # the provider root from the environment the launch handed the sidecar.
        _ORPHAN_GROUP_HELPER_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 2,
            "uid": _own_uid(),
            "environ": {"EVEROS_ROOT": str(process.provider_root), "HOME": "/tmp/child-home"},
        },
        # Same user, but nothing observable ties it to this installation.
        _FOREIGN_GROUP_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 3,
            "uid": _own_uid(),
            "cmdline": ("/bin/sleep", "600"),
            "environ": {"HOME": "/Users/someone"},
        },
        # Another user's process, which also withholds everything else.
        _FOREIGN_UID_GROUP_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 4,
            "uid": _own_uid() + 1,
        },
    }


def test_sidecar_launch_reaps_group_members_a_gone_leader_left_behind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog,
) -> None:
    """A leader that already exited still leaves its helpers holding the root.

    The reap only ran for a leader classified ``OURS``, which requires it to be
    alive. Once it had exited the record was deleted with no scan at all, so
    same-group helpers kept the provider root open while a replacement sidecar
    started -- the overlap this reap exists to prevent, reached the other way.

    The gone leader can no longer vouch for the group, so each member must tie
    itself to this installation. Members that cannot are logged and left running:
    they may belong to an unrelated process that took the recorded pid and led a
    group of the same number.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    disclosures = _recorded_group_disclosures(process)
    members = {pid: facts["create_time"] for pid, facts in disclosures.items()}
    live = dict(members)
    scanned_groups: list[int] = []
    group_signals: list[tuple[int, int]] = []
    signalled: list[dict[int, float]] = []

    def snapshot_group(group: int) -> dict[int, float]:
        scanned_groups.append(group)
        return dict(members)

    def signal_processes(identities, signum) -> None:
        del signum
        signalled.append(dict(identities))
        for process_id in identities:
            live.pop(process_id, None)

    # The recorded leader is confirmed gone; only its group is left to work from.
    monkeypatch.setattr(memory_process, "_inspect_process_identity", lambda _pid: None)
    monkeypatch.setattr(memory_process, "_snapshot_process_group", snapshot_group)
    monkeypatch.setattr(memory_process.psutil, "Process", _group_member_process_class(disclosures))
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))
    monkeypatch.setattr(memory_process, "_signal_owned_processes", signal_processes)
    monkeypatch.setattr(
        memory_process,
        "_live_owned_processes",
        lambda identities: {pid: live[pid] for pid in identities if pid in live},
    )

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._ownership.reap())

    # Discovery only ever looked at the group the record names.
    assert scanned_groups and set(scanned_groups) == {_ORPHAN_PID}
    # One SIGTERM round, carrying only the two members that identified themselves.
    assert signalled == [
        {
            _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
            _ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2,
        }
    ]
    # A group holding members Avibe cannot claim is never signalled group-wide.
    assert group_signals == []
    assert str(_FOREIGN_GROUP_PID) in caplog.text
    assert str(_FOREIGN_UID_GROUP_PID) in caplog.text
    # The unclaimed members are still running, and must not wedge startup.
    assert live == {
        _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 3,
        _FOREIGN_UID_GROUP_PID: _ORPHAN_CREATE_TIME + 4,
    }
    assert not record_path.exists()


def test_sidecar_launch_fails_closed_when_a_recorded_group_will_not_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
    caplog,
) -> None:
    """A surviving helper fails the launch, exactly as an unreapable orphan does.

    Nothing later can clear this by itself, so the log has to name the group and the
    record that points at it -- ``last_error`` only ever says
    ``memory_sidecar_unavailable``.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    disclosures = {
        _ORPHAN_GROUP_HELPER_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 2,
            "uid": _own_uid(),
            "environ": {"EVEROS_ROOT": str(process.provider_root)},
        }
    }
    members = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2}
    group_signals: list[tuple[int, int]] = []
    signalled: list[int] = []
    spawns: list[tuple] = []
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    monkeypatch.setattr(memory_process, "_inspect_process_identity", lambda _pid: None)
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: dict(members))
    monkeypatch.setattr(memory_process.psutil, "Process", _group_member_process_class(disclosures))
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))
    # The helper ignores every signal, so nothing proves the provider root is free.
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda _identities, signum: signalled.append(signum))
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda identities: dict(identities))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    assert spawns == []
    assert record_path.exists()
    # Every member of this group identified itself, so the group-wide signal is
    # allowed here -- unlike the mixed group above.
    assert group_signals == [(_ORPHAN_PID, signal.SIGTERM), (_ORPHAN_PID, kill_signal)]
    assert signalled == [signal.SIGTERM, kill_signal]
    assert "orphaned sidecar group did not exit" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text
    assert str(record_path) in caplog.text


def test_sidecar_launch_proceeds_when_a_gone_leader_left_an_empty_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """The common case: the whole tree died with the service, so only the record is left."""

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    record_path = _write_orphan_record(process, _orphan_record(process))
    signalled: list[object] = []
    spawns: list[tuple] = []

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    monkeypatch.setattr(memory_process, "_inspect_process_identity", lambda _pid: None)
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: {})
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: signalled.append(object()))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    assert asyncio.run(process.start()) is False

    assert signalled == []
    # The launch got past the orphan check rather than failing closed on nothing.
    assert spawns
    assert not record_path.exists()


@pytest.mark.parametrize("unscannable", ["record_from_an_older_build", "avibes_own_process_group"])
def test_sidecar_launch_never_scans_a_group_it_must_not_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unscannable: str,
) -> None:
    """Two records whose group must not be swept, for opposite reasons.

    A build that predates the group field leaves nothing but a dead leader's pid,
    which identifies no one. A record naming Avibe's own group cannot have been
    written by ``_isolated_process_group``, and sweeping it would signal Avibe.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record = _orphan_record(process)
    if unscannable == "record_from_an_older_build":
        del record["process_group"]
    else:
        record["process_group"] = os.getpgrp()
    record_path = _write_orphan_record(process, record)
    scanned_groups: list[int] = []
    signalled: list[object] = []

    def snapshot_group(group: int) -> dict[int, float]:
        scanned_groups.append(group)
        return {}

    monkeypatch.setattr(memory_process, "_inspect_process_identity", lambda _pid: None)
    monkeypatch.setattr(memory_process, "_snapshot_process_group", snapshot_group)
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: signalled.append(object()))
    monkeypatch.setattr(memory_process.os, "killpg", lambda *_args: signalled.append(object()))

    asyncio.run(process._ownership.reap())

    assert scanned_groups == []
    assert signalled == []
    assert not record_path.exists()


async def _succeed(*_args, **_kwargs) -> None:
    return None


def test_sidecar_start_fails_when_ownership_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """An unrecordable launch is a failed launch, and its child is reaped.

    A swallowed write failure left a running sidecar that no record pointed at, so
    a later crash produced an orphan the next boot could not see -- and that boot
    started a replacement on the same provider root. ``_start_locked`` already
    fails when in-memory ownership cannot be established; persisted ownership
    follows the same rule, which also hands the child to its cleanup path.
    """

    class _Child:
        pid = 999_999
        returncode = None

        async def wait(self) -> None:
            return None

        def send_signal(self, _signum) -> None:
            return None

    launches: list[_Child] = []
    reaped: list[_Child] = []

    async def spawn(*_args, **_kwargs) -> _Child:
        child = _Child()
        launches.append(child)
        return child

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    write_private_text = memory_process._write_private_text

    def refuse_the_record(path: Path, contents: str) -> None:
        if path == process._ownership.record_path:
            raise OSError("record could not be written")
        write_private_text(path, contents)

    terminate_owned_tree = process._terminate_owned_tree

    async def terminate(child, **kwargs) -> None:
        reaped.append(child)
        await terminate_owned_tree(child, **kwargs)

    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(memory_process, "_write_private_text", refuse_the_record)
    monkeypatch.setattr(memory_process, "_snapshot_owned_processes", lambda pid, _group: {pid: _ORPHAN_CREATE_TIME})
    monkeypatch.setattr(memory_process, "_owned_process_identity_is_live", lambda *_args: True)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda _identities: {})
    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)
    # Everything after the record succeeds, so the unwritten record is the only
    # thing that can fail this launch. Without these the start would fail on the
    # absent socket instead, and the test would hold whether or not the record
    # failure is respected.
    monkeypatch.setattr(process, "_wait_for_ready", _succeed)
    monkeypatch.setattr(process, "_secure_socket", lambda: None)
    monkeypatch.setattr(process, "_assert_no_tcp_listener", lambda *_args, **_kwargs: None)

    assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    # The child was already tracked when the record failed, so the start failure
    # reaped the tree it had just spawned instead of leaking it.
    assert len(launches) == 1
    assert reaped == launches
    assert process._process is None
    assert not process._ownership.record_path.exists()


_UNUSABLE_RECORDS: dict[str, bytes] = {
    "truncated": b'{"pid": 424242, "create_ti',
    "not_json": b"\x00\x01 not a record at all",
    "oversized": b"{}" + b" " * (5 * 1024),
    "no_pid": b'{"create_time": 1700000000.5}',
}


def _sidecar_scan_disclosures(process: EverOSProcess) -> dict[int, dict]:
    """One process that is our sidecar, and three that only look like it."""

    return {
        # Our entrypoint, serving our socket: the anchor.
        _ORPHAN_PID: {
            "create_time": _ORPHAN_CREATE_TIME,
            "uid": _own_uid(),
            "cmdline": (sys.executable, "-m", _SIDECAR_ENTRYPOINT_MODULE, "--uds", str(process.socket_path)),
            "environ": {"EVEROS_ROOT": str(process.provider_root)},
        },
        # A helper in the anchor's group, claimed through group membership.
        _ORPHAN_GROUP_HELPER_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 2,
            "uid": _own_uid(),
            "environ": {"EVEROS_ROOT": str(process.provider_root)},
        },
        # The short-lived processing probe. It carries the same environment, which
        # is exactly why the machine-wide test may not accept an environment match.
        _FOREIGN_GROUP_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 3,
            "uid": _own_uid(),
            "cmdline": (sys.executable, "-m", _SIDECAR_ENTRYPOINT_MODULE, "--probe-processing"),
            "environ": {"EVEROS_ROOT": str(process.provider_root)},
        },
        # Somebody looking at the provider root. Naming a path is not owning it.
        _FOREIGN_UID_GROUP_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 4,
            "uid": _own_uid(),
            "cmdline": ("/bin/ls", "-l", str(process.provider_root)),
        },
    }


def test_sidecar_launch_reaps_a_live_sidecar_an_unusable_record_cannot_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog,
) -> None:
    """A record that exists but cannot be parsed is not the same as no record.

    Both used to read as ``None`` and retire the file, so a truncated or unreadable
    record discarded the only ownership evidence and let a replacement launch
    against a socket and provider root the previous run's sidecar may still have
    been holding. Ownership is rebuilt from live processes instead, which needs no
    record: our entrypoint serving our socket, plus that anchor's own group.

    Failing closed on the corrupt file instead would have been unrecoverable --
    nothing repairs it, so every later start would fail forever.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record_path = process._ownership.record_path
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(_UNUSABLE_RECORDS["truncated"])
    disclosures = _sidecar_scan_disclosures(process)
    stub = _group_member_process_class(disclosures)
    group_members = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2,
    }
    live = dict(group_members)
    group_signals: list[tuple[int, int]] = []
    signalled: list[dict[int, float]] = []

    def signal_processes(identities, signum) -> None:
        del signum
        signalled.append(dict(identities))
        for pid in identities:
            live.pop(pid, None)

    monkeypatch.setattr(memory_process.psutil, "Process", stub)
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [stub(pid) for pid in disclosures])
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))
    # The sidecar leads a session of its own, so its helpers share its group.
    monkeypatch.setattr(memory_process, "_isolated_process_group", lambda pid: pid)
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: dict(group_members))
    monkeypatch.setattr(memory_process, "_signal_owned_processes", signal_processes)
    monkeypatch.setattr(
        memory_process,
        "_live_owned_processes",
        lambda identities: {pid: live[pid] for pid in identities if pid in live},
    )

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._ownership.reap())

    # The anchor and its group helper, and neither look-alike.
    assert signalled == [group_members]
    # Every member of the anchor's group is claimed, so the group signal is allowed.
    assert group_signals == [(_ORPHAN_PID, signal.SIGTERM)]
    assert str(_FOREIGN_GROUP_PID) not in caplog.text
    assert str(_FOREIGN_UID_GROUP_PID) not in caplog.text
    assert not record_path.exists()


@pytest.mark.parametrize("corruption", sorted(_UNUSABLE_RECORDS))
def test_sidecar_launch_proceeds_when_an_unusable_record_names_nothing_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
    corruption: str,
) -> None:
    """An unusable record must not brick startup when nothing of ours survives."""

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    record_path = process._ownership.record_path
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(_UNUSABLE_RECORDS[corruption])
    disclosures = _sidecar_scan_disclosures(process)
    # Only the look-alikes are running; the sidecar itself is gone.
    running = {pid: facts for pid, facts in disclosures.items() if pid != _ORPHAN_PID}
    stub = _group_member_process_class(running)
    signalled: list[object] = []
    spawns: list[tuple] = []

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    monkeypatch.setattr(memory_process.psutil, "Process", stub)
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [stub(pid) for pid in running])
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: signalled.append(object()))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    assert asyncio.run(process.start()) is False

    assert signalled == []
    # The launch got past the record check rather than failing closed on a file.
    assert spawns
    assert not record_path.exists()


def test_sidecar_launch_scans_for_processes_only_when_a_record_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """The ordinary first boot must not pay for a machine-wide process scan."""

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    scans: list[object] = []
    spawns: list[tuple] = []

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: scans.append(object()) or [])
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    assert not process._ownership.record_path.exists()
    assert asyncio.run(process.start()) is False

    assert scans == []
    assert spawns


def test_sidecar_launch_fails_closed_when_an_unusable_record_names_a_live_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    short_socket_path: Path,
    caplog,
) -> None:
    """A sidecar that will not exit fails the launch and keeps the record.

    The record is unusable, so the log is the only thing that can point an operator
    at what is blocking the start.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1, socket_path=short_socket_path)
    record_path = process._ownership.record_path
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(_UNUSABLE_RECORDS["truncated"])
    disclosures = {_ORPHAN_PID: _sidecar_scan_disclosures(process)[_ORPHAN_PID]}
    stub = _group_member_process_class(disclosures)
    spawns: list[tuple] = []

    async def spawn(*args, **_kwargs):
        spawns.append(args)
        raise OSError("stop before a real sidecar is spawned")

    monkeypatch.setattr(memory_process.psutil, "Process", stub)
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [stub(_ORPHAN_PID)])
    monkeypatch.setattr(memory_process, "_isolated_process_group", lambda _pid: None)
    # The sidecar ignores every signal.
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: None)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda identities: dict(identities))
    monkeypatch.setattr("core.memory.process.asyncio.create_subprocess_exec", spawn)

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    assert spawns == []
    assert record_path.exists()
    assert "sidecar left by an unusable record did not exit" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text
    assert str(record_path) in caplog.text


class _ExitedChild:
    """A direct child that has already exited, as ``_watch_child`` finds it."""

    pid = _ORPHAN_PID
    returncode = 0

    async def wait(self) -> None:
        return None

    def send_signal(self, _signum) -> None:
        raise AssertionError("a child that already exited must not be signalled")


def _supervising(process: EverOSProcess, child: _ExitedChild) -> None:
    """Put the supervisor in the state a running sidecar leaves behind."""

    process._process = child
    process._process_group = _ORPHAN_PID
    process._owned_processes = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}


def _late_helper_disclosures(process: EverOSProcess) -> dict[int, dict]:
    """One helper the sidecar spawned after the monitor's last snapshot.

    The recorded leader is deliberately absent, so ``psutil`` answers
    ``NoSuchProcess`` for it exactly as it does for a child that has exited.
    """

    return {
        _ORPHAN_GROUP_HELPER_PID: {
            "create_time": _ORPHAN_CREATE_TIME + 2,
            "uid": _own_uid(),
            "environ": {"EVEROS_ROOT": str(process.provider_root)},
        }
    }


def test_sidecar_notifies_reaped_callback_only_after_tree_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The runtime handoff begins after the exited child's tree is reaped."""

    events: list[str] = []

    async def on_reaped() -> None:
        assert process._process is None
        events.append("reaped")

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        on_reaped=on_reaped,
    )
    child = _ExitedChild()
    _supervising(process, child)
    process._desired_running = False

    async def terminate(*_args, **_kwargs) -> None:
        events.append("tree-cleaned")

    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)
    monkeypatch.setattr(process._ownership, "retire_if_group_is_clear", lambda _group: events.append("retired"))

    asyncio.run(process._watch_child(child))

    assert events == ["tree-cleaned", "retired", "reaped"]


def test_sidecar_start_failure_after_host_handoff_notifies_reaped(tmp_path: Path) -> None:
    """A pre-spawn launch failure leaves the host free to reclaim the call log."""

    reaped = 0

    async def on_reaped() -> None:
        nonlocal reaped
        reaped += 1

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        on_reaped=on_reaped,
    )
    process._consecutive_failures = 4
    process._validate_launch_inputs = lambda: (_ for _ in ()).throw(RuntimeError("launch rejected"))

    assert asyncio.run(process.start()) is False
    assert reaped == 1
    assert process._restart_task is None


def test_sidecar_restart_releases_process_lock_while_host_handoff_waits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stop wins while a restart waits for the controller-owned call-log lock."""

    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    launches = 0

    async def before_start() -> None:
        callback_entered.set()
        await release_callback.wait()

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        before_start=before_start,
    )
    process._desired_running = True

    async def start_locked() -> bool:
        nonlocal launches
        launches += 1
        return True

    monkeypatch.setattr(process, "_start_locked", start_locked)

    async def run() -> None:
        restarting = asyncio.create_task(process._restart_after(0))
        await asyncio.wait_for(callback_entered.wait(), timeout=1)

        # This models a callback already in flight when Stop begins. It must
        # not retain the process lock while waiting on controller ownership.
        await asyncio.wait_for(process.stop(), timeout=1)
        release_callback.set()
        await asyncio.wait_for(restarting, timeout=1)

    asyncio.run(run())
    assert launches == 0


def test_fake_sidecar_failed_start_notifies_reaped_for_runtime_handoff() -> None:
    reaped = 0

    async def on_reaped() -> None:
        nonlocal reaped
        reaped += 1

    process = FakeEverOSProcess(
        start_results=deque([False]),
        on_reaped=on_reaped,
    )

    assert asyncio.run(process.start()) is False
    assert reaped == 1


@pytest.mark.parametrize("group_holds_a_survivor", [True, False])
def test_sidecar_cleanup_retires_the_record_only_once_its_group_is_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog,
    group_holds_a_survivor: bool,
) -> None:
    """A reaped leader does not prove its group is empty.

    When the sidecar spawns a helper after the monitor's last snapshot and then
    exits, that helper is in none of the captured identities. Rediscovery is
    anchored on a live leader, the group signal is refused because the unknown
    member cannot be confirmed, and the wait then succeeds over the identities it
    does hold. Retiring the record on that evidence threw away the next launch's
    only route to the survivor -- the recorded group -- so a replacement sidecar
    came up beside it on the same provider root.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    disclosures = _late_helper_disclosures(process) if group_holds_a_survivor else {}
    survivors = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2} if group_holds_a_survivor else {}
    group_signals: list[tuple[int, int]] = []
    child = _ExitedChild()
    _supervising(process, child)
    process._desired_running = False

    monkeypatch.setattr(memory_process.psutil, "Process", _group_member_process_class(disclosures))
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: dict(survivors))
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._watch_child(child))

    # The cleanup itself is unchanged: it never signals a group holding a member
    # it cannot confirm, and it finishes rather than failing.
    assert group_signals == []
    assert process._process is None
    assert record_path.exists() is group_holds_a_survivor
    if group_holds_a_survivor:
        assert "Keeping the EverOS ownership record" in caplog.text
        assert str(_ORPHAN_GROUP_HELPER_PID) in caplog.text
        assert str(record_path) in caplog.text


def test_sidecar_stop_keeps_the_record_while_its_group_holds_a_survivor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stop retires the record on the same evidence, so it needs the same guard.

    Nothing sweeps here -- there is no launch to fail -- so the record is what
    carries the survivor to the next one.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    child = _ExitedChild()
    _supervising(process, child)

    monkeypatch.setattr(memory_process.psutil, "Process", _group_member_process_class(_late_helper_disclosures(process)))
    monkeypatch.setattr(
        memory_process,
        "_snapshot_process_group",
        lambda _group: {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2},
    )
    monkeypatch.setattr(memory_process.os, "killpg", lambda *_args: None)

    asyncio.run(process.stop())

    assert process._process is None
    assert record_path.exists()


def test_sidecar_orphan_reap_sweeps_the_group_before_retiring_the_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reaping the recorded root leaves the same gap as reaping a live child.

    ``_terminate_orphan_tree`` rediscovers only while the root is alive, so a
    helper spawned in its last moments is proven by nothing. With the root now
    gone, the leader-gone sweep is the follow-up, and a group it cannot clear
    fails the launch instead of retiring the record and spawning beside it.
    """

    process = _orphan_process(tmp_path, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    survivors = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2}
    group_signals: list[tuple[int, int]] = []

    async def reaped(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(
        memory_process,
        "_inspect_process_identity",
        lambda _pid: _orphan_identity(process),
    )
    monkeypatch.setattr(process._ownership, "_terminate_orphan_tree", reaped)
    monkeypatch.setattr(memory_process.psutil, "Process", _group_member_process_class(_late_helper_disclosures(process)))
    monkeypatch.setattr(memory_process, "_snapshot_process_group", lambda _group: dict(survivors))
    monkeypatch.setattr(memory_process.os, "killpg", lambda group, signum: group_signals.append((group, signum)))
    # The survivor ignores every signal.
    monkeypatch.setattr(memory_process, "_signal_owned_processes", lambda *_args: None)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda identities: dict(identities))

    with pytest.raises(RuntimeError, match="orphaned sidecar group did not exit"):
        asyncio.run(process._ownership.reap())

    assert record_path.exists()
    # Every member of that group is claimed, so the group signal is allowed here.
    assert group_signals == [(_ORPHAN_PID, signal.SIGTERM), (_ORPHAN_PID, getattr(signal, "SIGKILL", signal.SIGTERM))]

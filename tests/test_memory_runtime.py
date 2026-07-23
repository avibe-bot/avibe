from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from core.managed_runtime import ManagedRuntimeArchive, ManagedRuntimeManifest
import core.memory.artifact as memory_artifact
from core.memory.artifact import (
    MemoryArtifactCandidate,
    MemoryArtifactManager,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
)
import core.memory.process as memory_process
from core.memory.process import (
    EverOSProcess,
    EverOSProcessSettings,
    _live_owned_processes,
    _signal_owned_group_or_process,
    _signal_owned_processes,
    _snapshot_owned_processes,
)
from core.memory.runtime import MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.types import OperationFailed
from config.v2_config import (
    AgentsConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)


def _settings() -> EverOSProcessSettings:
    return EverOSProcessSettings(
        llm_base_url="https://llm.example.test/v1",
        llm_model="chat",
        llm_api_key="llm-secret",
        embedding_base_url="https://embed.example.test/v1",
        embedding_model="embed",
        embedding_api_key="embedding-secret",
    )


def test_memory_artifact_uses_shared_manager_status_shape(tmp_path: Path) -> None:
    manager = MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True)

    status = manager.status()

    assert status["id"] == "memory-runtime"
    assert status["status"] == "missing"
    assert status["reason"] == "memory_runtime_unpublished"
    assert manager.provider_root_format() is None
    assert manager.artifact_fingerprint() is None


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
        return subprocess.CompletedProcess(command, 0, stdout="1.1.3\n", stderr="")

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
    assert manager.provider_root_format() == "everos-1.1.3"
    assert manager.compatible_provider_root_formats() == frozenset({"everos-1.1.3"})
    assert manager.artifact_fingerprint() == "dev-everos-1.1.3"
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
        return subprocess.CompletedProcess(command, 0, stdout=memory_artifact.EVEROS_VERSION, stderr="")

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
        "runtime_version": "1.0",
        "platform": "darwin-arm64",
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
        "runtime_version": "1.0",
        "platform": "darwin-arm64",
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
        "runtime_version": "1.0",
        "platform": "darwin-arm64",
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
                "runtime_version": "1.0",
                "platform": "darwin-arm64",
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

    started_python: list[Path] = []

    class _Process:
        starting = False
        running = True

        def __init__(self, python: Path, *, on_ready=None, **_kwargs) -> None:
            self.python = python
            self._on_ready = on_ready

        async def processing_healthy(self) -> bool:
            return True

        async def start(self) -> bool:
            assert self._on_ready is not None
            started_python.append(self.python)
            await self._on_ready()
            return True

        async def stop(self) -> None:
            self.running = False

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)

    async def run() -> None:
        runtime = MemoryRuntime(
            MemoryConfig(enabled=True, processing=processing),
            artifact_manager=manager,
            effective_home=tmp_path,
        )
        assert (await runtime.reconcile(runtime._config))["ok"] is True
        assert started_python == [old_binary]
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


def test_sidecar_child_environment_is_allowlisted_and_generated_config_has_no_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/override.pem")
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        owner_id="owner-1",
        settings=_settings(),
    )
    process._prepare_owned_directories()
    process._write_generated_config()
    environment = process._child_environment()
    generated = (tmp_path / "memory" / "generated" / "everos.toml").read_text(encoding="utf-8")

    assert environment["EVEROS_LLM__API_KEY"] == "llm-secret"
    assert environment["EVEROS_EMBEDDING__API_KEY"] == "embedding-secret"
    assert "HTTP_PROXY" not in environment
    assert "SSL_CERT_FILE" not in environment
    assert "llm-secret" not in generated
    assert "embedding-secret" not in generated
    assert "rerank" in generated


def test_sidecar_rejects_sun_path_overflow_without_launching_child(tmp_path: Path) -> None:
    socket_path = tmp_path / ("a" * 180) / "everos.sock"
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        socket_path=socket_path,
        owner_id="owner-1",
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
        owner_id="owner-1",
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
        owner_id="owner-1",
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
        process = EverOSProcess(sys.executable, effective_home=tmp_path, owner_id="owner-1", settings=_settings())
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
        process = EverOSProcess(sys.executable, effective_home=tmp_path, owner_id="owner-1", settings=_settings())
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

    process = EverOSProcess(sys.executable, effective_home=tmp_path, owner_id="owner-1", settings=_settings())
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
    process = EverOSProcess(sys.executable, effective_home=tmp_path, owner_id="owner-1", settings=_settings())
    process._consecutive_failures = 4

    process._record_health_observation(True, observed_at=10.0)
    process._record_health_observation(False, observed_at=310.0)
    process._record_health_observation(True, observed_at=311.0)
    process._record_health_observation(True, observed_at=610.0)

    assert process.consecutive_failures == 4

    process._record_health_observation(True, observed_at=611.0)

    assert process.consecutive_failures == 0


def test_explicit_sidecar_retry_keeps_crash_budget_until_observed_health(monkeypatch, tmp_path: Path) -> None:
    process = EverOSProcess(sys.executable, effective_home=tmp_path, owner_id="owner-1", settings=_settings())
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

    class _Artifact:
        def resolve_python(self) -> Path:
            return Path(sys.executable)

        def provider_root_format(self) -> str:
            return "everos-1.1.3"

        def artifact_fingerprint(self) -> str:
            return "test-artifact"

        def status(self) -> dict:
            return {"reason": None}

    class _Process:
        starting = False

        def __init__(self, *_args, **_kwargs) -> None:
            started.append(object())

        async def processing_healthy(self) -> bool:
            return True

        async def start(self) -> bool:
            return True

        async def stop(self) -> None:
            return None

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, processing=processing),
        artifact_manager=_Artifact(),
        effective_home=tmp_path,
    )

    async def interrupted_clear() -> OperationFailed:
        return OperationFailed(error="memory_clear_failed")

    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)
    monkeypatch.setattr(runtime.module, "_recover_interrupted_clear", interrupted_clear)

    async def run() -> None:
        assert await runtime.reconcile(runtime._config) == {"ok": False, "error": "memory_clear_failed"}

    asyncio.run(run())
    assert started == []


def test_runtime_install_artifact_uses_controller_owned_manager(tmp_path: Path) -> None:
    calls: list[bool] = []

    class _Artifact:
        def __init__(self) -> None:
            self.activation_coordinator = None

        def set_provider_root(self, _provider_root: Path) -> None:
            return None

        def set_activation_coordinator(self, coordinator) -> None:
            self.activation_coordinator = coordinator

        def provider_root_format(self) -> None:
            return None

        def artifact_fingerprint(self) -> None:
            return None

        def compatible_provider_root_formats(self) -> frozenset[str]:
            return frozenset()

        def ensure(self, *, force: bool) -> dict:
            calls.append(force)
            return {"ok": False, "reason": "memory_runtime_unpublished", "download_error": None}

    artifact = _Artifact()
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


def test_runtime_repair_stops_retained_down_supervisor_before_replacing_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    events: list[str] = []

    class _Artifact:
        def provider_root_format(self) -> None:
            return None

        def artifact_fingerprint(self) -> None:
            return None

        def ensure(self, *, force: bool) -> dict:
            assert force is True
            assert runtime._process is None
            events.append("ensure")
            return {"ok": True}

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
        artifact_manager=_Artifact(),
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
    live sidecar requires a coordinated disable first (tech §12.2).
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    events: list[str] = []

    class _Artifact:
        def provider_root_format(self) -> None:
            return None

        def artifact_fingerprint(self) -> None:
            return None

        def ensure(self, *, force: bool) -> dict:
            events.append("ensure")
            return {"ok": True}

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
        artifact_manager=_Artifact(),
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
    class _Loop:
        def is_closed(self) -> bool:
            return False

    class _Future:
        def __init__(self) -> None:
            self.cancelled = False
            self.timeouts: list[float | None] = []

        def cancel(self) -> bool:
            self.cancelled = True
            return True

        def result(self, timeout: float | None = None) -> None:
            self.timeouts.append(timeout)
            if timeout is not None:
                raise concurrent.futures.TimeoutError()
            raise concurrent.futures.CancelledError()

    future = _Future()

    def submit(coroutine, _loop):
        coroutine.close()
        return future

    runtime = MemoryRuntime(
        MemoryConfig(enabled=False),
        artifact_manager=MemoryArtifactManager(runtime_dir=tmp_path / "runtime", offline=True),
        effective_home=tmp_path,
    )
    runtime._activation_loop = _Loop()  # type: ignore[assignment]
    monkeypatch.setattr("core.memory.runtime.asyncio.run_coroutine_threadsafe", submit)

    with pytest.raises(MemoryRuntimeActivationError, match="timed out"):
        runtime._coordinate_artifact_activation(
            MemoryArtifactCandidate(
                provider_root_format="everos-1.1.3",
                compatible_provider_root_formats=frozenset({"everos-1.1.3"}),
                artifact_fingerprint="candidate-artifact",
            ),
            MemoryProviderRootState(exists=False),
            lambda: None,
            lambda: None,
        )

    assert future.cancelled is True
    assert future.timeouts == [90.0, None]


def test_runtime_rejects_embedding_change_when_root_inspection_fails_under_lifecycle_lock(monkeypatch, tmp_path: Path) -> None:
    instances: list[object] = []

    class _Artifact:
        def resolve_python(self) -> Path:
            return Path(sys.executable)

        def provider_root_format(self) -> str:
            return "everos-1.1.3"

        def artifact_fingerprint(self) -> str:
            return "test-artifact"

        def status(self) -> dict:
            return {"reason": None}

    class _Process:
        starting = False
        running = True

        def __init__(self, _python, *, on_ready=None, **_kwargs) -> None:
            self.stopped = False
            self._on_ready = on_ready
            if on_ready is not None:
                instances.append(self)

        async def processing_healthy(self) -> bool:
            return True

        async def start(self) -> bool:
            assert self._on_ready is not None
            await self._on_ready()
            return True

        async def stop(self) -> None:
            self.stopped = True
            self.running = False

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)

    async def run() -> None:
        runtime = MemoryRuntime(initial, artifact_manager=_Artifact(), effective_home=tmp_path)
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

    class _Artifact:
        def provider_root_format(self) -> str:
            return "everos-1.1.3"

        def artifact_fingerprint(self) -> str:
            return "test-artifact"

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

    async def run() -> None:
        runtime = MemoryRuntime(restarted, artifact_manager=_Artifact(), effective_home=tmp_path)

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


def test_runtime_settles_embedding_candidate_before_resuming_claims(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    class _Artifact:
        def resolve_python(self) -> Path:
            return Path(sys.executable)

        def provider_root_format(self) -> str:
            return "everos-1.1.3"

        def artifact_fingerprint(self) -> str:
            return "test-artifact"

        def status(self) -> dict:
            return {"reason": None}

    observed_before_ready: list[tuple[bool, bool]] = []
    runtime: MemoryRuntime | None = None

    class _Process:
        starting = False
        running = True

        def __init__(self, _python, *, on_ready=None, **_kwargs) -> None:
            self._on_ready = on_ready

        async def processing_healthy(self) -> bool:
            return True

        async def start(self) -> bool:
            assert runtime is not None
            assert self._on_ready is not None
            observed_before_ready.append(
                (
                    V2Config.load().memory.embedding_change_pending,
                    runtime.module._worker._claims_paused,
                )
            )
            await self._on_ready()
            return True

        async def stop(self) -> None:
            self.running = False

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
    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)

    async def run() -> None:
        nonlocal runtime
        runtime = MemoryRuntime(restarted, artifact_manager=_Artifact(), effective_home=tmp_path)
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
    active = {
        "format": "everos-1.0",
        "fingerprint": "old-artifact",
        "compatible": frozenset({"everos-1.0"}),
    }
    lifecycle: list[str] = []

    class _Artifact:
        def resolve_python(self) -> Path:
            return Path(sys.executable)

        def provider_root_format(self) -> str:
            return active["format"]

        def compatible_provider_root_formats(self) -> frozenset[str]:
            return active["compatible"]

        def artifact_fingerprint(self) -> str:
            return active["fingerprint"]

        def status(self) -> dict:
            return {"reason": None}

    class _Process:
        starting = False
        running = True

        def __init__(self, _python, *, on_ready=None, **_kwargs) -> None:
            self.stopped = False
            self._on_ready = on_ready
            if on_ready is None:
                self.index = -1
            else:
                self.index = len(instances)
                instances.append(self)

        async def processing_healthy(self) -> bool:
            return True

        async def start(self) -> bool:
            if self.index == 1:
                return False
            assert self._on_ready is not None
            await self._on_ready()
            return True

        async def stop(self) -> None:
            self.stopped = True
            self.running = False

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)

    async def run() -> None:
        runtime = MemoryRuntime(
            initial,
            store=MemoryStore(),
            artifact_manager=_Artifact(),
        )
        assert (await runtime.reconcile(initial))["ok"] is True
        candidate = MemoryArtifactCandidate(
            provider_root_format="everos-2.0",
            compatible_provider_root_formats=frozenset({"everos-1.0", "everos-2.0"}),
            artifact_fingerprint="candidate-artifact",
        )

        def commit() -> None:
            lifecycle.append("commit")
            active.update(
                {
                    "format": "everos-2.0",
                    "fingerprint": "candidate-artifact",
                    "compatible": frozenset({"everos-1.0", "everos-2.0"}),
                }
            )

        def rollback() -> None:
            lifecycle.append("rollback")
            active.update(
                {
                    "format": "everos-1.0",
                    "fingerprint": "old-artifact",
                    "compatible": frozenset({"everos-1.0"}),
                }
            )

        with pytest.raises(MemoryRuntimeActivationError):
            await runtime._activate_artifact_candidate(
                candidate,
                MemoryProviderRootState(exists=True, provider_root_format="everos-1.0", empty=True),
                commit,
                rollback,
            )

        sentinel = json.loads((tmp_path / "memory" / "everos-root" / ".avibe-memory-root.json").read_text())
        assert lifecycle == ["commit", "rollback"]
        assert sentinel["provider_root_format"] == "everos-1.0"
        assert sentinel["created_by_artifact_fingerprint"] == "old-artifact"
        assert active["format"] == "everos-1.0"
        assert len(instances) == 3
        assert instances[0].stopped is True
        assert instances[1].stopped is True
        assert instances[2].stopped is False
        assert runtime.module._worker._claims_paused is False
        await runtime.close()

    asyncio.run(run())


def test_runtime_reconciliation_restarts_sidecar_with_fresh_child_settings(monkeypatch) -> None:
    instances: list[object] = []

    class _Artifact:
        def resolve_python(self) -> Path:
            return Path(sys.executable)

        def provider_root_format(self) -> str:
            return "everos-1.1.3"

        def artifact_fingerprint(self) -> str:
            return "test-artifact"

        def status(self) -> dict:
            return {"reason": None}

    class _Process:
        starting = False

        def __init__(self, _python, *, provider_root, on_ready=None, **_kwargs) -> None:
            self.provider_root = provider_root
            self.stopped = False
            self._on_ready = on_ready
            if on_ready is not None:
                instances.append(self)

        async def processing_healthy(self) -> bool:
            return True

        async def start(self) -> bool:
            assert self._on_ready is not None
            await self._on_ready()
            return True

        async def stop(self) -> None:
            self.stopped = True

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)

    async def run() -> None:
        runtime = MemoryRuntime(initial, artifact_manager=_Artifact())
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

    class _Artifact:
        def resolve_python(self) -> Path:
            return Path(sys.executable)

        def provider_root_format(self) -> str:
            return "everos-1.1.3"

        def artifact_fingerprint(self) -> str:
            return "test-artifact"

        def status(self) -> dict:
            return {"reason": None}

    class _Process:
        starting = False
        running = True

        def __init__(self, _python, *, on_ready=None, settings, **_kwargs) -> None:
            self.stopped = False
            self._on_ready = on_ready
            self._settings = settings
            if on_ready is not None:
                instances.append(self)

        async def processing_healthy(self) -> bool:
            return self._settings.llm_model != "unhealthy"

        async def start(self) -> bool:
            assert self._on_ready is not None
            await self._on_ready()
            return True

        async def stop(self) -> None:
            self.stopped = True
            self.running = False

    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig("https://embed.example.test/v1", "embed", "embed-key"),
    )
    initial = MemoryConfig(enabled=True, processing=processing)
    monkeypatch.setattr("core.memory.runtime.EverOSProcess", _Process)

    async def run() -> None:
        runtime = MemoryRuntime(initial, artifact_manager=_Artifact())
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


def test_generated_timezone_stays_with_existing_provider_root(tmp_path: Path) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        owner_id="owner-1",
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
        runtime_version="1.1.3",
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

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from core.managed_runtime import ManagedRuntimeArchive, ManagedRuntimeManifest
from core.memory.artifact import (
    MemoryArtifactCandidate,
    MemoryArtifactManager,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
)
from core.memory.process import EverOSProcess, EverOSProcessSettings, _snapshot_owned_processes
from core.memory.runtime import MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.types import OperationFailed
from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig


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

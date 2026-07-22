from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import psutil

from core.memory.artifact import MemoryArtifactManager
from core.memory.process import EverOSProcess, EverOSProcessSettings
from core.memory.runtime import MemoryRuntime
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
    assert manager.provider_root_format() == "everos-1.1.3"
    assert manager.artifact_fingerprint()


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
        await process._terminate_owned_tree(child, process_group=process._process_group)
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
        await process._terminate_owned_tree(child, process_group=process._process_group)
        return child.pid, descendant_pid

    parent_pid, descendant_pid = asyncio.run(run())
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(descendant_pid)


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

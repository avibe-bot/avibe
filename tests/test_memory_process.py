from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from config import paths
from config.v2_config import (
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
)
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.attachments import AttachmentPinStore, attachment_pin_root
from core.memory.everos import (
    EverOSPort,
    MULTIMODAL_EXPLICIT_ENV,
    ProviderCapture,
)
import core.memory.process as memory_process
import core.memory.runtime as memory_runtime
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
from core.memory.process import (
    _SIDECAR_ENTRYPOINT_MODULE,
    _ProcessHost,
    _SystemProcessHost,
    EverOSProcess,
    EverOSProcessSettings,
    EverOSRebuildProcess,
    FakeEverOSProcess,
    FakeEverOSProcessFactory,
    RebuildProcessResult,
    _MemoryChildRole,
    _ProcessKind,
    _ProcessIdentity,
    _RecordedSidecar,
    _classify_recorded_child,
    _classify_recorded_sidecar,
    _processes_rebuilding_owned_root,
    _processes_syncing_owned_root,
    _REBUILD_TIMEOUT_SECONDS,
)
from core.memory.sync_process import SYNC_NONCE_ENV, SYNC_ROLE, SYNC_ARGV
from core.memory.sidecar import _request_rejection
from core.memory.types import (
    CaptureAttachment,
    ProviderSessionRef,
)


PROJECT = "default"
_ORPHAN_PID = 424_242
_ORPHAN_DESCENDANT_PID = 424_243
_ORPHAN_GROUP_MEMBER_PID = 424_244
_ORPHAN_GROUP_HELPER_PID = 424_245
_FOREIGN_GROUP_PID = 424_246
_FOREIGN_UID_GROUP_PID = 424_247
_ORPHAN_CREATE_TIME = 1_700_000_000.5


@dataclass
class _FakeProcessHost:
    """Deterministic in-memory adapter for process supervision tests."""

    spawns: deque[object] = field(default_factory=deque)
    process_groups: dict[int, int | None] = field(default_factory=dict)
    identities: dict[int, _ProcessIdentity | None] = field(default_factory=dict)
    trees: dict[tuple[int, int | None], dict[int, float]] = field(default_factory=dict)
    groups: dict[int, tuple[dict[int, float], list[int]]] = field(default_factory=dict)
    sidecars: dict[int, float] = field(default_factory=dict)
    root_sidecars: dict[int, float] = field(default_factory=dict)
    rebuilds: dict[int, float] = field(default_factory=dict)
    live_processes: dict[int, float] = field(default_factory=dict)
    listeners: set[int] = field(default_factory=set)
    wait_results: deque[bool] = field(default_factory=deque)
    remove_on_signal: bool = True
    signal_effect: Callable[[Mapping[int, float], int], None] | None = None
    spawn_calls: list[tuple[_ProcessKind, Path, Path, Path | None, dict[str, str]]] = field(default_factory=list)
    snapshot_calls: list[tuple[int, int | None]] = field(default_factory=list)
    group_scans: list[int] = field(default_factory=list)
    sidecar_scans: list[Path] = field(default_factory=list)
    signal_calls: list[tuple[dict[int, float], int, int | None, int | None]] = field(default_factory=list)
    listener_checks: list[dict[int, float]] = field(default_factory=list)

    async def spawn(
        self,
        kind: _ProcessKind,
        python: Path,
        *,
        cwd: Path,
        env: Mapping[str, str],
        socket_path: Path | None = None,
    ):
        self.spawn_calls.append((kind, python, cwd, socket_path, dict(env)))
        if not self.spawns:
            raise OSError("no fake process queued")
        result = self.spawns.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def process_group(self, pid: int) -> int | None:
        return self.process_groups.get(pid)

    def inspect_identity(self, pid: int) -> _ProcessIdentity | None:
        return self.identities.get(pid)

    def snapshot_tree(self, pid: int, process_group: int | None) -> dict[int, float]:
        self.snapshot_calls.append((pid, process_group))
        return dict(self.trees.get((pid, process_group), {}))

    def recorded_group_members(
        self,
        process_group: int,
        *,
        socket_path: Path,
        provider_root: Path,
        role=None,
    ) -> tuple[dict[int, float], list[int]]:
        del socket_path, provider_root, role
        self.group_scans.append(process_group)
        owned, foreign = self.groups.get(process_group, ({}, []))
        return dict(owned), list(foreign)

    def find_sidecars(self, *, socket_path: Path) -> dict[int, float]:
        self.sidecar_scans.append(socket_path)
        return dict(self.sidecars)

    def find_sidecars_by_root(self, *, provider_root: Path) -> dict[int, float]:
        del provider_root
        return dict(self.root_sidecars)

    def find_rebuilds(
        self,
        *,
        provider_root: Path,
        python: Path | None,
    ) -> dict[int, float]:
        del provider_root, python
        return dict(self.rebuilds)

    def live(self, identities: Mapping[int, float]) -> dict[int, float]:
        return {
            pid: created_at
            for pid, created_at in identities.items()
            if self.live_processes.get(pid) == created_at
        }

    def signal(
        self,
        identities: Mapping[int, float],
        signum: int,
        *,
        process_group: int | None = None,
        process=None,
    ) -> None:
        self.signal_calls.append(
            (dict(identities), signum, process_group, None if process is None else process.pid)
        )
        if self.signal_effect is not None:
            self.signal_effect(identities, signum)
        elif self.remove_on_signal:
            for pid in identities:
                self.live_processes.pop(pid, None)

    async def wait_for_exit(
        self,
        identities: dict[int, float],
        timeout_seconds: float,
        *,
        process_group: int | None = None,
        process=None,
    ) -> bool:
        del timeout_seconds, process_group, process
        if self.wait_results:
            return self.wait_results.popleft()
        return not self.live(identities)

    async def wait_for_stopped(self, pid: int, timeout_seconds: float) -> bool:
        del pid, timeout_seconds
        return True

    def has_tcp_listener(self, identities: Mapping[int, float]) -> bool:
        self.listener_checks.append(dict(identities))
        return bool(self.listeners.intersection(identities))


assert isinstance(_FakeProcessHost(), _ProcessHost)


def _settings() -> EverOSProcessSettings:
    return EverOSProcessSettings(
        llm_base_url="https://llm.example.test/v1",
        llm_model="chat",
        llm_api_key="llm-secret",
        embedding_base_url="https://embed.example.test/v1",
        embedding_model="embed",
        embedding_api_key="embedding-secret",
    )


def test_processing_probe_timeout_is_derived_from_largest_provider_group() -> None:
    independent = replace(
        _settings(),
        rerank_base_url="https://rerank.example.test/v1/inference",
        rerank_model="rerank",
        rerank_api_key="shared-secret",
        multimodal_base_url="https://llm.example.test/v1",
        multimodal_model="vision",
        multimodal_api_key="vision-secret",
    )
    one_group = replace(
        independent,
        embedding_base_url="https://llm.example.test/v1",
        embedding_api_key="shared-secret",
        llm_api_key="shared-secret",
        rerank_base_url="https://llm.example.test/v1",
        multimodal_api_key="shared-secret",
    )

    assert memory_process._processing_probe_timeout_seconds(independent) == 10.0
    assert memory_process._processing_probe_timeout_seconds(one_group) == 34.0


async def test_processing_probe_applies_derived_parent_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _Probe:
        pid = 999_999
        returncode = 0
        stderr = None

        async def wait(self) -> None:
            return None

    settings = replace(
        _settings(),
        llm_api_key="shared-secret",
        embedding_base_url="https://llm.example.test/v1",
        embedding_api_key="shared-secret",
        rerank_base_url="https://llm.example.test/v1",
        rerank_model="rerank",
        rerank_api_key="shared-secret",
        multimodal_base_url="https://llm.example.test/v1",
        multimodal_model="vision",
        multimodal_api_key="shared-secret",
    )
    timeouts: list[float] = []

    async def capture_timeout(awaitable, *, timeout):
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(memory_process.asyncio, "wait_for", capture_timeout)
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=settings,
        _host=_FakeProcessHost(spawns=deque([_Probe()])),
    )

    assert await process.processing_healthy() is True
    assert timeouts == [34.0]


def _pid_exists(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def test_refresh_owned_processes_uses_one_snapshot_and_prunes_dead_identities(
    tmp_path: Path,
) -> None:
    host = _FakeProcessHost(
        trees={(100, 42_424): {100: 1.0, 300: 3.0}},
        live_processes={100: 1.0, 300: 3.0},
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )
    process._process_group = 42_424
    process._owned_processes = {100: 1.0, 200: 2.0}

    refreshed = process._refresh_owned_processes(100)

    assert host.snapshot_calls == [(100, 42_424)]
    assert refreshed == {100: 1.0, 300: 3.0}
    assert process._owned_processes == refreshed


def test_refresh_owned_processes_retains_unverifiable_identity_sentinels(
    tmp_path: Path,
) -> None:
    host = _FakeProcessHost(
        trees={(100, None): {100: 1.0}},
        live_processes={100: 1.0},
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )
    process._owned_processes = {100: 1.0, 200: -1.0}

    assert process._refresh_owned_processes(100) == {100: 1.0, 200: -1.0}


def test_tcp_listener_check_reuses_refreshed_owned_processes(
    tmp_path: Path,
) -> None:
    host = _FakeProcessHost()
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )

    process._assert_no_tcp_listener(100, owned_processes={100: 1.0, 300: 3.0})

    assert host.snapshot_calls == []
    assert host.listener_checks == [{100: 1.0, 300: 3.0}]


def test_sidecar_child_environment_is_allowlisted_and_generated_config_has_no_keys(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/override.pem")
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=replace(_settings(), timezone="UTC"),
    )
    process._prepare_owned_directories()
    process._write_generated_config()
    environment = process._child_environment()
    generated = (tmp_path / "memory" / "generated" / "everos.toml").read_text(encoding="utf-8")
    expected_generated = "\n".join(
        (
            "# Generated by Avibe. No API keys are stored here.",
            "[memory]",
            'timezone = "UTC"',
            "",
            "[memorize]",
            'mode = "chat"',
            "",
            "[rerank]",
            'model = ""',
            'base_url = ""',
            "",
            "[multimodal]",
            f'file_uri_allow_dirs = ["{attachment_pin_root(tmp_path)}"]',
            "file_uri_max_bytes = 26214400",
            "",
        )
    )

    assert environment["EVEROS_LLM__API_KEY"] == "llm-secret"
    # Workbench keeps its legacy implicit LLM inheritance for one cycle. IM
    # capture still requires an explicit persisted multimodal endpoint.
    assert environment["EVEROS_MULTIMODAL__BASE_URL"] == environment["EVEROS_LLM__BASE_URL"]
    assert environment["EVEROS_MULTIMODAL__MODEL"] == environment["EVEROS_LLM__MODEL"]
    assert environment["EVEROS_MULTIMODAL__API_KEY"] == "llm-secret"
    assert MULTIMODAL_EXPLICIT_ENV not in environment
    assert environment["AVIBE_MEMORY_ATTACHMENTS_ROOT"] == str(
        attachment_pin_root(tmp_path)
    )
    assert environment["EVEROS_EMBEDDING__API_KEY"] == "embedding-secret"
    assert not any(key.startswith("EVEROS_RERANK__") for key in environment)
    assert "HTTP_PROXY" not in environment
    assert "SSL_CERT_FILE" not in environment
    assert "llm-secret" not in generated
    assert "embedding-secret" not in generated
    assert "rerank" in generated
    assert '[rerank]\nmodel = ""\nbase_url = ""' in generated
    assert generated == expected_generated
    assert str(attachment_pin_root(tmp_path)) in generated
    assert "AVIBE_MEMORY_CALL_LOG_DB" not in environment


def test_configured_multimodal_stays_env_only_and_independent_from_llm(tmp_path: Path) -> None:
    settings = replace(
        _settings(),
        timezone="UTC",
        multimodal_base_url="https://vision.example.test/v1",
        multimodal_model="vision-model",
        multimodal_api_key="vision-secret",
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=settings,
    )
    process._prepare_owned_directories()
    process._write_generated_config()

    environment = process._child_environment()
    generated = (tmp_path / "memory" / "generated" / "everos.toml").read_text(
        encoding="utf-8"
    )
    parsed = memory_process.tomllib.loads(generated)

    assert environment["EVEROS_MULTIMODAL__BASE_URL"] == settings.multimodal_base_url
    assert environment["EVEROS_MULTIMODAL__MODEL"] == "vision-model"
    assert environment["EVEROS_MULTIMODAL__API_KEY"] == "vision-secret"
    assert environment["EVEROS_MULTIMODAL__MODEL"] != environment["EVEROS_LLM__MODEL"]
    assert environment[MULTIMODAL_EXPLICIT_ENV] == "1"
    assert parsed["multimodal"]["file_uri_max_bytes"] == 25 * 1024 * 1024
    assert "vision-secret" not in generated


def test_configured_rerank_stays_env_only_when_env_overrides_toml(tmp_path: Path) -> None:
    settings = replace(
        _settings(),
        timezone="UTC",
        rerank_base_url="https://rerank.example.test/v1/inference",
        rerank_model="Qwen/Qwen3-Reranker-4B",
        rerank_api_key="rerank-secret",
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=settings,
    )
    process._prepare_owned_directories()
    process._write_generated_config()

    environment = process._child_environment()
    generated = (tmp_path / "memory" / "generated" / "everos.toml").read_text(
        encoding="utf-8"
    )
    parsed = memory_process.tomllib.loads(generated)

    assert environment["EVEROS_RERANK__BASE_URL"] == settings.rerank_base_url
    assert environment["EVEROS_RERANK__MODEL"] == settings.rerank_model
    assert environment["EVEROS_RERANK__API_KEY"] == "rerank-secret"
    assert parsed["rerank"] == {"model": "", "base_url": ""}
    assert "rerank-secret" not in generated


def test_sidecar_child_home_preparation_hardens_every_created_directory(
    tmp_path: Path,
) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    previous_umask = os.umask(0o022)
    try:
        process._prepare_owned_directories()
    finally:
        os.umask(previous_umask)

    child_home = tmp_path / "memory" / ".child-home"
    created = [child_home]
    created.extend(path for path in child_home.rglob("*") if path.is_dir())
    assert {path.relative_to(child_home).as_posix() for path in created} == {
        ".",
        ".cache",
        ".config",
        ".local",
        ".local/share",
        ".local/state",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in created)


def test_pinned_attachment_uri_matches_process_body_and_sidecar_guard(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "avibe-home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    source_root = home / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700)
    source = source_root / "diagram.png"
    source.write_bytes(b"pinned image")
    source.chmod(0o600)
    pin_store = AttachmentPinStore(
        root=attachment_pin_root(home),
        source_root=source_root,
    )
    bundle = pin_store.pin(
        (
            CaptureAttachment(
                kind="image",
                name=source.name,
                uri=source.as_uri(),
                ext="png",
            ),
        )
    )
    pinned = pin_store.provider_attachments(bundle)

    process = EverOSProcess(sys.executable, effective_home=home, settings=_settings())
    environment = process._child_environment()
    allowed_root = Path(environment["AVIBE_MEMORY_ATTACHMENTS_ROOT"])
    assert allowed_root == attachment_pin_root(home)

    request: dict[str, object] = {}

    async def capture_request(
        method: str,
        route: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        request.update(method=method, route=route, payload=payload)
        return 200, b'{"data":{"status":"accumulated"}}'

    provider = EverOSPort(home / "memory" / ".rt" / "everos.sock")
    monkeypatch.setattr(provider, "_sidecar_write", capture_request)
    session_ref = ProviderSessionRef(
        principal_id="u-11111111111111111111111111111111",
        epoch=1,
        project_ref=PROJECT,
        session_id=f"src--{'1' * 64}--e1",
    )
    asyncio.run(
        provider.add(
            ProviderCapture(
                session_ref=session_ref,
                text="remember this diagram",
                provider_timestamp_ms=1_725_000_001_234,
                attachments=pinned,
            )
        )
    )

    body = json.dumps(request["payload"]).encode()
    assert request["method"] == "POST"
    assert request["route"] == "/api/v2/memory/add"
    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/add",
            body,
            attachments_root=allowed_root,
        )
        is None
    )

    outside = home / "outside.png"
    outside.write_bytes(b"outside")
    payload = request["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    assert isinstance(content, list)
    content[-1]["uri"] = outside.as_uri()
    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/add",
            json.dumps(payload).encode(),
            attachments_root=allowed_root,
        )
        == "add"
    )


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


async def test_sidecar_rejects_sun_path_overflow_without_launching_child(tmp_path: Path) -> None:
    socket_path = tmp_path / ("a" * 180) / "everos.sock"
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        socket_path=socket_path,
        settings=_settings(),
    )

    assert await process.start() is False
    assert process.last_error == "memory_sidecar_unavailable"
    assert process.consecutive_failures == 1
    await process.stop()


async def test_sidecar_start_failure_never_relaunches_beside_an_unreaped_child(monkeypatch, tmp_path: Path) -> None:
    class _Child:
        pid = 999_999
        returncode = None

        async def wait(self) -> None:
            return None

        def send_signal(self, _signum) -> None:
            return None

    child = _Child()
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): {child.pid: 1.0}},
        live_processes={child.pid: 1.0},
    )

    async def readiness_failure(_process) -> None:
        raise RuntimeError("readiness failed")

    async def cleanup_failure(*_args, **_kwargs) -> None:
        raise RuntimeError("child tree still alive")

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        socket_path=Path(f"/tmp/everos-{os.getpid()}.sock"),
        settings=_settings(),
        _host=host,
    )
    monkeypatch.setattr(process, "_prepare_owned_directories", lambda: None)
    monkeypatch.setattr(process, "_write_generated_config", lambda: None)
    monkeypatch.setattr(process, "_remove_owned_socket", lambda: None)
    monkeypatch.setattr(process, "_wait_for_ready", readiness_failure)
    monkeypatch.setattr(process, "_terminate_owned_tree", cleanup_failure)

    assert await process.start() is False
    assert process.down is True
    assert process._process is child
    assert process._restart_task is None
    assert await process.start() is False
    assert len(host.spawn_calls) == 1


async def test_processing_probe_reaps_child_when_its_caller_is_cancelled(monkeypatch, tmp_path: Path) -> None:
    started = asyncio.Event()
    cleanup_calls: list[object] = []

    class _Probe:
        pid = 999_999
        returncode = None

        async def wait(self) -> None:
            started.set()
            await asyncio.Event().wait()

    probe = _Probe()
    host = _FakeProcessHost(spawns=deque([probe]))

    async def cleanup(*_args, **_kwargs) -> None:
        cleanup_calls.append(object())

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )
    monkeypatch.setattr(process, "_terminate_owned_tree", cleanup)

    task = asyncio.create_task(process.processing_healthy())
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("processing probe cancellation was swallowed")
    assert cleanup_calls


async def test_probe_stderr_drain_keeps_only_bounded_tail() -> None:
    reader = asyncio.StreamReader()
    payload = b"head-secret\n" + (b"x" * 100_000) + b"\ntail-marker"
    reader.feed_data(payload)
    reader.feed_eof()

    tail = await memory_process._drain_probe_stderr(reader)

    assert len(tail) <= memory_process._PROCESSING_PROBE_STDERR_BYTES
    assert b"head-secret" not in tail
    assert tail.endswith(b"tail-marker")


async def test_sidecar_stop_signals_isolated_child_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); time.sleep(60)"
    )

    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script, start_new_session=True)
    deadline = time.monotonic() + 3
    while not child_pid_path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert child_pid_path.exists()
    descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    process._process_group = os.getpgid(child.pid)
    owned_processes = _SystemProcessHost().snapshot_tree(child.pid, process._process_group)
    await process._terminate_owned_tree(
        child,
        process_group=process._process_group,
        owned_processes=owned_processes,
    )
    parent_pid = child.pid
    assert not _pid_exists(parent_pid)
    assert not _pid_exists(descendant_pid)


def test_sidecar_safety_monitor_ignores_expected_shutdown(tmp_path: Path, caplog) -> None:
    class _Child:
        pid = 999_999
        returncode = None

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=_FakeProcessHost(),
    )
    child = _Child()
    process._process = child
    process._desired_running = False

    asyncio.run(process._monitor_child(child))

    assert "safety monitor rejected" not in caplog.text
    assert process._process is child


def test_sidecar_safety_monitor_logs_the_underlying_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Child:
        pid = 999_999
        returncode = None

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=_FakeProcessHost(),
    )
    child = _Child()
    process._process = child
    process._desired_running = True
    process._owned_processes = {child.pid: 11.0}

    def reject_tree(_pid: int) -> dict[int, float]:
        raise RuntimeError("sidecar ownership changed during monitoring")

    monkeypatch.setattr(process, "_refresh_owned_processes", reject_tree)

    with caplog.at_level(logging.ERROR, logger=memory_process.logger.name):
        asyncio.run(process._monitor_child(child))

    assert "EverOS sidecar safety monitor rejected the child tree" in caplog.text
    assert "sidecar ownership changed during monitoring" in caplog.text
    assert "recorded_stamp=11.0" in caplog.text


async def test_sidecar_stop_reaps_a_descendant_that_leaves_the_child_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "detached-child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); time.sleep(60)"
    )

    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script, start_new_session=True)
    deadline = time.monotonic() + 3
    while not child_pid_path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert child_pid_path.exists()
    descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))
    process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
    process._process_group = os.getpgid(child.pid)
    owned_processes = _SystemProcessHost().snapshot_tree(child.pid, process._process_group)
    await process._terminate_owned_tree(
        child,
        process_group=process._process_group,
        owned_processes=owned_processes,
    )
    parent_pid = child.pid
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


async def test_sidecar_stop_reaps_direct_child_after_create_time_clock_step(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A CLOCK_REALTIME step must not block SIGTERM on the handle we spawned."""

    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    try:
        host = _SystemProcessHost()
        process_group = os.getpgid(child.pid)
        identities = host.snapshot_tree(child.pid, process_group)
        captured_stamp = identities[child.pid]
        original_process = memory_process.psutil.Process
        original_iter = memory_process.psutil.process_iter
        original_stamp = memory_process._process_creation_stamp

        def _shifted_stamp(proc: object) -> float:
            if getattr(proc, "pid", None) == child.pid:
                return captured_stamp + 48.0
            return original_stamp(proc)

        class _ShiftedProcess:
            def __init__(self, process_id: int) -> None:
                self._inner = original_process(process_id)
                self.pid = process_id

            def create_time(self) -> float:
                return float(self._inner.create_time()) + 48.0

            def __getattr__(self, name: str):
                return getattr(self._inner, name)

        def _shifted_iter(*args, **kwargs):
            for candidate in original_iter(*args, **kwargs):
                try:
                    yield _ShiftedProcess(candidate.pid)
                except (psutil.Error, OSError):
                    continue

        monkeypatch.setattr(memory_process.psutil, "Process", _ShiftedProcess)
        monkeypatch.setattr(memory_process.psutil, "process_iter", _shifted_iter)
        monkeypatch.setattr(memory_process, "_process_creation_stamp", _shifted_stamp)
        monkeypatch.setattr(
            memory_process,
            "_read_linux_starttime_ticks",
            lambda _pid: captured_stamp + 48.0,
        )
        process = EverOSProcess(sys.executable, effective_home=tmp_path, settings=_settings())
        await process._terminate_owned_tree(
            child,
            process_group=process_group,
            owned_processes=identities,
        )
        assert child.returncode is not None
        assert not _pid_exists(child.pid)
    finally:
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), timeout=3.0)
            except TimeoutError:
                child.kill()
                await child.wait()


async def test_sidecar_cleanup_never_signals_spawned_pid_after_identity_changes(monkeypatch, tmp_path: Path) -> None:
    signals: list[tuple[str, int]] = []

    class _TrackedChild:
        returncode = None

        def __init__(self, pid: int) -> None:
            self.pid = pid

        def send_signal(self, signum: int) -> None:
            signals.append(("child", signum))

    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    try:
        host = _SystemProcessHost()
        identities = host.snapshot_tree(child.pid, None)
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

        def _reused_stamp(_proc: object) -> float:
            # Linux identity is starttime ticks, not create_time(). Shift the
            # stamp itself so this still models a recycled pid on CI.
            return captured_at + 1.0

        monkeypatch.setattr(memory_process.psutil, "Process", _ReusedProcess)
        monkeypatch.setattr(memory_process, "_process_creation_stamp", _reused_stamp)
        try:
            host.signal(identities, signal.SIGTERM)
            host.signal(
                identities,
                signal.SIGTERM,
                process=_TrackedChild(child.pid),
            )
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


def test_sidecar_cleanup_reverifies_asyncio_pid_before_signaling(monkeypatch) -> None:
    signals: list[int] = []

    class _Transport:
        _proc = SimpleNamespace(returncode=None)

        def get_pid(self) -> int:
            return _ORPHAN_PID

        def get_returncode(self) -> None:
            # Models the interval after waitpid() reaped the child but before
            # asyncio's scheduled process-exited callback updates the transport.
            return None

        def send_signal(self, signum: int) -> None:
            signals.append(signum)

    class _Protocol:
        stdin = None
        stdout = None
        stderr = None

    process = asyncio.subprocess.Process(_Transport(), _Protocol(), None)
    monkeypatch.setattr(memory_process, "_confirmed_owned_processes", lambda _identities: {})

    _SystemProcessHost().signal(
        {_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        signal.SIGTERM,
        process=process,
    )

    assert signals == []


async def test_sidecar_wait_never_discovers_from_a_reused_asyncio_pid(monkeypatch) -> None:
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    snapshot_calls: list[tuple[int, int | None]] = []

    class _ReapedChild:
        pid = _ORPHAN_PID
        returncode = None

        async def wait(self) -> int:
            return 0

    def snapshot(pid: int, process_group: int | None) -> dict[int, float]:
        snapshot_calls.append((pid, process_group))
        return {
            _ORPHAN_PID: _ORPHAN_CREATE_TIME + 1,
            _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 2,
        }

    monkeypatch.setattr(memory_process, "_snapshot_owned_processes", snapshot)
    monkeypatch.setattr(memory_process, "_live_owned_processes", lambda _identities: {})

    assert await memory_process._wait_for_owned_exit(
        _ReapedChild(),
        process_group=_ORPHAN_PID,
        identities=identities,
        timeout_seconds=0.1,
    )
    assert snapshot_calls == []
    assert identities == {_ORPHAN_PID: _ORPHAN_CREATE_TIME}


def test_owned_tree_refresh_reverifies_the_root_after_snapshot() -> None:
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}

    class _ReusingHost(_FakeProcessHost):
        def snapshot_tree(self, pid: int, process_group: int | None) -> dict[int, float]:
            snapshot = super().snapshot_tree(pid, process_group)
            self.live_processes[pid] = _ORPHAN_CREATE_TIME + 1
            return snapshot

    host = _ReusingHost(
        live_processes=dict(identities),
        trees={
            (_ORPHAN_PID, _ORPHAN_PID): {
                _ORPHAN_PID: _ORPHAN_CREATE_TIME,
                _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 2,
            }
        },
    )

    assert not memory_process._refresh_owned_process_tree(
        host,
        identities,
        _ORPHAN_PID,
        _ORPHAN_PID,
    )
    assert identities == {_ORPHAN_PID: _ORPHAN_CREATE_TIME}


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

    _SystemProcessHost().signal(
        {42_424: 11.0, 42_425: 12.0},
        signal.SIGTERM,
        process_group=42_424,
        process=_TrackedChild(),
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

    host = _SystemProcessHost()
    snapshot = host.snapshot_tree(parent_id, group_id)
    host.signal(
        {parent_id: 11.0, child_id: 12.0},
        signal.SIGTERM,
        process_group=group_id,
        process=_TrackedChild(),
    )

    assert snapshot == {parent_id: 11.0, child_id: -1.0}
    assert group_signals == []
    assert child_signals == [signal.SIGTERM]


def test_sidecar_cleanup_keeps_access_denied_identity_live_without_signaling(monkeypatch) -> None:
    process_id = 42_425

    class _InaccessibleProcess:
        def __init__(self, _process_id: int) -> None:
            raise psutil.AccessDenied(pid=process_id)

    monkeypatch.setattr(memory_process.psutil, "Process", _InaccessibleProcess)
    identities = {process_id: 11.0}

    host = _SystemProcessHost()
    assert host.live(identities) == identities
    host.signal(identities, signal.SIGTERM)


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


def _orphan_process(
    tmp_path: Path,
    *,
    host: _ProcessHost | None = None,
    **overrides,
) -> EverOSProcess:
    return EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
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
    creation_stamp = overrides.pop("stamp", overrides.pop("create_time", _ORPHAN_CREATE_TIME))
    fields = {
        "stamp": creation_stamp,
        "cmdline": (
            sys.executable,
            "-m",
            _SIDECAR_ENTRYPOINT_MODULE,
            "--uds",
            str(process.socket_path),
        ),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "wall_create_time": overrides.pop("wall_create_time", creation_stamp),
    }
    fields.update(overrides)
    return _ProcessIdentity(**fields)


def _write_orphan_record(process: EverOSProcess, record: dict) -> Path:
    path = process._ownership.record_path
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent.parent, 0o700)
    os.chmod(path.parent, 0o700)
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
        # Linux starttime ticks cannot be compared with a legacy epoch value.
        (
            _orphan_record(process),
            _orphan_identity(process, create_time=424_242.0, wall_create_time=None),
        ),
    ]
    for record, identity in unverifiable:
        assert verdict(record, identity) is _RecordedSidecar.UNVERIFIABLE, (record, identity)

    if own_uid is not None:
        # An unreadable uid is not an exclusion either.
        assert verdict(_orphan_record(process), _orphan_identity(process, uid=None)) is (
            _RecordedSidecar.UNVERIFIABLE
        )


def test_inspect_identity_keeps_wall_clock_beside_linux_starttime_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory_process, "_uses_linux_starttime_stamp", lambda: True)
    monkeypatch.setattr(memory_process, "_read_linux_starttime_ticks", lambda _pid: 99.0)
    monkeypatch.setattr(memory_process.psutil, "Process", _guarded_process_class(uid=4_242))

    identity = memory_process._inspect_process_identity(_ORPHAN_PID)

    assert identity == _ProcessIdentity(
        stamp=99.0,
        cmdline=None,
        uid=4_242,
        wall_create_time=_ORPHAN_CREATE_TIME,
    )


def test_linux_creation_stamp_reads_starttime_ticks_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = 424_242.0
    monkeypatch.setattr(memory_process, "_uses_linux_starttime_stamp", lambda: True)
    monkeypatch.setattr(memory_process, "_read_linux_starttime_ticks", lambda _pid: ticks)

    class _Proc:
        pid = 99

        def create_time(self) -> float:
            return _ORPHAN_CREATE_TIME + 48.0

    assert memory_process._process_creation_stamp(_Proc()) == ticks


def test_parse_proc_stat_starttime_handles_spaces_in_comm() -> None:
    # Field 2 (comm) can contain spaces and parentheses. Field 22 is starttime.
    after_comm = [b"S"] + [b"0"] * 18 + [b"987654", b"1"]
    stat_data = b"42 (python 3.11) " + b" ".join(after_comm)
    assert memory_process._parse_proc_stat_starttime(stat_data) == 987654.0


def test_linux_starttime_reader_accepts_non_utf8_comm(monkeypatch: pytest.MonkeyPatch) -> None:
    after_comm = [b"S"] + [b"0"] * 18 + [b"987654", b"1"]
    stat_data = b"42 (worker-\xff) " + b" ".join(after_comm)

    monkeypatch.setattr(Path, "read_bytes", lambda _path: stat_data)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        ),
    )

    assert memory_process._read_linux_starttime_ticks(_ORPHAN_PID) == 987654.0


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), float("-inf"), 10**1000])
def test_identity_stamps_must_be_finite(stamp: float | int) -> None:
    assert not memory_process._is_identity_stamp(stamp)


def test_legacy_record_rejects_a_nan_creation_stamp(tmp_path: Path) -> None:
    process = _orphan_process(tmp_path)
    assert _classify_recorded_sidecar(
        _orphan_record(process, create_time=float("nan")),
        _orphan_identity(
            process,
            wall_create_time=_ORPHAN_CREATE_TIME + 48.0,
            environment={"EVEROS_ROOT": str(process.provider_root)},
        ),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.NOT_OURS


def test_legacy_record_treats_a_nan_live_creation_stamp_as_unverifiable(tmp_path: Path) -> None:
    process = _orphan_process(tmp_path)
    assert _classify_recorded_sidecar(
        _orphan_record(process),
        _orphan_identity(
            process,
            wall_create_time=float("nan"),
            environment={"EVEROS_ROOT": str(process.provider_root)},
        ),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.UNVERIFIABLE


def test_creation_stamp_falls_back_to_create_time_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory_process, "_uses_linux_starttime_stamp", lambda: False)

    class _Proc:
        pid = 99

        def create_time(self) -> float:
            return _ORPHAN_CREATE_TIME

    assert memory_process._process_creation_stamp(_Proc()) == _ORPHAN_CREATE_TIME


def test_legacy_record_accepts_bounded_create_time_drift_when_facts_match(
    tmp_path: Path,
) -> None:
    ticks = 424_242.0
    drifted = _ORPHAN_CREATE_TIME + 48.0
    host = _FakeProcessHost(live_processes={_ORPHAN_PID: ticks})
    process = _orphan_process(tmp_path, host=host)
    host.identities[_ORPHAN_PID] = _orphan_identity(
        process,
        create_time=ticks,
        wall_create_time=drifted,
        environment={"EVEROS_ROOT": str(process.provider_root)},
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    asyncio.run(process._ownership.reap())

    assert host.signal_calls == [({_ORPHAN_PID: ticks}, signal.SIGTERM, None, None)]
    assert not record_path.exists()


def test_legacy_record_rejects_create_time_drift_when_cmdline_mismatches(
    tmp_path: Path,
) -> None:
    process = _orphan_process(tmp_path)
    assert _classify_recorded_sidecar(
        _orphan_record(process),
        _orphan_identity(
            process,
            create_time=424_242.0,
            wall_create_time=_ORPHAN_CREATE_TIME + 48.0,
            cmdline=(sys.executable, "-m", "http.server"),
            environment={"EVEROS_ROOT": str(process.provider_root)},
        ),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.NOT_OURS


def test_legacy_record_treats_undisclosed_cmdline_as_unverifiable_on_create_time_drift(
    tmp_path: Path,
) -> None:
    process = _orphan_process(tmp_path)
    assert _classify_recorded_sidecar(
        _orphan_record(process),
        _orphan_identity(
            process,
            create_time=424_242.0,
            wall_create_time=_ORPHAN_CREATE_TIME + 48.0,
            cmdline=None,
            environment={"EVEROS_ROOT": str(process.provider_root)},
        ),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.UNVERIFIABLE


def test_recorded_sidecar_prefers_starttime_ticks_over_create_time(tmp_path: Path) -> None:
    process = _orphan_process(tmp_path)
    record = _orphan_record(process, starttime_ticks=99.0, create_time=_ORPHAN_CREATE_TIME)
    assert _classify_recorded_sidecar(
        record,
        _orphan_identity(process, create_time=99.0),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.OURS
    assert _classify_recorded_sidecar(
        record,
        _orphan_identity(process, create_time=_ORPHAN_CREATE_TIME),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.NOT_OURS


def test_sidecar_record_writes_starttime_ticks_on_linux(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(memory_process, "_uses_linux_starttime_stamp", lambda: True)
    monkeypatch.setattr(
        memory_process,
        "_process_wall_create_time",
        lambda _pid: _ORPHAN_CREATE_TIME,
    )
    process = _orphan_process(tmp_path)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)

    process._ownership.record_launch(_ORPHAN_PID, 4242.0, _ORPHAN_PID)
    recorded = json.loads(process._ownership.record_path.read_text(encoding="utf-8"))

    assert recorded["create_time"] == _ORPHAN_CREATE_TIME
    assert recorded["starttime_ticks"] == 4242.0


def test_sidecar_record_omits_wall_create_time_when_undisclosed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(memory_process, "_uses_linux_starttime_stamp", lambda: True)
    monkeypatch.setattr(memory_process, "_process_wall_create_time", lambda _pid: None)
    process = _orphan_process(tmp_path)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)

    process._ownership.record_launch(_ORPHAN_PID, 4242.0, _ORPHAN_PID)
    recorded = json.loads(process._ownership.record_path.read_text(encoding="utf-8"))

    assert recorded["create_time"] is None
    assert recorded["starttime_ticks"] == 4242.0


def test_legacy_linux_ticks_identity_reaps_when_live_wall_drift_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ticks = 424_242.0
    drifted_wall = _ORPHAN_CREATE_TIME + 48.0
    helper = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 1}
    monkeypatch.setattr(memory_process, "_process_wall_create_time", lambda _pid: drifted_wall)
    host = _FakeProcessHost(
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (dict(helper), [])},
        live_processes={_ORPHAN_PID: ticks, **helper},
        trees={(_ORPHAN_PID, _ORPHAN_PID): {_ORPHAN_PID: ticks, **helper}},
    )
    process = _orphan_process(tmp_path, host=host)
    host.identities[_ORPHAN_PID] = _orphan_identity(
        process,
        stamp=ticks,
        wall_create_time=None,
        environment={"EVEROS_ROOT": str(process.provider_root)},
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    asyncio.run(process._ownership.reap())

    signaled = {pid for call in host.signal_calls for pid in call[0]}
    assert _ORPHAN_PID in signaled
    assert _ORPHAN_GROUP_HELPER_PID in signaled
    assert not record_path.exists()


def test_legacy_linux_ticks_identity_rejects_live_wall_drift_beyond_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        memory_process,
        "_process_wall_create_time",
        lambda _pid: _ORPHAN_CREATE_TIME + 301.0,
    )
    process = _orphan_process(tmp_path)
    assert _classify_recorded_sidecar(
        _orphan_record(process),
        _orphan_identity(
            process,
            stamp=424_242.0,
            wall_create_time=None,
            environment={"EVEROS_ROOT": str(process.provider_root)},
        ),
        socket_path=process.socket_path,
        provider_root=process.provider_root,
    ) is _RecordedSidecar.NOT_OURS


def test_new_sidecar_role_record_reaps_with_exact_role_environment(tmp_path: Path) -> None:
    host = _FakeProcessHost(
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        live_processes={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        trees={
            (_ORPHAN_PID, _ORPHAN_PID): {_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        },
    )
    process = _orphan_process(tmp_path, host=host)
    host.identities[_ORPHAN_PID] = _orphan_identity(
        process,
        environment={
            "EVEROS_ROOT": str(process.provider_root),
            "AVIBE_MEMORY_CHILD_ROLE": "sidecar",
        },
    )
    record_path = _write_orphan_record(
        process,
        _orphan_record(process, role="sidecar", python=sys.executable),
    )
    rebuild = _rebuild_process(tmp_path, host)

    asyncio.run(rebuild._ownership.reap())

    assert not host.live_processes
    assert not record_path.exists()
    assert process._ownership.consume_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    ) is True
    assert process._ownership.consume_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    ) is False


def test_legacy_sidecar_group_matching_remains_role_agnostic(tmp_path: Path) -> None:
    helper = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 1}

    class _LegacyGroupHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.matched_roles: list[_MemoryChildRole | None] = []

        def recorded_group_members(
            self,
            process_group: int,
            *,
            socket_path: Path,
            provider_root: Path,
            role=None,
        ) -> tuple[dict[int, float], list[int]]:
            del process_group, socket_path, provider_root
            self.matched_roles.append(role)
            return (dict(helper), []) if role is None else ({}, list(helper))

    host = _LegacyGroupHost(
        identities={_ORPHAN_PID: None},
        live_processes=dict(helper),
    )
    process = _orphan_process(tmp_path, host=host)
    record_path = _write_orphan_record(process, _orphan_record(process))

    asyncio.run(process._ownership.reap())

    assert host.matched_roles and set(host.matched_roles) == {None}
    assert not host.live_processes
    assert not record_path.exists()


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

    assert identity == _ProcessIdentity(
        stamp=_ORPHAN_CREATE_TIME,
        cmdline=None,
        uid=4_242,
        wall_create_time=_ORPHAN_CREATE_TIME,
    )

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
        stamp=_ORPHAN_CREATE_TIME,
        cmdline=None,
        uid=None,
        wall_create_time=_ORPHAN_CREATE_TIME,
    )


def test_sidecar_launch_reaps_a_recorded_orphan_from_a_previous_run(
    tmp_path: Path,
) -> None:
    """A crashed service leaves its ``start_new_session`` child running.

    Boot used to spawn a second sidecar beside it, so the orphan kept serving the
    socket and holding handles on provider data until it was killed by hand.
    """

    host = _FakeProcessHost(live_processes={_ORPHAN_PID: _ORPHAN_CREATE_TIME})
    process = _orphan_process(tmp_path, host=host)
    host.identities[_ORPHAN_PID] = _orphan_identity(process)
    record_path = _write_orphan_record(process, _orphan_record(process))

    asyncio.run(process._ownership.reap())

    assert host.signal_calls == [
        ({_ORPHAN_PID: _ORPHAN_CREATE_TIME}, signal.SIGTERM, None, None)
    ]
    assert not record_path.exists()


def test_sidecar_launch_never_signals_a_pid_it_cannot_identify(
    tmp_path: Path,
) -> None:
    """A recycled pid retires the record instead of killing its new owner."""

    host = _FakeProcessHost()
    process = _orphan_process(tmp_path, host=host)
    host.identities[_ORPHAN_PID] = _orphan_identity(
        process,
        create_time=_ORPHAN_CREATE_TIME + 10,
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    asyncio.run(process._ownership.reap())

    assert host.signal_calls == []
    assert not record_path.exists()


def test_sidecar_launch_refuses_to_start_beside_an_unreapable_orphan(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """Fail closed, exactly as ``start`` already does for an unreaped child."""

    host = _FakeProcessHost(
        live_processes={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        wait_results=deque([False, False]),
        remove_on_signal=False,
    )
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    host.identities[_ORPHAN_PID] = _orphan_identity(process)
    _write_orphan_record(process, _orphan_record(process))

    assert asyncio.run(process.start()) is False
    assert process.last_error == "memory_sidecar_unavailable"
    assert host.spawn_calls == []
    assert process._ownership.record_path.exists()


def test_sidecar_launch_reaps_the_whole_orphan_tree_not_just_the_recorded_pid(
    tmp_path: Path,
) -> None:
    """An orphan's helpers hold the provider root just as its root process does.

    Signalling only the recorded pid left same-group helpers running against the
    root while a replacement sidecar started, recreating the overlap this reap
    exists to prevent. Discovery must match the normal stop path: descendants plus
    every member of the isolated process group.
    """

    tree = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
        _ORPHAN_GROUP_MEMBER_PID: _ORPHAN_CREATE_TIME + 2,
    }
    host = _FakeProcessHost(
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        trees={(_ORPHAN_PID, _ORPHAN_PID): tree},
        live_processes=dict(tree),
    )
    process = _orphan_process(tmp_path, host=host)
    host.identities[_ORPHAN_PID] = _orphan_identity(process)
    record_path = _write_orphan_record(process, _orphan_record(process))

    asyncio.run(process._ownership.reap())

    assert host.snapshot_calls == [(_ORPHAN_PID, _ORPHAN_PID)]
    assert host.signal_calls == [(tree, signal.SIGTERM, _ORPHAN_PID, None)]
    assert not record_path.exists()


def test_sidecar_orphan_reap_refuses_a_group_signal_for_an_unverifiable_member(
    tmp_path: Path,
) -> None:
    """Widening discovery must not widen the blast radius.

    A group holding a member carrying the ``AccessDenied`` sentinel is never
    signaled group-wide, and a member that cannot be proven reaped still fails the
    launch instead of being written off.
    """

    discovered = {_ORPHAN_PID: _ORPHAN_CREATE_TIME, _ORPHAN_GROUP_MEMBER_PID: -1.0}

    host = _FakeProcessHost(
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        trees={(_ORPHAN_PID, _ORPHAN_PID): discovered},
        live_processes=dict(discovered),
        wait_results=deque([False, False]),
        remove_on_signal=False,
    )

    def leave_unverifiable_member(_identities, _signum) -> None:
        # The confirmed root exits; the unverifiable member cannot be proven gone.
        host.live_processes.pop(_ORPHAN_PID, None)

    host.signal_effect = leave_unverifiable_member
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
    )
    host.identities[_ORPHAN_PID] = _orphan_identity(process)
    record_path = _write_orphan_record(process, _orphan_record(process))

    with pytest.raises(RuntimeError, match="orphaned sidecar did not exit"):
        asyncio.run(process._ownership.reap())

    assert [call[1] for call in host.signal_calls] == [
        signal.SIGTERM,
        getattr(signal, "SIGKILL", signal.SIGTERM),
    ]
    assert record_path.exists()


def test_sidecar_launch_fails_closed_on_a_live_pid_it_cannot_describe(
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

    host = _FakeProcessHost()
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    host.identities[_ORPHAN_PID] = _ProcessIdentity(
        stamp=_ORPHAN_CREATE_TIME,
        cmdline=None,
        uid=os.getuid() if hasattr(os, "getuid") else None,
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    assert host.signal_calls == []
    assert host.spawn_calls == []
    assert record_path.exists()
    assert "recorded sidecar identity could not be verified" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text
    assert str(record_path) in caplog.text
    # `logger.exception`, so the traceback reaches the log too.
    assert "Traceback (most recent call last)" in caplog.text


def test_sidecar_launch_proceeds_past_a_recycled_pid_owned_by_another_user(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """Failing closed must not turn into a permanent brick.

    A pid recycled by another user's process is provably not our sidecar even when
    that process withholds its cmdline, so the record is retired and the launch
    continues rather than requiring manual intervention.
    """

    foreign_uid = (os.getuid() if hasattr(os, "getuid") else 0) + 1
    host = _FakeProcessHost()
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    host.identities[_ORPHAN_PID] = _ProcessIdentity(
        stamp=_ORPHAN_CREATE_TIME,
        cmdline=None,
        uid=foreign_uid,
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    assert asyncio.run(process.start()) is False
    assert host.signal_calls == []
    # The launch reached the spawn, so an unreadable stranger cannot wedge startup.
    assert host.spawn_calls
    assert not record_path.exists()


def test_sidecar_records_a_verified_launch_identity_privately(tmp_path: Path) -> None:
    """The record must be owner-only, and an unverifiable identity is not recorded."""

    process = _orphan_process(tmp_path)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)

    process._ownership.record_launch(_ORPHAN_PID, _ORPHAN_CREATE_TIME, _ORPHAN_PID)
    recorded = json.loads(process._ownership.record_path.read_text(encoding="utf-8"))

    expected = _orphan_record(
        process,
        role="sidecar",
        python=sys.executable,
    )
    if memory_process._uses_linux_starttime_stamp():
        expected["starttime_ticks"] = _ORPHAN_CREATE_TIME
        # Fake pid has no wall-clock create_time; do not store ticks there.
        expected["create_time"] = None
    assert recorded == expected
    assert stat.S_IMODE(process._ownership.record_path.lstat().st_mode) == 0o600

    process._ownership.record_path.unlink()
    # An AccessDenied group member carries a negative sentinel instead of a
    # creation time. Recording it would produce a record nothing can match, and
    # skipping the write would launch a child no later boot can identify, so the
    # launch has to fail instead.
    with pytest.raises(RuntimeError, match="could not verify the sidecar creation time"):
        process._ownership.record_launch(_ORPHAN_PID, -1.0, _ORPHAN_PID)

    assert not process._ownership.record_path.exists()


def test_sidecar_launch_reaps_group_members_a_gone_leader_left_behind(
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

    owned = {
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
        _ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2,
    }
    foreign = [_FOREIGN_GROUP_PID, _FOREIGN_UID_GROUP_PID]
    host = _FakeProcessHost(
        identities={_ORPHAN_PID: None},
        groups={_ORPHAN_PID: (owned, foreign)},
        live_processes={
            **owned,
            _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 3,
            _FOREIGN_UID_GROUP_PID: _ORPHAN_CREATE_TIME + 4,
        },
    )
    process = _orphan_process(tmp_path, host=host, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._ownership.reap())

    # Discovery only ever looked at the group the record names.
    assert host.group_scans and set(host.group_scans) == {_ORPHAN_PID}
    # One SIGTERM round, carrying only the two members that identified themselves.
    assert host.signal_calls == [(owned, signal.SIGTERM, _ORPHAN_PID, None)]
    assert str(_FOREIGN_GROUP_PID) in caplog.text
    assert str(_FOREIGN_UID_GROUP_PID) in caplog.text
    # The unclaimed members are still running, and must not wedge startup.
    assert host.live_processes == {
        _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 3,
        _FOREIGN_UID_GROUP_PID: _ORPHAN_CREATE_TIME + 4,
    }
    assert not record_path.exists()


def test_sidecar_launch_fails_closed_when_a_recorded_group_will_not_exit(
    tmp_path: Path,
    short_socket_path: Path,
    caplog,
) -> None:
    """A surviving helper fails the launch, exactly as an unreapable orphan does.

    Nothing later can clear this by itself, so the log has to name the group and the
    record that points at it -- ``last_error`` only ever says
    ``memory_sidecar_unavailable``.
    """

    members = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2}
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    host = _FakeProcessHost(
        identities={_ORPHAN_PID: None},
        groups={_ORPHAN_PID: (members, [])},
        live_processes=dict(members),
        wait_results=deque([False, False]),
        remove_on_signal=False,
    )
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    assert host.spawn_calls == []
    assert record_path.exists()
    # Every member of this group identified itself, so the group-wide signal is
    # allowed here -- unlike the mixed group above.
    assert [call[1:] for call in host.signal_calls] == [
        (signal.SIGTERM, _ORPHAN_PID, None),
        (kill_signal, _ORPHAN_PID, None),
    ]
    assert "orphaned sidecar group did not exit" in caplog.text
    assert str(_ORPHAN_PID) in caplog.text
    assert str(record_path) in caplog.text


def test_sidecar_launch_proceeds_when_a_gone_leader_left_an_empty_group(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """The common case: the whole tree died with the service, so only the record is left."""

    host = _FakeProcessHost(identities={_ORPHAN_PID: None})
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    record_path = _write_orphan_record(process, _orphan_record(process))

    assert asyncio.run(process.start()) is False

    assert host.signal_calls == []
    # The launch got past the orphan check rather than failing closed on nothing.
    assert host.spawn_calls
    assert not record_path.exists()


@pytest.mark.parametrize("unscannable", ["record_from_an_older_build", "avibes_own_process_group"])
def test_sidecar_launch_never_scans_a_group_it_must_not_signal(
    tmp_path: Path,
    unscannable: str,
) -> None:
    """Two records whose group must not be swept, for opposite reasons.

    A build that predates the group field leaves nothing but a dead leader's pid,
    which identifies no one. A record naming Avibe's own group cannot have been
    written by ``_isolated_process_group``, and sweeping it would signal Avibe.
    """

    host = _FakeProcessHost(identities={_ORPHAN_PID: None})
    process = _orphan_process(tmp_path, host=host, stop_timeout_seconds=0.1)
    record = _orphan_record(process)
    if unscannable == "record_from_an_older_build":
        del record["process_group"]
    else:
        record["process_group"] = os.getpgrp()
    record_path = _write_orphan_record(process, record)
    asyncio.run(process._ownership.reap())

    assert host.group_scans == []
    assert host.signal_calls == []
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

    child = _Child()
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): {child.pid: _ORPHAN_CREATE_TIME}},
        live_processes={child.pid: _ORPHAN_CREATE_TIME},
    )
    reaped: list[_Child] = []

    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    write_private_text = memory_process._write_private_text

    def refuse_the_record(path: Path, contents: str) -> None:
        if path == process._ownership.record_path:
            raise OSError("record could not be written")
        write_private_text(path, contents)

    terminate_owned_tree = process._terminate_owned_tree

    async def terminate(child, **kwargs) -> None:
        reaped.append(child)
        await terminate_owned_tree(child, **kwargs)

    monkeypatch.setattr(memory_process, "_write_private_text", refuse_the_record)
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
    assert len(host.spawn_calls) == 1
    assert reaped == [child]
    assert process._process is None
    assert not process._ownership.record_path.exists()


_UNUSABLE_RECORDS: dict[str, bytes] = {
    "truncated": b'{"pid": 424242, "create_ti',
    "not_json": b"\x00\x01 not a record at all",
    "oversized": b"{}" + b" " * (5 * 1024),
    "no_pid": b'{"create_time": 1700000000.5}',
}


def test_sidecar_launch_reaps_a_live_sidecar_an_unusable_record_cannot_name(
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

    group_members = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2,
    }
    host = _FakeProcessHost(
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (group_members, [])},
        sidecars={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        live_processes=dict(group_members),
    )
    process = _orphan_process(tmp_path, host=host, stop_timeout_seconds=0.1)
    record_path = process._ownership.record_path
    record_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(record_path.parent.parent, 0o700)
    os.chmod(record_path.parent, 0o700)
    record_path.write_bytes(_UNUSABLE_RECORDS["truncated"])
    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._ownership.reap())

    # The anchor and its group helper, and neither look-alike.
    assert host.signal_calls == [(group_members, signal.SIGTERM, _ORPHAN_PID, None)]
    assert str(_FOREIGN_GROUP_PID) not in caplog.text
    assert str(_FOREIGN_UID_GROUP_PID) not in caplog.text
    assert not record_path.exists()
    assert process._ownership.consume_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    ) is False


@pytest.mark.parametrize("corruption", sorted(_UNUSABLE_RECORDS))
def test_sidecar_launch_proceeds_when_an_unusable_record_names_nothing_running(
    tmp_path: Path,
    short_socket_path: Path,
    corruption: str,
) -> None:
    """An unusable record must not brick startup when nothing of ours survives."""

    host = _FakeProcessHost()
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    record_path = process._ownership.record_path
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(_UNUSABLE_RECORDS[corruption])
    assert asyncio.run(process.start()) is False

    assert host.signal_calls == []
    # The launch got past the record check rather than failing closed on a file.
    assert host.spawn_calls
    assert not record_path.exists()


def test_sidecar_launch_scans_for_recordless_managed_children(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """Every launch checks for a rebuild that died before recording ownership."""

    host = _FakeProcessHost()
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )

    assert not process._ownership.record_path.exists()
    assert asyncio.run(process.start()) is False

    assert host.sidecar_scans == [short_socket_path]
    assert host.spawn_calls


def test_sidecar_launch_fails_closed_when_an_unusable_record_names_a_live_sidecar(
    tmp_path: Path,
    short_socket_path: Path,
    caplog,
) -> None:
    """A sidecar that will not exit fails the launch and keeps the record.

    The record is unusable, so the log is the only thing that can point an operator
    at what is blocking the start.
    """

    host = _FakeProcessHost(
        sidecars={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        live_processes={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        wait_results=deque([False, False]),
        remove_on_signal=False,
    )
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )
    record_path = process._ownership.record_path
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(_UNUSABLE_RECORDS["truncated"])
    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        assert asyncio.run(process.start()) is False

    assert process.last_error == "memory_sidecar_unavailable"
    assert host.spawn_calls == []
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
    monkeypatch.setattr(
        process._ownership,
        "retire_if_group_is_clear",
        lambda _pid, _group: events.append("retired"),
    )

    asyncio.run(process._watch_child(child))

    assert events == ["tree-cleaned", "retired", "reaped"]


@pytest.mark.parametrize(
    "termination_signal",
    [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)],
)
async def test_planned_sidecar_reaps_do_not_consume_crash_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    termination_signal: int,
) -> None:
    """Rebuild handoffs keep restart supervision without charging crashes."""

    restart_delays: list[float] = []
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._memory_dir.mkdir(mode=0o700)

    async def terminate(*_args, **_kwargs) -> None:
        return None

    async def restart_after(delay_seconds: float) -> None:
        restart_delays.append(delay_seconds)

    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)
    monkeypatch.setattr(process, "_restart_after", restart_after)

    for _ in range(5):
        child = _ExitedChild()
        child.returncode = -termination_signal
        _supervising(process, child)
        process._desired_running = True
        process._ownership.record_planned_reap(
            child.pid,
            _ORPHAN_CREATE_TIME,
        )

        await process._watch_child(child)
        assert process._restart_task is not None
        await process._restart_task
        assert process._ownership.consume_planned_reap(
            child.pid,
            _ORPHAN_CREATE_TIME,
        ) is False

    assert process.consecutive_failures == 0
    assert process.down is False
    assert restart_delays == [0.0] * 5


@pytest.mark.parametrize(
    "termination_signal",
    [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)],
)
async def test_unplanned_sidecar_signals_still_consume_crash_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    termination_signal: int,
) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._consecutive_failures = 4
    child = _ExitedChild()
    child.returncode = -termination_signal
    _supervising(process, child)
    process._desired_running = True

    async def terminate(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)

    await process._watch_child(child)

    assert process.consecutive_failures == 5
    assert process.down is True
    assert process._restart_task is None


async def test_planned_reap_from_exited_issuer_cannot_exempt_same_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._memory_dir.mkdir(mode=0o700)
    issuer = """
import sys
from pathlib import Path
from core.memory.process import SidecarOwnership, _MemoryChildRole

root = Path(sys.argv[1])
ownership = SidecarOwnership(
    record_path=Path(sys.argv[2]),
    socket_path=Path(sys.argv[3]),
    provider_root=root,
    role=_MemoryChildRole.CASCADE_REBUILD,
)
ownership.record_planned_reap(int(sys.argv[4]), float(sys.argv[5]))
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            issuer,
            str(process.provider_root),
            str(process._ownership.record_path),
            str(process.socket_path),
            str(_ORPHAN_PID),
            str(_ORPHAN_CREATE_TIME),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )

    process._consecutive_failures = 4
    child = _ExitedChild()
    child.returncode = -signal.SIGTERM
    _supervising(process, child)
    process._desired_running = True

    async def terminate(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)

    await process._watch_child(child)

    assert process.consecutive_failures == 5
    assert process.down is True
    assert process._restart_task is None
    assert process._ownership.consume_planned_reap(
        child.pid,
        _ORPHAN_CREATE_TIME,
    ) is False


async def test_stale_planned_reap_cannot_exempt_replacement_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._memory_dir.mkdir(mode=0o700)
    process._consecutive_failures = 4
    process._ownership.record_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    )
    child = _ExitedChild()
    child.returncode = -signal.SIGTERM
    replacement_create_time = _ORPHAN_CREATE_TIME + 1
    process._process = child
    process._process_group = _ORPHAN_PID
    process._owned_processes = {_ORPHAN_PID: replacement_create_time}
    process._desired_running = True

    async def terminate(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)

    await process._watch_child(child)

    assert process.consecutive_failures == 5
    assert process.down is True
    assert process._restart_task is None


async def test_planned_reap_does_not_restart_a_stopped_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
    )
    process._memory_dir.mkdir(mode=0o700)
    child = _ExitedChild()
    child.returncode = -getattr(signal, "SIGKILL", signal.SIGTERM)
    _supervising(process, child)
    process._desired_running = False
    process._ownership.record_planned_reap(
        child.pid,
        _ORPHAN_CREATE_TIME,
    )

    async def terminate(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(process, "_terminate_owned_tree", terminate)

    await process._watch_child(child)

    assert process.consecutive_failures == 0
    assert process._restart_task is None
    assert process._ownership.consume_planned_reap(
        child.pid,
        _ORPHAN_CREATE_TIME,
    ) is False


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


async def test_sidecar_restart_releases_process_lock_while_host_handoff_waits(
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

    restarting = asyncio.create_task(process._restart_after(0))
    await asyncio.wait_for(callback_entered.wait(), timeout=1)

    # This models a callback already in flight when Stop begins. It must
    # not retain the process lock while waiting on controller ownership.
    await asyncio.wait_for(process.stop(), timeout=1)
    release_callback.set()
    await asyncio.wait_for(restarting, timeout=1)
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

    survivors = (
        {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2}
        if group_holds_a_survivor
        else {}
    )
    host = _FakeProcessHost(groups={_ORPHAN_PID: (survivors, [])})
    process = _orphan_process(tmp_path, host=host, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    child = _ExitedChild()
    _supervising(process, child)
    process._desired_running = False

    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._watch_child(child))

    # The cleanup itself is unchanged: it never signals a group holding a member
    # it cannot confirm, and it finishes rather than failing.
    assert all(call[2] == _ORPHAN_PID for call in host.signal_calls)
    assert process._process is None
    assert record_path.exists() is group_holds_a_survivor
    if group_holds_a_survivor:
        assert "Keeping the EverOS ownership record" in caplog.text
        assert str(_ORPHAN_GROUP_HELPER_PID) in caplog.text
        assert str(record_path) in caplog.text


def test_sidecar_stop_keeps_the_record_while_its_group_holds_a_survivor(
    tmp_path: Path,
) -> None:
    """Stop retires the record on the same evidence, so it needs the same guard.

    Nothing sweeps here -- there is no launch to fail -- so the record is what
    carries the survivor to the next one.
    """

    survivor = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2}
    host = _FakeProcessHost(groups={_ORPHAN_PID: (survivor, [])})
    process = _orphan_process(tmp_path, host=host, stop_timeout_seconds=0.1)
    record_path = _write_orphan_record(process, _orphan_record(process))
    child = _ExitedChild()
    _supervising(process, child)

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

    survivors = {_ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 2}
    host = _FakeProcessHost(
        groups={_ORPHAN_PID: (survivors, [])},
        live_processes=dict(survivors),
        wait_results=deque([False, False]),
        remove_on_signal=False,
    )
    process = _orphan_process(tmp_path, host=host, stop_timeout_seconds=0.1)
    host.identities[_ORPHAN_PID] = _orphan_identity(process)
    record_path = _write_orphan_record(process, _orphan_record(process))

    async def reaped(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(process._ownership, "_terminate_orphan_tree", reaped)

    with pytest.raises(RuntimeError, match="orphaned sidecar group did not exit"):
        asyncio.run(process._ownership.reap())

    assert record_path.exists()
    # Every member of that group is claimed, so the group signal is allowed here.
    assert [call[1:] for call in host.signal_calls] == [
        (signal.SIGTERM, _ORPHAN_PID, None),
        (getattr(signal, "SIGKILL", signal.SIGTERM), _ORPHAN_PID, None),
    ]


class _RebuildChild:
    def __init__(self, exit_code: int | None = 0, *, pid: int = _ORPHAN_PID) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._exit_code = exit_code
        self.waiting = asyncio.Event()

    async def wait(self) -> int:
        self.waiting.set()
        if self._exit_code is None:
            await asyncio.Event().wait()
        self.returncode = self._exit_code
        return int(self.returncode)

    def send_signal(self, _signum: int) -> None:
        return None


def _rebuild_process(
    tmp_path: Path,
    host: _FakeProcessHost,
    *,
    timeout_seconds: float = 1.0,
    settings: EverOSProcessSettings | None = None,
) -> EverOSRebuildProcess:
    return EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=settings or _settings(),
        timeout_seconds=timeout_seconds,
        stop_timeout_seconds=0.1,
        _host=host,
    )


def _runtime_rebuild_candidate() -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        recovery_intent="rebuild",
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                base_url="https://llm.example.test/v1",
                model="chat",
                api_key="llm-secret",
            ),
            embedding=MemoryEndpointConfig(
                base_url="https://embed.example.test/v1",
                model="embed",
                api_key="embedding-secret",
            ),
        ),
    )


def _runtime_artifact() -> FakeMemoryArtifactManager:
    return FakeMemoryArtifactManager(
        python=Path(sys.executable),
        root_format="everos-1.2.3",
        fingerprint="test-artifact",
        status_payload={"reason": None},
    )


def _inject_real_rebuild_process(
    monkeypatch: pytest.MonkeyPatch,
    host: _FakeProcessHost,
) -> None:
    async def preflight(
        self: memory_runtime.MemoryRuntime,
        config: MemoryConfig | None = None,
    ) -> dict[str, bool]:
        return {"ok": True}

    def rebuild_process(
        python,
        *,
        effective_home,
        provider_root,
        settings,
    ) -> EverOSRebuildProcess:
        return EverOSRebuildProcess(
            python,
            effective_home=effective_home,
            provider_root=provider_root,
            settings=settings,
            timeout_seconds=30 * 60,
            stop_timeout_seconds=0.1,
            _host=host,
        )

    monkeypatch.setattr(memory_runtime.MemoryRuntime, "preflight", preflight)
    monkeypatch.setattr(memory_runtime, "EverOSRebuildProcess", rebuild_process)


async def test_runtime_close_interrupts_rebuild_and_releases_owned_tree_and_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = _runtime_rebuild_candidate()
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    _inject_real_rebuild_process(monkeypatch, host)
    monkeypatch.setattr(
        memory_runtime.V2Config,
        "load",
        classmethod(lambda cls, config_path=None: SimpleNamespace(memory=candidate)),
    )
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_runtime_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True)
    rebuilding = asyncio.create_task(runtime.rebuild())
    await child.waiting.wait()
    record_path = memory_process.sidecar_record_path(tmp_path / "memory")
    assert record_path.exists()

    await asyncio.wait_for(memory_runtime_factory.close(runtime), timeout=1)

    assert await rebuilding == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "interrupted",
    }
    assert not host.live_processes
    assert host.signal_calls
    assert not record_path.exists()
    competing = MemoryOperationLease(tmp_path)
    competing.acquire()
    competing.release()


async def test_cancelled_runtime_close_waits_for_rebuild_cleanup_and_lease_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_runtime_factory,
) -> None:
    class _BlockingCleanupHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            return await super().wait_for_exit(*args, **kwargs)

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    candidate = _runtime_rebuild_candidate()
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _BlockingCleanupHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    _inject_real_rebuild_process(monkeypatch, host)
    monkeypatch.setattr(
        memory_runtime.V2Config,
        "load",
        classmethod(lambda cls, config_path=None: SimpleNamespace(memory=candidate)),
    )
    runtime = memory_runtime_factory(
        candidate,
        artifact_manager=_runtime_artifact(),
        process_factory=FakeEverOSProcessFactory(),
        effective_home=tmp_path,
    )
    monkeypatch.setattr(runtime, "_provider_data_exists_strict", lambda: True)
    rebuilding = asyncio.create_task(runtime.rebuild())
    await child.waiting.wait()
    record_path = memory_process.sidecar_record_path(tmp_path / "memory")
    closing = asyncio.create_task(runtime.close())
    await host.cleanup_started.wait()

    closing.cancel()
    await asyncio.sleep(0)
    assert closing.done() is False
    assert record_path.exists()
    competing = MemoryOperationLease(tmp_path)
    with pytest.raises(MemoryOperationBusy):
        competing.acquire()

    host.release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)

    assert await rebuilding == {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "interrupted",
    }
    assert not host.live_processes
    assert not record_path.exists()
    competing.acquire()
    competing.release()
    await memory_runtime_factory.close(runtime)


async def test_rebuild_requires_only_complete_embedding_settings(tmp_path: Path) -> None:
    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    settings = replace(
        _settings(),
        llm_base_url=None,
        llm_model=None,
        llm_api_key=None,
    )

    assert await _rebuild_process(tmp_path, host, settings=settings).run() is (
        RebuildProcessResult.COMPLETED
    )
    assert len(host.spawn_calls) == 1


async def test_rebuild_normalizes_relative_provider_root_before_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    provider_parent = tmp_path / "shared"
    provider_parent.mkdir(mode=0o700)
    expected_root = provider_parent / "everos-root"
    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    process = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path / "home",
        provider_root=Path("shared/everos-root"),
        settings=_settings(),
        _host=host,
    )

    assert await process.run() is RebuildProcessResult.COMPLETED
    assert process._provider_root == expected_root
    assert (expected_root / "everos.toml").is_file()
    assert host.spawn_calls[0][4]["EVEROS_ROOT"] == str(expected_root)
    assert memory_process._provider_rebuild_lock_path(
        provider_root=process._provider_root
    ).parent.parent == provider_parent


def test_sidecar_and_rebuild_use_one_physical_identity_for_a_symlinked_home(
    tmp_path: Path,
) -> None:
    physical_home = tmp_path / ".avibe"
    physical_home.mkdir(mode=0o700)
    logical_home = tmp_path / ".vibe_remote"
    logical_home.symlink_to(physical_home, target_is_directory=True)
    expected_home = paths.physical_home(logical_home)

    sidecar = EverOSProcess(
        sys.executable,
        effective_home=logical_home,
        settings=_settings(),
    )
    rebuild = EverOSRebuildProcess(
        sys.executable,
        effective_home=logical_home,
        settings=_settings(),
    )
    memory_process._prepare_memory_child_directories(
        memory_dir=sidecar._memory_dir,
        provider_root=sidecar.provider_root,
        settings=sidecar._settings,
    )

    expected_provider_root = expected_home / "memory" / "everos-root"
    assert sidecar._effective_home == expected_home
    assert sidecar.provider_root == expected_provider_root
    assert sidecar.socket_path == expected_home / "memory" / ".rt" / "everos.sock"
    assert sidecar._child_environment()["EVEROS_ROOT"] == str(expected_provider_root)
    assert rebuild._effective_home == expected_home
    assert rebuild._provider_root == expected_provider_root
    assert memory_process._provider_rebuild_lock_path(
        provider_root=sidecar.provider_root,
    ) == memory_process._provider_rebuild_lock_path(
        provider_root=rebuild._provider_root,
    )

    logical_socket = logical_home / "memory" / ".rt" / "everos.sock"
    logical_provider_root = logical_home / "memory" / "everos-root"
    legacy_spelling_record = {
        "pid": _ORPHAN_PID,
        "create_time": _ORPHAN_CREATE_TIME,
        "process_group": _ORPHAN_PID,
        "socket_path": str(logical_socket),
        "provider_root": str(logical_provider_root),
        "role": "sidecar",
        "python": sys.executable,
    }
    legacy_spelling_identity = _ProcessIdentity(
        stamp=_ORPHAN_CREATE_TIME,
        cmdline=(
            sys.executable,
            "-m",
            _SIDECAR_ENTRYPOINT_MODULE,
            "--uds",
            str(logical_socket),
        ),
        uid=os.getuid() if hasattr(os, "getuid") else None,
        environment={
            "EVEROS_ROOT": str(logical_provider_root),
            "AVIBE_MEMORY_CHILD_ROLE": "sidecar",
        },
        wall_create_time=_ORPHAN_CREATE_TIME,
    )
    assert _classify_recorded_child(
        legacy_spelling_record,
        legacy_spelling_identity,
        socket_path=sidecar.socket_path,
        provider_root=sidecar.provider_root,
        role=_MemoryChildRole.SIDECAR,
    ) is _RecordedSidecar.OURS


def test_provider_root_aliases_share_lock_identity_without_changing_access_path(
    tmp_path: Path,
) -> None:
    provider_parent = tmp_path / "provider-parent"
    provider_parent.mkdir(mode=0o700)
    alias_parent = tmp_path / "legacy-provider-parent"
    alias_parent.symlink_to(provider_parent, target_is_directory=True)
    expected_root = provider_parent / "everos-root"

    sidecar = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "sidecar-home",
        provider_root=alias_parent / "everos-root",
        settings=_settings(),
    )
    rebuild = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path / "rebuild-home",
        provider_root=expected_root,
        settings=_settings(),
    )

    alias_root = alias_parent / "everos-root"
    assert sidecar.provider_root == alias_root
    assert rebuild._provider_root == expected_root
    assert sidecar._ownership._provider_root == alias_root
    assert rebuild._ownership._provider_root == expected_root
    assert sidecar._child_environment()["EVEROS_ROOT"] == str(alias_root)
    assert memory_process._provider_rebuild_lock_path(
        provider_root=sidecar.provider_root,
    ) == memory_process._provider_rebuild_lock_path(
        provider_root=rebuild._provider_root,
    )
    sidecar._ownership.record_launch(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
        None,
    )
    record = json.loads(sidecar._ownership.record_path.read_text(encoding="utf-8"))
    assert record["provider_root"] == str(alias_root)


async def test_rebuild_rejects_final_provider_root_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(mode=0o700)
    target = tmp_path / "provider-target"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    provider_root = memory_dir / "everos-root"
    provider_root.symlink_to(target, target_is_directory=True)
    host = _FakeProcessHost()
    process = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path,
        provider_root=provider_root,
        settings=_settings(),
        _host=host,
    )

    assert await process.run() is RebuildProcessResult.FAILED
    assert host.spawn_calls == []
    assert provider_root.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in target.iterdir()) == ["sentinel"]


async def test_direct_ownership_reap_rejects_provider_root_symlink(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(mode=0o700)
    target = tmp_path / "provider-target"
    target.mkdir(mode=0o700)
    provider_root = memory_dir / "everos-root"
    provider_root.symlink_to(target, target_is_directory=True)
    host = _FakeProcessHost(root_sidecars={_ORPHAN_PID: _ORPHAN_CREATE_TIME})
    ownership = memory_process.SidecarOwnership(
        record_path=memory_process.sidecar_record_path(memory_dir),
        socket_path=memory_dir / ".rt" / "everos.sock",
        provider_root=provider_root,
        _host=host,
    )

    with pytest.raises(RuntimeError, match="provider root chain contains a symlink"):
        await ownership.reap(discover_missing=True)
    assert host.signal_calls == []


def test_case_aliases_share_existing_provider_root_lock_identity(
    tmp_path: Path,
) -> None:
    provider_parent = tmp_path / "ProviderData"
    provider_parent.mkdir(mode=0o700)
    provider_root = provider_parent / "EverOS"
    provider_root.mkdir(mode=0o700)
    case_alias = provider_parent / "everos"
    if case_alias == provider_root or not case_alias.exists():
        pytest.skip("filesystem is case-sensitive")

    first = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path / "first-home",
        provider_root=provider_root,
        settings=_settings(),
    )
    second = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path / "second-home",
        provider_root=case_alias,
        settings=_settings(),
    )
    first_lock = first._provider_root_lock()
    second_lock = second._provider_root_lock()
    first_lock.acquire()
    try:
        with pytest.raises(memory_process._ProviderRootBusy):
            second_lock.acquire()
    finally:
        first_lock.release()
        second_lock.release()


async def test_rebuild_normalizes_relative_interpreter_before_child_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    process = EverOSRebuildProcess(
        Path(".venv/bin/python"),
        effective_home=tmp_path / "home",
        settings=_settings(),
        _host=host,
    )

    assert await process.run() is RebuildProcessResult.COMPLETED
    assert process._python == interpreter
    assert host.spawn_calls[0][1] == interpreter


@pytest.mark.parametrize(
    "missing_field",
    ["embedding_base_url", "embedding_model", "embedding_api_key"],
)
async def test_rebuild_never_spawns_with_incomplete_embedding_settings(
    tmp_path: Path,
    missing_field: str,
) -> None:
    host = _FakeProcessHost()
    settings = replace(_settings(), **{missing_field: None})

    assert await _rebuild_process(tmp_path, host, settings=settings).run() is (
        RebuildProcessResult.FAILED
    )
    assert host.spawn_calls == []


async def test_rebuild_without_artifact_python_is_recovery_only(tmp_path: Path) -> None:
    host = _FakeProcessHost()
    process = EverOSRebuildProcess(
        None,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )

    assert await process.run() is RebuildProcessResult.FAILED
    assert host.spawn_calls == []


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (0, RebuildProcessResult.COMPLETED),
        (3, RebuildProcessResult.ROOT_BUSY),
        (130, RebuildProcessResult.INTERRUPTED),
        (1, RebuildProcessResult.FAILED),
    ],
)
async def test_rebuild_maps_closed_results_and_reaps_the_whole_group(
    tmp_path: Path,
    exit_code: int,
    expected: RebuildProcessResult,
) -> None:
    child = _RebuildChild(exit_code)
    identities = {
        child.pid: _ORPHAN_CREATE_TIME,
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
    }
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )

    result = await _rebuild_process(tmp_path, host).run()

    assert result is expected
    assert not host.live_processes
    kind, python, _cwd, socket_path, environment = host.spawn_calls[0]
    assert kind is _ProcessKind.CASCADE_REBUILD
    assert python == Path(sys.executable)
    assert socket_path is None
    assert environment["EVEROS_ROOT"] == str(tmp_path / "memory" / "everos-root")
    assert environment["AVIBE_MEMORY_CHILD_ROLE"] == "cascade_rebuild"


async def test_rebuild_timeout_terminates_and_reaps_before_returning(tmp_path: Path) -> None:
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )

    result = await _rebuild_process(tmp_path, host, timeout_seconds=0.01).run()

    assert result is RebuildProcessResult.TIMED_OUT
    assert not host.live_processes
    assert host.signal_calls


async def test_rebuild_never_reports_completed_while_a_late_group_helper_survives(
    tmp_path: Path,
) -> None:
    child = _RebuildChild(0)
    root = {child.pid: _ORPHAN_CREATE_TIME}
    helper = {_ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1}
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): root},
        groups={child.pid: (helper, [])},
        live_processes={**root, **helper},
        wait_results=deque([True, False, False]),
        remove_on_signal=False,
    )

    result = await _rebuild_process(tmp_path, host).run()

    assert result is RebuildProcessResult.FAILED
    assert host.live_processes
    assert (tmp_path / "memory" / ".rt" / "everos.sidecar.json").exists()


async def test_rebuild_never_reports_completed_with_an_unverifiable_group_member(
    tmp_path: Path,
) -> None:
    child = _RebuildChild(0)
    root = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): root},
        groups={child.pid: ({}, [_FOREIGN_GROUP_PID])},
        live_processes={
            **root,
            _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 1,
        },
    )

    result = await _rebuild_process(tmp_path, host).run()

    assert result is RebuildProcessResult.FAILED
    assert host.live_processes == {
        _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 1,
    }
    assert (tmp_path / "memory" / ".rt" / "everos.sidecar.json").exists()


async def test_rebuild_keeps_late_unverifiable_member_found_during_group_cleanup(
    tmp_path: Path,
) -> None:
    child = _RebuildChild(0)
    root = {child.pid: _ORPHAN_CREATE_TIME}
    helper = {_ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1}

    class _LateForeignHost(_FakeProcessHost):
        group_reads = 0

        def recorded_group_members(
            self,
            process_group: int,
            *,
            socket_path: Path,
            provider_root: Path,
            role=None,
        ) -> tuple[dict[int, float], list[int]]:
            del process_group, socket_path, provider_root, role
            self.group_reads += 1
            if self.group_reads == 1:
                return dict(helper), []
            return {}, [_FOREIGN_GROUP_PID]

    host = _LateForeignHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): root},
        live_processes={
            **root,
            **helper,
            _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 2,
        },
    )

    result = await _rebuild_process(tmp_path, host).run()

    assert result is RebuildProcessResult.FAILED
    assert host.live_processes == {
        _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 2,
    }
    assert (tmp_path / "memory" / ".rt" / "everos.sidecar.json").exists()


async def test_rebuild_cancellation_returns_interrupted_after_reaping(tmp_path: Path) -> None:
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    task = asyncio.create_task(_rebuild_process(tmp_path, host).run())
    await child.waiting.wait()

    task.cancel()
    result = await task

    assert result is RebuildProcessResult.INTERRUPTED
    assert not host.live_processes
    assert host.signal_calls


async def test_rebuild_cancellation_waits_for_spawn_handoff_then_reaps(tmp_path: Path) -> None:
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    releases: list[int] = []
    child.send_signal = releases.append

    class _BlockingSpawnHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.spawned = asyncio.Event()
            self.release_spawn = asyncio.Event()

        async def spawn(self, *args, **kwargs):
            self.live_processes[child.pid] = _ORPHAN_CREATE_TIME
            self.spawned.set()
            await self.release_spawn.wait()
            return await super().spawn(*args, **kwargs)

    host = _BlockingSpawnHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
    )
    task = asyncio.create_task(_rebuild_process(tmp_path, host).run())
    await host.spawned.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    host.release_spawn.set()

    assert await task is RebuildProcessResult.INTERRUPTED
    assert releases == [signal.SIGCONT]
    assert not host.live_processes
    assert host.signal_calls
    assert not (tmp_path / "memory" / ".rt" / "everos.sidecar.json").exists()


async def test_rebuild_cleanup_survives_a_second_cancellation(tmp_path: Path) -> None:
    class _BlockingCleanupHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            return await super().wait_for_exit(*args, **kwargs)

    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _BlockingCleanupHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    task = asyncio.create_task(_rebuild_process(tmp_path, host).run())
    await child.waiting.wait()

    task.cancel()
    await host.cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    host.release_cleanup.set()

    assert await task is RebuildProcessResult.INTERRUPTED
    assert not host.live_processes
    assert not (tmp_path / "memory" / ".rt" / "everos.sidecar.json").exists()


async def test_rebuild_cancellation_during_cleanup_cannot_report_completed(tmp_path: Path) -> None:
    class _BlockingCleanupHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            return await super().wait_for_exit(*args, **kwargs)

    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _BlockingCleanupHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    task = asyncio.create_task(_rebuild_process(tmp_path, host).run())
    await host.cleanup_started.wait()

    task.cancel()
    host.release_cleanup.set()

    assert await task is RebuildProcessResult.INTERRUPTED
    assert not host.live_processes


def test_rebuild_default_deadline_is_thirty_minutes(tmp_path: Path) -> None:
    process = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=_FakeProcessHost(),
    )

    assert _REBUILD_TIMEOUT_SECONDS == 30 * 60
    assert process._timeout_seconds == 30 * 60


async def test_system_host_uses_the_exact_pinned_rebuild_argv(monkeypatch, tmp_path: Path) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def create_subprocess(*arguments, **options):
        captured.append((arguments, options))
        return object()

    monkeypatch.setattr(memory_process.asyncio, "create_subprocess_exec", create_subprocess)

    await _SystemProcessHost().spawn(
        _ProcessKind.CASCADE_REBUILD,
        Path("/artifact/bin/python"),
        cwd=tmp_path,
        env={"EVEROS_ROOT": str(tmp_path / "root")},
    )

    assert captured[0][0] == (
        "/artifact/bin/python",
        "-m",
        "core.memory.rebuild_child",
        "cascade",
        "rebuild",
        "--yes",
    )
    assert captured[0][1]["start_new_session"] is True


def test_rebuild_child_installs_scrubbers_before_delegating(monkeypatch) -> None:
    from core.memory import rebuild_child

    calls: list[object] = []
    monkeypatch.setattr(
        rebuild_child,
        "_set_private_umask",
        lambda: calls.append("private-umask"),
        raising=False,
    )
    monkeypatch.setattr(
        rebuild_child,
        "_await_parent_ownership",
        lambda: calls.append("ownership-established"),
        raising=False,
    )
    monkeypatch.setattr(
        rebuild_child,
        "install_error_scrubbers",
        lambda: calls.append("scrubbers"),
    )
    monkeypatch.setattr(
        rebuild_child.runpy,
        "run_module",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert rebuild_child.main() == 0
    assert calls == [
        "private-umask",
        "ownership-established",
        "scrubbers",
        (("everos.entrypoints.cli.main",), {"run_name": "__main__", "alter_sys": True}),
    ]


@pytest.mark.parametrize("exit_code", [0, 3, 130])
def test_rebuild_child_real_seam_scrubs_before_cli_and_preserves_exit_code(
    tmp_path: Path,
    exit_code: int,
) -> None:
    artifact_site = tmp_path / "artifact-site"
    packages = (
        "everos",
        "everos/entrypoints",
        "everos/entrypoints/cli",
        "everos/infra",
        "everos/infra/ome",
        "everos/infra/ome/_stores",
        "everos/infra/persistence",
        "everos/infra/persistence/sqlite",
        "everos/infra/persistence/sqlite/repos",
    )
    for package in packages:
        directory = artifact_site / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (artifact_site / "everos/infra/ome/_stores/run_record.py").write_text(
        "class RunRecordStore:\n"
        "    async def _update_status(self, *args, **kwargs):\n"
        "        return None\n",
        encoding="utf-8",
    )
    (artifact_site / "everos/infra/persistence/sqlite/repos/md_change_state.py").write_text(
        "class StateRepo:\n"
        "    async def mark_failed(self, *args, **kwargs):\n"
        "        return None\n"
        "md_change_state_repo = StateRepo()\n",
        encoding="utf-8",
    )
    (artifact_site / "everos/entrypoints/cli/main.py").write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from everos.infra.ome._stores.run_record import RunRecordStore\n"
        "from everos.infra.persistence.sqlite.repos.md_change_state import md_change_state_repo\n"
        "assert getattr(RunRecordStore._update_status, '__avibe_memory_call_patch__', False)\n"
        "assert getattr(type(md_change_state_repo).mark_failed, '__avibe_memory_call_patch__', False)\n"
        "assert sys.argv[1:] == ['cascade', 'rebuild', '--yes']\n"
        "assert os.environ['AVIBE_MEMORY_CHILD_ROLE'] == 'cascade_rebuild'\n"
        "root = Path(os.environ['EVEROS_ROOT'])\n"
        "probe_dir = root / 'mode-probe'\n"
        "probe_dir.mkdir(parents=True)\n"
        "(probe_dir / 'created.txt').write_text('private', encoding='utf-8')\n"
        "raise SystemExit(int(os.environ['TEST_REBUILD_EXIT']))\n",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memory"
    provider_root = memory_dir / "everos-root"
    memory_dir.mkdir()
    source_root = Path(__file__).resolve().parents[1]
    environment = memory_process._memory_child_environment(
        python=Path(sys.executable),
        memory_dir=memory_dir,
        provider_root=provider_root,
        attachments_root=memory_dir / "attachments",
        settings=_settings(),
        role=_MemoryChildRole.CASCADE_REBUILD,
    )
    environment["PYTHONPATH"] = os.pathsep.join((str(source_root), str(artifact_site)))
    environment["TEST_REBUILD_EXIT"] = str(exit_code)

    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.memory.rebuild_child",
            "cascade",
            "rebuild",
            "--yes",
        ],
        cwd=memory_dir,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert asyncio.run(_SystemProcessHost().wait_for_stopped(child.pid, 10))
    child_process = psutil.Process(child.pid)
    assert child.poll() is None
    assert child_process.status() == psutil.STATUS_STOPPED
    child.send_signal(signal.SIGCONT)
    _stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == exit_code, stderr
    assert stat.S_IMODE((provider_root / "mode-probe").stat().st_mode) == 0o700
    assert stat.S_IMODE((provider_root / "mode-probe/created.txt").stat().st_mode) == 0o600


async def test_rebuild_releases_child_only_after_persisting_ownership(tmp_path: Path) -> None:
    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    releases: list[int] = []

    def release(signum: int) -> None:
        assert process._ownership.record_path.exists()
        releases.append(signum)

    child.send_signal = release

    assert await process.run() is RebuildProcessResult.COMPLETED
    assert releases == [signal.SIGCONT]


async def test_rebuild_waits_for_stopped_handshake_before_record_and_release(
    tmp_path: Path,
) -> None:
    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    releases: list[int] = []
    child.send_signal = releases.append

    class _DelayedHandshakeHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.handshake_started = asyncio.Event()
            self.child_stopped = asyncio.Event()

        async def wait_for_stopped(self, pid: int, timeout_seconds: float) -> bool:
            del pid, timeout_seconds
            self.handshake_started.set()
            await self.child_stopped.wait()
            return True

    host = _DelayedHandshakeHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    task = asyncio.create_task(process.run())

    await asyncio.wait_for(host.handshake_started.wait(), timeout=0.5)
    assert releases == []
    assert not process._ownership.record_path.exists()
    host.child_stopped.set()

    assert await task is RebuildProcessResult.COMPLETED
    assert releases == [signal.SIGCONT]


async def test_rebuild_cancellation_finishes_stopped_handoff_before_cleanup(
    tmp_path: Path,
) -> None:
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    releases: list[int] = []
    child.send_signal = releases.append

    class _DelayedHandshakeHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.handshake_started = asyncio.Event()
            self.child_stopped = asyncio.Event()

        async def wait_for_stopped(self, pid: int, timeout_seconds: float) -> bool:
            del pid, timeout_seconds
            self.handshake_started.set()
            await self.child_stopped.wait()
            return True

    host = _DelayedHandshakeHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    task = asyncio.create_task(process.run())

    await asyncio.wait_for(host.handshake_started.wait(), timeout=0.5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert releases == []
    assert not process._ownership.record_path.exists()
    host.child_stopped.set()

    assert await task is RebuildProcessResult.INTERRUPTED
    assert releases == [signal.SIGCONT]
    assert not host.live_processes
    assert not process._ownership.record_path.exists()


async def test_rebuild_never_releases_without_a_stopped_handshake(tmp_path: Path) -> None:
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    releases: list[int] = []
    child.send_signal = releases.append

    class _MissingHandshakeHost(_FakeProcessHost):
        async def wait_for_stopped(self, pid: int, timeout_seconds: float) -> bool:
            del pid, timeout_seconds
            return False

    host = _MissingHandshakeHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )

    assert await _rebuild_process(tmp_path, host).run() is RebuildProcessResult.FAILED
    assert releases == []
    assert not host.live_processes


async def test_rebuild_refuses_a_provider_root_locked_by_another_process(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    (memory_dir / "everos-root").mkdir(parents=True)
    memory_dir.chmod(0o700)
    lock_path = memory_process._provider_rebuild_lock_path(
        provider_root=memory_dir / "everos-root",
    )
    lock_path.parent.mkdir(parents=True)
    script = "\n".join(
        (
            "import fcntl",
            "import os",
            "import sys",
            "from pathlib import Path",
            "descriptor = os.open(Path(sys.argv[1]), os.O_RDWR | os.O_CREAT, 0o600)",
            "fcntl.flock(descriptor, fcntl.LOCK_EX)",
            "print('locked', flush=True)",
            "sys.stdin.read(1)",
            "fcntl.flock(descriptor, fcntl.LOCK_UN)",
            "os.close(descriptor)",
        )
    )
    locker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(lock_path),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert locker.stdout is not None
    assert await locker.stdout.readline() == b"locked\n"
    host = _FakeProcessHost()
    try:
        assert await _rebuild_process(tmp_path, host).run() is RebuildProcessResult.ROOT_BUSY
        assert host.spawn_calls == []
    finally:
        assert locker.stdin is not None
        locker.stdin.write(b"\n")
        await locker.stdin.drain()
        locker.stdin.close()
        await locker.wait()


async def test_rebuild_lock_is_shared_across_effective_homes_for_one_provider_root(
    tmp_path: Path,
) -> None:
    provider_root = tmp_path / "shared" / "everos-root"
    owner_home = tmp_path / "owner-home"
    contender_home = tmp_path / "contender-home"
    provider_root.parent.mkdir(mode=0o700)
    owner_home.mkdir(mode=0o700)
    contender_home.mkdir(mode=0o700)
    script = "\n".join(
        (
            "import asyncio",
            "import sys",
            "from core.memory.process import EverOSProcessSettings, EverOSRebuildProcess, RebuildProcessResult",
            "from pathlib import Path",
            "async def main():",
            "    process = EverOSRebuildProcess(",
            "        sys.executable,",
            "        effective_home=Path(sys.argv[1]),",
            "        provider_root=Path(sys.argv[2]),",
            "        settings=EverOSProcessSettings(",
            "            embedding_base_url='https://embedding.invalid/v1',",
            "            embedding_model='embedding-model',",
            "            embedding_api_key='secret',",
            "        ),",
            "    )",
            "    async def hold_lock():",
            "        print('locked', flush=True)",
            "        await asyncio.to_thread(sys.stdin.read, 1)",
            "        return RebuildProcessResult.COMPLETED",
            "    process._run_exclusive = hold_lock",
            "    assert await process.run() is RebuildProcessResult.COMPLETED",
            "asyncio.run(main())",
        )
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(owner_home),
        str(provider_root),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert owner.stdout is not None
    ready = await owner.stdout.readline()
    if ready != b"locked\n":
        assert owner.stderr is not None
        pytest.fail((await owner.stderr.read()).decode(errors="replace"))
    lock_path = memory_process._provider_rebuild_lock_path(provider_root=provider_root)
    assert lock_path.parent.parent == provider_root.parent
    provider_root.rmdir()
    contender_host = _FakeProcessHost()
    contender = EverOSRebuildProcess(
        sys.executable,
        effective_home=contender_home,
        provider_root=provider_root,
        settings=_settings(),
        stop_timeout_seconds=0.1,
        _host=contender_host,
    )
    reconcile_entered = False

    async def record_reconcile(*, discover_missing: bool = False) -> None:
        nonlocal reconcile_entered
        del discover_missing
        reconcile_entered = True

    contender._ownership.reap = record_reconcile
    try:
        with pytest.raises(memory_process._ProviderRootBusy):
            await contender.reconcile_orphan()
        assert reconcile_entered is False
        assert not provider_root.exists()
        assert await contender.run() is RebuildProcessResult.ROOT_BUSY
        assert reconcile_entered is False
        assert contender_host.spawn_calls == []
        assert not provider_root.exists()

        provider_root.mkdir(mode=0o755)
        sentinel = provider_root / "owner-data"
        sentinel.write_text("unchanged", encoding="utf-8")
        assert await contender.run() is RebuildProcessResult.ROOT_BUSY
        assert stat.S_IMODE(provider_root.stat().st_mode) == 0o755
        assert sentinel.read_text(encoding="utf-8") == "unchanged"
        assert sorted(path.name for path in provider_root.iterdir()) == ["owner-data"]
        assert contender._ownership.record_path != memory_process.sidecar_record_path(
            owner_home / "memory"
        )
    finally:
        assert owner.stdin is not None
        owner.stdin.write(b"\n")
        await owner.stdin.drain()
        owner.stdin.close()
        await owner.wait()
        assert owner.returncode == 0


async def _hold_provider_root_lock(provider_root: Path) -> asyncio.subprocess.Process:
    lock_path = memory_process._provider_rebuild_lock_path(provider_root=provider_root)
    lock_path.parent.mkdir(mode=0o700)
    script = "\n".join(
        (
            "import fcntl",
            "import os",
            "import sys",
            "descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)",
            "fcntl.flock(descriptor, fcntl.LOCK_EX)",
            "print('locked', flush=True)",
            "sys.stdin.read(1)",
            "fcntl.flock(descriptor, fcntl.LOCK_UN)",
            "os.close(descriptor)",
        )
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(lock_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert owner.stdout is not None
    assert await owner.stdout.readline() == b"locked\n"
    return owner


async def _release_provider_root_lock(owner: asyncio.subprocess.Process) -> None:
    assert owner.stdin is not None
    owner.stdin.write(b"\n")
    await owner.stdin.drain()
    owner.stdin.close()
    await owner.wait()
    assert owner.returncode == 0


@pytest.mark.parametrize("entrypoint", ["start", "supervisor_restart"])
async def test_sidecar_start_and_restart_wait_for_shared_rebuild_lock(
    tmp_path: Path,
    monkeypatch,
    short_socket_path: Path,
    entrypoint: str,
) -> None:
    provider_parent = tmp_path / "shared"
    provider_parent.mkdir(mode=0o700)
    provider_root = provider_parent / "everos-root"
    provider_root.mkdir(mode=0o755)
    sentinel = provider_root / "owner-data"
    sentinel.write_text("unchanged", encoding="utf-8")
    owner = await _hold_provider_root_lock(provider_root)
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
        live_processes=dict(identities),
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "other-home",
        provider_root=provider_root,
        socket_path=short_socket_path,
        settings=_settings(),
        _host=host,
    )
    lock_attempted = asyncio.Event()
    ownership_scan = asyncio.Event()
    original_acquire = memory_process._ProviderRootLock.acquire

    def observe_lock_attempt(root_lock) -> None:
        lock_attempted.set()
        original_acquire(root_lock)

    async def observe_reap(*, discover_missing: bool = False) -> None:
        assert discover_missing is True
        ownership_scan.set()

    async def ready(_child) -> None:
        return None

    monkeypatch.setattr(memory_process._ProviderRootLock, "acquire", observe_lock_attempt)
    monkeypatch.setattr(process._ownership, "reap", observe_reap)
    monkeypatch.setattr(process, "_wait_for_ready", ready)
    monkeypatch.setattr(process, "_secure_socket", lambda: None)
    monkeypatch.setattr(process, "_assert_no_tcp_listener", lambda *_args, **_kwargs: None)
    if entrypoint == "start":
        start_task = asyncio.create_task(process.start())
    else:
        process._desired_running = True
        start_task = asyncio.create_task(process._restart_after(0))
    try:
        await asyncio.wait_for(lock_attempted.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert not start_task.done()
        assert not ownership_scan.is_set()
        assert host.spawn_calls == []
        assert stat.S_IMODE(provider_root.stat().st_mode) == 0o755
        assert sentinel.read_text(encoding="utf-8") == "unchanged"
        assert sorted(path.name for path in provider_root.iterdir()) == ["owner-data"]

        await _release_provider_root_lock(owner)
        result = await asyncio.wait_for(start_task, timeout=1.0)
        assert result is (True if entrypoint == "start" else None)
        assert process.running
        assert ownership_scan.is_set()
        assert len(host.spawn_calls) == 1
    finally:
        if owner.returncode is None:
            await _release_provider_root_lock(owner)
        if process.running:
            await process.stop()


async def test_sidecar_stop_and_cancellation_end_a_busy_root_wait(
    tmp_path: Path,
    monkeypatch,
    short_socket_path: Path,
) -> None:
    provider_parent = tmp_path / "shared"
    provider_parent.mkdir(mode=0o700)
    provider_root = provider_parent / "everos-root"
    provider_root.mkdir(mode=0o755)
    owner = await _hold_provider_root_lock(provider_root)
    host = _FakeProcessHost()
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "other-home",
        provider_root=provider_root,
        socket_path=short_socket_path,
        settings=_settings(),
        _host=host,
    )
    lock_attempted = asyncio.Event()
    original_acquire = memory_process._ProviderRootLock.acquire

    def observe_lock_attempt(root_lock) -> None:
        lock_attempted.set()
        original_acquire(root_lock)

    monkeypatch.setattr(memory_process._ProviderRootLock, "acquire", observe_lock_attempt)
    try:
        start_task = asyncio.create_task(process.start())
        await asyncio.wait_for(lock_attempted.wait(), timeout=0.5)
        await asyncio.wait_for(process.stop(), timeout=0.5)
        assert await asyncio.wait_for(start_task, timeout=0.5) is False
        assert process.consecutive_failures == 0
        assert process.last_error is None
        assert process.down is False
        assert process.starting is False
        assert host.spawn_calls == []

        lock_attempted.clear()
        cancelled_start = asyncio.create_task(process.start())
        await asyncio.wait_for(lock_attempted.wait(), timeout=0.5)
        cancelled_start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_start
        assert process.consecutive_failures == 0
        assert process.last_error is None
        assert process.down is False
        assert process.starting is False
        assert host.spawn_calls == []
    finally:
        await process.stop()
        await _release_provider_root_lock(owner)


async def test_sidecar_stop_does_not_retire_an_active_rebuild_record(
    tmp_path: Path,
) -> None:
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )
    process._process = child
    process._process_group = child.pid
    process._owned_processes = dict(identities)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)
    process._ownership.record_path.write_text(
        json.dumps(
            {
                "pid": _ORPHAN_DESCENDANT_PID,
                "create_time": _ORPHAN_CREATE_TIME + 1,
                "process_group": _ORPHAN_DESCENDANT_PID,
                "socket_path": str(process._socket_path),
                "provider_root": str(process._provider_root),
                "role": "cascade_rebuild",
                "python": sys.executable,
            }
        ),
        encoding="utf-8",
    )

    await process.stop()

    record = json.loads(process._ownership.record_path.read_text(encoding="utf-8"))
    assert record["role"] == "cascade_rebuild"
    assert record["pid"] == _ORPHAN_DESCENDANT_PID


@pytest.mark.parametrize("cancel_stage", ["spawn_handoff", "readiness"])
async def test_cancelled_sidecar_start_holds_root_lock_through_owned_cleanup(
    tmp_path: Path,
    monkeypatch,
    short_socket_path: Path,
    cancel_stage: str,
) -> None:
    provider_parent = tmp_path / "shared"
    provider_parent.mkdir(mode=0o700)
    provider_root = provider_parent / "everos-root"
    provider_root.mkdir(mode=0o700)
    child = _RebuildChild(None)
    identities = {child.pid: _ORPHAN_CREATE_TIME}

    class _BlockingStartHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.spawn_started = asyncio.Event()
            self.release_spawn = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()

        async def spawn(self, *args, **kwargs):
            self.live_processes[child.pid] = _ORPHAN_CREATE_TIME
            self.spawn_started.set()
            if cancel_stage == "spawn_handoff":
                await self.release_spawn.wait()
            return await super().spawn(*args, **kwargs)

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            return await super().wait_for_exit(*args, **kwargs)

    host = _BlockingStartHost(
        spawns=deque([child]),
        trees={(child.pid, None): identities},
    )
    retention_started = asyncio.Event()

    async def on_reaped() -> None:
        assert host.live_processes == {}
        assert not process._ownership.record_path.exists()
        retention_started.set()

    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "sidecar-home",
        provider_root=provider_root,
        socket_path=short_socket_path,
        settings=_settings(),
        stop_timeout_seconds=0.1,
        on_reaped=on_reaped,
        _host=host,
    )
    ready_started = asyncio.Event()
    release_ready = asyncio.Event()

    async def blocking_ready(_child) -> None:
        ready_started.set()
        await release_ready.wait()

    monkeypatch.setattr(process, "_wait_for_ready", blocking_ready)
    monkeypatch.setattr(process, "_secure_socket", lambda: None)
    monkeypatch.setattr(process, "_assert_no_tcp_listener", lambda *_args, **_kwargs: None)
    start_task = asyncio.create_task(process.start())
    try:
        if cancel_stage == "spawn_handoff":
            await asyncio.wait_for(host.spawn_started.wait(), timeout=0.5)
        else:
            await asyncio.wait_for(ready_started.wait(), timeout=0.5)
            assert process._ownership.record_path.exists()
        start_task.cancel()
        await asyncio.sleep(0)
        assert not start_task.done()
        host.release_spawn.set()
        release_ready.set()
        await asyncio.wait_for(host.cleanup_started.wait(), timeout=0.5)

        contender_host = _FakeProcessHost()
        contender = EverOSRebuildProcess(
            sys.executable,
            effective_home=tmp_path / "rebuild-home",
            provider_root=provider_root,
            settings=_settings(),
            stop_timeout_seconds=0.1,
            _host=contender_host,
        )
        assert await contender.run() is RebuildProcessResult.ROOT_BUSY
        assert contender_host.spawn_calls == []
        assert not start_task.done()

        host.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert host.live_processes == {}
        assert not process._ownership.record_path.exists()
        assert retention_started.is_set()
        assert process.consecutive_failures == 0
        assert process.starting is False

        rebuild_child = _RebuildChild(0, pid=_ORPHAN_DESCENDANT_PID)
        rebuild_identities = {
            rebuild_child.pid: _ORPHAN_CREATE_TIME + 1,
        }
        contender_host.spawns.append(rebuild_child)
        contender_host.trees[(rebuild_child.pid, None)] = rebuild_identities
        contender_host.live_processes.update(rebuild_identities)
        assert await contender.run() is RebuildProcessResult.COMPLETED
    finally:
        host.release_spawn.set()
        release_ready.set()
        host.release_cleanup.set()
        if process._process is not None:
            await process.stop()


async def test_sidecar_start_discovers_recordless_rebuild_before_spawn(
    tmp_path: Path,
    monkeypatch,
    short_socket_path: Path,
) -> None:
    provider_parent = tmp_path / "shared"
    provider_parent.mkdir(mode=0o700)
    provider_root = provider_parent / "everos-root"
    provider_root.mkdir(mode=0o700)
    foreign_home = tmp_path / "rebuild-home"
    sidecar_child = _RebuildChild(None, pid=_ORPHAN_DESCENDANT_PID)
    rebuild = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_GROUP_HELPER_PID: _ORPHAN_CREATE_TIME + 0.5,
    }
    sidecar = {sidecar_child.pid: _ORPHAN_CREATE_TIME + 1}
    events: list[str] = []

    class _OrderedDiscoveryHost(_FakeProcessHost):
        def find_rebuilds(self, **kwargs) -> dict[int, float]:
            events.append("discover-rebuilds")
            return super().find_rebuilds(**kwargs)

        async def spawn(self, *args, **kwargs):
            events.append("spawn-sidecar")
            return await super().spawn(*args, **kwargs)

    host = _OrderedDiscoveryHost(
        spawns=deque([sidecar_child]),
        process_groups={
            _ORPHAN_PID: _ORPHAN_PID,
            sidecar_child.pid: sidecar_child.pid,
        },
        trees={(sidecar_child.pid, sidecar_child.pid): sidecar},
        groups={_ORPHAN_PID: (rebuild, [])},
        rebuilds={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        live_processes={**rebuild, **sidecar},
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "sidecar-home",
        provider_root=provider_root,
        socket_path=short_socket_path,
        settings=_settings(),
        _host=host,
    )
    host.identities[_ORPHAN_PID] = _rebuild_identity(process)
    foreign_record = memory_process.sidecar_record_path(foreign_home / "memory")
    foreign_record.parent.mkdir(parents=True, exist_ok=True)
    foreign_record.write_text(
        json.dumps(
            {
                "pid": _ORPHAN_PID,
                "create_time": _ORPHAN_CREATE_TIME,
                "process_group": _ORPHAN_PID,
                "socket_path": str(foreign_home / "memory/.rt/everos.sock"),
                "provider_root": str(provider_root),
                "role": "cascade_rebuild",
                "python": sys.executable,
            }
        ),
        encoding="utf-8",
    )

    async def ready(_child) -> None:
        return None

    monkeypatch.setattr(process, "_wait_for_ready", ready)
    monkeypatch.setattr(process, "_secure_socket", lambda: None)
    monkeypatch.setattr(process, "_assert_no_tcp_listener", lambda *_args, **_kwargs: None)
    try:
        assert await process.start() is True
        assert events.index("discover-rebuilds") < events.index("spawn-sidecar")
        assert not set(rebuild).intersection(host.live_processes)
        assert sidecar_child.pid in host.live_processes
        assert foreign_record.exists()
        record = json.loads(process._ownership.record_path.read_text(encoding="utf-8"))
        assert record["pid"] == sidecar_child.pid
        assert record["role"] == "sidecar"
    finally:
        await process.stop()


async def test_sidecar_start_fails_closed_on_ambiguous_recordless_rebuilds(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    host = _FakeProcessHost(
        rebuilds={
            _ORPHAN_PID: _ORPHAN_CREATE_TIME,
            _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
        }
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path,
        socket_path=short_socket_path,
        settings=_settings(),
        _host=host,
    )
    try:
        assert await process.start() is False
        assert host.signal_calls == []
        assert host.spawn_calls == []
    finally:
        await process.stop()


async def test_rebuild_refuses_a_symlinked_provider_root_lock(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    provider_root = tmp_path / "memory" / "everos-root"
    provider_root.mkdir(parents=True)
    memory_dir.chmod(0o700)
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("must stay intact", encoding="utf-8")
    lock_path = memory_process._provider_rebuild_lock_path(
        provider_root=provider_root,
    )
    lock_path.parent.mkdir()
    lock_path.symlink_to(sentinel)
    host = _FakeProcessHost()

    assert await _rebuild_process(tmp_path, host).run() is RebuildProcessResult.FAILED
    assert host.spawn_calls == []
    assert sentinel.read_text(encoding="utf-8") == "must stay intact"


async def test_rebuild_refuses_nonprivate_provider_parent_without_changing_its_mode(
    tmp_path: Path,
) -> None:
    provider_parent = tmp_path / "shared"
    provider_parent.mkdir(mode=0o755)
    provider_root = provider_parent / "everos-root"
    provider_root.mkdir(mode=0o700)
    host = _FakeProcessHost()
    process = EverOSRebuildProcess(
        sys.executable,
        effective_home=tmp_path,
        provider_root=provider_root,
        settings=_settings(),
        _host=host,
    )

    assert await process.run() is RebuildProcessResult.FAILED
    assert host.spawn_calls == []
    assert stat.S_IMODE(provider_parent.stat().st_mode) == 0o755


async def test_rebuild_holds_provider_root_lock_through_child_retirement(tmp_path: Path) -> None:
    child = _RebuildChild(0)
    identities = {child.pid: _ORPHAN_CREATE_TIME}

    class _BlockingRetirementHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.retirement_started = asyncio.Event()
            self.release_retirement = asyncio.Event()

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.retirement_started.set()
            await self.release_retirement.wait()
            return await super().wait_for_exit(*args, **kwargs)

    owner_host = _BlockingRetirementHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    owner = asyncio.create_task(_rebuild_process(tmp_path, owner_host).run())
    await owner_host.retirement_started.wait()

    contender_host = _FakeProcessHost()
    assert await _rebuild_process(tmp_path, contender_host).run() is RebuildProcessResult.ROOT_BUSY
    assert contender_host.spawn_calls == []

    owner_host.release_retirement.set()
    assert await owner is RebuildProcessResult.COMPLETED
    assert not (tmp_path / "memory" / ".rt" / "everos.sidecar.json").exists()


def _rebuild_record(process: EverOSRebuildProcess, **overrides) -> dict:
    record = {
        "pid": _ORPHAN_PID,
        "create_time": _ORPHAN_CREATE_TIME,
        "process_group": _ORPHAN_PID,
        "socket_path": str(process._socket_path),
        "provider_root": str(process._provider_root),
        "role": "cascade_rebuild",
        "python": sys.executable,
    }
    record.update(overrides)
    return record


def _rebuild_identity(process: EverOSRebuildProcess, **overrides) -> _ProcessIdentity:
    creation_stamp = overrides.pop("stamp", overrides.pop("create_time", _ORPHAN_CREATE_TIME))
    fields = {
        "stamp": creation_stamp,
        "cmdline": (
            sys.executable,
            "-m",
            "core.memory.rebuild_child",
            "cascade",
            "rebuild",
            "--yes",
        ),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "environment": {
            "EVEROS_ROOT": str(process._provider_root),
            "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild",
        },
        "wall_create_time": overrides.pop("wall_create_time", creation_stamp),
    }
    fields.update(overrides)
    return _ProcessIdentity(**fields)


def test_recorded_rebuild_requires_exact_role_argv_uid_and_root(tmp_path: Path) -> None:
    process = _rebuild_process(tmp_path, _FakeProcessHost())
    record = _rebuild_record(process)

    def verdict(identity: _ProcessIdentity) -> _RecordedSidecar:
        return _classify_recorded_child(
            record,
            identity,
            socket_path=process._socket_path,
            provider_root=process._provider_root,
            role=_MemoryChildRole.CASCADE_REBUILD,
        )

    assert verdict(_rebuild_identity(process)) is _RecordedSidecar.OURS
    assert verdict(
        _rebuild_identity(process, cmdline=(sys.executable, "-m", "http.server"))
    ) is _RecordedSidecar.NOT_OURS
    assert verdict(
        _rebuild_identity(
            process,
            cmdline=(
                "/other/python",
                "-m",
                "core.memory.rebuild_child",
                "cascade",
                "rebuild",
                "--yes",
            ),
        )
    ) is _RecordedSidecar.NOT_OURS
    assert verdict(
        _rebuild_identity(
            process,
            environment={
                "EVEROS_ROOT": str(process._provider_root),
                "AVIBE_MEMORY_CHILD_ROLE": "sidecar",
            },
        )
    ) is _RecordedSidecar.NOT_OURS
    assert verdict(
        _rebuild_identity(
            process,
            environment={
                "EVEROS_ROOT": str(tmp_path / "other-root"),
                "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild",
            },
        )
    ) is _RecordedSidecar.NOT_OURS
    if hasattr(os, "getuid"):
        assert verdict(
            _rebuild_identity(process, uid=os.getuid() + 1)
        ) is _RecordedSidecar.NOT_OURS
    assert verdict(_rebuild_identity(process, environment=None)) is _RecordedSidecar.UNVERIFIABLE


class _RebuildDiscoveryCandidate:
    def __init__(
        self,
        *,
        cmdline: tuple[str, ...],
        uid: int,
        environment: Mapping[str, str] | None,
    ) -> None:
        self.pid = _ORPHAN_PID
        self._cmdline = cmdline
        self._uid = uid
        self._environment = environment

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def create_time(self) -> float:
        return _ORPHAN_CREATE_TIME

    def uids(self):
        return SimpleNamespace(real=self._uid, effective=self._uid, saved=self._uid)

    def environ(self) -> dict[str, str]:
        if self._environment is None:
            raise psutil.AccessDenied(pid=self.pid)
        return dict(self._environment)


def test_rebuild_discovery_accepts_only_the_exact_role_owned_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    own_uid = os.getuid() if hasattr(os, "getuid") else 0
    command = (
        sys.executable,
        "-m",
        "core.memory.rebuild_child",
        "cascade",
        "rebuild",
        "--yes",
    )
    environment = {
        "EVEROS_ROOT": str(tmp_path),
        "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild",
    }
    exact = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment=environment,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [exact])
    assert _processes_rebuilding_owned_root(
        provider_root=tmp_path,
        python=Path(sys.executable),
    ) == {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME
    }

    rejected = [
        _RebuildDiscoveryCandidate(
            cmdline=("/other/python", *command[1:]),
            uid=own_uid,
            environment=environment,
        ),
        _RebuildDiscoveryCandidate(
            cmdline=(sys.executable, "-m", "http.server"),
            uid=own_uid,
            environment=environment,
        ),
        _RebuildDiscoveryCandidate(
            cmdline=command,
            uid=own_uid + 1,
            environment=environment,
        ),
        _RebuildDiscoveryCandidate(
            cmdline=command,
            uid=own_uid,
            environment={**environment, "EVEROS_ROOT": str(tmp_path / "other")},
        ),
        _RebuildDiscoveryCandidate(
            cmdline=command,
            uid=own_uid,
            environment={**environment, "AVIBE_MEMORY_CHILD_ROLE": "sidecar"},
        ),
    ]
    for candidate in rejected:
        monkeypatch.setattr(memory_process.psutil, "process_iter", lambda candidate=candidate: [candidate])
        assert _processes_rebuilding_owned_root(
            provider_root=tmp_path,
            python=Path(sys.executable),
        ) == {}

    unverifiable = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment=None,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [unverifiable])
    with pytest.raises(RuntimeError, match="identity could not be verified"):
        _processes_rebuilding_owned_root(
            provider_root=tmp_path,
            python=Path(sys.executable),
        )


def test_sync_discovery_ignores_foreign_uid_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    own_uid = os.getuid() if hasattr(os, "getuid") else 0
    command = (
        sys.executable,
        "-I",
        *SYNC_ARGV[1:],
    )
    environment = {
        "EVEROS_ROOT": str(tmp_path),
        "AVIBE_MEMORY_CHILD_ROLE": SYNC_ROLE,
        SYNC_NONCE_ENV: "a" * 64,
    }
    foreign = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid + 1,
        environment=None,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [foreign])

    assert _processes_syncing_owned_root(
        provider_root=tmp_path,
        python=Path(sys.executable),
        nonce="a" * 64,
    ) == {}

    exact = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment=environment,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [exact])
    assert _processes_syncing_owned_root(
        provider_root=tmp_path,
        python=Path(sys.executable),
        nonce="a" * 64,
    ) == {_ORPHAN_PID: _ORPHAN_CREATE_TIME}

    unverifiable = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment=None,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [unverifiable])
    with pytest.raises(RuntimeError, match="identity could not be verified"):
        _processes_syncing_owned_root(
            provider_root=tmp_path,
            python=Path(sys.executable),
            nonce="a" * 64,
        )

    class InaccessibleCmdlineCandidate(_RebuildDiscoveryCandidate):
        def cmdline(self) -> list[str]:
            raise psutil.AccessDenied(pid=self.pid)

    inaccessible = InaccessibleCmdlineCandidate(
        cmdline=command,
        uid=own_uid,
        environment=environment,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [inaccessible])
    with pytest.raises(RuntimeError, match="command line could not be verified"):
        _processes_syncing_owned_root(
            provider_root=tmp_path,
            python=Path(sys.executable),
            nonce="a" * 64,
        )


def test_sync_discovery_ignores_non_sync_candidate_with_inaccessible_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    own_uid = os.getuid() if hasattr(os, "getuid") else 0
    unrelated = _RebuildDiscoveryCandidate(
        cmdline=(sys.executable, "-m", "http.server"),
        uid=own_uid,
        environment=None,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [unrelated])

    assert _processes_syncing_owned_root(
        provider_root=tmp_path,
        python=Path(sys.executable),
        nonce="a" * 64,
    ) == {}


def test_sidecar_root_discovery_requires_exact_uid_argv_root_and_role(
    monkeypatch,
    tmp_path: Path,
) -> None:
    own_uid = os.getuid() if hasattr(os, "getuid") else 0
    command = (
        sys.executable,
        "-m",
        "core.memory.sidecar",
        "--uds",
        str(tmp_path / "other-home/memory/.rt/everos.sock"),
    )
    environment = {
        "EVEROS_ROOT": str(tmp_path),
        "AVIBE_MEMORY_CHILD_ROLE": "sidecar",
    }
    exact = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment=environment,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [exact])
    assert memory_process._processes_serving_owned_root(provider_root=tmp_path) == {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME
    }

    legacy = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment={"EVEROS_ROOT": str(tmp_path)},
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [legacy])
    assert memory_process._processes_serving_owned_root(provider_root=tmp_path) == {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME
    }

    canonical_root = tmp_path / "canonical-root"
    canonical_root.mkdir()
    legacy_root = tmp_path / "legacy-root"
    legacy_root.symlink_to(canonical_root, target_is_directory=True)
    symlink_spelling = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment={"EVEROS_ROOT": str(legacy_root)},
    )
    monkeypatch.setattr(
        memory_process.psutil,
        "process_iter",
        lambda: [symlink_spelling],
    )
    assert memory_process._processes_serving_owned_root(
        provider_root=canonical_root,
    ) == {_ORPHAN_PID: _ORPHAN_CREATE_TIME}

    rejected = [
        _RebuildDiscoveryCandidate(
            cmdline=(*command, "--extra"),
            uid=own_uid,
            environment=environment,
        ),
        _RebuildDiscoveryCandidate(
            cmdline=(sys.executable, "-m", "http.server"),
            uid=own_uid,
            environment=environment,
        ),
        _RebuildDiscoveryCandidate(
            cmdline=command,
            uid=own_uid + 1,
            environment=environment,
        ),
        _RebuildDiscoveryCandidate(
            cmdline=command,
            uid=own_uid,
            environment={**environment, "EVEROS_ROOT": str(tmp_path / "other")},
        ),
        _RebuildDiscoveryCandidate(
            cmdline=command,
            uid=own_uid,
            environment={**environment, "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild"},
        ),
    ]
    for candidate in rejected:
        monkeypatch.setattr(
            memory_process.psutil,
            "process_iter",
            lambda candidate=candidate: [candidate],
        )
        assert memory_process._processes_serving_owned_root(provider_root=tmp_path) == {}

    unverifiable = _RebuildDiscoveryCandidate(
        cmdline=command,
        uid=own_uid,
        environment=None,
    )
    monkeypatch.setattr(memory_process.psutil, "process_iter", lambda: [unverifiable])
    with pytest.raises(RuntimeError, match="sidecar identity could not be verified"):
        memory_process._processes_serving_owned_root(provider_root=tmp_path)


@pytest.mark.parametrize("record_state", ["absent", "corrupt"])
async def test_rebuild_boot_discovers_and_reaps_an_unrecorded_exact_child(
    tmp_path: Path,
    record_state: str,
) -> None:
    identities = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
    }
    host = _FakeProcessHost(
        rebuilds={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    host.identities[_ORPHAN_PID] = _rebuild_identity(process)
    reconstructed_records: list[dict] = []

    def observe_reconstructed_record(owned: Mapping[int, float], _signum: int) -> None:
        reconstructed_records.append(
            json.loads(process._ownership.record_path.read_text(encoding="utf-8"))
        )
        for pid in owned:
            host.live_processes.pop(pid, None)

    host.signal_effect = observe_reconstructed_record
    if record_state == "corrupt":
        process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)
        process._ownership.record_path.write_text("{broken", encoding="utf-8")

    await process.reconcile_orphan()

    assert not host.live_processes
    assert not process._ownership.record_path.exists()
    assert host.signal_calls == [(identities, signal.SIGTERM, _ORPHAN_PID, None)]
    assert reconstructed_records[0]["role"] == "cascade_rebuild"
    assert reconstructed_records[0]["python"] == sys.executable


async def test_rebuild_boot_without_artifact_python_discovers_and_reaps_child(
    tmp_path: Path,
) -> None:
    identities = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
    }
    provider_root = tmp_path / "memory" / "everos-root"
    host = _FakeProcessHost(
        rebuilds={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        identities={
            _ORPHAN_PID: _ProcessIdentity(
                stamp=_ORPHAN_CREATE_TIME,
                cmdline=(
                    sys.executable,
                    "-m",
                    "core.memory.rebuild_child",
                    "cascade",
                    "rebuild",
                    "--yes",
                ),
                uid=os.getuid() if hasattr(os, "getuid") else None,
                environment={
                    "EVEROS_ROOT": str(provider_root),
                    "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild",
                },
            )
        },
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    process = EverOSRebuildProcess(
        None,
        effective_home=tmp_path,
        settings=_settings(),
        _host=host,
    )
    reconstructed_records: list[dict] = []

    def observe_reconstructed_record(owned: Mapping[int, float], _signum: int) -> None:
        reconstructed_records.append(
            json.loads(process._ownership.record_path.read_text(encoding="utf-8"))
        )
        for pid in owned:
            host.live_processes.pop(pid, None)

    host.signal_effect = observe_reconstructed_record

    await process.reconcile_orphan()

    assert not host.live_processes
    assert not process._ownership.record_path.exists()
    assert reconstructed_records[0]["role"] == "cascade_rebuild"
    assert reconstructed_records[0]["python"] == sys.executable


async def test_rebuild_boot_discovers_an_orphan_from_the_previous_artifact(
    tmp_path: Path,
) -> None:
    old_python = tmp_path / "old-runtime" / "bin" / "python"
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}

    class _InterpreterFilteringHost(_FakeProcessHost):
        def find_rebuilds(
            self,
            *,
            provider_root: Path,
            python: Path | None,
        ) -> dict[int, float]:
            del provider_root
            return dict(self.rebuilds) if python is None else {}

    host = _InterpreterFilteringHost(
        rebuilds=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        identities={
            _ORPHAN_PID: _ProcessIdentity(
                stamp=_ORPHAN_CREATE_TIME,
                cmdline=(
                    str(old_python),
                    "-m",
                    "core.memory.rebuild_child",
                    "cascade",
                    "rebuild",
                    "--yes",
                ),
                uid=os.getuid() if hasattr(os, "getuid") else None,
                environment={
                    "EVEROS_ROOT": str(tmp_path / "memory" / "everos-root"),
                    "AVIBE_MEMORY_CHILD_ROLE": "cascade_rebuild",
                },
            )
        },
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    reconstructed_records: list[dict] = []

    def observe_reconstructed_record(owned: Mapping[int, float], _signum: int) -> None:
        reconstructed_records.append(
            json.loads(process._ownership.record_path.read_text(encoding="utf-8"))
        )
        for pid in owned:
            host.live_processes.pop(pid, None)

    host.signal_effect = observe_reconstructed_record

    await process.reconcile_orphan()

    assert not host.live_processes
    assert reconstructed_records[0]["python"] == str(old_python)
    assert not process._ownership.record_path.exists()


async def test_rebuild_boot_retains_reconstructed_record_for_unverifiable_group(
    tmp_path: Path,
) -> None:
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        rebuilds=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [_FOREIGN_GROUP_PID])},
        live_processes={
            **identities,
            _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 1,
        },
    )
    process = _rebuild_process(tmp_path, host)
    host.identities[_ORPHAN_PID] = _rebuild_identity(process)

    with pytest.raises(RuntimeError, match="rebuild group could not be verified"):
        await process.reconcile_orphan()

    assert host.live_processes == {
        _FOREIGN_GROUP_PID: _ORPHAN_CREATE_TIME + 1,
    }
    assert process._ownership.record_path.exists()


async def test_rebuild_boot_reaps_a_role_recorded_orphan(tmp_path: Path) -> None:
    host = _FakeProcessHost(
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        identities={_ORPHAN_PID: None},
    )
    process = _rebuild_process(tmp_path, host)
    host.identities[_ORPHAN_PID] = _rebuild_identity(process)
    host.live_processes[_ORPHAN_PID] = _ORPHAN_CREATE_TIME
    host.trees[(_ORPHAN_PID, _ORPHAN_PID)] = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)
    process._ownership.record_path.write_text(
        json.dumps(_rebuild_record(process)),
        encoding="utf-8",
    )

    await process.reconcile_orphan()

    assert not host.live_processes
    assert not process._ownership.record_path.exists()


async def test_rebuild_boot_discovers_exact_survivor_after_reaping_valid_record(
    tmp_path: Path,
) -> None:
    recorded = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    survivor = {_ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1}
    host = _FakeProcessHost(
        rebuilds=dict(survivor),
        process_groups={
            _ORPHAN_PID: _ORPHAN_PID,
            _ORPHAN_DESCENDANT_PID: _ORPHAN_DESCENDANT_PID,
        },
        identities={},
        trees={(_ORPHAN_PID, _ORPHAN_PID): recorded},
        groups={
            _ORPHAN_PID: ({}, []),
            _ORPHAN_DESCENDANT_PID: (survivor, []),
        },
        live_processes={**recorded, **survivor},
    )
    process = _rebuild_process(tmp_path, host)
    host.identities.update(
        {
            _ORPHAN_PID: _rebuild_identity(process),
            _ORPHAN_DESCENDANT_PID: _rebuild_identity(
                process,
                stamp=_ORPHAN_CREATE_TIME + 1,
            ),
        }
    )
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)
    process._ownership.record_path.write_text(
        json.dumps(_rebuild_record(process)),
        encoding="utf-8",
    )

    await process.reconcile_orphan()

    assert host.live_processes == {}
    assert {next(iter(owned)) for owned, *_rest in host.signal_calls} == {
        _ORPHAN_PID,
        _ORPHAN_DESCENDANT_PID,
    }
    assert not process._ownership.record_path.exists()


async def test_rebuild_boot_cancellation_waits_for_orphan_reaping(tmp_path: Path) -> None:
    class _BlockingReapHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.reap_started = asyncio.Event()
            self.release_reap = asyncio.Event()

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.reap_started.set()
            await self.release_reap.wait()
            return await super().wait_for_exit(*args, **kwargs)

    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    host = _BlockingReapHost(
        rebuilds=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    host.identities[_ORPHAN_PID] = _rebuild_identity(process)
    task = asyncio.create_task(process.reconcile_orphan())
    await host.reap_started.wait()

    task.cancel()
    host.release_reap.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not host.live_processes
    assert not process._ownership.record_path.exists()


async def test_standalone_rebuild_reconciles_are_provider_root_exclusive(
    tmp_path: Path,
) -> None:
    class _BlockingReapHost(_FakeProcessHost):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.reap_started = asyncio.Event()
            self.release_reap = asyncio.Event()

        async def wait_for_exit(self, *args, **kwargs) -> bool:
            self.reap_started.set()
            await self.release_reap.wait()
            return await super().wait_for_exit(*args, **kwargs)

    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    owner_host = _BlockingReapHost(
        rebuilds=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    owner = _rebuild_process(tmp_path, owner_host)
    owner_host.identities[_ORPHAN_PID] = _rebuild_identity(owner)
    owner_task = asyncio.create_task(owner.reconcile_orphan())
    await owner_host.reap_started.wait()

    contender = _rebuild_process(tmp_path, _FakeProcessHost())
    contender_scanned = False

    async def record_reconcile(*, discover_missing: bool = False) -> None:
        nonlocal contender_scanned
        del discover_missing
        contender_scanned = True

    contender._ownership.reap = record_reconcile
    with pytest.raises(memory_process._ProviderRootBusy):
        await contender.reconcile_orphan()
    assert contender_scanned is False

    owner_host.release_reap.set()
    await owner_task
    await contender.reconcile_orphan()
    assert contender_scanned is True


async def test_rebuild_boot_reaps_multiple_exact_sidecars_before_rebuild(
    tmp_path: Path,
) -> None:
    anchors = {
        _ORPHAN_PID: _ORPHAN_CREATE_TIME,
        _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
    }
    host = _FakeProcessHost(
        sidecars=dict(anchors),
        process_groups={pid: pid for pid in anchors},
        groups={pid: ({pid: created_at}, []) for pid, created_at in anchors.items()},
        live_processes=dict(anchors),
    )
    process = _rebuild_process(tmp_path, host)

    await process.reconcile_orphan()

    assert host.live_processes == {}
    assert {next(iter(owned)) for owned, *_rest in host.signal_calls} == set(anchors)
    assert not process._ownership.record_path.exists()


async def test_rebuild_boot_reaps_sidecar_from_another_home_on_shared_root(
    tmp_path: Path,
) -> None:
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        root_sidecars=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    sidecar = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "sidecar-home",
        provider_root=process._provider_root,
        settings=_settings(),
    )
    consumed: list[bool] = []

    def consume_while_issuer_is_active(
        signalled: Mapping[int, float],
        _signum: int,
    ) -> None:
        consumed.append(
            sidecar._ownership.consume_planned_reap(
                _ORPHAN_PID,
                _ORPHAN_CREATE_TIME,
            )
        )
        for pid in signalled:
            host.live_processes.pop(pid, None)

    host.signal_effect = consume_while_issuer_is_active

    await process.reconcile_orphan()

    assert host.live_processes == {}
    assert host.signal_calls == [(identities, signal.SIGTERM, _ORPHAN_PID, None)]
    assert not process._ownership.record_path.exists()
    assert consumed == [True]
    assert sidecar._ownership.consume_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    ) is False


async def test_rebuild_retains_committed_handoff_until_delayed_watcher_ack(
    tmp_path: Path,
) -> None:
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        root_sidecars=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    rebuild = _rebuild_process(tmp_path, host)
    await rebuild.reconcile_orphan()

    delayed_watcher = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "delayed-sidecar-home",
        provider_root=rebuild._provider_root,
        settings=_settings(),
    )
    assert delayed_watcher._ownership.consume_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    ) is True
    assert delayed_watcher._ownership.consume_planned_reap(
        _ORPHAN_PID,
        _ORPHAN_CREATE_TIME,
    ) is False


async def test_cancelled_rebuild_reap_before_signal_revokes_planned_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identities = {_ORPHAN_PID: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        root_sidecars=dict(identities),
        process_groups={_ORPHAN_PID: _ORPHAN_PID},
        groups={_ORPHAN_PID: (identities, [])},
        live_processes=dict(identities),
    )
    rebuild = _rebuild_process(tmp_path, host)

    async def cancel_before_signal(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        rebuild._ownership,
        "_terminate_claimed_processes",
        cancel_before_signal,
    )

    with pytest.raises(asyncio.CancelledError):
        await rebuild.reconcile_orphan()

    sidecar = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "sidecar-home",
        provider_root=rebuild._provider_root,
        settings=_settings(),
    )
    sidecar._consecutive_failures = 4
    child = _ExitedChild()
    child.returncode = -signal.SIGTERM
    _supervising(sidecar, child)
    sidecar._desired_running = True

    async def terminate(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sidecar, "_terminate_owned_tree", terminate)
    await sidecar._watch_child(child)

    assert host.signal_calls == []
    assert sidecar.consecutive_failures == 5
    assert sidecar.down is True
    assert sidecar._restart_task is None


async def test_sidecar_start_refuses_live_cross_home_peer_on_shared_root(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    provider_root = tmp_path / "shared" / "everos-root"
    provider_root.parent.mkdir(mode=0o700)
    peer_socket = tmp_path / "peer" / "everos.sock"
    host = _FakeProcessHost(
        root_sidecars={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        identities={
            _ORPHAN_PID: _ProcessIdentity(
                stamp=_ORPHAN_CREATE_TIME,
                cmdline=(
                    sys.executable,
                    "-m",
                    _SIDECAR_ENTRYPOINT_MODULE,
                    "--uds",
                    str(peer_socket),
                ),
                uid=os.getuid() if hasattr(os, "getuid") else None,
            )
        },
        live_processes={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
    )
    process = EverOSProcess(
        sys.executable,
        effective_home=tmp_path / "local-home",
        provider_root=provider_root,
        socket_path=short_socket_path,
        settings=_settings(),
        _host=host,
    )

    assert await process.start() is False
    assert process.consecutive_failures == 0
    assert process.last_error == "memory_provider_root_busy"
    assert host.signal_calls == []
    assert host.spawn_calls == []
    retry = process._restart_task
    process._desired_running = False
    if retry is not None:
        retry.cancel()
        try:
            await retry
        except asyncio.CancelledError:
            pass


async def test_rebuild_boot_fails_closed_on_ambiguous_discovery(tmp_path: Path) -> None:
    host = _FakeProcessHost(
        rebuilds={
            _ORPHAN_PID: _ORPHAN_CREATE_TIME,
            _ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1,
        }
    )

    with pytest.raises(RuntimeError, match="ambiguous EverOS child ownership"):
        await _rebuild_process(tmp_path, host).reconcile_orphan()

    assert host.signal_calls == []


async def test_rebuild_boot_fails_closed_on_mixed_role_discovery(tmp_path: Path) -> None:
    host = _FakeProcessHost(
        sidecars={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        rebuilds={_ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1},
    )

    with pytest.raises(RuntimeError, match="ambiguous EverOS child ownership"):
        await _rebuild_process(tmp_path, host).reconcile_orphan()

    assert host.signal_calls == []


async def test_rebuild_boot_fails_closed_on_cross_home_sidecar_and_rebuild(
    tmp_path: Path,
) -> None:
    host = _FakeProcessHost(
        root_sidecars={_ORPHAN_PID: _ORPHAN_CREATE_TIME},
        rebuilds={_ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1},
    )

    with pytest.raises(RuntimeError, match="ambiguous EverOS child ownership"):
        await _rebuild_process(tmp_path, host).reconcile_orphan()

    assert host.signal_calls == []


async def test_rebuild_boot_keeps_an_unknown_role_record_fail_closed(tmp_path: Path) -> None:
    host = _FakeProcessHost()
    process = _rebuild_process(tmp_path, host)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)
    process._ownership.record_path.write_text(
        json.dumps(_rebuild_record(process, role="future_role")),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="role could not be verified"):
        await process.reconcile_orphan()

    assert process._ownership.record_path.exists()
    assert host.signal_calls == []


async def test_rebuild_boot_keeps_record_when_a_group_survives(tmp_path: Path) -> None:
    survivor = {_ORPHAN_DESCENDANT_PID: _ORPHAN_CREATE_TIME + 1}
    host = _FakeProcessHost(
        identities={_ORPHAN_PID: None},
        groups={_ORPHAN_PID: (survivor, [])},
        live_processes=dict(survivor),
        wait_results=deque([False, False]),
        remove_on_signal=False,
    )
    process = _rebuild_process(tmp_path, host)
    process._ownership.record_path.parent.mkdir(parents=True, exist_ok=True)
    process._ownership.record_path.write_text(
        json.dumps(_rebuild_record(process)),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="orphaned sidecar group did not exit"):
        await process.reconcile_orphan()

    assert process._ownership.record_path.exists()


async def test_rebuild_record_failure_reaps_the_just_spawned_child(
    monkeypatch,
    tmp_path: Path,
) -> None:
    child = _RebuildChild(None)
    releases: list[int] = []
    child.send_signal = releases.append
    identities = {child.pid: _ORPHAN_CREATE_TIME}
    host = _FakeProcessHost(
        spawns=deque([child]),
        process_groups={child.pid: child.pid},
        trees={(child.pid, child.pid): identities},
        live_processes=dict(identities),
    )
    process = _rebuild_process(tmp_path, host)
    original_write = memory_process._write_private_text

    def fail_record(path: Path, contents: str) -> None:
        if path == process._ownership.record_path:
            raise OSError("simulated crash after spawn")
        original_write(path, contents)

    monkeypatch.setattr(memory_process, "_write_private_text", fail_record)

    assert await process.run() is RebuildProcessResult.FAILED
    assert releases == []
    assert not host.live_processes
    assert host.signal_calls

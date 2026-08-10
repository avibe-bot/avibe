from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
import stat
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

from core.memory.attachments import AttachmentPinStore, attachment_pin_root
from core.memory.everos import (
    EverOSPort,
    ProviderCapture,
)
import core.memory.process as memory_process
from core.memory.process import (
    _SIDECAR_ENTRYPOINT_MODULE,
    _ProcessHost,
    _SystemProcessHost,
    EverOSProcess,
    EverOSProcessSettings,
    FakeEverOSProcess,
    _ProcessKind,
    _ProcessIdentity,
    _RecordedSidecar,
    _classify_recorded_sidecar,
)
from core.memory.sidecar import _request_rejection
from core.memory.types import (
    CaptureAttachment,
    ProviderSessionRef,
)


PROJECT = "p-22222222222222222222222222222222"
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
    live_processes: dict[int, float] = field(default_factory=dict)
    listeners: set[int] = field(default_factory=set)
    wait_results: deque[bool] = field(default_factory=deque)
    remove_on_signal: bool = True
    signal_effect: Callable[[Mapping[int, float], int], None] | None = None
    spawn_calls: list[tuple[_ProcessKind, Path, Path, Path | None]] = field(default_factory=list)
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
        del env
        self.spawn_calls.append((kind, python, cwd, socket_path))
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
    ) -> tuple[dict[int, float], list[int]]:
        del socket_path, provider_root
        self.group_scans.append(process_group)
        owned, foreign = self.groups.get(process_group, ({}, []))
        return dict(owned), list(foreign)

    def find_sidecars(self, *, socket_path: Path) -> dict[int, float]:
        self.sidecar_scans.append(socket_path)
        return dict(self.sidecars)

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
    assert environment["AVIBE_MEMORY_ATTACHMENTS_ROOT"] == str(
        attachment_pin_root(tmp_path)
    )
    assert environment["EVEROS_EMBEDDING__API_KEY"] == "embedding-secret"
    assert "HTTP_PROXY" not in environment
    assert "SSL_CERT_FILE" not in environment
    assert "llm-secret" not in generated
    assert "embedding-secret" not in generated
    assert "rerank" in generated
    assert str(attachment_pin_root(tmp_path)) in generated
    assert "AVIBE_MEMORY_CALL_LOG_DB" not in environment


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

        monkeypatch.setattr(memory_process.psutil, "Process", _ReusedProcess)
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
        create_time=_ORPHAN_CREATE_TIME,
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
        create_time=_ORPHAN_CREATE_TIME,
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
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(_UNUSABLE_RECORDS["truncated"])
    with caplog.at_level(logging.WARNING, logger=memory_process.logger.name):
        asyncio.run(process._ownership.reap())

    # The anchor and its group helper, and neither look-alike.
    assert host.signal_calls == [(group_members, signal.SIGTERM, _ORPHAN_PID, None)]
    assert str(_FOREIGN_GROUP_PID) not in caplog.text
    assert str(_FOREIGN_UID_GROUP_PID) not in caplog.text
    assert not record_path.exists()


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


def test_sidecar_launch_scans_for_processes_only_when_a_record_exists(
    tmp_path: Path,
    short_socket_path: Path,
) -> None:
    """The ordinary first boot must not pay for a machine-wide process scan."""

    host = _FakeProcessHost()
    process = _orphan_process(
        tmp_path,
        host=host,
        stop_timeout_seconds=0.1,
        socket_path=short_socket_path,
    )

    assert not process._ownership.record_path.exists()
    assert asyncio.run(process.start()) is False

    assert host.sidecar_scans == []
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

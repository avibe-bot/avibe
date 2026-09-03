from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from core import watch_worker, watches as watches_module
from core.process_isolation import (
    PersistedProcessIdentity,
    ProcessIdentity,
    fingerprint_process_marker,
)
from core.runtime_work import RuntimeWorkItem, RuntimeWorkLane, RuntimeWorkSupervisor
from core.scheduled_tasks import TaskExecutionStore
from core.watches import (
    CIRCUIT_BREAKER_METADATA_KEY,
    DELIVERY_ACK_METADATA_KEY,
    FOLLOW_UP_RUN_ID_METADATA_KEY,
    LAST_DELIVERY_ENV,
    LIFETIME_STARTED_AT_METADATA_KEY,
    NO_EVENT_EXIT_CODE,
    NO_EVENT_MARKER,
    RECENT_EVENT_TIMESTAMPS_METADATA_KEY,
    WATCH_CIRCUIT_OUTPUT_LIMIT,
    WATCH_ID_ENV,
    ManagedWatchService,
    ManagedWatchStore,
    WatchRuntimeStateStore,
    _CycleResult,
    _cycle_env,
    _ManagedWatchRuntimeWorkHandler,
    _recent_event_timestamps,
    _StaleWorkerRecovery,
)
from storage.background import (
    WATCH_HOOK_OUTCOME_CIRCUIT_REPAIR,
    WATCH_HOOK_OUTCOME_EVENT,
    WATCH_HOOK_OUTCOME_METADATA_KEY,
    WATCH_HOOK_OUTCOME_WAITER_FAILURE,
    SQLiteBackgroundTaskStore,
)

TEST_MARKER = "test-watch-worker"
TEST_FINGERPRINT = fingerprint_process_marker(TEST_MARKER)


def _quiet_waiter_command(summary: str = "") -> list[str]:
    """A waiter that ends a cycle the way the no-event protocol asks it to.

    The marker is half the signal: the exit code on its own is ``sysexits`` EX_USAGE
    and stays a failure.
    """

    script = "import sys; "
    if summary:
        script += f"print({summary!r}, file=sys.stderr); "
    script += f"print({NO_EVENT_MARKER!r}, file=sys.stderr); sys.exit({NO_EVENT_EXIT_CODE})"
    return [sys.executable, "-c", script]


class _FakeStdin:
    def __init__(self) -> None:
        self.payload = bytearray()
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payload.extend(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _BrokenStdin(_FakeStdin):
    async def drain(self) -> None:
        raise BrokenPipeError


class _FakeProcess:
    def __init__(
        self,
        *,
        stdin: _FakeStdin | None = None,
        stderr: bytes = b"",
    ) -> None:
        self.pid = 1234
        self.returncode = 0
        self.stdin = stdin or _FakeStdin()
        self.stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok\n", self.stderr


async def _start_watch_service(service: ManagedWatchService) -> None:
    service.start()
    startup_task = service._startup_task
    if startup_task is not None:
        await startup_task


async def _await_fused_watch(
    service: ManagedWatchService,
    watch_id: str,
    timeout: float = 10.0,
) -> None:
    """Wait for a watch to be fused instead of sleeping a fixed interval.

    Fusing needs a real waiter subprocess to start and its result to reach the
    raising store, so the wait covers a process spawn plus interpreter startup.
    A fixed 0.12s was enough locally and not on a loaded CI runner, where the
    assertion saw an empty fused set.
    """

    deadline = time.monotonic() + timeout
    while watch_id not in service._fused_watch_ids:
        assert time.monotonic() < deadline, "watch was never fused after the store error"
        await asyncio.sleep(0.02)


def _add_recovery_watch(
    store: ManagedWatchStore,
    *,
    command: list[str],
    shell_command: str | None = None,
):
    return store.add_watch(
        name="Recovery watch",
        session_key="slack::channel::C123",
        command=command,
        shell_command=shell_command,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )


def _record_watch_pid(
    runtime_store: WatchRuntimeStateStore,
    watch_id: str,
    pid: int,
    *,
    identity: PersistedProcessIdentity | None = None,
) -> None:
    entry = {
        "running": True,
        "pid": pid,
        "started_at": "2026-05-15T00:00:00+00:00",
        "updated_at": "2026-05-15T00:00:01+00:00",
    }
    if identity is not None:
        entry["process_identity"] = {
            "pid": identity.pid,
            "create_time": identity.create_time,
            "worker_fingerprint": identity.worker_fingerprint,
        }
    runtime_store.write(
        {
            "watches": {
                watch_id: entry,
            }
        }
    )


def _live_identity(
    *,
    pid: int = 4321,
    create_time: float = 123.0,
    worker_fingerprint: str | None = TEST_FINGERPRINT,
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        create_time=create_time,
        worker_fingerprint=worker_fingerprint,
    )


def _persisted_identity(
    *,
    pid: int = 4321,
    create_time: float = 123.0,
    worker_fingerprint: str = TEST_FINGERPRINT,
) -> PersistedProcessIdentity:
    return PersistedProcessIdentity(
        pid=pid,
        create_time=create_time,
        worker_fingerprint=worker_fingerprint,
    )


def test_managed_watch_store_round_trip(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix="CI finished.",
        cwd="/tmp",
        mode="forever",
        timeout_seconds=600,
        lifetime_timeout_seconds=3600,
        retry_exit_codes=[75],
        retry_delay_seconds=45,
        post_to="channel",
        deliver_key=None,
    )

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    reloaded = ManagedWatchStore(store.path)
    saved = reloaded.get_watch(watch.id)

    assert payload["watches"][0]["id"] == watch.id
    assert saved is not None
    assert saved.name == "Watch CI"
    assert saved.mode == "forever"
    assert saved.retry_exit_codes == [75]
    assert saved.post_to == "channel"


def test_hfr_174_watch_definition_commit_emits_watch_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import inbox_events

    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", True)
    events: list[tuple[str, dict]] = []
    subscription = inbox_events.bus.subscribe_callback(
        lambda event_type, payload: events.append((event_type, payload))
    )
    store = ManagedWatchStore(tmp_path / "watches.json")
    store.add_watch(
        name="Definition wake",
        session_key="avibe::agent::default",
        command=["true"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=30,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    inbox_events.bus.unsubscribe(subscription)

    assert events == [
        (inbox_events.DEFINITIONS_UPDATED_EVENT, {"definition_type": "watch"})
    ]


def test_managed_watch_store_preserves_zero_values_on_reload(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Watch Zero",
        session_key="slack::channel::C123",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )

    reloaded = ManagedWatchStore(store.path)
    saved = reloaded.get_watch(watch.id)

    assert saved is not None
    assert saved.timeout_seconds == 0
    assert saved.lifetime_timeout_seconds == 0
    assert saved.retry_delay_seconds == 0


def test_managed_watch_store_recovery_accepts_empty_command_arguments(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py", ""])

    recovered = store.list_watches_for_recovery()

    assert [item.id for item in recovered] == [watch.id]
    assert recovered[0].command == [sys.executable, "wait.py", ""]


def test_malformed_remote_context_disables_watch_before_waiter_spawn(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Legacy remote watch",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "raise AssertionError('must not run')"],
        shell_command=None,
        prefix=None,
        cwd=str(tmp_path),
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    watch.metadata = {"resource_user_context": {"sub": "legacy-remote-user"}}
    store.upsert_watch(watch)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    service._running = True
    service._requires_service_lease = False

    async def unexpected_run_cycle(*_args, **_kwargs):
        raise AssertionError("remote-origin waiter must not spawn")

    service._run_cycle = unexpected_run_cycle  # type: ignore[method-assign]
    asyncio.run(service._run_watch(watch.id))

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is False
    assert saved.last_started_at is None
    assert saved.last_error == "harness_access_forbidden"


def test_managed_watch_exec_uses_stable_supervisor(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    watch = store.add_watch(
        name="Watch Python",
        session_key="slack::channel::C123",
        command=["python3", "-c", "print('ok')"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        process = _FakeProcess()
        captured["process"] = process
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(service._run_cycle(watch, timeout_seconds=5))

    assert result.exit_code == 0
    assert captured["args"] == (
        os.path.abspath(sys.executable),
        str(Path(watch_worker.__file__).resolve()),
    )
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["cwd"] == str(paths.get_vibe_remote_dir())
    process = captured["process"]
    assert isinstance(process, _FakeProcess)
    assert process.stdin.closed is True
    assert json.loads(process.stdin.payload) == {
        "version": 1,
        "command": ["python3", "-c", "print('ok')"],
        "shell_command": None,
    }


def test_managed_watch_shell_uses_stable_supervisor(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    watch = store.add_watch(
        name="Watch Shell",
        session_key="slack::channel::C123",
        command=[],
        shell_command="python3 -c 'print(\"ok\")'",
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        process = _FakeProcess()
        captured["process"] = process
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(service._run_cycle(watch, timeout_seconds=5))

    assert result.exit_code == 0
    assert captured["args"] == (
        os.path.abspath(sys.executable),
        str(Path(watch_worker.__file__).resolve()),
    )
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["cwd"] == str(paths.get_vibe_remote_dir())
    process = captured["process"]
    assert isinstance(process, _FakeProcess)
    assert process.stdin.closed is True
    assert json.loads(process.stdin.payload) == {
        "version": 1,
        "command": [],
        "shell_command": "python3 -c 'print(\"ok\")'",
    }


def test_managed_watch_clears_supervisor_state_when_startup_pipe_breaks(tmp_path: Path, monkeypatch) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    watch = store.add_watch(
        name="Broken supervisor",
        session_key="slack::channel::C123",
        command=["python3", "-c", "print('ok')"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    process = _FakeProcess(stdin=_BrokenStdin())

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="Watch worker supervisor exited during startup"):
        asyncio.run(service._run_cycle(watch, timeout_seconds=5))

    assert process.stdin.closed is True
    assert service._active_pids == {}
    assert service._active_process_identities == {}


def test_managed_watch_localizes_supervisor_startup_failure(tmp_path: Path, monkeypatch) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(config=SimpleNamespace(language="zh")),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    watch = store.add_watch(
        name="Broken supervisor",
        session_key="slack::channel::C123",
        command=["python3", "-c", "print('ok')"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    process = _FakeProcess(
        stdin=_BrokenStdin(),
        stderr=watch_worker.encode_watch_worker_error("invalidCommand").encode(),
    )

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="Watch 监控进程失败：Watch 工作进程命令无效。"):
        asyncio.run(service._run_cycle(watch, timeout_seconds=5))


def test_managed_watch_legacy_none_cwd_survives_deleted_process_cwd(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    watch = store.add_watch(
        name="Legacy watch",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "import os; print(os.getcwd())"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    deleted_cwd = tmp_path / "deleted-service-cwd"
    deleted_cwd.mkdir()
    original_cwd = Path.cwd()

    os.chdir(deleted_cwd)
    deleted_cwd.rmdir()
    try:
        result = asyncio.run(service._run_cycle(watch, timeout_seconds=5))
    finally:
        os.chdir(original_cwd)

    assert result.exit_code == 0
    assert result.stdout == str(paths.get_vibe_remote_dir())


def test_managed_watch_store_uses_sqlite_when_path_is_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = ManagedWatchStore()
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        session_id="sesk8m4q2p7x",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix="CI finished.",
        cwd=None,
        mode="forever",
        timeout_seconds=600,
        lifetime_timeout_seconds=3600,
        retry_exit_codes=[75],
        retry_delay_seconds=45,
        post_to="channel",
        deliver_key=None,
    )

    reloaded = ManagedWatchStore()
    saved = reloaded.get_watch(watch.id)
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")

    assert not (tmp_path / "state" / "watches.json").exists()
    assert saved is not None
    assert saved.session_id == "sesk8m4q2p7x"
    assert sqlite.get_watch(watch.id)["command"] == ["python3", "wait.py"]


def test_hfr_479_late_cycle_cannot_replace_committed_watch_terminal_outcome(
    tmp_path: Path,
) -> None:
    """HFR-479 -- the retiring cycle owns definition terminal fields forever."""

    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    winner = ManagedWatchStore(tmp_path / "winner.json")
    winner._sqlite = sqlite
    late = ManagedWatchStore(tmp_path / "late.json")
    late._sqlite = sqlite
    watch = winner.add_watch(
        name="ordered retirement",
        session_key="",
        command=[],
        shell_command="true",
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    late.load()
    late.maybe_reload()  # establish the invalidation baseline before retirement
    requests = TaskExecutionStore(tmp_path / "requests")
    requests._sqlite = sqlite

    assert winner.mark_cycle_result(
        watch.id,
        exit_code=7,
        error="terminal cycle failed",
        disable=True,
    )
    late_run = requests.build_hook_send(
        session_key="",
        prompt="late cycle evidence",
        run_type="watch",
        definition_id=watch.id,
        source_kind="watch",
    )
    assert late.mark_cycle_result(
        watch.id,
        exit_code=0,
        error=None,
        event_detected=True,
        disable=False,
        queued_run=late_run.to_dict(),
    )

    stored = sqlite.get_watch(watch.id)
    assert stored is not None
    assert stored["retired_at"] is not None
    assert stored["last_finished_at"] == stored["retired_at"]
    assert stored["last_exit_code"] == 7
    assert stored["last_error"] == "terminal cycle failed"
    assert sqlite.get_run(late_run.id) is not None


def test_cycle_start_preserves_last_completed_outcome_pair(tmp_path: Path) -> None:
    """Starting a cycle must not tear the last completed cycle's outcome pair."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="retrying watch",
        session_key="",
        command=[],
        shell_command="exit 75",
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )

    assert store.mark_cycle_result(
        watch.id,
        exit_code=75,
        error="watch command exited with status 75",
    )
    assert store.mark_cycle_start(watch.id)

    started = store.get_watch(watch.id)
    assert started is not None and started.last_started_at is not None
    assert (started.last_exit_code, started.last_error) == (
        75,
        "watch command exited with status 75",
    )


def test_sqlite_remove_watch_soft_deletes_watch_but_keeps_runtime(tmp_path: Path) -> None:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    store = ManagedWatchStore(tmp_path / "watches.json")
    store._sqlite = sqlite
    watch = store.add_watch(
        name="Watch CI",
        session_key="slack::channel::C123",
        session_id="sesk8m4q2p7x",
        command=["python3", "wait.py"],
        shell_command=None,
        prefix="CI finished.",
        cwd=None,
        mode="forever",
        timeout_seconds=600,
        lifetime_timeout_seconds=3600,
        retry_exit_codes=[75],
        retry_delay_seconds=45,
        post_to="channel",
        deliver_key=None,
    )
    sqlite.write_watch_runtime(
        {
            "watches": {
                watch.id: {
                    "running": True,
                    "pid": 1234,
                    "started_at": "2026-05-15T00:00:00+00:00",
                    "updated_at": "2026-05-15T00:00:00+00:00",
                }
            }
        },
        updated_at="2026-05-15T00:00:00+00:00",
    )

    assert store.remove_watch(watch.id) is True

    reloaded = ManagedWatchStore(tmp_path / "watches-reloaded.json")
    reloaded._sqlite = sqlite
    reloaded.load()

    assert reloaded.get_watch(watch.id) is None
    assert sqlite.get_watch(watch.id) is None
    assert sqlite.get_run(f"runtime:{watch.id}")["task_id"] == watch.id


def test_watch_runtime_store_uses_sqlite_when_path_is_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    store = WatchRuntimeStateStore()

    store.write(
        {
            "watches": {
                "watch-1": {
                    "running": True,
                    "pid": 1234,
                    "started_at": "2026-05-15T00:00:00+00:00",
                    "updated_at": "2026-05-15T00:00:01+00:00",
                }
            }
        }
    )

    assert not (tmp_path / "runtime" / "watch_runtime.json").exists()
    assert store.load()["watches"]["watch-1"]["pid"] == 1234


def test_managed_watch_service_once_success_enqueues_hook_and_disables(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait once",
        session_key="slack::channel::C123",
        command=["python3", "-c", "print('waiter output')"],
        shell_command=None,
        prefix="The waiter finished.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        for _ in range(100):
            if watch.id not in service._active_tasks:
                break
            await asyncio.sleep(0.05)
        await service.stop()

    asyncio.run(_run())

    pending = request_store.list_pending()
    saved = store.get_watch(watch.id)

    assert len(pending) == 1
    # ManagedWatchService enqueues with the dedicated "watch" run_type (core/watches.py),
    # which scheduled_tasks dispatches like a hook_send but tags as trigger_kind="watch".
    assert pending[0].request_type == "watch"
    assert pending[0].prompt == "The waiter finished.\n\nwaiter output"
    assert (
        pending[0].metadata[WATCH_HOOK_OUTCOME_METADATA_KEY]
        == WATCH_HOOK_OUTCOME_EVENT
    )
    assert saved is not None
    assert saved.enabled is False
    assert saved.last_exit_code == 0
    assert saved.last_event_at is not None


def test_managed_watch_service_does_not_enqueue_after_service_lease_loss(tmp_path: Path, monkeypatch) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait once",
        session_key="slack::channel::C123",
        command=["python3", "-c", "print('waiter output')"],
        shell_command=None,
        prefix="The waiter finished.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    service._running = True
    service._requires_service_lease = True
    owner_checks = iter([True, False])
    monkeypatch.setattr(
        "core.watches.runtime.current_process_owns_service_instance",
        lambda: next(owner_checks),
    )

    async def fake_run_cycle(*args, **kwargs):
        return _CycleResult(exit_code=0, stdout="waiter output", stderr="", timed_out=False)

    monkeypatch.setattr(service, "_run_cycle", fake_run_cycle)

    asyncio.run(service._run_watch(watch.id))

    assert request_store.list_pending() == []


def test_managed_watch_service_forever_timeout_disables_and_enqueues_failure(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait forever",
        session_key="slack::channel::C123",
        command=["python3", "-c", "import time; time.sleep(0.2)"],
        shell_command=None,
        prefix="Should stay silent.",
        cwd=None,
        mode="forever",
        timeout_seconds=0.05,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        watch_task = service._active_tasks[watch.id]
        try:
            await asyncio.wait_for(asyncio.shield(watch_task), timeout=10)
        finally:
            await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)

    pending = request_store.list_pending()
    assert saved is not None
    assert len(pending) == 1
    assert "stopped because the waiter timed out" in pending[0].prompt
    assert "Check whether the timeout is too short or the waiter is blocked" in pending[0].prompt
    assert saved.enabled is False
    assert saved.last_exit_code == 124


def test_managed_watch_service_forever_timeout_retries_when_explicitly_allowed(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Retry timeout forever",
        session_key="slack::channel::C123",
        command=["python3", "-c", "import time; time.sleep(0.2)"],
        shell_command=None,
        prefix="Should keep waiting.",
        cwd=None,
        mode="forever",
        timeout_seconds=0.05,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75, 124],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        await asyncio.sleep(0.2)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is True
    assert saved.last_exit_code == 124
    assert request_store.list_pending() == []


def test_managed_watch_service_stop_terminates_running_waiter(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait forever",
        session_key="slack::channel::C123",
        command=["python3", "-c", "import time; time.sleep(30)"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> tuple[int, int | None]:
        await _start_watch_service(service)
        for _ in range(100):
            pid = service._active_pids.get(watch.id)
            if pid:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("waiter pid was never recorded")
        pgid = os.getpgid(pid) if hasattr(os, "getpgid") else None
        await service.stop()
        return pid, pgid

    pid, pgid = asyncio.run(_run())

    if pgid is not None:
        assert pgid != os.getpgrp()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_managed_watch_service_lease_loss_terminates_running_waiter(tmp_path: Path, monkeypatch) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait forever",
        session_key="slack::channel::C123",
        command=["python3", "-c", "import time; time.sleep(30)"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    service._requires_service_lease = True
    owner_state = {"owns": True}
    monkeypatch.setattr("core.watches.runtime.current_process_owns_service_instance", lambda: owner_state["owns"])

    async def _run() -> tuple[int, int | None]:
        await _start_watch_service(service)
        for _ in range(100):
            pid = service._active_pids.get(watch.id)
            if pid:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("waiter pid was never recorded")
        pgid = os.getpgid(pid) if hasattr(os, "getpgid") else None
        active_tasks = list(service._active_tasks.values())
        owner_state["owns"] = False
        assert service._owns_service_instance() is False
        await asyncio.gather(*active_tasks, return_exceptions=True)
        if service._reconcile_task:
            try:
                await service._reconcile_task
            except asyncio.CancelledError:
                pass
            service._reconcile_task = None
        return pid, pgid

    pid, pgid = asyncio.run(_run())

    if pgid is not None:
        assert pgid != os.getpgrp()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_managed_watch_service_records_non_secret_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "runtime-expanded-secret"
    monkeypatch.setenv("WATCH_RUNTIME_SECRET", secret)
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait forever",
        session_key="slack::channel::C123",
        command=[],
        shell_command=(
            "exec python3 -c 'import time; time.sleep(30)' "
            '"$WATCH_RUNTIME_SECRET"'
        ),
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> dict[str, object]:
        await _start_watch_service(service)
        for _ in range(100):
            entry = runtime_store.load().get("watches", {}).get(watch.id, {})
            if entry.get("process_identity"):
                await service.stop()
                return entry
            await asyncio.sleep(0.02)
        await service.stop()
        raise AssertionError("process identity was never written")

    entry = asyncio.run(_run())
    identity = entry["process_identity"]
    assert isinstance(entry["pid"], int)
    assert datetime.fromisoformat(str(entry["started_at"])).year >= 2024
    assert isinstance(identity, dict)
    assert identity["pid"] == entry["pid"]
    assert identity["create_time"] > 0
    assert identity["worker_fingerprint"].startswith("sha256:")
    assert "cmdline" not in identity
    assert secret not in json.dumps(entry)


def test_managed_watch_service_turns_spawn_error_into_failed_cycle(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Broken waiter",
        session_key="slack::channel::C123",
        command=["/definitely/missing/waiter"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        for _ in range(100):
            if watch.id not in service._active_tasks:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    pending = request_store.list_pending()
    assert saved is not None
    assert saved.enabled is False
    assert saved.last_exit_code == 1
    assert saved.last_error
    assert len(pending) == 1
    assert "stopped because the waiter exited with code 1" in pending[0].prompt
    assert "fix the waiter or its dependencies" in pending[0].prompt
    assert (
        pending[0].metadata[WATCH_HOOK_OUTCOME_METADATA_KEY]
        == WATCH_HOOK_OUTCOME_WAITER_FAILURE
    )


@pytest.mark.parametrize("use_shell", [False, True], ids=["exec", "shell"])
def test_managed_watch_service_stops_and_notifies_when_cwd_was_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_shell: bool,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    removed_cwd = tmp_path / "removed-worktree"
    removed_cwd.mkdir()
    removed_cwd.rmdir()
    watch = store.add_watch(
        name="PR review",
        session_key="",
        session_id="ses-linked",
        command=[] if use_shell else [sys.executable, "-c", "raise AssertionError('must not run')"],
        shell_command="exit 99" if use_shell else None,
        prefix=None,
        message="PR review activity detected.",
        cwd=str(removed_cwd),
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    run_cycle_called = False

    async def unexpected_run_cycle(*_args, **_kwargs):
        nonlocal run_cycle_called
        run_cycle_called = True
        raise AssertionError("waiter must not spawn when its cwd is missing")

    monkeypatch.setattr(service, "_run_cycle", unexpected_run_cycle)
    service._running = True
    asyncio.run(service._run_watch(watch.id))

    saved = store.get_watch(watch.id)
    pending = request_store.list_pending()
    assert run_cycle_called is False
    assert saved is not None
    assert saved.enabled is False
    assert saved.last_exit_code == 1
    assert saved.last_error == (f"watch working directory no longer exists or is not a directory: {removed_cwd}")
    assert len(pending) == 1
    assert pending[0].session_id == "ses-linked"
    assert pending[0].task_id == watch.id
    assert pending[0].source_kind == "watch"
    assert pending[0].prompt == (
        "Watch 'PR review' stopped because its working directory is no longer available.\n"
        f"Working directory: {removed_cwd}\n"
        "Update or recreate the watch with a valid cwd before monitoring continues."
    )


def test_missing_watch_cwd_does_not_stop_other_watches(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    removed_cwd = tmp_path / "removed-worktree"
    broken = store.add_watch(
        name="Broken watch",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "raise AssertionError('must not run')"],
        shell_command=None,
        prefix=None,
        cwd=str(removed_cwd),
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    healthy = store.add_watch(
        name="Healthy watch",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "print('healthy completed')"],
        shell_command=None,
        prefix="Healthy watch event.",
        cwd=str(tmp_path),
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=30,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        for _ in range(100):
            broken_saved = store.get_watch(broken.id)
            healthy_saved = store.get_watch(healthy.id)
            if broken_saved and healthy_saved and not broken_saved.enabled and not healthy_saved.enabled:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    broken_saved = store.get_watch(broken.id)
    healthy_saved = store.get_watch(healthy.id)
    prompts = [request.prompt for request in request_store.list_pending()]
    assert broken_saved is not None
    assert broken_saved.last_error and str(removed_cwd) in broken_saved.last_error
    assert healthy_saved is not None
    assert healthy_saved.last_error is None
    assert healthy_saved.last_event_at is not None
    assert any("working directory is no longer available" in (prompt or "") for prompt in prompts)
    assert any("healthy completed" in (prompt or "") for prompt in prompts)


def test_managed_watch_service_forever_retries_only_allowed_exit_code(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Retry waiter",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "import sys; sys.exit(75)"],
        shell_command=None,
        prefix="Retry only.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            saved = store.get_watch(watch.id)
            if saved is not None and saved.last_exit_code == 75:
                break
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is True
    assert saved.last_exit_code == 75
    assert request_store.list_pending() == []


def test_managed_watch_service_once_retries_until_first_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-463: a retry result keeps a once Watch armed until its first event."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Wait until ready",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix="Continue after the event.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    results = iter(
        [
            _CycleResult(exit_code=75, stdout="", stderr="not ready", timed_out=False),
            _CycleResult(exit_code=0, stdout="ready", stderr="", timed_out=False),
        ]
    )
    calls = 0

    async def fake_run_cycle(*args, **kwargs):
        nonlocal calls
        calls += 1
        return next(results)

    monkeypatch.setattr(service, "_run_cycle", fake_run_cycle)
    service._running = True
    service._requires_service_lease = False

    asyncio.run(service._run_watch(watch.id))

    saved = store.get_watch(watch.id)
    pending = request_store.list_pending()
    assert calls == 2
    assert saved is not None and saved.enabled is False
    assert saved.last_exit_code == 0
    assert len(pending) == 1
    assert pending[0].prompt.endswith("ready")


@pytest.mark.parametrize(
    ("language", "expected_prefix", "expected_body"),
    [
        (
            "en",
            "Watch stopped after reaching its lifetime timeout.",
            "Watch 'Bounded wait' reached its lifetime timeout after 0 second(s).",
        ),
        (
            "zh",
            "Watch 达到生命周期上限后已停止。",
            "Watch「Bounded wait」已达到 0 秒的生命周期上限。",
        ),
    ],
)
def test_retrying_once_watch_lifetime_bounds_a_long_retry_delay(
    tmp_path: Path,
    monkeypatch,
    language: str,
    expected_prefix: str,
    expected_body: str,
) -> None:
    """HFR-470: the overall lifetime truncates a once Watch retry delay."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    watch = store.add_watch(
        name="Bounded wait",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0.05,
        retry_exit_codes=[75],
        retry_delay_seconds=10,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(config=SimpleNamespace(language=language)),
        store=store,
        request_store=request_store,
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )

    async def fake_run_cycle(*args, **kwargs):
        return _CycleResult(exit_code=75, stdout="", stderr="not ready", timed_out=False)

    monkeypatch.setattr(service, "_run_cycle", fake_run_cycle)
    service._running = True
    service._requires_service_lease = False

    started = time.monotonic()
    asyncio.run(service._run_watch(watch.id))

    saved = store.get_watch(watch.id)
    assert time.monotonic() - started < 1
    assert saved is not None and saved.enabled is False
    assert saved.last_exit_code == 124
    pending = request_store.list_pending()
    assert len(pending) == 1
    assert expected_prefix in pending[0].prompt
    assert expected_body in pending[0].prompt


@pytest.mark.parametrize("mode", ["once", "forever"])
@pytest.mark.parametrize("origin_source", ["metadata", "legacy_created_at"])
def test_watch_lifetime_origin_survives_supervisor_restart(
    tmp_path: Path,
    monkeypatch,
    mode: str,
    origin_source: str,
) -> None:
    """HFR-470: one armed episode keeps its deadline across supervisor restarts."""

    path = tmp_path / "watches.json"
    store = ManagedWatchStore(path)
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    watch = store.add_watch(
        name="Bounded wait",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode=mode,
        timeout_seconds=5,
        lifetime_timeout_seconds=1,
        retry_exit_codes=[75],
        retry_delay_seconds=10,
        post_to=None,
        deliver_key=None,
    )
    expired_origin = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    if origin_source == "metadata":
        watch.metadata[LIFETIME_STARTED_AT_METADATA_KEY] = expired_origin
    else:
        watch.metadata.pop(LIFETIME_STARTED_AT_METADATA_KEY)
        watch.created_at = expired_origin
    store.upsert_watch(watch)

    restarted_store = ManagedWatchStore(path)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=restarted_store,
        request_store=request_store,
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )

    async def unexpected_cycle(*args, **kwargs):
        raise AssertionError("a supervisor restart must not grant another cycle")

    monkeypatch.setattr(service, "_run_cycle", unexpected_cycle)
    service._running = True
    service._requires_service_lease = False

    asyncio.run(service._run_watch(watch.id))

    saved = restarted_store.get_watch(watch.id)
    assert saved is not None and saved.enabled is False
    assert saved.last_exit_code == 124
    assert len(request_store.list_pending()) == 1


@pytest.mark.parametrize("metadata_run_id", [None, "stale-run"])
def test_watch_lifetime_retires_while_an_existing_follow_up_is_active(
    tmp_path: Path,
    monkeypatch,
    metadata_run_id: str | None,
) -> None:
    """The actual queued Run owns expiry even when Watch metadata is absent or stale."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    watch = store.add_watch(
        name="Bounded follow-up",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix="Inspect the event.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0.05,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    if metadata_run_id is not None:
        assert store.mark_cycle_result(
            watch.id,
            exit_code=None,
            error=None,
            metadata_updates={FOLLOW_UP_RUN_ID_METADATA_KEY: metadata_run_id},
        )
    active = request_store.enqueue_hook_send(
        session_key=watch.session_key,
        prompt="existing event",
        run_type="watch",
        definition_id=watch.id,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )

    async def unexpected_cycle(*args, **kwargs):
        raise AssertionError("the follow-up fence must not admit another waiter")

    monkeypatch.setattr(service, "_run_cycle", unexpected_cycle)
    monkeypatch.setattr(watches_module, "WATCH_FOLLOW_UP_POLL_SECONDS", 0.005)
    service._running = True
    service._requires_service_lease = False

    started = time.monotonic()
    asyncio.run(service._run_watch(watch.id))

    saved = store.get_watch(watch.id)
    assert time.monotonic() - started < 1
    assert saved is not None and saved.enabled is False
    assert saved.last_exit_code == 124
    assert "retired without starting another Run" in str(saved.last_error)
    assert active.id in str(saved.last_error)
    pending = request_store.list_pending()
    assert [request.id for request in pending] == [active.id]


def test_once_watch_retries_timeout_only_when_124_is_allowed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-471: timeout retries require explicit policy on a once Watch."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    watch = store.add_watch(
        name="Retry one timeout",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix="Continue after the event.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[124],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    results = iter(
        [
            _CycleResult(exit_code=124, stdout="", stderr="", timed_out=True),
            _CycleResult(exit_code=0, stdout="ready", stderr="", timed_out=False),
        ]
    )

    async def fake_run_cycle(*args, **kwargs):
        return next(results)

    monkeypatch.setattr(service, "_run_cycle", fake_run_cycle)
    service._running = True
    service._requires_service_lease = False

    asyncio.run(service._run_watch(watch.id))

    saved = store.get_watch(watch.id)
    assert saved is not None and saved.enabled is False
    assert saved.last_exit_code == 0
    assert len(request_store.list_pending()) == 1


def test_forever_watch_waits_for_previous_follow_up_before_rearming(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-464: queued/running follow-ups and cooldown both hold re-arm closed."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Disk pressure",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix="Inspect the new pressure event.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    calls = 0

    async def fake_run_cycle(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _CycleResult(exit_code=0, stdout="event 1", stderr="", timed_out=False)
        service._running = False
        return _CycleResult(
            exit_code=NO_EVENT_EXIT_CODE,
            stdout="",
            stderr=NO_EVENT_MARKER,
            timed_out=False,
        )

    monkeypatch.setattr(service, "_run_cycle", fake_run_cycle)
    monkeypatch.setattr(watches_module, "WATCH_MIN_REARM_SECONDS", 0.1)
    monkeypatch.setattr(watches_module, "WATCH_FOLLOW_UP_POLL_SECONDS", 0.005)
    service._running = True
    service._requires_service_lease = False

    async def _run() -> None:
        task = asyncio.create_task(service._run_watch(watch.id))
        for _ in range(100):
            if request_store.list_pending():
                break
            await asyncio.sleep(0.01)
        assert calls == 1
        await asyncio.sleep(0.05)
        assert calls == 1, "an unfinished event Run must hold the waiter closed"
        request = request_store.list_pending()[0]
        claimed = request_store.claim(request.id)
        assert claimed is not None
        request_store.complete(claimed, ok=True)
        await asyncio.sleep(0.03)
        assert calls == 1, "the post-Run re-arm delay must hold the waiter closed"
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(_run())

    assert calls == 2


def test_atomic_watch_outbox_rejects_a_second_unsettled_follow_up(
    tmp_path: Path,
) -> None:
    """HFR-466: atomic admission rejects a second unfinished Watch follow-up."""

    store = ManagedWatchStore()
    request_store = TaskExecutionStore()
    assert store.sqlite_backend is not None
    assert request_store.sqlite_backend is not None
    assert store.sqlite_backend.db_path == request_store.sqlite_backend.db_path
    session_id = _bare_watch_session_row(workdir=tmp_path, anchor="single_flight")
    watch = store.add_watch(
        name="Single-flight watch",
        session_key="",
        session_id=session_id,
        session_policy="existing",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix="Inspect it.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )

    assert service._commit_cycle_result(
        watch,
        exit_code=0,
        error=None,
        event_detected=True,
        prompt="first event",
    )
    reloaded = store.get_watch(watch.id)
    assert reloaded is not None
    assert not service._commit_cycle_result(
        reloaded,
        exit_code=0,
        error=None,
        event_detected=True,
        prompt="duplicate event",
    )

    assert len(request_store.list_pending()) == 1
    unsettled = request_store.get_unsettled_watch_run(watch.id)
    assert unsettled is not None
    assert unsettled["prompt"] == "first event"
    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.metadata[DELIVERY_ACK_METADATA_KEY] == 1


def test_forever_watch_sixth_rapid_success_pauses_and_queues_one_repair_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-465: the sixth rapid event pauses once and preserves repair evidence."""

    store = ManagedWatchStore()
    request_store = TaskExecutionStore()
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    session_id = _bare_watch_session_row(workdir=tmp_path, anchor="circuit_repair")
    watch = store.add_watch(
        name="Level-triggered disk alert",
        session_key="",
        session_id=session_id,
        session_policy="existing",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix="Inspect the disk alert.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    calls = 0

    async def fake_run_cycle(*args, **kwargs):
        nonlocal calls
        calls += 1
        stdout = f"persistent level {calls}"
        stderr = "diagnostic stderr"
        if calls == 6:
            stdout = ("old stdout\n" * 500) + stdout
            stderr = ("old stderr\n" * 500) + stderr
        return _CycleResult(
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
        )

    monkeypatch.setattr(service, "_run_cycle", fake_run_cycle)
    monkeypatch.setattr(watches_module, "WATCH_MIN_REARM_SECONDS", 0)
    monkeypatch.setattr(watches_module, "WATCH_FOLLOW_UP_POLL_SECONDS", 0.001)
    service._running = True
    service._requires_service_lease = False

    async def _run() -> None:
        task = asyncio.create_task(service._run_watch(watch.id))
        while not task.done():
            for request in request_store.list_pending():
                outcome = request.metadata.get(WATCH_HOOK_OUTCOME_METADATA_KEY)
                if outcome != WATCH_HOOK_OUTCOME_CIRCUIT_REPAIR:
                    claimed = request_store.claim(request.id)
                    assert claimed is not None
                    request_store.complete(claimed, ok=True)
            await asyncio.sleep(0.001)
        await task

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    pending = request_store.list_pending()
    completed = request_store.list_runs(status="succeeded")
    assert calls == 6
    assert saved is not None and saved.enabled is False
    assert saved.retired_at is None
    assert saved.last_error is not None
    assert "60 seconds" in saved.last_error
    assert saved.metadata[DELIVERY_ACK_METADATA_KEY] == 5
    assert len(saved.metadata[RECENT_EVENT_TIMESTAMPS_METADATA_KEY]) == 6
    incident = saved.metadata[CIRCUIT_BREAKER_METADATA_KEY]
    assert incident["repair_run_id"] == saved.metadata[FOLLOW_UP_RUN_ID_METADATA_KEY]
    assert len(incident["stdout"]) <= WATCH_CIRCUIT_OUTPUT_LIMIT
    assert len(incident["stderr"]) <= WATCH_CIRCUIT_OUTPUT_LIMIT
    assert incident["stdout"].endswith("persistent level 6")
    assert incident["stderr"].endswith("diagnostic stderr")
    assert len(completed) == 5
    assert len(pending) == 1
    assert pending[0].id in saved.last_error
    assert (
        pending[0].metadata[WATCH_HOOK_OUTCOME_METADATA_KEY]
        == WATCH_HOOK_OUTCOME_CIRCUIT_REPAIR
    )
    assert "Level-triggered disk alert" in pending[0].prompt
    assert "persistent level 6" in pending[0].prompt
    assert "allowed retry exit codes: 75" in pending[0].prompt
    assert "vibe watch resume" in pending[0].prompt

    restarted = ManagedWatchService(
        controller=SimpleNamespace(),
        store=ManagedWatchStore(),
        request_store=request_store,
        runtime_store=runtime_store,
    )
    restarted._running = True
    restarted._requires_service_lease = False
    asyncio.run(restarted._run_watch(watch.id))
    assert len(request_store.list_pending()) == 1


def test_watch_event_burst_window_excludes_successes_older_than_sixty_seconds() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    timestamps = [
        (now - timedelta(seconds=61)).isoformat(),
        (now - timedelta(seconds=59)).isoformat(),
        now.isoformat(),
    ]

    recent = _recent_event_timestamps(
        {RECENT_EVENT_TIMESTAMPS_METADATA_KEY: timestamps},
        now=now,
    )

    assert recent == timestamps[1:]


def test_resuming_circuit_paused_watch_clears_window_but_keeps_repair_fence(
    tmp_path: Path,
) -> None:
    """HFR-467: resume clears burst state without releasing the active repair Run."""

    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="Disk alert",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
        metadata={
            FOLLOW_UP_RUN_ID_METADATA_KEY: "run-repair",
            RECENT_EVENT_TIMESTAMPS_METADATA_KEY: ["2026-08-10T00:00:00+00:00"],
            CIRCUIT_BREAKER_METADATA_KEY: {
                "status": "tripped",
                "repair_run_id": "run-repair",
            },
        },
    )
    store.set_enabled(watch.id, False)
    prior_lifetime_origin = watch.metadata[LIFETIME_STARTED_AT_METADATA_KEY]

    resumed = store.set_enabled(watch.id, True)

    assert RECENT_EVENT_TIMESTAMPS_METADATA_KEY not in resumed.metadata
    assert resumed.metadata[FOLLOW_UP_RUN_ID_METADATA_KEY] == "run-repair"
    assert resumed.metadata[CIRCUIT_BREAKER_METADATA_KEY]["status"] == "resumed"
    assert resumed.metadata[LIFETIME_STARTED_AT_METADATA_KEY] != prior_lifetime_origin


def test_storage_resume_applies_the_same_circuit_reset_as_cli() -> None:
    """HFR-468: storage and CLI resume paths apply the same circuit reset."""

    store = ManagedWatchStore()
    assert store.sqlite_backend is not None
    watch = store.add_watch(
        name="Disk alert",
        session_key="slack::channel::C123",
        command=[sys.executable, "wait.py"],
        shell_command=None,
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
        metadata={
            FOLLOW_UP_RUN_ID_METADATA_KEY: "run-repair",
            RECENT_EVENT_TIMESTAMPS_METADATA_KEY: ["2026-08-10T00:00:00+00:00"],
            CIRCUIT_BREAKER_METADATA_KEY: {
                "status": "tripped",
                "repair_run_id": "run-repair",
            },
        },
    )
    assert store.sqlite_backend.set_definition_enabled(
        watch.id,
        False,
        definition_type="watch",
    )
    prior_lifetime_origin = watch.metadata[LIFETIME_STARTED_AT_METADATA_KEY]
    assert store.sqlite_backend.set_definition_enabled(
        watch.id,
        True,
        definition_type="watch",
    )

    store.load()
    resumed = store.get_watch(watch.id)
    assert resumed is not None
    assert RECENT_EVENT_TIMESTAMPS_METADATA_KEY not in resumed.metadata
    assert resumed.metadata[FOLLOW_UP_RUN_ID_METADATA_KEY] == "run-repair"
    assert resumed.metadata[CIRCUIT_BREAKER_METADATA_KEY]["status"] == "resumed"
    assert resumed.metadata[LIFETIME_STARTED_AT_METADATA_KEY] != prior_lifetime_origin


def test_changing_watch_cwd_starts_a_fresh_burst_window(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    old_cwd = tmp_path / "old"
    new_cwd = tmp_path / "new"
    watch = store.add_watch(
        name="Relative waiter",
        session_key="slack::channel::C123",
        command=["./wait.py"],
        shell_command=None,
        prefix="Inspect it.",
        cwd=str(old_cwd),
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
        metadata={
            RECENT_EVENT_TIMESTAMPS_METADATA_KEY: ["2026-08-10T00:00:00+00:00"],
            FOLLOW_UP_RUN_ID_METADATA_KEY: "run-before-cwd-change",
        },
    )
    lifetime_origin = watch.metadata[LIFETIME_STARTED_AT_METADATA_KEY]

    updated = store.update_watch(
        watch.id,
        name=watch.name,
        session_key=watch.session_key,
        session_id=watch.session_id,
        command=watch.command,
        shell_command=watch.shell_command,
        prefix=watch.prefix,
        cwd=str(new_cwd),
        mode=watch.mode,
        timeout_seconds=watch.timeout_seconds,
        lifetime_timeout_seconds=watch.lifetime_timeout_seconds,
        retry_exit_codes=watch.retry_exit_codes,
        retry_delay_seconds=watch.retry_delay_seconds,
        post_to=watch.post_to,
        deliver_key=watch.deliver_key,
        agent_name=watch.agent_name,
        session_policy=watch.session_policy,
        message=watch.message,
        metadata=watch.metadata,
    )

    assert RECENT_EVENT_TIMESTAMPS_METADATA_KEY not in updated.metadata
    assert updated.metadata[FOLLOW_UP_RUN_ID_METADATA_KEY] == "run-before-cwd-change"
    assert updated.metadata[LIFETIME_STARTED_AT_METADATA_KEY] == lifetime_origin


def test_managed_watch_service_once_no_event_exit_finishes_without_follow_up(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Green CI waiter",
        session_key="slack::channel::C123",
        command=_quiet_waiter_command(),
        shell_command=None,
        prefix="CI failed. Fix it.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        service.start()
        for _ in range(100):
            saved = store.get_watch(watch.id)
            if saved and not saved.enabled:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is False
    assert saved.last_exit_code == NO_EVENT_EXIT_CODE
    assert saved.last_error is None
    # The whole point: a completed cycle that found nothing costs no Agent turn.
    assert request_store.list_pending() == []


def test_managed_watch_service_logs_what_a_quiet_cycle_suppressed(tmp_path: Path, caplog) -> None:
    """A cycle that reports nothing still has to leave its summary somewhere.

    No hook carries a no-event cycle's output, so the waiter's own account of what
    it saw and chose not to report — the green CI table, the filtered comments —
    died with the process. ``--only-on-failure`` documents that summary as
    inspectable, which it was not.
    """
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Green CI waiter",
        session_key="slack::channel::C123",
        command=_quiet_waiter_command("All watched workflows succeeded: lint, build"),
        shell_command=None,
        prefix="CI failed. Fix it.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        service.start()
        for _ in range(100):
            saved = store.get_watch(watch.id)
            if saved and not saved.enabled:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    with caplog.at_level(logging.INFO, logger="core.watches"):
        asyncio.run(_run())

    assert request_store.list_pending() == []
    quiet_logs = [record.getMessage() for record in caplog.records if "found nothing to report" in record.getMessage()]
    assert quiet_logs, caplog.text
    assert watch.id in quiet_logs[0]
    assert "All watched workflows succeeded: lint, build" in quiet_logs[0]


def test_managed_watch_service_forever_no_event_exit_rearms_without_follow_up(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Quiet forever waiter",
        session_key="slack::channel::C123",
        command=_quiet_waiter_command(),
        shell_command=None,
        prefix="Something happened.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            saved = store.get_watch(watch.id)
            if saved is not None and saved.last_exit_code == NO_EVENT_EXIT_CODE:
                break
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is True
    assert saved.last_exit_code == NO_EVENT_EXIT_CODE
    assert saved.last_error is None
    assert request_store.list_pending() == []


def test_managed_watch_service_treats_an_unmarked_exit_64_as_a_failure(tmp_path: Path) -> None:
    """64 is ``sysexits`` EX_USAGE, not a private Avibe signal.

    A generic watched command that rejects its own arguments exits 64. Reading that
    as a quiet cycle would swallow the failure notice the user relies on and, in
    forever mode, rerun the broken command for as long as the watch lives.
    """
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Misconfigured forever waiter",
        session_key="slack::channel::C123",
        command=[
            sys.executable,
            "-c",
            f"import sys; print('usage: mytool [--flag]', file=sys.stderr); sys.exit({NO_EVENT_EXIT_CODE})",
        ],
        shell_command=None,
        prefix="Investigate the failure.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        for _ in range(100):
            if watch.id not in service._active_tasks:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    pending = request_store.list_pending()
    assert saved is not None
    # Stopped, recorded with its error text, and reported once — as before the
    # no-event code existed.
    assert saved.enabled is False
    assert saved.last_exit_code == NO_EVENT_EXIT_CODE
    assert saved.last_error and "usage: mytool" in saved.last_error
    assert len(pending) == 1
    assert "usage: mytool" in pending[0].prompt


def test_managed_watch_service_forever_non_retry_error_disables_and_enqueues_failure(tmp_path: Path) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Broken forever waiter",
        session_key="slack::channel::C123",
        command=["python3", "-c", "import sys; sys.exit(1)"],
        shell_command=None,
        prefix="Investigate the failure.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        for _ in range(100):
            if watch.id not in service._active_tasks:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    pending = request_store.list_pending()
    assert saved is not None
    assert saved.enabled is False
    assert saved.last_exit_code == 1
    assert saved.last_error
    assert len(pending) == 1
    assert pending[0].prompt.startswith("Investigate the failure.\n\nWatch 'Broken forever waiter' stopped because the waiter exited with code 1.")


def test_managed_watch_service_fuses_watch_after_store_error(tmp_path: Path) -> None:
    class FailingResultStore(ManagedWatchStore):
        def __init__(self, path: Path):
            super().__init__(path)
            self.starts = 0

        def mark_cycle_start(self, watch_id: str) -> bool:
            self.starts += 1
            return super().mark_cycle_start(watch_id)

        def mark_cycle_result(self, *args, **kwargs) -> bool:
            raise RuntimeError("database disk image is malformed")

    store = FailingResultStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Broken persistence",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "import sys; sys.exit(75)"],
        shell_command=None,
        prefix="Should not storm.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        await _await_fused_watch(service, watch.id)
        # Settle briefly: a fused store must not let the cycle start again.
        await asyncio.sleep(0.08)
        await service.stop()

    asyncio.run(_run())

    assert store.starts == 1
    assert service._store_error_fused is True
    assert request_store.list_pending() == []


def test_managed_watch_service_fuses_quiet_cycle_after_store_error(tmp_path: Path) -> None:
    """A quiet cycle commits through the same wrapper as every other result branch.

    Called synchronously, a raising ``mark_cycle_result`` escapes ``_run_cycle`` and
    kills the watch task with the store unfused and the cycle unrecorded. Reconcile
    then restarts the same quiet cycle and repeats it, so a real storage failure
    never reaches the user as one.
    """

    class FailingResultStore(ManagedWatchStore):
        def __init__(self, path: Path):
            super().__init__(path)
            self.starts = 0

        def mark_cycle_start(self, watch_id: str) -> bool:
            self.starts += 1
            return super().mark_cycle_start(watch_id)

        def mark_cycle_result(self, *args, **kwargs) -> bool:
            raise RuntimeError("database disk image is malformed")

    store = FailingResultStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = store.add_watch(
        name="Quiet with broken persistence",
        session_key="slack::channel::C123",
        command=_quiet_waiter_command("nothing to report"),
        shell_command=None,
        prefix="Should not storm.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        await _await_fused_watch(service, watch.id)
        # Settle briefly: a fused store must not let the cycle start again.
        await asyncio.sleep(0.08)
        await service.stop()

    asyncio.run(_run())

    assert store.starts == 1, "the fused store let the quiet cycle restart"
    assert service._store_error_fused is True
    # A quiet cycle authorises no hook, and a failed one must not invent it.
    assert request_store.list_pending() == []


def test_managed_watch_service_fuses_reconcile_after_store_read_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)

    class FailingListStore(ManagedWatchStore):
        def __init__(self, path: Path):
            super().__init__(path)
            self.calls = 0

        def list_watches(self):
            self.calls += 1
            raise RuntimeError("database disk image is malformed")

    store = FailingListStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        service._running = True
        task = asyncio.create_task(service._watch_store())
        await asyncio.sleep(0.05)
        service._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert service._store_error_fused is True
    assert store.calls == 3
    assert service._active_tasks == {}


def test_managed_watch_service_idle_tick_does_not_write_runtime_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)

    class IdleStore(ManagedWatchStore):
        def __init__(self, path: Path):
            super().__init__(path)
            self.reloads = 0
            self.lists = 0

        def maybe_reload(self) -> bool:
            self.reloads += 1
            return False

        def list_watches(self):
            self.lists += 1
            return super().list_watches()

    class CountingRuntimeStore(WatchRuntimeStateStore):
        def __init__(self) -> None:
            self.writes = 0

        def write(self, payload: dict) -> None:
            self.writes += 1

        def load(self) -> dict:
            return {"watches": {}}

    store = IdleStore(tmp_path / "watches.json")
    runtime_store = CountingRuntimeStore()
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    service._running = True
    service._recovery_pending = False
    service._reconcile_dirty = False
    service._runtime_state_dirty = False

    async def _run() -> None:
        task = asyncio.create_task(service._watch_store())
        await asyncio.sleep(0.05)
        service._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert store.reloads > 0
    assert store.lists == 0
    assert runtime_store.writes == 0


def test_managed_watch_service_start_reaps_stale_worker_for_deleted_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    identity = _persisted_identity(pid=1234)
    _record_watch_pid(runtime_store, "stale-watch", 1234, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    terminated: list[int] = []
    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr("core.watches.inspect_process_identity", lambda _pid: identity)
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda pid, *_args, **_kwargs: terminated.append(pid) or True,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        try:
            assert runtime_store.load()["watches"] == {}
        finally:
            await service.stop()

    asyncio.run(_run())
    assert terminated == [1234]


@pytest.mark.parametrize(
    ("command", "shell_command"),
    [
        ([sys.executable, "-c", "print('watch')"], None),
        (["/tmp/wait.py"], None),
        ([], "python3 wait.py --forever"),
    ],
    ids=["exec", "shebang-interpreter", "shell-wrapper"],
)
def test_managed_watch_service_start_reaps_matching_stale_worker_before_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    shell_command: str | None,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=command, shell_command=shell_command)
    identity = _persisted_identity()
    _record_watch_pid(runtime_store, watch.id, 4321, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    events: list[tuple[str, int | None]] = []

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda pid: _live_identity(pid=pid),
    )

    def fake_terminate(pid, _logger, _label, *, expected_identity):
        assert expected_identity == identity
        events.append(("terminate", pid))
        return True

    def fake_reconcile():
        events.append(("reconcile", None))
        return False

    monkeypatch.setattr("core.watches.terminate_process_tree_by_pid", fake_terminate)
    monkeypatch.setattr(service, "reconcile_watches", fake_reconcile)

    async def _run() -> None:
        await _start_watch_service(service)
        await service.stop()

    asyncio.run(_run())

    assert events == [("terminate", 4321), ("reconcile", None)]


def test_managed_watch_service_start_does_not_reap_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    command = [sys.executable, "wait.py"]
    watch = _add_recovery_watch(store, command=command)
    identity = _persisted_identity()
    _record_watch_pid(runtime_store, watch.id, 4321, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    reconciles = 0

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda pid: _live_identity(pid=pid, create_time=456.0),
    )
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("a reused pid must not be terminated"),
    )

    def fake_reconcile():
        nonlocal reconciles
        reconciles += 1
        return False

    monkeypatch.setattr(service, "reconcile_watches", fake_reconcile)

    async def _run() -> None:
        await _start_watch_service(service)
        await service.stop()

    asyncio.run(_run())

    assert reconciles == 1
    assert watch.id not in service._recovery_blocked_watch_ids


def test_managed_watch_service_start_blocks_respawn_when_worker_marker_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    command = [sys.executable, "wait.py"]
    watch = _add_recovery_watch(store, command=command)
    identity = _persisted_identity()
    _record_watch_pid(runtime_store, watch.id, 4321, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda pid: _live_identity(
            pid=pid,
            worker_fingerprint=fingerprint_process_marker("other-watch-worker"),
        ),
    )
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("a changed worker marker must not be terminated"),
    )

    async def _run() -> None:
        await _start_watch_service(service)
        assert watch.id not in service._active_tasks
        assert runtime_store.load()["watches"][watch.id]["process_identity"]["create_time"] == 123.0
        await service.stop()

    asyncio.run(_run())

    assert watch.id in service._recovery_blocked_watch_ids
    assert runtime_store.load()["watches"][watch.id]["pid"] == 4321


def test_managed_watch_service_start_blocks_legacy_live_worker_without_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    _record_watch_pid(runtime_store, watch.id, 4321)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda _pid: _live_identity(),
    )
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("a legacy entry must not cause termination"),
    )

    async def _run() -> None:
        await _start_watch_service(service)
        assert watch.id not in service._active_tasks
        await service.stop()

    asyncio.run(_run())

    assert watch.id in service._recovery_blocked_watch_ids
    assert runtime_store.load()["watches"][watch.id]["pid"] == 4321


def test_managed_watch_service_start_rejects_overflowing_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    runtime_store.write(
        {
            "watches": {
                watch.id: {
                    "running": True,
                    "pid": 4321,
                    "started_at": "2026-05-15T00:00:00+00:00",
                    "updated_at": "2026-05-15T00:00:01+00:00",
                    "process_identity": {
                        "pid": 4321,
                        "create_time": 10**1000,
                        "worker_fingerprint": TEST_FINGERPRINT,
                    },
                }
            }
        }
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda pid: _live_identity(pid=pid),
    )
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("a malformed identity must not cause termination"),
    )

    async def _run() -> None:
        await _start_watch_service(service)
        assert service._running is True
        assert watch.id in service._recovery_blocked_watch_ids
        assert watch.id not in service._active_tasks
        await service.stop()

    asyncio.run(_run())


def test_managed_watch_service_start_ignores_dead_recorded_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    _record_watch_pid(runtime_store, watch.id, 4321)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    reconciles = 0

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda _pid: False)
    monkeypatch.setattr("core.watches.process_group_exists", lambda *_args: False)
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda _pid: pytest.fail("a dead pid must not have its identity inspected"),
    )
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("a dead pid must not be terminated"),
    )

    def fake_reconcile():
        nonlocal reconciles
        reconciles += 1
        return False

    monkeypatch.setattr(service, "reconcile_watches", fake_reconcile)

    async def _run() -> None:
        await _start_watch_service(service)
        await service.stop()

    asyncio.run(_run())

    assert reconciles == 1


def test_managed_watch_service_start_reaps_group_after_leader_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    identity = _persisted_identity()
    _record_watch_pid(runtime_store, watch.id, 4321, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    events: list[str] = []

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda _pid: False)
    monkeypatch.setattr("core.watches.process_group_exists", lambda *_args: True)
    monkeypatch.setattr(
        "core.watches.terminate_process_group_by_pgid",
        lambda *_args, **_kwargs: events.append("terminate-group") or True,
    )
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda _pid: pytest.fail("an exited leader must not have its identity inspected"),
    )

    def fake_reconcile() -> bool:
        events.append("reconcile")
        return False

    monkeypatch.setattr(service, "reconcile_watches", fake_reconcile)

    async def _run() -> None:
        await _start_watch_service(service)
        await service.stop()

    asyncio.run(_run())

    assert events == ["terminate-group", "reconcile"]


def test_managed_watch_service_does_not_reap_unverified_group_after_leader_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    _record_watch_pid(runtime_store, watch.id, 4321)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )

    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda _pid: False)
    monkeypatch.setattr("core.watches.process_group_exists", lambda *_args: True)
    monkeypatch.setattr(
        "core.watches.terminate_process_group_by_pgid",
        lambda *_args, **_kwargs: pytest.fail("an unverified process group must not be terminated"),
    )

    async def _run() -> None:
        await _start_watch_service(service)
        assert watch.id in service._recovery_blocked_watch_ids
        assert watch.id not in service._active_tasks
        await service.stop()

    asyncio.run(_run())


def test_managed_watch_service_start_keeps_recovery_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    def inspect_stale_workers() -> _StaleWorkerRecovery:
        time.sleep(0.15)
        return _StaleWorkerRecovery(True)

    monkeypatch.setattr(service, "_inspect_stale_workers", inspect_stale_workers)
    monkeypatch.setattr(service, "reconcile_watches", lambda: False)

    async def _run() -> None:
        started = time.monotonic()
        service.start()
        assert time.monotonic() - started < 0.05
        startup_task = service._startup_task
        assert startup_task is not None
        await asyncio.sleep(0.01)
        assert not startup_task.done()
        await startup_task
        await service.stop()

    asyncio.run(_run())


def test_hfr_164_watch_restart_replaces_only_its_registration_generation(
    tmp_path: Path,
) -> None:
    class EmptyHandler:
        def scan(self, *, limit, occupied, cursor):  # noqa: ANN001, ANN202
            del limit, occupied, cursor
            return [], False

        async def process(self, item) -> None:  # noqa: ANN001
            raise AssertionError(item)

    async def _run() -> None:
        supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
        delivery_token = supervisor.register(
            RuntimeWorkLane.SESSION_DELIVERIES,
            EmptyHandler(),
        )
        await supervisor.activate()
        service = ManagedWatchService(
            controller=SimpleNamespace(runtime_work_supervisor=supervisor),
            store=ManagedWatchStore(tmp_path / "watches.json"),
            request_store=TaskExecutionStore(tmp_path / "task_requests"),
            runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
        )

        await _start_watch_service(service)
        first = service._runtime_work_token
        assert first is not None
        assert RuntimeWorkLane.SESSION_DELIVERIES in supervisor._registrations
        await service.stop()
        assert RuntimeWorkLane.WATCH_DEFINITIONS not in supervisor._registrations
        assert supervisor._registrations[RuntimeWorkLane.SESSION_DELIVERIES].token == delivery_token

        await _start_watch_service(service)
        second = service._runtime_work_token
        assert second is not None
        assert second.generation > first.generation
        await service.stop()
        await supervisor.stop()

    asyncio.run(_run())


def test_hfr_176_legacy_watch_probe_only_wakes_watch_definitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[RuntimeWorkLane] = []
    service = ManagedWatchService(
        controller=SimpleNamespace(
            runtime_work_supervisor=SimpleNamespace(
                notify=lambda *lanes: notified.extend(lanes),
            )
        ),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    service._running = True
    service._legacy_watch_signature = (1, 1, 1)
    monkeypatch.setattr(watches_module, "_path_signature", lambda _path: (2, 2, 2))

    async def stop_after_tick(_delay: float) -> None:
        service._running = False

    monkeypatch.setattr(watches_module.asyncio, "sleep", stop_after_tick)
    asyncio.run(service._legacy_runtime_work_probe())

    assert notified == [RuntimeWorkLane.WATCH_DEFINITIONS]


def test_hfr_179_watch_store_phases_are_single_flight_and_apply_on_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    service._recovery_pending = False
    service._runtime_state_dirty = False
    scan_entered = threading.Event()
    release_scan = threading.Event()
    guarded_entered = threading.Event()
    scan_threads: list[int] = []
    reconcile_threads: list[int] = []

    def blocking_reload() -> bool:
        scan_threads.append(threading.get_ident())
        scan_entered.set()
        assert release_scan.wait(timeout=1)
        return True

    monkeypatch.setattr(service.store, "maybe_reload", blocking_reload)

    async def _run() -> None:
        loop_thread = threading.get_ident()
        handler = _ManagedWatchRuntimeWorkHandler(service)
        scan = asyncio.create_task(
            asyncio.to_thread(
                handler.scan,
                limit=1,
                occupied=frozenset(),
                cursor=None,
            )
        )
        assert await asyncio.to_thread(scan_entered.wait, 1)

        guarded = asyncio.create_task(
            service._watch_store_call_async(
                "watch-a",
                "guarded-test",
                lambda: guarded_entered.set() or True,
                guarded=True,
            )
        )
        await asyncio.sleep(0)
        assert not guarded_entered.is_set()
        release_scan.set()
        items, has_more = await scan
        assert not has_more
        assert await guarded is True

        def reconcile(watches) -> bool:  # noqa: ANN001
            del watches
            reconcile_threads.append(threading.get_ident())
            return False

        monkeypatch.setattr(service, "reconcile_watches", reconcile)
        assert await handler.process(items[0]) is True
        assert scan_threads and scan_threads[0] != loop_thread
        assert reconcile_threads == [loop_thread]

    asyncio.run(_run())


def test_hfr_179_watch_wake_during_reconcile_replays_after_owner_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = RuntimeWorkSupervisor(reconcile_interval=3600)
    service = ManagedWatchService(
        controller=SimpleNamespace(runtime_work_supervisor=supervisor),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    service._recovery_pending = False
    service._reconcile_dirty = False
    service._runtime_state_dirty = True
    reload_calls = 0
    persist_entered = asyncio.Event()
    release_persist = asyncio.Event()
    second_reload = threading.Event()

    def reload_store() -> bool:
        nonlocal reload_calls
        reload_calls += 1
        if reload_calls == 2:
            second_reload.set()
        return False

    async def persist_runtime_state() -> None:
        persist_entered.set()
        await release_persist.wait()
        service._runtime_state_dirty = False

    monkeypatch.setattr(service.store, "maybe_reload", reload_store)
    monkeypatch.setattr(service, "_persist_runtime_state", persist_runtime_state)

    async def _run() -> None:
        supervisor.register(
            RuntimeWorkLane.WATCH_DEFINITIONS,
            _ManagedWatchRuntimeWorkHandler(service),
        )
        await supervisor.activate()
        await asyncio.wait_for(persist_entered.wait(), 1)

        supervisor.notify(RuntimeWorkLane.WATCH_DEFINITIONS)
        for _ in range(10):
            await asyncio.sleep(0)
        assert reload_calls == 1

        release_persist.set()
        assert await asyncio.to_thread(second_reload.wait, 1)
        assert reload_calls == 2
        await supervisor.stop()

    asyncio.run(_run())


def test_hfr_179_blocked_watch_recovery_arms_generation_scoped_recheck(
    tmp_path: Path,
) -> None:
    delayed: list[tuple[object, float]] = []
    supervisor = SimpleNamespace(
        notify_after=lambda token, delay: delayed.append((token, delay)),
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(runtime_work_supervisor=supervisor),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    token = object()
    service._runtime_work_token = token  # type: ignore[assignment]
    service._recovery_pending = False
    service._reconcile_dirty = False
    service._runtime_state_dirty = False
    service._recovery_blocked_watch_ids.add("watch-a")
    handler = _ManagedWatchRuntimeWorkHandler(service)
    item = RuntimeWorkItem(
        "managed-watches",
        {
            "recovery": _StaleWorkerRecovery(True),
            "unblocked": (),
            "changed": False,
            "watches": (),
            "store_error": None,
            "fused": False,
        },
        rearm_after_process=False,
    )

    assert asyncio.run(handler.process(item)) is True
    assert delayed == [(token, watches_module.WATCH_RECONCILE_INTERVAL_SECONDS)]


def test_hfr_179_pending_watch_recovery_uses_watch_lane_cadence(
    tmp_path: Path,
) -> None:
    delayed: list[tuple[object, float]] = []
    supervisor = SimpleNamespace(
        notify_after=lambda token, delay: delayed.append((token, delay)),
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(runtime_work_supervisor=supervisor),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    token = object()
    service._runtime_work_token = token  # type: ignore[assignment]
    handler = _ManagedWatchRuntimeWorkHandler(service)
    item = RuntimeWorkItem(
        "managed-watches",
        {
            "recovery": _StaleWorkerRecovery(False),
            "unblocked": (),
            "changed": False,
            "watches": (),
            "store_error": None,
            "fused": False,
        },
        rearm_after_process=False,
    )

    assert asyncio.run(handler.process(item)) is True
    assert service._recovery_pending is True
    assert delayed == [(token, watches_module.WATCH_RECONCILE_INTERVAL_SECONDS)]


def test_hfr_179_watch_store_fuse_stops_generation_reads_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    service._recovery_pending = False
    service._reconcile_dirty = False
    service._runtime_state_dirty = False
    reads = 0

    def fail_read() -> bool:
        nonlocal reads
        reads += 1
        raise OSError("watch store unavailable")

    monkeypatch.setattr(service.store, "maybe_reload", fail_read)
    handler = _ManagedWatchRuntimeWorkHandler(service)

    async def _run() -> None:
        outcomes = []
        for _ in range(watches_module.WATCH_STORE_RECONCILE_FUSE_FAILURES):
            items, _has_more = await asyncio.to_thread(
                handler.scan,
                limit=1,
                occupied=frozenset(),
                cursor=None,
            )
            outcomes.append(await handler.process(items[0]))

        assert outcomes[:-1] == [False] * (len(outcomes) - 1)
        assert outcomes[-1] is True
        assert service._store_error_fused is True
        assert reads == watches_module.WATCH_STORE_RECONCILE_FUSE_FAILURES

        persisted = 0

        async def persist_runtime_state() -> None:
            nonlocal persisted
            persisted += 1
            service._runtime_state_dirty = False

        monkeypatch.setattr(service, "_persist_runtime_state", persist_runtime_state)
        service._runtime_state_dirty = True
        items, _has_more = await asyncio.to_thread(
            handler.scan,
            limit=1,
            occupied=frozenset(),
            cursor=None,
        )
        assert await handler.process(items[0]) is True
        assert reads == watches_module.WATCH_STORE_RECONCILE_FUSE_FAILURES
        assert persisted == 1

    asyncio.run(_run())


def test_watch_runtime_state_persistence_failure_rearms_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "watch_runtime.json"),
    )
    service._recovery_pending = False
    service._reconcile_dirty = False
    service._runtime_state_dirty = True

    async def _persist_but_remain_dirty() -> None:
        service._runtime_state_dirty = True

    monkeypatch.setattr(service, "_persist_runtime_state", _persist_but_remain_dirty)
    handler = _ManagedWatchRuntimeWorkHandler(service)
    item = RuntimeWorkItem(
        "managed-watches",
        {
            "recovery": _StaleWorkerRecovery(True),
            "unblocked": (),
            "changed": False,
            "watches": (),
        },
        rearm_after_process=False,
    )

    assert asyncio.run(handler.process(item)) is False
    assert service._runtime_state_dirty is True


def test_managed_watch_service_start_retries_unreadable_runtime_before_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UnreadableRuntimeStore(WatchRuntimeStateStore):
        def __init__(self) -> None:
            self.writes = 0
            self.readable = False

        def load_for_recovery(self) -> dict:
            if not self.readable:
                raise OSError("runtime state is temporarily unavailable")
            return {"watches": {}}

        def write(self, payload: dict) -> None:
            self.writes += 1

    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = UnreadableRuntimeStore()
    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    reconciles = 0

    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("unreadable state must not cause termination"),
    )

    def fake_reconcile():
        nonlocal reconciles
        reconciles += 1
        return False

    monkeypatch.setattr(service, "reconcile_watches", fake_reconcile)

    async def _run() -> None:
        await _start_watch_service(service)
        assert service._recovery_pending is True
        assert reconciles == 0
        assert runtime_store.writes == 0
        runtime_store.readable = True
        for _ in range(100):
            if reconciles:
                break
            await asyncio.sleep(0.01)
        await service.stop()

    with caplog.at_level("WARNING"):
        asyncio.run(_run())

    assert reconciles >= 1
    assert runtime_store.writes > 0
    assert "Unable to read prior watch runtime state" in caplog.text


def test_managed_watch_service_start_retries_malformed_runtime_before_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_path = tmp_path / "watch_runtime.json"
    runtime_path.write_text("{not-json", encoding="utf-8")
    runtime_store = WatchRuntimeStateStore(runtime_path)
    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    reconciles = 0

    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("malformed state must not cause termination"),
    )

    def fake_reconcile():
        nonlocal reconciles
        reconciles += 1
        return False

    monkeypatch.setattr(service, "reconcile_watches", fake_reconcile)

    async def _run() -> None:
        await _start_watch_service(service)
        assert service._recovery_pending is True
        assert reconciles == 0
        runtime_path.write_text('{"watches": {}}', encoding="utf-8")
        for _ in range(100):
            if reconciles:
                break
            await asyncio.sleep(0.01)
        await service.stop()

    with caplog.at_level("WARNING"):
        asyncio.run(_run())

    assert reconciles >= 1
    assert "Unable to read prior watch runtime state" in caplog.text


def test_managed_watch_service_start_retries_unavailable_watch_list_before_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    _record_watch_pid(runtime_store, watch.id, 4321)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    list_calls = 0
    available = {"value": False}
    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)

    def failing_list():
        nonlocal list_calls
        list_calls += 1
        if not available["value"]:
            raise RuntimeError("watch definitions are temporarily unavailable")
        return store.list_watches()

    monkeypatch.setattr(store, "list_watches_for_recovery", failing_list)
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("an unavailable watch list must not cause termination"),
    )

    async def fake_run_watch(_watch_id: str) -> None:
        await asyncio.Future()

    monkeypatch.setattr(service, "_run_watch", fake_run_watch)

    async def _run() -> None:
        await _start_watch_service(service)
        assert service._running is True
        assert service._recovery_pending is True
        assert watch.id not in service._active_tasks
        available["value"] = True
        for _ in range(100):
            if watch.id in service._active_tasks:
                break
            await asyncio.sleep(0.01)
        assert watch.id in service._active_tasks
        await service.stop()

    with caplog.at_level("WARNING"):
        asyncio.run(_run())

    assert list_calls >= 2
    assert service._store_reconcile_failures == 0
    assert "Unable to read managed watch definitions" in caplog.text


def test_managed_watch_service_start_does_not_reap_with_malformed_watch_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    watches_path = tmp_path / "watches.json"
    original_store = ManagedWatchStore(watches_path)
    watch = _add_recovery_watch(original_store, command=[sys.executable, "wait.py"])
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    _record_watch_pid(runtime_store, watch.id, 4321)
    watches_path.write_text("{not-json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        store = ManagedWatchStore(watches_path)
        service = ManagedWatchService(
            controller=SimpleNamespace(),
            store=store,
            request_store=TaskExecutionStore(tmp_path / "task_requests"),
            runtime_store=runtime_store,
        )
        monkeypatch.setattr(
            "core.watches.runtime.pid_alive",
            lambda _pid: pytest.fail("a malformed watch store must not cause pid inspection"),
        )
        monkeypatch.setattr(
            "core.watches.terminate_process_tree_by_pid",
            lambda *_args, **_kwargs: pytest.fail("a malformed watch store must not cause termination"),
        )

        async def _run() -> None:
            await _start_watch_service(service)
            assert service._active_tasks == {}
            await service.stop()

        asyncio.run(_run())

    assert "Unable to read managed watch definitions" in caplog.text


def test_managed_watch_service_start_preserves_worker_state_when_reap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    command = [sys.executable, "wait.py"]
    watch = _add_recovery_watch(store, command=command)
    identity = _persisted_identity()
    _record_watch_pid(runtime_store, watch.id, 4321, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr("core.watches.inspect_process_identity", lambda _pid: identity)
    monkeypatch.setattr("core.watches.terminate_process_tree_by_pid", lambda *_args, **_kwargs: False)

    async def _run() -> None:
        await _start_watch_service(service)
        assert service._active_tasks == {}
        assert runtime_store.load()["watches"][watch.id]["pid"] == 4321
        await service.stop()

    asyncio.run(_run())

    assert watch.id in service._recovery_blocked_watch_ids
    assert runtime_store.load()["watches"][watch.id]["pid"] == 4321


def test_managed_watch_service_periodically_unblocks_after_stale_worker_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    command = [sys.executable, "wait.py"]
    watch = _add_recovery_watch(store, command=command)
    identity = _persisted_identity()
    _record_watch_pid(runtime_store, watch.id, 4321, identity=identity)
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )
    worker = {"alive": True}

    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("core.watches.runtime.pid_alive", lambda _pid: worker["alive"])
    monkeypatch.setattr("core.watches.process_group_exists", lambda *_args: False)
    monkeypatch.setattr("core.watches.inspect_process_identity", lambda _pid: identity)
    monkeypatch.setattr("core.watches.terminate_process_tree_by_pid", lambda *_args, **_kwargs: False)

    async def fake_run_watch(_watch_id: str) -> None:
        await asyncio.Future()

    monkeypatch.setattr(service, "_run_watch", fake_run_watch)

    async def _run() -> None:
        await _start_watch_service(service)
        assert watch.id in service._recovery_blocked_watch_ids
        assert watch.id not in service._active_tasks

        worker["alive"] = False
        for _ in range(100):
            if watch.id in service._active_tasks:
                break
            await asyncio.sleep(0.01)

        assert watch.id not in service._recovery_blocked_watch_ids
        assert watch.id not in service._unreaped_runtime_entries
        assert watch.id in service._active_tasks
        await service.stop()

    asyncio.run(_run())


def test_managed_watch_service_start_without_prior_runtime_state_reconciles_normally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManagedWatchStore(tmp_path / "watches.json")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    watch = _add_recovery_watch(store, command=[sys.executable, "wait.py"])
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(tmp_path / "task_requests"),
        runtime_store=runtime_store,
    )

    monkeypatch.setattr(
        store,
        "list_watches_for_recovery",
        lambda: pytest.fail("empty runtime state must not reload watch definitions"),
    )
    monkeypatch.setattr(
        "core.watches.runtime.pid_alive",
        lambda _pid: pytest.fail("empty runtime state must not inspect any pids"),
    )
    monkeypatch.setattr(
        "core.watches.inspect_process_identity",
        lambda _pid: pytest.fail("empty runtime state must not inspect any processes"),
    )
    monkeypatch.setattr(
        "core.watches.terminate_process_tree_by_pid",
        lambda *_args, **_kwargs: pytest.fail("empty runtime state must not cause termination"),
    )

    async def fake_run_watch(_watch_id: str) -> None:
        await asyncio.Future()

    monkeypatch.setattr(service, "_run_watch", fake_run_watch)

    async def _run() -> None:
        await _start_watch_service(service)
        assert watch.id in service._active_tasks
        await service.stop()

    asyncio.run(_run())


def test_managed_watch_service_retries_transient_reconcile_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("core.watches.WATCH_RECONCILE_INTERVAL_SECONDS", 0.01)

    class TransientListStore(ManagedWatchStore):
        def __init__(self, path: Path):
            super().__init__(path)
            self.failures_remaining = 2

        def list_watches(self):
            if self.failures_remaining > 0:
                self.failures_remaining -= 1
                raise RuntimeError("database is locked")
            return super().list_watches()

    store = TransientListStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        service._running = True
        task = asyncio.create_task(service._watch_store())
        for _ in range(100):
            if store.failures_remaining == 0 and service._store_reconcile_failures == 0:
                break
            await asyncio.sleep(0.05)
        service._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert service._store_error_fused is False
    assert service._store_reconcile_failures == 0


def test_managed_watch_service_start_retries_initial_reconcile_error(tmp_path: Path) -> None:
    class FailingListStore(ManagedWatchStore):
        def list_watches(self):
            raise RuntimeError("database disk image is malformed")

    store = FailingListStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        assert service._store_error_fused is False
        assert 1 <= service._store_reconcile_failures < 3
        await service.stop()

    asyncio.run(_run())


def test_managed_watch_service_ignores_runtime_state_write_failure(tmp_path: Path) -> None:
    class FailingRuntimeStore(WatchRuntimeStateStore):
        def __init__(self) -> None:
            self.writes = 0

        def write(self, payload: dict) -> None:
            self.writes += 1
            raise RuntimeError("database disk image is malformed")

        def load(self) -> dict:
            return {"watches": {}}

        def load_for_recovery(self) -> dict:
            return {"watches": {}}

    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = FailingRuntimeStore()
    watch = store.add_watch(
        name="Runtime failure",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "print('done')"],
        shell_command=None,
        prefix="Finished.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        await _start_watch_service(service)
        for _ in range(100):
            if watch.id not in service._active_tasks:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is False
    assert runtime_store.writes > 0


#: The ``existing`` probe ``_upsert_definition`` runs before its guarded UPDATE.
#: The competing reclaim is committed when THIS read completes, which puts it
#: exactly inside the window the guard exists for: after the supervisor decided
#: what to write, before the write takes the lock.
_DEFINITION_EXISTS_SELECT = (
    "SELECT run_definitions.id FROM run_definitions WHERE run_definitions.id = ? LIMIT ? OFFSET ?"
)


def _bare_watch_session_row(*, workdir: Path, anchor: str = "slack_C123") -> str:
    """A real Session row for a watch to be bound to."""
    from config import paths as config_paths
    from storage.agent_session_rows import create_agent_session_row
    from storage.db import create_sqlite_engine

    engine = create_sqlite_engine(config_paths.get_sqlite_state_path())
    try:
        with engine.begin() as conn:
            return create_agent_session_row(
                conn,
                scope_id=None,
                session_anchor=anchor,
                agent_backend="codex",
                agent_variant="codex",
                model="gpt-5.5-codex",
                native_session_id="codex-native",
                workdir=str(workdir),
                require_workdir=False,
            )
    finally:
        engine.dispose()


def _commit_reclaim_after(engine, session_id: str, *, read: str, reason: str, occurrence: int = 1) -> dict:
    """Commit the REAL ``/new`` reclaim from a genuinely separate connection.

    The watch-side twin of ``_commit_competing_bind_after`` in
    ``tests/test_sqlite_sessions_store.py``: hooks ``after_cursor_execute`` on the
    engine the code under test uses, and when ``read`` completes opens its own
    engine, runs ``reclaim_bound_definitions`` and COMMITS. Control returns to the
    supervisor mid-write, so its next statement runs against a database another
    writer has already changed. Fires once; the returned dict records it, so a
    rendered-SQL drift shows up as "never raced" instead of a vacuous pass.

    ``occurrence`` selects WHICH guarded write to race. One ``_run_watch`` iteration
    runs several of them (``mark_cycle_start``, then ``mark_cycle_result``), and each
    emits ``read`` once, so ``occurrence=2`` lands the reclaim inside the RESULT
    stamp's write window -- after the start stamp has already been accepted. That is
    the window HFR-267 is about: the cycle legitimately ran, and the teardown arrives
    while its outcome is being recorded.
    """
    from sqlalchemy import event

    from config import paths as config_paths
    from storage.db import create_sqlite_engine
    from storage.session_reclaim import RECLAIM_PAUSE, reclaim_bound_definitions

    state: dict = {"fired": 0, "seen": 0, "summary": None}

    @event.listens_for(engine, "after_cursor_execute")
    def _race(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or " ".join(statement.split()) != read:
            return
        state["seen"] += 1
        if state["seen"] < occurrence:
            return
        state["fired"] += 1
        other = create_sqlite_engine(config_paths.get_sqlite_state_path())
        try:
            with other.begin() as other_conn:
                state["summary"] = reclaim_bound_definitions(
                    other_conn, session_id, mode=RECLAIM_PAUSE, reason=reason
                )
        finally:
            other.dispose()

    return state


def test_run_watch_stops_when_its_start_stamp_loses_to_a_reclaim(tmp_path: Path) -> None:
    """HFR-263 — a refused ``mark_cycle_start`` must stop the cycle, not be discarded.

    THE PRODUCTION STORY. A ``forever`` watch is bound to a Session. The user types
    ``/new`` in that channel: the teardown deletes the session row and
    ``reclaim_bound_definitions(mode='pause')`` pauses the watch and stamps its
    settings snapshot, and ``/new`` replies "1 watch paused". The supervisor loop was
    already past its own ``maybe_reload``, so it holds the pre-teardown mirror.

    THE CONSUMING END WAS MISSING. HFR-261 made ``mark_cycle_start`` a guarded
    full-row write that correctly REFUSES the stale payload and returns ``False`` --
    but ``_watch_store_call`` was ``callback(); return True``, discarding the
    callback's own return value. The refusal was reported to the loop as success, so
    the cycle went on to spawn its waiter subprocess and enqueue a hook against the
    definition the database had just torn down: the user is told the watch is paused
    while it runs a command and delivers a prompt into a dead session.

    Driven through the REAL ``_run_watch``, with the reclaim committed from a second
    connection INSIDE the write window. ``_run_cycle`` is the only double, and it
    returns a SUCCESS result on purpose: on the unfixed wrapper that is what produces
    the enqueued hook this test forbids.
    """
    from storage.session_reclaim import SESSION_SETTINGS_SNAPSHOT_KEY

    reason = "the bound agent session was cleared"
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test needs the SQLite-backed store; the guard lives there"
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    session_id = _bare_watch_session_row(workdir=tmp_path)
    watch = store.add_watch(
        name="Watch CI",
        session_key="",
        session_id=session_id,
        session_policy="existing",
        command=[sys.executable, "-c", "print('event')"],
        shell_command=None,
        prefix="CI is green.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
        metadata={"origin": "cli"},
    )

    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    service._running = True
    service._requires_service_lease = False

    cycles: list[str] = []

    async def _spy_cycle(watch_arg, *, timeout_seconds):  # noqa: ANN001
        cycles.append(watch_arg.id)
        return _CycleResult(exit_code=0, stdout="ci is green", stderr="", timed_out=False)

    service._run_cycle = _spy_cycle  # type: ignore[method-assign]

    race = _commit_reclaim_after(
        store._sqlite.engine, session_id, read=_DEFINITION_EXISTS_SELECT, reason=reason
    )

    asyncio.run(service._run_watch(watch.id))

    assert race["fired"] == 1, (
        "the competing reclaim never landed inside the write window, so this test "
        "proved nothing; the rendered SQL of the guarded upsert's existence probe drifted"
    )
    assert race["summary"] == {"paused": 1, "deleted": 0, "snapshotted": 1}, (
        f"the reclaim itself did not land ({race['summary']!r})"
    )

    assert cycles == [], (
        "the cycle ran its command after the store refused the start stamp; the "
        "waiter subprocess is spawned for a watch the teardown already paused"
    )
    assert request_store.list_pending() == [], (
        "a hook was enqueued for a refused cycle; the prompt is delivered into the "
        "session /new just tore down, after /new told the user the watch was paused"
    )

    stored = ManagedWatchStore().get_watch(watch.id)
    assert stored is not None
    assert stored.enabled is False, (
        "the refused start stamp re-enabled the watch the reclaim paused"
    )
    assert stored.last_error == reason, "the reclaim's pause reason was overwritten"
    assert stored.last_started_at is None, (
        "the start stamp partially landed — a lost compare-and-set must change NOTHING"
    )
    assert stored.last_exit_code is None and stored.last_finished_at is None, (
        "a success-shaped cycle result was recorded for a cycle that never ran"
    )
    assert SESSION_SETTINGS_SNAPSHOT_KEY in stored.metadata, (
        "the reclaim's settings snapshot was replaced by the pre-teardown metadata"
    )
    # HFR-271's rule: the refusal is only proven when the DURABLE row above and the LIVE
    # store the service is still running on say the same thing. ``store`` is the object
    # ``_run_watch`` mutated before the refused stamp, and the one ``reconcile_watches``
    # reads next.
    live = store.get_watch(watch.id)
    assert live is not None and live.to_dict() == stored.to_dict(), (
        "the start stamp was refused and the live store kept it: it serves "
        f"enabled={None if live is None else live.enabled!r} "
        f"last_started_at={None if live is None else live.last_started_at!r} while the "
        f"row says enabled={stored.enabled!r} last_started_at={stored.last_started_at!r}"
    )


#: Sentinel ``cwd``: replaced with a path under ``tmp_path`` that is deliberately
#: never created, so ``_missing_watch_cwd_error`` fires on the real filesystem.
_MISSING_CWD = "<missing-cwd>"

#: One row per ``_run_watch`` branch that enqueues a completion hook. ALL FIVE of
#: them: every ``_commit_cycle_result`` call site that can produce a hook is a row
#: here, so a branch cannot be added to production without a control below.
#:
#: ``cycles`` is what the doubled ``_run_cycle`` returns, one entry per call.
#: ``occurrence`` is which guarded definition write to land the reclaim inside --
#: every ``mark_cycle_start`` and ``mark_cycle_result`` emits the existence probe
#: once, in order, so the number identifies the RESULT stamp of the branch under
#: test. ``expect_hook`` is a fragment of the prompt the branch delivers, which is
#: what proves the control run reached that branch instead of some other one.
#: ``expect_cycle`` is False for the one branch that stops before the waiter spawns.
_HOOK_BRANCHES: dict[str, dict] = {
    # exit 0 -> the event fired; the watch delivers the waiter's stdout.
    "success": {
        "overrides": {"mode": "once", "lifetime_timeout_seconds": 0},
        "cycles": [_CycleResult(exit_code=0, stdout="ci is green", stderr="", timed_out=False)],
        "occurrence": 2,
        "expect_hook": "ci is green",
        "expect_exit_code": 0,
    },
    # the waiter overran its per-cycle timeout and 124 is not a retry code.
    "cycle_timeout": {
        "overrides": {"mode": "forever", "lifetime_timeout_seconds": 0},
        "cycles": [_CycleResult(exit_code=124, stdout="", stderr="", timed_out=True)],
        "occurrence": 2,
        "expect_hook": "the waiter timed out",
        "expect_exit_code": 124,
    },
    # the waiter exited non-zero with a code outside ``retry_exit_codes``.
    "terminal_failure": {
        "overrides": {"mode": "forever", "lifetime_timeout_seconds": 0},
        "cycles": [_CycleResult(exit_code=2, stdout="", stderr="boom", timed_out=False)],
        "occurrence": 2,
        "expect_hook": "exited with code 2",
        "expect_exit_code": 2,
    },
    # The supervisor's own deadline. The shared fixture arms the lifetime only after
    # cycle 1's retry result has landed, then expires it after iteration 2 reloads the
    # live mirror. The lifetime branch runs BEFORE ``mark_cycle_start``. Writes in order:
    # start #1, retry result #2, lifetime result #3.
    "lifetime_expiry": {
        "overrides": {"mode": "forever", "lifetime_timeout_seconds": 0},
        "cycles": [_CycleResult(exit_code=75, stdout="", stderr="not yet", timed_out=False)],
        "occurrence": 3,
        "expect_hook": "reached its lifetime timeout",
        "expect_exit_code": 124,
    },
    # the definition's working directory is gone (a deleted worktree). The check runs
    # right AFTER ``mark_cycle_start`` lands and before the waiter is spawned, so this
    # is the one hook-producing branch that never runs a cycle. Writes in order:
    # start #1, missing-cwd result #2.
    "missing_cwd": {
        "overrides": {"mode": "forever", "lifetime_timeout_seconds": 0, "cwd": _MISSING_CWD},
        "cycles": [],
        "occurrence": 2,
        "expect_hook": "working directory is no longer available",
        "expect_exit_code": 1,
        "expect_cycle": False,
    },
}


def _assert_branch_was_reached(branch: str, calls: list[str]) -> None:
    """The scenario is only wired if the branch under test was actually taken.

    Four branches are reached by running a cycle. ``missing_cwd`` is reached by NOT
    running one -- a spawned waiter there would mean the run took a different path --
    and the guarded result write the callers assert on is what proves it arrived.
    """

    if _HOOK_BRANCHES[branch].get("expect_cycle", True):
        assert calls, f"the {branch} branch never ran a cycle, so the scenario is not wired"
    else:
        assert calls == [], (
            f"the {branch} branch spawned a waiter, so the run did not take the "
            f"missing-cwd path at all: {calls!r}"
        )


def _hook_branch_service(tmp_path: Path, branch: str, *, request_store=None) -> tuple:
    """Build a real store/service/watch for one ``_HOOK_BRANCHES`` row.

    ``request_store`` defaults to a file-backed outbox, which is enough for the
    ordering tests. Pass ``TaskExecutionStore()`` for the SQLite outbox — the real
    ``agent_runs`` ledger, and the only backend that can share a transaction with the
    definition stamp (HFR-269).
    """

    spec = _HOOK_BRANCHES[branch]
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test needs the SQLite-backed store; the guard lives there"
    if request_store is None:
        request_store = TaskExecutionStore(tmp_path / f"requests_{branch}")
    runtime_store = WatchRuntimeStateStore(tmp_path / f"runtime_{branch}.json")
    session_id = _bare_watch_session_row(workdir=tmp_path, anchor=f"slack_C_{branch}")
    kwargs = {
        "name": f"Watch {branch}",
        "session_key": "",
        "session_id": session_id,
        "session_policy": "existing",
        "command": [sys.executable, "-c", "print('event')"],
        "shell_command": None,
        "prefix": "CI update.",
        "cwd": None,
        "mode": "forever",
        "timeout_seconds": 5,
        "lifetime_timeout_seconds": 0,
        "retry_exit_codes": [75],
        "retry_delay_seconds": 0.01,
        "post_to": None,
        "deliver_key": None,
        "metadata": {"origin": "cli"},
    }
    kwargs.update(spec["overrides"])
    if kwargs.get("cwd") == _MISSING_CWD:
        # Never created on purpose: the production check is a real ``Path.is_dir()``.
        kwargs["cwd"] = str(tmp_path / f"removed_worktree_{branch}")
    watch = store.add_watch(**kwargs)

    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    service._running = True
    service._requires_service_lease = False

    results = list(spec["cycles"])
    calls: list[str] = []

    async def _spy_cycle(watch_arg, *, timeout_seconds):  # noqa: ANN001
        calls.append(watch_arg.id)
        return results[min(len(calls) - 1, len(results) - 1)]

    service._run_cycle = _spy_cycle  # type: ignore[method-assign]
    if branch == "lifetime_expiry":
        # Production reaches _sleep_before_retry only after the guarded retry-result
        # stamp commits. Use that boundary as the condition; no wall-clock wait needed.
        retry_result_landed = False
        wait_for_follow_up_slot = service._wait_for_follow_up_slot

        async def _mark_retry_result_landed(watch_arg, *, lifetime_started):  # noqa: ANN001
            nonlocal retry_result_landed
            assert calls == [watch_arg.id], "lifetime expiry must follow exactly one retry cycle"
            retry_result_landed = True

        async def _expire_after_next_reload(watch_id, *, lifetime_started):  # noqa: ANN001
            slot = await wait_for_follow_up_slot(
                watch_id,
                lifetime_started=lifetime_started,
            )
            if retry_result_landed:
                live_watch = store.get_watch(watch_id)
                assert live_watch is not None
                elapsed = asyncio.get_running_loop().time() - lifetime_started
                assert elapsed > 0, "the retry result landed before the lifetime clock advanced"
                live_watch.lifetime_timeout_seconds = elapsed
            return slot

        service._sleep_before_retry = _mark_retry_result_landed  # type: ignore[method-assign]
        service._wait_for_follow_up_slot = _expire_after_next_reload  # type: ignore[method-assign]
    return store, service, watch, request_store, session_id, calls


@pytest.mark.parametrize("branch", list(_HOOK_BRANCHES))
def test_run_watch_enqueues_the_branch_hook_when_the_result_stamp_lands(tmp_path: Path, branch: str) -> None:
    """HFR-267 control — with no teardown racing it, every branch DOES deliver its hook.

    Without this, the refusal tests below could pass because the branch was never
    reached at all. Here the same wiring, minus the reclaim, must enqueue exactly one
    hook carrying that branch's own text.
    """
    _store, service, watch, request_store, _session_id, calls = _hook_branch_service(tmp_path, branch)

    asyncio.run(service._run_watch(watch.id))

    _assert_branch_was_reached(branch, calls)
    pending = request_store.list_pending()
    assert len(pending) == 1, f"the {branch} branch enqueued {len(pending)} hooks, expected exactly 1"
    assert _HOOK_BRANCHES[branch]["expect_hook"] in (pending[0].prompt or ""), (
        f"the enqueued hook is not the {branch} branch's: {pending[0].prompt!r}"
    )


@pytest.mark.parametrize("branch", list(_HOOK_BRANCHES))
def test_run_watch_enqueues_no_hook_when_the_result_stamp_is_refused(tmp_path: Path, branch: str) -> None:
    """HFR-267 — the guarded stamp must run BEFORE the hook it authorises, in every branch.

    THE PRODUCTION STORY. A watch is bound to a Session and its cycle completes. In the
    same instant the user types ``/new`` in that channel: the teardown deletes the
    session row, ``reclaim_bound_definitions(mode='pause')`` pauses the watch, and
    ``/new`` replies "1 watch paused".

    THE GUARD WAS ASKED TOO LATE. HFR-261 made ``mark_cycle_result`` a guarded
    compare-and-set and HFR-263 taught ``_watch_store_call`` to honour its refusal --
    but all four hook-enqueueing branches ran

        self._enqueue_hook(...)                      # durable request row, now unrecallable
        if not self._watch_store_call(..., guarded=True):
            return                                  # refused; unqueues nothing

    ``enqueue_hook_send`` writes a row a separate drain loop claims and executes, so
    ``return`` after the refusal cancels nothing: the prompt is still delivered into
    the session ``/new`` just deleted, for a definition the database has torn down,
    after the user was told the watch was paused. Respecting the guard's answer is not
    enough -- the effect has to come after the guard.

    Driven through the REAL ``_run_watch``, with the real ``/new`` reclaim committed
    from a second connection INSIDE the result stamp's write window. ``_run_cycle`` is
    the only double, and it returns each branch's own success/timeout/failure shape on
    purpose: on the unfixed ordering that is exactly what produces the hook this test
    forbids.
    """
    from storage.session_reclaim import SESSION_SETTINGS_SNAPSHOT_KEY

    reason = "the bound agent session was cleared"
    store, service, watch, request_store, session_id, calls = _hook_branch_service(tmp_path, branch)

    race = _commit_reclaim_after(
        store._sqlite.engine,
        session_id,
        read=_DEFINITION_EXISTS_SELECT,
        reason=reason,
        occurrence=_HOOK_BRANCHES[branch]["occurrence"],
    )

    asyncio.run(service._run_watch(watch.id))

    assert race["fired"] == 1, (
        f"the competing reclaim never landed inside the {branch} result stamp's write "
        f"window (saw {race['seen']} guarded writes, wanted #{_HOOK_BRANCHES[branch]['occurrence']}), "
        "so this test proved nothing"
    )
    assert race["summary"] == {"paused": 1, "deleted": 0, "snapshotted": 1}, (
        f"the reclaim itself did not land ({race['summary']!r})"
    )
    _assert_branch_was_reached(branch, calls)

    assert request_store.list_pending() == [], (
        f"the {branch} branch enqueued a hook for a cycle result the store REFUSED; the "
        "prompt is delivered into the session /new just tore down, after /new told the "
        "user the watch was paused"
    )

    stored = ManagedWatchStore().get_watch(watch.id)
    assert stored is not None
    assert stored.enabled is False, "the refused result stamp re-enabled the watch the reclaim paused"
    assert stored.last_error == reason, "the reclaim's pause reason was overwritten by the refused stamp"
    assert SESSION_SETTINGS_SNAPSHOT_KEY in stored.metadata, (
        "the reclaim's settings snapshot was replaced by the pre-teardown metadata"
    )
    # HFR-271's rule, on the live half a fresh store cannot see.
    live = service.store.get_watch(watch.id)
    assert live is not None and live.to_dict() == stored.to_dict(), (
        "the result stamp was refused and the live store kept it: it serves "
        f"enabled={None if live is None else live.enabled!r} "
        f"retired_at={None if live is None else live.retired_at!r} while the row says "
        f"enabled={stored.enabled!r} retired_at={stored.retired_at!r}"
    )


#: The outbox row's own INSERT. Matched by prefix so a new ``agent_runs`` column
#: cannot silently stop these tests from racing anything.
_AGENT_RUNS_INSERT = "INSERT INTO AGENT_RUNS"


def _is_agent_runs_insert(statement: str) -> bool:
    return " ".join(statement.split()).upper().startswith(_AGENT_RUNS_INSERT)


def _try_archive_before_the_outbox_insert(engines, session_id: str, *, reason: str) -> dict:
    """Try to commit the REAL archive teardown in the gap before the outbox INSERT.

    Hooks ``before_cursor_execute`` on every engine the code under test might write the
    ``agent_runs`` row through — the watch store's and the request store's, because
    WHICH one carries the INSERT is exactly what this test is about — and when that
    INSERT is about to run, opens its own engine and commits
    ``reclaim_bound_definitions(mode='delete')`` from a genuinely separate connection.

    ``PRAGMA busy_timeout = 0`` is what turns the outcome into a FACT instead of a five
    second sleep: at this instant the result stamp has already been decided, so if the
    stamp was its OWN transaction the database is unlocked and the teardown commits
    immediately; if the stamp and this INSERT are one transaction the write lock is
    already held and SQLite refuses at once with "database is locked". ``blocked``
    records which of the two happened, so "there is no gap" is asserted rather than
    assumed, and a rendered-SQL drift shows up as "never raced".
    """
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from config import paths as config_paths
    from storage.db import create_sqlite_engine
    from storage.session_reclaim import RECLAIM_DELETE, reclaim_bound_definitions

    state: dict = {"fired": 0, "blocked": None, "summary": None}

    def _race(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if state["fired"] or not _is_agent_runs_insert(statement):
            return
        state["fired"] += 1
        other = create_sqlite_engine(config_paths.get_sqlite_state_path())
        try:
            with other.begin() as other_conn:
                other_conn.exec_driver_sql("PRAGMA busy_timeout = 0")
                state["summary"] = reclaim_bound_definitions(
                    other_conn, session_id, mode=RECLAIM_DELETE, reason=reason
                )
            state["blocked"] = False
        except OperationalError as exc:
            # Serialised behind the transaction that holds the stamp: the teardown
            # cannot land in between, which is the property under test.
            state["blocked"] = True
            state["detail"] = str(exc)
        finally:
            other.dispose()

    for engine in engines:
        event.listens_for(engine, "before_cursor_execute")(_race)
    return state


def _fail_the_outbox_insert(engines) -> dict:
    """Make the outbox row's INSERT fail, wherever the code under test writes it."""
    from sqlalchemy import event

    state: dict = {"fired": 0}

    def _boom(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if not _is_agent_runs_insert(statement):
            return
        state["fired"] += 1
        raise RuntimeError("outbox write failed: disk I/O error")

    for engine in engines:
        event.listens_for(engine, "before_cursor_execute")(_boom)
    return state


def _hook_branch_service_on_the_run_ledger(tmp_path: Path, branch: str) -> tuple:
    """``_hook_branch_service`` on the SQLite outbox — the real ``agent_runs`` ledger."""

    request_store = TaskExecutionStore()
    assert request_store._sqlite is not None, "this test needs the SQLite outbox; a transaction is the fix"
    store, service, watch, request_store, session_id, calls = _hook_branch_service(
        tmp_path, branch, request_store=request_store
    )
    assert store._sqlite.db_path == request_store._sqlite.db_path, (
        "the definition and the outbox must be in ONE database for a shared transaction "
        "to be possible at all"
    )
    engines = [store._sqlite.engine, request_store._sqlite.engine]
    return store, service, watch, request_store, session_id, calls, engines


@pytest.mark.parametrize("branch", list(_HOOK_BRANCHES))
def test_the_atomic_stamp_and_its_hook_both_land_when_nothing_races_them(tmp_path: Path, branch: str) -> None:
    """HFR-269 control — with nothing racing and nothing faulting, BOTH halves land.

    A combined operation that simply refused everything would satisfy the two tests
    below vacuously: "no hook was queued" and "the watch was not retired" are precisely
    what a ``_commit_cycle_result`` that always returned False, or an
    ``upsert_watch_with_queued_run`` that never committed, would produce. So each
    terminal branch needs its positive half too, and this is it -- the same real
    ``_run_watch`` over the same real ``agent_runs`` ledger, with NO teardown racing the
    write and NO injected outbox fault. The one transaction must COMMIT, carrying both
    of the things it makes atomic:

    * the branch's own result transition, read back from the definition (that branch's
      terminal exit code, disabled, retired, finished), and
    * exactly ONE outbox row, carrying that branch's OWN prompt text -- so a branch
      cannot be credited with a row some other branch wrote, and "exactly one" rules
      out the atomic write and the fallback ``enqueue`` both firing.

    Parametrized over every row of ``_HOOK_BRANCHES``, which is every hook-producing
    ``_commit_cycle_result`` call site in ``_run_watch``: success, per-cycle timeout,
    terminal failure, lifetime expiry, and missing cwd.
    """
    _store, service, watch, request_store, _session_id, calls, _engines = (
        _hook_branch_service_on_the_run_ledger(tmp_path, branch)
    )

    asyncio.run(service._run_watch(watch.id))

    _assert_branch_was_reached(branch, calls)

    stored = ManagedWatchStore().get_watch(watch.id)
    assert stored is not None, f"the {branch} branch's definition is gone"
    assert stored.last_exit_code == _HOOK_BRANCHES[branch]["expect_exit_code"], (
        f"the {branch} branch's result transition did not commit: the definition records "
        f"exit {stored.last_exit_code!r}, not {_HOOK_BRANCHES[branch]['expect_exit_code']!r}"
    )
    assert stored.enabled is False and stored.retired_at is not None, (
        f"the {branch} branch is terminal, but the definition is still enabled/unretired "
        f"(enabled={stored.enabled!r}, retired_at={stored.retired_at!r}): the stamp was "
        "refused or rolled back when nothing opposed it"
    )
    assert stored.last_finished_at is not None, (
        f"the {branch} branch's stamp committed without recording that the cycle finished"
    )

    pending = request_store.list_pending()
    assert len(pending) == 1, (
        f"the {branch} branch queued {len(pending)} hooks on the run ledger, expected "
        "exactly 1; with nothing racing it, the hook the branch authorises must be durable"
    )
    assert _HOOK_BRANCHES[branch]["expect_hook"] in (pending[0].prompt or ""), (
        f"the queued hook is not the {branch} branch's own: {pending[0].prompt!r}"
    )
    assert pending[0].task_id == watch.id, (
        f"the queued hook belongs to {pending[0].task_id!r}, not to the watch under test"
    )


@pytest.mark.parametrize("branch", list(_HOOK_BRANCHES))
def test_no_teardown_can_commit_between_the_result_stamp_and_its_hook(tmp_path: Path, branch: str) -> None:
    """HFR-269 — the stamp and the hook it authorises must be ONE transaction.

    THE PRODUCTION STORY. A watch bound to a Session finishes a cycle. In the same
    instant the user archives that Session: the archive runs
    ``reclaim_bound_definitions(mode='delete')``, which soft-deletes the watch, and the
    confirm dialog reports it.

    ORDERING WAS NOT ENOUGH. HFR-267 put the guarded stamp before the hook, so the
    guard now runs before the effect it authorises — but the two are separate
    transactions:

        self.store.mark_cycle_result(...)              # transaction 1, COMMITS
        self.request_store.enqueue_hook_send(...)       # transaction 2, COMMITS

    and the archive commits in the GAP between those commits. The stamp is accepted —
    it won its compare-and-set fairly, against the state that existed when it ran — and
    the hook is queued afterwards anyway: a durable request row the drain loop will
    deliver into a session that no longer exists, under a definition the database has
    soft-deleted, after the user was told the archive was done. Nothing is refused,
    because by the time the teardown lands there is nothing left to refuse.

    Driven through the REAL ``_run_watch`` over the REAL ``agent_runs`` ledger, with the
    real archive reclaim attempted from a second connection at the instant the outbox
    INSERT is about to run, and a zero busy timeout so "could it commit in the gap?" is
    answered by the database rather than by a sleep. Green requires that it could NOT:
    the write lock the stamp took is still held, so the teardown is serialised behind
    the single transaction and can only land before the stamp (where HFR-267's tests
    show it is refused) or after the hook is already durable.
    """
    store, service, watch, request_store, session_id, calls, engines = (
        _hook_branch_service_on_the_run_ledger(tmp_path, branch)
    )

    race = _try_archive_before_the_outbox_insert(
        engines, session_id, reason="the session was archived"
    )

    outcome: dict = {}
    try:
        asyncio.run(service._run_watch(watch.id))
    except Exception as exc:  # noqa: BLE001 - the split-transaction path can also fault here
        outcome["error"] = exc

    _assert_branch_was_reached(branch, calls)
    assert race["fired"] == 1, (
        f"the {branch} branch never wrote an outbox row, so this test proved nothing; "
        "the rendered SQL of the run INSERT drifted"
    )
    assert race["blocked"] is True, (
        f"the archive teardown COMMITTED between the {branch} branch's result stamp and "
        f"its hook ({race['summary']!r}): the two are separate transactions, so a "
        "reclaim lands in the gap and the hook is queued anyway — delivered into the "
        "session the archive removed, under a definition it soft-deleted"
    )
    assert not outcome, f"the atomic stamp+hook write faulted: {outcome.get('error')!r}"

    stored = ManagedWatchStore().get_watch(watch.id)
    assert stored is not None
    assert stored.enabled is False and stored.retired_at is not None, (
        f"the {branch} branch's terminal stamp did not land with its hook"
    )
    assert stored.last_exit_code == _HOOK_BRANCHES[branch]["expect_exit_code"], (
        f"the stamp recorded exit {stored.last_exit_code!r} for the {branch} branch"
    )
    pending = request_store.list_pending()
    assert len(pending) == 1, f"the {branch} branch queued {len(pending)} hooks, expected exactly 1"
    assert _HOOK_BRANCHES[branch]["expect_hook"] in (pending[0].prompt or ""), (
        f"the queued hook is not the {branch} branch's: {pending[0].prompt!r}"
    )


@pytest.mark.parametrize("branch", list(_HOOK_BRANCHES))
def test_a_failed_outbox_write_does_not_retire_the_watch_with_its_hook_lost(
    tmp_path: Path, branch: str
) -> None:
    """HFR-269, inverse half — the other thing two commits cost.

    Same scenario id as ``test_no_teardown_can_commit_between_the_result_stamp_and_its_hook``:
    one defect (the stamp and its outbox row are not one transaction) with two failure
    halves, tested separately because they fail for opposite reasons -- that one needs a
    teardown racing the gap, this one needs no race at all, just a fault on the second
    commit.

    HFR-267's ordering traded one failure for another. With the stamp committed first
    and the hook second, a fault on the outbox write — a full disk, a locked database,
    any error between the two commits — leaves the watch DURABLY DISABLED and retired
    while the hook that tells the user it finished is gone. A ``once`` watch is
    unrecoverable at that point: the definition says "finished", the user was never
    told, and nothing will run it again.

    One transaction removes the trade instead of reversing it: the outbox INSERT and the
    stamp roll back together, so the watch stays enabled and its completion is still
    owed. The fault is injected at the INSERT itself, on whichever connection the code
    under test writes the row through.
    """
    store, service, watch, request_store, _session_id, calls, engines = (
        _hook_branch_service_on_the_run_ledger(tmp_path, branch)
    )

    boom = _fail_the_outbox_insert(engines)

    outcome: dict = {}
    try:
        asyncio.run(service._run_watch(watch.id))
    except Exception as exc:  # noqa: BLE001 - the unfixed path lets the fault escape the loop
        outcome["error"] = exc

    _assert_branch_was_reached(branch, calls)
    assert boom["fired"] >= 1, (
        f"the {branch} branch never attempted an outbox INSERT, so no fault was injected"
    )
    assert request_store.list_pending() == [], "the failed INSERT queued a hook anyway"

    stored = ManagedWatchStore().get_watch(watch.id)
    assert stored is not None
    assert stored.enabled is True, (
        f"the {branch} branch retired the watch while the hook that reports its outcome "
        "was lost to the failed outbox write; a once watch is finished, silent and "
        "unrecoverable"
    )
    assert stored.retired_at is None and stored.last_finished_at is None, (
        "a terminal cycle result survived the failure of the hook it authorises"
    )

    # HFR-271 — AND THE OTHER HALF OF "IT ROLLED BACK". Everything above reads the
    # DURABLE row through a store this test just built. That is the half that was never
    # in doubt once the write became one transaction; the half that stayed wrong is the
    # SERVICE STILL RUNNING, whose ``ManagedWatch`` mirror ``mark_cycle_result`` mutated
    # BEFORE the write it then lost. A rollback is not proven by re-reading the row.
    live = service.store.get_watch(watch.id)
    assert live is not None, "the live store dropped the watch the database still has"
    assert (live.enabled, live.retired_at, live.last_finished_at) == (
        stored.enabled,
        stored.retired_at,
        stored.last_finished_at,
    ), (
        "the transaction rolled back and the LIVE store did not: it reports "
        f"enabled={live.enabled!r} retired_at={live.retired_at!r} "
        f"last_finished_at={live.last_finished_at!r} while the durable row says "
        f"enabled={stored.enabled!r} retired_at={stored.retired_at!r} "
        f"last_finished_at={stored.last_finished_at!r}. Every in-process reader "
        "(``reconcile_watches`` decides which watches to keep running from exactly "
        "this mirror) believes a watch retired that the database never retired"
    )
    assert live.last_exit_code == stored.last_exit_code, (
        f"the live mirror kept the rolled-back exit code {live.last_exit_code!r}"
    )


#: Any full-row write of a definition. Prefix-matched on both statement shapes so a
#: column change cannot silently stop the fault from being injected.
_DEFINITION_WRITES = ("UPDATE RUN_DEFINITIONS", "INSERT INTO RUN_DEFINITIONS")


def _fail_the_definition_write(engine) -> dict:
    """Make the ``run_definitions`` write itself fail, the way a real fault would."""
    from sqlalchemy import event

    state: dict = {"fired": 0}

    def _boom(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        normalized = " ".join(statement.split()).upper()
        if not normalized.startswith(_DEFINITION_WRITES):
            return
        state["fired"] += 1
        raise RuntimeError("definition write failed: disk I/O error")

    event.listens_for(engine, "before_cursor_execute")(_boom)
    return state


#: Every guarded writer that mutates the cached ``ManagedWatch`` before persisting it.
#: The value applies the writer and returns the fields it was supposed to change.
_MIRROR_WRITERS = {
    "mark_cycle_start": lambda store, watch_id: store.mark_cycle_start(watch_id),
    "mark_cycle_result": lambda store, watch_id: store.mark_cycle_result(
        watch_id, exit_code=0, error=None, disable=True
    ),
    "set_enabled": lambda store, watch_id: store.set_enabled(watch_id, False),
    "update_watch": lambda store, watch_id: store.update_watch(
        watch_id,
        **{**_WATCH_FIXTURE_PAYLOAD, "name": "renamed", "session_id": None},
    ),
}

#: Values a round trip through ``run_definitions`` returns unchanged, so a baseline
#: mismatch cannot be mistaken for a mirror the failed write left ahead.
_WATCH_FIXTURE_PAYLOAD = {
    "name": "original",
    "session_key": "slack::channel::C1",
    "command": ["true"],
    "shell_command": None,
    "prefix": None,
    "cwd": None,
    "mode": "once",
    "timeout_seconds": 1.0,
    "lifetime_timeout_seconds": 0.0,
    "retry_exit_codes": [75],
    "retry_delay_seconds": 30.0,
    "post_to": None,
    "deliver_key": None,
}


def test_a_hook_queued_after_a_rename_names_an_agent_that_still_exists(tmp_path: Path) -> None:
    """The watch half of SCT-030 -- the completion hook must be claimable.

    ``_hook_request`` composes the hook from ``watch.agent_name`` when the cycle ends,
    and the row becomes durable inside the stamp's transaction. A rename committed
    between those two moments rewrites the definition and every ACTIVE run, but not a
    row still being composed, so the hook was inserted naming an Agent the catalog no
    longer had -- and the user is never told the watch finished, which for a ``once``
    watch is the only report it ever produces.

    The mirror reload is forced because ``PRAGMA data_version`` is timing-dependent; a
    rename the mirror has NOT absorbed is refused by the compare-and-set instead, which
    needs no fix.
    """

    from core.vibe_agents import VibeAgentStore

    store = ManagedWatchStore()
    assert store._sqlite is not None, "the shared transaction this covers lives in SQLite"
    request_store = TaskExecutionStore()
    assert request_store._sqlite is not None

    agent_store = VibeAgentStore(paths.get_sqlite_state_path())
    try:
        # A user Agent: a built-in one cannot be renamed at all.
        agent_store.create(name="ops", backend="claude")
    finally:
        agent_store.close()

    watch = store.add_watch(**{**_WATCH_FIXTURE_PAYLOAD, "agent_name": "ops"})
    hook = request_store.build_hook_send(
        session_key=watch.session_key,
        prompt="the watch finished",
        agent_name=watch.agent_name,
        run_type="watch",
        definition_id=watch.id,
        source_kind="watch",
    )

    agent_store = VibeAgentStore(paths.get_sqlite_state_path())
    try:
        renamed = agent_store.rename("ops", "night-shift")
    finally:
        agent_store.close()
    store.load()
    assert store.get_watch(watch.id).agent_name == "night-shift"

    assert store.mark_cycle_result(
        watch.id,
        exit_code=0,
        error=None,
        event_detected=True,
        disable=True,
        queued_run=hook.to_dict(),
    ) is True

    rows = [row for row in request_store._sqlite.list_runs() if row["id"] == hook.id]
    assert len(rows) == 1, f"the hook did not become durable: {rows!r}"
    assert rows[0]["agent_name"] == "night-shift", (
        "the hook was queued against the pre-rename name, which resolves to no Agent "
        f"at claim time: {rows[0]['agent_name']!r}"
    )
    assert rows[0]["agent_id"] == renamed.id, (
        f"the hook was not pinned to the Agent's durable identity: {rows[0]['agent_id']!r}"
    )


@pytest.mark.parametrize("writer", list(_MIRROR_WRITERS))
def test_a_definition_write_that_raises_leaves_no_live_mirror_ahead_of_the_database(
    tmp_path: Path, writer: str
) -> None:
    """HFR-271 — when the write raises, the in-process mirror must roll back with it.

    THE RULE THIS TEST EXISTS FOR. ``ManagedWatchStore`` is a WRITE-THROUGH CACHE: every
    writer below edits the cached ``ManagedWatch`` first and hands the whole row to
    ``_write_watch`` second. ``_write_watch`` already reloads when the compare-and-set
    RETURNS ``False`` — and only then. When the write RAISES, the mirror keeps edits the
    database rolled back, and the process goes on serving them: ``reconcile_watches``
    picks which watches keep running out of exactly this dict, ``vibe watch list``
    renders it, and the next guarded write derives its ``expect`` snapshot from it
    (``_read_state``), so a stale mirror also sends the NEXT compare-and-set the wrong
    expectation.

    Nothing about that is specific to the outbox fault HFR-269 found; the outbox fault is
    just the first caller that made the raising path reachable in a test. So this is
    parametrized over EVERY guarded writer that goes through the shared choke point, and
    the fault is injected at the ``run_definitions`` statement itself — a disk error, a
    locked database, anything — rather than at one caller's second write.
    """
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    watch = store.add_watch(**_WATCH_FIXTURE_PAYLOAD)

    durable_before = ManagedWatchStore().get_watch(watch.id)
    live_before = store.get_watch(watch.id)
    assert durable_before is not None and live_before is not None
    assert live_before.to_dict() == durable_before.to_dict(), (
        "the fixture starts with the mirror already disagreeing with the row, so the "
        "assertion below would pass or fail for the wrong reason"
    )
    boom = _fail_the_definition_write(store._sqlite.engine)

    with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
        _MIRROR_WRITERS[writer](store, watch.id)

    assert boom["fired"] >= 1, (
        f"{writer} never wrote a run_definitions row, so no fault was injected and this "
        "test proves nothing"
    )

    durable_after = ManagedWatchStore().get_watch(watch.id)
    assert durable_after is not None
    assert durable_after.to_dict() == durable_before.to_dict(), (
        f"{writer} committed something despite the injected fault; this test can no "
        "longer tell a rolled-back mirror from a written one"
    )

    live = store.get_watch(watch.id)
    assert live is not None, f"{writer} dropped the watch from the live store"
    assert live.to_dict() == durable_after.to_dict(), (
        f"{writer} left the live mirror ahead of the database. The transaction rolled "
        "back; the cached ManagedWatch kept the edit. Differing fields: "
        + repr(
            {
                key: (value, durable_after.to_dict().get(key))
                for key, value in live.to_dict().items()
                if durable_after.to_dict().get(key) != value
            }
        )
    )


def test_a_failed_create_leaves_no_phantom_watch_in_the_live_store() -> None:
    """HFR-275 — the create entry point must roll its mirror back as well.

    ``upsert_watch`` is the one writer that can put an id in the mirror the database has
    NEVER seen, and it inserted into ``self._watches`` before writing. It is also the
    worst place to leave a mirror ahead: the caller is told the watch could not be
    created, while ``reconcile_watches`` -- which picks what to run out of exactly this
    dict -- STARTS it, spawning its command and posting its output on every tick, with no
    durable row to stop it and nothing that will ever reload it away.
    """
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    before = {watch.id for watch in store.list_watches()}
    boom = _fail_the_definition_write(store._sqlite.engine)

    with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
        store.add_watch(**_WATCH_FIXTURE_PAYLOAD)

    assert boom["fired"] >= 1, "no run_definitions write was attempted, so nothing failed"
    phantom = {watch.id for watch in store.list_watches()} - before
    assert not phantom, (
        f"the failed create left {sorted(phantom)} in the live store: a watch the "
        "database never accepted, that reconcile_watches will start running anyway"
    )
    assert not {watch.id for watch in ManagedWatchStore().list_watches()} - before, (
        "the create committed despite the injected fault, so this test proves nothing"
    )


def test_a_failed_delete_does_not_stop_a_watch_the_database_still_has() -> None:
    """HFR-275 — the delete entry point, the same class in the safer direction.

    ``remove_watch`` drops the entry before the soft delete. The failure direction is the
    conservative one (absent reads as "gone" and stops the watch), but it is silent and
    it does NOT heal: the row is still there and UNCHANGED, so ``maybe_reload`` sees no
    external write, and the watch the user was told could not be deleted simply stops
    until the process restarts.
    """
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    watch = store.add_watch(**_WATCH_FIXTURE_PAYLOAD)
    boom = _fail_the_definition_write(store._sqlite.engine)

    with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
        store.remove_watch(watch.id)

    assert boom["fired"] >= 1, "no run_definitions write was attempted, so nothing failed"
    durable = ManagedWatchStore().get_watch(watch.id)
    assert durable is not None, (
        "the delete committed despite the injected fault, so this test proves nothing"
    )
    live = store.get_watch(watch.id)
    assert live is not None, (
        f"the failed delete dropped watch {watch.id} from the live store while the "
        "database still has it: it stops running, silently, until the process restarts"
    )
    assert live.to_dict() == durable.to_dict()


#: The list read ``ManagedWatchStore.load`` issues -- the only ``run_definitions``
#: SELECT that filters by ``definition_type`` -- so the guard's own
#: ``SELECT id ... LIMIT 1`` still runs and the write is genuinely ATTEMPTED.
_DEFINITION_LIST_READ_MARKERS = ("FROM RUN_DEFINITIONS", "DEFINITION_TYPE = ?")


def _fail_the_definition_write_and_the_reload(engine) -> dict:
    """A transient fault that takes out the guarded write AND the recovery read.

    The task store's twin helper. Flip ``state["live"]`` to ``False`` to end the fault
    WITHOUT committing anything: a commit would bump ``PRAGMA data_version`` and heal the
    mirror for a reason that has nothing to do with the fix.
    """
    from sqlalchemy import event

    state: dict = {"writes": 0, "reads": 0, "live": True}

    def _boom(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        if not state["live"]:
            return
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(("UPDATE RUN_DEFINITIONS", "INSERT INTO RUN_DEFINITIONS")):
            state["writes"] += 1
            raise RuntimeError("definition write failed: disk I/O error")
        if all(marker in normalized for marker in _DEFINITION_LIST_READ_MARKERS):
            state["reads"] += 1
            raise RuntimeError("definition read failed: disk I/O error")

    event.listens_for(engine, "before_cursor_execute")(_boom)
    return state


def test_a_dropped_watch_mirror_recovers_with_no_unrelated_commit_to_wake_it() -> None:
    """HFR-277 — the consuming end of HFR-271's own recovery path.

    THE DEFECT, and it is one WE introduced. ``_reload_after_lost_write`` drops the
    cached ``ManagedWatch`` when the guarded write fails and the immediate recovery
    ``load`` fails with it -- deliberately, because an absent watch reads as "gone" to
    ``reconcile_watches`` and stops it, where a stale one keeps running against state the
    database never had -- and promises ``maybe_reload`` restores it "as soon as the
    database is reachable again". It did not. ``maybe_reload`` asked only
    ``SqliteInvalidationProbe`` (``PRAGMA data_version``), which moves when another
    connection COMMITS; the failed write ROLLED BACK, so it never moved. Every later
    supervisor tick answered "nothing changed" and the watch stayed durably enabled in
    SQLite and invisible in-process until the service restarted.

    THE CLAUSE THAT IS THE TEST: nothing commits between the failure and the recovery.
    A witness probe on its own connection asserts data_version is unchanged at that
    instant, so a mirror that comes back did so because the store remembered it must
    reload -- not because an unrelated writer woke it up.
    """
    from storage.db import SqliteInvalidationProbe, create_sqlite_engine

    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test is about the guarded SQLite path"
    watch = store.add_watch(**_WATCH_FIXTURE_PAYLOAD)
    durable_before = ManagedWatchStore().get_watch(watch.id)
    assert durable_before is not None and durable_before.enabled, (
        "the fixture must start from a definition the database has, and has enabled"
    )

    # Settle the store's own probe on the fixture's commits first: a pending
    # data_version bump would reload the mirror below for the wrong reason.
    store.maybe_reload()
    assert store.maybe_reload() is False, "the store's probe is not settled"
    witness_engine = create_sqlite_engine(store._sqlite.db_path)
    witness = SqliteInvalidationProbe(witness_engine)
    witness.has_external_write()
    assert witness.has_external_write() is False, "the witness probe is not settled"

    fault = _fail_the_definition_write_and_the_reload(store._sqlite.engine)
    try:
        with pytest.raises(Exception):  # noqa: B017 - the fault type is the injected one's
            store.mark_cycle_start(watch.id)

        assert fault["writes"] >= 1, "no run_definitions write was attempted"
        assert fault["reads"] >= 1, (
            "the recovery reload was never attempted, so the entry was not dropped for "
            "the reason this test is about"
        )
        assert store.get_watch(watch.id) is None, (
            "the failed write did not drop the mirror entry, so there is nothing for "
            "maybe_reload to recover and this test proves nothing"
        )
        assert [item.id for item in store.list_watches()] == [], (
            "the dropped entry is still listed; the precondition is a mirror that has "
            "LOST the definition"
        )

        # The fault clears the way a transient one does: nothing is written.
        fault["live"] = False
        assert witness.has_external_write() is False, (
            "something COMMITTED between the failed write and the reload below. A "
            "data_version bump heals the mirror on its own, so this test would pass "
            "without the fix"
        )

        assert store.maybe_reload() is True, (
            "maybe_reload reported 'nothing changed' for a mirror the store itself knows "
            "is incomplete. data_version cannot see a rolled-back write, so the dropped "
            "watch stays invisible to reconcile_watches until the process restarts"
        )
        live = store.get_watch(watch.id)
        assert live is not None, (
            f"watch {watch.id} is still missing from the live store after a reload; it is "
            "enabled in SQLite and will never be reconciled again"
        )
        assert live.to_dict() == durable_before.to_dict(), (
            "the recovered entry does not match the durable row. Differing fields: "
            + repr(
                {
                    key: (value, durable_before.to_dict().get(key))
                    for key, value in live.to_dict().items()
                    if durable_before.to_dict().get(key) != value
                }
            )
        )
        assert [item.id for item in store.list_watches()] == [watch.id]
        assert store.maybe_reload() is False, (
            "the store keeps reloading unconditionally; the flag must be cleared by the "
            "reload that repaired the mirror"
        )
    finally:
        witness.close()
        witness_engine.dispose()

    durable_after = ManagedWatchStore().get_watch(watch.id)
    assert durable_after is not None and durable_after.to_dict() == durable_before.to_dict(), (
        "the durable row changed, so the failed write committed something and the "
        "recovery above was reading a different definition than the one that was dropped"
    )


def test_quiet_cycle_marker_must_be_a_line_of_its_own() -> None:
    """The marker is a protocol line, not a substring.

    A waiter that prints the marker's name while explaining the protocol — a usage
    message, a log line quoting it — would otherwise have its exit 64 read as a
    quiet cycle, and its failure notice would never reach the user.
    """
    from core.watches import _is_quiet_cycle

    def _result(text: str) -> _CycleResult:
        return _CycleResult(exit_code=NO_EVENT_EXIT_CODE, stdout="", stderr=text, timed_out=False)

    assert _is_quiet_cycle(_result(f"nothing new\n{NO_EVENT_MARKER}\n"))
    # Indentation is still a line of its own; the surrounding prose is not.
    assert _is_quiet_cycle(_result(f"  {NO_EVENT_MARKER}  "))
    assert not _is_quiet_cycle(_result(f"usage: exit 64 with '{NO_EVENT_MARKER}' to end quietly"))
    assert not _is_quiet_cycle(_result(f"{NO_EVENT_MARKER} is the marker"))


def test_quiet_cycle_summary_keeps_the_end_of_a_long_waiter_report() -> None:
    """The verdict is at the end of a waiter's report, so truncate from the front.

    A green CI waiter prints the run table first and its conclusion last. Squashing
    from the front kept the table and dropped the one line worth logging.
    """
    from core.watches import NO_EVENT_SUMMARY_LOG_LIMIT, _squash_tail

    short = "All watched workflows succeeded: lint, build"
    assert _squash_tail(f"  {short}\n", limit=NO_EVENT_SUMMARY_LOG_LIMIT) == short

    long_report = ("workflow line\n" * 500) + short
    squashed = _squash_tail(long_report, limit=NO_EVENT_SUMMARY_LOG_LIMIT)
    assert len(squashed) <= NO_EVENT_SUMMARY_LOG_LIMIT
    assert squashed.endswith(short)
    assert squashed.startswith("…")


def test_a_cycle_is_told_when_this_watch_last_had_a_report_delivered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-469: a waiter learns whether its earlier report was delivered.

    A waiter cannot observe its own delivery: its stdout reaches the supervisor only
    after the process exits, so a waiter that wants to advance its own cursors past a
    reported event has to be told. What it is told is a count of delivered reports,
    bumped by ``_commit_cycle_result`` in the same transaction as the follow-up hook --
    so a waiter that staged cursors alongside the value it saw can compare, and a
    CHANGED value means its report was queued.

    A durable count rather than a one-shot flag handed to the next cycle: it is still
    correct after a restart, and for a ``once`` watch, whose one report is followed by
    no cycle at all until the user resumes it -- where a lost flag would replay an
    event that was delivered long ago. And it must NOT move on a quiet cycle, which
    queued nothing: a waiter would then promote cursors covering a report that was
    never delivered and skip the activity for good.
    """
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test needs the SQLite-backed store"
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    session_id = _bare_watch_session_row(workdir=tmp_path, anchor="slack_C_ack")
    watch = store.add_watch(
        name="Watch PR",
        session_key="",
        session_id=session_id,
        session_policy="existing",
        command=[sys.executable, "-c", "print('event')"],
        shell_command=None,
        prefix="PR activity.",
        cwd=None,
        mode="forever",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
        metadata={"origin": "cli"},
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    service._running = True
    service._requires_service_lease = False

    # One event, one quiet cycle, then stop: the three states the flag has to
    # distinguish, in the order a forever watch meets them.
    scripted = [
        _CycleResult(exit_code=0, stdout="review_comment #501", stderr="", timed_out=False),
        _CycleResult(exit_code=NO_EVENT_EXIT_CODE, stdout="", stderr=NO_EVENT_MARKER, timed_out=False),
        _CycleResult(exit_code=NO_EVENT_EXIT_CODE, stdout="", stderr=NO_EVENT_MARKER, timed_out=False),
    ]
    told: list[str] = []

    async def _spy_cycle(watch_arg, *, timeout_seconds):  # noqa: ANN001
        told.append(_cycle_env(watch_arg)[LAST_DELIVERY_ENV])
        if len(told) >= len(scripted):
            service._running = False
        return scripted[min(len(told) - 1, len(scripted) - 1)]

    service._run_cycle = _spy_cycle  # type: ignore[method-assign]
    monkeypatch.setattr(watches_module, "WATCH_MIN_REARM_SECONDS", 0)
    monkeypatch.setattr(watches_module, "WATCH_FOLLOW_UP_POLL_SECONDS", 0.001)

    async def _run() -> None:
        task = asyncio.create_task(service._run_watch(watch.id))
        while not task.done():
            for request in request_store.list_pending():
                claimed = request_store.claim(request.id)
                if claimed is not None:
                    request_store.complete(claimed, ok=True)
            await asyncio.sleep(0.001)
        await task

    asyncio.run(_run())

    assert told[0] == "", "nothing has been reported yet"
    assert told[1], "the event was delivered, so the stamp has to have moved"
    assert told[2] == told[1], "a quiet cycle queued no hook; the stamp must not move"


def test_resuming_a_once_watch_keeps_what_it_already_delivered(tmp_path: Path) -> None:
    """A resume clears the cycle history and NOT the delivery acknowledgement.

    A ``once`` watch reports, retires, and is resumed by the user days later. That
    resume deliberately clears ``last_event_at`` -- the resumed watch begins a distinct
    cycle and its row must not claim an event it has not seen. But the waiter's staged
    cursors are still on disk beside the count they were staged against, and if that
    count resets too, the first cycle after the resume concludes its report never left
    and reports the same activity all over again.
    """
    store = ManagedWatchStore()
    assert store._sqlite is not None, "this test needs the SQLite-backed store"
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    session_id = _bare_watch_session_row(workdir=tmp_path, anchor="slack_C_resume_ack")
    watch = store.add_watch(
        name="Watch PR once",
        session_key="",
        session_id=session_id,
        session_policy="existing",
        command=[sys.executable, "-c", "print('event')"],
        shell_command=None,
        prefix="PR activity.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
        metadata={"origin": "cli"},
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    service._running = True
    service._requires_service_lease = False

    async def _one_event(watch_arg, *, timeout_seconds):  # noqa: ANN001
        return _CycleResult(exit_code=0, stdout="review_comment #501", stderr="", timed_out=False)

    service._run_cycle = _one_event  # type: ignore[method-assign]
    asyncio.run(service._run_watch(watch.id))

    reported = store.get_watch(watch.id)
    assert reported is not None and not reported.enabled, "a once watch retires on its report"
    delivered = _cycle_env(reported)[LAST_DELIVERY_ENV]
    assert delivered == "1"

    resumed = store.set_enabled(watch.id, True)
    assert resumed.last_event_at is None, "the resume clears the cycle history it owns"
    assert _cycle_env(resumed)[LAST_DELIVERY_ENV] == delivered
    assert resumed.metadata["origin"] == "cli", "the ack rides along with the rest of the metadata"


def test_the_cycle_environment_carries_the_watch_and_its_delivery_count() -> None:
    """Both facts reach the waiter as environment variables, empty for never.

    Metadata a writer never touched, or left as something other than a positive count,
    reads as "nothing delivered yet" -- the direction that reports a staged event twice
    rather than advancing past one that never left.
    """
    assert _cycle_env(SimpleNamespace(id="wat_9", metadata={})) == {
        WATCH_ID_ENV: "wat_9",
        LAST_DELIVERY_ENV: "",
    }
    assert _cycle_env(SimpleNamespace(id="wat_9", metadata={DELIVERY_ACK_METADATA_KEY: 3})) == {
        WATCH_ID_ENV: "wat_9",
        LAST_DELIVERY_ENV: "3",
    }
    for broken in ("3", True, 0, -1, None, {"count": 3}):
        assert _cycle_env(SimpleNamespace(id="wat_9", metadata={DELIVERY_ACK_METADATA_KEY: broken})) == {
            WATCH_ID_ENV: "wat_9",
            LAST_DELIVERY_ENV: "",
        }


def test_managed_watch_service_names_the_watch_in_the_cycle_environment(tmp_path: Path) -> None:
    """A waiter can tell which watch it is a cycle of.

    Waiters that keep per-watch state on disk own that state by this id, so two
    identically configured watches cannot silently share one state file.
    """
    store = ManagedWatchStore(tmp_path / "watches.json")
    request_store = TaskExecutionStore(tmp_path / "task_requests")
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch_runtime.json")
    echoed = tmp_path / "watch-id.txt"
    watch = store.add_watch(
        name="Identity echo waiter",
        session_key="slack::channel::C123",
        command=[
            sys.executable,
            "-c",
            (
                "import os, pathlib; "
                f"pathlib.Path({str(echoed)!r}).write_text(os.environ.get('AVIBE_WATCH_ID', ''))"
            ),
        ],
        shell_command=None,
        prefix="Something happened.",
        cwd=None,
        mode="once",
        timeout_seconds=5,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.01,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=request_store,
        runtime_store=runtime_store,
    )

    async def _run() -> None:
        service.start()
        for _ in range(100):
            if echoed.exists() and echoed.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(_run())

    assert echoed.read_text(encoding="utf-8") == watch.id

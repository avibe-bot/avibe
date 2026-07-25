from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from core.process_isolation import (
    PersistedProcessIdentity,
    ProcessIdentity,
    fingerprint_process_marker,
)
from core.scheduled_tasks import TaskExecutionStore
from core.watches import ManagedWatchService, ManagedWatchStore, WatchRuntimeStateStore, _CycleResult
from storage.background import SQLiteBackgroundTaskStore

TEST_MARKER = "test-watch-worker"
TEST_FINGERPRINT = fingerprint_process_marker(TEST_MARKER)


class _FakeProcess:
    pid = 1234
    returncode = 0

    async def communicate(self):
        return b"ok\n", b""


async def _start_watch_service(service: ManagedWatchService) -> None:
    service.start()
    startup_task = service._startup_task
    if startup_task is not None:
        await startup_task


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


def test_managed_watch_exec_detaches_waiter_stdin(tmp_path: Path, monkeypatch) -> None:
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
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(service._run_cycle(watch, timeout_seconds=5))

    assert result.exit_code == 0
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["cwd"] == str(paths.get_vibe_remote_dir())


def test_managed_watch_shell_detaches_waiter_stdin(tmp_path: Path, monkeypatch) -> None:
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

    async def fake_create_subprocess_shell(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    result = asyncio.run(service._run_cycle(watch, timeout_seconds=5))

    assert result.exit_code == 0
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    assert captured["kwargs"]["cwd"] == str(paths.get_vibe_remote_dir())


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
        await asyncio.sleep(0.2)
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
        await asyncio.sleep(0.08)
        await service.stop()

    asyncio.run(_run())

    saved = store.get_watch(watch.id)
    assert saved is not None
    assert saved.enabled is True
    assert saved.last_exit_code == 75
    assert request_store.list_pending() == []


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
        await asyncio.sleep(0.12)
        assert watch.id in service._fused_watch_ids
        await asyncio.sleep(0.08)
        await service.stop()

    asyncio.run(_run())

    assert store.starts == 1
    assert service._store_error_fused is True
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
    monkeypatch.setattr(service, "_reap_stale_workers", lambda: time.sleep(0.15))
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

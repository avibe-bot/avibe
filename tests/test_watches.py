from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scheduled_tasks import TaskExecutionStore
from core.watches import ManagedWatchService, ManagedWatchStore, WatchRuntimeStateStore
from storage.background import SQLiteBackgroundTaskStore


class _FakeProcess:
    pid = 1234
    returncode = 0

    async def communicate(self):
        return b"ok\n", b""


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


def test_managed_watch_store_uses_sqlite_when_path_is_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(tmp_path))
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
        service.start()
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
        service.start()
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
        service.start()
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
        service.start()
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


def test_managed_watch_service_records_wall_clock_started_at(tmp_path: Path) -> None:
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

    async def _run() -> str:
        service.start()
        for _ in range(100):
            started_at = runtime_store.load().get("watches", {}).get(watch.id, {}).get("started_at")
            if started_at:
                await service.stop()
                return started_at
            await asyncio.sleep(0.02)
        await service.stop()
        raise AssertionError("started_at was never written")

    started_at = asyncio.run(_run())
    assert datetime.fromisoformat(started_at).year >= 2024


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
        service.start()
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
        service.start()
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
        service.start()
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
        service.start()
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
        service.start()
        assert service._store_error_fused is False
        assert service._store_reconcile_failures == 1
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
        service.start()
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

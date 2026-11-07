"""Hermetic end-to-end Watch/Task cache benchmark; run from a source checkout.

No live service or user state is used. A private IPC server runs the real Watch
supervisor, and the measured client is a fresh CLI process.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="avibe-definition-bench-") as root:
        root = Path(root)
        # Do this before importing Avibe; children inherit the same private home.
        for key in tuple(os.environ):
            if key.startswith(("AVIBE_", "VIBE_", "CODEX_", "CLAUDE_")):
                os.environ.pop(key)
        for key, value in {
            "HOME": str(root), "AVIBE_HOME": str(root / "avibe"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
        }.items():
            os.environ[key] = value
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        asyncio.run(measure(root, args.samples))


async def measure(root, samples):
    from config import paths
    from core import internal_server
    from core.runtime_work import RuntimeWorkSupervisor
    from core.scheduled_tasks import ScheduledTaskStore, TaskExecutionStore
    from core.vibe_agents import VibeAgentStore
    from core.watches import ManagedWatchService, ManagedWatchStore, WatchRuntimeStateStore
    from storage.background import SQLiteBackgroundTaskStore
    from storage.importer import ensure_sqlite_state
    from storage.models import run_definitions
    import sqlite3
    import uvicorn

    ensure_sqlite_state()
    db = SQLiteBackgroundTaskStore()
    stamp = "2026-11-01T00:00:00+00:00"
    rows = []
    for kind, count, retired_count in (("watch", 3800, 3500), ("scheduled", 4000, 3500)):
        for index in range(count):
            retired = index < retired_count
            payload = {
                "id": f"bench-{kind}-{index}", "name": f"Definition {index}",
                "enabled": False, "mode": "once", "session_policy": "create_per_run",
                "schedule_type": "at" if retired else "cron", "cron": "0 * * * *",
                "run_at": stamp, "timezone": "UTC", "shell_command": "true",
                "created_at": stamp, "updated_at": stamp,
                "retired_at": stamp if retired else None,
                "last_finished_at": stamp if retired else None,
                "last_exit_code": 0 if retired else None,
                "metadata": {"benchmark": "x" * 1024},
            }
            rows.append(db._watch_values(payload) if kind == "watch" else db._scheduled_task_values(payload))
    with db.engine.begin() as conn:
        # Each kind has an identical set of columns, but the two shapes differ.
        conn.execute(run_definitions.insert(), rows[:3800])
        conn.execute(run_definitions.insert(), rows[3800:])
    agents = VibeAgentStore()
    agents.create(name="bench-worker", backend="codex")
    tasks, watches, requests, runtime = ScheduledTaskStore(), ManagedWatchStore(), TaskExecutionStore(), WatchRuntimeStateStore()
    supervisor = RuntimeWorkSupervisor()
    owner = SimpleNamespace(
        runtime_work_supervisor=supervisor,
        vibe_agent_store=agents,
        scheduled_task_service=SimpleNamespace(store=tasks, request_store=requests),
        session_turns=None,
    )
    service = ManagedWatchService(owner, store=watches, request_store=requests, runtime_store=runtime)
    owner.watch_service = service
    output = {"definitions": {"watch": 3800, "watch_retired": 3500, "task": 4000, "task_retired": 3500}}
    for label, store in (("watch_reload", watches), ("task_reload", tasks)):
        elapsed = []
        for _ in range(samples):
            start = time.perf_counter()
            store.load()
            elapsed.append(time.perf_counter() - start)
        output[label] = {"seconds": elapsed, "median": statistics.median(elapsed)}
    output["resident"] = {"watch": len(watches.list_watches()), "task": len(tasks.list_tasks())}

    reload_count = [0]
    reload_watches = watches.load

    def count_reload():
        reload_count[0] += 1
        reload_watches()

    watches.load = count_reload
    socket_path = root / "control.sock"
    os.environ["VIBE_INTERNAL_DISPATCH_SOCKET"] = str(socket_path)
    app = internal_server.create_app(owner)
    listener, _ = internal_server._bind_socket(socket_path)
    server = internal_server._create_controller_loop_server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.01)
    await supervisor.activate()
    service.start()
    stop = threading.Event()
    writes = [0]

    def busy_writer():
        conn = sqlite3.connect(paths.get_sqlite_state_path())
        try:
            while not stop.wait(0.02):
                conn.execute("UPDATE run_definitions SET updated_at=? WHERE id='bench-scheduled-3999'", (str(time.time()),))
                conn.commit()
                writes[0] += 1
        finally:
            conn.close()

    writer = threading.Thread(target=busy_writer)
    writer.start()
    try:
        timings, cpu_times, reloads = [], [], []
        for _ in range(samples):
            start = time.perf_counter()
            cpu_start = time.process_time()
            reload_start = reload_count[0]
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "from vibe.cli import main; main()", "watch", "add", "--name", "benchmark startup",
                "--create-session-per-run", "--agent", "bench-worker", "--cwd", str(root),
                "--shell", "sleep 1; printf 'avibe-watch: no-event\\n' >&2; exit 64",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            elapsed = time.perf_counter() - start
            if proc.returncode:
                raise RuntimeError(stderr.decode())
            result = json.loads(stdout)
            timings.append(elapsed)
            cpu_times.append(time.process_time() - cpu_start)
            reloads.append(reload_count[0] - reload_start)
            assert result["ok"] and result["definition"]["id"], result
            # A slow first worker may be confirmed stably running before it
            # returns no-event. Let that owned cycle finish before the next sample.
            while service._active_tasks:
                await asyncio.sleep(0.01)
        output["watch_add_e2e"] = {
            "seconds": timings, "median": statistics.median(timings),
            "service_cpu_seconds": cpu_times, "watch_mirror_reloads": reloads,
        }
        output["external_commits"] = writes[0]
        print(json.dumps(output, indent=2))
    finally:
        stop.set()
        await asyncio.to_thread(writer.join)
        await service.stop()
        await supervisor.stop()
        server.should_exit = True
        await serving
        listener.close()
        await owner.skill_observability.close()
        if hasattr(owner, "cli_control_stores"):
            owner.cli_control_stores.close()
        agents.close()
        for backend in (db, tasks._sqlite, watches._sqlite, requests.sqlite_backend, runtime._sqlite):
            backend.close()


if __name__ == "__main__":
    main()

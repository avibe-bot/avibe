"""A process's start token: the one identity of a running process, which no clock adjustment moves.

Every record that names a process by pid -- the service lock, Claude and OpenCode processes, restart
jobs, desktop installer owners -- must keep naming the same execution after the wall clock or the boot
time moves, and must never name a process that later reused the pid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from vibe import runtime


@contextmanager
def _clock_adjusted():
    """psutil's own view of the platform after the clock moved: its wall-clock start times shift."""

    if sys.platform == "darwin":
        from psutil import _psosx

        # kern.boottime crossed a whole second, as routine time-daemon adjustments make it.
        with patch.object(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME - 1):
            yield 1.0
    elif sys.platform.startswith("linux"):
        from psutil import _pslinux

        real_boot_time = _pslinux.boot_time
        # The wall clock was stepped, which moves the boot time /proc/stat reports.
        with patch.object(_pslinux, "boot_time", lambda: real_boot_time() - 21.0):
            yield -21.0
    else:
        pytest.skip("Windows stores a process's creation time and psutil reads it back unchanged")


@pytest.fixture
def live_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process
    finally:
        process.kill()
        process.wait(10)


def test_a_clock_adjustment_moves_the_wall_clock_start_but_not_the_start_token(live_process) -> None:
    pid = live_process.pid
    token = runtime.process_start_token(pid)
    created = runtime.process_create_time(pid)
    assert token is not None and created is not None

    with _clock_adjusted() as shift:
        # The adjustment is real for psutil: the wall-clock start it reports moved.
        assert runtime.process_create_time(pid) == pytest.approx(created + shift)
        assert runtime.process_start_token(pid) == token


def test_every_recorded_process_identity_survives_a_clock_adjustment(live_process) -> None:
    from modules.agents import claude_process_reaper
    from modules.agents.opencode.server import OpenCodeGeneration
    from vibe import desktop_backends

    pid = live_process.pid
    token = runtime.process_start_token(pid)
    # Recorded, not this process's child: only its token proves it.
    opencode = OpenCodeGeneration(generation_id="ocg_live", pid=pid, port=50100, spec_digest="spec", process_start_token=token)

    with _clock_adjusted():
        assert claude_process_reaper._recorded_start_matches(pid, token, None)
        assert opencode.process_alive()
        assert desktop_backends._installer_owner_gone(f"{pid}:{token}", None) is False
        assert runtime.process_start_token(pid) == token


def test_a_reused_pid_is_not_the_recorded_process(live_process) -> None:
    from modules.agents import claude_process_reaper

    assert not claude_process_reaper._recorded_start_matches(live_process.pid, "an-earlier-process", None)


@pytest.mark.parametrize("platform", ["freebsd14", "openbsd7", "sunos5"])
def test_a_platform_without_a_stable_start_gives_no_process_a_token(live_process, monkeypatch, platform) -> None:
    """There psutil's start moves with system clock updates: no exact token, so every record is unverifiable."""

    monkeypatch.setattr(sys, "platform", platform)

    assert runtime.process_start_token(live_process.pid) is None


def _released_started_at(pid: int) -> float:
    """The start a released build recorded for a Claude process.

    It read ``ps -o lstart``, which shows the start the kernel reports to the whole second.
    """

    started = runtime.process_start_time(pid)
    assert started is not None
    return float(int(started))


def _act_on_released_claude_record(pid: int, started_at: float, runtime_dir, monkeypatch) -> tuple[set, set, dict]:
    """Reap, report and end ``pid`` as a released build registered it: with ``started_at`` and no token.

    It is not the service's child, and its session is tracked under another process, so it is a leak.
    Returns the pids the reaper signals, the pids reported as orphans, and the end result.
    """

    from core.services import running_agents
    from modules.agents import claude_process_reaper as reaper

    (runtime_dir / reaper.CLAUDE_PROCESS_REGISTRY_NAME).write_text(
        json.dumps(
            {"processes": [{"pid": pid, "native_session_id": "sess-released", "owner": reaper.AVIBE_CLAUDE_SESSION_OWNER, "started_at": started_at}]}
        )
    )
    # A later registration rewrites the registry; the released record keeps its start.
    newcomer = SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(pid=os.getpid())))
    reaper.register_claude_owned_process(newcomer, native_session_id="sess-new", runtime_dir=runtime_dir)
    registry = reaper._load_owned_process_registry(runtime_dir)
    assert [(record.start_token, record.started_at) for record in registry if record.pid == pid] == [(None, started_at)]

    table = "\n".join(
        [
            f"{os.getpid()} 1 python service_main.py",
            f"{pid} 1 /usr/local/bin/claude --output-format stream-json --resume sess-released --input-format stream-json",
        ]
    )
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda runtime_dir=None: registry)
    monkeypatch.setattr(reaper, "_run_ps", lambda: table)
    monkeypatch.setattr(reaper, "_process_ages", lambda pids: {candidate: 999.0 for candidate in pids})
    signalled: list[set[int]] = []

    async def reap_pid_set(target_pids, *, terminate_timeout, logger):
        signalled.append(set(target_pids))
        return len(target_pids)

    monkeypatch.setattr(reaper, "_reap_pid_set", reap_pid_set)

    asyncio.run(
        reaper.reap_orphaned_claude_processes(
            owned_pids=set(), tracked_resume_ids={"sess-released": 4242}, logger=logging.getLogger("test.released")
        )
    )
    reaped = set().union(*signalled)
    reported = {row["pid"] for row in running_agents._collect_orphans({}, set())}
    return reaped, reported, asyncio.run(running_agents._end_orphan_pid(pid))


@pytest.mark.skipif(os.name == "nt", reason="the Claude process registry is POSIX-only")
def test_a_released_claude_record_still_names_its_process(live_process, tmp_path, monkeypatch) -> None:
    pid = live_process.pid

    reaped, reported, ended = _act_on_released_claude_record(pid, _released_started_at(pid), tmp_path, monkeypatch)

    assert pid in reaped
    assert pid in reported
    assert ended["ok"] is True and ended["action"] == "killed_process"


@pytest.mark.skipif(os.name == "nt", reason="the Claude process registry is POSIX-only")
def test_a_released_claude_record_whose_pid_was_reused_names_nothing(live_process, tmp_path, monkeypatch) -> None:
    pid = live_process.pid
    # The process the released build registered under this pid started an hour before the live one.
    reaped, reported, ended = _act_on_released_claude_record(pid, _released_started_at(pid) - 3600, tmp_path, monkeypatch)

    assert pid not in reaped
    assert pid not in reported
    assert ended == {"ok": False, "error": "pid_reused", "pid": pid}


def _restart_job_reader(name: str, monkeypatch):
    """One of the readers that decide whether a recorded restart job still runs."""

    if name == "web-restart":
        from vibe import ui_server

        return ui_server._restart_in_flight
    if name == "upgrade":
        from vibe import upgrade

        return upgrade.restart_is_pending
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "incus_regression_supervisor.py"
    spec = importlib.util.spec_from_file_location("incus_regression_supervisor", script)
    assert spec and spec.loader
    supervisor = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, supervisor)
    spec.loader.exec_module(supervisor)
    return supervisor._restart_in_progress


@pytest.mark.parametrize("reader", ["web-restart", "upgrade", "incus-supervisor"])
@pytest.mark.parametrize("job", ["its-job", "reused-pid"])
def test_a_restart_job_a_released_build_recorded_is_judged_by_its_create_time(
    live_process, tmp_path, monkeypatch, reader, job
) -> None:
    from config import paths

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    created = runtime.process_create_time(live_process.pid)
    assert created is not None
    # The process the released job ran as had this pid, if it is not the live one, an hour earlier.
    recorded = created if job == "its-job" else created - 3600
    runtime.write_json(
        runtime.get_restart_status_path(),
        {"ok": None, "state": "running", "supervisor_pid": live_process.pid, "supervisor_started_at": recorded},
    )

    assert _restart_job_reader(reader, monkeypatch)() is (job == "its-job")


@pytest.mark.skipif(sys.platform != "darwin", reason="the unadjusted start time is read through psutil on macOS")
def test_psutil_still_offers_the_unadjusted_start_time_its_identity_uses() -> None:
    """``create_time(monotonic=True)`` is private to psutil; a release without it must fail here, not in the field."""


    import psutil

    assert isinstance(psutil.Process(os.getpid())._proc.create_time(monotonic=True), float)

"""``LocalJobHost`` on Windows, where a job object holds the tree (``recovery.md`` J1-J6, T2).

Python is the shell here (``python -c``), so a command means the same on every Windows; the shell
the ``bash`` tool runs on Windows brings its own tests. A "crash" or "restart" is a new
``LocalJobHost`` over the same directory, or a host process that is terminated outright.

Every test ends the jobs it started: ``_end_every_job`` terminates each job object left under the
test's jobs directory, whatever the test did.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import pytest

import core.agent_core.tools.jobs as jobs_module
from core.agent_core.tools import windows_job
from core.agent_core.cancel import CancelToken
from core.agent_core.tools.base import ToolContext
from core.agent_core.tools.bash import BashTool, settle_bash_call
from core.agent_core.tools.jobs import JobStartError, LocalJobHost
from tests.agent_core.tools.conftest import instance, result_text

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the Windows job host")

REPO = Path(__file__).resolve().parents[3]


class Crash(Exception):
    pass


@pytest.fixture(autouse=True)
def _end_every_job(tmp_path):
    yield
    jobs_dir = tmp_path / "jobs"
    for entry in jobs_dir.iterdir() if jobs_dir.is_dir() else ():
        job = windows_job.open_job(f"avibe-job-{entry.name}")
        if job is not None:
            try:
                windows_job.terminate_job(job)
            finally:
                windows_job.close_handle(job)


@pytest.fixture
def make_ctx(tmp_path):
    """The tools' context with the whole environment: Python on Windows needs more of it than ``PATH``."""

    def make(*, tool_call_id="toolu_1"):
        return ToolContext(
            session_id="ses_test",
            tool_call_id=tool_call_id,
            call=instance(tool_call_id),
            cwd=str(tmp_path),
            env=dict(os.environ),
            cancel=CancelToken(),
            on_progress=None,
        )

    return make


def _host(tmp_path, **kwargs):
    return LocalJobHost(str(tmp_path / "jobs"), shell=sys.executable, **kwargs)


async def _start(host, tmp_path, script, *, tool_call_id="toolu_1", response_seq=1, timeout_s=None):
    return await host.start(
        script,
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_s=timeout_s,
        call=instance(tool_call_id, response_seq=response_seq),
    )


def _wait_until(predicate, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _gone(pid):
    return not psutil.pid_exists(pid)


def _wrapper_pid(host, job_id):
    return int(open(os.path.join(host.job_dir(job_id), "pid")).read())


def _pid_written(path):
    assert _wait_until(lambda: path.exists() and path.read_text().strip())
    return int(path.read_text())


def _tree(tmp_path, *, then="import time; time.sleep(60)"):
    """A command that records its pid, starts a detached grandchild that records its own, then runs ``then``.

    ``DETACHED_PROCESS`` is how ``Start-Process`` and services leave a console; it does not leave a job.
    """
    grandchild = f"import os, time; open(r'{tmp_path / 'grandchild.pid'}', 'w').write(str(os.getpid())); time.sleep(60)"
    return (
        "import os, subprocess, sys\n"
        f"open(r'{tmp_path / 'shell.pid'}', 'w').write(str(os.getpid()))\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], creationflags=subprocess.DETACHED_PROCESS)\n"
        f"{then}\n"
    )


def _tree_pids(tmp_path):
    return _pid_written(tmp_path / "shell.pid"), _pid_written(tmp_path / "grandchild.pid")


def _counter(tmp_path):
    path = tmp_path / "counter"
    return len(path.read_text().splitlines()) if path.exists() else 0


_COUNT = "open(r'{path}', 'a').write('ran\\n')"


def _crash_on_meta_write(monkeypatch, number):
    original = LocalJobHost._write_meta
    calls = []

    def write(self, job_id, meta):
        original(self, job_id, meta)
        calls.append(job_id)
        if len(calls) == number:
            raise Crash

    monkeypatch.setattr(LocalJobHost, "_write_meta", write)


async def test_a_command_runs_in_its_own_job_object_and_its_output_is_kept_byte_for_byte(tmp_path):
    host = _host(tmp_path)
    job_id = await _start(
        host,
        tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write(b'a\\nb\\r\\n\\x1a\\nend\\n'); sys.stdout.flush()\n"
        f"open(r'{tmp_path / 'member'}', 'w').write('started')\n"
        "import time; time.sleep(60)",
    )
    assert _wait_until(lambda: (tmp_path / "member").exists() and host.output(job_id)[1] == 11)
    job = windows_job.open_job(f"avibe-job-{job_id}")
    try:
        assert windows_job.job_contains(job, _wrapper_pid(host, job_id))
        assert windows_job.active_processes(job) == 2  # the wrapper and its shell
    finally:
        windows_job.close_handle(job)
    await host.kill(job_id)

    # The log is binary: no CRLF added, and 0x1A (Ctrl-Z) is not the end.
    assert host.output(job_id) == (b"a\nb\r\n\x1a\nend\n", 11)
    meta = host.meta(job_id)
    assert meta["process"]["pgid"] == meta["process"]["pid"] == _wrapper_pid(host, job_id)


async def test_an_exit_code_is_reported_in_full(tmp_path):
    host = _host(tmp_path)
    job_id = await _start(host, tmp_path, "import sys; sys.exit(7)")

    assert await host.wait(job_id, deadline_s=10) == jobs_module.JobStatus("exited", 7)


@pytest.mark.parametrize(
    "crash_point",
    ["after_meta_before_spawn", "after_spawn_before_pid", "after_identity_before_go", "after_go"],
)
async def test_a_crash_at_any_launch_step_runs_the_command_at_most_once(tmp_path, monkeypatch, crash_point):
    """J1."""
    host = _host(tmp_path)
    if crash_point == "after_meta_before_spawn":
        _crash_on_meta_write(monkeypatch, 1)
    elif crash_point == "after_spawn_before_pid":

        async def crash(self, *args):
            raise Crash

        monkeypatch.setattr(LocalJobHost, "_await_pid", crash)
    elif crash_point == "after_identity_before_go":
        _crash_on_meta_write(monkeypatch, 2)

    with pytest.raises(Crash) if crash_point != "after_go" else contextlib.nullcontext():
        await _start(host, tmp_path, _COUNT.format(path=tmp_path / "counter"))
    monkeypatch.undo()

    recovery = _host(tmp_path)
    job_id = recovery.find_job(instance())
    may_have_started = recovery.started(job_id)
    if crash_point != "after_meta_before_spawn":
        pid_file = Path(recovery.job_dir(job_id)) / "pid"
        assert _wait_until(lambda: not pid_file.exists() or _gone(int(pid_file.read_text())))

    assert may_have_started is (crash_point == "after_go")
    assert _counter(tmp_path) == (1 if may_have_started else 0)
    assert recovery.status(job_id).state == ("exited" if may_have_started else "gone")


async def test_a_wrapper_left_without_a_decision_abandons_and_never_runs_the_command(tmp_path, monkeypatch):
    """J1: the wrapper decides ``abandon`` itself when no decision comes in time."""
    monkeypatch.setattr(jobs_module, "DECISION_TIMEOUT_S", 0.05)
    original = LocalJobHost._await_pid

    async def slow_host(self, *args):
        identity = await original(self, *args)
        await asyncio.sleep(0.5)
        return identity

    monkeypatch.setattr(LocalJobHost, "_await_pid", slow_host)
    host = _host(tmp_path)

    with pytest.raises(JobStartError):
        await _start(host, tmp_path, _COUNT.format(path=tmp_path / "counter"))

    job_id = host.find_job(instance())
    assert _wait_until(lambda: _gone(_wrapper_pid(host, job_id)))
    assert not host.started(job_id)
    assert _counter(tmp_path) == 0


async def test_a_recorded_pid_outside_the_job_is_neither_reported_nor_killed(tmp_path):
    """J2: the pid on record now belongs to a process that is not in the job's job object."""
    host = _host(tmp_path)
    job_id = await _start(host, tmp_path, "import time; time.sleep(60)")
    await host.kill(job_id)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        meta = host.meta(job_id)
        meta["process"].update(pid=stranger.pid, pgid=stranger.pid)
        host._write_meta(job_id, meta)

        recovery = _host(tmp_path)
        assert recovery.status(job_id).state == "gone"
        await recovery.kill(job_id)

        time.sleep(0.2)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


async def test_kill_ends_the_whole_tree_and_no_process_can_leave_the_job(tmp_path):
    host = _host(tmp_path)
    breakaway = (
        "try:\n"
        "    subprocess.Popen([sys.executable, '-c', 'pass'], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)\n"
        f"    open(r'{tmp_path / 'breakaway'}', 'w').write('escaped')\n"
        "except OSError:\n"
        f"    open(r'{tmp_path / 'breakaway'}', 'w').write('refused')\n"
        "import time; time.sleep(60)"
    )
    job_id = await _start(host, tmp_path, _tree(tmp_path, then=breakaway))
    shell, grandchild = _tree_pids(tmp_path)
    assert _wait_until((tmp_path / "breakaway").exists)

    await host.kill(job_id, reason="aborted")

    assert _wait_until(lambda: _gone(shell) and _gone(grandchild) and _gone(_wrapper_pid(host, job_id)))
    assert (tmp_path / "breakaway").read_text() == "refused"
    assert host.status(job_id).state == "gone"
    assert host.stop_reason(job_id) == "aborted"


async def test_the_wrapper_enforces_the_deadline_while_no_host_runs(tmp_path):
    """J3: the host is gone right after the start; the deadline still ends the tree and is recorded."""
    host = _host(tmp_path)
    job_id = await _start(host, tmp_path, _tree(tmp_path), timeout_s=3)
    wrapper = _wrapper_pid(host, job_id)
    shell, grandchild = _tree_pids(tmp_path)
    del host

    assert _wait_until(lambda: _gone(wrapper), timeout_s=15)
    restarted = _host(tmp_path)
    assert restarted.status(job_id).state == "gone"
    assert restarted.stop_reason(job_id) == "timeout"
    assert _wait_until(lambda: _gone(shell) and _gone(grandchild))


async def test_terminating_the_wrapper_ends_its_tree_and_records_no_reason(tmp_path):
    """``TerminateProcess`` cannot be caught: the job object ends the tree, and T2 settles the call as interrupted."""
    host = _host(tmp_path)
    job_id = await _start(host, tmp_path, _tree(tmp_path))
    shell, grandchild = _tree_pids(tmp_path)

    psutil.Process(_wrapper_pid(host, job_id)).kill()  # TerminateProcess

    assert _wait_until(lambda: _gone(shell) and _gone(grandchild))
    recovery = _host(tmp_path)
    assert recovery.status(job_id).state == "gone"
    assert recovery.stop_reason(job_id) is None
    assert await settle_bash_call(recovery, instance()) is None  # the caller commits the interrupted result


async def test_a_job_outlives_the_host_process_that_started_it(tmp_path):
    """A restart: the service process is terminated outright, and its job keeps running for the next host."""
    script = (
        "import asyncio, os, sys\n"
        "from core.agent_core.tools.base import CallInstance\n"
        "from core.agent_core.tools.jobs import LocalJobHost\n"
        "async def main():\n"
        f"    host = LocalJobHost(r'{tmp_path / 'jobs'}', shell=sys.executable)\n"
        f"    job_id = await host.start({_tree(tmp_path)!r}, cwd=r'{tmp_path}', env=dict(os.environ),"
        " timeout_s=None, call=CallInstance('ses_test', 'msg_1', 1, 'toolu_1'))\n"
        "    print(job_id, flush=True)\n"
        "    await asyncio.sleep(60)\n"
        "asyncio.run(main())\n"
    )
    service = subprocess.Popen([sys.executable, "-c", script], cwd=REPO, stdout=subprocess.PIPE, text=True)
    try:
        job_id = (await asyncio.wait_for(asyncio.to_thread(service.stdout.readline), 60)).strip()
        shell, grandchild = _tree_pids(tmp_path)
        service.kill()  # TerminateProcess: no cleanup runs
        service.wait()
    finally:
        if service.poll() is None:
            service.kill()
            service.wait()

    restarted = _host(tmp_path)
    assert restarted.status(job_id).state == "running"
    assert not _gone(shell) and not _gone(grandchild)
    await restarted.kill(job_id)
    assert _wait_until(lambda: _gone(shell) and _gone(grandchild))
    assert restarted.stop_reason(job_id) == "killed"


async def test_a_reader_holding_the_logs_open_never_stops_the_command(tmp_path, monkeypatch):
    """J4: the wrapper replaces tail.log a few times a second; a host reading it must not make that fail."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 1000)
    monkeypatch.setattr(jobs_module, "OUTPUT_TAIL_BYTES", 2000)
    host = _host(tmp_path)
    job_id = await _start(
        host,
        tmp_path,
        "import sys, time\n"
        "for i in range(1, 3001):\n"
        "    sys.stdout.buffer.write(b'%d\\n' % i)\n"
        "    if i % 100 == 0:\n"
        "        sys.stdout.flush(); time.sleep(0.05)\n",
    )
    tail = os.path.join(host.job_dir(job_id), "tail.log")
    assert _wait_until(lambda: os.path.exists(tail))
    held = jobs_module._open_for_reading(tail, binary=True)
    try:
        offset, seen = 0, []
        while host.status(job_id).state == "running":
            data, offset = host.output(job_id, offset)
            seen.append(data)
            await asyncio.sleep(0.01)
        status = await host.wait(job_id, deadline_s=10)
    finally:
        held.close()

    assert status == jobs_module.JobStatus("exited", 0)
    assert host.stop_reason(job_id) is None
    assert os.path.getsize(host.output_path(job_id)) == 1000
    while True:
        data, offset = host.output(job_id, offset)
        if not data:
            break
        seen.append(data)
    total = sum(len(b"%d\n" % i) for i in range(1, 3001))
    assert offset == total
    assert b"".join(seen).endswith(b"2999\n3000\n")


async def test_a_program_holding_tail_log_open_only_delays_its_refresh(tmp_path, monkeypatch):
    """Python's ``open`` does not share delete, so no replace can land while it holds tail.log: the wrapper
    keeps the tail and writes it at the next snapshot, instead of stopping the command."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 1000)
    host = _host(tmp_path)
    job_id = await _start(
        host,
        tmp_path,
        "import sys, time\n"
        "for i in range(1, 3001):\n"
        "    sys.stdout.buffer.write(b'%d\\n' % i)\n"
        "    if i % 100 == 0:\n"
        "        sys.stdout.flush(); time.sleep(0.05)\n",
    )
    tail = os.path.join(host.job_dir(job_id), "tail.log")
    assert _wait_until(lambda: os.path.exists(tail))
    with open(tail, "rb"):
        await asyncio.sleep(0.6)  # several snapshots come due meanwhile

    assert await host.wait(job_id, deadline_s=15) == jobs_module.JobStatus("exited", 0)
    assert host.stop_reason(job_id) is None
    data, _ = host.output(job_id, os.path.getsize(host.output_path(job_id)))
    assert data.endswith(b"2999\n3000\n")


def _hold(path, sharing):
    """``path`` open for reading in another program's way: sharing reading and writing (Python's ``open``,
    ``Get-Content -Wait``) or reading alone, and never delete."""
    if sharing == "reading and writing":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p] + [
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(path, 0x80000000, 0x1, None, 3, 0, None)  # GENERIC_READ, FILE_SHARE_READ
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb")


@pytest.mark.parametrize("sharing", ["reading and writing", "reading alone"])
async def test_a_program_holding_tail_log_when_the_command_exits_leaves_its_exit_as_it_is(
    tmp_path, monkeypatch, sharing
):
    """The last tail cannot be renamed into place then: it is written into the file itself, which such a program
    shares for writing, and one that shares neither keeps the tail before it while it holds the file. The exit is
    the command's either way."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 10)
    host = _host(tmp_path)
    release = tmp_path / "release"
    job_id = await _start(
        host,
        tmp_path,
        "import os, sys, time\n"
        "sys.stdout.buffer.write(b'x' * 100 + b'\\nfirst\\n'); sys.stdout.flush()\n"
        f"while not os.path.exists(r'{release}'): time.sleep(0.01)\n"
        "sys.stdout.buffer.write(b'last\\n'); sys.stdout.flush()\n"
        "sys.exit(3)\n",
    )
    assert _wait_until(lambda: host.output(job_id, 10)[0].endswith(b"first\n"))
    held = _hold(os.path.join(host.job_dir(job_id), "tail.log"), sharing)
    try:
        release.write_text("")
        status = await host.wait(job_id, deadline_s=15)
        data, _ = host.output(job_id, 10)
    finally:
        held.close()

    assert status == jobs_module.JobStatus("exited", 3)
    assert host.stop_reason(job_id) is None
    assert data.endswith(b"first\nlast\n" if sharing == "reading and writing" else b"first\n")


@pytest.mark.skipif(not os.environ.get("AVIBE_TEST_FAT_DIR"), reason="needs a FAT volume (AVIBE_TEST_FAT_DIR)")
async def test_jobs_run_with_their_files_on_a_volume_without_hard_links(tmp_path):
    """FAT has no hard links: a decision and a stop reason are each created once by a rename instead."""
    jobs_dir = Path(os.environ["AVIBE_TEST_FAT_DIR"]) / f"jobs-{secrets.token_hex(4)}"
    host = LocalJobHost(str(jobs_dir), shell=sys.executable)
    try:
        done = await _start(host, tmp_path, "import sys; sys.stdout.buffer.write(b'ran\\n')")
        assert await host.wait(done, deadline_s=15) == jobs_module.JobStatus("exited", 0)
        assert host.output(done)[0] == b"ran\n"
        running = await _start(host, tmp_path, "import time; time.sleep(60)", tool_call_id="toolu_2")
        await host.kill(running)
        assert host.stop_reason(running) == "killed"
        assert (await host.wait(running, deadline_s=15)).state == "gone"
    finally:
        for entry in jobs_dir.iterdir():
            job = windows_job.open_job(f"avibe-job-{entry.name}")
            if job is not None:
                try:
                    windows_job.terminate_job(job)
                finally:
                    windows_job.close_handle(job)
        shutil.rmtree(jobs_dir, ignore_errors=True)


async def test_a_process_the_command_leaves_running_keeps_the_job_until_it_ends_or_is_killed(tmp_path):
    """POSIX leaves it in the process group; here the wrapper stays, so its job, and ``kill``, still hold it."""
    host = _host(tmp_path)
    job_id = await _start(host, tmp_path, _tree(tmp_path, then="pass"))
    shell, grandchild = _tree_pids(tmp_path)

    assert await host.wait(job_id, deadline_s=10) == jobs_module.JobStatus("exited", 0)
    assert _wait_until(lambda: _gone(shell))
    assert not _gone(grandchild) and not _gone(_wrapper_pid(host, job_id))
    assert host.prune(lambda meta: True, older_than_s=-60) == []

    await host.kill(job_id)
    assert _wait_until(lambda: _gone(grandchild) and _gone(_wrapper_pid(host, job_id)))


async def test_job_files_stay_until_the_call_is_settled(tmp_path):
    """J5."""
    host = _host(tmp_path)
    finished = await _start(host, tmp_path, "pass", tool_call_id="finished")
    running = await _start(host, tmp_path, "import time; time.sleep(60)", tool_call_id="running")
    await host.wait(finished, deadline_s=10)
    assert _wait_until(lambda: _gone(_wrapper_pid(host, finished)))

    assert host.prune(lambda meta: False, older_than_s=0) == []
    assert host.prune(lambda meta: True, older_than_s=0) == [finished]
    assert os.path.isdir(host.job_dir(running))
    await host.kill(running)


async def test_hand_over_gives_a_job_at_most_one_watch(tmp_path, monkeypatch):
    """J6."""
    watches: dict[str, str] = {}

    async def adopt_or_create(meta):
        return watches.setdefault(meta["job_id"], f"wch_{len(watches) + 1}")

    host = _host(tmp_path, on_hand_over=adopt_or_create)
    job_id = await _start(host, tmp_path, "import time; time.sleep(60)")
    _crash_on_meta_write(monkeypatch, 1)
    with pytest.raises(Crash):
        await host.hand_over(job_id)
    monkeypatch.undo()

    recovery = _host(tmp_path, on_hand_over=adopt_or_create)
    assert await recovery.hand_over(job_id) == "wch_1"
    assert await recovery.hand_over(job_id) == "wch_1"
    assert watches == {job_id: "wch_1"}
    await recovery.kill(job_id)


async def test_settlement_reports_each_job_state(tmp_path, make_ctx):
    """T2: exited, running (handed over), stopped with a reason, and unknown, after a restart."""
    by_job: dict[str, str] = {}

    async def watches(meta):
        return by_job.setdefault(meta["job_id"], f"wch_{len(by_job) + 1}")

    host = _host(tmp_path, on_hand_over=watches)
    foreground = await BashTool(host).execute(
        {"command": "import sys; sys.stdout.write('out\\n'); sys.exit(4)"}, make_ctx(tool_call_id="exited")
    )
    await BashTool(host, foreground_window_s=0.2).execute(
        {"command": "import time; print('still', flush=True); time.sleep(60)"}, make_ctx(tool_call_id="running")
    )

    recovery = _host(tmp_path, on_hand_over=watches)
    exited = await settle_bash_call(recovery, instance("exited"))
    assert (result_text(exited), exited.is_error) == (result_text(foreground), True)
    running = await settle_bash_call(recovery, instance("running"))
    assert result_text(running).startswith("Command is still running and is now Watch wch_1.")
    assert await settle_bash_call(recovery, instance("unknown")) is None
    await recovery.kill(running.details["job_id"], reason="aborted")
    stopped = await settle_bash_call(recovery, instance("running"))
    assert result_text(stopped).endswith("Stopped by the user; the command was terminated.")


async def test_a_timeout_holds_across_handover_and_a_restart(tmp_path, make_ctx):
    """J3: the deadline is the job's, so the next owner enforces it."""
    by_job: dict[str, str] = {}

    async def watches(meta):
        return by_job.setdefault(meta["job_id"], f"wch_{len(by_job) + 1}")

    result = await BashTool(_host(tmp_path, on_hand_over=watches)).execute(
        {"command": _tree(tmp_path), "timeout": 60, "watch": True}, make_ctx()
    )
    job_id = result.details["job_id"]
    shell, grandchild = _tree_pids(tmp_path)
    restarted = _host(tmp_path)
    meta = restarted.meta(job_id)
    meta["deadline_at"] = jobs_module._iso(datetime.now(timezone.utc))
    restarted._write_meta(job_id, meta)

    status = await asyncio.wait_for(restarted.wait(job_id, deadline_s=None), timeout=15)

    assert status.state == "gone"
    assert restarted.stop_reason(job_id) == "timeout"
    assert _wait_until(lambda: _gone(shell) and _gone(grandchild))


async def test_vibe_stop_ends_every_running_job(tmp_path):
    from core.watches import JOB_STOP_VIBE_STOP, stop_all_jobs

    host = _host(tmp_path)
    job_id = await _start(host, tmp_path, _tree(tmp_path))
    shell, grandchild = _tree_pids(tmp_path)

    await asyncio.to_thread(stop_all_jobs, jobs_dir=tmp_path / "jobs")

    assert _wait_until(lambda: _gone(shell) and _gone(grandchild))
    assert host.stop_reason(job_id) == JOB_STOP_VIBE_STOP

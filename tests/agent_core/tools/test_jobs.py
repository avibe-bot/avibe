"""``LocalJobHost`` recovery invariants (``recovery.md`` J1, J2, J4, J5, J6).

A "crash" raises out of ``start`` at one step and abandons that host object;
recovery is a new ``LocalJobHost`` over the same directory, as after a restart.
The wrapper runs in its own session, so it outlives the crash as it would in
production.
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import time
from datetime import datetime, timezone

import psutil
import pytest

import core.agent_core.tools.jobs as jobs_module
from core.agent_core.tools.jobs import JobStartError, LocalJobHost


class Crash(Exception):
    pass


def _env():
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


async def _start(host, cwd, command, *, tool_call_id="toolu_1", timeout_s=None):
    return await host.start(
        command, cwd=str(cwd), env=_env(), timeout_s=timeout_s, session_id="ses_test", tool_call_id=tool_call_id
    )


def _wait_until(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _wrapper_gone(job_dir):
    """True once the job's wrapper has exited (or never wrote its pid and never will)."""
    try:
        pid = int(open(os.path.join(job_dir, "pid")).read())
    except (FileNotFoundError, ValueError):
        return False
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _pid_written(path, timeout_s=10.0):
    """The pid a command wrote to ``path``: waits until it is written, not only created (``echo $$ >``)."""
    assert _wait_until(lambda: path.exists() and path.read_text().strip(), timeout_s=timeout_s)
    return int(path.read_text())


def _gone(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _counter(tmp_path):
    path = tmp_path / "counter"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _crash_on_meta_write(monkeypatch, number):
    """Crash right after the ``number``-th ``meta.json`` write of a start."""
    original = LocalJobHost._write_meta
    calls = []

    def write(self, job_id, meta):
        original(self, job_id, meta)
        calls.append(job_id)
        if len(calls) == number:
            raise Crash

    monkeypatch.setattr(LocalJobHost, "_write_meta", write)


async def _crash_before_pid(self, *args):
    raise Crash


@pytest.mark.parametrize(
    "crash_point",
    ["after_meta_before_spawn", "after_spawn_before_pid", "after_identity_before_go", "after_go"],
)
async def test_a_crash_at_any_launch_step_runs_the_command_at_most_once(tmp_path, monkeypatch, crash_point):
    jobs_dir = tmp_path / "jobs"
    host = LocalJobHost(str(jobs_dir))
    if crash_point == "after_meta_before_spawn":
        _crash_on_meta_write(monkeypatch, 1)
    elif crash_point == "after_spawn_before_pid":
        monkeypatch.setattr(LocalJobHost, "_await_pid", _crash_before_pid)
    elif crash_point == "after_identity_before_go":
        _crash_on_meta_write(monkeypatch, 2)

    try:
        await _start(host, tmp_path, f"echo ran >> {tmp_path / 'counter'}")
    except Crash:
        pass
    monkeypatch.undo()

    recovery = LocalJobHost(str(jobs_dir))
    job_id = recovery.find_job("ses_test", "toolu_1")
    assert job_id is not None
    may_have_started = recovery.started(job_id)
    if crash_point != "after_meta_before_spawn":
        assert _wait_until(lambda: _wrapper_gone(recovery.job_dir(job_id)))

    assert may_have_started is (crash_point == "after_go")
    assert _counter(tmp_path) == (1 if may_have_started else 0)
    assert recovery.status(job_id).state == ("exited" if may_have_started else "gone")


async def test_a_wrapper_left_without_a_decision_abandons_and_a_late_host_cannot_start_it(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "DECISION_TIMEOUT_S", 0.05)
    original = LocalJobHost._await_pid

    async def slow_host(self, *args):
        identity = await original(self, *args)
        await asyncio.sleep(0.5)  # the wrapper's own deadline passes meanwhile
        return identity

    monkeypatch.setattr(LocalJobHost, "_await_pid", slow_host)
    host = LocalJobHost(str(tmp_path / "jobs"))

    with pytest.raises(JobStartError):
        await _start(host, tmp_path, f"echo ran >> {tmp_path / 'counter'}")

    job_id = host.find_job("ses_test", "toolu_1")
    assert _wait_until(lambda: _wrapper_gone(host.job_dir(job_id)))
    assert not host.started(job_id)
    assert _counter(tmp_path) == 0


async def test_recovery_reads_exited_running_and_gone(tmp_path):
    jobs_dir = str(tmp_path / "jobs")
    host = LocalJobHost(jobs_dir)
    exited = await _start(host, tmp_path, "echo done; exit 7", tool_call_id="exited")
    running = await _start(host, tmp_path, "sleep 30", tool_call_id="running")
    gone = await _start(host, tmp_path, "sleep 30", tool_call_id="gone")
    assert (await host.wait(exited, deadline_s=5)).exit_code == 7
    # The wrapper dies without writing `exit` (as on SIGKILL); its command child is cleaned up too.
    gone_pid = host.meta(gone)["process"]["pid"]
    os.killpg(gone_pid, 9)
    assert _wait_until(lambda: _wrapper_gone(host.job_dir(gone)))

    recovery = LocalJobHost(jobs_dir)

    assert recovery.status(exited) == jobs_module.JobStatus("exited", 7)
    assert recovery.output(exited) == (b"done\n", 5)
    assert recovery.status(running).state == "running"
    assert recovery.status(gone).state == "gone"
    await recovery.kill(running)
    assert recovery.status(running).state == "gone"


async def test_a_recycled_pid_is_neither_reported_nor_killed(tmp_path):
    """J2: the recorded pid now belongs to another session leader."""
    jobs_dir = str(tmp_path / "jobs")
    host = LocalJobHost(jobs_dir)
    job_id = await _start(host, tmp_path, "sleep 30")
    await host.kill(job_id)
    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        meta = host.meta(job_id)
        meta["process"].update(pid=stranger.pid, pgid=stranger.pid)
        host._write_meta(job_id, meta)

        recovery = LocalJobHost(jobs_dir)
        assert recovery.status(job_id).state == "gone"
        await recovery.kill(job_id)

        time.sleep(0.1)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


async def test_output_on_disk_keeps_the_head_and_tail_of_a_long_command(tmp_path, monkeypatch):
    """J4: the reader is told how much was dropped."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 1000)
    monkeypatch.setattr(jobs_module, "OUTPUT_TAIL_BYTES", 2000)
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, "seq 1 100000")
    assert (await host.wait(job_id, deadline_s=10)).exit_code == 0

    job_dir = host.job_dir(job_id)
    assert os.path.getsize(os.path.join(job_dir, "output.log")) == 1000
    assert os.path.getsize(os.path.join(job_dir, "tail.log")) <= 2000 + 32

    chunks, offset, skipped = [], 0, 0
    while True:
        data, new_offset = host.output(job_id, offset)
        skipped += new_offset - len(data) - offset
        offset = new_offset
        if not data:
            break
        chunks.append(data)
    total = len(b"".join(f"{i}\n".encode() for i in range(1, 100001)))
    assert offset == total
    assert skipped == total - 3000
    assert b"".join(chunks).endswith(b"99999\n100000\n")


async def test_the_wrapper_enforces_the_deadline_while_no_host_runs(tmp_path):
    """J3: the host crashed right after the start; the deadline still ends the tree and is recorded."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, f"echo $$ > {tmp_path / 'sh.pid'}; sleep 30 & sleep 30", timeout_s=0.5)
    del host  # nobody waits on the job from here on

    # Either the shell ran and the deadline stopped its tree, or (on a slow launch) the deadline passed
    # first and the command never started: the wrapper owns both, and records the timeout either way.
    assert _wait_until(lambda: _wrapper_gone(str(tmp_path / "jobs" / job_id)), timeout_s=10)
    restarted = LocalJobHost(str(tmp_path / "jobs"))
    assert restarted.status(job_id).state == "gone"
    assert restarted.stop_reason(job_id) == "timeout"
    if (tmp_path / "sh.pid").exists():
        shell_pid = _pid_written(tmp_path / "sh.pid")
        assert _wait_until(lambda: _gone(shell_pid))


async def test_a_wrapper_that_cannot_keep_the_log_stops_its_command(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 10)
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, f"echo $$ > {tmp_path / 'sh.pid'}; sleep 0.5; seq 1 1000; sleep 30")
    # The tail cannot be written once the head is full, as on a full disk.
    os.mkdir(os.path.join(host.job_dir(job_id), "tail.log"))

    status = await asyncio.wait_for(host.wait(job_id, deadline_s=None), timeout=10)

    assert status.state == "gone"
    assert host.stop_reason(job_id) == "wrapper_error"
    shell_pid = int((tmp_path / "sh.pid").read_text())
    assert _wait_until(
        lambda: not psutil.pid_exists(shell_pid) or psutil.Process(shell_pid).status() == psutil.STATUS_ZOMBIE
    )


async def test_a_finished_wrapper_leaves_the_registry(tmp_path):
    """The reaper drops the wrapper even when no status call comes after it exits."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    # The shell exits at once; a background child keeps the pipe, so the wrapper outlives the exit.
    job_id = await _start(host, tmp_path, "sleep 0.5 &")
    assert (await host.wait(job_id, deadline_s=5)).state == "exited"

    assert _wait_until(lambda: job_id not in host._children, timeout_s=5)


async def test_the_first_recorded_stop_reason_wins(tmp_path, monkeypatch):
    """Two stoppers that both saw no reason yet: the second must not overwrite the first."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, "sleep 30")
    stopped = os.path.join(host.job_dir(job_id), "stopped")
    with open(stopped, "w") as handle:
        handle.write("aborted\n")  # the other stopper got there first
    real_read = jobs_module._read_text
    monkeypatch.setattr(jobs_module, "_read_text", lambda path: None if path == stopped else real_read(path))

    await host.kill(job_id, reason="timeout")  # it checked before the other one wrote
    monkeypatch.undo()

    assert host.stop_reason(job_id) == "aborted"


async def test_a_deadline_that_passed_during_the_launch_never_starts_the_command(tmp_path, monkeypatch):
    """The deadline is checked before the shell is spawned: a missing shell is never even tried."""
    original = LocalJobHost._await_pid

    async def slow_host(self, *args):
        identity = await original(self, *args)
        await asyncio.sleep(0.3)  # the job's deadline passes during the handshake
        return identity

    monkeypatch.setattr(LocalJobHost, "_await_pid", slow_host)
    host = LocalJobHost(str(tmp_path / "jobs"), shell=str(tmp_path / "no-such-shell"))
    job_id = await _start(host, tmp_path, "true", timeout_s=0.1)

    assert _wait_until(lambda: _wrapper_gone(host.job_dir(job_id)))
    assert host.stop_reason(job_id) == "timeout"


async def test_the_deadline_holds_after_the_command_closes_its_output(tmp_path):
    """A command that closes stdout and stderr and keeps running is still stopped at its deadline (J3)."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(
        host, tmp_path, f"echo $$ > {tmp_path / 'sh.pid'}; exec >/dev/null 2>&1; sleep 20", timeout_s=0.5
    )
    del host  # nobody waits on the job from here on

    stopped = _wait_until(lambda: _wrapper_gone(str(tmp_path / "jobs" / job_id)), timeout_s=6)
    restarted = LocalJobHost(str(tmp_path / "jobs"))
    if not stopped:
        await restarted.kill(job_id)
    assert stopped
    assert restarted.stop_reason(job_id) == "timeout"


async def test_a_kill_whose_reason_cannot_be_recorded_still_stops_the_command(tmp_path, monkeypatch):
    """Recording why is best effort at every stopper: a full disk must not leave the command running."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, f"echo $$ > {tmp_path / 'sh.pid'}; sleep 30")
    shell_pid = _pid_written(tmp_path / "sh.pid")
    real_create = host._create_once

    def full_disk(job_id, name, value):
        if name == "stopped":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_create(job_id, name, value)

    monkeypatch.setattr(host, "_create_once", full_disk)
    try:
        await host.kill(job_id, reason="aborted")
        stopped = _wait_until(
            lambda: not psutil.pid_exists(shell_pid) or psutil.Process(shell_pid).status() == psutil.STATUS_ZOMBIE
        )
    finally:
        monkeypatch.undo()
        await host.kill(job_id)

    assert stopped
    # The host could not record "aborted"; its SIGTERM reached the wrapper, which recorded what it saw.
    assert host.stop_reason(job_id) == "killed"


def test_a_shell_that_exited_by_the_deadline_check_is_not_a_timeout(tmp_path, monkeypatch):
    """The wrapper alone decides timeout versus exit, and it looks at the shell before the clock."""
    import core.agent_core.tools.job_wrapper as wrapper

    stops = []
    monkeypatch.setattr(wrapper, "_stop_group", lambda job_dir, reason, proc: stops.append(reason))
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 3"], stdout=subprocess.PIPE)
    proc.wait()  # it exited before the deadline was checked
    log = wrapper._BoundedLog(str(tmp_path), 1024, 1024)
    try:
        wrapper._run(str(tmp_path), proc, log, time.time() - 1)
    finally:
        proc.stdout.close()

    assert stops == []
    assert (tmp_path / "exit").read_text() == "3\n"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGUSR1])
async def test_a_signal_to_the_wrapper_alone_stops_its_command(tmp_path, sig):
    """``pkill -f python`` reaches the wrapper but not its shell: the wrapper must take the group with it."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, f"echo $$ > {tmp_path / 'sh.pid'}; sleep 30", timeout_s=60)
    shell_pid = _pid_written(tmp_path / "sh.pid")

    os.kill(int(open(os.path.join(host.job_dir(job_id), "pid")).read()), sig)

    try:
        assert _wait_until(lambda: _gone(shell_pid), timeout_s=6)
    finally:
        await host.kill(job_id)
    assert host.stop_reason(job_id) == "killed"


async def test_a_command_outliving_a_killed_wrapper_is_still_running_and_held_to_its_deadline(tmp_path, monkeypatch):
    """SIGKILL to the wrapper alone: the group carries the job's marker, so it is not gone (J2, J3)."""
    import sys

    monkeypatch.setattr(jobs_module, "_WRAPPER_DECIDES_S", 0.2)
    # Python as the shell: its environment, and so the marker, is readable on macOS too.
    host = LocalJobHost(str(tmp_path / "jobs"), shell=sys.executable)
    script = f"import os, time; open({str(tmp_path / 'sh.pid')!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    job_id = await _start(host, tmp_path, script, timeout_s=60)
    shell_pid = _pid_written(tmp_path / "sh.pid")
    os.kill(int(open(os.path.join(host.job_dir(job_id), "pid")).read()), signal.SIGKILL)
    assert _wait_until(lambda: _wrapper_gone(host.job_dir(job_id)))
    # Only the host is left to enforce the deadline, from meta.json: bring it to now, whatever the launch took.
    meta = host.meta(job_id)
    meta["deadline_at"] = jobs_module._iso(datetime.now(timezone.utc))
    host._write_meta(job_id, meta)

    try:
        assert host.status(job_id).state == "running"
        status = await asyncio.wait_for(host.wait(job_id, deadline_s=None), timeout=10)
        assert (status.state, host.stop_reason(job_id)) == ("gone", "timeout")
        assert _wait_until(lambda: _gone(shell_pid))
    finally:
        await host.kill(job_id)


async def test_start_reports_a_go_it_made_durable_even_if_reading_it_back_fails(tmp_path, monkeypatch):
    """Once ``go`` is linked, the command runs: start must not report that it could not start."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    real_read = jobs_module._read_text

    def read_fails_for_decision(path):
        if path.endswith(os.sep + "decision"):
            raise OSError(errno.EMFILE, "Too many open files")
        return real_read(path)

    monkeypatch.setattr(jobs_module, "_read_text", read_fails_for_decision)
    job_id = await _start(host, tmp_path, f"echo ran > {tmp_path / 'ran'}")
    monkeypatch.undo()

    assert (await host.wait(job_id, deadline_s=5)).state == "exited"
    assert (tmp_path / "ran").read_text() == "ran\n"


async def test_prune_keeps_an_exited_job_whose_wrapper_still_drains_children(tmp_path):
    host = LocalJobHost(str(tmp_path / "jobs"))
    # The shell exits at once; a background child keeps the pipe, so the wrapper keeps the log.
    job_id = await _start(host, tmp_path, "sleep 2 &")
    assert (await host.wait(job_id, deadline_s=5)).state == "exited"

    removed = host.prune(lambda meta: True, older_than_s=-60)

    assert removed == []
    assert os.path.isdir(host.job_dir(job_id))


async def test_hosts_over_different_spellings_of_the_jobs_directory_see_the_same_job(tmp_path):
    """A legacy alias (``~/.vibe_remote`` -> ``~/.avibe``, ``/tmp`` -> ``/private/tmp``) names the same jobs."""
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    host = LocalJobHost(str(tmp_path / "alias" / "jobs"))
    job_id = await _start(host, tmp_path, "sleep 30")
    try:
        other = LocalJobHost(str(tmp_path / "real" / "jobs"))
        assert other.status(job_id).state == "running"
    finally:
        await host.kill(job_id)


async def test_readers_of_job_files_bound_what_they_read(tmp_path):
    """Job files sit where the command can write: a state file or tail.log header of any size is read in a bound."""
    import tracemalloc

    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, "true")
    await host.wait(job_id, deadline_s=5)
    job_dir = host.job_dir(job_id)
    with open(os.path.join(job_dir, "stopped"), "w") as handle:
        handle.write("x" * 20_000_000)
    with open(os.path.join(job_dir, "tail.log"), "w") as handle:
        handle.write("[output from byte 0" + "0" * 20_000_000)
    tracemalloc.start()
    try:
        reason = host.stop_reason(job_id)
        data, offset = host.output(job_id, since=os.path.getsize(host.output_path(job_id)))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024
    assert len(reason) <= 4096
    assert (data, offset) == (b"", os.path.getsize(host.output_path(job_id)))


async def test_a_kill_signals_at_once_even_when_worker_threads_are_busy(tmp_path):
    """The default executor is shared with every file tool: a kill must not queue behind it."""
    import threading

    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, f"echo $$ > {tmp_path / 'sh.pid'}; sleep 30")
    shell_pid = _pid_written(tmp_path / "sh.pid")
    release = threading.Event()
    busy = [asyncio.ensure_future(asyncio.to_thread(release.wait, 10)) for _ in range(64)]
    try:
        killing = asyncio.ensure_future(host.kill(job_id, reason="aborted"))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not _gone(shell_pid):
            await asyncio.sleep(0.02)
        gone_in_time = _gone(shell_pid)
    finally:
        release.set()
        await asyncio.gather(*busy)
        await killing

    assert gone_in_time


async def test_a_cancelled_kill_still_finishes_the_group(tmp_path):
    """Once the first signal is sent, the SIGKILL fallback runs even if the caller is cancelled meanwhile."""
    import sys

    host = LocalJobHost(str(tmp_path / "jobs"), shell=sys.executable)
    script = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(tmp_path / 'sh.pid')!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    job_id = await _start(host, tmp_path, script)
    shell_pid = _pid_written(tmp_path / "sh.pid")
    os.kill(int(open(os.path.join(host.job_dir(job_id), "pid")).read()), signal.SIGKILL)  # only the host is left
    assert _wait_until(lambda: _wrapper_gone(host.job_dir(job_id)))

    killing = asyncio.ensure_future(host.kill(job_id, reason="aborted"))
    await asyncio.sleep(0.5)  # SIGTERM is ignored; the host is waiting before SIGKILL
    killing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await killing

    assert _wait_until(lambda: _gone(shell_pid), timeout_s=8)


@pytest.mark.parametrize(
    ("command", "starts"),
    [
        ("# " + "\x01" * 1000 + "\ntrue", False),  # JSON escapes each control character to six characters
        ("# " + "\u4e2d" * 1000 + "\ntrue", True),  # written as it is, not escaped
    ],
    ids=["escapes-past-the-bound", "non-ascii"],
)
async def test_a_command_whose_record_would_be_unreadable_never_starts(tmp_path, monkeypatch, command, starts):
    """The host reads meta.json in a bound, so it never writes one past it: such a command is refused unstarted."""
    monkeypatch.setattr(jobs_module, "_META_CHARS", 2000)
    host = LocalJobHost(str(tmp_path / "jobs"))

    if not starts:
        with pytest.raises(JobStartError):
            await _start(host, tmp_path, command)
        assert host.find_job("ses_test", "toolu_1") is None
        return
    job_id = await _start(host, tmp_path, command)
    assert (await host.wait(job_id, deadline_s=5)).state == "exited"
    assert host.meta(job_id)["command"] == command


@pytest.mark.parametrize("step", ["start", "kill"])
async def test_a_cancel_mid_transition_never_leaves_a_command_without_an_owner(tmp_path, monkeypatch, step):
    """A cancel during the step that publishes ``go``, or during kill's verification, still ends the command."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    name = "_record_and_decide" if step == "start" else "_verify_for_kill"
    real = getattr(LocalJobHost, name)

    def slow_after(self, *args):
        result = real(self, *args)  # go is published / stopped is recorded
        loop.call_soon_threadsafe(entered.set)
        time.sleep(0.5)
        return result

    command = f"echo $$ > {tmp_path / 'sh.pid'}; sleep 30"
    if step == "kill":
        job_id = await _start(host, tmp_path, command)
    monkeypatch.setattr(LocalJobHost, name, slow_after)
    task = asyncio.ensure_future(
        _start(host, tmp_path, command) if step == "start" else host.kill(job_id, reason="aborted")
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()

    job_id = host.find_job("ses_test", "toolu_1")
    if (tmp_path / "sh.pid").exists():
        shell_pid = _pid_written(tmp_path / "sh.pid")
        assert _wait_until(lambda: _gone(shell_pid), timeout_s=8)
    assert (await host.wait(job_id, deadline_s=8)).state != "running"


async def test_meta_json_is_read_in_a_bound(tmp_path):
    """The job directory is the command's to write: a huge meta.json is refused as corrupt, not loaded."""
    import tracemalloc

    host = LocalJobHost(str(tmp_path / "jobs"))
    job_id = await _start(host, tmp_path, "true")
    await host.wait(job_id, deadline_s=5)
    with open(os.path.join(host.job_dir(job_id), "meta.json"), "w") as handle:
        handle.write('{"pad": "' + "x" * 20_000_000 + '"}')
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            host.meta(job_id)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < 16 * 1024 * 1024  # the 4 Mi-character bound, read and decoded; the file is 20 MB


def test_the_wrapper_stops_its_group_even_when_its_diagnostics_fail(tmp_path, monkeypatch):
    """The wrapper's own failure handler must stop the command even if writing the traceback fails (ENOSPC)."""
    import core.agent_core.tools.job_wrapper as wrapper

    class Stopped(BaseException):
        pass

    def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    stops = []

    def stop_group(job_dir, reason, proc):
        stops.append(reason)
        raise Stopped  # the real one ends the process group and never returns

    monkeypatch.setattr(wrapper, "_wait_for_decision", lambda job_dir, timeout_s: "go")
    monkeypatch.setattr(wrapper, "_BoundedLog", full_disk)
    monkeypatch.setattr(wrapper.traceback, "print_exc", full_disk)
    monkeypatch.setattr(wrapper, "_stop_group", stop_group)
    argv = ["job_wrapper.py", "avibe-job", str(tmp_path), "/bin/sh", "true", "30", "10", "10", "-"]

    with pytest.raises(Stopped):
        wrapper.main(argv)

    assert stops == ["wrapper_error"]


async def test_job_files_stay_until_the_call_is_settled(tmp_path):
    """J5."""
    host = LocalJobHost(str(tmp_path / "jobs"))
    finished = await _start(host, tmp_path, "true", tool_call_id="finished")
    running = await _start(host, tmp_path, "sleep 30", tool_call_id="running")
    await host.wait(finished, deadline_s=5)
    # An exited job is kept while its wrapper still runs (it may drain children); this one has none.
    assert _wait_until(lambda: _wrapper_gone(host.job_dir(finished)))

    assert host.prune(lambda meta: False, older_than_s=0) == []
    assert host.prune(lambda meta: True, older_than_s=0) == [finished]
    assert os.path.isdir(host.job_dir(running))
    await host.kill(running)


async def test_hand_over_gives_a_job_at_most_one_watch(tmp_path, monkeypatch):
    """J6, job side: the Watch is adopt-or-create by job id, and a recorded Watch is reused."""
    watches: dict[str, str] = {}

    async def adopt_or_create(meta):
        return watches.setdefault(meta["job_id"], f"wch_{len(watches) + 1}")

    host = LocalJobHost(str(tmp_path / "jobs"), on_hand_over=adopt_or_create)
    job_id = await _start(host, tmp_path, "sleep 30")
    _crash_on_meta_write(monkeypatch, 1)  # the Watch exists, recording it crashes
    with pytest.raises(Crash):
        await host.hand_over(job_id)
    monkeypatch.undo()

    recovery = LocalJobHost(str(tmp_path / "jobs"), on_hand_over=adopt_or_create)
    assert await recovery.hand_over(job_id) == "wch_1"
    assert await recovery.hand_over(job_id) == "wch_1"
    assert watches == {job_id: "wch_1"}
    assert recovery.meta(job_id)["watch_id"] == "wch_1"
    await recovery.kill(job_id)

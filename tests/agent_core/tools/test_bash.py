"""C-7 ``bash``: Pi's results over a job handle, foreground-to-Watch handover, and settlement (J3)."""

from __future__ import annotations

import asyncio
import errno
import functools
import os
import signal
import time
from datetime import datetime, timezone

import psutil
import pytest

from core.agent_core.agent.jobs import TrackingJobHost
from core.agent_core.cancel import CancelToken
import core.agent_core.tools.bash as bash_module
from core.agent_core.tools.bash import BashTool, settle_bash_call
import core.agent_core.tools.jobs as jobs_module
from core.agent_core.tools.jobs import LocalJobHost
from tests.agent_core.tools.conftest import result_text

LABEL = "Output log (first 1.0MB, then the last 2.0MB in tail.log beside it)"


class Watches:
    """Adopt-or-create keyed by job id, as the adapter's Watch target is."""

    def __init__(self) -> None:
        self.by_job: dict[str, str] = {}

    async def __call__(self, meta) -> str:
        return self.by_job.setdefault(meta["job_id"], f"wch_{len(self.by_job) + 1}")


def _host(tmp_path, watches=None):
    return LocalJobHost(str(tmp_path / "jobs"), on_hand_over=watches)


def _process_gone(pid: int, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.02)
    return False


async def test_long_output_keeps_the_tail_and_names_the_full_output(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "seq 1 2500"}, make_ctx())

    path = result.details["output_path"]
    assert not result.is_error
    # One label in every state: the log keeps everything up to the cap, so it is never promised as complete.
    assert result_text(result).endswith(
        f"2499\n2500\n\n[Showing lines 501-2500 of 2500. Output log (first 1.0MB, then the last 2.0MB in tail.log beside it): {path}]"
    )
    assert open(path).read() == "".join(f"{i}\n" for i in range(1, 2501))


async def test_a_log_bounded_on_disk_is_not_called_the_full_output(tmp_path, make_ctx, monkeypatch):
    """J4: the result says the middle is gone instead of promising a complete file."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 1000)
    monkeypatch.setattr(jobs_module, "OUTPUT_TAIL_BYTES", 2000)

    result = await BashTool(_host(tmp_path)).execute({"command": "seq 1 100000"}, make_ctx())

    text = result_text(result)
    assert text.startswith("1\n2\n3\n")
    path = result.details["output_path"]
    assert f"100000\n\n\n[Output log (first 1000B, then the last 2.0KB in tail.log beside it): {path}]" in text
    assert "Full output" not in text
    assert result.details["omitted_bytes"] == len("".join(f"{i}\n" for i in range(1, 100001))) - 3000


async def test_a_completed_overlong_line_reports_its_size(tmp_path, make_ctx):
    """Avibe fix to Pi: the partial-line notice names the size of the last line even once it is complete."""
    command = "head -c 60000 /dev/zero | tr '\\0' x; echo"

    result = await BashTool(_host(tmp_path)).execute({"command": command}, make_ctx())

    path = result.details["output_path"]
    assert result_text(result).endswith(
        f"\n\n[Showing last 50.0KB of line 1 (line is 58.6KB). Output log (first 1.0MB, then the last 2.0MB in tail.log beside it): {path}]"
    )


async def test_a_line_longer_than_the_normalizer_keeps_reports_its_whole_size(tmp_path, make_ctx):
    command = "head -c 900000 /dev/zero | tr '\\0' x; echo"

    result = await BashTool(_host(tmp_path)).execute({"command": command}, make_ctx())

    assert "[Showing last 50.0KB of line 1 (line is 878.9KB). " in result_text(result)


async def test_output_past_the_head_names_where_its_end_is(tmp_path, make_ctx):
    """Between the 1 MiB head and the 3 MiB bound nothing is dropped, but the end is only in tail.log."""
    result = await BashTool(_host(tmp_path)).execute({"command": "seq 1 200000"}, make_ctx())

    path = result.details["output_path"]
    assert result_text(result).endswith(f"[Showing lines 198001-200000 of 200000. {LABEL}: {path}]")
    with open(os.path.join(os.path.dirname(path), "tail.log"), encoding="utf-8") as handle:
        header, rest = handle.read().split("\n", 1)
    assert header == f"[output from byte {os.path.getsize(path)}]"
    assert rest.endswith("199999\n200000\n")


@pytest.mark.parametrize(("arguments", "expected"), [({"command": "printf '%s' \ud800"}, "\ufffd")])
async def test_a_lone_surrogate_in_the_command_is_sanitized(tmp_path, make_ctx, arguments, expected):
    result = await BashTool(_host(tmp_path)).execute(arguments, make_ctx())

    assert (result.is_error, result_text(result)) == (False, expected)


async def test_a_non_zero_exit_is_an_error_result(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "echo oops >&2; exit 3"}, make_ctx())

    assert result.is_error
    assert result_text(result) == "oops\n\n\nCommand exited with code 3"


async def test_an_out_of_range_timeout_is_an_error_result(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "true", "timeout": 10**400}, make_ctx())

    assert result.is_error
    assert result_text(result) == "Invalid timeout: must be a finite number of seconds"


async def test_a_nul_byte_in_the_command_is_an_error_result(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "echo a\x00b"}, make_ctx())

    assert result.is_error
    assert result_text(result) == "Invalid command: contains a NUL byte"


async def test_only_the_recorded_reason_makes_a_gone_job_a_timeout(tmp_path, make_ctx, monkeypatch):
    """A wrapper failure inside a short timeout is reported as what it was, not as the timeout."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 10)
    host = _host(tmp_path)
    running = asyncio.ensure_future(
        BashTool(host).execute({"command": "sleep 0.3; seq 1 1000; sleep 30", "timeout": 1.0}, make_ctx())
    )
    job_id = None
    while job_id is None or not os.path.exists(os.path.join(host.job_dir(job_id), "pid")):
        await asyncio.sleep(0.01)
        job_id = job_id or host.find_job("ses_test", "toolu_1")
    # The wrapper cannot write its tail snapshot once the head is full, as on a full disk.
    wrapper_pid = open(os.path.join(host.job_dir(job_id), "pid")).read().strip()
    os.mkdir(os.path.join(host.job_dir(job_id), f"tail.log.{wrapper_pid}.tmp"))

    result = await asyncio.wait_for(running, timeout=10)

    assert host.stop_reason(job_id) == "wrapper_error"
    assert result.is_error
    assert result_text(result).endswith("Command terminated without an exit code")


@pytest.mark.parametrize("failure", ["cwd_missing", "cwd_not_accessible", "jobs_dir_not_writable", "shell_missing"])
async def test_a_command_that_cannot_start_has_a_defined_result(tmp_path, make_ctx, failure):
    if failure != "cwd_missing" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    cwd, jobs_dir, shell = tmp_path, tmp_path / "jobs", None
    if failure == "cwd_missing":
        cwd = tmp_path / "gone"
    elif failure == "cwd_not_accessible":
        cwd = tmp_path / "locked"
        cwd.mkdir(mode=0o000)
    elif failure == "jobs_dir_not_writable":
        jobs_dir.mkdir(mode=0o555)
    else:
        shell = str(tmp_path / "no-such-shell")
    locked = {"cwd_not_accessible": cwd, "jobs_dir_not_writable": jobs_dir}.get(failure)
    try:
        host = LocalJobHost(str(jobs_dir), shell=shell)
        result = await BashTool(host).execute({"command": f"touch {tmp_path / 'ran'}"}, make_ctx(cwd=str(cwd)))
    finally:
        if locked is not None:
            os.chmod(locked, 0o755)

    expected = {
        "cwd_missing": f"Working directory does not exist: {cwd}\nCannot execute bash commands.",
        "cwd_not_accessible": f"Working directory is not accessible: {cwd}\nCannot execute bash commands.",
        "jobs_dir_not_writable": "Could not start the command: permission denied.",
        # The wrapper cannot spawn the shell: it records wrapper_error and the job ends without an exit code.
        "shell_missing": "(no output)\n\nCommand terminated without an exit code",
    }[failure]
    assert (result.is_error, result_text(result)) == (True, expected)
    assert not (tmp_path / "ran").exists()


async def test_stdin_is_closed(tmp_path, make_ctx):
    started = time.monotonic()
    result = await BashTool(_host(tmp_path)).execute({"command": 'read x; echo "rc=$?"'}, make_ctx())

    assert result_text(result) == "rc=1\n"
    assert time.monotonic() - started < 5


async def test_timeout_kills_the_tree_including_background_children(tmp_path, make_ctx):
    command = f"sleep 30 & echo $! > {tmp_path / 'bg.pid'}; echo started; sleep 30"

    result = await BashTool(_host(tmp_path)).execute({"command": command, "timeout": 1.5}, make_ctx())

    assert result.is_error
    assert result_text(result) == "started\n\n\nCommand timed out after 1.5 seconds"
    assert _process_gone(int((tmp_path / "bg.pid").read_text()))


async def test_abort_kills_the_command(tmp_path, make_ctx):
    cancel = CancelToken()
    seen = []

    def progress(tail):
        seen.append(tail)
        cancel.cancel()

    ctx = make_ctx(cancel=cancel, on_progress=progress)
    result = await BashTool(_host(tmp_path)).execute(
        {"command": f"echo $$ > {tmp_path / 'sh.pid'}; echo started; sleep 30"}, ctx
    )

    assert seen == ["started\n"]
    assert result.is_error
    assert result_text(result) == "started\n\n\nStopped by the user; the command was terminated."
    assert _process_gone(int((tmp_path / "sh.pid").read_text()))


@pytest.mark.parametrize(
    ("arguments", "abort", "expected"),
    [
        ({"command": "echo started; sleep 30", "timeout": 1.5}, False, "started\n\n\nCommand timed out after 1.5 seconds"),
        ({"command": "echo started; sleep 30"}, True, "started\n\n\nStopped by the user; the command was terminated."),
    ],
    ids=["timeout", "abort"],
)
async def test_stops_are_reported_through_the_agents_job_host(tmp_path, make_ctx, arguments, abort, expected):
    """bash gets the loop's TrackingJobHost (Agent.jobs); reasoned kills and stop reasons pass through it."""
    cancel = CancelToken()
    ctx = make_ctx(cancel=cancel, on_progress=lambda tail: abort and cancel.cancel())

    result = await BashTool(TrackingJobHost(_host(tmp_path))).execute(arguments, ctx)

    assert (result.is_error, result_text(result)) == (True, expected)


@pytest.mark.parametrize("caller", ["host", "bash"])
async def test_only_a_live_wrapper_decides_at_the_deadline(tmp_path, make_ctx, monkeypatch, caller):
    """Only the wrapper sees the shell exit; the host and bash leave a live wrapper a grace, then stop the job."""
    monkeypatch.setattr(jobs_module, "_WRAPPER_DECIDES_S", 0.6)
    quick_kill = functools.partial(jobs_module._terminate_group, timeout_s=0.3)
    monkeypatch.setattr(jobs_module, "_terminate_group", quick_kill)
    host = _host(tmp_path)
    command = f"echo $$ > {tmp_path / 'sh.pid'}; sleep 30"
    if caller == "bash":
        running = asyncio.ensure_future(BashTool(host).execute({"command": command, "timeout": 60}, make_ctx()))
        job_id = None
        while job_id is None:
            await asyncio.sleep(0.01)
            job_id = host.find_job("ses_test", "toolu_1")
    else:
        job_id = await host.start(
            command,
            cwd=str(tmp_path),
            env={"PATH": os.environ["PATH"]},
            timeout_s=60,
            session_id="ses_test",
            tool_call_id="toolu_1",
        )
        running = asyncio.ensure_future(host.wait(job_id, deadline_s=None))
    wrapper_pid = os.path.join(host.job_dir(job_id), "pid")
    # Written, not only created: ``echo $$ >`` creates the file before it writes the pid.
    while not (os.path.exists(wrapper_pid) and (tmp_path / "sh.pid").exists() and (tmp_path / "sh.pid").read_text()):
        await asyncio.sleep(0.01)
    os.kill(int(open(wrapper_pid).read()), signal.SIGSTOP)  # the wrapper can no longer decide
    shell_pid = int((tmp_path / "sh.pid").read_text())
    # The deadline passes now, whatever the launch took; the host reads it from meta.json.
    meta = host.meta(job_id)
    meta["deadline_at"] = jobs_module._iso(datetime.now(timezone.utc))
    host._write_meta(job_id, meta)

    await asyncio.sleep(0.3)  # past the deadline, inside the grace
    alive_in_grace = psutil.pid_exists(shell_pid) and psutil.Process(shell_pid).status() != psutil.STATUS_ZOMBIE
    outcome = await asyncio.wait_for(running, timeout=10)

    assert alive_in_grace
    assert host.stop_reason(job_id) == "timeout"
    if caller == "bash":
        assert result_text(outcome).endswith("Command timed out after 60 seconds")
    else:
        assert outcome.state == "gone"
    assert _process_gone(shell_pid)


@pytest.mark.parametrize("command", ["yes", "yes $'a\\rb'"], ids=["lines", "redraws"])
async def test_a_flood_of_output_never_stalls_the_event_loop(tmp_path, make_ctx, command):
    """Every Session shares the event loop: following a job's output may not hold it, so an abort still lands."""
    cancel = CancelToken()
    lags: list[float] = []

    async def ticker():
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            lags.append(time.monotonic() - before)

    ticking = asyncio.ensure_future(ticker())
    asyncio.get_running_loop().call_later(1.0, cancel.cancel)
    started = time.monotonic()
    try:
        result = await BashTool(_host(tmp_path)).execute(
            {"command": command}, make_ctx(cancel=cancel, on_progress=lambda tail: None)
        )
    finally:
        ticking.cancel()

    assert result_text(result).endswith("Stopped by the user; the command was terminated.")
    assert time.monotonic() - started < 6.0
    # Following the output costs the loop about 0.1 s at worst (it took 3 to 5 s on the loop before);
    # the host's own small reads may add a slow disk's latency on top (ledger A56).
    assert max(lags) < 1.5, max(lags)


async def test_a_result_never_waits_behind_busy_file_tools(tmp_path, make_ctx):
    """asyncio's default executor is the file tools'; following a job's output has a pool of its own."""
    import threading

    release = threading.Event()
    busy = [asyncio.ensure_future(asyncio.to_thread(release.wait, 10)) for _ in range(64)]
    started = time.monotonic()
    try:
        result = await BashTool(_host(tmp_path)).execute({"command": "echo hi"}, make_ctx())
        took = time.monotonic() - started
    finally:
        release.set()
        await asyncio.gather(*busy)

    assert result_text(result) == "hi\n"
    assert took < 3.0


async def test_a_handover_shows_the_latest_output_however_much_came_before(tmp_path):
    """A one-shot render reads the whole retained log: head, the omitted middle if any, and the tail.

    A fresh ``JobOutput`` (settlement, a recovery handover) renders a job whose end is already on disk.
    """
    host = _host(tmp_path, Watches())
    job_id = await host.start(
        "seq 1 300000; sleep 30",
        cwd=str(tmp_path),
        env={"PATH": os.environ["PATH"]},
        timeout_s=None,
        session_id="ses_test",
        tool_call_id="toolu_1",
    )
    tail = os.path.join(host.job_dir(job_id), "tail.log")
    try:
        for _ in range(500):  # the wrapper snapshots tail.log four times a second
            if os.path.exists(tail) and open(tail, "rb").read().endswith(b"\n300000\n"):
                break
            await asyncio.sleep(0.02)

        result = await settle_bash_call(host, "ses_test", "toolu_1")

        text = result_text(result)
        assert text.startswith("Command is still running and is now Watch wch_1.")
        assert "\n300000\n" in text
    finally:
        await host.kill(job_id)


async def test_the_foreground_window_hands_over_and_the_command_runs_once(tmp_path, make_ctx):
    watches = Watches()
    host = _host(tmp_path, watches)
    command = f"echo run >> {tmp_path / 'counter'}; echo working; sleep 2; echo finished"

    result = await BashTool(host, foreground_window_s=1.0).execute({"command": command}, make_ctx())

    job_id = result.details["job_id"]
    path = host.output_path(job_id)
    text = result_text(result)
    assert not result.is_error
    assert text.startswith(
        "Command is still running and is now Watch wch_1. You will get a follow-up message when it finishes.\n\n"
    )
    # A running job's log may still pass the on-disk cap, so it is never called the full output (J4).
    assert text.endswith(
        f"\n\nOutput log (first 1.0MB, then the last 2.0MB in tail.log beside it): {path}\nCheck: vibe watch show wch_1\nStop: vibe watch remove wch_1"
    )
    assert (await host.wait(job_id, deadline_s=5)).exit_code == 0
    assert open(path).read() == "working\nfinished\n"
    assert (tmp_path / "counter").read_text() == "run\n"
    assert watches.by_job == {job_id: "wch_1"}


async def test_watch_true_returns_at_once(tmp_path, make_ctx, monkeypatch):
    """At once after the command may run (the 0.5 s grace), however long the launch itself took."""
    host = _host(tmp_path, Watches())
    may_run_at = []
    real_start = host.start

    async def start_and_note(*args, **kwargs):
        job_id = await real_start(*args, **kwargs)
        may_run_at.append(time.monotonic())
        return job_id

    monkeypatch.setattr(host, "start", start_and_note)

    result = await BashTool(host).execute({"command": "sleep 2; echo late", "watch": True}, make_ctx())

    assert time.monotonic() - may_run_at[0] < 1.5
    assert result_text(result).startswith("Command is still running and is now Watch wch_1.")
    assert host.status(result.details["job_id"]).state == "running"
    await host.kill(result.details["job_id"])


async def test_watch_true_keeps_the_result_of_a_command_that_already_ended(tmp_path, make_ctx, monkeypatch):
    """A handover is decided only after a fresh status, so a command that has ended keeps its own result."""
    monkeypatch.setattr(bash_module, "_WATCH_GRACE_S", 30.0)  # the command surely ends within it
    watches = Watches()

    result = await BashTool(_host(tmp_path, watches)).execute({"command": "exit 3", "watch": True}, make_ctx())

    assert result.is_error
    assert result_text(result) == "(no output)\n\nCommand exited with code 3"
    assert watches.by_job == {}


async def test_a_recorded_watch_is_a_handover_even_if_caching_its_id_fails(tmp_path, make_ctx, monkeypatch):
    """hand_over raising means no Watch owns the job; once the Watch exists, the handover stands."""
    watches = Watches()
    host = _host(tmp_path, watches)
    real_write = LocalJobHost._write_meta

    def write_meta(self, job_id, meta):
        if meta.get("watch_id"):
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(self, job_id, meta)

    monkeypatch.setattr(LocalJobHost, "_write_meta", write_meta)
    result = await BashTool(host).execute({"command": "sleep 5", "watch": True}, make_ctx())

    assert result_text(result).startswith("Command is still running and is now Watch wch_1.")
    assert list(watches.by_job.values()) == ["wch_1"]
    await host.kill(result.details["job_id"])


async def test_a_failing_watch_is_tried_again_before_the_command_stays_in_the_foreground(tmp_path, make_ctx):
    calls = []

    async def failing(meta):
        calls.append(meta["job_id"])
        raise RuntimeError("watch store unavailable")

    result = await BashTool(_host(tmp_path, failing)).execute(
        {"command": "sleep 1; echo hi", "watch": True}, make_ctx()
    )

    assert len(calls) == 2 and len(set(calls)) == 1
    assert result_text(result) == (
        "hi\n\n\n[Watch unavailable (watch store unavailable); the command ran in the foreground.]"
    )


async def test_without_a_watch_the_command_stays_in_the_foreground(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "sleep 1; echo hi", "watch": True}, make_ctx())

    assert not result.is_error
    assert result_text(result) == (
        "hi\n\n\n[Watch unavailable (No Watch is available to take over the command.); "
        "the command ran in the foreground.]"
    )


async def test_settlement_reports_each_job_state(tmp_path, make_ctx):
    watches = Watches()
    host = _host(tmp_path, watches)
    # Each call's outcome is fixed by construction, not by timing: the exited one runs with the default
    # window (it cannot be handed over before it ends), the running one never ends.
    foreground = await BashTool(host).execute({"command": "echo out; exit 4"}, make_ctx(tool_call_id="exited"))
    await BashTool(host, foreground_window_s=0.2).execute(
        {"command": "echo still; sleep 30"}, make_ctx(tool_call_id="running")
    )

    recovery = LocalJobHost(str(tmp_path / "jobs"), on_hand_over=watches)

    exited = await settle_bash_call(recovery, "ses_test", "exited")
    assert (result_text(exited), exited.is_error) == (result_text(foreground), True)
    running = await settle_bash_call(recovery, "ses_test", "running")
    assert result_text(running).startswith("Command is still running and is now Watch wch_1.")
    # Settling again adopts the same Watch. (The output may still be arriving, so only the Watch is compared.)
    again = await settle_bash_call(recovery, "ses_test", "running")
    assert result_text(again).startswith("Command is still running and is now Watch wch_1.")
    assert again.details["watch_id"] == running.details["watch_id"] == "wch_1"
    assert await settle_bash_call(recovery, "ses_test", "unknown") is None
    await recovery.kill(running.details["job_id"])
    assert await settle_bash_call(recovery, "ses_test", "running") is None


async def test_a_timeout_holds_across_handover_and_a_restart(tmp_path, make_ctx):
    """J3: the deadline is the job's, so the next owner enforces it."""
    result = await BashTool(_host(tmp_path, Watches())).execute(
        {"command": "echo begun; sleep 30", "timeout": 60, "watch": True}, make_ctx()
    )
    job_id = result.details["job_id"]
    assert result_text(result).startswith("Command is still running")
    restarted = LocalJobHost(str(tmp_path / "jobs"))
    output = restarted.output_path(job_id)
    for _ in range(500):
        if open(output).read() == "begun\n":
            break
        await asyncio.sleep(0.02)
    # The deadline passes now, whatever the launch took; only the restarted host is left to enforce it.
    meta = restarted.meta(job_id)
    meta["deadline_at"] = jobs_module._iso(datetime.now(timezone.utc))
    restarted._write_meta(job_id, meta)

    status = await asyncio.wait_for(restarted.wait(job_id, deadline_s=None), timeout=10)

    assert status.state == "gone"
    assert restarted.stop_reason(job_id) == "timeout"
    settled = await settle_bash_call(restarted, "ses_test", "toolu_1")
    assert result_text(settled) == "begun\n\n\nCommand timed out after 60 seconds"
    assert not os.path.exists(os.path.join(restarted.job_dir(job_id), "exit"))

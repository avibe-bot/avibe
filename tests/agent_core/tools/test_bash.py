"""C-7 ``bash``: Pi's results over a job handle, foreground-to-Watch handover, and settlement (J3)."""

from __future__ import annotations

import asyncio
import os
import time

import psutil

from core.agent_core.cancel import CancelToken
from core.agent_core.tools.bash import BashTool, settle_bash_call
import core.agent_core.tools.jobs as jobs_module
from core.agent_core.tools.jobs import LocalJobHost
from tests.agent_core.tools.conftest import result_text


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
    assert result_text(result).endswith(f"2499\n2500\n\n[Showing lines 501-2500 of 2500. Full output: {path}]")
    assert open(path).read() == "".join(f"{i}\n" for i in range(1, 2501))


async def test_a_log_bounded_on_disk_is_not_called_the_full_output(tmp_path, make_ctx, monkeypatch):
    """J4: the result says the middle is gone instead of promising a complete file."""
    monkeypatch.setattr(jobs_module, "OUTPUT_HEAD_BYTES", 1000)
    monkeypatch.setattr(jobs_module, "OUTPUT_TAIL_BYTES", 2000)

    result = await BashTool(_host(tmp_path)).execute({"command": "seq 1 100000"}, make_ctx())

    text = result_text(result)
    assert text.startswith("1\n2\n3\n")
    assert f"100000\n\n\n[Output log (middle omitted beyond 2.9KB): {result.details['output_path']}]" in text
    assert "Full output" not in text
    assert result.details["omitted_bytes"] == len("".join(f"{i}\n" for i in range(1, 100001))) - 3000


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
    assert result_text(result) == "started\n\n\nCommand aborted"
    assert _process_gone(int((tmp_path / "sh.pid").read_text()))


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
        f"\n\nOutput log (middle omitted beyond 3.0MB): {path}\nCheck: vibe watch show wch_1\nStop: vibe watch remove wch_1"
    )
    assert (await host.wait(job_id, deadline_s=5)).exit_code == 0
    assert open(path).read() == "working\nfinished\n"
    assert (tmp_path / "counter").read_text() == "run\n"
    assert watches.by_job == {job_id: "wch_1"}


async def test_watch_true_returns_at_once(tmp_path, make_ctx):
    host = _host(tmp_path, Watches())
    started = time.monotonic()

    result = await BashTool(host).execute({"command": "sleep 2; echo late", "watch": True}, make_ctx())

    assert time.monotonic() - started < 1.5
    assert result_text(result).startswith("Command is still running and is now Watch wch_1.")
    assert host.status(result.details["job_id"]).state == "running"
    await host.kill(result.details["job_id"])


async def test_watch_true_keeps_the_result_of_a_command_that_already_ended(tmp_path, make_ctx):
    watches = Watches()

    result = await BashTool(_host(tmp_path, watches)).execute({"command": "exit 3", "watch": True}, make_ctx())

    assert result.is_error
    assert result_text(result) == "(no output)\n\nCommand exited with code 3"
    assert watches.by_job == {}


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
    tool = BashTool(host, foreground_window_s=0.2)
    foreground = await tool.execute({"command": "echo out; exit 4"}, make_ctx(tool_call_id="exited"))
    await tool.execute({"command": "echo still; sleep 30"}, make_ctx(tool_call_id="running"))

    recovery = LocalJobHost(str(tmp_path / "jobs"), on_hand_over=watches)

    exited = await settle_bash_call(recovery, "ses_test", "exited")
    assert (result_text(exited), exited.is_error) == (result_text(foreground), True)
    running = await settle_bash_call(recovery, "ses_test", "running")
    assert result_text(running).startswith("Command is still running and is now Watch wch_1.")
    assert result_text(await settle_bash_call(recovery, "ses_test", "running")) == result_text(running)
    assert await settle_bash_call(recovery, "ses_test", "unknown") is None
    await recovery.kill(running.details["job_id"])
    assert await settle_bash_call(recovery, "ses_test", "running") is None


async def test_a_timeout_holds_across_handover_and_a_restart(tmp_path, make_ctx):
    """J3: the deadline is the job's, so the next owner enforces it."""
    result = await BashTool(_host(tmp_path, Watches())).execute(
        {"command": "echo begun; sleep 30", "timeout": 1.5, "watch": True}, make_ctx()
    )
    job_id = result.details["job_id"]
    assert result_text(result).startswith("Command is still running")

    restarted = LocalJobHost(str(tmp_path / "jobs"))
    status = await asyncio.wait_for(restarted.wait(job_id, deadline_s=None), timeout=5)

    assert status.state == "gone"
    assert restarted.stop_reason(job_id) == "timeout"
    settled = await settle_bash_call(restarted, "ses_test", "toolu_1")
    assert result_text(settled) == "begun\n\n\nCommand timed out after 1.5 seconds"
    assert not os.path.exists(os.path.join(restarted.job_dir(job_id), "exit"))

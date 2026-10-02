"""C-7 ``bash``: Pi's results over a job handle, foreground-to-Watch handover, and settlement (J3)."""

from __future__ import annotations

import asyncio
import os
import time

import psutil

from core.agent_core.cancel import CancelToken
from core.agent_core.tools.bash import BashTool, settle_bash_call
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

    path = result.details["full_output_path"]
    assert not result.is_error
    assert result_text(result).endswith(f"2499\n2500\n\n[Showing lines 501-2500 of 2500. Full output: {path}]")
    assert open(path).read() == "".join(f"{i}\n" for i in range(1, 2501))


async def test_a_non_zero_exit_is_an_error_result(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "echo oops >&2; exit 3"}, make_ctx())

    assert result.is_error
    assert result_text(result) == "oops\n\n\nCommand exited with code 3"


async def test_stdin_is_closed(tmp_path, make_ctx):
    started = time.monotonic()
    result = await BashTool(_host(tmp_path)).execute({"command": 'read x; echo "rc=$?"'}, make_ctx())

    assert result_text(result) == "rc=1\n"
    assert time.monotonic() - started < 5


async def test_timeout_kills_the_tree_including_background_children(tmp_path, make_ctx):
    command = f"sleep 30 & echo $! > {tmp_path / 'bg.pid'}; echo started; sleep 30"

    result = await BashTool(_host(tmp_path)).execute({"command": command, "timeout": 0.5}, make_ctx())

    assert result.is_error
    assert result_text(result) == "started\n\n\nCommand timed out after 0.5 seconds"
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
    command = f"echo run >> {tmp_path / 'counter'}; echo working; sleep 1; echo finished"

    result = await BashTool(host, foreground_window_s=0.3).execute({"command": command}, make_ctx())

    job_id = result.details["job_id"]
    path = host.output_path(job_id)
    assert not result.is_error
    assert result_text(result) == (
        "Command is still running and is now Watch wch_1. You will get a follow-up message when it finishes.\n\n"
        f"working\n\nFull output: {path}\nCheck: vibe watch show wch_1\nStop: vibe watch remove wch_1"
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


async def test_without_a_watch_the_command_stays_in_the_foreground(tmp_path, make_ctx):
    result = await BashTool(_host(tmp_path)).execute({"command": "echo hi", "watch": True}, make_ctx())

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
        {"command": "echo begun; sleep 30", "timeout": 0.5, "watch": True}, make_ctx()
    )
    job_id = result.details["job_id"]
    assert result_text(result).startswith("Command is still running")

    restarted = LocalJobHost(str(tmp_path / "jobs"))
    status = await asyncio.wait_for(restarted.wait(job_id, deadline_s=None), timeout=5)

    assert status.state == "gone"
    assert restarted.stop_reason(job_id) == "timeout"
    settled = await settle_bash_call(restarted, "ses_test", "toolu_1")
    assert result_text(settled) == "begun\n\n\nCommand timed out after 0.5 seconds"
    assert not os.path.exists(os.path.join(restarted.job_dir(job_id), "exit"))

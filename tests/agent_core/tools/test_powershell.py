"""The command tool on Windows: ``powershell``, over the job host (C-7 tools.md section 5).

Runs under both PowerShell 7 (``pwsh``, when installed) and Windows PowerShell 5.1, which every Windows
has. The tool's name and description are checked everywhere; running commands needs Windows.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

import psutil
import pytest

from core.agent_core.cancel import CancelToken
from core.agent_core.tools.base import ToolContext
from core.agent_core.tools.bash import BashTool
from core.agent_core.tools.jobs import LocalJobHost
from core.agent_core.tools.shell import Shell, read_line_advice
from tests.agent_core.tools.conftest import instance, result_text

WINDOWS = os.name == "nt"
ON_WINDOWS = pytest.mark.skipif(not WINDOWS, reason="PowerShell runs on Windows")


def _shells():
    if not WINDOWS:
        return []
    root = os.environ.get("SystemRoot", r"C:\Windows")
    found = [pytest.param(os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), id="5.1")]
    if shutil.which("pwsh"):
        found.append(pytest.param(shutil.which("pwsh"), id="7"))
    return found


@pytest.fixture(params=_shells())
def shell(request):
    return Shell(request.param, powershell=True)


@pytest.fixture
async def host(tmp_path, shell):
    """A job host over PowerShell; every job it started is ended with the test."""
    host = LocalJobHost(str(tmp_path / "jobs"), shell=shell)
    yield host
    for job_id in host._job_ids():
        await host.kill(job_id)


@pytest.fixture
def run(tmp_path, host):
    """Run a command through the powershell tool; return ``(is_error, text)``."""
    tool = BashTool(host, shell=host.shell)

    async def run_command(command, **arguments):
        ctx = ToolContext(
            session_id="ses_test",
            tool_call_id="toolu_1",
            call=instance(),
            cwd=str(tmp_path),
            env=dict(os.environ),
            cancel=CancelToken(),
            on_progress=None,
        )
        result = await tool.execute({"command": command, **arguments}, ctx)
        return result.is_error, result_text(result)

    return run_command


@pytest.mark.parametrize(
    ("path", "name", "legacy"),
    [
        (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "powershell", True),
        (r"C:\Program Files\PowerShell\7\pwsh.exe", "powershell", False),
        ("/bin/bash", "bash", False),
    ],
)
def test_the_tool_is_named_after_its_shell(path, name, legacy):
    shell = Shell(path, powershell=name == "powershell")
    spec = BashTool(object(), shell=shell).spec

    assert spec.name == name
    assert spec.description.startswith(
        "Execute a PowerShell command in the current working directory. Returns its output and errors."
        if name == "powershell"
        else "Execute a bash command in the current working directory. Returns stdout and stderr."
    )
    assert ("This is Windows PowerShell 5.1: it has no && or ||" in spec.description) is legacy


@ON_WINDOWS
@pytest.mark.parametrize(
    ("command", "is_error", "ending"),
    [
        ("Write-Output ok", False, "ok"),
        ("cmd /c exit 3", True, "Command exited with code 3"),
        ("Get-Item C:\\does-not-exist", True, "Command exited with code 1"),
        ("cmd /c exit 3; Write-Output ok", False, "ok"),
        ("Write-Output ok\ncmd /c exit 4", True, "Command exited with code 4"),
        ("exit 5", True, "Command exited with code 5"),
        ("throw 'boom'", True, "Command exited with code 1"),
        ("Write-Output ok\nif (", True, "Command exited with code 1"),
    ],
    ids=["ok", "native-fails", "cmdlet-fails", "fails-then-ok", "ok-then-fails", "exit", "throw", "parse-error"],
)
async def test_the_exit_code_follows_bash(run, command, is_error, ending):
    error, text = await run(command)

    assert (error, text.rstrip().splitlines()[-1]) == (is_error, ending)


@ON_WINDOWS
async def test_a_command_that_does_not_parse_runs_nothing_and_says_why(run, tmp_path):
    error, text = await run(f"Set-Content -Path '{tmp_path / 'ran'}' -Value x\nif (")

    assert error and "Missing condition in if statement" in text
    assert not (tmp_path / "ran").exists()


@ON_WINDOWS
async def test_output_comes_before_an_exit_that_follows_it(run):
    error, text = await run("Get-ChildItem $env:SystemRoot -Filter win.ini | Select-Object Name; exit 2")

    assert error and "win.ini" in text and text.rstrip().endswith("Command exited with code 2")


@ON_WINDOWS
async def test_output_is_utf8_from_powershell_and_from_native_programs(run):
    error, text = await run(
        "Write-Output '中文 ps'\n"
        "cmd /c echo 中文 cmd\n"
        f"& '{sys.executable}' -c \"print('中文 py')\""
    )

    assert not error, text
    assert text.splitlines() == ["中文 ps", "中文 cmd", "中文 py"]


# Runs in a console of its own (CREATE_NO_WINDOW), sets it to code page 936 as Chinese Windows starts one,
# then runs the shell in it, as a job's wrapper would, and hands back the bytes the shell wrote.
_UNDER_CODE_PAGE_936 = """
import ctypes, json, subprocess, sys
ctypes.windll.kernel32.SetConsoleOutputCP(936)
ctypes.windll.kernel32.SetConsoleCP(936)
completed = subprocess.run(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL, capture_output=True)
sys.stdout.write(json.dumps([completed.returncode, completed.stdout.hex(), completed.stderr.hex()]))
"""


@ON_WINDOWS
def test_output_is_utf8_on_a_console_with_a_chinese_code_page(tmp_path, shell):
    """Chinese Windows starts a console at code page 936; the bootstrap makes the output UTF-8 anyway."""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    argv = shell.prepare("Write-Output '中文 ps'\ncmd /c echo 中文 cmd", str(job_dir))
    completed = subprocess.run(
        [sys.executable, "-c", _UNDER_CODE_PAGE_936, json.dumps(argv)],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=60,
    )
    code, stdout, stderr = json.loads(completed.stdout)

    assert code == 0, bytes.fromhex(stderr)
    assert bytes.fromhex(stdout).decode("utf-8").splitlines() == ["中文 ps", "中文 cmd"]


@ON_WINDOWS
async def test_a_command_longer_than_a_command_line_runs(run):
    """A command line holds 32,767 characters; the command reaches PowerShell through a file."""
    error, text = await run("Write-Output ('" + "x" * 40_000 + "').Length")

    assert (error, text.strip()) == (False, "40000")


@ON_WINDOWS
async def test_a_process_started_with_start_process_stays_in_the_job_until_it_is_killed(tmp_path, host):
    job_id = await host.start(
        f"(Start-Process -FilePath '{sys.executable}' -ArgumentList '-c \"import time; time.sleep(60)\"' -PassThru).Id",
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_s=None,
        call=instance(),
    )
    status = await host.wait(job_id, deadline_s=60)
    pid = int(host.output(job_id)[0].decode().strip())

    assert (status.state, status.exit_code) == ("exited", 0)
    assert psutil.pid_exists(pid)
    await host.kill(job_id)
    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(pid)


@ON_WINDOWS
def test_the_advice_read_gives_for_a_long_line_runs_as_given(tmp_path, shell):
    """``read`` points at a command for a line too long to show; it must work whatever the path holds."""
    target = tmp_path / "it's big.txt"
    target.write_bytes(b"short\n" + b"x" * 61_440 + b"\nafter\n")
    advice = read_line_advice(str(target), 2, 51_200).removeprefix("Use powershell: ")

    completed = subprocess.run(
        [shell.path, "-NoProfile", "-NonInteractive", "-Command", advice],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.decode().strip() == "x" * 51_200


@ON_WINDOWS
async def test_the_agent_loop_runs_a_powershell_call_and_answers_with_its_output(tmp_path, host):
    """End to end without a model: a scripted provider calls the tool, and its output reaches the next request."""
    from core.agent_core.agent.loop import Agent
    from core.agent_core.ai.provider import Done
    from core.agent_core.messages import ToolCallBlock, ToolResultMessage
    from core.agent_core.tools.coding import create_coding_tools
    from tests.agent_core.fakes import FakeModelRouter, InMemoryTranscriptStore, ScriptedProvider, assistant, input_row

    call = ToolCallBlock("call_1", "powershell", {"command": "Write-Output ('中文 ' + $PSVersionTable.PSEdition)"})
    provider = ScriptedProvider([[Done(assistant(calls=[call]))], [Done(assistant("done"))]])
    agent = Agent(
        session_id="ses_windows",
        models=FakeModelRouter(provider),
        tools=(),
        hooks=(),
        store=InMemoryTranscriptStore(),
        jobs=host,
        cwd=str(tmp_path),
        env=dict(os.environ),
    )
    agent.set_tools(create_coding_tools(agent.jobs, shell=host.shell))

    [event async for event in agent.run(input_row("input", "run it"), turn_id="turn")]

    assert [spec.name for spec in provider.requests[0].tools] == ["read", "write", "edit", "powershell"]
    [result] = [message for message in provider.requests[1].messages if isinstance(message, ToolResultMessage)]
    edition = "Desktop" if host.shell.windows_powershell else "Core"
    assert (result.is_error, result.content[0].text.strip()) == (False, f"中文 {edition}")

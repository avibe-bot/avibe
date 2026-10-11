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

    async def run_command(command, *, env=None, **arguments):
        ctx = ToolContext(
            session_id="ses_test",
            tool_call_id="toolu_1",
            call=instance(),
            cwd=str(tmp_path),
            env=dict(os.environ) if env is None else env,
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
        ("cmd /c exit 23; Get-Item C:\\does-not-exist", True, "Command exited with code 1"),
        ("cmd /c exit 3; cmd /c exit 3", True, "Command exited with code 3"),
    ],
    ids=[
        "ok",
        "native-fails",
        "cmdlet-fails",
        "fails-then-ok",
        "ok-then-fails",
        "exit",
        "throw",
        "parse-error",
        "native-then-cmdlet-fails",
        "the-same-code-twice",
    ],
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
@pytest.mark.parametrize("flow", ["return", "break", "continue"])
async def test_a_top_level_return_ends_the_whole_script(run, tmp_path, flow):
    """The command is one script: a ``return`` at its top level ends it, and nothing after it runs."""
    marker = tmp_path / "after"
    error, text = await run(
        f"if ($true) {{ Write-Output before; {flow} }}\n"
        f"New-Item -ItemType File -Path '{marker}' | Out-Null\n"
        "Write-Output after"
    )

    assert (error, text.split()) == (False, ["before"])
    assert not marker.exists()


@ON_WINDOWS
async def test_text_comes_before_an_exit_that_follows_it(run):
    """Strings and native programs' output are written as they come; only objects still waiting to be laid out
    as a table can be lost to an ``exit`` (C-7 tools.md section 5, as with ``powershell -Command``)."""
    error, text = await run("Write-Output before; cmd /c echo native; exit 2")

    assert error and text.split()[:2] == ["before", "native"]
    assert text.rstrip().endswith("Command exited with code 2")


@ON_WINDOWS
@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("class Item { [string] $Name = 'item' }\nenum Color { Red }\n[Item]::new().Name; [Color]::Red", "item Red"),
        ("param($Name = 'default')\nWrite-Output $Name", "default"),
        ("using namespace System.Text\n[StringBuilder]::new().Append('built').ToString()", "built"),
        ("begin { Write-Output first }\nprocess { Write-Output each }\nend { Write-Output last }", "first each last"),
        ("process { Write-Output only }", "only"),
    ],
    ids=["types", "param", "using", "named-blocks", "process-only"],
)
async def test_a_command_keeps_powershell_script_semantics(run, command, output):
    """Types are visible across its statements, and a param block, using, or named blocks work as in a script."""
    error, text = await run(command)

    assert (error, " ".join(text.split())) == (False, output)


@ON_WINDOWS
@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("begin { Write-Output first }\nend { Write-Output last; cmd /c exit 4 }", 4),
        ("process { cmd /c exit 7 }", 7),
        ("begin { cmd /c exit 6 }", 6),
        # A function that writes an error and returns has succeeded, as $? says (C-7 tools.md section 5).
        ("function Get-Twice { cmd /c exit 23; Get-Item C:\\does-not-exist }\nGet-Twice", 0),
        # A compound statement is one statement, and it set $LASTEXITCODE.
        ("if ($true) { cmd /c exit 23; Get-Item C:\\does-not-exist }", 23),
        # Blocks may be written in any order; they run begin, process, end.
        ("end { cmd /c exit 4 }\nbegin { Write-Output begin }", 4),
        # The code an earlier block left is not the last statement's, whatever path led to it.
        ("begin { cmd /c exit 23 }\nend { Get-Item C:\\does-not-exist }", 1),
        # ...or parameter binding: a param default runs before the only statement.
        ("param($Ignored = $(cmd /c exit 23))\nGet-Item C:\\does-not-exist", 1),
        # --% makes the rest of its line arguments: nothing may be added there.
        ("cmd.exe --% /c exit 7", 7),
        ("cmd.exe --% /c exit 23\nGet-Item C:\\does-not-exist", 1),
    ],
    ids=[
        "end",
        "process-only",
        "begin-only",
        "a-function-returns",
        "a-compound-statement",
        "blocks-out-of-order",
        "a-code-from-an-earlier-block",
        "a-code-from-a-param-default",
        "stop-parsing-last",
        "stop-parsing-before-the-last",
    ],
)
async def test_the_exit_code_follows_the_statement_that_runs_last(run, command, code):
    error, text = await run(command)

    assert error == bool(code)
    if code:
        assert text.rstrip().endswith(f"Command exited with code {code}")


@ON_WINDOWS
async def test_a_clean_block_runs_last(run, shell, tmp_path):
    """PowerShell 7.3 and later run ``clean`` after the other blocks, so its last statement is the one that counts;
    in a script with no other block, PowerShell runs none of it, and nothing fails."""
    if shell.windows_powershell:
        pytest.skip("Windows PowerShell 5.1 has no clean block")
    marker = tmp_path / "clean-ran"

    after_process = await run("process { Write-Output ok }\nclean { cmd /c exit 5 }")
    alone = await run(f"clean {{ New-Item -ItemType File -Path '{marker}' | Out-Null; cmd /c exit 7 }}")

    assert after_process[0] and after_process[1].rstrip().endswith("Command exited with code 5")
    assert alone[0] is False
    assert not marker.exists()


@ON_WINDOWS
def test_constrained_language_mode_is_refused_with_a_reason(shell, tmp_path):
    """AppLocker and WDAC put PowerShell in ConstrainedLanguage mode, which blocks the bootstrap's .NET calls. A
    session can enter that mode itself, which stands in for the policy here."""
    marker = tmp_path / "ran"
    argv = shell.prepare(f"New-Item -ItemType File -Path '{marker}' | Out-Null", str(tmp_path))
    argv[-1] = "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'\n" + argv[-1]

    completed = subprocess.run(
        argv, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60
    )

    assert completed.returncode == 1
    assert "ConstrainedLanguage mode" in completed.stderr and "Nothing was run" in completed.stderr, completed.stderr
    assert not marker.exists()


@ON_WINDOWS
async def test_a_clean_block_after_an_early_return_reports_its_own_failure(run, shell):
    """``clean`` runs even after ``return``; the native code ``begin`` left is not its last statement's."""
    if shell.windows_powershell:
        pytest.skip("Windows PowerShell 5.1 has no clean block")
    error, text = await run("begin { cmd /c exit 23; return }\nclean { Get-Item C:\\does-not-exist }")

    assert error and text.rstrip().endswith("Command exited with code 1"), text


@ON_WINDOWS
async def test_a_dynamicparam_block_counts_when_it_runs(run, tmp_path):
    """It runs first, during parameter binding; if it is the only block, its last statement is the one that counts."""
    marker = tmp_path / "dynamicparam-ran"
    error, text = await run(
        f"dynamicparam {{ New-Item -ItemType File -Path '{marker}' | Out-Null; cmd /c exit 7 }}"
    )

    if marker.exists():
        assert error and text.rstrip().endswith("Command exited with code 7"), text
    else:
        assert not error, text


@ON_WINDOWS
@pytest.mark.parametrize(
    ("command", "is_error", "ending"),
    [
        ("Set-StrictMode -Version Latest\nWrite-Output ok", False, "ok"),
        ("Set-StrictMode -Version Latest\nWrite-Output ok; cmd /c exit 3", True, "Command exited with code 3"),
    ],
    ids=["ok", "native-fails"],
)
async def test_a_command_may_turn_strict_mode_on(run, command, is_error, ending):
    """``$LASTEXITCODE`` does not exist until something sets it, and strict mode refuses to read it then."""
    error, text = await run(command)

    assert (error, text.rstrip().splitlines()[-1]) == (is_error, ending)


@ON_WINDOWS
async def test_the_command_sees_lastexitcode_as_powershell_sets_it(run):
    """Watching which statement set ``$LASTEXITCODE`` leaves its value, and its type, as they are."""
    error, text = await run("cmd /c exit 7\n$LASTEXITCODE.GetType().Name; $LASTEXITCODE")

    assert (error, text.split()) == (False, ["Int32", "7"])


@ON_WINDOWS
async def test_an_error_is_reported_at_the_command_s_own_line_and_column(run):
    """The wrapping adds no line, so the model is pointed at the line it wrote (in the view that shows it)."""
    error, text = await run("$ErrorView = 'NormalView'\nWrite-Output first\n  throw 'boom'")

    assert error
    assert "At line:3 char:3" in text, text


@ON_WINDOWS
async def test_output_is_utf8_from_powershell_and_from_native_programs(run):
    error, text = await run(
        "Write-Output '中文 ps'\n"
        "cmd /c echo 中文 cmd\n"
        f"& '{sys.executable}' -c \"import sys; print('中文 py', sys.flags.utf8_mode)\""
    )

    # Only Python's standard streams are made UTF-8: the files it opens keep the system's encoding.
    utf8_mode = 1 if os.environ.get("PYTHONUTF8") == "1" else 0
    assert not error, text
    assert text.splitlines() == ["中文 ps", "中文 cmd", f"中文 py {utf8_mode}"]


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
@pytest.mark.parametrize(
    ("content", "line", "expected"),
    [
        (b"short\n" + b"x" * 61_440 + b"\nafter\n", 2, "x" * 51_200),
        (b"x" * 61_440, 1, "x" * 51_200),
        # UTF-8 without a BOM, which Windows PowerShell would read as ANSI; 51,200 bytes end on a character.
        (b"ab" + "\u4e2d".encode() * 30_000 + b"\n", 1, "ab" + "\u4e2d" * 17_066),
        # ...and inside one, which is left out whole.
        (b"a" + "\u4e2d".encode() * 30_000, 1, "a" + "\u4e2d" * 17_066),
    ],
    ids=["a later line", "the only line", "cjk", "cjk-cut-inside-a-character"],
)
async def test_the_advice_read_gives_for_a_long_line_runs_as_given(tmp_path, run, content, line, expected):
    """``read`` points at a command for a line too long to show. Run through the tool, it prints what ``sed`` and
    ``head -c`` would: that line as UTF-8, at most 51,200 bytes of it, whatever the path holds; line 1 too, which
    ``Get-Content`` alone gives as a string rather than a list of lines."""
    target = tmp_path / "it's big.txt"
    target.write_bytes(content)
    advice = read_line_advice(str(target), line, 51_200).removeprefix("Use powershell: ")

    error, text = await run(advice)

    assert not error, text[-300:]
    assert text.encode() == expected.encode()


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

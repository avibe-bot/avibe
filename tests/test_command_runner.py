"""Tests for the shared supervised-command runner."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from core import watch_worker
from core.command_runner import (
    STREAM_TRUNCATION_MARKER,
    SupervisedCommandResult,
    SupervisedCommandStartupError,
    run_supervised_command,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.payload = bytearray()
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payload.extend(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _BrokenStdin(_FakeStdin):
    async def drain(self) -> None:
        raise BrokenPipeError


class _FakeProcess:
    def __init__(
        self,
        *,
        stdin: _FakeStdin | None = None,
        stderr: bytes = b"",
    ) -> None:
        self.pid = 1234
        self.returncode = 0
        self.stdin = stdin or _FakeStdin()
        self.stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok\n", self.stderr


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_argv_command_success_reports_streams_and_exit_code(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(0)",
        ],
        cwd=str(tmp_path),
        timeout_seconds=10,
        label="test argv",
    )

    assert isinstance(result, SupervisedCommandResult)
    assert result.exit_code == 0
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert result.timed_out is False
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


async def test_shell_command_success_preserves_non_zero_exit_code(tmp_path: Path) -> None:
    result = await run_supervised_command(
        shell_command="echo hi; exit 7",
        cwd=str(tmp_path),
        timeout_seconds=10,
        label="test shell",
    )

    assert result.exit_code == 7
    assert "hi" in result.stdout
    assert result.timed_out is False


async def test_timeout_terminates_child_and_maps_exit_code_124(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        timeout_seconds=1,
        label="test timeout",
    )

    assert result.timed_out is True
    assert result.exit_code == 124
    assert result.stdout == ""


async def test_timeout_keeps_the_output_the_capped_readers_already_retained(
    tmp_path: Path,
) -> None:
    """SCT-006 -- a timed-out command keeps what it printed before it hung.

    The capped readers hold the retained bytes in their own buffers, so cancelling
    them on timeout threw away exactly the diagnostic the user needs to understand
    why the command hung -- and a timeout is the failure mode where that output
    matters most, because there is no exit status to explain it.

    Only the capped path is asserted here. Watches pass ``max_output_bytes=None``
    and keep ``communicate()`` semantics, guarded by
    ``test_timeout_terminates_child_and_maps_exit_code_124`` above.
    """

    result = await run_supervised_command(
        shell_command="echo diagnostic; sleep 30",
        cwd=str(tmp_path),
        timeout_seconds=1,
        label="test timeout output",
        max_output_bytes=64 * 1024,
    )

    assert result.timed_out is True
    assert result.exit_code == 124
    assert "diagnostic" in result.stdout


async def test_zero_timeout_means_no_timeout(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[sys.executable, "-c", "import time; time.sleep(0.2); print('done')"],
        cwd=str(tmp_path),
        timeout_seconds=0,
        label="test no timeout",
    )

    assert result.timed_out is False
    assert result.exit_code == 0
    assert "done" in result.stdout


async def test_on_spawn_is_called_once_before_the_result_returns(tmp_path: Path) -> None:
    spawns: list[tuple[int, object]] = []

    def on_spawn(pid: int, identity: object) -> None:
        spawns.append((pid, identity))

    result = await run_supervised_command(
        command=[sys.executable, "-c", "print('ok')"],
        cwd=str(tmp_path),
        timeout_seconds=10,
        label="test on_spawn",
        on_spawn=on_spawn,
    )

    assert result.exit_code == 0
    assert len(spawns) == 1
    pid, identity = spawns[0]
    assert pid > 0
    assert identity is None or identity.pid == pid


async def test_cancellation_propagates_and_kills_the_child(tmp_path: Path) -> None:
    spawned: list[int] = []

    task = asyncio.ensure_future(
        run_supervised_command(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path),
            timeout_seconds=0,
            label="test cancel",
            on_spawn=lambda pid, _identity: spawned.append(pid),
        )
    )
    for _ in range(200):
        if spawned:
            break
        await asyncio.sleep(0.02)
    assert spawned, "the supervisor never spawned"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pid = spawned[0]
    for _ in range(100):
        if not _pid_alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(pid)


async def test_startup_pipe_break_raises_startup_error_with_raw_detail(tmp_path: Path, monkeypatch) -> None:
    process = _FakeProcess(
        stdin=_BrokenStdin(),
        stderr=watch_worker.encode_watch_worker_error("invalidCommand").encode() + b"\n",
    )

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(SupervisedCommandStartupError) as excinfo:
        await run_supervised_command(
            command=[sys.executable, "-c", "print('ok')"],
            cwd=str(tmp_path),
            timeout_seconds=5,
            label="test startup",
        )

    assert excinfo.value.detail == watch_worker.encode_watch_worker_error("invalidCommand")
    assert process.stdin.closed is True


async def test_spawn_uses_the_stable_supervisor_entrypoint(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await run_supervised_command(
        command=["python3", "-c", "print('ok')"],
        cwd=str(tmp_path),
        timeout_seconds=5,
        label="test argv shape",
    )

    assert result.exit_code == 0
    assert captured["args"] == (
        os.path.abspath(sys.executable),
        str(Path(watch_worker.__file__).resolve()),
    )
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE


_LARGE_OUTPUT_COMMAND = (
    "import sys; sys.stdout.write('x' * 300000); sys.stdout.flush(); sys.exit(0)"
)


async def test_output_cap_truncates_but_keeps_draining(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[sys.executable, "-c", _LARGE_OUTPUT_COMMAND],
        cwd=str(tmp_path),
        timeout_seconds=20,
        label="test cap",
        max_output_bytes=65536,
    )

    assert result.timed_out is False
    assert result.exit_code == 0
    assert len(result.stdout.encode("utf-8")) <= 65536
    assert result.stdout_truncated is True
    assert result.stderr_truncated is False


async def test_output_cap_keeps_the_tail_where_the_cause_usually_is(tmp_path: Path) -> None:
    """SCT-015 -- a capped stream must retain its END, not only its beginning.

    Every consumer of a failed command's output reads the tail: the notice and the CLI
    list show ``_last_nonempty_line`` of stderr, because a failure's explanation is the
    last thing written (a traceback's exception line, a shell's "command not found").
    A head-only cap makes that read a lie -- it reports the last line of the first 64 KiB
    of a chatty command and drops the sentence that says why the run failed, with no
    sign that the real ending was ever there.

    Head is kept too: the first error in a build log is at the top. The gap between them
    is marked, and the tail starts on a line boundary, so no consumer can mistake a
    spliced fragment for a line the command actually printed.
    """

    result = await run_supervised_command(
        command=[
            sys.executable,
            "-c",
            (
                "import sys\n"
                "sys.stderr.write('noise line\\n' * 40000)\n"
                "sys.stderr.write('FINAL: the cause of the failure\\n')\n"
            ),
        ],
        cwd=str(tmp_path),
        timeout_seconds=20,
        label="test cap tail",
        max_output_bytes=65536,
    )

    assert result.exit_code == 0
    assert result.stderr_truncated is True
    assert len(result.stderr.encode("utf-8")) <= 65536, (
        "the cap stays a hard cap on the returned bytes, marker included"
    )
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines[-1] == "FINAL: the cause of the failure", (
        f"the real last line is what the notice reads: {lines[-3:]}"
    )
    assert lines[0] == "noise line", f"the head is retained as whole lines too: {lines[:2]}"
    assert STREAM_TRUNCATION_MARKER.decode().strip() in result.stderr, (
        "the dropped middle has to be visible, not a silent splice"
    )
    assert set(lines) <= {
        "noise line",
        "FINAL: the cause of the failure",
        STREAM_TRUNCATION_MARKER.decode().strip(),
    }, "no spliced fragment may appear as a line the command never printed"


async def test_output_cap_leaves_small_output_untruncated(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[sys.executable, "-c", "print('small')"],
        cwd=str(tmp_path),
        timeout_seconds=20,
        label="test cap small",
        max_output_bytes=65536,
    )

    assert result.exit_code == 0
    assert "small" in result.stdout
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


async def test_no_cap_returns_large_output_in_full(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[sys.executable, "-c", _LARGE_OUTPUT_COMMAND],
        cwd=str(tmp_path),
        timeout_seconds=20,
        label="test uncapped",
        max_output_bytes=None,
    )

    assert result.exit_code == 0
    assert len(result.stdout.encode("utf-8")) == 300000
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False

"""Tests for the shared supervised-command runner."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from core import command_runner, watch_worker
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


def _pid_exited(pid: int) -> bool:
    """Dead as far as these tests care: exited, whether or not it has been waited on.

    The kill fallback in ``_reap_or_kill`` deliberately does NOT await
    ``communicate()`` -- a failed drain is exactly what put it there -- so the exited
    supervisor can sit as a zombie for the moment it takes asyncio's child watcher to
    reap it, and ``os.kill(pid, 0)`` succeeds for a zombie. Where ``/proc`` is
    unavailable this degrades to the liveness check alone.
    """

    if not _pid_alive(pid):
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
    except OSError:
        return False
    return bool(fields) and fields[0] == b"Z"


async def _wait_until_exited(pid: int, *, attempts: int = 100) -> bool:
    for _ in range(attempts):
        if _pid_exited(pid):
            return True
        await asyncio.sleep(0.05)
    return False


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


async def test_an_unexpected_error_after_the_spawn_still_reaps_the_command(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-035 -- cancellation and timeout are not the only ways out of a run.

    Those two ways out reap the tree; every other exception left it running. An
    ``OSError`` draining a capped pipe, one reader dying while its twin still reads,
    an unexpected fault anywhere between the spawn and the collector -- the exception
    propagated with the supervisor and the backup or migration under it untouched.

    That is the unrecoverable case, not merely an untidy one: ``on_spawn`` has already
    handed the caller the worker's identity, and the scheduled-task caller clears it in
    its own ``finally`` the moment this raises, on the documented assumption that the
    runner has reaped the tree by then. So the orphan is left with nothing on disk
    naming it, and the next fire runs a second copy beside it.
    """

    spawned: list[int] = []

    async def _explode(*_args, **_kwargs):
        raise OSError("the pipe went away")

    monkeypatch.setattr(command_runner, "_read_capped_stream", _explode)

    with pytest.raises(OSError, match="the pipe went away"):
        await run_supervised_command(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path),
            timeout_seconds=0,
            label="test unexpected failure",
            on_spawn=lambda pid, _identity: spawned.append(pid),
            # The capped path, because that is where the readers -- and the realistic
            # unexpected failure -- live.
            max_output_bytes=65536,
        )

    assert spawned, "the supervisor never spawned, so nothing could be orphaned"
    pid = spawned[0]
    for _ in range(100):
        if not _pid_alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_alive(pid), (
        "an unexpected runner error left the supervisor and its command running while "
        "the caller was clearing the only record of them"
    )


async def test_a_teardown_that_cannot_drain_still_kills_the_command(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-045 -- the two specialized handlers survive their OWN teardown failing.

    Cancellation and timeout each reap the tree by draining it, and a drain can fail:
    an ``OSError`` on a pipe, a ``RuntimeError`` from a loop already closing. Both
    handlers are ``except`` clauses, so an exception raised out of one is not caught by
    the catch-all clause beside it -- the fix in SCT-035 does not cover them. The frame
    then left with the supervisor still running, while the scheduled-task caller cleared
    the worker record in its own ``finally``: the same orphan-with-no-name as SCT-035,
    reached through the paths that were supposed to be the safe ones.

    Both handlers are driven here because they fail differently: cancellation owes the
    caller a ``CancelledError``, a timeout owes it a 124 result, and neither may be
    replaced by the teardown's own error.
    """

    async def _explode(*_args, **_kwargs):
        raise OSError("the pipe went away")

    monkeypatch.setattr(command_runner, "terminate_and_communicate", _explode)

    timed_out_spawns: list[int] = []
    result = await run_supervised_command(
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        timeout_seconds=1,
        label="test timeout teardown failure",
        on_spawn=lambda pid, _identity: timed_out_spawns.append(pid),
    )

    assert result.timed_out is True
    assert result.exit_code == 124
    assert timed_out_spawns, "the supervisor never spawned"
    assert await _wait_until_exited(timed_out_spawns[0]), (
        "a timeout whose drain failed left the supervisor and its command running"
    )

    canceled_spawns: list[int] = []
    task = asyncio.ensure_future(
        run_supervised_command(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path),
            timeout_seconds=0,
            label="test cancel teardown failure",
            on_spawn=lambda pid, _identity: canceled_spawns.append(pid),
        )
    )
    for _ in range(200):
        if canceled_spawns:
            break
        await asyncio.sleep(0.02)
    assert canceled_spawns, "the supervisor never spawned"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _wait_until_exited(canceled_spawns[0]), (
        "a cancellation whose drain failed left the supervisor and its command running"
    )


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


async def test_spawn_can_discard_supervisor_stderr(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*_args, **kwargs):
        captured.update(kwargs)
        process = _FakeProcess(stderr=None)
        stdout = asyncio.StreamReader()
        stdout.feed_data(b"ok\n")
        stdout.feed_eof()
        process.stdout = stdout

        async def wait() -> int:
            return 0

        process.wait = wait
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await run_supervised_command(
        command=["python3", "-c", "print('ok')"],
        cwd=str(tmp_path),
        timeout_seconds=5,
        label="test discarded stderr",
        max_output_bytes=1024,
        discard_stderr=True,
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert captured["stderr"] == asyncio.subprocess.DEVNULL


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


async def test_extra_env_reaches_the_child_without_replacing_its_environment(tmp_path: Path) -> None:
    """Callers can name the child's context without rebuilding the whole env.

    The supervisor owns the rest of the spawn environment (PATH, the isolation
    marker), so an extra variable is merged into it rather than passed instead of it.
    """
    result = await run_supervised_command(
        command=[
            sys.executable,
            "-c",
            "import os; print(os.environ.get('AVIBE_WATCH_ID', 'missing')); print(bool(os.environ.get('PATH')))",
        ],
        cwd=str(tmp_path),
        timeout_seconds=10,
        label="test extra env",
        extra_env={"AVIBE_WATCH_ID": "wat_123"},
    )

    assert result.exit_code == 0
    assert "wat_123" in result.stdout
    assert "True" in result.stdout


async def test_remove_env_deletes_inherited_and_extra_keys(tmp_path: Path) -> None:
    result = await run_supervised_command(
        command=[
            sys.executable,
            "-c",
            "import os; print('DROP_ME' in os.environ); print(os.environ.get('KEEP_ME'))",
        ],
        cwd=str(tmp_path),
        timeout_seconds=10,
        label="test removed env",
        extra_env={"DROP_ME": "secret", "KEEP_ME": "present"},
        remove_env={"DROP_ME"},
    )

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["False", "present"]

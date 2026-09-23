"""A spawn either hands its caller a child the caller owns, or leaves no child.

Each test starts a real, long-sleeping child and makes one step between
``Popen`` and the handover fail. Before the rule, every one of them left that
child running with nothing recording it: no pid file, no ``start_info``, so no
rollback could find it. The assertion is on the process itself -- killed and
reaped -- not on a mock having been called.
"""

from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path

import pytest

from vibe import runtime

MARKER = "avibe-spawn-ownership-probe"


class _Spawned(list):
    """The probe children, plus the log sinks started for them."""

    def __init__(self) -> None:
        super().__init__()
        self.sinks: list[subprocess.Popen] = []


@pytest.fixture
def spawned(monkeypatch, tmp_path: Path):
    """Every real child started with the probe marker, still inspectable afterwards.

    The spy is a real ``Popen`` subclass, so the log sinks and the child are
    started exactly as in production; it only keeps a handle the code under test
    would otherwise have dropped with its exception.
    """

    monkeypatch.setattr(runtime.paths, "get_runtime_dir", lambda: tmp_path / "runtime")
    children = _Spawned()
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            super().__init__(args, *rest, **kwargs)
            if any(MARKER in str(argument) for argument in args):
                children.append(self)
            elif "vibe.log_sink" in args:
                children.sinks.append(self)

    monkeypatch.setattr(runtime.subprocess, "Popen", SpyPopen)
    yield children
    for child in [*children, *children.sinks]:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _sleeper() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)", MARKER]


def _assert_killed_and_reaped(children: list[subprocess.Popen]) -> None:
    assert len(children) == 1, f"expected exactly one probe child, got {children}"
    child = children[0]
    assert child.returncode is not None, f"pid={child.pid} was left running after its spawn failed"
    assert not runtime.pid_alive(child.pid)


def _assert_log_sinks_released(spawned: _Spawned) -> None:
    """Every parent-side handle was closed, so each sink saw end-of-file and exited."""

    assert len(spawned.sinks) == 2, f"expected the two log sinks, got {spawned.sinks}"
    for sink in spawned.sinks:
        assert sink.wait(timeout=10) is not None


def test_a_child_whose_pid_record_cannot_be_written_does_not_survive(spawned, tmp_path: Path) -> None:
    # A real write failure: the record's directory does not exist.
    pid_path = tmp_path / "missing" / "vibe-ui.pid"

    with pytest.raises(FileNotFoundError):
        runtime.spawn_background(_sleeper(), pid_path, "ui_stdout.log", "ui_stderr.log")

    _assert_killed_and_reaped(spawned)
    assert not pid_path.exists()
    _assert_log_sinks_released(spawned)


@pytest.mark.parametrize("primitive", ["spawn_background", "spawn_service_background_process"])
def test_a_child_whose_secret_cannot_be_delivered_does_not_survive(
    spawned, monkeypatch, tmp_path: Path, primitive: str
) -> None:
    def broken_pipe(process, *, memory_ui_secret):
        raise BrokenPipeError(errno.EPIPE, "injected: the child closed stdin before the secret")

    monkeypatch.setattr(runtime, "_spawn_stdin", broken_pipe)
    pid_path = tmp_path / "vibe-ui.pid"

    with pytest.raises(BrokenPipeError):
        if primitive == "spawn_background":
            runtime.spawn_background(
                _sleeper(), pid_path, "ui_stdout.log", "ui_stderr.log", memory_ui_secret="secret"
            )
        else:
            runtime.spawn_service_background_process(
                _sleeper(), "service_stdout.log", "service_stderr.log", memory_ui_secret="secret"
            )

    _assert_killed_and_reaped(spawned)
    # The record is written after the secret, so a failed delivery never names
    # a process that was then killed.
    assert not pid_path.exists()
    _assert_log_sinks_released(spawned)


class _HandleThatFailsToClose:
    """A parent-side handle whose close releases the descriptor, then raises.

    That is what a buffered file does when its final flush fails: the
    descriptor is gone, the error still propagates. ``Popen`` only needs
    ``fileno()`` from it.
    """

    def __init__(self, real) -> None:
        self._real = real

    def fileno(self) -> int:
        return self._real.fileno()

    def close(self) -> None:
        already_closed = self._real.closed
        self._real.close()
        if not already_closed:
            raise OSError(errno.EIO, "injected: closing the parent's spawn handle failed")


@pytest.mark.parametrize("failing_handle", ["stdin", "stdout_sink", "stderr_sink"])
def test_a_child_whose_parent_handles_cannot_be_closed_does_not_survive(
    spawned, monkeypatch, tmp_path: Path, failing_handle: str
) -> None:
    real_sinks = runtime._spawn_runtime_log_sinks  # noqa: SLF001

    def sinks_with_one_failing_close(stdout_path, stderr_path):
        stdout_sink, stderr_sink = real_sinks(stdout_path, stderr_path)
        target = {"stdout_sink": stdout_sink, "stderr_sink": stderr_sink}.get(failing_handle)
        if target is not None:
            target.stdin = _HandleThatFailsToClose(target.stdin)
        return stdout_sink, stderr_sink

    monkeypatch.setattr(runtime, "_spawn_runtime_log_sinks", sinks_with_one_failing_close)
    if failing_handle == "stdin":
        real_open = open

        def open_with_failing_devnull(file, *rest, **kwargs):
            handle = real_open(file, *rest, **kwargs)
            return _HandleThatFailsToClose(handle) if file == runtime.os.devnull else handle

        monkeypatch.setattr(runtime, "open", open_with_failing_devnull, raising=False)
    pid_path = tmp_path / "vibe-ui.pid"

    with pytest.raises(OSError) as raised:
        runtime.spawn_background(_sleeper(), pid_path, "ui_stdout.log", "ui_stderr.log")

    assert raised.value.errno == errno.EIO
    _assert_killed_and_reaped(spawned)
    # The handles close before the record, so a failed close never names a
    # process that was then killed.
    assert not pid_path.exists()
    _assert_log_sinks_released(spawned)


def test_a_service_whose_reservation_cannot_be_written_does_not_survive(
    spawned, monkeypatch, tmp_path: Path
) -> None:
    launcher = _marked_service(tmp_path, monkeypatch)

    def disk_full(pid: int) -> None:
        raise OSError(errno.ENOSPC, "injected: no space left for the service reservation")

    monkeypatch.setattr(runtime, "_record_service_pid_reservation", disk_full)
    start_info = runtime.ProcessStartInfo()

    with pytest.raises(OSError) as raised:
        runtime.start_service(wait_for_ready=False, start_info=start_info, launcher=launcher)

    assert raised.value.errno == errno.ENOSPC
    _assert_killed_and_reaped(spawned)
    # Nothing is recorded as created, and nothing is left for a reaper thread.
    assert start_info.pid is None
    assert spawned[0].pid not in runtime._SERVICE_START_PROCESSES  # noqa: SLF001


class _InterruptedCapture(runtime.ProcessStartInfo):
    """A signal that lands after the child's record is written, before its capture.

    It notes what the record said at that moment, so a test can show the
    intrusion came where it was aimed rather than earlier.
    """

    def __init__(self, record: Path) -> None:
        super().__init__()
        self.record = record
        self.record_at_intrusion: str | None = None

    def capture(self, pid: int, *, reused: bool) -> int:
        self.record_at_intrusion = self.record.read_text(encoding="utf-8")
        raise KeyboardInterrupt


def _marked_interpreter(tmp_path: Path) -> Path:
    """A stand-in UI interpreter that only sleeps, named so the spy can see it."""

    interpreter = tmp_path / f"{MARKER}-python"
    interpreter.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    interpreter.chmod(0o755)
    return interpreter


def test_a_ui_interrupted_between_its_record_and_its_capture_does_not_survive(spawned, tmp_path: Path) -> None:
    launcher = runtime.ServiceLauncher(python=str(_marked_interpreter(tmp_path)), main="unused")
    pid_path = runtime.paths.get_runtime_ui_pid_path()
    start_info = _InterruptedCapture(pid_path)

    with pytest.raises(KeyboardInterrupt):
        runtime.start_ui("127.0.0.1", 5123, wait_for_ready=False, start_info=start_info, launcher=launcher)

    _assert_killed_and_reaped(spawned)
    assert start_info.record_at_intrusion == str(spawned[0].pid)
    # The record goes with the child, and nothing was captured as created.
    assert not pid_path.exists()
    assert start_info.pid is None
    _assert_log_sinks_released(spawned)


def _marked_service(tmp_path: Path, monkeypatch) -> runtime.ServiceLauncher:
    # The service launch has no free argument slot, so the entry point's own
    # name is what marks it.
    script = tmp_path / f"{MARKER}.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    # The real service a developer has running must not be seen as the holder.
    monkeypatch.setattr(runtime, "extra_service_process_pids", lambda **kwargs: [])
    monkeypatch.setattr(runtime, "maybe_systemd_scope_prefix", lambda: [])
    return runtime.ServiceLauncher(python=sys.executable, main=str(script))


def test_a_service_interrupted_between_its_reservation_and_its_capture_does_not_survive(
    spawned, monkeypatch, tmp_path: Path
) -> None:
    launcher = _marked_service(tmp_path, monkeypatch)
    reservation = runtime.paths.get_runtime_pid_path()
    start_info = _InterruptedCapture(reservation)

    with pytest.raises(KeyboardInterrupt):
        runtime.start_service(wait_for_ready=False, start_info=start_info, launcher=launcher)

    _assert_killed_and_reaped(spawned)
    assert start_info.record_at_intrusion == str(spawned[0].pid)
    assert not reservation.exists()
    assert start_info.pid is None
    assert spawned[0].pid not in runtime._SERVICE_START_PROCESSES  # noqa: SLF001
    _assert_log_sinks_released(spawned)


def test_a_child_that_could_not_be_confirmed_dead_keeps_its_record(spawned, monkeypatch, tmp_path: Path) -> None:
    # A kill that did not take leaves a live child; withdrawing its record too
    # would leave it running with nothing that names it.
    monkeypatch.setattr(runtime, "discard_spawned_child", lambda process: None)
    launcher = runtime.ServiceLauncher(python=str(_marked_interpreter(tmp_path)), main="unused")
    pid_path = runtime.paths.get_runtime_ui_pid_path()

    with pytest.raises(KeyboardInterrupt):
        runtime.start_ui(
            "127.0.0.1", 5123, wait_for_ready=False, start_info=_InterruptedCapture(pid_path), launcher=launcher
        )

    assert len(spawned) == 1
    assert spawned[0].poll() is None
    assert pid_path.read_text(encoding="utf-8") == str(spawned[0].pid)

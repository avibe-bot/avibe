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


@pytest.fixture
def spawned(monkeypatch, tmp_path: Path):
    """Every real child started with the probe marker, still inspectable afterwards.

    The spy is a real ``Popen`` subclass, so the log sinks and the child are
    started exactly as in production; it only keeps a handle the code under test
    would otherwise have dropped with its exception.
    """

    monkeypatch.setattr(runtime.paths, "get_runtime_dir", lambda: tmp_path / "runtime")
    children: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            super().__init__(args, *rest, **kwargs)
            if any(MARKER in str(argument) for argument in args):
                children.append(self)

    monkeypatch.setattr(runtime.subprocess, "Popen", SpyPopen)
    yield children
    for child in children:
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


def test_a_child_whose_pid_record_cannot_be_written_does_not_survive(spawned, tmp_path: Path) -> None:
    # A real write failure: the record's directory does not exist.
    pid_path = tmp_path / "missing" / "vibe-ui.pid"

    with pytest.raises(FileNotFoundError):
        runtime.spawn_background(_sleeper(), pid_path, "ui_stdout.log", "ui_stderr.log")

    _assert_killed_and_reaped(spawned)
    assert not pid_path.exists()


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


def test_a_service_whose_reservation_cannot_be_written_does_not_survive(
    spawned, monkeypatch, tmp_path: Path
) -> None:
    # The service launch has no free argument slot, so the entry point's own
    # name is what marks it.
    script = tmp_path / f"{MARKER}.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    launcher = runtime.ServiceLauncher(python=sys.executable, main=str(script))
    # The real service a developer has running must not be seen as the holder.
    monkeypatch.setattr(runtime, "extra_service_process_pids", lambda **kwargs: [])
    monkeypatch.setattr(runtime, "maybe_systemd_scope_prefix", lambda: [])

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

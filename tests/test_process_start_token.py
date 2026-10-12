"""A process's start token: the one identity of a running process, which no clock adjustment moves.

Every record that names a process by pid -- the service lock, Claude and OpenCode processes, restart
jobs, desktop installer owners -- must keep naming the same execution after the wall clock or the boot
time moves, and must never name a process that later reused the pid.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from vibe import runtime


@contextmanager
def _clock_adjusted():
    """psutil's own view of the platform after the clock moved: its wall-clock start times shift."""

    if sys.platform == "darwin":
        from psutil import _psosx

        # kern.boottime crossed a whole second, as routine time-daemon adjustments make it.
        with patch.object(_psosx, "boot_time", lambda: _psosx.INIT_BOOT_TIME - 1):
            yield 1.0
    elif sys.platform.startswith("linux"):
        from psutil import _pslinux

        real_boot_time = _pslinux.boot_time
        # The wall clock was stepped, which moves the boot time /proc/stat reports.
        with patch.object(_pslinux, "boot_time", lambda: real_boot_time() - 21.0):
            yield -21.0
    else:
        pytest.skip("Windows stores a process's creation time and psutil reads it back unchanged")


@pytest.fixture
def live_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process
    finally:
        process.kill()
        process.wait(10)


def test_a_clock_adjustment_moves_the_wall_clock_start_but_not_the_start_token(live_process) -> None:
    pid = live_process.pid
    token = runtime.process_start_token(pid)
    created = runtime.process_create_time(pid)
    assert token is not None and created is not None

    with _clock_adjusted() as shift:
        # The adjustment is real for psutil: the wall-clock start it reports moved.
        assert runtime.process_create_time(pid) == pytest.approx(created + shift)
        assert runtime.process_start_token(pid) == token


def test_every_recorded_process_identity_survives_a_clock_adjustment(live_process) -> None:
    from modules.agents import claude_process_reaper
    from modules.agents.opencode.server import OpenCodeGeneration
    from vibe import desktop_backends

    pid = live_process.pid
    token = runtime.process_start_token(pid)
    claude_record = claude_process_reaper.ClaudeOwnedProcess(pid=pid, start_token=token)
    claude_row = claude_process_reaper.ClaudeProcessRow(pid=pid, ppid=1, command="claude")
    # Recorded, not this process's child: only its token proves it.
    opencode = OpenCodeGeneration(generation_id="ocg_live", pid=pid, port=50100, spec_digest="spec", process_start_token=token)

    with _clock_adjusted():
        current = claude_process_reaper._process_start_tokens({pid})
        assert claude_process_reaper._process_identity_matches(claude_record, claude_row, current.get(pid))
        assert opencode.process_alive()
        assert desktop_backends._installer_owner_gone(f"{pid}:{token}", None) is False
        assert runtime.process_start_token(pid) == token


def test_a_reused_pid_is_not_the_recorded_process(live_process) -> None:
    from modules.agents import claude_process_reaper

    pid = live_process.pid
    earlier = claude_process_reaper.ClaudeOwnedProcess(pid=pid, start_token="an-earlier-process")
    row = claude_process_reaper.ClaudeProcessRow(pid=pid, ppid=1, command="claude")

    assert not claude_process_reaper._process_identity_matches(
        earlier, row, claude_process_reaper._process_start_tokens({pid}).get(pid)
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="the unadjusted start time is read through psutil on macOS")
def test_psutil_still_offers_the_unadjusted_start_time_its_identity_uses() -> None:
    """``create_time(monotonic=True)`` is private to psutil; a release without it must fail here, not in the field."""

    import os

    import psutil

    assert isinstance(psutil.Process(os.getpid())._proc.create_time(monotonic=True), float)

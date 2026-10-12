"""A process's start token: the one identity of a running process, which no clock adjustment moves.

Every record that names a process by its token -- the service lock and restart jobs -- must keep naming
the same execution after the wall clock or the boot time moves, and must never name a process that
later reused the pid.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
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


@pytest.mark.parametrize("platform", ["freebsd14", "openbsd7", "sunos5"])
def test_a_platform_without_a_stable_start_gives_no_process_a_token(live_process, monkeypatch, platform) -> None:
    """There psutil's start moves with system clock updates: no exact token, so every record is unverifiable."""

    monkeypatch.setattr(sys, "platform", platform)

    assert runtime.process_start_token(live_process.pid) is None


@pytest.mark.parametrize("contents", ["", "\n", OSError("unreadable")], ids=["empty", "blank", "unreadable"])
def test_a_linux_boot_id_that_cannot_be_read_is_none(monkeypatch, contents) -> None:
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/random/boot_id":
            if isinstance(contents, Exception):
                raise contents
            return contents
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert runtime._linux_boot_id.__wrapped__() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux token is the boot id and the start ticks")
def test_a_linux_process_has_no_token_without_its_boot_id(live_process, monkeypatch) -> None:
    """Start ticks restart at every boot: alone they would name a later boot's process with the same pid."""

    assert runtime.process_start_token(live_process.pid).startswith(f"{runtime._linux_boot_id()}:")

    monkeypatch.setattr(runtime, "_linux_boot_id", lambda: None)

    assert runtime.process_start_token(live_process.pid) is None


def _restart_job_reader(name: str, monkeypatch):
    """One of the readers that decide whether a recorded restart job still runs."""

    if name == "web-restart":
        from vibe import ui_server

        return ui_server._restart_in_flight
    if name == "upgrade":
        from vibe import upgrade

        return upgrade.restart_is_pending
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "incus_regression_supervisor.py"
    spec = importlib.util.spec_from_file_location("incus_regression_supervisor", script)
    assert spec and spec.loader
    supervisor = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, supervisor)
    spec.loader.exec_module(supervisor)
    return supervisor._restart_in_progress


# Older than every reader's bound for a status it cannot verify (60 s for Web restarts, 300 s otherwise).
_PAST_EVERY_BOUND = 3600


@pytest.mark.parametrize("reader", ["web-restart", "upgrade", "incus-supervisor"])
@pytest.mark.parametrize(
    ("record", "age", "blocks"),
    [
        pytest.param("token-its-job", _PAST_EVERY_BOUND, True, id="token-its-job"),
        pytest.param("token-reused-pid", 0, False, id="token-reused-pid"),
        pytest.param("released-its-job", _PAST_EVERY_BOUND, True, id="released-its-job"),
        pytest.param("released-reused-pid", 0, False, id="released-reused-pid"),
        pytest.param("no-token", 0, True, id="no-token-within-bound"),
        pytest.param("no-token", _PAST_EVERY_BOUND, False, id="no-token-past-bound"),
        pytest.param("token-unreadable-now", 0, True, id="unreadable-now-within-bound"),
        pytest.param("token-unreadable-now", _PAST_EVERY_BOUND, False, id="unreadable-now-past-bound"),
    ],
)
def test_every_restart_status_reader_applies_one_ownership_policy(
    live_process, tmp_path, monkeypatch, reader, record, age, blocks
) -> None:
    """A running status blocks while its job is verified, and one that cannot be verified only for a bounded time.

    The live process stands for the pid the status names: the job itself, or an unrelated process that
    took its pid. A status written where no token could be read, or whose token cannot be read now,
    cannot tell the two apart.
    """

    from config import paths

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    pid = live_process.pid
    status = {"ok": None, "state": "running", "supervisor_pid": pid}
    if record == "token-its-job":
        status["supervisor_start_token"] = runtime.process_start_token(pid)
    elif record == "token-reused-pid":
        status["supervisor_start_token"] = "an-earlier-process"
    elif record.startswith("released-"):
        # A released build recorded psutil's create time; the job that had this pid started an hour earlier.
        created = runtime.process_create_time(pid)
        status["supervisor_started_at"] = created if record == "released-its-job" else created - 3600
    elif record == "no-token":
        status["supervisor_start_token"] = None
    else:
        status["supervisor_start_token"] = runtime.process_start_token(pid)
        monkeypatch.setattr(runtime, "process_start_token", lambda _pid: None)
    path = runtime.get_restart_status_path()
    runtime.write_json(path, status)
    written = time.time() - age
    os.utime(path, (written, written))

    assert _restart_job_reader(reader, monkeypatch)() is blocks


@pytest.mark.parametrize("reader", ["web-restart", "upgrade", "incus-supervisor"])
def test_a_running_restart_job_is_still_its_job_after_a_clock_adjustment(live_process, tmp_path, monkeypatch, reader) -> None:
    from config import paths

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    pid = live_process.pid
    path = runtime.get_restart_status_path()
    runtime.write_json(
        path,
        {"ok": None, "state": "running", "supervisor_pid": pid, "supervisor_start_token": runtime.process_start_token(pid)},
    )
    # Past every bound, so only its identity can keep it the job.
    written = time.time() - _PAST_EVERY_BOUND
    os.utime(path, (written, written))
    in_flight = _restart_job_reader(reader, monkeypatch)

    with _clock_adjusted():
        assert in_flight() is True


@pytest.mark.skipif(sys.platform != "darwin", reason="the unadjusted start time is read through psutil on macOS")
def test_psutil_still_offers_the_unadjusted_start_time_its_identity_uses() -> None:
    """``create_time(monotonic=True)`` is private to psutil; a release without it must fail here, not in the field."""


    import psutil

    assert isinstance(psutil.Process(os.getpid())._proc.create_time(monotonic=True), float)

"""What the whole-machine service-process scan is allowed to cost.

``vibe stop`` resolves the authoritative lock owner and then walks every
process on the machine looking for lock-less Avibe daemons left behind by older
lifecycle bugs. Whatever that walk does per process, it does a few hundred
times per stop -- so anything in it that starts an external process is paid
that many times over, before the service has been asked to exit or a single
byte has been printed.

On Linux the per-process fallback is a ``/proc`` read and the design is
invisible. On Windows it was a ``powershell`` launch, and that is why
``gh-v3.1.1rc10`` timed out in ``vibe stop`` with no output at all.
"""

import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import runtime
from vibe.desktop_runtime import DESKTOP_RUNTIME_ID_ENV

# Generous by design. A scan that only reads what psutil already collected
# finishes in about a second on a CI runner, and the packaging probe allows the
# whole stop 60s -- so this catches a per-process external call without ever
# being close enough to the real cost to go flaky.
SCAN_BUDGET_SECONDS = 20.0


class _UnreadableProcess:
    """A process psutil can see but whose command line it cannot read.

    ``process_iter`` fills a field it was denied with its ``ad_value``, which
    defaults to ``None``. On Windows that is the ordinary outcome for every
    process the current user cannot open, which is most of the system ones.
    """

    def __init__(self, pid: int) -> None:
        self.info = {"pid": pid, "cmdline": None}

    def cwd(self) -> None:
        return None

    def environ(self) -> dict:
        return {}


def test_the_service_scan_starts_no_external_process_per_scanned_process(monkeypatch):
    """The scan must answer from what psutil already collected, or not at all.

    Asserting the mechanism rather than a duration: a per-process shell launch
    is the defect whatever it costs on the machine running the test, and a
    timing assertion alone would be both flaky and silent about the cause.

    Nothing is lost by refusing to look further. The scan exists to find
    lock-less Avibe daemons, and those are processes this install started,
    whose command line psutil can read. One it cannot read is, by construction,
    not ours -- and the authoritative owner comes from the service lock, not
    from this scan.
    """

    processes = [_UnreadableProcess(90000 + offset) for offset in range(64)]
    launched = []

    def record_launch(command, *args, **kwargs):
        launched.append(command)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(runtime.psutil, "process_iter", lambda attrs=None: list(processes))
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(runtime.subprocess, "run", record_launch)

    assert runtime.service_processes() == []
    assert launched == [], (
        f"the scan started {len(launched)} external process(es) for {len(processes)} scanned processes: {launched[:2]}"
    )


def test_the_command_lookup_reads_a_live_process_without_starting_one(monkeypatch):
    """The single-pid lookup reads the command line in this process.

    It names one process for an identity check or a log line, on lifecycle
    paths such as ``vibe start`` and ``vibe stop``. A ``powershell`` or ``ps``
    child per lookup took seconds on Windows, and could not run at all where a
    setuid ``ps`` is barred, as under a macOS sandbox. A path with a space must
    stay one argument.
    """

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "/tmp/Vibe Tools/cloudflared"])
    launched = []
    try:
        monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: launched.append(command))
        command = runtime.get_process_command(child.pid)
    finally:
        child.kill()
        child.wait(timeout=10)

    assert launched == []
    assert command is not None
    if os.name == "nt":
        assert command.endswith(' "/tmp/Vibe Tools/cloudflared"'), command
    else:
        assert shlex.split(command)[-1] == "/tmp/Vibe Tools/cloudflared", command


def _deny_every_process(monkeypatch):
    def denied(pid):
        raise runtime.psutil.AccessDenied(pid)

    monkeypatch.setattr(runtime.psutil, "Process", denied)


@pytest.mark.skipif(os.name == "nt", reason="only POSIX has a ps that reads what psutil may not")
def test_another_users_process_is_still_read_through_ps(monkeypatch):
    """On macOS psutil may not read another user's process; the setuid ``ps`` can."""

    _deny_every_process(monkeypatch)
    asked = []
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda command, **kwargs: asked.append(command) or SimpleNamespace(stdout="/usr/sbin/someone-else --flag\n"),
    )

    assert runtime.get_process_command(4321) == "/usr/sbin/someone-else --flag"
    assert asked == [["ps", "-p", "4321", "-o", "command="]]


@pytest.mark.skipif(os.name != "nt", reason="Windows has no other reader to ask")
def test_on_windows_a_process_psutil_may_not_read_is_unread_without_a_shell(monkeypatch):
    _deny_every_process(monkeypatch)
    asked = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: asked.append(command))

    assert runtime.get_process_command(4321) is None
    assert asked == []


@pytest.mark.skipif(os.name != "nt", reason="only a real Windows process table holds enough unreadable processes")
def test_the_service_scan_finishes_promptly_on_a_real_windows_process_table():
    """The same claim again, end to end, on the platform that pays for it.

    Read-only: it walks the process table and stops. The mechanism assertion
    above is the primary signal; this one exists because the defect class this
    release has kept hitting is a POSIX cost assumption that only a Windows
    process table can expose, and no unit test of our own fakes can expose it.
    """

    walk_started = time.monotonic()
    visible = len(list(runtime.psutil.process_iter(attrs=["pid", "cmdline"])))
    walk_seconds = time.monotonic() - walk_started

    # Price the budget off what this machine actually charges for the same walk
    # rather than off a guess. `process_iter` never shells out, so the defect
    # does not inflate this measurement and cannot buy itself room -- it only
    # keeps a slow runner from failing a test about external processes.
    budget = max(SCAN_BUDGET_SECONDS, walk_seconds * 4)
    finished = threading.Event()

    def scan() -> None:
        try:
            runtime.service_processes()
        finally:
            finished.set()

    # Daemon on purpose: the bound says we stop waiting, not that we can call
    # back a scan that is already busy launching shells.
    threading.Thread(target=scan, daemon=True).start()

    assert finished.wait(budget), (
        f"service_processes() did not finish within {budget:.1f}s over {visible} processes, "
        f"while the raw psutil walk of the same table took {walk_seconds:.2f}s"
    )


@pytest.mark.skipif(os.name != "nt", reason="POSIX runs this scan against real processes in test_desktop_runtime_stop")
def test_the_marker_scan_finds_a_marked_child_on_a_real_windows_process_table():
    """A scoped ``vibe stop`` finds its targets by this scan.

    psutil refuses to collect an attribute its platform lacks, and a Windows
    process has no uids, so only a Windows process table shows a scan that
    raises before it looks at a single process.
    """

    runtime_id = secrets.token_hex(32)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
        env={**os.environ, DESKTOP_RUNTIME_ID_ENV: runtime_id},
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline() == "ready\n"
        found = runtime.processes_carrying_marker(
            runtime.fingerprint_process_marker(runtime_id), marker_env=DESKTOP_RUNTIME_ID_ENV
        )
        assert child.pid in [process.pid for process in found]
    finally:
        child.kill()
        child.wait(timeout=10)
        child.stdout.close()

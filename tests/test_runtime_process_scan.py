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
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import runtime

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


def test_the_windows_command_lookup_cannot_hang_its_caller(monkeypatch):
    """The single-pid diagnostic lookup keeps its shell, but on a leash.

    Dropping it from the scan leaves it right for what it is actually for:
    naming one specific process. It still runs on lifecycle paths such as
    ``vibe stop``, though, so it must not be able to wait forever on a shell
    that never answers.
    """

    calls = []

    def record(command, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", record)

    runtime._get_process_command_windows(4321)

    assert calls, "the lookup never ran its shell, so this asserts nothing"
    assert all(isinstance(call.get("timeout"), (int, float)) and call["timeout"] > 0 for call in calls), (
        f"an unbounded external shell on a lifecycle path: {calls}"
    )


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

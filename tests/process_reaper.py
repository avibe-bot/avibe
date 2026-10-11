"""Every process a ``reap_processes`` test starts ends with that test, however it ends.

A test that starts a real process and then fails, or waits on a gate until
``hang_guard`` gives up, never reaches its own cleanup. The process it started
kept running: a job wrapper the test had stopped with SIGSTOP, and mock Model
Hub engines, outlived their deleted worktrees by days. Two owners close that.

- ``TestProcesses.reap`` runs once every fixture of a marked test is torn down
  (``tests/conftest.py``). It kills each process the test started that is still
  running: every ``subprocess.Popen`` child the test recorded, with the process
  group the child founded, and every other new descendant of this pytest
  process, each with its own descendants.
- A pytest that is killed outright (SIGKILL, the CI watchdog's ``_exit``) runs
  no teardown. The warden, a process started with the first marked test, reads
  the running test's recorded children from a pipe. When pytest is gone,
  however it ended, the warden kills each one still running and the group it
  founded.

Opt-in, for the suites whose children are real and long-lived: the job wrapper
tests (``tests/agent_core/tools/test_bash.py``, ``test_jobs.py``,
``tests/test_watch_job_target.py``), the mock-engine tests
(``tests/test_model_hub_runtime.py``, ``test_model_hub_local_error_details.py``)
and the remote-access process tests (``tests/test_tunnel_recovery.py``,
``tests/test_cloudflared_runtime.py``).
Each starts its children through ``subprocess.Popen`` in a session of their own,
which is what the record covers. A marked test starts no ``multiprocessing``
helper, which serves a whole run and would be reaped with the test.

Invariant: once a marked test has ended, no process it started is still
running, and when a run is killed, none its running test started is. A process
is killed, not waited for, so a test that needs one to finish waits for it in
its body. Every signal is proven to reach what the test started:

- a process is matched by pid and creation time, so a reused pid is never
  signalled;
- a group is signalled only while its recorded leader still runs, or while
  that pid names no process at all, which leaves only the leader's orphans in
  it. A pid reused by any live process makes the record stale. The warden
  forgets each test's records once its reap has confirmed them gone.

A helper interpreter a test starts (a cross-process child) spawns processes
this pytest process never sees: once the helper exits they reparent to init.
The helper calls ``record_spawns`` first, so each of its spawns is appended to
the test's spawn record, and the ``reap_helper_spawns`` fixture reaps them by
identity after the test's own cleanup; the warden reads the same record when
the run is killed. Nothing is ever killed by name: an orphan that is in no
record but names the test's ``tmp_path`` is only reported, and fails the test.

Reports name a process by pid and executable name only: an argv can carry a
credential.

POSIX only, like the foreign-signal guard whose ``Popen`` hook records spawns.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import psutil
import pytest

from tests import hang_guard

# Captured before tests/conftest.py guards them: the reaper may signal what no test may.
_KILL = os.kill
_KILLPG = getattr(os, "killpg", None)
_GETPGID = getattr(os, "getpgid", None)
_GETPGRP = getattr(os, "getpgrp", None)


class _Spawn(NamedTuple):
    created: float
    # The spawn leads a process group of its own (``start_new_session``).
    founded_group: bool


def _created(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _same(pid: int, created: float) -> psutil.Process | None:
    try:
        process = psutil.Process(pid)
        if process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except psutil.Error:
        pass
    return None


def _group_is_ours(leader: int, created: float) -> bool:
    """Whether group ``leader`` still holds only what the recorded leader started."""
    try:
        return psutil.Process(leader).create_time() == created
    except psutil.NoSuchProcess:
        # No process holds the pid, so no other process can lead a group by it.
        return True
    except psutil.Error:
        return False


def _descendants() -> dict[int, float]:
    found: dict[int, float] = {}
    for process in psutil.Process().children(recursive=True):
        try:
            found[process.pid] = process.create_time()
        except psutil.Error:
            pass
    return found


def _group_members(groups: set[int]) -> list[psutil.Process]:
    members = []
    for process in psutil.process_iter():
        try:
            if _GETPGID(process.pid) in groups and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process)
        except (OSError, psutil.Error):
            pass
    return members


def _describe(pid: int) -> str:
    try:
        return f"{pid} ({psutil.Process(pid).name()})"
    except psutil.Error:
        return str(pid)


def _add_tree(targets: dict[int, float], process: psutil.Process) -> None:
    try:
        for each in (process, *process.children(recursive=True)):
            targets.setdefault(each.pid, each.create_time())
    except psutil.Error:
        pass


def _kill_and_wait(targets: dict[int, float], groups: set[int]) -> None:
    """SIGKILL every group, then every target still the same process, and wait until all are gone."""
    __tracebackhide__ = True
    for pgid in groups:
        try:
            _KILLPG(pgid, signal.SIGKILL)
        except OSError:
            pass
    for pid, created in targets.items():
        if _same(pid, created) is not None:
            try:
                _KILL(pid, signal.SIGKILL)
            except OSError:
                pass
    give_up = time.monotonic() + hang_guard.HANG_GUARD_SECONDS
    while running := [pid for pid, created in targets.items() if _same(pid, created) is not None]:
        if time.monotonic() >= give_up:
            pytest.fail(
                f"hung for {hang_guard.HANG_GUARD_SECONDS:.0f}s waiting for SIGKILLed processes to end: "
                + ", ".join(map(_describe, running))
            )
        time.sleep(0.01)


# A log sink ends once the connector whose output it drains has: what the
# test's own cleanup stopped gets this long to go before it counts as left behind.
_SPAWN_EXIT_SECONDS = 5.0


def names_path(path: Path) -> Callable[[str], re.Match | None]:
    """Search for ``path`` itself or a path inside it: the whole final component, so ``test_x1`` never names ``test_x10``."""
    return re.compile(re.escape(str(path)) + r"(?![\w.-])").search


def helper_spawn_record(test_temp: Path) -> Path:
    """Where a test's helper interpreters record their spawns."""
    return test_temp / "helper-spawns"


def _append_spawn(record: Path, pid: int) -> None:
    created = _created(pid)
    if created is None:
        return
    try:
        founded_group = _GETPGID(pid) == pid != _GETPGRP()
    except (OSError, TypeError):
        founded_group = False
    with open(record, "a", encoding="utf-8") as file:
        file.write(f"{pid} {created!r} {int(founded_group)}\n")
        file.flush()
        os.fsync(file.fileno())


def record_spawns(record: Path) -> None:
    """Called first in a helper interpreter a test starts: record it, then each process it spawns, in ``record``.

    One line per process, ``<pid> <creation time> <1 if it founded its group>``,
    flushed and synced before ``Popen`` returns, so a helper killed right after
    a spawn has still recorded it. The helper's own line lets teardown end a
    helper that is still running before it reads the record for the last time.
    """
    _append_spawn(record, os.getpid())
    initialize = subprocess.Popen.__init__

    def recording_init(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        _append_spawn(record, self.pid)

    subprocess.Popen.__init__ = recording_init


def _recorded_spawns(record: Path) -> dict[int, _Spawn]:
    spawns: dict[int, _Spawn] = {}
    try:
        lines = record.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return spawns
    for line in lines:
        try:
            pid, created, founded_group = line.split()
            spawns[int(pid)] = _Spawn(float(created), founded_group == "1")
        except ValueError:
            # A line cut short by a helper killed mid-write.
            continue
    return spawns


def require_inside_basetemp(path: Path, basetemp: Path) -> None:
    """Fail unless ``path`` is strictly inside the run's ``basetemp``."""
    __tracebackhide__ = True
    resolved, root = path.resolve(), basetemp.resolve()
    if root not in resolved.parents:
        pytest.fail(f"refusing {resolved}: it is not strictly inside this run's basetemp {root}")


def _lineage() -> set[int]:
    me = psutil.Process()
    return {me.pid, *(ancestor.pid for ancestor in me.parents())}


def orphans_naming(test_temp: Path) -> list[str]:
    """Describe each orphan (ppid 1) whose executable or argv names ``test_temp``. Never signals one.

    A process with a live parent, such as a developer's ``tail -f``, is not an orphan.
    """
    names = names_path(test_temp)
    lineage = _lineage()
    found = []
    for process in psutil.process_iter():
        if process.pid in lineage:
            continue
        try:
            if process.ppid() != 1 or process.status() == psutil.STATUS_ZOMBIE:
                continue
            words = [process.exe(), *process.cmdline()]
        except psutil.Error:
            continue
        if any(word and names(word) for word in words):
            found.append(_describe(process.pid))
    return found


def end_helper_spawns(
    record: Path, test_temp: Path, started: TestProcesses | None = None
) -> tuple[list[str], list[str]]:
    """After the test's own cleanup, reap what ``record`` names by identity; then list unrecorded orphans.

    ``started`` is the test's own ``reap_processes`` record. Its children
    include the helpers, recorded from the moment they exist, so a helper that
    has not yet recorded itself still holds the grace open and is ended before
    the last read.

    Returns every process that was still running and was killed (a helper, a
    spawn, a descendant, or a member a recorded group leader left behind) and
    the orphans still naming ``test_temp``, which get no signal.
    """
    __tracebackhide__ = True
    lineage = _lineage()
    spawns: dict[int, _Spawn] = dict(started.spawned) if started is not None else {}

    def leftovers() -> tuple[dict[int, float], set[int]]:
        # A helper still running may append at any time, so every pass reads the record again.
        spawns.update(_recorded_spawns(record))
        targets: dict[int, float] = {}
        for pid, spawn in spawns.items():
            if (process := _same(pid, spawn.created)) is not None:
                _add_tree(targets, process)
        # A group outlives its leader: what a spawned shell put in the background stays in it.
        groups = {pid for pid, spawn in spawns.items() if spawn.founded_group and _group_is_ours(pid, spawn.created)}
        for member in _group_members(groups) if groups else ():
            _add_tree(targets, member)
        for pid in lineage:
            targets.pop(pid, None)
        return targets, groups

    give_up = time.monotonic() + _SPAWN_EXIT_SECONDS
    while (leftovers()[0] or orphans_naming(test_temp)) and time.monotonic() < give_up:
        time.sleep(0.05)
    reaped: list[str] = []
    # Killing a helper ends its appends, so the read after it is final.
    while (found := leftovers())[0]:
        targets, groups = found
        reaped.extend(_describe(pid) for pid, created in targets.items() if _same(pid, created) is not None)
        _kill_and_wait(targets, groups)
    return reaped, orphans_naming(test_temp)


class TestProcesses:
    """One test's spawns, and the descendants this pytest process had before it began."""

    __test__ = False

    def __init__(self, warden: "Warden | None") -> None:
        self._warden = warden
        self._before = _descendants()
        self._spawned: dict[int, _Spawn] = {}

    @property
    def spawned(self) -> dict[int, _Spawn]:
        return dict(self._spawned)

    def record(self, pid: int) -> None:
        """Called for every ``subprocess.Popen`` child the test starts, once the child exists."""
        created = _created(pid)
        if created is None:
            return
        try:
            founded_group = _GETPGID(pid) == pid != _GETPGRP()
        except OSError:
            founded_group = False
        self._spawned[pid] = _Spawn(created, founded_group)
        if self._warden is not None:
            self._warden.record(pid, created, founded_group)

    def reap(self) -> list[str]:
        """Kill what the test left running and wait until it is gone; describe each process killed."""
        __tracebackhide__ = True
        targets: dict[int, float] = {}
        for pid, created in _descendants().items():
            if self._before.get(pid) != created and (process := _same(pid, created)) is not None:
                _add_tree(targets, process)
        for pid, spawn in self._spawned.items():
            if (process := _same(pid, spawn.created)) is not None:
                _add_tree(targets, process)
        # A group outlives its leader: what a spawned shell put in the background stays in it.
        groups = {
            pid for pid, spawn in self._spawned.items() if spawn.founded_group and _group_is_ours(pid, spawn.created)
        }
        for member in _group_members(groups) if groups else ():
            _add_tree(targets, member)
        targets.pop(os.getpid(), None)
        reaped = [_describe(pid) for pid, created in targets.items() if _same(pid, created) is not None]
        _kill_and_wait(targets, groups)
        if self._warden is not None:
            self._warden.settle()
        return reaped


# Runs as ``python -I -c _WARDEN <pytest pid>`` with the record pipe on stdin. A
# line is "<pid> <creation time> <1 if it founded its group>", "settled" once
# the running test's reap has confirmed every record gone, "spawns <path>" for
# a ``reap_helper_spawns`` test's spawn record, or "unspawns <path>" once that
# test's teardown has reaped it; a teardown that did not finish leaves its
# record armed. EOF, or a parent that is no longer pytest, means the run is over.
_WARDEN = r"""
import os, select, signal, sys, time
import psutil

pytest_pid = int(sys.argv[1])
records = {}
spawn_records = set()
pending = b""
while True:
    ready, _, _ = select.select([0], [], [], 1.0)
    if not ready:
        if os.getppid() != pytest_pid:
            break
        continue
    chunk = os.read(0, 65536)
    if not chunk:
        break
    *lines, pending = (pending + chunk).split(b"\n")
    for line in lines:
        if line == b"settled":
            records.clear()
            continue
        if line.startswith(b"spawns "):
            spawn_records.add(line[7:].decode())
            continue
        if line.startswith(b"unspawns "):
            spawn_records.discard(line[9:].decode())
            continue
        pid, created, founded_group = line.split()
        records[int(pid)] = (float(created), founded_group == b"1")


def recorded():
    # The helper records, in the same format, read afresh: a helper still running may append.
    found = dict(records)
    for path in spawn_records:
        try:
            with open(path, encoding="utf-8") as file:
                for line in file:
                    try:
                        pid, created, founded_group = line.split()
                        found[int(pid)] = (float(created), founded_group == "1")
                    except ValueError:
                        pass
        except OSError:
            pass
    return found


def reap_pass():
    acted = False
    for pid, (created, founded_group) in recorded().items():
        try:
            process = psutil.Process(pid)
            same = process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            process, same = None, False
        except psutil.Error:
            continue
        if same:
            acted = True
            try:
                tree = [*process.children(recursive=True), process]
            except psutil.Error:
                # It ended after the identity check; its group is still handled below.
                tree = []
            for each in tree:
                try:
                    each.send_signal(signal.SIGKILL)
                except psutil.Error:
                    pass
        # The group is the leader's while it runs, or while no process holds its pid.
        if founded_group and (same or process is None):
            try:
                os.killpg(pid, signal.SIGKILL)
                acted = True
            except OSError:
                pass
    return acted


# Killing a helper ends its appends, so passes repeat until one finds nothing running.
for _ in range(40):
    if not reap_pass():
        break
    time.sleep(0.05)
"""


class Warden:
    """The run's last owner of its spawns: it acts once this pytest process is gone."""

    def __init__(self) -> None:
        read_end, self._write_end = os.pipe()
        try:
            # Its own session, so a signal to pytest's process group cannot end it first.
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-c", _WARDEN, str(os.getpid())],
                stdin=read_end,
                start_new_session=True,
            )
        finally:
            os.close(read_end)

    def _send(self, line: str) -> None:
        try:
            os.write(self._write_end, f"{line}\n".encode())
        except BrokenPipeError:
            # The warden is gone; the per-test reaper still owns every spawn.
            pass

    def record(self, pid: int, created: float, founded_group: bool) -> None:
        self._send(f"{pid} {created!r} {int(founded_group)}")

    def watch_spawns(self, record: Path, basetemp: Path) -> None:
        """Reap what ``record`` names if the run ends before ``forget_spawns``.

        Checked here: the warden of a dead run has nothing to check it against.
        """
        __tracebackhide__ = True
        require_inside_basetemp(record, basetemp)
        self._send(f"spawns {record}")

    def forget_spawns(self, record: Path) -> None:
        self._send(f"unspawns {record}")

    def settle(self) -> None:
        """Every record so far is confirmed gone: forget them, so none can go stale."""
        self._send("settled")

    def close(self) -> None:
        """End the run: the warden kills whatever recorded process is still running, then exits."""
        os.close(self._write_end)
        self._process.wait()

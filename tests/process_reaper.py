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
``tests/test_watch_job_target.py``) and the mock-engine tests
(``tests/test_model_hub_runtime.py``, ``test_model_hub_local_error_details.py``).
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

Reports name a process by pid and executable name only: an argv can carry a
credential.

POSIX only, like the foreign-signal guard whose ``Popen`` hook records spawns.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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


class TestProcesses:
    """One test's spawns, and the descendants this pytest process had before it began."""

    __test__ = False

    def __init__(self, warden: "Warden | None") -> None:
        self._warden = warden
        self._before = _descendants()
        self._spawned: dict[int, _Spawn] = {}

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

        def add(process: psutil.Process) -> None:
            try:
                for each in (process, *process.children(recursive=True)):
                    targets.setdefault(each.pid, each.create_time())
            except psutil.Error:
                pass

        for pid, created in _descendants().items():
            if self._before.get(pid) != created and (process := _same(pid, created)) is not None:
                add(process)
        for pid, spawn in self._spawned.items():
            if (process := _same(pid, spawn.created)) is not None:
                add(process)
        # A group outlives its leader: what a spawned shell put in the background stays in it.
        groups = {
            pid for pid, spawn in self._spawned.items() if spawn.founded_group and _group_is_ours(pid, spawn.created)
        }
        for member in _group_members(groups) if groups else ():
            add(member)
        targets.pop(os.getpid(), None)
        reaped = [_describe(pid) for pid, created in targets.items() if _same(pid, created) is not None]
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
        if self._warden is not None:
            self._warden.settle()
        return reaped


# Runs as ``python -I -c _WARDEN <pytest pid>`` with the record pipe on stdin. A
# line is "<pid> <creation time> <1 if it founded its group>", or "settled" once
# the running test's reap has confirmed every record gone. EOF, or a parent
# that is no longer pytest, means the run is over.
_WARDEN = r"""
import os, select, signal, sys
import psutil

pytest_pid = int(sys.argv[1])
records = {}
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
        pid, created, founded_group = line.split()
        records[int(pid)] = (float(created), founded_group == b"1")
for pid, (created, founded_group) in records.items():
    try:
        process = psutil.Process(pid)
        same = process.create_time() == created
    except psutil.NoSuchProcess:
        process, same = None, False
    except psutil.Error:
        continue
    if same:
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
        except OSError:
            pass
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

    def settle(self) -> None:
        """Every record so far is confirmed gone: forget them, so none can go stale."""
        self._send("settled")

    def close(self) -> None:
        """End the run: the warden kills whatever recorded process is still running, then exits."""
        os.close(self._write_end)
        self._process.wait()

"""The process that runs one job's command (``LocalJobHost``'s ``pipe`` backend).

Run as ``python -I -S -B job_wrapper.py avibe-job <job_dir> <decision_timeout_s>
<head_cap> <tail_cap> <deadline_epoch_s or -> <argv...>`` in a new session, so
its pid is the job's process group, where ``argv`` runs the command (``shell.py``:
``<shell> -c <command>``, or PowerShell reading ``command.ps1``). Standard
library only: ``-S`` skips site-packages.

On Windows the tree is held by a job object instead of a process group: the
wrapper creates ``Local\\avibe-job-<job_id>`` with ``KILL_ON_JOB_CLOSE`` and
joins it before it writes ``pid``, so every process the command starts is in it,
and none outlives the wrapper (``windows_job.py``). Files are replaced with
POSIX semantics (``windows_files.py``), so a host reading ``tail.log`` never
makes its replacement fail.

1. Write ``pid`` (temporary file, rename).
2. Wait for ``decision``, at most ``decision_timeout_s``; on timeout, decide
   ``abandon`` itself through the same exclusive ``link(2)`` the host uses (on
   Windows a rename, which never replaces there).
3. Only on ``go``: run ``argv`` with stdin closed and stdout and stderr into one
   pipe.
4. Keep the output bounded on disk: the first ``head_cap`` bytes are appended
   to ``output.log`` as they arrive; after that, the last ``tail_cap`` bytes are
   kept in ``tail.log`` (a ``[output from byte <offset>]\\n`` header, readable by
   anyone who opens the file, then the bytes from that offset of the whole
   stream), replaced atomically a few times a second.
5. When the shell exits, read what it wrote, then write ``exit`` (temporary
   file, rename). Keep reading until every holder of the pipe closes it, so
   background children are not killed by ``SIGPIPE``.
6. If the deadline passes before the shell exits, the wrapper itself fails, or
   a catchable signal reaches the wrapper alone (``pkill -f python``), write
   ``stopped`` (``timeout``, ``wrapper_error``, or ``killed``) and end the whole
   process group: SIGTERM, then SIGKILL after 3 s. The wrapper decides timeout
   versus exit, and it looks at the shell first. On Windows the job object ends
   at once, with no graceful phase.
7. Windows only: once the shell has exited and its output is closed, stay
   while processes it started still run. They are in the job, which ends with
   its last handle, the wrapper's; on POSIX they run on in the process group.

Every wait is bounded or watches the deadline: the decision wait (30 s), the
check just before the shell is spawned, the read loop, the wait after the
command closes its output, and the 3 s grace before SIGKILL.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import time
import traceback
from typing import Any, Optional

_WINDOWS = os.name == "nt"
_SNAPSHOT_INTERVAL_S = 0.25
_DRAIN_LIMIT_S = 0.5
_TERMINATE_GRACE_S = 3.0
# Catchable signals that would otherwise end the wrapper alone and leave its command running
# (``pkill -f python``, a hangup; SIGINT would surface as a failure). Each stops the whole group as
# "killed" instead. Python already ignores SIGPIPE and SIGXFSZ; SIGKILL cannot be caught.
_STOP_SIGNALS = tuple(
    getattr(signal, name)
    for name in (
        "SIGTERM SIGHUP SIGINT SIGQUIT SIGUSR1 SIGUSR2 SIGALRM SIGVTALRM SIGPROF SIGXCPU SIGPWR SIGIO SIGBREAK"
    ).split()
    if hasattr(signal, name)
)
#: Polling the output pipe on Windows, where ``select`` takes only sockets.
_PIPE_POLL_S = 0.01


def _load_beside(name: str) -> Any:
    """A module beside this file, by its path: ``-I -S`` leaves the package and site-packages off ``sys.path``."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"avibe_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_job_objects = _load_beside("windows_job") if _WINDOWS else None
_files = _load_beside("windows_files") if _WINDOWS else None
#: Windows: the handle of the job object that holds this wrapper and its command's tree.
_job: Optional[int] = None


def _write_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    (_files.replace if _WINDOWS else os.replace)(tmp, path)


def _create_once(path: str, value: str) -> None:
    """Create ``path`` holding ``value`` unless it exists: ``link(2)`` of a written temp file, as the host does.

    On Windows a rename, which never replaces there, and works on volumes without hard links (FAT).
    """
    tmp = f"{path}.{value}.{os.getpid()}.tmp"
    with open(tmp, "w") as handle:
        handle.write(f"{value}\n")
    try:
        (os.rename if _WINDOWS else os.link)(tmp, path)
    except FileExistsError:
        pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _wait_for_decision(job_dir: str, timeout_s: float) -> str:
    decision = os.path.join(job_dir, "decision")
    deadline = time.monotonic() + timeout_s
    delay = 0.001
    while not os.path.exists(decision):
        if time.monotonic() >= deadline:
            _create_once(decision, "abandon")
            break
        time.sleep(delay)
        delay = min(delay * 2, 0.05)
    # With delete sharing, as Avibe reads every job file: on Windows the host creates ``decision`` by a rename,
    # whose handle asks for delete until it closes, and a plain ``open`` meanwhile is a sharing violation.
    fd = _files.open_for_reading(decision) if _WINDOWS else os.open(decision, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(64).decode("ascii", "replace").strip()


class _BoundedLog:
    def __init__(self, job_dir: str, head_cap: int, tail_cap: int) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        self._fd = os.open(os.path.join(job_dir, "output.log"), flags, 0o600)
        self._tail_path = os.path.join(job_dir, "tail.log")
        self._head_cap = head_cap
        self._tail_cap = tail_cap
        self._total = 0
        self._tail = bytearray()
        self._dirty = False
        self._last_snapshot = 0.0

    def write(self, data: bytes) -> None:
        if self._total < self._head_cap:
            head = data[: self._head_cap - self._total]
            view = memoryview(head)
            while view:
                view = view[os.write(self._fd, view) :]
            self._total += len(head)
            data = data[len(head) :]
        if data:
            self._tail += data
            self._total += len(data)
            if len(self._tail) > 2 * self._tail_cap:
                del self._tail[: len(self._tail) - self._tail_cap]
            self._dirty = True

    def snapshot(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not self._dirty or (not force and now - self._last_snapshot < _SNAPSHOT_INTERVAL_S):
            return
        kept = bytes(self._tail[-self._tail_cap :])
        data = b"[output from byte %d]\n" % (self._total - len(kept)) + kept
        try:
            _write_atomic(self._tail_path, data)
        except PermissionError:
            # Windows: another program holds tail.log open without sharing delete (an editor, ``Get-Content
            # -Wait``). The tail is still here, so a periodic snapshot leaves it to the next one. The last one
            # cannot wait: it goes into the file itself, which such a program shares for writing; one that shares
            # neither keeps the previous tail while it holds the file, and the command's exit still stands.
            if not _WINDOWS:
                raise
            if not (force and _rewrite_in_place(self._tail_path, data)):
                self._last_snapshot = now
                return
        self._dirty = False
        self._last_snapshot = now


def _rewrite_in_place(path: str, data: bytes) -> bool:
    """Windows: write ``data`` over the existing file ``path`` itself; whether its readers' sharing allowed it.

    Not atomic: a reader in the middle of it may see part of each, so it stands in only for the last
    snapshot, which cannot wait for the next.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_BINARY)
    except PermissionError:
        return False
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)
    return True


def _stop_group(job_dir: str, reason: str, proc: Optional[subprocess.Popen]) -> None:
    """Record why, then end the whole process group, this wrapper included; never returns.

    SIGTERM first, which the wrapper ignores for itself; SIGKILL for everything left after 3 s, so
    background children of the command end too.
    """
    for signum in _STOP_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)  # nothing interrupts the stop, and SIGTERM to the group spares us
    try:
        _create_once(os.path.join(job_dir, "stopped"), reason)  # the first stopper's reason wins
    except OSError:
        pass  # the kill matters more than the record
    if _WINDOWS:
        if _job is not None:
            with contextlib.suppress(OSError):
                _job_objects.terminate_job(_job)  # every process of the job, this wrapper included
        os._exit(1)  # no job yet, or it could not be ended: its last handle closes with us (KILL_ON_JOB_CLOSE)
    group = os.getpgrp()
    with contextlib.suppress(OSError):
        os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATE_GRACE_S
    while proc is not None and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    os.killpg(group, signal.SIGKILL)


def _readable(fd: int, timeout_s: float) -> bool:
    """Whether reading ``fd``, the command's output pipe, would not block, waiting at most ``timeout_s``."""
    if not _WINDOWS:
        return bool(select.select([fd], [], [], timeout_s)[0])
    end = time.monotonic() + timeout_s
    while not _job_objects.pipe_readable(fd):
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, _PIPE_POLL_S))
    return True


def _run(job_dir: str, proc: subprocess.Popen, log: _BoundedLog, deadline: Optional[float]) -> None:
    fd = proc.stdout.fileno()
    exit_written = False
    eof = False

    def write_exit() -> None:
        code = proc.returncode
        _write_atomic(os.path.join(job_dir, "exit"), b"%d\n" % (code if code >= 0 else 128 - code))

    def enforce_deadline() -> None:
        # J3: the deadline holds even when no host is there to enforce it. The wrapper alone decides
        # timeout versus exit, and it looks at the shell first: one that has exited is never a timeout.
        if exit_written or deadline is None or time.time() < deadline or proc.poll() is not None:
            return
        _stop_group(job_dir, "timeout", proc)

    while not eof:
        enforce_deadline()
        if _readable(fd, 0.05):
            chunk = os.read(fd, 65536)
            if chunk:
                log.write(chunk)
            else:
                eof = True
        if not exit_written and proc.poll() is not None:
            # Everything the shell wrote is in the pipe by now; take it before reporting the exit.
            drain_until = time.monotonic() + _DRAIN_LIMIT_S
            while not eof and time.monotonic() < drain_until and _readable(fd, 0):
                chunk = os.read(fd, 65536)
                if chunk:
                    log.write(chunk)
                else:
                    eof = True
            log.snapshot(force=True)
            write_exit()
            exit_written = True
        log.snapshot()
    log.snapshot(force=True)
    # The command closed its output but may still run (``exec >/dev/null``): keep the deadline while waiting.
    while not exit_written and proc.poll() is None:
        enforce_deadline()
        time.sleep(0.05)
    if not exit_written:
        write_exit()


def _stay_while_the_job_runs() -> None:
    """Windows: keep the job object open while processes the command started still run in it.

    The job ends with its last handle, this wrapper's, so a background process the command left running
    would die with the wrapper. On POSIX it runs on in the process group, where ``kill`` still reaches it;
    here the wrapper stays, so ``kill`` reaches it through the job.
    """
    while _job_objects.active_processes(_job) > 1:
        time.sleep(_SNAPSHOT_INTERVAL_S)


def main(argv: list[str]) -> int:
    if len(argv) < 8 or argv[1] != "avibe-job":
        return 64
    job_dir, command = argv[2], argv[7:]
    decision_timeout_s, head_cap, tail_cap = float(argv[3]), int(argv[4]), int(argv[5])
    deadline = None if argv[6] == "-" else float(argv[6])
    proc: Optional[subprocess.Popen] = None

    def stop_on_signal(signum: int, frame: object) -> None:
        _stop_group(job_dir, "killed", proc)

    for signum in _STOP_SIGNALS:
        signal.signal(signum, stop_on_signal)
    if _WINDOWS:
        global _job
        # Before ``pid``: the host records the identity only of a wrapper that is in its job (J2). The host
        # names it the same way (``jobs._job_object_name``).
        _job = _job_objects.create_kill_on_close_job(f"avibe-job-{os.path.basename(job_dir)}")
    _write_atomic(os.path.join(job_dir, "pid"), b"%d\n" % os.getpid())
    if _wait_for_decision(job_dir, decision_timeout_s) != "go":
        return 0
    try:
        if deadline is not None and time.time() >= deadline:
            # The deadline passed during the launch: the command must not start at all.
            _stop_group(job_dir, "timeout", None)
        log = _BoundedLog(job_dir, head_cap, tail_cap)
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _run(job_dir, proc, log, deadline)
        if _WINDOWS:
            _stay_while_the_job_runs()
    except BaseException:
        # A wrapper that cannot follow its command (a full disk, a bug) must not leave it running unmanaged.
        # The diagnostics may fail for the same reason (ENOSPC), so they can never stand in the way.
        with contextlib.suppress(BaseException):
            traceback.print_exc()
            sys.stderr.flush()
        _stop_group(job_dir, "wrapper_error", proc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

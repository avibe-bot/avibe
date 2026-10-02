"""The process that runs one job's command (``LocalJobHost``'s ``pipe`` backend).

Run as ``python -I -S job_wrapper.py avibe-job <job_dir> <shell> <command>
<decision_timeout_s> <head_cap> <tail_cap> <deadline_epoch_s or ->`` in a new
session, so its pid is the job's process group. Standard library only: ``-S``
skips site-packages.

1. Write ``pid`` (temporary file, rename).
2. Wait for ``decision``, at most ``decision_timeout_s``; on timeout, decide
   ``abandon`` itself through the same exclusive ``link(2)`` the host uses.
3. Only on ``go``: run ``<shell> -c <command>`` with stdin closed and stdout and
   stderr into one pipe.
4. Keep the output bounded on disk: the first ``head_cap`` bytes are appended
   to ``output.log`` as they arrive; after that, the last ``tail_cap`` bytes are
   kept in ``tail.log`` (a ``<offset>\\n`` header, then the bytes from that
   offset of the whole stream), replaced atomically a few times a second.
5. When the shell exits, read what it wrote, then write ``exit`` (temporary
   file, rename). Keep reading until every holder of the pipe closes it, so
   background children are not killed by ``SIGPIPE``.
6. If the deadline passes before the shell exits, or the wrapper itself fails,
   write ``stopped`` (``timeout`` or ``wrapper_error``) and end the whole
   process group: SIGTERM, then SIGKILL after 3 s.

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
from typing import Optional

_SNAPSHOT_INTERVAL_S = 0.25
_DRAIN_LIMIT_S = 0.5
_TERMINATE_GRACE_S = 3.0


def _write_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _create_once(path: str, value: str) -> None:
    """Create ``path`` holding ``value`` unless it exists: ``link(2)`` of a written temp file, as the host does."""
    tmp = f"{path}.{value}.{os.getpid()}.tmp"
    with open(tmp, "w") as handle:
        handle.write(f"{value}\n")
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
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
    with open(decision) as handle:
        return handle.read().strip()


class _BoundedLog:
    def __init__(self, job_dir: str, head_cap: int, tail_cap: int) -> None:
        self._fd = os.open(os.path.join(job_dir, "output.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
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
        _write_atomic(self._tail_path, b"%d\n" % (self._total - len(kept)) + kept)
        self._dirty = False
        self._last_snapshot = now


def _stop_group(job_dir: str, reason: str, proc: Optional[subprocess.Popen]) -> None:
    """Record why, then end the whole process group, this wrapper included; never returns.

    SIGTERM first, which the wrapper ignores for itself; SIGKILL for everything left after 3 s, so
    background children of the command end too.
    """
    try:
        _create_once(os.path.join(job_dir, "stopped"), reason)  # the first stopper's reason wins
    except OSError:
        pass  # the kill matters more than the record
    group = os.getpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with contextlib.suppress(OSError):
        os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATE_GRACE_S
    while proc is not None and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    os.killpg(group, signal.SIGKILL)


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
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            chunk = os.read(fd, 65536)
            if chunk:
                log.write(chunk)
            else:
                eof = True
        if not exit_written and proc.poll() is not None:
            # Everything the shell wrote is in the pipe by now; take it before reporting the exit.
            drain_until = time.monotonic() + _DRAIN_LIMIT_S
            while not eof and time.monotonic() < drain_until and select.select([fd], [], [], 0)[0]:
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


def main(argv: list[str]) -> int:
    if len(argv) != 9 or argv[1] != "avibe-job":
        return 64
    job_dir, shell, command = argv[2], argv[3], argv[4]
    decision_timeout_s, head_cap, tail_cap = float(argv[5]), int(argv[6]), int(argv[7])
    deadline = None if argv[8] == "-" else float(argv[8])
    _write_atomic(os.path.join(job_dir, "pid"), b"%d\n" % os.getpid())
    if _wait_for_decision(job_dir, decision_timeout_s) != "go":
        return 0
    proc: Optional[subprocess.Popen] = None
    try:
        if deadline is not None and time.time() >= deadline:
            # The deadline passed during the launch: the command must not start at all.
            _stop_group(job_dir, "timeout", None)
        log = _BoundedLog(job_dir, head_cap, tail_cap)
        proc = subprocess.Popen(
            [shell, "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        _run(job_dir, proc, log, deadline)
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

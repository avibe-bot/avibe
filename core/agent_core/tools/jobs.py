"""``LocalJobHost``: the ``pipe`` backend of ``JobHost`` (C-7 section 7, ``recovery.md`` J1-J6).

Every command runs under ``job_wrapper.py`` in its own session: stdin closed,
stdout and stderr through a pipe into a bounded log the wrapper keeps. Nothing
in the host holds a pipe to the job, so any ``LocalJobHost`` over the same
directory, in this process or after a restart, can read, wait on, or kill it.

Job directory: ``meta.json``, ``pid``, ``decision``, ``output.log`` (the head),
``tail.log`` (the rolling tail, once the head is full), ``exit``, ``stopped``
(why the host killed it), ``wrapper.log`` (the wrapper's own diagnostics).

J1, launch handshake: the host writes ``meta.json`` and spawns the wrapper; the
wrapper writes ``pid`` and waits for ``decision``; the host records the process
identity in ``meta.json`` (J2), then decides ``go``; the wrapper runs the
command only on ``go``, and decides ``abandon`` itself if no decision comes in
time. A decision is created exclusively, through ``link(2)`` of a written
temporary file, so exactly one side decides and nobody reads it half-written.
Recovery decides ``abandon`` for a job that has none; a missing or late
``pid`` proves nothing.

J2, identity: the wrapper's argv names the job directory, a random path no
recycled pid carries, and argv is readable on every POSIX platform. The
inherited ``AVIBE_PROCESS_IDENTITY`` marker is set and fingerprinted as well,
but macOS hides the environment of its own binaries (``/bin/bash``,
``/bin/sleep``), so the marker only verifies a group whose wrapper has exited.

J3: ``meta.json.deadline_at`` is absolute. The wrapper enforces it itself, so
it holds while no host runs; ``wait`` and ``enforce_deadline`` are a second,
idempotent owner. Whoever kills records ``stopped`` first.

POSIX only in v1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

import psutil

from core.agent_core.tools.base import JobStatus
from core.process_isolation import (
    DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS,
    KILL_SIGNAL,
    PROCESS_IDENTITY_ENV,
    PersistedProcessIdentity,
    capture_spawned_process_identity,
    new_process_identity_marker,
    process_group_identity_status,
    process_identity_from_payload,
    serialize_process_identity,
)

logger = logging.getLogger(__name__)

#: Adopt-or-create the once Watch (target kind ``job``) for the job's ``meta.json``; returns the Watch id.
#: Keyed by ``job_id``, so calling it again after a crash returns the same Watch (J6).
HandOver = Callable[[Mapping[str, Any]], Awaitable[str]]
#: Whether the tool call that owns a job has a durable ``tool_result`` and its Watch, if any, has settled (J5).
Settled = Callable[[Mapping[str, Any]], bool]

#: How long the wrapper waits for the host's decision before abandoning the command.
DECISION_TIMEOUT_S = 30.0
#: The host gives up on the wrapper's ``pid`` well before the wrapper gives up on a decision.
PID_TIMEOUT_S = 10.0
#: Output kept on disk per job (J4): this much from the start and this much from the end.
OUTPUT_HEAD_BYTES = 1024 * 1024
OUTPUT_TAIL_BYTES = 2 * 1024 * 1024
OUTPUT_CHUNK_BYTES = 1024 * 1024
FINISHED_JOB_RETENTION_S = 7 * 24 * 3600

STOP_TIMEOUT = "timeout"

_WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_wrapper.py")
_JOB_ID = re.compile(r"^job_[a-z0-9]+$")
_GO = "go"
_ABANDON = "abandon"


class JobStartError(RuntimeError):
    """The command was not started; nothing ran."""


class JobHandOverUnavailable(RuntimeError):
    """This host has no Watch to hand a job to."""


def default_shell() -> str:
    """Pi's choice: ``/bin/bash``, then ``bash`` on PATH, then ``sh``."""
    if os.path.exists("/bin/bash"):
        return "/bin/bash"
    return shutil.which("bash") or "sh"


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ceil_ms(moment: datetime) -> datetime:
    """Round up to the millisecond ``_iso`` keeps, so a recorded deadline is never early."""
    rest = moment.microsecond % 1000
    return moment + timedelta(microseconds=1000 - rest) if rest else moment


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write_atomic(path: str, data: str) -> None:
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return None


class LocalJobHost:
    """Job handles under ``jobs_dir`` (``<state>/agent_core/jobs``).

    ``hand_over`` delegates to ``on_hand_over``, which the adapter backs with
    Watch; without it, ``hand_over`` raises ``JobHandOverUnavailable``.
    """

    def __init__(
        self,
        jobs_dir: str,
        *,
        on_hand_over: Optional[HandOver] = None,
        shell: Optional[str] = None,
    ) -> None:
        self._jobs_dir = os.path.abspath(jobs_dir)
        self._on_hand_over = on_hand_over
        self._shell = shell or default_shell()
        # Wrappers this host spawned. Each has a reaper thread, so a finished wrapper never lingers
        # as a zombie (which would keep its process group alive), and an unreaped pid cannot be recycled.
        self._children: dict[str, subprocess.Popen] = {}

    # --- paths and metadata ---------------------------------------------

    def job_dir(self, job_id: str) -> str:
        if not _JOB_ID.match(job_id):
            raise KeyError(job_id)
        return os.path.join(self._jobs_dir, job_id)

    def _path(self, job_id: str, name: str) -> str:
        return os.path.join(self.job_dir(job_id), name)

    def output_path(self, job_id: str) -> str:
        return self._path(job_id, "output.log")

    def meta(self, job_id: str) -> dict[str, Any]:
        try:
            with open(self._path(job_id, "meta.json"), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise KeyError(job_id) from None

    def _write_meta(self, job_id: str, meta: Mapping[str, Any]) -> None:
        _write_atomic(self._path(job_id, "meta.json"), json.dumps(meta, indent=2))

    def find_job(self, session_id: str, tool_call_id: str) -> Optional[str]:
        """The job started for a tool call, for settling it at resume."""
        for name in self._job_ids():
            try:
                meta = self.meta(name)
            except (KeyError, ValueError, OSError):
                continue
            if meta.get("session_id") == session_id and meta.get("tool_call_id") == tool_call_id:
                return name
        return None

    def _job_ids(self) -> list[str]:
        try:
            return sorted(name for name in os.listdir(self._jobs_dir) if _JOB_ID.match(name))
        except FileNotFoundError:
            return []

    # --- decision (J1) ----------------------------------------------------

    def _create_decision(self, job_id: str, value: str) -> str:
        """Try to decide; return the decision that holds, ours or the wrapper's."""
        decision = self._path(job_id, "decision")
        tmp = f"{decision}.{value}.{secrets.token_hex(4)}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
        try:
            os.link(tmp, decision)
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
        return _read_text(decision) or _ABANDON

    def _decision(self, job_id: str) -> str:
        """The job's decision; a job without one is abandoned now, as recovery requires."""
        current = _read_text(self._path(job_id, "decision"))
        if current is not None:
            return current
        self.meta(job_id)  # an unknown job raises KeyError
        return self._create_decision(job_id, _ABANDON)

    def started(self, job_id: str) -> bool:
        """Whether the command may have run. ``False`` is final: it never ran and never will."""
        return self._decision(job_id) == _GO

    # --- JobHost ------------------------------------------------------------

    async def start(
        self,
        command: str,
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: Optional[float],
        session_id: str,
        tool_call_id: str,
    ) -> str:
        """Start ``command`` with ``env`` as its whole environment; return once it may run.

        Raises ``JobStartError`` when the command did not start.
        """
        if os.name != "posix":
            raise JobStartError("Command jobs are not supported on Windows yet.")
        if not sys.executable:
            raise JobStartError("No Python interpreter is available to run the command.")
        job_id = f"job_{secrets.token_hex(8)}"
        job_dir = self.job_dir(job_id)
        os.makedirs(self._jobs_dir, exist_ok=True)
        os.mkdir(job_dir, 0o700)
        created = datetime.now(timezone.utc)
        deadline = _ceil_ms(created + timedelta(seconds=timeout_s)) if timeout_s is not None else None
        meta: dict[str, Any] = {
            "version": 1,
            "job_id": job_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "backend": "pipe",
            "command": command,
            "cwd": cwd,
            "timeout_s": timeout_s,
            "created_at": _iso(created),
            "state_dir": job_dir,
            "process": None,
            "terminal": None,
            "watch_id": None,
            "deadline_at": _iso(deadline) if deadline is not None else None,
        }
        self._write_meta(job_id, meta)

        marker = new_process_identity_marker()
        child_env = dict(env)
        child_env[PROCESS_IDENTITY_ENV] = marker
        argv = [
            sys.executable,
            "-I",
            "-S",
            _WRAPPER,
            "avibe-job",
            job_dir,
            self._shell,
            command,
            repr(DECISION_TIMEOUT_S),
            str(OUTPUT_HEAD_BYTES),
            str(OUTPUT_TAIL_BYTES),
            repr(deadline.timestamp()) if deadline is not None else "-",
        ]
        open(self.output_path(job_id), "ab").close()
        diagnostics = os.open(self._path(job_id, "wrapper.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=diagnostics,
                stderr=diagnostics,
                start_new_session=True,
            )
        except OSError as exc:
            self._create_decision(job_id, _ABANDON)
            raise JobStartError(f"Could not start the command: {exc}") from exc
        finally:
            os.close(diagnostics)
        self._children[job_id] = proc
        threading.Thread(target=proc.wait, name=f"avibe-{job_id}-reaper", daemon=True).start()

        identity = await self._await_pid(job_id, proc, marker)
        if identity is not None:
            meta["process"] = {**serialize_process_identity(identity), "pgid": identity.pid}
            self._write_meta(job_id, meta)
        decision = self._create_decision(job_id, _GO if identity is not None else _ABANDON)
        if decision != _GO:
            raise JobStartError("The command did not start.")
        return job_id

    async def _await_pid(self, job_id: str, proc: subprocess.Popen, marker: str) -> Optional[PersistedProcessIdentity]:
        deadline = time.monotonic() + PID_TIMEOUT_S
        pid_path = self._path(job_id, "pid")
        while time.monotonic() < deadline:
            text = _read_text(pid_path)
            if text:
                try:
                    pgid = os.getpgid(proc.pid)
                except ProcessLookupError:
                    return None
                if text != str(proc.pid) or pgid != proc.pid:
                    logger.error("Job %s wrapper reported pid %s, expected %s", job_id, text, proc.pid)
                    return None
                return capture_spawned_process_identity(proc.pid, marker)
            if proc.poll() is not None:
                return None
            await asyncio.sleep(0.002)
        return None

    def status(self, job_id: str) -> JobStatus:
        exit_code = self._exit_code(job_id)
        if exit_code is not None:
            self._reap(job_id)
            return JobStatus("exited", exit_code)
        if self._decision(job_id) != _GO:
            return JobStatus("gone")
        if self._alive(job_id):
            return JobStatus("running")
        # The wrapper writes `exit` before it exits, so look once more.
        exit_code = self._exit_code(job_id)
        self._reap(job_id)
        return JobStatus("exited", exit_code) if exit_code is not None else JobStatus("gone")

    async def wait(self, job_id: str, *, deadline_s: Optional[float]) -> JobStatus:
        """Wait until exit, or at most ``deadline_s`` seconds from now; enforce the job's own deadline meanwhile."""
        end = None if deadline_s is None else time.monotonic() + max(0.0, deadline_s)
        delay = 0.005
        while True:
            if await self.enforce_deadline(job_id):
                return self.status(job_id)
            current = self.status(job_id)
            if current.state != "running":
                return current
            if end is not None:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return current
                delay = min(delay, remaining)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.1)

    def output(self, job_id: str, since: int = 0) -> tuple[bytes, int]:
        """At most ``OUTPUT_CHUNK_BYTES`` of raw output after offset ``since``; call again until empty.

        Offsets count the command's whole output. Past the kept head, a reader
        that falls behind the kept tail jumps forward: the new offset then
        exceeds ``since + len(data)`` by the bytes dropped from disk (J4).
        """
        head_path = self.output_path(job_id)
        try:
            head_size = os.path.getsize(head_path)
        except FileNotFoundError:
            self.meta(job_id)
            return b"", since
        if since < head_size:
            with open(head_path, "rb") as handle:
                handle.seek(since)
                data = handle.read(min(OUTPUT_CHUNK_BYTES, head_size - since))
            return data, since + len(data)
        try:
            with open(self._path(job_id, "tail.log"), "rb") as handle:
                tail_start = int(handle.readline())
                start = max(since, tail_start)
                handle.seek(start - tail_start, os.SEEK_CUR)
                data = handle.read(OUTPUT_CHUNK_BYTES)
        except (FileNotFoundError, ValueError):
            return b"", since
        return data, start + len(data)

    async def kill(self, job_id: str, *, reason: str = "killed") -> None:
        """Terminate the job's process tree, background children included, if it is provably the job's.

        ``reason`` is recorded first (``stop_reason``), so whoever reports the
        job later can say why it ended.
        """
        if self._decision(job_id) != _GO:
            return
        identity = self._identity(job_id)
        if identity is None:
            logger.warning("Job %s has no recorded process identity; not signaling", job_id)
            return
        if not (self._alive(job_id) or self._group_carries_marker(identity)):
            if _group_exists(identity.pid):
                logger.warning("Job %s process group %s cannot be verified; not signaling", job_id, identity.pid)
            return
        if _read_text(self._path(job_id, "stopped")) is None:
            _write_atomic(self._path(job_id, "stopped"), f"{reason}\n")
        if not await asyncio.to_thread(_terminate_group, identity.pid):
            logger.warning("Job %s process group %s survived termination", job_id, identity.pid)
        self._reap(job_id)

    async def hand_over(self, job_id: str) -> str:
        """Give the job to its Watch: adopt-or-create through ``on_hand_over``, then record the id (J6)."""
        meta = self.meta(job_id)
        if meta.get("watch_id"):
            return meta["watch_id"]
        if self._on_hand_over is None:
            raise JobHandOverUnavailable("No Watch is available to take over the command.")
        watch_id = await self._on_hand_over(meta)
        meta = self.meta(job_id)
        meta["watch_id"] = watch_id
        self._write_meta(job_id, meta)
        return watch_id

    # --- deadline (J3) and reporting --------------------------------------

    async def enforce_deadline(self, job_id: str) -> bool:
        """Kill the job if its ``deadline_at`` has passed while it runs; ``True`` if it was killed."""
        deadline_at = self.meta(job_id).get("deadline_at")
        if not deadline_at or datetime.now(timezone.utc) < _parse_iso(deadline_at):
            return False
        if self.status(job_id).state != "running":
            return False
        await self.kill(job_id, reason=STOP_TIMEOUT)
        return True

    def stop_reason(self, job_id: str) -> Optional[str]:
        """Why the host killed the job (``"timeout"``, or the caller's reason), if it did."""
        return _read_text(self._path(job_id, "stopped"))

    # --- maintenance (J5) -------------------------------------------------

    def prune(self, settled: Settled, *, older_than_s: float = FINISHED_JOB_RETENTION_S) -> list[str]:
        """Remove jobs that are finished, settled (``settled(meta)``), and untouched for ``older_than_s``."""
        removed: list[str] = []
        cutoff = time.time() - older_than_s
        for name in self._job_ids():
            try:
                job_dir = self.job_dir(name)
                if max(entry.stat().st_mtime for entry in os.scandir(job_dir)) > cutoff:
                    continue
                if self.status(name).state == "running" or not settled(self.meta(name)):
                    continue
            except (KeyError, ValueError, OSError):
                continue
            shutil.rmtree(job_dir, ignore_errors=True)
            removed.append(name)
        return removed

    # --- internals --------------------------------------------------------

    def _exit_code(self, job_id: str) -> Optional[int]:
        text = _read_text(self._path(job_id, "exit"))
        if text is None:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _identity(self, job_id: str) -> Optional[PersistedProcessIdentity]:
        process = self.meta(job_id).get("process")
        if not isinstance(process, dict) or process.get("pgid") != process.get("pid"):
            return None
        return process_identity_from_payload(
            {key: process.get(key) for key in ("pid", "create_time", "worker_fingerprint")}, process.get("pid")
        )

    def _alive(self, job_id: str) -> bool:
        """Whether the job's wrapper still runs: our unreaped child, or the process whose argv names this job."""
        child = self._children.get(job_id)
        if child is not None:
            return child.returncode is None
        identity = self._identity(job_id)
        if identity is None:
            return False
        try:
            argv = psutil.Process(identity.pid).cmdline()
        except (psutil.Error, OSError):
            # Gone, a zombie, or unreadable: none of them proves the job still runs.
            return False
        job_dir = self.job_dir(job_id)
        return any(argv[i : i + 2] == ["avibe-job", job_dir] for i in range(len(argv) - 1))

    def _group_carries_marker(self, identity: PersistedProcessIdentity) -> bool:
        return process_group_identity_status(identity.pid, identity, logger, "agent job") == "match"

    def _reap(self, job_id: str) -> None:
        child = self._children.get(job_id)
        if child is not None and child.returncode is not None:
            del self._children[job_id]


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_group(pgid: int, timeout_s: float = DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS) -> bool:
    """SIGTERM the verified group, then SIGKILL what is left; ``True`` once the group is gone.

    The group was verified just before. A process group id is not reused while
    the group exists, so signaling it until it is gone reaches only its members.
    """
    if pgid <= 1 or pgid == os.getpgrp():
        logger.error("Refusing to signal process group %s", pgid)
        return False
    for sig in (signal.SIGTERM, KILL_SIGNAL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + timeout_s
        while _group_exists(pgid):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        else:
            return True
    return not _group_exists(pgid)

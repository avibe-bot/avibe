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
temporary file (on Windows a rename, which never replaces there), so exactly
one side decides and nobody reads it half-written.
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

Windows: the tree is the wrapper's job object, ``Local\\avibe-job-<job_id>``,
which the wrapper creates and joins before it writes ``pid`` and which ends its
members when the wrapper's handle closes (``windows_job``). J2 then asks for a
wrapper that is a member of that job instead of a group leader, and a kill
terminates the job. Nothing outlives the wrapper, so a job whose wrapper is
gone is gone. Job files are replaced with POSIX semantics and read shared for
delete (``windows_files``): a reader of ``tail.log``, which the wrapper
replaces a few times a second, would otherwise make the replace fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
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

from core.agent_core.tools import windows_files, windows_job
from core.agent_core.tools.base import CallInstance, JobStatus
from core.agent_core.tools.paths import os_reason, run_to_end
from core.agent_core.tools.shell import Shell, default_shell
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
#: Keyed by ``job_id``, so calling it again after a crash returns the same Watch (J6). The adapter promises
#: that raising means no Watch owns the job: adopt-or-create is atomic. Once it returns, the Watch owns it.
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
STOP_ABORTED = "aborted"
#: A stop by anyone else, the wrapper's catchable signals included; ``kill``'s default.
STOP_KILLED = "killed"
#: The wrapper itself failed (a full disk, a bug) and stopped the command it could no longer follow.
STOP_WRAPPER_ERROR = "wrapper_error"

_WINDOWS = os.name == "nt"
_WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "job_wrapper.py")
#: How long past the deadline the host leaves a live wrapper to decide (its poll interval, its
#: 0.5 s drain, and margin) before stopping the job itself.
_WRAPPER_DECIDES_S = 1.0
_JOB_ID = re.compile(r"^job_[a-z0-9]+$")
# tail.log starts with this line (written by the wrapper), so a reader of the file sees where it begins.
_TAIL_HEADER = re.compile(rb"\[output from byte (\d+)\]\n")
_GO = "go"
_ABANDON = "abandon"
_STATE_FILE_CHARS = 4096
_SURROGATE = re.compile("[\ud800-\udfff]")
# The host's own meta.json holds the command, which a shell cannot even take past ARG_MAX (1 MiB here).
_META_CHARS = 4 * 1024 * 1024


class JobStartError(RuntimeError):
    """The command was not started; nothing ran."""


class JobHandOverUnavailable(RuntimeError):
    """This host has no Watch to hand a job to."""


def _job_object_name(job_id: str) -> str:
    """The Windows job object of a job's tree; the wrapper creates it under the same name."""
    return f"avibe-job-{job_id}"


def _open_for_reading(path: str, *, binary: bool = False) -> Any:
    """A job file, open for reading. On Windows it is shared for delete, so a writer can still replace it."""
    if not _WINDOWS:
        return open(path, "rb") if binary else open(path, encoding="utf-8", errors="replace")
    handle = os.fdopen(windows_files.open_for_reading(path), "rb")
    return handle if binary else io.TextIOWrapper(handle, encoding="utf-8", errors="replace")


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
    (windows_files.replace if _WINDOWS else os.replace)(tmp, path)


def _meta_text(meta: Mapping[str, Any]) -> str:
    """``meta.json`` as written: UTF-8 as it is (not escaped), and never past the bound ``meta`` reads in.

    A POSIX path can hold bytes that are not UTF-8, which Python keeps as surrogate escapes; UTF-8 cannot
    carry those, so such a record is written ASCII-escaped instead, which JSON reads back exactly.
    """
    text = json.dumps(meta, indent=2, ensure_ascii=False)
    if _SURROGATE.search(text):
        text = json.dumps(meta, indent=2)
    if len(text) > _META_CHARS:
        raise ValueError(f"the job record would be {len(text)} characters, over {_META_CHARS}")
    return text


def _read_text(path: str) -> Optional[str]:
    """A small state file (``pid``, ``decision``, ``exit``, ``stopped``), read in a bound: the job's
    directory is one its command can write to."""
    try:
        with _open_for_reading(path) as handle:
            return handle.read(_STATE_FILE_CHARS).strip()
    except FileNotFoundError:
        return None


def call_instance(meta: Mapping[str, Any]) -> Optional[CallInstance]:
    """The call instance that started a job, from its ``meta.json``; ``None`` if the record names none."""
    session_id, response_id = meta.get("session_id"), meta.get("response_id")
    response_seq, tool_call_id = meta.get("response_seq"), meta.get("tool_call_id")
    if not all(isinstance(value, str) for value in (session_id, response_id, tool_call_id)):
        return None
    if not isinstance(response_seq, int) or isinstance(response_seq, bool):
        return None
    return CallInstance(session_id, response_id, response_seq, tool_call_id)


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
        shell: Optional[Shell] = None,
    ) -> None:
        # Canonical, so every host names a job's directory (and its wrapper's argv) the same way, whatever
        # alias it was given (``~/.vibe_remote`` -> ``~/.avibe``, ``/tmp`` -> ``/private/tmp``).
        self._jobs_dir = os.path.realpath(jobs_dir)
        self._on_hand_over = on_hand_over
        self._shell = shell or default_shell()
        # Wrappers this host spawned. Each has a reaper thread, so a finished wrapper never lingers
        # as a zombie (which would keep its process group alive), and an unreaped pid cannot be recycled.
        self._children: dict[str, subprocess.Popen] = {}

    @property
    def shell(self) -> Shell:
        """The shell every command of this host runs in."""
        return self._shell

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
        """The job's metadata. Read in a bound: the job's directory is one its command can write to, and a
        file over the bound is refused as corrupt (``ValueError``), as unreadable JSON is."""
        try:
            with _open_for_reading(self._path(job_id, "meta.json")) as handle:
                text = handle.read(_META_CHARS + 1)
        except FileNotFoundError:
            raise KeyError(job_id) from None
        if len(text) > _META_CHARS:
            raise ValueError(f"meta.json of job {job_id} is over {_META_CHARS} characters")
        return json.loads(text)

    def _write_meta(self, job_id: str, meta: Mapping[str, Any]) -> None:
        _write_atomic(self._path(job_id, "meta.json"), _meta_text(meta))

    def find_job(self, call: CallInstance) -> Optional[str]:
        """The job a call instance started, for settling the call at resume; it starts at most one.

        Matched on the whole instance, never the call id alone: a provider may reuse an id in a later response.
        """
        for name in self._job_ids():
            try:
                meta = self.meta(name)
            except (KeyError, ValueError, OSError):
                continue
            if call_instance(meta) == call:
                return name
        return None

    def _job_ids(self) -> list[str]:
        try:
            return sorted(name for name in os.listdir(self._jobs_dir) if _JOB_ID.match(name))
        except FileNotFoundError:
            return []

    # --- decision (J1) ----------------------------------------------------

    def _create_once(self, job_id: str, name: str, value: str) -> str:
        """Create ``name`` holding ``value`` unless it exists; return what it holds, ours or the earlier writer's.

        ``link(2)`` of a written temporary file: exclusive, and never visible half-written. On Windows a rename,
        which never replaces there, and works on volumes without hard links (FAT).
        """
        target = self._path(job_id, name)
        tmp = f"{target}.{value}.{secrets.token_hex(4)}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
        try:
            (os.rename if os.name == "nt" else os.link)(tmp, target)
        except FileExistsError:
            return _read_text(target) or value
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)  # a leftover temp is harmless; it goes with the job directory
        # Linked: ours is durable, so nothing that fails after this may say otherwise.
        return value

    def _create_decision(self, job_id: str, value: str) -> str:
        """Try to decide; return the decision that holds, ours or the wrapper's."""
        decision = self._create_once(job_id, "decision", value)
        return decision if decision in (_GO, _ABANDON) else _ABANDON

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
        call: CallInstance,
    ) -> str:
        """Start ``command`` for ``call`` with ``env`` as its whole environment; return once it may run.

        Raises ``JobStartError`` when the command did not start.
        """
        if not sys.executable:
            raise JobStartError("No Python interpreter is available to run the command.")
        # The host's own steps are small and synchronous, so the only await is the wait for the pid: a cancel
        # can land nowhere else, and never between publishing ``go`` and returning the job id.
        job_id, proc, marker, meta = self._spawn(command, cwd, env, timeout_s, call)
        try:
            identity = await self._await_pid(job_id, proc, marker)
        except asyncio.CancelledError:
            # Undecided: abandon the wrapper now rather than leave it waiting 30 s for a decision.
            self._create_decision(job_id, _ABANDON)
            raise
        decision = self._record_and_decide(job_id, meta, identity)
        if decision != _GO:
            raise JobStartError("The command did not start.")
        return job_id

    def _spawn(
        self,
        command: str,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: Optional[float],
        call: CallInstance,
    ) -> tuple[str, subprocess.Popen, str, dict[str, Any]]:
        """The job directory, ``meta.json``, and the wrapper, which waits for the decision (J1)."""
        job_id = f"job_{secrets.token_hex(8)}"
        job_dir = self.job_dir(job_id)
        created = datetime.now(timezone.utc)
        deadline = _ceil_ms(created + timedelta(seconds=timeout_s)) if timeout_s is not None else None
        meta: dict[str, Any] = {
            "version": 1,
            "job_id": job_id,
            "session_id": call.session_id,
            "response_id": call.response_id,
            "response_seq": call.response_seq,
            "tool_call_id": call.tool_call_id,
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
        try:
            # Checked before anything exists: a record the host could not read back would leave the job unmanaged.
            _meta_text(meta)
        except ValueError:
            raise JobStartError(
                f"Could not start the command: its job record would be over {_META_CHARS} characters."
            ) from None
        os.makedirs(self._jobs_dir, exist_ok=True)
        os.mkdir(job_dir, 0o700)
        self._write_meta(job_id, meta)

        marker = new_process_identity_marker()
        child_env = dict(env)
        child_env[PROCESS_IDENTITY_ENV] = marker
        if self._shell.powershell:
            # Python writes a pipe in the ANSI code page otherwise; the bootstrap makes everything else UTF-8. Only
            # its standard streams: UTF-8 mode would also change the encoding of every file it opens.
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-B",  # on Windows the wrapper loads its helpers by path: no bytecode written into the install
            _WRAPPER,
            "avibe-job",
            job_dir,
            repr(DECISION_TIMEOUT_S),
            str(OUTPUT_HEAD_BYTES),
            str(OUTPUT_TAIL_BYTES),
            repr(deadline.timestamp()) if deadline is not None else "-",
            *self._shell.prepare(command, job_dir),
        ]
        open(self.output_path(job_id), "ab").close()
        diagnostics = os.open(
            self._path(job_id, "wrapper.log"),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        # Windows: its own process group, so no console control event of the service reaches it, and no console
        # window, which its shell and every console program under it share.
        windows = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if _WINDOWS else {}
        )
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=diagnostics,
                stderr=diagnostics,
                start_new_session=True,
                **windows,
            )
        except OSError as exc:
            self._create_decision(job_id, _ABANDON)
            raise JobStartError(f"Could not start the command: {os_reason(exc)}.") from exc
        finally:
            os.close(diagnostics)
        self._children[job_id] = proc
        threading.Thread(target=self._reap, args=(job_id, proc), name=f"avibe-{job_id}-reaper", daemon=True).start()
        return job_id, proc, marker, meta

    def _record_and_decide(
        self, job_id: str, meta: dict[str, Any], identity: Optional[PersistedProcessIdentity]
    ) -> str:
        """Record the identity (J2), then decide: ``go`` only with an identity on record."""
        if identity is not None:
            meta["process"] = {**serialize_process_identity(identity), "pgid": identity.pid}
            self._write_meta(job_id, meta)
        return self._create_decision(job_id, _GO if identity is not None else _ABANDON)

    async def _await_pid(self, job_id: str, proc: subprocess.Popen, marker: str) -> Optional[PersistedProcessIdentity]:
        deadline = time.monotonic() + PID_TIMEOUT_S
        pid_path = self._path(job_id, "pid")
        while time.monotonic() < deadline:
            text = _read_text(pid_path)
            if text:
                heads = _heads_its_tree(job_id, proc.pid)
                if heads is None:
                    return None
                if text != str(proc.pid) or not heads:
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
            return JobStatus("exited", exit_code)
        if self._decision(job_id) != _GO:
            return JobStatus("gone")
        if self._alive(job_id) or self._orphaned_group_runs(job_id):
            return JobStatus("running")
        # The wrapper writes `exit` before it exits, so look once more.
        exit_code = self._exit_code(job_id)
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
            with _open_for_reading(head_path, binary=True) as handle:
                handle.seek(since)
                data = handle.read(min(OUTPUT_CHUNK_BYTES, head_size - since))
            return data, since + len(data)
        try:
            with _open_for_reading(self._path(job_id, "tail.log"), binary=True) as handle:
                header = _TAIL_HEADER.fullmatch(handle.readline(64))
                if header is None:
                    raise ValueError("tail.log header")
                tail_start = int(header.group(1))
                start = max(since, tail_start)
                handle.seek(start - tail_start, os.SEEK_CUR)
                data = handle.read(OUTPUT_CHUNK_BYTES)
        except (FileNotFoundError, ValueError):
            return b"", since
        return data, start + len(data)

    async def kill(self, job_id: str, *, reason: str = STOP_KILLED) -> None:
        """Terminate the job's process tree, background children included, if it is provably the job's.

        ``reason`` is recorded first (``stop_reason``), so whoever reports the
        job later can say why it ended.
        """
        # The sequence runs to its end once begun: verification records ``stopped``, so stopping short of
        # the signals, or of the SIGKILL fallback, would leave a running group. A cancel is re-raised after.
        await run_to_end(self._kill(job_id, reason))

    async def _kill(self, job_id: str, reason: str) -> None:
        identity = self._verify_for_kill(job_id, reason)
        if identity is None:
            return
        if _WINDOWS:
            if not await _terminate_job(job_id, identity.pid):
                logger.warning("Job %s job object survived termination", job_id)
        elif not await _terminate_group(identity.pid):
            logger.warning("Job %s process group %s survived termination", job_id, identity.pid)

    def _verify_for_kill(self, job_id: str, reason: str) -> Optional[PersistedProcessIdentity]:
        """The identity to signal, once the group is provably the job's, with ``reason`` recorded; else ``None``."""
        if self._decision(job_id) != _GO:
            return None
        identity = self._identity(job_id)
        if identity is None:
            logger.warning("Job %s has no recorded process identity; not signaling", job_id)
            return None
        if not (self._alive(job_id) or self._group_carries_marker(identity)):
            if not _WINDOWS and _group_exists(identity.pid):
                logger.warning("Job %s process group %s cannot be verified; not signaling", job_id, identity.pid)
            return None
        try:
            self._create_once(job_id, "stopped", reason)  # the first stopper's reason wins
        except OSError:
            # Recording is best effort at every stopper: the kill matters more than the record.
            logger.warning("Could not record why job %s was stopped", job_id, exc_info=True)
        return identity

    async def hand_over(self, job_id: str) -> str:
        """Give the job to its Watch: adopt-or-create through ``on_hand_over``, then record the id (J6).

        Raising means no Watch owns the job: it never raises after ``on_hand_over`` returned.
        """
        meta = self.meta(job_id)
        if meta.get("watch_id"):
            return meta["watch_id"]
        if self._on_hand_over is None:
            raise JobHandOverUnavailable("No Watch is available to take over the command.")
        watch_id = await self._on_hand_over(meta)
        # The Watch owns the job from here on, so nothing below may undo the handover: the recorded id
        # is a cache, and adopt-or-create finds the Watch by job id without it.
        try:
            self._record_watch(job_id, watch_id)
        except (OSError, KeyError, ValueError):
            logger.warning(
                "Job %s is Watch %s, but its meta.json could not record that", job_id, watch_id, exc_info=True
            )
        return watch_id

    def _record_watch(self, job_id: str, watch_id: str) -> None:
        meta = self.meta(job_id)
        meta["watch_id"] = watch_id
        self._write_meta(job_id, meta)

    # --- deadline (J3) and reporting --------------------------------------

    async def enforce_deadline(self, job_id: str) -> bool:
        """Kill the job if its ``deadline_at`` has passed while it runs; ``True`` if it was killed.

        Only the wrapper sees the shell exit, and it publishes the exit after draining the output, so a
        running job may already have ended on time: a live wrapper decides timeout versus exit. The
        host stops the job as the second owner once the wrapper has not, ``_WRAPPER_DECIDES_S`` late.
        """
        if not self._overdue(job_id):
            return False
        await self.kill(job_id, reason=STOP_TIMEOUT)
        return True

    def _overdue(self, job_id: str) -> bool:
        deadline_at = self.meta(job_id).get("deadline_at")
        if not deadline_at:
            return False
        late = (datetime.now(timezone.utc) - _parse_iso(deadline_at)).total_seconds()
        return late >= _WRAPPER_DECIDES_S and self.status(job_id).state == "running"

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
                # An exited job's wrapper may still drain children that hold the pipe; it needs the directory.
                if self.status(name).state == "running" or self._alive(name) or not settled(self.meta(name)):
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
        if _WINDOWS and not windows_job.process_running(identity.pid):
            return False  # an ended process stays readable for a moment, and its object for as long as it is held
        try:
            argv = psutil.Process(identity.pid).cmdline()
        except (psutil.Error, OSError):
            # Gone, a zombie, or unreadable: none of them proves the job still runs.
            return False
        job_dir = self.job_dir(job_id)
        return any(argv[i : i + 2] == ["avibe-job", job_dir] for i in range(len(argv) - 1))

    def _orphaned_group_runs(self, job_id: str) -> bool:
        """The wrapper was killed on its own (SIGKILL), but its group still provably runs the job.

        Never on Windows: the tree ends with the wrapper's handle to its job object.
        """
        if _WINDOWS:
            return False
        identity = self._identity(job_id)
        return identity is not None and _group_exists(identity.pid) and self._group_carries_marker(identity)

    def _group_carries_marker(self, identity: PersistedProcessIdentity) -> bool:
        return process_group_identity_status(identity.pid, identity, logger, "agent job") == "match"

    def _reap(self, job_id: str, proc: subprocess.Popen) -> None:
        """The reaper thread: wait for the wrapper, then drop it from the registry."""
        proc.wait()
        if self._children.get(job_id) is proc:
            del self._children[job_id]


def _heads_its_tree(job_id: str, pid: int) -> Optional[bool]:
    """Whether the wrapper ``pid`` heads the job's tree: it leads its own process group, or on Windows it is
    in the job object it creates before it writes ``pid``. ``None`` when the process is already gone."""
    if _WINDOWS:
        job = windows_job.open_job(_job_object_name(job_id))
        if job is None:
            return False
        try:
            return windows_job.job_contains(job, pid)
        finally:
            windows_job.close_handle(job)
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return None


async def _terminate_job(
    job_id: str, wrapper_pid: int, timeout_s: float = DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS
) -> bool:
    """Windows: end the verified job's tree through its job object; ``True`` once none of it runs.

    The job is ended only while the wrapper is one of its members. A job that cannot be opened, or that the
    wrapper has left, has lost or is losing its last handle, and with it every member (``KILL_ON_JOB_CLOSE``).
    """
    job = windows_job.open_job(_job_object_name(job_id))
    if job is None:
        return True
    try:
        if not windows_job.job_contains(job, wrapper_pid):
            return True
        windows_job.terminate_job(job)
        deadline = time.monotonic() + timeout_s
        # The job counts a process out before the process has ended; ``status`` asks the wrapper itself.
        while windows_job.active_processes(job) or windows_job.process_running(wrapper_pid):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True
    finally:
        windows_job.close_handle(job)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _terminate_group(pgid: int, timeout_s: float = DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS) -> bool:
    """SIGTERM the verified group, then SIGKILL what is left; ``True`` once the group is gone.

    The group was verified just before. A process group id is not reused while
    the group exists, so signaling it until it is gone reaches only its members.
    Signals go out from the event loop at once, never queued behind the shared
    worker threads; the waits between them are asynchronous.
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
            await asyncio.sleep(0.02)
        else:
            return True
    return not _group_exists(pgid)

"""Shared supervised-command runner.

One place owns the mechanics of running a user command through the stable
``core.watch_worker`` supervisor: spawn, spec handshake, timeout, cancellation,
and tree teardown. Callers own policy -- registries, localization, persistence
and result interpretation -- so this module stays a leaf with no knowledge of
watches, scheduled tasks, storage or the service layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import watch_worker
from core.process_isolation import (
    DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS,
    KILL_SIGNAL,
    PersistedProcessIdentity,
    capture_spawned_process_identity,
    isolated_subprocess_kwargs,
    new_process_identity_marker,
    process_identity_subprocess_env,
    signal_process_tree,
    terminate_and_communicate,
)

logger = logging.getLogger(__name__)

TIMEOUT_EXIT_CODE = 124
_READ_CHUNK_BYTES = 64 * 1024

#: What a capped stream puts where its dropped middle was. Plain ASCII on purpose: this
#: module is a leaf with no localization, and the marker is part of the captured bytes
#: rather than user-facing chrome. Its own length is charged to the cap, so the returned
#: output never exceeds ``max_output_bytes``.
STREAM_TRUNCATION_MARKER = b"[avibe: output truncated]\n"

#: One line is all any surface gives a command, so the cap is shared rather than
#: repeated: an uncapped ``bash -lc`` pipeline would bury the error text that follows
#: it in a failure notice, and wrap the row in ``vibe task list``.
COMMAND_PREVIEW_MAX_CHARS = 120


def command_line_preview(
    shell_command: Optional[str],
    argv: Optional[list[str]],
    *,
    max_chars: int = COMMAND_PREVIEW_MAX_CHARS,
) -> str:
    """The one-line form of a command, or ``""`` when there is none.

    Lives beside the runner because every surface that names a command has to name the
    SAME string: ``vibe task list``, ``vibe watch list``, a failure notice, and an Agent
    escalation prompt were three separate copies of this, and a user comparing the
    notice against the list had no guarantee they were reading one command.

    ``shell_command`` wins when present -- it is the string the user typed -- and an
    argv is re-quoted with ``shlex.join`` so the preview is something they could paste.
    """

    preview = (shell_command or (shlex.join(argv) if argv else "")).strip()
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 1].rstrip() + "…"


@dataclass(frozen=True)
class SupervisedCommandResult:
    """The outcome of one supervised command run.

    ``stdout``/``stderr`` are decoded with ``errors="replace"`` and returned
    verbatim: not stripped, not localized. Callers decide what to do with them.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class SupervisedCommandStartupError(RuntimeError):
    """The worker exited or broke the pipe while receiving its spec.

    ``detail`` is the raw decoded+stripped stderr (may be empty).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail or "supervised command worker exited during startup")
        self.detail = detail


async def _read_capped_stream(stream: object, max_bytes: int) -> tuple[bytes, bool]:
    """Drain ``stream`` to EOF, retaining a bounded HEAD *and* TAIL of it.

    Draining must continue past the cap: a reader that stops consuming would
    block the child once the pipe buffer fills, and the run would look like a
    timeout instead of a large output.

    Both ends are kept because both ends are read. The head holds the first error a
    build or migration printed; the tail holds the sentence that explains the exit
    status, and the tail is what every failure surface actually shows -- the notice
    and the CLI list report the LAST non-empty line of stderr. A head-only cap made
    that read a confident lie: it named the last line of the first ``max_bytes`` and
    silently dropped the real ending, on exactly the runs whose ending matters.

    The budget is split evenly between the ends, with ``STREAM_TRUNCATION_MARKER``
    charged to it, so the retained bytes never exceed ``max_bytes``. Both ends are
    moved to a line boundary -- the head back to its last newline, the tail forward
    past its first -- so no half line survives to be mistaken for a line the command
    printed, which is the same mistake a head-only cap made one level up. A
    ``max_bytes`` too small to hold the marker degrades to a head-only cap rather
    than returning nothing but chrome.
    """

    if stream is None:
        return b"", False
    # One extra byte for the newline a head with no line boundary of its own needs
    # before the marker.
    budget = max_bytes - len(STREAM_TRUNCATION_MARKER) - 1
    head_max = max(budget // 2, 0)
    tail_max = budget - head_max if budget > 0 else 0
    if tail_max <= 0:
        head_max = max_bytes
    head: list[bytes] = []
    head_bytes = 0
    # Rolling window over the end of the stream: every chunk that did not fit the head
    # enters here and the oldest bytes are dropped, so the last ``tail_max`` bytes seen
    # are always the ones being held.
    tail: deque[bytes] = deque()
    tail_bytes = 0
    total_bytes = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)  # type: ignore[attr-defined]
        if not chunk:
            break
        total_bytes += len(chunk)
        if head_bytes < head_max:
            take = min(head_max - head_bytes, len(chunk))
            head.append(chunk[:take])
            head_bytes += take
            chunk = chunk[take:]
            if not chunk:
                continue
        if tail_max <= 0:
            continue
        tail.append(chunk)
        tail_bytes += len(chunk)
        while tail_bytes > tail_max:
            oldest = tail.popleft()
            drop = min(len(oldest), tail_bytes - tail_max)
            if drop < len(oldest):
                tail.appendleft(oldest[drop:])
            tail_bytes -= drop
    if tail_max <= 0:
        # No room for a marked tail: a head-only cap, and no marker either, because the
        # cap is a hard cap and the marker would be most of what fits.
        return b"".join(head), total_bytes > head_bytes
    if total_bytes <= head_bytes + tail_bytes:
        return b"".join(head) + b"".join(tail), False
    retained_head = b"".join(head)
    boundary = retained_head.rfind(b"\n")
    if boundary != -1:
        retained_head = retained_head[: boundary + 1]
    elif retained_head:
        retained_head += b"\n"
    retained_tail = b"".join(tail)
    boundary = retained_tail.find(b"\n")
    if boundary != -1:
        retained_tail = retained_tail[boundary + 1 :]
    return retained_head + STREAM_TRUNCATION_MARKER + retained_tail, True


async def _cancel_readers(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    tasks.clear()


async def _timed_out_keeping_retained_output(
    process: asyncio.subprocess.Process,
    collector: "asyncio.Future[tuple[bytes, bytes, bool, bool]]",
    label: str,
) -> SupervisedCommandResult:
    """End a timed-out capped run, keeping what its readers already retained.

    ``terminate_and_communicate`` must NOT be used here: its ``communicate()``
    would be a SECOND consumer on the streams ``_read_capped_stream`` is already
    awaiting. Signalling the tree closes the pipes instead, so the readers reach
    EOF on their own and the collector returns everything it kept -- which is the
    point. Cancelling the readers, as this path used to, threw those buffers away
    and left a hung command reporting no output at all, the one failure mode with
    no exit status to explain itself.

    The collector is shielded so the grace period cannot cancel it: after
    ``KILL_SIGNAL`` we still want the bytes it is holding.
    """

    if process.returncode is None:
        signal_process_tree(process, signal.SIGTERM, logger, label)
    try:
        stdout, stderr, stdout_truncated, stderr_truncated = await asyncio.wait_for(
            asyncio.shield(collector),
            timeout=DEFAULT_PROCESS_TERMINATE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        signal_process_tree(process, KILL_SIGNAL, logger, label)
        stdout, stderr, stdout_truncated, stderr_truncated = await collector
    return SupervisedCommandResult(
        exit_code=TIMEOUT_EXIT_CODE,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=True,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


async def run_supervised_command(
    *,
    command: Optional[list[str]] = None,
    shell_command: Optional[str] = None,
    cwd: str,
    timeout_seconds: float,
    label: str,
    on_spawn: Optional[Callable[[int, Optional[PersistedProcessIdentity]], None]] = None,
    max_output_bytes: Optional[int] = None,
) -> SupervisedCommandResult:
    """Run one command under the stable supervisor and return its outcome.

    ``timeout_seconds <= 0`` means no timeout. ``max_output_bytes`` of ``None``
    keeps exact ``communicate()`` semantics; a value caps the retained bytes per
    stream while still draining the child to EOF.
    """

    worker_marker = new_process_identity_marker()
    spawn_env = process_identity_subprocess_env(worker_marker)
    process = await asyncio.create_subprocess_exec(
        os.path.abspath(sys.executable),
        os.fspath(Path(watch_worker.__file__).resolve()),
        cwd=cwd,
        env=spawn_env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **isolated_subprocess_kwargs(),
    )
    identity = capture_spawned_process_identity(process.pid, worker_marker)
    if on_spawn is not None:
        on_spawn(process.pid, identity)

    reader_tasks: list[asyncio.Task] = []
    try:
        if process.stdin is None:
            await terminate_and_communicate(process, logger, label)
            raise RuntimeError("watch worker supervisor stdin is unavailable")
        try:
            process.stdin.write(
                watch_worker.encode_watch_worker_spec(
                    command=list(command or []),
                    shell_command=shell_command,
                )
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            _startup_stdout, startup_stderr = await process.communicate()
            raise SupervisedCommandStartupError(
                startup_stderr.decode("utf-8", errors="replace").strip()
            ) from None
        finally:
            process.stdin.close()
        if max_output_bytes is None:
            if timeout_seconds > 0:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            else:
                stdout, stderr = await process.communicate()
            stdout_truncated = False
            stderr_truncated = False
        else:

            async def _collect() -> tuple[bytes, bytes, bool, bool]:
                stdout_task = asyncio.ensure_future(_read_capped_stream(process.stdout, max_output_bytes))
                stderr_task = asyncio.ensure_future(_read_capped_stream(process.stderr, max_output_bytes))
                reader_tasks.extend((stdout_task, stderr_task))
                (out_bytes, out_truncated), (err_bytes, err_truncated) = await asyncio.gather(
                    stdout_task,
                    stderr_task,
                )
                await process.wait()
                return out_bytes, err_bytes, out_truncated, err_truncated

            if timeout_seconds > 0:
                # ``asyncio.wait`` rather than ``wait_for``: a timeout must NOT cancel
                # the collector, because its readers are holding the retained output
                # this run still owes the user. ``_timed_out_keeping_retained_output``
                # ends it by closing the pipes instead.
                collector = asyncio.ensure_future(_collect())
                done, _still_running = await asyncio.wait(
                    {collector}, timeout=timeout_seconds
                )
                if not done:
                    return await _timed_out_keeping_retained_output(
                        process, collector, label
                    )
                stdout, stderr, stdout_truncated, stderr_truncated = await collector
            else:
                stdout, stderr, stdout_truncated, stderr_truncated = await _collect()
    except asyncio.CancelledError:
        await _cancel_readers(reader_tasks)
        await terminate_and_communicate(process, logger, label)
        raise
    except asyncio.TimeoutError:
        await _cancel_readers(reader_tasks)
        stdout, stderr = await terminate_and_communicate(process, logger, label)
        return SupervisedCommandResult(
            exit_code=TIMEOUT_EXIT_CODE,
            stdout="",
            stderr=stderr.decode("utf-8", errors="replace"),
            timed_out=True,
        )

    return SupervisedCommandResult(
        exit_code=process.returncode if process.returncode is not None else 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=False,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )

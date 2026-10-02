"""The ``bash`` tool (C-7 section 5).

Ported from Pi ``packages/coding-agent/src/core/tools/bash.ts`` (MIT, Copyright
(c) 2025 Mario Zechner): the description, parameters, tail truncation, and
status lines are Pi's. Avibe additions: every command is a job handle from its
first moment, so a command still running after the foreground window, or
started with ``watch: true``, is handed to a Watch instead of being killed, and
it runs exactly once either way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Mapping, Optional

from core.agent_core.cancel import CancelToken
from core.agent_core.tools.args import (
    ToolInputError,
    error_result,
    format_number,
    optional_bool_arg,
    optional_number_arg,
    str_arg,
    text_result,
)
from core.agent_core.tools.base import MAX_BYTES, MAX_LINES, JobHost, JobStatus, ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.jobs import STOP_ABORTED, STOP_TIMEOUT, JobHandOverUnavailable, JobStartError, LocalJobHost
from core.agent_core.tools.paths import os_reason
from core.agent_core.tools.output import JobOutput

logger = logging.getLogger(__name__)

DEFAULT_FOREGROUND_WINDOW_S = 120.0
_MAX_TIMEOUT_S = 2_147_483.647
_PROGRESS_INTERVAL_S = 0.1  # Pi's BASH_UPDATE_THROTTLE_MS
_WATCH_GRACE_S = 0.5

BASH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["command"],
    "properties": {
        "command": {"type": "string", "description": "Shell command to execute"},
        "timeout": {"type": "number", "description": "Timeout in seconds (optional, no default timeout)"},
        "watch": {
            "type": "boolean",
            "description": "Start the command as a background Watch and return immediately. Defaults to false.",
        },
    },
}


def bash_description(foreground_window_s: float = DEFAULT_FOREGROUND_WINDOW_S) -> str:
    return (
        "Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated to "
        f"last {MAX_LINES} lines or {MAX_BYTES // 1024}KB (whichever is hit first). If truncated, the output log is saved "
        "to a file; very large logs keep their beginning and end. Optionally provide a timeout in seconds. "
        f"Commands still running after {format_number(foreground_window_s)} seconds, or started with watch=true, "
        "continue in the background as an Avibe Watch; you get a follow-up message when they finish."
    )


def _append_status(text: str, status: str) -> str:
    return f"{text}\n\n{status}" if text else status


def _timeout_arg(arguments: Mapping[str, Any]) -> Optional[float]:
    try:
        timeout = optional_number_arg(arguments, "timeout")
    except ToolInputError:
        raise ToolInputError("Invalid timeout: must be a finite number of seconds") from None
    if timeout is None:
        return None
    if timeout <= 0:
        raise ToolInputError("Invalid timeout: must be a finite number of seconds")
    if timeout > _MAX_TIMEOUT_S:
        raise ToolInputError(f"Invalid timeout: maximum is {format_number(_MAX_TIMEOUT_S)} seconds")
    return timeout


def final_result(output: JobOutput, status: JobStatus, *, note: Optional[str] = None) -> ToolResult:
    """The result of a job that is no longer running, as Pi formats a finished command."""
    output.finish()
    text, truncation = output.render("(no output)")
    if note:
        text = f"{text}\n\n{note}"
    details = {**output.details(truncation), "exit_code": status.exit_code}
    if status.state == "exited" and status.exit_code == 0:
        return text_result(text, details=details)
    if status.state == "exited":
        return error_result(_append_status(text, f"Command exited with code {status.exit_code}"), details=details)
    return error_result(_append_status(text, "Command terminated without an exit code"), details=details)


def stopped_result(output: JobOutput, status_line: str) -> ToolResult:
    """The result of a job that was killed (timeout, abort): the output so far, then why it stopped."""
    output.finish()
    text, truncation = output.render("")
    return error_result(_append_status(text, status_line), details=output.details(truncation))


def _timed_out_line(timeout_s: float) -> str:
    return f"Command timed out after {format_number(timeout_s)} seconds"


def _stopped_line(reason: Optional[str], timeout_s: Optional[float]) -> Optional[str]:
    """Pi's status line for a job someone stopped, from the reason recorded when it was stopped."""
    if reason == STOP_TIMEOUT and timeout_s is not None:
        return _timed_out_line(timeout_s)
    if reason == STOP_ABORTED:
        return "Command aborted"
    return None


def handover_result(output: JobOutput, watch_id: str) -> ToolResult:
    """Avibe: the command keeps running as a Watch; the model sees the output so far and how to manage it."""
    output.poll()
    text, truncation = output.render("(no output yet)")
    text = text.rstrip("\n")
    lines = [
        f"Command is still running and is now Watch {watch_id}. You will get a follow-up message when it finishes.",
        "",
        text,
        "",
    ]
    if truncation is None:
        lines.append(output.where())
    lines += [f"Check: vibe watch show {watch_id}", f"Stop: vibe watch remove {watch_id}"]
    return text_result("\n".join(lines), details={**output.details(truncation), "watch_id": watch_id})


async def settle_bash_call(jobs: LocalJobHost, session_id: str, tool_call_id: str) -> Optional[ToolResult]:
    """The durable result for a ``bash`` call left open by a crash (``recovery.md``).

    A finished job gets its final output, a job stopped at its deadline or by an
    abort gets that result, and a running one is handed to Watch. ``None`` means
    the job is gone for another reason, never ran, or does not exist: the caller
    commits the synthetic interrupted result.
    """
    job_id = jobs.find_job(session_id, tool_call_id)
    if job_id is None:
        return None
    await jobs.enforce_deadline(job_id)
    status = jobs.status(job_id)
    if status.state == "running":
        return handover_result(JobOutput(jobs, job_id), await jobs.hand_over(job_id))
    if status.state == "exited":
        return final_result(JobOutput(jobs, job_id), status)
    line = _stopped_line(jobs.stop_reason(job_id), jobs.meta(job_id).get("timeout_s"))
    return stopped_result(JobOutput(jobs, job_id), line) if line else None


class BashTool:
    """``jobs`` is the loop's tracking wrapper around the adapter's host; ``bash`` never makes its own."""

    def __init__(self, jobs: JobHost, *, foreground_window_s: float = DEFAULT_FOREGROUND_WINDOW_S) -> None:
        self._jobs = jobs
        self._window_s = foreground_window_s
        self._spec = ToolSpec(name="bash", description=bash_description(foreground_window_s), input_schema=BASH_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            command = str_arg(arguments, "command")
            if "\x00" in command:
                raise ToolInputError("Invalid command: contains a NUL byte")
            timeout = _timeout_arg(arguments)
            watch = optional_bool_arg(arguments, "watch")
        except ToolInputError as exc:
            return error_result(str(exc))
        if not os.path.isdir(ctx.cwd):
            return error_result(f"Working directory does not exist: {ctx.cwd}\nCannot execute bash commands.")
        if not os.access(ctx.cwd, os.X_OK):
            return error_result(f"Working directory is not accessible: {ctx.cwd}\nCannot execute bash commands.")
        if ctx.cancel.cancelled:
            return error_result("Command aborted")

        # The job host enforces the timeout (its wrapper decides timeout versus exit); bash only reports
        # the recorded reason. This clock times the handover.
        started = time.monotonic()
        try:
            job_id = await self._jobs.start(
                command,
                cwd=ctx.cwd,
                env=ctx.env,
                timeout_s=timeout,
                session_id=ctx.session_id,
                tool_call_id=ctx.tool_call_id,
            )
        except JobStartError as exc:
            return error_result(str(exc))
        except OSError as exc:
            logger.warning("Could not start a bash job", exc_info=True)
            return error_result(f"Could not start the command: {os_reason(exc)}.")

        output = JobOutput(self._jobs, job_id)
        # watch=true still lets a command that ends at once report its own result instead of becoming a Watch.
        handover_at = min(self._window_s, _WATCH_GRACE_S) if watch else self._window_s
        handover_error: Optional[Exception] = None
        last_progress = ""
        while True:
            elapsed = time.monotonic() - started
            hand_over = handover_error is None and elapsed >= handover_at
            # A handover is decided only after a fresh status: a command that already ended keeps its own result.
            slice_s = 0.0 if hand_over else _PROGRESS_INTERVAL_S
            if handover_error is None:
                slice_s = min(slice_s, max(0.0, handover_at - elapsed))
            status = await self._wait_or_cancel(job_id, slice_s, ctx.cancel)

            if status is not None and status.state == "gone":
                # Whoever stopped the job recorded why; the job host or its wrapper may have stopped it first.
                line = _stopped_line(self._jobs.stop_reason(job_id), timeout)
                if line:
                    return stopped_result(output, line)
            if status is not None and status.state != "running":
                note = None
                if watch and handover_error is not None:
                    note = f"[Watch unavailable ({handover_error}); the command ran in the foreground.]"
                return final_result(output, status, note=note)
            if ctx.cancel.cancelled:
                await self._jobs.kill(job_id, reason=STOP_ABORTED)
                return stopped_result(output, "Command aborted")
            if hand_over:
                watch_id, handover_error = await self._hand_over(job_id)
                if watch_id is not None:
                    return handover_result(output, watch_id)
                continue
            if ctx.on_progress is not None and output.poll():
                tail = output.snapshot().content
                if tail != last_progress:
                    last_progress = tail
                    ctx.on_progress(tail)

    async def _hand_over(self, job_id: str) -> tuple[Optional[str], Optional[Exception]]:
        """``(watch_id, None)``, or ``(None, error)`` when no Watch owns the job and it stays in the foreground.

        ``hand_over`` raising means no Watch owns the job, and adopt-or-create is idempotent, so one
        failed attempt is tried once more; ``JobHandOverUnavailable`` means there is no Watch to try.
        """
        error: Optional[Exception] = None
        for _ in range(2):
            try:
                return await self._jobs.hand_over(job_id), None
            except JobHandOverUnavailable as exc:
                error = exc
                break
            except Exception as exc:
                error = exc
        logger.info("Job %s stays in the foreground: %s", job_id, error)
        return None, error

    async def _wait_or_cancel(self, job_id: str, seconds: float, cancel: CancelToken) -> Optional[JobStatus]:
        """The job's status after at most ``seconds``, or ``None`` as soon as ``cancel`` fires."""
        if cancel.cancelled:
            return None
        waiting = asyncio.ensure_future(self._jobs.wait(job_id, deadline_s=seconds))
        cancelled = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({waiting, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiting, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, cancelled, return_exceptions=True)
        return waiting.result() if waiting.done() and not waiting.cancelled() else None

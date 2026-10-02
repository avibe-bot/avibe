"""Durable tool-call settlement before resume (C-5/C-7 recovery.md).

The adapter calls this under its Session writer lock, before Agent.run or any
projection. A restarted call loads committed results again and skips them; each
remaining result is appended immediately. It never executes a tool/command.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Awaitable, Callable, Mapping, Optional

from core.agent_core.harness.projection import interrupted_result, open_tool_calls, validate_message_append
from core.agent_core.harness.store import ContextEntry, TranscriptStore
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from core.agent_core.tools.base import JobHost, JobStatus, ToolResult

RecoveryRenderer = Callable[[ToolCallBlock, str, JobStatus, Optional[str]], Awaitable[ToolResult]]

# recovery.md T2: no job evidence can establish whether a file mutation ran.
UNRECORDED_EFFECT = "[tool call interrupted; it may or may not have completed; re-read the file before continuing]"


async def settle_open_calls(
    *,
    session_id: str,
    store: TranscriptStore,
    jobs: JobHost,
    job_ids: Mapping[tuple[str, str], str],
    render_result: RecoveryRenderer,
) -> tuple[ContextEntry, ...]:
    """Commit one result per open call, using original-owner job lookup.

    ``job_ids`` is keyed by (response's session_id, tool_call_id), so a fork can
    find an inherited call's job. The adapter supplies bash's output-governance
    renderer; it receives (call, job_id, status, watch_id). ``JobHost.hand_over``
    must reuse a job's existing Watch when recovery retries after an interrupted
    commit. The job host owns the launch-handshake reconciliation behind status.

    Store writes remain under the caller's single-writer ownership. A concurrent
    second recovery or Agent.run for that Session is not permitted.
    Without job state, the result explicitly leaves the effect uncertain.
    The adapter settles the interrupted Turn after this step; no tool or model
    is invoked and only a new user input starts another Turn (T4).
    """
    committed: list[ContextEntry] = []
    rows = list(await store.load(session_id))
    for owner, call in open_tool_calls(rows):
        job_id = job_ids.get((owner.session_id, call.id))
        message = interrupted_result(call)
        details = {}
        if job_id is None:
            message = ToolResultMessage(call.id, call.name, (text(UNRECORDED_EFFECT),), is_error=True)
        else:
            status = jobs.status(job_id)
            details["job_id"] = job_id
            if status.state in {"running", "exited"}:
                watch_id = await jobs.hand_over(job_id) if status.state == "running" else None
                result = await render_result(deepcopy(call), job_id, status, watch_id)
                message = ToolResultMessage(call.id, call.name, result.content, result.is_error)
                details.update(deepcopy(dict(result.details)))
                details["job_id"] = job_id
                if watch_id is not None:
                    details["watch_id"] = watch_id
                if status.state == "exited":
                    details["exit_code"] = status.exit_code
        validate_message_append(rows, session_id=session_id, kind="tool_result", message=message)
        row = await store.append_tool_result(session_id, deepcopy(message), details=details)
        rows.append(row)
        committed.append(row)
    return tuple(committed)

"""C-5 projection before P3 context management.

The store supplies rows including fork ancestry. Projection sorts and copies
them, restores hook state, and repairs orphan calls without executing tools or
writing rows. System text and rehydrated messages are supplied by the caller.
Job lookup/Watch handover and output governance stay outside this pure transform.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    text,
)
from core.agent_core.tools.base import JobHost, JobStatus

# C-1 cross-provider.md; also Pi (MIT), packages/ai/src/utils/transform-messages.ts.
INTERRUPTED = "[tool call interrupted; no result recorded]"
OrphanSettler = Callable[[ContextEntry, ToolCallBlock], Optional[ToolResultMessage]]


class ProjectionError(ValueError):
    """The rows cannot be represented by this projection version."""


@dataclass(frozen=True)
class Projection:
    system: str
    messages: tuple[Message, ...]
    context_seq: int
    state: Mapping[str, Any] = field(default_factory=dict)


def interrupted_result(call: ToolCallBlock) -> ToolResultMessage:
    return ToolResultMessage(call.id, call.name, (text(INTERRUPTED),), is_error=True)


def project(
    entries: Sequence[ContextEntry],
    *,
    system: str = "",
    rehydrated: Sequence[Message] = (),
    settle_orphan: Optional[OrphanSettler] = None,
    fork_point: Optional[int] = None,
) -> Projection:
    """Project resolved ancestry; ``fork_point`` is an optional inclusive cut.

    Orphan results are placed in call order immediately after their response.
    The injected settler must return an already governed result, or None when
    the call never ran. All returned objects are detached from stored objects.
    """
    rows = sorted(
        (entry for entry in entries if fork_point is None or entry.context_seq <= fork_point),
        key=lambda entry: entry.context_seq,
    )
    state: dict[str, Any] = {}
    previous_seq = -1
    for row in rows:
        if row.context_seq <= previous_seq:
            raise ProjectionError(f"duplicate context_seq: {row.context_seq}")
        previous_seq = row.context_seq
        if row.kind in {"compaction", "context_edit"}:
            raise ProjectionError(f"{row.kind} requires P3 context management (row {row.row_id})")
        if row.kind == "agent_state":
            if row.payload.get("version") != 1 or not isinstance(row.payload.get("state"), dict):
                raise ProjectionError(f"unsupported agent_state payload (row {row.row_id})")
            state = deepcopy(row.payload["state"])
        elif row.message is None:
            raise ProjectionError(f"{row.kind} has no message (row {row.row_id})")
        if row.message is not None and any(
            isinstance(block, TextBlock) and block.ref is not None for block in row.message.content
        ):
            raise ProjectionError(f"large-content references are not supported (row {row.row_id})")

    messages: list[Message] = list(rehydrated)
    pending: list[tuple[ContextEntry, ToolCallBlock]] = []
    results: dict[str, ToolResultMessage] = {}

    def flush() -> None:
        for owner, call in pending:
            result = results.pop(call.id, None)
            if result is None and settle_orphan is not None:
                result = settle_orphan(deepcopy(owner), deepcopy(call))
            if result is None:
                result = interrupted_result(call)
            if result.tool_call_id != call.id or result.tool_name != call.name:
                raise ProjectionError(f"result identity does not match call {call.id}")
            messages.append(result)
        pending.clear()

    for row in rows:
        if row.kind == "agent_state":
            continue
        message = row.message
        if isinstance(message, ToolResultMessage):
            if message.tool_call_id not in {call.id for _, call in pending}:
                raise ProjectionError(f"tool result has no preceding call: {message.tool_call_id}")
            if message.tool_call_id in results:
                raise ProjectionError(f"duplicate tool result: {message.tool_call_id}")
            results[message.tool_call_id] = message
            continue
        flush()
        if message is not None:
            messages.append(message)
        if isinstance(message, AssistantMessage):
            ids = [call.id for call in message.tool_calls]
            if len(set(ids)) != len(ids):
                raise ProjectionError(f"duplicate tool call id in response {row.row_id}")
            pending.extend((row, call) for call in message.tool_calls)
    flush()
    return Projection(system, tuple(deepcopy(messages)), rows[-1].context_seq if rows else 0, state)


@dataclass(frozen=True)
class JobOrphanSettler:
    """Read an externally resolved job map; never spawn or hand over a job.

    ``job_ids`` keys include the original row's session, so forks settle their
    parent's jobs. The adapter supplies Watch text and final governed output.
    Missing/gone jobs yield the canonical interrupted result.
    """

    jobs: JobHost
    job_ids: Mapping[tuple[str, str], str]
    running_result: Callable[[str, ToolCallBlock], ToolResultMessage]
    exited_result: Callable[[str, JobStatus, ToolCallBlock], ToolResultMessage]

    def __call__(self, owner: ContextEntry, call: ToolCallBlock) -> Optional[ToolResultMessage]:
        job_id = self.job_ids.get((owner.session_id, call.id))
        if job_id is None:
            return None
        status = self.jobs.status(job_id)
        if status.state == "running":
            return self.running_result(job_id, call)
        if status.state == "exited":
            return self.exited_result(job_id, status, call)
        return None

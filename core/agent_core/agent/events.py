"""C-4 agent events (``agent-core-contracts/agent-event.schema.json``).

Yielded by ``Agent.run`` in process; committed context lives in transcript rows,
which these events reference by id. ``turn_id`` is ``session_turns.id``, the same
identity as ``agent_events.turn_id``; the Harness Run id stays with the adapter.
The adapter maps events onto existing Avibe outputs (``loop-control.md`` section 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Union

RunEndReason = Literal["completed", "aborted", "error", "ended_by_hook", "context_exhausted"]
CompactionReason = Literal["manual", "threshold", "overflow"]
CompactionMode = Literal["normal", "rolling", "dropped"]


@dataclass(frozen=True)
class RunStarted:
    turn_id: str
    seq: int


@dataclass(frozen=True)
class AssistantTextDelta:
    turn_id: str
    seq: int
    delta: str


@dataclass(frozen=True)
class AssistantThinkingDelta:
    turn_id: str
    seq: int
    delta: str


@dataclass(frozen=True)
class MessageCommitted:
    turn_id: str
    seq: int
    message_id: str
    context_seq: int
    final: bool


@dataclass(frozen=True)
class ToolStarted:
    turn_id: str
    seq: int
    tool_call_id: str
    name: str
    preview: str
    job_id: Optional[str] = None


@dataclass(frozen=True)
class ToolProgress:
    turn_id: str
    seq: int
    tool_call_id: str
    tail: str


@dataclass(frozen=True)
class ToolFinished:
    turn_id: str
    seq: int
    tool_call_id: str
    event_id: str
    is_error: bool
    watch_id: Optional[str] = None


@dataclass(frozen=True)
class SteerApplied:
    turn_id: str
    seq: int
    message_id: str


@dataclass(frozen=True)
class CompactionStarted:
    turn_id: str
    seq: int
    reason: CompactionReason


@dataclass(frozen=True)
class CompactionFinished:
    turn_id: str
    seq: int
    event_id: str
    reason: CompactionReason
    mode: CompactionMode
    tokens_before: int
    tokens_after_estimate: int


@dataclass(frozen=True)
class CompactionFailed:
    turn_id: str
    seq: int
    reason: CompactionReason
    error: str


@dataclass(frozen=True)
class CompactionPaused:
    """Auto-compaction paused for the Session (C-9 section 10); emitted once, at the transition."""

    turn_id: str
    seq: int
    cause: Literal["failures", "ineffective"]


@dataclass(frozen=True)
class ContextPart:
    name: Literal["system", "tools", "history", "current_request", "latest_tool_batch", "output"]
    tokens: int


@dataclass(frozen=True)
class ContextExhausted:
    """What fills a context the overflow ladder could not make fit (C-9 section 8 d)."""

    turn_id: str
    seq: int
    limit: int
    parts: tuple[ContextPart, ...]


@dataclass(frozen=True)
class RunEnded:
    turn_id: str
    seq: int
    reason: RunEndReason


@dataclass(frozen=True)
class AgentError:
    turn_id: str
    seq: int
    kind: str
    message: str


AgentEvent = Union[
    RunStarted,
    AssistantTextDelta,
    AssistantThinkingDelta,
    MessageCommitted,
    ToolStarted,
    ToolProgress,
    ToolFinished,
    SteerApplied,
    CompactionStarted,
    CompactionFinished,
    CompactionFailed,
    CompactionPaused,
    ContextExhausted,
    RunEnded,
    AgentError,
]

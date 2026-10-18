"""C-4 agent events (``agent-core-contracts/agent-event.schema.json``).

Yielded by ``Agent.run`` in process; committed context lives in transcript rows,
which these events reference by id. ``turn_id`` is ``session_turns.id``, the same
identity as ``agent_events.turn_id``; the Harness Run id stays with the adapter.
The adapter maps events onto existing Avibe outputs (``loop-control.md`` section 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Union, get_args

from core.agent_core.ai.provider import ErrorKind
from core.agent_core.messages import StopReason

RunEndReason = Literal["completed", "aborted", "error", "ended_by_hook", "context_exhausted"]
CompactionReason = Literal["threshold", "overflow"]
CompactionMode = Literal["normal", "rolling", "dropped"]
#: Who produced a failure: ``source`` is the served model (a provider error other than an overflow, an answer the loop
#: rejected, a response the transcript cannot hold); ``local`` is everything else (a Stop, an overflow or context
#: exhaustion, a hook, tool, or store error).
ErrorOrigin = Literal["source", "local"]
#: The kinds the loop names itself (C-3 section 6), beside a provider error's C-2 ``ErrorKind`` and a failed
#: response's ``StopReason``: an empty reply, what does not fit the context (C-9 section 8 d), a cancelled dependency,
#: a failed stream cleanup, and the two exceptions that have copy of their own.
LoopErrorKind = Literal[
    "empty_response",
    "context_exhausted",
    "tool_output_too_large",
    "input_too_large",
    "step_too_large",
    "dependency_cancelled",
    "stream_cleanup",
    "UnsupportedModelRoute",
    "ProviderProtocolViolation",
]
#: Every kind an ``AgentError`` can name (``tool_use`` never ends a run). Any other kind is the class name of an
#: exception the loop did not expect, and that error is local.
NAMED_ERROR_KINDS: frozenset[str] = frozenset(
    (*get_args(ErrorKind), *get_args(StopReason), *get_args(LoopErrorKind))
) - {"tool_use"}


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
    #: A completed reply that queued input took the final word from: tool-free and not failed, committed non-final
    #: only because a steer or follow-up was pending. Never narration beside tool calls or a stream error's partial.
    superseded: bool = False


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
    #: The error that decided ``reason``, the first cause; None when no error did. A diagnostic never does.
    cause: Optional[AgentError] = None


@dataclass(frozen=True)
class AgentError:
    turn_id: str
    seq: int
    kind: str
    message: str
    origin: ErrorOrigin
    #: Provider permissions survive the loop unchanged; local/protocol errors grant neither by default.
    retryable: bool = False
    continuable: bool = False
    retry_after_s: Optional[float] = None


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
    ContextExhausted,
    RunEnded,
    AgentError,
]

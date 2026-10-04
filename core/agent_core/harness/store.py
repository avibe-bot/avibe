"""C-5 transcript store interface (``agent-core-contracts/transcript.md``).

The loop is the single writer of a Session's context. Each method commits one
context entry and returns it with its ``context_seq``; the adapter implements
this over the ``messages`` and ``agent_events`` tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence

from core.agent_core.messages import AssistantMessage, Message, ToolResultMessage, UserMessage

EntryKind = Literal["input", "response", "tool_result", "compaction", "context_edit", "agent_state"]


@dataclass(frozen=True)
class ContextEntry:
    """One committed context row.

    ``row_id`` is ``messages.id`` for inputs and responses, ``agent_events.id``
    otherwise. ``message`` is set for inputs, responses, and tool results;
    ``payload`` carries the versioned shape of the other kinds. ``created_at``
    is the commit time in epoch seconds when the store knows it; C-9 reads a
    response's to tell whether the provider cache has gone cold.
    """

    session_id: str
    context_seq: int
    kind: EntryKind
    row_id: str
    message: Optional[Message] = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: Optional[float] = None


class TranscriptStore(Protocol):
    async def load(self, session_id: str) -> Sequence[ContextEntry]:
        """Context entries of the Session and its fork ancestry, ordered by ``context_seq``."""
        ...

    async def consume_input(self, session_id: str, message_id: str, message: UserMessage) -> ContextEntry:
        """Admit an already stored input row into the context as rendered for the model."""
        ...

    async def append_response(self, session_id: str, message: AssistantMessage, *, final: bool) -> ContextEntry: ...

    async def append_tool_result(
        self, session_id: str, message: ToolResultMessage, *, details: Mapping[str, Any]
    ) -> ContextEntry: ...

    async def append_payload(
        self, session_id: str, kind: Literal["compaction", "context_edit", "agent_state"], payload: Mapping[str, Any]
    ) -> ContextEntry: ...

    async def append_checkpoint_turn(self, session_id: str, payload: Mapping[str, Any]) -> str:
        """Record one checkpoint attempt (C-9 ``CheckpointTurn``) as an audit row outside the context; its id."""
        ...

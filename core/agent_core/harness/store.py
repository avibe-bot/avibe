"""C-5 transcript store interface (``agent-core-contracts/transcript.md``).

The loop is the single writer of a Session's context. ``load`` reads it; the
other writes commit one context entry and return it with its ``context_seq``,
except ``append_payloads`` (several entries in one transaction) and
``append_audit`` (a non-context row; its id). The SQLite store implements this
over the ``messages`` and ``agent_events`` tables, and one contract suite runs
the same tests on it and on the in-memory store (C-5 ``transcript.md`` §2).
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
    is the row's commit time in epoch seconds; every store sets it, as written
    and as loaded, and C-9 reads a response's to tell whether the provider
    cache has gone cold.
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

    async def append_response(
        self, session_id: str, message: AssistantMessage, *, final: bool, request: Optional[Mapping[str, Any]] = None
    ) -> ContextEntry:
        """Commit a response; ``request`` (C-9 ``ModelResponse.request``) is stored with it and read back in ``payload``.

        The loop passes ``request`` only when context management is on.
        """
        ...

    async def append_tool_result(
        self, session_id: str, message: ToolResultMessage, *, details: Mapping[str, Any]
    ) -> ContextEntry: ...

    async def append_payload(
        self, session_id: str, kind: Literal["compaction", "context_edit", "agent_state"], payload: Mapping[str, Any]
    ) -> ContextEntry: ...

    async def append_payloads(
        self, session_id: str, entries: Sequence[tuple[Literal["compaction", "context_edit", "agent_state"], Mapping[str, Any]]]
    ) -> Sequence[ContextEntry]:
        """Commit several payload entries in order, in one transaction: all of them or none (C-9 section 10)."""
        ...

    async def append_audit(
        self, session_id: str, kind: Literal["fork_turn", "attempt"], payload: Mapping[str, Any]
    ) -> str:
        """An audit row outside the context (C-10 ``ForkTurn`` or C-9 ``ModelAttempt``); its id."""
        ...

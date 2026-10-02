"""C-5 projection before P3 context management.

The store supplies rows including fork ancestry. Projection sorts and copies
them, restores hook state, and repairs orphan calls without executing tools or
writing rows. System text and rehydrated messages are supplied by the caller.
No job host or external settler is consulted. Resume settlement is the separate
write step in ``agent.recovery``; any remaining orphan gets deterministic text.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Sequence

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    require_json_value,
    text,
)

# C-1 cross-provider.md; also Pi (MIT), packages/ai/src/utils/transform-messages.ts.
INTERRUPTED = "[tool call interrupted; no result recorded]"


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


def _ordered(entries: Sequence[ContextEntry], fork_point: Optional[int] = None) -> list[ContextEntry]:
    rows = sorted(
        (entry for entry in entries if fork_point is None or entry.context_seq <= fork_point),
        key=lambda entry: entry.context_seq,
    )
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
        elif row.message is None:
            raise ProjectionError(f"{row.kind} has no message (row {row.row_id})")
        if row.message is not None and any(
            isinstance(block, TextBlock) and block.ref is not None for block in row.message.content
        ):
            raise ProjectionError(f"large-content references are not supported (row {row.row_id})")
        if isinstance(row.message, AssistantMessage):
            for call in row.message.tool_calls:
                try:
                    # Arguments remain mutable on a frozen block. Reuse the
                    # foundation's value invariant at admission and load time.
                    require_json_value(call.arguments, "arguments")
                except (ValueError, RecursionError) as error:
                    raise ProjectionError(
                        f"invalid arguments for tool call {call.id} (row {row.row_id}): {error}"
                    ) from error
    return rows


def _index_results(
    rows: Sequence[ContextEntry],
) -> tuple[dict[tuple[int, str], ToolResultMessage], dict[str, tuple[ContextEntry, ToolCallBlock]]]:
    """Associate late recovery rows with their original response, in row order."""
    pending: dict[str, tuple[ContextEntry, ToolCallBlock]] = {}
    results: dict[tuple[int, str], ToolResultMessage] = {}
    seen: set[str] = set()

    for row in rows:
        if row.kind == "agent_state":
            continue
        message = row.message
        if isinstance(message, ToolResultMessage):
            owner = pending.pop(message.tool_call_id, None)
            if owner is None:
                if message.tool_call_id in seen:
                    raise ProjectionError(f"duplicate tool result: {message.tool_call_id}")
                raise ProjectionError(f"tool result has no preceding call: {message.tool_call_id}")
            response, call = owner
            if message.tool_name != call.name:
                raise ProjectionError(f"result identity does not match call {call.id}")
            results[response.context_seq, call.id] = message
        elif isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                if call.id in pending:
                    raise ProjectionError(f"duplicate open tool call id: {call.id}")
                seen.add(call.id)
                pending[call.id] = (row, call)
    return results, pending


def open_tool_calls(entries: Sequence[ContextEntry]) -> tuple[tuple[ContextEntry, ToolCallBlock], ...]:
    """Return uncommitted outcomes in call order for the resume writer."""
    _, pending = _index_results(_ordered(entries))
    return tuple(deepcopy(list(pending.values())))


def project(
    entries: Sequence[ContextEntry],
    *,
    system: str = "",
    rehydrated: Sequence[Message] = (),
    fork_point: Optional[int] = None,
) -> Projection:
    """Project committed ancestry, with an optional inclusive fork cut.

    Results (including late recovery rows) are placed immediately after their
    response in call order. A missing result always gets INTERRUPTED. Neither
    projection nor the returned detached objects can change persisted rows.
    """
    rows = _ordered(entries, fork_point)
    results, _ = _index_results(rows)
    messages: list[Message] = list(rehydrated)
    state: Mapping[str, Any] = {}
    for row in rows:
        if row.kind == "agent_state":
            state = row.payload["state"]
            continue
        message = row.message
        if isinstance(message, ToolResultMessage):
            continue
        if message is not None:
            messages.append(message)
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                messages.append(results.get((row.context_seq, call.id), interrupted_result(call)))
    return Projection(system, tuple(deepcopy(messages)), rows[-1].context_seq if rows else 0, deepcopy(state))


def validate_message_append(
    entries: Sequence[ContextEntry],
    *,
    session_id: str,
    kind: Literal["input", "response", "tool_result"],
    message: Message,
) -> None:
    """Reject a candidate before persistence using the actual projection rules.

    Every message-bearing writer uses this boundary, including resume
    settlement. The caller owns serialization with other writers; neither
    the candidate nor the supplied entries are mutated or committed here.
    """
    seq = max((entry.context_seq for entry in entries), default=0) + 1
    candidate = ContextEntry(session_id, seq, kind, f"uncommitted-{kind}", message)
    project([*entries, candidate])

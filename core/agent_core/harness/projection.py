"""C-5 projection, with C-9 checkpoints and cleared tool results.

The store supplies rows including fork ancestry. Projection sorts and copies
them, restores loop state, applies the latest checkpoint and every context
edit, and repairs orphan calls without executing tools or writing rows.
System text and hook-rehydrated messages are supplied by the caller; a
checkpoint's own text and state were fixed when its row was written, so the
result is a pure function of the rows. No job host or external settler is
consulted. Resume settlement is the separate write step in ``agent.recovery``;
any remaining orphan gets deterministic text.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, Optional, Sequence

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
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
    #: Context-management guard state (``AgentState.context``), restored like hook state.
    context_state: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Unit:
    """An input, or a response with the results of its calls: what a cut keeps whole (C-9 section 5).

    ``entries`` pairs every projected message with its row; a synthesized
    interrupted result has none.
    """

    seq: int
    entries: tuple[tuple[Optional[ContextEntry], Message], ...]

    @property
    def lead(self) -> ContextEntry:
        entry = self.entries[0][0]
        assert entry is not None
        return entry

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(message for _, message in self.entries)


@dataclass(frozen=True)
class ContextView:
    """The context after the latest checkpoint, in units, with what the C-9 rules read."""

    compaction: Optional[ContextEntry]
    checkpoint: Optional[UserMessage]
    units: tuple[Unit, ...]
    #: Row ids of tool results a ``context_edit`` replaced.
    edited: frozenset[str]
    #: The latest ``context_compaction`` or ``context_edit`` row; usage before it is ignored.
    boundary_seq: int
    context_seq: int
    state: Mapping[str, Any]
    context_state: Mapping[str, Any]

    @property
    def messages(self) -> tuple[Message, ...]:
        head = (self.checkpoint,) if self.checkpoint is not None else ()
        return head + tuple(message for unit in self.units for message in unit.messages)


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
        if row.kind == "agent_state":
            if row.payload.get("version") != 1 or not isinstance(row.payload.get("state"), dict):
                raise ProjectionError(f"unsupported agent_state payload (row {row.row_id})")
            if not isinstance(row.payload.get("context", {}), dict):
                raise ProjectionError(f"unsupported agent_state context (row {row.row_id})")
        elif row.kind == "compaction":
            _require_compaction(row)
        elif row.kind == "context_edit":
            _require_edit(row)
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


def _require_compaction(row: ContextEntry) -> None:
    payload = row.payload
    first = payload.get("first_kept_seq")
    state = payload.get("state")
    if (
        payload.get("version") != 1
        or not isinstance(payload.get("summary"), str)
        or not isinstance(state, list)
        or not all(isinstance(item, str) for item in state)
        or not isinstance(first, int)
        or isinstance(first, bool)
        or not 0 < first <= row.context_seq
    ):
        raise ProjectionError(f"unsupported context_compaction payload (row {row.row_id})")


def _require_edit(row: ContextEntry) -> None:
    payload = row.payload
    replacement = payload.get("replacement")
    if (
        payload.get("version") != 1
        or not isinstance(payload.get("target_event_id"), str)
        or not isinstance(replacement, Mapping)
        or not isinstance(replacement.get("text"), str)
    ):
        raise ProjectionError(f"unsupported context_edit payload (row {row.row_id})")


def _index_results(
    rows: Sequence[ContextEntry],
) -> tuple[dict[tuple[int, str], ContextEntry], dict[str, tuple[ContextEntry, ToolCallBlock]]]:
    """Associate late recovery rows with their original response, in row order."""
    pending: dict[str, tuple[ContextEntry, ToolCallBlock]] = {}
    results: dict[tuple[int, str], ContextEntry] = {}
    seen: set[str] = set()

    for row in rows:
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
            results[response.context_seq, call.id] = row
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


def context_view(entries: Sequence[ContextEntry], *, fork_point: Optional[int] = None) -> ContextView:
    """The context in units: the latest checkpoint's message, then the rows from its ``first_kept_seq``.

    Results (including late recovery rows) follow their response in call
    order; a missing result is INTERRUPTED; a result whose call was summarized
    leaves with it. The latest edit of a tool result replaces its content.
    """
    rows = _ordered(entries, fork_point)
    results, _ = _index_results(rows)
    rows_by_id = {row.row_id: row for row in rows}
    state: Mapping[str, Any] = {}
    context_state: Mapping[str, Any] = {}
    compaction: Optional[ContextEntry] = None
    edits: dict[str, str] = {}
    boundary = 0
    for row in rows:
        if row.kind == "agent_state":
            state = row.payload["state"]
            context_state = row.payload.get("context", {})
        elif row.kind == "compaction":
            compaction, boundary = row, row.context_seq
        elif row.kind == "context_edit":
            target = rows_by_id.get(row.payload["target_event_id"])
            if target is None or target.kind != "tool_result" or target.context_seq >= row.context_seq:
                raise ProjectionError(f"context_edit row {row.row_id} targets no earlier tool result")
            edits[target.row_id] = row.payload["replacement"]["text"]
            boundary = row.context_seq
    first_kept = compaction.payload["first_kept_seq"] if compaction is not None else 0
    units: list[Unit] = []
    for row in rows:
        if row.kind not in {"input", "response"} or row.context_seq < first_kept:
            continue
        message = row.message
        entries_of_unit: list[tuple[Optional[ContextEntry], Message]] = [(row, message)]
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                result = results.get((row.context_seq, call.id))
                if result is None:
                    entries_of_unit.append((None, interrupted_result(call)))
                    continue
                projected = result.message
                if result.row_id in edits:
                    projected = replace(projected, content=(text(edits[result.row_id]),))
                entries_of_unit.append((result, projected))
        units.append(Unit(row.context_seq, tuple(entries_of_unit)))
    checkpoint = None
    if compaction is not None:
        checkpoint = UserMessage(
            (text(compaction.payload["summary"]), *(text(item) for item in compaction.payload["state"]))
        )
    return ContextView(
        compaction=compaction,
        checkpoint=checkpoint,
        units=tuple(deepcopy(units)),
        edited=frozenset(edits),
        boundary_seq=boundary,
        context_seq=rows[-1].context_seq if rows else 0,
        state=deepcopy(state),
        context_state=deepcopy(context_state),
    )


def project(
    entries: Sequence[ContextEntry],
    *,
    system: str = "",
    rehydrated: Sequence[Message] = (),
    fork_point: Optional[int] = None,
) -> Projection:
    """Project committed ancestry, with an optional inclusive fork cut.

    Rehydrated messages come first, then the latest checkpoint's message, then
    the kept rows (``context_view``). Neither projection nor the returned
    detached objects can change persisted rows.
    """
    view = context_view(entries, fork_point=fork_point)
    messages = (*deepcopy(tuple(rehydrated)), *view.messages)
    return Projection(system, messages, view.context_seq, view.state, view.context_state)


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

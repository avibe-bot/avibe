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
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
    origin_from_dict,
    require_json_value,
    text,
    usage_from_dict,
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
    #: Row ids of tool results whose latest ``context_edit`` cleared them (a result cut to fit can still be cleared).
    cleared: frozenset[str]
    context_seq: int
    state: Mapping[str, Any]
    #: How many of ``units``, first, are inputs the latest checkpoint keeps (``kept_inputs``, C-9 context.md §5).
    pinned: int = 0

    @property
    def messages(self) -> tuple[Message, ...]:
        return self.prefix(len(self.units))

    def prefix(self, cut: int) -> tuple[Message, ...]:
        """The projected messages before ``units[cut]``: the kept inputs of the in-flight Turn first, in order, then the
        checkpoint, whose state is the latest environment the model reads, then the units."""
        pinned = self.units[: min(self.pinned, cut)]
        head = (self.checkpoint,) if self.checkpoint is not None else ()
        return (
            *(message for unit in pinned for message in unit.messages),
            *head,
            *(message for unit in self.units[len(pinned) : cut] for message in unit.messages),
        )


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
        shape = _PAYLOAD_SHAPES.get(row.kind)
        if shape is not None:
            # Every field a later step reads is checked here, so a malformed row fails at load.
            if not shape(row.payload) or (row.kind == "compaction" and row.payload["first_kept_seq"] > row.context_seq):
                raise ProjectionError(f"unsupported {row.kind} payload (row {row.row_id})")
        elif row.message is None:
            raise ProjectionError(f"{row.kind} has no message (row {row.row_id})")
        elif row.kind == "response" and "request" in row.payload and not _REQUEST_FACTS(row.payload["request"]):
            raise ProjectionError(f"unsupported response request facts (row {row.row_id})")
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


# --- persisted payload shapes (transcript-rows.schema.json) --------------------

Check = Callable[[Any], bool]


def _object(required: Mapping[str, Check], optional: Mapping[str, Check] = {}) -> Check:
    """An exact JSON object: no unknown key, every required key present, every value valid."""

    def check(value: Any) -> bool:
        if not isinstance(value, Mapping) or set(value) - set(required) - set(optional):
            return False
        if any(key not in value for key in required):
            return False
        return all(test(value[key]) for key, test in {**required, **optional}.items() if key in value)

    return check


def _reads(reader: Callable[[Any], Any]) -> Check:
    """Valid when the foundation's own reader accepts it."""

    def check(value: Any) -> bool:
        try:
            reader(value)
        except (TypeError, ValueError):
            return False
        return True

    return check


def _one_of(*values: Any) -> Check:
    return lambda value: any(type(value) is type(item) and value == item for item in values)


def _nullable(check: Check) -> Check:
    return lambda value: value is None or check(value)


def _list(check: Check) -> Check:
    return lambda value: isinstance(value, list) and all(check(item) for item in value)


def _string(value: Any) -> bool:
    return isinstance(value, str)


def _integer(value: Any) -> bool:
    return type(value) is int


def _boolean(value: Any) -> bool:
    return type(value) is bool


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


#: ``ModelResponse.request``: what C-9 records about the request a response answered.
_REQUEST_FACTS = _object({"tokens": _count})
_PAYLOAD_SHAPES: dict[str, Check] = {
    "compaction": _object(
        {
            "version": _one_of(1),
            "mode": _one_of("normal", "rolling", "dropped"),
            "reason": _one_of("threshold", "overflow"),
            "summary": _string,
            "checkpoint": _string,
            "state": _list(_string),
            "first_kept_seq": lambda value: _integer(value) and value > 0,
            "summarized_to_seq": _count,
            "files_read": _list(_string),
            "files_read_omitted": _boolean,
            "files_modified": _list(_string),
            "files_modified_omitted": _boolean,
            "skills": _list(_object({"name": _string})),
            "skills_omitted": _boolean,
            "tokens_before": _count,
            "tokens_after_estimate": _count,
            "threshold": _integer,
        },
        {
            "previous_compaction_id": _nullable(_string),
            # Optional: a row without it keeps no inputs, as rows written before the field existed behave.
            "kept_inputs": _list(lambda value: _integer(value) and value > 0),
            "summarizer": _nullable(
                _object({"origin": _reads(origin_from_dict), "prompt_version": _string, "rounds": _count})
            ),
            "usage": _reads(usage_from_dict),
        },
    ),
    "context_edit": _object(
        {
            "version": _one_of(1),
            "target_event_id": _string,
            "replacement": _object({"text": _string}),
            "reason": _one_of("clear_old_tool_result", "fit_tool_result"),
        }
    ),
    "agent_state": _object({"version": _one_of(1), "state": lambda value: isinstance(value, dict)}),
}


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
    """The context in units: the in-flight Turn's inputs the latest checkpoint keeps, that checkpoint's message, then
    the rows from its ``first_kept_seq`` (``ContextView.prefix``).

    Results (including late recovery rows) follow their response in call
    order; a missing result is INTERRUPTED; a result whose call was summarized
    leaves with it. The latest edit of a tool result replaces its content.
    """
    rows = _ordered(entries, fork_point)
    results, _ = _index_results(rows)
    rows_by_id = {row.row_id: row for row in rows}
    state: Mapping[str, Any] = {}
    compaction: Optional[ContextEntry] = None
    edits: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for row in rows:
        if row.kind == "agent_state":
            state = row.payload["state"]
        elif row.kind == "compaction":
            compaction = row
        elif row.kind == "context_edit":
            target = rows_by_id.get(row.payload["target_event_id"])
            if target is None or target.kind != "tool_result" or target.context_seq >= row.context_seq:
                raise ProjectionError(f"context_edit row {row.row_id} targets no earlier tool result")
            edits[target.row_id] = row.payload["replacement"]["text"]
            reasons[target.row_id] = row.payload["reason"]
    first_kept = compaction.payload["first_kept_seq"] if compaction is not None else 0
    units: list[Unit] = []
    inputs = {row.context_seq: row for row in rows if row.kind == "input" and row.context_seq < first_kept}
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
    kept: list[int] = []
    if compaction is not None:
        # The checkpoint's cut fell inside the in-flight Turn: each of its inputs, the first and every steer, stays as
        # it was, in order, before the checkpoint and the kept rows (C-9 context.md section 5).
        kept = kept_inputs(compaction, inputs, first_is_input=bool(units) and units[0].lead.kind == "input")
        if any(seq not in inputs for seq in kept) or kept != sorted(set(kept)):
            raise ProjectionError(f"compaction row {compaction.row_id} keeps inputs it cannot place: {kept}")
        units[:0] = [Unit(seq, ((inputs[seq], inputs[seq].message),)) for seq in kept]
    checkpoint = None
    if compaction is not None:
        checkpoint = UserMessage(
            (text(compaction.payload["summary"]), *(text(item) for item in compaction.payload["state"]))
        )
    return ContextView(
        compaction=compaction,
        checkpoint=checkpoint,
        units=tuple(deepcopy(units)),
        cleared=frozenset(row_id for row_id, reason in reasons.items() if reason == "clear_old_tool_result"),
        context_seq=rows[-1].context_seq if rows else 0,
        state=deepcopy(state),
        pinned=len(kept),
    )


def kept_inputs(compaction: ContextEntry, inputs: Mapping[int, ContextEntry], *, first_is_input: bool) -> list[int]:
    """The inputs a checkpoint row keeps: its ``kept_inputs``; a row written before that field kept the latest input
    before its ``first_kept_seq`` when its cut fell inside a turn (the first kept unit is not an input), and the same
    rule reads it now. ``inputs``: the input rows before ``first_kept_seq``, by ``context_seq``."""
    if "kept_inputs" in compaction.payload:
        return list(compaction.payload["kept_inputs"])
    return [max(inputs)] if inputs and not first_is_input else []


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
    return Projection(system, messages, view.context_seq, view.state)


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

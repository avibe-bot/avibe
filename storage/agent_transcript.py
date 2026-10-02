"""C-5 transcript store over the ``messages`` and ``agent_events`` tables.

The Avibe Agent's model context is Avibe's own rows, with no second copy
(``docs/plans/agent-core-contracts/transcript.md``). A row is in a Session's
context exactly when its ``context_seq`` is non-null:

* inputs are existing ``messages`` rows (``user``, ``harness``,
  ``agent_initiated``, ``annotation``); consuming one sets its ``context_seq``
  and ``content_json.model`` once;
* responses are new ``assistant`` / ``result`` rows with ``content_json.model``,
  a rendered ``content_text``, and the output outbox state
  ``metadata_json.delivery``;
* tool results, checkpoints, context edits, and hook state are new
  ``agent_events`` rows with ``visibility='context'``.

Allocation. Every write takes SQLite's writer lock before its first read
(``reserve_write_lock``) and runs under a per-Session lock, so
``max(context_seq over both tables) + 1`` cannot be allocated twice, across
tasks or processes; the partial unique indexes back the rule.

Attribution. Inserted rows take ``platform`` and ``scope_id`` from the Turn's
initial input: the latest consumed input names its Turn through its accepted
Delivery, and that Turn's initial Delivery names the input row. That is the
channel the Turn answers, where ``persist_agent_message`` attributes the same
output, and a steer from another surface does not re-home the reply. An input
without a Delivery link stands for itself. ``agent_events.turn_id`` is that
Turn.

Fork. A child inherits its source's context rows up to ``anchor_seq``, read
from the top-level Session metadata ``fork_source_session_id`` and
``fork_source_context_seq`` and followed through the source's own fork. The
anchor is resolved once, when the fork is reserved
(``resolve_fork_anchor_seq``), so rows that receive a ``context_seq`` later can
never move into or out of the prefix. A fork without that key (a released fork,
or one from another backend) inherits nothing. The child's own rows continue
from ``anchor_seq + 1``.

Display. The adapter's renderer writes a response row's display copy inside
the commit transaction: ``content_text`` and, optionally, display keys of
``content_json`` next to the reserved ``model`` key (``RenderedDisplay``). It
receives the transaction's connection and the row's attribution, so a
surface-specific rewrite (the Workbench media proxy) lands with the row. The
display copy is never part of the context.

Outbox. A response row commits with ``delivery = {"state": "pending",
"parts": []}``. The adapter records a receipt for each part a surface splits it
into; the row becomes ``delivered`` only when every part has one
(``recovery.md`` D1). A response whose display text is blank (only thinking or
tool calls) has no part on any surface and commits ``delivered``.

Cancellation. A worker thread cannot be interrupted, so a cancelled write keeps
the Session's lock until its transaction settles and only then raises: when a
write call returns or raises, the rows already show whether it committed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Sequence, TypeVar, Union

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Connection, Engine

from core.agent_core.harness.store import ContextEntry, EntryKind
from core.agent_core.messages import (
    AssistantMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
)
from storage import agent_events_service, messages_service
from storage.agent_session_rows import reserve_write_lock
from storage.models import agent_events, agent_sessions, message_deliveries, messages, session_turns

INPUT_TYPES = ("user", "harness", "agent_initiated", "annotation")
RESPONSE_TYPES = ("assistant", "result")
CONTEXT_VISIBILITY = "context"
PAYLOAD_VERSION = 1

PayloadKind = Literal["compaction", "context_edit", "agent_state"]
_EVENT_TYPE_BY_KIND: dict[str, str] = {
    "tool_result": "tool_result",
    "compaction": "context_compaction",
    "context_edit": "context_edit",
    "agent_state": "agent_state",
}
_KIND_BY_EVENT_TYPE = {event_type: kind for kind, event_type in _EVENT_TYPE_BY_KIND.items()}

_T = TypeVar("_T")
MODEL_KEY = "model"


@dataclass(frozen=True)
class RenderedDisplay:
    """A response row's display copy: ``content_text`` plus display keys of ``content_json``."""

    text: str
    content: Mapping[str, Any] = field(default_factory=dict)


class DisplayRenderer(Protocol):
    """Renders a response row inside its commit transaction.

    ``platform`` and ``scope_id`` are the row's attribution (the Turn's channel).
    Returning a ``str`` sets only ``content_text``.
    """

    def __call__(
        self,
        message: AssistantMessage,
        *,
        final: bool,
        conn: Connection,
        platform: str,
        scope_id: Optional[str],
        session_id: str,
    ) -> Union[str, RenderedDisplay]: ...


def render_text(message: AssistantMessage, **_attribution: Any) -> str:
    """Default display copy of a response: its text blocks, verbatim, in order."""
    return "\n\n".join(block.text for block in message.content if isinstance(block, TextBlock) and block.text)


class TranscriptError(RuntimeError):
    """A write the context rules refuse, or rows that cannot form a context."""


@dataclass(frozen=True)
class PendingDelivery:
    """A committed response whose surface delivery has not been recorded."""

    session_id: str
    context_seq: int
    row_id: str
    final: bool
    text: str
    message: AssistantMessage
    parts: tuple[Optional[Mapping[str, Any]], ...] = ()
    """Receipts by part index from an earlier attempt; ``None`` marks a part still to send."""


@dataclass(frozen=True)
class _TurnOrigin:
    platform: str
    scope_id: Optional[str]
    turn_id: Optional[str]
    agent_name: Optional[str]
    backend: Optional[str]


class SQLiteTranscriptStore:
    """``TranscriptStore`` over Avibe's tables, plus the output outbox helpers.

    ``render`` produces a response row's display copy inside its commit
    transaction; the adapter supplies its display rendering, the default keeps
    the text blocks.
    """

    def __init__(self, engine: Engine, *, render: DisplayRenderer = render_text) -> None:
        self._engine = engine
        self._render = render
        self._locks: dict[str, asyncio.Lock] = {}

    # --- TranscriptStore ----------------------------------------------------

    async def load(self, session_id: str) -> Sequence[ContextEntry]:
        return await asyncio.to_thread(self._load, session_id)

    async def consume_input(self, session_id: str, message_id: str, message: UserMessage) -> ContextEntry:
        if not isinstance(message, UserMessage):
            raise TypeError("consume_input takes a UserMessage")
        model = _canonical({"version": PAYLOAD_VERSION, "message": message_to_dict(message)})

        def work(conn: Connection) -> ContextEntry:
            row = conn.execute(
                select(messages.c.session_id, messages.c.type, messages.c.context_seq, messages.c.content_json).where(
                    messages.c.id == message_id
                )
            ).mappings().first()
            if row is None or row["session_id"] != session_id:
                raise TranscriptError(f"input {message_id} is not a message of Session {session_id}")
            if row["type"] not in INPUT_TYPES:
                raise TranscriptError(f"message {message_id} of type {row['type']!r} is not an input")
            content = _json_object(row["content_json"], message_id)
            seq = row["context_seq"]
            if seq is not None:
                # A retried commit of the same input is the committed entry; anything
                # else would rewrite a context row after commit.
                if content.get("model") != model:
                    raise TranscriptError(f"input {message_id} is already in the context")
            else:
                seq = _next_context_seq(conn, session_id)
                conn.execute(
                    messages.update()
                    .where(messages.c.id == message_id, messages.c.context_seq.is_(None))
                    .values(context_seq=seq, content_json=json.dumps({**content, "model": model}), updated_at=_utc_now())
                )
            return ContextEntry(session_id, seq, "input", message_id, message=message, payload=model)

        return await self._write(session_id, work)

    async def append_response(self, session_id: str, message: AssistantMessage, *, final: bool) -> ContextEntry:
        if not isinstance(message, AssistantMessage):
            raise TypeError("append_response takes an AssistantMessage")
        model = _canonical({"version": PAYLOAD_VERSION, "message": message_to_dict(message)})

        def work(conn: Connection) -> ContextEntry:
            origin = _turn_origin(conn, session_id)
            display = _display(
                self._render(
                    message,
                    final=final,
                    conn=conn,
                    platform=origin.platform,
                    scope_id=origin.scope_id,
                    session_id=session_id,
                )
            )
            seq = _next_context_seq(conn, session_id)
            row = messages_service.append(
                conn,
                scope_id=origin.scope_id,
                session_id=session_id,
                platform=origin.platform,
                author="agent",
                source="agent",
                author_name=origin.agent_name,
                message_type="result" if final else "assistant",
                text=display.text,
                content={**display.content, MODEL_KEY: model},
                metadata={"delivery": {"state": "pending" if display.text.strip() else "delivered", "parts": []}},
            )
            conn.execute(messages.update().where(messages.c.id == row["id"]).values(context_seq=seq))
            return ContextEntry(session_id, seq, "response", row["id"], message=message, payload=model)

        return await self._write(session_id, work)

    async def append_tool_result(
        self, session_id: str, message: ToolResultMessage, *, details: Mapping[str, Any]
    ) -> ContextEntry:
        if not isinstance(message, ToolResultMessage):
            raise TypeError("append_tool_result takes a ToolResultMessage")
        payload: dict[str, Any] = {"version": PAYLOAD_VERSION, "message": message_to_dict(message)}
        if details:
            payload["details"] = dict(details)
        payload = _canonical(payload)
        return await self._write(
            session_id, lambda conn: self._append_event(conn, session_id, "tool_result", payload, message)
        )

    async def append_payload(self, session_id: str, kind: PayloadKind, payload: Mapping[str, Any]) -> ContextEntry:
        if kind not in ("compaction", "context_edit", "agent_state"):
            raise ValueError(f"not a payload entry kind: {kind!r}")
        data = _canonical(dict(payload))
        if not _is_current_version(data):
            raise ValueError(f"a {kind} payload needs version {PAYLOAD_VERSION}")
        return await self._write(session_id, lambda conn: self._append_event(conn, session_id, kind, data, None))

    # --- output outbox --------------------------------------------------------

    async def pending_deliveries(self, session_id: str) -> list[PendingDelivery]:
        """The Session's committed responses not yet delivered, in ``context_seq`` order."""
        return await asyncio.to_thread(self._pending_deliveries, session_id)

    async def record_delivery_part(
        self,
        session_id: str,
        row_id: str,
        *,
        index: int,
        count: int,
        native_message_id: Optional[str] = None,
    ) -> bool:
        """Record the receipt of part ``index`` of ``count``; True once every part has one.

        A part that already has a receipt keeps its first one, so a retried send is
        recorded once.
        """
        if not 0 <= index < count:
            raise ValueError(f"part {index} is outside a delivery of {count} part(s)")
        return await asyncio.to_thread(self._record_delivery_part, session_id, row_id, index, count, native_message_id)

    # --- implementation -------------------------------------------------------

    async def _write(self, session_id: str, work: Callable[[Connection], _T]) -> _T:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            commit = asyncio.ensure_future(asyncio.to_thread(self._commit, work))
            try:
                return await asyncio.shield(commit)
            except asyncio.CancelledError:
                await _settle(commit)
                raise

    def _commit(self, work: Callable[[Connection], _T]) -> _T:
        with self._engine.begin() as conn:
            reserve_write_lock(conn)
            return work(conn)

    def _append_event(
        self,
        conn: Connection,
        session_id: str,
        kind: EntryKind,
        payload: dict[str, Any],
        message: Optional[ToolResultMessage],
    ) -> ContextEntry:
        origin = _turn_origin(conn, session_id)
        seq = _next_context_seq(conn, session_id)
        row = agent_events_service.append(
            conn,
            scope_id=origin.scope_id,
            session_id=session_id,
            platform=origin.platform,
            event_type=_EVENT_TYPE_BY_KIND[kind],
            content=payload,
            agent_name=origin.agent_name,
            backend=origin.backend,
            turn_id=origin.turn_id,
            visibility=CONTEXT_VISIBILITY,
            source="agent",
            context_seq=seq,
        )
        return ContextEntry(session_id, seq, kind, row["id"], message=message, payload=payload)

    def _load(self, session_id: str) -> list[ContextEntry]:
        with self._engine.begin() as conn:
            # One read snapshot across both tables and every ancestor.
            if not conn.connection.dbapi_connection.in_transaction:
                conn.exec_driver_sql("BEGIN")
            entries = [
                entry for member, bound in _ancestry(conn, session_id) for entry in _context_rows(conn, member, bound)
            ]
        entries.sort(key=lambda entry: entry.context_seq)
        for previous, current in zip(entries, entries[1:]):
            if previous.context_seq == current.context_seq:
                raise TranscriptError(
                    f"rows {previous.row_id} and {current.row_id} share context_seq {current.context_seq}"
                )
        return entries

    def _pending_deliveries(self, session_id: str) -> list[PendingDelivery]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    messages.c.id,
                    messages.c.type,
                    messages.c.context_seq,
                    messages.c.content_text,
                    messages.c.content_json,
                    messages.c.metadata_json,
                )
                .where(
                    messages.c.session_id == session_id,
                    messages.c.context_seq.is_not(None),
                    messages.c.type.in_(RESPONSE_TYPES),
                    func.json_extract(messages.c.metadata_json, "$.delivery.state") == "pending",
                )
                .order_by(messages.c.context_seq)
            ).mappings().all()
        deliveries = []
        for row in rows:
            entry = _message_entry(session_id, row)
            assert isinstance(entry.message, AssistantMessage)
            deliveries.append(
                PendingDelivery(
                    session_id=session_id,
                    context_seq=entry.context_seq,
                    row_id=entry.row_id,
                    final=row["type"] == "result",
                    text=row["content_text"] or "",
                    message=entry.message,
                    parts=tuple(_delivery(row["metadata_json"], entry.row_id)["parts"]),
                )
            )
        return deliveries

    def _record_delivery_part(
        self, session_id: str, row_id: str, index: int, count: int, native_message_id: Optional[str]
    ) -> bool:
        with self._engine.begin() as conn:
            reserve_write_lock(conn)
            raw = conn.execute(
                select(messages.c.metadata_json).where(
                    messages.c.id == row_id,
                    messages.c.session_id == session_id,
                    messages.c.context_seq.is_not(None),
                    messages.c.type.in_(RESPONSE_TYPES),
                )
            ).scalar_one_or_none()
            if raw is None:
                raise TranscriptError(f"{row_id} is not a response of Session {session_id}")
            metadata = _json_object(raw, row_id)
            delivery = _delivery(raw, row_id)
            if delivery["state"] == "delivered":
                return True
            parts = delivery["parts"] or [None] * count
            if len(parts) != count:
                raise TranscriptError(f"{row_id} was split into {len(parts)} part(s), not {count}")
            if parts[index] is None:
                now = _utc_now()
                receipt: dict[str, Any] = {"delivered_at": now}
                if native_message_id:
                    receipt["native_message_id"] = native_message_id
                parts[index] = receipt
                state = "delivered" if all(part is not None for part in parts) else "pending"
                conn.execute(
                    messages.update()
                    .where(messages.c.id == row_id)
                    .values(
                        metadata_json=json.dumps({**metadata, "delivery": {"state": state, "parts": parts}}),
                        updated_at=now,
                    )
                )
            return all(part is not None for part in parts)


async def _settle(task: asyncio.Future[Any]) -> None:
    """Wait for ``task`` to finish, whatever cancellations arrive meanwhile."""
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            pass
    if not task.cancelled():
        # The cancelled caller does not see this outcome; retrieve it so a failed
        # commit is not reported as an unhandled task exception.
        task.exception()


# --- allocation and attribution ----------------------------------------------


def _own_bound(conn: Connection, session_id: str) -> Optional[int]:
    values = [
        conn.execute(
            select(func.max(table.c.context_seq)).where(
                table.c.session_id == session_id, table.c.context_seq.is_not(None)
            )
        ).scalar()
        for table in (messages, agent_events)
    ]
    return max((value for value in values if value is not None), default=None)


def _next_context_seq(conn: Connection, session_id: str) -> int:
    top = _own_bound(conn, session_id)
    if top is None:
        link = _fork_link(conn, session_id)
        top = link[1] if link is not None else 0
    return top + 1


def _turn_origin(conn: Connection, session_id: str) -> _TurnOrigin:
    session = conn.execute(
        select(agent_sessions.c.agent_name, agent_sessions.c.agent_backend).where(agent_sessions.c.id == session_id)
    ).first()
    if session is None:
        raise TranscriptError(f"Session {session_id} does not exist")
    latest = conn.execute(
        select(messages.c.id, messages.c.platform, messages.c.scope_id)
        .where(
            messages.c.session_id == session_id,
            messages.c.context_seq.is_not(None),
            messages.c.type.in_(INPUT_TYPES),
        )
        .order_by(messages.c.context_seq.desc())
        .limit(1)
    ).first()
    if latest is None:
        raise TranscriptError(f"Session {session_id} has consumed no input; a context row answers a Turn")
    platform, scope_id = latest.platform, latest.scope_id
    turn_id = conn.execute(
        select(message_deliveries.c.turn_id)
        .where(
            message_deliveries.c.session_id == session_id,
            message_deliveries.c.state == "accepted",
            message_deliveries.c.message_id == latest.id,
        )
        .limit(1)
    ).scalar()
    if turn_id is not None:
        initial = conn.execute(
            select(messages.c.platform, messages.c.scope_id)
            .select_from(
                session_turns.join(
                    message_deliveries, message_deliveries.c.id == session_turns.c.initial_delivery_id
                ).join(messages, messages.c.id == message_deliveries.c.message_id)
            )
            .where(session_turns.c.id == turn_id)
        ).first()
        if initial is not None:
            platform, scope_id = initial.platform, initial.scope_id
    return _TurnOrigin(platform, scope_id, turn_id, session.agent_name, session.agent_backend)


# --- fork ancestry -------------------------------------------------------------


def resolve_fork_anchor_seq(conn: Connection, source_session_id: str, anchor_message_id: Optional[str]) -> int:
    """``anchor_seq`` for a fork of ``source_session_id`` at ``anchor_message_id`` (C-5 §4).

    The largest ``context_seq`` of the source's context among rows at or before
    the anchor in transcript order, including the prefix the source itself
    inherited; the anchor may be a display-only row. The fork reservation calls
    this in its own transaction and persists the result as the child's
    ``fork_source_context_seq``.
    """
    link = _fork_link(conn, source_session_id)
    inherited = link[1] if link is not None else 0
    if anchor_message_id is None:
        return inherited
    order = messages_service.transcript_order_value()
    anchor = conn.execute(
        select(messages.c.session_id, order.label("order_at")).where(messages.c.id == anchor_message_id)
    ).first()
    if anchor is None or anchor.session_id != source_session_id:
        raise TranscriptError(f"fork anchor {anchor_message_id} is not a message of Session {source_session_id}")
    own_messages = conn.execute(
        select(func.max(messages.c.context_seq)).where(
            messages.c.session_id == source_session_id,
            messages.c.context_seq.is_not(None),
            or_(order < anchor.order_at, and_(order == anchor.order_at, messages.c.id <= anchor_message_id)),
        )
    ).scalar()
    own_events = conn.execute(
        select(func.max(agent_events.c.context_seq)).where(
            agent_events.c.session_id == source_session_id,
            agent_events.c.context_seq.is_not(None),
            agent_events.c.created_at <= anchor.order_at,
        )
    ).scalar()
    return max(value for value in (inherited, own_messages, own_events) if value is not None)


def _fork_link(conn: Connection, session_id: str) -> Optional[tuple[str, int]]:
    """``(source_session_id, anchor_seq)`` when the Session inherits a context."""
    raw = conn.execute(
        select(agent_sessions.c.metadata_json).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    try:
        metadata = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    source = str(metadata.get("fork_source_session_id") or "").strip()
    anchor = metadata.get("fork_source_context_seq")
    if not source or anchor is None:
        return None
    if not isinstance(anchor, int) or isinstance(anchor, bool) or anchor < 0:
        raise TranscriptError(f"Session {session_id} has an invalid fork_source_context_seq: {anchor!r}")
    return source, anchor


def _ancestry(conn: Connection, session_id: str) -> list[tuple[str, Optional[int]]]:
    """The Session and its fork sources, each with its ``context_seq`` upper bound."""
    chain: list[tuple[str, Optional[int]]] = [(session_id, None)]
    seen = {session_id}
    member, bound = session_id, None
    while (link := _fork_link(conn, member)) is not None:
        source_id, anchor_seq = link
        if source_id in seen:
            raise TranscriptError(f"fork ancestry of Session {session_id} loops back to {source_id}")
        bound = anchor_seq if bound is None else min(bound, anchor_seq)
        chain.append((source_id, bound))
        seen.add(source_id)
        member = source_id
    return chain


# --- reading rows --------------------------------------------------------------


def _context_rows(conn: Connection, session_id: str, bound: Optional[int]) -> list[ContextEntry]:
    entries: list[ContextEntry] = []
    for table, columns, to_entry in (
        (messages, (messages.c.type,), _message_entry),
        (agent_events, (agent_events.c.event_type, agent_events.c.visibility), _event_entry),
    ):
        conditions = [table.c.session_id == session_id, table.c.context_seq.is_not(None)]
        if bound is not None:
            conditions.append(table.c.context_seq <= bound)
        rows = conn.execute(
            select(table.c.id, table.c.context_seq, table.c.content_json, *columns).where(*conditions)
        ).mappings()
        entries.extend(to_entry(session_id, row) for row in rows)
    return entries


def _message_entry(session_id: str, row: Mapping[str, Any]) -> ContextEntry:
    row_id = row["id"]
    if row["type"] in INPUT_TYPES:
        kind: EntryKind = "input"
        expected: type = UserMessage
    elif row["type"] in RESPONSE_TYPES:
        kind, expected = "response", AssistantMessage
    else:
        raise TranscriptError(f"context row {row_id} has message type {row['type']!r}")
    payload = _versioned(_json_object(row["content_json"], row_id).get("model"), row_id)
    message = _message(payload.get("message"), expected, row_id)
    return ContextEntry(session_id, row["context_seq"], kind, row_id, message=message, payload=payload)


def _event_entry(session_id: str, row: Mapping[str, Any]) -> ContextEntry:
    row_id = row["id"]
    kind = _KIND_BY_EVENT_TYPE.get(row["event_type"])
    if kind is None or row["visibility"] != CONTEXT_VISIBILITY:
        raise TranscriptError(f"context row {row_id} is a {row['visibility']} {row['event_type']!r} event")
    payload = _versioned(_json_object(row["content_json"], row_id), row_id)
    message = _message(payload.get("message"), ToolResultMessage, row_id) if kind == "tool_result" else None
    return ContextEntry(session_id, row["context_seq"], kind, row_id, message=message, payload=payload)


def _message(value: Any, expected: type, row_id: str) -> Any:
    try:
        message = message_from_dict(value)
    except (TypeError, ValueError) as exc:
        raise TranscriptError(f"context row {row_id}: {exc}") from exc
    if not isinstance(message, expected):
        raise TranscriptError(f"context row {row_id} holds a {message.role} message")
    return message


def _versioned(value: Any, row_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not _is_current_version(value):
        raise TranscriptError(f"context row {row_id} has no version {PAYLOAD_VERSION} payload")
    return value


def _is_current_version(payload: Mapping[str, Any]) -> bool:
    version = payload.get("version")
    return isinstance(version, int) and not isinstance(version, bool) and version == PAYLOAD_VERSION


def _delivery(raw_metadata: Any, row_id: str) -> dict[str, Any]:
    delivery = _json_object(raw_metadata, row_id).get("delivery")
    parts = delivery.get("parts") if isinstance(delivery, dict) else None
    if (
        not isinstance(delivery, dict)
        or delivery.get("state") not in ("pending", "delivered")
        or not isinstance(parts, list)
        or any(part is not None and not isinstance(part, dict) for part in parts)
    ):
        raise TranscriptError(f"response {row_id} has no readable delivery state")
    return {"state": delivery["state"], "parts": list(parts)}


def _json_object(raw: Any, row_id: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise TranscriptError(f"row {row_id} holds invalid JSON") from exc
    if not isinstance(value, dict):
        raise TranscriptError(f"row {row_id} holds a JSON {type(value).__name__}, not an object")
    return value


def _display(rendered: Union[str, RenderedDisplay]) -> RenderedDisplay:
    if isinstance(rendered, str):
        return RenderedDisplay(rendered)
    if not isinstance(rendered, RenderedDisplay) or not isinstance(rendered.text, str):
        raise TypeError("a display renderer returns a str or a RenderedDisplay")
    if MODEL_KEY in rendered.content:
        raise TranscriptError(f"display content must not set the reserved {MODEL_KEY!r} key")
    return RenderedDisplay(rendered.text, _canonical(dict(rendered.content)))


def _canonical(value: dict[str, Any]) -> dict[str, Any]:
    """The value exactly as it reads back from a row."""
    return json.loads(json.dumps(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

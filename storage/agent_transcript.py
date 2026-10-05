"""C-5 transcript store over the ``messages`` and ``agent_events`` tables.

The Avibe Agent's model context is Avibe's own rows, with no second copy
(``docs/plans/agent-core-contracts/transcript.md``). A row is in a Session's
context exactly when its ``context_seq`` is non-null:

* inputs are existing ``messages`` rows (``user``, ``harness``,
  ``agent_initiated``, ``annotation``); consuming one sets its ``context_seq``
  and ``content_json.model`` once;
* responses are new ``assistant`` / ``result`` / ``error`` rows with
  ``content_json.model`` (with the C-9 request facts when the loop gives them)
  and a rendered ``content_text``;
* tool results, checkpoints, context edits, and hook and guard state are new
  ``agent_events`` rows with ``visibility='context'``;
* audit rows (C-9: a conversation attempt's usage, a checkpoint turn with its
  own attempts) are ``agent_events`` rows with ``visibility='audit'`` and no
  ``context_seq``: never context.

Every entry carries its row's ``created_at`` as epoch seconds, as written and
as loaded (C-9 reads a response's to tell a cold provider cache after a
restart). ``append_payloads`` commits several payload rows in one transaction.

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
Turn. A fork that settles the calls it inherited open before its first input
attributes those rows to its own scope, with no Turn.

Fork. A child inherits its source's context rows up to ``anchor_seq``, read
from the top-level Session metadata ``fork_source_session_id`` and
``fork_source_context_seq`` and followed through the source's own fork. The
anchor is resolved once, when the fork is reserved
(``resolve_fork_anchor_seq``), so rows that receive a ``context_seq`` later can
never move into or out of the prefix. A fork without that key (a released fork,
or one from another backend) inherits nothing. The child's own rows continue
from ``anchor_seq + 1``.

Display. A response row commits with the display text its renderer gives it.
Its display columns (``content_text``, the display keys of ``content_json``,
type ``result`` or ``error``) are written again when the dispatcher delivers
it, exactly as for the other backends' rows (``persist_agent_message`` with
``existing_row_id``). The transcript payload, ``content_json.model``, never
changes after commit. A final whose ``final_outcome`` failed commits as
``error``.

Cancellation. A worker thread cannot be interrupted, so a cancelled write keeps
the Session's lock until its transaction settles and only then raises: when a
write call returns or raises, the rows already show whether it committed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Optional, Sequence, TypeVar

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.engine import Connection, Engine

from core.agent_core.harness.store import ContextEntry, EntryKind
from core.agent_core.messages import (
    AssistantMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
    require_json_value,
)
from storage import agent_events_service, messages_service
from storage.agent_session_rows import reserve_write_lock
from storage.models import agent_events, agent_sessions, message_deliveries, messages, scopes, session_turns

INPUT_TYPES = ("user", "harness", "agent_initiated", "annotation")
# A final response is ``result``, or ``error`` when ``final_outcome`` says it failed:
# the row carries its outcome, as the other backends' terminal rows do. A final with
# nothing to show (a silent reply) is the hidden response type ``assistant``: context,
# but no transcript row, inbox reply, or unread result, as the other backends persist
# nothing visible for it.
FINAL_TYPES = ("result", "error")
RESPONSE_TYPES = ("assistant", *FINAL_TYPES)
CONTEXT_VISIBILITY = "context"
AUDIT_VISIBILITY = "audit"
AuditKind = Literal["attempt", "checkpoint_turn"]
_AUDIT_EVENT_TYPE: dict[str, str] = {"attempt": "model_attempt", "checkpoint_turn": "context_checkpoint_turn"}
# Only the Avibe Agent writes context rows; the Session's routed backend may change mid-Turn.
CONTEXT_WRITER = "avibe"
PAYLOAD_VERSION = 1

PayloadKind = Literal["compaction", "context_edit", "agent_state"]
_EVENT_TYPE_BY_KIND: dict[str, str] = {
    "tool_result": "tool_result",
    "compaction": "context_compaction",
    "context_edit": "context_edit",
    "agent_state": "agent_state",
}
_KIND_BY_EVENT_TYPE = {event_type: kind for kind, event_type in _EVENT_TYPE_BY_KIND.items()}

# ``render(message, final=...)``: a response row's commit-time display text.
DisplayRenderer = Callable[..., str]
_T = TypeVar("_T")
FinalOutcome = Literal["completed", "failed"]


def final_outcome(message: AssistantMessage) -> FinalOutcome:
    """The Turn outcome a committed final response determines by itself.

    The one rule, shared by the row's type at commit and live settlement: a final
    reply without text of its own (an unexplained refusal or safety stop, or an
    empty answer) failed; any other final completed. It is the loop's own
    empty-reply test (loop-control.md section 2). Failures only a live run can
    observe after the commit are applied by the live caller.
    """
    has_text = any(isinstance(block, TextBlock) and block.text and block.text.strip() for block in message.content)
    return "completed" if has_text else "failed"


def _response_type(message: AssistantMessage, *, final: bool, display_text: str) -> str:
    if not final:
        return "assistant"
    if final_outcome(message) == "failed":
        return "error"
    return "result" if display_text.strip() else "assistant"


def render_text(message: AssistantMessage, *, final: bool = False) -> str:
    """Default display copy of a response: its text blocks, verbatim, in order."""
    return "\n\n".join(block.text for block in message.content if isinstance(block, TextBlock) and block.text)


class TranscriptError(RuntimeError):
    """A write the context rules refuse, or rows that cannot form a context."""


@dataclass(frozen=True)
class _TurnOrigin:
    platform: str
    scope_id: Optional[str]
    turn_id: Optional[str]
    agent_name: Optional[str]
    backend: Optional[str]


class SQLiteTranscriptStore:
    """``TranscriptStore`` over Avibe's tables.

    ``render(message, final=...)`` produces a response row's commit-time
    ``content_text``; the adapter supplies its display rendering, the default keeps
    the text blocks.
    """

    def __init__(self, engine: Engine, *, render: DisplayRenderer = render_text) -> None:
        self._engine = engine
        self._render = render
        self._locks: dict[str, asyncio.Lock] = {}

    def forget(self, session_id: str) -> None:
        """Drop an idle Session's write lock; the next write creates it again."""
        lock = self._locks.get(session_id)
        if lock is not None and not lock.locked():
            del self._locks[session_id]

    # --- TranscriptStore ----------------------------------------------------

    async def load(self, session_id: str) -> Sequence[ContextEntry]:
        return await asyncio.to_thread(self._load, session_id)

    async def consume_input(self, session_id: str, message_id: str, message: UserMessage) -> ContextEntry:
        if not isinstance(message, UserMessage):
            raise TypeError("consume_input takes a UserMessage")
        model = _canonical({"version": PAYLOAD_VERSION, "message": message_to_dict(message)}, "input")

        def work(conn: Connection) -> ContextEntry:
            row = conn.execute(
                select(
                    messages.c.session_id,
                    messages.c.type,
                    messages.c.context_seq,
                    messages.c.content_json,
                    messages.c.created_at,
                ).where(messages.c.id == message_id)
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
            return ContextEntry(
                session_id, seq, "input", message_id, message=message, payload=model, created_at=_epoch(row["created_at"])
            )

        return await self._write(session_id, work)

    async def append_response(
        self,
        session_id: str,
        message: AssistantMessage,
        *,
        final: bool,
        request: Optional[Mapping[str, Any]] = None,
        agent_name: Optional[str] = None,
    ) -> ContextEntry:
        if not isinstance(message, AssistantMessage):
            raise TypeError("append_response takes an AssistantMessage")
        response: dict[str, Any] = {"version": PAYLOAD_VERSION, "message": message_to_dict(message)}
        if request is not None:
            response["request"] = dict(request)  # C-9 ``ModelResponse.request``: the anchor's request facts
        model = _canonical(response, "response")
        display_text = self._render(message, final=final)

        def work(conn: Connection) -> ContextEntry:
            origin = _turn_origin(conn, session_id, agent_name)
            seq = _next_context_seq(conn, session_id)
            row = messages_service.append(
                conn,
                scope_id=origin.scope_id,
                session_id=session_id,
                platform=origin.platform,
                author="agent",
                source="agent",
                author_name=origin.agent_name,
                message_type=_response_type(message, final=final, display_text=display_text),
                text=display_text,
                content={"model": model},
            )
            conn.execute(messages.update().where(messages.c.id == row["id"]).values(context_seq=seq))
            return ContextEntry(
                session_id, seq, "response", row["id"], message=message, payload=model, created_at=_epoch(row["created_at"])
            )

        return await self._write(session_id, work)

    async def append_tool_result(
        self,
        session_id: str,
        message: ToolResultMessage,
        *,
        details: Mapping[str, Any],
        agent_name: Optional[str] = None,
    ) -> ContextEntry:
        if not isinstance(message, ToolResultMessage):
            raise TypeError("append_tool_result takes a ToolResultMessage")
        payload: dict[str, Any] = {"version": PAYLOAD_VERSION, "message": message_to_dict(message)}
        if details:
            payload["details"] = dict(details)
        payload = _canonical(payload, "tool result")

        def work(conn: Connection) -> ContextEntry:
            # A call instance is settled once, whoever writes (a run, T2 at resume, a
            # recovery pass): checked under the writer lock, so a racing second writer
            # gets the first result back instead of a duplicate.
            settled = _settled_result(conn, session_id, message.tool_call_id)
            if settled is not None:
                return settled
            return self._append_event(conn, session_id, "tool_result", payload, message, agent_name)

        return await self._write(session_id, work)

    async def append_payload(
        self, session_id: str, kind: PayloadKind, payload: Mapping[str, Any], *, agent_name: Optional[str] = None
    ) -> ContextEntry:
        return (await self.append_payloads(session_id, [(kind, payload)], agent_name=agent_name))[0]

    async def append_payloads(
        self,
        session_id: str,
        entries: Sequence[tuple[PayloadKind, Mapping[str, Any]]],
        *,
        agent_name: Optional[str] = None,
    ) -> list[ContextEntry]:
        """Several payload rows in order, in one transaction: all of them or none (C-9 section 10)."""
        rows = [(kind, _payload(kind, payload)) for kind, payload in entries]

        def work(conn: Connection) -> list[ContextEntry]:
            return [self._append_event(conn, session_id, kind, data, None, agent_name) for kind, data in rows]

        return await self._write(session_id, work)

    async def append_audit(
        self, session_id: str, kind: AuditKind, payload: Mapping[str, Any], *, agent_name: Optional[str] = None
    ) -> str:
        """A non-context audit row (C-9 ``ModelAttempt`` or ``CheckpointTurn``); its id. Never loaded as context."""
        if kind not in _AUDIT_EVENT_TYPE:
            raise ValueError(f"not an audit kind: {kind!r}")
        data = _canonical(dict(payload), f"{kind} audit")

        def work(conn: Connection) -> str:
            origin = _turn_origin(conn, session_id, agent_name)
            row = agent_events_service.append(
                conn,
                scope_id=origin.scope_id,
                session_id=session_id,
                platform=origin.platform,
                event_type=_AUDIT_EVENT_TYPE[kind],
                content=data,
                agent_name=origin.agent_name,
                backend=origin.backend,
                turn_id=origin.turn_id,
                visibility=AUDIT_VISIBILITY,
                source="agent",
            )
            return row["id"]

        return await self._write(session_id, work)

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
        agent_name: Optional[str] = None,
    ) -> ContextEntry:
        origin = _turn_origin(conn, session_id, agent_name)
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
        return ContextEntry(
            session_id, seq, kind, row["id"], message=message, payload=payload, created_at=_epoch(row["created_at"])
        )

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
        link = fork_link(conn, session_id)
        top = link[1] if link is not None else 0
    return top + 1


def _turn_origin(conn: Connection, session_id: str, agent_name: Optional[str] = None) -> _TurnOrigin:
    """Where an inserted row belongs, and the Agent that wrote it.

    ``agent_name`` is the running Turn's Agent (its request snapshot): the Session's
    selected Agent may change mid-Turn, so the Session row is only the fallback for a
    write outside a Turn (startup recovery).
    """
    session = conn.execute(
        select(agent_sessions.c.agent_name).where(agent_sessions.c.id == session_id)
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
        # A fork settles the calls it inherited open (T2) before it consumes its first
        # input: those rows belong to the Session's own scope, outside any Turn.
        home = (
            conn.execute(
                select(scopes.c.id, scopes.c.platform)
                .select_from(agent_sessions.join(scopes, scopes.c.id == agent_sessions.c.scope_id))
                .where(agent_sessions.c.id == session_id)
            ).first()
            if fork_link(conn, session_id) is not None
            else None
        )
        if home is None:
            raise TranscriptError(f"Session {session_id} has consumed no input; a context row answers a Turn")
        return _TurnOrigin(home.platform, home.id, None, agent_name or session.agent_name, CONTEXT_WRITER)
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
    return _TurnOrigin(platform, scope_id, turn_id, agent_name or session.agent_name, CONTEXT_WRITER)


# --- fork ancestry -------------------------------------------------------------


def context_bound(conn: Connection, session_id: str) -> int:
    """The last ``context_seq`` of a Session's context, including the prefix it inherited.

    A fork of a Session with no running Turn inherits all of it: a Turn that ended
    silently or was stopped shows no row a message anchor could name.
    """
    link = fork_link(conn, session_id)
    return max(value for value in (_own_bound(conn, session_id), link[1] if link else 0) if value is not None)


def resolve_fork_anchor_seq(conn: Connection, source_session_id: str, anchor_message_id: Optional[str]) -> int:
    """``anchor_seq`` for a fork of ``source_session_id`` at ``anchor_message_id`` (C-5 §4).

    The largest ``context_seq`` of the source's context among rows at or before
    the anchor in transcript order, including the prefix the source itself
    inherited; the anchor may be a display-only row. The fork reservation calls
    this in its own transaction and persists the result as the child's
    ``fork_source_context_seq``.
    """
    link = fork_link(conn, source_session_id)
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


def _settled_result(conn: Connection, session_id: str, tool_call_id: str) -> Optional[ContextEntry]:
    """The result already committed for the latest call instance with this id in the Session's context.

    The call instance is the last response in the context (own rows and the inherited
    prefix) that carries a tool call with this id; its result is the first
    ``tool_result`` for the id after that response, in context order.
    """
    chain = _ancestry(conn, session_id)
    owner_seq: Optional[int] = None
    for member, bound in chain:
        query = (
            "SELECT MAX(m.context_seq) FROM messages AS m, "
            "json_each(json_extract(m.content_json, '$.model.message.content')) AS block "
            "WHERE m.session_id = :member AND m.context_seq IS NOT NULL "
            "AND json_extract(block.value, '$.type') = 'tool_call' "
            "AND json_extract(block.value, '$.id') = :call_id"
            + (" AND m.context_seq <= :bound" if bound is not None else "")
        )
        seq = conn.execute(
            text(query), {"member": member, "call_id": tool_call_id, "bound": bound}
        ).scalar()
        if seq is not None and (owner_seq is None or seq > owner_seq):
            owner_seq = seq
    if owner_seq is None:
        return None
    first = None
    for member, bound in chain:
        conditions = [
            agent_events.c.session_id == member,
            agent_events.c.event_type == _EVENT_TYPE_BY_KIND["tool_result"],
            agent_events.c.visibility == CONTEXT_VISIBILITY,
            agent_events.c.context_seq > owner_seq,
            func.json_extract(agent_events.c.content_json, "$.message.tool_call_id") == tool_call_id,
        ]
        if bound is not None:
            conditions.append(agent_events.c.context_seq <= bound)
        row = (
            conn.execute(
                select(
                    agent_events.c.id,
                    agent_events.c.session_id,
                    agent_events.c.context_seq,
                    agent_events.c.content_json,
                    agent_events.c.created_at,
                    agent_events.c.event_type,
                    agent_events.c.visibility,
                )
                .where(*conditions)
                .order_by(agent_events.c.context_seq)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is not None and (first is None or row["context_seq"] < first["context_seq"]):
            first = row
    return _event_entry(first["session_id"], first) if first is not None else None


def source_tool_result(
    conn: Connection, owner_session_id: str, response_seq: int, tool_call_id: str
) -> Optional[ContextEntry]:
    """The result its owner committed for a call instance a fork inherited open.

    A fork anchored between a response and its tool results inherits those calls
    open. A call's identity is its instance: the response that carries it
    (``owner_session_id`` at ``response_seq``) plus its id, because providers may
    reuse ids. Its result is the first ``tool_result`` for that id after the
    response in the owner's own rows, in context order: every call is settled
    before the next model call, so a later reuse of the id comes after it. The
    child settles with that result instead of re-deriving it from job state that
    J5 may since have pruned.
    """
    row = (
        conn.execute(
            select(
                agent_events.c.id,
                agent_events.c.session_id,
                agent_events.c.context_seq,
                agent_events.c.content_json,
                agent_events.c.created_at,
                agent_events.c.event_type,
                agent_events.c.visibility,
            )
            .where(
                agent_events.c.session_id == owner_session_id,
                agent_events.c.event_type == _EVENT_TYPE_BY_KIND["tool_result"],
                agent_events.c.visibility == CONTEXT_VISIBILITY,
                agent_events.c.context_seq > response_seq,
                func.json_extract(agent_events.c.content_json, "$.message.tool_call_id") == tool_call_id,
            )
            .order_by(agent_events.c.context_seq)
            .limit(1)
        )
        .mappings()
        .first()
    )
    return _event_entry(row["session_id"], row) if row is not None else None


def recovered_watch_ids(conn: Connection, session_id: str, turn_id: str) -> list[str]:
    """The Watches recovery handed a Turn's open calls to (``recovery.md`` T2), in context order.

    Only results T2 committed count: a call the Turn handed over itself (``watch: true``,
    the foreground window) was already reported to the model, so it is not work the
    Turn lost.
    """
    from core.agent_core.agent.recovery import RECOVERED

    rows = conn.execute(
        select(agent_events.c.content_json)
        .where(
            agent_events.c.session_id == session_id,
            agent_events.c.turn_id == turn_id,
            agent_events.c.event_type == _EVENT_TYPE_BY_KIND["tool_result"],
            agent_events.c.visibility == CONTEXT_VISIBILITY,
            func.json_extract(agent_events.c.content_json, f"$.details.{RECOVERED}") == 1,
            func.json_extract(agent_events.c.content_json, "$.details.watch_id").is_not(None),
        )
        .order_by(agent_events.c.context_seq)
    ).scalars()
    return [str(json.loads(raw)["details"]["watch_id"]) for raw in rows]


def fork_link(conn: Connection, session_id: str) -> Optional[tuple[str, int]]:
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
    while (link := fork_link(conn, member)) is not None:
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
            select(table.c.id, table.c.context_seq, table.c.content_json, table.c.created_at, *columns).where(
                *conditions
            )
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
    return ContextEntry(
        session_id, row["context_seq"], kind, row_id, message=message, payload=payload, created_at=_epoch(row["created_at"])
    )


def _event_entry(session_id: str, row: Mapping[str, Any]) -> ContextEntry:
    row_id = row["id"]
    kind = _KIND_BY_EVENT_TYPE.get(row["event_type"])
    if kind is None or row["visibility"] != CONTEXT_VISIBILITY:
        raise TranscriptError(f"context row {row_id} is a {row['visibility']} {row['event_type']!r} event")
    payload = _versioned(_json_object(row["content_json"], row_id), row_id)
    message = _message(payload.get("message"), ToolResultMessage, row_id) if kind == "tool_result" else None
    return ContextEntry(
        session_id, row["context_seq"], kind, row_id, message=message, payload=payload, created_at=_epoch(row["created_at"])
    )


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


def _json_object(raw: Any, row_id: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise TranscriptError(f"row {row_id} holds invalid JSON") from exc
    if not isinstance(value, dict):
        raise TranscriptError(f"row {row_id} holds a JSON {type(value).__name__}, not an object")
    return value


def _canonical(value: dict[str, Any], what: str) -> dict[str, Any]:
    """A detached copy of ``value``, refused unless it reads back from a row unchanged.

    A tuple, a non-str key, NaN, or bytes would otherwise be coerced on the way
    to disk, and the context would no longer be what its writer committed.
    """
    require_json_value(value, what)
    return json.loads(json.dumps(value))


def _payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if kind not in ("compaction", "context_edit", "agent_state"):
        raise ValueError(f"not a payload entry kind: {kind!r}")
    data = _canonical(dict(payload), f"{kind} payload")
    if not _is_current_version(data):
        raise ValueError(f"a {kind} payload needs version {PAYLOAD_VERSION}")
    return data


def _epoch(value: Any) -> Optional[float]:
    """A row's ``created_at`` (ISO 8601, UTC when it names no zone) as epoch seconds; None if it is not a time."""
    text_value = str(value or "").strip()
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        instant = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.timestamp()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

"""The handoff a session's first turn on a new backend carries.

A bound session that switches backend keeps its Avibe session id and transcript,
but the new backend starts a fresh native conversation
(``docs/plans/2026-10-22-session-backend-switch.md``). The switch leaves a
``backend_handoff`` marker on the session; until the new backend has both accepted
a turn carrying it and bound its native, each turn of that conversation is prefixed
with this block (``AgentInputMetadata.handoff``).

The block is mechanical and bounded: a continuity notice, the open Harness work
bound to the session, the most recent transcript that fits the budget, and a
runnable query for the rest. It reads only durable state, so a retried turn builds
it again from what is true then.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Connection

from modules.agents.catalog import display_name_for_backend

#: ``platform_specific`` key naming the handoff a turn carries; its native-start
#: receipt accepts exactly that marker (``SessionTurnManager.on_native_start``).
BACKEND_HANDOFF_TURN_KEY = "backend_handoff_id"

#: The whole block, in characters. Each transcript message is clipped first.
HANDOFF_BUDGET_CHARS = 16_000
_MESSAGE_CLIP_CHARS = 1_500
_TRANSCRIPT_FETCH_LIMIT = 60
_OPEN_WORK_LIMIT = 12

#: Message types that carry the conversation, and how the block labels them.
_TRANSCRIPT_LABELS = {
    "user": "User",
    "harness": "Harness input",
    "annotation": "Show Page annotation",
    "output": "Assistant",
    "interim": "Assistant",
    "result": "Assistant",
    "error": "Error",
}
_OPEN_WORK_LABELS = {
    "agent_run": "Delegated run",
    "watch": "Watch",
    "task": "Task",
}
#: An id the example query can quote as it is, in SQL and in the shell.
_PLAIN_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class PendingHandoff:
    """A session's pending switch handoff, read fresh from its row at dispatch."""

    handoff_id: str
    #: The row's native as it is now: empty until the new backend binds one.
    native_session_id: str
    text: str


def pending_handoff_for_row(row: Any, backend: str) -> dict[str, Any] | None:
    """The handoff a turn on ``backend`` owes, from the session row read at dispatch.

    Read by primary key, not from a routing projection: a Delivery admitted before
    the switch, or an IM run target, can carry a copy from before it. Only the
    backend the session switched to carries it; a turn routed to another backend
    (a one-off Agent run) is not continuing this conversation.
    """

    from storage.agent_session_rows import decode_metadata_object, pending_backend_handoff

    if not isinstance(row, Mapping) or str(row.get("agent_backend") or "") != str(backend or ""):
        return None
    return pending_backend_handoff(decode_metadata_object(row.get("metadata_json")))


def load_pending_handoff(row: Mapping[str, Any], handoff: Mapping[str, Any], backend: str) -> PendingHandoff:
    """Build the block for ``handoff`` on its own read connection. Blocking."""

    from storage.agent_session_rows import decode_session_value
    from storage.db import get_cached_sqlite_engine

    session_id = str(row.get("id") or "")
    with get_cached_sqlite_engine().connect() as conn:
        text = build_backend_handoff(conn, session_id, handoff, to_backend=backend)
    return PendingHandoff(
        handoff_id=str(handoff["id"]),
        native_session_id=str(decode_session_value(row.get("native_session_id")) or "").strip(),
        text=text,
    )


def build_backend_handoff(
    conn: Connection,
    session_id: str,
    handoff: Mapping[str, str],
    *,
    to_backend: str,
) -> str:
    """Render the handoff for ``session_id`` from the store as it is now."""

    from storage.messages_service import list_session_messages
    from storage.workbench_sessions_service import derive_session_harness_activities

    page = list_session_messages(
        conn,
        session_id=session_id,
        types=tuple(_TRANSCRIPT_LABELS),
        tail=True,
        limit=_TRANSCRIPT_FETCH_LIMIT,
    )
    open_work = derive_session_harness_activities(conn, session_id)
    return render_backend_handoff(
        session_id,
        handoff,
        to_backend=to_backend,
        messages=page.get("messages") or [],
        has_older=bool(page.get("next_before_id")),
        open_work=open_work,
    )


def render_backend_handoff(
    session_id: str,
    handoff: Mapping[str, str],
    *,
    to_backend: str,
    messages: Sequence[Mapping[str, Any]],
    has_older: bool,
    open_work: Sequence[Mapping[str, Any]],
    budget: int = HANDOFF_BUDGET_CHARS,
) -> str:
    """The block itself: a pure function of what ``build_backend_handoff`` read."""

    from_backend = str(handoff.get("from_backend") or "")
    from_label = display_name_for_backend(from_backend) if from_backend else "another"
    to_label = display_name_for_backend(to_backend) if to_backend else "a new"
    head = [
        "[Avibe handoff]",
        (
            f"This continues Avibe session `{session_id}`. It ran on the {from_label} backend until the user"
            f" switched it to {to_label}, so you start without that backend's context. The working directory,"
            " its files, and everything Avibe holds for this session (Watches, Tasks, delegated runs) are unchanged."
        ),
        (
            "Continue the work from the record below. Tool calls and their outputs from before the switch are not"
            " in it; read files or re-run commands when you need them."
        ),
    ]
    query = _history_query(session_id)
    tail = ["[End of Avibe handoff]"]
    if query:
        tail.insert(0, f"The full conversation is in Avibe's transcript. For example:\n{query}")

    remaining = budget - _size(head) - _size(tail)
    work_lines = _open_work_lines(open_work)
    if work_lines and remaining > 0:
        work_block = _fit(["Open work bound to this session:", *work_lines], remaining)
        remaining -= _size(work_block)
    else:
        work_block = []

    transcript = _transcript_lines(messages, remaining - len("Recent conversation, oldest first:") - 2)
    if transcript:
        omitted = has_older or len(transcript) < len(messages)
        transcript_block = [
            "Recent conversation, oldest first" + (" (earlier messages omitted):" if omitted else ":"),
            *transcript,
        ]
    else:
        transcript_block = []
    return "\n\n".join(
        "\n".join(block) for block in (head, work_block, transcript_block, tail) if block
    )


def _row_message_text(row: Mapping[str, Any]) -> str:
    text = row.get("content_text")
    if text:
        return str(text)
    try:
        content = json.loads(row.get("content_json") or "{}")
    except (TypeError, ValueError):
        return ""
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return ""


def build_fork_summary_handoff(
    conn: Connection,
    source_session_id: str,
    *,
    backend: str,
    source_message_id: str | None = None,
    budget: int = HANDOFF_BUDGET_CHARS,
) -> str:
    """Render a bounded summary handoff for a forked session without native fork."""

    from storage.messages_service import list_session_messages, transcript_order_value
    from storage.models import messages as messages_table

    anchor_id = str(source_message_id or "").strip() or None
    if anchor_id is not None:
        order_value = transcript_order_value()
        anchor_order_at = conn.execute(
            select(order_value)
            .where(
                messages_table.c.session_id == source_session_id,
                messages_table.c.id == anchor_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if anchor_order_at is None:
            return render_fork_summary_handoff(
                source_session_id,
                backend=backend,
                messages=(),
                has_older=False,
                budget=budget,
            )
        rows = (
            conn.execute(
                select(
                    messages_table.c.id,
                    messages_table.c.type,
                    messages_table.c.content_text,
                    messages_table.c.content_json,
                    messages_table.c.created_at,
                )
                .where(
                    messages_table.c.session_id == source_session_id,
                    messages_table.c.type.in_(tuple(_TRANSCRIPT_LABELS)),
                    or_(
                        order_value < anchor_order_at,
                        and_(order_value == anchor_order_at, messages_table.c.id <= anchor_id),
                    ),
                )
                .order_by(order_value.desc(), messages_table.c.id.desc())
                .limit(_TRANSCRIPT_FETCH_LIMIT + 1)
            )
            .mappings()
            .all()
        )
        has_older = len(rows) > _TRANSCRIPT_FETCH_LIMIT
        bounded = list(rows[:_TRANSCRIPT_FETCH_LIMIT])
        bounded.reverse()
        bounded_messages = [
            {
                "id": row["id"],
                "type": row["type"],
                "created_at": row["created_at"],
                "text": _row_message_text(row),
            }
            for row in bounded
        ]
        return render_fork_summary_handoff(
            source_session_id,
            backend=backend,
            messages=bounded_messages,
            has_older=has_older,
            budget=budget,
        )

    page = list_session_messages(
        conn,
        session_id=source_session_id,
        types=tuple(_TRANSCRIPT_LABELS),
        tail=True,
        limit=_TRANSCRIPT_FETCH_LIMIT,
    )
    return render_fork_summary_handoff(
        source_session_id,
        backend=backend,
        messages=page.get("messages") or [],
        has_older=bool(page.get("next_before_id")),
        budget=budget,
    )


def render_fork_summary_handoff(
    source_session_id: str,
    *,
    backend: str,
    messages: Sequence[Mapping[str, Any]],
    has_older: bool,
    budget: int = HANDOFF_BUDGET_CHARS,
) -> str:
    """Render a bounded summary handoff clearly labeled as a summary, not full native history."""

    backend_label = display_name_for_backend(backend) if backend else "this"
    source_ref = source_session_id or "unknown"
    head = [
        "[Avibe handoff]",
        (
            f"This session is a bounded summary handoff forked from Avibe session `{source_ref}` "
            f"on the {backend_label} backend. Native session fork is unavailable on this backend, "
            "so this carries a bounded summary of recent messages rather than full native history."
        ),
        (
            "Continue the work from the summary below. Earlier turns, tool calls, and their outputs "
            "are not included; read files or query the source transcript when you need them."
        ),
    ]
    query = _history_query(source_session_id) if source_session_id else None
    tail = ["[End of Avibe handoff]"]
    if query:
        tail.insert(0, f"The full source conversation is in Avibe's transcript. For example:\n{query}")

    heading_omitted = "Recent conversation, oldest first (earlier messages omitted):"
    remaining = budget - _size(head) - _size(tail)
    transcript = _transcript_lines(messages, max(0, remaining - len(heading_omitted) - 2))
    if transcript:
        omitted = has_older or len(transcript) < len(messages)
        transcript_block = [
            heading_omitted if omitted else "Recent conversation, oldest first:",
            *transcript,
        ]
    else:
        transcript_block = []
    return "\n\n".join(
        "\n".join(block) for block in (head, transcript_block, tail) if block
    )


def _history_query(session_id: str) -> str | None:
    if _PLAIN_ID.fullmatch(session_id) is None:
        return None
    sql = (
        "SELECT at, role, kind, text FROM transcript"
        f" WHERE session_id='{session_id}' ORDER BY at, message_id LIMIT 50"
    )
    return f'vibe data query --sql "{sql}" --format text'


def _open_work_lines(items: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        kind = str(item.get("item_kind") or item.get("kind") or "")
        item_id = str(item.get("id") or "")
        if (kind, item_id) in seen:
            continue
        seen.add((kind, item_id))
        label = _one_line(str(item.get("label") or item.get("description") or ""), 160)
        status = str(item.get("status") or "")
        parts = [_OPEN_WORK_LABELS.get(kind, kind or "Item"), item_id]
        if status:
            parts.append(f"({status})")
        line = "- " + " ".join(part for part in parts if part)
        lines.append(f"{line}: {label}" if label else line)
    if len(lines) > _OPEN_WORK_LIMIT:
        omitted = len(lines) - _OPEN_WORK_LIMIT
        lines = lines[:_OPEN_WORK_LIMIT] + [f"- ... {omitted} more; see `vibe watch list`, `vibe task list`, `vibe runs list`."]
    return lines


def _transcript_lines(messages: Sequence[Mapping[str, Any]], budget: int) -> list[str]:
    """The newest messages that fit ``budget``, returned oldest first."""

    picked: list[str] = []
    used = 0
    for message in reversed(messages):
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        label = _TRANSCRIPT_LABELS.get(str(message.get("type") or ""), "Message")
        when = str(message.get("created_at") or "")[:19].replace("T", " ")
        clipped = text if len(text) <= _MESSAGE_CLIP_CHARS else text[:_MESSAGE_CLIP_CHARS] + " [...]"
        entry = f"[{label}{' ' + when if when else ''}]\n{clipped}"
        if used + len(entry) + 1 > budget:
            break
        picked.append(entry)
        used += len(entry) + 1
    picked.reverse()
    return picked


def _fit(lines: list[str], budget: int) -> list[str]:
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return kept if len(kept) > 1 else []


def _one_line(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _size(lines: Sequence[str]) -> int:
    return sum(len(line) + 1 for line in lines) + 2

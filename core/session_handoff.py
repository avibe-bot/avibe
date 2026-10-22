"""The handoff a session's first turn on a new backend carries.

A bound session that switches backend keeps its Avibe session id and transcript,
but the new backend starts a fresh native conversation
(``docs/plans/2026-10-22-session-backend-switch.md``). The switch leaves a
``backend_handoff`` marker on the session; until the new backend accepts a turn,
each turn's input is prefixed with this block (``AgentInputMetadata.handoff``).

The block is mechanical and bounded: a continuity notice, the open Harness work
bound to the session, the most recent transcript that fits the budget, and a
runnable query for the rest. It reads only durable state, so a retried turn builds
it again from what is true then.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from sqlalchemy.engine import Connection

from modules.agents.catalog import display_name_for_backend

#: ``platform_specific`` key naming the handoff a turn carries; its native-start
#: receipt clears exactly that marker (``SessionTurnManager.on_native_start``).
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


def build_backend_handoff_from_store(session_id: str, handoff: Mapping[str, str], to_backend: str) -> str:
    """``build_backend_handoff`` on its own read connection; blocking, run it off the loop."""

    from storage.db import get_cached_sqlite_engine

    with get_cached_sqlite_engine().connect() as conn:
        return build_backend_handoff(conn, session_id, handoff, to_backend=to_backend)


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
    try:
        open_work = derive_session_harness_activities(conn, session_id)
    except Exception:
        open_work = []
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
        tail.insert(0, f"The full conversation is in Avibe's messages table. For example:\n{query}")

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


def _history_query(session_id: str) -> str | None:
    if _PLAIN_ID.fullmatch(session_id) is None:
        return None
    sql = (
        "SELECT created_at, type, substr(content_text,1,2000) FROM messages"
        f" WHERE session_id='{session_id}' ORDER BY created_at, id LIMIT 50 OFFSET 0"
    )
    return f'vibe data query --sql "{sql}"'


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

"""CRUD over the platform-agnostic ``messages`` table.

The workbench Inbox + per-session history both read through this
module so they get a consistent shape regardless of which platform
originated the row. ``append`` is the canonical write path —
adapters and REST routes call it instead of touching the table
directly so future invariants (e.g. SSE fan-out hooks, audit logging)
land in one place.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import Connection

from storage.db import escape_sql_like
from storage.models import agent_sessions, messages, scope_settings, scopes
from vibe.message_identity import HARNESS_TYPE, INPUT_TURN_AUTHOR_TYPES


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_message_id() -> str:
    """Time-sortable message id.

    The transcript and inbox order rows by ``(created_at, id)`` and
    ``created_at`` is second-resolution, so two rows written in the same second
    — e.g. a fast avibe turn where the user prompt and the agent result land
    together — tie on ``created_at``. A microsecond-clock prefix makes the id
    monotonic so that tie-break preserves insertion order; otherwise a random
    uuid could render the result before the prompt, or make the inbox pick the
    wrong "last" row for its activity / replied state. The random suffix keeps
    ids unique within the same microsecond.
    """
    return f"msg_{int(time.time() * 1_000_000):015x}{uuid.uuid4().hex[:8]}"


def _row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        content = json.loads(row.get("content_json") or "{}")
    except json.JSONDecodeError:
        content = {}
    try:
        metadata = json.loads(row.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": row["id"],
        "scope_id": row.get("scope_id"),
        "session_id": row.get("session_id"),
        "platform": row.get("platform"),
        "author": row.get("author"),
        "type": row.get("type"),
        "author_id": row.get("author_id"),
        "author_name": row.get("author_name"),
        "source": row.get("source"),
        "native_message_id": row.get("native_message_id"),
        "parent_native_message_id": row.get("parent_native_message_id"),
        "text": row.get("content_text") or content.get("text") or "",
        "content": content,
        "metadata": metadata,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "delivered_at": row.get("delivered_at"),
        "read_at": row.get("read_at"),
    }


_WS_RE = re.compile(r"\s+")

# Snippet window radii (chars) either side of the matched term. Tuned for a
# single-line result row: a little context before, a bit more after.
_SNIPPET_BEFORE = 40
_SNIPPET_AFTER = 50
# Fallback head shown when the term isn't found in ``content_text`` (e.g. the
# match lived in a non-text field) — keeps the row from rendering empty.
_SNIPPET_FALLBACK_HEAD = 90


def _collapse_ws(value: str) -> str:
    """Collapse any run of whitespace/newlines to a single space (no strip)."""
    return _WS_RE.sub(" ", value)


def build_snippet(content_text: str, query: str) -> dict[str, str]:
    """Split *content_text* into ``{prefix, match, suffix}`` around *query*.

    The match is located case-insensitively but ``match`` carries the ORIGINAL
    casing from the text. A window of ~``_SNIPPET_BEFORE`` chars before and
    ~``_SNIPPET_AFTER`` after is kept; whitespace/newlines are collapsed to single
    spaces so the row stays one line. A leading ``…`` marks a prefix truncated at
    the start, a trailing ``…`` a suffix truncated at the end. When the query
    isn't found, fall back to the first ~``_SNIPPET_FALLBACK_HEAD`` chars as the
    prefix with an empty match (so the API contract — three string fields — holds
    regardless of where the DB matched)."""
    text = content_text or ""
    idx = text.lower().find(query.lower())
    if idx == -1:
        head = text[:_SNIPPET_FALLBACK_HEAD]
        prefix = _collapse_ws(head)
        if len(text) > _SNIPPET_FALLBACK_HEAD:
            prefix = f"{prefix}…"
        return {"prefix": prefix, "match": "", "suffix": ""}

    end = idx + len(query)
    start = max(0, idx - _SNIPPET_BEFORE)
    stop = min(len(text), end + _SNIPPET_AFTER)

    prefix = _collapse_ws(text[start:idx])
    match = text[idx:end]  # original casing
    suffix = _collapse_ws(text[end:stop])
    if start > 0:
        prefix = f"…{prefix}"
    if stop < len(text):
        suffix = f"{suffix}…"
    return {"prefix": prefix, "match": match, "suffix": suffix}


def search_messages(
    conn: Connection,
    *,
    query: str,
    platform: str = "avibe",
    types: Optional[Iterable[str]] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Global message-content search, grouped by session.

    Substring (case-insensitive) ``LIKE`` over ``messages.content_text``, scoped
    to one ``platform`` (Workbench = ``avibe``) and a set of transcript-visible
    ``types`` (human prompts + harness prompts + the agent's rendered ``result``
    replies — all land on a message the chat actually renders, so a clicked result
    is always jumpable). Archived sessions are excluded, as are messages under an
    archived PROJECT — ``projects_service.archive_project`` disables a project by setting
    ``scope_settings.enabled = 0`` (its sessions stay ``active``), so the scope's
    disabled state is the authoritative "archived project" signal here. A scope
    with no ``scope_settings`` row is treated as enabled (legacy / folder-less
    projects never got one). ``limit`` caps the number of
    MATCHED messages scanned (newest first), so it bounds total work; the matches
    are then grouped into sessions. The snippet is built in Python (see
    :func:`build_snippet`) so the client renders ``match`` with a highlight and
    needs no offset math.

    Returns ``{"sessions": [...], "total": <#matches>, "session_count": <#sessions>}``
    where each session is ``{session_id, title, project_id, project_name,
    matches: [{id, author, source, type, created_at, snippet}]}``. Sessions are
    ordered by their most-recent match; matches are newest-first within a session.
    An empty / whitespace query short-circuits to an empty result.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return {"sessions": [], "total": 0, "session_count": 0}

    like = escape_sql_like(cleaned)
    type_list = list(types if types is not None else ("user", HARNESS_TYPE, "result"))
    effective_limit = min(max(int(limit), 1), 200)

    stmt = (
        select(
            messages.c.id,
            messages.c.session_id,
            messages.c.author,
            messages.c.source,
            messages.c.type,
            messages.c.content_text,
            messages.c.created_at,
            agent_sessions.c.title,
            scopes.c.native_id.label("project_id"),
            scopes.c.display_name.label("project_name"),
        )
        .select_from(
            messages.join(agent_sessions, agent_sessions.c.id == messages.c.session_id)
            .join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
            .join(scope_settings, scope_settings.c.scope_id == agent_sessions.c.scope_id, isouter=True)
        )
        .where(messages.c.platform == platform)
        .where(messages.c.type.in_(type_list))
        .where(messages.c.content_text.is_not(None))
        .where(messages.c.content_text.ilike(f"%{like}%", escape="\\"))
        # Archived sessions are soft-deleted — never surface their messages.
        .where(agent_sessions.c.status != "archived")
        # Archived PROJECTS are modelled as scope_settings.enabled = 0 (the
        # sessions stay active), so exclude a disabled scope's messages too. A
        # missing scope_settings row (legacy / folder-less project) is enabled.
        .where(or_(scope_settings.c.enabled.is_(None), scope_settings.c.enabled != 0))
        .order_by(messages.c.created_at.desc(), messages.c.id.desc())
        .limit(effective_limit)
    )

    rows = conn.execute(stmt).mappings().all()

    # Group by session, preserving the newest-match-first row order: the first
    # time a session appears is its most-recent match, so insertion order already
    # ranks sessions by recency and matches stay newest-first within each.
    grouped: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        session_id = row["session_id"]
        bucket = grouped.get(session_id)
        if bucket is None:
            bucket = {
                "session_id": session_id,
                "title": row["title"],
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "matches": [],
            }
            grouped[session_id] = bucket
        bucket["matches"].append(
            {
                "id": row["id"],
                "author": row["author"],
                "source": row["source"],
                "type": row["type"],
                "created_at": row["created_at"],
                "snippet": build_snippet(row["content_text"], cleaned),
            }
        )
        total += 1

    sessions = list(grouped.values())
    return {"sessions": sessions, "total": total, "session_count": len(sessions)}


def append(
    conn: Connection,
    *,
    scope_id: str,
    session_id: Optional[str],
    platform: str,
    author: str,
    message_type: Optional[str] = None,
    text: Optional[str] = None,
    content: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    author_id: Optional[str] = None,
    author_name: Optional[str] = None,
    source: Optional[str] = None,
    native_message_id: Optional[str] = None,
    parent_native_message_id: Optional[str] = None,
    delivered_at: Optional[str] = None,
    read_at: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a new message row and return its payload.

    ``content`` is the rich blob (text + attachments + tool_calls); if
    ``text`` is omitted we project ``content['text']`` into
    ``content_text`` so plain-text search keeps working.
    """

    body: dict[str, Any] = {}
    if content:
        body.update(content)
    if text is not None:
        body.setdefault("text", text)
    plain = text if text is not None else body.get("text") or None

    # Default the type from the author so legacy callers that only set ``author``
    # (e.g. show-page transcript annotations) stay correctly typed — a human row
    # must be ``user`` (not ``assistant``), or the user+result transcript filter
    # would drop it. Typed callers (inbox/IM mirror) pass message_type explicitly.
    resolved_type = message_type or ("user" if author == "user" else "assistant")
    if source == HARNESS_TYPE and author == "user" and resolved_type == "user":
        author = HARNESS_TYPE
        resolved_type = HARNESS_TYPE

    now = _utc_now_iso()
    payload = {
        "id": _new_message_id(),
        "scope_id": scope_id,
        "session_id": session_id,
        "platform": platform,
        "author": author,
        "type": resolved_type,
        "author_id": author_id,
        "author_name": author_name,
        "source": source,
        "native_message_id": native_message_id,
        "parent_native_message_id": parent_native_message_id,
        "content_text": plain,
        "content_json": json.dumps(body),
        "metadata_json": json.dumps(metadata or {}),
        "created_at": now,
        "updated_at": now,
        "delivered_at": delivered_at,
        "read_at": read_at,
    }
    conn.execute(messages.insert().values(**payload))
    return _row_to_payload(payload)


def native_message_exists(conn: Connection, *, platform: str, native_message_id: str) -> bool:
    """True when a platform/native message id has already been recorded."""
    platform = str(platform or "").strip()
    native_message_id = str(native_message_id or "").strip()
    if not platform or not native_message_id:
        return False
    row_id = conn.execute(
        select(messages.c.id)
        .where(messages.c.platform == platform)
        .where(messages.c.native_message_id == native_message_id)
        .limit(1)
    ).scalar_one_or_none()
    return row_id is not None


def get_quick_reply_chosen(conn: Connection, session_id: str, message_id: str) -> Optional[str]:
    """The label already chosen for *message_id*'s quick-reply group, or None.

    The chosen answer is recorded on the AGENT message itself (the question) as
    the single source of truth for the locked/answered state, so this is one
    row lookup — no correlating a separate, mergeable user reply. Scoped to
    *session_id* so a request for one session can't read another's message.
    """
    row = conn.execute(
        select(messages.c.content_json).where(
            messages.c.id == message_id, messages.c.session_id == session_id
        )
    ).first()
    if not row or not row[0]:
        return None
    try:
        content = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    chosen = content.get("quick_reply_chosen")
    return chosen if isinstance(chosen, str) and chosen else None


def set_quick_reply_chosen(conn: Connection, session_id: str, message_id: str, choice: str) -> bool:
    """Record *choice* as the answer to *message_id*'s quick-reply group, once.

    Returns True if newly recorded; False if the message has no such option or was
    already answered (set-once → idempotent). Writing the answer onto the agent
    message is the root of the design: the lock then derives from that one row and
    is immune to how the user reply is queued / merged / removed. Scoped to
    *session_id* so a request for one session can't mutate another's message.
    """
    row = conn.execute(
        select(messages.c.content_json).where(
            messages.c.id == message_id, messages.c.session_id == session_id
        )
    ).first()
    if not row or not row[0]:
        return False
    try:
        content = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    options = content.get("quick_replies")
    if not isinstance(options, list) or choice not in options:
        return False
    if content.get("quick_reply_chosen"):
        return False  # set-once: already answered
    content["quick_reply_chosen"] = choice
    conn.execute(
        messages.update()
        .where(messages.c.id == message_id, messages.c.session_id == session_id)
        .values(content_json=json.dumps(content))
    )
    return True


def list_session_messages(
    conn: Connection,
    *,
    session_id: str,
    after_id: Optional[str] = None,
    before_id: Optional[str] = None,
    around_id: Optional[str] = None,
    limit: int = 50,
    types: Optional[Iterable[str]] = None,
    include_metadata_sources: Iterable[str] = (),
    tail: bool = False,
) -> dict[str, Any]:
    """Return messages for one session in chronological order with cursor pagination.

    ``types`` optionally restricts the rows to a set of message types. The chat
    transcript passes ``('user', 'result')`` so the intermediate ``assistant`` /
    ``tool_call`` / ``notify`` rows — now persisted for avibe sessions too — stay
    out of the conversation view (they're the process log, not the dialogue).

    ``include_metadata_sources`` keeps rows whose ``metadata.source`` matches even
    when their type is filtered out — the chat transcript passes ``('show_page',)``
    so Show-Page transcript marks (written with ``author='agent'`` → ``type
    ='assistant'``) stay visible alongside the user/result dialogue.

    ``before_id`` returns the page immediately older than that row, still in
    chronological order. This powers upward history loading from the chat page.

    ``around_id`` centers a window on a specific message (deep-link / search
    jump): up to ``limit`` rows strictly older + the anchor + up to ``limit`` rows
    strictly newer, merged chronologically. It takes precedence over
    ``after_id`` / ``before_id`` / ``tail``. ``next_before_id`` is set when older
    rows remain, ``next_after_id`` when newer rows remain, so the chat can page in
    both directions from the centered window. An unknown ``around_id`` returns no
    messages and null cursors.

    ``tail`` returns the most-recent ``limit`` rows (still chronological) instead
    of the oldest page — used by the Chat page's reconnect/visibility gap
    recovery, which needs the RECENT window (a long chat's oldest page would
    never surface a missed latest prompt/reply). ``tail`` ignores ``after_id``
    and returns no forward cursor.
    """

    query = select(messages).where(messages.c.session_id == session_id)
    metadata_sources = list(include_metadata_sources)
    if types is not None:
        type_filter = messages.c.type.in_(list(types))
        if metadata_sources:
            query = query.where(
                or_(type_filter, func.json_extract(messages.c.metadata_json, "$.source").in_(metadata_sources))
            )
        else:
            query = query.where(type_filter)
    effective_limit = min(max(int(limit), 1), 500)
    if around_id:
        # Window centered on a specific message (deep-link / search jump). Resolve
        # the anchor's (created_at, id); an unknown id (or one in another session)
        # yields an empty window. ``query`` already carries the type/metadata
        # filter, so the older/anchor/newer sub-queries inherit it — the anchor
        # only appears if it is itself transcript-visible.
        anchor = conn.execute(
            select(messages.c.created_at).where(
                messages.c.id == around_id, messages.c.session_id == session_id
            )
        ).scalar_one_or_none()
        if anchor is None:
            return {"messages": [], "next_after_id": None, "next_before_id": None}

        older_q = (
            query.where(
                or_(
                    messages.c.created_at < anchor,
                    and_(messages.c.created_at == anchor, messages.c.id < around_id),
                )
            )
            .order_by(messages.c.created_at.desc(), messages.c.id.desc())
            .limit(effective_limit + 1)
        )
        older = [_row_to_payload(dict(row)) for row in conn.execute(older_q).mappings().all()]
        has_older = len(older) > effective_limit
        older = older[:effective_limit]
        older.reverse()

        anchor_rows = [
            _row_to_payload(dict(row))
            for row in conn.execute(query.where(messages.c.id == around_id)).mappings().all()
        ]

        newer_q = (
            query.where(
                or_(
                    messages.c.created_at > anchor,
                    and_(messages.c.created_at == anchor, messages.c.id > around_id),
                )
            )
            .order_by(messages.c.created_at.asc(), messages.c.id.asc())
            .limit(effective_limit + 1)
        )
        newer = [_row_to_payload(dict(row)) for row in conn.execute(newer_q).mappings().all()]
        has_newer = len(newer) > effective_limit
        newer = newer[:effective_limit]

        merged = older + anchor_rows + newer
        return {
            "messages": merged,
            "next_after_id": newer[-1]["id"] if has_newer and newer else None,
            "next_before_id": older[0]["id"] if has_older and older else None,
        }
    if tail:
        # Newest ``limit`` rows, then flip back to chronological for the caller.
        query = query.order_by(messages.c.created_at.desc(), messages.c.id.desc()).limit(effective_limit + 1)
        rows = [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
        has_older = len(rows) > effective_limit
        rows = rows[:effective_limit]
        rows.reverse()
        return {
            "messages": rows,
            "next_after_id": None,
            "next_before_id": rows[0]["id"] if has_older and rows else None,
        }
    if before_id:
        anchor = conn.execute(
            select(messages.c.created_at).where(messages.c.id == before_id)
        ).scalar_one_or_none()
        if anchor is not None:
            query = query.where(
                or_(
                    messages.c.created_at < anchor,
                    and_(messages.c.created_at == anchor, messages.c.id < before_id),
                )
            )
        query = query.order_by(messages.c.created_at.desc(), messages.c.id.desc()).limit(effective_limit + 1)
        rows = [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
        has_older = len(rows) > effective_limit
        rows = rows[:effective_limit]
        rows.reverse()
        return {
            "messages": rows,
            "next_after_id": None,
            "next_before_id": rows[0]["id"] if has_older and rows else None,
        }
    if after_id:
        anchor = conn.execute(
            select(messages.c.created_at).where(messages.c.id == after_id)
        ).scalar_one_or_none()
        if anchor is not None:
            query = query.where(
                or_(
                    messages.c.created_at > anchor,
                    and_(messages.c.created_at == anchor, messages.c.id > after_id),
                )
            )
    query = query.order_by(messages.c.created_at.asc(), messages.c.id.asc()).limit(effective_limit + 1)
    rows = [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
    # Probe one extra row against the clamped page size: a full page alone does
    # not prove there is another page, but the extra row does.
    has_newer = len(rows) > effective_limit
    rows = rows[:effective_limit]
    next_after = rows[-1]["id"] if has_newer and rows else None
    return {"messages": rows, "next_after_id": next_after, "next_before_id": None}


def first_user_text(conn: Connection, session_id: str) -> str:
    """Return the first visible user text for a session, if any."""

    row = conn.execute(
        select(messages.c.content_text, messages.c.content_json)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == "user")
        .order_by(messages.c.created_at.asc(), messages.c.id.asc())
        .limit(1)
    ).first()
    if row is None:
        return ""
    text = str(row[0] or "").strip()
    if text:
        return text
    try:
        content = json.loads(row[1] or "{}")
    except json.JSONDecodeError:
        return ""
    return str(content.get("text") or "").strip() if isinstance(content, dict) else ""


# --- Send-while-busy queue + per-session draft -----------------------------
# Both reuse the ``messages`` table via dedicated ``type`` values so no extra
# table is needed (the queue is ephemeral operational state, not conversation):
#   type='queued' — a message the user sent while a turn was in flight; flushed
#                   (merged, in order) into one dispatch when the turn ends.
#   type='draft'  — the user's unsent compose text for a session; one row per
#                   session, persisted so switching sessions/devices keeps it.
# Both carry author='user'; the transcript (user/result/notify), inbox and
# unread queries are all type-filtered, so neither leaks into the conversation.

QUEUED_TYPE = "queued"
DRAFT_TYPE = "draft"
# A reserved-but-not-yet-accepted user row: persisted BEFORE dispatch (so it
# reserves its (created_at, id) for correct ordering) but hidden from the
# transcript, the queue AND the inbox until the controller decides whether the
# turn started (→ promote to 'user') or must be queued (→ promote to 'queued').
# This stops another tab from briefly seeing the row as a sent prompt during the
# dispatch window (Codex P2).
PENDING_TYPE = "pending"
# Hidden row used only to keep native-message-id dedupe coverage after multiple
# queued harness callbacks are coalesced into one dispatched turn.
HARNESS_DEDUPE_TYPE = "harness_dedupe"
# An INVISIBLE, agent-authored terminal marker persisted when a turn completes
# NORMALLY but produces no user-visible message — a ``<silent>``-stripped or empty
# final reply, or a reply-less bookkeeping turn (common for watch/scheduled
# orchestration). It exists ONLY so the activity grouping can close such a turn as
# DONE instead of misreading "activity rows + no terminal" as ``interrupted``. It is
# kept out of every user-facing surface by the allowlist reads (TRANSCRIPT_TYPES,
# inbox preview, unread, web-push, live publish) and, being author='agent', is listed
# in NON_CONVERSATION_TYPES below so it never bumps the inbox activity clock / last
# author. Never delivered to IM (avibe-persistence only).
SILENT_TYPE = "silent"
# Types that must never count as inbox conversation activity: the ephemeral user rows
# above plus the invisible agent silent-completion marker.
NON_CONVERSATION_TYPES = (QUEUED_TYPE, DRAFT_TYPE, PENDING_TYPE, HARNESS_DEDUPE_TYPE, SILENT_TYPE)

# The transcript-visible types — the SINGLE source of truth shared by the
# history fetch (``list_session_messages``) AND the live ``message.new`` publish
# gate, so what a page loads and what it receives over the stream are identical.
# Excludes the agent's process log (``assistant`` / ``tool_call``) and ``system``
# (which isn't persisted at all). Harness-triggered prompts have their own type
# so they cannot be mistaken for human input. ``show_page`` transcript marks are
# kept via a metadata-source
# override in the fetch even though their row type is ``assistant``. ``error`` is a
# terminal FAILED result (turned the dot red): shown in the conversation like any
# terminal message, but the unread queries below stay ``result``-only so a failure
# is not counted as an unread agent reply.
TRANSCRIPT_TYPES = ("user", HARNESS_TYPE, "result", "notify", "error")


def enqueue_queued(
    conn: Connection,
    *,
    scope_id: str,
    session_id: str,
    text: str,
    author_id: Optional[str] = None,
    author_name: Optional[str] = None,
) -> dict[str, Any]:
    """Append a queued ('send while busy') message for a session."""
    return append(
        conn,
        scope_id=scope_id,
        session_id=session_id,
        platform="avibe",
        author="user",
        source="user",
        message_type=QUEUED_TYPE,
        text=text,
        author_id=author_id,
        author_name=author_name,
    )


def list_queued(conn: Connection, session_id: str) -> list[dict[str, Any]]:
    """Pending queued messages for a session, oldest first."""
    query = (
        select(messages)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == QUEUED_TYPE)
        .order_by(messages.c.created_at.asc(), messages.c.id.asc())
    )
    return [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]


def list_queued_session_ids(conn: Connection) -> list[str]:
    """Session ids with persisted queue rows, ordered by their oldest row."""

    query = (
        select(messages.c.session_id)
        .where(messages.c.session_id.is_not(None))
        .where(messages.c.type == QUEUED_TYPE)
        .group_by(messages.c.session_id)
        .order_by(func.min(messages.c.created_at), func.min(messages.c.id))
    )
    return [str(session_id) for session_id in conn.execute(query).scalars() if session_id]


def pop_queued(conn: Connection, session_id: str) -> list[dict[str, Any]]:
    """Claim the session's queued messages: read them (oldest first), then delete
    them in the SAME transaction, so the rows are returned exactly once. Empty
    list when the queue is empty.

    Select-then-delete rather than ``DELETE ... RETURNING``: RETURNING needs
    SQLite >= 3.35, which the project does not pin, so on an older libsqlite the
    flush would raise — ``_flush_queue`` returns False and the send-while-busy
    queue never dispatches, stranding the user's queued follow-up (Codex P2).

    The DELETE is scoped to the CLAIMED row ids (not a broad session+type
    predicate): the UI server is a SEPARATE writer that can promote a just-sent
    prompt to ``queued`` between the SELECT and the DELETE, and a broad delete
    would drop that newer row without returning it — losing the user's message.
    Deleting only the ids we actually read leaves any concurrently-enqueued row
    for the next flush (Codex P2).
    """
    rows_q = (
        select(messages)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == QUEUED_TYPE)
        .order_by(messages.c.created_at.asc(), messages.c.id.asc())
    )
    rows = [_row_to_payload(dict(row)) for row in conn.execute(rows_q).mappings().all()]
    if not rows:
        return []
    claimed_ids = [r["id"] for r in rows]
    conn.execute(delete(messages).where(messages.c.id.in_(claimed_ids)))
    return rows


def delete_queued(conn: Connection, ids: list[str]) -> None:
    """Delete a CLAIMED subset of queued rows by id. The caller read them via
    ``list_queued`` and is claiming exactly this segment (e.g. the leading run of
    user rows, or one scheduled row). Scoped to the read ids — not a broad
    session+type predicate — so a row another writer enqueued after the read
    survives for the next flush (same safety rationale as ``pop_queued``)."""
    if not ids:
        return
    conn.execute(delete(messages).where(messages.c.id.in_(ids)))


def clear_queued(conn: Connection, session_id: str) -> int:
    """Drop ALL send-while-busy queued rows for a session. Used by archive so no
    queued prompt can later be flushed into a now-terminal session (on natural
    turn completion or via send-now) — unlike ``delete_queued``, which claims a
    specific id segment during a flush. Returns the number removed."""
    result = conn.execute(
        delete(messages)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == QUEUED_TYPE)
    )
    return result.rowcount or 0


def clear_pending(conn: Connection, session_id: str) -> int:
    """Drop ALL ``pending`` send reservations for a session. Used by archive: a
    send that reserved its row just before the archive committed must not later
    be promoted into a visible message / accepted turn for a now-terminal session
    — once the row is gone, ``promote_pending`` no-ops on the in-flight send.
    Returns the number removed."""
    result = conn.execute(
        delete(messages)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == PENDING_TYPE)
    )
    return result.rowcount or 0


def promote_pending(conn: Connection, message_id: str, to_type: str) -> bool:
    """Promote a reserved ``pending`` row to its decided type — ``user`` once the
    turn is accepted, or ``queued`` when a turn is already running. The row is
    persisted as ``pending`` BEFORE dispatch (reserving its (created_at, id) for
    correct ordering) and stays hidden until this promotes it, so no other tab
    can briefly see it as a sent prompt during the dispatch window. Returns True
    if a pending row was promoted.
    """
    result = conn.execute(
        update(messages)
        .where(messages.c.id == message_id)
        .where(messages.c.type == PENDING_TYPE)
        .values(type=to_type)
    )
    return bool(result.rowcount)


def remove_queued(conn: Connection, session_id: str, message_id: str) -> bool:
    """Delete one queued message, scoped to its session so a stale / cross-session
    id can't drop another chat's queued row. Returns True if a row was removed."""
    result = conn.execute(
        delete(messages)
        .where(messages.c.id == message_id)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == QUEUED_TYPE)
    )
    return bool(result.rowcount)


def get_draft(conn: Connection, session_id: str) -> Optional[dict[str, Any]]:
    """The session's current unsent draft, or None."""
    query = (
        select(messages)
        .where(messages.c.session_id == session_id)
        .where(messages.c.type == DRAFT_TYPE)
        .order_by(messages.c.created_at.desc(), messages.c.id.desc())
        .limit(1)
    )
    row = conn.execute(query).mappings().first()
    return _row_to_payload(dict(row)) if row else None


def set_draft(conn: Connection, *, scope_id: str, session_id: str, text: Optional[str]) -> Optional[dict[str, Any]]:
    """Upsert the session's draft (one row per session). Blank text clears it."""
    conn.execute(
        delete(messages).where(messages.c.session_id == session_id).where(messages.c.type == DRAFT_TYPE)
    )
    if not text or not text.strip():
        return None
    return append(
        conn,
        scope_id=scope_id,
        session_id=session_id,
        platform="avibe",
        author="user",
        source="user",
        message_type=DRAFT_TYPE,
        text=text,
    )


def clear_draft(conn: Connection, session_id: str) -> None:
    """Drop the session's draft (e.g. after a successful send)."""
    conn.execute(
        delete(messages).where(messages.c.session_id == session_id).where(messages.c.type == DRAFT_TYPE)
    )


def unread_counts(
    conn: Connection,
    *,
    platform: Optional[str] = None,
) -> dict[str, int]:
    """Return ``{scope_id: count}`` for unread agent ``result`` messages.

    Used by the sidebar / hover popover to show per-session unread dots
    plus the global count without dragging every row through Python.
    Filtered to ``type='result'`` so it agrees with the inbox feed's UNREAD
    count, which is also result-only — otherwise intermediate ``assistant`` /
    ``tool_call`` rows (now persisted for avibe too) would inflate the badge
    past what the feed shows. (Inbox *eligibility* and *preview* also accept a
    terminal ``notify`` so failed turns stay visible, but a failure notify is
    not an unread reply — it never bumps this badge.)
    """

    query = (
        select(messages.c.scope_id, func.count(messages.c.id))
        .where(messages.c.author == "agent")
        .where(messages.c.type == "result")
        .where(messages.c.read_at.is_(None))
        # Don't let an archived session's unread results inflate its scope badge
        # (keep null-session rows, which aren't attributable to any session).
        .where(
            or_(
                messages.c.session_id.is_(None),
                messages.c.session_id.not_in(
                    select(agent_sessions.c.id).where(agent_sessions.c.status == "archived")
                ),
            )
        )
        .group_by(messages.c.scope_id)
    )
    if platform is not None:
        query = query.where(messages.c.platform == platform)
    return {scope: int(count) for scope, count in conn.execute(query).all()}


def unread_counts_by_session(
    conn: Connection,
    *,
    platform: Optional[str] = None,
) -> dict[str, int]:
    """Return ``{session_id: count}`` for unread agent ``result`` messages.

    Per-session granularity for the sidebar: a project can hold several
    sessions, so a scope-level count (see ``unread_counts``) would stamp the
    same badge on every session row. Rows with a null ``session_id`` are
    skipped — they can't be attributed to a specific session. Filtered to
    ``type='result'`` so the sidebar badge matches the inbox card's unread
    count (the realtime ``inbox.session.updated`` row is result-only too).
    """

    query = (
        select(messages.c.session_id, func.count(messages.c.id))
        .where(messages.c.author == "agent")
        .where(messages.c.type == "result")
        .where(messages.c.read_at.is_(None))
        .where(messages.c.session_id.is_not(None))
        # Archived sessions are inert — their unread results must not light the
        # sidebar / global badge.
        .where(messages.c.session_id.not_in(select(agent_sessions.c.id).where(agent_sessions.c.status == "archived")))
        .group_by(messages.c.session_id)
    )
    if platform is not None:
        query = query.where(messages.c.platform == platform)
    return {session_id: int(count) for session_id, count in conn.execute(query).all()}


def total_unread(conn: Connection, *, platform: Optional[str] = None) -> int:
    """Global unread agent-``result`` count across all non-archived sessions.

    This is the sum of :func:`unread_counts_by_session`, i.e. the exact number
    the Inbox nav badge shows (``ui_server`` returns it as ``unread_total``). It
    is mirrored onto the installed PWA's app-icon badge — page-side while the app
    is open, and from the Web Push payload while it is closed — so the home
    screen icon never disagrees with the in-app count.
    """

    return sum(unread_counts_by_session(conn, platform=platform).values())


def list_inbox_sessions(
    conn: Connection,
    *,
    platform: Optional[str] = "avibe",
    unread_only: bool = False,
    limit: int = 30,
    before: Optional[str] = None,
    only_session: Optional[str] = None,
) -> dict[str, Any]:
    """Per-session ("Slack-like") inbox feed.

    One row per session that has at least one agent reply. Sorted by the
    session's most recent message of *any* author (the activity clock),
    descending. The preview text is the session's latest *agent* reply
    (distinct from the sort key). ``replied`` is True when the session is
    *awaiting the agent* — the latest human or harness input is newer than the
    agent's latest reply — so it stays set for the whole time the agent is
    working and survives a reload, clearing only once the agent replies.

    Keyset pagination via ``before`` (an opaque ``"<last_activity_at>|<session_id>"``
    cursor returned as ``next_cursor``).
    """

    def _latest_message_value(
        column_name: str,
        *,
        author: Optional[str] = None,
        types: Optional[tuple[str, ...]] = None,
        conversation_only: bool = False,
        input_turn_only: bool = False,
    ) -> Any:
        msg = messages.alias()
        query = (
            select(getattr(msg.c, column_name))
            .where(msg.c.session_id == agent_sessions.c.id)
            .where(msg.c.session_id.is_not(None))
            .order_by(msg.c.created_at.desc(), msg.c.id.desc())
            .limit(1)
        )
        if platform is not None:
            query = query.where(msg.c.platform == platform)
        if author is not None:
            query = query.where(msg.c.author == author)
        if types is not None:
            query = query.where(msg.c.type.in_(types))
        if input_turn_only:
            query = query.where(
                or_(
                    *(
                        and_(msg.c.author == input_author, msg.c.type == input_type)
                        for input_author, input_type in INPUT_TURN_AUTHOR_TYPES
                    )
                )
            )
        if conversation_only:
            query = query.where(msg.c.type.notin_(NON_CONVERSATION_TYPES))
        return query.scalar_subquery()

    # Drive from the small session set and do top-1 index probes per session.
    # This preserves the inbox contract while avoiding full message-window
    # materialization as history grows.
    last_activity_at = _latest_message_value("created_at", conversation_only=True)
    last_author = _latest_message_value("author", conversation_only=True)
    preview_id = _latest_message_value("id", types=("result", "notify", "error"))
    preview_at = _latest_message_value("created_at", types=("result", "notify", "error"))
    # The awaiting/replied calc must count the INVISIBLE ``silent`` completion marker
    # as a reply too (a reply-less turn is still answered) — otherwise a silently
    # completed turn keeps the sidebar showing "awaiting the agent". The PREVIEW text
    # stays the last VISIBLE reply, so ``silent`` is included here but NOT in preview_*.
    _terminal_types = ("result", "notify", "error", SILENT_TYPE)
    last_terminal_id = _latest_message_value("id", types=_terminal_types)
    last_terminal_at = _latest_message_value("created_at", types=_terminal_types)
    last_input_at = _latest_message_value(
        "created_at", conversation_only=True, input_turn_only=True
    )
    last_input_id = _latest_message_value("id", conversation_only=True, input_turn_only=True)

    # Unread agent messages per session.
    m = messages
    unread_q = (
        select(m.c.session_id.label("session_id"), func.count().label("unread_count"))
        .where(m.c.session_id.is_not(None))
        .where(m.c.author == "agent")
        .where(m.c.type == "result")
        .where(m.c.read_at.is_(None))
        .group_by(m.c.session_id)
    )
    if platform is not None:
        unread_q = unread_q.where(m.c.platform == platform)
    unread_sub = unread_q.subquery()

    unread_count_col = func.coalesce(unread_sub.c.unread_count, 0).label("unread_count")
    session_rows = (
        select(
            agent_sessions.c.id.label("session_id"),
            last_activity_at.label("last_activity_at"),
            last_author.label("last_author"),
            agent_sessions.c.title,
            agent_sessions.c.scope_id,
            scopes.c.native_id.label("project_id"),
            scopes.c.display_name.label("project_name"),
            unread_count_col,
            preview_id.label("preview_id"),
            preview_at.label("preview_at"),
            last_terminal_id.label("last_terminal_id"),
            last_terminal_at.label("last_terminal_at"),
            last_input_at.label("last_input_at"),
            last_input_id.label("last_input_id"),
        )
        .select_from(
            agent_sessions.join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True).join(
                unread_sub, unread_sub.c.session_id == agent_sessions.c.id, isouter=True
            )
        )
    )
    # Archived sessions are hidden everywhere — keep them out of the inbox feed too.
    session_rows = session_rows.where(agent_sessions.c.status != "archived")
    if only_session:
        session_rows = session_rows.where(agent_sessions.c.id == only_session)

    session_rows_sub = session_rows.subquery()
    query = select(session_rows_sub).where(session_rows_sub.c.preview_id.is_not(None))
    if unread_only:
        query = query.where(session_rows_sub.c.unread_count > 0)
    if before:
        cursor_at, _, cursor_session = before.partition("|")
        if cursor_at and cursor_session:
            query = query.where(
                or_(
                    session_rows_sub.c.last_activity_at < cursor_at,
                    and_(
                        session_rows_sub.c.last_activity_at == cursor_at,
                        session_rows_sub.c.session_id < cursor_session,
                    ),
                )
            )

    effective_limit = min(max(int(limit), 1), 100)
    query = query.order_by(
        session_rows_sub.c.last_activity_at.desc(), session_rows_sub.c.session_id.desc()
    ).limit(effective_limit)
    limited_sessions = query.subquery()
    preview_msg = messages.alias()
    query = (
        select(
            limited_sessions,
            preview_msg.c.content_text.label("preview_text"),
            preview_msg.c.content_json.label("preview_json"),
        )
        .select_from(limited_sessions.join(preview_msg, preview_msg.c.id == limited_sessions.c.preview_id))
        .order_by(limited_sessions.c.last_activity_at.desc(), limited_sessions.c.session_id.desc())
    )

    rows = conn.execute(query).mappings().all()
    sessions: list[dict[str, Any]] = []
    for row in rows:
        preview = row["preview_text"]
        if not preview and row["preview_json"]:
            try:
                preview = (json.loads(row["preview_json"]) or {}).get("text") or ""
            except json.JSONDecodeError:
                preview = ""
        unread = int(row["unread_count"] or 0)
        # Awaiting the agent: the latest human or harness input is newer than the
        # agent's latest reply. Persistent across a reload and stays set for the whole
        # agent turn, unlike a literal "last author" check. ``created_at`` is
        # second-resolution, so compare ``(created_at, id)`` tuples — the message
        # id carries a microsecond-clock prefix (see ``_new_message_id``), giving
        # the right order for a follow-up sent in the same second as the prior
        # reply.
        last_input_at = row["last_input_at"]
        last_input_id = row["last_input_id"]
        # Compare against the last TERMINAL (incl. the invisible ``silent`` marker),
        # not the preview: a silently-completed turn HAS replied even though its text
        # is not the visible preview.
        terminal_at = row["last_terminal_at"]
        terminal_id = row["last_terminal_id"]
        awaiting_reply = bool(
            last_input_at is not None
            and terminal_at is not None
            and (
                last_input_at > terminal_at
                or (last_input_at == terminal_at and (last_input_id or "") > (terminal_id or ""))
            )
        )
        sessions.append(
            {
                "session_id": row["session_id"],
                "scope_id": row["scope_id"],
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "title": row["title"],
                "last_activity_at": row["last_activity_at"],
                "last_message_author": row["last_author"],
                "replied": awaiting_reply,
                "preview_text": preview or "",
                "preview_at": row["preview_at"],
                "unread_count": unread,
                "unread": unread > 0,
            }
        )

    next_cursor = None
    if len(sessions) == effective_limit:
        tail = sessions[-1]
        next_cursor = f"{tail['last_activity_at']}|{tail['session_id']}"
    return {"sessions": sessions, "next_cursor": next_cursor}


def get_inbox_session(
    conn: Connection,
    session_id: str,
    *,
    platform: Optional[str] = "avibe",
) -> Optional[dict[str, Any]]:
    """Return one session's inbox row (or None if it has no agent ``result`` /
    terminal ``notify`` yet). Used to build realtime ``inbox.session.updated``
    payloads."""
    rows = list_inbox_sessions(conn, platform=platform, only_session=session_id, limit=1)["sessions"]
    return rows[0] if rows else None


def mark_session_read(
    conn: Connection,
    session_id: str,
    *,
    until_message_id: Optional[str] = None,
) -> int:
    """Mark unread agent messages in a session as read, up to ``until_message_id``.

    Returns the number of rows updated.
    """

    now = _utc_now_iso()
    base = (
        update(messages)
        .where(messages.c.session_id == session_id)
        .where(messages.c.author == "agent")
        .where(messages.c.read_at.is_(None))
        .values(read_at=now, updated_at=now)
    )
    if until_message_id:
        anchor = conn.execute(
            select(messages.c.created_at).where(messages.c.id == until_message_id)
        ).scalar_one_or_none()
        if anchor is not None:
            # ``created_at`` is stored at second precision, so a bare
            # ``<= anchor`` would also mark newer messages created in the
            # same second as read. Tie-break on ``id`` so only rows at-or-
            # before the anchor message itself are affected.
            base = base.where(
                or_(
                    messages.c.created_at < anchor,
                    and_(
                        messages.c.created_at == anchor,
                        messages.c.id <= until_message_id,
                    ),
                )
            )
    result = conn.execute(base)
    return result.rowcount or 0


def list_messages_for_inbox_scope(
    conn: Connection,
    scope_id: str,
    *,
    limit: int = 1,
) -> Iterable[dict[str, Any]]:
    """Return the latest N messages for a given scope (for inbox previews)."""

    query = (
        select(messages)
        .where(messages.c.scope_id == scope_id)
        .order_by(messages.c.created_at.desc(), messages.c.id.desc())
        .limit(min(max(int(limit), 1), 50))
    )
    return [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]

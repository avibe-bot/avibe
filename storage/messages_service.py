"""Accepted communication records in the platform-agnostic ``messages`` table.

The workbench Inbox + per-session history both read through this
module so they get a consistent shape regardless of which platform
originated the row. Inbound content materializes here only after a Delivery is
accepted; ``append`` is for communication that is already accepted when written,
such as agent output and mirrored IM records.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.engine import Connection

from storage.agent_session_rows import INBOX_SESSION_VISIBILITIES
from storage.db import escape_sql_like
from storage.models import (
    agent_runs,
    agent_sessions,
    agents,
    message_deliveries,
    messages,
    scope_settings,
    scopes,
    session_turns,
)
from storage.pagination import PageRequest, PageResult, page_result_from_limit_plus_one
from storage.sessions_service import session_agent_display_label
from vibe.message_identity import HARNESS_TYPE, INPUT_TURN_AUTHOR_TYPES
from vibe.message_types import types_with


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_key(value: Any, row_id: Any) -> tuple[datetime, str]:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        instant = datetime.min.replace(tzinfo=timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc), str(row_id or "")


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


_AGENT_RUN_NATIVE_PREFIX = "agent_run:"


def _attach_agent_run_provenance(
    conn: Connection, payloads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Read-side provenance for agent-callback ("自动触发") chat messages (A9a).

    A harness ``agent_run`` prompt is stored as ``native_message_id =
    "agent_run:<execution_id>"`` with no source-session pointer (the write path
    records none). Resolve it read-side: message → its ``agent_runs`` row
    (``id == <execution_id>``) → the run's ``source_actor`` (the session that
    triggered the callback) → that session's title, so the Chat chip can name the
    source and deep-link to ``/chat/<source_session_id>``. No schema/write-path
    change. Batched: at most two extra queries per page, only when such a message
    is present.
    """
    exec_by_msg: dict[str, str] = {}
    for payload in payloads:
        native_id = payload.get("native_message_id")
        if (
            payload.get("source") == "harness"
            and isinstance(native_id, str)
            and native_id.startswith(_AGENT_RUN_NATIVE_PREFIX)
        ):
            exec_by_msg[payload["id"]] = native_id[len(_AGENT_RUN_NATIVE_PREFIX):]
    if not exec_by_msg:
        return payloads

    # execution_id == agent_runs.id. The source SESSION differs by run kind:
    #  - source_kind='agent'   → source_actor IS the caller session id.
    #  - source_kind='callback'→ source_actor is the parent RUN id (or a decorated
    #    "<run>:terminal:<status>"), NOT a session; the real source is the parent
    #    (delegated) run's session_id.
    runs = {
        row["id"]: row
        for row in conn.execute(
            select(
                agent_runs.c.id, agent_runs.c.source_kind,
                agent_runs.c.source_actor, agent_runs.c.parent_run_id,
            ).where(agent_runs.c.id.in_(set(exec_by_msg.values())))
        ).mappings()
    }
    callback_parents = {
        run["parent_run_id"] for run in runs.values()
        if run["source_kind"] == "callback" and run["parent_run_id"]
    }
    parent_session: dict[str, str] = {}
    if callback_parents:
        for row in conn.execute(
            select(agent_runs.c.id, agent_runs.c.session_id).where(
                agent_runs.c.id.in_(callback_parents)
            )
        ).mappings():
            sess = (row["session_id"] or "").strip()
            if sess:
                parent_session[row["id"]] = sess

    source_by_exec: dict[str, str] = {}
    for exec_id, run in runs.items():
        if run["source_kind"] == "callback":
            source_id = parent_session.get(run["parent_run_id"])
        else:
            source_id = (run["source_actor"] or "").strip() or None
        # A session id never contains ':' (decorated run/terminal forms do); guard
        # so an unexpected source_actor shape can't become a bogus /chat target.
        if source_id and ":" not in source_id:
            source_by_exec[exec_id] = source_id
    if not source_by_exec:
        return payloads

    meta_by_session: dict[str, dict[str, Optional[str]]] = {}
    for row in conn.execute(
        select(
            agent_sessions.c.id,
            agent_sessions.c.title,
            agent_sessions.c.agent_name,
            agent_sessions.c.agent_backend,
            agents.c.name.label("catalog_agent_name"),
            agents.c.archived_at.label("catalog_agent_archived_at"),
            agents.c.metadata_json.label("catalog_agent_metadata_json"),
        )
        .select_from(
            agent_sessions.outerjoin(
                agents,
                or_(
                    agents.c.id == agent_sessions.c.agent_id,
                    and_(
                        agent_sessions.c.agent_id.is_(None),
                        agents.c.name == agent_sessions.c.agent_name,
                    ),
                ),
            )
        )
        .where(
            agent_sessions.c.id.in_(set(source_by_exec.values()))
        )
    ).mappings():
        meta_by_session[row["id"]] = {
            "title": row["title"],
            "agent_name": session_agent_display_label(row),
        }

    for payload in payloads:
        source_id = source_by_exec.get(exec_by_msg.get(payload["id"], ""))
        # Only attach when the source session still exists (is in meta_by_session);
        # a stale/imported/deleted source would otherwise write source_session_id
        # and produce a dead /chat/<missing id> link with only the fallback label.
        if source_id in meta_by_session:
            meta = meta_by_session[source_id]
            payload["source_session_id"] = source_id
            payload["source_session_title"] = meta.get("title")
            payload["source_session_agent_name"] = meta.get("agent_name")
    return payloads


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
    include_archived: bool = False,
) -> dict[str, Any]:
    """Global message-content search, grouped by session.

    Substring (case-insensitive) ``LIKE`` over ``messages.content_text``, scoped
    to one ``platform`` (Workbench = ``avibe``) and a set of transcript-visible
    ``types`` (human prompts + harness prompts + the agent's rendered ``result``
    replies + Show annotations — all land on a message the chat
    actually renders, so a clicked result is always jumpable). Archived SESSIONS
    are excluded unless ``include_archived=True`` opts them in (archive is
    terminal, so those transcripts are read-only — the flag only makes them
    findable). Messages under an archived PROJECT are excluded
    UNCONDITIONALLY, independent of ``include_archived``:
    ``projects_service.archive_project``
    disables a project by setting ``scope_settings.enabled = 0`` (its sessions
    stay ``active``), so the scope's disabled state is the authoritative
    "archived project" signal here, and project archive is reversible through its
    own restore flow rather than through search. A scope with no
    ``scope_settings`` row is
    treated as enabled (legacy / folder-less projects never got one). ``limit``
    caps the number of
    MATCHED messages scanned (newest first), so it bounds total work; the matches
    are then grouped into sessions. The snippet is built in Python (see
    :func:`build_snippet`) so the client renders ``match`` with a highlight and
    needs no offset math.

    Returns ``{"sessions": [...], "total": <#matches>, "session_count": <#sessions>}``
    where each session is ``{session_id, title, project_id, project_name,
    archived, matches: [{id, author, source, type, created_at, snippet}]}``.
    ``archived`` is always present and is ``False`` for every group in the
    default (opt-out) mode. Sessions are
    ordered by their most-recent match; matches are newest-first within a session.
    An empty / whitespace query short-circuits to an empty result.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return {"sessions": [], "total": 0, "session_count": 0}

    like = escape_sql_like(cleaned)
    type_list = list(types if types is not None else types_with("searchable"))
    effective_limit = min(max(int(limit), 1), 200)
    # Applied in place below so the default (opt-out) statement keeps exactly the
    # predicates — and the predicate order — it had before the flag existed.
    archived_session_filters = () if include_archived else (agent_sessions.c.status != "archived",)

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
            agent_sessions.c.status,
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
        # Archived sessions are soft-deleted — never surface their messages
        # unless the caller explicitly opted in via ``include_archived``.
        .where(*archived_session_filters)
        # DELIBERATELY ``foreground`` ALONE, i.e. narrower than the inbox
        # (``INBOX_SESSION_VISIBILITIES``, which also admits ``system``). Search
        # returns RESULTS a user goes on to open and read in context, and the only
        # system session is the runtime's workspace-notifications row: machine-authored
        # failure notices, already surfaced as inbox cards, with no conversation around
        # a hit to read. Admitting it would put runtime bookkeeping into every user text
        # search for no reachable next action. (Today's notices are additionally not
        # ``searchable`` by TYPE — this predicate is the one that stays true if that
        # ever changes.)
        .where(agent_sessions.c.visibility == "foreground")
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
                "archived": row["status"] == "archived",
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
    scope_id: Optional[str],
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


def get_message(
    conn: Connection,
    message_id: str,
    *,
    session_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Load one message by id, optionally requiring its owning session."""

    query = select(messages).where(messages.c.id == message_id)
    if session_id is not None:
        query = query.where(messages.c.session_id == session_id)
    row = conn.execute(query).mappings().first()
    return _row_to_payload(dict(row)) if row else None


def native_message_exists(
    conn: Connection,
    *,
    platform: str,
    scope_id: str | None,
    native_message_id: str,
) -> bool:
    """True when a conversation-scoped native message has been recorded."""
    return (
        get_native_message(
            conn,
            platform=platform,
            scope_id=scope_id,
            native_message_id=native_message_id,
        )
        is not None
    )


def get_native_message(
    conn: Connection,
    *,
    platform: str,
    scope_id: str | None,
    native_message_id: str,
) -> Optional[dict[str, Any]]:
    """Load one accepted Message by its conversation-scoped native identity."""

    platform = str(platform or "").strip()
    native_message_id = str(native_message_id or "").strip()
    if not platform or not native_message_id:
        return None
    scope_predicate = (
        messages.c.scope_id == scope_id
        if scope_id is not None
        else messages.c.scope_id.is_(None)
    )
    row = conn.execute(
        select(messages)
        .where(messages.c.platform == platform)
        .where(scope_predicate)
        .where(messages.c.native_message_id == native_message_id)
        .limit(1)
    ).mappings().first()
    return _row_to_payload(dict(row)) if row else None


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
    tail: bool = False,
) -> dict[str, Any]:
    """Return messages for one session in chronological order with cursor pagination.

    ``types`` optionally restricts the rows to a set of message types. The chat
    transcript passes :data:`TRANSCRIPT_TYPES` so intermediate process-log rows
    stay out of the conversation view.

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
    if types is not None:
        query = query.where(messages.c.type.in_(list(types)))
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

        merged = _attach_agent_run_provenance(conn, older + anchor_rows + newer)
        return {
            "messages": merged,
            "next_after_id": newer[-1]["id"] if has_newer and newer else None,
            "next_before_id": older[0]["id"] if has_older and older else None,
        }
    if tail:
        # Newest ``limit`` rows, then flip back to chronological for the caller.
        query = query.order_by(messages.c.created_at.desc(), messages.c.id.desc()).limit(effective_limit + 1)
        rows = _attach_agent_run_provenance(
            conn, [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
        )
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
        rows = _attach_agent_run_provenance(
            conn, [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
        )
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
    rows = _attach_agent_run_provenance(
        conn, [_row_to_payload(dict(row)) for row in conn.execute(query).mappings().all()]
    )
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


ANNOTATION_TYPE = "annotation"
INBOX_ACTIVITY_TYPES = types_with("inboxActivity")

# The transcript-visible types — the SINGLE source of truth shared by the
# history fetch (``list_session_messages``) AND the live ``message.new`` publish
# gate, so what a page loads and what it receives over the stream are identical.
# Excludes hidden ``assistant`` communication; tool calls live in ``agent_events``
# and ``system`` is not persisted. Harness-triggered prompts have their own type
# so they cannot be mistaken for human input. Show Page annotations in either
# direction share one explicit transcript type. ``error`` is a terminal FAILED
# result (turned the dot red): shown in the conversation like any terminal
# message, but the unread queries below stay ``result``-only so a failure is not
# counted as an unread agent reply.
TRANSCRIPT_TYPES = types_with("transcript")
_INBOX_PREVIEW_TYPES = types_with("inboxPreview")
_INBOX_SETTLES_REPLY_TYPES = types_with("inboxSettlesReply")
_UNREAD_TYPES = types_with("unread")


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
    process-event rows in ``agent_events`` would inflate the badge past what the
    feed shows. (Inbox *eligibility* and *preview* also accept a
    terminal ``notify`` so failed turns stay visible, but a failure notify is
    not an unread reply — it never bumps this badge.)
    """

    query = (
        select(messages.c.scope_id, func.count(messages.c.id))
        .where(messages.c.author == "agent")
        .where(messages.c.type.in_(_UNREAD_TYPES))
        .where(messages.c.read_at.is_(None))
        # Don't let an archived session's unread results inflate its scope badge
        # (keep null-session rows, which aren't attributable to any session).
        .where(
            or_(
                messages.c.session_id.is_(None),
                messages.c.session_id.not_in(
                    select(agent_sessions.c.id).where(
                        or_(
                            agent_sessions.c.status == "archived",
                            # NEGATIVE form, so it has to name every visibility the
                            # inbox admits or the badge would disagree with the feed
                            # it is supposed to count. Spelling it as "not one of the
                            # admitted values" also keeps a future fourth visibility
                            # hidden by default rather than silently badged.
                            agent_sessions.c.visibility.notin_(INBOX_SESSION_VISIBILITIES),
                        )
                    )
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
        .where(messages.c.type.in_(_UNREAD_TYPES))
        .where(messages.c.read_at.is_(None))
        .where(messages.c.session_id.is_not(None))
        # Archived sessions are inert — their unread results must not light the
        # sidebar / global badge.
        .where(
            messages.c.session_id.not_in(
                select(agent_sessions.c.id).where(
                    or_(
                        agent_sessions.c.status == "archived",
                        # See ``unread_counts``: negative form, so it names the
                        # admitted set rather than one value.
                        agent_sessions.c.visibility.notin_(INBOX_SESSION_VISIBILITIES),
                    )
                )
            )
        )
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
            query = query.where(msg.c.type.in_(INBOX_ACTIVITY_TYPES))
        return query.scalar_subquery()

    # Drive from the small session set and do top-1 index probes per session.
    # This preserves the inbox contract while avoiding full message-window
    # materialization as history grows.
    last_activity_at = _latest_message_value("created_at", conversation_only=True)
    last_author = _latest_message_value("author", conversation_only=True)
    preview_id = _latest_message_value("id", types=_INBOX_PREVIEW_TYPES)
    preview_at = _latest_message_value("created_at", types=_INBOX_PREVIEW_TYPES)
    last_terminal_id = _latest_message_value("id", types=_INBOX_SETTLES_REPLY_TYPES)
    last_terminal_at = _latest_message_value("created_at", types=_INBOX_SETTLES_REPLY_TYPES)
    last_turn_terminal_id = (
        select(session_turns.c.id)
        .where(
            session_turns.c.session_id == agent_sessions.c.id,
            session_turns.c.state == "terminal",
            session_turns.c.terminal_outcome != "not_written",
        )
        .order_by(
            func.julianday(session_turns.c.terminal_at).desc(),
            session_turns.c.id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    last_turn_terminal_at = (
        select(session_turns.c.terminal_at)
        .where(
            session_turns.c.session_id == agent_sessions.c.id,
            session_turns.c.state == "terminal",
            session_turns.c.terminal_outcome != "not_written",
        )
        .order_by(
            func.julianday(session_turns.c.terminal_at).desc(),
            session_turns.c.id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    last_input_at = _latest_message_value(
        "created_at", conversation_only=True, input_turn_only=True
    )
    last_input_id = _latest_message_value("id", conversation_only=True, input_turn_only=True)
    last_input_turn_state = (
        select(session_turns.c.state)
        .select_from(
            message_deliveries.join(
                session_turns,
                session_turns.c.id == message_deliveries.c.turn_id,
            )
        )
        .where(message_deliveries.c.message_id == last_input_id)
        .limit(1)
        .scalar_subquery()
    )

    # Unread agent messages per session.
    m = messages
    unread_q = (
        select(m.c.session_id.label("session_id"), func.count().label("unread_count"))
        .where(m.c.session_id.is_not(None))
        .where(m.c.author == "agent")
        .where(m.c.type.in_(_UNREAD_TYPES))
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
            last_turn_terminal_id.label("last_turn_terminal_id"),
            last_turn_terminal_at.label("last_turn_terminal_at"),
            last_input_at.label("last_input_at"),
            last_input_id.label("last_input_id"),
            last_input_turn_state.label("last_input_turn_state"),
        )
        .select_from(
            agent_sessions.join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True).join(
                unread_sub, unread_sub.c.session_id == agent_sessions.c.id, isouter=True
            )
        )
    )
    # Archived sessions are hidden everywhere — keep them out of the inbox feed too.
    # Visibility is the ADMISSION list, not "foreground": the runtime's
    # workspace-notifications row is ``system`` — deliberately absent from every
    # ordinary session list — and the inbox is the surface it is delivered on, so
    # excluding it here would hide the notice this session exists to show while the
    # notice was still recorded as sent. ``background`` remains excluded (it also
    # sets ``suppress_delivery``).
    session_rows = session_rows.where(
        agent_sessions.c.status != "archived",
        agent_sessions.c.visibility.in_(INBOX_SESSION_VISIBILITIES),
    )
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
        # Execution settlement belongs to SessionTurn. A visible result Message
        # can be later (for example after delivery retries), so use the newest
        # semantic terminal evidence without creating an invisible Message marker.
        terminal_candidates = [
            (row["last_terminal_at"], row["last_terminal_id"]),
            (row["last_turn_terminal_at"], row["last_turn_terminal_id"]),
        ]
        terminal_at, terminal_id = max(
            (candidate for candidate in terminal_candidates if candidate[0] is not None),
            key=lambda candidate: _timestamp_key(candidate[0], candidate[1]),
            default=(None, None),
        )
        accepted_turn_state = str(row["last_input_turn_state"] or "")
        if accepted_turn_state:
            awaiting_reply = accepted_turn_state in {"starting", "active"}
        else:
            awaiting_reply = bool(
                last_input_at is not None
                and terminal_at is not None
                and _timestamp_key(last_input_at, last_input_id)
                > _timestamp_key(terminal_at, terminal_id)
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

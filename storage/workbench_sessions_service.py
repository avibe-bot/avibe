"""Workbench-scoped session CRUD over ``agent_sessions``.

``storage/sessions_service.py`` exposes the runtime-facing primitives
that IM dispatchers use to reserve sessions during message handling.
The workbench REST API needs different shapes — listing sessions in a
project, creating one with explicit Agent / model / effort, renaming,
archiving — so this module wraps the same ``agent_sessions`` table
with workbench-friendly queries instead of bolting another concern
onto ``SQLiteSessionsService``.

Avibe scope_ids look like ``avibe::project::proj_<hex12>`` — see
``storage/projects_service.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import Connection

from storage.agent_session_rows import create_agent_session_row
from storage.db import escape_sql_like
from storage.pagination import PageRequest, PageResult, page_result_from_limit_plus_one
from storage.models import (
    agent_runs,
    agent_sessions,
    messages,
    run_definitions,
    scope_settings,
    scopes,
    show_pages,
    agents,
)

# Raw ``agent_runs.status`` values that are not yet terminal — archive cancels these.
_ACTIVE_RUN_STATUSES = ("pending", "queued", "processing", "running")
_PENDING_RUN_STATUSES = ("pending", "queued")


SESSION_ID_ALPHABET = "23456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"

# Distinguishes an omitted update field from a present ``None`` (clear). See
# ``update_session``: a present ``model=None`` must clear the column, but an
# omitted ``model`` must leave it untouched.
_UNSET: Any = object()

# Title sources that mean the title is DELIBERATELY owned (set or cleared on purpose),
# so backend auto-fill must never overwrite it and the "name this session" prompt nudge
# must never re-prompt it. ``user`` = Web UI / human edit; ``agent`` = the agent via
# ``vibe session update``. Auto sources (``backend``, ``derived_first_prompt``) and a
# never-touched session (no title_source) are NOT deliberate.
DELIBERATE_TITLE_SOURCES: tuple[str, ...] = ("user", "agent")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    metadata = _load_metadata(row.get("metadata_json"))
    return {
        "id": row["id"],
        "scope_id": row.get("scope_id"),
        # Platform is the first ``scope_id`` segment (``make_scope_id`` always
        # builds ``<platform>::<scope_type>::<native_id>``), so it needs no join.
        # ``avibe`` == Web/Workbench; the rest are IM platforms.
        "platform": ((row.get("scope_id") or "").split("::", 1)[0] or None),
        "project_id": (row.get("scope_id") or "").rsplit("::", 1)[-1] or None,
        "title": row.get("title"),
        "agent_id": row.get("agent_id"),
        "agent_name": row.get("agent_name"),
        "agent_backend": row.get("agent_backend"),
        "agent_variant": row.get("agent_variant"),
        "model": row.get("model"),
        "reasoning_effort": row.get("reasoning_effort"),
        "status": row.get("status"),
        # Live agent-runtime status (idle/running/failed), separate from the
        # lifecycle ``status``. Older rows predating the column read as ``idle``.
        "agent_status": row.get("agent_status") or "idle",
        "workdir": row.get("workdir"),
        # The reserved native-session anchor (workbench sessions self-anchor to
        # their id). Dispatch carries it so resume binds by the stored anchor
        # after a restart instead of a computed one (Codex P2).
        "session_anchor": row.get("session_anchor"),
        "native_session_id": row.get("native_session_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_active_at": row.get("last_active_at"),
        "metadata": metadata,
    }


def _load_metadata(value: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _dumps_metadata(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata)


def list_sessions(
    conn: Connection,
    *,
    scope_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    before_id: Optional[str] = None,
    title_query: Optional[str] = None,
) -> dict[str, Any]:
    """Return sessions for the workbench list. Cursor pagination via ``before_id``.

    ``status`` accepts ``active`` / ``archived`` (or omit for both). The
    cursor is the row id; results are sorted by ``last_active_at DESC``
    so the cursor row is "the last id you already saw".

    ``title_query`` powers the chat composer ``#``-mention global search: a
    case-insensitive title LIKE match (LIKE metacharacters escaped).
    """

    query = select(agent_sessions)
    if scope_id is not None:
        query = query.where(agent_sessions.c.scope_id == scope_id)
    if status is not None and status != "all":
        query = query.where(agent_sessions.c.status == status)
    if title_query:
        # `#`-mention global search: case-insensitive title LIKE. Escape the LIKE
        # metacharacters so a literal ``%`` / ``_`` in the query can't widen it.
        like = escape_sql_like(title_query.strip())
        if like:
            query = query.where(agent_sessions.c.title.ilike(f"%{like}%", escape="\\"))
    if before_id is not None:
        cursor_row = conn.execute(
            select(agent_sessions.c.last_active_at, agent_sessions.c.created_at).where(agent_sessions.c.id == before_id)
        ).first()
        if cursor_row is not None:
            cursor_active, cursor_created = cursor_row
            # ``last_active_at`` + ``created_at`` are both second-granularity
            # ISO strings, so multiple sessions can share the same pair and
            # become unreachable on later pages without an ``id`` tie-breaker
            # that matches the ORDER BY shape.
            query = query.where(
                (agent_sessions.c.last_active_at < cursor_active)
                | (
                    (agent_sessions.c.last_active_at == cursor_active)
                    & (agent_sessions.c.created_at < cursor_created)
                )
                | (
                    (agent_sessions.c.last_active_at == cursor_active)
                    & (agent_sessions.c.created_at == cursor_created)
                    & (agent_sessions.c.id < before_id)
                )
            )
    effective_limit = min(max(int(limit), 1), 200)
    query = (
        query.order_by(
            agent_sessions.c.last_active_at.desc(),
            agent_sessions.c.created_at.desc(),
            agent_sessions.c.id.desc(),
        )
        .limit(effective_limit)
    )
    rows = [dict(row) for row in conn.execute(query).mappings().all()]
    sessions = [_row_to_payload(row) for row in rows]
    # Use the clamped page size for the cursor check — comparing against
    # the raw ``limit`` would emit ``next_before_id=null`` for callers who
    # requested > 200 and force them to stop paginating mid-history.
    next_cursor = sessions[-1]["id"] if len(sessions) == effective_limit else None
    return {"sessions": sessions, "next_before_id": next_cursor}


def get_session(conn: Connection, session_id: str) -> dict[str, Any]:
    row = conn.execute(
        select(agent_sessions).where(agent_sessions.c.id == session_id)
    ).mappings().first()
    if row is None:
        raise LookupError(f"Session not found: {session_id}")
    return _row_to_payload(dict(row))


def get_active_session(conn: Connection, session_id: str) -> dict[str, Any]:
    """Like :func:`get_session` but treats archived sessions as absent.

    Archived sessions are soft-deleted: the agent-facing ``vibe session`` surface
    must never surface them, so ``get`` / ``update`` raise ``LookupError`` for an
    archived id exactly as they would for a missing one.
    """
    payload = get_session(conn, session_id)
    if payload.get("status") == "archived":
        raise LookupError(f"Session not found: {session_id}")
    return payload


def list_sessions_page(
    conn: Connection,
    *,
    platform: Optional[str] = None,
    page: int = 1,
    limit: int = 10,
) -> PageResult[dict[str, Any]]:
    """Active sessions, most-recently-active first, offset-paginated for the CLI.

    Always excludes archived (soft-deleted) sessions — they are never surfaced on
    the agent-facing surface. ``platform`` filters by the scope platform via the
    ``<platform>::`` ``scope_id`` prefix (no join needed; see ``make_scope_id``).
    Fetches ``limit + 1`` rows so the caller learns whether a next page exists
    without a second COUNT query.
    """
    request = PageRequest(page=max(int(page), 1), limit=max(int(limit), 1))
    query = select(agent_sessions).where(agent_sessions.c.status == "active")
    if platform:
        query = query.where(agent_sessions.c.scope_id.like(f"{platform}::%"))
    query = (
        query.order_by(
            agent_sessions.c.last_active_at.desc(),
            agent_sessions.c.created_at.desc(),
            agent_sessions.c.id.desc(),
        )
        .limit(request.limit + 1)
        .offset(request.offset)
    )
    rows = [dict(row) for row in conn.execute(query).mappings().all()]
    payloads = [_row_to_payload(row) for row in rows]
    return page_result_from_limit_plus_one(payloads, request)


def create_session(
    conn: Connection,
    *,
    scope_id: str,
    agent_backend: str,
    agent_name: Optional[str] = None,
    agent_id: Optional[str] = None,
    agent_variant: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    title: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a workbench session inside the given project scope.

    Pulls ``workdir`` from ``scope_settings`` so Agent runs already know
    where to cd. ``native_session_id`` stays empty — Claude / OpenCode /
    Codex fill it on their first turn.
    """

    scope_row = conn.execute(
        select(
            scopes.c.id,
            scope_settings.c.workdir,
            scope_settings.c.enabled,
            scope_settings.c.agent_name,
            agents.c.backend.label("agent_backend"),
            scope_settings.c.agent_variant,
            scope_settings.c.model,
            scope_settings.c.reasoning_effort,
        )
        .select_from(
            scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id)
            .outerjoin(agents, agents.c.name == scope_settings.c.agent_name)
        )
        .where(scopes.c.id == scope_id)
    ).mappings().first()
    if scope_row is None:
        raise LookupError(f"Scope not found: {scope_id}")
    if scope_row.get("enabled") == 0:
        raise PermissionError(f"Scope is archived: {scope_id}")
    if agent_name and not agent_backend:
        agent_backend = _backend_for_agent_name(conn, str(agent_name))
    # Inherit the project's default Agent when the caller didn't pin a backend.
    # The default lives in ``scope_settings`` (set via Project Settings); adopting
    # it at creation makes the chat header open on the right Agent and the first
    # turn run on it. It is a SOFT default: until a native conversation exists
    # the user can still re-route the session to any backend or clear it back to
    # the global default (see ``update_session``). No project default → the
    # fields stay empty and dispatch falls back to the global default Vibe
    # Agent. An explicit caller backend always wins.
    if not agent_name and not agent_backend and scope_row.get("agent_name") and scope_row.get("agent_backend"):
        agent_backend = str(scope_row["agent_backend"])
        if agent_name is None:
            agent_name = scope_row.get("agent_name")
        if agent_variant is None:
            agent_variant = scope_row.get("agent_variant")
        if model is None:
            model = scope_row.get("model")
        if reasoning_effort is None:
            reasoning_effort = scope_row.get("reasoning_effort")

    now = _utc_now_iso()
    variant = agent_variant or agent_backend or "default"
    metadata_payload = {"created_via": "workbench"}
    if metadata:
        metadata_payload.update(metadata)

    session_id = create_agent_session_row(
        conn,
        scope_id=scope_id,
        agent_id=agent_id,
        agent_name=agent_name,
        agent_backend=agent_backend,
        agent_variant=str(variant),
        model=model,
        reasoning_effort=reasoning_effort,
        # Workbench sessions self-anchor; IM platforms use the parent message ts.
        session_anchor=None,
        workdir=scope_row.get("workdir") or os.getcwd(),
        title=title,
        metadata=metadata_payload,
        now=now,
    )
    return get_session(conn, session_id)


class SessionBackendLockedError(Exception):
    """Raised when a caller tries to switch the backend of a session that already
    has a native conversation (pinned for life: the native can only be resumed by
    the backend that created it, so switching would strand it and silently lose
    context) or whose turn is currently running (the in-flight turn will bind its
    native on the current route any moment). Changing the agent WITHIN the same
    backend stays allowed."""

    def __init__(self, *, session_id: str, current_backend: Optional[str], requested_backend: Optional[str]):
        self.session_id = session_id
        self.current_backend = current_backend
        self.requested_backend = requested_backend
        super().__init__(
            f"Session {session_id} is bound to backend "
            f"'{current_backend}' and cannot switch to '{requested_backend}'."
        )


def update_session(
    conn: Connection,
    session_id: str,
    *,
    title: Any = _UNSET,
    title_source: str = "user",
    agent_id: Any = _UNSET,
    agent_name: Any = _UNSET,
    agent_backend: Any = _UNSET,
    agent_variant: Any = _UNSET,
    model: Any = _UNSET,
    reasoning_effort: Any = _UNSET,
) -> dict[str, Any]:
    existing = conn.execute(
        select(
            agent_sessions.c.id,
            agent_sessions.c.agent_backend,
            agent_sessions.c.native_session_id,
            agent_sessions.c.agent_status,
            agent_sessions.c.metadata_json,
        ).where(agent_sessions.c.id == session_id)
    ).first()
    if existing is None:
        raise LookupError(f"Session not found: {session_id}")

    derived_backend = False
    if agent_name is not _UNSET and agent_backend is _UNSET:
        agent_backend = _backend_for_agent_name(conn, str(agent_name or "")) if agent_name else None
        derived_backend = True

    # Backend is pinned once a NATIVE conversation exists: the native can only
    # be resumed by the backend that created it, so switching (or clearing) the
    # backend would strand it. A RUNNING turn locks it too — the first turn is
    # already executing on the current route and will bind its native shortly,
    # so a mid-turn switch would either be silently overwritten by the bind-time
    # backfill or route queued follow-ups inconsistently (a stale ``running``
    # after a crash is reset to ``idle`` on startup). Before the first turn
    # nothing is strandable — a fresh session may carry a project-default
    # backend (see ``create_session``) and the user can still re-route it to ANY
    # backend or clear back to the default. A pending fork is the exception: it
    # has no native id yet, but its saved source-native id belongs to exactly
    # one backend, so the fork target must stay on that source backend until the
    # native fork binds. Within the same backend, agent/model/effort changes
    # stay allowed for the session's whole life.
    # Legacy agent-less rows whose native predates the bind-time backend
    # backfill keep the old empty -> concrete "initial pin" escape (while idle)
    # — the row doesn't know which backend owns its native, and locking them
    # would leave their picker permanently stuck.
    backend_changes = agent_backend is not _UNSET and str(agent_backend or "") != str(
        existing.agent_backend or ""
    )
    existing_metadata = _load_metadata(existing.metadata_json)
    pending_fork = bool(
        existing_metadata.get("created_via") == "session_fork"
        and not str(existing.native_session_id or "")
        and str(existing_metadata.get("fork_source_backend") or "")
    )
    if backend_changes and (
        (str(existing.native_session_id or "") and str(existing.agent_backend or ""))
        or str(existing.agent_status or "") == "running"
        or pending_fork
    ):
        raise SessionBackendLockedError(
            session_id=session_id,
            current_backend=existing.agent_backend,
            requested_backend=agent_backend,
        )

    values: dict[str, Any] = {"updated_at": _utc_now_iso()}
    if title is not _UNSET:
        cleaned = str(title or "").strip()
        values["title"] = cleaned or None
        # "user" (Web UI / human) or "agent" (vibe session update) — both deliberate.
        existing_metadata["title_source"] = str(title_source or "user")
        existing_metadata["title_user_modified_at"] = values["updated_at"]
        values["metadata_json"] = _dumps_metadata(existing_metadata)
    if agent_id is not _UNSET:
        values["agent_id"] = agent_id or None
    if agent_name is not _UNSET:
        values["agent_name"] = agent_name or None
    if agent_backend is not _UNSET:
        values["agent_backend"] = agent_backend or ""
        if derived_backend and agent_variant is _UNSET:
            values["agent_variant"] = str(agent_backend or "default")
    if agent_variant is not _UNSET:
        values["agent_variant"] = str(agent_variant or "default")
    # ``model`` / ``reasoning_effort`` use a sentinel default so a PRESENT
    # ``None`` clears the column (switching to an agent whose default model /
    # effort is empty must drop the previous agent's override), while an omitted
    # field leaves the stored value untouched (Codex P2).
    if model is not _UNSET:
        values["model"] = model or None
    if reasoning_effort is not _UNSET:
        values["reasoning_effort"] = reasoning_effort or None

    stmt = update(agent_sessions).where(agent_sessions.c.id == session_id)
    if backend_changes:
        # Re-assert the lock INSIDE the UPDATE: the guard above is read-then-
        # write, and a turn start / native bind can commit in between (the
        # SELECT runs before this statement takes the write lock). The predicate
        # makes the change atomic — it only lands while the session is still
        # unlocked (idle AND (no native or the legacy blank-backend escape)) or
        # the row already converged to the requested backend.
        stmt = stmt.where(
            or_(
                agent_sessions.c.agent_backend == str(agent_backend or ""),
                and_(
                    func.coalesce(agent_sessions.c.agent_status, "idle") != "running",
                    or_(
                        func.coalesce(agent_sessions.c.native_session_id, "") == "",
                        func.coalesce(agent_sessions.c.agent_backend, "") == "",
                    ),
                ),
            )
        )
    result = conn.execute(stmt.values(**values))
    if result.rowcount == 0:
        current = conn.execute(
            select(agent_sessions.c.agent_backend).where(agent_sessions.c.id == session_id)
        ).first()
        if current is None or not backend_changes:
            raise LookupError(f"Session not found: {session_id}")
        raise SessionBackendLockedError(
            session_id=session_id,
            current_backend=current.agent_backend,
            requested_backend=agent_backend,
        )
    return get_session(conn, session_id)


def _backend_for_agent_name(conn: Connection, agent_name: str) -> str:
    cleaned = str(agent_name or "").strip()
    if not cleaned:
        return ""
    backend = conn.execute(select(agents.c.backend).where(agents.c.name == cleaned)).scalar_one_or_none()
    return str(backend or "")


def derive_backend_for_agent_name(conn: Connection, agent_name: str) -> str:
    return _backend_for_agent_name(conn, agent_name)


def backfill_session_title(
    conn: Connection,
    session_id: str,
    *,
    title: str,
    backend: str,
    source: str = "backend",
    confidence: Optional[str] = None,
    native_session_id: Optional[str] = None,
) -> dict[str, Any] | None:
    """Fill an empty Vibe session title from a backend/derived source.

    Returns the updated session payload when a title was written; returns
    ``None`` when the row is missing, already has any title, is explicitly
    user-owned, or the incoming title is empty. This is intentionally
    write-once for title content: backend sync backfills blank sessions only.
    """

    cleaned = str(title or "").strip()
    if not cleaned:
        return None

    row = conn.execute(
        select(
            agent_sessions.c.id,
            agent_sessions.c.title,
            agent_sessions.c.metadata_json,
            agent_sessions.c.native_session_id,
        ).where(agent_sessions.c.id == session_id)
    ).mappings().first()
    if row is None:
        return None

    metadata = _load_metadata(row.get("metadata_json"))
    if metadata.get("title_source") in DELIBERATE_TITLE_SOURCES:
        return None
    if str(row.get("title") or "").strip():
        return None

    now = _utc_now_iso()
    metadata.update(
        {
            "title_source": source,
            "title_backend": backend,
            "title_synced_at": now,
        }
    )
    if native_session_id:
        metadata["title_native_session_id"] = native_session_id
    elif row.get("native_session_id"):
        metadata["title_native_session_id"] = row.get("native_session_id")
    if confidence:
        metadata["title_confidence"] = confidence

    result = conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == session_id)
        .where((agent_sessions.c.title.is_(None)) | (agent_sessions.c.title == ""))
        .where(
            func.coalesce(func.json_extract(agent_sessions.c.metadata_json, "$.title_source"), "").notin_(
                list(DELIBERATE_TITLE_SOURCES)
            )
        )
        .values(title=cleaned, metadata_json=_dumps_metadata(metadata), updated_at=now)
    )
    if result.rowcount == 0:
        return None
    return get_session(conn, session_id)


def is_session_archived(conn: Connection, session_id: str) -> bool:
    """True iff the session exists and is archived. The shared write-guard for
    by-id entry points (workbench send, show events) so "archived is terminal" is
    enforced in one place rather than re-derived per caller. Resolution paths
    rely on the archived row's vacated anchor instead (it no longer matches a
    live thread); this covers the entries that target a session by id."""
    status = conn.execute(
        select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    return status == "archived"


def count_bound_resources(conn: Connection, session_id: str) -> dict[str, int]:
    """Count what archiving ``session_id`` will permanently reclaim: bound
    scheduled tasks + watches (live, not-yet-deleted definitions) and
    not-yet-terminal runs. Shared by the archive teardown and the confirm-dialog
    preview so both agree on the numbers shown vs. acted on."""
    types = (
        conn.execute(
            select(run_definitions.c.definition_type)
            .where(run_definitions.c.session_id == session_id)
            .where(run_definitions.c.deleted_at.is_(None))
        )
        .scalars()
        .all()
    )
    watches = sum(1 for t in types if t == "watch")
    runs = (
        conn.execute(
            select(func.count())
            .select_from(agent_runs)
            .where(agent_runs.c.session_id == session_id)
            .where(agent_runs.c.status.in_(_ACTIVE_RUN_STATUSES))
        ).scalar()
        or 0
    )
    # Send-while-busy queued prompts are user-entered text that archive discards;
    # surface them so the confirm dialog doesn't say "nothing linked" while
    # silently dropping them. (PENDING reservations are transient dispatch state,
    # not user-visible, so they're not counted here.)
    from storage.messages_service import QUEUED_TYPE

    queued = (
        conn.execute(
            select(func.count())
            .select_from(messages)
            .where(messages.c.session_id == session_id)
            .where(messages.c.type == QUEUED_TYPE)
        ).scalar()
        or 0
    )
    return {
        "tasks": len(types) - watches,
        "watches": watches,
        "runs": int(runs),
        "queued": int(queued),
    }


# Longest harness label carried in the banner payload; longer strings are
# elided so one runaway prompt can't bloat the runtime-state response.
_HARNESS_LABEL_MAX = 80


def _banner_label(text: Any, limit: int = _HARNESS_LABEL_MAX) -> str:
    """Collapse whitespace and clip a harness label to a banner-friendly head."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _harness_banner_item(
    *,
    item_id: str,
    item_kind: str,
    session_id: str,
    status: str,
    label: str,
    since: str,
    updated_at: str,
    backend: str = "",
    schedule_type: str | None = None,
) -> dict[str, Any]:
    """Shape one harness row like a serialized background activity.

    The banner unions these with process-local backend activities
    (``core/session_activities.SessionActivity.to_dict``), so a harness item
    carries every key an existing ``background_activities`` consumer already
    reads, plus the unified ``item_kind`` / ``label`` / ``since`` /
    ``schedule_type`` fields the banner uses to render and route each row.
    Nothing here is written back into the registry — the row is derived fresh
    on every runtime-state build.
    """
    return {
        "id": item_id,
        "backend": backend,
        "runtime_key": "",
        "session_id": session_id,
        "kind": item_kind,
        "status": status,
        "description": label or None,
        "foreground": False,
        "detached_from_run": False,
        "parent_activity_id": None,
        "turn_id": None,
        "run_id": None,
        "metadata": {},
        "started_at": since,
        "updated_at": updated_at or since,
        "completed_at": None,
        # Unified background-work banner fields.
        "item_kind": item_kind,
        "label": label,
        "since": since,
        "schedule_type": schedule_type,
    }


def derive_session_harness_activities(conn: Connection, session_id: str) -> list[dict[str, Any]]:
    """Live-derive the harness items the background-work banner unions in.

    Read-only projection from the durable store — deliberately NOT mirrored into
    the process-local ``SessionActivityRegistry`` so the banner stays correct by
    construction across restarts (a watch survives a restart; the registry does
    not). Three sources, all scoped to ``session_id``:

    - enabled, live (not soft-deleted) watches bound to this session;
    - enabled, live scheduled tasks bound to this session;
    - queued/running delegated agent runs whose callback returns here (work this
      session dispatched and is waiting on).

    Each row is shaped like a serialized background activity plus the unified
    banner fields (see ``_harness_banner_item``).
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return []
    items: list[dict[str, Any]] = []

    # Watches + scheduled tasks live in ``run_definitions``, discriminated by
    # ``definition_type`` (see storage/background.py). Both must be enabled and
    # not soft-deleted to count as ongoing background work for the session.
    definition_rows = (
        conn.execute(
            select(
                run_definitions.c.id,
                run_definitions.c.definition_type,
                run_definitions.c.name,
                run_definitions.c.prompt,
                run_definitions.c.message,
                run_definitions.c.schedule_type,
                run_definitions.c.created_at,
                run_definitions.c.updated_at,
            )
            .where(run_definitions.c.session_id == session_id)
            .where(run_definitions.c.deleted_at.is_(None))
            .where(run_definitions.c.enabled == 1)
            .order_by(run_definitions.c.created_at, run_definitions.c.id)
        )
        .mappings()
        .all()
    )
    for row in definition_rows:
        definition_type = str(row["definition_type"] or "")
        if definition_type == "watch":
            item_kind, status = "watch", "enabled"
            label = _banner_label(row["name"])
        elif definition_type == "scheduled":
            # A fired one-shot is disabled/removed by the scheduler, so an
            # enabled scheduled definition is the pending-work proxy here.
            item_kind, status = "task", "scheduled"
            label = _banner_label(row["name"] or row["prompt"] or row["message"])
        else:
            continue
        since = str(row["created_at"] or "")
        schedule_type = (
            str(row["schedule_type"] or "").strip() or None
            if item_kind == "task"
            else None
        )
        items.append(
            _harness_banner_item(
                item_id=f"{item_kind}:{row['id']}",
                item_kind=item_kind,
                session_id=session_id,
                status=status,
                label=label,
                since=since,
                updated_at=str(row["updated_at"] or since),
                schedule_type=schedule_type,
            )
        )

    # Delegated agent runs this session dispatched and is waiting on: the run
    # executes elsewhere but its result returns here (``callback_session_id``)
    # and it is not yet terminal. ``run_type='agent_run'`` excludes task/watch
    # execution runs (already represented by their definitions above); the
    # self-run guard keeps a session's OWN foreground turn — reported via
    # ``foreground``, not the banner — out of the union. ``callback_status =
    # 'pending'`` further limits this to async/detached runs that will actually
    # post back: a synchronous ``--sync`` run carries ``callback_session_id`` but
    # a null ``callback_status`` (the caller is waiting inline, no callback owed).
    # Active-run cardinality is small, so the (run_type, status) index carries
    # this without a dedicated ``callback_session_id`` index.
    run_rows = (
        conn.execute(
            select(
                agent_runs.c.id,
                agent_runs.c.agent_name,
                agent_runs.c.agent_backend,
                agent_runs.c.message,
                agent_runs.c.prompt,
                agent_runs.c.status,
                agent_runs.c.created_at,
                agent_runs.c.started_at,
                agent_runs.c.updated_at,
            )
            .where(agent_runs.c.callback_session_id == session_id)
            .where(agent_runs.c.run_type == "agent_run")
            .where(agent_runs.c.callback_status == "pending")
            .where(agent_runs.c.status.in_(_ACTIVE_RUN_STATUSES))
            .where(
                or_(
                    agent_runs.c.session_id.is_(None),
                    agent_runs.c.session_id != agent_runs.c.callback_session_id,
                )
            )
            .order_by(agent_runs.c.created_at, agent_runs.c.id)
        )
        .mappings()
        .all()
    )
    for row in run_rows:
        head = _banner_label(row["message"] or row["prompt"])
        agent_name = str(row["agent_name"] or "").strip()
        if agent_name and head:
            label = _banner_label(f"{agent_name}: {head}")
        else:
            label = agent_name or head
        since = str(row["started_at"] or row["created_at"] or "")
        items.append(
            _harness_banner_item(
                item_id=f"agent_run:{row['id']}",
                item_kind="agent_run",
                session_id=session_id,
                status=str(row["status"] or "running"),
                label=label,
                since=since,
                updated_at=str(row["updated_at"] or since),
                backend=str(row["agent_backend"] or ""),
            )
        )
    return items


def archive_session(conn: Connection, session_id: str) -> dict[str, Any]:
    """Permanently archive a session and reclaim everything bound to it.

    Archive is terminal (there is no un-archive) — so we don't just flip a flag,
    we tear down the resources that would otherwise keep firing into a hidden
    session: bound scheduled tasks + watches are soft-deleted, queued/running
    runs are cancelled, and the Show Page is taken offline. All of it rides the
    caller's transaction so the teardown is atomic with the status flip.

    The one piece that can't live here is cancelling an in-flight chat turn: it
    runs in the controller process, reachable only over the internal socket, so
    the DELETE endpoint does that (best-effort) after this commits. ``agent_status``
    is reset to ``idle`` regardless so the workbench stops showing a "working" dot.

    Returns the archived session payload plus a ``reclaimed`` summary
    (``{tasks, watches, runs}``) for the confirm-dialog / post-archive notice.
    """
    existing = conn.execute(
        select(agent_sessions.c.id).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    if existing is None:
        raise LookupError(f"Session not found: {session_id}")
    now = _utc_now_iso()

    # 1) Mark archived + clear any stale "running" dot, and VACATE the thread
    #    anchor. There's a UNIQUE index on (scope_id, session_anchor): leaving the
    #    archived row on the live anchor would make the next inbound message on
    #    that thread collide on INSERT (the lookup guards force a fresh-row create).
    #    Re-anchoring to a per-row sentinel frees the (scope, anchor) slot while
    #    keeping this row's own anchor unique; the session stays viewable by id.
    conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == session_id)
        .values(
            status="archived",
            agent_status="idle",
            session_anchor=f"archived:{session_id}",
            updated_at=now,
        )
    )

    # Tally before teardown so the response reports what was reclaimed.
    reclaimed = count_bound_resources(conn, session_id)

    # 2) Soft-delete bound scheduled tasks + watches (same table, distinguished by
    #    ``definition_type``). Deleting — not pausing — is deliberate: a paused
    #    definition could be re-enabled later and would then target a dead session.
    conn.execute(
        update(run_definitions)
        .where(run_definitions.c.session_id == session_id)
        .where(run_definitions.c.deleted_at.is_(None))
        .values(deleted_at=now, updated_at=now)
    )

    # 3) Cancel not-yet-terminal runs for this session. Flag every active run as
    #    cancel-requested (the executor honors it for in-flight ones) and
    #    terminalize the ones that haven't started.
    if reclaimed["runs"]:
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.session_id == session_id)
            .where(agent_runs.c.status.in_(_ACTIVE_RUN_STATUSES))
            .values(cancel_requested=1, cancel_requested_at=now, updated_at=now)
        )
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.session_id == session_id)
            .where(agent_runs.c.status.in_(_PENDING_RUN_STATUSES))
            .values(status="canceled", completed_at=now)
        )

    # 3b) Reclaim all unsent user input so the terminal session retains none:
    #     queued prompts (flushed on completion / send-now), ``pending`` rows a
    #     concurrent send reserved just before this committed (``promote_pending``
    #     then no-ops on that in-flight send), and the saved composer draft.
    from storage.messages_service import clear_draft, clear_pending, clear_queued
    from storage.vault_service import (
        ACTIVE_GRANT_STATES,
        agent_release_scopes_after_rows,
        expire_session_requests,
        revoke_session_grants,
        vault_grants,
    )

    clear_queued(conn, session_id)
    clear_pending(conn, session_id)
    clear_draft(conn, session_id)
    revoked_vault_grant_rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES), vault_grants.c.session_id == session_id)
        ).mappings()
    ]
    expire_session_requests(conn, session_id)
    revoke_session_grants(conn, session_id)
    revoked_vault_grant_scopes = agent_release_scopes_after_rows(conn, revoked_vault_grant_rows)

    # 4) Take the Show Page offline so a shared link can't keep serving the
    #    archived session (no-op when the session never had one).
    conn.execute(
        update(show_pages)
        .where(show_pages.c.session_id == session_id)
        .values(visibility="offline", offline_at=now, updated_at=now)
    )

    payload = get_session(conn, session_id)
    payload["reclaimed"] = reclaimed
    payload["revoked_vault_grant_scopes"] = revoked_vault_grant_scopes
    return payload


def touch_session(conn: Connection, session_id: str) -> None:
    """Bump ``last_active_at`` after a new message arrives."""

    conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == session_id)
        .values(last_active_at=_utc_now_iso(), updated_at=_utc_now_iso())
    )


VALID_AGENT_STATUSES = ("idle", "running", "failed")


def set_agent_status(conn: Connection, session_id: str, status: str) -> bool:
    """Set a session's live agent-runtime status (idle/running/failed).

    Returns ``True`` when the stored value actually changed, so the caller can
    skip a redundant ``session.status`` broadcast (and the write) when the dot
    colour wouldn't move. Unknown status / missing session is a no-op (False).
    Deliberately does NOT bump ``updated_at`` — a status flip is not a content
    edit and must not re-rank the session list.
    """

    if status not in VALID_AGENT_STATUSES:
        return False
    current = conn.execute(
        select(agent_sessions.c.agent_status).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    if current is None or current == status:
        return False
    conn.execute(
        update(agent_sessions).where(agent_sessions.c.id == session_id).values(agent_status=status)
    )
    return True


def reset_running_agent_status(conn: Connection) -> int:
    """Reset every ``running`` session to ``idle`` (startup crash recovery).

    No turn survives a controller restart, so a ``running`` left in the table
    is stale. Returns the number of rows reset. The browser reconciles the reset
    by refetching sessions when its inbox-event stream (re)connects, NOT from a
    broadcast — this runs in ``Controller.__init__`` before any event subscriber
    exists, so a broadcast here would be dropped.
    """

    result = conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.agent_status == "running")
        .values(agent_status="idle")
    )
    return result.rowcount or 0

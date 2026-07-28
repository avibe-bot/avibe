from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import IntegrityError

from storage.models import agent_sessions, scope_settings
from storage.session_reclaim import (
    OVERRIDABLE_SETTING_COLUMNS,
    reconcile_explicit_overrides,
)

SESSION_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
JSON_VALUE_PREFIX = "__json__:"
SESSION_VISIBILITIES = frozenset({"foreground", "background"})

PRIVATE_AGENT_RUN_SCOPE_TYPE = "private_agent_run"

logger = logging.getLogger(__name__)


def session_openable_in_chat(*, session_id: Any, scope_native_type: Any = None) -> bool:
    """Whether ``/chat/<session_id>`` will actually open this session.

    One predicate for the three surfaces that each carried their own answer: the
    agent graph excluded private agent-run scopes, the running-agents list
    accepted anything with an id, and the harness cards linked only *workbench*
    sessions. Three rules for one question, so the same session could be a link
    in one view and a bare id in the next.

    The harness rule was the wrong one, and it is the one this replaces.
    ``GET /api/sessions/<id>`` resolves through
    ``workbench_sessions_service.get_session``, which filters on neither scope
    nor status: an IM-bound session opens in chat exactly as a workbench one
    does, and an archived session opens read-only (only *sending* into it is
    refused, with 409 ``session_archived``). Declining to link them hid working
    destinations behind unreadable ids.

    Only two things genuinely do not open: a row with no id, and the legacy
    ``private_agent_run`` pseudo-scope, which is deliberately kept off the chat
    surface. Callers with no cheap join to ``scopes`` may omit
    ``scope_native_type``; the result is then "openable if it exists", which is
    what the running-agents list already assumed.
    """
    if not session_id:
        return False
    return str(scope_native_type or "").strip() != PRIVATE_AGENT_RUN_SCOPE_TYPE


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_session_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return JSON_VALUE_PREFIX + json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def decode_session_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value.startswith(JSON_VALUE_PREFIX):
        return value
    try:
        return json.loads(value[len(JSON_VALUE_PREFIX) :])
    except (TypeError, ValueError):
        return value


def _metadata_object(value: Any) -> dict[str, Any]:
    """Parse a stored ``metadata_json`` blob; never raise on legacy junk."""
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def snapshot_scope_workdir(conn: Connection, scope_id: str | None) -> str | None:
    if not scope_id:
        return None
    value = conn.execute(
        select(scope_settings.c.workdir).where(scope_settings.c.scope_id == str(scope_id))
    ).scalar_one_or_none()
    return normalize_workdir(value)


def normalize_workdir(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return os.path.abspath(os.path.expanduser(text))


def new_session_id(conn: Connection) -> str:
    used = {str(value) for value in conn.execute(select(agent_sessions.c.id)).scalars()}
    while True:
        candidate = "ses" + "".join(secrets.choice(SESSION_ID_ALPHABET) for _ in range(10))
        if candidate not in used:
            return candidate


def create_agent_session_row(
    conn: Connection,
    *,
    scope_id: str | None,
    session_id: str | None = None,
    session_anchor: str | None,
    agent_backend: str,
    agent_variant: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    workdir: str | None = None,
    native_session_id: Any = "",
    title: str | None = None,
    status: str = "active",
    visibility: str = "foreground",
    agent_status: str = "idle",
    metadata: dict[str, Any] | None = None,
    now: str | None = None,
    require_workdir: bool = True,
) -> str:
    """Create the one public Session row used by every platform.

    A Session owns its cwd. Scope settings are only consulted at creation time
    to snapshot the initial workdir; later Agent turns must read the stored
    ``agent_sessions.workdir`` and never re-resolve cwd from Scope.
    """

    resolved_workdir = normalize_workdir(workdir) or snapshot_scope_workdir(conn, scope_id)
    if require_workdir and not resolved_workdir:
        raise ValueError(f"cannot create agent session without workdir for scope_id={scope_id!r}")

    visibility_value = str(visibility or "").strip()
    if visibility_value not in SESSION_VISIBILITIES:
        raise ValueError(f"invalid session visibility: {visibility!r}")

    row_id = str(session_id or new_session_id(conn))
    anchor = str(session_anchor) if session_anchor is not None else row_id
    now_value = now or utc_now_iso()
    backend = str(agent_backend or "")
    variant = str(agent_variant or backend or "default")
    title_value = title.strip() if (title or "").strip() else None
    conn.execute(
        agent_sessions.insert().values(
            id=row_id,
            scope_id=scope_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_backend=backend,
            agent_variant=variant,
            model=model,
            reasoning_effort=reasoning_effort,
            session_anchor=anchor,
            workdir=resolved_workdir,
            native_session_id=encode_session_value(native_session_id),
            title=title_value,
            status=status,
            visibility=visibility_value,
            agent_status=agent_status,
            metadata_json=json.dumps(dict(metadata or {}), separators=(",", ":"), ensure_ascii=False),
            created_at=now_value,
            updated_at=now_value,
            last_active_at=now_value,
        )
    )
    return row_id


def get_or_create_agent_session_row(
    conn: Connection,
    *,
    scope_id: str | None,
    session_anchor: str,
    agent_backend: str,
    **create_kwargs: Any,
) -> tuple[str, bool]:
    """Resolve the one session row for ``(scope_id, session_anchor)``, creating it once.

    Returns ``(session_id, created)``.

    Three things make a plain find-then-create unsafe here, and all three are the
    same defect seen from a different angle:

    1. **Lookup key narrower than the constraint key.** Callers look up by
       ``(scope, anchor, backend)`` while the UNIQUE index is ``(scope, anchor)``
       alone, so a row owned by another backend is invisible to the finder and
       fatal to the INSERT. Resolved by looking up on the CONSTRAINT key.
    2. **The find-then-create race.** SQLite deferred transactions take no write
       lock at the SELECT, so two callers can both miss. The INSERT runs inside a
       SAVEPOINT and an ``IntegrityError`` means "someone else won" — re-read.
    3. **Two backends, one thread.** A thread is ONE session per (scope, anchor);
       the incoming backend wins the anchor. An unbound row is relabelled in place
       (nothing to lose). A row that already carries a native session id is
       *superseded* instead: its anchor is moved aside so the slot frees, which
       keeps the old transcript, Show Page and any bound definitions attached to a
       row that still resolves by id. The resume path
       (``core/handlers/session_handler.py``) deletes that row outright; this is
       the same policy without the collateral damage, and it is the reason nothing
       needs reclaiming here.
    """

    anchor = str(session_anchor) if session_anchor is not None else None
    backend = str(agent_backend or "")

    existing = _row_for_scope_anchor(conn, scope_id=scope_id, session_anchor=anchor)
    if existing is not None:
        return _claim_anchor_row(
            conn,
            existing,
            scope_id=scope_id,
            session_anchor=anchor,
            backend=backend,
            create_kwargs=create_kwargs,
        )

    try:
        with conn.begin_nested():
            return (
                create_agent_session_row(
                    conn,
                    scope_id=scope_id,
                    session_anchor=anchor,
                    agent_backend=backend,
                    **create_kwargs,
                ),
                True,
            )
    except IntegrityError:
        # Lost the race for this (scope, anchor). The SAVEPOINT rolled back, so the
        # caller's transaction is intact and the winner's row is readable.
        existing = _row_for_scope_anchor(conn, scope_id=scope_id, session_anchor=anchor)
        if existing is None:
            raise
        return _claim_anchor_row(
            conn,
            existing,
            scope_id=scope_id,
            session_anchor=anchor,
            backend=backend,
            create_kwargs=create_kwargs,
        )


def _row_for_scope_anchor(
    conn: Connection,
    *,
    scope_id: str | None,
    session_anchor: str | None,
) -> dict[str, Any] | None:
    if session_anchor is None:
        return None
    row = (
        conn.execute(
            select(
                agent_sessions.c.id,
                agent_sessions.c.agent_backend,
                agent_sessions.c.agent_variant,
                agent_sessions.c.native_session_id,
            )
            .where(agent_sessions.c.scope_id == scope_id)
            .where(agent_sessions.c.session_anchor == session_anchor)
            # Never resolve onto an archived row: archive vacates the anchor, so a
            # match here would be a stale one. Matches the bind/resolve read paths.
            .where(agent_sessions.c.status != "archived")
            .order_by(agent_sessions.c.last_active_at.desc(), agent_sessions.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


# A superseded row keeps its original anchor with this marker appended, so
# ``thread_id_from_session_anchor`` still recovers the thread for definitions
# pinned to it. Anything that matches anchors by PREFIX must exclude the marker
# explicitly -- ``delete_agent_sessions`` does, because superseding promises the
# row is kept and a prefix clear would otherwise hard-delete it.
SUPERSEDED_ANCHOR_MARKER = "superseded"
SUPERSEDED_ANCHOR_INFIX = f":{SUPERSEDED_ANCHOR_MARKER}:"


def _claim_anchor_row(
    conn: Connection,
    row: dict[str, Any],
    *,
    scope_id: str | None,
    session_anchor: str | None,
    backend: str,
    create_kwargs: dict[str, Any],
) -> tuple[str, bool]:
    row_id = str(row["id"])
    current_backend = str(row["agent_backend"] or "")
    if not backend or current_backend == backend:
        return row_id, False

    now = utc_now_iso()
    if not decode_session_value(row["native_session_id"]):
        # Unbound row: relabel it to the incoming backend. This is the ordinary
        # ``ensure_agent_session_id`` case — the row exists only to reserve the
        # thread's identity, so adopting it costs nothing and avoids a collision.
        #
        # The WHOLE route is replaced, not merged. The guard above restricts this
        # branch to a genuine backend CHANGE on a row with NO committed native
        # conversation: it never produced a turn under its old backend, so its
        # agent / model / reasoning_effort are provisional reservations, not a
        # user's choice. Merging them ("keep what the incoming route leaves
        # ``None``") is what let a row become Claude-owned while still carrying a
        # Codex model and Codex Agent name — the legacy
        # ``ensure_agent_session_id`` / ``bind_agent_session`` callers have no
        # model / effort parameters and pass ``None`` unconditionally, so for them
        # the merge was guaranteed to keep the old backend's settings. The "None
        # means I have no opinion" reading that legitimately applies to
        # same-backend binds never reaches here, because a same-backend claim
        # returns above.
        values: dict[str, Any] = {
            "agent_backend": backend,
            "agent_variant": str(create_kwargs.get("agent_variant") or backend),
            "updated_at": now,
        }
        for column in ("agent_id", "agent_name", "model", "reasoning_effort"):
            values[column] = create_kwargs.get(column)
        # Resetting the columns must also drop their explicit-override marker:
        # left behind, it keeps telling dispatch that this session pins the (now
        # cleared) model on purpose, so the adopted row would still route the old
        # backend's settings.
        stored_metadata = conn.execute(
            select(agent_sessions.c.metadata_json).where(agent_sessions.c.id == row_id)
        ).scalar_one_or_none()
        values["metadata_json"] = json.dumps(
            reconcile_explicit_overrides(
                _metadata_object(stored_metadata), cleared=OVERRIDABLE_SETTING_COLUMNS
            ),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        # The three predicates below RE-ASSERT the state this branch was decided
        # from, because none of the reads above hold it. ``_row_for_scope_anchor``
        # and the metadata SELECT reserve nothing: pysqlite emits no ``BEGIN`` for a
        # bare SELECT, so the write lock is taken at the first DML and a
        # still-finishing turn on the OLD backend can commit its native id -- or an
        # archive can land -- between the decision and this statement. A bare ``id``
        # match then relabels a row that is no longer relabellable: the row ends up
        # ``agent_backend='claude'`` while still holding the Codex native id the
        # winner just bound, with the whole route wiped and the override marker
        # cleared. The sibling writer ``bind_agent_session_by_id`` carries exactly
        # these three for the same reason (HFR-251); this one, added by the same PR,
        # carried none.
        relabelled = conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == row_id)
            # Still live. An archive is terminal and vacates the anchor, so a
            # relabel here would rewrite a row nothing should resolve to.
            .where(agent_sessions.c.status != "archived")
            # Still unbound: WRITE-ONCE enforced BY THE STATEMENT. A committed native
            # id is backend-specific, so a bound row must be SUPERSEDED (below) and
            # never relabelled. A rule enforced by a preceding SELECT is not
            # write-once.
            .where(func.coalesce(agent_sessions.c.native_session_id, "") == "")
            # Still on the backend this branch decided AGAINST, so a concurrent
            # claim that already moved the row cannot be overwritten by this
            # caller's stale route.
            .where(func.coalesce(agent_sessions.c.agent_backend, "") == current_backend)
            .values(**values)
        )
        if relabelled.rowcount:
            return row_id, False
        # LOST the race. Return the winner UNTOUCHED -- its backend identity, its
        # write-once native id, its model / reasoning_effort and its override marker
        # all stand. Falling through to the supersede branch below would be a second
        # write decided from the same stale snapshot, which is the defect this guard
        # exists to prevent; the caller's next resolve re-reads and re-decides.
        logger.warning(
            "Lost the anchor-relabel race for session %s; keeping the winner's route "
            "(requested backend=%s, decided from=%s)",
            row_id,
            backend,
            current_backend or "''",
        )
        return row_id, False

    # Bound to another backend: its native id is write-once and backend-specific,
    # so it cannot be relabelled. Move its anchor aside and let the caller create a
    # fresh row on the freed slot. Nothing is deleted.
    #
    # The marker is a SUFFIX on the original anchor, not a replacement for it.
    # Definitions pinned to this row survive the supersede, and
    # ``resolve_session_id_target`` derives their thread solely from
    # ``session_anchor`` via ``thread_id_from_session_anchor``, which reads
    # ``anchor.split(":", 1)[0]``. A bare ``superseded:<id>`` leaves that base as
    # the literal "superseded", matching no platform prefix, so every pinned
    # ``--post-to thread`` definition would silently fall back to the channel
    # root -- and because the row still exists, the unresolvable-binding recovery
    # never fires to catch it. Suffixing keeps the base parseable while still
    # freeing the slot, since anchor lookups match the full string exactly.
    # Colon-suffixed anchors are already the convention here (see the
    # "pre-scoped session anchors" branch in ``thread_id_from_session_anchor``).
    # The row was located by this exact anchor, so the parameter is its current
    # value; the row mapping here does not carry the column.
    current_anchor = str(session_anchor or "")
    if not current_anchor or SUPERSEDED_ANCHOR_INFIX in current_anchor:
        superseded_anchor = f"{SUPERSEDED_ANCHOR_MARKER}:{row_id}"
    else:
        superseded_anchor = f"{current_anchor}{SUPERSEDED_ANCHOR_INFIX}{row_id}"
    conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == row_id)
        .values(session_anchor=superseded_anchor, updated_at=now)
    )
    return (
        create_agent_session_row(
            conn,
            scope_id=scope_id,
            session_anchor=session_anchor,
            agent_backend=backend,
            **create_kwargs,
        ),
        True,
    )

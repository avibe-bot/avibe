from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, select, update

from storage.models import agent_sessions, scope_settings
from storage.session_reclaim import (
    OVERRIDABLE_SETTING_COLUMNS,
    reconcile_explicit_overrides,
)

SESSION_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
JSON_VALUE_PREFIX = "__json__:"

#: ``agent_sessions.visibility`` — the STORAGE vocabulary, and the axis every session
#: list and inbox surface already filters on (it is indexed: ``storage/models.py``
#: ``ix_agent_sessions_visibility``). ``foreground`` = an ordinary chat; ``background``
#: = a session that is hidden AND whose replies are not delivered; ``system`` = a row
#: the RUNTIME owns, kept out of ordinary session lists while staying a first-class
#: inbox/receipt destination (see ``WORKSPACE_NOTICE_SESSION_VISIBILITY``).
#:
#: There is no CHECK constraint on the column, so this set IS the vocabulary — every
#: writer validates against it in Python.
SESSION_VISIBILITIES = frozenset({"foreground", "background", "system"})

#: The subset a CALLER may choose. ``system`` is deliberately excluded: it is a runtime
#: classification, not a user preference, so ``PATCH /api/sessions/<id>`` and
#: ``vibe session update`` cannot promote an ordinary chat into a system surface (which
#: would hide it from every list while leaving it delivering). Only this module's own
#: reserved-row create/heal writes ``system``.
ASSIGNABLE_SESSION_VISIBILITIES = frozenset({"foreground", "background"})

#: Visibilities the INBOX admits, i.e. the ones whose sessions can own a card, a
#: realtime ``inbox.session.updated`` and a Web Push. Positive form on purpose: a
#: future fourth value is hidden by default and has to opt in here explicitly.
INBOX_SESSION_VISIBILITIES: tuple[str, ...] = ("foreground", "system")

PRIVATE_AGENT_RUN_SCOPE_TYPE = "private_agent_run"
SESSION_PROJECT_BASE_METADATA_KEY = "project_base"

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


def normalize_session_project_base(workdir: Any, candidate: Any) -> str | None:
    """Return an absolute project base only when it contains the Session cwd."""

    normalized_workdir = normalize_workdir(workdir)
    normalized_candidate = normalize_workdir(candidate)
    if not normalized_workdir or not normalized_candidate:
        return None
    resolved_workdir = Path(normalized_workdir).resolve()
    resolved_candidate = Path(normalized_candidate).resolve()
    try:
        resolved_workdir.relative_to(resolved_candidate)
    except ValueError:
        return None
    return str(resolved_candidate)


#: A write that changes nothing, used ONLY to reserve SQLite's writer slot inside a
#: transaction that is already open, where ``BEGIN IMMEDIATE`` is illegal. SQLite takes
#: the write lock at the START of an UPDATE program -- before its WHERE loop -- so a
#: never-matching UPDATE becomes the writer without touching a row. That is the one
#: property it exists for, so a test pins it rather than trusting it.
_WRITE_LOCK_RESERVATION_SQL = "UPDATE agent_sessions SET id = id WHERE 1 = 0"


def reserve_write_lock(conn: Connection) -> None:
    """Hold SQLite's write lock for the rest of this transaction, from BEFORE the reads.

    THE RACE THIS REMOVES INSTEAD OF DETECTING. SQLite takes the write lock at a
    transaction's first WRITE, never at its reads, and in WAL mode a transaction that
    already holds a READ snapshot can no longer BECOME a writer once another connection
    has committed: SQLite answers ``SQLITE_BUSY_SNAPSHOT`` and ``busy_timeout`` never
    retries it, because waiting cannot make a snapshot newer. So every "read, decide,
    write" sequence here carries a window, and the widest one was the first-turn INSERT:
    a ``SAVEPOINT`` opened the transaction and ``new_session_id``'s scan pinned its
    snapshot inside it, so a competing turn committing in between left the loser unable
    to write at all -- and every caller of a shared get-or-create saw "database is
    locked" out of an ordinary first turn.

    Calling this BEFORE those reads means the failure cannot happen rather than being
    recovered from afterwards: the lock is held for the remainder of the transaction, so
    nothing the decision rests on can change before the write lands, and there is no
    error left to classify. That is also why it holds on every supported interpreter.
    RECOGNISING ``SQLITE_BUSY_SNAPSHOT`` requires ``sqlite3.Error.sqlite_errorcode``
    (Python 3.11+), because its primary code and its message are the ones every
    unrelated ``SQLITE_BUSY`` carries and neither discriminates -- while this project
    supports Python 3.10 (``requires-python >= 3.10``), where that attribute does not
    exist. NOT PRODUCING the error requires nothing from the interpreter at all.

    ``_claim_anchor_row``'s supersede branch has always depended on exactly this: its
    anchor-move UPDATE takes the lock, which is the whole reason the INSERT after it
    needs no re-read of its own. This makes the same guarantee available to the paths
    that have no write to lead with.

    TWO SPELLINGS, ONE GUARANTEE, because SQLite has no ``LOCK TABLE``:

    * In autocommit -- which is every production entry point into the create path, since
      pysqlite opens no transaction for a bare SELECT -- ``BEGIN IMMEDIATE`` takes the
      write lock AND the read snapshot in ONE statement, so there is no instant between
      them for a competing commit to land in. SQLAlchemy's pysqlite dialect emits no
      ``BEGIN`` of its own, so this is the transaction the enclosing ``engine.begin()``
      goes on to commit or roll back.
    * Inside a transaction that is already open, ``BEGIN IMMEDIATE`` is illegal, so
      ``_WRITE_LOCK_RESERVATION_SQL`` reserves the writer slot instead. If that
      transaction is already a writer the statement is a no-op; if it has only read, this
      is where a stale snapshot surfaces -- before this module's own reads and with
      nothing of ours yet written, which is the earliest and cheapest place for it.

    WHAT IT COSTS is contention, not correctness: concurrent FIRST turns on the same
    database now queue behind each other for the length of the caller's transaction
    rather than racing and having one lose. ``busy_timeout`` (5s, ``storage/db.py``)
    bounds that wait, the create path runs once per thread, and the previous behaviour on
    this very interleaving was an exception rather than a faster answer. It is
    deliberately NOT applied to the read-mostly fast path where a session row already
    exists: those writes re-assert their read inside the statement instead
    (``unchanged_text``), which costs no serialisation.
    """

    if conn.connection.dbapi_connection.in_transaction:
        conn.exec_driver_sql(_WRITE_LOCK_RESERVATION_SQL)
        return
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def new_session_id(conn: Connection) -> str:
    used = {str(value) for value in conn.execute(select(agent_sessions.c.id)).scalars()}
    while True:
        candidate = "ses" + "".join(secrets.choice(SESSION_ID_ALPHABET) for _ in range(10))
        if candidate not in used:
            return candidate


#: The reserved workspace-notifications Session — D5 rung (5) of the harness failure
#: ladder (``docs/plans/harness-run-reliability.md:3193``, :3215-3222).
#:
#: SPELLED OUTSIDE ``SESSION_ID_ALPHABET``, which is the whole reason it is a literal
#: rather than a generated id: the alphabet has no ``-``, so ``new_session_id`` can
#: never mint this value and the reserved row can never collide with an ordinary
#: session. That makes the PRIMARY KEY the idempotence key — two racing creators
#: produce one row and the loser simply reads it back — with no marker search, no new
#: column and no migration.
WORKSPACE_NOTICE_SESSION_ID = "ses-workspace-notices"

#: Its ``session_anchor``. Also outside the generated namespace, so the
#: ``uq_agent_sessions_scope_anchor`` slot it occupies cannot be claimed by a thread.
WORKSPACE_NOTICE_SESSION_ANCHOR = "avibe_workspace_notices"

#: Metadata marker on the row, for an operator reading ``agent_sessions`` directly.
#: NOT the lookup key — see ``WORKSPACE_NOTICE_SESSION_ID``.
WORKSPACE_NOTICE_SESSION_METADATA_KEY = "workspace_notice_session"

#: Its ``visibility``: a SYSTEM surface, which is what makes the row simultaneously
#: invisible to ordinary session lists and a first-class inbox destination. See
#: ``resolve_workspace_notice_session`` for why that pair needs a third value rather
#: than either of the two that existed.
WORKSPACE_NOTICE_SESSION_VISIBILITY = "system"


def session_is_runtime_owned(*, session_id: Any, visibility: Any = None) -> bool:
    """Whether the RUNTIME owns this session, i.e. it accepts no user turn.

    ``resolve_workspace_notice_session`` creates its row with "no backend and no turns"
    (``docs/plans/harness-run-reliability.md``): it exists only to hold workspace
    failure notices. Everything that WRITES a session already refuses it —
    ``archive_session`` and ``update_session`` raise ``ReservedSessionError`` — but
    ``system`` visibility deliberately keeps the row in the inbox, so its card is a
    clickable chat, and a chat's composer POSTs messages. Without this predicate that
    was the one door left open: a user could type into the workspace-notifications card
    and dispatch a real agent turn into a machine-owned row with an empty
    ``agent_backend``, mixing conversation into the failure-notice transcript.

    TWO TESTS, OR'd, because they fail in different directions:

    * ``visibility == 'system'`` is the PROJECTION, and the axis every other surface
      already filters on — so a future system-owned row inherits the refusal instead of
      needing its own line here. It is also free at the call site: the payload
      ``get_session`` already returns carries ``visibility``.
    * the reserved IDENTITY covers the row in the states where the projection is
      momentarily wrong. That row heals lazily — ``resolve_workspace_notice_session``
      repairs an archived status, a drifted visibility or a vacated anchor on the NEXT
      notice, not on read — so between an operator's ``UPDATE … SET
      visibility='foreground'`` (or a round-12/13 development row) and that heal, a
      visibility-only test would admit the turn. This is the same reason the archive and
      update guards test identity rather than row state.

    ``visibility`` is optional so a caller holding only an id (no row loaded) still gets
    the identity half rather than a false ``False``.
    """
    if str(session_id or "").strip() == WORKSPACE_NOTICE_SESSION_ID:
        return True
    return str(visibility or "").strip() == WORKSPACE_NOTICE_SESSION_VISIBILITY


def _workspace_notice_session_is_usable(conn: Connection) -> bool | None:
    """``True`` usable, ``False`` present-but-unusable, ``None`` absent.

    THREE answers, not two, because the caller's three actions are different: return it,
    heal it, create it. Collapsing "unusable" into "absent" would make the create path
    collide with the row's own primary key; collapsing it into "usable" is the silent
    swallow this predicate exists to detect.

    Usability is defined by what the DELIVERY SURFACE requires, not by what the row
    looks like: ``list_inbox_sessions`` filters on ``status != 'archived'`` AND
    ``visibility IN ('foreground', 'system')``, and the reserved anchor has to be back
    in place for the row to be the one the identity names. Everything else about the
    row — title, metadata, history — is somebody's record and no business of this check.

    ``system`` is required EXACTLY, not merely accepted: a ``foreground`` row is
    deliverable but is also a visible chat in every ordinary session list, which is the
    projection this round removed. So a round-12/13 development row (or any row an
    operator re-labelled) is repaired rather than left half-projected — which is also
    why this needs no Alembic migration.
    """

    row = conn.execute(
        select(
            agent_sessions.c.status,
            agent_sessions.c.visibility,
            agent_sessions.c.session_anchor,
        ).where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
    ).mappings().first()
    if row is None:
        return None
    return (
        str(row["status"] or "") == "active"
        and str(row["visibility"] or "") == WORKSPACE_NOTICE_SESSION_VISIBILITY
        and str(row["session_anchor"] or "") == WORKSPACE_NOTICE_SESSION_ANCHOR
    )


def resolve_workspace_notice_session(
    conn: Connection,
    *,
    title: str | None = None,
    now: str | None = None,
) -> str:
    """Resolve — creating once — the Session that harness failure notices fall back to.

    D5's ladder ends in a row addressed to the WORKSPACE rather than to a person,
    because a definition created by a plain ``vibe task add`` has no caller provenance
    (so rungs (3) and (4) are empty), may have no delivery key (rung (1)), and may have
    no session binding at all (rung (2), and the session-derived rung (5) candidate).
    Without this row every rung is empty and the notice can only dead-letter.

    DURABILITY BY LAZY RECREATION, not by exemption. Nothing here asks ``/new``'s
    clear path or session eviction for a special case: if some other machinery removes
    this row, the next notice creates it again. That is why the identity has to be
    STABLE and why it is the primary key that enforces uniqueness.

    CONCURRENCY. Two notice-drain owners can race the create. The unlocked read is the
    hot path (the row almost always exists); the create path reserves SQLite's writer
    slot with ``reserve_write_lock`` FIRST and re-decides underneath it, which is the
    same answer ``get_or_create_agent_session_row`` gives the first-turn INSERT — one
    row, and no ``SQLITE_BUSY_SNAPSHOT`` for the loser to classify.

    NO SCOPE, deliberately. Every ``avibe`` project scope is a row in
    ``projects_service.list_projects``, so anchoring this session to a reserved project
    would mint a fake project in the workbench sidebar. ``agent_sessions.scope_id`` is
    nullable and already carries scope-less sessions (``reserve_standalone_agent_session``),
    ``persist_agent_message``'s avibe branch writes on ``session_row is not None``
    regardless of scope, and the inbox card falls back to ``'avibe'`` for a null
    project — so a scope-less session is an existing shape rather than a new one.

    A SYSTEM SURFACE, NOT A FOREGROUND CHAT (``visibility='system'``). The row has to be
    two things at once, and the two visibility values that existed each gave exactly one
    of them: ``foreground`` delivers but is also an ordinary chat in every session list
    (a machine-owned row users cannot usefully talk to, offered as somewhere to talk);
    ``background`` hides but is filtered OUT of ``list_inbox_sessions`` /
    ``unread_counts_by_session``, i.e. it hides the notice itself, which is the one thing
    rung (5) exists to show — and it additionally sets ``suppress_delivery``, so no
    realtime event and no push either. ``system`` is the projection: kept out of the
    ordinary session lists (which all filter POSITIVELY on ``== 'foreground'``, so they
    exclude it with no change of theirs) and admitted to the inbox surfaces explicitly
    (``INBOX_SESSION_VISIBILITIES``). Delivery is untouched — ``suppress_delivery`` keys
    on ``== 'background'`` alone, so a system session delivers exactly like a foreground
    one.

    WHY A VALUE IN AN EXISTING COLUMN. ``visibility`` is precisely the axis every list
    and inbox surface already filters on, and it is indexed
    (``ix_agent_sessions_visibility``), so the projection costs one predicate on queries
    that already carry one. A ``metadata_json`` flag would put JSON parsing into the hot
    inbox/list queries; a dedicated column would be a migration for a single row. A
    session class that some surfaces refuse is also not new here —
    ``PRIVATE_AGENT_RUN_SCOPE_TYPE`` is deliberately kept off the chat surface the same
    way. And because ``system`` is not in ``ASSIGNABLE_SESSION_VISIBILITIES``, no caller
    can label an ordinary chat with it.

    ``title`` is the localized display name, applied at CREATE time only: it is
    persisted product copy, so a later language change does not rewrite it (the row is
    named once, by whoever's notice created it).

    AND IT HEALS, because recreation covers only REMOVAL. The states that matter are the
    ones where the row still exists under its reserved primary key and is nonetheless
    unusable — an ARCHIVED status, a non-``system`` visibility, a vacated
    ``archived:<id>`` anchor — and each of them fails SILENTLY rather than loudly: the
    notice still persists through ``_session_row`` (no status filter), still earns its
    receipt, still acks, while ``list_inbox_sessions`` shows nothing (or shows it in the
    wrong place). So a row that exists but is not usable is repaired in place, under the
    same write lock the create takes, which also repairs a database archived before
    ``archive_session`` learned to refuse this id — and converts a round-12/13
    development row that predates ``system``, which is why this needs no migration: the
    row exists in no release. The defences are independent on purpose: the
    archive/update guards close the API doors, this closes every other one.
    """

    healthy = _workspace_notice_session_is_usable(conn)
    if healthy is True:
        return WORKSPACE_NOTICE_SESSION_ID

    # BEFORE the re-read the INSERT rests on, for the reason ``reserve_write_lock``
    # spells out: a competing creator committing between the read and the write leaves
    # this transaction unable to write at all.
    reserve_write_lock(conn)
    healthy = _workspace_notice_session_is_usable(conn)
    if healthy is True:
        return WORKSPACE_NOTICE_SESSION_ID
    if healthy is False:
        # The row exists and is unusable. Repair exactly the columns that make it
        # unusable and nothing else — its title, metadata and message history are
        # somebody's record and are not this call's to rewrite.
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
            .values(
                status="active",
                visibility=WORKSPACE_NOTICE_SESSION_VISIBILITY,
                # ``archive_session`` re-anchors to ``archived:<id>`` to free the
                # (scope, anchor) slot; restoring the reserved anchor cannot collide,
                # because this row's ``scope_id`` is NULL and SQLite treats NULLs in a
                # UNIQUE index as distinct.
                session_anchor=WORKSPACE_NOTICE_SESSION_ANCHOR,
                agent_status="idle",
                updated_at=now or utc_now_iso(),
            )
        )
        return WORKSPACE_NOTICE_SESSION_ID

    return create_agent_session_row(
        conn,
        scope_id=None,
        session_id=WORKSPACE_NOTICE_SESSION_ID,
        session_anchor=WORKSPACE_NOTICE_SESSION_ANCHOR,
        # No backend and no turns: nothing is ever dispatched into this session, it
        # only holds notification rows. An empty backend is the honest value, and
        # ``create_agent_session_row`` normalizes the variant to ``default``.
        agent_backend="",
        title=title,
        visibility=WORKSPACE_NOTICE_SESSION_VISIBILITY,
        metadata={WORKSPACE_NOTICE_SESSION_METADATA_KEY: True},
        now=now,
        # No Scope to snapshot a workdir from, and none is wanted: a workdir would
        # imply a runtime, and creating a directory for a row that never runs anything
        # is a filesystem side effect the notice path has no business taking.
        require_workdir=False,
    )


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

    scope_workdir = snapshot_scope_workdir(conn, scope_id)
    resolved_workdir = normalize_workdir(workdir) or scope_workdir
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
    metadata_value = dict(metadata or {})
    project_base = normalize_session_project_base(
        resolved_workdir,
        metadata_value.get(SESSION_PROJECT_BASE_METADATA_KEY),
    ) or normalize_session_project_base(resolved_workdir, scope_workdir)
    if project_base is not None:
        metadata_value[SESSION_PROJECT_BASE_METADATA_KEY] = project_base

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
            metadata_json=json.dumps(metadata_value, separators=(",", ":"), ensure_ascii=False),
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
) -> tuple[str | None, bool]:
    """Resolve the one session row for ``(scope_id, session_anchor)``, creating it once.

    Returns ``(session_id, created)``, where ``session_id`` is ``None`` when there is
    NO usable session: a claim race was lost (relabel or supersede, see
    ``_claim_anchor_row``) to a writer that ARCHIVED or removed the row. An archive is
    terminal, so the winner's id is not an answer, and every caller must degrade
    instead of using it.

    Three things make a plain find-then-create unsafe here, and all three are the
    same defect seen from a different angle:

    1. **Lookup key narrower than the constraint key.** Callers look up by
       ``(scope, anchor, backend)`` while the UNIQUE index is ``(scope, anchor)``
       alone, so a row owned by another backend is invisible to the finder and
       fatal to the INSERT. Resolved by looking up on the CONSTRAINT key.
    2. **The find-then-create race.** SQLite takes no write lock at a SELECT, so two
       callers can both miss and both try to INSERT the first row. Resolved by
       ``reserve_write_lock``: the create path takes the write lock BEFORE the reads the
       INSERT rests on and re-decides under it, so there is no window for a competing
       creator to commit in — neither the ``IntegrityError`` from the UNIQUE index nor
       the ``SQLITE_BUSY_SNAPSHOT`` that a stale read snapshot used to produce.
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
    if existing is None:
        # NOTHING holds the anchor, so this call intends to INSERT. Reserve the writer
        # slot BEFORE the reads that INSERT rests on -- the id scan in
        # ``new_session_id`` and the workdir snapshot -- and then take the decision
        # AGAIN underneath it. With the lock held no other connection can commit, so
        # the second read is final and the INSERT cannot collide with a competing
        # creator; it is the same reason the supersede branch's INSERT needs no re-read.
        #
        # The first read stays unlocked deliberately: it is the hot path every message
        # takes and a session row almost always exists by then. That is exactly why it
        # cannot be trusted here -- a winner may commit between it and the reservation --
        # so the reserved re-read is what the create decides from.
        reserve_write_lock(conn)
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


def unchanged_text(column: Any, value: Any) -> Any:
    """Predicate re-asserting that a TEXT column still holds what a read observed.

    THE ONE IDIOM in this module: a decision made from a SELECT is re-asserted inside
    the statement that acts on it, because the SELECT reserves nothing -- pysqlite
    emits no ``BEGIN`` for a bare SELECT, so SQLite takes the write lock at the first
    DML and another connection can commit in between. Factored because every such
    re-assertion needs the same NULL handling: these columns are nullable text and a
    bare ``col == value`` over a NULL evaluates to NULL, not false, so without
    ``COALESCE`` the guard silently stops guarding exactly the legacy rows (blank
    backend, NULL anchor) most likely to be raced on.
    """

    return func.coalesce(column, "") == str(value or "")


def live_session_or_none(conn: Connection, row_id: str) -> str | None:
    """Re-read a lost race's winner: its id if still usable, ``None`` if not.

    The ANSWER half of the idiom, shared by the lost-race paths here: refusing the
    write is only half of it, since what the function RETURNS is part of the same
    invariant. A winner that ARCHIVED the row is terminal and vacates the anchor --
    every read path filters ``status != 'archived'``, and
    ``BaseAgent.ensure_agent_session_id`` pins any non-empty answer straight into the
    turn context without re-resolving -- so the archived id is no answer at all, and
    neither is a missing row (a concurrent hard delete). Same answer the sibling
    writers ``bind_agent_session`` / ``bind_agent_session_by_id`` give for the same
    interleaving (HFR-251/252/253/254).
    """

    status = conn.execute(
        select(agent_sessions.c.status).where(agent_sessions.c.id == str(row_id))
    ).scalar_one_or_none()
    if status is None or str(status) == "archived":
        return None
    return str(row_id)


def _claim_anchor_row(
    conn: Connection,
    row: dict[str, Any],
    *,
    scope_id: str | None,
    session_anchor: str | None,
    backend: str,
    create_kwargs: dict[str, Any],
) -> tuple[str | None, bool]:
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
        # exists to prevent.
        #
        # WHICH WINNER, THOUGH. Two different writes can take this branch's row, and
        # only one of them leaves a usable session:
        #
        # * a concurrent BIND (the row is live and now carries a native id) -- a real
        #   session, on the winner's backend, which this caller's turn may go on to
        #   use; answer with its id.
        # * a concurrent ARCHIVE -- terminal. The archive VACATES the anchor, and
        #   every read path here filters ``status != 'archived'`` (this function's own
        #   ``_row_for_scope_anchor`` included), so the archived id resolves to nothing
        #   anywhere else. There is no later re-resolve to correct it either:
        #   ``BaseAgent.ensure_agent_session_id`` pins whatever non-empty id it is
        #   handed straight into ``context.platform_specific['agent_session_id']`` and
        #   the turn runs against that row. So the answer is ``None``, exactly as the
        #   two sibling writers ``bind_agent_session`` and
        #   ``bind_agent_session_by_id`` already answer for the same interleaving
        #   (HFR-251/252/254). Not a re-resolve and not a fresh row: either would be a
        #   second decision from the snapshot this branch was just refused for.
        winner = live_session_or_none(conn, row_id)
        logger.warning(
            "Lost the anchor-relabel race for session %s; keeping the winner's route "
            "(requested backend=%s, decided from=%s, winner=%s)",
            row_id,
            backend,
            current_backend or "''",
            "live" if winner else "archived or gone",
        )
        return winner, False

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
    # The three predicates RE-ASSERT the state this branch was decided from, for the
    # reason the relabel branch above spells out: the reads reserve nothing, so a second
    # backend claim observing the SAME bound ``(scope_id, session_anchor)`` row can move
    # the anchor and create its replacement inside this window. With a bare ``id`` match
    # the loser reported a successful anchor move -- it re-superseded a row whose anchor
    # had already moved -- and then ran straight into ``create_agent_session_row``, where
    # its INSERT collided with the winner's replacement on the UNIQUE
    # ``(scope_id, session_anchor)`` index. The caller got an ``IntegrityError`` out of a
    # function whose entire contract is "resolve the one session row for this anchor,
    # creating it once", instead of the winning session.
    moved = conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == row_id)
        # Still live. An archive is terminal and vacates the anchor to its own
        # sentinel, so there is no slot here to free and nothing to supersede.
        .where(agent_sessions.c.status != "archived")
        # Still holding the anchor this call resolved it by: once another claim has
        # moved it aside, the slot is not ours to free and may already be filled by
        # the winner's replacement row.
        .where(unchanged_text(agent_sessions.c.session_anchor, current_anchor))
        # Still on the backend this branch decided against, so a concurrent claim that
        # already adopted the row cannot be superseded out from under it.
        .where(unchanged_text(agent_sessions.c.agent_backend, current_backend))
        .values(session_anchor=superseded_anchor, updated_at=now)
    )
    if not moved.rowcount:
        # LOST the race. Answer with the row that HOLDS the anchor now, re-read; do NOT
        # insert. A second decision from the refused snapshot is exactly the defect, and
        # the INSERT is the statement that would raise. ``_row_for_scope_anchor`` already
        # excludes archived rows, so a winner that archived (or a hard delete) yields
        # ``None`` -- no usable session -- which every caller of
        # ``get_or_create_agent_session_row`` already handles.
        #
        # The winner may be on a DIFFERENT backend than this caller asked for. That is
        # the same degradation the relabel branch accepts: a thread is ONE session per
        # (scope, anchor), the race decided whose, and re-superseding from the stale
        # snapshot would leave two writers taking turns evicting each other.
        winner = _row_for_scope_anchor(conn, scope_id=scope_id, session_anchor=session_anchor)
        winner_id = str(winner["id"]) if winner is not None else None
        logger.warning(
            "Lost the anchor-supersede race for session %s; returning the current "
            "anchor holder (requested backend=%s, decided from=%s, winner=%s)",
            row_id,
            backend,
            current_backend or "''",
            winner_id or "archived or gone",
        )
        return winner_id, False
    # The anchor move above took the write lock, and it is held for the rest of this
    # transaction, so no other connection can fill the slot between freeing it and this
    # INSERT. That -- not a preceding read -- is why the create needs no re-read of its
    # own here, and it is the guarantee ``reserve_write_lock`` now gives the first-turn
    # create, which has no write to lead with.
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

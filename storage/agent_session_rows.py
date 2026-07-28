from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from storage.models import agent_sessions, scope_settings
from storage.session_reclaim import (
    OVERRIDABLE_SETTING_COLUMNS,
    reconcile_explicit_overrides,
)

#: How many times the first-turn INSERT may be re-attempted after a stale read
#: snapshot. Two, because each attempt resets the snapshot immediately before the
#: write: losing twice to writers that did not take the anchor is already a
#: pathological interleaving, and an unbounded retry inside a caller's transaction is
#: how a livelock is built.
_INSERT_ATTEMPTS = 2

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


#: SQLite extended result code ``SQLITE_BUSY_SNAPSHOT`` (5 | (2<<8)). Raised as a
#: PLAIN ``sqlite3.OperationalError`` whose primary code is ``SQLITE_BUSY`` and whose
#: message is the same "database is locked" every unrelated busy error carries, so the
#: extended code is the only thing that identifies it.
SQLITE_BUSY_SNAPSHOT = 517


def lost_to_stale_snapshot(exc: OperationalError) -> bool:
    """Whether ``exc`` is WAL's "your read snapshot is too old to write from".

    THE INTERLEAVING. In WAL mode a deferred transaction takes its read snapshot at
    its first read. If another connection COMMITS after that and this transaction
    then tries to write, SQLite cannot serialise the two and returns
    ``SQLITE_BUSY_SNAPSHOT``. It is not a lock-contention error: ``busy_timeout``
    does not retry it, because waiting cannot make a snapshot newer. The only
    remedy is to end the read transaction and decide again.

    MATCHED ON THE EXTENDED CODE, NEVER THE MESSAGE OR THE CLASS. Catching
    ``OperationalError`` broadly here would swallow a corrupt database, a missing
    table or a genuine lock timeout and silently degrade the caller's session
    instead of failing; and the message text is shared with every other
    ``SQLITE_BUSY``, so it discriminates nothing.

    ``sqlite_errorcode`` is available from Python 3.11. On an older interpreter this
    returns ``False`` and the error propagates unchanged: deliberately, because the
    only alternative there is guessing from the message, which is the broad catch
    this function exists to avoid.
    """

    return getattr(getattr(exc, "orig", None), "sqlite_errorcode", None) == SQLITE_BUSY_SNAPSHOT


def _abandon_stale_read_snapshot(conn: Connection) -> bool:
    """End a read transaction that ``SQLITE_BUSY_SNAPSHOT`` has made unusable.

    Nothing else can be done with such a transaction: it cannot write (that is the
    error) and it cannot READ the winner either -- its snapshot predates the
    competing commit, so a re-read still shows the row set the decision was already
    refused for. Rolling it back is what SQLite documents, and it costs nothing here:
    ``SQLITE_BUSY_SNAPSHOT`` is only reachable while this transaction holds NO write
    lock, and SQLite allows exactly one writer, so a transaction that hits it has
    written nothing that a rollback could throw away. pysqlite tracks
    ``sqlite3_get_autocommit``, so the caller's next statement opens a fresh
    transaction and the enclosing ``engine.begin()`` still commits or rolls back
    normally.

    Returns ``False`` when an ENCLOSING savepoint owns this transaction: that
    savepoint is a caller's rollback boundary and a bare ``ROLLBACK`` would destroy
    it, so the error is left to propagate instead.
    """

    if conn.get_nested_transaction() is not None:
        return False
    conn.exec_driver_sql("ROLLBACK")
    return True


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
    2. **The find-then-create race.** SQLite deferred transactions take no write
       lock at the SELECT, so two callers can both miss. The INSERT runs inside a
       SAVEPOINT, and the loser learns it lost in one of TWO ways depending on when
       the winner committed: an ``IntegrityError`` from the UNIQUE index (winner
       committed before this transaction's read snapshot) or ``SQLITE_BUSY_SNAPSHOT``
       (winner committed after it, so the transaction cannot become a writer at all —
       see ``lost_to_stale_snapshot``). Both mean "someone else won" — re-read.
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

    # TWO ways to lose this INSERT, and only one of them was handled. Both are the
    # same race seen at different instants, so both end in the same re-read.
    for attempt in range(_INSERT_ATTEMPTS):
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
            # The winner committed BEFORE this transaction took its read snapshot, so
            # the INSERT reached the UNIQUE (scope_id, session_anchor) index and was
            # rejected by it. The SAVEPOINT rolled back, so the caller's transaction is
            # intact and the winner's row is readable.
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
        except OperationalError as exc:
            # The winner committed AFTER this transaction took its read snapshot, which
            # under WAL is a different error entirely: the INSERT never reaches the
            # index, because the transaction cannot be upgraded to a writer at all.
            # ``SAVEPOINT`` opens the transaction and ``new_session_id``'s scan (or the
            # workdir snapshot) pins the snapshot inside it, so a first-turn caller that
            # loses by microseconds got ``SQLITE_BUSY_SNAPSHOT`` -- a plain
            # ``OperationalError``, which the ``IntegrityError`` catch above does not
            # cover -- and every shared caller surfaced "database is locked" out of a
            # function whose contract is "resolve the one session row for this anchor".
            if not lost_to_stale_snapshot(exc) or not _abandon_stale_read_snapshot(conn):
                raise
            # The snapshot is gone, so this read finally SEES what committed.
            existing = _row_for_scope_anchor(conn, scope_id=scope_id, session_anchor=anchor)
            if existing is not None:
                # Someone took this anchor: join them exactly as the IntegrityError path
                # does. Not a fresh row -- that is the duplicate this whole function
                # exists to prevent, and the UNIQUE index would refuse it anyway.
                return _claim_anchor_row(
                    conn,
                    existing,
                    scope_id=scope_id,
                    session_anchor=anchor,
                    backend=backend,
                    create_kwargs=create_kwargs,
                )
            # NOBODY took the anchor: the commit this transaction lost to was some
            # unrelated writer (a message, a run row), so there is no winner to join and
            # this caller is still the only claimant. Retry the INSERT once, now that the
            # snapshot has been reset -- deliberately bounded, and NOT a retry over
            # ``OperationalError`` at large: only this one extended code, only while
            # nothing holds the anchor.
            logger.warning(
                "Retrying the first-turn session insert for anchor %r after a stale read "
                "snapshot (attempt %d/%d)",
                anchor,
                attempt + 1,
                _INSERT_ATTEMPTS,
            )
    # Every attempt lost its snapshot to a writer that did not take the anchor. Answer
    # "no usable session" rather than raising: the contract already has that answer and
    # every caller degrades on it, whereas the exception would surface in inbound
    # message handling -- which is the defect, not the remedy.
    logger.warning(
        "Gave up creating the first session row for anchor %r after %d stale read "
        "snapshots; the turn runs without a persisted session",
        anchor,
        _INSERT_ATTEMPTS,
    )
    return None, False


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
    # INSERT. That -- not a preceding read -- is why the create needs no SAVEPOINT
    # re-read of its own here.
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

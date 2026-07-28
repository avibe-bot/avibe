"""Reclaim the definitions bound to a Session that is going away.

Two teardown paths, one contract. ``archive_session`` already reclaimed —
it vacates the anchor and soft-deletes bound ``run_definitions`` — while the IM
``/new`` path hard-deleted the session rows and reclaimed nothing, so a
``create_once`` task pinned to that session fired and failed forever with nobody
told. This module owns the shared half so a new teardown path cannot forget it.

The two callers need OPPOSITE outcomes, so ``mode`` is required and has no
default:

- ``delete`` — archive is terminal. A paused definition could be re-enabled later
  and would then target a dead session, so it is soft-deleted.
- ``pause`` — ``/new`` is an everyday command (D2). ``enabled=0`` keeps the
  definition recoverable; soft-deleting a user's tasks on ``/new`` is the
  regression this split exists to prevent.

Reclaim is also the only code path that still sees BOTH rows, so it snapshots the
session settings that live nowhere else. ``run_definitions`` stores ``agent_name``
and ``cwd`` but no ``model``/``reasoning_effort``; without the snapshot a later
``create_once`` rebind re-resolves the current Agent and silently changes the
task's model (D3). Anywhere later is too late.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Literal

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from storage.models import agent_sessions, run_definitions

logger = logging.getLogger(__name__)

RECLAIM_DELETE = "delete"
RECLAIM_PAUSE = "pause"
ReclaimMode = Literal["delete", "pause"]

#: Durable definition-metadata key holding the settings of the session that was
#: reclaimed. Read by the ``create_once`` rebind so it can carry the previous
#: workdir / agent / model forward instead of resetting to scope defaults.
SESSION_SETTINGS_SNAPSHOT_KEY = "session_settings_snapshot"

#: Durable SESSION-metadata key listing the settings this session pins
#: EXPLICITLY, so a stored NULL can be told apart from an absent value.
#:
#: ``agent_sessions.model IS NULL`` already means "inherit whatever the Agent
#: resolves to at dispatch time" for every session ever created, and dispatch
#: implements that with ``or vibe_agent.model``. A preserved ``create_once``
#: rebind needs the opposite: the session it replaces pinned NOTHING, and D3
#: says keep it that way even if the Agent has since gained a default. Those two
#: cannot be the same NULL, and REINTERPRETING the global NULL would silently
#: change the routing of every existing session -- so the distinction is carried
#: as an explicit presence marker on the sessions that need it, and every row
#: without the marker keeps today's inherit semantics untouched.
#:
#: Value is the list of column names that are explicit, e.g.
#: ``["model", "reasoning_effort"]``.
SESSION_SETTINGS_OVERRIDE_KEY = "explicit_setting_overrides"

#: The only columns the override marker can name. Kept next to the key so a
#: writer that adds a third pinnable column has one place to look.
OVERRIDABLE_SETTING_COLUMNS = ("model", "reasoning_effort")

_SNAPSHOT_COLUMNS = (
    "scope_id",
    "agent_backend",
    "agent_variant",
    "agent_id",
    "agent_name",
    "model",
    "reasoning_effort",
    "workdir",
    "session_anchor",
    "visibility",
)

_DEFAULT_PAUSE_REASON = "the bound agent session was cleared"

# Ambient teardown context. The ``/new`` path reaches ``delete_agent_sessions``
# through every registered backend's ``clear_sessions``, so threading a reason and
# a result count through four backend adapters would push transport-level detail
# into the storage signature. The context is set once by the command handler and
# read here; an explicit ``reason=`` argument always wins over it.
_teardown_reason: ContextVar[str | None] = ContextVar("_avibe_teardown_reason", default=None)
_teardown_ledger: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "_avibe_teardown_ledger", default=None
)


@contextmanager
def session_teardown_context(*, reason: str | None = None) -> Iterator[list[dict[str, Any]]]:
    """Name the cause of a session teardown and collect what it reclaimed.

    Yields the ledger, one entry per definition whose state actually changed, so a
    caller can report "N tasks paused" without every layer in between growing a
    return value.
    """

    entries: list[dict[str, Any]] = []
    reason_token = _teardown_reason.set(reason)
    ledger_token = _teardown_ledger.set(entries)
    try:
        yield entries
    finally:
        _teardown_ledger.reset(ledger_token)
        _teardown_reason.reset(reason_token)


def current_reclaim_ledger() -> list[dict[str, Any]] | None:
    """The active teardown ledger, or ``None`` when nothing is collecting."""

    return _teardown_ledger.get()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def explicit_override_names(metadata: Any) -> set[str]:
    """The setting names a session pins EXPLICITLY, per its metadata marker.

    One parser for every reader, and it never raises: the marker is user-visible
    JSON on a row that predates it, so a malformed or absent value must read as
    "this session pins nothing" rather than break the turn that consulted it.
    """

    if not isinstance(metadata, dict):
        return set()
    marked = metadata.get(SESSION_SETTINGS_OVERRIDE_KEY)
    if isinstance(marked, str):
        # Tolerate a hand-edited scalar; treat it as a one-element list.
        return {marked} if marked.strip() else set()
    if isinstance(marked, (list, tuple, set)):
        return {str(name) for name in marked if str(name).strip()}
    return set()


def reconcile_explicit_overrides(
    metadata: Any,
    *,
    cleared: Iterable[str] = (),
    explicit: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a metadata dict whose override marker matches the columns just written.

    THE INVARIANT: the marker is a claim about what the ROW currently pins, so
    every writer of ``agent_sessions.model`` / ``.reasoning_effort`` must
    reconcile it in the same statement. A writer that resets or replaces one of
    those columns and leaves the marker behind keeps forcing dispatch to honour
    a value the writer just changed -- the Workbench "Default" action clears the
    model, the stale marker still says "this session pins NULL on purpose", and
    dispatch keeps sending NULL instead of the Agent's default. The control
    inverts: the user's edit is stored and displayed but never routed.

    ``cleared`` names the settings this write reset or replaced (their marker
    entries are dropped); ``explicit`` names the settings the writer is pinning
    on purpose (their entries are added). Returns a NEW dict -- callers compose
    it with their own metadata edits -- and removes the key entirely when no
    names are left, so a row that pins nothing looks exactly like every row
    created before the marker existed. ``metadata=None`` and a malformed marker
    value are both tolerated (see ``explicit_override_names``).
    """

    result: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    cleared_names = {str(name) for name in cleared}
    names = [name for name in sorted(explicit_override_names(result)) if name not in cleared_names]
    for name in explicit:
        text = str(name)
        if text and text not in names:
            names.append(text)
    if names:
        result[SESSION_SETTINGS_OVERRIDE_KEY] = names
    else:
        result.pop(SESSION_SETTINGS_OVERRIDE_KEY, None)
    return result


def session_settings_snapshot(
    conn: Connection,
    session_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Copy the settings that live only on the session row.

    Returns ``None`` when the row is already gone — the caller must run this
    BEFORE the delete, in the same transaction.
    """

    row = (
        conn.execute(
            select(
                agent_sessions.c.id,
                *(agent_sessions.c[name] for name in _SNAPSHOT_COLUMNS),
            ).where(agent_sessions.c.id == str(session_id))
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    snapshot: dict[str, Any] = {"session_id": row["id"], "captured_at": _utc_now_iso()}
    for name in _SNAPSHOT_COLUMNS:
        snapshot[name] = row[name]
    if reason:
        snapshot["reason"] = reason
    return snapshot


def reclaim_bound_definitions(
    conn: Connection,
    session_id: str,
    *,
    mode: ReclaimMode,
    reason: str | None = None,
) -> dict[str, int]:
    """Detach scheduled tasks / watches from a session that is going away.

    ``mode`` is required: ``delete`` soft-deletes (terminal teardown), ``pause``
    sets ``enabled=0`` and records ``last_error`` (recoverable teardown). Runs in
    the caller's transaction, before the session row is removed.

    Each definition is reclaimed by a COMPARE-AND-SET on the binding this call was
    decided from, and the returned counters plus the teardown ledger count only the
    writes that actually LANDED — a definition repointed to another session inside
    the window is left alone and reported to nobody.
    """

    if mode not in (RECLAIM_DELETE, RECLAIM_PAUSE):
        raise ValueError(f"invalid reclaim mode: {mode!r}")

    summary = {"paused": 0, "deleted": 0, "snapshotted": 0}
    sid = str(session_id or "").strip()
    if not sid:
        return summary

    rows = (
        conn.execute(
            select(
                run_definitions.c.id,
                run_definitions.c.definition_type,
                run_definitions.c.enabled,
                run_definitions.c.metadata_json,
            )
            .where(run_definitions.c.session_id == sid)
            .where(run_definitions.c.deleted_at.is_(None))
        )
        .mappings()
        .all()
    )
    if not rows:
        return summary

    effective_reason = reason or _teardown_reason.get() or _DEFAULT_PAUSE_REASON
    snapshot = session_settings_snapshot(conn, sid, reason=effective_reason)
    ledger = _teardown_ledger.get()
    now = _utc_now_iso()

    for row in rows:
        values: dict[str, Any] = {"updated_at": now}
        counters: list[str] = []
        if snapshot is not None:
            metadata = _json_object(row["metadata_json"])
            metadata[SESSION_SETTINGS_SNAPSHOT_KEY] = snapshot
            values["metadata_json"] = _dump_json(metadata)
            counters.append("snapshotted")
        changed = False
        if mode == RECLAIM_DELETE:
            values["deleted_at"] = now
            counters.append("deleted")
            changed = True
        elif row["enabled"]:
            # Already-paused definitions keep the fresh snapshot but must not have
            # an unrelated pause reason restamped over their own.
            values["enabled"] = 0
            values["last_error"] = effective_reason
            counters.append("paused")
            changed = True
        # The TWO predicates below RE-ASSERT what the SELECT above decided, because
        # that read reserves nothing. pysqlite emits no ``BEGIN`` for a bare SELECT, so
        # the write lock is taken at the first DML: on the hard-delete path
        # (``_delete_agent_session_rows``) the reclaim helper is reached after only
        # reads -- a resolved two-part scope key never upserts -- so a second
        # connection can commit between the read and this statement.
        # ``archive_session`` writes first and is serialised, but the shared helper
        # cannot depend on which caller it is under.
        #
        # WHAT A BARE ``id`` MATCH LOSES: the user repoints this task / watch to
        # another Session (``upsert_scheduled_task``, a full-row UPDATE) or deletes it
        # inside the window. This statement then pauses or soft-deletes a definition
        # that is now bound to a DIFFERENT, live session, and overwrites its
        # ``session_settings_snapshot`` with the settings of the session it no longer
        # belongs to -- so a later ``create_once`` rebind carries the wrong model
        # forward. Both re-asserted: the binding this reclaim was decided for, and the
        # live ``deleted_at`` state.
        reclaimed = conn.execute(
            update(run_definitions)
            .where(run_definitions.c.id == row["id"])
            # Still bound to the session that is going away.
            .where(run_definitions.c.session_id == sid)
            # Still live: a definition deleted inside the window stays deleted, and its
            # ``deleted_at`` must not be restamped with this teardown's clock.
            .where(run_definitions.c.deleted_at.is_(None))
            .values(**values)
        )
        if not reclaimed.rowcount:
            # LOST the race, so this reclaim did NOT happen and must not be reported as
            # though it had. The accounting is part of the guard: ``summary`` is what
            # the archive confirm dialog reports, and the ledger is what ``/new`` counts
            # in its reply ("N tasks paused"). Crediting a write that never landed tells
            # the user a task was paused while it keeps firing on its new session -- the
            # same class of silent lie as the write itself.
            logger.warning(
                "Skipped reclaiming definition %s for session %s mode=%s: it was "
                "repointed or deleted concurrently",
                row["id"],
                sid,
                mode,
            )
            continue
        for name in counters:
            summary[name] += 1
        if changed and ledger is not None:
            ledger.append(
                {
                    "definition_id": row["id"],
                    "definition_type": row["definition_type"],
                    "mode": mode,
                    "session_id": sid,
                    "reason": effective_reason,
                }
            )

    if summary["paused"] or summary["deleted"]:
        logger.info(
            "Reclaimed definitions bound to session %s mode=%s paused=%d deleted=%d",
            sid,
            mode,
            summary["paused"],
            summary["deleted"],
        )
    return summary

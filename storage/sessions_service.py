from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import Connection, and_, case, func, literal_column, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from config import paths
from config.v2_config import V2Config
from config.v2_sessions import ActivePollInfo, SessionState
from config.v2_settings import _split_scoped_key
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.agent_session_rows import (
    SUPERSEDED_ANCHOR_INFIX,
    create_agent_session_row,
    decode_session_value,
    encode_session_value,
    get_or_create_agent_session_row,
    new_session_id,
    normalize_workdir,
    reserve_write_lock,
    snapshot_scope_workdir,
)
from storage.models import (
    agents,
    agent_runs,
    agent_sessions,
    message_deliveries,
    messages,
    metadata,
    run_definitions,
    runtime_records,
    scopes,
    state_meta,
)
from storage.session_reclaim import (
    OVERRIDABLE_SETTING_COLUMNS,
    RECLAIM_PAUSE,
    ReclaimMode,
    explicit_override_names,
    reclaim_bound_definitions,
    reclaim_ledger_transaction,
    reconcile_explicit_overrides,
    retire_session_delivery_owners,
)
from storage.settings_service import make_scope_id, upsert_scope
from storage import message_deliveries as delivery_store


_RUNTIME_RECORD_ROWID = literal_column("runtime_records.rowid")

SESSIONS_LAST_ACTIVITY_KEY = "sessions_last_activity"
SESSION_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

logger = logging.getLogger(__name__)


def _publish_definition_reclaim_hint() -> None:
    """Wake both definition owners after a committed Session teardown."""

    try:
        from core.inbox_events import publish_definitions_updated

        publish_definitions_updated(definition_type="scheduled")
        publish_definitions_updated(definition_type="watch")
    except Exception:
        logger.debug("session definition reclaim wake failed", exc_info=True)


def _require_enabled_agent_identity(
    conn: Connection,
    *,
    agent_id: str | None,
    agent_name: str | None,
) -> None:
    cleaned_id = str(agent_id or "").strip()
    cleaned_name = str(agent_name or "").strip()
    if not cleaned_id or not cleaned_name:
        raise ValueError("an enabled Agent identity is required for this session")
    row = conn.execute(
        select(agents.c.id)
        .where(agents.c.id == cleaned_id)
        .where(agents.c.name == cleaned_name)
        .where(agents.c.enabled == 1)
        .where(agents.c.archived_at.is_(None))
        .limit(1)
    ).first()
    if row is None:
        raise ValueError(
            f"agent '{cleaned_name}' was archived, disabled, renamed, or replaced before session creation"
        )


def _require_agent_reference_identity(
    conn: Connection,
    *,
    expected_agent_id: str | None,
) -> dict[str, str]:
    cleaned_id = str(expected_agent_id or "").strip()
    if not cleaned_id:
        raise ValueError("an Agent identity is required for this session reference")
    row = conn.execute(
        select(
            agents.c.id,
            agents.c.name,
            agents.c.backend,
            agents.c.enabled,
            agents.c.archived_at,
            agents.c.metadata_json,
        )
        .where(agents.c.id == cleaned_id)
        .limit(1)
    ).mappings().first()
    if row is None:
        raise ValueError(f"agent reference '{cleaned_id}' no longer exists")
    from core.vibe_agents import agent_reference_is_usable

    if not agent_reference_is_usable(
        enabled=bool(row["enabled"]),
        archived_at=row["archived_at"],
        metadata=_json_loads(row["metadata_json"], {}),
    ):
        raise ValueError(f"agent reference '{row['name']}' is disabled")
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "backend": str(row["backend"]),
    }


def _catalog_agent_name_value(agent_id: str | None, fallback_name: str) -> Any:
    """Resolve an Agent's current routing name in the statement that persists it."""

    cleaned_id = str(agent_id or "").strip()
    if not cleaned_id:
        return fallback_name
    return func.coalesce(
        select(agents.c.name).where(agents.c.id == cleaned_id).limit(1).scalar_subquery(),
        fallback_name,
    )


def _set_native_once(conn: Connection, row_id: str, encoded_session_id: str) -> bool:
    """Return True iff a row's ``native_session_id`` should be written now.

    Enforces the write-once invariant: a native session id is bound exactly once
    and never changed. Returns True only when the row currently has no native
    (first bind). If a DIFFERENT native is already stored, keep it and log the
    ignored attempt; if the SAME value is already stored, no rewrite is needed.
    No fallback / fork / subagent / recapture flow may overwrite a stored native.
    """
    current = conn.execute(
        select(agent_sessions.c.native_session_id).where(agent_sessions.c.id == row_id)
    ).scalar_one_or_none()
    current_str = str(current or "")
    if not current_str:
        return True
    if current_str != str(encoded_session_id):
        logger.warning(
            "WRITE-ONCE: native_session_id for session %s is already set; ignoring attempt to change it",
            row_id,
        )
    return False


_BACKEND_LABELS = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}


def session_agent_display_label(row: Mapping[str, Any]) -> str | None:
    agent_name = str(row["agent_name"] or "").strip()
    catalog_name = str(row["catalog_agent_name"] or "").strip()
    if row["catalog_agent_archived_at"]:
        try:
            metadata_json = json.loads(row["catalog_agent_metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata_json = {}
        archive = metadata_json.get("_avibe_archive") if isinstance(metadata_json, dict) else None
        if isinstance(archive, dict):
            original_name = str(archive.get("original_name") or "").strip()
            if original_name:
                return original_name
    backend = str(row["agent_backend"] or "").strip()
    return catalog_name or agent_name or _BACKEND_LABELS.get(backend, backend or None)


def read_session_display_meta(
    session_ids: list[str], *, db_path: Path | None = None
) -> dict[str, dict[str, str | None]]:
    """Map session id -> display metadata for Show Page rows.

    Returns ``{id: {"title", "platform", "agent"}}``. ``title`` is the user-set
    ``agent_sessions.title`` (``None`` for IM-dispatch sessions, which always
    persist ``title=None`` — the UI falls back to the session id). ``agent``
    falls back to a friendly backend label when no explicit agent name is set.
    """
    ids = [str(value) for value in session_ids if str(value or "").strip()]
    if not ids:
        return {}
    engine = create_sqlite_engine(db_path or paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    select(
                        agent_sessions.c.id,
                        agent_sessions.c.title,
                        agent_sessions.c.agent_name,
                        agent_sessions.c.agent_backend,
                        agents.c.name.label("catalog_agent_name"),
                        agents.c.archived_at.label("catalog_agent_archived_at"),
                        agents.c.metadata_json.label("catalog_agent_metadata_json"),
                        scopes.c.platform,
                    )
                    .select_from(
                        agent_sessions
                        .join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
                        .join(
                            agents,
                            or_(
                                agents.c.id == agent_sessions.c.agent_id,
                                and_(
                                    agent_sessions.c.agent_id.is_(None),
                                    agents.c.name == agent_sessions.c.agent_name,
                                ),
                            ),
                            isouter=True,
                        )
                    )
                    .where(agent_sessions.c.id.in_(ids))
                )
                .mappings()
                .all()
            )
    finally:
        engine.dispose()
    meta: dict[str, dict[str, str | None]] = {}
    for row in rows:
        title = str(row["title"] or "").strip() or None
        platform = str(row["platform"] or "").strip() or None
        agent = session_agent_display_label(row)
        meta[str(row["id"])] = {"title": title, "platform": platform, "agent": agent}
    return meta


#: ``classify_reserved_agent_session`` verdicts (HFR-279). Plain strings rather than an
#: Enum because the retry bookkeeping in ``core.scheduled_tasks`` logs them verbatim and
#: an operator reading those lines should see the fact, not a repr.
RESERVATION_ABSENT = "absent"
RESERVATION_ADOPTED = "adopted"
RESERVATION_RESERVED = "reserved"

#: Session-metadata key naming the harness definition whose recovery reserved the row
#: (HFR-276). Written INSIDE the reservation's own transaction, so the durable handle
#: exists exactly when the reservation does: a fault that later refuses both the release
#: and the ``orphaned_reservations`` record cannot leave a row this key does not name.
#: The key is scoped to the create_once recovery path on purpose -- a create_per_run
#: reservation is legitimately unbound and unreferenced between its reserve and its
#: dispatch, so a sweep keyed on this stamp must never be able to see one.
RESERVED_BY_DEFINITION_METADATA_KEY = "reserved_by_harness_definition"


class SQLiteSessionsService:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.engine = create_sqlite_engine(db_path)
        metadata.create_all(self.engine)
        self._probe = SqliteInvalidationProbe(self.engine)

    def close(self) -> None:
        self._probe.close()
        self.engine.dispose()

    def has_external_write(self) -> bool:
        return self._probe.has_external_write()

    def get_agent_session_row_id(
        self,
        *,
        scope_key: str,
        agent_name: str,
        session_anchor: str,
    ) -> str | None:
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=_utc_now_iso())
            if scope_id is None:
                return None
            return conn.execute(
                select(agent_sessions.c.id)
                .where(agent_sessions.c.scope_id == scope_id)
                .where(agent_sessions.c.agent_variant == (str(agent_name) or "default"))
                .where(agent_sessions.c.session_anchor == str(session_anchor))
                .limit(1)
            ).scalar_one_or_none()

    def get_agent_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == str(session_id)).limit(1)
            ).mappings().first()
            return dict(row) if row else None

    def get_agent_session_runtime_marker(
        self,
        session_id: str,
        *,
        backend: str,
        native_session_id: Any,
        key: str,
    ) -> Any:
        """Read one backend marker only from the exact active native binding."""

        marker_key = str(key or "").strip()
        if not marker_key:
            raise ValueError("runtime marker key is required")
        expected_native = encode_session_value(native_session_id)
        with self.engine.connect() as conn:
            raw_metadata = conn.execute(
                select(agent_sessions.c.metadata_json)
                .where(agent_sessions.c.id == str(session_id))
                .where(agent_sessions.c.status != "archived")
                .where(agent_sessions.c.agent_backend == str(backend))
                .where(agent_sessions.c.native_session_id == expected_native)
                .limit(1)
            ).scalar_one_or_none()
        metadata_value = _json_loads(raw_metadata, {})
        if not isinstance(metadata_value, dict):
            return None
        return metadata_value.get(marker_key)

    def set_agent_session_runtime_marker(
        self,
        session_id: str,
        *,
        backend: str,
        native_session_id: Any,
        key: str,
        value: Any,
    ) -> bool:
        """Merge one backend marker into the exact active native binding.

        The writer reservation makes the read/merge/write atomic, so unrelated
        Session metadata cannot be lost and a replaced native binding cannot
        inherit state that belonged to its predecessor.
        """

        marker_key = str(key or "").strip()
        if not marker_key:
            raise ValueError("runtime marker key is required")
        expected_native = encode_session_value(native_session_id)
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(
                    agent_sessions.c.metadata_json,
                    agent_sessions.c.status,
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.native_session_id,
                )
                .where(agent_sessions.c.id == str(session_id))
                .limit(1)
            ).mappings().first()
            if (
                row is None
                or str(row["status"] or "") == "archived"
                or str(row["agent_backend"] or "") != str(backend)
                or str(row["native_session_id"] or "") != expected_native
            ):
                return False
            metadata_value = _json_loads(row["metadata_json"], {})
            metadata_object = dict(metadata_value) if isinstance(metadata_value, dict) else {}
            if metadata_object.get(marker_key) == value:
                return True
            metadata_object[marker_key] = value
            result = conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == str(session_id))
                .where(agent_sessions.c.status != "archived")
                .where(agent_sessions.c.agent_backend == str(backend))
                .where(agent_sessions.c.native_session_id == expected_native)
                .values(
                    metadata_json=_json_dumps(metadata_object),
                    updated_at=_utc_now_iso(),
                )
            )
            return bool(result.rowcount)

    def reserve_agent_session(
        self,
        *,
        scope_key: str,
        agent_backend: str,
        session_anchor: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        workdir: str | None = None,
        visibility: str = "foreground",
        metadata: dict[str, Any] | None = None,
        require_enabled_agent: bool = False,
        expected_reference_agent_id: str | None = None,
    ) -> str | None:
        now = _utc_now_iso()
        backend = str(agent_backend or "default")
        with self.engine.begin() as conn:
            if require_enabled_agent:
                reserve_write_lock(conn)
                _require_enabled_agent_identity(
                    conn,
                    agent_id=agent_id,
                    agent_name=agent_name,
                )
            elif expected_reference_agent_id is not None:
                reserve_write_lock(conn)
                identity = _require_agent_reference_identity(
                    conn,
                    expected_agent_id=expected_reference_agent_id,
                )
                agent_id = identity["id"]
                agent_name = identity["name"]
                backend = identity["backend"]
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is None:
                return None
            return create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend=_agent_backend(backend),
                agent_variant=backend,
                session_anchor=session_anchor,
                native_session_id="",
                agent_id=agent_id,
                agent_name=agent_name,
                model=model,
                reasoning_effort=reasoning_effort,
                workdir=_new_session_workdir(conn, scope_id, workdir),
                visibility=visibility,
                metadata={"legacy_scope_key": str(scope_key), **dict(metadata or {})},
                now=now,
                require_workdir=False,
            )

    def reserve_standalone_agent_session(
        self,
        *,
        agent_backend: str,
        session_anchor: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        workdir: str | None = None,
        visibility: str = "background",
        metadata: dict[str, Any] | None = None,
        require_enabled_agent: bool = False,
        expected_reference_agent_id: str | None = None,
    ) -> str:
        """Reserve a session with no Scope and its own lazy Show workspace."""
        now = _utc_now_iso()
        backend = str(agent_backend or "default")
        with self.engine.begin() as conn:
            if require_enabled_agent:
                reserve_write_lock(conn)
                _require_enabled_agent_identity(
                    conn,
                    agent_id=agent_id,
                    agent_name=agent_name,
                )
            elif expected_reference_agent_id is not None:
                reserve_write_lock(conn)
                identity = _require_agent_reference_identity(
                    conn,
                    expected_agent_id=expected_reference_agent_id,
                )
                agent_id = identity["id"]
                agent_name = identity["name"]
                backend = identity["backend"]
            session_id = new_session_id(conn)
            resolved_workdir = normalize_workdir(workdir)
            if resolved_workdir is None:
                resolved_workdir = str(paths.get_show_page_dir(session_id))
            Path(resolved_workdir).mkdir(parents=True, exist_ok=True)
            return create_agent_session_row(
                conn,
                session_id=session_id,
                scope_id=None,
                agent_backend=_agent_backend(backend),
                agent_variant=backend,
                session_anchor=session_anchor,
                native_session_id="",
                agent_id=agent_id,
                agent_name=agent_name,
                model=model,
                reasoning_effort=reasoning_effort,
                workdir=resolved_workdir,
                visibility=visibility,
                metadata=dict(metadata or {}),
                now=now,
            )

    def release_reserved_agent_session(self, session_id: str, *, reason: str) -> bool:
        """Give back a session this process reserved and then could not use.

        The inverse of the two ``reserve_*`` entry points above, and deliberately the
        NARROWEST thing that undoes them: it names exactly ONE row, by the id the
        reservation returned, and it removes a workspace only when that workspace is the
        Show Page directory the standalone reservation mkdir'd for that same id. A
        scoped reservation inherits the Scope's workdir, which is shared with every other
        session in that Scope and is never this call's to delete.

        A reservation is committed BEFORE the caller can know whether it will be able to
        use it (the guarded write that adopts it is a different transaction, in a
        different store), so "reserve, lose the race, give it back" is a real sequence
        and not an error path. Returns ``True`` only when this call removed the row.

        TWO PREDICATES, and they are re-asserted BY the delete (``_delete_agent_session_rows``
        re-runs the id query with the write lock held), so they hold at the instant of the
        delete rather than at the instant they were read:

        * ``native_session_id`` is still empty -- nothing was ever dispatched into it. A
          bound row has a transcript and is not a reservation any more.
        * no ``run_definitions`` row points at it -- if a definition adopted it after all,
          it is somebody's live binding and deleting it would recreate the dangling
          pointer the whole reclaim machinery exists to prevent.

        Neither predicate is what keeps a CONCURRENT WINNER safe; the id does that. They
        are there so that a caller which is WRONG about having lost the race destroys
        nothing.

        AND THE DECISION IS TAKEN UNDER THE WRITE LOCK (HFR-278). Re-asserting the
        predicates in the DELETE was enough to keep the winner's ROW, and not enough to
        keep the winner: ``_delete_agent_session_rows`` runs the id query first and calls
        ``reclaim_bound_definitions`` second, and the id read reserves nothing (pysqlite
        opens no transaction for a bare SELECT). An adoption committing between the two
        left the reclaim looking at the WINNER's definition -- so it paused it, stamped
        its ``last_error`` and overwrote its settings snapshot -- and only then did the
        DELETE re-evaluate ``NOT EXISTS`` and correctly preserve the session. The row
        survived; the definition that had just adopted it did not, and its reclaim is
        deliberately never rolled back. ``reserve_write_lock`` removes the window instead
        of detecting it: taken here, at the top of the transaction and BEFORE the read
        the decision rests on, so no adoption can land between the read and the reclaim.
        Nothing has been read yet on this connection, so this is the cheap
        ``BEGIN IMMEDIATE`` spelling, which takes the write lock and the read snapshot in
        one statement.
        """

        row = self.get_agent_session_by_id(str(session_id))
        if row is None:
            return False
        from storage.background import run_update_event_transaction

        with reclaim_ledger_transaction(), run_update_event_transaction(
            self.engine
        ) as conn:
            reserve_write_lock(conn)
            deleted = _delete_agent_session_rows(
                conn,
                select(agent_sessions.c.id)
                .where(agent_sessions.c.id == str(session_id))
                .where(
                    or_(
                        agent_sessions.c.native_session_id.is_(None),
                        agent_sessions.c.native_session_id == "",
                    )
                )
                .where(
                    ~select(run_definitions.c.id)
                    .where(run_definitions.c.session_id == str(session_id))
                    .exists()
                ),
                # Nothing may be bound to a row that satisfies the predicates above, so
                # the reclaim is a no-op by construction. ``RECLAIM_PAUSE`` is the
                # conservative answer if that ever stops being true: pause a definition,
                # never silently unbind one.
                reclaim_mode=RECLAIM_PAUSE,
                reclaim_reason=reason,
            )
        if not deleted:
            logger.warning(
                "Not releasing reserved agent session %s (%s): it was bound or adopted "
                "after it was reserved",
                session_id,
                reason,
            )
            return False
        self._remove_reserved_workspace(str(session_id), row.get("workdir"))
        return True

    def classify_reserved_agent_session(self, session_id: str) -> str:
        """Which of the three reservation facts holds for ``session_id`` (HFR-279).

        ``release_reserved_agent_session`` answers ``False`` for three different facts
        -- the row is gone, the row was adopted, the release faulted -- and the retry
        bookkeeping needs them apart: an absent or adopted row must be taken OFF the
        retry record, a genuine orphan must stay on it. The verdicts:

        * ``RESERVATION_ABSENT`` -- no row. Released by an earlier attempt or deleted
          by its owner; nothing left to retry.
        * ``RESERVATION_ADOPTED`` -- the row is somebody's live binding: a native
          session was dispatched into it, or a ``run_definitions`` row points at it.
          The same two predicates the guarded release re-asserts under the write lock,
          read here WITHOUT that lock -- this probe exists precisely so an adopted
          winner is never made to pay the release's ``BEGIN IMMEDIATE`` again on every
          fire of the loser.
        * ``RESERVATION_RESERVED`` -- the row exists, has no native binding and no
          definition references it: still a reservation, still this caller's to
          release.

        ONE statement on purpose: both facts come from the same read snapshot, so the
        answer is a state the row actually was in, never half of one state and half of
        another. A read fault propagates -- the caller must keep the entry rather than
        guess.
        """

        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    agent_sessions.c.native_session_id,
                    select(run_definitions.c.id)
                    .where(run_definitions.c.session_id == str(session_id))
                    .exists()
                    .label("definition_referenced"),
                )
                .where(agent_sessions.c.id == str(session_id))
                .limit(1)
            ).first()
        if row is None:
            return RESERVATION_ABSENT
        if str(row[0] or "") or bool(row[1]):
            return RESERVATION_ADOPTED
        return RESERVATION_RESERVED

    def list_reserved_agent_sessions_for_definition(self, definition_id: str) -> list[str]:
        """The still-unadopted reservations stamped with ``definition_id`` (HFR-276).

        The durable side of the orphan retry: ``metadata.orphaned_reservations`` on the
        definition is written AFTER a release fails, through the same database, so the
        fault that refused the release can refuse the record too and the id -- random,
        known to nothing else -- was lost. The stamp is different: it is written inside
        the reservation's own INSERT transaction, so if the reservation committed, the
        handle committed with it, and a later fire recovers the id from the row itself.

        The filters are the classification facts, in the same single statement: only
        rows that are still empty-native AND unreferenced are returned, so an adopted
        winner is invisible here by construction. The guarded release re-asserts both
        under the write lock anyway; this listing only decides who is WORTH a release
        attempt.
        """

        stamped = func.json_extract(
            agent_sessions.c.metadata_json,
            f'$."{RESERVED_BY_DEFINITION_METADATA_KEY}"',
        )
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(agent_sessions.c.id)
                .where(stamped == str(definition_id))
                .where(
                    or_(
                        agent_sessions.c.native_session_id.is_(None),
                        agent_sessions.c.native_session_id == "",
                    )
                )
                .where(
                    ~select(run_definitions.c.id)
                    .where(run_definitions.c.session_id == agent_sessions.c.id)
                    .exists()
                )
                .order_by(agent_sessions.c.id)
            ).all()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _remove_reserved_workspace(session_id: str, workdir: Any) -> None:
        """Remove the Show Page workspace a standalone reservation created, if any.

        Ownership is decided by identity, not by emptiness: only ``show/<session_id>``
        belongs to this session, and only this session can have created it. A workdir
        that is anything else -- a Scope's shared directory, a user-supplied path -- is
        left alone. ``rmdir`` rather than ``rmtree`` for the same reason: a released
        reservation never ran, so its workspace is empty, and a non-empty one means
        something happened in there that this call is not entitled to destroy.
        """

        if not workdir or str(workdir) != str(paths.get_show_page_dir(session_id)):
            return
        path = Path(str(workdir))
        try:
            path.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "Left the workspace %s of released session %s in place: %s",
                path,
                session_id,
                exc,
            )

    def ensure_agent_session_id(
        self,
        *,
        scope_key: str,
        agent_name: str,
        session_anchor: str,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
    ) -> str | None:
        """Ensure a Vibe-owned agent-session row exists before native binding."""
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is None:
                return None
            row_id = _find_agent_session_row_id(
                conn,
                scope_id=scope_id,
                agent_name=agent_name,
                session_anchor=session_anchor,
            )
            if row_id:
                return row_id
            # Route the create through the CONSTRAINT key: the finder above filters
            # on backend, the unique index does not, so a same-anchor row owned by
            # another backend is invisible here and fatal to a bare INSERT.
            #
            # ``session_id`` is ``None`` when that resolve found NO usable session --
            # the anchor-relabel race lost to a writer that archived the row -- and
            # ``None`` is exactly what this function must then return: an archive is
            # terminal, and ``BaseAgent.ensure_agent_session_id`` pins any non-empty
            # answer into the turn's context without ever re-resolving it.
            session_id, _created = get_or_create_agent_session_row(
                conn,
                scope_id=scope_id,
                agent_backend=_agent_backend(str(agent_name)),
                agent_variant=str(agent_name) or "default",
                session_anchor=session_anchor,
                native_session_id="",
                workdir=workdir,
                agent_id=vibe_agent_id,
                agent_name=vibe_agent_name,
                model=None,
                reasoning_effort=None,
                metadata={"legacy_scope_key": str(scope_key)},
                now=now,
                require_workdir=False,
            )
            return session_id

    def bind_agent_session(
        self,
        *,
        scope_key: str,
        agent_name: str,
        session_anchor: str,
        native_session_id: Any,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
        workdir: str | None = None,
    ) -> str | None:
        """Bind a backend-native session id to the stable Vibe session row."""
        now = _utc_now_iso()
        persisted_agent_name = (
            _catalog_agent_name_value(vibe_agent_id, vibe_agent_name)
            if vibe_agent_name is not None
            else None
        )
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is None:
                return None
            row_id = _find_agent_session_row_id(
                conn,
                scope_id=scope_id,
                agent_name=agent_name,
                session_anchor=session_anchor,
            )
            encoded_session_id = encode_session_value(native_session_id)
            requested_workdir = str(workdir) if workdir is not None else None
            if not row_id:
                # Same reason as ``ensure_agent_session_id``: the finder's key is
                # narrower than the unique index, and the SELECT above took no write
                # lock. When this resolves to an existing row it falls through to the
                # write-once update below rather than stealing the native id.
                row_id, created = get_or_create_agent_session_row(
                    conn,
                    scope_id=scope_id,
                    agent_backend=_agent_backend(str(agent_name)),
                    agent_variant=str(agent_name) or "default",
                    session_anchor=session_anchor,
                    native_session_id=encoded_session_id,
                    workdir=requested_workdir,
                    agent_id=vibe_agent_id,
                    agent_name=persisted_agent_name,
                    model=None,
                    reasoning_effort=None,
                    metadata={"legacy_scope_key": str(scope_key)},
                    now=now,
                    require_workdir=False,
                )
                if not row_id:
                    # NO usable session: the resolve lost the anchor-relabel race to a
                    # writer that archived the row, and an archive is terminal. ``None``
                    # is the same answer the two lost-race paths further down already
                    # give for an archived winner, and it is also what fell out of the
                    # old code by accident -- every statement below is keyed on
                    # ``row_id``, so they all became ``id IS NULL``, matched nothing and
                    # left the final ``rowcount``-0 return. Accidentally right is not
                    # right: those statements also emit a spurious WRITE-ONCE race
                    # warning naming session ``None``. Decide it here instead.
                    return None
                if created:
                    return row_id
            values = {
                "status": "active",
                "updated_at": now,
                "last_active_at": now,
            }
            if requested_workdir:
                current_workdir = conn.execute(
                    select(agent_sessions.c.workdir).where(agent_sessions.c.id == row_id)
                ).scalar_one_or_none()
                if current_workdir and str(current_workdir) != str(requested_workdir):
                    logger.warning(
                        "Ignoring native bind workdir override; session workdir is authoritative session_id=%s current=%s requested=%s",
                        row_id,
                        current_workdir,
                        requested_workdir,
                    )
            if vibe_agent_id is not None:
                values["agent_id"] = vibe_agent_id
            if vibe_agent_name is not None:
                values["agent_name"] = (
                    case(
                        (agent_sessions.c.agent_id == vibe_agent_id, agent_sessions.c.agent_name),
                        else_=persisted_agent_name,
                    )
                    if vibe_agent_id is not None
                    else persisted_agent_name
                )
            # WRITE-ONCE: a row's native_session_id is bound exactly once and never
            # changed. Never let a recapture, fork, subagent, or any fallback
            # overwrite an existing native (product invariant — one agent session ↔
            # one fixed native).
            #
            # The invariant is carried by the PREDICATE, not by the
            # ``_set_native_once`` read: pysqlite emits no ``BEGIN`` for a bare
            # SELECT, so the write lock is only taken at this UPDATE and another
            # connection can commit a bind — or an archive — in between. A rule
            # enforced by a preceding SELECT is not write-once. ``_set_native_once``
            # still decides INTENT (and logs a differing native); the statement
            # decides whether the write may land. Same shape as the twin
            # ``bind_agent_session_by_id`` (HFR-251/252), which this path was missing.
            if _set_native_once(conn, row_id, encoded_session_id):
                first_bind = conn.execute(
                    agent_sessions.update()
                    .where(agent_sessions.c.id == row_id)
                    .where(agent_sessions.c.status != "archived")
                    .where(func.coalesce(agent_sessions.c.native_session_id, "") == "")
                    .values(**values, native_session_id=encoded_session_id)
                )
                if first_bind.rowcount:
                    return row_id
                # Return the winner immediately. Falling through to the UPDATE below
                # would preserve the winner's native id and then overwrite everything
                # else it owns -- ``values`` carries ``status='active'``, both
                # timestamps, and this caller's ``agent_id`` / ``agent_name`` -- so
                # the row would attribute the winner's conversation to the Agent that
                # LOST the race. The native id was never the only thing the winner
                # owns, and this is the same correction the twin
                # ``bind_agent_session_by_id`` needed: all THREE lost-race paths in
                # this module now answer the same way instead of two of them
                # continuing.
                logger.warning(
                    "WRITE-ONCE: session %s was bound concurrently; keeping the winner's "
                    "native id and Agent identity",
                    row_id,
                )
                winner_status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == row_id)
                ).scalar_one_or_none()
                if winner_status is None or winner_status == "archived":
                    return None
                return row_id
            result = conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == row_id)
                # ``values`` carries ``status='active'``, so without this predicate the
                # bind RESURRECTS a row archived after the lookup above — the lookup
                # (``_find_agent_session_row_id``) filters archived rows but reserves
                # nothing, and the cancel that accompanies an archive is
                # best-effort/background, so a still-finishing turn lands its bind
                # here. Make the write itself the no-op instead.
                .where(agent_sessions.c.status != "archived")
                .values(**values)
            )
            return row_id if result.rowcount else None

    def materialize_agent_session_route(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        expected_route: Mapping[str, Any] | None = None,
    ) -> bool:
        """Pin the resolved Agent identity and route into EMPTY columns.

        A session created on an inherited default carries NULLs (dispatch
        resolves the live Agent default); the first turn pins the resolved Agent
        identity, model, and effort — same lifecycle as the backend pin on native
        bind. Called at dispatch time (turn START). The writer reservation
        serializes the marker read with this write, while ``expected_route`` makes
        a stale turn a no-op if the user has already changed any Agent, model,
        effort, or explicit-pin part of the route.
        COALESCE keeps each setting fill-if-empty. Returns True when a row was
        updated.

        A setting the row pins EXPLICITLY is never filled, even when its column
        is empty: that is the whole point of the explicit-override marker (a
        preserved ``create_once`` rebind pins "no model" on purpose, D3). Filling
        it here would turn the first turn into the thing the rebind was preventing
        -- the Agent's current default becoming the session's pinned model."""
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            pinned = explicit_override_names(
                _json_loads(
                    conn.execute(
                        select(agent_sessions.c.metadata_json).where(
                            agent_sessions.c.id == str(session_id)
                        )
                    ).scalar_one_or_none(),
                    {},
                )
            )
            if expected_route is not None and "explicit_overrides" in expected_route:
                expected_pinned = {
                    str(name)
                    for name in (expected_route.get("explicit_overrides") or [])
                }
                if pinned != expected_pinned:
                    return False
            values: dict[str, Any] = {}
            if agent_id:
                values["agent_id"] = func.coalesce(func.nullif(agent_sessions.c.agent_id, ""), agent_id)
            if agent_name:
                values["agent_name"] = func.coalesce(func.nullif(agent_sessions.c.agent_name, ""), agent_name)
            if model and "model" not in pinned:
                values["model"] = func.coalesce(func.nullif(agent_sessions.c.model, ""), model)
            if reasoning_effort and "reasoning_effort" not in pinned:
                values["reasoning_effort"] = func.coalesce(
                    func.nullif(agent_sessions.c.reasoning_effort, ""), reasoning_effort
                )
            if not values:
                return False
            values["updated_at"] = _utc_now_iso()
            statement = (
                agent_sessions.update()
                .where(agent_sessions.c.id == str(session_id))
                .where(agent_sessions.c.status != "archived")
                .values(**values)
            )
            for field in (
                "agent_id",
                "agent_name",
                "agent_backend",
                "agent_variant",
                "model",
                "reasoning_effort",
            ):
                if expected_route is not None and field in expected_route:
                    statement = statement.where(
                        func.coalesce(getattr(agent_sessions.c, field), "")
                        == str(expected_route.get(field) or "")
                    )
            result = conn.execute(statement)
            return bool(result.rowcount)

    def bind_agent_session_by_id(
        self,
        *,
        session_id: str,
        native_session_id: Any,
        workdir: str | None = None,
        vibe_agent_id: str | None = None,
        vibe_agent_name: str | None = None,
        vibe_agent_backend: str | None = None,
    ) -> str | None:
        """Bind a backend-native session id to an already-reserved Vibe session row."""
        now = _utc_now_iso()
        encoded_session_id = encode_session_value(native_session_id)
        values = {
            "status": "active",
            "updated_at": now,
            "last_active_at": now,
        }
        if vibe_agent_id is not None:
            values["agent_id"] = vibe_agent_id
        if vibe_agent_name is not None:
            persisted_agent_name = _catalog_agent_name_value(vibe_agent_id, vibe_agent_name)
            values["agent_name"] = (
                case(
                    (agent_sessions.c.agent_id == vibe_agent_id, agent_sessions.c.agent_name),
                    else_=persisted_agent_name,
                )
                if vibe_agent_id is not None
                else persisted_agent_name
            )
        requested_backend = (
            str(vibe_agent_backend or "") if vibe_agent_backend is not None else None
        )
        with self.engine.begin() as conn:
            # Never resurrect an archived (terminal) session. ``bind_agent_session_by_id``
            # targets an explicit row, bypassing the ``status != 'archived'`` lookup
            # guards — and a turn that was still finishing when the session was
            # archived (the cancel is now best-effort/background) can land a late
            # native-id bind here. Refuse it so the terminal archive sticks.
            #
            # THIS READ IS A FAST PATH, NOT THE GUARD. It reserves nothing either
            # -- SQLite takes the write lock at the UPDATE -- so an archive can
            # commit after it and before any write below. That interleaving is
            # harmless because every UPDATE in this function re-asserts the
            # predicate itself: the cross-backend adopt, the same-backend first
            # bind, and the final statement all carry ``status != 'archived'``, so
            # a late archive makes the write a no-op, and each rowcount-0 path
            # re-reads the status and returns ``None``. Nothing here is left to
            # make atomic; the read only spares an already-lost caller the rest of
            # the work. Proven by HFR-252.
            current_status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == str(session_id))
            ).scalar_one_or_none()
            if current_status == "archived":
                return None
            if workdir is not None:
                # LOG-ONLY, so the stale-snapshot hazard does not reach state: this
                # read is never written back. ``workdir`` is not among the columns
                # this function assigns -- neither ``values`` nor ``adopt_values``
                # ever carries it, because the row's workdir is authoritative and
                # only the create path sets it -- so the caller's requested workdir
                # is DISCARDED whether it matches or not. A workdir another
                # connection commits inside this window can therefore at worst make
                # the warning below wrong or missing, which costs operator
                # visibility and not correctness. Nothing to make atomic.
                requested_workdir = str(workdir) or None
                current = conn.execute(
                    select(agent_sessions.c.workdir, agent_sessions.c.session_anchor)
                    .where(agent_sessions.c.id == str(session_id))
                ).mappings().first()
                current_workdir = current.get("workdir") if current else None
                if current_workdir and str(current_workdir) != str(requested_workdir):
                    logger.warning(
                        "Ignoring native bind workdir override; session workdir is authoritative session_id=%s current=%s requested=%s",
                        session_id,
                        current_workdir,
                        requested_workdir,
                    )
            # This is the SECOND backend-adoption entry point. ``_claim_anchor_row``
            # is not involved -- that one resolves a row by (scope, anchor), while
            # this binds an explicitly targeted reserved row -- so the
            # complete-route replacement it performs does not protect this path.
            # Left merged, the row took the incoming backend and variant while
            # keeping the PREVIOUS backend's model, reasoning_effort and
            # explicit-setting marker: a Codex-owned session still routing an
            # OpenCode model.
            route_row = conn.execute(
                select(
                    agent_sessions.c.agent_backend,
                    agent_sessions.c.native_session_id,
                    agent_sessions.c.metadata_json,
                ).where(agent_sessions.c.id == str(session_id))
            ).mappings().first()
            current_backend = str((route_row or {}).get("agent_backend") or "")
            parsed_route_metadata = _json_loads((route_row or {}).get("metadata_json"), {})
            route_metadata = parsed_route_metadata if isinstance(parsed_route_metadata, dict) else {}
            already_bound = bool(decode_session_value((route_row or {}).get("native_session_id")))
            backend_changes = bool(requested_backend) and requested_backend != current_backend
            if backend_changes and not already_bound:
                # The three cases below are decided from a SNAPSHOT, and these reads
                # reserve nothing: SQLite takes the write lock at the UPDATE, so a
                # second connection can bind this row after the read and before the
                # write. A stale caller would then still take this branch and
                # relabel a row that is now bound, clear the winner's route, and
                # overwrite its write-once native id.
                #
                # So the adoption is ONE statement whose predicate re-asserts the
                # snapshot it was decided from -- the same shape
                # ``update_session`` uses for its backend lock. Route replacement
                # and the native write therefore succeed or lose TOGETHER; there is
                # no interleaving that applies one without the other.
                adopt_values = dict(values)
                adopt_values["agent_backend"] = requested_backend
                adopt_values["agent_variant"] = requested_backend or "default"
                # Only Workbench performs turn-start materialization. Its
                # backend-less row therefore owns the resolved route already and
                # initial adoption must keep it. Placeholder IM rows never pass
                # through that lifecycle, so their old route is incompatible with
                # the newly adopted backend and must be cleared like a concrete
                # backend-to-backend replacement (HFR-250).
                preserves_materialized_workbench_route = (
                    not _is_owned_backend(current_backend)
                    and route_metadata.get("created_via") == "workbench"
                )
                if not preserves_materialized_workbench_route:
                    adopt_values["model"] = None
                    adopt_values["reasoning_effort"] = None
                    adopt_values["metadata_json"] = json.dumps(
                        reconcile_explicit_overrides(
                            route_metadata,
                            cleared=OVERRIDABLE_SETTING_COLUMNS,
                        ),
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                adopt_values["native_session_id"] = encoded_session_id
                adopted = conn.execute(
                    agent_sessions.update()
                    .where(agent_sessions.c.id == str(session_id))
                    .where(agent_sessions.c.status != "archived")
                    # Still unbound: write-once enforced BY THE STATEMENT. A rule
                    # enforced by a preceding SELECT is not write-once.
                    .where(func.coalesce(agent_sessions.c.native_session_id, "") == "")
                    # Still on the backend we decided against.
                    .where(func.coalesce(agent_sessions.c.agent_backend, "") == current_backend)
                    .values(**adopt_values)
                )
                if adopted.rowcount:
                    return str(session_id)
                # LOST the race. Return the winner untouched -- its backend
                # identity, native id, model / effort and marker all stand. Falling
                # through to the unconditional update below would apply this
                # caller's stale identity on top of the winner, which is the defect
                # this branch exists to prevent.
                winner_status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == str(session_id))
                ).scalar_one_or_none()
                if winner_status is None or winner_status == "archived":
                    return None
                logger.warning(
                    "Lost the native-bind race for session %s; keeping the winner's route "
                    "(requested backend=%s)",
                    session_id,
                    requested_backend,
                )
                return str(session_id)
            if requested_backend is not None and not (backend_changes and already_bound):
                values["agent_backend"] = requested_backend
                values["agent_variant"] = requested_backend or "default"
            if backend_changes and already_bound:
                # WRITE-ONCE extends to the backend, not just the native id: the row
                # already holds a conversation that a specific backend produced, and
                # re-labelling it would leave that transcript attributed to a backend
                # that never generated it. Drop the identity half of this bind rather
                # than the whole call -- the native-id write below is separately
                # guarded and a same-native re-bind stays idempotent.
                values.pop("agent_id", None)
                values.pop("agent_name", None)
                logger.warning(
                    "Ignoring native bind backend switch on an already-bound session; "
                    "session_id=%s current=%s requested=%s",
                    session_id,
                    current_backend,
                    requested_backend,
                )
            # Same-backend (or backend-less) bind. WRITE-ONCE for the native id is
            # carried by the PREDICATE, not by the ``_set_native_once`` read above
            # it: between that read and this write another connection can commit a
            # bind, and a rule enforced by a preceding SELECT is not write-once.
            # ``_set_native_once`` still decides INTENT (and logs a differing
            # native); the statement decides whether the write may land.
            wants_native = _set_native_once(conn, str(session_id), encoded_session_id)
            if wants_native:
                first_bind = conn.execute(
                    agent_sessions.update()
                    .where(agent_sessions.c.id == str(session_id))
                    .where(agent_sessions.c.status != "archived")
                    .where(func.coalesce(agent_sessions.c.native_session_id, "") == "")
                    .values(**values, native_session_id=encoded_session_id)
                )
                if first_bind.rowcount:
                    return str(session_id)
                # LOST the first bind. Return the winner immediately, exactly as the
                # cross-backend branch above does -- the two lost-race paths now
                # answer the same way instead of one of them continuing.
                #
                # The earlier version only dropped the identity columns when the
                # winner's backend DIFFERED, which left two ways through: a winner
                # on the SAME backend, and a caller that supplied no backend at all
                # (the comparison cannot fire, so the conditional silently passed).
                # Either way the final UPDATE below still ran with this caller's
                # stale snapshot and overwrote the winner's selected Agent id/name,
                # status and timestamps while dutifully preserving its native id.
                # The native id was never the only thing the winner owns.
                logger.warning(
                    "WRITE-ONCE: session %s was bound concurrently; keeping the winner's "
                    "native id and Agent identity",
                    session_id,
                )
                winner_status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == str(session_id))
                ).scalar_one_or_none()
                if winner_status is None or winner_status == "archived":
                    return None
                return str(session_id)
            result = conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == str(session_id))
                # Atomic with the early guard above: never flip an archived row
                # back to active even if the archive commits between that read and
                # this write — the predicate makes the update itself a no-op.
                .where(agent_sessions.c.status != "archived")
                .values(**values)
            )
            return str(session_id) if result.rowcount else None

    def replace_agent_session_native(
        self,
        *,
        session_id: str,
        expected_native_session_id: Any,
        replacement_native_session_id: Any,
    ) -> str | None:
        """Supersede one native binding while preserving the public Session id.

        Native ids remain write-once on ordinary bind paths. A backend repair is
        different: it first snapshots the old binding as an inert superseded row,
        then atomically replaces the active row's binding. Keeping the active row
        id preserves its transcript, deliveries, definitions, and Workbench URL.
        """

        expected = encode_session_value(expected_native_session_id)
        replacement = encode_session_value(replacement_native_session_id)
        if not expected or not replacement:
            raise ValueError("expected and replacement native session ids are required")

        now = _utc_now_iso()
        with self.engine.begin() as conn:
            reserve_write_lock(conn)
            row = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == str(session_id)).limit(1)
            ).mappings().first()
            if row is None or str(row["status"] or "") == "archived":
                return None

            current = str(row["native_session_id"] or "")
            if current == replacement:
                return str(session_id)
            if current != expected:
                return None

            snapshot_id = new_session_id(conn)
            anchor = str(row["session_anchor"] or "")
            superseded_anchor = f"{anchor}{SUPERSEDED_ANCHOR_INFIX}{snapshot_id}"
            metadata_value = _json_loads(row["metadata_json"], {})
            snapshot_metadata = (
                dict(metadata_value) if isinstance(metadata_value, dict) else {}
            )
            snapshot_metadata["superseded_native_binding"] = {
                "active_session_id": str(session_id),
                "replaced_at": now,
            }
            snapshot = dict(row)
            snapshot.update(
                {
                    "id": snapshot_id,
                    "session_anchor": superseded_anchor,
                    "status": "archived",
                    "visibility": "background",
                    "pinned": 0,
                    "agent_status": "idle",
                    "composer_draft_text": None,
                    "composer_draft_updated_at": None,
                    "metadata_json": json.dumps(
                        snapshot_metadata,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    "updated_at": now,
                }
            )
            conn.execute(agent_sessions.insert().values(**snapshot))
            replaced = conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == str(session_id))
                .where(agent_sessions.c.status != "archived")
                .where(func.coalesce(agent_sessions.c.native_session_id, "") == current)
                .values(
                    native_session_id=replacement,
                    updated_at=now,
                    last_active_at=now,
                )
            )
            if not replaced.rowcount:
                raise RuntimeError(
                    f"lost native session replacement for Avibe session {session_id}"
                )
            return str(session_id)

    def find_session_for_anchor(self, *, scope_key: str, session_anchor: str) -> dict[str, Any] | None:
        """Latest ``agent_sessions`` row for ``(scope, anchor)``, any backend.

        Basis for the new session model: a thread resolves to ONE session via
        ``(scope_id, session_anchor)`` and its backend is pinned to whatever that
        row's agent uses — independent of the scope's current routing. The
        most-recently-active row wins if legacy duplicates for the same
        ``(scope, anchor)`` still exist. Read-only: never creates a scope (unlike
        the bind path), so resolving a brand-new thread returns ``None``."""
        with self.engine.begin() as conn:
            scope_id = _lookup_scope_id(conn, str(scope_key))
            if scope_id is None:
                return None
            row = (
                conn.execute(
                    select(agent_sessions)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .where(agent_sessions.c.session_anchor == str(session_anchor))
                    # Archived sessions are terminal + inert: a new inbound message on
                    # the same thread must NOT adopt an archived row — skip it so the
                    # caller falls through to creating a fresh session.
                    .where(agent_sessions.c.status != "archived")
                    .order_by(agent_sessions.c.last_active_at.desc(), agent_sessions.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            data = dict(row)
            if "native_session_id" in data:
                data["native_session_id"] = decode_session_value(data["native_session_id"])
            return data

    def delete_agent_session(
        self,
        *,
        scope_key: str,
        agent_name: str,
        session_anchor: str,
        reclaim_mode: ReclaimMode = RECLAIM_PAUSE,
        reclaim_reason: str | None = None,
    ) -> bool:
        now = _utc_now_iso()
        from storage.background import run_update_event_transaction

        # ``reclaim_ledger_transaction`` OUTSIDE ``begin()``: every transaction that can
        # reclaim a definition must discard its ledger entries if it does not commit
        # (HFR-273), and the truncation has to run after the rollback.
        deleted = 0
        with reclaim_ledger_transaction(), run_update_event_transaction(
            self.engine
        ) as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is not None:
                deleted = _delete_agent_session_rows(
                    conn,
                    select(agent_sessions.c.id)
                    .where(agent_sessions.c.scope_id == scope_id)
                    .where(_agent_session_name_predicate(str(agent_name) or "default"))
                    .where(agent_sessions.c.session_anchor == str(session_anchor)),
                    reclaim_mode=reclaim_mode,
                    reclaim_reason=reclaim_reason,
                )
        if scope_id is not None:
            _publish_definition_reclaim_hint()
        return bool(deleted)

    def delete_agent_sessions(
        self,
        *,
        scope_key: str,
        agent_name: str | None = None,
        session_anchor_prefix: str | None = None,
        reclaim_mode: ReclaimMode = RECLAIM_PAUSE,
        reclaim_reason: str | None = None,
        include_superseded: bool = False,
    ) -> int:
        now = _utc_now_iso()
        deleted = 0
        from storage.background import run_update_event_transaction

        with reclaim_ledger_transaction(), run_update_event_transaction(
            self.engine
        ) as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is None:
                stmt = None
            else:
                stmt = select(agent_sessions.c.id).where(
                    agent_sessions.c.scope_id == scope_id
                )
            if stmt is not None and agent_name is not None:
                stmt = stmt.where(
                    _agent_session_name_predicate(str(agent_name) or "default")
                )
            if stmt is not None and session_anchor_prefix is not None:
                prefix = str(session_anchor_prefix)
                prefix_pattern = f"{_escape_sql_like(prefix)}:%"
                stmt = stmt.where(
                    (agent_sessions.c.session_anchor == prefix)
                    | (agent_sessions.c.session_anchor.like(prefix_pattern, escape="\\"))
                )
            if stmt is not None and not include_superseded:
                # A superseded row carries ``<original_anchor>:superseded:<id>``.
                # This is a HARD delete and superseding deliberately keeps the row
                # -- its native id is write-once and its history is not
                # recoverable -- so exclude it from EVERY deletion path here, not
                # just the prefix clear.
                #
                # The prefix branch alone is not enough, and the reason is the
                # call order in ``handle_new``: it runs
                # ``agent_service.clear_sessions()`` first, whose backend adapters
                # reach this method with an ``agent_name`` and NO
                # ``session_anchor_prefix``. A guard nested in the branch above is
                # skipped there, so the row is already gone before the guarded
                # clear runs. Callers that genuinely mean "remove everything",
                # such as tearing down a scope that no longer exists, pass
                # ``include_superseded=True`` rather than relying on which branch
                # they happen to take.
                #
                # NULL-safe by construction: ``NOT LIKE`` over a NULL anchor
                # evaluates to NULL, not true, so a bare negation silently
                # PRESERVES every row whose ``session_anchor`` is NULL -- the
                # exact inverse of this guard's purpose, and invisible because
                # the rows simply fail to be deleted. Only a real marker may
                # survive.
                stmt = stmt.where(
                    or_(
                        agent_sessions.c.session_anchor.is_(None),
                        ~agent_sessions.c.session_anchor.like(
                            f"%{_escape_sql_like(SUPERSEDED_ANCHOR_INFIX)}%", escape="\\"
                        ),
                    )
                )
            if stmt is not None:
                deleted = _delete_agent_session_rows(
                    conn,
                    stmt,
                    reclaim_mode=reclaim_mode,
                    reclaim_reason=reclaim_reason,
                )
        if scope_id is not None:
            _publish_definition_reclaim_hint()
        return deleted

    def load_state(self) -> SessionState:
        with self.engine.connect() as conn:
            return SessionState(
                session_mappings=self._load_session_mappings(conn),
                active_slack_threads=self._load_active_threads(conn),
                active_polls=self._load_active_polls(conn),
                processed_message_ts=self._load_processed_messages(conn),
                last_activity=self._load_last_activity(conn),
            )

    def processed_message_exists(
        self,
        channel_id: str,
        thread_ts: str,
        message_ts: str,
    ) -> bool:
        record_key = _processed_message_record_key(
            str(channel_id),
            str(thread_ts),
            str(message_ts),
        )
        with self.engine.connect() as conn:
            row_id = conn.execute(
                select(runtime_records.c.id)
                .where(runtime_records.c.record_type == "processed_message")
                .where(runtime_records.c.record_key == record_key)
                .limit(1)
            ).scalar_one_or_none()
        return row_id is not None

    def try_record_processed_message(self, channel_id: str, thread_ts: str, message_ts: str) -> bool:
        """Atomically claim a message for processing.

        Multiple Socket Mode clients or stale runtime instances can receive the same
        IM event. The unique runtime record is the cross-process source of truth.
        """
        now = _utc_now_iso()
        channel_key = str(channel_id)
        thread_key = str(thread_ts)
        message_key = str(message_ts)
        record_key = _processed_message_record_key(channel_key, thread_key, message_key)
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    runtime_records.insert().values(
                        id=f"runtime::processed_message::{record_key}",
                        record_type="processed_message",
                        record_key=record_key,
                        scope_id=None,
                        session_anchor=thread_key,
                        workdir=None,
                        payload_json=_json_dumps(
                            {
                                "channel_id": channel_key,
                                "thread_id": thread_key,
                                "message_id": message_key,
                                "processed_at": now,
                            }
                        ),
                        expires_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                _prune_processed_message_records(conn, channel_id=channel_key, thread_id=thread_key)
        except IntegrityError:
            return False
        return True

    def try_record_runtime_event(
        self,
        record_type: str,
        record_key: str,
        payload: dict[str, Any] | None = None,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Atomically claim a short-lived runtime event."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        event_type = str(record_type or "").strip()
        event_key = str(record_key or "").strip()
        if not event_type or not event_key:
            return True
        values = _runtime_record_values(
            record_type=event_type,
            record_key=event_key,
            scope_id=None,
            session_anchor=None,
            workdir=None,
            payload=dict(payload or {}),
            now=now,
        )
        if ttl_seconds is not None and ttl_seconds > 0:
            values["expires_at"] = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    runtime_records.delete()
                    .where(runtime_records.c.record_type == event_type)
                    .where(runtime_records.c.expires_at.is_not(None))
                    .where(runtime_records.c.expires_at < now)
                )
                conn.execute(runtime_records.insert().values(**values))
        except IntegrityError:
            return False
        return True

    def upsert_processed_message(self, channel_id: str, thread_ts: str, message_ts: str) -> None:
        now = _utc_now_iso()
        channel_key = str(channel_id)
        thread_key = str(thread_ts)
        message_key = str(message_ts)
        record_key = _processed_message_record_key(channel_key, thread_key, message_key)
        values = _runtime_record_values(
            record_type="processed_message",
            record_key=record_key,
            scope_id=None,
            session_anchor=thread_key,
            workdir=None,
            payload={
                "channel_id": channel_key,
                "thread_id": thread_key,
                "message_id": message_key,
                "processed_at": now,
            },
            now=now,
        )
        with self.engine.begin() as conn:
            _upsert_runtime_record(conn, values)
            _prune_processed_message_records(conn, channel_id=channel_key, thread_id=thread_key)

    def mark_thread_active(self, scope_key: str, channel_id: str, thread_ts: str, last_active_at: float) -> None:
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            record_key = f"{scope_key}|{channel_id}|{thread_ts}"
            _upsert_runtime_record(
                conn,
                _runtime_record_values(
                    record_type="active_thread",
                    record_key=record_key,
                    scope_id=scope_id,
                    session_anchor=str(thread_ts),
                    workdir=None,
                    payload={
                        "scope_key": str(scope_key),
                        "channel_id": str(channel_id),
                        "thread_id": str(thread_ts),
                        "last_active_at": _float(last_active_at),
                    },
                    now=now,
                ),
            )

    def delete_active_thread(self, scope_key: str, channel_id: str, thread_ts: str) -> bool:
        record_key = f"{scope_key}|{channel_id}|{thread_ts}"
        with self.engine.begin() as conn:
            result = conn.execute(
                runtime_records.delete()
                .where(runtime_records.c.record_type == "active_thread")
                .where(runtime_records.c.record_key == record_key)
            )
            return bool(result.rowcount)

    def upsert_active_poll(self, poll_info: ActivePollInfo | dict[str, Any]) -> None:
        now = _utc_now_iso()
        data = poll_info.to_dict() if isinstance(poll_info, ActivePollInfo) else dict(poll_info)
        record_key = str(data.get("opencode_session_id") or "")
        if not record_key:
            return
        settings_key = str(data.get("settings_key") or "")
        platform = str(data.get("platform") or "")
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(
                conn,
                f"{platform}::{settings_key}" if platform and "::" not in settings_key else settings_key,
                now=now,
            )
            _upsert_runtime_record(
                conn,
                _runtime_record_values(
                    record_type="active_poll",
                    record_key=record_key,
                    scope_id=scope_id,
                    session_anchor=str(data.get("base_session_id") or data.get("thread_id") or ""),
                    workdir=str(data.get("working_path") or "") or None,
                    payload=data,
                    now=now,
                ),
            )

    def delete_active_poll(self, opencode_session_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                runtime_records.delete()
                .where(runtime_records.c.record_type == "active_poll")
                .where(runtime_records.c.record_key == str(opencode_session_id))
            )
            return bool(result.rowcount)

    def save_state(self, state: SessionState) -> None:
        with self.engine.begin() as conn:
            state.processed_message_ts = _merge_processed_message_maps(
                state.processed_message_ts,
                self._load_processed_messages(conn),
            )
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            existing_session_ids = self._load_existing_session_ids(conn)
            used_session_ids: set[str] = set(existing_session_ids.values())

            # (scope_id, bare anchor) -> row id already written in THIS call. A thread
            # is now ONE session per (scope, anchor); legacy JSON can list several
            # backends under one thread, so the FIRST one establishes the row and
            # later duplicates are skipped (write-once), instead of fighting the
            # unique index with a second insert.
            seen_anchor_rows: dict[tuple[str | None, str], str] = {}
            for scope_key, agent_maps in state.session_mappings.items():
                if not isinstance(agent_maps, dict):
                    continue
                scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
                resolved_imported_identities: dict[str, tuple[str, str | None, str | None]] = {}
                normalized_anchors: dict[tuple[str, str], str] = {}
                anchor_owner_candidates: dict[
                    str, list[tuple[str, str, str | None, str | None]]
                ] = {}
                for candidate_name, candidate_thread_map in agent_maps.items():
                    if not isinstance(candidate_thread_map, dict):
                        continue
                    candidate_variant = str(candidate_name) or "default"
                    candidate_identity = _resolve_imported_agent_identity(conn, candidate_variant)
                    resolved_imported_identities[candidate_variant] = candidate_identity
                    for candidate_thread_id in candidate_thread_map:
                        candidate_thread_key = str(candidate_thread_id)
                        candidate_anchor = _base_session_anchor(candidate_thread_key)
                        normalized_anchors[(candidate_variant, candidate_thread_key)] = candidate_anchor
                        anchor_owner_candidates.setdefault(candidate_anchor, []).append(
                            (candidate_variant, *candidate_identity)
                        )
                for agent_name, thread_map in agent_maps.items():
                    if not isinstance(thread_map, dict):
                        continue
                    imported_variant = str(agent_name) or "default"
                    imported_backend, imported_agent_id, imported_agent_name = resolved_imported_identities[
                        imported_variant
                    ]
                    for thread_id, native_session_id in thread_map.items():
                        thread_key = str(thread_id)
                        # Normalise OpenCode ``base:/cwd`` composites to the bare
                        # anchor so imported rows match the bare-anchor read path;
                        # subagent ``base:<name>`` anchors are preserved. Workdir is
                        # snapshotted from scope settings, never inferred from the
                        # legacy anchor suffix.
                        base_anchor = normalized_anchors[(imported_variant, thread_key)]
                        dedup_key = (scope_id, base_anchor)
                        if dedup_key in seen_anchor_rows:
                            continue
                        encoded_session_id = encode_session_value(native_session_id)
                        existing_anchor_row = _find_scope_anchor_row(
                            conn,
                            scope_id=scope_id,
                            session_anchor=base_anchor,
                        )
                        skip_mapping = False
                        while existing_anchor_row is not None:
                            if str(existing_anchor_row["status"] or "") == "archived":
                                logger.warning(
                                    "Skipping legacy session import because an archived row owns the anchor "
                                    "scope_id=%s anchor=%s imported_backend=%s imported_variant=%s",
                                    scope_id,
                                    base_anchor,
                                    imported_backend,
                                    imported_variant,
                                )
                                skip_mapping = True
                                break
                            observed_backend = str(existing_anchor_row["agent_backend"] or "")
                            observed_variant = str(existing_anchor_row["agent_variant"] or "")
                            observed_agent_id = str(existing_anchor_row["agent_id"] or "")
                            observed_agent_name = str(existing_anchor_row["agent_name"] or "")
                            observed_native_session_id = str(existing_anchor_row["native_session_id"] or "")
                            existing_backend = observed_backend.strip()
                            existing_variant = observed_variant.strip() or "default"
                            existing_agent_id = observed_agent_id.strip() or None
                            existing_agent_name = observed_agent_name.strip() or None
                            existing_native_session_id = observed_native_session_id.strip()
                            existing_is_owned = _is_owned_backend(existing_backend)
                            existing_variant_is_owned = not _is_sentinel_variant(existing_variant)
                            import_matches_existing_owner = _import_matches_existing_owner(
                                existing_backend=existing_backend,
                                existing_variant=existing_variant,
                                existing_agent_id=existing_agent_id,
                                existing_agent_name=existing_agent_name,
                                imported_backend=imported_backend,
                                imported_variant=imported_variant,
                                imported_agent_id=imported_agent_id,
                                imported_agent_name=imported_agent_name,
                            )
                            # Prefer a legacy mapping for the reserved owner when one
                            # exists; otherwise an unbound route reservation is
                            # provisional and may be adopted by the imported session.
                            has_existing_owner_mapping = any(
                                _import_matches_existing_owner(
                                    existing_backend=existing_backend,
                                    existing_variant=existing_variant,
                                    existing_agent_id=existing_agent_id,
                                    existing_agent_name=existing_agent_name,
                                    imported_backend=candidate_backend,
                                    imported_variant=candidate_variant,
                                    imported_agent_id=candidate_agent_id,
                                    imported_agent_name=candidate_agent_name,
                                )
                                for (
                                    candidate_variant,
                                    candidate_backend,
                                    candidate_agent_id,
                                    candidate_agent_name,
                                ) in anchor_owner_candidates.get(base_anchor, ())
                            )
                            adopts_unbound_route = (
                                not existing_native_session_id
                                and not import_matches_existing_owner
                                and not has_existing_owner_mapping
                            )
                            existing_identity_is_durable = (
                                existing_is_owned
                                or existing_variant_is_owned
                                or bool(existing_native_session_id)
                            )
                            preserves_existing_identity = (
                                existing_identity_is_durable and not adopts_unbound_route
                            )
                            sentinel_variant_compatible = (
                                existing_is_owned
                                and _is_sentinel_variant(existing_variant)
                                and (
                                    imported_backend == "unknown" or existing_backend == imported_backend
                                )
                            )
                            replaces_route_owner = not import_matches_existing_owner and (
                                not existing_is_owned
                                or adopts_unbound_route
                                or sentinel_variant_compatible
                            )
                            same_agent_identity = (
                                imported_agent_id is not None and existing_agent_id == imported_agent_id
                            )
                            same_agent_name = (
                                imported_agent_name is not None
                                and existing_agent_name is not None
                                and _normalize_agent_name_key(existing_agent_name)
                                == _normalize_agent_name_key(imported_agent_name)
                            )
                            imported_variant_matches_existing_agent = (
                                existing_agent_name is not None
                                and _normalize_agent_name_key(imported_variant)
                                == _normalize_agent_name_key(existing_agent_name)
                            )
                            backend_conflicts = imported_backend != "unknown" and existing_backend != imported_backend
                            variant_conflicts = (
                                not import_matches_existing_owner
                                and not sentinel_variant_compatible
                            )
                            if not adopts_unbound_route and (
                                (existing_variant_is_owned and variant_conflicts)
                                or (existing_is_owned and backend_conflicts)
                            ):
                                logger.warning(
                                    "Skipping legacy session import that would relabel anchor row to a different owner "
                                    "scope_id=%s anchor=%s existing_backend=%s existing_variant=%s "
                                    "imported_backend=%s imported_variant=%s",
                                    scope_id,
                                    base_anchor,
                                    existing_backend,
                                    existing_variant,
                                    imported_backend,
                                    imported_variant,
                                )
                                skip_mapping = True
                                break
                            identity_conflicts = (
                                preserves_existing_identity
                                and imported_backend == "unknown"
                                and imported_agent_id is None
                                and imported_agent_name is None
                                and (existing_agent_id is not None or existing_agent_name is not None)
                                and not imported_variant_matches_existing_agent
                            )
                            if preserves_existing_identity and imported_agent_id is not None and existing_agent_id not in {
                                None,
                                imported_agent_id,
                            }:
                                identity_conflicts = True
                            if (
                                preserves_existing_identity
                                and imported_agent_name is not None
                                and not same_agent_identity
                                and not same_agent_name
                                and existing_agent_name not in {
                                    None,
                                    imported_agent_name,
                                }
                            ):
                                identity_conflicts = True
                            if identity_conflicts:
                                logger.warning(
                                    "Skipping legacy session import that would replace the durable Agent identity "
                                    "scope_id=%s anchor=%s existing_agent_id=%s existing_agent_name=%s "
                                    "imported_agent_id=%s imported_agent_name=%s",
                                    scope_id,
                                    base_anchor,
                                    existing_agent_id,
                                    existing_agent_name,
                                    imported_agent_id,
                                    imported_agent_name,
                                )
                                skip_mapping = True
                                break
                            backfills_agent_id = imported_agent_id is not None and existing_agent_id is None
                            backfills_agent_name = imported_agent_name is not None and existing_agent_name is None
                            update_values: dict[str, Any] = {"updated_at": now}
                            if not existing_is_owned or adopts_unbound_route:
                                update_values["agent_variant"] = imported_variant
                                update_values["agent_backend"] = (
                                    imported_backend if imported_backend != "unknown" else existing_backend or "default"
                                )
                            if replaces_route_owner:
                                update_values["model"] = None
                                update_values["reasoning_effort"] = None
                                update_values["metadata_json"] = json.dumps(
                                    reconcile_explicit_overrides(
                                        _json_loads(existing_anchor_row["metadata_json"], {}),
                                        cleared=OVERRIDABLE_SETTING_COLUMNS,
                                    ),
                                    separators=(",", ":"),
                                    ensure_ascii=False,
                                )
                            if not preserves_existing_identity:
                                update_values["agent_id"] = imported_agent_id
                                update_values["agent_name"] = imported_agent_name
                            if sentinel_variant_compatible and existing_variant != imported_variant:
                                update_values["agent_variant"] = imported_variant
                            if backfills_agent_id:
                                update_values["agent_id"] = imported_agent_id
                            if backfills_agent_name:
                                update_values["agent_name"] = imported_agent_name
                            update_stmt = (
                                agent_sessions.update()
                                .where(agent_sessions.c.id == str(existing_anchor_row["id"]))
                                .where(agent_sessions.c.status != "archived")
                                .where(func.coalesce(agent_sessions.c.agent_backend, "") == observed_backend)
                                .where(func.coalesce(agent_sessions.c.agent_variant, "") == observed_variant)
                                .where(func.coalesce(agent_sessions.c.agent_id, "") == observed_agent_id)
                                .where(func.coalesce(agent_sessions.c.agent_name, "") == observed_agent_name)
                                .where(
                                    func.coalesce(agent_sessions.c.native_session_id, "")
                                    == observed_native_session_id
                                )
                            )
                            if conn.execute(update_stmt.values(**update_values)).rowcount:
                                break
                            logger.warning(
                                "Lost the legacy-import anchor update race for session %s; re-resolving the anchor "
                                "instead (imported backend=%s variant=%s)",
                                existing_anchor_row["id"],
                                imported_backend,
                                imported_variant,
                            )
                            refreshed_anchor_row = _find_scope_anchor_row(
                                conn,
                                scope_id=scope_id,
                                session_anchor=base_anchor,
                            )
                            if refreshed_anchor_row is None:
                                logger.warning(
                                    "Skipping legacy session import because the anchor disappeared during update "
                                    "scope_id=%s anchor=%s imported_backend=%s imported_variant=%s",
                                    scope_id,
                                    base_anchor,
                                    imported_backend,
                                    imported_variant,
                                )
                                skip_mapping = True
                                break
                            refreshed_native_session_id = str(
                                refreshed_anchor_row["native_session_id"] or ""
                            )
                            refreshed_route = tuple(
                                str(refreshed_anchor_row[column] or "")
                                for column in ("agent_backend", "agent_variant")
                            )
                            if refreshed_route != (observed_backend, observed_variant):
                                logger.warning(
                                    "Skipping legacy session import after losing a concurrent route claim "
                                    "scope_id=%s anchor=%s imported_backend=%s imported_variant=%s",
                                    scope_id,
                                    base_anchor,
                                    imported_backend,
                                    imported_variant,
                                )
                                skip_mapping = True
                                break
                            if (
                                refreshed_native_session_id != observed_native_session_id
                                and refreshed_native_session_id != encoded_session_id
                            ):
                                logger.warning(
                                    "Skipping legacy session import after losing a concurrent native-session claim "
                                    "scope_id=%s anchor=%s winner_native_session_id=%s imported_variant=%s",
                                    scope_id,
                                    base_anchor,
                                    refreshed_native_session_id,
                                    imported_variant,
                                )
                                skip_mapping = True
                                break
                            existing_anchor_row = refreshed_anchor_row
                        if skip_mapping:
                            continue
                        row_key = _session_row_key(
                            scope_id=scope_id,
                            agent_variant=imported_variant,
                            session_anchor=base_anchor,
                            native_session_id=encoded_session_id,
                        )
                        row_id = (
                            str(existing_anchor_row["id"]) if existing_anchor_row is not None else None
                            or existing_session_ids.get(row_key)
                            or _new_session_id(used_session_ids)
                        )
                        seen_anchor_rows[dedup_key] = row_id
                        stmt = sqlite_insert(agent_sessions).values(
                            id=row_id,
                            scope_id=scope_id,
                            agent_id=imported_agent_id,
                            agent_name=imported_agent_name,
                            agent_backend=imported_backend,
                            agent_variant=imported_variant,
                            model=None,
                            reasoning_effort=None,
                            session_anchor=base_anchor,
                            workdir=snapshot_scope_workdir(conn, scope_id),
                            native_session_id=encoded_session_id,
                            title=None,
                            status="active",
                            metadata_json=_json_dumps({"legacy_scope_key": str(scope_key)}),
                            created_at=now,
                            updated_at=now,
                            last_active_at=now,
                        )
                        conn.execute(
                            stmt.on_conflict_do_update(
                                index_elements=[agent_sessions.c.id],
                                set_={
                                    "scope_id": stmt.excluded.scope_id,
                                    "session_anchor": stmt.excluded.session_anchor,
                                    "native_session_id": stmt.excluded.native_session_id,
                                    "status": stmt.excluded.status,
                                    "metadata_json": stmt.excluded.metadata_json,
                                    "updated_at": stmt.excluded.updated_at,
                                    # ``last_active_at`` is deliberately absent: it is the
                                    # session-list ranking column and only real activity may
                                    # move it. ``now`` is computed once per call, so writing it
                                    # here stamped every reconciled row with one identical
                                    # value and collapsed the ranking to its tiebreakers.
                                    # New rows still get their stamp from ``values()`` above.
                                },
                            )
                        )

            for scope_key, channel_map in state.active_slack_threads.items():
                if not isinstance(channel_map, dict):
                    continue
                scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
                for channel_id, thread_map in channel_map.items():
                    if not isinstance(thread_map, dict):
                        continue
                    for thread_id, last_active_at in thread_map.items():
                        record_key = f"{scope_key}|{channel_id}|{thread_id}"
                        _upsert_runtime_record(
                            conn,
                            _runtime_record_values(
                                record_type="active_thread",
                                record_key=record_key,
                                scope_id=scope_id,
                                session_anchor=str(thread_id),
                                workdir=None,
                                payload={
                                    "scope_key": str(scope_key),
                                    "channel_id": str(channel_id),
                                    "thread_id": str(thread_id),
                                    "last_active_at": _float(last_active_at),
                                },
                                now=now,
                            ),
                        )

            for opencode_session_id, item in state.active_polls.items():
                data = item.to_dict() if isinstance(item, ActivePollInfo) else item
                if not isinstance(data, dict):
                    continue
                record_key = str(opencode_session_id)
                settings_key = str(data.get("settings_key") or "")
                platform = str(data.get("platform") or "")
                scope_id = resolve_scope_from_legacy_key(
                    conn,
                    f"{platform}::{settings_key}" if platform and "::" not in settings_key else settings_key,
                    now=now,
                )
                _upsert_runtime_record(
                    conn,
                    _runtime_record_values(
                        record_type="active_poll",
                        record_key=record_key,
                        scope_id=scope_id,
                        session_anchor=str(data.get("base_session_id") or data.get("thread_id") or ""),
                        workdir=str(data.get("working_path") or "") or None,
                        payload=data,
                        now=now,
                    ),
                )

            seen_messages: set[tuple[str, str, str]] = set()
            retained_processed_records: dict[tuple[str, str], set[str]] = {}
            message_order = 0
            for channel_id, thread_map in state.processed_message_ts.items():
                if not isinstance(thread_map, dict):
                    continue
                for thread_id, value in thread_map.items():
                    message_ids = [value] if isinstance(value, str) else list(value or [])
                    for message_id in message_ids[-200:]:
                        key = (str(channel_id), str(thread_id), str(message_id))
                        if key in seen_messages:
                            continue
                        seen_messages.add(key)
                        record_key = _processed_message_record_key(*key)
                        retained_processed_records.setdefault((key[0], key[1]), set()).add(record_key)
                        ordered_at = (now_dt + timedelta(microseconds=message_order)).isoformat()
                        message_order += 1
                        _upsert_runtime_record(
                            conn,
                            _runtime_record_values(
                                record_type="processed_message",
                                record_key=record_key,
                                scope_id=None,
                                session_anchor=str(thread_id),
                                workdir=None,
                                payload={
                                    "channel_id": str(channel_id),
                                    "thread_id": str(thread_id),
                                    "message_id": str(message_id),
                                    "processed_at": ordered_at,
                                },
                                now=ordered_at,
                            ),
                            update_created_at=True,
                        )

            for (channel_id, thread_id), record_keys in retained_processed_records.items():
                _prune_processed_message_records(
                    conn,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    retained_record_keys=record_keys,
                )

            if state.last_activity is not None:
                stmt = sqlite_insert(state_meta).values(
                    key=SESSIONS_LAST_ACTIVITY_KEY,
                    value_json=_json_dumps(state.last_activity),
                    updated_at=now,
                )
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[state_meta.c.key],
                        set_={
                            "value_json": stmt.excluded.value_json,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                )

    def _load_existing_session_ids(self, conn: Connection) -> dict[tuple[str | None, str, str, str], str]:
        rows = conn.execute(
            select(
                agent_sessions.c.id,
                agent_sessions.c.scope_id,
                agent_sessions.c.agent_variant,
                agent_sessions.c.session_anchor,
                agent_sessions.c.native_session_id,
            )
        ).mappings()
        result: dict[tuple[str | None, str, str, str], str] = {}
        for row in rows:
            result[
                _session_row_key(
                    scope_id=row["scope_id"],
                    agent_variant=str(row["agent_variant"] or "default"),
                    session_anchor=str(row["session_anchor"] or ""),
                    native_session_id=str(row["native_session_id"] or ""),
                )
            ] = str(row["id"])
        return result

    def _load_session_mappings(self, conn: Connection) -> dict[str, dict[str, dict[str, Any]]]:
        rows = conn.execute(
            select(
                agent_sessions.c.scope_id,
                agent_sessions.c.agent_variant,
                agent_sessions.c.session_anchor,
                agent_sessions.c.native_session_id,
                agent_sessions.c.metadata_json,
                scopes.c.platform,
                scopes.c.scope_type,
                scopes.c.native_id,
            ).join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
        ).mappings()
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            scope_key = _legacy_scope_key(row)
            agent_name = str(row["agent_variant"] or "default")
            result.setdefault(scope_key, {}).setdefault(agent_name, {})[str(row["session_anchor"])] = (
                decode_session_value(row["native_session_id"])
            )
        return result

    def _load_active_threads(self, conn: Connection) -> dict[str, dict[str, dict[str, float]]]:
        rows = conn.execute(
            select(runtime_records.c.payload_json).where(runtime_records.c.record_type == "active_thread")
        )
        result: dict[str, dict[str, dict[str, float]]] = {}
        for (payload_json,) in rows:
            payload = _json_loads(payload_json, {})
            scope_key = str(payload.get("scope_key") or "")
            channel_id = str(payload.get("channel_id") or "")
            thread_id = str(payload.get("thread_id") or "")
            if not scope_key or not channel_id or not thread_id:
                continue
            result.setdefault(scope_key, {}).setdefault(channel_id, {})[thread_id] = _float(
                payload.get("last_active_at")
            )
        return result

    def _load_active_polls(self, conn: Connection) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            select(runtime_records.c.record_key, runtime_records.c.payload_json).where(
                runtime_records.c.record_type == "active_poll"
            )
        )
        result: dict[str, dict[str, Any]] = {}
        for record_key, payload_json in rows:
            payload = _json_loads(payload_json, {})
            if not isinstance(payload, dict):
                continue
            payload.setdefault("opencode_session_id", str(record_key))
            result[str(record_key)] = payload
        return result

    def _load_processed_messages(self, conn: Connection) -> dict[str, dict[str, list[str]]]:
        rows = conn.execute(
            select(runtime_records.c.payload_json)
            .where(runtime_records.c.record_type == "processed_message")
            .order_by(runtime_records.c.created_at, _RUNTIME_RECORD_ROWID)
        )
        result: dict[str, dict[str, list[str]]] = {}
        for (payload_json,) in rows:
            payload = _json_loads(payload_json, {})
            channel_id = str(payload.get("channel_id") or "")
            thread_id = str(payload.get("thread_id") or "")
            message_id = str(payload.get("message_id") or "")
            if not channel_id or not thread_id or not message_id:
                continue
            result.setdefault(channel_id, {}).setdefault(thread_id, []).append(message_id)
        return result

    def _load_last_activity(self, conn: Connection) -> str | None:
        value = conn.execute(
            select(state_meta.c.value_json).where(state_meta.c.key == SESSIONS_LAST_ACTIVITY_KEY)
        ).scalar_one_or_none()
        return _json_loads(value, None)


def _lookup_scope_id(conn: Connection, scope_key: str) -> str | None:
    """Read-only scope-id resolution. Like ``resolve_scope_from_legacy_key`` but
    NEVER upserts a scope — for read paths that must not create one."""
    raw = str(scope_key or "")
    parts = raw.split("::")
    scope_type = None
    if len(parts) >= 3 and parts[1] in {"channel", "user", "platform", "project"}:
        platform, scope_type, native_id = parts[0], parts[1], "::".join(parts[2:])
    else:
        platform, native_id = _split_scoped_key(scope_key)
        if platform is None:
            platform = "unknown"
    if not platform or not native_id:
        return None
    if scope_type:
        found = conn.execute(
            select(scopes.c.id)
            .where(scopes.c.platform == platform, scopes.c.scope_type == scope_type, scopes.c.native_id == native_id)
            .limit(1)
        ).scalar_one_or_none()
        return str(found) if found is not None else None
    found = conn.execute(
        select(scopes.c.id).where(scopes.c.platform == platform, scopes.c.native_id == native_id).limit(1)
    ).scalar_one_or_none()
    return str(found) if found is not None else None


def resolve_scope_from_legacy_key(conn: Connection, scope_key: str, *, now: str) -> str | None:
    raw_scope_key = str(scope_key or "")
    parts = raw_scope_key.split("::")
    if len(parts) == 3 and parts[1] in {"channel", "user", "platform", "project"}:
        platform, scope_type, native_id = parts
        if not platform or not native_id:
            return None
        return upsert_scope(conn, platform, scope_type, native_id, now=now)

    platform, native_id = _split_scoped_key(scope_key)
    if not native_id:
        return None
    if platform is None:
        platform = "unknown"
    existing = conn.execute(
        select(scopes.c.id).where(scopes.c.platform == platform, scopes.c.native_id == native_id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return str(existing)
    return upsert_scope(conn, platform, _infer_scope_type(platform, native_id), native_id, now=now)


def _merge_processed_message_maps(
    primary: dict[str, dict[str, Any]],
    secondary: dict[str, dict[str, list[str]]],
) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for source in (primary, secondary):
        if not isinstance(source, dict):
            continue
        for channel_id, thread_map in source.items():
            if not isinstance(thread_map, dict):
                continue
            channel_key = str(channel_id)
            for thread_id, value in thread_map.items():
                thread_key = str(thread_id)
                message_ids = [value] if isinstance(value, str) else list(value or [])
                for message_id in message_ids[-200:]:
                    key = (channel_key, thread_key, str(message_id))
                    if key in seen:
                        continue
                    seen.add(key)
                    result.setdefault(channel_key, {}).setdefault(thread_key, []).append(str(message_id))
    for thread_map in result.values():
        for thread_id, message_ids in list(thread_map.items()):
            thread_map[thread_id] = message_ids[-200:]
    return result


def _legacy_scope_key(row: dict[str, Any]) -> str:
    metadata = _json_loads(row.get("metadata_json"), {})
    if isinstance(metadata, dict) and metadata.get("legacy_scope_key"):
        return str(metadata["legacy_scope_key"])
    platform = row.get("platform")
    scope_type = row.get("scope_type")
    native_id = row.get("native_id")
    if platform and native_id:
        if scope_type == "user" and platform in {"telegram", "wechat"}:
            return f"{platform}::user::{native_id}"
        return f"{platform}::{native_id}"
    scope_id = row.get("scope_id")
    if isinstance(scope_id, str) and scope_id.count("::") >= 2:
        parts = scope_id.split("::", 2)
        return f"{parts[0]}::{parts[2]}"
    return str(scope_id or "")


def _infer_scope_type(platform: str, native_id: str) -> str:
    if platform == "slack" and native_id and native_id[0] in {"U", "W"}:
        return "user"
    if platform == "lark" and native_id.startswith("ou_"):
        return "user"
    if platform == "wechat" and (native_id.startswith("wxid_") or native_id.startswith("user")):
        return "user"
    return "channel"


_BACKEND_AGENT_NAMES = {"codex", "claude", "opencode"}
_ROUTING_SENTINEL_VARIANTS = {"", "default", *_BACKEND_AGENT_NAMES}


def _agent_backend(agent_name: str) -> str:
    return agent_name if agent_name in _BACKEND_AGENT_NAMES else "unknown"


def _is_owned_backend(agent_backend: str) -> bool:
    return str(agent_backend or "").strip() not in {"", "default", "unknown"}


def _is_sentinel_variant(agent_variant: str) -> bool:
    return str(agent_variant or "").strip() in {"", "default"}


def _normalize_agent_name_key(agent_name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(agent_name or "").strip().lower()).strip("-_")


def _import_matches_existing_owner(
    *,
    existing_backend: str,
    existing_variant: str,
    existing_agent_id: str | None,
    existing_agent_name: str | None,
    imported_backend: str,
    imported_variant: str,
    imported_agent_id: str | None,
    imported_agent_name: str | None,
) -> bool:
    """Compare owners from most-specific Agent identity to generic backend aliases."""
    if existing_agent_id is not None and imported_agent_id is not None:
        return existing_agent_id == imported_agent_id
    if existing_agent_name is not None:
        existing_name_key = _normalize_agent_name_key(existing_agent_name)
        return existing_name_key in {
            _normalize_agent_name_key(imported_variant),
            _normalize_agent_name_key(imported_agent_name or ""),
        }
    if existing_agent_id is not None:
        return False

    existing_variant_key = _normalize_agent_name_key(existing_variant)
    imported_variant_key = _normalize_agent_name_key(imported_variant)
    if not _is_sentinel_variant(existing_variant) and existing_variant_key not in _BACKEND_AGENT_NAMES:
        return existing_variant_key == imported_variant_key
    if imported_agent_id is not None or imported_agent_name is not None:
        return False
    return imported_variant_key == existing_variant_key or (
        imported_backend == existing_backend
        and imported_backend != "unknown"
        and imported_variant_key == _normalize_agent_name_key(existing_backend)
    )


def _resolve_imported_agent_identity(conn: Connection, agent_name: str) -> tuple[str, str | None, str | None]:
    """Resolve a legacy mapping name through built-ins and the Vibe Agent catalog."""
    requested = str(agent_name or "").strip()
    normalized = _normalize_agent_name_key(requested)
    if normalized:
        catalog_agent = conn.execute(
            select(agents.c.id, agents.c.name, agents.c.backend)
            .where(or_(agents.c.name == requested, agents.c.normalized_name == normalized))
            .limit(1)
        ).mappings().one_or_none()
        if catalog_agent is not None:
            catalog_backend = str(catalog_agent["backend"] or "").strip()
            if catalog_backend not in _BACKEND_AGENT_NAMES:
                return ("unknown", None, None)
            return (catalog_backend, str(catalog_agent["id"]), str(catalog_agent["name"]))
    backend = _agent_backend(requested)
    if backend != "unknown":
        return (backend, None, None)
    return ("unknown", None, None)


def _agent_session_name_predicate(agent_name: str) -> Any:
    requested = str(agent_name) or "default"
    backend = _agent_backend(requested)
    if backend != "unknown":
        return (agent_sessions.c.agent_backend == backend) | (agent_sessions.c.agent_variant == requested)
    return agent_sessions.c.agent_variant == requested


def _delete_agent_session_rows(
    conn: Connection,
    id_query: Any,
    *,
    reclaim_mode: ReclaimMode,
    reclaim_reason: str | None,
) -> int:
    """Remove Session rows from active routing, preserving retained history.

    Empty rows are deleted. A row with Message or Delivery history is archived
    and re-anchored instead, so ``/new`` frees the original anchor without
    orphaning immutable communication or execution audit.

    ``id_query`` is re-asserted BY THE DELETE, so a row that stopped matching after
    the id read is kept, and the returned count names only the rows actually removed.
    """

    session_ids = [str(row) for row in conn.execute(id_query).scalars().all()]
    if not session_ids:
        return 0
    deleted = 0
    for session_id in session_ids:
        # The id read above reserves nothing -- pysqlite emits no ``BEGIN`` for a bare
        # SELECT and ``resolve_scope_from_legacy_key`` does not write for a resolved
        # 2-part scope key -- so every predicate ``id_query`` carries was evaluated
        # before the write lock existed. The one that matters most is the
        # ``include_superseded=False`` guard in ``delete_agent_sessions``: superseding
        # PROMISES the row is kept (its native id is write-once and its transcript is not
        # recoverable), and a supersede committed inside the window left this a hard
        # delete of exactly the row that guard exists to protect, because the id was read
        # while the anchor was still bare. Re-running ``id_query`` inside the DELETE
        # re-evaluates every one of those predicates with the lock held, whichever caller
        # built them.
        #
        # THE RECLAIM IS NOT ROLLED BACK when the delete is refused, and that is a
        # deliberate choice rather than an oversight. It has to run first (it needs both
        # rows visible for the settings snapshot), so undoing it would mean wrapping both
        # statements in a SAVEPOINT -- and under WAL that makes things WORSE, not better:
        # the SAVEPOINT opens the SQLite transaction before the reclaim's own SELECT, so
        # the read pins a snapshot, and the reclaim's UPDATE then fails outright with
        # ``SQLITE_BUSY_SNAPSHOT`` ("database is locked") on exactly the interleaving this
        # whole guard exists to survive. Measured, not assumed. Leaving the reclaim in
        # place is also the recoverable half: the definitions were owned by the session
        # the user asked to clear, ``pause`` keeps them re-enablable, and the kept row is
        # a superseded one the thread has already moved off. Owner-only Tasks briefly
        # receive the same orphan marker as a successful teardown; the refused-claim
        # branch removes it again once it proves the owner row is still live.
        reclaim_bound_definitions(conn, session_id, mode=reclaim_mode, reason=reclaim_reason)
        claimed = bool(
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session_id)
                .where(agent_sessions.c.id.in_(id_query))
                .values(updated_at=agent_sessions.c.updated_at)
            ).rowcount
        )
        if not claimed:
            if reclaim_mode == RECLAIM_PAUSE:
                from storage.background import clear_task_resume_blocks_for_available_owner

                clear_task_resume_blocks_for_available_owner(conn, session_id)
            logger.warning(
                "Skipped hard-deleting session %s: it stopped matching the teardown "
                "query concurrently (superseded, re-anchored or already gone)",
                session_id,
            )
            continue
        now = _utc_now_iso()
        # BEFORE the history branch, because both halves end this Session's ability to
        # run anything: the archival half cancels its queued runs outright, and the
        # delete half removes the row they are bound to. A queued command-task
        # escalation suppressed its parent's failure notice on the promise that the turn
        # would carry the report, so either way that report is now impossible and the
        # failure has to fall back to the notice ladder -- which does not need the
        # Session, since a notice is delivered to the scope. Same reason as the archive
        # path in ``workbench_sessions_service``, and in this same transaction.
        #
        # The delete half used to be the quieter of the two: it cancelled nothing, so
        # the escalation was left queued against a row that no longer exists and no
        # cancel-shaped guard could ever see it. The cancel below closes that.
        from storage.background import (
            TEARDOWN_CONDEMNED_RUN_STATUSES,
            _defer_run_ids_updated_from_connection,
            rearm_notices_for_escalations_canceled_with_session,
        )

        rearm_notices_for_escalations_canceled_with_session(conn, session_id, now=now)
        condemned_run_ids = list(
            conn.execute(
                select(agent_runs.c.id)
                .where(agent_runs.c.session_id == session_id)
                .where(agent_runs.c.status.in_(TEARDOWN_CONDEMNED_RUN_STATUSES))
            ).scalars()
        )
        # Also before the branch, and for the same reason. ``agent_runs.session_id``
        # carries no foreign key, so deleting the Session row leaves its runs ``queued``
        # and claimable against a Session that is gone -- and for an escalation that is
        # not merely untidy: the re-arm just above handed the failure to the notice
        # ladder on the grounds that the turn can never run, so a turn that IS claimed
        # and then dies on the missing Session reports the same failure twice, the
        # second time from the lane meant to replace the first. Terminalizing here is
        # what makes that premise true. In-flight runs are cancel-requested only; the
        # executor owns the transition for work it has already claimed.
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.session_id == session_id)
            .where(agent_runs.c.status.in_(TEARDOWN_CONDEMNED_RUN_STATUSES))
            .values(cancel_requested=1, cancel_requested_at=now, updated_at=now)
        )
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.session_id == session_id)
            .where(agent_runs.c.status.in_(("pending", "queued")))
            .values(status="canceled", completed_at=now, updated_at=now)
        )
        _defer_run_ids_updated_from_connection(conn, condemned_run_ids)
        has_retained_history = bool(
            conn.execute(
                select(messages.c.id)
                .where(messages.c.session_id == session_id)
                .limit(1)
            ).first()
            or conn.execute(
                select(message_deliveries.c.id)
                .where(message_deliveries.c.session_id == session_id)
                .limit(1)
            ).first()
        )
        if has_retained_history:
            row = conn.execute(
                select(agent_sessions.c.session_anchor).where(
                    agent_sessions.c.id == session_id
                )
            ).first()
            current_anchor = str((row or ("",))[0] or "")
            superseded_anchor = (
                current_anchor
                if SUPERSEDED_ANCHOR_INFIX in current_anchor
                else (
                    f"{current_anchor}{SUPERSEDED_ANCHOR_INFIX}{session_id}"
                    if current_anchor
                    else f"superseded:{session_id}"
                )
            )
            retire_session_delivery_owners(conn, session_id)
            delivery_store.set_draft(conn, session_id, None)
            retained = conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == session_id)
                .values(
                    status="archived",
                    agent_status="idle",
                    session_anchor=superseded_anchor,
                    updated_at=now,
                )
            )
            if retained.rowcount != 1:
                raise RuntimeError(
                    f"claimed Session {session_id} disappeared during archival"
                )
            deleted += 1
            continue
        removed = bool(
            conn.execute(
                agent_sessions.delete().where(agent_sessions.c.id == session_id)
            ).rowcount
        )
        if not removed:
            raise RuntimeError(f"claimed Session {session_id} disappeared during teardown")
        deleted += 1
    return deleted


def _new_session_id(used: set[str]) -> str:
    while True:
        value = "ses" + "".join(secrets.choice(SESSION_ID_ALPHABET) for _ in range(10))
        if value not in used:
            used.add(value)
            return value


def _find_agent_session_row_id(
    conn: Connection,
    *,
    scope_id: str | None,
    agent_name: str,
    session_anchor: str,
) -> str | None:
    requested = str(agent_name) or "default"
    backend = _agent_backend(requested)
    base_query = (
        select(agent_sessions.c.id)
        .where(agent_sessions.c.scope_id == scope_id)
        .where(agent_sessions.c.session_anchor == str(session_anchor))
        # Never re-bind onto an archived row. ``bind_agent_session`` flips a found
        # row back to ``status='active'``; skipping archived rows here forces a
        # fresh session for the thread instead of resurrecting an archived one.
        .where(agent_sessions.c.status != "archived")
    )
    if backend != "unknown":
        row_id = conn.execute(
            base_query.where(agent_sessions.c.agent_backend == backend)
            .order_by(
                case(
                    (agent_sessions.c.agent_variant.notin_(sorted(_ROUTING_SENTINEL_VARIANTS)), 0),
                    else_=1,
                ),
                agent_sessions.c.last_active_at.desc(),
                agent_sessions.c.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if row_id:
            return row_id
        legacy_row_id = conn.execute(
            base_query.where(agent_sessions.c.agent_backend.in_(["", "default"]))
            .where(agent_sessions.c.agent_variant.in_(["", "default"]))
            .order_by(agent_sessions.c.last_active_at.desc(), agent_sessions.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if legacy_row_id:
            # The predicates RE-ASSERT what the SELECT above decided, because that read
            # reserves nothing: pysqlite emits no ``BEGIN`` for a bare SELECT, so the
            # write lock is taken here, at the first DML. Both callers reach this
            # function through ``resolve_scope_from_legacy_key``, which for the 2-part
            # key form (``slack::C1``) that ``build_context_session_key`` emits for
            # ordinary channel / thread turns returns after a pure SELECT -- so the
            # window is open, exactly as in the sibling writers (HFR-251..254).
            #
            # A bare ``id`` match had the HFR-253 shape twice over. It relabelled a row
            # whose placeholder backend a concurrent claim had already filled with a
            # CONCRETE backend -- so the row came out labelled with this caller's
            # backend while holding the winner's native id -- and it then RETURNED that
            # id: ``ensure_agent_session_id`` hands its answer back unchanged, and
            # ``BaseAgent.ensure_agent_session_id`` pins any non-empty id into
            # ``context.platform_specific['agent_session_id']`` without ever
            # re-resolving, so an archive committed inside the window (terminal, and
            # filtered out by every read here, ``base_query`` included) was handed to the
            # turn as its session.
            relabelled = conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == legacy_row_id)
                # Still live: an archive vacates the anchor and is terminal.
                .where(agent_sessions.c.status != "archived")
                # Still a placeholder. That is the whole justification for relabelling
                # in place (a blank / "default" label names no previous backend, so
                # nothing on the row can belong to a different one); once another claim
                # has filled it, the justification is gone.
                .where(agent_sessions.c.agent_backend.in_(["", "default"]))
                .where(agent_sessions.c.agent_variant.in_(["", "default"]))
                .values(agent_backend=backend, agent_variant=backend)
            )
            if relabelled.rowcount:
                return legacy_row_id
            # LOST the race: answer "no row for this (scope, anchor, backend)", which is
            # what this function returns when the read finds nothing at all. The two
            # callers then resolve the anchor through
            # ``get_or_create_agent_session_row``, whose reads exclude archived rows and
            # whose writes are individually guarded -- so the outcome converges with the
            # serial order (winner first, then this caller) instead of this caller
            # deciding a second time from the snapshot it was just refused for.
            logger.warning(
                "Lost the placeholder-relabel race for session %s; re-resolving the "
                "anchor instead (requested backend=%s)",
                legacy_row_id,
                backend,
            )
            return None
        return None
    return conn.execute(
        base_query.where(agent_sessions.c.agent_variant == requested).limit(1)
    ).scalar_one_or_none()


def _find_row_id_for_scope_anchor(
    conn: Connection,
    *,
    scope_id: str | None,
    session_anchor: str,
) -> str | None:
    """Latest row id for ``(scope_id, session_anchor)`` regardless of backend.

    The dedup key for the new ``(scope, anchor)`` unique invariant. The
    variant-filtered ``_find_agent_session_row_id`` is wrong for the import path:
    two legacy backends under one thread (``claude`` + ``codex`` at the same bare
    anchor) would each miss and INSERT, colliding on the unique index. Matching by
    (scope, anchor) only lets the import collapse them onto one row instead."""
    return conn.execute(
        select(agent_sessions.c.id)
        .where(agent_sessions.c.scope_id == scope_id)
        .where(agent_sessions.c.session_anchor == str(session_anchor))
        .order_by(agent_sessions.c.last_active_at.desc(), agent_sessions.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _find_scope_anchor_row(
    conn: Connection,
    *,
    scope_id: str | None,
    session_anchor: str,
) -> Mapping[str, Any] | None:
    return (
        conn.execute(
            select(
                agent_sessions.c.id,
                agent_sessions.c.agent_backend,
                agent_sessions.c.agent_variant,
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.native_session_id,
                agent_sessions.c.model,
                agent_sessions.c.reasoning_effort,
                agent_sessions.c.metadata_json,
                agent_sessions.c.status,
            )
            .where(agent_sessions.c.scope_id == scope_id)
            .where(agent_sessions.c.session_anchor == str(session_anchor))
            .order_by(agent_sessions.c.last_active_at.desc(), agent_sessions.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


def _runtime_record_values(
    *,
    record_type: str,
    record_key: str,
    scope_id: str | None,
    session_anchor: str | None,
    workdir: str | None,
    payload: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    return {
        "id": f"runtime::{record_type}::{record_key}",
        "record_type": record_type,
        "record_key": record_key,
        "scope_id": scope_id,
        "session_anchor": session_anchor,
        "workdir": workdir,
        "payload_json": _json_dumps(payload),
        "expires_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _processed_message_record_key(channel_id: str, thread_id: str, message_id: str) -> str:
    return "|".join((str(channel_id), str(thread_id), str(message_id)))


def _processed_message_like_prefix(channel_id: str, thread_id: str) -> str:
    prefix = _processed_message_record_key(channel_id, thread_id, "")
    return f"{_escape_sql_like(prefix)}%"


def _escape_sql_like(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _prune_processed_message_records(
    conn: Connection,
    *,
    channel_id: str,
    thread_id: str,
    retained_record_keys: set[str] | None = None,
) -> None:
    retained = set(retained_record_keys or [])
    prefix_pattern = _processed_message_like_prefix(channel_id, thread_id)
    if retained:
        conn.execute(
            runtime_records.delete()
            .where(runtime_records.c.record_type == "processed_message")
            .where(runtime_records.c.record_key.like(prefix_pattern, escape="\\"))
            .where(runtime_records.c.record_key.not_in(retained))
        )
        return

    rows = conn.execute(
        select(runtime_records.c.record_key)
        .where(runtime_records.c.record_type == "processed_message")
        .where(runtime_records.c.record_key.like(prefix_pattern, escape="\\"))
        .order_by(runtime_records.c.created_at.desc(), _RUNTIME_RECORD_ROWID.desc())
        .offset(200)
    ).all()
    old_record_keys = [row[0] for row in rows]
    if not old_record_keys:
        return
    conn.execute(
        runtime_records.delete()
        .where(runtime_records.c.record_type == "processed_message")
        .where(runtime_records.c.record_key.in_(old_record_keys))
    )


def _upsert_runtime_record(conn: Connection, values: dict[str, Any], *, update_created_at: bool = False) -> None:
    stmt = sqlite_insert(runtime_records).values(**values)
    set_values = {
        "scope_id": stmt.excluded.scope_id,
        "session_anchor": stmt.excluded.session_anchor,
        "workdir": stmt.excluded.workdir,
        "payload_json": stmt.excluded.payload_json,
        "expires_at": stmt.excluded.expires_at,
        "updated_at": stmt.excluded.updated_at,
    }
    if update_created_at:
        set_values["created_at"] = stmt.excluded.created_at
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[runtime_records.c.record_type, runtime_records.c.record_key],
            set_=set_values,
        )
    )


def _session_row_key(
    *,
    scope_id: str | None,
    agent_variant: str,
    session_anchor: str,
    native_session_id: str,
) -> tuple[str | None, str, str, str]:
    return (scope_id, agent_variant, session_anchor, native_session_id)


# An ABSOLUTE cwd suffix: POSIX ``/...``, Windows drive ``C:\`` / ``C:/``, or UNC
# ``\\...``. OpenCode's cwd is always absolute (``get_cwd`` -> ``os.path.abspath``),
# so this cleanly separates a cwd composite from a claude/codex subagent name.
_ABS_CWD_PREFIX = re.compile(r"(/|[A-Za-z]:[\\/]|\\\\)")


def _base_session_anchor(anchor: str) -> str:
    """Strip an OpenCode ``base:<abs-cwd>`` suffix back to the bare base anchor.

    The anchor is the bare thread identity. Split on the FIRST ``:`` and drop the
    suffix iff it is an absolute path — POSIX ``/...``, Windows ``C:\\...`` /
    ``C:/...``, or UNC ``\\\\...``. A non-path suffix is a claude/codex subagent
    name (``base:reviewer``) and is preserved. Splitting on the first colon also
    collapses a double-nested cwd (``base:/p:/p``) in one pass and tolerates the
    drive-letter colon in Windows paths (which a last-colon split would mangle
    into ``base:C``).

    The Python twin of the alembic ``session_anchor`` strip (migration
    20260601_0011) for the legacy-JSON import path: ``ensure_sqlite_state`` runs
    migrations on an empty table and only then imports ``sessions.json``, so the
    import writer must normalise legacy rows itself or it persists composite
    anchors the bare-anchor read path can't find."""
    base, sep, suffix = str(anchor).partition(":")
    if sep and base and _ABS_CWD_PREFIX.match(suffix):
        return base
    return str(anchor)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_default_workdir() -> str | None:
    try:
        config = V2Config.load()
        return normalize_workdir(config.runtime.default_cwd)
    except FileNotFoundError:
        return normalize_workdir(Path.home() / "work")
    except Exception:
        logger.debug("Unable to load runtime default workdir", exc_info=True)
        return None


def _new_session_workdir(conn: Connection, scope_id: str | None, explicit_workdir: str | None) -> str | None:
    return normalize_workdir(explicit_workdir) or snapshot_scope_workdir(conn, scope_id) or _runtime_default_workdir()

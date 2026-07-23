from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from config import paths
from config.v2_config import V2Config
from config.v2_sessions import ActivePollInfo, SessionState
from config.v2_settings import _split_scoped_key
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.agent_session_rows import (
    create_agent_session_row,
    decode_session_value,
    encode_session_value,
    new_session_id,
    normalize_workdir,
    snapshot_scope_workdir,
)
from storage.models import agent_sessions, metadata, runtime_records, scopes, state_meta
from storage.settings_service import make_scope_id, upsert_scope

SESSIONS_LAST_ACTIVITY_KEY = "sessions_last_activity"
SESSION_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

logger = logging.getLogger(__name__)


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
                        scopes.c.platform,
                    )
                    .select_from(
                        agent_sessions.join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)
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
        agent_name = str(row["agent_name"] or "").strip()
        backend = str(row["agent_backend"] or "").strip()
        agent = agent_name or _BACKEND_LABELS.get(backend, backend or None)
        meta[str(row["id"])] = {"title": title, "platform": platform, "agent": agent}
    return meta


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
    ) -> str | None:
        now = _utc_now_iso()
        backend = str(agent_backend or "default")
        with self.engine.begin() as conn:
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
    ) -> str:
        """Reserve a session with no Scope and its own lazy Show workspace."""
        now = _utc_now_iso()
        backend = str(agent_backend or "default")
        with self.engine.begin() as conn:
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
            return create_agent_session_row(
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
                return create_agent_session_row(
                    conn,
                    scope_id=scope_id,
                    agent_backend=_agent_backend(str(agent_name)),
                    agent_variant=str(agent_name) or "default",
                    session_anchor=session_anchor,
                    native_session_id=encoded_session_id,
                    workdir=requested_workdir,
                    agent_id=vibe_agent_id,
                    agent_name=vibe_agent_name,
                    model=None,
                    reasoning_effort=None,
                    metadata={"legacy_scope_key": str(scope_key)},
                    now=now,
                    require_workdir=False,
                )
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
            # WRITE-ONCE: a row's native_session_id is bound exactly once and never
            # changed. Set it only when the row has none yet; never let a recapture,
            # fork, subagent, or any fallback overwrite an existing native (product
            # invariant — one agent session ↔ one fixed native).
            if _set_native_once(conn, row_id, encoded_session_id):
                values["native_session_id"] = encoded_session_id
            if vibe_agent_id is not None:
                values["agent_id"] = vibe_agent_id
            if vibe_agent_name is not None:
                values["agent_name"] = vibe_agent_name
            conn.execute(agent_sessions.update().where(agent_sessions.c.id == row_id).values(**values))
            return row_id

    def materialize_agent_session_route(
        self,
        session_id: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> bool:
        """Pin the model / effort a turn is about to run with into EMPTY columns.

        A session created on an inherited default carries NULLs (dispatch
        resolves the live Agent default); the first turn pins the resolved
        values — same lifecycle as the backend pin on native bind. Called at
        dispatch time (turn START), so a user's later explicit header pick —
        including an explicit clear back to NULL — happens after this write and
        is never undone by it. COALESCE keeps the fill-if-empty atomic against
        a concurrent pick. Returns True when a row was updated."""
        values: dict[str, Any] = {}
        if model:
            values["model"] = func.coalesce(func.nullif(agent_sessions.c.model, ""), model)
        if reasoning_effort:
            values["reasoning_effort"] = func.coalesce(
                func.nullif(agent_sessions.c.reasoning_effort, ""), reasoning_effort
            )
        if not values:
            return False
        values["updated_at"] = _utc_now_iso()
        with self.engine.begin() as conn:
            result = conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == str(session_id))
                .where(agent_sessions.c.status != "archived")
                .values(**values)
            )
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
            values["agent_name"] = vibe_agent_name
        if vibe_agent_backend is not None:
            values["agent_backend"] = vibe_agent_backend or ""
            values["agent_variant"] = vibe_agent_backend or "default"
        with self.engine.begin() as conn:
            # Never resurrect an archived (terminal) session. ``bind_agent_session_by_id``
            # targets an explicit row, bypassing the ``status != 'archived'`` lookup
            # guards — and a turn that was still finishing when the session was
            # archived (the cancel is now best-effort/background) can land a late
            # native-id bind here. Refuse it so the terminal archive sticks.
            current_status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == str(session_id))
            ).scalar_one_or_none()
            if current_status == "archived":
                return None
            if workdir is not None:
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
            # WRITE-ONCE: bind the native only if the row has none yet; never let a
            # recapture / fork / subagent overwrite an existing native (see
            # ``_set_native_once`` + bind_agent_session).
            if _set_native_once(conn, str(session_id), encoded_session_id):
                values["native_session_id"] = encoded_session_id
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
    ) -> bool:
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is None:
                return False
            result = conn.execute(
                agent_sessions.delete()
                .where(agent_sessions.c.scope_id == scope_id)
                .where(_agent_session_name_predicate(str(agent_name) or "default"))
                .where(agent_sessions.c.session_anchor == str(session_anchor))
            )
            return bool(result.rowcount)

    def delete_agent_sessions(
        self,
        *,
        scope_key: str,
        agent_name: str | None = None,
        session_anchor_prefix: str | None = None,
    ) -> int:
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            scope_id = resolve_scope_from_legacy_key(conn, str(scope_key), now=now)
            if scope_id is None:
                return 0
            stmt = agent_sessions.delete().where(agent_sessions.c.scope_id == scope_id)
            if agent_name is not None:
                stmt = stmt.where(_agent_session_name_predicate(str(agent_name) or "default"))
            if session_anchor_prefix is not None:
                prefix = str(session_anchor_prefix)
                prefix_pattern = f"{_escape_sql_like(prefix)}:%"
                stmt = stmt.where(
                    (agent_sessions.c.session_anchor == prefix)
                    | (agent_sessions.c.session_anchor.like(prefix_pattern, escape="\\"))
                )
            result = conn.execute(stmt)
            return int(result.rowcount or 0)

    def load_state(self) -> SessionState:
        with self.engine.connect() as conn:
            return SessionState(
                session_mappings=self._load_session_mappings(conn),
                active_slack_threads=self._load_active_threads(conn),
                active_polls=self._load_active_polls(conn),
                processed_message_ts=self._load_processed_messages(conn),
                last_activity=self._load_last_activity(conn),
            )

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
                for agent_name, thread_map in agent_maps.items():
                    if not isinstance(thread_map, dict):
                        continue
                    for thread_id, native_session_id in thread_map.items():
                        thread_key = str(thread_id)
                        # Normalise OpenCode ``base:/cwd`` composites to the bare
                        # anchor so imported rows match the bare-anchor read path;
                        # subagent ``base:<name>`` anchors are preserved. Workdir is
                        # snapshotted from scope settings, never inferred from the
                        # legacy anchor suffix.
                        base_anchor = _base_session_anchor(thread_key)
                        dedup_key = (scope_id, base_anchor)
                        if dedup_key in seen_anchor_rows:
                            continue
                        encoded_session_id = encode_session_value(native_session_id)
                        row_key = _session_row_key(
                            scope_id=scope_id,
                            agent_variant=str(agent_name) or "default",
                            session_anchor=base_anchor,
                            native_session_id=encoded_session_id,
                        )
                        row_id = (
                            _find_row_id_for_scope_anchor(
                                conn,
                                scope_id=scope_id,
                                session_anchor=base_anchor,
                            )
                            or existing_session_ids.get(row_key)
                            or _new_session_id(used_session_ids)
                        )
                        seen_anchor_rows[dedup_key] = row_id
                        stmt = sqlite_insert(agent_sessions).values(
                            id=row_id,
                            scope_id=scope_id,
                            agent_backend=_agent_backend(str(agent_name)),
                            agent_variant=str(agent_name) or "default",
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
                                    "agent_backend": stmt.excluded.agent_backend,
                                    "agent_variant": stmt.excluded.agent_variant,
                                    "session_anchor": stmt.excluded.session_anchor,
                                    "native_session_id": stmt.excluded.native_session_id,
                                    "status": stmt.excluded.status,
                                    "metadata_json": stmt.excluded.metadata_json,
                                    "updated_at": stmt.excluded.updated_at,
                                    "last_active_at": stmt.excluded.last_active_at,
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
            .order_by(runtime_records.c.created_at)
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


def _agent_session_name_predicate(agent_name: str) -> Any:
    requested = str(agent_name) or "default"
    backend = _agent_backend(requested)
    if backend != "unknown":
        return (agent_sessions.c.agent_backend == backend) | (agent_sessions.c.agent_variant == requested)
    return agent_sessions.c.agent_variant == requested


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
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == legacy_row_id)
                .values(agent_backend=backend, agent_variant=backend)
            )
            return legacy_row_id
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
        .order_by(runtime_records.c.created_at.desc())
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

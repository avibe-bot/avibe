"""Durable operational snapshots for Session Activities.

Activity is intentionally stored in the existing ``runtime_records`` aggregate:
these rows are restart/recovery state, not a new public schema or transcript.
The shared Activity registry owns lifecycle semantics and keeps this adapter
limited to structured snapshot persistence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from storage.db import retry_sqlite_write
from storage.models import runtime_records


ACTIVITY_RECORD_TYPE = "session_activity"
CONNECTION_RECORD_TYPE = "session_activity_connection"
ACTIVE_PHASE = "active"
AWAITING_OUTPUT_PHASE = "awaiting_output"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _record_key(*parts: str) -> str:
    identity = _json_dumps([str(part) for part in parts])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _payload(row: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class SQLiteSessionActivityStore:
    """Persist current Activity/connection facts without defining a new table."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def activity_key(*, backend: str, runtime_key: str, activity_id: str) -> str:
        return _record_key(backend, runtime_key, activity_id)

    @staticmethod
    def connection_key(*, backend: str, runtime_key: str) -> str:
        return _record_key(backend, runtime_key)

    def _activity_values(self, activity: dict[str, Any], *, phase: str) -> dict[str, Any]:
        backend = str(activity.get("backend") or "")
        runtime_key = str(activity.get("runtime_key") or "")
        activity_id = str(activity.get("id") or "")
        if not backend or not runtime_key or not activity_id:
            raise ValueError("Activity persistence requires backend, runtime_key, and id")
        now = _utc_now_iso()
        record_key = self.activity_key(
            backend=backend,
            runtime_key=runtime_key,
            activity_id=activity_id,
        )
        return {
            "id": f"runtime::{ACTIVITY_RECORD_TYPE}::{record_key}",
            "record_type": ACTIVITY_RECORD_TYPE,
            "record_key": record_key,
            "scope_id": None,
            "session_anchor": activity.get("session_id"),
            "workdir": None,
            "payload_json": _json_dumps(
                {
                    "version": 1,
                    "phase": phase,
                    "activity": activity,
                }
            ),
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }

    def _activity_upsert(self, activity: dict[str, Any], *, phase: str):
        values = self._activity_values(activity, phase=phase)
        stmt = sqlite_insert(runtime_records).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=[runtime_records.c.record_type, runtime_records.c.record_key],
            set_={
                "session_anchor": stmt.excluded.session_anchor,
                "payload_json": stmt.excluded.payload_json,
                "updated_at": stmt.excluded.updated_at,
            },
        )

    def upsert_activity(self, activity: dict[str, Any], *, phase: str) -> None:
        stmt = self._activity_upsert(activity, phase=phase)
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def commit_activity_completion(
        self,
        *,
        expected: dict[str, Any],
        expected_phase: str,
        replacement: dict[str, Any] | None,
        phase: str | None,
    ) -> bool:
        """CAS one frozen completion/compensation, on a worker only.

        Unlike the synchronous lifecycle methods, this operation retries a whole
        transaction. It never inserts a missing owner: teardown or replacement
        wins over a delayed completion. Compensation compares the exact receipt
        (including its unique completion token), so it cannot undo a later write.
        """

        record_key = self.activity_key(
            backend=expected["backend"],
            runtime_key=expected["runtime_key"],
            activity_id=expected["id"],
        )
        predicate = (
            (runtime_records.c.record_type == ACTIVITY_RECORD_TYPE)
            & (runtime_records.c.record_key == record_key)
            & (runtime_records.c.payload_json == _json_dumps({
                "version": 1, "phase": expected_phase, "activity": expected,
            }))
        )
        if replacement is None:
            stmt = delete(runtime_records).where(predicate)
        else:
            if phase is None:
                raise ValueError("Activity completion replacement requires a phase")
            stmt = update(runtime_records).where(predicate).values(
                session_anchor=replacement.get("session_id"),
                payload_json=_json_dumps({
                    "version": 1, "phase": phase, "activity": replacement,
                }),
                updated_at=_utc_now_iso(),
            )

        def commit() -> bool:
            with self.engine.begin() as conn:
                return conn.execute(stmt).rowcount == 1

        return retry_sqlite_write(commit)

    def restore_activity_if_missing(
        self,
        activity: dict[str, Any],
        *,
        phase: str,
    ) -> bool:
        """Restore a frozen terminal snapshot only when its row is absent."""

        stmt = sqlite_insert(runtime_records).values(
            **self._activity_values(activity, phase=phase),
        ).on_conflict_do_nothing(
            index_elements=[runtime_records.c.record_type, runtime_records.c.record_key],
        )

        def restore() -> bool:
            with self.engine.begin() as conn:
                return conn.execute(stmt).rowcount == 1

        return retry_sqlite_write(restore)

    def upsert_activities(
        self,
        activities: list[dict[str, Any]],
        *,
        phase: str,
    ) -> None:
        """Persist one concrete output batch in a single transaction."""

        statements = [
            self._activity_upsert(activity, phase=phase)
            for activity in activities
        ]
        with self.engine.begin() as conn:
            for stmt in statements:
                conn.execute(stmt)

    def delete_activity(self, *, backend: str, runtime_key: str, activity_id: str) -> None:
        record_key = self.activity_key(
            backend=backend,
            runtime_key=runtime_key,
            activity_id=activity_id,
        )
        with self.engine.begin() as conn:
            conn.execute(
                delete(runtime_records)
                .where(runtime_records.c.record_type == ACTIVITY_RECORD_TYPE)
                .where(runtime_records.c.record_key == record_key)
            )

    def list_activities(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(runtime_records)
                    .where(runtime_records.c.record_type == ACTIVITY_RECORD_TYPE)
                    .order_by(runtime_records.c.created_at, runtime_records.c.id)
                ).mappings()
            )
        return [payload for row in rows if (payload := _payload(row)) is not None]

    def upsert_connection(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        state: str,
    ) -> None:
        now = _utc_now_iso()
        record_key = self.connection_key(backend=backend, runtime_key=runtime_key)
        values = {
            "id": f"runtime::{CONNECTION_RECORD_TYPE}::{record_key}",
            "record_type": CONNECTION_RECORD_TYPE,
            "record_key": record_key,
            "scope_id": None,
            "session_anchor": session_id,
            "workdir": None,
            "payload_json": _json_dumps(
                {
                    "version": 1,
                    "backend": str(backend),
                    "runtime_key": str(runtime_key),
                    "session_id": session_id,
                    "state": str(state),
                }
            ),
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }
        stmt = sqlite_insert(runtime_records).values(**values)
        with self.engine.begin() as conn:
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[runtime_records.c.record_type, runtime_records.c.record_key],
                    set_={
                        "session_anchor": stmt.excluded.session_anchor,
                        "payload_json": stmt.excluded.payload_json,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )

    def list_connections(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(runtime_records)
                    .where(runtime_records.c.record_type == CONNECTION_RECORD_TYPE)
                    .order_by(runtime_records.c.created_at, runtime_records.c.id)
                ).mappings()
            )
        return [payload for row in rows if (payload := _payload(row)) is not None]

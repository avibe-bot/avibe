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

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

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

    def upsert_activity(self, activity: dict[str, Any], *, phase: str) -> None:
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
        values = {
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

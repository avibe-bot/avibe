"""Durable follow-ups of a Vibey Turn recovery completed from its committed final.

Recovery settles such a Turn at startup, before external transports connect, and
two effects of that settlement run after it: the silent final's promotion of the
Turn's visible text (``promote``) and the removal of the 👀 the dead process left
on its IM message (``clear_reaction``). Each is recorded here before the Turn's
terminal write and removed once done, so another restart before they run loses
neither (``docs/plans/vibey-restart-auto-resume.md`` item 7). Rows live only while
pending, so a rescan reads exactly the outstanding ones.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import Connection

from storage.models import runtime_records

RECORD_TYPE = "vibey_restart_followup"
#: Failed attempts after which a follow-up is dropped: a platform reports a reaction
#: that is already gone the same way as a transient failure.
MAX_ATTEMPTS = 5


def _record_id(turn_id: str) -> str:
    return f"runtime::{RECORD_TYPE}::{turn_id}"


def save(conn: Connection, turn_id: str, followups: dict[str, Any]) -> None:
    """Record (or replace) a Turn's pending follow-ups; none removes the record."""
    conn.execute(delete(runtime_records).where(runtime_records.c.id == _record_id(turn_id)))
    if not followups:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        runtime_records.insert().values(
            id=_record_id(turn_id),
            record_type=RECORD_TYPE,
            record_key=turn_id,
            payload_json=json.dumps(followups, ensure_ascii=False, sort_keys=True),
            created_at=now,
            updated_at=now,
        )
    )


def pending(conn: Connection, turn_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Outstanding follow-ups by Turn: one Turn's, or every Turn's."""
    query = select(runtime_records.c.record_key, runtime_records.c.payload_json).where(
        runtime_records.c.record_type == RECORD_TYPE
    )
    if turn_id is not None:
        query = query.where(runtime_records.c.record_key == turn_id)
    found: dict[str, dict[str, Any]] = {}
    for row in conn.execute(query):
        try:
            payload = json.loads(row.payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        found[str(row.record_key)] = payload if isinstance(payload, dict) else {}
    return found


def done(conn: Connection, turn_id: str, key: str) -> None:
    """One follow-up of the Turn ran; the record goes with its last one."""
    payload = pending(conn, turn_id).get(turn_id)
    if payload is None:
        return
    payload.pop(key, None)
    payload.pop(f"{key}_attempts", None)
    save(conn, turn_id, payload if set(payload) - {"session_id"} else {})


def failed(conn: Connection, turn_id: str, key: str) -> bool:
    """One attempt at a follow-up failed: whether it is now dropped, after ``MAX_ATTEMPTS``."""
    payload = pending(conn, turn_id).get(turn_id)
    if payload is None or key not in payload:
        return True
    attempts = int(payload.get(f"{key}_attempts") or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        payload.pop(key, None)
        payload.pop(f"{key}_attempts", None)
        save(conn, turn_id, payload if set(payload) - {"session_id"} else {})
        return True
    payload[f"{key}_attempts"] = attempts
    save(conn, turn_id, payload)
    return False

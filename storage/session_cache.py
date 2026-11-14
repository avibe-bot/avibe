"""Transaction-local prefix state, owned by the originating transcript commit.

Never read a parent's mutable state. Archive/delete cleanup belongs to the
boundary migration's triggers so the released inline writer is safe too.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from core.agent_core.harness.cache_metrics import valid_record
from storage.compatibility import permits_compact_writes
from storage.models import agent_sessions, session_cache_state


def _upsert(conn: Connection, session_id: str, record: Any) -> None:
    """The caller holds SQLite's writer reservation before any reads."""
    if not valid_record(record) or record.get("input_tokens") is None:
        return
    if conn.execute(select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)).scalar_one_or_none() in (
        None, "archived",
    ):
        return
    key = dict(session_id=session_id, provider=record["provider"], model=record["model"])
    existing = conn.execute(select(session_cache_state.c.record_json).where(
        *(session_cache_state.c[name] == value for name, value in key.items())
    )).scalar_one_or_none()
    try:
        old = json.loads(existing) if existing is not None else None
    except (ValueError, TypeError):
        old = None
    if valid_record(old) and old.get("input_tokens") is not None:
        if old["started_at"] > record["started_at"]:
            return
        if old["started_at"] == record["started_at"] and "prefix" in old and "prefix" not in record:
            return
        if old == record:
            return
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stmt = insert(session_cache_state).values(**key, record_json=raw)
    conn.execute(stmt.on_conflict_do_update(
        index_elements=[session_cache_state.c[name] for name in key],
        set_={"record_json": raw},
    ))


def compact_payload(conn: Connection, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Store reusable prefixes and omit only repeated state from the new row.

    Carry the current audit's legacy route snapshot forward as well: on the
    first new audit after old-writer work, the bounded latest-audit reader will
    no longer see the previous audit. Dropping those routes without persisting
    them loses state. Timestamp ordering prevents a stale snapshot from winning.
    This consumes the new payload, never scans/backfills historical rows.
    """
    if not permits_compact_writes(conn):
        return payload
    compact = dict(payload)
    for record in payload.get("cache_state", ()) if isinstance(payload.get("cache_state"), list) else ():
        _upsert(conn, session_id, record)
    compact.pop("cache_state", None)

    def cache(value: Any) -> Any:
        if not valid_record(value):
            return value
        _upsert(conn, session_id, value)
        return {key: item for key, item in value.items() if key != "prefix"}

    if "cache" in payload:
        compact["cache"] = cache(payload["cache"])
    if isinstance(payload.get("cache_attempts"), list):
        compact["cache_attempts"] = [
            {**attempt, "cache": cache(attempt["cache"])}
            if isinstance(attempt, dict) and "cache" in attempt else attempt
            for attempt in payload["cache_attempts"]
        ]
    return compact

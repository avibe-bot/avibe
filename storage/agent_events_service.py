"""Append-only trace events emitted by agent runtimes.

``agent_events`` is intentionally separate from ``messages``: rows here are
debug/trace material, not transcript content. The first writer is tool-call
output from backend SDK streams, which should be retained for diagnosis without
ever becoming a chat/inbox message.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage.models import agent_events


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_event_id() -> str:
    return f"evt_{int(time.time() * 1_000_000):015x}{uuid.uuid4().hex[:8]}"


def _row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        content = json.loads(row.get("content_json") or "{}")
    except json.JSONDecodeError:
        content = {}
    try:
        metadata = json.loads(row.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": row["id"],
        "scope_id": row.get("scope_id"),
        "session_id": row.get("session_id"),
        "turn_id": row.get("turn_id"),
        "run_id": row.get("run_id"),
        "platform": row.get("platform"),
        "agent_name": row.get("agent_name"),
        "backend": row.get("backend"),
        "event_type": row.get("event_type"),
        "visibility": row.get("visibility"),
        "sequence": row.get("sequence"),
        "text": row.get("content_text") or content.get("text") or "",
        "content": content,
        "metadata": metadata,
        "source": row.get("source"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def append(
    conn: Connection,
    *,
    scope_id: Optional[str],
    session_id: Optional[str],
    platform: str,
    event_type: str,
    text: Optional[str] = None,
    content: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    agent_name: Optional[str] = None,
    backend: Optional[str] = None,
    turn_id: Optional[str] = None,
    run_id: Optional[str] = None,
    visibility: str = "trace",
    source: Optional[str] = "agent",
    sequence: Optional[int] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if content:
        body.update(content)
    if text is not None:
        body.setdefault("text", text)
    plain = text if text is not None else body.get("text") or None

    now = _utc_now_iso()
    payload = {
        "id": _new_event_id(),
        "scope_id": scope_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "platform": platform,
        "agent_name": agent_name,
        "backend": backend,
        "event_type": event_type,
        "visibility": visibility,
        "sequence": sequence,
        "content_text": plain,
        "content_json": json.dumps(body),
        "metadata_json": json.dumps(metadata or {}),
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(agent_events.insert().values(**payload))
    return _row_to_payload(payload)


def list_session_events(
    conn: Connection,
    *,
    session_id: str,
    event_types: Optional[tuple[str, ...]] = None,
    limit: Optional[int] = None,
    newest_first: bool = False,
) -> list[dict[str, Any]]:
    """Return this session's ``agent_events`` as payload dicts, oldest first.

    Backed by the ``ix_agent_events_session_created_id`` index. ``event_types``
    filters by ``event_type`` (e.g. ``("tool_call",)``). ``newest_first`` fetches
    the most-recent ``limit`` rows (then returns them oldest-first), so a bounded
    scan still yields the recent tail — the result order is always chronological.
    """
    stmt = select(agent_events).where(agent_events.c.session_id == session_id)
    if event_types:
        stmt = stmt.where(agent_events.c.event_type.in_(tuple(event_types)))
    if newest_first:
        stmt = stmt.order_by(agent_events.c.created_at.desc(), agent_events.c.id.desc())
    else:
        stmt = stmt.order_by(agent_events.c.created_at, agent_events.c.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = [_row_to_payload(dict(row)) for row in conn.execute(stmt).mappings().all()]
    if newest_first:
        rows.reverse()
    return rows

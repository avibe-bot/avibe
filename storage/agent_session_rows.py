from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, select

from storage.models import agent_sessions, scope_settings

SESSION_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
JSON_VALUE_PREFIX = "__json__:"
SESSION_VISIBILITIES = frozenset({"foreground", "background"})


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

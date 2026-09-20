from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from storage.models import state_meta

RUNTIME_SETTINGS_REVISION_KEY = "runtime_settings_revision"
RUNTIME_SETTINGS_SCOPE_TYPES = frozenset({"channel", "thread", "platform", "guild", "user"})


def read_runtime_settings_revision(conn: Connection) -> str | None:
    value = conn.execute(
        select(state_meta.c.value_json).where(state_meta.c.key == RUNTIME_SETTINGS_REVISION_KEY)
    ).scalar_one_or_none()
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    revision = payload.get("revision") if isinstance(payload, dict) else None
    return str(revision) if revision else None


def mark_runtime_settings_changed(conn: Connection) -> str:
    """Publish one settings-domain revision in the caller's transaction."""
    revision = uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    stmt = sqlite_insert(state_meta).values(
        key=RUNTIME_SETTINGS_REVISION_KEY,
        value_json=json.dumps({"revision": revision}, separators=(",", ":")),
        updated_at=now,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[state_meta.c.key],
            set_={"value_json": stmt.excluded.value_json, "updated_at": stmt.excluded.updated_at},
        )
    )
    return revision

"""Append-only, immutable Vault audit snapshot interning inside the audit commit."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage.agent_session_rows import reserve_write_lock
from storage.models import state_meta, vault_audit_access_snapshots

HIGH_WATER_KEY = "vault_audit_snapshot_high_water"
_MAX_ID = (1 << 63) - 1


def intern(conn: Connection, snapshot: dict[str, Any]) -> int:
    # SQLAlchemy's logical transaction alone doesn't pin SQLite reads. Reserve
    # before lookup; concurrent identical snapshots then converge on one ID.
    reserve_write_lock(conn)
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    existing = conn.execute(select(vault_audit_access_snapshots.c.id).where(
        vault_audit_access_snapshots.c.snapshot == raw
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    value = conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == HIGH_WATER_KEY)).scalar_one_or_none()
    try:
        high = json.loads(value) if value is not None else None
    except (ValueError, TypeError):
        high = None
    if type(high) is not int or not 0 <= high < _MAX_ID:
        raise ValueError("Vault audit snapshot allocator is missing, invalid or exhausted; refusing allocation")
    next_id = high + 1
    conn.execute(state_meta.update().where(state_meta.c.key == HIGH_WATER_KEY).values(value_json=str(next_id)))
    conn.execute(vault_audit_access_snapshots.insert().values(id=next_id, snapshot=raw))
    return next_id

"""Short-lived server-side storage for remote organization authorization claims."""

from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import select

from storage.db import get_cached_sqlite_engine
from storage.models import remote_access_authorizations


def store(
    *,
    reference: str,
    instance_id: str,
    subject: str,
    claims: Mapping[str, Any],
    expires_at: int,
    created_at: int,
) -> None:
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            remote_access_authorizations.delete().where(
                remote_access_authorizations.c.expires_at <= created_at
            )
        )
        conn.execute(
            remote_access_authorizations.insert().values(
                id=reference,
                instance_id=instance_id,
                subject=subject,
                claims_json=json.dumps(dict(claims), separators=(",", ":"), sort_keys=True),
                expires_at=expires_at,
                created_at=created_at,
            )
        )


def load(
    *,
    reference: str,
    instance_id: str,
    subject: str,
    now: int,
) -> dict[str, Any] | None:
    engine = get_cached_sqlite_engine()
    with engine.connect() as conn:
        value = conn.execute(
            select(remote_access_authorizations.c.claims_json)
            .where(remote_access_authorizations.c.id == reference)
            .where(remote_access_authorizations.c.instance_id == instance_id)
            .where(remote_access_authorizations.c.subject == subject)
            .where(remote_access_authorizations.c.expires_at > now)
        ).scalar_one_or_none()
    if not isinstance(value, str):
        return None
    try:
        claims = json.loads(value)
    except (TypeError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None

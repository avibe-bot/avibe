"""Durable current authorization plus legacy short-lived claims references."""

from __future__ import annotations

import json
import secrets
from typing import Any, Mapping

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

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


def _decode_claims(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        claims = json.loads(value)
    except (TypeError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def _record_from_row(row: Any) -> dict[str, Any] | None:
    claims = _decode_claims(row.claims_json)
    if claims is None:
        return None
    return {
        "id": row.id,
        "instance_id": row.instance_id,
        "subject": row.subject,
        "email": row.email,
        "scope_kind": row.scope_kind,
        "scope_ref": row.scope_ref,
        "authorization_state": row.authorization_state,
        "claims": claims,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "last_checked_at": row.last_checked_at,
        "updated_at": row.updated_at,
    }


def load_reference_record(
    *,
    reference: str,
    instance_id: str,
    subject: str,
    now: int,
) -> dict[str, Any] | None:
    engine = get_cached_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(remote_access_authorizations)
            .where(remote_access_authorizations.c.id == reference)
            .where(remote_access_authorizations.c.instance_id == instance_id)
            .where(remote_access_authorizations.c.subject == subject)
            .where(
                or_(
                    remote_access_authorizations.c.scope_kind.is_not(None),
                    remote_access_authorizations.c.expires_at > now,
                )
            )
        ).one_or_none()
    return _record_from_row(row) if row is not None else None


def load(
    *,
    reference: str,
    instance_id: str,
    subject: str,
    now: int,
) -> dict[str, Any] | None:
    record = load_reference_record(
        reference=reference,
        instance_id=instance_id,
        subject=subject,
        now=now,
    )
    return record["claims"] if record is not None else None


def load_scoped(
    *,
    instance_id: str,
    subject: str,
    scope_kind: str,
    scope_ref: str,
) -> dict[str, Any] | None:
    engine = get_cached_sqlite_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(remote_access_authorizations)
            .where(remote_access_authorizations.c.instance_id == instance_id)
            .where(remote_access_authorizations.c.subject == subject)
            .where(remote_access_authorizations.c.scope_kind == scope_kind)
            .where(remote_access_authorizations.c.scope_ref == scope_ref)
        ).one_or_none()
    return _record_from_row(row) if row is not None else None


def upsert_scoped(
    *,
    reference: str | None,
    instance_id: str,
    subject: str,
    email: str,
    scope_kind: str,
    scope_ref: str,
    authorization_state: str,
    claims: Mapping[str, Any],
    last_checked_at: int,
    updated_at: int,
) -> str:
    """Store one current Instance or Show Page context without expiring it."""

    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    reference = reference or secrets.token_urlsafe(24)
    values = {
        "id": reference,
        "instance_id": instance_id,
        "subject": subject,
        "email": email,
        "scope_kind": scope_kind,
        "scope_ref": scope_ref,
        "authorization_state": authorization_state,
        "claims_json": json.dumps(dict(claims), separators=(",", ":"), sort_keys=True),
        "expires_at": None,
        "created_at": updated_at,
        "last_checked_at": last_checked_at,
        "updated_at": updated_at,
    }
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        existing_scope_reference = conn.execute(
            select(remote_access_authorizations.c.id)
            .where(remote_access_authorizations.c.instance_id == instance_id)
            .where(remote_access_authorizations.c.subject == subject)
            .where(remote_access_authorizations.c.scope_kind == scope_kind)
            .where(remote_access_authorizations.c.scope_ref == scope_ref)
        ).scalar_one_or_none()
        if existing_scope_reference is not None:
            conn.execute(
                update(remote_access_authorizations)
                .where(remote_access_authorizations.c.id == existing_scope_reference)
                .values(
                    email=email,
                    authorization_state=authorization_state,
                    claims_json=values["claims_json"],
                    expires_at=None,
                    last_checked_at=last_checked_at,
                    updated_at=updated_at,
                )
            )
            return str(existing_scope_reference)

        # A released cookie may still point at a random-reference row. Promote
        # that exact row on its first successful refresh so the cookie remains
        # valid and the new durable scope does not collide with its primary key.
        legacy_reference = conn.execute(
            select(
                remote_access_authorizations.c.instance_id,
                remote_access_authorizations.c.subject,
                remote_access_authorizations.c.scope_kind,
            ).where(remote_access_authorizations.c.id == reference)
        ).one_or_none()
        if (
            legacy_reference is not None
            and legacy_reference.instance_id == instance_id
            and legacy_reference.subject == subject
            and legacy_reference.scope_kind is None
        ):
            conn.execute(
                update(remote_access_authorizations)
                .where(remote_access_authorizations.c.id == reference)
                .values(**{key: value for key, value in values.items() if key != "id"})
            )
            return reference
        if legacy_reference is not None:
            reference = secrets.token_urlsafe(24)
            values["id"] = reference

        statement = sqlite_insert(remote_access_authorizations).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                remote_access_authorizations.c.instance_id,
                remote_access_authorizations.c.subject,
                remote_access_authorizations.c.scope_kind,
                remote_access_authorizations.c.scope_ref,
            ],
            index_where=and_(
                remote_access_authorizations.c.scope_kind.is_not(None),
                remote_access_authorizations.c.scope_ref.is_not(None),
            ),
            set_={
                "email": email,
                "authorization_state": authorization_state,
                "claims_json": values["claims_json"],
                "expires_at": None,
                "last_checked_at": last_checked_at,
                "updated_at": updated_at,
            },
        )
        conn.execute(statement)
        stored_reference = conn.execute(
            select(remote_access_authorizations.c.id)
            .where(remote_access_authorizations.c.instance_id == instance_id)
            .where(remote_access_authorizations.c.subject == subject)
            .where(remote_access_authorizations.c.scope_kind == scope_kind)
            .where(remote_access_authorizations.c.scope_ref == scope_ref)
        ).scalar_one()
    return str(stored_reference)


def mark_matching_revision_checked(
    *,
    instance_id: str,
    authorization_revision: int,
    checked_at: int,
) -> int:
    """Extend outage grace only for contexts matching a fresh device watermark."""

    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                remote_access_authorizations.c.id,
                remote_access_authorizations.c.claims_json,
            )
            .where(remote_access_authorizations.c.instance_id == instance_id)
            .where(remote_access_authorizations.c.scope_kind.is_not(None))
            .where(remote_access_authorizations.c.authorization_state == "current")
        ).all()
        matching_ids = []
        for row in rows:
            claims = _decode_claims(row.claims_json)
            revision = claims.get("vibe_instance_authorization_revision") if claims else None
            if isinstance(revision, int) and not isinstance(revision, bool) and revision == authorization_revision:
                matching_ids.append(row.id)
        if not matching_ids:
            return 0
        result = conn.execute(
            update(remote_access_authorizations)
            .where(remote_access_authorizations.c.id.in_(matching_ids))
            .values(last_checked_at=checked_at, updated_at=checked_at)
        )
    return int(result.rowcount or 0)


def delete_for_instance(instance_id: str) -> int:
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        result = conn.execute(
            remote_access_authorizations.delete().where(
                remote_access_authorizations.c.instance_id == instance_id
            )
        )
    return int(result.rowcount or 0)

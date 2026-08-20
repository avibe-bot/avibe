"""Durable current authorization plus legacy short-lived claims references."""

from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from storage.db import get_cached_sqlite_engine
from storage.models import remote_access_authorizations, state_meta

INSTANCE_BINDING_STATE_META_KEY = "remote_access.instance_binding.v1"
INSTANCE_BINDING_STATE_RECONCILING = "reconciling"
INSTANCE_BINDING_STATE_READY = "ready"
INSTANCE_BINDING_STATE_INVALID = "invalid"
INSTANCE_BINDING_GENERATION_KEY = "vibe_instance_binding_generation"
INSTANCE_BINDING_LOCK_FILENAME = "remote-access-instance-binding.lock"


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


def _decode_binding_state(value: Any) -> dict[str, Any] | None:
    """Decode one state row, rejecting malformed values instead of guessing."""

    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            return None
        state = payload.get("state")
        if state not in {INSTANCE_BINDING_STATE_RECONCILING, INSTANCE_BINDING_STATE_READY}:
            return None
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            return None
        instance_kind = payload.get("instance_kind")
        if instance_kind not in {None, "personal", "organization"}:
            return None
        if state == INSTANCE_BINDING_STATE_READY and instance_kind is None:
            return None
        generation = int(payload.get("generation"))
        schema_version = int(payload.get("schema_version") or 1)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if generation <= 0 or schema_version <= 0:
        return None
    return {
        "schema_version": schema_version,
        "state": state,
        "instance_id": instance_id.strip(),
        "instance_kind": instance_kind,
        "generation": generation,
        "updated_at": payload.get("updated_at"),
    }


def _load_binding_state_from_connection(conn) -> dict[str, Any] | None:
    raw = conn.execute(
        select(state_meta.c.value_json).where(
            state_meta.c.key == INSTANCE_BINDING_STATE_META_KEY
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    decoded = _decode_binding_state(raw)
    if decoded is not None:
        return decoded
    # Keep corruption distinguishable from a never-initialized install. Any
    # caller that sees this value must fail closed until a transition repairs it.
    return {
        "schema_version": 0,
        "state": INSTANCE_BINDING_STATE_INVALID,
        "instance_id": None,
        "instance_kind": None,
        "generation": 0,
        "updated_at": None,
    }


def _ensure_sqlite_state() -> None:
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()


def _binding_file_lock():
    from config import paths
    from storage.lock import MigrationFileLock

    return MigrationFileLock(paths.get_state_dir() / INSTANCE_BINDING_LOCK_FILENAME)


@contextmanager
def instance_binding_lock() -> Iterator[None]:
    """Serialize binding transitions across controller and UI processes."""

    _ensure_sqlite_state()
    with _binding_file_lock():
        yield


def load_instance_binding_state(*, ensure: bool = True) -> dict[str, Any] | None:
    """Read the durable binding epoch shared by UI and controller processes."""

    if ensure:
        _ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    try:
        with engine.connect() as conn:
            return _load_binding_state_from_connection(conn)
    except OperationalError as exc:
        # A pre-migration process may not have created state_meta yet. Treat
        # that as a legacy no-row install; callers that need a durable write
        # use the default ``ensure=True`` path and will still fail closed on a
        # real storage error.
        if not ensure and "no such table" in str(exc).lower():
            return None
        raise


def _validate_transition_identity(
    *,
    instance_id: str,
    instance_kind: str | None,
) -> tuple[str, str | None]:
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValueError("instance_id_required")
    if instance_kind not in {None, "personal", "organization"}:
        raise ValueError("instance_kind_required")
    return instance_id.strip(), instance_kind


def _binding_payload(
    *,
    state: str,
    instance_id: str,
    instance_kind: str | None,
    generation: int,
) -> tuple[dict[str, Any], str]:
    now = str(int(time.time()))
    payload = {
        "schema_version": 1,
        "state": state,
        "instance_id": instance_id,
        "instance_kind": instance_kind,
        "generation": int(generation),
        "updated_at": now,
    }
    return payload, now


def _write_binding_state(conn, payload: Mapping[str, Any], updated_at: str) -> None:
    encoded = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
    statement = sqlite_insert(state_meta).values(
        key=INSTANCE_BINDING_STATE_META_KEY,
        value_json=encoded,
        updated_at=updated_at,
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=[state_meta.c.key],
            set_={"value_json": encoded, "updated_at": updated_at},
        )
    )


def _begin_instance_binding_transition_locked(
    conn,
    *,
    instance_id: str,
    instance_kind: str | None,
) -> dict[str, Any]:
    instance_id, instance_kind = _validate_transition_identity(
        instance_id=instance_id,
        instance_kind=instance_kind,
    )
    existing = _load_binding_state_from_connection(conn)
    if (
        existing is not None
        and existing["state"] == INSTANCE_BINDING_STATE_READY
        and existing["instance_id"] == instance_id
        and existing["instance_kind"] == instance_kind
    ):
        return {
            "generation": existing["generation"],
            "changed": False,
            "previous": existing,
            "state": INSTANCE_BINDING_STATE_READY,
        }
    if (
        existing is not None
        and existing["state"] == INSTANCE_BINDING_STATE_RECONCILING
        and existing["instance_id"] == instance_id
        and existing["instance_kind"] == instance_kind
    ):
        return {
            "generation": existing["generation"],
            "changed": True,
            "previous": existing,
            "state": INSTANCE_BINDING_STATE_RECONCILING,
        }
    generation = (int(existing["generation"]) if existing is not None else 0) + 1
    payload, now = _binding_payload(
        state=INSTANCE_BINDING_STATE_RECONCILING,
        instance_id=instance_id,
        instance_kind=instance_kind,
        generation=generation,
    )
    _write_binding_state(conn, payload, now)
    return {
        "generation": generation,
        "changed": True,
        "previous": existing,
        "state": INSTANCE_BINDING_STATE_RECONCILING,
    }


def begin_instance_binding_transition(
    *,
    instance_id: str,
    instance_kind: str | None,
) -> dict[str, Any]:
    """Commit a fail-closed ``reconciling`` epoch before derived work starts."""

    _ensure_sqlite_state()
    with _binding_file_lock():
        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            return _begin_instance_binding_transition_locked(
                conn,
                instance_id=instance_id,
                instance_kind=instance_kind,
            )


def _invalidate_instance_binding_authorizations_locked(
    conn,
    *,
    instance_id: str,
    previous_instance_id: str | None = None,
    preserve_show_page: bool = True,
    keep_reference_for_revalidation: bool = True,
) -> int:
    ids = {
        value.strip()
        for value in (instance_id, previous_instance_id or "")
        if isinstance(value, str) and value.strip()
    }
    if not ids:
        return 0
    predicate = remote_access_authorizations.c.instance_id.in_(ids)
    if preserve_show_page:
        predicate = and_(
            predicate,
            or_(
                remote_access_authorizations.c.scope_kind.is_(None),
                remote_access_authorizations.c.scope_kind != "show_page",
            ),
        )
    if keep_reference_for_revalidation:
        result = conn.execute(
            update(remote_access_authorizations)
            .where(predicate)
            .values(authorization_state="stale", updated_at=int(time.time()))
        )
    else:
        result = conn.execute(delete(remote_access_authorizations).where(predicate))
    return int(result.rowcount or 0)


def invalidate_instance_binding_authorizations(
    *,
    instance_id: str,
    previous_instance_id: str | None = None,
    preserve_show_page: bool = True,
    keep_reference_for_revalidation: bool = True,
) -> int:
    """Invalidate derived instance claims without touching exact Show Page grants."""

    _ensure_sqlite_state()
    with _binding_file_lock():
        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            return _invalidate_instance_binding_authorizations_locked(
                conn,
                instance_id=instance_id,
                previous_instance_id=previous_instance_id,
                preserve_show_page=preserve_show_page,
                keep_reference_for_revalidation=keep_reference_for_revalidation,
            )


def _complete_instance_binding_transition_locked(
    conn,
    *,
    instance_id: str,
    instance_kind: str,
    generation: int,
) -> bool:
    if instance_kind not in {"personal", "organization"}:
        return False
    existing = _load_binding_state_from_connection(conn)
    if (
        existing is None
        or existing["state"] != INSTANCE_BINDING_STATE_RECONCILING
        or existing["instance_id"] != instance_id
        or existing["instance_kind"] not in {None, instance_kind}
        or existing["generation"] != int(generation)
    ):
        return False
    payload, now = _binding_payload(
        state=INSTANCE_BINDING_STATE_READY,
        instance_id=instance_id,
        instance_kind=instance_kind,
        generation=generation,
    )
    _write_binding_state(conn, payload, now)
    return True


def complete_instance_binding_transition(
    *,
    instance_id: str,
    instance_kind: str,
    generation: int,
) -> bool:
    """Publish a reconciled binding epoch after all derived work succeeds."""

    _ensure_sqlite_state()
    with _binding_file_lock():
        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            return _complete_instance_binding_transition_locked(
                conn,
                instance_id=instance_id,
                instance_kind=instance_kind,
                generation=generation,
            )


def reconcile_instance_binding(
    *,
    instance_id: str,
    instance_kind: str | None,
    reconcile: Callable[[], Any] | None = None,
    previous_instance_id: str | None = None,
    preserve_show_page: bool = True,
) -> dict[str, Any]:
    """Run one serialized identity transition and publish ``ready`` last.

    A callback failure deliberately leaves the committed ``reconciling`` row
    in place. Callers must treat the returned ``ok=False`` as fail-closed and
    retry from the next heartbeat.
    """

    instance_id, instance_kind = _validate_transition_identity(
        instance_id=instance_id,
        instance_kind=instance_kind,
    )
    _ensure_sqlite_state()
    with _binding_file_lock():
        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            transition = _begin_instance_binding_transition_locked(
                conn,
                instance_id=instance_id,
                instance_kind=instance_kind,
            )
            previous = transition.get("previous")
            previous_id = previous.get("instance_id") if isinstance(previous, Mapping) else None
            if previous_instance_id and previous_instance_id != instance_id:
                previous_id = previous_instance_id
            preserve = preserve_show_page and previous_id in {None, instance_id}
            invalidated = _invalidate_instance_binding_authorizations_locked(
                conn,
                instance_id=instance_id,
                previous_instance_id=previous_id,
                preserve_show_page=preserve,
            ) if transition["changed"] else 0

        if not transition["changed"]:
            return {
                **transition,
                "ok": True,
                "ready": True,
                "invalidated": 0,
            }

        try:
            if reconcile is not None:
                reconcile()
        except Exception as exc:
            return {
                **transition,
                "ok": False,
                "ready": False,
                "invalidated": invalidated,
                "error": str(exc),
            }

        if instance_kind is None:
            return {
                **transition,
                "ok": True,
                "ready": False,
                "invalidated": invalidated,
                "pending": True,
            }
        with engine.begin() as conn:
            completed = _complete_instance_binding_transition_locked(
                conn,
                instance_id=instance_id,
                instance_kind=instance_kind,
                generation=transition["generation"],
            )
        if not completed:
            return {
                **transition,
                "ok": False,
                "ready": False,
                "invalidated": invalidated,
                "error": "binding_generation_changed",
            }
        return {
            **transition,
            "ok": True,
            "ready": True,
            "invalidated": invalidated,
        }


def instance_binding_generation(
    *,
    instance_id: str,
    instance_kind: str,
) -> int | None:
    state = load_instance_binding_state()
    if (
        state is None
        or state["state"] != INSTANCE_BINDING_STATE_READY
        or state["instance_id"] != instance_id
        or state["instance_kind"] != instance_kind
    ):
        return None
    return int(state["generation"])

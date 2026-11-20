"""Instance-local device/session membership. All revocation writes share the SID ledger."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import Integer, String, cast, func, literal, select
from sqlalchemy.dialects.sqlite import insert

from storage.db import get_cached_sqlite_engine
from storage.models import remote_device_deadlines as deadlines
from storage.models import remote_device_sessions as sessions
from storage.models import state_meta, web_push_subscriptions as push
from vibe.device_metadata import device_key


def _cutoff_key(binding, subject):
    return "cutoff:" + hashlib.sha256(json.dumps([binding, subject]).encode()).hexdigest()


def fenced(conn, binding, subject, sid, signed_in, now):
    # Registered sessions at the cutoff have individual tombstones. A fresh
    # login after commit is allowed even in the very same wall-clock second.
    return (
        conn.execute(
            select(deadlines.c.key)
            .where(
                deadlines.c.key.in_([_cutoff_key(binding, subject), _cutoff_key(binding, None)]),
                deadlines.c.issued_at >= signed_in,
                deadlines.c.expires_at > now,
                ~select(sessions.c.session_id).where(sessions.c.session_id == sid).exists(),
            )
            .limit(1)
        ).first()
        is not None
    )


def _deadline(conn, key, now, expiry):
    stmt = insert(deadlines).values(key=key, issued_at=now, expires_at=expiry)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "issued_at": func.max(deadlines.c.issued_at, now),
                "expires_at": func.max(deadlines.c.expires_at, expiry),
            },
        )
    )


def prune(conn, now):
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    keys = (
        conn.execute(
            select(deadlines.c.key).where(deadlines.c.expires_at <= now).order_by(deadlines.c.expires_at).limit(32)
        )
        .scalars()
        .all()
    )
    if keys:
        # Old binaries may have extended a tombstone without its cleanup hint.
        conn.execute(
            state_meta.delete().where(
                state_meta.c.key.in_([k for k in keys if k.startswith(REVOKED_BROWSER_SESSION_KEY_PREFIX)]),
                cast(state_meta.c.value_json, Integer) <= now,
            )
        )
        conn.execute(deadlines.delete().where(deadlines.c.key.in_(keys)))
    conn.execute(
        sessions.delete().where(
            sessions.c.session_id.in_(
                select(sessions.c.session_id)
                .where(sessions.c.expires_at <= now)
                .order_by(sessions.c.expires_at)
                .limit(32)
            )
        )
    )


def record_session(binding, identity, role, metadata, *, expires_at, now, fresh):
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    sid, subject = identity["browser_session_id"], identity["sub"]
    signed_in = identity["auth_time"]
    engine = get_cached_sqlite_engine()
    if not fresh:
        with engine.connect() as conn:
            if conn.execute(select(sessions.c.session_id).where(sessions.c.session_id == sid)).first():
                return
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        if not fresh and (
            conn.execute(
                select(state_meta.c.key).where(state_meta.c.key == REVOKED_BROWSER_SESSION_KEY_PREFIX + sid)
            ).first()
            or fenced(conn, binding, subject, sid, signed_in, now)
        ):
            return
        source = "installation" if metadata.get("installation_id") else "session"
        key = device_key(binding, subject, source, metadata.get("installation_id") or sid)
        prune(conn, now)
        conn.execute(
            insert(sessions)
            .values(
                session_id=sid,
                binding=binding,
                subject=subject,
                email=identity["email"],
                login_role=role,
                device_id=key,
                identity_source=source,
                signed_in_at=signed_in,
                last_active_at=now,
                expires_at=expires_at,
                **{k: metadata.get(k) for k in ("kind", "display_name", "app_version", "system_version")},
            )
            .on_conflict_do_nothing(index_elements=["session_id"])
        )


def touch(session_id, now):
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(
            sessions.update()
            .where(sessions.c.session_id == session_id, sessions.c.last_active_at < now - 300)
            .values(last_active_at=now)
        )


def attach_push(conn, identity, subscription):
    if not identity or not subscription or not subscription.get("device_id"):
        return
    sid, subject = identity["browser_session_id"], identity["sub"]
    if subscription["user_key"] != "remote:" + subject:
        return
    push_id = subscription["device_id"]
    row = conn.execute(
        select(sessions.c.binding, sessions.c.identity_source, sessions.c.push_id).where(
            sessions.c.session_id == sid, sessions.c.subject == subject
        )
    ).first()
    if not row or row.push_id == push_id:
        return
    values = {"push_id": push_id}
    if row.identity_source != "installation":
        values.update(identity_source="push", device_id=device_key(row.binding, subject, "push", push_id))
    conn.execute(sessions.update().where(sessions.c.session_id == sid).values(**values))


def list_devices(binding, subject, *, owner, caller, now):
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    notifications = (
        select(push.c.id)
        .where(
            push.c.user_key == literal("remote:") + sessions.c.subject,
            push.c.device_id == sessions.c.push_id,
            push.c.enabled == 1,
        )
        .exists()
    )
    stmt = select(sessions, notifications.label("notifications")).where(
        sessions.c.binding == binding,
        sessions.c.expires_at > now,
        ~select(state_meta.c.key)
        .where(state_meta.c.key == literal(REVOKED_BROWSER_SESSION_KEY_PREFIX) + sessions.c.session_id)
        .exists(),
    )
    if not owner:
        stmt = stmt.where(sessions.c.subject == subject)
    with get_cached_sqlite_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    grouped = {}
    for row in sorted(rows, key=lambda r: (-r["last_active_at"], r["session_id"])):
        item = grouped.setdefault(
            row["device_id"],
            {
                "id": row["device_id"],
                "kind": row["kind"],
                "display_name": row["display_name"],
                "account": {"subject": row["subject"], "email": row["email"], "login_role": row["login_role"]},
                "app_version": row["app_version"],
                "system_version": row["system_version"],
                "signed_in_at": row["signed_in_at"],
                "last_active_at": row["last_active_at"],
                "current_path": {"kind": row["path_kind"], "family": row["path_family"], "rtt_ms": row["path_rtt_ms"]}
                if row["path_kind"]
                else None,
                "current": False,
                "capabilities": {"push_enabled": False},
            },
        )
        item["current"] |= row["session_id"] == caller
        item["capabilities"]["push_enabled"] |= row["notifications"]
    return list(grouped.values())


def revoke_sid(conn, sid, expiry, now):
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    key = REVOKED_BROWSER_SESSION_KEY_PREFIX + sid
    stmt = insert(state_meta).values(key=key, value_json=str(expiry), updated_at=str(now))
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "value_json": str(expiry),
                "updated_at": str(now),
            },
            where=cast(state_meta.c.value_json, Integer) < expiry,
        )
    )
    _deadline(conn, key, now, expiry)
    conn.execute(sessions.delete().where(sessions.c.session_id == sid))


def sign_out(binding, subject, *, owner, caller, device_id, now, lifetime):
    """One atomic selection + revocation; None device means the caller's bulk scope."""
    stmt = select(sessions.c.session_id, sessions.c.expires_at).where(
        sessions.c.binding == binding,
        sessions.c.expires_at > now,
    )
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        if (
            caller
            and not conn.execute(
                select(sessions.c.session_id).where(
                    sessions.c.session_id == caller,
                    sessions.c.binding == binding,
                    sessions.c.subject == subject,
                    sessions.c.expires_at > now,
                )
            ).first()
        ):
            raise PermissionError("session_revoked")
        if device_id is None:
            _deadline(conn, _cutoff_key(binding, subject), now, now + lifetime)
            if caller:
                stmt = stmt.where(sessions.c.subject == subject, sessions.c.session_id != caller)
        else:
            stmt = stmt.where(sessions.c.device_id == device_id)
            if not owner:
                stmt = stmt.where(sessions.c.subject == subject)
        ids = conn.execute(stmt.with_only_columns(sessions.c.session_id)).scalars().all()
        if ids:
            from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

            key = literal(REVOKED_BROWSER_SESSION_KEY_PREFIX) + sessions.c.session_id
            # One indexed pass per ledger, not three DB round-trips per device.
            expiry = sessions.c.expires_at
            tombstones = insert(state_meta).from_select(
                ["key", "value_json", "updated_at"],
                stmt.with_only_columns(key, cast(expiry, String), literal(str(now))),
            )
            conn.execute(
                tombstones.on_conflict_do_update(
                    index_elements=["key"],
                    set_={
                        "value_json": tombstones.excluded.value_json,
                        "updated_at": str(now),
                    },
                    where=cast(state_meta.c.value_json, Integer) < cast(tombstones.excluded.value_json, Integer),
                )
            )
            hints = insert(deadlines).from_select(
                ["key", "issued_at", "expires_at"], stmt.with_only_columns(key, literal(now), expiry)
            )
            conn.execute(
                hints.on_conflict_do_update(
                    index_elements=["key"],
                    set_={
                        "issued_at": now,
                        "expires_at": func.max(deadlines.c.expires_at, hints.excluded.expires_at),
                    },
                )
            )
            conn.execute(sessions.delete().where(stmt.whereclause))
        prune(conn, now)
    return ids

"""Instance-local device/session membership. All revocation writes share the SID ledger."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import Integer, String, cast, func, literal, or_, select
from sqlalchemy.dialects.sqlite import insert

from storage.db import get_cached_sqlite_engine
from storage.models import remote_device_deadlines as deadlines
from storage.models import remote_account_refs as account_refs
from storage.models import remote_device_sessions as sessions
from storage import device_activity, device_functions, device_health_registry
from storage import device_push_registry as device_push
from storage.models import remote_device_push_links as links, state_meta
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
        conn.execute(deadlines.delete().where(deadlines.c.key.in_(keys)))
    # Predecessors only write state_meta. Walk one bounded keyset page, even
    # after a rollback/upgrade, without scanning the lifetime of sign-outs.
    cursor_key = "remote_access.tombstone_gc_cursor.v1"
    cursor = conn.scalar(select(state_meta.c.value_json).where(state_meta.c.key == cursor_key))
    lower = json.loads(cursor) if cursor else REVOKED_BROWSER_SESSION_KEY_PREFIX
    end = REVOKED_BROWSER_SESSION_KEY_PREFIX[:-1] + chr(ord(REVOKED_BROWSER_SESSION_KEY_PREFIX[-1]) + 1)
    page = conn.execute(
        select(state_meta.c.key, state_meta.c.value_json)
        .where(state_meta.c.key > lower, state_meta.c.key >= REVOKED_BROWSER_SESSION_KEY_PREFIX, state_meta.c.key < end)
        .order_by(state_meta.c.key)
        .limit(32)
    ).all()
    expired = []
    for key, raw in page:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue  # malformed revocations remain fail-closed
        if type(value) is int and value <= now:
            expired.append(key)
    if expired:
        conn.execute(state_meta.delete().where(state_meta.c.key.in_(expired)))
    if len(page) == 32:
        marker = insert(state_meta).values(key=cursor_key, value_json=json.dumps(page[-1].key), updated_at=str(now))
        conn.execute(
            marker.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value_json": marker.excluded.value_json,
                    "updated_at": marker.excluded.updated_at,
                },
            )
        )
    elif cursor:
        conn.execute(state_meta.delete().where(state_meta.c.key == cursor_key))
    from storage.models import device_session_extensions as ext
    expired_sessions = select(sessions.c.session_id).where(sessions.c.expires_at <= now).order_by(
        sessions.c.expires_at, sessions.c.session_id).limit(32)
    conn.execute(account_refs.delete().where(account_refs.c.sid.in_(expired_sessions)))
    conn.execute(ext.delete().where(ext.c.session_id.in_(expired_sessions)))
    conn.execute(sessions.delete().where(sessions.c.session_id.in_(expired_sessions)))


def record_session(binding, identity, role, metadata, *, expires_at, now, fresh, epoch=None, reference=None,
                   account_enroll_before=None):
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
        source = "session"
        key = device_key(binding, subject, source, sid)
        prune(conn, now)
        written = conn.execute(
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
                cookie_exp=identity["exp"],
                expires_at=expires_at,
                **{k: metadata.get(k) for k in ("kind", "display_name", "app_version", "system_version")},
            )
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        if fresh and reference:
            from storage.device_account_signout import AccountSessionSignedOut, enroll
            import time
            if account_enroll_before is not None and int(time.time()) >= account_enroll_before:
                raise AccountSessionSignedOut('account_session_signed_out')
            enroll(conn, sid=sid, subject=subject, binding=binding, epoch=epoch, reference=reference, expires_at=expires_at)
        if fresh and written.rowcount:
            device_activity.record(conn,kind='signedIn',binding=binding,subject=subject,device=key,now=now)


def describe_self(binding, subject, sid, report, now):
    """At most one PK update per minute; a sign-out can never re-create a row."""
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        return describe_in_transaction(conn, binding, subject, sid, report, now)


def describe_in_transaction(conn, binding, subject, sid, report, now):
    values = dict(report)
    installation = values.pop("installation_id")
    key = device_key(binding, subject, "installation", installation)
    values.update(device_id=key, identity_source="installation")
    target = (
        sessions.c.session_id == sid,
        sessions.c.binding == binding,
        sessions.c.subject == subject,
        sessions.c.expires_at > now,
    )
    stmt = sessions.update().where(
        *target,
        (sessions.c.identity_source != 'installation') | (sessions.c.device_id == key),
        sessions.c.described_at.is_(None) | (sessions.c.described_at <= now - 60),
        or_(*(sessions.c[k].is_distinct_from(v) for k, v in values.items())),
    ).values(**values, described_at=now)
    previous = conn.scalar(select(sessions.c.device_id).where(*target))
    if conn.execute(stmt).rowcount:
        device_functions.promote(conn,binding,subject,previous,key,sid)
        conn.execute(links.update().where(links.c.session_id == sid, links.c.binding == binding,
                                          links.c.subject == subject).values(device_id=key))
        return 200
    row = conn.execute(select(sessions).where(*target)).mappings().first()
    if not row:
        return 404
    if all(row[k] == v for k, v in values.items()):
        return 200
    return 409 if row['identity_source'] == 'installation' and row['device_id'] != key else 429


def touch(session_id, now, cookie_exp):
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(
            sessions.update()
            .where(
                sessions.c.session_id == session_id,
                (sessions.c.cookie_exp < cookie_exp) | (sessions.c.last_active_at < now - 300),
            )
            .values(
                last_active_at=func.max(sessions.c.last_active_at, now),
                cookie_exp=func.max(sessions.c.cookie_exp, cookie_exp),
            )
        )


def begin_push_change(conn, identity, *, now):
    """Serialize a remote push registration with device sign-out before it can re-enable delivery."""
    if identity is None:
        return True
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    return (
        conn.execute(
            select(sessions.c.session_id).where(
                sessions.c.session_id == identity["browser_session_id"],
                sessions.c.binding == identity["_device_binding"],
                sessions.c.subject == identity["sub"],
                sessions.c.expires_at > now,
            )
        ).first()
        is not None
    )


def attach_push(conn, identity, subscription):
    if not identity or not subscription:
        return
    sid, subject = identity["browser_session_id"], identity["sub"]
    if subscription["user_key"] != "remote:" + subject:
        return
    push_id = subscription["device_id"]
    row = conn.execute(
        select(
            sessions.c.binding,
            sessions.c.identity_source,
            sessions.c.push_id,
            sessions.c.device_id,
            sessions.c.email,
            sessions.c.login_role,
        ).where(sessions.c.session_id == sid, sessions.c.subject == subject)
    ).first()
    if not row:
        return
    key = row.device_id
    if push_id and row.push_id != push_id:
        values = {"push_id": push_id}
        if row.identity_source != "installation":
            key = device_key(row.binding, subject, "push", push_id)
            values.update(identity_source="push", device_id=key)
        conn.execute(sessions.update().where(sessions.c.session_id == sid).values(**values))
        device_functions.promote(conn,row.binding,subject,row.device_id,key,sid)
    device_push.attest(conn, subscription, sid, row.binding, subject, key, row.email, row.login_role)


def list_devices(binding, subject, *, owner, caller, now, paths=None, conn=None):
    if conn is None:
        with get_cached_sqlite_engine().connect() as reader:
            reader.exec_driver_sql('BEGIN')
            return list_devices(binding, subject, owner=owner, caller=caller, now=now, paths=paths, conn=reader)
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    stmt = select(sessions).where(
        sessions.c.binding == binding,
        sessions.c.cookie_exp > now,
        sessions.c.expires_at > now,
        ~select(state_meta.c.key)
        .where(state_meta.c.key == literal(REVOKED_BROWSER_SESSION_KEY_PREFIX) + sessions.c.session_id)
        .exists(),
    )
    if not owner:
        stmt = stmt.where(sessions.c.subject == subject)
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
                "current_path": None,
                "push_only": False,
                "current": False,
                "capabilities": {"push_enabled": False},
            },
        )
        if paths:
            path = paths.get((row["subject"], row["session_id"]))
            current = item["current_path"]
            if path and (not current or (current["kind"] == "tunnel" and path["kind"] != "tunnel")):
                item["current_path"] = path
        item["current"] |= row["session_id"] == caller
    device_push.join_devices(conn, grouped, binding, subject, owner)
    return list(grouped.values())


def _end_functions(conn,binding,subject,device,now,sids,keep,actor,actor_subject,*,visible, codes_only=False):
    from storage.models import device_function_settings as settings
    pending = device_health_registry.queue_disconnect(conn, binding, subject, device, sids=sids,
                                                      remove_pairing=visible and not keep, codes_only=codes_only)
    if not keep:
        conn.execute(settings.delete().where(settings.c.binding==binding,settings.c.subject==subject,settings.c.device_id==device))
    device_activity.record(conn,kind='signedOut',binding=binding,subject=subject,device=device,now=now,
                           actor=actor,actor_subject=actor_subject)
    return pending


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
    pending = []
    row = conn.execute(select(sessions).where(sessions.c.session_id==sid)).mappings().first()
    if row:
        sibling = conn.scalar(select(sessions.c.session_id).where(sessions.c.binding==row['binding'],
            sessions.c.subject==row['subject'],sessions.c.device_id==row['device_id'],sessions.c.session_id!=sid,
            sessions.c.cookie_exp>now,sessions.c.expires_at>now).limit(1))
        if not sibling:
            device_push.stop_targets(conn,row['binding'],row['subject'],False,sid,row['device_id'],None,None,now)
        pending = _end_functions(conn,row['binding'],row['subject'],row['device_id'],now,[sid],bool(sibling),row['device_id'],row['subject'],
                                 visible=row['cookie_exp'] > now and row['expires_at'] > now)
    from storage.models import device_session_extensions as ext
    conn.execute(account_refs.delete().where(account_refs.c.sid == sid))
    conn.execute(ext.delete().where(ext.c.session_id == sid))
    conn.execute(sessions.delete().where(sessions.c.session_id == sid))
    return pending


def sign_out(binding, subject, *, owner, caller, device_id, now, lifetime, host_epoch=None, delegate=False, receipt_scope=None):
    """One atomic selection + revocation; None device means the caller's bulk scope."""
    stmt = select(sessions.c.session_id, sessions.c.expires_at).where(
        sessions.c.binding == binding,
        sessions.c.expires_at > now,
    )
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        keeper = None
        if caller:
            keeper = conn.execute(
                select(sessions.c.device_id, sessions.c.push_id).where(
                    sessions.c.session_id == caller,
                    sessions.c.binding == binding,
                    sessions.c.subject == subject,
                    sessions.c.expires_at > now,
                )
            ).first()
            if not keeper:
                raise PermissionError("session_revoked")
        if device_id is None:
            _deadline(conn, _cutoff_key(binding, subject), now, now + lifetime)
            if caller:
                stmt = stmt.where(sessions.c.subject == subject, sessions.c.session_id != caller)
        else:
            stmt = stmt.where(sessions.c.device_id == device_id)
            if not owner:
                stmt = stmt.where(sessions.c.subject == subject)
        rows = conn.execute(stmt.with_only_columns(sessions.c.session_id, sessions.c.cookie_exp,
                            sessions.c.device_id,sessions.c.subject)).all()
        ids = [r.session_id for r in rows]
        visible_count = sum(r.cookie_exp > now for r in rows)
        stopped = device_push.stop_targets(
            conn,
            binding,
            subject,
            owner,
            caller,
            device_id,
            keeper.device_id if keeper else None,
            keeper.push_id if keeper else None,
            now,
        )
        from storage.device_account_signout import stage
        results = stage(conn, binding=binding, actor=subject, delegate=delegate, caller=caller,
                        keeper_device=keeper.device_id if keeper else None, target=device_id,
                        selection=stmt.whereclause, push_devices=stopped, now=now, receipt_scope=receipt_scope)
        keys = {(r.device_id, r.subject) for r in rows}
        from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX
        # Match list_devices' live-session predicate, excluding push-only and revoked rows.
        visible_rows = stmt.with_only_columns(sessions.c.device_id, sessions.c.subject, sessions.c.email).where(
            sessions.c.cookie_exp > now, ~select(state_meta.c.key).where(
                state_meta.c.key == literal(REVOKED_BROWSER_SESSION_KEY_PREFIX) + sessions.c.session_id
            ).exists())
        # Use the same representative account as list_devices. Local administrative
        # access can fence any target, but is not proof of Health ownership.
        device_accounts = {}
        for target, account, email in conn.execute(visible_rows.order_by(sessions.c.last_active_at.desc(), sessions.c.session_id)):
            device_accounts.setdefault(target, (account, email))
        visible = {(target, account) for target, (account, _) in device_accounts.items() if caller}
        if host_epoch and not caller:
            from storage.device_health_owner_checks import queue
            for target, (account, email) in device_accounts.items():
                queue(conn, binding=binding, epoch=host_epoch, subject=account, device=target, email=email, now=now)
        pending = []
        for target, account in keys:
            keep = device_id is None and keeper is not None and target == keeper.device_id
            pending.extend(_end_functions(conn, binding, account, target, now, ids, keep,
                           keeper.device_id if keeper else 'host', subject, visible=(target, account) in visible, codes_only=not caller))
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
            from storage.models import device_session_extensions as ext
            selected_ids = stmt.with_only_columns(sessions.c.session_id)
            conn.execute(account_refs.delete().where(account_refs.c.sid.in_(selected_ids)))
            conn.execute(ext.delete().where(ext.c.session_id.in_(selected_ids)))
            conn.execute(sessions.delete().where(stmt.whereclause))
        prune(conn, now)
    deferred = device_health_registry.finish_pending(get_cached_sqlite_engine(), pending)
    return ids, visible_count, results, deferred

"""Instance-local device/session membership. All revocation writes share the SID ledger."""

from __future__ import annotations

import hashlib
import json
import time

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


def binding_retired(conn, binding):
    return bool(binding and conn.scalar(select(state_meta.c.key).where(
        state_meta.c.key == 'remote_access.retired_registry.v1:' + binding)))


def retire_binding(binding, *, now):
    """Before config publication: freeze the old registry and stop its remote deliveries.

    No account sign-out and no Health unpair. Snapshot unattributed remote endpoints
    in this same write transaction, while the new binding cannot yet register.
    A replay after publication must never select endpoints a second time.
    """
    from storage.models import device_session_extensions as ext, web_push_subscriptions as push
    from storage.models import live_activity_trackings as tracks
    from storage import web_push_service

    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        if binding_retired(conn, binding):
            return
        conn.execute(insert(state_meta).values(key='remote_access.retired_registry.v1:' + binding,
                                               value_json='true', updated_at=str(now)))
        web_push_service.record_binding_retirement(conn, binding, now)
        # A rotated phone token may revive a token_gone row, but never across
        # retirement, even if the phone enrolls the same endpoint again.
        revivable = (tracks.c.status == 'stopped') & (tracks.c.stop_reason == 'token_gone')
        conn.execute(tracks.update().where(tracks.c.owner_user_key.like('remote:%'),
                                           (tracks.c.status == 'tracking') | revivable)
                     .values(status='stopped', stop_reason='registration_unavailable', confirmed_at=now,
                             pending_state=None, updated_at=now))
        old_sids = select(sessions.c.session_id).where(sessions.c.binding == binding)
        for endpoint, user_key in conn.execute(select(push.c.endpoint, push.c.user_key).where(
                push.c.user_key.like('remote:%'))).all():
            web_push_service.disable_subscription(conn, endpoint=endpoint, user_key=user_key)
        conn.execute(links.delete().where(links.c.binding == binding))
        conn.execute(account_refs.delete().where(account_refs.c.cookie_binding == binding))
        conn.execute(ext.delete().where(ext.c.session_id.in_(old_sids)))
        conn.execute(sessions.delete().where(sessions.c.binding == binding))


def fenced(conn, binding, subject, sid, signed_in, now):
    # Registered sessions at the cutoff have individual tombstones. A fresh
    # login after commit is allowed even in the very same wall-clock second.
    return binding_retired(conn, binding) or (
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


class SessionNotRegistered(RuntimeError):
    """A fresh session could not be recorded under its own new id: it must not be issued."""


def _tombstoned(conn, sid, now):
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    raw = conn.scalar(select(state_meta.c.value_json).where(state_meta.c.key == REVOKED_BROWSER_SESSION_KEY_PREFIX + sid))
    if raw is None:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return True  # fail closed, as the request check does
    return type(value) is not int or value > now


def _tombstone(conn, sid, expiry, now):
    """Write or raise a session's tombstone; never shorten one."""
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    stmt = insert(state_meta).values(key=REVOKED_BROWSER_SESSION_KEY_PREFIX + sid, value_json=str(expiry), updated_at=str(now))
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value_json": str(expiry), "updated_at": str(now)},
            where=cast(state_meta.c.value_json, Integer) < expiry,
        )
    )


# What a new session takes over from the session of the same browser it replaces. Everything
# else (role, email, sign-in time, lifetime, account reference) is this login's own.
_DEVICE_FIELDS = ("device_id", "identity_source", "push_id", "described_at", "health_alias_hash", "schedule_alias")
_REPORTED_FIELDS = ("kind", "display_name", "app_version", "system_version")


def _replaced(conn, binding, subject, previous, now):
    """The live row of the session this browser proved it holds, or None. Under the write lock."""
    if not previous or previous["binding"] != binding or previous["subject"] != subject:
        return None
    sid = previous["sid"]
    # The cookie's own deadline, as the request check computes it, not the row's cookie_exp (which
    # another, renewed copy of the cookie may have raised).
    if previous["valid_until"] <= now or _tombstoned(conn, sid, now):
        return None
    row = conn.execute(select(sessions).where(
        sessions.c.session_id == sid, sessions.c.binding == binding, sessions.c.subject == subject,
        sessions.c.cookie_exp > now, sessions.c.expires_at > now,
    )).mappings().first()
    if row is None or fenced(conn, binding, subject, sid, previous["auth_time"], now):
        return None
    return row


def _hand_over(conn, binding, subject, old, new_sid, identity, role, cap, now):
    """Session-level sign-out of `old`, keeping its device row for `new_sid`. No push stop, no
    Health unpair, no settings change, no account sign-out, no signedOut activity."""
    from storage.models import device_session_extensions as ext, web_push_subscriptions as push

    old_sid, device = old["session_id"], old["device_id"]
    # Push: only the old session's own links whose subscription still proves this person's
    # ownership. Never another session's, never an unproven one, no endpoint or state change.
    proven = select(push.c.id).where(
        push.c.id == links.c.subscription_id,
        push.c.user_key == literal("remote:") + links.c.subject,
        push.c.device_id.is_not_distinct_from(links.c.push_id),
    ).exists()
    conn.execute(links.update().where(
        links.c.session_id == old_sid, links.c.binding == binding, links.c.subject == subject,
        links.c.device_id == device, proven,
    ).values(session_id=new_sid, email=identity["email"], login_role=role))
    # Health ticket rate counters move; the schedule principal (old role and groups) does not.
    counters = conn.execute(select(ext.c.ticket_window, ext.c.ticket_attempts)
                            .where(ext.c.session_id == old_sid)).first()
    if counters and (counters.ticket_window is not None or counters.ticket_attempts is not None):
        conn.execute(insert(ext).values(session_id=new_sid, ticket_window=counters.ticket_window,
                                        ticket_attempts=counters.ticket_attempts))
    # Unused pairing codes stay with the session that issued them and are cleared with it.
    pending = device_health_registry.queue_disconnect(conn, binding, subject, device, sids=[old_sid],
                                                      remove_pairing=False, codes_only=True)
    _tombstone(conn, old_sid, cap, now)
    conn.execute(account_refs.delete().where(account_refs.c.sid == old_sid))
    conn.execute(ext.delete().where(ext.c.session_id == old_sid))
    conn.execute(sessions.delete().where(sessions.c.session_id == old_sid))
    return pending


def record_session(binding, identity, role, metadata, *, expires_at, now, fresh, epoch=None, reference=None,
                   account_enroll_before=None, previous=None):
    """Record a session. A fresh one may replace `previous`, the same browser's own live session
    (proved by its cookie on this very request), and returns ``(old sid, Health cleanup)`` then."""
    from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX

    sid, subject = identity["browser_session_id"], identity["sub"]
    signed_in = identity["auth_time"]
    engine = get_cached_sqlite_engine()
    if not fresh:
        with engine.connect() as conn:
            if conn.execute(select(sessions.c.session_id).where(sessions.c.session_id == sid)).first():
                return None
    replaced = None
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        if binding_retired(conn, binding):
            raise PermissionError('session_revoked')
        if not fresh and (
            conn.execute(
                select(state_meta.c.key).where(state_meta.c.key == REVOKED_BROWSER_SESSION_KEY_PREFIX + sid)
            ).first()
            or fenced(conn, binding, subject, sid, signed_in, now)
        ):
            return None
        if fresh and _tombstoned(conn, sid, now):
            raise SessionNotRegistered('session_not_registered')
        # The clock read after the lock: a deadline crossed while waiting counts.
        old = _replaced(conn, binding, subject, previous, int(time.time())) if fresh else None
        source = "session"
        key = device_key(binding, subject, source, sid)
        device = dict(device_id=key, identity_source=source)
        reported = {k: metadata.get(k) for k in _REPORTED_FIELDS}
        if old is not None:
            device = {k: old[k] for k in _DEVICE_FIELDS}
            if old["identity_source"] == "installation":
                reported = {k: old[k] for k in _REPORTED_FIELDS}
        prune(conn, now)
        written = conn.execute(
            insert(sessions)
            .values(
                session_id=sid,
                binding=binding,
                subject=subject,
                email=identity["email"],
                login_role=role,
                signed_in_at=signed_in,
                last_active_at=now,
                cookie_exp=identity["exp"],
                expires_at=expires_at,
                **device,
                **reported,
            )
            .on_conflict_do_nothing(index_elements=["session_id"])
        )
        if fresh and not written.rowcount:
            # Not recorded under its own id: issuing it would hand out an unlisted session.
            raise SessionNotRegistered('session_not_registered')
        if fresh and reference:
            from storage.device_account_signout import AccountSessionSignedOut, enroll
            if account_enroll_before is not None and int(time.time()) >= account_enroll_before:
                raise AccountSessionSignedOut('account_session_signed_out')
            enroll(conn, sid=sid, subject=subject, binding=binding, epoch=epoch, reference=reference, expires_at=expires_at)
        if fresh and written.rowcount:
            device_activity.record(conn, kind='signedIn', binding=binding, subject=subject,
                                   device=device["device_id"], now=now)
        if old is not None:
            replaced = old["session_id"], _hand_over(conn, binding, subject, old, sid, identity, role,
                                                     previous["cap"], now)
    return replaced


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
    _tombstone(conn, sid, expiry, now)
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


def sign_out(binding, subject, *, owner, caller, device_id, now, lifetime, host_epoch=None, delegate=False, receipt_scope=None,
             prepare=None):
    """One atomic selection + revocation; None device means the caller's bulk scope.

    ``prepare(conn)`` runs first inside the same transaction, while the rows this
    call will delete are still readable (the address change snapshots them).
    """
    stmt = select(sessions.c.session_id, sessions.c.expires_at).where(
        sessions.c.binding == binding,
        sessions.c.expires_at > now,
    )
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        if prepare is not None:
            prepare(conn)
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

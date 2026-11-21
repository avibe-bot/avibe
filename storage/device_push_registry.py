"""Push provenance is independent of cookie lifetime. Unattested rows are owner-only."""

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.dialects.sqlite import insert

from storage.models import remote_device_sessions as sessions
from storage.models import remote_device_push_links as links, web_push_subscriptions as push
from storage import web_push_service
from vibe.device_metadata import describe, device_key


def attest(conn, row, session, binding, subject, logical_id, email, login_role):
    values = dict(
        binding=binding,
        subject=subject,
        email=email,
        login_role=login_role,
        push_id=row["device_id"],
        session_id=session,
        device_id=logical_id,
    )
    stmt = insert(links).values(subscription_id=row["id"], **values)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["subscription_id"],
            set_=values,
            where=or_(*(links.c[k].is_distinct_from(v) for k, v in values.items())),
        )
    )


def _proven(binding):
    return and_(
        links.c.subscription_id == push.c.id,
        links.c.binding == binding,
        push.c.user_key == literal("remote:") + links.c.subject,
        push.c.device_id.is_not_distinct_from(links.c.push_id),
    )


def _rows(binding, subject, owner):
    stmt = (
        select(
            push.c.id,
            push.c.user_key,
            push.c.device_id,
            push.c.user_agent,
            push.c.device_label,
            links.c.device_id.label("logical_id"),
            links.c.subject,
            links.c.email,
            links.c.login_role,
        )
        .select_from(push.outerjoin(links, _proven(binding)) if owner else links.join(push, _proven(binding)))
        .where(push.c.enabled == 1)
    )
    if not owner:
        stmt = stmt.where(links.c.subject == subject)
    return stmt


def _legacy_key(binding, row):
    source = "push" if row.device_id else "subscription"
    return device_key(binding, row.user_key, source, row.device_id or row.id)


def join_devices(conn, grouped, binding, subject, owner):
    # Pick a server-issued representative for each legacy bucket. A stale action
    # is revalidated against both bucket identity and lack of current attestation.
    legacy = {}
    for row in conn.execute(_rows(binding, subject, owner)):
        key = row.logical_id
        if key is None:
            group = _legacy_key(binding, row)
            if group in legacy and legacy[group].id <= row.id:
                continue
            if group in legacy:
                grouped.pop("p." + legacy[group].id + "." + group)
            legacy[group] = row
            key = "p." + row.id + "." + group
        if key in grouped:
            grouped[key]["capabilities"]["push_enabled"] = True
            continue
        metadata = describe({"User-Agent": row.user_agent or ""})
        grouped[key] = dict(
            id=key,
            kind=metadata["kind"],
            display_name=row.device_label or metadata["display_name"],
            account={"subject": row.subject, "email": row.email, "login_role": row.login_role} if row.subject else None,
            app_version=metadata["app_version"],
            system_version=None,
            signed_in_at=None,
            last_active_at=None,
            current_path=None,
            current=False,
            push_only=True,
            capabilities={"push_enabled": True},
        )


def stop_legacy(conn, binding, public_id):
    parts = public_id.split(".")
    if len(parts) != 3 or parts[0] != "p":
        return False
    stmt = _rows(binding, None, True).where(links.c.subscription_id.is_(None))
    row = conn.execute(stmt.where(push.c.id == parts[1])).first()
    if not row or _legacy_key(binding, row) != parts[2]:
        return False
    bucket = (
        (push.c.user_key == row.user_key) & (push.c.device_id == row.device_id)
        if row.device_id
        else push.c.id == row.id
    )
    for endpoint, user_key in conn.execute(
        stmt.with_only_columns(push.c.endpoint, push.c.user_key).where(bucket)
    ).all():
        web_push_service.disable_subscription(conn, endpoint=endpoint, user_key=user_key)
    meta = describe({"User-Agent": row.user_agent or ""})
    return {(public_id, None): (meta['kind'], row.device_label or meta['display_name'])}


def stop_targets(conn, binding, subject, owner, caller, device_id, keep_device, keep_push_id, now):
    if device_id is not None and device_id.startswith("p."):
        return (stop_legacy(conn, binding, device_id) or {}) if owner else {}
    stmt = _rows(binding, subject, owner).with_only_columns(
        push.c.endpoint,
        push.c.user_key,
        push.c.id,
        push.c.user_agent,
        push.c.device_label,
        push.c.device_id,
        links.c.subject,
        links.c.device_id.label("logical_id"),
    )
    if device_id is not None:
        stmt = stmt.where(links.c.device_id == device_id)
    elif caller:
        stmt = stmt.where(links.c.subject == subject, links.c.device_id != keep_device)
    else:
        # Direct loopback's bulk scope is remote; local subscriptions have
        # their own explicit one-device stop action and are not remote sessions.
        stmt = stmt.where(push.c.user_key.like("remote:%"))
    # Match listing's representative for each unattributed bucket before disabling push.
    legacy = {}
    stopped = {}
    rows = conn.execute(stmt).all()
    for row in rows:
        meta = describe({'User-Agent': row.user_agent or ''})
        label = (meta['kind'], row.device_label or meta['display_name'])
        if row.logical_id:
            stopped[(row.logical_id, row.subject)] = label
        else:
            group = _legacy_key(binding, row)
            if group not in legacy or row.id < legacy[group][0]:
                legacy[group] = row.id, label
        survivor = None
        if device_id is not None and row.device_id:
            survivor = conn.execute(
                select(sessions.c.session_id, sessions.c.device_id)
                .where(
                    sessions.c.binding == binding,
                    sessions.c.subject == row.subject,
                    sessions.c.push_id == row.device_id,
                    sessions.c.device_id != device_id,
                    sessions.c.cookie_exp > now,
                    sessions.c.expires_at > now,
                )
                .limit(1)
            ).first()
        elif caller and keep_push_id and row.device_id == keep_push_id:
            survivor = (caller, keep_device)
        if survivor:
            email, role = conn.execute(
                select(sessions.c.email, sessions.c.login_role).where(sessions.c.session_id == survivor[0])
            ).one()
            conn.execute(
                links.update()
                .where(links.c.subscription_id == row.id)
                .values(session_id=survivor[0], device_id=survivor[1], email=email, login_role=role)
            )
        else:
            web_push_service.disable_subscription(conn, endpoint=row.endpoint, user_key=row.user_key)
    stopped.update({('p.' + rep + '.' + group, None): label for group, (rep, label) in legacy.items()})
    return stopped

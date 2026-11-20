"""Function gates are instance-local and addressed by the signed registry identity."""

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.sqlite import insert

from storage.models import (
    device_activity_events as events,
    device_function_settings as settings,
    device_health_links as health,
    remote_device_sessions as sessions,
    remote_device_push_links as attestations,
    system_schedule_operations as operations,
    web_push_subscriptions as subscriptions,
)


class DeviceError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


def live_caller(conn, viewer, now):
    binding, subject, _, sid = viewer
    if not sid:
        return None
    row = (
        conn.execute(
            select(sessions).where(
                sessions.c.session_id == sid,
                sessions.c.subject == subject,
                sessions.c.binding == binding,
                sessions.c.cookie_exp > now,
                sessions.c.expires_at > now,
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise DeviceError("session_revoked", 403)
    return row


def preference(conn, binding, subject, device, **updates):
    stmt = insert(settings).values(binding=binding, subject=subject, device_id=device, **updates)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["binding", "subject", "device_id"],
            set_=updates,
            where=~and_(*(settings.c[k] == v for k, v in updates.items())),
        )
    )


def schedule_claimant(conn, viewer, now):
    caller = live_caller(conn, viewer, now)
    if not caller:
        return None, True
    key = caller["binding"], caller["subject"], caller["device_id"]
    preference(conn, *key, schedule_capable=1)
    on = conn.scalar(
        select(settings.c.schedule_on).where(
            settings.c.binding == key[0], settings.c.subject == key[1], settings.c.device_id == key[2]
        )
    )
    return dict(registry_binding=key[0], registry_subject=key[1], registry_device=key[2]), bool(on)


def delivery_allowed():
    """An enrollment remains valid while delivery is paused; match its attested ownership."""
    return (
        ~select(settings.c.device_id)
        .select_from(
            attestations.join(
                settings,
                and_(
                    settings.c.binding == attestations.c.binding,
                    settings.c.subject == attestations.c.subject,
                    settings.c.device_id == attestations.c.device_id,
                ),
            )
        )
        .where(
            attestations.c.subscription_id == subscriptions.c.id,
            subscriptions.c.user_key == "remote:" + attestations.c.subject,
            subscriptions.c.device_id.is_not_distinct_from(attestations.c.push_id),
            settings.c.notifications_on == 0,
        )
        .exists()
    )


def promote(conn, binding, subject, old, new, sid):
    if old == new:
        return
    key = (settings.c.binding == binding, settings.c.subject == subject)
    previous = conn.execute(select(settings).where(*key, settings.c.device_id == old)).mappings().first()
    if previous:
        stmt = insert(settings).values(
            binding=binding,
            subject=subject,
            device_id=new,
            **{k: previous[k] for k in ("notifications_on", "schedule_on", "schedule_capable")},
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["binding", "subject", "device_id"],
                set_={
                    "notifications_on": func.min(settings.c.notifications_on, stmt.excluded.notifications_on),
                    "schedule_on": func.min(settings.c.schedule_on, stmt.excluded.schedule_on),
                    "schedule_capable": func.max(settings.c.schedule_capable, stmt.excluded.schedule_capable),
                },
            )
        )
    shared = conn.scalar(
        select(sessions.c.session_id)
        .where(
            sessions.c.session_id != sid,
            sessions.c.binding == binding,
            sessions.c.subject == subject,
            sessions.c.device_id == old,
        )
        .limit(1)
    )
    if shared:
        # Do not move another live session's proof merely because they shared an old push identity.
        conn.execute(
            health.update()
            .where(
                health.c.binding == binding,
                health.c.subject == subject,
                health.c.device_id == old,
                health.c.issuer_sid == sid,
            )
            .values(device_id=new)
        )
        return
    conn.execute(settings.delete().where(*key, settings.c.device_id == old))
    conn.execute(
        health.update()
        .where(health.c.binding == binding, health.c.subject == subject, health.c.device_id == old)
        .values(device_id=new)
    )
    conn.execute(
        events.update()
        .where(events.c.binding == binding, events.c.subject == subject, events.c.device_id == old)
        .values(device_id=new)
    )
    conn.execute(
        events.update()
        .where(events.c.binding == binding, events.c.actor_subject == subject, events.c.actor_id == old)
        .values(actor_id=new)
    )
    conn.execute(
        operations.update()
        .where(
            operations.c.registry_binding == binding,
            operations.c.registry_subject == subject,
            operations.c.registry_device == old,
        )
        .values(registry_device=new)
    )


def owns(row, viewer, live_owner_email):
    acct = row.get("account")
    if not acct:
        return False
    if viewer[1]:
        return acct["subject"] == viewer[1]
    return bool(viewer[2] and live_owner_email and acct["email"].casefold() == live_owner_email)

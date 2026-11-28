"""Deferred live owner verification for a trusted-host sign-out, never for a caller-supplied target."""

import time
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from storage.db import get_cached_sqlite_engine
from storage.models import device_health_links as links, device_health_owner_checks as checks, remote_device_sessions as sessions

TTL = 600


def queue(conn, *, binding, epoch, subject, device, email, now):
    # Only exact enrolled pairings. Push-only/unlinked/expired devices never get here.
    rows = conn.execute(select(links.c.mobile_id, links.c.linked_at).where(
        links.c.binding == binding, links.c.subject == subject, links.c.device_id == device,
        links.c.code_hash.is_(None), links.c.linked_at.is_not(None), links.c.cleanup_pending.is_(None))).all()
    for mid, linked in rows:
        values = dict(operation=uuid.uuid4().hex, epoch=epoch, binding=binding, subject=subject,
                      device_id=device, owner_email=(email or "").casefold(), linked_at=linked, next_at=now, expires_at=now + TTL)
        stmt = insert(checks).values(mobile_id=mid, **values)
        conn.execute(stmt.on_conflict_do_update(index_elements=['mobile_id'], set_=values))
        conn.execute(links.update().where(links.c.mobile_id == mid).values(ticket_hash=None))


def run_once(load_config):
    from config.v2_config import config_file_lock
    from storage.mobile_store import MobileStore
    from vibe import permissions
    from vibe.account_session_revoke import pairing_epoch
    from vibe.remote_access import device_binding

    now = int(time.time())
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        row = conn.execute(select(checks).where(checks.c.next_at <= now).order_by(checks.c.next_at).limit(1)).mappings().first()
        if row is None:
            return False
        exact = (checks.c.mobile_id == row['mobile_id']) & (checks.c.operation == row['operation'])
        if row['expires_at'] <= now:
            conn.execute(checks.delete().where(exact))
            return True
        conn.execute(checks.update().where(exact).values(next_at=now + 30))
    cfg = load_config()
    if cfg is None:
        return False
    epoch = pairing_epoch(cfg)
    owner = None
    owner_subject = None
    if epoch == row['epoch'] and device_binding(cfg) == row['binding']:
        # Do not hold a config, registry or private-store lock across external I/O.
        try:
            response = permissions.get_current_permissions(cfg)
        except permissions.PermissionsError:
            return False
        if response.source != 'live':
            return False
        cloud = cfg.remote_access.vibe_cloud
        if response.projection['instance']['id'] == cloud.instance_id:
            record = response.projection['access']['owner']
            owner_subject = record.get('user_id')
            if 'user_id' in record and (not isinstance(owner_subject, str) or not owner_subject.strip()
                                       or owner_subject != owner_subject.strip()):
                return False
            owner = (record.get('email') or '').casefold()
    with config_file_lock():
        current = load_config()
        if current is None:
            return False
        with engine.begin() as conn:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
            if not conn.scalar(select(checks.c.mobile_id).where(exact)):
                return True  # Superseded by another explicit action.
            now = int(time.time())
            if (row['expires_at'] <= now
                    or pairing_epoch(current) != row['epoch'] or device_binding(current) != row['binding']):
                conn.execute(checks.delete().where(exact))
                return True
            if owner is None:
                return False
            store = MobileStore(existing_only=True)
            if owner_subject is None and store.has_subject():
                return False  # Keep the scheduled retry; missing proof is not a different owner.
            matches = owner_subject == row['subject'] if owner_subject is not None else bool(owner and owner == row['owner_email'])
            if not matches:
                conn.execute(checks.delete().where(exact))
                return True
            link = conn.scalar(select(links.c.mobile_id).where(links.c.mobile_id == row['mobile_id'],
                links.c.binding == row['binding'], links.c.subject == row['subject'], links.c.device_id == row['device_id'],
                links.c.linked_at == row['linked_at'], links.c.code_hash.is_(None), links.c.cleanup_pending.is_(None)))
            restored = conn.scalar(select(sessions.c.session_id).where(sessions.c.binding == row['binding'],
                sessions.c.subject == row['subject'], sessions.c.device_id == row['device_id'],
                sessions.c.expires_at > now, sessions.c.cookie_exp > now).limit(1))
            if link and not restored:
                store.revoke_confirmed_pairing(link, instance=cloud.instance_id, owner=owner, owner_subject=owner_subject,
                                               legacy_subject_proven=owner_subject == row["subject"])
            conn.execute(checks.delete().where(exact))
    return True

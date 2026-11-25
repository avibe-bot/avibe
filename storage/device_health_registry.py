"""Registry ownership of native Health pairing. The registry lock precedes the private store lock."""

from datetime import datetime, timezone
import json
import math
import secrets
import time

from sqlalchemy import case, select

from core.mobile_health_contract import METRICS, MobileError
from storage.db import get_cached_sqlite_engine
from storage.device_functions import DeviceError, live_caller
from storage.device_health_tickets import digest
from storage.models import device_health_links as links
from storage.mobile_store import MobileStore


def issue_pairing(store, request_id, identity, viewer):
    if not viewer or not viewer[3]:
        return store.issue_pairing(request_id, identity)
    now = int(time.time())
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            row = live_caller(conn, viewer, now)
        except DeviceError:
            raise MobileError("device_unauthorized", 401) from None
        result = store.issue_pairing(request_id, identity)
        conn.execute(
            links.insert().values(
                mobile_id="mdev_" + secrets.token_hex(16),
                binding=row["binding"],
                subject=row["subject"],
                device_id=row["device_id"],
                issuer_sid=row["session_id"],
                code_hash=digest(result["pairing_code"]),
                code_expires_at=math.ceil(datetime.fromisoformat(result['expires_at']).timestamp()),
                linked_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        return result


def pair(store, payload, identity):
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        link = (
            conn.execute(select(links).where(links.c.code_hash == digest(payload["pairing_code"]))).mappings().first()
        )
        if not link:
            return store.pair(payload, identity)
        if link['cleanup_pending']:
            raise MobileError('device_unauthorized', 401)
        viewer = (link["binding"], link["subject"], True, link["issuer_sid"])
        try:
            live_caller(conn, viewer, int(time.time()))
        except DeviceError:
            raise MobileError("device_unauthorized", 401) from None
        result = store.pair(payload, identity, reserved_device_id=link["mobile_id"])
        conn.execute(links.update().where(links.c.mobile_id == link["mobile_id"]).values(code_hash=None))
        return result


def queue_disconnect(conn, binding, subject, device, *, sids, remove_pairing, codes_only=False):
    """Commit the exact cleanup targets with the session tombstone. No private DB IO."""
    condition = (links.c.code_hash.is_not(None) & links.c.issuer_sid.in_(sids))
    if remove_pairing:
        condition |= links.c.linked_at.is_not(None) & links.c.code_hash.is_(None)
        if not codes_only:
            # A code left for codes-only cleanup (a browser that signed in again) may already
            # have paired in the private store: an unpair of this device takes it with it.
            condition |= links.c.cleanup_pending == 2
    scope = (links.c.binding == binding, links.c.subject == subject, links.c.device_id == device, condition)
    # Caller owns the main DB write lock. This also works on SQLite before RETURNING.
    targets = list(conn.scalars(select(links.c.mobile_id).where(*scope)))
    if targets:
        # A full unpair (1) is never lowered to codes-only (2).
        intent = case((links.c.cleanup_pending == 1, 1), else_=2) if codes_only else 1
        conn.execute(links.update().where(*scope).values(cleanup_pending=intent, ticket_hash=None))
    return targets


def finish_pending(engine, targets):
    """True if cleanup still needs retry. Both steps are idempotent across crashes."""
    import logging
    deferred = False
    for mid in targets:
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql('BEGIN IMMEDIATE')
                row = conn.execute(select(links).where(links.c.mobile_id == mid,
                                                       links.c.cleanup_pending.in_((1, 2)))).mappings().first()
                if not row:
                    continue
                store = MobileStore(existing_only=True)
                with store.transaction() as mobile:
                    if row['code_hash']:
                        mobile.execute('DELETE FROM pairings WHERE code_hash=?', (row['code_hash'],))
                    active = mobile.execute('SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL', (mid,)).fetchone()
                if active and row['cleanup_pending'] == 1:
                    store.revoke(device_id=mid, caller='device_sign_out')
                if row['code_hash'] and not active:
                    conn.execute(links.delete().where(links.c.mobile_id == mid))
                else:
                    conn.execute(links.update().where(links.c.mobile_id == mid).values(
                        cleanup_pending=None, code_hash=None, ticket_hash=None))
        except Exception:
            # The tombstone has committed. Never include credentials, ids or private DB errors.
            logging.getLogger(__name__).warning('Device Health cleanup deferred')
            deferred = True
    return deferred


def maintain(engine, now, cancel=None):
    with engine.connect() as conn:
        pending = conn.execute(select(links.c.mobile_id).where(links.c.cleanup_pending.in_((1, 2)))
                               .order_by(links.c.mobile_id).limit(128)).scalars().all()
    if cancel is not None and cancel.is_set():
        return
    finish_pending(engine, pending)
    # A private pairing may have committed before the main DB transaction was interrupted.
    # Retain that exact pairing's provenance; delete only unused, expired reservations.
    with engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        expired = conn.execute(select(links.c.mobile_id).where(links.c.code_expires_at <= now,
            links.c.code_hash.is_not(None), links.c.cleanup_pending.is_(None)).limit(128)).scalars().all()
        if expired:
            with MobileStore(existing_only=True).transaction() as mobile:
                for mid in expired:
                    match = links.c.mobile_id == mid
                    if mobile.execute('SELECT 1 FROM devices WHERE id=?', (mid,)).fetchone():
                        conn.execute(links.update().where(match).values(code_hash=None, code_expires_at=None))
                    else:
                        conn.execute(links.delete().where(match))


def scoped_rows(conn, instance, owner):
    rows = conn.execute(
        """SELECT d.id,d.name,d.metrics,json_extract(d.manifest,'$.os_version') AS os_version,
            d.last_seen,max(s.synced_at) AS latest,group_concat(CASE WHEN s.synced_at IS NOT NULL THEN s.metric END) AS synced
        FROM devices d LEFT JOIN metric_sync s ON s.device_id=d.id
        WHERE d.instance_id=? AND d.owner=? AND d.revoked_at IS NULL GROUP BY d.id""",
        (instance, owner),
    )
    for row in rows:
        keys = row["synced"].split(",") if row["synced"] else []
        consented = json.loads(row["metrics"])
        yield (
            row["id"],
            {
                "kind": "health",
                "on": True,
                "data": [k for k in METRICS if k in consented],
                **({"syncedAt": row["latest"]} if row["latest"] else {}),
            },
            {
                "pairing_id": row["id"],
                "device_name": row["name"],
                "platform": "ios",
                "system_version": row["os_version"],
                "last_seen_at": row["last_seen"],
                "data": [k for k in METRICS if k in keys],
            },
        )

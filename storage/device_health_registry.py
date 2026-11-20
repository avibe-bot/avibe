"""Registry ownership of native Health pairing. The registry lock precedes the private store lock."""

from datetime import datetime, timezone
import json
import secrets
import time

from sqlalchemy import select

from core.mobile_health_contract import METRICS, MobileError
from storage.db import get_cached_sqlite_engine
from storage.device_functions import DeviceError, live_caller
from storage.device_health_tickets import digest
from storage.models import device_health_links as links
from storage.mobile_store import MobileStore, mobile_store_path


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
        viewer = (link["binding"], link["subject"], True, link["issuer_sid"])
        try:
            live_caller(conn, viewer, int(time.time()))
        except DeviceError:
            raise MobileError("device_unauthorized", 401) from None
        result = store.pair(payload, identity, reserved_device_id=link["mobile_id"])
        conn.execute(links.update().where(links.c.mobile_id == link["mobile_id"]).values(code_hash=None))
        return result


def disconnect(conn, binding, subject, device, *, sids=(), keep_device=False):
    rows = (
        conn.execute(
            select(links).where(links.c.binding == binding, links.c.subject == subject, links.c.device_id == device)
        )
        .mappings()
        .all()
    )
    if not rows or not mobile_store_path().exists():
        return
    store = MobileStore()
    for row in rows:
        if keep_device and (not row["code_hash"] or row["issuer_sid"] not in sids):
            continue
        with store.transaction() as mobile:
            if row["code_hash"]:
                mobile.execute("DELETE FROM pairings WHERE code_hash=?", (row["code_hash"],))
            active = mobile.execute(
                "SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL", (row["mobile_id"],)
            ).fetchone()
        if active and not keep_device:
            store.revoke(device_id=row["mobile_id"], caller="device_sign_out")
        conn.execute(
            links.update().where(links.c.mobile_id == row["mobile_id"]).values(code_hash=None, ticket_hash=None)
        )


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

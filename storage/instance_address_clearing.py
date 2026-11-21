"""Which devices held data under a retired instance address, and which reported clearing it.

Rules F1-F4 of ``docs/plans/instance-address-change.md``. A row is "cleared"
only when the clearing browser posted its receipt back; nothing else marks it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from storage.models import instance_address_clearing_receipts as receipts
from storage.models import instance_address_clearings as clearings
from storage.models import remote_device_sessions as sessions
from storage.models import web_push_subscriptions


def _row_id(instance_id: str, hostname: str, device_id: str) -> str:
    return hashlib.sha256(json.dumps([instance_id, hostname, device_id]).encode("utf-8")).hexdigest()


def snapshot(conn, *, instance_id: str, hostname: str, binding: str, now: int,
             receipt_hash_for: Callable[[str], str]) -> int:
    """Copy the Owner's device view for ``binding`` into the list for ``hostname``.

    Runs inside the sign-out transaction, before the session rows are deleted.
    """

    from storage import device_registry

    devices = device_registry.list_devices(binding, None, owner=True, caller=None, now=now, conn=conn)
    # An unattributed push row made on this computer's local address holds its
    # data under that address, not under the retired one.
    remote_push = {
        row.id
        for row in conn.execute(
            select(web_push_subscriptions.c.id).where(web_push_subscriptions.c.user_key.like("remote:%"))
        )
    }
    written = 0
    for device in devices:
        parts = str(device["id"]).split(".")
        if len(parts) == 3 and parts[0] == "p" and parts[1] not in remote_push:
            continue
        account = device.get("account") or {}
        result = conn.execute(
            insert(clearings)
            .values(
                id=_row_id(instance_id, hostname, device["id"]),
                instance_id=instance_id,
                hostname=hostname,
                device_id=device["id"],
                kind=device.get("kind") or "browser",
                display_name=device.get("display_name"),
                email=account.get("email"),
                last_active_at=device.get("last_active_at"),
                push_only=1 if device.get("push_only") else 0,
                retired_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        written += result.rowcount or 0
    ids = {
        row.device_id: row.id
        for row in conn.execute(
            select(clearings.c.id, clearings.c.device_id).where(
                clearings.c.instance_id == instance_id, clearings.c.hostname == hostname
            )
        )
    }
    # A session whose cookie already lapsed cannot present its id again.
    live = conn.execute(
        select(sessions.c.session_id, sessions.c.device_id).where(
            sessions.c.binding == binding, sessions.c.cookie_exp > now, sessions.c.expires_at > now
        )
    ).all()
    for row in live:
        clearing_id = ids.get(row.device_id)
        if clearing_id is None:
            continue
        conn.execute(
            insert(receipts)
            .values(receipt_hash=receipt_hash_for(row.session_id), clearing_id=clearing_id)
            .on_conflict_do_nothing(index_elements=["receipt_hash"])
        )
    return written


def receipt_is_listed(conn, *, instance_id: str, hostname: str, receipt_hash: str) -> bool:
    return (
        conn.execute(
            select(clearings.c.id)
            .select_from(receipts.join(clearings, clearings.c.id == receipts.c.clearing_id))
            .where(
                receipts.c.receipt_hash == receipt_hash,
                clearings.c.instance_id == instance_id,
                clearings.c.hostname == hostname,
            )
            .limit(1)
        ).first()
        is not None
    )


def mark_cleared(conn, *, instance_id: str, hostname: str, receipt_hash: str, now: int) -> bool:
    """Mark the receipt's own device, once. A second report changes nothing."""

    target = (
        select(receipts.c.clearing_id).where(receipts.c.receipt_hash == receipt_hash).scalar_subquery()
    )
    result = conn.execute(
        clearings.update()
        .where(
            clearings.c.id == target,
            clearings.c.instance_id == instance_id,
            clearings.c.hostname == hostname,
            clearings.c.cleared_at.is_(None),
        )
        .values(cleared_at=now)
    )
    return bool(result.rowcount)


def list_for(conn, *, instance_id: str, hostnames: Iterable[str]) -> dict[str, list[dict]]:
    names = list(hostnames)
    grouped: dict[str, list[dict]] = {name: [] for name in names}
    if not names:
        return grouped
    rows = conn.execute(
        select(clearings)
        .where(clearings.c.instance_id == instance_id, clearings.c.hostname.in_(names))
        .order_by(func.coalesce(clearings.c.last_active_at, 0).desc(), clearings.c.id)
    ).mappings()
    for row in rows:
        grouped[row["hostname"]].append(
            {
                "id": row["device_id"],
                "kind": row["kind"],
                "display_name": row["display_name"],
                "account": {"email": row["email"]} if row["email"] else None,
                "last_active_at": row["last_active_at"],
                "push_only": bool(row["push_only"]),
                "state": "cleared" if row["cleared_at"] is not None else "unconfirmed",
                "cleared_at": row["cleared_at"],
                "retired_at": row["retired_at"],
            }
        )
    return grouped


def forget_except(conn, *, instance_id: str, keep_hostnames: Iterable[str]) -> int:
    """Delete this instance's lists for names that are no longer cleanup names (F4)."""

    keep = list(keep_hostnames)
    stale = select(clearings.c.id).where(clearings.c.instance_id == instance_id)
    if keep:
        stale = stale.where(clearings.c.hostname.notin_(keep))
    conn.execute(receipts.delete().where(receipts.c.clearing_id.in_(stale)))
    condition = [clearings.c.instance_id == instance_id]
    if keep:
        condition.append(clearings.c.hostname.notin_(keep))
    return conn.execute(clearings.delete().where(*condition)).rowcount or 0

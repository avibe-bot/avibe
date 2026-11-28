"""One-use native Health proofs. Only hashes and pairing references enter the registry."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import secrets
import time

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from core.mobile_health_contract import MobileError
from storage.db import get_cached_sqlite_engine
from storage.mobile_store import owner_matches
from storage.models import device_health_links as links, remote_device_sessions as sessions, device_session_extensions as ext
from vibe.device_metadata import device_key


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def issue(store, device, binding):
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        with store.transaction() as mobile:
            store._ensure_active(mobile, device["id"])
            if conn.scalar(select(links.c.cleanup_pending).where(links.c.mobile_id == device['id'])):
                raise MobileError('device_revoked', 403)
            now = int(time.time())
            token = secrets.token_urlsafe(32)
            values = dict(ticket_hash=digest(token), ticket_binding=binding, issued_at=now, expires_at=now + 120)
            stmt = insert(links).values(mobile_id=device["id"], **values)
            written = conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["mobile_id"],
                    set_=values,
                    where=links.c.issued_at.is_(None) | (links.c.issued_at <= now - 30),
                )
            )
            if not written.rowcount:
                raise MobileError("binding_ticket_rate_limited", 429)
    return {"health_binding_ticket": token, "expires_at": datetime.fromtimestamp(now + 120, timezone.utc).isoformat()}


def redeem(store, viewer, context, report, ticket, now):
    from storage import device_registry

    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket) or report["kind"] != "iphone_app":
        return 400, "invalid_device_description"
    binding, subject, _, sid = viewer
    if not (
        sid and subject and context.is_remote and context.is_instance_owner and context.instance_id
    ):
        return 403, "health_binding_not_accepted"
    target = device_key(binding, subject, "installation", report["installation_id"])
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        row = (
            conn.execute(
                select(sessions, ext.c.ticket_window, ext.c.ticket_attempts).select_from(
                    sessions.outerjoin(ext, ext.c.session_id == sessions.c.session_id)
                ).where(
                    sessions.c.session_id == sid,
                    sessions.c.binding == binding,
                    sessions.c.subject == subject,
                    sessions.c.cookie_exp > now,
                    sessions.c.expires_at > now,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return 404, "device_not_found"
        window = row["ticket_window"] or 0
        count = row["ticket_attempts"] or 0
        if now >= window + 60:
            window, count = now, 0
        if count >= 5:
            return 429, "health_binding_rate_limited"
        counters = dict(ticket_window=window, ticket_attempts=count + 1)
        conn.execute(insert(ext).values(session_id=sid, **counters).on_conflict_do_update(
            index_elements=[ext.c.session_id], set_=counters))
        proof = (
            conn.execute(
                select(links).where(
                    links.c.ticket_hash == digest(ticket), links.c.ticket_binding == binding, links.c.expires_at > now,
                    links.c.cleanup_pending.is_(None),
                )
            )
            .mappings()
            .first()
        )
        if not proof or (
            proof["device_id"] is not None
            and (proof["binding"], proof["subject"], proof["device_id"])
            not in ((binding, subject, target), (binding, subject, row["device_id"]))
        ):
            return 403, "health_binding_not_accepted"
        with store.transaction() as mobile:
            valid = mobile.execute(
                "SELECT * FROM devices WHERE id=? AND revoked_at IS NULL",
                (proof["mobile_id"],),
            ).fetchone()
            if valid is None or not owner_matches(valid, {
                "id": context.instance_id, "owner": (context.email or "").casefold(),
                "owner_subject": subject if valid["owner_subject"] is not None else None,
            }):
                return 403, "health_binding_not_accepted"
            status = device_registry.describe_in_transaction(conn, binding, subject, sid, report, now)
            if status != 200:
                return status, {
                    404: "device_not_found",
                    409: "installation_mismatch",
                    429: "device_report_rate_limited",
                }[status]
            linked = proof["linked_at"] or datetime.fromtimestamp(now, timezone.utc).isoformat()
            conn.execute(
                links.update()
                .where(links.c.mobile_id == proof["mobile_id"])
                .values(
                    binding=binding,
                    subject=subject,
                    device_id=target,
                    linked_at=linked,
                    ticket_hash=None,
                    ticket_binding=None,
                    expires_at=None,
                )
            )
    return 200, None

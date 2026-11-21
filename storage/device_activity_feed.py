"""Read-through activity projection. Page the three sources independently after actor scoping."""

import base64
from datetime import datetime, timezone
import json
import time

from sqlalchemy import and_, or_, select

from core.mobile_health_contract import METRICS
from storage.device_activity import RETENTION_SECONDS
from storage.device_functions import DeviceError, live_caller
from storage.mobile_store import MobileStore, mobile_store_path
from storage.models import (
    device_activity_events as events,
    device_health_links as links,
    device_schedule_links as schedule_links,
    system_schedule_operations as ops,
    system_schedule_attempts as attempts,
)
from storage.workbench_sessions_service import get_active_session


def instant(s):
    return datetime.fromtimestamp(s, timezone.utc)


def microseconds(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp()) * 1000000 + dt.microsecond


def parse_cursor(raw):
    if raw is None:
        return None
    try:
        if not isinstance(raw, str) or len(raw) > 300:
            raise ValueError
        t, source, key = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
        if (
            type(t) is not int
            or t < 0
            or t > 2**53
            or source not in ("e", "h", "s")
            or type(key) is not int
            or key < 1
            or key >= 2**63
        ):
            raise ValueError
        return t, source, key
    except (ValueError, TypeError, UnicodeError):
        raise DeviceError("invalid_activity_cursor", 400) from None


def window(cursor, source, quantum, stamp):
    """A source may timestamp only to seconds or millis. Floor before comparing ties."""
    ts, src, key = cursor
    return stamp(ts // quantum * quantum), 2**63 - 1 if ts % quantum or source < src else (key if source == src else 0)


def list_page(conn, viewer, context, instance, owner_email, *, limit, raw_cursor, device=None, health_only=False):
    now = int(time.time())
    live_caller(conn, viewer, now)
    if not 1 <= limit <= 100:
        raise DeviceError("invalid_activity_limit", 400)
    cursor = parse_cursor(raw_cursor)
    binding, subject, owner, _ = viewer
    cutoff = now - RETENTION_SECONDS
    merged = []

    def add(at, source, k, **fields):
        key = (at, source, k)
        merged.append((key, dict(id=f"{source}.{k}", at=instant(at / 1e6).isoformat(), **fields)))

    def page(stmt, ts, pk, source, q, stamp):
        if cursor:
            bound, tie = window(cursor, source, q, stamp)
            stmt = stmt.where(or_(ts < bound, and_(ts == bound, pk < tie)))
        return conn.execute(stmt.order_by(ts.desc(), pk.desc()).limit(limit + 1)).mappings()

    if not health_only:
        stmt = select(events).where(events.c.binding == binding, events.c.at > cutoff)
        if not owner:
            stmt = stmt.where(events.c.subject == subject)
        if device:
            stmt = stmt.where(events.c.device_id == device)
        for r in page(stmt, events.c.at, events.c.id, "e", 1000000, lambda t: t // 1000000):
            extra = (
                {"actor_device_id": r["actor_id"] if owner or r["actor_subject"] == subject else None}
                if r["kind"] == "signedOut"
                else {}
            )
            add(r["at"] * 1000000, "e", r["id"], kind=r["kind"], device_id=r["device_id"], **extra)

        stmt = (
            select(
                schedule_links.c.registry_activity_id,
                schedule_links.c.registry_device,
                schedule_links.c.registry_subject,
                ops.c.session_id,
                ops.c.kind,
                ops.c.created_at,
                ops.c.title,
                attempts.c.terminal_state,
            )
            .join_from(
                ops, schedule_links, schedule_links.c.operation_id == ops.c.operation_id,
            )
            .join_from(
                ops,
                attempts,
                and_(
                    ops.c.operation_id == attempts.c.operation_id,
                    attempts.c.action == "create",
                    attempts.c.generation == 1,
                ),
            )
            .where(
                schedule_links.c.registry_binding == binding,
                schedule_links.c.registry_activity_id.is_not(None),
                ops.c.created_at > instant(cutoff).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
        )
        if not owner:
            stmt = stmt.where(schedule_links.c.registry_subject == subject)
        if device:
            stmt = stmt.where(schedule_links.c.registry_device == device)
        for r in page(
            stmt,
            ops.c.created_at,
            schedule_links.c.registry_activity_id,
            "s",
            1000,
            lambda t: instant(t / 1e6).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ):
            extra = {}
            own = subject is not None and r["registry_subject"] == subject
            if not subject and owner_email:
                from storage.models import remote_device_sessions as sessions

                own = bool(
                    conn.scalar(
                        select(sessions.c.session_id)
                        .where(
                            sessions.c.binding == binding,
                            sessions.c.device_id == r["registry_device"],
                            sessions.c.subject == r["registry_subject"],
                            sessions.c.email.collate("NOCASE") == owner_email,
                            sessions.c.cookie_exp > now,
                            sessions.c.expires_at > now,
                        )
                        .limit(1)
                    )
                )
            if own:
                try:
                    get_active_session(conn, r["session_id"], authorization_context=context)
                    extra["label"] = r["title"]
                except LookupError:
                    pass
            add(
                microseconds(r["created_at"]),
                "s",
                r["registry_activity_id"],
                kind="alarmCreated" if r["kind"] == "alarm" else "reminderCreated",
                device_id=r["registry_device"],
                confirmed=r["terminal_state"] == "scheduled",
                **extra,
            )

    if owner and instance and owner_email and mobile_store_path().exists():
        # Health is owner-only. Restrict both the proven registry links AND private
        # pairing's actual owner/instance before pagination. An old audit without a
        # proven pairing never gains a device through a guess based on time.
        stmt = select(links.c.mobile_id, links.c.device_id, links.c.linked_at).where(
            links.c.binding == binding, links.c.linked_at.is_not(None)
        )
        if subject:
            stmt = stmt.where(links.c.subject == subject)
        if device:
            stmt = stmt.where(links.c.device_id == device)
        linked = {r.mobile_id: (r.device_id, r.linked_at) for r in conn.execute(stmt)}
        if linked:
            with MobileStore().transaction() as m:
                sql = """WITH proof(mid,linked) AS (SELECT key,value FROM json_each(?))
                    SELECT a.id,a.at,a.device_id,a.metrics,a.from_day,a.to_day,a.session_id FROM accesses a
                    JOIN proof ON proof.mid=a.device_id AND a.at>=proof.linked JOIN devices d ON d.id=a.device_id
                    WHERE d.instance_id=? AND d.owner=? AND a.operation='health.read' AND a.caller!='local' AND a.at>?"""
                params = [
                    json.dumps({k: v[1] for k, v in linked.items()}),
                    instance,
                    owner_email,
                    instant(cutoff).isoformat(),
                ]
                if cursor:
                    bound, tie = window(cursor, "h", 1, lambda t: instant(t / 1e6).isoformat())
                    sql += " AND (a.at<? OR (a.at=? AND a.id<?))"
                    params.extend([bound, bound, tie])
                rows = m.execute(sql + " ORDER BY a.at DESC,a.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            for r in rows:
                extra = {}
                if r["session_id"]:
                    try:
                        title = get_active_session(conn, r["session_id"], authorization_context=context).get("title")
                        if title:
                            extra["task"] = title
                    except LookupError:
                        pass
                kinds = json.loads(r["metrics"])
                add(
                    microseconds(r["at"]),
                    "h",
                    r["id"],
                    kind="healthRead",
                    device_id=linked[r["device_id"]][0],
                    data=[k for k in METRICS if k in kinds],
                    from_day=r["from_day"],
                    to_day=r["to_day"],
                    **extra,
                )
    merged.sort(key=lambda pair: pair[0], reverse=True)
    token = (
        base64.urlsafe_b64encode(json.dumps(merged[limit - 1][0]).encode()).decode() if len(merged) > limit else None
    )
    return {"items": [item for _, item in merged[:limit]], "next_cursor": token}

"""Instance-local device lifecycle history. Never accept arbitrary event payloads."""

from __future__ import annotations

from sqlalchemy import select

from storage.models import device_activity_events as events

RETENTION_SECONDS = 90 * 86400
KINDS = frozenset({"signedIn", "signedOut", "remoteAccessOn", "remoteAccessOff"})


def record(conn, *, kind, binding, subject, device, now, actor=None, actor_subject=None):
    if kind not in KINDS:
        raise ValueError("invalid_device_event")
    conn.execute(
        events.insert().values(
            at=now,
            kind=kind,
            binding=binding,
            subject=subject,
            device_id=device,
            actor_id=actor,
            actor_subject=actor_subject,
        )
    )


def prune(engine, now, cancel=None):
    """Short transactions, indexed by time; the controller calls this immediately on startup."""
    while cancel is None or not cancel.is_set():
        with engine.begin() as conn:
            batch = select(events.c.id).where(events.c.at <= now - RETENTION_SECONDS).order_by(events.c.at).limit(256)
            count = conn.execute(events.delete().where(events.c.id.in_(batch))).rowcount
        if count < 256:
            break

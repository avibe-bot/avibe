"""Check the merged feed against its three actual ledgers, including a three-way tie."""

from datetime import datetime, timezone
import time

from sqlalchemy import select

from storage import device_activity as ledger, device_activity_feed as feed, device_health_registry as health
from storage.db import get_cached_sqlite_engine
from storage.mobile_store import MobileStore
from storage.models import (
    agent_sessions,
    device_activity_events as events,
    device_health_links as links,
    device_schedule_links as sl,
    system_schedule_operations as ops,
)
from tests.test_device_health_cleanup import phone, code_for
from tests.test_instance_device_registry import install  # noqa: F401
from tests.test_system_schedule_service import _create, isolated_state  # noqa: F401
from vibe.authorization import AuthorizationContext


def test_scoped_page_title_label_and_retention(isolated_state, install):
    # Keep the three-way tie older than sign-in, regardless of setup duration.
    stamp = int(time.time()) - 2
    _, viewer, key = phone(install, "feed")
    store = MobileStore()
    identity, payload = code_for(store, viewer)
    mid = health.pair(store, payload, identity)["device_id"]
    at = datetime.fromtimestamp(stamp, timezone.utc)
    engine = get_cached_sqlite_engine()
    _create()
    excluded = stamp - ledger.RETENTION_SECONDS
    with engine.begin() as conn:
        conn.execute(
            ops.update()
            .where(ops.c.operation_id == "wake-up")
            .values(created_at=at.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        )
        conn.execute(
            sl.insert().values(
                operation_id="wake-up",
                registry_activity_id=37,
                registry_device=key,
                registry_subject="owner",
                registry_binding=viewer[0],
            )
        )
        conn.execute(links.update().where(links.c.mobile_id == mid).values(linked_at=at.isoformat()))
        for when in (stamp, excluded):
            ledger.record(conn, kind="remoteAccessOn", binding=viewer[0], subject="owner", device=key, now=when)
    with store.transaction() as mobile:
        mobile.execute(
            "INSERT INTO accesses(at,caller,session_id,operation,metrics,from_day,to_day,device_id) VALUES(?,?,?,?,?,?,?,?)",
            (at.isoformat(), "agent", "ses-schedule", "health.read", '["steps"]', "2026-01-01", "2026-01-01", mid),
        )
    owner = AuthorizationContext(instance_role="owner", subject="owner", is_remote=True)
    denied = AuthorizationContext(instance_role="editor", subject="other", is_remote=True)
    loopback = (viewer[0], None, True, None)

    def page(v=viewer, ctx=owner, email=identity["owner"], cursor=None, size=100):
        with engine.connect() as conn:
            return feed.list_page(conn, v, ctx, identity["id"], email, limit=size, raw_cursor=cursor, device=key)

    rows = page()["items"]
    kinds = [r["kind"] for r in rows]
    assert kinds[-3:] == ["alarmCreated", "healthRead", "remoteAccessOn"]
    assert rows[-3]["label"] == "Wake up" and rows[-2]["task"] == "Schedule test"
    ids = []
    cursor = None
    while True:
        part = page(cursor=cursor, size=1)
        ids += [r["id"] for r in part["items"]]
        cursor = part["next_cursor"]
        if cursor is None:
            break
        assert page((viewer[0], "other", False, None), denied, None, cursor)["items"] == []
    assert ids == [r["id"] for r in rows] and len(ids) == len(set(ids))
    assert page(loopback, owner)["items"][-3]["label"] == "Wake up"
    assert page(loopback, owner)["items"][-2]["task"] == "Schedule test"
    assert "label" not in page(loopback, owner, "wrong@example.invalid")["items"][-2]
    hidden = page(viewer, denied)["items"]
    assert "label" not in hidden[-3] and "task" not in hidden[-2]
    with engine.begin() as conn:
        conn.execute(sl.update().where(sl.c.operation_id == "wake-up").values(registry_subject="other"))
    assert "label" not in page()["items"][-3]  # Owner reads Session, does not own the phone.
    with engine.begin() as conn:
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == "ses-schedule").values(status="archived"))
    assert "task" not in page()["items"][-2]
    ledger.prune(engine, stamp)
    with engine.connect() as conn:
        assert conn.scalar(select(events.c.id).where(events.c.at == excluded)) is None
        assert conn.scalar(select(events.c.id).where(events.c.at == stamp)) is not None

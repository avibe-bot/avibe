"""Transitions the page cannot infer from a device name or a prior rendered list."""

import time

import pytest
from sqlalchemy import select

from core.mobile_health_contract import MobileError
from storage import device_functions as gates, device_health_registry as registry
from storage.db import get_cached_sqlite_engine
from storage.mobile_store import MobileStore
from storage.models import (
    device_activity_events as events,
    device_function_settings as prefs,
    device_health_links as links,
    remote_device_sessions as sessions,
)
from tests.test_device_health_cleanup import phone, code_for
from tests.test_instance_device_registry import ORIGIN, client, install, listing, login, report, sign_out  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access


def test_logout_invalidates_issuer_code_but_preserves_pairing_on_live_sibling(install):
    store = MobileStore()
    cookie, viewer, target = phone(install, "same-phone")
    identity, first = code_for(store, viewer)
    mid = registry.pair(store, first, identity)["device_id"]
    # A second request starts only after the first connection has finished.
    with store.transaction() as mobile:
        setup_id = mobile.execute("SELECT id FROM requests WHERE device_id=?", (mid,)).fetchone()[0]
    store.cancel_request(setup_id)
    identity, pending = code_for(store, viewer)
    sibling, _, same_target = phone(install, "same-phone")
    assert same_target == target
    c = client(cookie)
    assert c.post("/auth/logout", json={}, base_url=ORIGIN, headers=csrf_headers(c, ORIGIN)).status_code == 200
    with pytest.raises(MobileError):
        registry.pair(store, pending, identity)
    with store.transaction() as m:
        assert m.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0] is None
    assert remote_access.parse_session_identity(install, sibling) is not None
    other = client(login(install, "owner", "owner"))
    assert sign_out(other, target + "/sign-out").status_code == 200
    with store.transaction() as m:
        assert m.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0]


def test_promotion_moves_disabled_preferences_proof_and_activity(install):
    store = MobileStore()
    engine = get_cached_sqlite_engine()
    cookie = login(install, "owner", "owner")
    c = client(cookie)
    sid = remote_access.parse_session_identity(install, cookie)["browser_session_id"]
    viewer = (remote_access.device_binding(install), "owner", True, sid)
    with engine.begin() as conn:
        old = conn.scalar(select(sessions.c.device_id).where(sessions.c.session_id == sid))
        gates.preference(conn, *viewer[:2], old, schedule_on=0, schedule_capable=1, notifications_on=0)
    identity, payload = code_for(store, viewer)
    mid = registry.pair(store, payload, identity)["device_id"]
    assert report(c, "native-install").status_code == 200
    row = next(r for r in listing(c)["mine"] if r["id"] != "host")
    assert row["id"] != old
    with engine.connect() as conn:
        assert conn.execute(
            select(prefs.c.notifications_on, prefs.c.schedule_on).where(prefs.c.device_id == row["id"])
        ).one() == (0, 0)
        assert conn.scalar(select(links.c.device_id).where(links.c.mobile_id == mid)) == row["id"]
        assert (
            conn.scalar(select(events.c.device_id).where(events.c.kind == "signedIn", events.c.subject == "owner"))
            == row["id"]
        )
    # A stale unlinked deletion no longer names a currently unlinked pairing.
    assert sign_out(c, "unlinked-health/" + mid + "/revoke").status_code == 404
    with store.transaction() as m:
        assert m.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0] is None


def test_expired_unused_code_rows_are_pruned_without_losing_a_real_pairing(install):
    store = MobileStore()
    engine = get_cached_sqlite_engine()
    _, viewer, _ = phone(install, "issuer")
    identity, first = code_for(store, viewer)
    _, unused = code_for(store, viewer)
    assert first["pairing_code"] != unused["pairing_code"]
    mid = registry.pair(store, first, identity)["device_id"]
    registry.maintain(engine, int(time.time()) + 600)
    with engine.connect() as conn:
        assert conn.scalars(select(links.c.mobile_id)).all() == [mid]

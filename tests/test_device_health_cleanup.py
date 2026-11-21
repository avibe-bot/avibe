"""Device fencing and the private Health cleanup journal use two real SQLite files."""

import time

import pytest
from sqlalchemy import select

from storage import device_health_registry as registry
from storage.db import get_cached_sqlite_engine
from storage.mobile_store import MobileStore
from storage.models import device_health_links as links, remote_device_sessions as sessions
from tests.test_instance_device_registry import ORIGIN, client, install, listing, login, sign_out  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access


def phone(config, name):
    cookie = login(config, "owner", "owner", installation=name)
    sid = remote_access.parse_session_identity(config, cookie)["browser_session_id"]
    with get_cached_sqlite_engine().connect() as conn:
        row = conn.execute(select(sessions).where(sessions.c.session_id == sid)).mappings().one()
    return cookie, (row["binding"], row["subject"], True, sid), row["device_id"]


def code_for(store, viewer):
    identity = {"id": "inst_123", "owner": "owner@test.invalid", "name": "Fixture"}
    request, _ = store.create_request("health.connect", identity, names=["steps"])
    code = registry.issue_pairing(store, request["id"], identity, viewer)["pairing_code"]
    return identity, {
        "pairing_code": code,
        "device_name": "Fixture",
        "platform": "ios",
        "app_version": "1",
        "metrics": ["steps"],
        "installation_id": "fixture-" + request["id"],
    }


@pytest.mark.parametrize("bulk", [False, True])
def test_expired_cookie_pair_survives_signout_and_stays_individually_removable(install, bulk):
    store = MobileStore()
    _, viewer, target = phone(install, "old")
    identity, payload = code_for(store, viewer)
    mid = registry.pair(store, payload, identity)["device_id"]
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(
            sessions.update().where(sessions.c.session_id == viewer[3]).values(cookie_exp=int(time.time()) - 1)
        )
    owner = client(login(install, "owner", "owner"))
    assert [p["pairing_id"] for p in listing(owner)["unlinked_health"]] == [mid]
    response = sign_out(owner, "sign-out-others" if bulk else target + "/sign-out")
    assert response.status_code in (200, 404)
    assert [p["pairing_id"] for p in listing(owner)["unlinked_health"]] == [mid]
    response = sign_out(owner, "unlinked-health/" + mid + "/revoke")
    assert response.status_code == 200 and listing(owner)["unlinked_health"] == []


def test_missing_private_store_is_not_replaced_by_empty_cleanup(install):
    store = MobileStore()
    _, viewer, target = phone(install, "lost")
    identity, payload = code_for(store, viewer)
    mid = registry.pair(store, payload, identity)["device_id"]
    other = client(login(install, "owner", "owner"))
    saved = store.path.with_suffix(".temporarily-offline")
    store.path.rename(saved)
    response = sign_out(other, target + "/sign-out")
    assert response.status_code == 200 and response.get_json()["health_cleanup_pending"]
    assert not store.path.exists()
    saved.rename(store.path)
    registry.maintain(get_cached_sqlite_engine(), int(time.time()))
    with store.transaction() as mobile:
        assert mobile.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0]


@pytest.mark.parametrize("logout", [False, True])
def test_fence_commits_before_unavailable_health_cleanup_and_maintenance_retries(install, monkeypatch, logout):
    store = MobileStore()
    cookie, viewer, target = phone(install, "lost")
    identity, payload = code_for(store, viewer)
    mid = registry.pair(store, payload, identity)["device_id"]
    other = client(login(install, "owner", "owner"))
    engine = get_cached_sqlite_engine()
    observed = []

    def unavailable(**_):
        # A separate reader sees the tombstone even before the private DB is opened.
        with engine.connect() as conn:
            observed.append(conn.scalar(select(sessions.c.session_id).where(sessions.c.session_id == viewer[3])))
        raise OSError("unavailable")

    with monkeypatch.context() as m:
        m.setattr(registry, "MobileStore", unavailable)
        if logout:
            c = client(cookie)
            response = c.post("/auth/logout", json={}, base_url=ORIGIN, headers=csrf_headers(c, ORIGIN))
        else:
            response = sign_out(other, target + "/sign-out")
        assert response.status_code == 200 and response.get_json()["health_cleanup_pending"] is True
    assert observed == [None]
    assert remote_access.parse_session_identity(install, cookie) is None
    with engine.connect() as conn:
        assert conn.scalar(select(links.c.cleanup_pending).where(links.c.mobile_id == mid)) == 1
    registry.maintain(engine, int(time.time()))
    registry.maintain(engine, int(time.time()))
    with store.transaction() as mobile:
        assert mobile.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0]
    with engine.connect() as conn:
        assert conn.scalar(select(links.c.cleanup_pending).where(links.c.mobile_id == mid)) is None

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


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("proof", ["unavailable", "other-owner", "verified"])
def test_loopback_signout_fences_without_purging_unverified_health(install, monkeypatch, bulk, proof):
    from types import SimpleNamespace
    from vibe import permissions, ui_server

    store = MobileStore()
    cookie, viewer, target = phone(install, "local-owner-phone")
    install.remote_access.vibe_cloud.instance_secret = "fixture-runtime-credential"
    identity, payload = code_for(store, viewer)
    mid = registry.pair(store, payload, identity)["device_id"]
    _, unused = code_for(store, viewer)
    local, origin = ui_server.app.test_client(), "http://127.0.0.1:5123"

    with monkeypatch.context() as m:
        if proof != "verified":

            def permissions_answer(*_):
                if proof == "unavailable":
                    raise permissions.PermissionsError("offline")
                return SimpleNamespace(
                    source="live",
                    projection={
                        "instance": {"id": identity["id"]},
                        "access": {"owner": {"email": "other@test.invalid"}},
                    },
                )

            m.setattr(permissions, "get_current_permissions", permissions_answer)
        row = next(r for r in listing(local, origin)["mine"] if r["id"] == target)
        assert any(f["kind"] == "health" for f in row["functions"]) == (proof == "verified")
        # The route may not even try to obtain a live owner proof; worker runs after return.
        with monkeypatch.context() as route_only:
            route_only.setattr(permissions, 'get_current_permissions', lambda *_: pytest.fail('cloud in sign-out'))
            response = sign_out(local, "sign-out-others" if bulk else target + "/sign-out", origin)
        assert response.status_code == 200
        result = response.get_json()
        assert result['revoked_sessions'] == 1 and result['health_cleanup_pending']
        with store.transaction() as mobile:
            assert not mobile.execute('SELECT revoked_at FROM devices WHERE id=?', (mid,)).fetchone()[0]
        from storage import device_health_owner_checks
        device_health_owner_checks.run_once(lambda: install)
    assert remote_access.parse_session_identity(install, cookie) is None
    with store.transaction() as mobile:
        assert bool(mobile.execute("SELECT revoked_at FROM devices WHERE id=?", (mid,)).fetchone()[0]) == (
            proof == "verified"
        )
        assert not mobile.execute(
            "SELECT 1 FROM pairings WHERE code_hash=?", (registry.digest(unused["pairing_code"]),)
        ).fetchone()
    if proof != "verified":
        assert [p["pairing_id"] for p in listing(local, origin)["unlinked_health"]] == [mid]
        assert sign_out(local, "unlinked-health/" + mid + "/revoke", origin).status_code == 200


@pytest.mark.parametrize('change', ['binding', 'expired', 'new_login', 'different_link', 'no_pairing'])
def test_deferred_loopback_cleanup_does_not_retarget(install, monkeypatch, change):
    from storage import device_health_owner_checks as worker
    from storage.models import device_health_owner_checks as queue
    from vibe import permissions, ui_server
    store = MobileStore()
    _, viewer, target = phone(install, 'phone')
    mid = None
    if change != 'no_pairing':
        identity, payload = code_for(store, viewer)
        mid = registry.pair(store, payload, identity)['device_id']
    install.remote_access.vibe_cloud.instance_secret = 'test-device-secret'
    def no_cloud(*_):
        pytest.fail('unexpected owner lookup')
    with monkeypatch.context() as m:
        m.setattr(permissions, 'get_current_permissions', no_cloud)
        assert sign_out(ui_server.app.test_client(), target + '/sign-out', 'http://127.0.0.1:5123').status_code == 200
    if change in ('binding', 'expired', 'no_pairing'):
        monkeypatch.setattr(permissions, 'get_current_permissions', no_cloud)
    if change == 'binding':
        install.remote_access.vibe_cloud.instance_secret = 'replacement-device-secret'
    elif change == 'new_login':
        install.save()
        phone(install, 'phone')
    elif change in ('expired', 'different_link'):
        with get_cached_sqlite_engine().begin() as conn:
            if change == 'expired':
                conn.execute(queue.update().values(expires_at=int(time.time()) - 1))
            else:
                conn.execute(links.update().where(links.c.mobile_id == mid).values(linked_at='new-link'))
    worker.run_once(lambda: install)
    with get_cached_sqlite_engine().connect() as conn:
        assert not conn.scalar(select(queue.c.mobile_id))
    if mid:
        with store.transaction() as mobile:
            assert mobile.execute('SELECT revoked_at FROM devices WHERE id=?', (mid,)).fetchone()[0] is None


def test_deferred_private_store_failure_expires_without_erasure(install, monkeypatch):
    from storage import device_health_owner_checks as worker
    from storage.models import device_health_owner_checks as checks
    from vibe import ui_server
    store = MobileStore()
    _, viewer, target = phone(install, 'phone')
    identity, payload = code_for(store, viewer)
    mid = registry.pair(store, payload, identity)['device_id']
    install.remote_access.vibe_cloud.instance_secret = 'test-device-secret'
    local, origin = ui_server.app.test_client(), 'http://127.0.0.1:5123'
    assert sign_out(local, target + '/sign-out', origin).get_json()['health_cleanup_pending']
    def unavailable(*args, **kwargs):
        raise OSError('private_store_unavailable')
    with monkeypatch.context() as m:
        m.setattr(MobileStore, 'revoke_confirmed_pairing', unavailable)
        with pytest.raises(OSError):
            worker.run_once(lambda: install)
    with get_cached_sqlite_engine().begin() as conn:
        assert conn.scalar(select(checks.c.mobile_id)) == mid
        conn.execute(checks.update().values(next_at=0, expires_at=0))
    worker.run_once(lambda: install)
    assert [p['pairing_id'] for p in listing(local, origin)['unlinked_health']] == [mid]
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(checks.c.mobile_id)) is None


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

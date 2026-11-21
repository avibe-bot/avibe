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
        response = sign_out(local, "sign-out-others" if bulk else target + "/sign-out", origin)
        assert response.status_code == 200
        result = response.get_json()
        assert result["revoked_sessions"] == 1
        if proof == "unavailable":
            assert result["health_pairing_cleanup_skipped"] == "current_owner_unverified"
        else:
            assert "health_pairing_cleanup_skipped" not in result
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

"""HTTP/session contract: role filtering, same-device grouping, old cookie and rollback admission."""

import json
import time
import urllib.parse

import pytest
from sqlalchemy import event, select

from storage.db import get_cached_sqlite_engine
from storage.models import remote_device_sessions, state_meta
from storage.remote_access_authorization_service import REVOKED_BROWSER_SESSION_KEY_PREFIX
from tests.test_remote_access_vibe_cloud import _config, _session_claims
from tests.ui_server_test_helpers import csrf_headers
from vibe import remote_access, ui_server

ORIGIN = "https://alex.avibe.bot"


@pytest.fixture
def install(monkeypatch):
    config = _config()
    monkeypatch.setattr(ui_server, "_load_remote_access_config", lambda: config)
    return config


def login(cfg, subject, role, *, installation=None):
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/100.0 Safari/537.36"}
    if installation:
        headers.update(
            {
                "X-Avibe-Device-Name": "Work device",
                "X-Avibe-Device-Kind": "desktop_app",
                "X-Avibe-Installation-ID": installation,
            }
        )
    return remote_access.make_session_cookie(
        cfg, subject + "@test.invalid", subject, session_claims=_session_claims(cfg, role=role), device_headers=headers
    )


def client(cookie):
    c = ui_server.app.test_client()
    c.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    return c


def listing(c, origin=ORIGIN):
    response = c.get("/api/devices", base_url=origin)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def sign_out(c, target, origin=ORIGIN):
    return c.post("/api/devices/" + target, base_url=origin, headers=csrf_headers(c, origin), json={})


def test_device_action_selects_binding_and_subject_not_reported_metadata(install, monkeypatch):
    owner = login(install, "owner", "owner", installation="one")
    first = login(install, "member", "member", installation="one")
    second = login(install, "member", "member", installation="one")
    member_view = listing(client(first))
    assert member_view["others"] == []
    (device,) = member_view["mine"]
    assert device["account"]["subject"] == "member" and device["current"]
    owner_view = listing(client(owner))
    host, own = owner_view["mine"]
    assert host["id"] == "host" and own["id"] != device["id"]
    assert [row["id"] for row in owner_view["others"]] == [device["id"]]
    assert sign_out(client(first), own["id"] + "/sign-out").status_code == 404
    assert sign_out(client(owner), "host/sign-out").status_code == 400
    published = []
    from vibe.sse_broker import broker

    monkeypatch.setattr(broker, "publish", lambda name, data: published.append((name, data)))
    assert sign_out(client(owner), device["id"] + "/sign-out").get_json() == {"ok": True, "revoked_sessions": 2}
    assert len([e for e, data in published if e == "authorization.session-revoked"]) == 2
    assert remote_access.parse_session_identity(install, first) is None
    assert remote_access.parse_session_identity(install, second) is None
    assert listing(client(owner))["others"] == []


def _pre_registry_cookie(config, issued_cookie):
    # Reconstruct the released representation: a legitimate cookie but no registry entry.
    payload = json.loads(urllib.parse.unquote(issued_cookie.rsplit(".", 1)[0]))
    payload.pop("device_registered")
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(
            remote_device_sessions.delete().where(remote_device_sessions.c.session_id == payload["browser_session_id"])
        )
    return remote_access._encode_session_cookie(config.remote_access.vibe_cloud.session_secret, payload)


def test_bulk_fence_covers_unseen_and_racing_legacy_cookie_preserves_caller(install):
    caller = login(install, "member", "member")
    known = login(install, "member", "member")
    known_id = remote_access.parse_session_identity(install, known)["browser_session_id"]
    legacy = _pre_registry_cookie(install, login(install, "member", "member"))
    in_flight = remote_access.parse_session_identity(install, legacy)
    other = login(install, "owner", "owner")
    assert sign_out(client(caller), "sign-out-others").get_json() == {"ok": True, "revoked_sessions": 1}
    raced = remote_access.renew_session_cookie(install, in_flight)
    for cookie in (legacy, raced, known):
        assert remote_access.parse_session_identity(install, cookie) is None
    # The exact, pre-registry SID lookup remains sufficient for previously enrolled sessions.
    with get_cached_sqlite_engine().connect() as conn:
        key = REVOKED_BROWSER_SESSION_KEY_PREFIX + known_id
        assert int(conn.scalar(select(state_meta.c.value_json).where(state_meta.c.key == key))) > time.time()
    for cookie in (caller, other, login(install, "member", "member")):
        assert remote_access.parse_session_identity(install, cookie)
    local = ui_server.app.test_client()
    local_origin = "http://127.0.0.1:5123"
    view = listing(local, local_origin)
    assert view["bulk_action"] == "all_remote"
    assert [r["id"] for r in view["mine"][:1]] == ["host"]
    assert [r["account"]["subject"] for r in view["mine"][1:]] == ["owner"]
    unseen = _pre_registry_cookie(install, login(install, "guest", "viewer"))
    assert sign_out(local, "sign-out-others", local_origin).status_code == 200
    assert remote_access.parse_session_identity(install, unseen) is None
    assert remote_access.parse_session_identity(install, caller) is None
    assert remote_access.parse_session_identity(install, other) is None


def test_push_identity_survives_login_and_capability_follows_subscription(install):
    def subscribe(c, endpoint):
        return c.post(
            "/api/web-push/subscriptions",
            base_url=ORIGIN,
            headers=csrf_headers(c, ORIGIN),
            json={
                "device_id": "stable-push-id",
                "device_label": "Display only",
                "subscription": {
                    "endpoint": endpoint,
                    "keys": {"auth": "test-auth", "p256dh": "test-public"},
                },
            },
        )

    observer = client(login(install, "observer", "owner"))
    a = client(login(install, "member", "member"))
    b = client(login(install, "member", "member"))
    assert subscribe(a, "https://example.invalid/1").status_code == 200
    assert subscribe(b, "https://example.invalid/2").status_code == 200
    (device,) = listing(observer)["others"]
    assert device["display_name"] == "Chrome · macOS"
    assert device["capabilities"]["push_enabled"] is True
    assert "stable-push-id" not in json.dumps(device)
    from storage.web_push_service import disable_device_subscription

    with get_cached_sqlite_engine().begin() as conn:
        disable_device_subscription(conn, user_key="remote:member", device_id="stable-push-id")
    (disabled,) = listing(observer)["others"]
    assert disabled["id"] == device["id"] and disabled["capabilities"]["push_enabled"] is False


def test_legacy_adoption_and_ordinary_requests_are_read_only_after_first_visit(install):
    cookie = _pre_registry_cookie(install, login(install, "member", "member"))
    c = client(cookie)
    (row,) = listing(c)["mine"]
    assert row["current"]
    engine, observed = get_cached_sqlite_engine(), []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "remote_device_" in statement:
            observed.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert listing(c)["mine"] == [row]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert observed and all(sql.lstrip().upper().startswith("SELECT") for sql, _ in observed)
    with engine.connect() as conn:
        for sql, params in observed:
            plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params).all()
            assert not any("SCAN remote_device" in r[-1] or "SCAN web_push" in r[-1] for r in plan), plan

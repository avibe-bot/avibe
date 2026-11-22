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
    from vibe import permissions
    from types import SimpleNamespace
    monkeypatch.setattr(permissions,'get_current_permissions',lambda *_: SimpleNamespace(source='live',
        projection={'instance':{'id':config.remote_access.vibe_cloud.instance_id},'access':{'owner':{'email':'owner@test.invalid'}}}))
    monkeypatch.setattr(ui_server, "_load_remote_access_config", lambda: config)
    return config


def login(cfg, subject, role, *, installation=None):
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/100.0 Safari/537.36"}
    cookie = remote_access.make_session_cookie(
        cfg, subject + "@test.invalid", subject, session_claims=_session_claims(cfg, role=role), device_headers=headers
    )
    if installation:
        c = client(cookie)
        assert report(c, installation).status_code == 200
    return cookie


def report(c, installation, **fields):
    body = dict(
        kind="iphone_app",
        display_name="Work device",
        installation_id=installation,
        aliases=[],
        app_version="1.0 (42)",
        system_version="iOS 26.1",
    )
    return c.post("/api/devices/self", base_url=ORIGIN, headers=csrf_headers(c, ORIGIN), json={**body, **fields})


def client(cookie):
    c = ui_server.app.test_client()
    c.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    return c


def listing(c, origin=ORIGIN):
    response = c.get("/api/devices", base_url=origin)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def sign_out(c, target, origin=ORIGIN):
    return c.post("/api/devices/" + target, base_url=origin, headers=csrf_headers(c, origin), json={})


def test_device_paths_follow_authenticated_offer_not_device_or_stream_claims(install, monkeypatch, caplog):
    from vibe import direct_rendezvous as direct

    clock = [time.time()]
    rv = direct.DirectRendezvous(clock=lambda: clock[0])
    monkeypatch.setattr(direct, "rendezvous", rv)
    epochs = []
    rv.set_admission_listener(lambda _peer, _ttl, epoch: epochs.append(epoch))
    server, peer = "1" * 64, "2" * 64
    rv.publish(direct.DirectEndpoint(server, ("192.168.1.2:3210",)))
    caller = client(login(install, "member", "member", installation="alpha"))
    other = client(login(install, "member", "member", installation="beta"))
    def paths():
        return {row["current"]: row["current_path"] for row in listing(caller)["mine"]}
    caplog.set_level("INFO", logger="vibe.direct_rendezvous")
    assert caller.post('/api/direct/rendezvous', base_url=ORIGIN, headers=csrf_headers(caller, ORIGIN),
                       json={"version": 1, "client_endpoint_id": peer}).get_json()["status"] == "ready"
    path = {"kind": "lan", "family": "ipv4", "rtt_ms": 8}
    epoch = epochs[-1]
    assert epoch > 0
    rv.observe_path("3" * 64, epoch, 1, "open", path)
    assert paths() == {True: None, False: None}
    rv.observe_path(peer, epoch, 1, "open", path)
    assert paths() == {True: path, False: None}
    assert peer not in json.dumps(listing(other)) and peer not in caplog.text
    rv.observe_path(peer, epoch, 1, "close", path)
    assert paths() == {True: None, False: None}
    rv.observe_path(peer, epoch, 2, "open", path)
    # The session has an independently observed tunnel. Renewing the grant
    # doesn't renew its last path measurement or hide that tunnel indefinitely.
    from types import SimpleNamespace
    from vibe.sse_broker import broker
    sid = rv._admissions[peer].session[1]
    binding = remote_access.device_binding(install)
    monkeypatch.setattr(broker, "tunnel_sessions", lambda b: {("member", sid)} if b == binding else set())
    tick = time.monotonic()
    monkeypatch.setattr(direct, "time", SimpleNamespace(monotonic=lambda: tick + 11))
    assert caller.post('/api/direct/rendezvous', base_url=ORIGIN, headers=csrf_headers(caller, ORIGIN),
                       json={"version": 1, "client_endpoint_id": peer}).get_json()["status"] == "ready"
    assert epochs[-1] == epoch and rv.is_admitted(peer)
    assert paths()[True] == {"kind": "tunnel", "family": None, "rtt_ms": None}
    rv.observe_path(peer, epoch, 2, "change", path)
    assert paths()[True] == path
    monkeypatch.setattr(broker, "tunnel_sessions", lambda b: set())
    clock[0] += direct.RENDEZVOUS_TTL_SECONDS + 1
    assert paths() == {True: None, False: None}


@pytest.mark.parametrize('next_subject', ['first', 'second'])
def test_conflicting_sessions_are_unattributed_and_old_epochs_cannot_rebind(install, monkeypatch, next_subject):
    from vibe import direct_rendezvous as direct

    now = [1000]
    rv = direct.DirectRendezvous(clock=lambda: now[0])
    monkeypatch.setattr(direct, 'rendezvous', rv)
    peer = 'a' * 64
    rv.publish(direct.DirectEndpoint('b' * 64, ('192.168.1.2:3210',)))
    epochs = []
    rv.set_admission_listener(lambda _peer, _ttl, epoch: epochs.append(epoch))
    first = client(login(install, 'first', 'member', installation='alpha'))
    second = client(login(install, next_subject, 'member', installation='beta'))
    def nominate(c):
        response = c.post('/api/direct/rendezvous', base_url=ORIGIN, headers=csrf_headers(c, ORIGIN),
                          json={'version': 1, 'client_endpoint_id': peer})
        assert response.get_json()['status'] == 'ready'
        return epochs[-1]
    def path_for(c):
        return next(r['current_path'] for r in listing(c)['mine'] if r['current'])
    path = {'kind': 'relay', 'family': None, 'rtt_ms': 23}
    old = nominate(first)
    rv.observe_path(peer, old, 1, 'open', path)
    assert path_for(first) == path
    now[0] += 60
    assert nominate(second) == 0 and rv.is_admitted(peer)
    rv.observe_path(peer, old, 2, 'open', path)
    rv.observe_path(peer, 0, 3, 'open', path)
    assert path_for(first) is None and path_for(second) is None
    # The second session renews while the key is still admitted, after the
    # first session's original deadline. A fresh connection gets that renewal's
    # epoch, not proof of which session holds the key.
    now[0] = 1001 + direct.RENDEZVOUS_TTL_SECONDS
    reconnect_epoch = nominate(second)
    rv.observe_path(peer, reconnect_epoch, 4, 'open', path)
    assert path_for(first) is None and path_for(second) is None
    assert reconnect_epoch == 0 and rv.is_admitted(peer)
    now[0] += direct.RENDEZVOUS_TTL_SECONDS + 1
    assert not rv.is_admitted(peer)
    new = nominate(second)
    assert new and new != old
    rv.observe_path(peer, old, 4, 'open', path)
    assert path_for(second) is None
    rv.observe_path(peer, new, 5, 'open', path)
    assert path_for(first) is None and path_for(second) == path


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
    assert_local_result(sign_out(client(owner), device["id"] + "/sign-out"), 2)
    assert len([e for e, data in published if e == "authorization.session-revoked"]) == 2
    assert remote_access.parse_session_identity(install, first) is None
    assert remote_access.parse_session_identity(install, second) is None
    assert listing(client(owner))["others"] == []


def assert_local_result(response, expected_count):
    result = response.get_json()
    assert response.status_code == 200 and result['ok'] is True
    assert result['revoked_sessions'] == expected_count
    assert all(row['local_status'] == 'ended' for row in result['sign_out_results'])


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
    assert_local_result(sign_out(client(caller), "sign-out-others"), 1)
    raced = remote_access.renew_session_cookie(install, in_flight)
    for cookie in (legacy, raced, known):
        assert remote_access.parse_session_identity(install, cookie) is None
    # The exact, pre-registry SID lookup remains sufficient for previously enrolled sessions.
    with get_cached_sqlite_engine().connect() as conn:
        key = REVOKED_BROWSER_SESSION_KEY_PREFIX + known_id
        assert int(conn.scalar(select(state_meta.c.value_json).where(state_meta.c.key == key))) > time.time()
    for cookie in (caller, other, login(install, "member", "member")):
        assert remote_access.parse_session_identity(install, cookie)
    unseen = _pre_registry_cookie(install, login(install, "guest", "viewer"))
    install.remote_access.vibe_cloud.instance_secret = "fixture-runtime-credential"
    local = ui_server.app.test_client()
    local_origin = "http://127.0.0.1:5123"
    view = listing(local, local_origin)
    assert view["bulk_action"] == "all_remote"
    assert [r["id"] for r in view["mine"][:1]] == ["host"]
    assert [r["account"]["subject"] for r in view["mine"][1:]] == ["owner"]
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
    # A bulk logout preserves the caller's shared registration. Targeting
    # the final session later disables actual delivery, not just the UI join.
    assert sign_out(a, "sign-out-others").get_json()["revoked_sessions"] == 1
    assert listing(a)["mine"][0]["capabilities"]["push_enabled"]
    from storage.web_push_service import count_enabled, disable_device_subscription

    with get_cached_sqlite_engine().begin() as conn:
        disable_device_subscription(conn, user_key="remote:member", device_id="stable-push-id")
    (disabled,) = listing(observer)["others"]
    assert disabled["id"] == device["id"] and disabled["capabilities"]["push_enabled"] is False
    assert subscribe(a, "https://example.invalid/3").status_code == 200
    assert sign_out(observer, device["id"] + "/sign-out").status_code == 200
    with get_cached_sqlite_engine().connect() as conn:
        assert count_enabled(conn, user_key="remote:member") == 0


@pytest.mark.parametrize("path", ["/api/web-push/status", "/api/web-push/subscriptions"])
def test_inflight_push_registration_cannot_restore_signed_out_delivery(install, monkeypatch, path):
    from storage import device_registry, web_push_service

    c = client(login(install, "subject", "member"))
    data = {
        "device_id": "installation-push",
        "subscription": {
            "endpoint": "https://example.invalid/registered",
            "keys": {"auth": "test-auth", "p256dh": "test-public"},
        },
    }

    def post(route):
        return c.post(route, base_url=ORIGIN, headers=csrf_headers(c, ORIGIN), json=data)

    assert post("/api/web-push/subscriptions").status_code == 200
    target = listing(c)["mine"][0]["id"]
    original = device_registry.begin_push_change

    def revoke_before_transaction(conn, identity, *, now):
        # Force the ordering: HTTP authorization succeeded, then sign-out committed,
        # then the registration handler tries to mutate the subscription.
        _, count, _, _ = device_registry.sign_out(
            identity["_device_binding"],
            "subject",
            owner=False,
            caller=identity["browser_session_id"],
            device_id=target,
            now=now,
            lifetime=remote_access.SESSION_ABSOLUTE_TTL_SECONDS,
        )
        assert count == 1
        return original(conn, identity, now=now)

    monkeypatch.setattr(device_registry, "begin_push_change", revoke_before_transaction)
    assert post(path).status_code == 401
    with get_cached_sqlite_engine().connect() as conn:
        assert web_push_service.count_enabled(conn, user_key="remote:subject") == 0


def test_notification_pause_survives_sync_and_activity_stays_in_subject_scope(install):
    from storage.web_push_service import count_enabled, list_enabled

    mine = client(login(install, 'my-account', 'viewer'))
    owner = client(login(install, 'other-account', 'owner'))
    payload = {'device_id': 'synthetic-push', 'subscription': {'endpoint': 'https://example.invalid/test',
               'keys': {'auth': 'test', 'p256dh': 'test'}}}
    def send(c, method, path, data):
        return getattr(c, method)(path, base_url=ORIGIN, headers=csrf_headers(c, ORIGIN), json=data)
    assert send(mine, 'post', '/api/web-push/subscriptions', payload).status_code == 200
    ident = listing(mine)['mine'][0]['id']
    route = f'/api/devices/{ident}/notifications'
    assert send(owner, 'put', route, {'on': False}).status_code == 403
    assert send(mine, 'put', route, {'on': False}).get_json()['function']['on'] is False
    assert send(mine, 'post', '/api/web-push/subscriptions', payload).status_code == 200
    with get_cached_sqlite_engine().connect() as conn:
        assert count_enabled(conn, user_key='remote:my-account') == 1
        assert list_enabled(conn, user_key='remote:my-account') == []
    assert send(mine, 'put', route, {'on': True}).get_json()['function']['on'] is True
    items = mine.get('/api/devices/activity', base_url=ORIGIN).get_json()['items']
    assert [(e['kind'], e['device_id']) for e in items] == [('signedIn', ident)]
    assert sign_out(owner, ident + '/sign-out').status_code == 200
    items = owner.get('/api/devices/activity?device_id=' + ident, base_url=ORIGIN).get_json()['items']
    assert [e['kind'] for e in items] == ['signedOut', 'signedIn']
    assert items[0]['actor_device_id'] == listing(owner)['mine'][1]['id']


def test_self_describe_changes_only_own_session_and_checks_reports(install):
    import hashlib

    c = client(login(install, "subject", "viewer"))
    old = listing(c)["mine"][0]
    aliases = [
        {"source": "health", "id": "hc"},
        {"source": "schedule", "id": "sc"},
        {"source": "future", "id": "ignored"},
    ]
    assert report(c, "app", aliases=aliases, subject="another").status_code == 400
    assert report(c, "app", aliases=aliases).get_json() == {"ok": True}
    new = listing(c)["mine"][0]
    assert new["id"] != old["id"] and new["display_name"] == "Work device" and new["app_version"] == "1.0 (42)"
    assert sign_out(c, old["id"] + "/sign-out").status_code == 404
    assert report(c, "app", aliases=aliases).status_code == 200
    assert report(c, "app", aliases=[], display_name="Changed").status_code == 429
    assert report(c, "different", aliases=aliases).status_code == 409
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(
            select(remote_device_sessions.c.health_alias_hash, remote_device_sessions.c.schedule_alias)
        ).one() == (hashlib.sha256(b"hc").hexdigest(), "sc")
    response = sign_out(c, new["id"] + "/sign-out")
    assert response.status_code == 200
    cookies = [
        h for h in response.headers.getlist("Set-Cookie") if h.startswith(remote_access.SESSION_COOKIE_NAME + "=")
    ]
    assert len(cookies) == 1 and "Max-Age=0" in cookies[0]


def test_local_device_administration_needs_pairing_and_direct_loopback(install, monkeypatch):
    import ipaddress

    monkeypatch.setattr(ui_server, "_local_interface_network", lambda *_: ipaddress.ip_network("192.168.2.0/24"))
    local = ui_server.app.test_client()
    loop = "http://127.0.0.1:5123"
    assert listing(local, loop)["bulk_action"] is None
    assert sign_out(local, "sign-out-others", loop).status_code == 403
    install.ui.setup_host = "192.168.2.12"
    lan = dict(base_url="http://192.168.2.12:5123", environ_base={"REMOTE_ADDR": "192.168.2.50"})
    assert local.get("/api/devices", **lan).get_json()["bulk_action"] is None
    install.remote_access.vibe_cloud.instance_secret = "paired-test-instance"
    assert local.get("/api/devices", **lan).status_code == 403
    for headers in ({"X-Forwarded-For": "192.168.2.50"}, {"CF-Connecting-IP": "192.168.2.50"}):
        assert local.get("/api/devices", base_url=loop, headers=headers).status_code in (403, 503)


def test_demotion_is_not_overridden_by_registry_login_role(install):
    previous = client(login(install, "former-owner", "owner"))
    other = client(login(install, "other", "owner"))
    other_id = listing(other)["mine"][-1]["id"]
    login(install, "former-owner", "member")
    view = listing(previous)
    assert view["others"] == [] and all(r["account"]["subject"] == "former-owner" for r in view["mine"])
    assert any(r["account"]["login_role"] == "owner" for r in view["mine"])
    assert sign_out(previous, other_id + "/sign-out").status_code == 404


def test_idle_expiry_hides_device_but_bulk_still_writes_its_absolute_tombstone(install, monkeypatch):
    from tests.test_remote_access_vibe_cloud import _pin_clock

    start = int(time.time())
    clock = _pin_clock(monkeypatch, start)
    cookie = login(install, "member", "member")
    identity = remote_access.parse_session_identity(install, cookie)
    sid = identity["browser_session_id"]
    clock["now"] = identity["exp"]
    fresh = client(login(install, "member", "member"))
    assert len(listing(fresh)["mine"]) == 1
    assert_local_result(sign_out(fresh, "sign-out-others"), 0)
    with get_cached_sqlite_engine().connect() as conn:
        assert (
            int(
                conn.scalar(
                    select(state_meta.c.value_json).where(state_meta.c.key == REVOKED_BROWSER_SESSION_KEY_PREFIX + sid)
                )
            )
            == start + remote_access.SESSION_ABSOLUTE_TTL_SECONDS
        )


def test_enabled_push_outlives_cookie_and_only_proven_subject_can_manage_it(install, monkeypatch):
    from storage.web_push_service import upsert_subscription, count_enabled
    from tests.test_remote_access_vibe_cloud import _pin_clock

    start = int(time.time())
    clock = _pin_clock(monkeypatch, start)
    # The earlier registration was made with an owner role; later authorization
    # is member. Loopback grouping retains the historical role without granting it.
    c = client(login(install, "member", "owner"))
    payload = {"endpoint": "https://example.invalid/proven", "keys": {"auth": "fixture", "p256dh": "fixture"}}
    assert (
        c.post(
            "/api/web-push/subscriptions",
            base_url=ORIGIN,
            headers=csrf_headers(c, ORIGIN),
            json={"subscription": payload, "device_id": "same-id"},
        ).status_code
        == 200
    )
    clock["now"] += remote_access.SESSION_ABSOLUTE_TTL_SECONDS + 1
    current = client(login(install, "member", "member"))
    owner = client(login(install, "owner", "owner"))
    with get_cached_sqlite_engine().begin() as conn:
        for tag, user_key, device_id in [("legacy", "remote:member", "other-id"), ("local", "local", None)]:
            upsert_subscription(
                conn,
                user_key=user_key,
                device_id=device_id,
                payload={**payload, "endpoint": "https://example.invalid/" + tag},
            )
    (proven,) = [r for r in listing(current)["mine"] if r["push_only"]]
    assert proven["account"]["subject"] == "member" and proven["capabilities"]["push_enabled"]
    assert proven["group"] == "mine" and proven["account"]["login_role"] == "owner"
    assert listing(current)["unattributed"] == []
    install.remote_access.vibe_cloud.instance_secret = "paired-test-instance"
    loop_view = listing(ui_server.app.test_client(), "http://127.0.0.1:5123")
    assert [r["id"] for r in loop_view["mine"] if r["push_only"]] == [proven["id"]]
    install.remote_access.vibe_cloud.instance_secret = ""
    (legacy, local) = listing(owner)["unattributed"]
    for row in [legacy, local]:
        assert row["group"] == "unattributed" and row["push_only"]
        assert row["account"] is None and row["display_name"] is None
        assert sign_out(current, row["id"] + "/sign-out").status_code == 404
    stopped = sign_out(current, proven["id"] + "/sign-out")
    assert_local_result(stopped, 0)
    # A device left with notifications only is still known by its proven account and that account's role.
    assert [row["group"] for row in stopped.get_json()["sign_out_results"]] == ["mine"]
    from storage.models import remote_signout_receipt_roles as roles, remote_signout_receipts as receipts

    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(receipts.c.subject, roles.c.login_role).join_from(
            receipts, roles, roles.c.receipt == receipts.c.id)).all() == [("member", "owner")]
    stopped = sign_out(owner, legacy["id"] + "/sign-out")
    assert_local_result(stopped, 0)
    assert [row["group"] for row in stopped.get_json()["sign_out_results"]] == [None]
    loop = "http://127.0.0.1:5123"
    unpaired = ui_server.app.test_client()
    (unproven,) = listing(unpaired, loop)["unattributed"]
    assert_local_result(sign_out(unpaired, unproven["id"] + "/sign-out", loop), 0)
    with get_cached_sqlite_engine().connect() as conn:
        assert count_enabled(conn) == 0


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


def test_a_sign_out_receipt_says_whose_device_it_was_for_each_viewer(install):
    from storage.device_account_signout import recent
    from storage.models import remote_signout_receipt_roles as roles, remote_signout_receipts as receipts
    from storage.web_push_service import upsert_subscription
    from vibe.account_session_revoke import pairing_epoch

    loop = "http://127.0.0.1:5123"
    owner = client(login(install, "owner", "owner", installation="owner-desk"))
    member = client(login(install, "member", "member", installation="member-desk"))
    login(install, "owner", "owner", installation="owner-spare")
    login(install, "member", "member", installation="member-spare")
    login(install, "partner", "owner", installation="partner-desk")  # a second account that signs in as Owner
    with get_cached_sqlite_engine().begin() as conn:
        upsert_subscription(conn, user_key="remote:member", device_id="unproven", payload={
            "endpoint": "https://example.invalid/unproven", "keys": {"auth": "fixture", "p256dh": "fixture"}})

    def other(view, group):
        (row,) = [r for r in view[group] if not r["current"] and r["id"] != "host"]
        return row["id"]

    def groups(c, origin=ORIGIN):
        return {row["device_id"]: row["group"] for row in listing(c, origin)["recent_sign_outs"]}

    def answered(response):
        assert response.status_code == 200, response.get_json()
        (result,) = response.get_json()["sign_out_results"]
        return result["group"]

    own_spare, member_spare = other(listing(owner), "mine"), other(listing(member), "mine")
    member_desk = next(r["id"] for r in listing(member)["mine"] if r["current"])
    (partner,) = [r["id"] for r in listing(owner)["others"] if r["account"]["subject"] == "partner"]
    (unproven,) = [r["id"] for r in listing(owner)["unattributed"]]
    # A signed-in viewer: by account, in the answer to the request and when read later.
    assert answered(sign_out(owner, own_spare + "/sign-out")) == "mine"
    assert answered(sign_out(owner, member_spare + "/sign-out")) == "others"
    assert groups(owner) == {own_spare: "mine", member_spare: "others"}
    # A member sees only receipts of their own devices, and they are theirs.
    assert groups(member) == {member_spare: "mine"}
    # A receipt written before the role was recorded is not sorted for anyone, whatever its name or kind says.
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(roles.delete().where(roles.c.receipt.in_(
            select(receipts.c.id).where(receipts.c.device_id == own_spare))))
    assert groups(owner) == {own_spare: None, member_spare: "others"}
    for row in listing(owner)["recent_sign_outs"]:
        assert set(row) == {"operation_id", "device_id", "kind", "display_name", "group", "local_status", "account_results"}
    # A viewer on this computer itself has no account: by the role the device signed in with.
    install.remote_access.vibe_cloud.instance_secret = "paired-test-instance"
    local = ui_server.app.test_client()
    assert answered(sign_out(local, partner + "/sign-out", loop)) == "mine"
    assert answered(sign_out(local, member_desk + "/sign-out", loop)) == "others"
    assert answered(sign_out(local, unproven + "/sign-out", loop)) is None  # no account was ever proven for it
    assert groups(local, loop) == {partner: "mine", member_desk: "others", unproven: None}
    # The same receipts read by the signed-in Owner: the other Owner's device is not theirs.
    with get_cached_sqlite_engine().connect() as conn:
        seen = recent(conn, pairing_epoch(install), "owner", True, int(time.time()))
    assert {row["device_id"]: row["group"] for row in seen} == {partner: "others", member_desk: "others", unproven: None}

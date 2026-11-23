"""Published state (not connector health) retires network-position grants."""

import asyncio
import subprocess
import sys

import pytest

from config import paths
from config.v2_config import V2Config
from storage import web_push_cutover as cutover, web_push_service as push
from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.network_position import BindingState, binding_state

LAN = "http://192.168.2.3:15131"
LAN_PEER = {"REMOTE_ADDR": "192.168.2.4"}
HOST = "http://127.0.0.1:15131"


def _unbound():
    config = V2Config.default()
    config.ui.setup_host = "192.168.2.3"
    config.save()
    return config


def _bind(config):
    cloud = config.remote_access.vibe_cloud
    cloud.instance_id = "fixture-instance"
    cloud.instance_secret = "fixture-secret"
    cloud.session_secret = "fixture-cookie-key"
    cloud.public_url = "https://fixture.example.test"
    # A released, saved binding persists when the connector is Off.
    assert not cloud.enabled
    return config


def _payload(device):
    return {
        "endpoint": f"https://push.example.test/{device}",
        "keys": {"p256dh": f"key-{device}", "auth": f"auth-{device}"},
    }


@pytest.fixture
def serving_ui_writer():
    # Exercise the real publication transaction without opening any listener.
    with cutover.listener_publications():
        yield


@pytest.mark.parametrize('already_bound', [False, True], ids=['first-bind', 'replacement'])
def test_cli_publication_waits_for_listener_policy_without_erasing_grants(monkeypatch, tmp_path, already_bound):
    import os

    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, 'https://desk.example.test')
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.10')
    config = _unbound()
    if already_bound:
        _bind(config).save()
        with cutover.listener_publications():
            cutover.reconcile_startup(config)
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        anon = push.upsert_subscription(conn, user_key='local', payload=_payload('anon'))
        signed = push.upsert_subscription(conn, user_key='remote:member', payload=_payload('member'))
    assert cutover.may_dispatch(engine, anon)
    # The independent config writer has no serving sockets and neither declaration.
    child_env = {k: v for k, v in os.environ.items() if k not in (ui_server.TRUSTED_PROXY_IPS_ENV, ui_server.TRUSTED_PUBLIC_ORIGINS_ENV)}
    subprocess.run([sys.executable, '-c', 'from vibe.api import save_config; save_config({"remote_access":{"vibe_cloud":{"instance_id":"replacement","instance_secret":"fixture"}}})'],
                   env=child_env, check=True, timeout=15)
    assert not cutover.may_dispatch(engine, anon)
    assert cutover.may_dispatch(engine, signed)
    with engine.connect() as conn:
        assert push.get_enabled_by_endpoint(conn, endpoint=anon['endpoint'], user_key='local')['id'] == anon['id']
    cutover.reconcile_startup(V2Config.load())  # Another non-listener cannot release the pause either.
    assert not cutover.may_dispatch(engine, anon)
    with cutover.listener_publications():
        cutover.reconcile_startup(V2Config.load())
    # A sender with no declarations follows the committed listener receipt, not its environment.
    monkeypatch.delenv(ui_server.TRUSTED_PROXY_IPS_ENV)
    monkeypatch.delenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV)
    assert cutover.may_dispatch(engine, anon)
    assert cutover.may_dispatch(engine, signed)


@pytest.mark.parametrize("first_start", [True, False], ids=["persisted-upgrade", "publish"])
def test_retirement_and_opt_out(monkeypatch, tmp_path, first_start, serving_ui_writer):
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        rows = {
            device: push.upsert_subscription(
                conn,
                user_key="remote:member" if device == "signed" else "local",
                payload=_payload(device),
                device_id=device,
            )
            for device in ["host", "old-proxy", "off", "signed"]
        }
        push.disable_subscription(conn, endpoint=rows["off"]["endpoint"], user_key="local")
    client = ui_server.app.test_client()
    assert client.get("/api/session", base_url=LAN, environ_base=LAN_PEER).status_code == 200
    if first_start:
        _bind(config).save()  # no modern publication path/receipt on the released install
        assert not cutover.may_dispatch(engine, rows["host"])
        cutover.reconcile_startup(V2Config.load())
    else:
        config = api.save_config({"remote_access": {"vibe_cloud": vars(_bind(config).remote_access.vibe_cloud)}})
    assert binding_state(V2Config.load()) is BindingState.BOUND
    assert V2Config.load().ui.setup_host == "192.168.2.3"
    for device in ["host", "old-proxy", "off"]:
        assert not cutover.may_dispatch(engine, rows[device])
    with engine.connect() as conn:
        assert [r["endpoint"] for r in push.list_enabled(conn)] == [rows["signed"]["endpoint"]]
    headers = csrf_headers(client, HOST)

    def status(device):
        return client.post(
            "/api/web-push/status",
            base_url=HOST,
            headers=headers,
            json={
                "endpoint": rows[device]["endpoint"],
                "subscription": _payload(device),
                "device_id": device,
            },
        ).get_json()

    def repair(device, **extra):
        return client.post(
            "/api/web-push/subscriptions",
            base_url=HOST,
            headers=headers,
            json={
                "binding_repair": True,
                "subscription": _payload(device),
                "device_id": device,
                **extra,
            },
        ).get_json()

    assert status("host")["current_subscription_binding_retired"]
    assert not status("host")["current_subscription_enabled"]
    assert not repair("host", background_rotation=True)["accepted"]
    assert repair("host")["accepted"]
    assert status("host")["current_subscription_enabled"]
    assert not status("off")["current_subscription_binding_retired"]
    assert not repair("off")["accepted"]
    assert not repair("off", binding_repair=False)["accepted"]  # legacy foreground retry
    assert not repair("signed", binding_repair=False, explicit_enable=True)["accepted"]
    assert not repair("unseen", binding_repair=False)["accepted"]  # no unattended fresh opt-in
    cutover.reconcile_startup(V2Config.load())
    with engine.connect() as conn:
        assert {r["endpoint"] for r in push.list_enabled(conn)} == {
            rows["host"]["endpoint"],
            rows["signed"]["endpoint"],
        }
        assert push.is_binding_retired(conn, rows["old-proxy"]["endpoint"])
        current_host = push.get_enabled_by_endpoint(conn, endpoint=rows["host"]["endpoint"], user_key="local")
    assert cutover.may_dispatch(engine, current_host)
    assert not cutover.may_dispatch(engine, rows["host"])  # A queued pre-binding grant stays retired.
    denied = client.post(
        "/api/web-push/status", base_url=LAN, environ_base=LAN_PEER, json={"subscription": _payload("old-proxy")}
    )
    assert denied.status_code == 503
    assert repair("off", binding_repair=False, explicit_enable=True)["accepted"]


@pytest.mark.parametrize("route", ["/api/web-push/subscriptions", "/api/web-push/status"])
def test_registration_admitted_before_publish_is_fenced_at_write(monkeypatch, tmp_path, route):
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    ensure_sqlite_state()
    client = ui_server.app.test_client()
    headers = csrf_headers(client, LAN)
    # The ASGI admission and the browser identity lookup already succeeded.
    # Interleave publication immediately before this request reserves its writer.
    check = ui_server._local_push_registration_state
    entered = []

    def publish_then_check(conn):
        entered.append(True)
        api.save_config({"remote_access": {"vibe_cloud": vars(_bind(config).remote_access.vibe_cloud)}})
        return check(conn)

    monkeypatch.setattr(ui_server, "_local_push_registration_state", publish_then_check)
    response = client.post(
        route,
        base_url=LAN,
        environ_base=LAN_PEER,
        headers=headers,
        json={"subscription": _payload("late"), "device_id": "late"},
    )
    assert entered == [True]
    assert response.status_code == 503 and response.get_json()["error"] == "remote_access_host_mismatch"
    with get_cached_sqlite_engine().connect() as conn:
        assert push.get_by_endpoint(conn, endpoint=_payload("late")["endpoint"]) is None


@pytest.mark.parametrize('change', ['settings', 'file', 'listener-environment', 'remote-on'])
def test_bound_proxy_withdrawal_retires_mixed_grants_not_signed_or_opt_out(monkeypatch, tmp_path, serving_ui_writer, change):
    from vibe import api

    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    saved = 'https://desk.example.test'
    remaining = 'https://remaining.example.test'
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.0/24,127.0.0.1')
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, remaining)
    config = _bind(_unbound())
    config.ui.trusted_public_origins = [saved]
    config.save()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        rows = {key: push.upsert_subscription(conn, user_key='remote:member' if key == 'signed' else 'local',
                     payload=_payload(key), device_id=key) for key in ('host', 'proxy', 'off', 'signed')}
        push.disable_subscription(conn, endpoint=rows['off']['endpoint'], user_key='local')
    cutover.reconcile_startup(config)
    assert cutover.may_dispatch(engine, rows['host']) and cutover.may_dispatch(engine, rows['proxy'])
    if change == 'settings':
        # Removal at save, not at the next timer or process start.
        api.save_config({'ui': {'trusted_public_origins': []}})
    elif change == 'file':
        config.ui.trusted_public_origins = []
        config.save()
        assert not cutover.may_dispatch(engine, rows['host'])
        with engine.connect() as conn:
            assert push.get_enabled_by_endpoint(conn, endpoint=rows['host']['endpoint'], user_key='local')
        cutover.reconcile_startup(config)
    elif change == 'listener-environment':
        monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.10')
        assert cutover.may_dispatch(engine, rows['host'])  # The sender cannot attest an env change.
        cutover.reconcile_startup(config)
    else:
        api.save_config({'remote_access': {'vibe_cloud': {'enabled': True}}})
    for key in ('host', 'proxy'):
        assert not cutover.may_dispatch(engine, rows[key])
    assert cutover.may_dispatch(engine, rows['signed'])
    with engine.connect() as conn:
        assert all(push.is_binding_retired(conn, rows[k]['endpoint']) for k in ('host', 'proxy'))
        assert not push.is_binding_retired(conn, rows['off']['endpoint'])
        assert push.get_enabled_by_endpoint(conn, endpoint=rows['signed']['endpoint'], user_key='remote:member')
    # Expanding the declaration never undoes the cutover. A remaining proxy
    # can explicitly grant again, but cannot consume the host's prompt-free repair.
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.0/24,127.0.0.1')
    api.save_config({'ui': {'trusted_public_origins': [saved]}})
    cutover.reconcile_startup(V2Config.load())
    client = ui_server.app.test_client()
    headers = csrf_headers(client, HOST)

    def enroll(key, *, via_proxy=False, **flags):
        request_headers = dict(headers)
        peer = '127.0.0.1'
        if via_proxy:
            peer = '192.0.2.10'
            request_headers.update({'Origin': remaining, 'X-Forwarded-Host': 'remaining.example.test', 'X-Forwarded-Proto': 'https'})
        response = client.post('/api/web-push/subscriptions', base_url=HOST, headers=request_headers,
                               environ_base={'REMOTE_ADDR': peer}, json={'subscription': _payload(key), 'device_id': key, **flags})
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    assert not enroll('proxy', via_proxy=True, binding_repair=True)['accepted']
    assert not enroll('off', binding_repair=True)['accepted']
    assert not enroll('proxy', via_proxy=True)['accepted']
    host = enroll('host', binding_repair=True)['subscription']
    new_proxy = enroll('proxy', via_proxy=True, explicit_enable=True)['subscription']
    for old, fresh in [(rows['host'], host), (rows['proxy'], new_proxy)]:
        assert old['id'] != fresh['id'] and not cutover.may_dispatch(engine, old)
        assert cutover.may_dispatch(engine, fresh)


def test_listener_started_without_config_picks_up_later_binding_and_retries(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.10')
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, 'https://desk.example.test')
    assert 0 < cutover._POLICY_CHECK_INTERVAL <= 1
    monkeypatch.setattr(cutover, '_POLICY_CHECK_INTERVAL', 0.05)
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        anon = push.upsert_subscription(conn, user_key='local', payload=_payload('anon'))
    attempted, committed = threading.Event(), threading.Event()
    original = cutover.reconcile_startup

    def one_transient_failure(config):
        if binding_state(config) is BindingState.BOUND and not attempted.is_set():
            attempted.set()
            raise RuntimeError('test: temporary storage failure')
        original(config)
        if attempted.is_set() and cutover.may_dispatch(engine, anon):
            committed.set()

    monkeypatch.setattr(cutover, 'reconcile_startup', one_transient_failure)
    # Only the production checker runs here; no sockets, sender or UI service.
    with cutover.serving_listener():
        _bind(_unbound()).save()
        assert attempted.wait(5) and committed.wait(5)
    assert cutover.may_dispatch(engine, anon)


@pytest.mark.parametrize('unreadable', ['not-json', 'null', '{"version": 99}'])
def test_only_listener_recovers_unreadable_receipt_by_retiring_mixed_grants(monkeypatch, tmp_path, unreadable):
    from sqlalchemy import update
    from storage.models import state_meta
    from vibe import api

    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.10')
    monkeypatch.delenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, raising=False)
    config = _bind(_unbound())
    config.ui.trusted_public_origins = ['https://work.example.test']
    config.save()
    engine = get_cached_sqlite_engine()
    with cutover.listener_publications():
        cutover.reconcile_startup(config)
    with engine.begin() as conn:
        anon = push.upsert_subscription(conn, user_key='local', payload=_payload('anon'))
        off = push.upsert_subscription(conn, user_key='local', payload=_payload('off'))
        push.disable_subscription(conn, user_key='local', endpoint=off['endpoint'])
        signed = push.upsert_subscription(conn, user_key='remote:member', payload=_payload('signed'))
        conn.execute(update(state_meta).where(state_meta.c.key == 'web_push.local_binding.v1').values(value_json=unreadable))
    # The sender and a config-only publisher leave the unreadable history alone.
    config = api.save_config({'ui': {'trusted_public_origins': ['https://desk.example.test']}})
    cutover.reconcile_startup(config)
    assert not cutover.may_dispatch(engine, anon) and cutover.may_dispatch(engine, signed)
    with engine.connect() as conn:
        assert push.get_enabled_by_endpoint(conn, endpoint=anon['endpoint'], user_key='local')
        assert not cutover.receipt_current(conn, config)

    # Even with a complete active proxy declaration, the listener cannot infer
    # where the old rows came from. Publish a current receipt and retire in one commit.
    with cutover.serving_listener():
        with engine.connect() as conn:
            assert cutover.receipt_current(conn, config)
            assert push.is_binding_retired(conn, anon['endpoint'])
            assert not push.is_binding_retired(conn, off['endpoint'])
            assert not push.get_enabled_by_endpoint(conn, endpoint=off['endpoint'], user_key='local')
            assert push.get_enabled_by_endpoint(conn, endpoint=signed['endpoint'], user_key='remote:member')
        client = ui_server.app.test_client()
        response = client.post('/api/web-push/subscriptions', base_url=HOST, headers=csrf_headers(client, HOST),
                               json={'subscription': _payload('anon'), 'binding_repair': True})
        assert response.status_code == 200 and response.get_json()['accepted']
        fresh = response.get_json()['subscription']
        cutover.reconcile_startup(config)
    assert fresh['id'] != anon['id'] and cutover.may_dispatch(engine, fresh)
    assert not cutover.may_dispatch(engine, anon) and cutover.may_dispatch(engine, signed)


def test_host_push_grant_survives_remote_off_and_cookie_rotation(monkeypatch, tmp_path, serving_ui_writer):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _bind(_unbound())
    config.remote_access.vibe_cloud.enabled = True
    config.save()
    cutover.reconcile_startup(config)
    client = ui_server.app.test_client()
    headers = csrf_headers(client, HOST)

    def register(device):
        result = client.post(
            "/api/web-push/subscriptions",
            base_url=HOST,
            headers=headers,
            json={"subscription": _payload(device), "explicit_enable": True},
        )
        assert result.status_code == 200 and result.get_json()["accepted"]
        return result.get_json()["subscription"]

    engine = get_cached_sqlite_engine()
    host = register("host")
    assert cutover.may_dispatch(engine, host)
    # Use the actual settings writer, without starting or stopping a connector.
    before = config.remote_access.vibe_cloud.session_secret
    ui_server._save_config_and_runtime_decisions({"remote_access": {"vibe_cloud": {"enabled": False}}})
    off = V2Config.load()
    assert not off.remote_access.vibe_cloud.enabled and off.remote_access.vibe_cloud.session_secret != before
    assert cutover.may_dispatch(engine, host)
    second = register("second")
    cutover.reconcile_startup(off)
    assert cutover.may_dispatch(engine, host) and cutover.may_dispatch(engine, second)

    # A real replacement pairing still requires its own committed receipt.
    off.remote_access.vibe_cloud.instance_secret = "replacement-pairing"
    off.save()
    assert not cutover.may_dispatch(engine, host)


def test_published_file_with_rolled_back_receipt_blocks_delivery_until_reconciled(monkeypatch, tmp_path, serving_ui_writer):
    from config.v2_config import config_file_lock

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    before = _unbound()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        stale = push.upsert_subscription(conn, user_key="local", payload=_payload("browser"))
    after = _bind(V2Config.load())
    with pytest.raises(RuntimeError, match="before replace"), config_file_lock(), cutover.publishing(before, after):
        raise RuntimeError("interrupted before replace")
    assert binding_state(V2Config.load()) is BindingState.UNBOUND
    assert cutover.may_dispatch(engine, stale)
    with pytest.raises(RuntimeError, match="after atomic replace"), config_file_lock(), cutover.publishing(before, after):
        after.save()
        raise RuntimeError("interrupted after atomic replace")
    # Rollback really left an enabled row. A queued preview must not send it.
    with engine.connect() as conn:
        assert push.get_deliverable_by_endpoint(conn, endpoint=stale["endpoint"], user_key="local")
    assert not cutover.may_dispatch(engine, stale)
    client = ui_server.app.test_client()
    headers = csrf_headers(client, HOST)
    assert (
        client.post(
            "/api/web-push/subscriptions", base_url=HOST, headers=headers, json={"subscription": _payload("new")}
        ).status_code
        == 503
    )
    cutover.reconcile_startup(after)
    with engine.connect() as conn:
        assert push.is_binding_retired(conn, stale["endpoint"])
    assert not cutover.may_dispatch(engine, stale)


def test_rotated_foreground_grant_cannot_consume_an_opt_out_or_signed_endpoint(monkeypatch, tmp_path, serving_ui_writer):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        for device in ("a", "b", "c", "native", "member"):
            push.upsert_subscription(
                conn,
                user_key="remote:member" if device == "member" else "local",
                payload=_payload(device),
                device_id=None if device == "native" else device,
            )
    _bind(config).save()
    cutover.reconcile_startup(config)
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

        def repair(endpoint, previous, device_id):
            return push.resume_retired_host_subscription(
                conn,
                payload=_payload(endpoint),
                device_id=device_id,
                device_label=None,
                user_agent=None,
                previous_endpoints=[_payload(previous)["endpoint"]],
            )

        assert repair("member", "a", "a") is None
        assert repair("new", "a", "other") is None
        assert push.disable_device_subscription(conn, endpoint=_payload("b")["endpoint"], user_key="local")
        assert repair("new", "b", "b") is None
        assert repair("new", "a", "a")["enabled"]
        assert repair("second", "a", "a") is None
        assert repair("new-native", "native", None)["enabled"]
        assert push.disable_device_subscription(conn, endpoint=_payload("native")["endpoint"], user_key="local")
        assert not push.get_enabled_by_endpoint(conn, endpoint=_payload("new-native")["endpoint"], user_key="local")
        assert push.is_binding_retired(conn, _payload("c")["endpoint"])
        assert push.get_enabled_by_endpoint(conn, endpoint=_payload("member")["endpoint"], user_key="remote:member")


def test_declared_ingress_uses_the_same_transport_policy_for_http_and_websocket(monkeypatch, tmp_path):
    from starlette.requests import HTTPConnection

    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.0/28,2001:db8::10,127.0.0.1')
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, 'https://work.example.test:8443')
    config = _bind(_unbound())
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    config.ui.trusted_public_origins = [cloud.public_url, 'https://old-app.avibe.bot']
    config.save()

    def decisions(current, *, peer='192.0.2.10', host='localhost', origin_host='work.example.test:8443', direct=False):
        # Exercise both consumers' peer/authority extraction, not two calls to
        # the shared predicate. These scopes never open a connection or run a route.
        headers = {'host': host, 'x-forwarded-host': origin_host, 'x-forwarded-proto': 'https'}
        with ui_server.app.test_request_context('/', base_url='http://' + host, headers=headers) as req:
            scope = req._request.scope
            scope['client'] = (peer, 1234) if peer is not None else None
            if direct:
                scope[ui_server.DIRECT_INGRESS_SCOPE_KEY] = {}
            ws = HTTPConnection({**scope, 'type': 'websocket'})
            return (ui_server._trusted_public_origin_local_request(current),
                    ui_server._websocket_trusted_public_origin_local_request(ws, current))

    for peer in ('192.0.2.10', '192.0.2.11', '2001:db8::10'):
        assert decisions(config, peer=peer) == (True, True)
    for facts in ({'peer': None}, {'peer': '192.0.2.16'}, {'peer': '127.0.0.1'}, {'peer': '::ffff:127.0.0.1'},
                  {'peer': '::ffff:127.0.0.1', 'host': 'fixture.example.test'},
                  {'direct': True}, {'host': 'fixture.example.test'}, {'origin_host': 'fixture.example.test'},
                  {'origin_host': 'old-app.avibe.bot'}, {'origin_host': 'work.example.test'}, {'origin_host': config.ui.setup_host}):
        assert decisions(config, **facts) == (False, False), facts
    assert decisions(None) == (False, False)
    # The very same declarations remain on disk; lack of binding evidence is not permission.
    (paths.get_state_dir() / 'remote-binding-reset.json').write_text('{}')
    assert decisions(config) == (False, False)
    assert ui_server._owner_proxy_fields() == {}


def test_declared_origin_navigation_shares_private_notice_without_granting_access(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    config = _bind(_unbound())
    origin = 'https://home.example.test:9443'
    # When remote access is enabled, the listener's loopback connector is
    # ineligible even if its network and the custom origin were declared.
    config.remote_access.vibe_cloud.enabled = True
    config.ui.trusted_public_origins = [origin]
    config.save()
    client = ui_server.app.test_client()
    nav = {'Accept': 'text/html', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    reference = client.get('/settings/devices', base_url=LAN, environ_base=LAN_PEER, headers=nav)
    forward = {**nav, 'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'home.example.test:9443'}
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '127.0.0.1')
    for url, peer, headers in [(origin, LAN_PEER, nav), (HOST, {'REMOTE_ADDR': '127.0.0.1'}, forward)]:
        reply = client.get('/settings/devices?private=fixture', base_url=url, environ_base=peer, headers=headers)
        assert reference.status_code == reply.status_code == 200 and reference.content == reply.content
        assert reply.headers['Cache-Control'] == 'no-store' and reply.headers['Referrer-Policy'] == 'no-referrer'
        assert 'Location' not in reply.headers
    for url, path, headers in [(origin, '/', {'Accept': '*/*'}), (origin, '/api/session', nav),
            (origin, '/', {**nav, 'Sec-Fetch-Mode': 'cors'}), ('https://home.example.test', '/', nav),
            ('https://unlisted.example.test:9443', '/', nav), (HOST, '/', forward)]:
        result = client.get(path, base_url=url, environ_base=LAN_PEER, headers=headers)
        assert result.status_code == 503 and result.get_json()['error'] == 'remote_access_host_mismatch'
    with pytest.raises(WebSocketDisconnect) as closed:
        with TestClient(ui_server.app).websocket_connect('wss://home.example.test:9443/api/terminal/local'):
            pytest.fail('retired origin opened a socket')
    assert closed.value.code == 1008


def test_persisted_bound_lan_liveness_docker_and_canonical(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _bind(_unbound())
    config.save()
    client = ui_server.app.test_client()

    def get(path, origin=LAN, environ=LAN_PEER, **kwargs):
        return client.get(path, base_url=origin, environ_base=environ, **kwargs)

    notice = get("/settings/devices?private=no", headers={"Accept": "text/html"})
    assert notice.status_code == 200 and "Location" not in notice.headers
    assert notice.headers["Cache-Control"] == "no-store" and notice.headers["Referrer-Policy"] == "no-referrer"
    assert (
        b"https://avibe.bot/app" in notice.content
        and b"fixture" not in notice.content
        and b"?private" not in notice.content
    )
    assert get("/health").get_json() == {"status": "ok"}
    assert get("/health", environ={"REMOTE_ADDR": "8.8.8.8"}).status_code == 503
    assert get("/", origin="http://192.168.2.99", headers={"Accept": "text/html"}).status_code == 503
    assert get("/", headers={"Accept": "application/json"}).status_code == 503
    for path in ["/status", "/api/session", "/api/config/", "/api/remote-access/claims/synthetic", "/auth/login"]:
        assert get(path).status_code == 503
    assert get("/api/session", HOST, {}).status_code == 200
    bridge = {"REMOTE_ADDR": "172.19.0.1"}
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.19.0.1")
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    assert get("/api/session", HOST, bridge).status_code == 503
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    assert get("/api/session", HOST, bridge).status_code == 200
    assert get("/api/desktop/local-instance", HOST, bridge).status_code == 403
    assert get("/api/desktop/local-instance", HOST, bridge).get_json()["error"] == "direct_loopback_required"
    assert get("/api/session", HOST, bridge, headers={"X-Forwarded-Proto": "https"}).status_code == 503
    assert get("/api/session", HOST, {"REMOTE_ADDR": "172.19.0.2"}).status_code == 503
    config.remote_access.vibe_cloud.enabled = True
    config.save()
    assert get("/settings/devices", headers={"Accept": "text/html"}).content == notice.content
    public = get(
        "/api/session",
        "http://fixture.example.test",
        {"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert public.status_code == 200
    assert public.get_json()["remote"] and public.get_json()["authenticated"] is False
    assert "Secure" in public.headers.get("Set-Cookie", "")


@pytest.mark.parametrize(
    ("saved", "unbound", "bound"),
    [
        # A network address or a bind to every address: the loopback address of its family.
        ("192.168.2.3", "192.168.2.3", "127.0.0.1"),
        ("fd00::4", "[fd00::4]", "[::1]"),
        ("0.0.0.0", "127.0.0.1", "127.0.0.1"),
        ("::", "[::1]", "[::1]"),
        # Already a loopback name or address: kept as written, where the listener is.
        ("localhost", "localhost", "localhost"),
        ("127.0.0.1", "127.0.0.1", "127.0.0.1"),
        ("[::1]", "[::1]", "[::1]"),
        ("::1", "[::1]", "[::1]"),
    ],
)
def test_cli_bound_ui_target_is_loopback_where_the_listener_is(monkeypatch, tmp_path, saved, unbound, bound):
    from vibe.cli import _local_ui_url, _ui_show_events_host

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    config.ui.setup_host = saved
    assert _ui_show_events_host(config) == unbound
    _bind(config)
    # Show events, prewarm and session activity all build their address from this host.
    assert _ui_show_events_host(config) == bound
    assert _local_ui_url(config) == f"http://{bound}:{config.ui.setup_port}"


def test_cli_follows_a_localhost_that_resolves_to_ipv6_only(monkeypatch, tmp_path):
    """Bound, remote access on, host saved as ``localhost``: the UI listens on ``::1`` alone."""
    from vibe import runtime
    from vibe.cli import _local_ui_url, _ui_show_events_host

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _bind(_unbound())
    config.ui.setup_host = "localhost"
    config.remote_access.vibe_cloud.enabled = True
    monkeypatch.setattr(runtime, "resolve_localhost_family", lambda: "inet6")
    assert runtime.effective_ui_bind_host(config) == "::1"
    # The name reaches that listener; an IPv4 literal would reach nothing. No
    # name is resolved to decide this: only the saved value is read.
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: pytest.fail("resolved a name"))
    assert _ui_show_events_host(config) == "localhost"
    assert _local_ui_url(config) == f"http://localhost:{config.ui.setup_port}"


@pytest.mark.parametrize("saved", ["localhost.example.test", "127.0.0.1.example.test", "printer.local", "8.8.8.8"])
def test_cli_never_keeps_a_bound_host_that_is_not_literally_loopback(monkeypatch, tmp_path, saved):
    from vibe.cli import _local_ui_url, _ui_show_events_host

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _bind(_unbound())
    config.ui.setup_host = saved
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: pytest.fail("resolved a name"))
    assert _ui_show_events_host(config) == "127.0.0.1"
    assert _local_ui_url(config) == f"http://127.0.0.1:{config.ui.setup_port}"


def test_missing_partial_reset_block_position(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = ui_server.app.test_client()

    def denied():
        assert client.get("/api/session", base_url=LAN, environ_base=LAN_PEER).status_code == 503
        assert client.get("/api/session", base_url=HOST).status_code == 200

    denied()
    config = _unbound()
    config.remote_access.vibe_cloud.session_secret = "partial"
    config.save()
    denied()
    _unbound()
    (paths.get_state_dir() / "remote-binding-reset.json").write_text("not valid json")
    denied()


def test_the_reset_record_has_one_path_shared_with_the_reset_itself(monkeypatch, tmp_path):
    """The reset (AV-13) writes this record and this gate reads it: one name, taken from one place.

    The name is a shipped artifact: a reset that an older version started must
    still close network-position trust after an upgrade, so it is pinned here.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    record = paths.get_remote_binding_reset_path()
    assert record == paths.get_state_dir() / "remote-binding-reset.json"

    config = _unbound()
    assert binding_state(config) is BindingState.UNBOUND
    # A record under any other name is not the reset's and changes nothing.
    (paths.get_state_dir() / "remote-binding-reset.json.tmp").write_text("{}")
    (paths.get_state_dir() / "binding-reset.json").write_text("{}")
    assert binding_state(config) is BindingState.UNBOUND
    # The record itself closes trust, whatever it holds, and a dangling link does too.
    record.write_text("{}")
    assert binding_state(config) is BindingState.UNAVAILABLE
    record.unlink()
    record.symlink_to(tmp_path / "nowhere")
    assert binding_state(config) is BindingState.UNAVAILABLE
    record.unlink()
    assert binding_state(config) is BindingState.UNBOUND


@pytest.mark.asyncio
@pytest.mark.parametrize('publication', ['binding', 'settings-save', 'proxy-withdrawal'])
async def test_second_process_retires_live_setup_channels(monkeypatch, tmp_path, publication):
    # Real persisted config and actual ASGI middleware. Only the consumers are
    # stand-ins: no PTY, Vite server, network sockets or user's processes.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    # Separate per-message admission from the idle timer: even if no tick
    # can occur during this test, the very first input and output must fail.
    from vibe import setup_network_stream

    monkeypatch.setattr(setup_network_stream, "CHECK_INTERVAL_SECONDS", 60.0)
    config = _unbound()
    proxy = publication == 'proxy-withdrawal'
    if proxy:
        _bind(config)
        config.ui.trusted_public_origins = ['https://workbench.example.test']
        config.save()
        monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, '192.0.2.10')
        monkeypatch.delenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, raising=False)
    inbox = asyncio.Queue()
    release_output = asyncio.Event()
    events = {k: [] for k in ("terminal", "hmr", "sse", "host")}
    ready = {k: asyncio.Event() for k in events}
    received = []

    async def consumer(scope, receive, send):
        name = scope["path"][1:]
        sse = name == "sse"
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]}
            if sse
            else {"type": "websocket.accept"}
        )
        ready[name].set()
        if name in ("terminal", "host"):
            msg = await receive()
            received.append((name, msg["text"]))
        else:
            await release_output.wait()
            await send(
                {"type": "http.response.body", "body": b"private", "more_body": True}
                if sse
                else {"type": "websocket.send", "text": "private"}
            )

    gate = ui_server._SetupNetworkMiddleware(consumer)

    async def run(name):
        host = name == "host"

        async def send(message):
            events[name].append(message)

        async def receive():
            await inbox.get()
            return {"type": "websocket.receive", "text": "first"}

        address = '127.0.0.1' if host else '192.168.2.4'
        headers = [(b'host', b'localhost' if host else b'192.168.2.3')]
        if proxy and not host:
            address = '192.0.2.10'
            headers += [(b'x-forwarded-host', b'workbench.example.test'), (b'x-forwarded-proto', b'https')]
        await gate(
            {
                "type": "http" if name == "sse" else "websocket",
                "path": "/" + name,
                "method": "GET",
                "client": (address, 42100),
                "headers": headers,
            },
            receive,
            send,
        )

    tasks = [asyncio.create_task(run(name)) for name in events]
    try:
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in ready.values())), 5)
        # Complete this publication before delivering a key or making output ready.
        # The parent does not share any Python cache, Event, or lock with the writer.
        subprocess.run(
            [
                sys.executable,
                "-c",
                'import sys,json; from vibe.api import save_config; save_config(json.loads(sys.argv[1]))',
                {'settings-save': '{"ui":{"setup_host":"127.0.0.1"}}',
                 'proxy-withdrawal': '{"ui":{"trusted_public_origins":[]}}',
                 'binding': '{"remote_access":{"vibe_cloud":{"instance_id":"fixture","instance_secret":"bound","session_secret":"fixture-cookie"}}}'}[publication],
            ],
            check=True,
            timeout=15,
        )
        release_output.set()
        for _ in range(2):
            inbox.put_nowait(True)
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert received == [("host", "first")]
        for name in ("terminal", "hmr"):
            assert events[name] == [{"type": "websocket.accept"}, {"type": "websocket.close", "code": 1008}]
        assert events["sse"][-1] == {"type": "http.response.body", "body": b""}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['bound', 'snapshot-error', 'restrict-from-lan'])
async def test_idle_terminal_closes_on_first_tick_and_host_terminal_stays_open(monkeypatch, tmp_path, change):
    from starlette.websockets import WebSocketDisconnect
    from vibe import setup_network_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    _unbound()
    writes = []

    class Terminal:
        def start_reaper(self):
            pass

        async def handle_websocket(self, websocket, session_id, *, initial_cwd):
            await websocket.send_text("ready")
            try:
                while True:
                    value = await websocket.receive_text()
                    writes.append((session_id, value))
                    await websocket.send_text("received")
            except WebSocketDisconnect:
                pass

    monkeypatch.setattr(ui_server, "get_terminal_service", Terminal)
    assert 0 < setup_network_stream.CHECK_INTERVAL_SECONDS <= 1

    def open_terminal(name, host, peer):
        sent, incoming = asyncio.Queue(), asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "http_version": "1.1",
            "path": "/api/terminal/" + name,
            "query_string": b"",
            "client": (peer, 1234),
            "server": (host, 15131),
            "headers": [(b"host", host.encode() + b":15131"), (b"origin", ("http://" + host + ":15131").encode())],
        }
        return asyncio.create_task(ui_server.app(scope, incoming.get, sent.put)), incoming, sent

    both = [open_terminal("idle", "192.168.2.3", "192.168.2.4"), open_terminal("host", "127.0.0.1", "127.0.0.1")]
    try:
        for _, _, sent in both:
            assert (await asyncio.wait_for(sent.get(), 3))["type"] == "websocket.accept"
            assert (await asyncio.wait_for(sent.get(), 3))["text"] == "ready"
        if change == 'restrict-from-lan':
            from types import SimpleNamespace
            from tests.test_host_network_access import Acceptor, restrict

            port = V2Config.load().ui.setup_port
            monkeypatch.setattr(ui_server, '_is_containerized_runtime', lambda: False)
            monkeypatch.setattr(ui_server, '_server', SimpleNamespace(started=True, servers=[
                Acceptor('192.168.2.3', port), Acceptor('127.0.0.1', port)]))
            reply = restrict(ui_server.app.test_client(), LAN, LAN_PEER)
            assert reply.status_code == 200, reply.content
        elif change == 'snapshot-error':
            from vibe import network_position

            def failed_read(*args):
                raise RuntimeError("snapshot reader unavailable")

            monkeypatch.setattr(network_position, "_config_revision", failed_read)
            response = ui_server.app.test_client().get("/api/session", base_url=LAN, environ_base=LAN_PEER)
            assert response.status_code == 503 and response.get_json()["error"] == "remote_access_config_unavailable"
        else:
            _bind(V2Config.load()).save()
        _, _, idle_out = both[0]
        _, host_in, host_out = both[1]
        # No receive or protected send on the old origin can wake the guard.
        assert (await asyncio.wait_for(idle_out.get(), 2))["code"] == 1008
        await host_in.put({"type": "websocket.receive", "text": "still-local"})
        assert (await asyncio.wait_for(host_out.get(), 2))["text"] == "received"
        assert writes == [("host", "still-local")]
        await host_in.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(asyncio.gather(*(task for task, _, _ in both)), 3)
    finally:
        for task, _, _ in both:
            task.cancel()
        await asyncio.gather(*(task for task, _, _ in both), return_exceptions=True)

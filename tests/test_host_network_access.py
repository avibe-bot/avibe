"""Owner-scoped network restriction: actual acceptors, persistent admission, and readback."""

import asyncio
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from storage import web_push_service
from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.authorization import http_authorization_policy

HOST = 'http://127.0.0.1:15131'
LAN = 'http://192.168.2.3:15131'
PEER = {'REMOTE_ADDR': '192.168.2.4'}
ACTION = '/api/devices/host/network-access'
MODE = {'mode': 'this_computer'}


class Acceptor:
    """Stand in for kernel/socket facts only; no network traffic on the owner's LAN."""

    def __init__(self, host, port=15131):
        self.host = host
        self.port = port
        self.open = True
        self.sockets = [self]

    def getsockname(self):
        return self.host, self.port

    def getsockopt(self, *args):
        return int(self.open)

    def is_serving(self):
        return self.open

    def close(self):
        self.open = False


@pytest.fixture
def installation(monkeypatch, tmp_path):
    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    monkeypatch.setattr(ui_server, '_is_containerized_runtime', lambda: False)
    for env in ('VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS', 'VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST', ui_server.TRUSTED_PUBLIC_ORIGINS_ENV):
        monkeypatch.delenv(env, raising=False)
    config = V2Config.default()
    config.ui.setup_host = '192.168.2.3'
    config.ui.setup_port = 15131
    config.save()
    servers = [Acceptor(config.ui.setup_host), Acceptor('127.0.0.1')]
    monkeypatch.setattr(ui_server, '_server', SimpleNamespace(started=True, servers=servers))
    return ui_server.app.test_client(), servers


def host_row(client, origin=HOST, peer=None):
    response = client.get('/api/devices', base_url=origin, environ_base=peer or {})
    assert response.status_code == 200, response.content
    return next(row for row in response.get_json()['mine'] if row['id'] == 'host')


def restrict(client, origin=HOST, peer=None, **kwargs):
    kwargs.setdefault('json', MODE)
    kwargs.setdefault('headers', csrf_headers(client, origin))
    return client.post(ACTION, base_url=origin, environ_base=peer or {}, **kwargs)


def test_restrict_from_lan_changes_only_host_and_keeps_notification_grant(installation):
    client, servers = installation
    before = V2Config.load()._persisted_document()
    assert host_row(client)['current'] is True
    row = host_row(client, LAN, PEER)
    assert row['network_open'] is True and row['current'] is False
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        web_push_service.upsert_subscription(conn, user_key='local', payload={
            'endpoint': 'https://push.example.test/anonymous', 'keys': {'p256dh': 'fixture', 'auth': 'fixture'}})
    reply = restrict(client, LAN, PEER)
    assert reply.status_code == 200 and reply.get_json() == {'ok': True, 'mode': 'this_computer'}
    assert not servers[0].open and servers[1].open
    expected = before.copy()
    expected['ui'] = {**before['ui'], 'setup_host': '127.0.0.1'}
    assert V2Config.load()._persisted_document() == expected
    assert client.get('/api/session', base_url=LAN, environ_base=PEER).status_code == 503
    assert host_row(client)['network_open'] is False
    assert restrict(client).status_code == 200
    legacy, = client.get('/api/devices', base_url=HOST).get_json()['unattributed']
    assert legacy['push_only'] and legacy['account'] is None
    stop = client.post('/api/devices/' + legacy['id'] + '/sign-out', base_url=HOST, headers=csrf_headers(client, HOST), json={})
    assert stop.status_code == 200
    with engine.connect() as conn:
        assert web_push_service.count_enabled(conn) == 0


@pytest.mark.parametrize(('wild', 'target'), [('0.0.0.0', '127.0.0.1'), ('::', '::1')])
def test_lone_wildcard_never_reported_closed_until_actual_loopback(installation, wild, target):
    client, servers = installation
    config = V2Config.load()
    config.ui.setup_host = wild
    config.save()
    servers[:] = [Acceptor(wild)]
    assert restrict(client).status_code == 200
    assert servers[0].open and V2Config.load().ui.setup_host == target
    assert host_row(client)['network_open'] is None
    servers[:] = [Acceptor(target)]
    assert host_row(client)['network_open'] is False


@pytest.mark.parametrize('deployment', ['container', 'docker-declaration', 'proxy-config', 'proxy-env', 'no-sockets', 'bound', 'reset'])
def test_no_write_when_not_avibe_controlled_or_not_unbound(installation, monkeypatch, deployment):
    from config import paths

    client, servers = installation
    config = V2Config.load()
    error = 'network_access_proxy_managed'
    if deployment == 'container':
        monkeypatch.setattr(ui_server, '_is_containerized_runtime', lambda: True)
        error = 'network_access_container_managed'
    elif deployment == 'docker-declaration':
        monkeypatch.setenv('VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST', '127.0.0.1')
        error = 'network_access_container_managed'
    elif deployment == 'proxy-config':
        config.ui.trusted_public_origins = ['https://proxy.example.test']
    elif deployment == 'proxy-env':
        monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, 'https://proxy.example.test')
    elif deployment == 'no-sockets':
        servers.clear()
        error = 'network_access_listener_unavailable'
    elif deployment == 'bound':
        config.remote_access.vibe_cloud.instance_id = 'fixture'
        config.remote_access.vibe_cloud.instance_secret = 'fixture-key'
        monkeypatch.setenv('VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST', '127.0.0.1')
        error = 'network_access_bound'
    else:
        (paths.get_state_dir() / 'remote-binding-reset.json').touch()
        error = 'network_access_state_unavailable'
    config.save()
    before = paths.get_config_path().read_bytes()
    row = host_row(client)
    assert ('network_open' not in row) if deployment == 'bound' else row['network_open'] is None
    reply = restrict(client)
    assert reply.status_code in (409, 503) and reply.get_json()['error'] == error
    assert paths.get_config_path().read_bytes() == before


def test_restriction_guards_and_storage_failure_leave_old_access(installation, monkeypatch):
    client, servers = installation
    assert http_authorization_policy('POST', ACTION) == http_authorization_policy('POST', '/api/ui/reload')
    assert client.post(ACTION, base_url=HOST, headers={'Origin': HOST}, json=MODE).status_code == 403
    assert restrict(client, headers={**csrf_headers(client, HOST), 'Origin': 'http://other.example.test'}).status_code == 403
    assert restrict(client, json={**MODE, 'host': 'localhost'}).status_code == 400

    def failed_save(*args):
        raise OSError('fixture write failure')

    monkeypatch.setattr(V2Config, 'save', failed_save)
    reply = restrict(client)
    assert reply.status_code == 503 and reply.get_json()['error'] == 'network_access_save_failed'
    assert all(s.open for s in servers) and host_row(client, LAN, PEER)['network_open'] is True


@pytest.mark.asyncio
async def test_reply_is_attempted_before_cleanup_even_if_send_fails(installation):
    client, servers = installation
    config = V2Config.load()
    config.ui.setup_host = '127.0.0.1'
    config.save()
    seen = []

    async def broken_send(message):
        assert all(s.open for s in servers)
        seen.append(message['type'])
        if message['type'] == 'http.response.body':
            raise OSError('fixture disconnect')

    with pytest.raises(OSError):
        await ui_server._NetworkRestrictionResponse('127.0.0.1', 15131)({'type': 'http'}, None, broken_send)
    assert seen == ['http.response.start', 'http.response.body']
    assert not servers[0].open and servers[1].open
    assert host_row(client)['network_open'] is False

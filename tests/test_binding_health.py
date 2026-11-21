"""Behavior of the scheduled device credential check, isolated from real issuers and connectors."""
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from vibe import binding_health as health, remote_access as remote


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    cfg = V2Config.default()
    cloud = cfg.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = 'https://issuer.test'
    cloud.instance_id = 'instance'
    cloud.instance_secret = 'synthetic-device-key'
    cloud.tunnel_token = 'synthetic-token'
    cfg.save()
    return cfg


def test_scheduled_rejection_is_consecutive_persistent_and_reversible(config, monkeypatch):
    now = [1000]
    monitor = health.RevisionMonitor()
    reads = []
    replies = iter(('reject', 'unknown', 'reject', 'reject', 'reject', 'accept'))

    def read(_config):
        reads.append(now[0])
        answer = next(replies)
        if answer == 'reject':
            raise remote.BackendRequestError(401, {'error': 'invalid_device_secret'})
        if answer == 'unknown':
            raise TimeoutError()
        return {'authorization_revision': 3}

    monkeypatch.setattr(health, 'time', SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(remote, 'time', SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    monkeypatch.setattr(remote, '_device_json_request', lambda *_: {'authorization_revision': 3})
    monkeypatch.setattr(health, 'read_revision', read)
    monkeypatch.setattr(remote.instance_address, 'note_generation_hint', lambda _: None)
    from storage import remote_access_authorization_service
    monkeypatch.setattr(remote_access_authorization_service, 'mark_matching_revision_checked', lambda **kw: None)
    for second in (1000, 1001, 1015, 1030):
        now[0] = second
        remote.sync_authorization_revision_once(monitor=monitor)
        assert health.rejected_at(config) is None
    now[0] = 1045
    remote.sync_authorization_revision_once(monitor=monitor)
    assert health.rejected_at(V2Config.load()) == 1045
    assert 'synthetic' not in health._path().read_text()
    for name in ('_stop_connector', 'stop', '_resolve_binary'):
        monkeypatch.setattr(remote, name, lambda *_: pytest.fail('must not disturb the running connector'))
    assert remote._start(config, record_request=False)['error'] == 'binding_rejected'
    with pytest.raises(RuntimeError, match='binding_rejected'):
        remote._start_candidate_connector(config, 'not-spawned', 'auto')
    monitor = health.RevisionMonitor()  # Only the verdict survives a restart.
    now[0] = 1165
    remote.sync_authorization_revision_once(monitor=monitor)
    now[0] = 1180
    assert remote.sync_authorization_revision_once(monitor=monitor)['error'] == 'authorization_revision_deferred'
    now[0] = 1465
    assert remote.sync_authorization_revision_once(monitor=monitor) == {'ok': True, 'authorization_revision': 3}
    assert reads == [1000, 1015, 1030, 1045, 1165, 1465]
    assert health.rejected_at(config) is None


@pytest.mark.parametrize('answer', ['accepted', 'rejected'])
def test_split_dns_can_refresh_watermark_but_never_changes_verdict(config, monkeypatch, answer):
    import ipaddress
    monkeypatch.setattr(remote, '_resolve_pairing_backend_addresses', lambda *_: (ipaddress.ip_address('10.2.3.4'),))
    monkeypatch.setattr(remote, '_validated_backend_connections', lambda *_: pytest.fail('private address not pinned'))
    calls = []
    def established(cfg, method, suffix):
        calls.append((method, suffix))
        if answer == 'rejected':
            raise remote.BackendRequestError(401, {'error': 'invalid_device_secret'})
        return {'authorization_revision': 4}
    monkeypatch.setattr(remote, '_device_json_request', established)
    monitor = health.RevisionMonitor()
    for _ in range(2):
        monitor.next_probe = 0
        result = remote.sync_authorization_revision_once(monitor=monitor)
        assert result['ok'] == (answer == 'accepted')
        assert monitor.first is None and health.rejected_at(config) is None
    if answer == 'accepted':
        assert remote._load_authorization_revision_snapshot(config)[0] == 4
    assert calls == [('GET', 'authorization-revision')] * 2


def test_delayed_rejection_cannot_affect_another_pairing_or_recovery(config, monkeypatch):
    monitor = health.RevisionMonitor()
    monitor.due(config, 0)
    monitor.observe(config, 'rejected', 0)
    current = V2Config.load()
    current.remote_access.vibe_cloud.instance_secret = 'replacement'
    current.save()
    monitor.observe(config, 'rejected', 15)
    assert not health._path().exists()
    assert monitor.due(current, 16)
    monitor.observe(current, 'rejected', 16)
    assert health.rejected_at(current) is None
    monkeypatch.setattr(remote, '_read_pending_pairing_record', lambda: ({'phase': 'prepared'}, None))
    monitor.observe(current, 'rejected', 31)
    assert not health._path().exists()


@pytest.mark.parametrize(('status', 'body', 'mime', 'age', 'expected'), [
    (401, b'{"error":"invalid_device_secret"}', 'application/json', '0', 'reject'),
    (401, b'{"error":"invalid_device_secret"}', 'text/html', '0', 'unknown'),
    (401, b'{"error":"invalid_device_secret"}', 'application/json', '60', 'unknown'),
    (429, b'{"error":"invalid_device_secret"}', 'application/json', '0', 'unknown'),
    (302, b'{"error":"invalid_device_secret"}', 'application/json', '0', 'unknown'),
    (200, b'{"authorization_revision":false}', 'application/json', '0', 'unknown'),
    (200, b'{"authorization_revision":4}', 'application/json', '0', 'accept'),
])
def test_reader_uses_validated_proxy_and_classifies_only_issuer_reply(config, monkeypatch, status, body, mime, age, expected):
    calls = []
    target = SimpleNamespace(base_url='https://issuer.test', requires_proxy=True, host_header='issuer.test')
    monkeypatch.setattr(remote, '_normalize_pairing_backend_url', lambda url: (target, None))
    monkeypatch.setattr(remote, '_validated_backend_proxy_url', lambda url: 'http://proxy.test')
    response = SimpleNamespace(status=status, read=lambda limit: body[:limit], getheader={'Content-Type': mime, 'Age': age}.get)
    conn = SimpleNamespace(connect=lambda: calls.append('connect'), request=lambda *a, **kw: calls.append(a),
                           getresponse=lambda: response, close=lambda: calls.append('close'))

    def connections(actual, timeout, proxy):
        assert actual is target and proxy == 'http://proxy.test'
        yield conn

    monkeypatch.setattr(remote, '_validated_backend_connections', connections)
    if expected == 'accept':
        assert health.read_revision(config) == {'authorization_revision': 4}
    else:
        with pytest.raises(remote.BackendRequestError if expected == 'reject' else ValueError):
            health.read_revision(config)
    assert calls == ['connect', ('GET', '/api/v1/instances/instance/authorization-revision'), 'close']

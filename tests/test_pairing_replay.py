"""Request-bound replay across the installed Runtime's actual HTTP decoder."""
import json
import logging
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from tests.test_desktop_local_instance import _prepare, _pair_body, desktop_host as desktop_host
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from tests.ui_server_test_helpers import csrf_headers
from vibe import pairing_replay as replay, remote_access as ra

_HTTP = ra._json_request
ROUTE = '/api/remote-access/vibe-cloud/pair'


class Wire:
    def __init__(self):
        self.sent = []
        self.advice = None
        self.status = 502
        self.body = b'{"error":"tunnel_origin_update_failed"}'

    def request(self, method, path, *, body, headers):
        assert method == 'POST' and path == '/api/v1/pairing/redeem'
        self.sent.append((ra._read_pending_pairing_record()[0]['operation_id'], body))

    def getresponse(self):
        if not self.status:
            raise ConnectionResetError('fixture: connection lost')
        return SimpleNamespace(status=self.status, read=lambda: self.body, getheader=lambda name: self.advice)

    def close(self):
        pass


@pytest.fixture
def setup_replay(desktop_host, monkeypatch):
    wire = Wire()
    body = _pair_body(_prepare(desktop_host).get_json())
    monkeypatch.setattr(ra, '_json_request', _HTTP)
    monkeypatch.setattr(ra, '_validated_backend_proxy_url', lambda _: None)
    monkeypatch.setattr(ra, '_validated_backend_connections', lambda *args: [wire])
    clock = [ra.time.time()]
    monkeypatch.setattr(replay, '_now', lambda: clock[0])
    return wire, clock, body, lambda payload: desktop_host.post(ROUTE, json=payload, headers=csrf_headers(desktop_host))


def view():
    return ra.pending_pairing_status(V2Config.load())


@pytest.mark.parametrize(('status', 'body', 'category'), [
    (502, b'{"error":"tunnel_origin_update_failed"}', 'server_error'),
    (503, b'<html><body>Service Unavailable</body></html>', 'server_error'),
    (0, b'', 'network'),
])
def test_retry_from_disk_uses_identical_wire_request_and_safe_log(setup_replay, caplog, status, body, category):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = status, body
    with caplog.at_level(logging.INFO, logger='vibe.pairing_replay'):
        assert post(payload).get_json()['error'] == 'pairing_redeem_indeterminate'
        assert view() == dict(phase='prepared', can_resume=True, replay_state='scheduled', next_retry_at=clock[0]+2)
        replay.tick()
        assert len(wire.sent) == 1
        wire.status, wire.body = 200, json.dumps(_response('replayed')).encode()
        clock[0] += 2
        replay.tick()  # no browser poll, CLI call, or in-memory timer to restore
    assert len(wire.sent) == 2 and wire.sent[0] == wire.sent[1]
    assert view() is None and V2Config.load().remote_access.vibe_cloud.instance_id == 'replayed'
    logs = [r.getMessage() for r in caplog.records if r.name == 'vibe.pairing_replay']
    assert logs == [f'Pairing redeem attempt=1 result={category} next_retry_at={clock[0]}',
                    'Pairing redeem attempt=2 result=success next_retry_at=None']


def test_server_pending_is_a_floor_for_manual_and_auto_then_expiry_stops(setup_replay):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 409, b'{"error":"pairing_redeem_pending","retry_after_seconds":7}'
    assert post(payload).status_code == 409
    clock[0] += 3
    assert post({}).get_json()['retry_after_seconds'] == 4
    replay.tick()
    assert len(wire.sent) == 1
    wire.status, wire.body = 400, b'{"error":"pairing_key_expired"}'
    clock[0] += 4
    replay.tick()
    assert view() == dict(phase='prepared', can_resume=False, replay_state='terminated')
    assert post({}).get_json()['error'] == 'pairing_key_expired'
    clock[0] += 60
    replay.tick()
    assert len(wire.sent) == 2


@pytest.mark.parametrize('limit', ['time', 'attempts'])
def test_exhaustion_survives_restart_manual_attempt_does_not_reset_budget(setup_replay, limit):
    wire, clock, payload, post = setup_replay
    if limit == 'attempts':
        wire.status, wire.body = 409, b'{"error":"pairing_redeem_pending","retry_after_seconds":1}'
    assert not post(payload).get_json()['ok']
    if limit == 'time':
        # Anchor to the durable first-send deadline, not the wall clock before HTTP setup.
        clock[0] = replay._state(ra._read_pending_pairing_record()[0])['deadline'] + 1
    else:
        for _ in range(20):
            clock[0] += 1
            replay.tick()
        clock[0] += 1
    before = len(wire.sent)
    assert before == (1 if limit == 'time' else 21)
    assert view()['replay_state'] == 'manual'
    replay.tick()
    assert len(wire.sent) == before
    post({})
    assert len(wire.sent) == before + 1 and view()['replay_state'] == 'manual'


def test_rate_limit_advice_is_not_shortened(setup_replay):
    wire, clock, payload, post = setup_replay
    wire.status, wire.advice = 429, '90'
    post(payload)
    assert view()['next_retry_at'] == clock[0] + 90
    clock[0] += 60
    assert post({}).get_json()['retry_after_seconds'] == 30
    replay.tick()
    assert len(wire.sent) == 1


@pytest.mark.parametrize('age, expected', [(10, 'scheduled'), (901, 'manual')])
def test_upgrade_only_adopts_recent_legacy_journal(setup_replay, age, expected):
    wire, clock, payload, post = setup_replay
    post(payload)
    record, _ = ra._read_pending_pairing_record()
    record.pop('redeem_prepared_at')
    record['prepared_at'] = clock[0] - age
    ra._write_pending_pairing_record(record)
    replay._path().unlink()
    assert view()['replay_state'] == expected
    replay.tick()
    assert len(wire.sent) == (2 if expected == 'scheduled' else 1)


def test_concurrent_cli_cannot_dispatch_during_automatic_replay(setup_replay, monkeypatch):
    wire, clock, payload, post = setup_replay
    post(payload)
    clock[0] += 2
    entered, release = threading.Event(), threading.Event()
    respond = wire.getresponse
    def slow():
        entered.set()
        assert release.wait(20)
        return respond()
    monkeypatch.setattr(wire, 'getresponse', slow)
    worker = threading.Thread(target=replay.tick)
    worker.start()
    try:
        assert entered.wait(10)
        child = subprocess.run([sys.executable, '-c',
            'from vibe import remote_access as r; '
            'r._json_request=lambda *a,**kw: (_ for _ in ()).throw(AssertionError("duplicate send")); '
            'assert r.pair("", "") ["error"] == "pairing_redeem_pending"'],
            env=dict(os.environ), timeout=15, check=False)
        assert child.returncode == 0 and len(wire.sent) == 2
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()

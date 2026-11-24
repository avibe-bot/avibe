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

    def connect(self):
        pass

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


@pytest.mark.parametrize('error', ['invalid_pairing_key', 'pairing_key_used'])
def test_replayed_rejection_never_sets_first_input_hint(setup_replay, error):
    wire, clock, payload, post = setup_replay
    assert post(payload).get_json()['replay_state'] == 'scheduled'
    clock[0] += 2
    wire.status = 400
    wire.body = json.dumps({'error': error, 'input_rejected': True}).encode()
    result = post({}).get_json()
    assert len(wire.sent) == 2 and wire.sent[0] == wire.sent[1]
    assert not result['ok'] and 'input_rejected' not in result


def test_server_pending_is_a_floor_for_manual_and_auto_then_expiry_stops(setup_replay):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 409, b'{"error":"pairing_redeem_pending","retry_after_seconds":7}'
    pending = post(payload)
    assert pending.status_code == 400
    assert pending.get_json()['status'] == 409 and pending.get_json()['retry_after_seconds'] == 7
    assert pending.get_json()['pairing'] == {'ok': False, 'recoverable': True, 'cause': 'pairing_redeem_pending'}
    clock[0] += 3
    assert post({}).get_json()['retry_after_seconds'] == 4
    replay.tick()
    assert len(wire.sent) == 1
    wire.status, wire.body = 400, b'{"error":"pairing_key_expired"}'
    clock[0] += 4
    replay.tick()
    assert view() == dict(phase='prepared', can_resume=False, replay_state='terminated', operation_owner='desktop_app')
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


@pytest.mark.parametrize('age', [10, 901])
@pytest.mark.parametrize('outcome', ['unarchived_success', 'rate_limited', 'server_error'])
def test_upgrade_cannot_distinguish_legacy_deliveries(setup_replay, monkeypatch, age, outcome):
    wire, clock, payload, post = setup_replay
    if outcome == 'unarchived_success':
        wire.status, wire.body = 200, json.dumps(_response('received')).encode()
        write = ra._write_pending_pairing_record
        def fail_archive(record):
            if record['phase'] == 'redeemed':
                raise OSError('fixture: full')
            write(record)
        monkeypatch.setattr(ra, '_write_pending_pairing_record', fail_archive)
    elif outcome == 'rate_limited':
        wire.status, wire.advice = 429, '900'
    post(payload)
    record, _ = ra._read_pending_pairing_record()
    record.pop('redeem_prepared_at')
    record.pop('replay_budget_claimed')
    record['prepared_at'] = clock[0] - age
    ra._write_pending_pairing_record(record)
    replay._path().unlink()
    assert view() == dict(phase='prepared', can_resume=False, replay_state='needs_reconciliation')
    before = ra._pending_pairing_path().read_bytes()
    for advance in (0, 60, 901):
        clock[0] += advance
        replay.tick()
        assert post({}).get_json()['error'] == 'pairing_needs_reconciliation'
        assert not post({**payload, 'pairing_key': 'fixture-renewal'}).get_json()['ok']
        assert not ra.pair('other-fixture', payload['backend_url'])['ok']
    assert len(wire.sent) == 1 and ra._pending_pairing_path().read_bytes() == before


@pytest.mark.parametrize('failed_before_post', [False, True])
def test_multiple_pinned_addresses_only_fall_back_before_delivery(setup_replay, monkeypatch, failed_before_post):
    first, clock, payload, post = setup_replay
    second = Wire()
    first.status = 0  # read failed after the first peer received the POST
    if failed_before_post:
        def failed_connect():
            raise ConnectionRefusedError()
        monkeypatch.setattr(first, 'connect', failed_connect)
    monkeypatch.setattr(ra, '_validated_backend_connections', lambda *args: [first, second])
    post(payload)
    assert (len(first.sent), len(second.sent)) == ((0, 1) if failed_before_post else (1, 0))
    replay.tick()
    assert len(first.sent) + len(second.sent) == 1
    clock[0] += 2
    replay.tick()
    assert len(first.sent) + len(second.sent) == 2


def test_received_success_cannot_be_automatically_redeemed_again(setup_replay, monkeypatch):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 200, json.dumps(_response('received')).encode()
    write = ra._write_pending_pairing_record
    def fail_archive(record):
        if record['phase'] == 'redeemed':
            raise OSError('fixture: full')
        return write(record)
    monkeypatch.setattr(ra, '_write_pending_pairing_record', fail_archive)
    assert post(payload).get_json()['pairing']['recoverable'] is False
    assert view() == dict(phase='prepared', can_resume=False, replay_state='needs_reconciliation')
    clock[0] += 120
    replay.tick()
    assert post({}).get_json()['error'] == 'pairing_needs_reconciliation'
    assert not ra.pair('another-key', payload['backend_url'])['ok']
    assert len(wire.sent) == 1


@pytest.mark.parametrize('lost', ['crash', 'write_failed'])
@pytest.mark.parametrize('status', [409, 429])
def test_unsettled_reservation_never_acquires_permission_with_time(setup_replay, monkeypatch, lost, status):
    wire, clock, payload, post = setup_replay
    wire.status, wire.advice = status, '900' if status == 429 else None
    wire.body = b'{"error":"pairing_redeem_pending","retry_after_seconds":60}'
    save = replay._save
    class Crash(BaseException):
        pass
    def settle(state):
        if state['mode'] != 'unsettled':
            clock[0] += 75  # response/settlement can be much later than reservation
            if lost == 'crash':
                raise Crash()
            raise OSError('fixture: full')
        return save(state)
    with monkeypatch.context() as m:
        m.setattr(replay, '_save', settle)
        def send():
            return ra.pair(payload['pairing_key'], payload['backend_url'], payload['device_name'],
                           desktop_enrollment=payload['desktop_enrollment'])
        if lost == 'crash':
            with pytest.raises(Crash):
                send()
        else:
            assert send()['ok'] is False
    for advance in (0, 60, 901):
        clock[0] += advance
        replay.tick()
        assert post({}).get_json()['error'] == 'pairing_needs_reconciliation'
    assert len(wire.sent) == 1


@pytest.mark.parametrize('damage', ['missing', 'no_key', 'wrong_key'])
def test_claimed_scheduler_damage_cannot_buy_more_sends(setup_replay, damage):
    wire, clock, payload, post = setup_replay
    post(payload)
    if damage == 'missing':
        replay._path().unlink()
    else:
        saved = json.loads(replay._path().read_text())
        saved.pop('key')
        if damage == 'wrong_key':
            saved['key'] = 'unrelated'
        replay._save(saved)
    clock[0] += 120
    assert view()['replay_state'] == 'needs_reconciliation'
    replay.tick()
    assert post({}).get_json()['error'] == 'pairing_needs_reconciliation'
    assert len(wire.sent) == 1


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
    before = ra._pending_pairing_path().read_bytes()
    worker = threading.Thread(target=replay.tick)
    worker.start()
    try:
        assert entered.wait(10)
        busy = post({'pairing_key': ''})
        assert busy.status_code == 400
        data = busy.get_json()
        assert data['error'] == 'pairing_local_busy' and data['accepted'] is False
        assert 'retry_after_seconds' not in data and 'pairing' not in data
        child = subprocess.run([sys.executable, '-c',
            'from vibe import remote_access as r; '
            'r._json_request=lambda *a,**kw: (_ for _ in ()).throw(AssertionError("duplicate send")); '
            'assert r.pair("", "") ["error"] == "pairing_local_busy"; '
            'assert r.pair("different-test-key", "https://other.test") ["error"] == "pairing_local_busy"'],
            env=dict(os.environ), timeout=15, check=False)
        assert child.returncode == 0 and len(wire.sent) == 2
        assert ra._pending_pairing_path().read_bytes() == before
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive()

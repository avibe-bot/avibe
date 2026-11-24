"""Offline state-machine acceptance for the A–H pairing recovery boundary.

Build the roots by running real claims, intercepting only the fixture transport
and a selected crash/write boundary. Real journal/sidecar/marker files, config
and locks survive each step. An absorbing successor with identical disk bytes,
clock and peer ledgers is explored once per remaining depth, not 8**4 times.
"""
import json
import subprocess
import sys

import pytest

from config.v2_config import V2Config
from tests.test_desktop_local_instance import desktop_host as desktop_host
from tests.test_pairing_recovery import pairing_host as pairing_host, _response
from tests.test_pairing_replay import Wire, _HTTP, setup_replay as setup_replay
from tests.test_personal_claims import claim_peer as claim_peer
from tests.ui_server_test_helpers import csrf_headers
from vibe import desktop_local_instance as local, pairing_replay as replay, personal_claims as claims, remote_access as ra, ui_server


class PowerLoss(BaseException):
    """Unwind real held locks without running the normal settlement path."""


def claim():
    return claims.run_link(expected_subject='owner-id', show_link=lambda _: None)


class GrantPeer:
    """Two boundaries: raw arrivals vs. idempotent transitions on fixture grants."""

    def __init__(self):
        self.accepts, self.posts = [], []
        self.issued, self.consumed = {}, {}
        self.ready = set()

    def issue(self, claim_id, offered_key):
        # A repeated accept serves the already issued authorization.
        return self.issued.setdefault(claim_id, offered_key)

    def redeem(self, raw):
        self.posts.append(raw)  # first operation on every redeem, including malformed requests
        key = json.loads(raw)['pairing_key']
        if key not in self.issued.values():
            return 403, b'{}'
        if key in self.consumed:
            return (200, self.consumed[key][1]) if self.consumed[key][0] == raw else (409, b'{}')
        if key in self.ready:
            receipt = json.dumps(_response()).encode()
            self.consumed[key] = (raw, receipt)
            return 200, receipt
        return None

    def snapshot(self):
        return (tuple(self.accepts), tuple(self.posts), dict(self.issued), dict(self.consumed))


def assert_quiet(before, after):
    assert after == before, 'unexpected fixture transport arrival or grant transition'


def test_outside_oracle_observes_a_rejected_delivery():
    peer = GrantPeer()
    baseline = peer.snapshot()
    body = b'{"pairing_key":"unissued-fixture"}'
    assert peer.redeem(body)[0] == 403 and not peer.consumed
    with pytest.raises(AssertionError, match='unexpected fixture transport'):
        assert_quiet(baseline, peer.snapshot())


@pytest.mark.parametrize('damage', [False, True])
def test_guarded_desktop_delivery_uses_real_prepare_and_evidence(setup_replay, damage, monkeypatch):
    wire, clock, payload, post = setup_replay  # real prepare route, same installation and operation
    arrived = []
    request = wire.request
    def observe(method, path, *, body, headers):
        arrived.append(body)
        return request(method, path, body=body, headers=headers)
    monkeypatch.setattr(wire, 'request', observe)
    wire.status, wire.advice, wire.body = 429, '90', b'{}'
    assert post(payload).get_json()['replay_state'] == 'scheduled'
    if damage:
        claims._marker().write_text('{"bound":{"claim_id":"12345678-1234-4234-a234-123456789abc","generation":"bad"}}')
    clock[0] += 100  # past the floor, within the original budget in both histories
    result = post(payload).get_json()
    if damage:
        assert result['error'] == 'pairing_needs_reconciliation' and len(arrived) == 1
    else:
        assert result['replay_state'] == 'scheduled' and len(arrived) == 2 and arrived[0] == arrived[1]


@pytest.mark.parametrize('cut', ['issued_before_handoff', 'unsettled', 'received_not_archived', 'damaged_marker', 'retryable'])
def test_bounded_restart_sequences(cut, desktop_host, claim_peer, monkeypatch):
    clock = [replay._now()]
    monkeypatch.setattr(replay, '_now', lambda: clock[0])
    wire = Wire()
    peer = GrantPeer()
    arrivals = []
    peer_request = claim_peer.request
    def accept(method, path, body, headers):
        arrivals.append((method, path, body))  # first operation, even for malformed input
        payload = json.loads(body)
        if 'accepted_subject' in payload:
            peer.accepts.append(payload['claim_id'])
        peer_request(method, path, body, headers)
        if 'accepted_subject' in payload and claim_peer.result[1].get('status') == 'redeemed':
            claim_peer.result[1]['pairing']['pairing_key'] = peer.issue(payload['claim_id'], claim_peer.key)
    monkeypatch.setattr(claim_peer, 'request', accept)
    request = wire.request
    def post(method, path, *, body, headers):
        answer = peer.redeem(body)
        if answer:
            wire.status, wire.body = answer
        request(method, path, body=body, headers=headers)
    monkeypatch.setattr(wire, 'request', post)
    monkeypatch.setattr(ra, '_json_request', _HTTP)
    monkeypatch.setattr(ra, '_validated_backend_connections', lambda *_: [wire])
    claim_peer.key = 'fixture-A'
    wire.status, wire.body = 422, b'{"error":"pairing_request_failed"}'
    assert claim()['code'] == 'pairing_recovery_required'
    a = claim_peer.id
    first_request = wire.sent[0]
    claim_peer.key = 'fixture-B'

    # G gets a request-bound, replaceable refusal for A. H accepts B while
    # holding the send lock. The fixture may interrupt that exact handoff.
    publish = ra._write_pending_pairing_record
    def write(record):
        if record.get('personal_claim', {}).get('claim_id') != a:
            if cut == 'issued_before_handoff' and record['phase'] == 'prepared':
                raise PowerLoss()
            if cut == 'received_not_archived' and record['phase'] == 'redeemed':
                raise OSError('fixture receipt write failure')
        publish(record)
    with monkeypatch.context() as m:
        m.setattr(ra, '_write_pending_pairing_record', write)
        response = wire.getresponse
        def respond():
            if claim_peer.id != a and json.loads(wire.sent[-1][1])['pairing_key'] == 'fixture-B':
                if cut == 'unsettled':
                    raise PowerLoss()
                if cut != 'received_not_archived':
                    wire.status, wire.advice, wire.body = 429, '90', b'{}'
            return response()
        m.setattr(wire, 'getresponse', respond)
        if cut == 'received_not_archived':
            peer.ready.add('fixture-B')
        if cut in ('issued_before_handoff', 'unsettled'):
            with pytest.raises(PowerLoss):
                claim()
        else:
            assert claim()['state'] == 'error'
    b = claim_peer.id
    assert a != b and peer.issued == {a: 'fixture-A', b: 'fixture-B'} and peer.accepts == [a, b]
    assert wire.sent[:2] == [first_request, first_request]
    if cut == 'damaged_marker':
        saved = claims._outcome()
        saved['expires_at'] = 'not a timestamp'
        claims._marker().write_text(json.dumps(saved))
    elif cut == 'retryable':
        saved = claims._outcome()
        saved['expires_at'] = '2001-01-01T00:00:00+00:00'  # valid historical date, same grant
        claims._marker().write_text(json.dumps(saved))
    paths = (ra._pending_pairing_path(), replay._path(), claims._marker(), local._identity_path())
    def evidence():
        return (tuple(p.read_bytes() if p.exists() else None for p in paths),
                ra._pairing_identity_from_cloud(V2Config.load().remote_access.vibe_cloud),
                list(wire.sent), tuple(arrivals), peer.snapshot())
    rec, _ = ra._read_pending_pairing_record()
    assert rec['personal_claim']['claim_id'] == (a if cut == 'issued_before_handoff' else b)
    assert json.loads(claims._marker().read_bytes())['claim_id'] == b
    if cut == 'received_not_archived':
        assert replay._saved()['mode'] == 'received_not_archived' and set(peer.consumed) == {'fixture-B'}
    elif cut == 'unsettled':
        assert replay._saved()['mode'] == 'unsettled'

    # New clients and fresh disk reads; no in-memory flow survives a cut.
    # All actions are confined to this pytest instance and the two fixture peers.
    def step(entry, ident):
        client = ui_server.app.test_client()
        guard = {'installation_id': rec['personal_claim']['installation_id'], 'operation_id': rec['operation_id'],
                 'expected_binding_revision': local.claim_revision(V2Config.load(), local._installation_id(), rec),
                 'account_id': 'owner-id'}
        key = 'fixture-A' if ident == a else 'fixture-B'
        try:
            if entry == 'A':
                replay.tick()
            elif entry == 'B':
                client.post('/api/remote-access/vibe-cloud/pair', json={}, headers=csrf_headers(client))
            elif entry == 'C':
                ra.pair('', '', manual_only=True)
            elif entry == 'D':
                ra.pair(key, rec['backend_url'])
            elif entry == 'E':
                ra.pair(key, rec['backend_url'], 'fixture', desktop_enrollment=guard)
            elif entry == 'F':
                claims.recover_pairing(ident)
            elif entry == 'G':
                claims._Source.load(None)
            else:
                claim()
        except (claims.ClaimError, local.LocalInstanceError):
            pass

    baseline = evidence()
    # A genuinely new interpreter loads the same installation from disk. No
    # worker sockets or transport out of this test are permitted.
    subprocess.run([sys.executable, '-c', '''
import sys
from config.v2_config import V2Config
from vibe import pairing_replay as replay, remote_access as ra

def no_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise SystemExit('unexpected external transport in restart probe')
sys.addaudithook(no_network)
replay._now = lambda: float(sys.argv[2])
assert ra.pending_pairing_status(V2Config.load())['replay_state'] == sys.argv[1]
replay.tick()
''', 'scheduled' if cut == 'retryable' else 'needs_reconciliation', str(clock[0])], check=True, timeout=20)
    assert evidence() == baseline
    # In the retryable control stay strictly before Retry-After; in the four
    # blocked states advance past the floor/window: age cannot clear evidence.
    # The external clock advances between actions. At a given depth every
    # equal successor has the same remaining suffixes. Folding these states
    # explores all choices of A–H and either claim to depth four without
    # cloning SQLite or replaying a combinatorial number of identical reads.
    explored = set()
    def visit(remaining):
        assert evidence() == baseline
        signature = (remaining, clock[0], tuple(baseline[0]))
        if not remaining or signature in explored:
            return
        explored.add(signature)
        previous = clock[0]
        for entry in 'ABCDEFGH':
            for ident in (a, b):
                clock[0] = previous + (1 if cut == 'retryable' else 100)
                monkeypatch.setattr(claims, 'redirect_claims', claims.RedirectClaims())
                step(entry, ident)
                assert_quiet(baseline[-1], peer.snapshot())
                assert evidence() == baseline, (cut, remaining, entry, ident)
                visit(remaining - 1)
        clock[0] = previous
    visit(4)
    assert len(explored) == 4
    if cut == 'retryable':
        clock[0] += 100
        peer.ready.add('fixture-B')
        replay.tick()
        assert wire.sent[-2] == wire.sent[-1] and len(wire.sent) == len(baseline[2]) + 1
        assert set(peer.consumed) == {'fixture-B'} and peer.accepts == [a, b]
        assert peer.posts[-1] == peer.posts[-2] and peer.issued == {a: 'fixture-A', b: 'fixture-B'}
        assert claims.recover_pairing(b)['state'] == 'bound'

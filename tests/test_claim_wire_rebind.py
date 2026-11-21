"""Machine contract at 540333042d81. No account, network or connector is used."""
from uuid import uuid4

import pytest

from tests.test_personal_claims import claim_peer as claim_peer, pairing_host as pairing_host
from vibe.personal_claim_client import ClaimError, ClaimsClient, ConfirmedRefusal, ConstrainedClaimsClient


@pytest.fixture
def issuer(claim_peer, monkeypatch):
    flags = {}
    request = claim_peer.request

    def respond(method, path, body, headers):
        request(method, path, body, headers)
        if path == '/api/v1/claims':
            claim_peer.result[1].update(flags)

    monkeypatch.setattr(claim_peer, 'request', respond)
    return claim_peer, flags


def start(client, mode='redirect', origin='http://127.0.0.1'):
    return client.start(str(uuid4()), 'fixture', mode, 'en', state='fixture-state', origin=origin)


@pytest.mark.parametrize(('ack', 'reason', 'extra', 'expected'), [
    (True, 'paired_installation_loopback_required', {}, 'paired_installation_loopback_required'),
    (True, 'active_pairing_recent', {}, 'active_pairing_recent'),
    (False, 'active_pairing_recent', {}, 'binding_unavailable'),
    (1, 'paired_installation_loopback_required', {}, 'binding_unavailable'),
    (True, 'active_pairing_recent', {'recovery': 'reset_as_new'}, 'binding_unavailable'),
])
def test_ordinary_reason_requires_negotiation_and_closed_shape(issuer, ack, reason, extra, expected):
    peer, flags = issuer
    flags['ordinary_decision_readback'] = ack
    client = ClaimsClient('https://avibe.bot')
    ticket = start(client)
    peer.outcomes.append((409, {'error': 'binding_unavailable', 'reason': reason, **extra}, None))
    with pytest.raises(ClaimError) as caught:
        client.observe(ticket)
    assert caught.value.code == expected
    payload = peer.calls[-1][1]
    assert set(payload) == ({'claim_id', 'code_verifier', 'read_decision'} if ack is True else {'claim_id', 'code_verifier'})
    assert not isinstance(caught.value, ConfirmedRefusal)


@pytest.mark.parametrize(('fields', 'outcome'), [
    ({'reason': 'other_account', 'owner_hint': 'a***@b***', 'recovery': 'reset_as_new'}, 'other_account'),
    ({'reason': 'predecessor_unrecoverable', 'recovery': 'reset_as_new'}, 'predecessor_unrecoverable'),
    ({'reason': 'active_pairing_recent'}, 'active_pairing_recent'),
    ({'reason': 'other_account', 'owner_hint': 'raw@example.test', 'recovery': 'reset_as_new'}, None),
    ({'reason': 'predecessor_unrecoverable', 'owner_hint': '***', 'recovery': 'reset_as_new'}, None),
    ({'reason': 'active_pairing_recent', 'recovery': 'reset_as_new'}, None),
])
def test_v2_observation_needs_return_and_exact_refusal(issuer, fields, outcome):
    peer, flags = issuer
    flags['predecessor_constrained'] = True
    peer.code = 'RB2-ABCD234567'
    client = ConstrainedClaimsClient('https://avibe.bot', 'original', 'synthetic-key')
    ticket = start(client)
    assert peer.calls[0][1]['predecessor'] == {'instance_id': 'original', 'instance_secret': 'synthetic-key'}
    with pytest.raises(ClaimError, match='invalid_return'):
        client.observe(ticket)
    assert len(peer.calls) == 1
    with pytest.raises(ClaimError, match='invalid_return'):
        client.confirm_return(ticket, 'A' * 42 + 'B')
    client.confirm_return(ticket, 'A' * 43)
    peer.outcomes.append((409, {'error': 'installation_unverified', **fields}, None))
    with pytest.raises(ClaimError) as caught:
        client.observe(ticket)
    assert getattr(caught.value, 'reason', None) == outcome
    assert peer.calls[-1][1] == {'claim_id': ticket.id, 'code_verifier': client._verifier,
                                 'predecessor_constrained': True, 'callback_token': 'A' * 43}


@pytest.mark.parametrize(('ack', 'code'), [(None, 'RB2-ABCD234567'), (1, 'RB2-ABCD234567'), (True, 'ABCD234567')])
def test_v2_never_retries_as_v1(issuer, ack, code):
    peer, flags = issuer
    flags['predecessor_constrained'] = ack
    peer.code = code
    client = ConstrainedClaimsClient('https://avibe.bot', 'original', 'synthetic-key')
    with pytest.raises(ClaimError):
        start(client, origin='http://10.0.0.1')
    assert peer.calls == []
    with pytest.raises(ClaimError):
        start(client)
    assert len(peer.calls) == 1 and 'predecessor' in peer.calls[0][1]

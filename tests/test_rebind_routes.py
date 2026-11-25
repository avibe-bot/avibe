"""Loopback API against a synthetic issuer and a temporary Runtime home."""
import pytest

from config.v2_config import V2Config
from tests.test_claim_wire_rebind import issuer as issuer
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from tests.test_personal_claims import callback_proof, claim_peer as claim_peer
from tests.ui_server_test_helpers import csrf_headers
from vibe import personal_claims as claims, remote_access, ui_server

ROOT = '/api/remote-access/rebind/claims'
ORIGIN = 'http://localhost'


@pytest.fixture
def machine(issuer, monkeypatch):
    peer, flags = issuer
    assert remote_access.pair('key_A', 'https://avibe.bot')['ok']
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.enabled = False
    cfg.save()
    flags['predecessor_constrained'] = True
    peer.code, peer.key = 'RB2-ABCD234567', 'vrp2_' + 'A' * 43
    previous = peer.request

    def issue(method, path, body, headers):
        previous(method, path, body, headers)
        if peer.result[1].get('status') == 'redeemed':
            peer.result[1]['binding_disposition'] = 'existing'

    redeemed = []

    def pair(url, payload, **kwargs):
        redeemed.append(dict(payload))
        return {**_response(), 'instance_secret': 'fixture-rotated', 'binding_disposition': 'existing'}

    monkeypatch.setattr(peer, 'request', issue)
    monkeypatch.setattr(claims, 'rebind_claims', claims.RebindClaims())
    monkeypatch.setattr(remote_access, '_json_request', pair)
    monkeypatch.setattr(ui_server, '_ensure_remote_access_monitoring', lambda: None)
    return peer, redeemed


def test_admission_is_prior_to_issuer(machine):
    peer, redeemed = machine
    browser = ui_server.app.test_client()
    hdr = csrf_headers(browser)
    status = browser.get('/api/remote-access/rebind', base_url=ORIGIN)
    assert status.status_code == 200
    assert status.get_json() == {'binding_state': 'bound', 'connector_still_running': False, 'can_start': True,
                                 'unavailable_reason': None, 'attempt': None, 'next_action': 'rebind', 'reason_code': None}
    from vibe import desktop_local_instance as local
    assert not local._identity_path().exists() and not peer.calls
    for extra in ({'X-Avibe-Delegated-Caller': '1'}, {'Origin': 'null'}, {'X-Forwarded-For': '127.0.0.1'},
                  {'X-Vibe-CSRF-Token': 'wrong'}):
        resp = browser.post(ROOT, base_url=ORIGIN, headers={**hdr, **extra}, json={})
        assert resp.status_code == 403, (extra, resp.get_json())
    assert not peer.calls and not redeemed


@pytest.mark.parametrize('reason', [None, 'predecessor_unrecoverable', 'other_account', 'active_pairing_recent', 'malformed'])
def test_rebind_through_shared_callback(machine, reason):
    peer, redeemed = machine
    browser = ui_server.app.test_client()
    hdr = {**csrf_headers(browser), 'Referer': ORIGIN + '/show/fixture'}
    resp = browser.post(ROOT, json={'locale': 'zh'}, headers=hdr)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    ident = body['claim_id']
    assert peer.calls[0][1]['predecessor']['instance_id'] == 'inst_A'
    assert 'avibe_rebind_browser' in resp.headers['Set-Cookie']
    assert browser.get('/api/remote-access/claims/' + ident).status_code == 404
    assert browser.post('/api/remote-access/claims/' + ident + '/cancel', json={}, headers=hdr).status_code == 404
    url = '/remote/claim/callback?claim_id=' + ident + '&state=' + callback_proof(body)
    assert browser.get(url, base_url=ORIGIN).headers['Location'].endswith('claim_error=invalid_return')
    assert browser.get('/api/remote-access/rebind', base_url=ORIGIN).get_json()['can_start'] is False
    assert len(peer.calls) == 1
    if reason:
        refusal = {'error': 'installation_unverified', 'reason': reason}
        if reason != 'active_pairing_recent':
            refusal['recovery'] = 'reset_as_new'
        if reason == 'other_account':
            refusal['owner_hint'] = 'a***@b***'
        peer.outcomes.append((409, refusal, None))
    assert browser.get(url + '&callback_token=' + 'A' * 43, base_url=ORIGIN).headers['Location'] == '/settings/remote-access'
    flow = claims.rebind_claims._get(ident)
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view['state'] in ('bound', 'error'), timeout=5)
    result = browser.get(ROOT + '/' + ident)
    assert result.headers['Cache-Control'] == 'no-store'
    view = result.get_json()
    cloud = V2Config.load().remote_access.vibe_cloud
    assert not cloud.enabled
    if reason:
        assert view['code'] == 'installation_unverified'
        assert view.get('reason_code') == (None if reason == 'malformed' else reason)
        assert not redeemed and cloud.instance_secret == 'fixture-secret-inst_A'
        path = ROOT + '/' + ident + '/reset'
        preview = browser.post(path + '/preview', base_url=ORIGIN, headers=hdr, json={})
        if reason in ('other_account', 'predecessor_unrecoverable'):
            assert preview.status_code == 200, preview.get_json()
            assert set(preview.get_json()) == {'confirmation_id', 'expires_at', 'health_connected'}
            confirmation = {'confirmation_id': preview.get_json()['confirmation_id']}
            stranger = ui_server.app.test_client()
            denied = stranger.post(path, base_url=ORIGIN, headers=csrf_headers(stranger), json=confirmation)
            assert denied.status_code == 409 and denied.get_json()['code'] == 'reset_not_eligible'
            assert browser.post(path, base_url=ORIGIN, headers=hdr, json={'confirmation_id': None}).status_code == 400
        else:
            assert preview.status_code == 409 and preview.get_json()['code'] == 'reset_not_eligible'
        if reason != 'malformed':
            cfg = V2Config.load()
            cfg.remote_access.vibe_cloud.instance_secret = 'fixture-replaced'
            cfg.save()
            assert browser.get(ROOT + '/' + ident).get_json() == {'state': 'error', 'code': 'local_binding_changed'}
            if reason in ('other_account', 'predecessor_unrecoverable'):
                stale = browser.post(path, base_url=ORIGIN, headers=hdr, json=confirmation)
                assert stale.status_code == 409 and stale.get_json()['code'] == 'local_binding_changed'
                assert V2Config.load().remote_access.vibe_cloud.instance_secret == 'fixture-replaced'
    else:
        assert view['state'] == 'bound' and view['binding_disposition'] == 'existing', view
        assert len(redeemed) == 1 and redeemed[0]['redeem_request_id']
        assert cloud.instance_secret == 'fixture-rotated'


@pytest.mark.parametrize('finish', ['confirm', 'cancel', 'expired', 'cloud_then_local'])
def test_reset_confirmation_lifecycle(machine, monkeypatch, finish):
    from vibe import binding_admission, desktop_local_instance as local, direct_certificates

    peer, redeems = machine
    monkeypatch.setattr(remote_access, 'stop', lambda _: {'ok': True})
    monkeypatch.setattr(remote_access, '_stop_direct_lifecycle_before_pairing_discard', lambda _: None)
    monkeypatch.setattr(direct_certificates, 'discard_direct_certificates', lambda: None)
    browser = ui_server.app.test_client()
    hdr = csrf_headers(browser)
    def post(path, body):
        return browser.post(path, base_url=ORIGIN, headers=hdr, json=body)
    started = post(ROOT, {}).get_json()
    ident = started['claim_id']
    scope = ROOT + '/' + ident
    path = scope + '/reset'
    assert post(path + '/preview', {}).status_code == 409
    peer.outcomes.append((409, {'error': 'installation_unverified', 'recovery': 'reset_as_new',
                                'reason': 'predecessor_unrecoverable'}, None))
    callback = '/remote/claim/callback?claim_id=' + ident + '&state=' + callback_proof(started) + '&callback_token=' + 'A' * 43
    assert browser.get(callback, base_url=ORIGIN).headers['Location'] == '/settings/remote-access'
    flow = claims.rebind_claims._get(ident)
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view['state'] == 'error', timeout=5)
    assert browser.get('/api/remote-access/rebind', base_url=ORIGIN).get_json()['next_action'] == 'confirm_as_new'
    old, fresh = (post(path + '/preview', {}).get_json()['confirmation_id'] for _ in range(2))
    assert old != fresh and post(path, {'confirmation_id': old}).status_code == 409
    assert not binding_admission.reset_in_progress() and local._installation_id()
    if finish == 'cloud_then_local':
        from vibe import binding_unbind
        sends = []
        monkeypatch.setattr(binding_unbind, 'send_original', lambda cfg, operation, **kw:
                            sends.append(operation) or 'invalid_credential')
        cloud = path + '/unbind'
        fallback = path + '/local-after-refusal'
        preview = post(cloud + '/preview', {})
        assert preview.status_code == 200, preview.get_json()
        cloud_token = {'confirmation_id': preview.get_json()['confirmation_id']}
        assert post(cloud, {'confirmation_id': fresh}).status_code == 409
        assert post(path, cloud_token).status_code == 409
        assert post(fallback + '/preview', {}).status_code == 409
        assert post(cloud, cloud_token).status_code == 202 and not sends
        assert remote_access._drive_pending_pairing()[0] and len(sends) == 1
        assert local._installation_id() and V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
        second = post(fallback + '/preview', {})
        assert second.status_code == 200, second.get_json()
        token = {'confirmation_id': second.get_json()['confirmation_id']}
        assert post(fallback, cloud_token).status_code == 409
        assert post(fallback, token).get_json() == {'state': 'unpaired'}
        assert post(fallback, token).status_code == 409
        assert not local._installation_id() and len(sends) == 1 and not redeems
        return
    if finish == 'cancel':
        assert post(scope + '/cancel', {}).status_code == 200
    elif finish == 'expired':
        flow.reset_expiry = 0
    result = post(path, {'confirmation_id': fresh})
    if finish == 'confirm':
        assert result.status_code == 200 and result.get_json() == {'state': 'unpaired'}
        assert local._installation_id() is None and not V2Config.load().remote_access.vibe_cloud.instance_id
        assert post(path, {'confirmation_id': fresh}).status_code == 409
    else:
        assert result.status_code == 409 and V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
        assert local._installation_id()
    assert len(peer.calls) == 2 and not redeems and not binding_admission.reset_in_progress()


@pytest.mark.parametrize('decline', [(200, {'status': 'declined'}), (400, {'error': 'invalid_request'}),
                                     (409, {'error': 'installation_unverified'})])
def test_cancel_before_return_reads_as_cancelled(machine, decline):
    peer, redeemed = machine
    browser = ui_server.app.test_client()
    hdr = csrf_headers(browser)
    ident = browser.post(ROOT, base_url=ORIGIN, headers=hdr, json={}).get_json()['claim_id']
    peer.outcomes.append((*decline, None))
    assert browser.post(ROOT + '/' + ident + '/cancel', base_url=ORIGIN, headers=hdr, json={}).status_code == 200
    flow = claims.rebind_claims._get(ident)
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view['state'] not in ('pending', 'cancelling'), timeout=5)
    # Whatever avibe.bot answers to the best-effort decline, the person only pressed Cancel.
    view = browser.get(ROOT + '/' + ident, base_url=ORIGIN).get_json()
    assert (view['state'], view['code']) == ('declined', 'declined')
    assert sum('decline' in body for _, body in peer.calls) == 1 and not redeemed


def test_cancel_at_avibe_bot_returns_here_as_cancelled(machine):
    peer, redeemed = machine
    browser = ui_server.app.test_client()
    hdr = csrf_headers(browser)
    body = browser.post(ROOT, base_url=ORIGIN, headers=hdr, json={}).get_json()
    assert peer.calls[0][1]['cancel_return'] is True and 'predecessor' in peer.calls[0][1]
    ident = body['claim_id']
    base = '/remote/claim/callback?claim_id=' + ident + '&state=' + callback_proof(body)
    calls = len(peer.calls)
    for bad in ('&outcome=declined&callback_token=' + 'A' * 43, '&outcome=refused', '&outcome=declined&outcome=declined'):
        assert browser.get(base + bad, base_url=ORIGIN).headers['Location'].endswith('claim_error=invalid_return')
    forged = '/remote/claim/callback?claim_id=' + ident + '&state=wrong&outcome=declined'
    assert browser.get(forged, base_url=ORIGIN).headers['Location'].endswith('claim_error=invalid_return')
    flow = claims.rebind_claims._get(ident)
    assert flow.view['state'] == 'pending'
    assert browser.get(base + '&outcome=declined', base_url=ORIGIN).headers['Location'] == '/settings/remote-access'
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view['state'] == 'declined', timeout=5)
    view = browser.get(ROOT + '/' + ident, base_url=ORIGIN).get_json()
    assert (view['state'], view['code']) == ('declined', 'declined')
    # Declined at avibe.bot already: nothing more is sent or redeemed, and a late return is refused.
    assert len(peer.calls) == calls and not redeemed
    late = browser.get(base + '&callback_token=' + 'A' * 43, base_url=ORIGIN)
    assert late.headers['Location'].endswith('claim_error=invalid_return')

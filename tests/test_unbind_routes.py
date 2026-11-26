"""The owner confirms a machine generation, never an instance supplied by a browser."""
from tests.test_claim_wire_rebind import issuer as issuer
from tests.test_rebind_routes import claim_peer as claim_peer, machine as machine, pairing_host as pairing_host
from tests.test_instance_device_registry import ORIGIN as HOSTED_ORIGIN, client, install as install, login
from tests.ui_server_test_helpers import csrf_headers
from vibe import binding_admission as gate, binding_reset, remote_access, ui_server


ROOT = '/api/remote-access/unbind'


def test_remote_confirmation_is_private_to_owner_session(install, monkeypatch):
    install.remote_access.vibe_cloud.instance_secret = 'synthetic-device-credential'
    install.save()
    first = client(login(install, 'owner', 'owner'))
    second = client(login(install, 'owner', 'owner'))
    member = client(login(install, 'member', 'member'))
    def post(browser, action, body):
        return browser.post(ROOT + '/' + action, base_url=HOSTED_ORIGIN,
                            headers=csrf_headers(browser, HOSTED_ORIGIN), json=body)
    assert post(member, 'preview', {}).status_code == 403
    preview = post(first, 'preview', {})
    assert preview.status_code == 200, preview.get_json()
    body = {'confirmation_id': preview.get_json()['confirmation_id']}
    assert post(second, 'commit', body).status_code == 409
    assert not gate.reset_in_progress()
    monkeypatch.setattr(remote_access, 'stop', lambda cfg: {'ok': True})
    monkeypatch.setattr(remote_access, '_stop_direct_lifecycle_before_pairing_discard', lambda cfg: None)
    response = post(first, 'commit', body)
    assert response.status_code == 202, response.get_json()
    assert gate.reset_in_progress()


def test_owner_and_origin_rechecked_at_commit(machine, monkeypatch):
    browser = ui_server.app.test_client()
    headers = csrf_headers(browser)
    preview = browser.post(ROOT + '/preview', json={}, headers=headers)
    assert preview.status_code == 200
    body = {'confirmation_id': preview.get_json()['confirmation_id']}
    for extra in ({'Origin': 'null'}, {ui_server.CSRF_HEADER_NAME: 'wrong'}, {'X-Avibe-Delegated-Caller': '1'}):
        denied = browser.post(ROOT + '/commit', json=body, headers={**headers, **extra})
        # A foreign Origin may be stopped by the host gate before the unbind handler.
        assert (denied.status_code == 403 or (extra == {'Origin': 'null'} and denied.status_code == 503
                and denied.get_json().get('error') == 'remote_access_host_mismatch')), denied.get_json()
        assert not gate.reset_in_progress()
    with monkeypatch.context() as patch:
        patch.setattr(ui_server, '_device_viewer', lambda: ('test-binding', 'member', False, 'session'))
        assert browser.post(ROOT + '/commit', json=body, headers=headers).status_code == 403
    assert not gate.reset_in_progress()


def test_owner_confirmation_seals_before_stop_and_does_not_send(machine, monkeypatch):
    from vibe import binding_unbind
    browser = ui_server.app.test_client()
    hdr = csrf_headers(browser)
    seen = []
    monkeypatch.setattr(remote_access, 'stop', lambda cfg: seen.append(binding_reset._read()['phase']) or {'ok': True})
    monkeypatch.setattr(binding_unbind, 'send_original', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('inline send')))
    reply = browser.post(ROOT + '/preview', headers=hdr, json={})
    assert reply.status_code == 200, reply.get_json()
    confirmation = reply.get_json()['confirmation_id']
    assert not gate.reset_in_progress() and seen == []
    reply = browser.post(ROOT + '/commit', headers=hdr, json={'confirmation_id': confirmation})
    assert reply.status_code == 202 and reply.get_json() == {'state': 'unbinding'}
    assert seen == ['cloud'] and gate.reset_in_progress()
    devices = browser.get('/api/devices')
    assert devices.status_code == 200 and devices.get_json()['unbind'] == {'state': 'unbinding'}
    # Readback is local-only and must not advance the journal.
    assert binding_reset._read()['phase'] == 'cloud' and seen == ['cloud']
    assert browser.post(ROOT + '/commit', headers=hdr, json={'confirmation_id': confirmation}).status_code == 409
    assert seen == ['cloud']


def test_a_failed_stop_after_sealing_is_still_accepted_and_sends_nothing(machine, monkeypatch):
    from vibe import binding_unbind
    browser = ui_server.app.test_client()
    hdr = csrf_headers(browser)
    monkeypatch.setattr(remote_access, 'stop', lambda cfg: {'ok': False})
    monkeypatch.setattr(binding_unbind, 'send_original', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('sent')))
    confirmation = browser.post(ROOT + '/preview', headers=hdr, json={}).get_json()['confirmation_id']
    reply = browser.post(ROOT + '/commit', headers=hdr, json={'confirmation_id': confirmation})
    assert reply.status_code == 202 and gate.reset_in_progress()
    # The worker retries the stop and does not send while the connector may still run.
    assert remote_access._drive_pending_pairing()[0] and binding_reset._read()['phase'] == 'cloud'

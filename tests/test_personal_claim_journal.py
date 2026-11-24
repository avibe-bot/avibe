"""Claims meet the already-shipped pending-pair journal at these recovery boundaries."""

import pytest

from config.v2_config import V2Config
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from tests.test_personal_claims import claim_peer as claim_peer, service_adoptions as service_adoptions
from vibe import personal_claims as claims, remote_access


def _claim(subject="owner-id"):
    return claims.run_link(expected_subject=subject, show_link=lambda _: None)


@pytest.mark.parametrize('bad_receipt', [{}, {'binding_disposition': 'new'}, {'binding_disposition': True}])
def test_constrained_pair_replays_same_request_until_receipt_matches(pairing_host, monkeypatch, bad_receipt):
    from uuid import uuid4
    from vibe import desktop_local_instance as local

    assert remote_access.pair('key_A', 'https://backend.test')['ok']
    cfg = V2Config.load()
    installation = local.ensure_installation_id()
    key = 'vrp2_' + 'A' * 43
    claim_id = str(uuid4())
    requests = []

    def redeem(_url, payload, **_kw):
        requests.append(dict(payload))
        receipt = bad_receipt if len(requests) == 1 else {'binding_disposition': 'existing'}
        return {**_response('inst_B'), **receipt}

    monkeypatch.setattr(remote_access, '_json_request', redeem)
    assert not remote_access.pair(key, 'https://backend.test')['ok']
    assert not requests
    result = remote_access.pair_personal_claim(
        key=key, backend_url='https://backend.test', device='fixture',
        revision=local.claim_revision(cfg, installation, None), installation=installation,
        subject='owner-id', claim_id=claim_id, instance_id='inst_B', hostname='fixture.avibe.bot',
        binding_disposition='existing',
    )
    assert result['error'] == 'pairing_redeem_indeterminate'
    assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
    journal, error = remote_access._read_pending_pairing_record()
    assert not error and journal['phase'] == 'prepared'
    assert journal['personal_claim']['binding_disposition'] == 'existing'
    if bad_receipt:
        recovered = remote_access._retry_installation_pairing(journal)
        assert recovered['ok'] and recovered['binding_disposition'] == 'existing'
    else:
        # A new UI process starts with only this journal. Its first tick must not
        # interpret the now invalid source credential as a detector observation.
        workers, probes = [], []

        class Worker:
            def __init__(self, *, target, **_):
                self.target = target

            def start(self):
                workers.append(self.target)

        with monkeypatch.context() as patch:
            patch.setattr(remote_access, '_AUTHORIZATION_REVISION_POLL_STARTED', False)
            patch.setattr(remote_access.threading, 'Thread', Worker)
            remote_access.start_authorization_revision_polling()
        assert len(workers) == 1

        class EndTick(Exception):
            pass

        def stop_tick(delay):
            assert delay > 0
            raise EndTick

        monkeypatch.setattr(remote_access.time, 'sleep', stop_tick)
        monkeypatch.setattr(remote_access, 'sync_authorization_revision_once', lambda **kw: probes.append(kw['monitor']))
        with pytest.raises(EndTick):
            workers[0]()
        assert probes == [None]  # refresh the live binding, but no rejection evidence
    assert requests[0] == requests[1] and requests[0]['redeem_request_id']
    assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_B'
    assert 'bound' not in claims._outcome()
    assert claims._outcome(constrained=True)['bound']['binding_disposition'] == 'existing'
    assert claims.RebindClaims().state(claim_id) == {'state': 'bound', 'binding_disposition': 'existing'}
    with pytest.raises(claims.ClaimError, match='claim_unavailable'):
        claims.RedirectClaims().state(claim_id)
    assert remote_access._read_pending_pairing_record() == (None, None)


@pytest.mark.parametrize('kind', ['existing', 'new'])
def test_desktop_disposition_checks_target_but_never_selects_rebind_journal(claim_peer, monkeypatch, kind):
    from tests.test_personal_claims import _desktop_claim

    peer = claim_peer
    peer.start_fields['ordinary_decision_readback'] = True
    request = peer.request

    def issue(*args):
        request(*args)
        if peer.result[1].get('status') == 'redeemed':
            peer.result[1]['binding_disposition'] = kind

    monkeypatch.setattr(peer, 'request', issue)
    sealed = []

    def redeem(*args, **kwargs):
        journal, error = remote_access._read_pending_pairing_record()
        assert not error and journal['phase'] == 'prepared'
        sealed.append(journal['personal_claim'])
        return _response()  # Ordinary pairing has no disposition at all.

    monkeypatch.setattr(remote_access, '_json_request', redeem)

    def review(ticket):
        assert ticket['ordinary_decision_readback'] is True
        flow.arm(claim_id=ticket['claim_id'], kind=kind, hostname=peer.hostname,
                 existing_instance_id='inst_A' if kind == 'existing' else None)

    flow = _desktop_claim(signed_in_subject=peer.account['subject'], publish_ticket=review)
    assert flow.run()['state'] == 'bound'
    assert sum(payload.get('read_decision') is True for _, payload in peer.calls) == 1
    assert len(sealed) == 1 and 'binding_disposition' not in sealed[0]
    assert set(claims._outcome()['bound']) == {'claim_id', 'generation'}
    assert not claims._marker(True).exists()


@pytest.mark.parametrize('kind,old_marker,resume', [
    ('existing', False, False), ('new', False, False), ('existing', True, False), ('new', True, False),
    ('existing', True, True), ('new', True, True),
])
def test_web_rebind_notifies_service_for_its_own_publication(claim_peer, monkeypatch, kind, old_marker, resume):
    from tests.test_pairing_recovery import _fail_config_publication_once
    from tests.test_personal_claims import callback_proof
    from tests.ui_server_test_helpers import csrf_headers
    from vibe import ui_server

    if old_marker:
        assert _claim()['state'] == 'bound'  # Real prior ordinary generation.
    else:
        assert remote_access.pair('key_A', 'https://avibe.bot')['ok']
    ordinary = claims._outcome()
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.enabled = False
    cfg.save()
    peer = claim_peer
    target = 'inst_A' if kind == 'existing' else 'inst_B'
    peer.code, peer.key = 'RB2-ABCD234567', 'vrp2_' + 'A' * 43
    peer.start_fields.update(predecessor_constrained=True, ordinary_decision_readback=True)
    request = peer.request

    def issue(*args):
        request(*args)
        if peer.result[1].get('status') == 'redeemed':
            peer.result[1]['binding_disposition'] = kind
            peer.result[1]['instance']['id'] = target

    sends, activations = [], []

    def redeem(_url, payload, **kw):
        sends.append(payload['redeem_request_id'])
        return {**_response(target), 'instance_secret': 'fixture-rotated', 'binding_disposition': kind}

    def activate(*, binding_changed=False):
        cloud = V2Config.load().remote_access.vibe_cloud
        activations.append((binding_changed, cloud.instance_id, cloud.enabled))
        return True

    monkeypatch.setattr(claims, 'rebind_claims', claims.RebindClaims())
    monkeypatch.setattr(peer, 'request', issue)
    monkeypatch.setattr(remote_access, '_json_request', redeem)
    monkeypatch.setattr(ui_server, '_ensure_remote_access_monitoring', activate)
    browser = ui_server.app.test_client()
    headers = csrf_headers(browser)
    started = browser.post('/api/remote-access/rebind/claims', headers=headers, json={})
    assert started.status_code == 201
    body = started.get_json()
    flow = claims.rebind_claims._get(body['claim_id'])
    assert not flow.ticket.ordinary_decision_readback  # Extra ordinary ack on an RB2 ticket is inert.
    if resume:
        _fail_config_publication_once(monkeypatch)
    callback = '/remote/claim/callback?claim_id=' + body['claim_id'] + '&state=' + callback_proof(body)
    assert browser.get(callback + '&callback_token=' + 'A' * 43, base_url='http://localhost').headers['Location'] == '/settings/remote-access'
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view['state'] in ('bound', 'error'), timeout=5)
    if resume:
        assert flow.view['code'] == 'pairing_recovery_required' and not activations
        assert remote_access._read_pending_pairing_record()[0]['phase'] == 'redeemed'
        done = browser.post('/api/remote-access/vibe-cloud/pair', headers=headers, json={})
        assert done.status_code == 200 and done.get_json()['binding_disposition'] == kind
    else:
        assert flow.view['state'] == 'bound'
    assert len(sends) == 1 and activations == [(True, target, False)]
    assert claims._outcome() == ordinary and claims._outcome(True)['bound']['binding_disposition'] == kind
    assert V2Config.load().remote_access.vibe_cloud.instance_secret == 'fixture-rotated'


def test_refused_rebind_retry_cannot_begin_legacy_desktop_dispatch(pairing_host, monkeypatch):
    from tests.test_binding_recovery import _attempt
    from vibe import desktop_local_instance as local

    assert remote_access.pair('key_A', 'https://backend.test')['ok']
    requests = []

    def refused(_url, payload, **kw):
        requests.append(payload['redeem_request_id'])
        raise remote_access.BackendRequestError(409, {'error': 'registration_indeterminate'})

    monkeypatch.setattr(remote_access, '_json_request', refused)
    monkeypatch.setattr(remote_access.desktop_redeem_evidence, 'begin', lambda *_: pytest.fail('legacy dispatch'))
    assert _attempt(V2Config.load(), local.ensure_installation_id())['error'] == 'issuer_replay_refused'
    pending, error = remote_access._read_pending_pairing_record()
    assert not error and remote_access._issuer_replay_refused(pending)
    assert remote_access._retry_installation_pairing(pending)['error'] == 'issuer_replay_refused'
    assert requests == [pending['redeem_request']['redeem_request_id']]
    assert remote_access._read_pending_pairing_record() == (pending, None)


def test_off_binding_is_not_repaired_or_replaced(claim_peer):
    assert _claim()["state"] == "bound"
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.enabled = False
    cfg.save()
    before = len(claim_peer.calls)
    assert _claim()["code"] == "already_bound"
    assert not V2Config.load().remote_access.vibe_cloud.enabled
    assert len(claim_peer.calls) == before


def test_legacy_prepared_survives_refusal_then_can_be_superseded_by_matching_accept(claim_peer):
    record = remote_access._new_pairing_claim(V2Config.load(), "https://avibe.bot", "original", pairing_key="fixture-old")
    remote_access._write_pending_pairing_record(record)
    assert remote_access._drive_pending_pairing() == (True, 60)
    assert not claim_peer.calls
    assert _claim("different")["state"] == "declined"
    assert remote_access._read_pending_pairing_record() == (record, None)
    assert _claim()["state"] == "bound"
    assert remote_access._read_pending_pairing_record() == (None, None)
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


@pytest.mark.parametrize("status,replace", [(422, True), (400, True), (429, False), (408, False), (503, False)])
def test_fresh_owner_can_exit_rejected_request_but_not_transient_failure(claim_peer, monkeypatch, status, replace):
    def reject(*_args, **_kwargs):
        raise remote_access.BackendRequestError(status, {"error": "fixture-private-backend-reason"})
    monkeypatch.setattr(remote_access, "_json_request", reject)
    assert _claim()["code"] == "pairing_recovery_required"
    original = remote_access._read_pending_pairing_record()
    refused = _claim("wrong")
    assert refused["state"] == ("declined" if replace else "error")
    assert original == remote_access._read_pending_pairing_record()
    assert len(claim_peer.challenges) == (2 if replace else 1)
    if replace:
        requests = []
        def redeem(_url, request, **_kw):
            requests.append(request["redeem_request_id"])
            if len(requests) == 1:
                reject()
            return _response()
        monkeypatch.setattr(remote_access, "_json_request", redeem)
        assert _claim()["state"] == "bound"
        assert requests[0] == original[0]["redeem_request"]["redeem_request_id"] != requests[1]


def test_missing_installation_id_uses_its_journal_identity_not_a_new_uuid(claim_peer, monkeypatch):
    from vibe import desktop_local_instance as local
    with monkeypatch.context() as patch:
        def unavailable(*_, **__):
            raise OSError
        patch.setattr(remote_access, "_json_request", unavailable)
        assert _claim()["code"] == "pairing_recovery_required"
    installed = local._installation_id()
    local._identity_path().unlink()
    assert _claim()["code"] == "already_bound"
    assert local._installation_id() == installed
    assert len(claim_peer.challenges) == 1


def test_changed_source_only_supersedes_on_owner_accept(claim_peer, monkeypatch):
    with monkeypatch.context() as patch:
        def unavailable(*_, **__):
            raise OSError
        patch.setattr(remote_access, "_json_request", unavailable)
        assert _claim()["code"] == "pairing_recovery_required"
    original = remote_access._read_pending_pairing_record()
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.enabled = not cfg.remote_access.vibe_cloud.enabled
    cfg.save()
    assert _claim("wrong")["state"] == "declined"
    assert remote_access._read_pending_pairing_record() == original
    assert _claim()["state"] == "bound"
    assert len(claim_peer.challenges) == 3


@pytest.mark.parametrize("via", ["legacy", "fresh_start", "explicit_recovery", "startup"])
def test_durable_status_after_other_caller_finishes_sealed_journal(claim_peer, monkeypatch, service_adoptions, via):
    from tests.test_pairing_recovery import _fail_config_publication_once
    from tests.test_personal_claims import _start
    from vibe import ui_server
    _fail_config_publication_once(monkeypatch)
    assert _claim()["code"] == "pairing_recovery_required"
    ident = claim_peer.id
    assert remote_access._read_pending_pairing_record()[0]["phase"] == "redeemed"
    client = ui_server.app.test_client()
    route = "/api/remote-access/claims/" + ident
    assert client.get(route).get_json()["code"] == "pairing_recovery_required"
    if via == "legacy":
        assert remote_access.pair("", "https://avibe.bot")["ok"]
    elif via == "fresh_start":
        assert _start(client).get_json()["code"] == "already_bound"
        assert service_adoptions == ["inst_A"]
    elif via == "startup":
        monkeypatch.setattr(remote_access, '_json_request', lambda *a, **kw: pytest.fail('sealed response was already redeemed'))
        assert remote_access._drive_pending_pairing() == (True, 1)
    else:
        assert claims.recover_pairing(ident)["state"] == "bound"
    assert len(claim_peer.challenges) == 1 and client.get(route).get_json() == {"state": "bound"}
    config = V2Config.load()
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    assert client.get(route).get_json() == {"state": "bound"}
    config.remote_access.vibe_cloud.instance_secret = "different-binding"
    config.save()
    assert client.get(route).get_json()["code"] == "local_binding_changed"


def test_live_redirect_status_observes_legacy_resume(claim_peer, monkeypatch):
    from tests.test_pairing_recovery import _fail_config_publication_once
    from tests.test_personal_claims import _start, callback_proof
    from vibe import ui_server
    _fail_config_publication_once(monkeypatch)
    client = ui_server.app.test_client()
    start = _start(client).get_json()
    ident = start["claim_id"]
    ret = client.get(f"/remote/claim/callback?claim_id={ident}&state={callback_proof(start)}", base_url="http://localhost")
    assert ret.status_code == 303
    flow = claims.redirect_claims._get(ident)
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view["state"] == "error", timeout=3)
    assert flow.view["code"] == "pairing_recovery_required"
    assert remote_access.pair("", "https://avibe.bot")["ok"]
    assert client.get("/api/remote-access/claims/" + ident).get_json() == {"state": "bound"}


def test_bad_journal_does_not_turn_status_into_http_500(claim_peer):
    from vibe import ui_server
    path = remote_access._pending_pairing_path()
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text("invalid journal")
    response = ui_server.app.test_client().get("/api/remote-access/claims/unknown")
    assert response.status_code == 503
    assert response.get_json() == {"code": "local_enrollment_unavailable", "state": "error"}
    assert not claim_peer.calls


@pytest.mark.parametrize("replay,expected", [("complete", "already_bound"), ("pending", "pairing_recovery_required"),
                                            ("transport", "pairing_recovery_required"), ("changed", "bound")])
def test_fresh_start_resumes_journal_before_conditional_replacement(claim_peer, monkeypatch, replay, expected):
    calls = []
    def redeem(_url, request, **kwargs):
        calls.append(dict(request))
        if len(calls) == 1:
            raise OSError("fixture lost pairing response")
        if len(calls) == 2:
            if replay == "pending":
                raise remote_access.BackendRequestError(409, {"error": "pairing_redeem_pending", "retry_after_seconds": 1})
            if replay == "transport":
                raise OSError("fixture still unavailable")
            if replay == "changed":
                raise remote_access.BackendRequestError(409, {"error": "registration_indeterminate"})
        return _response()
    monkeypatch.setattr(remote_access, "_json_request", redeem)
    assert _claim()["code"] == "pairing_recovery_required"
    original, _ = remote_access._read_pending_pairing_record()
    assert original["redeem_request"] == calls[0]
    if replay == "changed":
        claim_peer.key = "fixture-new-grant-key"
    result = _claim()
    assert result.get("code", result["state"]) == expected
    assert calls[1] == calls[0] and calls[0]["redeem_request_id"]
    if replay in ("pending", "transport"):
        assert remote_access._read_pending_pairing_record() == (original, None)
        assert len(claim_peer.challenges) == 1
    elif replay == "changed":
        assert len(calls) == 3 and calls[2]["redeem_request_id"] != calls[0]["redeem_request_id"]
        assert len(claim_peer.challenges) == 2
    else:
        assert len(claim_peer.challenges) == 1


def test_changed_local_binding_during_decision_declines_grant(claim_peer):
    def confirm(_email, _hostname):
        cfg = V2Config.load()
        cfg.remote_access.vibe_cloud.instance_id = "a-different-operation"
        cfg.save()
        return True
    result = claims.run_link(show_link=lambda _: None, confirm=confirm)
    assert result["code"] == "local_binding_changed" and claim_peer.decisions == ["decline"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "a-different-operation"


def test_resume_does_not_forward_credential_bearing_errors(claim_peer, monkeypatch):
    def fail_redeem(_url, request, **_kw):
        raise remote_access.BackendRequestError(503, {"error": request["pairing_key"], "detail": dict(request)})
    monkeypatch.setattr(remote_access, "_json_request", fail_redeem)
    assert _claim()["code"] == "pairing_recovery_required"
    public_result = remote_access.pair("", "https://avibe.bot")
    assert public_result["error"] == "pairing_redeem_indeterminate"
    assert public_result["pairing"]["cause"] == "pairing_request_failed"
    assert claim_peer.key not in str(public_result) and "redeem_request_id" not in str(public_result)


def test_pairing_cannot_publish_a_target_different_from_the_approved_claim(claim_peer, monkeypatch):
    monkeypatch.setattr(remote_access, "_json_request", lambda *_a, **_kw: _response("other-instance"))
    result = _claim()
    assert result["code"] == "fresh_claim_required"
    assert not V2Config.load().remote_access.vibe_cloud.instance_id


@pytest.mark.parametrize("damage", ["{not json", "[]", '{"bound": 5}', b"\xff\xfe"])
def test_damaged_status_file_blocks_neither_a_new_claim_nor_its_sealed_pairing(claim_peer, monkeypatch, damage):
    """The outcome file holds no secret: unparseable reads as absent, and the next write replaces it."""
    import json
    from tests.test_pairing_recovery import _fail_config_publication_once
    marker = claims._marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    def damage_marker():
        marker.write_bytes(damage if isinstance(damage, bytes) else damage.encode())
    damage_marker()
    _fail_config_publication_once(monkeypatch)
    assert _claim()["code"] == "pairing_recovery_required"
    ident = claim_peer.id
    assert remote_access._read_pending_pairing_record()[0]["phase"] == "redeemed"
    damage_marker()
    assert claims.recover_pairing(ident)["state"] == "bound"
    assert remote_access._read_pending_pairing_record() == (None, None)
    assert json.loads(marker.read_text())["bound"]["claim_id"] == ident
    assert claims.RedirectClaims().state(ident) == {"state": "bound"}

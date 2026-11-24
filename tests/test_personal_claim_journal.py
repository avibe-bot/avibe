"""Claims meet the already-shipped pending-pair journal at these recovery boundaries."""

import pytest

from config.v2_config import V2Config
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from tests.test_personal_claims import claim_peer as claim_peer
from vibe import personal_claims as claims, remote_access


def _claim(subject="owner-id"):
    return claims.run_link(expected_subject=subject, show_link=lambda _: None)


def test_expired_unconsumed_grant_retires_only_its_claim_attempt(claim_peer, monkeypatch):
    from vibe import pairing_replay

    def expired(*args, **kwargs):
        raise remote_access.BackendRequestError(400, {'error': 'pairing_key_expired'})

    with monkeypatch.context() as patch:
        patch.setattr(remote_access, '_json_request', expired)
        outcome = _claim()
        assert (outcome['state'], outcome['code']) == ('error', 'fresh_claim_required')
    assert remote_access._read_pending_pairing_record() == (None, None)
    assert 'claim_id' not in claims._outcome()
    assert not pairing_replay.orphaned_delivery()
    assert _claim()['state'] == 'bound'


@pytest.mark.parametrize('journal_lost', [False, True])
def test_claim_poll_distinguishes_retired_expiry_from_lost_journal(claim_peer, monkeypatch, journal_lost):
    from tests.test_personal_claims import _start, callback_proof
    from vibe import ui_server

    sends = []
    def expired(_url, request, **kwargs):
        sends.append(request['redeem_request_id'])
        if journal_lost:
            remote_access._pending_pairing_path().unlink()
        raise remote_access.BackendRequestError(400, {'error': 'pairing_key_expired'})

    monkeypatch.setattr(remote_access, '_json_request', expired)
    client = ui_server.app.test_client()
    start = _start(client).get_json()
    ident = start['claim_id']
    callback = client.get(f'/remote/claim/callback?claim_id={ident}&state={callback_proof(start)}', base_url='http://localhost')
    assert callback.status_code == 303
    flow = claims.redirect_claims._get(ident)
    with flow.lock:
        assert flow.lock.wait_for(lambda: flow.view['state'] == 'error', timeout=3)
    expected = 'pairing_needs_reconciliation' if journal_lost else 'fresh_claim_required'
    for _ in range(2):
        assert client.get('/api/remote-access/claims/' + ident).get_json()['code'] == expected
    assert (claims._outcome().get('claim_id') == ident) is journal_lost
    assert len(sends) == 1 and claim_peer.decisions == ['owner-id']


def test_explicit_key_leaves_settled_personal_request_with_its_owner(claim_peer, monkeypatch):
    from vibe import pairing_replay

    sends = []
    def rejected(url, request, **kwargs):
        sends.append(request['redeem_request_id'])
        raise remote_access.BackendRequestError(422, {'error': 'pairing_request_failed'})

    with monkeypatch.context() as patch:
        patch.setattr(remote_access, '_json_request', rejected)
        assert _claim()['code'] == 'pairing_recovery_required'
        assert remote_access.pending_pairing_status(V2Config.load())['operation_owner'] == 'personal_claim'
        evidence = [remote_access._pending_pairing_path(), pairing_replay._path(), claims._marker()]
        before = [path.read_bytes() for path in evidence]
        decisions = list(claim_peer.decisions)
        result = remote_access.pair('fixture-unrelated-key', 'https://avibe.bot')
        assert result['error'] == 'local_instance_conflict' and result['operation_owner'] == 'personal_claim'
        assert [path.read_bytes() for path in evidence] == before
        assert len(sends) == 1 and claim_peer.decisions == decisions
    assert claims.recover_pairing(claim_peer.id)['state'] == 'bound'
    assert claim_peer.decisions == decisions and not pairing_replay.orphaned_delivery()


@pytest.mark.parametrize('superseded', [False, True])
def test_personal_recovery_classifies_the_generation_that_tick_finished(claim_peer, monkeypatch, superseded):
    from vibe import pairing_replay

    now = [pairing_replay._now()]
    monkeypatch.setattr(pairing_replay, '_now', lambda: now[0])
    requests = []
    def redeem(_url, payload, **_kwargs):
        requests.append(dict(payload))
        if len(requests) == 1:
            raise remote_access.BackendRequestError(503, {'error': 'temporarily_unavailable'})
        return _response()
    monkeypatch.setattr(remote_access, '_json_request', redeem)
    assert _claim()['code'] == 'pairing_recovery_required'
    retry = remote_access._retry_installation_pairing
    def finish_between_selection_and_dispatch(record):
        now[0] += 2
        pairing_replay.tick()
        if superseded:
            cfg = V2Config.load()
            cfg.remote_access.vibe_cloud.instance_secret = 'fixture-successor'
            cfg.save()
        return retry(record)
    monkeypatch.setattr(remote_access, '_retry_installation_pairing', finish_between_selection_and_dispatch)
    result = claims.recover_pairing(claim_peer.id)
    assert result == ({'state': 'error', 'code': 'local_binding_changed'} if superseded else {'state': 'bound'})
    assert len(requests) == 2 and requests[0] == requests[1]
    assert claim_peer.decisions == ['owner-id']


def test_off_binding_is_not_repaired_or_replaced(claim_peer):
    assert _claim()["state"] == "bound"
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.enabled = False
    cfg.save()
    before = len(claim_peer.calls)
    assert _claim()["code"] == "already_bound"
    assert not V2Config.load().remote_access.vibe_cloud.enabled
    assert len(claim_peer.calls) == before


def test_legacy_prepared_without_a_receipt_blocks_new_accept(claim_peer):
    record = remote_access._new_pairing_claim(V2Config.load(), "https://avibe.bot", "original", pairing_key="fixture-old")
    remote_access._write_pending_pairing_record(record)
    for subject in ('different', 'owner-id'):
        assert _claim(subject)['code'] == 'pairing_needs_reconciliation'
    assert not claim_peer.calls
    assert remote_access._read_pending_pairing_record() == (record, None)
    assert not V2Config.load().remote_access.vibe_cloud.instance_id


def test_busy_after_consent_does_not_accept_decline_or_change_the_journal(claim_peer, monkeypatch):
    import threading
    from vibe import pairing_replay

    holding, release = threading.Event(), threading.Event()
    def hold():
        with pairing_replay._send_lock():
            holding.set()
            assert release.wait(10)
    worker = threading.Thread(target=hold)
    def confirm(*_):
        worker.start()
        assert holding.wait(5)
        return True
    def no_pairing(*args, **kwargs):
        pytest.fail('busy input must not reach the pairing endpoint')
    monkeypatch.setattr(remote_access, '_json_request', no_pairing)
    try:
        result = claims.run_link(confirm=confirm, show_link=lambda _: None)
        assert result['code'] == 'pairing_local_busy' and result['accepted'] is False
        assert claim_peer.calls[-1][0] == '/api/v1/claims/redeem'  # observation only
        assert claim_peer.decisions == [] and not claims._marker().exists()
        assert remote_access._read_pending_pairing_record() == (None, None)
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(10)


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


@pytest.mark.parametrize("via", ["legacy", "fresh_start", "explicit_recovery"])
def test_durable_status_after_other_caller_finishes_sealed_journal(claim_peer, monkeypatch, via):
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
        calls = []
        monkeypatch.setattr(ui_server, "_ensure_remote_access_monitoring", lambda: calls.append(V2Config.load().remote_access.vibe_cloud.instance_id))
        assert _start(client).get_json()["code"] == "already_bound"
        assert calls == ["inst_A"]
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
    assert result["code"] == "pairing_needs_reconciliation"
    assert not V2Config.load().remote_access.vibe_cloud.instance_id


@pytest.mark.parametrize("damage", ["{not json", "[]", '{"bound": 5}', b"\xff\xfe"])
def test_damaged_attempt_blocks_new_claim_but_not_matching_sealed_archive(claim_peer, monkeypatch, damage):
    """This file may be the sole acceptance evidence: only a sealed local receipt can repair it."""
    import json
    from tests.test_pairing_recovery import _fail_config_publication_once
    marker = claims._marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    def damage_marker():
        marker.write_bytes(damage if isinstance(damage, bytes) else damage.encode())
    damage_marker()
    before = marker.read_bytes()
    assert _claim()['code'] == 'pairing_needs_reconciliation'
    assert not claim_peer.calls and marker.read_bytes() == before
    # Independent clean fixture history: seal a real receipt first, then damage the marker.
    marker.unlink()
    _fail_config_publication_once(monkeypatch)
    assert _claim()["code"] == "pairing_recovery_required"
    ident = claim_peer.id
    assert remote_access._read_pending_pairing_record()[0]["phase"] == "redeemed"
    damage_marker()
    assert claims.recover_pairing(ident)["state"] == "bound"
    assert remote_access._read_pending_pairing_record() == (None, None)
    assert json.loads(marker.read_text())["bound"]["claim_id"] == ident
    assert claims.RedirectClaims().state(ident) == {"state": "bound"}

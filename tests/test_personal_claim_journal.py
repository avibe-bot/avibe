"""Claims meet the already-shipped pending-pair journal at these recovery boundaries."""

import pytest

from config.v2_config import V2Config
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from tests.test_personal_claims import claim_peer as claim_peer
from vibe import personal_claims as claims, remote_access


def _claim(subject="owner-id"):
    return claims.run_link(expected_subject=subject, show_link=lambda _: None)


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
    assert _claim("different")["state"] == "declined"
    assert remote_access._read_pending_pairing_record() == (record, None)
    assert _claim()["state"] == "bound"
    assert remote_access._read_pending_pairing_record() == (None, None)
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


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

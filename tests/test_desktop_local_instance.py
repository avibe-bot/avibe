"""Desktop local identity boundaries through real HTTP, locks, and persistence."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
from uuid import UUID

import pytest

from config.v2_config import V2Config
from tests import hang_guard
from tests.test_pairing_recovery import (
    _response,
    _save_paired_identity,
    pairing_host as pairing_host,
)
from tests.ui_server_test_helpers import _save_config, csrf_headers, remote_session_cookie
from vibe import desktop_local_instance, remote_access, ui_server


@pytest.fixture
def desktop_host(pairing_host, monkeypatch):
    monkeypatch.setattr(ui_server, "_ensure_remote_access_monitoring", lambda: None)
    monkeypatch.setattr(desktop_local_instance.socket, "gethostname", lambda: "小明的 Mac")
    return ui_server.app.test_client()


def _prepare(client, account="account-a", revision=None):
    if revision is None:
        revision = client.get("/api/desktop/local-instance").get_json()["binding_revision"]
    return client.post("/api/desktop/local-instance/prepare", json={
        "account_id": account, "expected_binding_revision": revision,
    }, headers=csrf_headers(client))


def _pair_body(prepared):
    local = prepared["local"]
    return {
        "pairing_key": "fixture-desktop-key", "backend_url": "https://backend.test",
        "device_name": local["device_name"],
        "desktop_enrollment": {
            "installation_id": local["installation_id"],
            "operation_id": prepared["operation_id"],
            "account_id": local["enrollment"]["account_id"],
            "expected_binding_revision": local["binding_revision"],
        },
    }


def test_descriptor_read_and_prepare_are_stable_and_nonsecret(desktop_host):
    client = desktop_host
    response = client.get("/api/desktop/local-instance")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["installation_id"] is None
    assert not desktop_local_instance._identity_path().exists()
    first = _prepare(client).get_json()
    assert str(UUID(first["local"]["installation_id"])) == first["local"]["installation_id"]
    assert first["local"]["device_name"] == "小明的 Mac"
    assert _prepare(client).get_json() == first
    claim = remote_access._read_pending_pairing_record()[0]
    assert claim["session_secret"] not in json.dumps(first)
    if os.name != "nt":
        assert desktop_local_instance._identity_path().stat().st_mode & 0o777 == 0o600
        assert remote_access._pending_pairing_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("headers,peer", [
    ({"X-Forwarded-For": "127.0.0.1"}, "127.0.0.1"),
    ({"Forwarded": "for=127.0.0.1;host=localhost"}, "127.0.0.1"),
    ({}, "192.168.1.2"),
])
def test_descriptor_and_prepare_refuse_indirect_connections(desktop_host, headers, peer):
    client = desktop_host
    revision = client.get("/api/desktop/local-instance").get_json()["binding_revision"]
    for method, path in [
        ("get", "/api/desktop/local-instance"),
        ("post", "/api/desktop/local-instance/prepare"),
    ]:
        response = getattr(client, method)(
            path, headers={**csrf_headers(client), **headers},
            environ_base={"REMOTE_ADDR": peer},
            **({"json": {"account_id": "a", "expected_binding_revision": revision}} if method == "post" else {}),
        )
        assert response.status_code == 403
    assert not desktop_local_instance._identity_path().exists()


@pytest.mark.parametrize("mutation", ["missing_origin", "other_port", "missing_csrf", "wrong_csrf"])
def test_prepare_requires_exact_origin_and_csrf(desktop_host, mutation):
    client = desktop_host
    headers = csrf_headers(client)
    if mutation == "missing_origin":
        headers.pop("Origin")
    elif mutation == "other_port":
        headers["Origin"] = "http://localhost:9999"
    elif mutation == "missing_csrf":
        headers.pop("X-Vibe-CSRF-Token")
    else:
        headers["X-Vibe-CSRF-Token"] = "wrong"
    response = client.post("/api/desktop/local-instance/prepare", json={
        "account_id": "a", "expected_binding_revision": "anything",
    }, headers=headers, base_url="http://localhost")
    assert response.status_code == 403
    assert not desktop_local_instance._identity_path().exists()


def test_concurrent_prepare_reserves_one_account(desktop_host):
    """AUTH-SETUP-173: the shared config lock owns concurrent preparation."""
    revision = desktop_host.get("/api/desktop/local-instance").get_json()["binding_revision"]
    barrier = threading.Barrier(2)

    def reserve(account):
        barrier.wait(timeout=10)
        return _prepare(ui_server.app.test_client(), account, revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(reserve, ["account-a", "account-b"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.get_json() for response in responses if response.status_code == 200)
    other = "account-b" if winner["local"]["enrollment"]["account_id"] == "account-a" else "account-a"
    assert _prepare(desktop_host, other).get_json()["error"] == "local_instance_conflict"
    assert desktop_host.get("/api/desktop/local-instance").get_json() == winner["local"]


@pytest.mark.parametrize("field", ["account_id", "operation_id", "installation_id", "expected_binding_revision"])
def test_guard_refuses_mismatched_owner_before_redeem(desktop_host, monkeypatch, field):
    body = _pair_body(_prepare(desktop_host).get_json())
    body["desktop_enrollment"][field] = "different"
    monkeypatch.setattr(remote_access, "_json_request", lambda *_a, **_kw: pytest.fail("redeemed stale guard"))
    response = desktop_host.post("/api/remote-access/vibe-cloud/pair", json=body,
                                 headers=csrf_headers(desktop_host))
    assert response.status_code == 409
    assert "redeem_request" not in remote_access._read_pending_pairing_record()[0]


def test_definitely_expired_grant_can_renew_only_the_same_operation(desktop_host, monkeypatch):
    body = _pair_body(_prepare(desktop_host).get_json())
    calls = []

    def redeem(_url, payload, **_kwargs):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise remote_access.BackendRequestError(400, {"error": "pairing_key_expired"})
        return _response()

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    first = desktop_host.post("/api/remote-access/vibe-cloud/pair", json=body,
                              headers=csrf_headers(desktop_host))
    assert first.get_json()["error"] == "pairing_key_expired"
    assert _prepare(desktop_host).get_json()["operation_id"] == body["desktop_enrollment"]["operation_id"]
    body["pairing_key"] = "renewed-fixture-key"
    second = desktop_host.post("/api/remote-access/vibe-cloud/pair", json=body,
                               headers=csrf_headers(desktop_host))
    assert second.status_code == 200, second.get_json()
    assert calls[0]["redeem_request_id"] != calls[1]["redeem_request_id"]


@pytest.mark.parametrize("status,error,delay", [
    (409, "access_denied", 1),
    (503, "pairing_redeem_pending", 1),
    (409, "pairing_redeem_pending", 0),
    (409, "pairing_redeem_pending", True),
    (409, "registration_indeterminate", None),
    (400, "invalid_pairing_key", None),
    (400, "pairing_key_used", None),
    (409, "pairing_key_expired", None),
    (503, "pairing_key_expired", None),
])
def test_only_explicit_bounded_pending_is_retry_advice(desktop_host, monkeypatch, status, error, delay):
    body = _pair_body(_prepare(desktop_host).get_json())

    def redeem(*_args, **_kwargs):
        raise remote_access.BackendRequestError(status, {"error": error, "retry_after_seconds": delay})

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    response = desktop_host.post("/api/remote-access/vibe-cloud/pair", json=body,
                                 headers=csrf_headers(desktop_host))
    assert response.status_code == 400
    assert response.get_json()["error"] == "pairing_redeem_indeterminate"
    pending = remote_access._read_pending_pairing_record()[0]
    assert pending["operation_id"] == body["desktop_enrollment"]["operation_id"]
    assert pending["phase"] == "prepared" and pending.get("redeem_request")
    assert "desktop_failure" not in pending
    assert not V2Config.load().remote_access.vibe_cloud.instance_id
    body["pairing_key"] = "cannot-replace-an-uncertain-request"
    refused = desktop_host.post("/api/remote-access/vibe-cloud/pair", json=body,
                                headers=csrf_headers(desktop_host))
    assert refused.status_code == 409
    assert refused.get_json()["error"] == "local_instance_conflict"
    assert remote_access._read_pending_pairing_record()[0] == pending


@pytest.mark.parametrize("damaged", ["identity", "pending"])
def test_damaged_state_is_preserved(desktop_host, damaged):
    _prepare(desktop_host)
    path = desktop_local_instance._identity_path() if damaged == "identity" else remote_access._pending_pairing_path()
    path.write_text("{broken", encoding="utf-8")
    for response in [
        desktop_host.get("/api/desktop/local-instance"),
        _prepare(desktop_host, revision="any"),
    ]:
        assert response.status_code == 503
    assert path.read_text() == "{broken"


def test_delayed_desktop_redeem_cannot_publish_over_new_binding(desktop_host, monkeypatch):
    """AUTH-SETUP-173: publication compares the binding after the network wait."""
    prepared = _prepare(desktop_host).get_json()
    entered, release = threading.Event(), threading.Event()
    result = []

    def redeem(*_args, **_kwargs):
        entered.set()
        assert release.wait(10)
        return _response("late")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    body = _pair_body(prepared)
    worker = threading.Thread(daemon=True, target=lambda: result.append(remote_access.pair(
        body["pairing_key"], body["backend_url"], body["device_name"],
        desktop_enrollment=body["desktop_enrollment"],
    )))
    worker.start()
    try:
        assert entered.wait(10)
        _save_paired_identity("new-owner")
    finally:
        release.set()
        hang_guard.join(worker, "delayed Desktop redeem")
    assert result[0]["error"] == "pairing_superseded_after_redeem"
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "new-owner"


def test_matching_paired_prepare_preserves_off_and_legacy_identity(desktop_host, monkeypatch):
    """AUTH-SETUP-174: relogin neither initializes legacy identity nor starts a tunnel."""
    saved = _save_paired_identity("existing")
    monkeypatch.setattr(remote_access, "start", lambda *_: pytest.fail("relogin started tunnel"))
    prepared = _prepare(desktop_host).get_json()
    assert prepared["operation_id"] is None
    assert prepared["local"]["installation_id"] is None
    assert prepared["local"]["pairing"]["instance_id"] == "existing"
    assert prepared["local"]["remote_access"]["requested"] is False
    assert V2Config.load().remote_access == saved.remote_access
    assert not remote_access._pending_pairing_path().exists()


def test_remote_owner_cannot_use_guarded_local_pair(desktop_host, monkeypatch, tmp_path):
    config = _save_config(tmp_path, paired=True, instance_kind="personal")
    desktop_host.set_cookie(remote_access.SESSION_COOKIE_NAME,
                            remote_session_cookie(config, "owner@example.test", "owner"),
                            domain="alex.avibe.bot")
    headers = csrf_headers(desktop_host, "https://alex.avibe.bot")
    monkeypatch.setattr(remote_access, "pair", lambda *_a, **_kw: pytest.fail("remote Desktop pair"))
    response = desktop_host.post("/api/remote-access/vibe-cloud/pair",
                                 json={"desktop_enrollment": {}}, headers=headers,
                                 base_url="https://alex.avibe.bot")
    assert response.status_code == 403
    assert response.get_json()["error"] == "direct_loopback_required"

@pytest.mark.parametrize("late_outcome", ["expired", "success", "malformed_success"])
def test_late_old_grant_outcome_cannot_change_replacement_request(desktop_host, monkeypatch, late_outcome):
    """AUTH-SETUP-172/173: renewal fences every old request outcome, not just its operation.

    Serial expiry coverage misses a second in-flight old response arriving after
    a replacement has redeemed and lost its response. That response must neither
    fail, publish nor retire the newer replay record.
    """
    body = _pair_body(_prepare(desktop_host).get_json())
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    calls_lock = threading.Lock()

    def redeem(_url, payload, **_kwargs):
        with calls_lock:
            calls.append(dict(payload))
            call_number = len(calls)
        if call_number == 1:
            entered.set()
            assert release.wait(10)
            if late_outcome == "success":
                return _response("stale-device")
            if late_outcome == "malformed_success":
                return {"ok": True}
            raise remote_access.BackendRequestError(400, {"error": "pairing_key_expired"})
        if call_number == 2:
            raise remote_access.BackendRequestError(400, {"error": "pairing_key_expired"})
        if call_number == 3:
            raise ConnectionError("fixture: replacement response lost")
        return _response("replacement-device")

    monkeypatch.setattr(remote_access, "_json_request", redeem)

    def pair(key):
        return remote_access.pair(key, body["backend_url"], body["device_name"],
                                  desktop_enrollment=body["desktop_enrollment"])

    worker = threading.Thread(daemon=True, target=lambda: results.append(pair(body["pairing_key"])))
    worker.start()
    try:
        assert entered.wait(10)
        assert pair(body["pairing_key"])["error"] == "pairing_key_expired"
        assert pair("replacement-key")["error"] == "pairing_redeem_indeterminate"
        replacement = remote_access._read_pending_pairing_record()[0]
    finally:
        release.set()
        hang_guard.join(worker, "old grant response after renewal")
    assert results[0]["error"] == "pairing_superseded_after_redeem"
    assert remote_access._read_pending_pairing_record()[0] == replacement
    assert pair("third-key")["error"] == "local_instance_conflict"
    assert not V2Config.load().remote_access.vibe_cloud.instance_id
    assert remote_access.pair("", body["backend_url"])["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "replacement-device"
    assert calls[0] == calls[1]
    assert calls[2] == calls[3]
    assert calls[0]["redeem_request_id"] != calls[2]["redeem_request_id"]

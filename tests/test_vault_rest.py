from __future__ import annotations

import json

from unittest.mock import Mock

from storage import vault_service
from storage.vault_crypto import Sealed
from tests.ui_server_test_helpers import csrf_headers
from vibe import api
from vibe.ui_server import app


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _mock_avault_p2(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_p2_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)


def test_rest_create_rejects_protected_plaintext_value(monkeypatch):
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    client = app.test_client()

    response = client.post(
        "/api/vault/secrets",
        json={"name": "NO_PLAINTEXT", "protection": "protected", "value": "secret"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "plaintext_value_rejected"
    seal.assert_not_called()


def test_rest_create_accepts_blind_box(monkeypatch):
    seal = Mock(return_value=_sealed("rest"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    client = app.test_client()
    blind_box = {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}

    response = client.post(
        "/api/vault/secrets",
        json={"name": "REST_KEY", "blind_box": blind_box},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["secret"]["name"] == "REST_KEY"
    seal.assert_called_once_with("REST_KEY", blind_box)


def test_rest_create_rejects_standard_plaintext_value(monkeypatch):
    seal = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    client = app.test_client()

    response = client.post(
        "/api/vault/secrets",
        json={"name": "REST_FALLBACK", "value": "secret"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "plaintext_value_rejected"
    seal.assert_not_called()


def test_rest_list_invalid_tag_returns_vault_error():
    client = app.test_client()

    response = client.get("/api/vault/secrets?tag=bad%20tag")

    assert response.status_code == 409
    assert response.get_json()["code"] == "invalid_request"


def test_rest_delete_protected_without_assertion_is_rejected(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "PROTECTED_HTTP_DELETE",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    client = app.test_client()

    response = client.delete("/api/vault/secrets/PROTECTED_HTTP_DELETE", headers=csrf_headers(client))

    assert response.status_code == 409
    assert response.get_json()["code"] == "protected_auth_required"
    with api._vault_engine().connect() as conn:
        assert vault_service.get_secret_meta(conn, "PROTECTED_HTTP_DELETE")["protection"] == "protected"


def test_rest_create_rejects_plaintext_value_in_mixed_payloads(monkeypatch):
    seal = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    client = app.test_client()

    standard = client.post(
        "/api/vault/secrets",
        json={
            "name": "MIXED_REST",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "value": "secret",
        },
        headers=csrf_headers(client),
    )
    protected = client.post(
        "/api/vault/secrets",
        json={
            "name": "MIXED_PROTECTED_REST",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "value": "secret",
        },
        headers=csrf_headers(client),
    )

    assert standard.status_code == 400
    assert standard.get_json()["code"] == "plaintext_value_rejected"
    assert protected.status_code == 400
    assert protected.get_json()["code"] == "plaintext_value_rejected"
    seal.assert_not_called()


def test_rest_create_rejects_nested_plaintext_value_fields(monkeypatch):
    seal = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    client = app.test_client()

    standard = client.post(
        "/api/vault/secrets",
        json={
            "name": "NESTED_REST",
            "blind_box": {
                "scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1",
                "enc": "enc",
                "ct": "ct",
                "value": "secret",
            },
        },
        headers=csrf_headers(client),
    )
    protected = client.post(
        "/api/vault/secrets",
        json={
            "name": "NESTED_PROTECTED_REST",
            "protection": "protected",
            "sealed": {
                "ciphertext": "ct",
                "nonce": "n",
                "wrap_meta": "wm",
                "value": "secret",
            },
        },
        headers=csrf_headers(client),
    )

    assert standard.status_code == 400
    assert standard.get_json()["code"] == "plaintext_value_rejected"
    assert protected.status_code == 400
    assert protected.get_json()["code"] == "plaintext_value_rejected"
    seal.assert_not_called()


def test_rest_agent_pubkey_route(monkeypatch):
    monkeypatch.setattr(api, "avault_agent_pubkey", Mock(return_value={"public_key": "pk", "fingerprint": "fp"}))
    client = app.test_client()

    response = client.get("/api/vault/agent/pubkey")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "public_key": "pk", "fingerprint": "fp"}


def test_rest_requests_and_grants_routes(monkeypatch):
    _mock_avault_p2(monkeypatch)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    monkeypatch.setattr(api, "avault_agent_release", Mock(return_value={"released": True}))
    api.create_vault_secret(
        {
            "name": "PROTECTED_REST",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    client = app.test_client()
    with api._vault_engine().begin() as conn:
        from storage import vault_service

        req = vault_service.create_access_request(
            conn,
            "PROTECTED_REST",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    grant_response = client.post(
        "/api/vault/grants",
        json={
            "session_id": "ses_1",
            "request_id": req["id"],
            "deks": [
                {
                    "name": "PROTECTED_REST",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        },
        headers=csrf_headers(client),
    )
    assert grant_response.status_code == 200
    grant_id = grant_response.get_json()["grant"]["id"]
    assert client.get("/api/vault/grants").get_json()["grants"][0]["id"] == grant_id

    revoke_response = client.delete(f"/api/vault/grants/{grant_id}", headers=csrf_headers(client))
    assert revoke_response.status_code == 200
    assert revoke_response.get_json()["grant"]["status"] == "revoked"

    with api._vault_engine().begin() as conn:
        vault_service.create_access_request(conn, "PROTECTED_REST", delivery={"command": "python sync.py"})
    inbox = client.get("/api/vault/requests").get_json()
    assert inbox["requests"][0]["card"]["card_type"] == "approval"


def test_rest_request_get_hydrates_browser_unlock_material_for_protected_access(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "PROTECTED_REST",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_REST", "session_id": "ses_1"})
    client = app.test_client()

    response = client.get(f"/api/vault/requests/{requested['request']['id']}")

    assert response.status_code == 200
    card = response.get_json()["request"]["card"]
    assert card["secret_unlock_material"] == {
        "name": "PROTECTED_REST",
        "kind": "static",
        "envelope": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
    }
    agent_payload = api.get_vault_request(requested["request"]["id"])
    assert "secret_unlock_material" not in json.dumps(agent_payload)


def test_rest_grant_rejects_mismatched_request(monkeypatch):
    _mock_avault_p2(monkeypatch)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    api.create_vault_secret(
        {
            "name": "A_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-a", "nonce": "n-a", "wrap_meta": "wm-a"},
        }
    )
    api.create_vault_secret(
        {
            "name": "B_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-b", "nonce": "n-b", "wrap_meta": "wm-b"},
        }
    )
    with api._vault_engine().begin() as conn:
        from storage import vault_service

        req = vault_service.create_access_request(conn, "A_KEY", requester={"session_id": "ses_1"}, delivery={"session_id": "ses_1"})
    client = app.test_client()

    response = client.post(
        "/api/vault/grants",
        json={
            "member_names": ["B_KEY"],
            "source_selector": {"env": ["B_KEY"]},
            "session_id": "ses_1",
            "request_id": req["id"],
            "deks": [
                {
                    "name": "B_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "invalid_request"


def test_rest_fulfill_access_request_route(monkeypatch):
    _mock_avault_p2(monkeypatch)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_REST",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_REST", "session_id": "ses_1"})
    client = app.test_client()

    response = client.post(
        f"/api/vault/requests/{requested['request']['id']}/fulfill-access",
        json={
            "session_id": "ses_1",
            "deks": [
                {
                    "name": "PROTECTED_REST",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["result"]["type"] == "grant"
    assert agent_grant.call_count == 1


def test_rest_fulfill_access_request_unknown_default_scope_returns_vault_error(monkeypatch):
    _mock_avault_p2(monkeypatch)
    client = app.test_client()

    response = client.post(
        "/api/vault/requests/vrq_missing/fulfill-access",
        json={
            "deks": [
                {
                    "name": "PROTECTED_REST",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        },
        headers=csrf_headers(client),
    )

    assert response.status_code == 404
    assert response.get_json()["code"] == "request_not_found"


def test_rest_sign_errors_are_stable(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    client = app.test_client()
    missing = client.post(
        "/api/vault/sign",
        json={"name": "NOPE", "digest": "00" * 32},
        headers=csrf_headers(client),
    )
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "secret_not_found"

    api.create_vault_secret({"name": "STATIC_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    not_key = client.post(
        "/api/vault/sign",
        json={"name": "STATIC_KEY", "digest": "00" * 32},
        headers=csrf_headers(client),
    )
    assert not_key.status_code == 409
    assert not_key.get_json()["code"] == "not_signing_key"

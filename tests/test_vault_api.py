"""Tests for the vault REST wrappers in vibe/api.py.

REST create delegates sealing to avault and stores only the returned envelope.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import vault_service
from storage.models import vault_secrets
from storage.vault_crypto import Sealed
from vibe import api


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def test_create_list_delete_roundtrip(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("api"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)

    blind_box = {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}
    created = api.create_vault_secret({"name": "OPENAI_API_KEY", "blind_box": blind_box, "description": "key"})
    assert created["ok"] is True
    assert created["secret"]["name"] == "OPENAI_API_KEY"
    assert "preview" not in created["secret"]
    assert "sk-ant-abcd1234" not in json.dumps(created)
    assert "1234" not in json.dumps(created)
    seal.assert_called_once_with("OPENAI_API_KEY", blind_box)
    with api._vault_engine().connect() as conn:
        assert vault_service.get_envelope(conn, "OPENAI_API_KEY") == _sealed("api")
        public_meta_raw = conn.execute(
            select(vault_secrets.c.public_meta).where(vault_secrets.c.name == "OPENAI_API_KEY")
        ).scalar_one()
        public_meta = json.loads(public_meta_raw)
        assert public_meta == {"description": "key"}
        assert "preview" not in public_meta
        assert "1234" not in json.dumps(vault_service.get_secret_meta(conn, "OPENAI_API_KEY"))

    listed = api.get_vault_secrets()
    assert [s["name"] for s in listed["secrets"]] == ["OPENAI_API_KEY"]
    assert "sk-ant-abcd1234" not in json.dumps(listed)
    assert "1234" not in json.dumps(listed)

    removed = api.delete_vault_secret("OPENAI_API_KEY")
    assert removed == {"ok": True, "removed": True, "name": "OPENAI_API_KEY"}
    assert api.get_vault_secrets()["secrets"] == []


def test_standard_rest_create_rejects_when_pinned_avault_lacks_blind_box(monkeypatch):
    from unittest.mock import Mock

    run = Mock()
    monkeypatch.setattr(api, "_run_avault", run)
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": "0.1.2"})

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "OPENAI_API_KEY",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            }
        )

    assert exc.value.code == "avault_failed"
    assert f"requires avault >= {api.AVAULT_P2_MIN_VERSION}" in str(exc.value)
    run.assert_not_called()


def test_create_with_policy_persists_allowed_hosts(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "GH_PAT",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"allowed_hosts": ["api.github.com"], "auth": {"type": "bearer"}},
        }
    )
    secret = api.get_vault_secrets()["secrets"][0]
    assert secret["policy"]["allowed_hosts"] == ["api.github.com"]


def test_duplicate_name_conflict(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "DUP", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "DUP", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc2", "ct": "ct2"}})
    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_invalid_name_rejected_before_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "lower", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    assert exc.value.code == "invalid_name"
    seal.assert_not_called()


def test_rest_plaintext_value_rejected_before_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "NO_PLAINTEXT", "value": "secret"})
    assert exc.value.code == "blind_box_required"
    seal.assert_not_called()


def test_avault_failure_maps_to_api_error(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(side_effect=api.AvaultError("seal failed")))
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "FAIL_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    assert exc.value.code == "avault_failed"


def test_delete_missing_is_404():
    with pytest.raises(api.VaultApiError) as exc:
        api.delete_vault_secret("NOPE")
    assert exc.value.code == "secret_not_found"
    assert exc.value.status == 404


def test_audit_lists_events_without_values(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "AUD_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    api.delete_vault_secret("AUD_KEY")
    audit = api.get_vault_audit()
    events = {e["event"] for e in audit["events"]}
    assert {"created", "deleted"} <= events
    assert "supersecret-AUD" not in json.dumps(audit)


def test_create_protected_stores_browser_envelope_without_avault(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("should-not-use"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    created = api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "browser-ct", "nonce": "browser-n", "wrap_meta": {"v": 1, "wrapped_dek": "dek"}},
            "public_meta": {"factor_hint": "passkey-first"},
        }
    )
    assert created["secret"]["protection"] == "protected"
    seal.assert_not_called()
    with api._vault_engine().connect() as conn:
        row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == "PROTECTED_KEY")).mappings().one()
    assert row["ciphertext"] == "browser-ct"
    assert json.loads(row["wrap_meta"]) == {"v": 1, "wrapped_dek": "dek"}


def test_create_protected_rejects_non_string_envelope_fields(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("should-not-use"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "BAD_ENVELOPE",
                "protection": "protected",
                "sealed": {"ciphertext": None, "nonce": "browser-n", "wrap_meta": "wm"},
            }
        )

    assert exc.value.code == "invalid_envelope"
    seal.assert_not_called()
    assert api.get_vault_secrets()["secrets"] == []


def test_pubkey_wrapper_parses_avault(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(
        api,
        "_run_avault",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"public_key":"pk","fingerprint":"fp"}', stderr=b"")),
    )
    assert api.avault_pubkey() == {"public_key": "pk", "fingerprint": "fp"}


def test_blind_box_wrapper_relays_json_to_avault(monkeypatch):
    from types import SimpleNamespace

    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"ciphertext":"ct","nonce":"n","wrap_meta":"wm"}', stderr=b""))
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(api, "_run_avault", run)
    sealed = api.avault_seal_blind_box("API_KEY", {"scheme": "s", "enc": "e", "ct": "c"})
    assert sealed == Sealed(ciphertext="ct", nonce="n", wrap_meta="wm")
    args, kwargs = run.call_args
    assert args[0] == ["seal", "--name", "API_KEY", "--blind-box"]
    assert json.loads(kwargs["stdin"]) == {"scheme": "s", "enc": "e", "ct": "c"}


def test_blind_box_wrapper_single_object_strips_request_metadata(monkeypatch):
    from types import SimpleNamespace

    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"ciphertext":"ct","nonce":"n","wrap_meta":"wm"}', stderr=b""))
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(api, "_run_avault", run)
    api.avault_seal_blind_box({"name": "API_KEY", "scheme": "s", "enc": "e", "ct": "c"})
    assert json.loads(run.call_args.kwargs["stdin"]) == {"scheme": "s", "enc": "e", "ct": "c"}


def test_sign_wrapper_sends_name_and_envelope_to_avault(monkeypatch):
    from types import SimpleNamespace

    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'{"signature":"abcd","recovery_id":1}', stderr=b""))
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_P2_MIN_VERSION})
    monkeypatch.setattr(api, "_run_avault", run)
    result = api.avault_sign(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")
    assert result == {"signature": "abcd", "recovery_id": 1}
    body = json.loads(run.call_args.kwargs["stdin"])
    assert body["name"] == "ETH_KEY"
    assert body["key_envelope"] == {"ciphertext": "ct-key", "nonce": "n-key", "wrap_meta": "wm-key"}


def test_standard_keypair_signs_via_avault(monkeypatch):
    sign = Mock(return_value={"signature": "sig", "recovery_id": 1})
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})
    assert result == {"ok": True, "signature": {"signature": "sig", "recovery_id": 1}}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "ETH_KEY")
    assert meta["use_count"] == 1
    assert meta["last_used_at"] is not None


def test_vault_sign_rejects_non_keypair_secret(monkeypatch):
    from unittest.mock import Mock

    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "STATIC_KEY", "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"}})
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "STATIC_KEY", "digest": "00" * 32})
    assert exc.value.code == "not_signing_key"
    sign.assert_not_called()


def test_create_and_revoke_grant_api(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
    created = api.create_vault_grant(
        {
            "scope_type": "secret",
            "scope_ref": "GRANT_KEY",
            "session_id": "ses_1",
            "ttl_seconds": 300,
            "request_id": req["id"],
            "deks_by_secret": {"GRANT_KEY": "dek"},
        }
    )
    assert created["grant"]["cached_member_count"] == 1
    grants = api.get_vault_grants()["grants"]
    assert grants[0]["id"] == created["grant"]["id"]
    revoked = api.revoke_vault_grant(created["grant"]["id"])
    assert revoked["grant"]["status"] == "revoked"


def test_create_grant_api_rejects_missing_deks_before_approval(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "scope_type": "secret",
                "scope_ref": "GRANT_KEY",
                "session_id": "ses_1",
                "request_id": req["id"],
                "deks_by_secret": {"GRANT_KEY": None},
            }
        )

    assert exc.value.code == "invalid_grant"
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
    assert status == "pending"


def test_create_grant_api_preserves_unbound_session_choice(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    created = api.create_vault_grant(
        {
            "scope_type": "secret",
            "scope_ref": "GRANT_KEY",
            "request_id": req["id"],
            "this_session_only": False,
            "deks_by_secret": {"GRANT_KEY": "dek"},
        }
    )

    assert created["grant"]["session_id"] is None


def test_protected_sign_requires_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})
    assert result["ok"] is False
    assert result["code"] == "browser_signature_required"
    assert result["request"]["card"]["request_type"] == "sign"
    assert result["request"]["card"]["scope_options"] == []


def test_protected_sign_completion_requires_matching_request(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    digest = "00" * 32
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable", "signature": {"signature": "sig"}})
    assert exc.value.code == "missing_request_id"

    pending = api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable"})
    request_id = pending["request"]["id"]
    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": "11" * 32,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": request_id,
                "signature": {"signature": "sig"},
            }
        )
    assert exc.value.code == "invalid_request"

    result = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": request_id,
            "signature": {"signature": "ab" * 64, "recovery_id": 1},
        }
    )
    assert result["ok"] is True
    assert result["request"]["status"] == "approved"


def test_protected_sign_completion_rejects_malformed_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    digest = "00" * 32
    pending = api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable"})

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": {"signature": "not-hex", "recovery_id": 1},
            }
        )

    assert exc.value.code == "invalid_request"


def test_vault_sign_rejects_malformed_digest_before_request_or_avault(monkeypatch):
    from unittest.mock import Mock

    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": "not-hex", "scheme": "ecdsa-secp256k1-recoverable"})

    assert exc.value.code == "invalid_digest"
    sign.assert_not_called()
    assert api.get_vault_requests()["requests"] == []

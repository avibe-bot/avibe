"""Tests for the vault REST wrappers in vibe/api.py.

REST create delegates sealing to avault and stores only the returned envelope.
"""

from __future__ import annotations

import json
import socket
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import vault_service
from storage.models import vault_secrets
from storage.vault_crypto import Sealed
from vibe import api


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


@pytest.fixture
def avault_p2(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_p2_surface", lambda _feature: None)


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


def test_standard_rest_create_uses_plaintext_fallback_when_pinned_avault_lacks_blind_box(monkeypatch):
    from unittest.mock import Mock

    seal = Mock(return_value=_sealed("fallback"))
    blind_box = Mock(side_effect=api.AvaultError(f"blind-box seal requires avault >= {api.AVAULT_P2_MIN_VERSION}"))
    monkeypatch.setattr(api, "avault_seal", seal)
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    created = api.create_vault_secret({"name": "OPENAI_API_KEY", "value": "secret"})

    assert created["secret"]["name"] == "OPENAI_API_KEY"
    seal.assert_called_once_with("OPENAI_API_KEY", b"secret")
    blind_box.assert_not_called()


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
        api.create_vault_secret({"name": "NO_PLAINTEXT", "protection": "protected", "value": "secret"})
    assert exc.value.code == "invalid_envelope"
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


def test_agent_pubkey_reports_upgrade_required_when_managed_pin_lacks_p2(monkeypatch):
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": api.AVAULT_VERSION})
    monkeypatch.setattr(api, "_managed_avault_release_satisfies_p2", lambda: False)

    with pytest.raises(api.VaultApiError) as exc:
        api.get_vault_agent_pubkey()

    assert exc.value.code == "avault_upgrade_required"
    assert exc.value.status == 409
    assert api.AVAULT_VERSION in str(exc.value)
    assert api.AVAULT_P2_MIN_VERSION in str(exc.value)


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
    assert "dek_blindbox" not in body
    assert "approval" not in body


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


def test_standard_keypair_sign_returns_signature_when_usage_audit_fails(monkeypatch):
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
    monkeypatch.setattr(vault_service, "record_signing_use", Mock(side_effect=RuntimeError("audit write failed")))

    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})

    assert result == {"ok": True, "signature": {"signature": "sig", "recovery_id": 1}}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")


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


def test_vault_sign_rejects_non_local_signer(monkeypatch):
    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "WALLET_KEY",
            "kind": "keypair",
            "signer_kind": "external",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "WALLET_KEY", "digest": "00" * 32})

    assert exc.value.code == "unsupported_signer_kind"
    sign.assert_not_called()


def test_create_and_revoke_grant_api(monkeypatch, avault_p2):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
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
            "deks": [
                {
                    "name": "GRANT_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    "dek": "raw-dek-must-not-cross-python-agent-boundary",
                    "value": "plaintext-must-not-cross-python-agent-boundary",
                }
            ],
        }
    )
    assert created["grant"]["runtime_member_count"] == 1
    assert created["grant"]["delivery_ready"] is True
    assert agent_grant.call_args.kwargs["ttl_secs"] == 300
    assert agent_grant.call_args.kwargs["deks"] == [
        {
            "name": "GRANT_KEY",
            "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
        }
    ]
    grants = api.get_vault_grants()["grants"]
    assert grants[0]["id"] == created["grant"]["id"]
    revoked = api.revoke_vault_grant(created["grant"]["id"])
    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(scope_type="secret", scope_ref="GRANT_KEY")


def test_revoke_grant_keeps_agent_scope_when_other_active_grant_exists(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req_1 = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant_1 = vault_service.create_grant(
            conn,
            scope_type="secret",
            scope_ref="GRANT_KEY",
            created_by_request_id=req_1["id"],
        )
        req_2 = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = vault_service.create_grant(
            conn,
            scope_type="secret",
            scope_ref="GRANT_KEY",
            created_by_request_id=req_2["id"],
        )

    revoked = api.revoke_vault_grant(grant_1["id"])

    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_not_called()
    with api._vault_engine().connect() as conn:
        assert vault_service.find_active_grant_for_secret(conn, "GRANT_KEY", session_id="ses_2")["id"] == grant_2["id"]


def test_revoke_grant_releases_scope_when_remaining_members_do_not_cover_cache(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "A_KEY", "protection": "protected", "group": "crypto", "sealed": {"ciphertext": "ct-a", "nonce": "n-a", "wrap_meta": "wm-a"}})
    with api._vault_engine().begin() as conn:
        req_narrow = vault_service.create_access_request(
            conn,
            "A_KEY",
            requester={"session_id": "ses_narrow"},
            delivery={"session_id": "ses_narrow"},
        )
        vault_service.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_narrow",
            created_by_request_id=req_narrow["id"],
        )
        vault_service.create_secret(conn, name="B_KEY", protection="protected", group="crypto", sealed=_sealed("b"))
        req_group = vault_service.create_access_request(
            conn,
            "A_KEY",
            requester={"session_id": "ses_group"},
            delivery={"session_id": "ses_group"},
        )
        group_grant = vault_service.create_grant(
            conn,
            scope_type="group",
            scope_ref="crypto",
            session_id="ses_group",
            created_by_request_id=req_group["id"],
        )

    revoked = api.revoke_vault_grant(group_grant["id"])

    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(scope_type="group", scope_ref="crypto")


def test_delete_protected_secret_releases_agent_scope(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}}
    )
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        vault_service.create_grant(
            conn,
            scope_type="secret",
            scope_ref="GRANT_KEY",
            session_id="ses_1",
            created_by_request_id=req["id"],
        )

    removed = api.delete_vault_secret("GRANT_KEY")

    assert removed["removed"] is True
    agent_release.assert_called_once_with(scope_type="secret", scope_ref="GRANT_KEY")


def test_release_agent_scope_fail_closed_resets_and_quarantines_socket(monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-release-", dir="/tmp")) / "s"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}}
    )
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant = vault_service.create_grant(
            conn,
            scope_type="secret",
            scope_ref="GRANT_KEY",
            created_by_request_id=req["id"],
        )

    class Manager:
        def __init__(self) -> None:
            self.socket_path = socket_path
            self.reset = Mock()

    manager = Manager()
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: manager)
    monkeypatch.setattr(api, "avault_agent_release", Mock(side_effect=api.AvaultError("timed out waiting for release")))
    try:
        api.release_vault_agent_scopes([{"scope_type": "secret", "scope_ref": "GRANT_KEY"}], reason="test")
    finally:
        listener.close()

    manager.reset.assert_called_once()
    assert not socket_path.exists()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_grants.c.status).where(vault_service.vault_grants.c.id == grant["id"])).scalar_one()
    assert status == "expired"


def test_release_agent_scope_ignores_absent_agent(monkeypatch):
    manager = Mock()
    manager.socket_path = Path("/tmp/missing-avault.sock")
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: manager)
    monkeypatch.setattr(
        api,
        "avault_agent_release",
        Mock(side_effect=api.AvaultError("failed to connect to avault agent: [Errno 2] No such file or directory")),
    )

    api.release_vault_agent_scopes([{"scope_type": "secret", "scope_ref": "GRANT_KEY"}], reason="test")

    manager.reset.assert_not_called()


def test_agent_grant_rejects_pubkey_mismatch(monkeypatch):
    agent_client = Mock()
    monkeypatch.setattr(api, "_require_avault_p2_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_client", lambda: agent_client)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})

    with pytest.raises(api.AvaultError, match="fingerprint mismatch"):
        api.avault_agent_grant(
            scope_type="secret",
            scope_ref="GRANT_KEY",
            ttl_secs=300,
            deks=[
                {
                    "name": "GRANT_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
            expected_pubkey={"public_key": "old-pk", "fingerprint": "old-fp"},
        )

    agent_client.grant.assert_not_called()


def test_agent_deliver_run_reuses_resident_agent_socket(monkeypatch):
    seen_timeout = []

    class FakeClient:
        def deliver_run(self, **kwargs):
            return {"exit_code": 7}

    class FakeManager:
        def client(self, *, timeout=None):
            seen_timeout.append(timeout)
            return FakeClient()

    monkeypatch.setattr(api, "_require_avault_p2_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    result = api.avault_agent_deliver_run(
        scope_type="secret",
        scope_ref="GRANT_KEY",
        secrets=[{"name": "GRANT_KEY", "env": "GRANT_KEY", "envelope": _sealed()}],
        command=["python3", "-c", "pass"],
    )

    assert result == {"exit_code": 7}
    assert seen_timeout == [None]


def test_agent_deliver_fetch_uses_finite_timeout(monkeypatch):
    seen_timeout = []

    class FakeClient:
        def deliver_fetch(self, **kwargs):
            return {"status": 200, "headers": {}, "body": "ok"}

    class FakeManager:
        def client(self, *, timeout=None):
            seen_timeout.append(timeout)
            return FakeClient()

    monkeypatch.setattr(api, "_require_avault_p2_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    result = api.avault_agent_deliver_fetch(
        scope_type="secret",
        scope_ref="GRANT_KEY",
        name="GRANT_KEY",
        sealed=_sealed(),
        request={"method": "GET", "url": "https://example.com", "allowed_hosts": ["example.com"], "inject": {"type": "bearer"}},
    )

    assert result["status"] == 200
    assert seen_timeout == [api._AVAULT_FETCH_TIMEOUT_SECONDS]


def test_create_grant_api_requires_resident_agent_deks(monkeypatch, avault_p2):
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
            }
        )

    assert exc.value.code == "invalid_grant"
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
    assert status == "pending"


def test_create_grant_api_rejects_mismatched_deks_before_claiming_request(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
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
                "deks": [
                    {
                        "name": "WRONG_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "invalid_grant"
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_create_grant_api_relay_runs_after_grant_commit(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    def relay(**kwargs):
        with api._vault_engine().connect() as conn:
            status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
            grants = vault_service.list_grants(conn, status="active")
        assert status == "approved"
        assert len(grants) == 1
        assert grants[0]["member_snapshot"] == ["GRANT_KEY"]
        assert grants[0]["delivery_ready"] is False
        return {"granted": 1, "ttl_secs": kwargs["ttl_secs"]}

    monkeypatch.setattr(api, "avault_agent_grant", relay)

    created = api.create_vault_grant(
        {
            "scope_type": "secret",
            "scope_ref": "GRANT_KEY",
            "session_id": "ses_1",
            "request_id": req["id"],
            "deks": [
                {
                    "name": "GRANT_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        }
    )

    assert created["grant"]["status"] == "active"


def test_create_grant_api_rejects_stale_agent_pubkey_before_claiming_request(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})
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
                "agent_pubkey": {"public_key": "old-pk", "fingerprint": "old-fp"},
                "deks": [
                    {
                        "name": "GRANT_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "avault_failed"
    assert "fingerprint mismatch" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_create_grant_api_expires_grant_when_agent_grant_fails(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(side_effect=api.AvaultError("grant is missing")))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
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
                "deks": [
                    {
                        "name": "GRANT_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "avault_failed"
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "approved"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(scope_type="secret", scope_ref="GRANT_KEY")


def test_create_grant_api_rejects_partial_agent_cache(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 0, "ttl_secs": 300}))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
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
                "deks": [
                    {
                        "name": "GRANT_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "avault_failed"
    assert "cached fewer DEKs" in str(exc.value)
    with api._vault_engine().connect() as conn:
        grants = vault_service.list_grants(conn, status=None)
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(scope_type="secret", scope_ref="GRANT_KEY")


def test_grant_ttl_uses_approved_lifetime():
    now = datetime.now(timezone.utc)
    ttl = api._grant_ttl_seconds(
        {
            "created_at": (now - timedelta(seconds=900)).isoformat(),
            "expires_at": (now + timedelta(seconds=120)).isoformat(),
        }
    )

    assert ttl == 1020


def test_create_grant_api_releases_scope_when_mark_ready_fails(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    monkeypatch.setattr(vault_service, "mark_grant_agent_ready", Mock(side_effect=vault_service.GrantNotActiveError("vgr_raced")))
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
                "deks": [
                    {
                        "name": "GRANT_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "invalid_grant"
    agent_release.assert_called_once_with(scope_type="secret", scope_ref="GRANT_KEY")
    with api._vault_engine().connect() as conn:
        grants = vault_service.list_grants(conn, status=None)
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False


def test_create_grant_api_keeps_shared_agent_scope_when_relay_fails(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(side_effect=api.AvaultError("grant is missing")))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret({"name": "GRANT_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})
    with api._vault_engine().begin() as conn:
        existing_req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_existing"},
            delivery={"session_id": "ses_existing"},
        )
        existing_grant = vault_service.create_grant(
            conn,
            scope_type="secret",
            scope_ref="GRANT_KEY",
            session_id="ses_existing",
            created_by_request_id=existing_req["id"],
        )
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
                "deks": [
                    {
                        "name": "GRANT_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            }
        )

    assert exc.value.code == "avault_failed"
    agent_release.assert_not_called()
    with api._vault_engine().connect() as conn:
        grants = {grant["id"]: grant for grant in vault_service.list_grants(conn, status=None)}
    assert grants[existing_grant["id"]]["status"] == "active"
    expired = [grant for grant in grants.values() if grant["id"] != existing_grant["id"]]
    assert len(expired) == 1
    assert expired[0]["status"] == "expired"


def test_create_grant_api_preserves_unbound_session_choice(monkeypatch, avault_p2):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
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
            "deks": [
                {
                    "name": "GRANT_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
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


def test_protected_sign_rejects_unsupported_scheme_before_request(monkeypatch):
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

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "not-a-real-scheme"})

    assert exc.value.code == "invalid_request"
    assert api.get_vault_requests()["requests"] == []


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

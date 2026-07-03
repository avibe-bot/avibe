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
from storage.models import vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from vibe import api


def _sealed(suffix: str = "1") -> Sealed:
    return Sealed(ciphertext=f"ct-{suffix}", nonce=f"n-{suffix}", wrap_meta=f"wm-{suffix}")


def _grant_from_request(conn, request: dict, *, session_id: str | None = None, cache_ready: bool = True) -> dict:
    option = request["card"]["grant_options"][0]
    return vault_service.create_grant(
        conn,
        member_names=option["member_snapshot"],
        source_selector=option["source_selector"],
        purpose=option["purpose"],
        session_id=session_id,
        request_id=request["id"],
        cache_ready=cache_ready,
    )


def _browser_ecdsa_signature_for_digest(digest: str, *, key_value: int = 1) -> tuple[dict, dict]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    private_key = ec.derive_private_key(key_value, ec.SECP256K1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )
    der = private_key.sign(bytes.fromhex(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r, s = utils.decode_dss_signature(der)
    recovery_id = next(
        index
        for index in range(4)
        if api._secp256k1_recover_ecdsa_public_key(r, s, bytes.fromhex(digest), index)
        == api._secp256k1_decompress_public_key(public_key)
    )
    signature = {
        "signature": r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex(),
        "recovery_id": recovery_id,
    }
    public_meta = {"signing_public_key": {"curve": "secp256k1", "public_key": public_key.hex()}}
    return public_meta, signature


def _assert_no_unlock_material(payload: object) -> None:
    encoded = json.dumps(payload)
    assert "secret_unlock_material" not in encoded
    assert "unlock_material" not in encoded
    assert "ct-protected" not in encoded
    assert "wm-protected" not in encoded


@pytest.fixture
def avault_p2(monkeypatch):
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)


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


def test_standard_rest_create_rejects_plaintext_value(monkeypatch):
    from unittest.mock import Mock

    blind_box = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({"name": "OPENAI_API_KEY", "value": "secret"})

    assert exc.value.code == "plaintext_value_rejected"
    blind_box.assert_not_called()


def test_create_rejects_plaintext_value_even_with_sealed_payload(monkeypatch):
    from unittest.mock import Mock

    blind_box = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    with pytest.raises(api.VaultApiError) as standard_exc:
        api.create_vault_secret(
            {
                "name": "MIXED_KEY",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "value": "secret",
            }
        )
    assert standard_exc.value.code == "plaintext_value_rejected"

    with pytest.raises(api.VaultApiError) as protected_exc:
        api.create_vault_secret(
            {
                "name": "PROTECTED_MIXED",
                "protection": "protected",
                "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
                "value": "secret",
            }
        )
    assert protected_exc.value.code == "plaintext_value_rejected"
    blind_box.assert_not_called()


def test_create_rejects_nested_plaintext_value_fields(monkeypatch):
    from unittest.mock import Mock

    blind_box = Mock()
    monkeypatch.setattr(api, "avault_seal_blind_box", blind_box)

    with pytest.raises(api.VaultApiError) as standard_exc:
        api.create_vault_secret(
            {
                "name": "NESTED_STANDARD",
                "blind_box": {
                    "scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1",
                    "enc": "enc",
                    "ct": "ct",
                    "value": "secret",
                },
            }
        )
    assert standard_exc.value.code == "plaintext_value_rejected"

    with pytest.raises(api.VaultApiError) as protected_exc:
        api.create_vault_secret(
            {
                "name": "NESTED_PROTECTED",
                "protection": "protected",
                "sealed": {
                    "ciphertext": "ct",
                    "nonce": "n",
                    "wrap_meta": "wm",
                    "value": "secret",
                },
            }
        )
    assert protected_exc.value.code == "plaintext_value_rejected"
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


def test_create_with_links_persists_skill_link(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "GH_PAT",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "links": {"skills": ["github-pr-review"]},
        }
    )
    assert api.get_vault_secrets()["secrets"][0]["tags"] == ["skill:github-pr-review"]


def test_create_with_invalid_skill_link_returns_vault_api_error(monkeypatch):
    seal = Mock(return_value=_sealed())
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "GH_PAT",
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "links": {"skills": ["github review"]},
            }
        )

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409
    seal.assert_not_called()


def test_get_vault_secrets_invalid_tag_filter_returns_vault_api_error():
    with pytest.raises(api.VaultApiError) as exc:
        api.get_vault_secrets(tag="bad tag")

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409


def test_get_provision_request_by_name_returns_pending_spec():
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(
            conn,
            "GH_TOKEN",
            spec={"tags": ["github"], "links": {"skills": ["github-pr-review"]}},
        )

    result = api.get_vault_provision_request_by_name("GH_TOKEN")

    assert result["request"]["id"] == req["id"]
    assert result["request"]["card"]["spec"]["tags"] == ["github", "skill:github-pr-review"]
    assert result["ambiguous"] is False


def test_get_provision_request_returns_request_id_match():
    with api._vault_engine().begin() as conn:
        old_req = vault_service.create_provision_request(
            conn,
            "GH_TOKEN",
            spec={"tags": ["old"]},
        )
        vault_service.create_provision_request(
            conn,
            "GH_TOKEN",
            spec={"tags": ["new"]},
        )

    result = api.get_vault_provision_request(old_req["id"])

    assert result["request"]["id"] == old_req["id"]
    assert result["request"]["card"]["spec"]["tags"] == ["old"]


def test_get_provision_request_by_name_returns_none_when_ambiguous():
    with api._vault_engine().begin() as conn:
        vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["old"]})
        vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["new"]})

    result = api.get_vault_provision_request_by_name("GH_TOKEN")

    assert result["request"] is None
    assert result["ambiguous"] is True


def test_create_secret_with_provision_request_id_fulfills_sibling_requests(monkeypatch):
    seal = Mock(return_value=_sealed("api"))
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        old_req = vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["old"]})
        new_req = vault_service.create_provision_request(conn, "GH_TOKEN", spec={"tags": ["new"]})

    created = api.create_vault_secret(
        {
            "name": "GH_TOKEN",
            "tags": ["new"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "provision_request_id": new_req["id"],
        }
    )

    assert created["secret"]["tags"] == ["new"]
    with api._vault_engine().connect() as conn:
        rows = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_requests.c.id, vault_requests.c.status).where(
                    vault_requests.c.id.in_([old_req["id"], new_req["id"]])
                )
            ).mappings()
        }
    assert rows == {old_req["id"]: "fulfilled", new_req["id"]: "fulfilled"}


def test_create_secret_with_fulfilled_provision_request_returns_secret_exists(monkeypatch):
    seal = Mock(side_effect=[_sealed("first"), _sealed("second")])
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(conn, "GH_TOKEN")

    payload = {
        "name": "GH_TOKEN",
        "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        "provision_request_id": req["id"],
    }
    created = api.create_vault_secret(payload)
    assert created["ok"] is True

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({**payload, "blind_box": {**payload["blind_box"], "enc": "enc2", "ct": "ct2"}})

    assert exc.value.code == "secret_exists"
    assert exc.value.status == 409


def test_secret_exists_response_commits_stale_pending_provision_cleanup(monkeypatch):
    seal = Mock(side_effect=[_sealed("first"), _sealed("second")])
    monkeypatch.setattr(api, "avault_seal_blind_box", seal)
    with api._vault_engine().begin() as conn:
        req = vault_service.create_provision_request(conn, "GH_TOKEN")

    payload = {
        "name": "GH_TOKEN",
        "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        "provision_request_id": req["id"],
    }
    api.create_vault_secret(payload)
    with api._vault_engine().begin() as conn:
        stale = vault_service.create_provision_request(conn, "GH_TOKEN")
        conn.execute(vault_requests.update().where(vault_requests.c.id == stale["id"]).values(status="pending"))

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret({**payload, "blind_box": {**payload["blind_box"], "enc": "enc2", "ct": "ct2"}})

    assert exc.value.code == "secret_exists"
    with api._vault_engine().connect() as conn:
        stale_status = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == stale["id"])).scalar_one()
    assert stale_status == "fulfilled"


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
    assert exc.value.code == "plaintext_value_rejected"
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


def test_protected_create_establishing_vmk_rejects_second_init(monkeypatch):
    first = api.create_vault_secret(
        {
            "name": "FIRST_PROTECTED",
            "protection": "protected",
            "sealed": {"ciphertext": "ct1", "nonce": "n1", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d1"}},
            "establishing_vmk": True,
        }
    )
    assert first["ok"] is True

    # A second "establishing" create (concurrent first-time setup) must be rejected so
    # the vault key history can't split under a different VMK.
    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_secret(
            {
                "name": "SECOND_PROTECTED",
                "protection": "protected",
                "sealed": {"ciphertext": "ct2", "nonce": "n2", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d2"}},
                "establishing_vmk": True,
            }
        )
    assert exc.value.code == "vault_already_initialized"
    assert exc.value.status == 409

    # Adding a secret under the already-established VMK (not establishing) still works.
    more = api.create_vault_secret(
        {
            "name": "THIRD_PROTECTED",
            "protection": "protected",
            "sealed": {"ciphertext": "ct3", "nonce": "n3", "wrap_meta": {"v": 1, "copies": [], "wrapped_dek": "d3"}},
        }
    )
    assert more["ok"] is True


def test_get_vault_vmk_returns_latest_protected_wrap_meta(monkeypatch):
    from unittest.mock import Mock

    assert api.get_vault_vmk() == {"ok": True, "exists": False, "wrap_meta": None}

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    assert api.get_vault_vmk() == {"ok": True, "exists": False, "wrap_meta": None}

    api.create_vault_secret(
        {
            "name": "PROTECTED_OLD",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-old", "nonce": "n-old", "wrap_meta": '{"vmk":"old"}'},
        }
    )
    api.create_vault_secret(
        {
            "name": "PROTECTED_NEW",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-new", "nonce": "n-new", "wrap_meta": '{"vmk":"new"}'},
        }
    )
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "PROTECTED_OLD")
            .values(updated_at="2026-01-01T00:00:00+00:00")
        )
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == "PROTECTED_NEW")
            .values(updated_at="2026-01-02T00:00:00+00:00")
        )

    assert api.get_vault_vmk() == {"ok": True, "exists": True, "wrap_meta": '{"vmk":"new"}'}


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
    # An installed avault that predates the P2 surface (below AVAULT_P2_MIN_VERSION),
    # paired with a managed pin that also lacks P2.
    installed_pre_p2 = "0.1.2"
    assert not api._version_at_least(installed_pre_p2, api.AVAULT_P2_MIN_VERSION)
    monkeypatch.setattr(api, "avault_status", lambda: {"installed": True, "version": installed_pre_p2})
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


def test_agent_access_request_and_standard_always_ask_grant_api(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    agent_grant = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"always_ask": True},
        }
    )

    requested = api.request_vault_access(
        {
            "name": "STANDARD_KEY",
            "session_id": "ses_1",
            "command": "python sync.py",
            "egress": "local child process",
        }
    )

    assert requested["ok"] is True
    assert requested["request"]["request_type"] == "access"
    assert requested["request"]["card"]["grant_options"]
    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": requested["request"]["id"],
        }
    )
    assert created["grant"]["member_snapshot"] == ["STANDARD_KEY"]
    assert created["grant"]["delivery_ready"] is True
    assert created["grant"]["delivery_status"] == "standard_ready"
    assert created["grant"]["one_shot"] is True
    agent_grant.assert_not_called()
    with api._vault_engine().begin() as conn:
        grant_row = conn.execute(
            select(vault_service.vault_grants).where(vault_service.vault_grants.c.id == created["grant"]["id"])
        ).mappings().one()
        release_scopes = vault_service.agent_release_scopes_after_rows(conn, [dict(grant_row)])
    assert int(grant_row["agent_ready"] or 0) == 0
    assert release_scopes == []

    fetched = api.get_vault_request(requested["request"]["id"])
    assert fetched["request"]["status"] == "approved"
    assert fetched["result"]["type"] == "grant"
    assert fetched["result"]["grant"]["id"] == created["grant"]["id"]


def test_agent_access_request_and_tag_standard_always_ask_grant_api(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    agent_grant = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    for name in ("ASK_A", "ASK_B"):
        api.create_vault_secret(
            {
                "name": name,
                "tags": ["deploy"],
                "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "policy": {"always_ask": True},
            }
        )
    api.create_vault_secret(
        {
            "name": "NORMAL_DEPLOY",
            "tags": ["deploy"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )

    requested = api.request_vault_access({"source_selector": {"tags": ["deploy"]}, "session_id": "ses_1"})

    assert requested["ok"] is True
    option = requested["request"]["card"]["grant_options"][0]
    assert option["source_selector"] == {"tags": ["deploy"]}
    assert option["member_snapshot"] == ["ASK_A", "ASK_B"]
    assert option["one_shot"] is True
    created = api.create_vault_grant(
        {
            "session_id": "ses_1",
            "request_id": requested["request"]["id"],
        }
    )
    assert created["grant"]["member_snapshot"] == ["ASK_A", "ASK_B"]
    assert created["grant"]["delivery_status"] == "standard_ready"
    assert created["grant"]["one_shot"] is True
    agent_grant.assert_not_called()


def test_agent_access_request_does_not_return_protected_tag_unlock_material(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "tags": ["crypto"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "tags": ["crypto"],
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )

    requested = api.request_vault_access({"source_selector": {"tags": ["crypto"]}, "session_id": "ses_1"})

    _assert_no_unlock_material(requested["request"]["card"])
    assert requested["request"]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_KEY"]


def test_agent_access_request_ignores_client_source_for_hydration(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "STANDARD_KEY",
            "tags": ["crypto"],
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "tags": ["crypto"],
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )

    requested = api.request_vault_access({"source_selector": {"tags": ["crypto"]}, "session_id": "ses_1", "source": "ui"})

    assert requested["request"]["requester"]["source"] == "agent-cli"
    _assert_no_unlock_material(requested["request"]["card"])


def test_request_vault_access_malformed_selector_returns_vault_api_error():
    with pytest.raises(api.VaultApiError) as exc:
        api.request_vault_access({"source_selector": {"tags": "deploy"}, "session_id": "ses_1"})

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409


def test_request_vault_access_reports_missing_selector_member_name():
    with pytest.raises(api.VaultApiError) as exc:
        api.request_vault_access({"source_selector": {"env": ["MISSING_KEY"]}, "session_id": "ses_1"})

    assert exc.value.code == "secret_not_found"
    assert exc.value.status == 404
    assert "MISSING_KEY" in str(exc.value)


def test_agent_access_sibling_request_result_returns_covering_grant(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 1, "ttl_secs": 300}))
    api.create_vault_secret(
        {
            "name": "A_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-a", "nonce": "n-a", "wrap_meta": "wm-a"},
        }
    )
    req_a = api.request_vault_access({"name": "A_KEY", "session_id": "ses_1"})
    req_b = api.request_vault_access({"name": "A_KEY", "session_id": "ses_1"})
    created = api.create_vault_grant(
        {
            "request_id": req_a["request"]["id"],
            "session_id": "ses_1",
            "deks": [
                {
                    "name": "A_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        }
    )

    fetched = api.get_vault_request(req_b["request"]["id"])

    assert fetched["request"]["status"] == "approved"
    assert fetched["result"]["type"] == "grant"
    assert fetched["result"]["grant"]["id"] == created["grant"]["id"]


def test_agent_access_selector_sibling_request_result_returns_covering_grant(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "avault_agent_grant", Mock(return_value={"granted": 2, "ttl_secs": 300}))
    for name in ("A_KEY", "B_KEY"):
        api.create_vault_secret(
            {
                "name": name,
                "protection": "protected",
                "tags": ["deploy"],
                "sealed": {
                    "ciphertext": f"ct-{name.lower()}",
                    "nonce": f"n-{name.lower()}",
                    "wrap_meta": f"wm-{name.lower()}",
                },
            }
        )
    req_a = api.request_vault_access({"source_selector": {"tags": ["deploy"]}, "session_id": "ses_1"})
    req_b = api.request_vault_access({"source_selector": {"tags": ["deploy"]}, "session_id": "ses_1"})
    created = api.create_vault_grant(
        {
            "request_id": req_a["request"]["id"],
            "session_id": "ses_1",
            "deks": [
                {
                    "name": "A_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc-a", "ct": "ct-a"},
                    "approval": {"nonce": "bm9uY2UtYQ==", "expires_at_unix": 4102444800},
                },
                {
                    "name": "B_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc-b", "ct": "ct-b"},
                    "approval": {"nonce": "bm9uY2UtYg==", "expires_at_unix": 4102444800},
                },
            ],
        }
    )

    fetched = api.get_vault_request(req_b["request"]["id"])

    assert req_b["request"]["secret_name"] is None
    assert fetched["request"]["status"] == "approved"
    assert fetched["result"]["type"] == "grant"
    assert fetched["result"]["grant"]["id"] == created["grant"]["id"]


def test_get_vault_request_expires_timed_out_pending_request(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1", "request_ttl_seconds": 1})
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_requests.update()
            .where(vault_service.vault_requests.c.id == requested["request"]["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )

    fetched = api.get_vault_request(requested["request"]["id"])

    assert fetched["request"]["status"] == "expired"
    assert "result" not in fetched
    with api._vault_engine().connect() as conn:
        stored = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
    assert stored == "expired"


def test_get_vault_requests_expires_and_commits_timed_out_pending_request(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1", "request_ttl_seconds": 1})
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_requests.update()
            .where(vault_service.vault_requests.c.id == requested["request"]["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )

    listed = api.get_vault_requests(status="pending")

    assert listed["requests"] == []
    with api._vault_engine().connect() as conn:
        stored = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
    assert stored == "expired"


def test_get_vault_request_does_not_hydrate_protected_pending_unlock_material(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("standard")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    with api._vault_engine().begin() as conn:
        request = vault_service.create_access_request(
            conn,
            "PROTECTED_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    fetched = api.get_vault_request(request["id"])

    assert fetched["request"]["status"] == "pending"
    _assert_no_unlock_material(fetched["request"]["card"])


def test_agent_access_request_accepts_protected_static(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("protected")))
    api.create_vault_secret({"name": "PROTECTED_KEY", "protection": "protected", "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"}})

    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    assert requested["ok"] is True
    assert requested["request"]["secret_name"] == "PROTECTED_KEY"
    assert requested["request"]["card"]["protection"] == "protected"
    _assert_no_unlock_material(requested["request"])


def test_deny_vault_request_api(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("api")))
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    denied = api.deny_vault_request(requested["request"]["id"], {"reason": "not now", "requester": {"source": "test"}})

    assert denied["ok"] is True
    assert denied["request"]["status"] == "denied"
    with pytest.raises(api.VaultApiError) as exc:
        api.deny_vault_request(requested["request"]["id"])
    assert exc.value.code == "invalid_request"


def test_agent_sign_request_approved_via_avault_sign(monkeypatch):
    sign = Mock(return_value={"signature": "ab" * 64, "recovery_id": 1})
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
    requested = api.request_vault_sign(
        {"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable", "session_id": "ses_1"}
    )

    completed = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": "00" * 32,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": requested["request"]["id"],
        }
    )

    assert completed["ok"] is True
    assert completed["request"]["status"] == "approved"
    assert completed["request"]["delivery"]["signature"] == {"signature": "ab" * 64, "recovery_id": 1}
    assert api.get_vault_request(requested["request"]["id"])["result"]["signature"] == {"signature": "ab" * 64, "recovery_id": 1}
    sign.assert_called_once_with(_sealed("key"), "00" * 32, "ecdsa-secp256k1-recoverable", name="ETH_KEY")


def test_agent_sign_claims_request_before_avault_sign(monkeypatch):
    def sign_after_attempted_deny(*_args, **_kwargs):
        with api._vault_engine().begin() as conn:
            status = conn.execute(
                select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.secret_name == "ETH_KEY")
            ).scalar_one()
            assert status == "signing"
            request_id = conn.execute(
                select(vault_service.vault_requests.c.id).where(vault_service.vault_requests.c.secret_name == "ETH_KEY")
            ).scalar_one()
            with pytest.raises(vault_service.InvalidRequestError):
                vault_service.deny_request(conn, request_id)
        return {"signature": "ab" * 64, "recovery_id": 1}

    monkeypatch.setattr(api, "avault_sign", Mock(side_effect=sign_after_attempted_deny))
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_sign(
        {"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable", "session_id": "ses_1"}
    )

    completed = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": "00" * 32,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": requested["request"]["id"],
        }
    )

    assert completed["request"]["status"] == "approved"


def test_agent_sign_request_accepts_protected_browser_signature(monkeypatch):
    sign = Mock()
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest)
    api.create_vault_secret(
        {
            "name": "PROTECTED_ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct-key", "nonce": "n-key", "wrap_meta": "wm-key"},
            "public_meta": public_meta,
        }
    )
    requested = api.request_vault_sign(
        {
            "name": "PROTECTED_ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "session_id": "ses_1",
        }
    )

    completed = api.vault_sign(
        {
            "name": "PROTECTED_ETH_KEY",
            "digest": digest,
            "scheme": "ecdsa-secp256k1-recoverable",
            "request_id": requested["request"]["id"],
            "signature": signature,
        }
    )

    assert requested["ok"] is True
    assert requested["request"]["card"]["protection"] == "protected"
    assert completed["ok"] is True
    assert completed["signature"] == signature
    assert api.get_vault_request(requested["request"]["id"])["result"] == {"type": "signature", "signature": signature}
    sign.assert_not_called()
    with api._vault_engine().connect() as conn:
        meta = vault_service.get_secret_meta(conn, "PROTECTED_ETH_KEY")
    assert meta["use_count"] == 1


def test_agent_sign_marks_claimed_request_failed_when_signature_validation_rejects(monkeypatch):
    monkeypatch.setattr(api, "avault_sign", Mock(return_value={"signature": "ab", "recovery_id": 1}))
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("key")))
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "kind": "keypair",
            "signer_kind": "local",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_sign(
        {"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable", "session_id": "ses_1"}
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": "00" * 32,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": requested["request"]["id"],
            }
        )

    assert exc.value.code == "invalid_request"
    fetched = api.get_vault_request(requested["request"]["id"])
    assert fetched["request"]["status"] == "failed"
    assert fetched["request"]["delivery"]["failure"] == {"request_type": "sign", "reason": "signature_rejected"}


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
            "session_id": "ses_1",
            "ttl_seconds": 300,
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
    agent_release.assert_called_once_with(grant_id=created["grant"]["id"])


def test_fulfill_access_request_relays_only_browser_dek_blindbox(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    validate_pubkey = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "validate_avault_agent_pubkey", validate_pubkey)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})
    grant_id = requested["request"]["card"]["grant_options"][0]["grant_id"]

    fulfilled = api.fulfill_vault_access_request(
        requested["request"]["id"],
        {
            "grant_id": grant_id,
            "session_id": "ses_1",
            "ttl_seconds": 300,
            "agent_pubkey": {"public_key": "pk", "fingerprint": "fp"},
            "deks": [
                {
                    "name": "PROTECTED_KEY",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        },
    )

    assert fulfilled["ok"] is True
    assert fulfilled["result"]["type"] == "grant"
    assert fulfilled["grant"]["id"] == grant_id
    assert agent_grant.call_args.kwargs["grant_id"] == grant_id
    assert fulfilled["grant"]["delivery_ready"] is True
    assert agent_grant.call_args.kwargs["deks"] == [
        {
            "name": "PROTECTED_KEY",
            "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
        }
    ]
    with api._vault_engine().connect() as conn:
        resolved = vault_service.resolve_secret_access(conn, "PROTECTED_KEY", session_id="ses_1", create_request=False)
    assert resolved["status"] == "agent_delivery_ready"
    validate_pubkey.assert_called_once_with({"public_key": "pk", "fingerprint": "fp"})
    encoded = json.dumps({"fulfilled": fulfilled, "agent_deks": agent_grant.call_args.kwargs["deks"]})
    assert "raw-dek-must-not-cross-python-agent-boundary" not in encoded
    assert "plaintext-must-not-cross-python-agent-boundary" not in encoded


def test_fulfill_protected_always_ask_access_uses_one_shot_resident_agent_grant(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    validate_pubkey = Mock()
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    monkeypatch.setattr(api, "validate_avault_agent_pubkey", validate_pubkey)
    api.create_vault_secret(
        {
            "name": "PROTECTED_ASK",
            "protection": "protected",
            "policy": {"always_ask": True},
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_ASK", "session_id": "ses_1"})

    fulfilled = api.fulfill_vault_access_request(
        requested["request"]["id"],
        {
            "session_id": "ses_1",
            "agent_pubkey": {"public_key": "pk", "fingerprint": "fp"},
            "deks": [
                {
                    "name": "PROTECTED_ASK",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        },
    )

    assert requested["request"]["card"]["one_shot"] is True
    assert requested["request"]["card"]["grant_options"][0]["member_snapshot"] == ["PROTECTED_ASK"]
    assert fulfilled["grant"]["one_shot"] is True
    assert fulfilled["grant"]["source_selector"] == {"env": ["PROTECTED_ASK"]}
    assert fulfilled["grant"]["delivery_ready"] is True
    validate_pubkey.assert_called_once_with({"public_key": "pk", "fingerprint": "fp"})
    assert agent_grant.call_args.kwargs["deks"] == [
        {
            "name": "PROTECTED_ASK",
            "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
        }
    ]


def test_always_ask_grant_api_maps_stale_request_preflight_to_json_error(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "policy": {"always_ask": True},
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_access({"name": "ASK_KEY", "session_id": "ses_1"})
    api.deny_vault_request(requested["request"]["id"])

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
            }
        )

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409


def test_always_ask_grant_api_commits_expired_preflight_request(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "policy": {"always_ask": True},
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
        }
    )
    requested = api.request_vault_access({"name": "ASK_KEY", "session_id": "ses_1"})
    with api._vault_engine().begin() as conn:
        conn.execute(
            vault_service.vault_requests.update()
            .where(vault_service.vault_requests.c.id == requested["request"]["id"])
            .values(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
                "session_id": "ses_1",
                "request_id": requested["request"]["id"],
            }
        )

    assert exc.value.code == "invalid_request"
    assert exc.value.status == 409
    with api._vault_engine().connect() as conn:
        stored = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
    assert stored == "expired"


def test_fulfill_access_request_rejects_dek_or_plaintext_material(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    with pytest.raises(api.VaultApiError) as exc:
        api.fulfill_vault_access_request(
            requested["request"]["id"],
            {
                "session_id": "ses_1",
                "deks": [
                    {
                        "name": "PROTECTED_KEY",
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                        "dek": "raw-dek-must-not-cross-python-agent-boundary",
                        "value": "plaintext-must-not-cross-python-agent-boundary",
                    }
                ],
            },
        )

    assert exc.value.code == "invalid_grant"
    assert "opaque blind-box metadata" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_fulfill_access_request_rejects_raw_material_in_deks_by_secret(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    with pytest.raises(api.VaultApiError) as exc:
        api.fulfill_vault_access_request(
            requested["request"]["id"],
            {
                "session_id": "ses_1",
                "deks_by_secret": {
                    "PROTECTED_KEY": {
                        "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                        "dek": "raw-dek-must-not-cross-python-agent-boundary",
                        "value": "plaintext-must-not-cross-python-agent-boundary",
                    }
                },
            },
        )

    assert exc.value.code == "invalid_grant"
    assert "opaque blind-box metadata" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_fulfill_access_request_rejects_raw_material_nested_in_dek_blindbox(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_KEY",
            "protection": "protected",
            "sealed": {"ciphertext": "ct-protected", "nonce": "n-protected", "wrap_meta": "wm-protected"},
        }
    )
    requested = api.request_vault_access({"name": "PROTECTED_KEY", "session_id": "ses_1"})

    with pytest.raises(api.VaultApiError) as exc:
        api.fulfill_vault_access_request(
            requested["request"]["id"],
            {
                "session_id": "ses_1",
                "deks": [
                    {
                        "name": "PROTECTED_KEY",
                        "dek_blindbox": {
                            "scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1",
                            "enc": "enc",
                            "ct": "ct",
                            "dek": "raw-dek-must-not-cross-python-agent-boundary",
                        },
                        "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                    }
                ],
            },
        )

    assert exc.value.code == "invalid_grant"
    assert "opaque blind-box metadata" in str(exc.value)
    agent_grant.assert_not_called()
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == requested["request"]["id"])
        ).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert grants == []


def test_fulfill_access_request_rejects_protected_keypair_value_delivery(monkeypatch, avault_p2):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_grant = Mock(return_value={"granted": 1, "ttl_secs": 300})
    monkeypatch.setattr(api, "avault_agent_grant", agent_grant)
    api.create_vault_secret(
        {
            "name": "PROTECTED_ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.request_vault_access({"name": "PROTECTED_ETH_KEY", "session_id": "ses_1"})

    assert exc.value.code == "keypair_not_value_deliverable"
    agent_grant.assert_not_called()


def test_revoke_grant_releases_only_that_grant_id(monkeypatch):
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
        grant_1 = _grant_from_request(conn, req_1)
        req_2 = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = _grant_from_request(conn, req_2)

    revoked = api.revoke_vault_grant(grant_1["id"])

    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(grant_id=grant_1["id"])
    with api._vault_engine().connect() as conn:
        assert vault_service.find_active_grant_for_secret(conn, "GRANT_KEY", session_id="ses_2")["id"] == grant_2["id"]


def test_consume_one_shot_releases_only_that_grant_id(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "protection": "protected",
            "policy": {"always_ask": True},
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    with api._vault_engine().begin() as conn:
        req_1 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant_1 = _grant_from_request(conn, req_1)
        req_2 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = _grant_from_request(conn, req_2)

    api.consume_one_shot_grants([grant_1], reason="test")

    agent_release.assert_called_once_with(grant_id=grant_1["id"])
    with api._vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_service.vault_grants.c.id, vault_service.vault_grants.c.status).where(
                    vault_service.vault_grants.c.id.in_([grant_1["id"], grant_2["id"]])
                )
            ).mappings()
        }
        active = vault_service.find_active_grant_for_secret(conn, "ASK_KEY", session_id="ses_2")
    assert statuses[grant_1["id"]] == "expired"
    assert statuses[grant_2["id"]] == "active"
    assert active["id"] == grant_2["id"]
    assert active["delivery_ready"] is True


def test_forced_agent_grant_cleanup_treats_reserved_grant_as_live(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "protection": "protected",
            "policy": {"always_ask": True},
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
        }
    )
    with api._vault_engine().begin() as conn:
        req_1 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )
        grant_1 = _grant_from_request(conn, req_1)
        req_2 = vault_service.create_access_request(
            conn,
            "ASK_KEY",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
        )
        grant_2 = _grant_from_request(conn, req_2, session_id="ses_2")
        reserved = vault_service.resolve_secret_access(
            conn,
            "ASK_KEY",
            session_id="ses_2",
            requester={"session_id": "ses_2"},
            delivery={"session_id": "ses_2"},
            reserve_one_shot=True,
        )

    assert reserved["grant"]["id"] == grant_2["id"]
    assert reserved["grant"]["status"] == "reserved"

    api._cleanup_failed_agent_grant(
        engine=api._vault_engine(),
        grant=grant_1,
        reason="test",
        force_release_scope=True,
    )

    agent_release.assert_called_once_with(grant_id=grant_1["id"])
    with api._vault_engine().connect() as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute(
                select(vault_service.vault_grants.c.id, vault_service.vault_grants.c.status).where(
                    vault_service.vault_grants.c.id.in_([grant_1["id"], grant_2["id"]])
                )
            ).mappings()
        }
    assert statuses == {grant_1["id"]: "expired", grant_2["id"]: "reserved"}


def test_revoke_tag_grant_releases_its_grant_id(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    agent_release = Mock(return_value={"released": True})
    monkeypatch.setattr(api, "avault_agent_release", agent_release)
    api.create_vault_secret(
        {
            "name": "A_KEY",
            "protection": "protected",
            "tags": ["crypto"],
            "sealed": {"ciphertext": "ct-a", "nonce": "n-a", "wrap_meta": "wm-a"},
        }
    )
    with api._vault_engine().begin() as conn:
        req_narrow = vault_service.create_access_request(
            conn,
            "A_KEY",
            requester={"session_id": "ses_narrow"},
            delivery={"session_id": "ses_narrow"},
        )
        _grant_from_request(conn, req_narrow, session_id="ses_narrow")
        vault_service.create_secret(conn, name="B_KEY", protection="protected", tags=["crypto"], sealed=_sealed("b"))
        req_tag = vault_service.create_access_request(
            conn,
            source_selector={"tags": ["crypto"]},
            requester={"session_id": "ses_tag"},
            delivery={"session_id": "ses_tag"},
        )
        tag_grant = _grant_from_request(conn, req_tag, session_id="ses_tag")

    revoked = api.revoke_vault_grant(tag_grant["id"])

    assert revoked["grant"]["status"] == "revoked"
    agent_release.assert_called_once_with(grant_id=tag_grant["id"])


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
        grant = _grant_from_request(conn, req, session_id="ses_1")

    removed = api.delete_vault_secret("GRANT_KEY")

    assert removed["removed"] is True
    agent_release.assert_called_once_with(grant_id=grant["id"])


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
        grant = _grant_from_request(conn, req)

    class Manager:
        def __init__(self) -> None:
            self.socket_path = socket_path
            self.reset = Mock()

    manager = Manager()
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: manager)
    monkeypatch.setattr(api, "avault_agent_release", Mock(side_effect=api.AvaultError("timed out waiting for release")))
    try:
        api.release_vault_agent_scopes([{"grant_id": grant["id"]}], reason="test")
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

    api.release_vault_agent_scopes([{"grant_id": "vgr_missing"}], reason="test")

    manager.reset.assert_not_called()


def test_agent_grant_rejects_pubkey_mismatch(monkeypatch):
    agent_client = Mock()
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_client", lambda: agent_client)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})

    with pytest.raises(api.AvaultError, match="fingerprint mismatch"):
        api.avault_agent_grant(
            grant_id="vgr_grant",
            purpose="run",
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


def test_agent_grant_uses_deliver_custody_purpose(monkeypatch):
    agent_client = Mock()
    agent_client.grant.return_value = {"granted": 1}
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_client", lambda: agent_client)
    monkeypatch.setattr(api, "avault_agent_pubkey", lambda: {"public_key": "current-pk", "fingerprint": "current-fp"})

    assert api.avault_agent_grant(
        grant_id="vgr_grant",
        purpose="run",
        ttl_secs=300,
        deks=[
            {
                "name": "GRANT_KEY",
                "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
            }
        ],
    ) == {"granted": 1}
    assert agent_client.grant.call_args.kwargs["purpose"] == "deliver"


def test_agent_deliver_run_reuses_resident_agent_socket(monkeypatch):
    seen_timeout = []
    seen_kwargs = []

    class FakeClient:
        def deliver_run(self, **kwargs):
            seen_kwargs.append(kwargs)
            return {"exit_code": 7}

    class FakeManager:
        def client(self, *, timeout=None):
            seen_timeout.append(timeout)
            return FakeClient()

    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    result = api.avault_agent_deliver_run(
        grant_id="vgr_grant",
        secrets=[{"name": "GRANT_KEY", "env": "GRANT_KEY", "envelope": _sealed(), "tier": "protected"}],
        command=["python3", "-c", "pass"],
    )

    assert result == {"exit_code": 7}
    assert seen_timeout == [None]
    assert seen_kwargs == [
        {
            "grant_id": "vgr_grant",
            "command": ["python3", "-c", "pass"],
            "secrets": [
                {
                    "name": "GRANT_KEY",
                    "env": "GRANT_KEY",
                    "envelope": {"ciphertext": "ct-1", "nonce": "n-1", "wrap_meta": "wm-1"},
                    "tier": "protected",
                }
            ],
            "context": None,
        }
    ]


def test_agent_deliver_run_treats_connect_failure_as_pre_handoff(monkeypatch):
    from vibe.avault_agent import AvaultAgentError

    class FakeManager:
        def client(self, *, timeout=None):
            raise AvaultAgentError("failed to connect to avault agent: [Errno 2] No such file or directory")

    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    with pytest.raises(api.AvaultPreHandoffError):
        api.avault_agent_deliver_run(
            grant_id="vgr_grant",
            secrets=[{"name": "GRANT_KEY", "env": "GRANT_KEY", "envelope": _sealed()}],
            command=["python3", "-c", "pass"],
        )


def test_agent_deliver_fetch_uses_finite_timeout(monkeypatch):
    seen_timeout = []

    class FakeClient:
        def deliver_fetch(self, **kwargs):
            return {"status": 200, "headers": {}, "body": "ok"}

    class FakeManager:
        def client(self, *, timeout=None):
            seen_timeout.append(timeout)
            return FakeClient()

    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)
    monkeypatch.setattr(api, "_avault_agent_manager", lambda: FakeManager())

    result = api.avault_agent_deliver_fetch(
        grant_id="vgr_grant",
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
    assert status == "pending"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(grant_id=grants[0]["id"])


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
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(grant_id=grants[0]["id"])


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
    with api._vault_engine().connect() as conn:
        status = conn.execute(select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == req["id"])).scalar_one()
        grants = vault_service.list_grants(conn, status=None)
    assert status == "pending"
    assert len(grants) == 1
    assert grants[0]["status"] == "expired"
    assert grants[0]["delivery_ready"] is False
    agent_release.assert_called_once_with(grant_id=grants[0]["id"])


def test_create_grant_api_releases_failed_grant_id_without_touching_existing_grant(monkeypatch, avault_p2):
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
        existing_grant = _grant_from_request(conn, existing_req, session_id="ses_existing")
        req = vault_service.create_access_request(
            conn,
            "GRANT_KEY",
            requester={"session_id": "ses_1"},
            delivery={"session_id": "ses_1"},
        )

    with pytest.raises(api.VaultApiError) as exc:
        api.create_vault_grant(
            {
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
        grants = {grant["id"]: grant for grant in vault_service.list_grants(conn, status=None)}
    assert grants[existing_grant["id"]]["status"] == "active"
    expired = [grant for grant in grants.values() if grant["id"] != existing_grant["id"]]
    assert len(expired) == 1
    assert expired[0]["status"] == "expired"
    agent_release.assert_called_once_with(grant_id=expired[0]["id"])


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


def test_create_grant_api_binds_one_shot_to_request_session_when_unbound_requested(monkeypatch):
    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed("ask")))
    api.create_vault_secret(
        {
            "name": "ASK_KEY",
            "blind_box": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
            "policy": {"always_ask": True},
        }
    )
    requested = api.request_vault_access({"name": "ASK_KEY", "session_id": "ses_1"})

    created = api.create_vault_grant(
        {
            "request_id": requested["request"]["id"],
            "this_session_only": False,
        }
    )

    assert created["grant"]["one_shot"] is True
    assert created["grant"]["session_id"] == "ses_1"
    with api._vault_engine().begin() as conn:
        assert vault_service.find_active_grant_for_secret(conn, "ASK_KEY", session_id="other") is None
        assert vault_service.find_active_grant_for_secret(conn, "ASK_KEY", session_id="ses_1")["id"] == created["grant"]["id"]


def test_protected_sign_requires_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    result = api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})
    assert result["ok"] is False
    assert result["code"] == "browser_signature_required"
    assert result["request"]["card"]["request_type"] == "sign"
    assert result["request"]["card"]["grant_options"] == []


def test_protected_sign_request_requires_pinned_signing_public_key(monkeypatch):
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
        api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "ecdsa-secp256k1-recoverable"})

    assert exc.value.code == "invalid_request"
    assert "per-use signable" in str(exc.value)
    assert api.get_vault_requests()["requests"] == []


def test_protected_sign_rejects_unsupported_scheme_before_request(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign({"name": "ETH_KEY", "digest": "00" * 32, "scheme": "not-a-real-scheme"})

    assert exc.value.code == "invalid_request"
    assert api.get_vault_requests()["requests"] == []


def test_protected_sign_completion_requires_matching_request(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
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
            "signature": signature,
        }
    )
    assert result["ok"] is True
    assert result["request"]["status"] == "approved"


def test_protected_sign_completion_rejects_malformed_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    public_meta, _signature = _browser_ecdsa_signature_for_digest("00" * 32)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
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


def test_protected_sign_completion_rejects_signature_extra_fields(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    pending = api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable"})

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": {**signature, "private_key": "raw", "dek": "raw"},
            }
        )

    assert exc.value.code == "invalid_request"
    assert "unsupported fields" in str(exc.value)
    with api._vault_engine().connect() as conn:
        row = conn.execute(
            select(vault_service.vault_requests).where(vault_service.vault_requests.c.id == pending["request"]["id"])
        ).mappings().one()
    assert row["status"] == "pending"
    assert "signature" not in json.loads(row["delivery"])


def test_protected_sign_completion_rejects_signature_that_does_not_verify(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, _valid_signature = _browser_ecdsa_signature_for_digest(digest, key_value=1)
    _other_public_meta, invalid_signature = _browser_ecdsa_signature_for_digest(digest, key_value=2)
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    pending = api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable"})

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": invalid_signature,
            }
        )

    assert exc.value.code == "invalid_request"
    assert "does not verify" in str(exc.value)
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == pending["request"]["id"])
        ).scalar_one()
    assert status == "pending"


def test_protected_sign_completion_rejects_wrong_recovery_id(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "00" * 32
    public_meta, signature = _browser_ecdsa_signature_for_digest(digest, key_value=1)
    bad_signature = dict(signature)
    bad_signature["recovery_id"] = (int(signature["recovery_id"]) + 1) % 4
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": public_meta,
        }
    )
    pending = api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "ecdsa-secp256k1-recoverable"})

    with pytest.raises(api.VaultApiError) as exc:
        api.vault_sign(
            {
                "name": "ETH_KEY",
                "digest": digest,
                "scheme": "ecdsa-secp256k1-recoverable",
                "request_id": pending["request"]["id"],
                "signature": bad_signature,
            }
        )

    assert exc.value.code == "invalid_request"
    assert "recovery_id" in str(exc.value)
    with api._vault_engine().connect() as conn:
        status = conn.execute(
            select(vault_service.vault_requests.c.status).where(vault_service.vault_requests.c.id == pending["request"]["id"])
        ).scalar_one()
    assert status == "pending"


def test_protected_sign_completion_verifies_schnorr_browser_signature(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(api, "avault_seal_blind_box", Mock(return_value=_sealed()))
    digest = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    signature = {
        "signature": "931a3386e9ec69fe1471ba85933640948c0296a79ce2d3801ad5a4d9353550aeb0a5e80358b68088bda70b46e6b77a640c1216826f96292e5799ba2bb7bf1342",
    }
    api.create_vault_secret(
        {
            "name": "ETH_KEY",
            "protection": "protected",
            "kind": "keypair",
            "signer_kind": "local",
            "sealed": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            "public_meta": {
                "signing_public_key": {
                    "curve": "secp256k1",
                    "public_key": "024e3b81af9c2234cad09d679ce6035ed1392347ce64ce405f5dcd36228a25de6e",
                }
            },
        }
    )
    pending = api.vault_sign({"name": "ETH_KEY", "digest": digest, "scheme": "schnorr-secp256k1-bip340"})

    result = api.vault_sign(
        {
            "name": "ETH_KEY",
            "digest": digest,
            "scheme": "schnorr-secp256k1-bip340",
            "request_id": pending["request"]["id"],
            "signature": signature,
        }
    )

    assert result["ok"] is True
    assert result["request"]["status"] == "approved"


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

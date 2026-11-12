"""Staged signing-policy edits may raise confirmation but cannot lower it.

The policy owner is storage (also called without HTTP); REST must preserve that
refusal and atomicity when a browser submits display edits in the same payload.
No test invokes custody or reads real Vault state.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import resource_access_service, vault_service
from storage.models import vault_audit, vault_secrets
from storage.vault_crypto import Sealed
from tests.ui_server_test_helpers import csrf_headers
from vibe import api


def create_key(*, protection="standard", signer_kind="local", kind="keypair"):
    with api._vault_engine().begin() as conn:
        return vault_service.create_secret(
            conn, name="POLICY_KEY", kind=kind, protection=protection,
            signer_kind=signer_kind if kind == "keypair" else None,
            sealed=Sealed(ciphertext="test-ciphertext", nonce="test-nonce", wrap_meta="test-wrap"),
            description="Original", policy={"allowed_hosts": ["original.example"]},
        )


def snapshot():
    with api._vault_engine().connect() as conn:
        return (
            [dict(row) for row in conn.execute(select(vault_secrets)).mappings()],
            [dict(row) for row in conn.execute(select(vault_audit)).mappings()],
        )


@pytest.mark.parametrize("protection", ["standard", "protected"])
@pytest.mark.parametrize("signer_kind", [None, "local"])
def test_storage_keypair_policy_is_monotonic_and_preserves_envelope(protection, signer_kind):
    create_key(protection=protection, signer_kind=signer_kind)
    before, _ = snapshot()
    with api._vault_engine().begin() as conn:
        unchanged = vault_service.update_secret_metadata(conn, "POLICY_KEY", always_ask=False)
        assert not unchanged["policy"].get("always_ask", False)
        updated = vault_service.update_secret_metadata(
            conn, "POLICY_KEY", always_ask=True, description="Raised",
            policy={"allowed_hosts": ["new.example"]},
        )
        assert updated["policy"] == {"allowed_hosts": ["new.example"], "always_ask": True}
        assert vault_service.sign_needs_approval(conn, "POLICY_KEY") is True
    raised = snapshot()
    for field in ("id", "ciphertext", "nonce", "wrap_meta", "kind", "protection", "signer_kind", "updated_at"):
        assert raised[0][0][field] == before[0][field]
    assert json.loads(raised[1][-1]["delivery"])["fields"] == ["description", "policy", "always_ask"]
    with api._vault_engine().begin() as conn:
        vault_service.update_secret_metadata(conn, "POLICY_KEY", always_ask=True)
    assert snapshot() == raised  # Same-value policy save is an ordinary-key no-op.
    with pytest.raises(vault_service.SigningPolicyDowngradeUnavailableError):
        with api._vault_engine().begin() as conn:
            vault_service.update_secret_metadata(conn, "POLICY_KEY", always_ask=False, description="Must roll back")
    assert snapshot() == raised
    with api._vault_engine().begin() as conn:
        edited = vault_service.update_secret_metadata(conn, "POLICY_KEY", description="Still editable", tags=["wallet"])
    assert edited["description"] == "Still editable" and edited["policy"]["always_ask"] is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_invalid_boolean_cannot_modify_key_policy(value):
    create_key()
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_secret("POLICY_KEY", {"always_ask": value, "description": "Must not change"})
    assert error.value.code == "invalid_metadata"
    assert snapshot() == before


def test_policy_patch_cannot_lower_or_erase_internal_confirmation():
    create_key()
    api.update_vault_secret("POLICY_KEY", {"always_ask": True})
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_secret("POLICY_KEY", {"policy": {"always_ask": False}, "description": "Must not change"})
    assert error.value.code == "invalid_metadata"
    assert snapshot() == before
    for policy in (None, {}, {"allowed_hosts": ["another.example"]}):
        updated = api.update_vault_secret("POLICY_KEY", {"policy": policy})
        assert updated["secret"]["policy"]["always_ask"] is True


def test_static_secret_does_not_gain_keypair_policy_editor():
    create_key(kind="static")
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_secret("POLICY_KEY", {"always_ask": True})
    assert error.value.code == "invalid_metadata"
    assert snapshot() == before


def test_policy_raise_keeps_existing_management_acl():
    create_key()
    before = snapshot()
    context = resource_access_service.ResourceUserContext(instance_role="viewer", is_remote=False)
    with pytest.raises(api.VaultApiError) as error:
        api.update_vault_secret("POLICY_KEY", {"always_ask": True}, user_context=context)
    assert error.value.code == "resource_access_forbidden"
    assert snapshot() == before


def test_http_raise_and_lower_error_do_not_invoke_custody(monkeypatch):
    from vibe.ui_server import app

    create_key()
    custody = Mock(side_effect=AssertionError("metadata editing must not invoke custody"))
    monkeypatch.setattr(api, "_run_avault", custody)
    client = app.test_client()
    headers = csrf_headers(client)
    response = client.patch("/api/vault/secrets/POLICY_KEY", json={"always_ask": True}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["secret"]["policy"]["always_ask"] is True
    before = snapshot()
    response = client.patch("/api/vault/secrets/POLICY_KEY", json={"always_ask": False, "description": "No"}, headers=headers)
    assert response.status_code == 409
    assert response.get_json()["code"] == "vault_policy_downgrade_unavailable"
    assert snapshot() == before
    custody.assert_not_called()


def test_standard_raise_causes_next_sign_to_request_approval(monkeypatch):
    create_key()
    signing = Mock(side_effect=AssertionError("raised policy must request approval before custody"))
    monkeypatch.setattr(api, "avault_sign", signing)
    monkeypatch.setattr(api, "_notify_vault_request_created", Mock())
    api.update_vault_secret("POLICY_KEY", {"always_ask": True})
    response = api.vault_sign({"name": "POLICY_KEY", "digest": "01" * 32, "scheme": "ecdsa-secp256k1-recoverable"})
    assert response["code"] == "approval_required"
    assert response["request"]["status"] == "pending"
    signing.assert_not_called()


def test_racing_policy_update_is_not_overwritten_by_raise(monkeypatch):
    create_key()
    original = vault_service._require_signing_policy_transition

    def competing_update(current, requested):
        original(current, requested)
        with api._vault_engine().begin() as conn:
            vault_service.update_secret_metadata(conn, "POLICY_KEY", policy={"allowed_hosts": ["winner.example"]})

    with monkeypatch.context() as patch:
        patch.setattr(vault_service, "_require_signing_policy_transition", competing_update)
        with pytest.raises(vault_service.SecretConflictError):
            with api._vault_engine().begin() as conn:
                vault_service.update_secret_metadata(conn, "POLICY_KEY", always_ask=True)
    stored, _ = snapshot()
    assert json.loads(stored[0]["policy"]) == {"allowed_hosts": ["winner.example"]}
    updated = api.update_vault_secret("POLICY_KEY", {"always_ask": True})
    assert updated["secret"]["policy"] == {"allowed_hosts": ["winner.example"], "always_ask": True}

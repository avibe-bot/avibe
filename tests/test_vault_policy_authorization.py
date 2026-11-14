"""Fresh policy proof is exact-target, one-shot and transactional in storage.

Existing enable-only tests cannot detect a replay/ABA, an enlarged allowlist, or
partial factor consumption after audit failure. These tests use real synthetic
WebAuthn signatures and isolated DBs, not mocked verification or real custody.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from storage import resource_access_service, vault_hd, vault_policy, vault_service
from storage.models import vault_audit, vault_auth_factors, vault_operation_challenges, vault_secrets
from storage.vault_crypto import Sealed
from tests.vault_webauthn_helpers import WebAuthnTestCredential
from vibe import api

HD = json.loads((Path(__file__).parent / "fixtures/vault_signing/hd-seed-wallet.json").read_text())
RP = "sandbox.avibe.bot"
ORIGIN = "https://sandbox.avibe.bot"
NAME = "POLICY_KEY"
PUBLIC_KEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def create_target(kind="local", protection="standard", name=NAME):
    if kind == "hd-seed":
        public = {"hd_accounts": vault_hd.import_accounts(HD["import_response"]["accounts"])}
        sealed = Sealed(**HD["import_response"]["envelope"])
    else:
        public = {"signing_public_key": {"curve": "secp256k1", "public_key": PUBLIC_KEY}}
        sealed = Sealed(ciphertext="synthetic-ciphertext", nonce="synthetic-nonce", wrap_meta="synthetic-wrap")
    with api._vault_engine().begin() as conn:
        result = vault_service.create_secret(conn, name=name, kind="keypair", signer_kind=kind,
            protection=protection, sealed=sealed, public_meta=public)
        if kind == "hd-seed":
            vault_service.update_hd_account(conn, name, account=0, changes={"always_ask": True})
        else:
            vault_service.update_secret_metadata(conn, name, always_ask=True)
    return result


def enroll(credential=None):
    credential = credential or WebAuthnTestCredential(rp_id=RP, origin=ORIGIN)
    # Real registration owner; no VMK required for policy authorization.
    with api._vault_engine().begin() as conn:
        options = vault_service.create_webauthn_registration_options(conn, rp_id=RP, origin=ORIGIN)
        result = vault_service.register_webauthn_factor(conn, credential.registration_payload(
            challenge_id=options["challenge_id"], challenge_b64=options["webauthn"]["challenge"]),
            rp_id=RP, origin=ORIGIN, establishment=True)
    return credential, result["factor"]["id"]


def issue(account=None, name=NAME, user_context=None):
    with api._vault_engine().begin() as conn:
        return vault_service.create_signing_policy_challenge(conn, name,
            {"always_ask": False, **({"account": account} if account is not None else {})},
            rp_id=RP, origin=ORIGIN, user_context=user_context)["context"]


def proof(context, credential, factor_id, **kwargs):
    return {"context": context, "authorization": credential.assertion_authz(
        challenge_id=context["challengeId"], factor_id=factor_id,
        challenge_b64=base64.b64encode(vault_policy.challenge_bytes(context)).decode(), **kwargs)}


def complete(payload, name=NAME, **kwargs):
    with api._vault_engine().begin() as conn:
        return vault_service.complete_signing_policy_downgrade(conn, name, payload, rp_id=RP, origin=ORIGIN, **kwargs)


def state():
    with api._vault_engine().connect() as conn:
        return {table.name: [dict(row) for row in conn.execute(select(table)).mappings()]
                for table in (vault_secrets, vault_auth_factors, vault_operation_challenges, vault_audit)}


def stored_settings(snapshot, account=None):
    row = snapshot["vault_secrets"][0]
    return json.loads(row["policy"] or "{}") if account is None else json.loads(row["public_meta"])["hd_accounts"][account]


@pytest.fixture
def capable_policy_sandbox(monkeypatch):
    # Test-only registry entry; no shipped pin or browser capability is asserted.
    from vibe import vault_sandbox_release as release

    current = release.current_support()
    assert current is not None
    monkeypatch.setattr(release, "CAPABILITIES_BY_PIN_ID", {current.pin_id: {
        "ordinary_input_types": ("p2pkh", "p2wpkh", "p2tr"),
        "hd_input_types": (), "ecdsa_return_types": ("p2pkh", "p2wpkh", "p2tr"),
    }})
    return release.current_support()


@pytest.mark.parametrize("kind,protection", [("local", "standard"), ("local", "protected"), ("hd-seed", "standard")])
def test_real_assertion_lowers_only_bound_flag_and_consumes_everything_together(kind, protection, capable_policy_sandbox):
    create_target(kind, protection)
    credential, factor_id = enroll()
    account = 0 if kind == "hd-seed" else None
    context = issue(account)
    before = state()
    payload = proof(context, credential, factor_id)
    assert complete(payload)["ok"]
    after = state()
    assert stored_settings(after, account)["always_ask"] is False
    assert stored_settings(after, account)[vault_policy.REVISION_KEY] != stored_settings(before, account)[vault_policy.REVISION_KEY]
    for key in ("ciphertext", "nonce", "wrap_meta", "id"):
        assert after["vault_secrets"][0][key] == before["vault_secrets"][0][key]
    assert after["vault_auth_factors"][0]["sign_count"] == 2
    assert after["vault_operation_challenges"][-1]["consumed_at"]
    assert after["vault_audit"][-1]["event"] == "signing-policy-lowered"
    with pytest.raises(vault_service.SigningPolicyError) as error:
        complete(payload)
    assert error.value.code == "vault_policy_authz_used"
    assert state() == after


@pytest.mark.parametrize("mutation", ["target", "revision", "nonce", "factor", "purpose", "origin", "rp", "signature", "uv", "up", "challenge-id", "raw-id", "factor-id"])
def test_modified_context_or_proof_never_mutates_state(mutation):
    create_target()
    credential, factor_id = enroll()
    payload = proof(issue(), credential, factor_id)
    before = state()
    context, auth = payload["context"], payload["authorization"]
    if mutation == "target":
        context["target"]["addresses"]["eth"] = "0x0000000000000000000000000000000000000000"
    elif mutation == "revision":
        context["change"]["revision"] = "a" * 32
    elif mutation == "nonce":
        context["nonce"] = base64.b64encode(b"x" * 32).decode()
    elif mutation == "factor":
        context["factors"][0]["credentialId"] = "AA=="
    elif mutation == "purpose":
        context["purpose"] = "delete_secret"
    elif mutation == "origin":
        context["origin"] = "https://other.avibe.bot"
    elif mutation == "rp":
        context["rpId"] = "other.avibe.bot"
    elif mutation == "signature":
        auth["assertion"]["response"]["signature"] = "AA=="
    elif mutation in {"uv", "up"}:
        response = auth["assertion"]["response"]
        raw = bytearray(base64.b64decode(response["authenticatorData"]))
        raw[32] &= ~(4 if mutation == "uv" else 1)
        response["authenticatorData"] = base64.b64encode(raw).decode()
        # Keep the signature valid: absence of UP/UV, not a broken signature,
        # must be the reason the real verifier rejects this assertion.
        signed = bytes(raw) + hashlib.sha256(base64.b64decode(response["clientDataJSON"])).digest()
        response["signature"] = base64.b64encode(credential.private_key.sign(signed, ec.ECDSA(hashes.SHA256()))).decode()
    elif mutation == "challenge-id":
        auth["challenge_id"] = "vop_other"
    elif mutation == "raw-id":
        auth["assertion"]["rawId"] = "AA=="
    else:
        auth["factor_id"] = "vaf_other"
    with pytest.raises((vault_service.SigningPolicyError, vault_service.InvalidRequestError)):
        complete(payload)
    assert state() == before


@pytest.mark.parametrize("change", ["rotate", "rename-account", "aba", "delete-recreate", "disabled-factor", "wrong-operation", "expiry"])
def test_stale_server_state_rejects_original_valid_assertion(change):
    account = 0 if change == "rename-account" else None
    create_target("hd-seed" if account is not None else "local")
    credential, factor_id = enroll()
    context = issue(account)
    payload = proof(context, credential, factor_id, sign_count=4)
    with api._vault_engine().begin() as conn:
        if change == "rotate":
            vault_service.rotate_secret(conn, NAME, Sealed(ciphertext="replacement", nonce="other", wrap_meta="other"))
        elif change == "rename-account":
            vault_service.update_hd_account(conn, NAME, account=0, changes={"name": "Renamed"})
        elif change == "aba":
            other = vault_service.create_signing_policy_challenge(conn, NAME, {"always_ask": False}, rp_id=RP, origin=ORIGIN)["context"]
            vault_service.complete_signing_policy_downgrade(conn, NAME, proof(other, credential, factor_id), rp_id=RP, origin=ORIGIN)
            vault_service.update_secret_metadata(conn, NAME, always_ask=True)
        elif change == "delete-recreate":
            vault_service.delete_secret(conn, NAME)
        elif change == "disabled-factor":
            conn.execute(vault_auth_factors.update().values(disabled_at="disabled"))
        elif change == "wrong-operation":
            conn.execute(vault_operation_challenges.update().where(vault_operation_challenges.c.id == context["challengeId"]).values(operation="delete_secret"))
        else:
            conn.execute(vault_operation_challenges.update().where(vault_operation_challenges.c.id == context["challengeId"]).values(expires_at="2000-01-01T00:00:00.000Z"))
    if change == "delete-recreate":
        create_target()
    before = state()
    with pytest.raises(vault_service.SigningPolicyError):
        complete(payload)
    assert state() == before


def test_adding_factor_does_not_enlarge_outstanding_allowlist():
    create_target()
    first, first_id = enroll()
    context = issue()
    second = WebAuthnTestCredential(rp_id=RP, origin=ORIGIN, credential_id=b"second-factor")
    with api._vault_engine().begin() as conn:
        options = vault_service.create_webauthn_registration_options(conn, rp_id=RP, origin=ORIGIN)
        registration = second.registration_payload(challenge_id=options["challenge_id"], challenge_b64=options["webauthn"]["challenge"])
        auth = options["authorization"]
        registration["authz"] = first.assertion_authz(challenge_id=auth["challenge_id"], factor_id=first_id, challenge_b64=auth["webauthn"]["challenge"])
        result = vault_service.register_webauthn_factor(conn, registration, rp_id=RP, origin=ORIGIN)
    before = state()
    with pytest.raises(vault_service.SigningPolicyError, match="verified"):
        complete(proof(context, second, result["factor"]["id"]))
    assert state() == before
    assert complete(proof(context, first, first_id, sign_count=3))["ok"]


def test_failed_audit_rolls_back_proof_even_when_caller_catches_inside_transaction(monkeypatch):
    create_target()
    credential, factor_id = enroll()
    payload = proof(issue(), credential, factor_id)
    before = state()
    original = vault_service.audit

    def fail_audit(conn, event, **kwargs):
        if event == "signing-policy-lowered":
            raise RuntimeError("synthetic audit failure")
        return original(conn, event, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(vault_service, "audit", fail_audit)
        with api._vault_engine().begin() as conn:
            with pytest.raises(RuntimeError, match="audit failure"):
                vault_service.complete_signing_policy_downgrade(conn, NAME, payload, rp_id=RP, origin=ORIGIN)
    assert state() == before
    assert complete(payload)["ok"]


def test_simultaneous_completions_have_one_winner():
    create_target()
    credential, factor_id = enroll()
    payloads = [proof(issue(), credential, factor_id, sign_count=count) for count in (2, 3)]
    barrier = Barrier(2)

    def finish(payload):
        barrier.wait(timeout=10)
        try:
            complete(payload)
            return "ok"
        except vault_service.SigningPolicyError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, payloads))
    assert results.count("ok") == 1
    assert results.count("vault_policy_state_changed") == 1
    assert sum(row["event"] == "signing-policy-lowered" for row in state()["vault_audit"]) == 1


def test_policy_management_acl_and_missing_proof_fail_closed():
    create_target()
    credential, factor_id = enroll()
    viewer = resource_access_service.ResourceUserContext(instance_role="viewer", is_remote=False)
    with pytest.raises(vault_service.VaultSecretAccessError):
        issue(user_context=viewer)
    payload = proof(issue(), credential, factor_id)
    before = state()
    with pytest.raises(vault_service.VaultSecretAccessError):
        complete(payload, user_context=viewer)
    with pytest.raises(vault_service.SigningPolicyError) as error:
        complete({"context": payload["context"]})
    assert error.value.code == "vault_policy_authz_required"
    assert state() == before


def test_reserved_revision_cannot_be_injected_via_create_or_metadata():
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError, match="storage-owned"):
            vault_service.create_secret(conn, name=NAME, sealed=Sealed("c", "n", "w"), policy={vault_policy.REVISION_KEY: "a" * 32})
    create_target()
    before = state()
    with pytest.raises(vault_service.VaultServiceError):
        with api._vault_engine().begin() as conn:
            vault_service.update_secret_metadata(conn, NAME, policy={vault_policy.REVISION_KEY: "b" * 32})
    assert state() == before
    assert state() == before


@pytest.mark.parametrize("case,code", [
    ("never", "setup_required"), ("legacy", "legacy_registration_missing"),
    ("disabled", "factor_unavailable"), ("wrong-rp", "factor_unavailable"),
    ("bad-key", "factor_unavailable"), ("long-credential", "context_unsupported"),
])
def test_factor_readiness_does_not_reset_or_silently_omit_a_factor(case, code):
    create_target()
    if case == "legacy":
        with api._vault_engine().begin() as conn:
            vault_service.create_secret(conn, name="LEGACY", protection="protected", sealed=Sealed("c", "n", "legacy-wrap"))
    elif case != "never":
        enroll()
        with api._vault_engine().begin() as conn:
            change = {"disabled": {"disabled_at": "disabled"}, "wrong-rp": {"rp_id": "other.example"},
                      "bad-key": {"public_key": "AA=="}, "long-credential": {"credential_id": base64.b64encode(b"x" * 1024).decode()}}[case]
            conn.execute(vault_auth_factors.update().values(**change))
    before = state()
    with pytest.raises(vault_service.SigningPolicyError) as error:
        issue()
    assert error.value.code == f"vault_policy_{code}"
    assert state() == before


@pytest.mark.parametrize("case", ["client-origin", "authenticator-rp", "counter", "user-handle"])
def test_signed_but_incompatible_assertions_fail_without_consumption(case):
    create_target()
    credential, factor_id = enroll()
    context = issue()
    kwargs = {}
    if case == "client-origin":
        credential.origin = "https://other.example"
    elif case == "authenticator-rp":
        credential.rp_id = "other.example"
    elif case == "counter":
        kwargs["sign_count"] = 1
    else:
        kwargs["user_handle"] = b"unrelated-user"
    payload = proof(context, credential, factor_id, **kwargs)
    before = state()
    with pytest.raises(vault_service.SigningPolicyError) as error:
        complete(payload)
    assert error.value.code == "vault_policy_authz_invalid"
    assert state() == before


def test_policy_native_routes_return_actual_proof_errors_and_commit_once(monkeypatch):
    from tests.ui_server_test_helpers import csrf_headers
    from vibe import ui_server

    create_target()
    credential, factor_id = enroll()
    monkeypatch.setattr(ui_server, "_vault_sandbox_webauthn_origin", lambda: ORIGIN)
    client = ui_server.app.test_client()
    headers = csrf_headers(client)
    path = f"/api/vault/secrets/{NAME}/signing-policy"
    response = client.post(path + "/challenge", json={"always_ask": False}, headers=headers)
    assert response.status_code == 200
    context = response.get_json()["context"]
    before = state()
    response = client.post(path, json={"context": context}, headers=headers)
    assert response.status_code == 409 and response.get_json()["code"] == "vault_policy_authz_required"
    bad = proof(context, credential, factor_id)
    bad["authorization"]["assertion"]["response"]["signature"] = "AA=="
    response = client.post(path, json=bad, headers=headers)
    assert response.status_code == 403 and response.get_json()["code"] == "vault_policy_authz_invalid"
    assert state() == before
    payload = proof(context, credential, factor_id)
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200 and response.get_json()["secret"]["policy"]["always_ask"] is False
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 409 and response.get_json()["code"] == "vault_policy_authz_used"
    response = client.post(path + "/challenge", json={"always_ask": False, "account": True}, headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize("client_data", [[], None, True, 42, "not-an-object"])
def test_nonobject_client_data_is_invalid_proof_not_an_uncaught_server_error(client_data):
    create_target()
    credential, factor_id = enroll()
    payload = proof(issue(), credential, factor_id)
    payload["authorization"]["assertion"]["response"]["clientDataJSON"] = base64.b64encode(json.dumps(client_data).encode()).decode()
    before = state()
    with pytest.raises(vault_service.SigningPolicyError) as error:
        complete(payload)
    assert error.value.code == "vault_policy_authz_invalid"
    assert state() == before

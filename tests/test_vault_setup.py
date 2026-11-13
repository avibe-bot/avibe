"""Shared setup survives zero items and is one serialized first-write decision.

Old tests bound setup to first protected creation. They miss empty-vault reload,
seal's lingering establishing flag, competing setup candidates and legacy refusal.
All material here is synthetic; no browser, keychain, PRF or custody is invoked.
"""
from __future__ import annotations

import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from storage import vault_service, vault_setup
from storage.models import state_meta, vault_audit, vault_auth_factors, vault_operation_challenges, vault_secrets
from storage.vault_crypto import Sealed
from tests.vault_webauthn_helpers import WebAuthnTestCredential
from vibe import api

ORIGIN = "https://sandbox.avibe.bot"
RP = "sandbox.avibe.bot"


def candidate(label=b"first"):
    credential = WebAuthnTestCredential(rp_id=RP, origin=ORIGIN, credential_id=label)
    options = api.create_vault_authz_webauthn_options(origin=ORIGIN)
    return credential, {"wrap_meta": credential.wrap_meta(), "authz_factor_registration": credential.registration_payload(
        challenge_id=options["challenge_id"], challenge_b64=options["webauthn"]["challenge"])}


def snapshot():
    with api._vault_engine().connect() as conn:
        return {table.name: [dict(row) for row in conn.execute(select(table)).mappings()]
                for table in (vault_secrets, vault_auth_factors, vault_operation_challenges, vault_audit, state_meta)}


def create_payload(name, setup, *, replay_registration=False):
    return {"name": name, "protection": "protected", "establishing_vmk": True,
            "sealed": {"ciphertext": "synthetic-record", "nonce": "synthetic-record-nonce",
                       "wrap_meta": {**setup["wrap_meta"], "scheme": "protected-v1", "wrapped_dek": name, "dek_nonce": "record"}},
            **({"authz_factor_registration": setup["authz_factor_registration"]} if replay_registration else {})}


def test_setup_without_items_is_durable_idempotent_and_real_seal_payload_reuses_root():
    _credential, setup = candidate()
    result = api.complete_vault_setup(setup, origin=ORIGIN)
    assert result["exists"] and api.get_vault_vmk() == result
    assert snapshot()["vault_secrets"] == []
    before = snapshot()
    # Same-result retry acknowledges; it does not verify/enroll/consume twice.
    assert api.complete_vault_setup(copy.deepcopy(setup), origin=ORIGIN) == result
    equivalent = {**setup, "wrap_meta": json.dumps(setup["wrap_meta"], indent=2, sort_keys=True)}
    assert api.complete_vault_setup(equivalent, origin=ORIGIN) == result
    after = snapshot()
    for table in ("vault_auth_factors", "vault_operation_challenges", "vault_audit"):
        assert after[table] == before[table]
    # Sandbox may keep establishingVmk=true after durable setup. React can drop
    # consumed registration; the backend accepts only the exact committed root.
    for name in ("FIRST", "SECOND"):
        assert api.create_vault_secret(create_payload(name, setup), origin=ORIGIN)["ok"]
    for name in ("FIRST", "SECOND"):
        api.delete_vault_secret(name)
    assert api.get_vault_vmk() == result
    factors = snapshot()["vault_auth_factors"]
    assert len(factors) == 1 and factors[0]["disabled_at"] is None


def test_old_first_protected_create_commits_same_durable_setup_owner():
    _credential, setup = candidate()
    assert api.create_vault_secret(create_payload("FIRST", setup, replay_registration=True), origin=ORIGIN)["ok"]
    assert json.loads(api.get_vault_vmk()["wrap_meta"]) == setup["wrap_meta"]
    assert api.create_vault_secret(create_payload("SECOND", setup, replay_registration=True), origin=ORIGIN)["ok"]
    assert len(snapshot()["vault_auth_factors"]) == 1


@pytest.mark.parametrize("mutation", ["copy-id", "rp", "root", "registration"])
def test_setup_rejects_conflicting_binding_without_overwrite(mutation):
    credential, setup = candidate()
    if mutation in {"root", "registration"}:
        api.complete_vault_setup(setup, origin=ORIGIN)
    changed = copy.deepcopy(setup)
    if mutation == "copy-id":
        changed["wrap_meta"]["copies"][0]["credential_id"] = "AA=="
    elif mutation == "rp":
        changed["wrap_meta"]["rp_id"] = "other.example"
    elif mutation == "root":
        changed["wrap_meta"] = credential.wrap_meta(marker=2)
    else:
        changed["authz_factor_registration"]["challenge_id"] = "vop_different"
    before = snapshot()
    with pytest.raises(api.VaultApiError):
        api.complete_vault_setup(changed, origin=ORIGIN)
    assert snapshot() == before


def test_competing_standalone_and_first_create_have_one_complete_winner():
    _first, first = candidate(b"candidate-one")
    _second, second = candidate(b"candidate-two")
    barrier = Barrier(2)

    def finish(item):
        label, setup = item
        barrier.wait(timeout=10)
        try:
            if label == "standalone":
                api.complete_vault_setup(setup, origin=ORIGIN)
            else:
                api.create_vault_secret(create_payload("WINNER", setup, replay_registration=True), origin=ORIGIN)
            return label
        except api.VaultApiError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, [("standalone", first), ("create", second)]))
    assert results.count("vault_already_initialized") == 1
    winner = first if "standalone" in results else second
    saved = snapshot()
    assert json.loads(api.get_vault_vmk()["wrap_meta"]) == winner["wrap_meta"]
    assert [factor["credential_id"] for factor in saved["vault_auth_factors"]] == [winner["wrap_meta"]["copies"][0]["credential_id"]]
    assert len(saved["vault_secrets"]) == (0 if "standalone" in results else 1)
    assert sum(row["consumed_at"] is not None for row in saved["vault_operation_challenges"]) == 1


def test_setup_audit_failure_rolls_back_root_registration_and_consumption(monkeypatch):
    _credential, setup = candidate()
    before = snapshot()
    original = vault_service.audit

    def audit(conn, event, **kwargs):
        if event == "vault-setup-established":
            raise RuntimeError("setup audit failed")
        return original(conn, event, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(vault_service, "audit", audit)
        with pytest.raises(RuntimeError):
            api.complete_vault_setup(setup, origin=ORIGIN)
    assert snapshot() == before
    assert api.complete_vault_setup(setup, origin=ORIGIN)["ok"]


@pytest.mark.parametrize("history", ["active", "disabled"])
def test_existing_factor_without_root_never_creates_another_credential(history):
    _credential, first = candidate()
    with api._vault_engine().begin() as conn:
        vault_service.register_webauthn_factor(conn, first["authz_factor_registration"], rp_id=RP, origin=ORIGIN, establishment=True)
        if history == "disabled":
            conn.execute(vault_auth_factors.update().values(disabled_at="disabled"))
    _other, second = candidate(b"would-be-replacement")
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.complete_vault_setup(second, origin=ORIGIN)
    assert error.value.code == "vault_policy_factor_unavailable"
    assert snapshot() == before


def test_legacy_root_is_not_silently_enrolled_or_overwritten_and_survives_deletion():
    credential = WebAuthnTestCredential(rp_id=RP, origin=ORIGIN)
    original = {"ciphertext": "legacy-ciphertext", "nonce": "legacy-nonce", "wrap_meta": credential.wrap_meta()}
    api.create_vault_secret({"name": "LEGACY", "protection": "protected", "sealed": original})
    _other, setup = candidate(b"new-unrelated")
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.complete_vault_setup(setup, origin=ORIGIN)
    assert error.value.code == "vault_already_initialized"
    assert snapshot() == before
    api.delete_vault_secret("LEGACY")
    assert json.loads(api.get_vault_vmk()["wrap_meta"]) == credential.wrap_meta()
    assert snapshot()["vault_auth_factors"] == []
    with api._vault_engine().connect() as conn:
        stored = json.loads(conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == vault_setup.META_KEY)).scalar_one())
    assert stored["source"] == "legacy-record" and stored["factor_id"] is None


def test_same_root_retry_compares_typed_canonical_bytes_not_python_scalar_equality():
    _credential, setup = candidate()
    setup["wrap_meta"]["root_meta"] = {"v": 1, "nonce": "opaque-nonce", "ciphertext": "opaque-encrypted-metadata"}
    api.complete_vault_setup(setup, origin=ORIGIN)
    changed = copy.deepcopy(setup)
    changed["wrap_meta"]["root_meta"]["v"] = True
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(create_payload("BAD_ROOT", changed), origin=ORIGIN)
    assert error.value.code == "vault_already_initialized"
    assert snapshot() == before


def test_caught_first_create_failure_cannot_commit_partial_shared_setup():
    _credential, setup = candidate()
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.VaultServiceError, match="fields must be an array"):
            vault_service.create_secret(conn, name="FAILED", protection="protected", establishing_vmk=True,
                sealed=Sealed("c", "n", json.dumps(setup["wrap_meta"])),
                authz_factor_registration=setup["authz_factor_registration"], authz_factor_origin=ORIGIN, fields="not-an-array")
    saved = snapshot()
    assert saved["vault_secrets"] == [] and saved["vault_auth_factors"] == [] and saved["vault_audit"] == []
    assert all(challenge["consumed_at"] is None for challenge in saved["vault_operation_challenges"])
    assert not any(row["key"] == vault_setup.META_KEY for row in saved["state_meta"])


@pytest.mark.parametrize("raw", [b"[]", b"null", b"true", b"42", b'"scalar"', b"\xff", b"[" * 1100 + b"]" * 1100])
def test_setup_rejects_nonobject_or_malformed_client_data_without_registration(raw):
    _credential, setup = candidate()
    setup["authz_factor_registration"]["credential"]["response"]["clientDataJSON"] = base64.b64encode(raw).decode()
    before = snapshot()
    with pytest.raises(api.VaultApiError) as error:
        api.complete_vault_setup(setup, origin=ORIGIN)
    assert error.value.code == "invalid_protected_authz" and error.value.status == 403
    assert snapshot() == before

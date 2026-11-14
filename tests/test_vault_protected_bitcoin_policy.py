"""Protected-secp policy eligibility and cohort-bound internal identity.

The frozen public oracle catches a codec/domain change; real synthetic WebAuthn
assertions catch acceptance of an old hash or changed cohort before consumption.
These fixtures assert no custody import, actual sandbox build, pin or physical UV.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from storage import vault_policy, vault_service
from storage.models import vault_operation_challenges, vault_secrets
from tests.test_vault_policy_authorization import (
    capable_policy_sandbox as capable_policy_sandbox,
    complete, create_target, enroll, issue, proof, state,
)
from tests.test_vault_policy_context import FIXTURES, VECTORS, _source_row
from vibe import api, vault_sandbox_release as release

ORACLE = json.loads((FIXTURES / "protected-bitcoin-policy-v1-cohort.json").read_text())
BASE = next(vector for vector in VECTORS if vector["id"] == ORACLE["base_vector_id"])


@pytest.mark.parametrize("vector", ORACLE["vectors"], ids=lambda item: item["id"])
def test_protected_internal_preimage_and_public_v1_match_frozen_cohort_oracle(capable_policy_sandbox, monkeypatch, vector):
    cohort = vector["cohort"]
    support = replace(capable_policy_sandbox, build_hash=cohort["buildHash"], manifest_hash=cohort["manifestHash"])
    monkeypatch.setattr(release, "current_support", lambda: support)
    row = _source_row(BASE)
    target, current, revision = vault_policy.target_state(row, None)
    assert current is True and revision == BASE["context"]["change"]["revision"]
    assert target == {**BASE["context"]["target"], "identitySha256": vector["identity_sha256"]}
    snapshot = {**BASE["identity_snapshot"], "v": 2, "sandboxCohort": cohort}
    assert vault_policy.identity_hash(snapshot) == vector["identity_sha256"]
    context = {**BASE["context"], "target": target}
    assert context["v"] == 1
    challenge = vault_policy.challenge_bytes(context)
    assert challenge.hex() == vector["challenge_hex"]
    assert base64.urlsafe_b64encode(challenge).decode().rstrip("=") == vector["challenge_base64url"]
    assert hashlib.sha256(challenge).hexdigest() == vector["stored_challenge_hash"]
    if "identity_canonical_json" in vector:
        assert vault_policy.canonical_json(snapshot) == vector["identity_canonical_json"]
        assert vault_policy.canonical_json(context) == vector["context_canonical_json"]
    for field in ("ciphertext", "nonce", "wrap_meta", "updated_at"):
        changed = {**row, field: row[field] + "changed"}
        assert vault_policy.target_state(changed, None)[0]["identitySha256"] != target["identitySha256"]


def test_current_taproot_only_pin_refuses_policy_before_factor_readiness():
    create_target(protection="protected")
    before = state()  # No factor: unsupported must precede setup_required.
    with pytest.raises(vault_service.SigningPolicyError) as error:
        issue()
    assert error.value.code == "vault_policy_context_unsupported"
    assert state() == before


@pytest.mark.parametrize("change", ["unsupported", "shrink", "same-build-new-manifest", "old-preimage"])
def test_changed_cohort_and_old_issued_hash_refuse_before_assertion_consume(capable_policy_sandbox, monkeypatch, change):
    create_target(protection="protected")
    credential, factor_id = enroll()
    context = issue()
    if change == "old-preimage":
        # Model a pre-upgrade issued row, not a forged context against a new
        # challenge hash. Both wire and stored challenge must be the old bytes.
        with api._vault_engine().begin() as conn:
            row = conn.execute(select(vault_secrets)).mappings().one()
            old_snapshot = {
                "v": 1, "kind": "keypair", "protection": "protected", "signerKind": "local",
                "envelope": {"ciphertext": row["ciphertext"], "nonce": row["nonce"], "wrapMeta": row["wrap_meta"], "revision": row["updated_at"]},
                "keys": {"signing": {"curve": "secp256k1", "publicKey": json.loads(row["public_meta"])["signing_public_key"]["public_key"]}},
            }
            context["target"]["identitySha256"] = hashlib.sha256(json.dumps(old_snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            conn.execute(vault_operation_challenges.update().where(vault_operation_challenges.c.id == context["challengeId"]).values(
                challenge_hash=hashlib.sha256(vault_policy.challenge_bytes(context)).hexdigest(),
            ))
    payload = proof(context, credential, factor_id)
    if change == "unsupported":
        monkeypatch.setattr(release, "current_support", lambda: None)
    elif change == "shrink":
        monkeypatch.setattr(release, "current_support", lambda: replace(capable_policy_sandbox, ordinary_input_types=("p2tr",)))
    elif change == "same-build-new-manifest":
        changed = replace(capable_policy_sandbox, manifest_hash=ORACLE["vectors"][1]["cohort"]["manifestHash"])
        assert changed.build_hash == capable_policy_sandbox.build_hash
        monkeypatch.setattr(release, "current_support", lambda: changed)
    before = state()
    with pytest.raises(vault_service.SigningPolicyError) as error:
        complete(payload)
    assert error.value.code == "vault_policy_state_changed"
    assert state() == before  # Counter, consumed flag, policy, revision and audit.


def test_new_internal_identity_does_not_change_standard_or_solana_pending_bytes():
    for baseline in ORACLE["unchanged_baselines"]:
        vector = next(item for item in VECTORS if item["id"] == baseline["id"])
        context = copy.deepcopy(vector["context"])
        assert vault_policy.target_state(_source_row(vector), context["target"]["account"])[0] == context["target"]
        assert vault_policy.challenge_bytes(context).hex() == baseline["challenge_hex"]

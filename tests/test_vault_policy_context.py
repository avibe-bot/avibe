"""Pin the public producer wire to independent Python/JS freeze vectors.

Credible regressions are a changed byte/field domain, permissive context variant,
address checksum/network confusion, and identity loss during metadata edits.
Existing signing tests do not exercise this separate authorization purpose.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from storage import vault_addresses, vault_hd, vault_policy

FIXTURES = Path(__file__).parent / "fixtures/vault_signing"
VECTORS = json.loads((FIXTURES / "policy-downgrade-v1.json").read_text())["vectors"]
HD = json.loads((FIXTURES / "hd-seed-wallet.json").read_text())


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["id"])
def test_frozen_policy_context_and_identity_bytes(vector):
    assert vault_policy.canonical_json(vector["identity_snapshot"]) == vector["identity_canonical_json"]
    assert vault_policy.identity_hash(vector["identity_snapshot"]) == vector["context"]["target"]["identitySha256"]
    assert vault_policy.validate_context(vector["context"]) == vector["context"]
    assert vault_policy.canonical_json(vector["context"]) == vector["canonical_json"]
    challenge = vault_policy.challenge_bytes(vector["context"])
    assert challenge.hex() == vector["challenge_hex"]
    assert base64.urlsafe_b64encode(challenge).decode().rstrip("=") == vector["challenge_base64url"]
    assert hashlib.sha256(challenge).hexdigest() == vector["stored_challenge_hash"]


def _source_row(vector):
    context, identity = vector["context"], vector["identity_snapshot"]
    target, envelope = context["target"], identity["envelope"]
    policy = {}
    if target["signerKind"] == "hd-seed":
        accounts = vault_hd.import_accounts(HD["import_response"]["accounts"])
        accounts[0].update(name=target["accountName"], always_ask=True)
        meta = {"hd_accounts": accounts}
    else:
        policy["always_ask"] = True
        key = identity["keys"]["signing"]
        meta = {"signing_public_key": {"curve": key["curve"], "public_key": key["publicKey"], "ignored_note": "not-identity"}}
        if key["curve"] == "ed25519":
            meta["signing_public_key"]["chain"] = "solana"
        else:
            # Existing ordinary-key validation accepts upper hex before pinning.
            meta["signing_public_key"]["public_key"] = key["publicKey"].upper()
        policy[vault_policy.REVISION_KEY] = context["change"]["revision"]
    return {"id": target["secretId"], "name": target["name"], "kind": "keypair", "protection": target["protection"],
            "signer_kind": target["signerKind"], "ciphertext": envelope["ciphertext"], "nonce": envelope["nonce"],
            "wrap_meta": envelope["wrapMeta"], "updated_at": envelope["revision"], "public_meta": json.dumps(meta), "policy": json.dumps(policy)}


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["id"])
def test_storage_identity_projection_matches_frozen_public_vector(vector):
    row = _source_row(vector)
    target, current, revision = vault_policy.target_state(row, vector["context"]["target"]["account"])
    assert target == vector["context"]["target"]
    assert current is True and revision == vector["context"]["change"]["revision"]
    meta = json.loads(row["public_meta"])
    meta["description"] = "Unrelated label"
    if row["signer_kind"] == "hd-seed":
        meta["hd_accounts"][0]["last_signer"] = {"session_id": "test", "at": "now"}
    row["public_meta"] = json.dumps(meta)
    assert vault_policy.target_state(row, target["account"])[0] == target
    for field in ("ciphertext", "nonce", "wrap_meta", "updated_at"):
        assert vault_policy.target_state({**row, field: row[field] + "changed"}, target["account"])[0]["identitySha256"] != target["identitySha256"]


def _context():
    return copy.deepcopy(VECTORS[0]["context"])


@pytest.mark.parametrize("path,value", [
    (("v",), True), (("v",), 1.0), (("purpose",), "delete_secret"),
    (("challengeId",), "vop space"), (("challengeId",), "vop_newline\n"),
    (("nonce",), "AA=="), (("nonce",), base64.b64encode(b"x" * 32).decode().rstrip("=")),
    (("expiresAt",), "2026-02-30T00:00:00.000Z"), (("expiresAt",), "2026-11-13T00:02:00Z"),
    (("target", "secretId"), "vlt\x00bad"), (("target", "protection"), "protected"),
    (("target", "account"), True), (("target", "account"), 20), (("target", "account"), None),
    (("target", "accountName"), ""), (("target", "accountName"), "x" * 81), (("target", "accountName"), "\ud800"),
    (("target", "identitySha256"), "a" * 63), (("target", "identitySha256"), "A" * 64),
    (("change", "field"), "name"), (("change", "from"), 1), (("change", "to"), 0),
    (("change", "revision"), ""), (("factors",), []),
])
def test_policy_context_rejects_wrong_fields_types_and_bounds(path, value):
    context = _context()
    node = context
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(vault_policy.PolicyContextError):
        vault_policy.validate_context(context)


def test_policy_context_rejects_extra_fields_and_oversize_without_truncating():
    for context in ({**_context(), "webauthn": {}}, {**_context(), "target": {**_context()["target"], "extra": True}}):
        with pytest.raises(vault_policy.PolicyContextError):
            vault_policy.validate_context(context)
    context = _context()
    context["target"]["name"] = "A" * 65_536
    with pytest.raises(vault_policy.PolicyContextError, match="64KiB"):
        vault_policy.validate_context(context)
    assert len(context["target"]["name"]) == 65_536


def test_context_credential_encoding_is_strict_without_weakening_existing_assertion_parser():
    for size in (1, 1023):
        context = _context()
        context["factors"][0]["credentialId"] = base64.b64encode(b"x" * size).decode()
        vault_policy.validate_context(context)
    for encoded in ("", "AA", "AA===", "AB==", "AA==\n", "_w==", base64.b64encode(b"x" * 1024).decode()):
        context = _context()
        context["factors"][0]["credentialId"] = encoded
        with pytest.raises(vault_policy.PolicyContextError):
            vault_policy.validate_context(context)


def test_factor_ids_and_credentials_are_unique_and_ascii_sorted():
    context = _context()
    first = context["factors"][0]
    for factors in ([first, first], [first, {**first, "factorId": "zzz"}],
                    [{**first, "factorId": "zzz", "credentialId": "AA=="}, first]):
        with pytest.raises(vault_policy.PolicyContextError):
            vault_policy.validate_context({**context, "factors": factors})


@pytest.mark.parametrize("field", sorted(vault_policy.ADDRESS_FIELDS))
def test_canonical_address_checksum_network_and_variant_are_enforced(field):
    vector = VECTORS[1] if field == "solana" else VECTORS[2]
    canonical = vector["context"]["target"]["addresses"][field]
    vault_addresses.validate_public_address(field, canonical)
    bad = "1" * 31 if field == "solana" else canonical + "1"
    if field == "eth":
        bad = canonical.lower()
        assert bad != canonical
    elif field.startswith("btc_"):
        bad = canonical[:-1] + ("q" if canonical[-1] != "q" else "p")
    with pytest.raises(ValueError):
        vault_addresses.validate_public_address(field, bad)
    context = copy.deepcopy(vector["context"])
    context["target"]["addresses"][field] = bad
    with pytest.raises(vault_policy.PolicyContextError):
        vault_policy.validate_context(context)
    if field in {"btc_segwit", "btc_taproot"}:
        with pytest.raises(ValueError):
            vault_addresses.validate_public_address(field, canonical.upper())
        with pytest.raises(ValueError):
            vault_addresses.validate_public_address(field, "tb" + canonical[2:])


def test_cross_variant_addresses_and_ordinary_account_are_not_accepted():
    context = copy.deepcopy(VECTORS[2]["context"])
    context["target"]["account"] = 0
    with pytest.raises(vault_policy.PolicyContextError):
        vault_policy.validate_context(context)
    context = _context()
    context["target"]["addresses"]["btc_legacy"] = VECTORS[2]["context"]["target"]["addresses"]["btc_legacy"]
    with pytest.raises(vault_policy.PolicyContextError):
        vault_policy.validate_context(context)


def test_canonical_encoding_rejects_lossy_numbers_and_sorts_utf16():
    for value in (1.5, float("nan"), 2**53, "\ud800"):
        with pytest.raises(vault_policy.PolicyContextError):
            vault_policy.canonical_json({"value": value})
    assert vault_policy.canonical_json({"\ue000": 1, "🧀": 2}) == '{"🧀":2,"\ue000":1}'


def test_exact_context_byte_and_astral_name_bounds():
    context = _context()
    context["target"]["accountName"] = "🧀" * 80
    vault_policy.validate_context(context)
    context["target"]["accountName"] += "🧀"
    with pytest.raises(vault_policy.PolicyContextError):
        vault_policy.validate_context(context)
    context = _context()
    context["target"]["name"] = "A"
    # Independent stdlib encoding has the same size for these fixed ASCII keys.
    used = len(json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode())
    context["target"]["name"] = "A" * (65_536 - used + 1)
    vault_policy.validate_context(context)
    context["target"]["name"] += "A"
    with pytest.raises(vault_policy.PolicyContextError, match="64KiB"):
        vault_policy.validate_context(context)


def test_nonzero_hd_target_uses_that_accounts_independent_public_addresses():
    row = _source_row(VECTORS[0])
    meta = json.loads(row["public_meta"])
    for item in meta["hd_accounts"]:
        item["enabled"] = True
    meta["hd_accounts"][19]["always_ask"] = True
    row["public_meta"] = json.dumps(meta)
    target, current, revision = vault_policy.target_state(row, 19)
    assert target["account"] == 19 and current is True and revision == "legacy"
    expected = HD["expected_addresses"][19]
    assert {field: target["addresses"][field] for field in ("eth", "btc_taproot", "solana")} == {field: expected[field] for field in ("eth", "btc_taproot", "solana")}
    assert target["identitySha256"] != vault_policy.target_state(row, 0)[0]["identitySha256"]


def test_valid_checksum_testnet_address_is_not_a_mainnet_policy_address():
    # Independent embit corpus pins the same public output key encoded for testnet.
    with pytest.raises(ValueError):
        vault_addresses.validate_public_address("btc_taproot", "tb1pncxfv6agmud3hle0dt6efnrn0dkphju0tsm7ymufmec288ss7mgq00ultr")

"""Protected-HD public consistency and atomic persistence, with public test data.

Observable contracts: no custody/version call; exact authenticated projection and
five-address expectations; caught failures cannot commit partial records/setup.
Existing standard-HD tests cannot reach this distinct browser-custody branch.
Synthetic root copies below do not attest PRF or ciphertext/seed consistency.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import resource_access_service, vault_hd, vault_protected_hd, vault_service, vault_setup
from storage.models import resource_access_policies, state_meta, vault_audit, vault_auth_factors, vault_requests, vault_secrets
from storage.vault_crypto import Sealed
from tests.vault_webauthn_helpers import WebAuthnTestCredential
from vibe import api, cli

FIXTURE = json.loads((Path(__file__).parent / "fixtures/vault_signing/protected-hd-codec-vectors.json").read_text())
ORIGIN = "https://sandbox.avibe.bot"


def payload(case=0, *, setup=None):
    vector = FIXTURE["cases"][case]
    envelope = copy.deepcopy(vector["envelope"])
    if setup:
        root = setup["wrap_meta"]
    else:
        with api._vault_engine().connect() as conn:
            current = conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == vault_setup.META_KEY)).scalar_one_or_none()
        root = (json.loads(json.loads(current)["wrap_meta"]) if current is not None else
                WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin=ORIGIN).wrap_meta())
    wrap = {**root, **{key: child for key, child in json.loads(envelope["wrap_meta"]).items() if key in vault_setup.RECORD_FIELDS}}
    envelope["wrap_meta"] = json.dumps(wrap, ensure_ascii=False, separators=(",", ":"))
    return {"name": vector["name"], "kind": "keypair", "signer_kind": "hd-seed", "protection": "protected",
            "sealed": envelope, "accounts": copy.deepcopy(vector["record_meta"]["accounts"]),
            "expected_account0": copy.deepcopy(vector["expected_account0"])}


def rows(table):
    with api._vault_engine().connect() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def stored():
    row, = rows(vault_secrets)
    return row


def storage_create(conn, value, **extra):
    return vault_service.create_secret(conn, name=value["name"], kind="keypair", signer_kind="hd-seed",
        protection="protected", sealed=Sealed(**value["sealed"]), accounts=value["accounts"],
        expected_account0=value["expected_account0"], **extra)


@pytest.fixture(autouse=True)
def no_custody(monkeypatch, request):
    for name in ("_run_avault", "avault_status", "_hd_bitcoin_types_available", "avault_hd_seed_import",
                 "_require_avault_hd_seed_surface", "_require_avault_ed25519_surface", "avault_sign", "avault_sign_hd_seed"):
        monkeypatch.setattr(api, name, Mock(side_effect=AssertionError("protected HD must never discover/use avault")))
    monkeypatch.setattr(api, "_notify_vault_request_created", Mock())
    if not request.node.name.startswith("test_failed_first_wallet"):
        # Test-owned existing legacy root, deliberately without a policy factor.
        # Setup-ceremony cases below start empty and exercise the real owner.
        root = WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin=ORIGIN).wrap_meta()
        with api._vault_engine().begin() as conn:
            conn.execute(state_meta.insert().values(key=vault_setup.META_KEY,
                value_json=json.dumps({"v": 1, "wrap_meta": json.dumps(root), "source": "legacy-record", "factor_id": None}),
                updated_at="2026-01-01T00:00:00+00:00"))


@pytest.mark.parametrize("case", range(5))
def test_create_builds_exact_projection_and_taproot_only_public_choices(case):
    value = payload(case)
    result = api.create_vault_secret(value)["secret"]
    row = stored()
    assert {key: row[key] for key in ("ciphertext", "nonce", "wrap_meta")} == value["sealed"]
    meta = json.loads(row["public_meta"])
    assert meta["bitcoin_profile"] == vault_hd.BITCOIN_PROFILE
    assert meta["hd_accounts"] == [{**item, "enabled": i == 0, "name": None, "always_ask": True, "last_signer": None}
                                    for i, item in enumerate(value["accounts"])]
    assert len(meta["hd_accounts"]) == 20 and all(len(item) == 10 for item in meta["hd_accounts"])
    assert result["per_use_sign"] and not result["access_grantable"]
    assert result["accounts"][0]["bitcoin_address_types"] == ["p2tr"]
    assert result["accounts"][0]["always_ask"] is True
    assert "btc_legacy" not in result["accounts"][0] and "btc_segwit" not in result["accounts"][0]
    assert {key: result["accounts"][0][key] for key in ("eth", "btc_taproot", "solana")} == {
        key: value["expected_account0"][key] for key in ("eth", "btc_taproot", "solana")}


@pytest.mark.parametrize("field", ["eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"])
def test_storage_owns_all_five_expected_addresses_before_any_insert(field):
    value = payload()
    value["expected_account0"][field] = FIXTURE["cases"][1]["expected_account0"][field]
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidHdWalletError, match="addresses"):
            storage_create(conn, value)
    assert rows(vault_secrets) == rows(vault_audit) == rows(resource_access_policies) == []


@pytest.mark.parametrize("field,value", [
    ("public_meta", {}), ("public_meta", None), ("account0_always_ask", True), ("account0_always_ask", False),
    ("fields", []), ("default_field", "value"), ("blind_box", {}), ("keypair", None),
    ("seed", "not accepted"), ("passphrase", "not accepted"), ("words", []), ("signing_public_key", {}),
    ("policy", {"always_ask": True}), ("establishing_vmk", "true"),
])
def test_api_rejects_alternate_authority_or_custody_inputs(field, value):
    candidate = {**payload(), field: value}
    with pytest.raises(api.VaultApiError):
        api.create_vault_secret(candidate)
    assert rows(vault_secrets) == rows(vault_audit) == []


@pytest.mark.parametrize("mutation", ["name", "object-wrap", "extra-envelope", "noncanonical-base64", "ciphertext-size", "boolean-version",
    "profile", "extra-record", "extra-row", "boolean-account", "wrong-key", "table-mismatch", "unicode", "nonfinite", "depth", "oversize"])
def test_malformed_envelopes_and_typed_identity_disagreements_refuse_without_writes(mutation):
    value = payload()
    wrap = json.loads(value["sealed"]["wrap_meta"])
    if mutation == "name":
        value["name"] = " " + value["name"]
    elif mutation == "object-wrap":
        value["sealed"]["wrap_meta"] = wrap
    elif mutation == "extra-envelope":
        value["sealed"]["extra"] = True
    elif mutation == "noncanonical-base64":
        value["sealed"]["ciphertext"] = value["sealed"]["ciphertext"][:-3] + "B=="
    elif mutation == "ciphertext-size":
        value["sealed"]["ciphertext"] = base64.b64encode(b"x" * 128).decode()
    else:
        if mutation == "boolean-version":
            wrap["v"] = True
        elif mutation == "profile":
            wrap["record_meta"]["profile"] = "future"
        elif mutation == "extra-record":
            wrap["record_meta"]["extra"] = True
        elif mutation == "extra-row":
            wrap["record_meta"]["accounts"][0]["enabled"] = True
        elif mutation == "boolean-account":
            wrap["record_meta"]["accounts"][0]["account"] = False
            value["accounts"][0]["account"] = False
        elif mutation == "wrong-key":
            wrap["record_meta"]["accounts"][0]["bitcoin_p2pkh"] = "02" + "ff" * 32
        elif mutation == "table-mismatch":
            value["accounts"][0]["bitcoin_p2pkh"] = value["accounts"][1]["bitcoin_p2pkh"]
        elif mutation == "unicode":
            wrap["extension"] = "\ud800"
        elif mutation == "nonfinite":
            wrap["extension"] = float("inf")
        elif mutation == "depth":
            wrap["extension"] = json.loads("[" * 64 + "0" + "]" * 64)
        elif mutation == "oversize":
            wrap["extension"] = "x" * 65_536
        value["sealed"]["wrap_meta"] = json.dumps(wrap)
    with pytest.raises(api.VaultApiError):
        api.create_vault_secret(value)
    assert rows(vault_secrets) == rows(vault_audit) == []


def test_duplicate_effective_members_and_unknown_root_extensions_keep_raw_fingerprint():
    value = payload()
    wrap = value["sealed"]["wrap_meta"]
    value["sealed"]["wrap_meta"] = '{"v":false,"extension":{"unicode":"用户 🚀","number":1.5},' + wrap[1:]
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.update().where(state_meta.c.key == vault_setup.META_KEY).values(value_json=json.dumps({
            "v": 1, "wrap_meta": json.dumps({key: child for key, child in json.loads(value["sealed"]["wrap_meta"]).items()
                if key not in {"scheme", "record_meta", "wrapped_dek", "dek_nonce"}})})))
    api.create_vault_secret(value)
    assert stored()["wrap_meta"] == value["sealed"]["wrap_meta"]
    # Root numbers/extensions are not a new whitelist; duplicate v uses last-wins parsing.
    parsed = vault_protected_hd.parse_wrap(stored()["wrap_meta"])
    assert parsed["v"] == 1 and parsed["extension"]["number"] == 1.5


def test_authenticated_duplicate_points_do_not_gain_a_new_cross_row_rejection_rule():
    value = payload()
    value["accounts"][1]["bitcoin_p2pkh"] = value["accounts"][0]["bitcoin_p2pkh"]
    wrap = json.loads(value["sealed"]["wrap_meta"])
    wrap["record_meta"]["accounts"] = value["accounts"]
    value["sealed"]["wrap_meta"] = json.dumps(wrap)
    assert api.create_vault_secret(value)["ok"]


@pytest.mark.parametrize("failure", ["readback", "audit", "fulfil"])
def test_caught_create_failure_rolls_back_record_resource_audit_and_provision(failure, monkeypatch):
    value = payload()
    original_require = vault_service._require_row
    if failure == "readback":
        def bad_readback(conn, name):
            row = original_require(conn, name)
            return {**row, "nonce": base64.b64encode(b"wrong nonce!").decode()}
        monkeypatch.setattr(vault_service, "_require_row", bad_readback)
    elif failure == "audit":
        monkeypatch.setattr(vault_service, "audit", Mock(side_effect=RuntimeError("synthetic audit failure")))
    else:
        monkeypatch.setattr(vault_service, "fulfill_pending_provision_requests_for_secret", Mock(side_effect=RuntimeError("synthetic fulfil failure")))
    with api._vault_engine().begin() as conn:
        with pytest.raises((vault_service.InvalidHdWalletError, RuntimeError)):
            storage_create(conn, value, user_context=resource_access_service.ResourceUserContext(instance_role="editor", subject="creator", is_remote=True))
    assert rows(vault_secrets) == rows(vault_audit) == rows(resource_access_policies) == rows(vault_requests) == []


@pytest.mark.parametrize("durable", [False, True])
def test_failed_first_wallet_rolls_back_only_new_setup_not_preexisting_durable_setup(durable, monkeypatch):
    credential = WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin=ORIGIN)
    options = api.create_vault_authz_webauthn_options(origin=ORIGIN)
    setup = {"wrap_meta": credential.wrap_meta(), "authz_factor_registration": credential.registration_payload(
        challenge_id=options["challenge_id"], challenge_b64=options["webauthn"]["challenge"])}
    if durable:
        api.complete_vault_setup(setup, origin=ORIGIN)
    before_factors, before_audit = rows(vault_auth_factors), rows(vault_audit)
    value = payload(setup=setup)
    monkeypatch.setattr(vault_service, "fulfill_pending_provision_requests_for_secret", Mock(side_effect=RuntimeError("synthetic fulfil failure")))
    with api._vault_engine().begin() as conn:
        with pytest.raises(RuntimeError):
            storage_create(conn, value, establishing_vmk=True, authz_factor_registration=setup["authz_factor_registration"], authz_factor_origin=ORIGIN)
    assert rows(vault_secrets) == rows(resource_access_policies) == []
    assert rows(vault_auth_factors) == before_factors and rows(vault_audit) == before_audit
    setup_rows = [row for row in rows(state_meta) if row["key"] == vault_setup.META_KEY]
    assert len(setup_rows) == int(durable)


@pytest.mark.parametrize("mutation", ["missing-profile", "profile", "key", "extra-row", "name-type", "enabled-type", "envelope", "retagged"])
def test_corrupt_item_degrades_listing_and_refuses_account_management(mutation):
    value = payload()
    api.create_vault_secret(value)
    with api._vault_engine().begin() as conn:
        row = stored()
        meta = json.loads(row["public_meta"])
        changes = {}
        if mutation == "missing-profile":
            meta.pop("bitcoin_profile")
        elif mutation == "profile":
            meta["bitcoin_profile"] = "unknown"
        elif mutation == "key":
            meta["hd_accounts"][0]["bitcoin_p2wpkh"] = meta["hd_accounts"][1]["bitcoin_p2wpkh"]
        elif mutation == "extra-row":
            meta["hd_accounts"][19]["unexpected"] = True
        elif mutation == "name-type":
            meta["hd_accounts"][19]["name"] = 17
        elif mutation == "enabled-type":
            meta["hd_accounts"][0]["enabled"] = 1
        elif mutation == "envelope":
            changes["nonce"] = "malformed"
        elif mutation == "retagged":
            changes["protection"] = "standard"
        conn.execute(vault_secrets.update().values(public_meta=json.dumps(meta), **changes))
    result, = api.get_vault_secrets()["secrets"]
    assert result["accounts"] == [] and not result["per_use_sign"]
    before = stored()
    with pytest.raises(api.VaultApiError):
        api.update_vault_hd_account(value["name"], {})
    if mutation != "retagged":
        with api._vault_engine().connect() as conn:
            with pytest.raises(vault_service.InvalidHdWalletError):
                vault_service.get_key_envelope(conn, value["name"])
    assert stored() == before


def test_management_enables_next_index_and_labels_without_resealing_or_lowering_policy():
    value = payload()
    api.create_vault_secret(value)
    before = stored()
    assert api.update_vault_hd_account(value["name"], {})["secret"]["accounts"][1]["account"] == 1
    result = api.update_vault_hd_account(value["name"], {"name": "  Daily  "}, account=1)["secret"]
    assert result["accounts"][1]["name"] == "Daily"
    with pytest.raises(api.VaultApiError):
        api.update_vault_hd_account(value["name"], {"always_ask": False}, account=1)
    after = stored()
    assert {key: before[key] for key in ("ciphertext", "nonce", "wrap_meta")} == {key: after[key] for key in ("ciphertext", "nonce", "wrap_meta")}
    assert all(len(item) == 10 for item in json.loads(after["public_meta"])["hd_accounts"])
    with api._vault_engine().begin() as conn:
        for tier in ("standard",):
            with pytest.raises(vault_service.InvalidRequestError):
                vault_service.update_secret_classification(conn, value["name"], protection=tier)
        for operation in (vault_service.get_protected_record_envelope, vault_service.get_envelope):
            with pytest.raises(vault_service.VaultServiceError):
                operation(conn, value["name"])


SIGNATURES = json.loads((Path(__file__).parent / "fixtures/vault_signing/hd-seed-wallet.json").read_text())
BITCOIN = json.loads((Path(__file__).parent / "fixtures/vault_bitcoin/core-fixtures.json").read_text())
SOLANA = json.loads((Path(__file__).parent / "fixtures/vault_signing/protected-solana-sign-context.json").read_text())


def signing_wallet(chain="evm"):
    """Synthetic storage material with independent public signature oracles at #1.

    Only the selected public point is replaced in both authenticated-table claims.
    This exercises server consistency/verification, NOT AEAD, seed derivation, or
    browser decoding of EVM typed data. Real seal/reopen is producer/runtime-owned.
    """
    value = payload()
    if chain == "bitcoin":
        case = BITCOIN["cases"][0]
        key = next(item for item in BITCOIN["keys"] if item["id"] == case["signing_key_id"])["internal_pubkey_compressed_hex"]
        context = {"kind": "bitcoin-taproot-keypath-v1", "network": "signet", "unsignedTransaction": case["unsigned_tx_hex"],
                   "prevout": {"amountSats": case["funding"]["spent_output_value_sats"], "scriptPubKey": case["funding"]["spent_output_script_hex"]},
                   "digestAlgorithm": "bip341-sighash-default-keypath", "digest": case["expected"]["taproot_digest_hex"]}
        signature = {"signature": case["expected"]["bip340_signature_hex"]}
        material = {"digest": context["digest"]}
        scheme = "schnorr-secp256k1-bip341-keypath"
    elif chain == "solana":
        key = SOLANA["publicKey"]
        context = copy.deepcopy(SOLANA["signedContext"]["release"]["signingContext"])
        signature = {"signature": SOLANA["expectedSignature"]}
        material = {"message_hex": context["messageHex"]}
        scheme = "ed25519"
    else:
        case = next(item for item in SIGNATURES["signatures"] if item["chain"] == chain and item["account"] == 1)
        key, signature, scheme = case["public_key"], copy.deepcopy(case["signature"]), case["scheme"]
        material = {"digest": case["digest"]}
        context = {"kind": "eip-712-typed-data", "digestAlgorithm": "eip712", "typedData": {}, "digest": case["digest"]}
    value["accounts"][1][chain] = key
    wrap = json.loads(value["sealed"]["wrap_meta"])
    wrap["record_meta"]["accounts"] = value["accounts"]
    value["sealed"]["wrap_meta"] = json.dumps(wrap, ensure_ascii=False)
    api.create_vault_secret(value)
    api.update_vault_hd_account(value["name"], {})
    signing = {"name": value["name"], "chain": chain, "account": 1, "scheme": scheme,
               **material, "signing_context": context, "requester": {"session_id": "original-requester"}}
    return signing, signature, key


def complete(payload, request, signature):
    derivation = request["delivery"]["derivation"]
    return api.vault_sign({**payload, **derivation, "request_id": request["id"], "signature": signature,
                           "requester": {"session_id": "approving-browser"}})


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
@pytest.mark.parametrize("entrypoint", ["sign", "request"])
def test_selected_protected_hd_pending_release_completion_and_original_provenance(chain, entrypoint):
    signing, signature, key = signing_wallet(chain)
    before = stored()
    requested = (api.vault_sign(signing) if entrypoint == "sign" else api.request_vault_sign(signing))
    request = requested["request"]
    assert request["status"] == "pending"
    derivation = {"chain": chain, "account": 1, **({"address_type": "p2tr"} if chain == "bitcoin" else {})}
    binding = {"secret_id": before["id"], "public_key": key, "envelope_sha256": hashlib.sha256(json.dumps(
        [before[field] for field in ("ciphertext", "nonce", "wrap_meta")], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()}
    assert request["delivery"]["derivation"] == derivation
    assert request["delivery"]["signing_key"] == binding
    assert request["delivery"]["operation_context"]["release"] == {
        "name": signing["name"], "scheme": signing["scheme"], "publicKey": key,
        "signingContext": signing["signing_context"], "derivation": derivation, "signingKey": binding,
        **({"messageHex": signing["message_hex"]} if chain == "solana" else {"digest": signing["digest"]})}
    result = complete(signing, request, signature)
    assert result["ok"] and result["request"]["status"] == "approved"
    assert result["signature"] == signature
    after = stored()
    accounts = json.loads(after["public_meta"])["hd_accounts"]
    assert after["use_count"] == 1 and accounts[0]["last_signer"] is None
    assert accounts[1]["last_signer"]["session_id"] == "original-requester"
    events = [item for item in rows(vault_audit) if item["event"] == "signed"]
    assert len(events) == 1 and json.loads(events[0]["requester"])["session_id"] == "original-requester"
    with pytest.raises(api.VaultApiError, match="not pending"):
        complete(signing, request, signature)


@pytest.mark.parametrize("entrypoint,session_id,expected", [
    ("sign", 17, None), ("sign", False, None), ("sign", {}, None),
    ("sign", ["not-a-session"], None), ("request", "", None),
    ("request", [], None), ("request", None, None), ("sign", " \t ", None),
    ("sign", " original-requester ", "original-requester"),
    ("request", " original-requester ", "original-requester"),
])
def test_completion_provenance_cannot_poison_the_next_protected_hd_read(entrypoint, session_id, expected):
    """A successful use must remain readable; good-string provenance missed this.

    The two API entrypoints preserve different malformed requester values. Their
    original audit provenance must survive, but cannot become invalid HD settings.
    """
    signing, signature, _ = signing_wallet()
    signing["requester"] = {"session_id": session_id}
    request = (api.vault_sign(signing) if entrypoint == "sign" else api.request_vault_sign(signing))["request"]
    assert complete(signing, request, signature)["ok"]
    secret, = api.get_vault_secrets()["secrets"]
    assert secret["per_use_sign"] and [item["account"] for item in secret["accounts"]] == [0, 1]
    assert secret["accounts"][1]["last_signer"]["session_id"] == expected
    signed, = [item for item in rows(vault_audit) if item["event"] == "signed"]
    assert json.loads(signed["requester"]) == request["requester"]
    assert api.update_vault_hd_account(signing["name"], {"name": "Still usable"}, account=1)["secret"]["accounts"][1]["name"] == "Still usable"
    assert api.vault_sign(signing)["request"]["status"] == "pending"
    assert stored()["use_count"] == 1


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
def test_cli_protected_hd_uses_same_pending_contract_without_avault(chain, capfd):
    signing, _, key = signing_wallet(chain)
    args = ["vault", "sign", signing["name"], "--chain", chain, "--account", "1", "--signing-context-json", json.dumps(signing["signing_context"])]
    args += ["--message-hex", signing["message_hex"]] if chain == "solana" else ["--digest", signing["digest"]]
    assert cli.cmd_vault_sign(cli.build_parser().parse_args(args)) == 0
    result = json.loads(capfd.readouterr().out)
    assert result["kind"] == "vault_sign_request" and result["request"]["status"] == "pending"
    release = result["request"]["delivery"]["operation_context"]["release"]
    assert release["signingKey"]["public_key"] == key and release["derivation"]["account"] == 1
    assert ("address_type" in release["derivation"]) == (chain == "bitcoin")


def test_false_flags_do_not_turn_protected_custody_into_headless():
    signing, _, _ = signing_wallet()
    with api._vault_engine().begin() as conn:
        meta = json.loads(stored()["public_meta"])
        meta["hd_accounts"][1]["always_ask"] = False
        conn.execute(vault_secrets.update().values(public_meta=json.dumps(meta), policy=json.dumps({"always_ask": False})))
    assert api.vault_sign(signing)["request"]["status"] == "pending"
    assert stored()["use_count"] == 0


@pytest.mark.parametrize("mutation", ["missing-type", "legacy", "segwit", "other-account", "wrong-chain", "missing-chain", "missing-account"])
def test_bitcoin_completion_never_repairs_or_switches_persisted_selection(mutation):
    signing, signature, _ = signing_wallet("bitcoin")
    request = api.vault_sign(signing)["request"]
    completion = {**signing, **request["delivery"]["derivation"], "request_id": request["id"], "signature": signature}
    if mutation == "missing-type":
        completion.pop("address_type")
    elif mutation in ("legacy", "segwit"):
        completion["address_type"] = "p2pkh" if mutation == "legacy" else "p2wpkh"
    elif mutation == "other-account":
        completion["account"] = 0
    elif mutation == "wrong-chain":
        completion["chain"] = "evm"
    elif mutation == "missing-chain":
        completion.pop("chain")
    else:
        completion.pop("account")
    with pytest.raises(api.VaultApiError):
        api.vault_sign(completion)
    assert api.get_vault_request(request["id"])["request"]["status"] == "pending"
    assert stored()["use_count"] == 0


@pytest.mark.parametrize("chain", ["evm", "bitcoin", "solana"])
def test_direct_storage_completion_verifies_signature_and_rolls_back_caught_postclaim_failure(chain, monkeypatch):
    signing, signature, _ = signing_wallet(chain)
    request = api.vault_sign(signing)["request"]
    kwargs = {"name": signing["name"], "scheme": signing["scheme"], "digest": signing.get("digest"),
              "message_hex": signing.get("message_hex"), "derivation": request["delivery"]["derivation"],
              "signing_context": signing["signing_context"],
              "signature": (copy.deepcopy(next(item["signature"] for item in SIGNATURES["signatures"]
                  if item["chain"] == "evm" and item["account"] == 0)) if chain == "evm" else {"signature": "00" * 64})}
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError):
            vault_service.complete_sign_request(conn, request["id"], **kwargs)
    original_record = vault_service.record_signing_use
    def fail_after_use(*args, **kwargs):
        original_record(*args, **kwargs)
        raise RuntimeError("synthetic postclaim usage failure")
    monkeypatch.setattr(vault_service, "record_signing_use", fail_after_use)
    with api._vault_engine().begin() as conn:
        with pytest.raises(RuntimeError, match="postclaim"):
            vault_service.complete_sign_request(conn, request["id"], **{**kwargs, "signature": signature})
    assert api.get_vault_request(request["id"])["request"]["status"] == "pending"
    assert stored()["use_count"] == 0 and stored()["last_used_at"] is None
    assert json.loads(stored()["public_meta"])["hd_accounts"][1]["last_signer"] is None
    assert not any(item["event"] == "signed" for item in rows(vault_audit))


def test_label_only_rename_is_cosmetic_while_pending():
    signing, signature, _ = signing_wallet()
    request = api.vault_sign(signing)["request"]
    api.update_vault_hd_account(signing["name"], {"name": "Renamed after intake"}, account=1)
    assert complete(signing, request, signature)["ok"]
    assert json.loads(stored()["public_meta"])["hd_accounts"][1]["name"] == "Renamed after intake"


@pytest.mark.parametrize("mutation", ["id", "record-name", "envelope", "raw-wrap", "profile", "projection", "table-and-projection",
    "release-key", "release-input", "release-selection", "release-extra", "request-id", "missing-snapshot", "missing-derivation"])
def test_fresh_completion_refuses_security_identity_or_snapshot_changes(mutation):
    signing, signature, _ = signing_wallet()
    request = api.vault_sign(signing)["request"]
    with api._vault_engine().begin() as conn:
        row = stored()
        meta = json.loads(row["public_meta"])
        if mutation == "id":
            conn.execute(vault_secrets.update().values(id="vlt_replacement"))
        elif mutation == "record-name":
            conn.execute(vault_secrets.update().values(name="RENAMED_RECORD"))
        elif mutation == "envelope":
            conn.execute(vault_secrets.update().values(nonce=base64.b64encode(b"different IV").decode()))
        elif mutation == "raw-wrap":
            conn.execute(vault_secrets.update().values(wrap_meta=row["wrap_meta"] + " "))
        elif mutation in ("profile", "projection", "table-and-projection"):
            if mutation == "profile":
                meta["bitcoin_profile"] = "unknown"
            else:
                meta["hd_accounts"][1]["evm"] = meta["hd_accounts"][0]["evm"]
            changes = {"public_meta": json.dumps(meta)}
            if mutation == "table-and-projection":
                wrap = json.loads(row["wrap_meta"])
                wrap["record_meta"]["accounts"][1]["evm"] = meta["hd_accounts"][1]["evm"]
                changes["wrap_meta"] = json.dumps(wrap)
            conn.execute(vault_secrets.update().values(**changes))
        else:
            delivery = copy.deepcopy(request["delivery"])
            operation = delivery["operation_context"]
            if mutation == "release-key":
                operation["release"]["publicKey"] = meta["hd_accounts"][0]["evm"]
            elif mutation == "release-input":
                operation["release"]["digest"] = "02" * 32
            elif mutation == "release-selection":
                operation["release"]["derivation"]["account"] = 0
            elif mutation == "release-extra":
                operation["release"]["extra"] = True
            elif mutation == "request-id":
                operation["requestId"] = "vrq_other"
            elif mutation == "missing-snapshot":
                delivery.pop("operation_context")
            else:
                delivery.pop("derivation")
            conn.execute(vault_requests.update().where(vault_requests.c.id == request["id"]).values(delivery=json.dumps(delivery)))
    with pytest.raises(api.VaultApiError):
        complete(signing, request, signature)
    current = next(item for item in rows(vault_requests) if item["id"] == request["id"])
    assert current["status"] == "pending" and stored()["use_count"] == 0
    assert not any(item["event"] == "signed" for item in rows(vault_audit))


@pytest.mark.parametrize("chain", ["evm", "solana"])
@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh", "p2tr"])
def test_non_bitcoin_chain_refuses_address_type_before_intake(chain, address_type):
    signing, _, _ = signing_wallet(chain)
    with pytest.raises(api.VaultApiError):
        api.vault_sign({**signing, "address_type": address_type})
    assert rows(vault_requests) == []


@pytest.mark.parametrize("kind", ["avault-agent-operation", "solana-transaction-v1", [], {}])
def test_direct_storage_intake_refuses_wrong_chain_context_without_writes(kind):
    signing, _, _ = signing_wallet()
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidSigningContextError):
            vault_service.create_sign_request(conn, signing["name"], digest=signing["digest"], scheme=signing["scheme"],
                derivation={"chain": "evm", "account": 1}, signing_context={**signing["signing_context"], "kind": kind})
    assert rows(vault_requests) == []


def test_protected_hd_policy_downgrade_challenge_is_not_supported():
    signing, _, _ = signing_wallet()
    before = stored()
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.SigningPolicyError, match="unsupported HD tier"):
            vault_service.create_signing_policy_challenge(conn, signing["name"], {"always_ask": False, "account": 1},
                rp_id="sandbox.avibe.bot", origin=ORIGIN)
    assert stored() == before


def test_protected_hd_signing_refuses_disabled_account():
    signing, _, _ = signing_wallet()
    with pytest.raises(api.VaultApiError, match="enabled"):
        api.vault_sign({**signing, "account": 2})
    assert rows(vault_requests) == []


def test_two_concurrent_completions_deliver_exactly_one_signature():
    signing, signature, _ = signing_wallet("solana")
    request = api.vault_sign(signing)["request"]
    engine = api._vault_engine()
    barrier = Barrier(2)
    def attempt():
        barrier.wait(timeout=5)
        try:
            with engine.begin() as conn:
                result = vault_service.complete_sign_request(conn, request["id"], name=signing["name"], scheme=signing["scheme"],
                    message_hex=signing["message_hex"], signing_context=signing["signing_context"], signature=signature,
                    derivation=request["delivery"]["derivation"])
            return result["status"]
        except vault_service.InvalidRequestError:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["approved", "refused"]
    assert stored()["use_count"] == 1
    assert sum(item["event"] == "signed" for item in rows(vault_audit)) == 1


def test_same_name_concurrent_create_and_committed_response_loss_do_not_replace_winner():
    value = payload()
    engine = api._vault_engine()
    barrier = Barrier(2)
    def attempt():
        barrier.wait(timeout=5)
        try:
            with engine.begin() as conn:
                result = storage_create(conn, value, user_context=resource_access_service.ResourceUserContext(
                    instance_role="editor", subject="creator", is_remote=True))
            return result["name"]
        except vault_service.SecretExistsError:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == sorted([value["name"], "refused"])
    before = stored()
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(value)
    assert error.value.code == "secret_exists" and stored() == before
    assert len(rows(vault_secrets)) == len(rows(resource_access_policies)) == 1


def test_expired_request_cannot_complete_or_record_signing_use():
    signing, signature, _ = signing_wallet()
    request = api.vault_sign(signing)["request"]
    with api._vault_engine().begin() as conn:
        conn.execute(vault_requests.update().values(expires_at="2000-01-01T00:00:00+00:00"))
    with pytest.raises(api.VaultApiError, match="expired"):
        complete(signing, request, signature)
    assert stored()["use_count"] == 0 and not any(item["event"] == "signed" for item in rows(vault_audit))


def test_nonestablishing_create_requires_a_current_root_without_side_effects():
    value = payload()
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.delete().where(state_meta.c.key == vault_setup.META_KEY))
        with pytest.raises(vault_service.ProtectedAuthzSetupRequiredError):
            storage_create(conn, value)
    assert rows(vault_secrets) == rows(vault_audit) == rows(resource_access_policies) == rows(vault_auth_factors) == []
    assert not any(item["key"] == vault_setup.META_KEY for item in rows(state_meta))


def test_matching_durable_factorless_root_is_reused_without_registration_or_rewrite():
    value = payload()
    before = rows(state_meta)
    wrap = json.loads(value["sealed"]["wrap_meta"])
    value["sealed"]["wrap_meta"] = json.dumps(dict(reversed(list(wrap.items()))), indent=1)
    assert api.create_vault_secret(value)["ok"]
    assert rows(state_meta) == before and rows(vault_auth_factors) == []
    assert stored()["wrap_meta"] == value["sealed"]["wrap_meta"]


@pytest.mark.parametrize("mutation", ["nonce", "wrapped", "prf_salt", "rp_id", "root_meta", "extension"])
def test_nonestablishing_create_binds_full_root_not_just_scope_or_credential(mutation):
    value = payload()
    wrap = json.loads(value["sealed"]["wrap_meta"])
    if mutation in ("nonce", "wrapped", "prf_salt"):
        wrap["copies"][0][mutation] = base64.b64encode(b"x" * {"nonce": 12, "wrapped": 48, "prf_salt": 32}[mutation]).decode()
    else:
        wrap[mutation] = {"changed": True} if mutation != "rp_id" else "unrelated.example"
    value["sealed"]["wrap_meta"] = json.dumps(wrap)
    before = rows(state_meta)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(value)
    assert error.value.code == "vault_already_initialized"
    assert rows(state_meta) == before and rows(vault_secrets) == rows(vault_audit) == rows(vault_auth_factors) == []


def test_root_boolean_is_not_equal_to_json_number():
    value = payload()
    wrap = json.loads(value["sealed"]["wrap_meta"])
    root = {key: child for key, child in wrap.items() if key not in {"scheme", "record_meta", "wrapped_dek", "dek_nonce"}}
    root["extension"] = {"version": 1}
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.update().where(state_meta.c.key == vault_setup.META_KEY).values(
            value_json=json.dumps({"v": 1, "wrap_meta": json.dumps(root)})))
    wrap["extension"] = {"version": True}
    value["sealed"]["wrap_meta"] = json.dumps(wrap)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(value)
    assert error.value.code == "vault_already_initialized" and rows(vault_secrets) == []


@pytest.mark.parametrize("preserved", [False, True])
def test_legacy_factorless_root_allows_matching_hd_before_and_after_last_record_deletion(preserved):
    value = payload()
    root = {key: child for key, child in json.loads(value["sealed"]["wrap_meta"]).items()
            if key not in {"scheme", "record_meta", "wrapped_dek", "dek_nonce"}}
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.delete().where(state_meta.c.key == vault_setup.META_KEY))
    api.create_vault_secret({"name": "LEGACY_ROOT", "protection": "protected", "sealed": {
        "ciphertext": "legacy-public-ciphertext", "nonce": "legacy-public-nonce", "wrap_meta": json.dumps(root)}})
    if preserved:
        api.delete_vault_secret("LEGACY_ROOT")
    before = rows(state_meta)
    assert api.create_vault_secret(value)["ok"]
    assert rows(vault_auth_factors) == [] and rows(state_meta) == before
    if preserved:
        setup = json.loads(next(item["value_json"] for item in before if item["key"] == vault_setup.META_KEY))
        assert setup["source"] == "legacy-record" and setup["factor_id"] is None


def test_corrupt_durable_root_does_not_fall_back_to_a_matching_legacy_record():
    value = payload()
    root = {key: child for key, child in json.loads(value["sealed"]["wrap_meta"]).items()
            if key not in {"scheme", "record_meta", "wrapped_dek", "dek_nonce"}}
    api.create_vault_secret({"name": "LEGACY_ROOT", "protection": "protected", "sealed": {
        "ciphertext": "legacy-public-ciphertext", "nonce": "legacy-public-nonce", "wrap_meta": json.dumps(root)}})
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.update().where(state_meta.c.key == vault_setup.META_KEY).values(
            value_json=json.dumps({"v": 1, "wrap_meta": "corrupt"})))
    before = rows(vault_secrets), rows(vault_audit), rows(state_meta)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(value)
    assert error.value.code == "vault_policy_factor_unavailable"
    assert (rows(vault_secrets), rows(vault_audit), rows(state_meta)) == before


def typed_envelope(kind="hd-seed"):
    value = payload()
    if kind != "hd-seed":
        wrap = json.loads(value["sealed"]["wrap_meta"])
        wrap["record_meta"]["kind"] = kind
        value["sealed"]["wrap_meta"] = json.dumps(wrap)
    return Sealed(**value["sealed"])


@pytest.mark.parametrize("operation", ["create-extra", "replace-default", "add-extra", "replace-extra", "rotate"])
@pytest.mark.parametrize("kind", ["hd-seed", "future-kind"])
def test_generic_value_ingest_refuses_typed_signer_or_unknown_envelopes_before_effects(operation, kind):
    sealed = typed_envelope(kind)
    if kind == "hd-seed":
        assert vault_protected_hd.envelope_table(FIXTURE["cases"][0]["name"], sealed) == FIXTURE["cases"][0]["record_meta"]["accounts"]
    if operation != "create-extra":
        api.create_vault_secret({"name": "STATIC", "protection": "protected", "sealed": {
            "ciphertext": "legacy-default", "nonce": "legacy-nonce", "wrap_meta": "legacy-wrap"},
            "fields": [{"name": "extra", "record": "STATIC.extra", "sealed": {"ciphertext": "legacy-extra", "nonce": "n", "wrap_meta": "w"}}]
                      if operation == "replace-extra" else []})
    before = rows(vault_secrets), rows(vault_audit), rows(vault_requests), rows(resource_access_policies)
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.InvalidRequestError):
            if operation == "create-extra":
                vault_service.create_secret(conn, name="STATIC", protection="protected", sealed=Sealed("legacy", "n", "w"),
                    fields=[{"name": "extra", "sealed": sealed}])
            elif operation == "rotate":
                vault_service.rotate_secret(conn, "STATIC", sealed)
            else:
                default = operation == "replace-default"
                vault_service.update_secret_field(conn, "STATIC", "value" if default else "extra", sealed,
                    record="STATIC" if default else "STATIC.extra")
    assert (rows(vault_secrets), rows(vault_audit), rows(vault_requests), rows(resource_access_policies)) == before


@pytest.mark.parametrize("kind", ["hd-seed", "future-kind"])
def test_selected_typed_extra_refuses_reveal_capture_selector_and_dek_contexts(kind, monkeypatch):
    api.create_vault_secret({"name": "STATIC", "protection": "protected", "sealed": {
        "ciphertext": "legacy-default", "nonce": "legacy-nonce", "wrap_meta": "legacy-wrap"}})
    request = api.request_vault_access({"name": "STATIC"})["request"]
    grant_id = request["card"]["grant_options"][0]["grant_id"]
    sealed = typed_envelope(kind)
    field = {"name": "extra", "ciphertext": sealed.ciphertext, "nonce": sealed.nonce, "wrap_meta": sealed.wrap_meta,
             "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"}
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().values(fields=json.dumps([field])))
    custody = Mock(side_effect=AssertionError("wrong typed material must refuse before custody discovery"))
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", custody)
    monkeypatch.setattr(api, "avault_agent_pubkey", custody)
    before = rows(vault_audit), rows(vault_requests)
    with api._vault_engine().begin() as conn:
        for operation in (
            lambda: vault_service.get_protected_record_envelope(conn, "STATIC.extra"),
            lambda: vault_service.capture_deliveries(conn, ["STATIC.extra"]),
            lambda: vault_service.expand_value_delivery_selector(conn, source_selector={"env": ["STATIC.extra"]}),
            lambda: vault_service.resolve_secret_access(conn, "STATIC.extra"),
            lambda: vault_service.request_grantable_member_metas(conn, request["id"]),
        ):
            with pytest.raises(vault_service.KeypairNotValueDeliverableError):
                operation()
    for operation in (
        lambda: api.create_vault_reveal_context("STATIC", {"field": "extra"}),
        lambda: api.create_vault_agent_binding({"name": "STATIC.extra", "request_id": request["id"], "grant_id": grant_id}),
        lambda: api.create_vault_agent_bindings_batch({"request_id": request["id"]}),
    ):
        with pytest.raises(api.VaultApiError):
            operation()
    current = api.get_vault_request(request["id"])["request"]
    assert current["status"] == "pending"
    assert "secret_unlock_material" not in current["card"]
    assert "unlock_material" not in current["card"]["grant_options"][0]
    assert (rows(vault_audit), rows(vault_requests)) == before
    custody.assert_not_called()


@pytest.mark.parametrize("kind", ["hd-seed", "future-kind"])
def test_ordinary_signing_owner_refuses_disguised_typed_envelope(kind):
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="ORDINARY", kind="keypair", signer_kind="local", sealed=Sealed("legacy", "n", "w"),
            public_meta={"signing_public_key": {"curve": "secp256k1", "public_key": FIXTURE["cases"][0]["record_meta"]["accounts"][0]["evm"]}})
        sealed = typed_envelope(kind)
        conn.execute(vault_secrets.update().values(ciphertext=sealed.ciphertext, nonce=sealed.nonce, wrap_meta=sealed.wrap_meta))
        with pytest.raises(vault_service.InvalidSigningKeyError, match="ordinary signing"):
            vault_service.get_signing_public_key(conn, "ORDINARY")
    assert rows(vault_requests) == []


@pytest.mark.parametrize("history", ["active", "disabled"])
def test_factor_history_without_root_does_not_attach_an_hd_root(history):
    value = payload()
    credential = WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin=ORIGIN)
    options = api.create_vault_authz_webauthn_options(origin=ORIGIN)
    registration = credential.registration_payload(challenge_id=options["challenge_id"], challenge_b64=options["webauthn"]["challenge"])
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.delete().where(state_meta.c.key == vault_setup.META_KEY))
        vault_service.register_webauthn_factor(conn, registration, rp_id="sandbox.avibe.bot", origin=ORIGIN, establishment=True)
        if history == "disabled":
            conn.execute(vault_auth_factors.update().values(disabled_at="disabled"))
    before = rows(vault_auth_factors), rows(vault_audit), rows(state_meta)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(value)
    assert error.value.code == "vault_policy_factor_unavailable" and rows(vault_secrets) == []
    assert (rows(vault_auth_factors), rows(vault_audit), rows(state_meta)) == before


def test_no_durable_root_does_not_search_older_matching_legacy_records():
    value = payload()
    root = {key: child for key, child in json.loads(value["sealed"]["wrap_meta"]).items()
            if key not in {"scheme", "record_meta", "wrapped_dek", "dek_nonce"}}
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.delete().where(state_meta.c.key == vault_setup.META_KEY))
    for name, candidate in (("OLDER_MATCH", root), ("CURRENT_OTHER", WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin=ORIGIN).wrap_meta(marker=2))):
        api.create_vault_secret({"name": name, "protection": "protected", "sealed": {
            "ciphertext": "legacy-public-ciphertext", "nonce": "legacy-public-nonce", "wrap_meta": json.dumps(candidate)}})
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret(value)
    assert error.value.code == "vault_already_initialized"
    assert len(rows(vault_secrets)) == 2 and rows(vault_auth_factors) == []
    assert not any(item["key"] == vault_setup.META_KEY for item in rows(state_meta))


def test_fresh_hd_create_establishes_one_shared_root_and_factor_atomically():
    credential = WebAuthnTestCredential(rp_id="sandbox.avibe.bot", origin=ORIGIN)
    options = api.create_vault_authz_webauthn_options(origin=ORIGIN)
    setup = {"wrap_meta": credential.wrap_meta(), "authz_factor_registration": credential.registration_payload(
        challenge_id=options["challenge_id"], challenge_b64=options["webauthn"]["challenge"])}
    with api._vault_engine().begin() as conn:
        conn.execute(state_meta.delete().where(state_meta.c.key == vault_setup.META_KEY))
    value = payload(setup=setup)
    assert api.create_vault_secret({**value, "establishing_vmk": True,
        "authz_factor_registration": setup["authz_factor_registration"]}, origin=ORIGIN)["ok"]
    assert len(rows(vault_secrets)) == len(rows(vault_auth_factors)) == 1
    assert sum(item["key"] == vault_setup.META_KEY for item in rows(state_meta)) == 1
    before = rows(vault_auth_factors), rows(vault_audit), rows(state_meta), rows(vault_secrets)
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**value, "establishing_vmk": True,
            "authz_factor_registration": setup["authz_factor_registration"]}, origin=ORIGIN)
    assert error.value.code == "secret_exists"
    assert (rows(vault_auth_factors), rows(vault_audit), rows(state_meta), rows(vault_secrets)) == before

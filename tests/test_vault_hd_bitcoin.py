"""BTC purpose selection/public preparation at its owning API/storage boundaries.

Observable contracts: displayed/stored/selected-signing identity, atomic five-address
create, identity-CAS additive preparation, and immutable approval selection. Credible
regressions are re-encoding BIP86 as BIP84, replacing mutable metadata after a slow
custody call, or dropping a same-scheme address_type during approval. The old HD
suite only exercised Taproot; the independent corpus and DER fixtures never fund.
"""
from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
import pytest

from storage import resource_access_service, vault_hd, vault_service
from storage.models import vault_audit, vault_requests, vault_secrets
from tests.test_vault_hd import BLIND_BOX, CREATE, DIGEST, FIXTURE, create, custody, meta, rows  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers
from vibe import api, cli

ROOT = Path(__file__).parent / "fixtures/vault_signing/hd-bitcoin"
CORPUS = json.loads((ROOT / "corpus.json").read_text())
DER = json.loads((ROOT / "der-signatures.json").read_text())
TRANSACTIONS = json.loads((ROOT / "transaction-signing-vectors.json").read_text())
ENTRY = next(item for item in CORPUS["entries"] if item["id"] == "bip86-abandon-no-pass")
PROFILE = vault_hd.BITCOIN_PROFILE
NAME = CREATE["name"]
TYPES = ["p2pkh", "p2wpkh", "p2tr"]


def table(entry=ENTRY):
    incoming = []
    for account in entry["accounts"]:
        evm = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), bytes.fromhex(account["public_keys"]["evm"]),
        ).public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint).hex()
        incoming.append({"account": account["account"], **account["public_keys"], "evm": evm})
    return incoming


def public_response():
    return {"type": "hd-seed", "profile": PROFILE, "accounts": table()}


def expected():
    account = ENTRY["accounts"][0]
    return {"eth": account["eth"], "solana": account["solana"], "btc_taproot": account["btc_taproot"],
            "btc_legacy": account["btc_p2pkh"], "btc_segwit": account["btc_p2wpkh"]}


def create_extended(custody):
    custody.reply = {**public_response(), "envelope": copy.deepcopy(FIXTURE["import_response"]["envelope"])}
    return api.create_vault_secret({**copy.deepcopy(CREATE),
        "keypair": {**CREATE["keypair"], "profile": PROFILE}, "expected_account0": expected()})["secret"]


def prepare(custody):
    custody.reply = public_response()
    return api.prepare_vault_hd_bitcoin_types(NAME, {})["secret"]


def sign_input(kind, account=0, **extra):
    return {"name": NAME, "chain": "bitcoin", "account": account, "address_type": kind, "digest": DIGEST, **extra}


def sign_reply(kind, account=0):
    if kind == "p2tr":
        return next(item["signature"] for item in FIXTURE["signatures"]
                    if item["chain"] == "bitcoin" and item["account"] == account)
    item = next(item for item in DER["signatures"] if item["address_type"] == kind and item["account"] == account)
    return {key: item[key] for key in ("signature", "recovery_id")}


@pytest.fixture
def runtime(custody, monkeypatch):
    custody.version = "0.1.12"
    monkeypatch.setattr(api, "_hd_bitcoin_types_available", lambda: api._version_at_least(custody.version, "0.1.12"))
    monkeypatch.setattr(api, "_publish_vaults_updated", Mock())
    return custody


@pytest.mark.parametrize("entry", CORPUS["entries"], ids=lambda entry: entry["id"])
def test_all_twenty_public_identities_and_selected_purpose_keys_match_independent_oracle(entry):
    incoming = table(entry)
    accounts = vault_hd.import_accounts(incoming, profile=PROFILE)
    for item in accounts:
        item["enabled"] = True
    stored = {"hd_accounts": accounts, "bitcoin_profile": PROFILE}
    observed = vault_hd.public_accounts(stored)
    assert len(observed) == 20
    for actual, oracle in zip(observed, entry["accounts"], strict=True):
        assert actual["eth"] == oracle["eth"] and actual["solana"] == oracle["solana"]
        assert actual["btc_taproot"] == oracle["btc_taproot"]
        assert actual["btc_legacy"] == oracle["btc_p2pkh"]
        assert actual["btc_segwit"] == oracle["btc_p2wpkh"]
        for kind, field in vault_hd.BITCOIN_KEYS.items():
            selection = vault_hd.derivation("bitcoin", actual["account"], kind)
            assert vault_hd.signing_public_key(stored, selection)["public_key"] == incoming[actual["account"]][field]
            path = oracle["paths"]["bitcoin" if kind == "p2tr" else "bitcoin_" + kind]
            assert actual["bitcoin_paths"][kind] == path
        assert actual["paths"]["bitcoin"] == oracle["paths"]["bitcoin"]
    assert hashlib.sha256((ROOT / "corpus.json").read_bytes()).hexdigest() == DER["corpus_sha256"]


def test_extended_create_checks_five_addresses_and_stores_only_public_profile(runtime):
    secret = create_extended(runtime)
    assert runtime.calls[0][0][-2:] == ["--profile", PROFILE]
    assert secret["bitcoin_profile"] == PROFILE
    assert secret["accounts"][0]["bitcoin_address_types"] == TYPES
    assert {field: secret["accounts"][0][field] for field in expected()} == expected()
    row, = rows(vault_secrets)
    assert {field: row[field] for field in ("ciphertext", "nonce", "wrap_meta")} == FIXTURE["import_response"]["envelope"]
    stored = json.loads(row["public_meta"])
    assert stored["bitcoin_profile"] == PROFILE
    assert len(stored["hd_accounts"]) == 20
    assert "seed" not in stored and "words" not in stored
    assert secret["accounts"][0]["paths"]["bitcoin"] == "m/86'/0'/0'/0/0"


@pytest.mark.parametrize("field", list(expected()))
def test_any_five_address_mismatch_leaves_no_row_audit_or_publication(runtime, field, monkeypatch):
    runtime.reply = {**public_response(), "envelope": FIXTURE["import_response"]["envelope"]}
    wrong = expected()
    other = ENTRY["accounts"][1]
    wrong[field] = other[{"btc_legacy": "btc_p2pkh", "btc_segwit": "btc_p2wpkh"}.get(field, field)]
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**copy.deepcopy(CREATE), "keypair": {**CREATE["keypair"], "profile": PROFILE},
                                 "expected_account0": wrong})
    assert error.value.code == "hd_address_check_failed"
    assert rows(vault_secrets) == rows(vault_audit) == []
    api._publish_vaults_updated.assert_not_called()


@pytest.mark.parametrize("expected_value", [None, {}, {"eth": "0x" + "11" * 20}, "missing"])
def test_extended_create_requires_exact_five_before_custody(runtime, expected_value):
    body = {**copy.deepcopy(CREATE), "keypair": {**CREATE["keypair"], "profile": PROFILE}}
    if expected_value != "missing":
        body["expected_account0"] = expected_value
    with pytest.raises(api.VaultApiError):
        api.create_vault_secret(body)
    assert runtime.calls == [] and rows(vault_secrets) == []


def test_existing_wallet_preparation_preserves_seed_and_mutable_concurrent_changes(runtime):
    before = create(runtime)
    original, = rows(vault_secrets)

    def edit_while_custody_is_busy():
        runtime.hook = None
        # A separate connection can WRITE here: no custody-time writer reservation.
        api.update_vault_hd_account(NAME, {})
        api.update_vault_hd_account(NAME, {"name": "Preserved", "always_ask": True}, account=0)
        with api._vault_engine().begin() as conn:
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == NAME).values(use_count=17))

    runtime.hook = edit_while_custody_is_busy
    result = prepare(runtime)
    assert runtime.calls == [(["keypair", "derive-public"], {
        "name": NAME, "key_envelope": FIXTURE["import_response"]["envelope"], "profile": PROFILE,
    })]
    row, = rows(vault_secrets)
    for field in ("id", "ciphertext", "nonce", "wrap_meta", "kind", "signer_kind", "protection", "created_at"):
        assert row[field] == original[field]
    assert row["use_count"] == 17
    assert result["accounts"][0]["btc_taproot"] == before["accounts"][0]["btc_taproot"]
    assert result["accounts"][0]["name"] == "Preserved"
    assert result["accounts"][0]["always_ask"] is True
    assert [item["account"] for item in result["accounts"]] == [0, 1]
    runtime.calls.clear()
    before_repeat = rows(vault_secrets)[0]
    assert prepare(runtime)["accounts"] == result["accounts"]
    assert rows(vault_secrets)[0] == before_repeat
    assert len(runtime.calls) == 1 and runtime.calls[0][0] == ["keypair", "derive-public"]
    assert sum(row["event"] == "bitcoin-address-types-prepared" for row in rows(vault_audit)) == 1


@pytest.mark.parametrize("mutation", ["row-id", "envelope", "base-key", "delete"])
def test_preparation_refuses_changed_identity_without_partial_new_keys(runtime, mutation):
    create(runtime)

    def replace_identity():
        runtime.hook = None
        with api._vault_engine().begin() as conn:
            row = vault_service.require_secret_access(conn, NAME)
            if mutation == "delete":
                conn.execute(vault_secrets.delete().where(vault_secrets.c.id == row["id"]))
            else:
                public = json.loads(row["public_meta"])
                if mutation == "base-key":
                    public["hd_accounts"][0]["bitcoin"] = public["hd_accounts"][1]["bitcoin"]
                changes = {"id": "replacement-id"} if mutation == "row-id" else {"nonce": "changed-envelope"} if mutation == "envelope" else {"public_meta": json.dumps(public)}
                conn.execute(vault_secrets.update().where(vault_secrets.c.id == row["id"]).values(**changes))

    runtime.hook = replace_identity
    with pytest.raises(api.VaultApiError) as error:
        prepare(runtime)
    assert error.value.code == "hd_bitcoin_types_state_changed"
    for row in rows(vault_secrets):
        assert "bitcoin_p2pkh" not in row["public_meta"]
    assert not any(row["event"] == "bitcoin-address-types-prepared" for row in rows(vault_audit))


@pytest.mark.parametrize("corruption", ["profile", "envelope-leak", "short", "account-bool", "extra", "bad-point", "wrong-base", "duplicate"])
def test_preparation_strict_public_response_refusals_are_atomic(runtime, corruption):
    create(runtime)
    runtime.reply = public_response()
    if corruption == "profile":
        runtime.reply["profile"] = "unknown"
    elif corruption == "envelope-leak":
        runtime.reply["envelope"] = FIXTURE["import_response"]["envelope"]
    elif corruption == "short":
        runtime.reply["accounts"].pop()
    elif corruption == "account-bool":
        runtime.reply["accounts"][0]["account"] = False
    elif corruption == "extra":
        runtime.reply["accounts"][0]["chaincode"] = "forbidden"
    elif corruption == "bad-point":
        runtime.reply["accounts"][19]["bitcoin_p2wpkh"] = "02" + "ff" * 32
    elif corruption == "duplicate":
        runtime.reply["accounts"][19]["bitcoin_p2pkh"] = runtime.reply["accounts"][18]["bitcoin_p2pkh"]
    else:
        runtime.reply["accounts"][0]["bitcoin"] = ENTRY["accounts"][0]["public_keys"]["bitcoin_p2pkh"]
    with pytest.raises(api.VaultApiError) as error:
        api.prepare_vault_hd_bitcoin_types(NAME, {})
    assert error.value.code == "hd_bitcoin_types_integrity_failed"
    assert "bitcoin_profile" not in rows(vault_secrets)[0]["public_meta"]
    assert len(rows(vault_audit)) == 1


@pytest.mark.parametrize("stage", ["create", "prepare"])
@pytest.mark.parametrize("field,source_field,source_account", [
    (field, source_field, source_account)
    for field in ("bitcoin_p2pkh", "bitcoin_p2wpkh")
    for source_field in ("bitcoin", "bitcoin_p2pkh", "bitcoin_p2wpkh")
    for source_account in (0, 19)
    if source_account != 19 or source_field != field
])
def test_profiled_bitcoin_points_are_distinct_across_purposes_and_indices(
    runtime, stage, field, source_field, source_account,
):
    # Same-column checks missed valid SEC1 aliases across purpose44/84/86, so
    # malformed custody tables could advertise a spendable but wrong-purpose key.
    if stage == "prepare":
        create(runtime)
    before, audits = rows(vault_secrets), rows(vault_audit)
    api._publish_vaults_updated.reset_mock()
    runtime.reply = public_response()
    runtime.reply["accounts"][19][field] = runtime.reply["accounts"][source_account][source_field]
    with pytest.raises(api.VaultApiError) as error:
        if stage == "create":
            runtime.reply["envelope"] = copy.deepcopy(FIXTURE["import_response"]["envelope"])
            api.create_vault_secret({**copy.deepcopy(CREATE),
                "keypair": {**CREATE["keypair"], "profile": PROFILE}, "expected_account0": expected()})
        else:
            api.prepare_vault_hd_bitcoin_types(NAME, {})
    assert error.value.code == ("avault_failed" if stage == "create" else "hd_bitcoin_types_integrity_failed")
    assert rows(vault_secrets) == before and rows(vault_audit) == audits
    assert rows(vault_requests) == []
    api._publish_vaults_updated.assert_not_called()


def test_two_concurrent_equal_preparations_are_idempotent(runtime):
    create(runtime)
    runtime.reply = public_response()
    barrier = Barrier(2)
    runtime.hook = lambda: barrier.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: api.prepare_vault_hd_bitcoin_types(NAME, {}), range(2)))
    assert all(result["secret"]["bitcoin_profile"] == PROFILE for result in results)
    assert len(runtime.calls) == 2
    assert sum(row["event"] == "bitcoin-address-types-prepared" for row in rows(vault_audit)) == 1


def test_preparation_audit_failure_rolls_back_then_lost_response_is_safe(runtime, monkeypatch):
    create(runtime)
    runtime.reply = public_response()
    original = rows(vault_secrets)[0]
    audit = vault_service.audit
    monkeypatch.setattr(vault_service, "audit", Mock(side_effect=RuntimeError("synthetic audit failure")))
    with pytest.raises(RuntimeError, match="audit failure"):
        api.prepare_vault_hd_bitcoin_types(NAME, {})
    assert rows(vault_secrets)[0] == original
    monkeypatch.setattr(vault_service, "audit", audit)
    with monkeypatch.context() as patch:
        patch.setattr(api, "_publish_vaults_updated", Mock(side_effect=RuntimeError("lost response")))
        with pytest.raises(RuntimeError, match="lost response"):
            api.prepare_vault_hd_bitcoin_types(NAME, {})
    runtime.calls.clear()
    assert api.prepare_vault_hd_bitcoin_types(NAME, {})["secret"]["bitcoin_profile"] == PROFILE
    assert len(runtime.calls) == 1 and runtime.calls[0][0] == ["keypair", "derive-public"]


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_prepare_requires_resource_management_before_custody(runtime, role):
    create(runtime)
    context = resource_access_service.ResourceUserContext(instance_role=role, subject="not-owner", is_remote=True)
    with pytest.raises(api.VaultApiError) as error:
        api.prepare_vault_hd_bitcoin_types(NAME, {}, user_context=context)
    assert error.value.code == "forbidden" and error.value.status == 403
    assert runtime.calls == []


def test_prepare_http_route_empty_payload_and_old_runtime_fail_closed(runtime):
    from vibe.ui_server import app

    create(runtime)
    runtime.reply = public_response()
    client = app.test_client()
    headers = csrf_headers(client)
    runtime.version = "0.1.11"
    response = client.post(f"/api/vault/secrets/{NAME}/bitcoin-address-types", json={}, headers=headers)
    assert response.status_code == 409
    assert response.get_json()["code"] == "avault_upgrade_required"
    assert response.get_json()["minimum_version"] == "0.1.12"
    assert runtime.calls == [] and "bitcoin_profile" not in rows(vault_secrets)[0]["public_meta"]
    runtime.version = "0.1.12"
    for invalid in ({"account": 0}, {"profile": PROFILE}, [], "", 1):
        response = client.post(f"/api/vault/secrets/{NAME}/bitcoin-address-types", json=invalid, headers=headers)
        assert response.status_code == 400
    assert runtime.calls == []
    response = client.post(f"/api/vault/secrets/{NAME}/bitcoin-address-types", json={}, headers=headers)
    assert response.status_code == 200 and response.get_json()["secret"]["bitcoin_profile"] == PROFILE


def test_all_twenty_new_type_signatures_use_selected_key_and_scheme(runtime):
    create_extended(runtime)
    for _ in range(19):
        api.update_vault_hd_account(NAME, {})
    runtime.calls.clear()
    for vector in DER["signatures"]:
        kind, account = vector["address_type"], vector["account"]
        runtime.reply = sign_reply(kind, account)
        result = api.vault_sign(sign_input(kind, account))
        assert result["signature"] == runtime.reply
        assert result["signing_public_key"] == {"curve": "secp256k1", "public_key": vector["public_key"]}
        assert result["derivation"] == {"chain": "bitcoin", "account": account, "address_type": kind}
        _, wire = runtime.calls[-1]
        assert wire["derivation"] == result["derivation"] and wire["scheme"] == "ecdsa-secp256k1-der"
        assert wire["digest"] == DIGEST and "message_hex" not in wire
        with api._vault_engine().connect() as conn:
            binding = vault_service.hd_signing_binding(
                vault_service.require_secret_access(conn, NAME),
                {"chain": "bitcoin", "account": account, "address_type": kind},
            )
        audit_delivery = json.loads(rows(vault_audit)[-1]["delivery"])
        assert audit_delivery["signing_key"] == binding

    assert sum(row["event"] == "signed" for row in rows(vault_audit)) == 40


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
@pytest.mark.parametrize("substitution", ["other-purpose", "other-account", "sighash-byte"])
def test_wrong_key_or_transaction_hash_byte_never_delivered_or_audited(runtime, kind, substitution):
    create_extended(runtime)
    runtime.reply = sign_reply("p2wpkh" if kind == "p2pkh" else "p2pkh") if substitution == "other-purpose" else sign_reply(kind, 1)
    if substitution == "sighash-byte":
        runtime.reply = sign_reply(kind)
        runtime.reply["signature"] += "01"
    with pytest.raises(api.VaultApiError):
        api.vault_sign(sign_input(kind))
    assert meta()["use_count"] == 0
    assert not any(row["event"] == "signed" for row in rows(vault_audit))


@pytest.mark.parametrize("change", [
    {"address_type": None}, {"address_type": False}, {"address_type": "p2sh"}, {"chain": "evm"},
    {"chain": "solana"}, {"account": True}, {"scheme": "schnorr-secp256k1-bip341-keypath"},
])
def test_invalid_selection_refuses_before_custody_request_or_use(runtime, change):
    create_extended(runtime)
    runtime.calls.clear()
    with pytest.raises(api.VaultApiError):
        api.vault_sign({**sign_input("p2wpkh"), **change})
    assert runtime.calls == [] and rows(vault_requests) == [] and meta()["use_count"] == 0


@pytest.mark.parametrize("kind", TYPES)
def test_explicit_signing_withholds_signature_when_record_changes_after_custody(runtime, kind):
    create_extended(runtime)
    runtime.reply = sign_reply(kind)

    def replace_record():
        with api._vault_engine().begin() as conn:
            conn.execute(vault_secrets.update().where(vault_secrets.c.name == NAME).values(id="replacement-record"))

    runtime.hook = replace_record
    with pytest.raises(api.VaultApiError, match="state changed"):
        api.vault_sign(sign_input(kind))
    assert meta()["use_count"] == 0
    assert not any(row["event"] == "signed" for row in rows(vault_audit))


@pytest.mark.parametrize("kind", TYPES)
def test_approval_binds_script_type_card_and_same_scheme_completion(runtime, kind, capfd):
    create_extended(runtime)
    api.update_vault_hd_account(NAME, {})
    api.update_vault_hd_account(NAME, {"always_ask": True}, account=0)
    runtime.calls.clear()
    pending = api.vault_sign(sign_input(kind))["request"]
    assert runtime.calls == []
    delivery = pending["delivery"]
    assert delivery["derivation"] == {"chain": "bitcoin", "account": 0, "address_type": kind}
    card = delivery["card"]["account"]
    assert card["bitcoin_address_type"] == kind
    assert card["bitcoin_address"] == ENTRY["accounts"][0]["btc_taproot" if kind == "p2tr" else "btc_" + kind]
    assert card["bitcoin_path"] == ENTRY["accounts"][0]["paths"]["bitcoin" if kind == "p2tr" else "bitcoin_" + kind]
    for changed in ({"address_type": "p2wpkh" if kind == "p2pkh" else "p2pkh"}, {"account": 1}):
        with pytest.raises(api.VaultApiError, match="does not match"):
            api.vault_sign({**sign_input(kind), **changed, "request_id": pending["id"]})
    no_type = {**sign_input(kind), "request_id": pending["id"]}
    no_type.pop("address_type")
    with pytest.raises(api.VaultApiError):
        api.vault_sign(no_type)
    assert runtime.calls == []
    runtime.reply = sign_reply(kind)
    approved = api.vault_sign(sign_input(kind, request_id=pending["id"]))
    assert approved["request"]["status"] == "approved" and approved["signature"] == runtime.reply
    frozen_key = {"curve": "secp256k1", "public_key": table()[0][vault_hd.BITCOIN_KEYS[kind]]}
    assert approved["signing_public_key"] == frozen_key
    with pytest.raises(api.VaultApiError):
        api.vault_sign(sign_input(kind, request_id=pending["id"]))
    assert len(runtime.calls) == 1 and meta()["use_count"] == 1
    # Historical signature results use their verified request snapshot, never a
    # second lookup of a same-name/current account whose point later changed.
    with api._vault_engine().begin() as conn:
        row = vault_service.require_secret_access(conn, NAME)
        public = json.loads(row["public_meta"])
        public["hd_accounts"][0][vault_hd.BITCOIN_KEYS[kind]] = public["hd_accounts"][0]["evm"]
        conn.execute(vault_secrets.update().where(vault_secrets.c.id == row["id"]).values(public_meta=json.dumps(public)))
    result = api.get_vault_request(pending["id"])["result"]
    assert result["signing_public_key"] == frozen_key
    assert result["derivation"] == delivery["derivation"]
    assert cli.cmd_vault_await(cli.build_parser().parse_args(["vault", "await", pending["id"]])) == 0
    assert json.loads(capfd.readouterr().out)["result"] == result


def test_cli_type_selection_list_projection_and_ordinary_refusal(runtime, capfd):
    create_extended(runtime)
    parser = cli.build_parser()
    runtime.reply = sign_reply("p2wpkh")
    args = parser.parse_args(["vault", "sign", NAME, "--chain", "bitcoin", "--address-type", "p2wpkh", "--digest", DIGEST])
    assert cli.cmd_vault_sign(args) == 0
    output = json.loads(capfd.readouterr().out)
    assert output["derivation"] == {"chain": "bitcoin", "account": 0, "address_type": "p2wpkh"}
    assert cli.cmd_vault_list(parser.parse_args(["vault", "list"])) == 0
    listed = json.loads(capfd.readouterr().out)["secrets"][0]
    assert listed["bitcoin_profile"] == PROFILE and listed["accounts"][0]["bitcoin_address_types"] == TYPES
    assert listed["accounts"][0]["btc_segwit"] == expected()["btc_segwit"]
    from storage.vault_crypto import Sealed
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(conn, name="ORDINARY", kind="keypair", sealed=Sealed("opaque", "opaque", "opaque"),
                                    public_meta={"signing_public_key": {"curve": "secp256k1", "public_key": table()[0]["bitcoin"]}})
    runtime.calls.clear()
    args.name = "ORDINARY"
    assert cli.cmd_vault_sign(args) == 1
    assert "invalid_request" in capfd.readouterr().err and runtime.calls == []


def test_runtime_downgrade_hides_new_capabilities_without_changing_addresses_or_old_default(runtime):
    original = create_extended(runtime)
    runtime.version = "0.1.11"
    current = api.get_vault_secrets()["secrets"][0]
    assert current["accounts"][0]["bitcoin_address_types"] == ["p2tr"]
    assert current["accounts"][0]["btc_segwit"] == original["accounts"][0]["btc_segwit"]
    runtime.calls.clear()
    with pytest.raises(api.VaultApiError) as error:
        api.vault_sign(sign_input("p2wpkh"))
    assert error.value.code == "avault_upgrade_required" and runtime.calls == []
    runtime.reply = next(item["signature"] for item in FIXTURE["signatures"] if item["chain"] == "bitcoin" and item["account"] == 0)
    result = api.vault_sign({"name": NAME, "chain": "bitcoin", "digest": DIGEST})
    assert result["derivation"] == {"chain": "bitcoin", "account": 0}
    assert "signing_public_key" not in result
    assert runtime.calls[-1][1]["derivation"] == result["derivation"]


def test_capability_probe_is_cached_by_binary_identity_outside_serialization(tmp_path, monkeypatch):
    path = tmp_path / "avault"
    path.write_bytes(b"public fake executable identity")
    probe = Mock(return_value="0.1.12")
    monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: str(path))
    monkeypatch.setattr(api, "_probe_avault_version", probe)
    api._cached_hd_bitcoin_capability.cache_clear()
    for _ in range(20):
        assert api._hd_bitcoin_types_available()
    probe.assert_called_once_with(str(path))
    path.write_bytes(b"different old executable")
    probe.return_value = "0.1.11"
    assert not api._hd_bitcoin_types_available()
    assert probe.call_count == 2


def test_protected_ordinary_capabilities_do_not_offer_ecdsa_bitcoin_or_probe_avault(runtime, monkeypatch):
    from storage.vault_crypto import Sealed
    probe = Mock(side_effect=AssertionError("protected projection must not probe custody"))
    monkeypatch.setattr(api, "_hd_bitcoin_types_available", probe)
    with api._vault_engine().begin() as conn:
        secret = vault_service.create_secret(conn, name="PROTECTED_BTC", kind="keypair", protection="protected",
            sealed=Sealed("opaque", "opaque", "opaque"), public_meta={
                "signing_public_key": {"curve": "secp256k1", "public_key": table()[0]["bitcoin"]}})
    assert api.vault_public_capabilities(secret)["bitcoin_address_types"] == ["p2tr"]
    assert cli._vault_secret_row(secret)["bitcoin_address_types"] == ["p2tr"]
    probe.assert_not_called()


def test_headless_transaction_assembly_uses_the_supported_cli_sign_result_key(runtime, capfd):
    create_extended(runtime)
    for _ in range(19):
        api.update_vault_hd_account(NAME, {})
    for vector in TRANSACTIONS["synthetic_public_corpus_vectors"]:
        runtime.reply = {"signature": vector["der"], "recovery_id": None}
        digest = hashlib.sha256(hashlib.sha256(bytes.fromhex(vector["preimage"])).digest()).digest()
        assert digest.hex() == vector["digest"]
        args = cli.build_parser().parse_args(["vault", "sign", NAME, "--chain", "bitcoin",
            "--account", str(vector["account"]), "--address-type", vector["address_type"], "--digest", digest.hex()])
        assert cli.cmd_vault_sign(args) == 0
        result = json.loads(capfd.readouterr().out)
        # Only supported CLI response data supplies the assembly key; the golden
        # fixture's public_key is not an input to scriptSig/witness construction.
        public_key = bytes.fromhex(result["signing_public_key"]["public_key"])
        der = bytes.fromhex(result["signature"]["signature"])
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), public_key).verify(
            der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        key_hash = hashlib.new("ripemd160", hashlib.sha256(public_key).digest()).hexdigest()
        assert vector["script_code"] == "76a914" + key_hash + "88ac"
        signature = der + b"\x01"
        if vector["address_type"] == "p2pkh":
            script_sig = bytes([len(signature)]) + signature + bytes([len(public_key)]) + public_key
            assert script_sig.hex() == vector["script_sig"]
        else:
            assert [signature.hex(), public_key.hex()] == vector["witness"]
            assert vector["prevout_script_pubkey"] == "0014" + key_hash


def test_prepared_valid_point_substitution_is_reauthenticated_and_refused(runtime):
    create_extended(runtime)
    with api._vault_engine().begin() as conn:
        row = vault_service.require_secret_access(conn, NAME)
        public = json.loads(row["public_meta"])
        # Valid SEC1 and unique in the purpose84 column: only seed affinity is wrong.
        public["hd_accounts"][0]["bitcoin_p2wpkh"] = public["hd_accounts"][0]["evm"]
        conn.execute(vault_secrets.update().where(vault_secrets.c.id == row["id"]).values(public_meta=json.dumps(public)))
    before, audit_before = rows(vault_secrets), rows(vault_audit)
    runtime.reply = public_response()
    runtime.calls.clear()
    with pytest.raises(api.VaultApiError) as error:
        api.prepare_vault_hd_bitcoin_types(NAME, {})
    assert error.value.code == "hd_bitcoin_types_integrity_failed"
    assert len(runtime.calls) == 1 and runtime.calls[0][0] == ["keypair", "derive-public"]
    assert rows(vault_secrets) == before and rows(vault_audit) == audit_before


def test_storage_preparation_savepoint_survives_caught_audit_error_and_outer_rollback(runtime, monkeypatch):
    create(runtime)
    original = rows(vault_secrets)
    with api._vault_engine().connect() as conn:
        snapshot = vault_service.capture_hd_bitcoin_types(conn, NAME)
    with monkeypatch.context() as patch:
        patch.setattr(vault_service, "audit", Mock(side_effect=RuntimeError("audit failure")))
        with api._vault_engine().begin() as conn:
            with pytest.raises(RuntimeError, match="audit failure"):
                vault_service.commit_hd_bitcoin_types(conn, snapshot, table())
    assert rows(vault_secrets) == original
    with pytest.raises(RuntimeError, match="outer rollback"):
        with api._vault_engine().begin() as conn:
            vault_service.commit_hd_bitcoin_types(conn, snapshot, table())
            raise RuntimeError("outer rollback")
    assert rows(vault_secrets) == original and len(rows(vault_audit)) == 1


def test_competing_different_extra_key_candidates_cannot_overwrite_first_preparation(runtime):
    create(runtime)
    with api._vault_engine().connect() as conn:
        snapshot = vault_service.capture_hd_bitcoin_types(conn, NAME)
    with api._vault_engine().begin() as conn:
        vault_service.commit_hd_bitcoin_types(conn, snapshot, table())
    different = table()
    different[0]["bitcoin_p2pkh"] = different[0]["evm"]
    before, audits = rows(vault_secrets), rows(vault_audit)
    with api._vault_engine().begin() as conn:
        with pytest.raises(vault_service.HdBitcoinTypesIntegrityError):
            vault_service.commit_hd_bitcoin_types(conn, snapshot, different)
    assert rows(vault_secrets) == before and rows(vault_audit) == audits


@pytest.mark.parametrize("kind", TYPES)
def test_new_explicit_selector_and_extended_create_refuse_avault_011_without_fallback(runtime, kind):
    create_extended(runtime)
    runtime.calls.clear()
    runtime.version = "0.1.11"
    with pytest.raises(api.VaultApiError) as error:
        api.vault_sign(sign_input(kind))
    assert error.value.code == "avault_upgrade_required" and error.value.minimum_version == "0.1.12"
    assert runtime.calls == [] and meta()["use_count"] == 0
    with pytest.raises(api.VaultApiError) as error:
        api.create_vault_secret({**copy.deepcopy(CREATE), "name": "NEW_WALLET",
            "keypair": {**CREATE["keypair"], "profile": PROFILE}, "expected_account0": expected()})
    assert error.value.code == "avault_upgrade_required"
    assert runtime.calls == [] and len(rows(vault_secrets)) == 1


def test_profile_and_public_identity_survive_database_reopen_and_backup_restore(runtime, tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    create_extended(runtime)
    for _ in range(19):
        api.update_vault_hd_account(NAME, {})
    runtime.reply = sign_reply("p2wpkh", 19)
    api.vault_sign(sign_input("p2wpkh", 19))
    before = meta()
    engine = api._vault_engine()
    original = sqlite3.connect(engine.url.database)
    backup = tmp_path / "restored.sqlite"
    restored = sqlite3.connect(backup)
    original.backup(restored)
    restored.close()
    original.close()
    fresh = create_engine("sqlite:///" + str(backup))
    try:
        with fresh.connect() as conn:
            current = vault_service.get_secret_meta(conn, NAME)
            assert current == before
            key = vault_service.get_signing_public_key(conn, NAME, derivation=vault_hd.derivation("bitcoin", 19, "p2wpkh"))
            assert key["public_key"] == table()[19]["bitcoin_p2wpkh"]
    finally:
        fresh.dispose()


def test_official_bip84_and_slip132_published_addresses_remain_exact():
    from storage.vault_addresses import derive_addresses
    anchors = json.loads((ROOT / "official-anchors.json").read_text())
    for vector in anchors["bip84"]:
        assert derive_addresses(vector["public_key"])["btc_segwit"] == vector["mainnet_address"]
    legacy = anchors["slip132_bip44"]
    assert derive_addresses(legacy["public_key"])["btc_legacy"] == legacy["mainnet_address"]


def test_published_bip143_der_is_accepted_only_for_its_exact_digest_and_key():
    vector = TRANSACTIONS["official_bip143"]
    signature = {"signature": vector["published_der_with_sighash_byte"][:-2], "recovery_id": None}
    kwargs = {"signing_public_key": {"curve": "secp256k1", "public_key": vector["public_key"]},
              "digest": vector["published_digest"], "scheme": "ecdsa-secp256k1-der", "signature": signature}
    assert api._verify_standard_signature(**kwargs) is None
    for changed in ({"digest": DIGEST}, {"signing_public_key": {"curve": "secp256k1", "public_key": table()[0]["bitcoin_p2wpkh"]}},
                    {"signature": {"signature": vector["published_der_with_sighash_byte"], "recovery_id": None}}):
        with pytest.raises(api.VaultApiError):
            api._verify_standard_signature(**{**kwargs, **changed})

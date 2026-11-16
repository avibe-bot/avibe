"""Protected-HD ECDSA upgrade/downgrade and adversarial settlement boundaries.

All keys, roots and envelopes are synthetic PUBLIC NEVER-FUND fixtures, not
browser/seed/UV/custody evidence. No real keychain, clipboard, service or state.
"""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from cryptography.hazmat.primitives.asymmetric import utils

from storage import vault_service
from storage.models import vault_audit, vault_requests, vault_secrets
from tests.test_vault_bitcoin_ecdsa_context import ORDER, _case, _signature
from tests.test_vault_protected_bitcoin import legacy_cohort  # noqa: F401 -- complete historical pin
from tests.test_vault_protected_hd import complete, no_custody, rows, stored  # noqa: F401 -- hermetic fixture
from tests.test_vault_protected_hd_bitcoin_integration import TYPES, request, wallet
from vibe import api, vault_sandbox_release


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_historical_complete_pins_keep_receiving_and_signing_closed(legacy_cohort, kind):
    signing, _ = wallet(kind)
    secret, = api.get_vault_secrets()["secrets"]
    account = secret["accounts"][1]
    assert account["bitcoin_address_types"] == ["p2tr"]
    assert "btc_legacy" not in account and "btc_segwit" not in account
    before = len(rows(vault_audit))
    with pytest.raises(api.VaultApiError) as error:
        request(signing)
    assert error.value.code == "protected_bitcoin_unsupported"
    assert not rows(vault_requests) and len(rows(vault_audit)) == before


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_unknown_pin_never_enables_existing_public_precommitments(kind, monkeypatch):
    signing, _ = wallet(kind)
    monkeypatch.setattr(vault_sandbox_release, "CAPABILITIES_BY_PIN_ID", {})
    assert api.get_vault_secrets()["secrets"][0]["accounts"][1]["bitcoin_address_types"] == ["p2tr"]
    with pytest.raises(api.VaultApiError) as error:
        request(signing)
    assert error.value.code == "protected_bitcoin_unsupported" and not rows(vault_requests)


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_existing_saved_wallet_gains_choices_without_recreate_import_or_reseal(kind, monkeypatch):
    pins = json.loads((Path(__file__).parent / "fixtures/vault-sandbox-legacy-pins.json").read_text())
    old_pin = pins["9f1264cb022625f1d3880c16019cc4ddcf515fce252193ad482714fe30ad046a"]
    with monkeypatch.context() as old:
        old.setattr(vault_sandbox_release, "load_packaged_pin", lambda: copy.deepcopy(old_pin))
        signing, signature = wallet(kind)
        before = stored()
        assert api.get_vault_secrets()["secrets"][0]["accounts"][1]["bitcoin_address_types"] == ["p2tr"]
    assert stored() == before  # changing the source pin does not write saved state
    assert api.get_vault_secrets()["secrets"][0]["accounts"][1]["bitcoin_address_types"] == TYPES
    pending = request(signing)
    assert complete(signing, pending, signature)["ok"]
    after = stored()
    assert all(after[key] == before[key] for key in ("id", "ciphertext", "nonce", "wrap_meta"))
    assert len(rows(vault_secrets)) == 1


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_context_absence_resolves_exact_saved_request_but_null_never_does(kind):
    signing, signature = wallet(kind)
    pending = request(signing)
    completion = {key: value for key, value in signing.items() if key != "signing_context"}
    completion.update(request_id=pending["id"], signature=signature)
    with pytest.raises(api.VaultApiError):
        api.vault_sign({**completion, "signing_context": None})
    assert rows(vault_requests)[0]["status"] == "pending"
    assert api.vault_sign(completion)["ok"]


@pytest.mark.parametrize("mutation", ["hash-byte", "high-s", "wrong-digest", "uppercase", "recovery", "extra-field", "too-long"])
def test_der_rejection_never_consumes_cas_usage_or_audit(mutation):
    signing, signature = wallet()
    pending = request(signing)
    if mutation == "hash-byte":
        signature["signature"] += "01"
    elif mutation == "high-s":
        r, s = utils.decode_dss_signature(bytes.fromhex(signature["signature"]))
        signature["signature"] = utils.encode_dss_signature(r, ORDER - s).hex()
    elif mutation == "wrong-digest":
        signature["signature"] = _signature(_case("p2pkh"))
    elif mutation == "uppercase":
        signature["signature"] = signature["signature"].upper()
    elif mutation == "recovery":
        signature["recovery_id"] = 0
    elif mutation == "extra-field":
        signature["untrusted"] = True
    else:
        signature["signature"] = "00" * 72
    with pytest.raises(api.VaultApiError):
        complete(signing, pending, signature)
    assert rows(vault_requests)[0]["status"] == "pending" and stored()["use_count"] == 0
    assert not [row for row in rows(vault_audit) if row["event"] == "signed"]


def test_two_der_completions_deliver_exactly_one_original_requester_use():
    signing, signature = wallet()
    pending = request(signing)
    barrier = Barrier(2)
    def settle(_):
        barrier.wait(timeout=10)
        try:
            with api._vault_engine().begin() as conn:
                vault_service.complete_sign_request(conn, pending["id"], name=signing["name"], scheme=signing["scheme"],
                    digest=signing["digest"], signing_context=signing["signing_context"], signature=signature,
                    derivation=pending["delivery"]["derivation"], requester={"session_id": "not-the-requester"})
            return True
        except vault_service.InvalidRequestError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(settle, range(2)))
    assert sorted(results) == [False, True]
    assert stored()["use_count"] == 1
    assert json.loads(stored()["public_meta"])["hd_accounts"][1]["last_signer"]["session_id"] == "original-hd-btc-requester"
    assert len([row for row in rows(vault_audit) if row["event"] == "signed"]) == 1


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_proof_free_collection_requires_the_exact_saved_id_full_read(kind):
    signing, _ = wallet(kind)
    pending = request(signing)
    summary, = api.get_vault_requests()["requests"]
    assert summary["id"] == pending["id"]
    for field in ("secret_name", "requester", "delivery", "message_id", "card", "session"):
        assert summary[field] is None
    assert len(json.dumps(summary, ensure_ascii=False).encode()) <= 1024
    full = api.get_vault_request(summary["id"])["request"]
    assert full["delivery"]["signing_key"] == pending["delivery"]["signing_key"]
    assert full["delivery"]["operation_context"] == pending["delivery"]["operation_context"]
    assert full["card"]["account"]["bitcoin_address_type"] == kind


@pytest.mark.parametrize("field", ["account", "address_type", "publicKey", "sandboxCohort", "signingKey"])
def test_persisted_release_mutation_cannot_rebind_the_initial_hd_capture(field):
    signing, signature = wallet()
    pending = request(signing)
    with api._vault_engine().begin() as conn:
        delivery = copy.deepcopy(pending["delivery"])
        release = delivery["operation_context"]["release"]
        if field in {"account", "address_type"}:
            release["derivation"][field] = 0 if field == "account" else "p2pkh"
        elif field == "publicKey":
            release[field] = "02" + "00" * 32
        elif field == "sandboxCohort":
            release[field]["buildHash"] = "00" * 32
        else:
            release[field]["secret_id"] = "untrusted-other-saved-wallet"
        conn.execute(vault_requests.update().where(vault_requests.c.id == pending["id"]).values(delivery=json.dumps(delivery)))
    with pytest.raises(api.VaultApiError):
        complete(signing, pending, signature)
    assert rows(vault_requests)[0]["status"] == "pending" and stored()["use_count"] == 0
    assert not [row for row in rows(vault_audit) if row["event"] == "signed"]


@pytest.mark.parametrize("pin,types", [("current", TYPES), ("historical", ["p2tr"]), ("unknown", ["p2tr"])])
def test_cli_listing_warning_names_only_the_pin_listed_hd_forms(pin, types, monkeypatch, capfd):
    from vibe import cli

    if pin == "historical":
        pins = json.loads((Path(__file__).parent / "fixtures/vault-sandbox-legacy-pins.json").read_text())
        monkeypatch.setattr(vault_sandbox_release, "load_packaged_pin",
                            lambda: pins["9f1264cb022625f1d3880c16019cc4ddcf515fce252193ad482714fe30ad046a"])
    elif pin == "unknown":
        monkeypatch.setattr(vault_sandbox_release, "CAPABILITIES_BY_PIN_ID", {})
    wallet("p2wpkh")
    parser = cli.build_parser()
    for argv in (["vault", "list"], ["vault", "find"]):
        assert getattr(cli, f"cmd_vault_{argv[1]}")(parser.parse_args(argv)) == 0
        secret, = json.loads(capfd.readouterr().out)["secrets"]
        assert all(account["bitcoin_address_types"] == types for account in secret["accounts"])
        warning = secret["bitcoin_signing_warning"]
        assert "Taproot key-path" in warning and "fresh browser approval" in warning and "unsupported" not in warning
        assert ("Legacy (with the parent transaction)" in warning) == ("p2pkh" in types)
        assert ("Native SegWit (with the BIP143 amount)" in warning) == ("p2wpkh" in types)

"""Protected-HD ECDSA owning API/storage/CLI integration, PUBLIC NEVER FUND.

Opaque envelopes/root metadata are synthetic: these tests establish public-table
consistency, immutable requests and transaction settlement, NOT seed/AEAD/PRF/UV
or custody proof. The pinned sandbox separately proves all three seed-derived
Bitcoin siblings. Scalar7 here is public test data, unrelated to any user key.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from storage import vault_hd, vault_service
from storage.models import vault_audit, vault_requests, vault_secrets
from tests.test_vault_bitcoin_ecdsa_context import DESTINATION, PUBLIC, _case, _signature
from tests.test_vault_protected_hd import (  # noqa: F401 -- imported hermetic root/custody fixture
    complete, no_custody, payload, rows, stored,
)
from vibe import api, cli, vault_sandbox_release

PUBLIC_ADDRESSES = {"p2pkh": "19ZewH8Kk1PDbSNdJ97FP4EiCjTRaZMZQA", "p2wpkh": "bc1qthklh702txwafc72d2qtxv7ywt7sk0mfy3mw6y"}
TYPES = ["p2pkh", "p2wpkh", "p2tr"]


def wallet(kind="p2wpkh", index=1, *, scripts=(DESTINATION,)):
    value = payload()
    # Replace just this selected test point in both immutable public claims; no
    # claim is made that the opaque ciphertext derives this synthetic table.
    value["accounts"][index][vault_hd.BITCOIN_KEYS[kind]] = PUBLIC.hex()
    if index == 0:
        value["expected_account0"][{"p2pkh": "btc_legacy", "p2wpkh": "btc_segwit"}[kind]] = PUBLIC_ADDRESSES[kind]
    wrap = json.loads(value["sealed"]["wrap_meta"])
    wrap["record_meta"]["accounts"] = value["accounts"]
    value["sealed"]["wrap_meta"] = json.dumps(wrap, ensure_ascii=False)
    api.create_vault_secret(value)
    for _ in range(index):
        api.update_vault_hd_account(value["name"], {})
    context = _case(kind, scripts=scripts)
    signing = {"name": value["name"], "scheme": "ecdsa-secp256k1-der", "chain": "bitcoin", "account": index,
               "address_type": kind, "digest": context["digest"], "signing_context": context,
               "requester": {"source": "cli", "session_id": "original-hd-btc-requester"}}
    return signing, {"signature": _signature(context), "recovery_id": None}


def request(signing, entrypoint="sign", capfd=None):
    if entrypoint == "storage":
        with api._vault_engine().begin() as conn:
            result = vault_service.create_sign_request(conn, signing["name"], scheme=signing["scheme"],
                digest=signing["digest"], signing_context=signing["signing_context"], requester=signing["requester"],
                derivation={key: signing[key] for key in ("chain", "account", "address_type")})
        return api._attach_signed_sign_operation_context(result["id"])
    if entrypoint == "cli":
        args = ["vault", "sign", signing["name"], "--chain", "bitcoin", "--account", str(signing["account"]),
                "--address-type", signing["address_type"], "--digest", signing["digest"],
                "--signing-context-json", json.dumps(signing["signing_context"])]
        assert cli.cmd_vault_sign(cli.build_parser().parse_args(args)) == 0
        return json.loads(capfd.readouterr().out)["request"]
    return (api.vault_sign(signing) if entrypoint == "sign" else api.request_vault_sign(signing))["request"]


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
@pytest.mark.parametrize("index", [0, 1, 19])
@pytest.mark.parametrize("entrypoint", ["sign", "request", "storage", "cli"])
def test_purpose_selected_request_release_der_and_frozen_result(kind, index, entrypoint, capfd):
    signing, signature = wallet(kind, index)
    before = stored()
    pending = request(signing, entrypoint, capfd)
    delivery = pending["delivery"]
    derivation = {key: signing[key] for key in ("chain", "account", "address_type")}
    fingerprint = hashlib.sha256(json.dumps([before[field] for field in ("ciphertext", "nonce", "wrap_meta")],
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    binding = {"secret_id": before["id"], "public_key": PUBLIC.hex(), "envelope_sha256": fingerprint}
    assert delivery["signing_key"] == binding
    assert delivery["bitcoin_identity"] == {"kind": "keypair", "protection": "protected", "signer_kind": "hd-seed"}
    support = vault_sandbox_release.current_support()
    assert support is not None and list(support.hd_input_types) == TYPES
    assert delivery["bitcoin_cohort"] == support.cohort
    assert delivery["operation_context"]["release"] == {
        "name": signing["name"], "scheme": signing["scheme"], "digest": signing["digest"],
        "publicKey": PUBLIC.hex(), "signingKey": binding, "derivation": derivation,
        "signingContext": signing["signing_context"],
        "sandboxCohort": {"buildHash": support.build_hash, "manifestHash": support.manifest_hash},
    }
    assert pending["card"]["account"]["bitcoin_address_type"] == kind
    assert pending["card"]["account"]["bitcoin_path"] == f"m/{44 if kind == 'p2pkh' else 84}'/0'/0'/0/{index}"
    assert pending["card"]["account"]["bitcoin_address"] == PUBLIC_ADDRESSES[kind]
    result = complete(signing, pending, signature)
    assert result["ok"] and result["signature"] == {"signature": signature["signature"]}
    assert result["signing_public_key"] == {"curve": "secp256k1", "public_key": PUBLIC.hex()}
    assert result["derivation"] == derivation
    after = stored()
    assert all(after[key] == before[key] for key in ("id", "ciphertext", "nonce", "wrap_meta"))
    assert after["use_count"] == before["use_count"] + 1
    accounts = json.loads(after["public_meta"])["hd_accounts"]
    if entrypoint != "cli":
        assert accounts[index]["last_signer"]["session_id"] == "original-hd-btc-requester"
    signed, = [row for row in rows(vault_audit) if row["event"] == "signed"]
    assert json.loads(signed["requester"]) == pending["requester"]
    assert json.loads(signed["delivery"])["derivation"] == derivation
    # A lost response uses frozen saved results, not the current row's key/envelope.
    with api._vault_engine().begin() as conn:
        conn.execute(vault_secrets.update().where(vault_secrets.c.id == before["id"]).values(nonce="changed-after-success"))
    saved = api.get_vault_request(pending["id"])["result"]
    assert saved == {"type": "signature", "signature": result["signature"], "derivation": derivation,
                     "signing_public_key": result["signing_public_key"]}


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_two_sibling_returns_refuse_through_real_intake(kind):
    signing, _ = wallet(kind)
    with api._vault_engine().connect() as conn:
        row = vault_service.require_secret_access(conn, signing["name"])
        returns = sorted(vault_service.protected_hd_bitcoin_return_scripts(row, {key: signing[key] for key in ("chain", "account", "address_type")}))
    signing["signing_context"] = _case(kind, scripts=tuple(returns[:2]))
    signing["digest"] = signing["signing_context"]["digest"]
    before = len(rows(vault_requests)), len(rows(vault_audit))
    with pytest.raises(api.VaultApiError, match="at most one same-account return"):
        api.vault_sign(signing)
    assert (len(rows(vault_requests)), len(rows(vault_audit))) == before
    assert stored()["use_count"] == 0


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
def test_each_authenticated_sibling_is_a_return_not_an_ordinary_scalar_reencoding(kind):
    signing, _ = wallet(kind)
    with api._vault_engine().connect() as conn:
        row = vault_service.require_secret_access(conn, signing["name"])
        scripts = vault_service.protected_hd_bitcoin_return_scripts(row, {key: signing[key] for key in ("chain", "account", "address_type")})
    # Real intake must admit a sibling return and a recipient, and reject TWO
    # distinct sibling returns. Ordinary single-scalar semantics miss this case.
    for script in scripts:
        signing["signing_context"] = _case(kind, scripts=(script, DESTINATION))
        signing["digest"] = signing["signing_context"]["digest"]
        assert request(signing)["status"] == "pending"


@pytest.mark.parametrize("mutation", ["id", "raw-wrap", "cohort", "selected-point", "sibling-point", "disabled"])
def test_saved_identity_changes_withhold_completion_and_usage(mutation, monkeypatch):
    signing, signature = wallet()
    pending = request(signing)
    with api._vault_engine().begin() as conn:
        row = stored()
        values = {}
        if mutation == "id":
            values["id"] = "vlt-replacement-same-public-points"
        elif mutation == "raw-wrap":
            values["wrap_meta"] = row["wrap_meta"] + " "
        elif mutation in {"selected-point", "sibling-point", "disabled"}:
            meta = json.loads(row["public_meta"])
            field = "bitcoin_p2wpkh" if mutation == "selected-point" else "bitcoin_p2pkh"
            if mutation == "disabled":
                meta["hd_accounts"][1]["enabled"] = False
            else:
                meta["hd_accounts"][1][field] = meta["hd_accounts"][2][field]
            values["public_meta"] = json.dumps(meta)
        if values:
            conn.execute(vault_secrets.update().where(vault_secrets.c.id == row["id"]).values(**values))
    if mutation == "cohort":
        monkeypatch.setattr(vault_sandbox_release, "CAPABILITIES_BY_PIN_ID", {})
    with pytest.raises(api.VaultApiError):
        complete(signing, pending, signature)
    assert stored()["use_count"] == 0
    assert rows(vault_requests)[0]["status"] == "pending"
    assert not [row for row in rows(vault_audit) if row["event"] == "signed"]


@pytest.mark.parametrize("kind", ["p2pkh", "p2wpkh"])
@pytest.mark.parametrize("failure", ["audit", "outer-rollback"])
def test_der_completion_is_one_atomic_cas_use_last_signer_audit_unit(kind, failure, monkeypatch):
    signing, signature = wallet(kind)
    pending = request(signing)
    def settle(conn):
        return vault_service.complete_sign_request(conn, pending["id"], name=signing["name"], scheme=signing["scheme"],
            digest=signing["digest"], signing_context=signing["signing_context"], signature=signature,
            derivation=pending["delivery"]["derivation"], requester={"session_id": "untrusted-completer"})
    if failure == "audit":
        original = vault_service.audit
        def audit(conn, event, **kwargs):
            original(conn, event, **kwargs)
            if event == "signed":
                raise RuntimeError("synthetic signed-audit failure")
        monkeypatch.setattr(vault_service, "audit", audit)
        with api._vault_engine().begin() as conn:
            with pytest.raises(RuntimeError, match="synthetic signed-audit"):
                settle(conn)
    else:
        with api._vault_engine().connect() as conn:
            transaction = conn.begin()
            assert settle(conn)["status"] == "approved"
            transaction.rollback()
    assert rows(vault_requests)[0]["status"] == "pending"
    assert stored()["use_count"] == 0
    assert json.loads(stored()["public_meta"])["hd_accounts"][1]["last_signer"] is None
    assert not [row for row in rows(vault_audit) if row["event"] == "signed"]

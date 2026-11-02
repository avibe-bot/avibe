"""Bounded protected Bitcoin context and settlement contracts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from storage import vault_bitcoin, vault_service
from storage.models import vault_requests
from storage.vault_crypto import Sealed
from vibe import api

FIXTURES = Path(__file__).parent / "fixtures" / "vault_bitcoin"
CORE = json.loads((FIXTURES / "core-fixtures.json").read_text())
CODEC = json.loads((FIXTURES / "codec-fixtures.json").read_text())
POLICY = json.loads((FIXTURES / "policy-fixtures.json").read_text())
KEYS = {item["id"]: item for item in CORE["keys"]}
SCHEME = vault_bitcoin.BITCOIN_SCHEME


def _context(case: dict, *, network: str = "signet") -> dict:
    return {
        "kind": vault_bitcoin.BITCOIN_CONTEXT_KIND,
        "network": network,
        "unsignedTransaction": case["unsigned_tx_hex"],
        "prevout": {
            "amountSats": case["funding"]["spent_output_value_sats"],
            "scriptPubKey": case["funding"]["spent_output_script_hex"],
        },
        "digestAlgorithm": vault_bitcoin.BITCOIN_DIGEST_ALGORITHM,
        "digest": case["expected"]["taproot_digest_hex"],
    }


def _create_protected_key(public_key: str) -> None:
    with api._vault_engine().begin() as conn:
        vault_service.create_secret(
            conn,
            name="BTC_KEY",
            kind="keypair",
            protection="protected",
            signer_kind="local",
            sealed=Sealed(ciphertext="test-envelope", nonce="test-nonce", wrap_meta="test-meta"),
            public_meta={"signing_public_key": {"curve": "secp256k1", "public_key": public_key}},
        )


@pytest.mark.parametrize("case", CORE["cases"], ids=lambda case: case["id"])
def test_core_vectors_validate_digest_and_key_binding(case: dict) -> None:
    key = KEYS[case["signing_key_id"]]
    validated = vault_bitcoin.validate_context(
        _context(case),
        expected_digest=case["expected"]["taproot_digest_hex"],
        compressed_public_key=key["internal_pubkey_compressed_hex"],
    )
    assert validated.digest == case["expected"]["taproot_digest_hex"]
    assert validated.transaction.unsigned_txid == case["expected"]["txid"]
    assert validated.output_key_xonly.hex() == key["output_xonly_hex"]


@pytest.mark.parametrize("case", POLICY["valid"], ids=lambda case: case["id"])
def test_core_policy_boundaries_and_rbf(case: dict) -> None:
    validated = vault_bitcoin.validate_context(
        case["context"], expected_digest=case["context"]["digest"], compressed_public_key=POLICY["publicKey"]
    )
    assert validated.rbf_signals is case["expected"]["signalsRbf"]
    assert validated.high_fee_warning is case["expected"]["highFee"]
    assert validated.fee_sats == case["expected"]["feeSats"]
    assert validated.signed_vsize == case["expected"]["vsize"]
    assert validated.transaction.unsigned_txid == case["expected"]["txid"]


@pytest.mark.parametrize("case", POLICY["invalid"], ids=lambda case: case["id"])
def test_parser_and_policy_mutations_fail_for_the_intended_reason(case: dict) -> None:
    with pytest.raises(vault_bitcoin.BitcoinContextError, match=case["error"]):
        vault_bitcoin.validate_context(
            case["context"], expected_digest=case["context"].get("digest", "00" * 32),
            compressed_public_key=POLICY["publicKey"],
        )


@pytest.mark.parametrize("network", [[], {}, 1, True])
def test_malformed_network_is_a_typed_context_error(network: object) -> None:
    context = _context(CORE["cases"][0])
    context["network"] = network
    with pytest.raises(vault_bitcoin.BitcoinContextError, match="network"):
        vault_bitcoin.validate_context_shape(context, expected_digest=context["digest"])


@pytest.mark.parametrize("case", CODEC["cases"], ids=lambda case: case["id"])
def test_codec_vectors_keep_fee_size_and_supported_scripts_bounded(case: dict) -> None:
    context = case["context"]
    validated = vault_bitcoin.validate_context(
        context,
        expected_digest=context["digest"],
        compressed_public_key=case["publicKey"],
    )
    expected = case["expected"]
    assert validated.transaction.unsigned_txid == expected["txid"]
    assert validated.fee_sats == expected["feeSats"]
    assert validated.signed_vsize == expected["vsize"]
    assert validated.net_value_leaving_key_sats == expected["netLeavingKeySats"]
    assert validated.rbf_signals is expected["signalsRbf"]


@pytest.mark.parametrize("case", CODEC["sameKeyPolicyRefusals"], ids=lambda case: case["id"])
def test_same_key_legacy_and_segwit_v0_outputs_are_rejected(case: dict) -> None:
    with pytest.raises(vault_bitcoin.BitcoinContextError, match="same-key"):
        vault_bitcoin.validate_context(case["context"], expected_digest=case["context"]["digest"], compressed_public_key=case["publicKey"])


def test_protected_api_persists_and_settles_a_valid_bitcoin_request(monkeypatch: pytest.MonkeyPatch) -> None:
    case = CORE["cases"][0]
    key = KEYS[case["signing_key_id"]]
    _create_protected_key(key["internal_pubkey_compressed_hex"])
    monkeypatch.setattr(api, "_notify_vault_request_created", Mock())
    context = _context(case)
    requested = api.request_vault_sign({
        "name": "BTC_KEY",
        "digest": context["digest"],
        "scheme": SCHEME,
        "signing_context": context,
        "delivery": {"command": "bitcoin test payment", "egress": "signature"},
    })
    request_id = requested["request"]["id"]
    assert requested["request"]["delivery"]["signing_context"] == context
    assert requested["request"]["delivery"]["operation_context"]["release"] == {
        "name": "BTC_KEY", "digest": context["digest"], "scheme": SCHEME,
        "publicKey": key["internal_pubkey_compressed_hex"], "signingContext": context,
    }
    result = api.vault_sign({
        "name": "BTC_KEY",
        "request_id": request_id,
        "digest": context["digest"],
        "scheme": SCHEME,
        "signature": {"signature": case["expected"]["bip340_signature_hex"]},
    })
    assert result["ok"] is True
    assert result["request"]["status"] == "approved"


@pytest.mark.parametrize("mutation", ["amount", "non-bitcoin-kind", "missing", "algorithm"])
def test_persisted_bitcoin_context_is_revalidated_before_settlement(mutation: str) -> None:
    case = CORE["cases"][0]
    key = KEYS[case["signing_key_id"]]
    _create_protected_key(key["internal_pubkey_compressed_hex"])
    context = _context(case)
    with api._vault_engine().begin() as conn:
        request = vault_service.create_sign_request(conn, "BTC_KEY", digest=context["digest"], scheme=SCHEME, signing_context=context)
        row = conn.execute(select(vault_requests).where(vault_requests.c.id == request["id"])).mappings().one()
        delivery = json.loads(row["delivery"])
        if mutation == "amount":
            delivery["signing_context"]["prevout"]["amountSats"] += 1
        elif mutation == "non-bitcoin-kind":
            delivery["signing_context"] = {"kind": "avault-agent-operation", "digest": context["digest"]}
        elif mutation == "missing":
            del delivery["signing_context"]
        else:
            delivery["signing_context"]["digestAlgorithm"] = "sha256"
        conn.execute(vault_requests.update().where(vault_requests.c.id == request["id"]).values(delivery=json.dumps(delivery)))
        with pytest.raises(vault_service.InvalidSigningContextError):
            vault_service.complete_sign_request(
                conn,
                request["id"],
                name="BTC_KEY",
                digest=context["digest"],
                scheme=SCHEME,
                signature={"signature": case["expected"]["bip340_signature_hex"]},
                browser_signed=True,
            )
        current = conn.execute(select(vault_requests.c.status).where(vault_requests.c.id == request["id"])).scalar_one()
    assert current == "pending"


@pytest.mark.parametrize("entrypoint", ["service", "cli", "request", "sign"])
@pytest.mark.parametrize("case", ["wrong-q", "false-amount", "extra-field", "wrong-scheme"])
def test_bitcoin_preflight_rejects_before_request_notification_or_signing(monkeypatch, capfd, entrypoint, case):
    from vibe import cli

    _create_protected_key(POLICY["publicKey"])
    source = next(item for item in POLICY["invalid"] if item["id"] == case) if case != "wrong-scheme" else POLICY["valid"][0]
    context = copy.deepcopy(source["context"])
    scheme = "schnorr-secp256k1-bip340" if case == "wrong-scheme" else SCHEME
    notify, sign = Mock(), Mock()
    monkeypatch.setattr(api, "_notify_vault_request_created", notify)
    monkeypatch.setattr(api, "avault_sign", sign)
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", notify)
    if entrypoint == "service":
        with api._vault_engine().begin() as conn:
            # Catch inside the transaction: rollback cannot hide an inserted request.
            with pytest.raises(vault_service.InvalidSigningContextError):
                vault_service.create_sign_request(conn, "BTC_KEY", digest=context["digest"], scheme=scheme, signing_context=context)
    elif entrypoint == "cli":
        args = cli.build_parser().parse_args([
            "vault", "sign", "BTC_KEY", "--digest", context["digest"], "--scheme", scheme,
            "--signing-context-json", json.dumps(context),
        ])
        assert cli.cmd_vault_sign(args) == 1
        assert json.loads(capfd.readouterr().err)["code"] == "invalid_request"
    else:
        with pytest.raises(api.VaultApiError) as error:
            (api.request_vault_sign if entrypoint == "request" else api.vault_sign)({
                "name": "BTC_KEY", "digest": context["digest"], "scheme": scheme, "signing_context": context,
            })
        assert error.value.code == "invalid_request"
    with api._vault_engine().connect() as conn:
        assert conn.execute(select(vault_requests.c.id)).all() == []
    notify.assert_not_called()
    sign.assert_not_called()


def test_api_shape_accepts_only_exact_bitcoin_fields() -> None:
    case = CORE["cases"][0]
    context = _context(case)
    assert api._verifiable_signing_context_from_payload({"signing_context": context}, digest=context["digest"]) == context
    with pytest.raises(api.VaultApiError, match="unsupported fields"):
        api._verifiable_signing_context_from_payload(
            {"signing_context": {**copy.deepcopy(context), "sighash": "SIGHASH_DEFAULT"}},
            digest=context["digest"],
        )

"""Typed ECDSA/closure and bounded-tree tests; synthetic public keys NEVER FUND.

These are daemon-owner tests, not browser custody, UV, chain or broadcast proof.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from Crypto.Hash import RIPEMD160
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from storage import vault_bitcoin as bitcoin

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
# Public deterministic fixture scalar, unrelated to any saved/user key.
KEY = ec.derive_private_key(7, ec.SECP256K1())
PUBLIC = KEY.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
KEY_HASH = RIPEMD160.new(hashlib.sha256(PUBLIC).digest()).digest()
PKH = b"\x76\xa9\x14" + KEY_HASH + b"\x88\xac"
WPKH = b"\x00\x14" + KEY_HASH
DESTINATION = b"\x76\xa9\x14" + b"\x11" * 20 + b"\x88\xac"
VECTORS = json.loads((
    Path(__file__).parent / "fixtures/vault_signing/hd-bitcoin/transaction-signing-vectors.json"
).read_text())["synthetic_public_corpus_vectors"]


def _hash(value):
    return hashlib.sha256(hashlib.sha256(value).digest()).digest()


def _outputs(outputs):
    return b"".join(amount.to_bytes(8, "little") + bytes([len(script)]) + script for amount, script in outputs)


def _case(address_type="p2pkh", *, fee=1000, scripts=(DESTINATION,), amount=10_000_000):
    parent = (
        b"\x01\0\0\0\x01" + bytes(32) + b"\xff" * 4 + b"\x00" + b"\xff" * 4
        + b"\x01" + _outputs([(amount, PKH)]) + bytes(4)
    )
    outpoint = _hash(parent) + bytes(4)
    values = [1000] * (len(scripts) - 1) + [amount - fee - 1000 * (len(scripts) - 1)]
    outputs = _outputs(list(zip(values, scripts, strict=True)))
    raw = b"\x02\0\0\0\x01" + outpoint + b"\x00\xfd\xff\xff\xff" + bytes([len(scripts)]) + outputs + bytes(4)
    if address_type == "p2pkh":
        preimage = raw[:41] + bytes([len(PKH)]) + PKH + raw[42:] + b"\x01\0\0\0"
        prevout = {
            "parentTransaction": parent.hex(), "txid": _hash(parent)[::-1].hex(), "vout": 0,
            "amountSats": amount, "scriptPubKey": PKH.hex(),
        }
    else:
        preimage = (
            raw[:4] + _hash(outpoint) + _hash(b"\xfd\xff\xff\xff") + outpoint
            + bytes([len(PKH)]) + PKH + amount.to_bytes(8, "little") + b"\xfd\xff\xff\xff"
            + _hash(outputs) + bytes(4) + b"\x01\0\0\0"
        )
        prevout = {"amountSats": amount, "scriptPubKey": WPKH.hex()}
    return {
        "kind": f"bitcoin-{address_type}-v1", "addressType": address_type, "network": "regtest",
        "unsignedTransaction": raw.hex(), "prevout": prevout, "sighashType": 1,
        "digestAlgorithm": f"bitcoin-{'legacy' if address_type == 'p2pkh' else 'bip143'}-sighash-all",
        "digest": _hash(preimage).hex(),
    }


def _maximal_legacy_case():
    """Independent legal1MiB parent plus a fully bound one-input context."""
    from tests.test_vault_bitcoin_ecdsa import _parent

    context = _case()
    outputs = ((context["prevout"]["amountSats"], PKH),)
    scripts = [b""] * 128
    remaining = bitcoin.MAX_FUNDING_BYTES - len(_parent(scripts=tuple(scripts), outputs=outputs))
    for index in range(128):
        if remaining == 0:
            break
        size = min(10_000, remaining - 2) if remaining >= 255 else remaining
        scripts[index] = b"\xab" * size
        remaining -= size + (2 if size >= 253 else 0)
    assert remaining == 0
    parent = _parent(scripts=tuple(scripts), outputs=outputs)
    assert len(parent) == bitcoin.MAX_FUNDING_BYTES
    raw = bytes.fromhex(context["unsignedTransaction"])
    raw = raw[:5] + _hash(parent) + raw[37:]
    context["unsignedTransaction"] = raw.hex()
    context["prevout"].update(parentTransaction=parent.hex(), txid=_hash(parent)[::-1].hex())
    context["digest"] = _hash(raw[:41] + bytes([len(PKH)]) + PKH + raw[42:] + b"\x01\0\0\0").hex()
    return context


def _validate(context, **kwargs):
    return bitcoin.validate_ecdsa_context(context, expected_digest=context["digest"], public_key=PUBLIC.hex(), **kwargs)


def _signature(context):
    r, s = utils.decode_dss_signature(KEY.sign(bytes.fromhex(context["digest"]), ec.ECDSA(utils.Prehashed(hashes.SHA256()))))
    return utils.encode_dss_signature(r, min(s, ORDER - s)).hex()


@pytest.mark.parametrize("case", [v for v in VECTORS if v["address_type"] == "p2wpkh"], ids=lambda v: str(v["account"]))
def test_native_full_validation_and_closure_matches_independent_public_corpus(case):
    context = {
        "kind": "bitcoin-p2wpkh-v1", "addressType": "p2wpkh", "network": "bitcoin",
        "unsignedTransaction": case["unsigned_transaction"],
        "prevout": {"amountSats": case["prevout_amount_satoshis"], "scriptPubKey": case["prevout_script_pubkey"]},
        "sighashType": 1, "digestAlgorithm": "bitcoin-bip143-sighash-all", "digest": case["digest"],
    }
    validated = bitcoin.validate_ecdsa_context(context, expected_digest=case["digest"], public_key=case["public_key"])
    closure = bitcoin.verify_ecdsa_result(validated, {"signature": case["der"], "recovery_id": None})
    assert closure.signed_transaction.hex() == case["signed_transaction"]
    assert (closure.txid, closure.wtxid) == (case["txid"], case["wtxid"])
    assert closure.signature == {"signature": case["der"]}


@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh"])
def test_complete_context_and_exact_supported_assembly(address_type):
    context = _case(address_type)
    validated = _validate(context)
    signature = _signature(context)
    closure = bitcoin.verify_ecdsa_result(validated, {"signature": signature})
    raw, der = bytes.fromhex(context["unsignedTransaction"]), bytes.fromhex(signature)
    if address_type == "p2pkh":
        script_sig = bytes([len(der) + 1]) + der + b"\x01\x21" + PUBLIC
        expected = raw[:41] + bytes([len(script_sig)]) + script_sig + raw[42:]
        stripped = expected
        weight = 4 * len(expected)
    else:
        witness = b"\x02" + bytes([len(der) + 1]) + der + b"\x01\x21" + PUBLIC
        expected = raw[:4] + b"\x00\x01" + raw[4:-4] + witness + raw[-4:]
        stripped = raw
        weight = 4 * len(raw) + len(witness) + 2
    assert closure.signed_transaction == expected
    assert closure.txid == _hash(stripped)[::-1].hex()
    assert closure.wtxid == _hash(expected)[::-1].hex()
    assert closure.weight == weight and closure.vsize == (weight + 3) // 4
    assert validated.fee_sats == 1000 and validated.net_value_leaving_sats == 10_000_000
    assert not validated.high_fee_warning


@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh"])
def test_return_output_identity_and_card_net_includes_fee(address_type):
    context = _case(address_type, scripts=(DESTINATION, WPKH))
    validated = _validate(context)
    assert validated.same_account_output_indexes == (1,)
    assert validated.net_value_leaving_sats == 2000  # external1000 + fee1000
    # HD caller supplies server-derived purpose siblings, never arbitrary caller flags.
    assert _validate(context, return_scripts={DESTINATION}).same_account_output_indexes == (0,)
    with pytest.raises(bitcoin.BitcoinContextError, match="at most one"):
        _validate(_case(address_type, scripts=(PKH, WPKH)))


@pytest.mark.parametrize("address_type", ["p2pkh", "p2wpkh"])
def test_fee_exact_minimum_vsize_boundaries_and_threshold_equality(address_type):
    initial = _validate(_case(address_type))
    minimum, maximum = initial.vsize_min, initial.vsize_max
    n = len(initial.transaction.raw)
    assert (minimum, maximum) == ((n + 44, n + 107) if address_type == "p2pkh" else ((4 * n + 50) // 4, (4 * n + 113) // 4))
    accepted = _validate(_case(address_type, fee=minimum * 10_000))
    assert accepted.high_fee_warning
    with pytest.raises(bitcoin.BitcoinContextError, match="ceiling"):
        _validate(_case(address_type, fee=minimum * 10_000 + 1))
    with pytest.raises(bitcoin.BitcoinContextError, match="positive"):
        _validate(_case(address_type, fee=0))
    assert not _validate(_case(address_type, fee=minimum * 100)).high_fee_warning
    assert _validate(_case(address_type, fee=minimum * 100 + 1)).high_fee_warning
    # The independent2% threshold also uses strict >, not >=.
    assert not _validate(_case(address_type, fee=2000, amount=100_000)).high_fee_warning
    assert _validate(_case(address_type, fee=2001, amount=100_000)).high_fee_warning
    assert accepted.rate_hundredths == (accepted.fee_sats * 100 // maximum, (accepted.fee_sats * 100 + minimum - 1) // minimum)


@pytest.mark.parametrize(("field", "value"), [
    ("sighashType", True), ("sighashType", 0), ("network", "testnet4"),
    ("addressType", "p2tr"), ("digestAlgorithm", "bip341-sighash-default-keypath"),
    ("unsignedTransaction", "AA"), ("digest", "ab" * 32),
])
def test_exact_new_context_pairing_primitives_and_digest(field, value):
    context = _case()
    original = context["digest"]
    context[field] = value
    with pytest.raises(bitcoin.BitcoinContextError):
        bitcoin.validate_ecdsa_context(context, expected_digest=original, public_key=PUBLIC.hex())


def test_native_forbids_legacy_proof_and_amount_lie_changes_digest():
    context = _case("p2wpkh")
    for field, value in [("parentTransaction", "00"), ("txid", "00" * 32), ("vout", 0)]:
        altered = copy.deepcopy(context)
        altered["prevout"][field] = value
        with pytest.raises(bitcoin.BitcoinContextError):
            _validate(altered)
    context["prevout"]["amountSats"] += 1
    with pytest.raises(bitcoin.BitcoinContextError, match="independently computed"):
        _validate(context)


@pytest.mark.parametrize("mutation", ["high-s", "zero-r", "zero-s", "order-r", "trailing", "nonminimal", "uppercase", "recovery", "extra", "wrong-key", "wrong-digest"])
def test_result_refuses_noncanonical_or_wrong_identity(mutation):
    context = _case()
    validated = _validate(context)
    signature = _signature(context)
    result = {"signature": signature}
    r, s = utils.decode_dss_signature(bytes.fromhex(signature))
    if mutation in {"high-s", "zero-r", "zero-s", "order-r"}:
        r, s = {"high-s": (r, ORDER - s), "zero-r": (0, s), "zero-s": (r, 0), "order-r": (ORDER, s)}[mutation]
        result["signature"] = utils.encode_dss_signature(r, s).hex()
    elif mutation == "trailing":
        result["signature"] += "01"
    elif mutation == "nonminimal":
        result["signature"] = "300702020001020101"
    elif mutation == "uppercase":
        result["signature"] = signature.upper()
    elif mutation == "recovery":
        result["recovery_id"] = 0
    elif mutation == "extra":
        result["echo-secret-payload"] = "do not echo"
    elif mutation == "wrong-key":
        other = ec.derive_private_key(8, ec.SECP256K1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        validated = replace(validated, public_key=other.hex())
    else:
        validated = replace(validated, digest="00" * 32)
    with pytest.raises(bitcoin.BitcoinContextError) as error:
        bitcoin.verify_ecdsa_result(validated, result)
    assert "echo-secret-payload" not in str(error.value)


def _proof_context(proof="00"):
    return {"kind": "bitcoin-p2pkh-v1", "prevout": {"parentTransaction": proof}}


def _pad(value, size):
    value["padding"] = ""
    used = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    value["padding"] = "x" * (size - used)
    return value


def test_context_native_and_aggregate_byte_limits_are_inclusive():
    full_proof = "00" * bitcoin.MAX_FUNDING_BYTES
    context = _pad(_proof_context(full_proof), bitcoin.MAX_CONTEXT_BYTES)
    bitcoin.preflight_ecdsa_context(context)
    context["padding"] += "x"
    with pytest.raises(bitcoin.BitcoinContextError, match="budget"):
        bitcoin.preflight_ecdsa_context(context)
    native = _pad({"kind": "bitcoin-p2wpkh-v1"}, 4096)
    bitcoin.preflight_ecdsa_context(native)
    native["padding"] += "x"
    with pytest.raises(bitcoin.BitcoinContextError, match="budget"):
        bitcoin.preflight_ecdsa_context(native)
    # Same Python string object may occur in exactly the two effective locations.
    operation = {"delivery": {"signing_context": _proof_context(full_proof), "operation_context": {"release": {"signingContext": _proof_context(full_proof)}}}}
    base = len(json.dumps(operation, separators=(",", ":")).encode()) - 2 * len(full_proof)
    _pad(operation, len(json.dumps(operation, separators=(",", ":")).encode()) + bitcoin.MAX_NON_PROOF_BYTES - base)
    assert bitcoin.preflight_ecdsa_operation(operation)
    operation["padding"] += "x"
    with pytest.raises(bitcoin.BitcoinContextError, match="budget"):
        bitcoin.preflight_ecdsa_operation(operation)


@pytest.mark.parametrize("proof", [None, 1, "0", "AA", "0x00", "00 " , "00" * (bitcoin.MAX_FUNDING_BYTES + 1)])
@pytest.mark.parametrize("location", ["direct", "nested"])
def test_all_proofs_are_bounded_before_any_general_walker(monkeypatch, proof, location):
    malformed = {"prevout": {"parentTransaction": proof}}  # deliberately missing kind
    operation = {"signing_context": _proof_context(), "context": {"release": {"signingContext": _proof_context()}}}
    if location == "direct":
        operation["signing_context"] = malformed
    else:
        operation["context"]["release"]["signingContext"] = malformed
    monkeypatch.setattr(bitcoin, "_bounded_json_size", lambda *a, **k: pytest.fail("malformed proof reached walker"))
    with pytest.raises(bitcoin.BitcoinContextError, match="parentTransaction"):
        bitcoin.preflight_ecdsa_operation(operation)


def test_small_transaction_primitives_are_bounded_before_general_walk(monkeypatch):
    monkeypatch.setattr(bitcoin, "_bounded_json_size", lambda *a, **k: pytest.fail("oversized primitive reached walker"))
    for context in [
        {**_proof_context(), "unsignedTransaction": "00" * 138},
        {**_proof_context(), "digest": "00" * 33},
        {"prevout": {"parentTransaction": "00", "txid": "00" * 33}},
        {"prevout": {"parentTransaction": "00", "scriptPubKey": "00" * 26}},
    ]:
        with pytest.raises(bitcoin.BitcoinContextError, match="primitive bound"):
            bitcoin.preflight_ecdsa_context(context)


def test_proof_allowance_uses_exact_paths_not_object_or_string_identity():
    proof = "00" * bitcoin.MAX_FUNDING_BYTES
    context = _proof_context(proof)
    operation = {"signing_context": context, "unexpected_copy": proof}
    with pytest.raises(bitcoin.BitcoinContextError, match="non-proof"):
        bitcoin.preflight_ecdsa_operation(operation)
    operation["unexpected_copy"] = context  # same subtree also is not an effective proof location
    with pytest.raises(bitcoin.BitcoinContextError, match="additional embedded"):
        bitcoin.preflight_ecdsa_operation(operation)
    operation = {"signingContext": context, "signing_context": context, "context": {"release": {"signingContext": context}}}
    with pytest.raises(bitcoin.BitcoinContextError, match="two effective"):
        bitcoin.preflight_ecdsa_operation(operation)


@pytest.mark.parametrize(("payload", "message"), [
    ({"a": 1.5}, "plain lossless"), ({"a": 1 << 53}, "unsafe integer"),
    ({"a": "\ud800"}, "surrogate"), ({"a": [None] * 2049}, "member bounds"),
    ({str(i): None for i in range(129)}, "member bounds"), ({1: "value"}, "non-string"),
    ({"a": object()}, "plain lossless"), ({"ciphertext": "a" * 262_145}, "material string"),
])
def test_iterative_plain_json_guards(payload, message):
    with pytest.raises(bitcoin.BitcoinContextError, match=message):
        bitcoin._bounded_json_size(payload, maximum=bitcoin.MAX_OPERATION_BYTES)


def test_depth_nodes_cycle_and_unicode_byte_accounting():
    assert bitcoin._bounded_json_size({"é": ["😀", "\n", "\0", True, None, -1]}, maximum=1000) == len(
        json.dumps({"é": ["😀", "\n", "\0", True, None, -1]}, ensure_ascii=False, separators=(",", ":")).encode()
    )
    value = None
    for _ in range(16):
        value = [value]
    bitcoin._bounded_json_size(value, maximum=1000)
    with pytest.raises(bitcoin.BitcoinContextError, match="depth"):
        bitcoin._bounded_json_size([value], maximum=1000)
    cycle = []
    cycle.append(cycle)
    with pytest.raises(bitcoin.BitcoinContextError, match="cycle"):
        bitcoin._bounded_json_size(cycle, maximum=1000)
    with pytest.raises(bitcoin.BitcoinContextError, match="4096 JSON nodes"):
        bitcoin._bounded_json_size([[None] * 2048, [None] * 2048], maximum=100_000)


def test_legacy_contexts_without_new_kind_or_proof_keep_their_own_precedence():
    # No generic new-kind walk for unrelated legacy inputs.
    assert not bitcoin.preflight_ecdsa_operation({"signing_context": {"kind": "evm-transaction", "a": object()}})

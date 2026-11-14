"""Public ECDSA digest and Legacy funding-proof owner regressions.

The existing Taproot corpus cannot catch a valid Legacy signature whose claimed
input amount understates its fee. This owner suite anchors both ECDSA hashes in
the independent public corpus and exercises complete funding bytes separately
from the narrow spending grammar. Nothing here is a chain or custody test.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from storage import vault_bitcoin as bitcoin

VECTORS = json.loads((
    Path(__file__).parent / "fixtures/vault_signing/hd-bitcoin/transaction-signing-vectors.json"
).read_text())["synthetic_public_corpus_vectors"]
SCRIPT = bytes.fromhex("76a914" + "11" * 20 + "88ac")


def _compact(value: int) -> bytes:
    if value < 253:
        return bytes([value])
    if value <= 65_535:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


def _parent(*, scripts: tuple[bytes, ...] = (b"",), outputs=None) -> bytes:
    if outputs is None:
        outputs = ((1_000_000, SCRIPT), (0, b"\x6a"))
    # Deliberately NOT a spending transaction: opaque version/locktime/sequence,
    # null coinbase-shaped inputs and zero/nonstandard nonselected output.
    inputs = b"".join(bytes(32) + b"\xff" * 4 + _compact(len(s)) + s + b"\x01\x02\x03\x04" for s in scripts)
    serialized_outputs = b"".join(a.to_bytes(8, "little") + _compact(len(s)) + s for a, s in outputs)
    return b"\xff" * 4 + _compact(len(scripts)) + inputs + _compact(len(outputs)) + serialized_outputs + b"\x99" * 4


def _bound_case():
    raw = _parent()
    digest = hashlib.sha256(hashlib.sha256(raw).digest()).digest()
    spend = (
        b"\x02\0\0\0\x01" + digest + bytes(4) + b"\x00\xfd\xff\xff\xff\x01"
        + (99_000).to_bytes(8, "little") + bytes([len(SCRIPT)]) + SCRIPT + bytes(4)
    )
    prevout = {
        "parentTransaction": raw.hex(), "txid": digest[::-1].hex(), "vout": 0,
        "amountSats": 1_000_000, "scriptPubKey": SCRIPT.hex(),
    }
    return prevout, bitcoin.parse_unsigned_transaction(spend.hex())


@pytest.mark.parametrize("case", VECTORS, ids=lambda case: f"{case['id']}-{case['address_type']}")
def test_ecdsa_sighashes_match_independent_public_corpus(case):
    """B01: neither expected hash nor scriptCode comes from the owner under test."""
    tx = bitcoin.parse_unsigned_transaction(case["unsigned_transaction"])
    if case["address_type"] == "p2pkh":
        digest = bitcoin.compute_legacy_sighash(tx, script_code=bytes.fromhex(case["script_code"]))
    else:
        digest = bitcoin.compute_bip143_sighash(
            tx, script_code=bytes.fromhex(case["script_code"]),
            prevout_amount_sats=case["prevout_amount_satoshis"],
        )
    assert digest.hex() == case["digest"]


def test_funding_opaque_grammar_does_not_inherit_spend_policy():
    """B02: a stripped funding parent is serialized evidence, not a valid-spend claim."""
    raw = _parent(scripts=(b"\x00\xff",), outputs=((1_000_000, SCRIPT), (0, b""), (0, b"\x6a\xff")))
    parent = bitcoin.parse_funding_transaction(raw.hex())
    assert parent.raw == raw
    assert [(o.amount_sats, o.script_pub_key) for o in parent.outputs] == [
        (1_000_000, SCRIPT), (0, b""), (0, b"\x6a\xff"),
    ]
    assert parent.txid_digest == hashlib.sha256(hashlib.sha256(raw).digest()).digest()
    with pytest.raises(bitcoin.BitcoinContextError, match="version"):
        bitcoin.parse_unsigned_transaction(raw.hex())


def test_legacy_amount_lie_refuses_despite_unchanged_sighash():
    """B02: actual1M/claimed100k/output99k cannot hide901k fee as1k."""
    prevout, tx = _bound_case()
    selected = bitcoin.validate_legacy_funding(prevout, tx, expected_script=SCRIPT)
    assert selected.amount_sats == 1_000_000
    signed_digest = bitcoin.compute_legacy_sighash(tx, script_code=SCRIPT)
    claimed = {**prevout, "amountSats": 100_000}
    assert bitcoin.compute_legacy_sighash(tx, script_code=SCRIPT) == signed_digest
    with pytest.raises(bitcoin.BitcoinContextError, match="claimed amount"):
        bitcoin.validate_legacy_funding(claimed, tx, expected_script=SCRIPT)


@pytest.mark.parametrize(("field", "value", "error"), [
    ("txid", "00" * 32, "txid"),
    ("vout", 1, "vout"),
    ("vout", True, "integer"),
    ("vout", 1 << 32, "integer"),
    ("amountSats", True, "integer"),
    ("amountSats", 0, "integer"),
    ("scriptPubKey", "76a914" + "22" * 20 + "88ac", "script"),
])
def test_legacy_funding_redundancies_are_not_caller_authority(field, value, error):
    prevout, tx = _bound_case()
    prevout[field] = value
    with pytest.raises(bitcoin.BitcoinContextError, match=error):
        bitcoin.validate_legacy_funding(prevout, tx, expected_script=SCRIPT)


def test_legacy_txid_byte_order_and_selected_key_are_exact():
    prevout, tx = _bound_case()
    wrong = copy.deepcopy(prevout)
    wrong["txid"] = bytes.fromhex(prevout["txid"])[::-1].hex()
    with pytest.raises(bitcoin.BitcoinContextError, match="txid"):
        bitcoin.validate_legacy_funding(wrong, tx, expected_script=SCRIPT)
    with pytest.raises(bitcoin.BitcoinContextError, match="selected signing key"):
        bitcoin.validate_legacy_funding(prevout, tx, expected_script=bytes.fromhex("76a914" + "22" * 20 + "88ac"))


@pytest.mark.parametrize(("mutate", "error"), [
    (lambda raw: raw[:4] + b"\x00\x01" + raw[5:], "input count"),
    (lambda raw: raw[:4] + b"\xfd\x01\x00" + raw[5:], "minimal CompactSize"),
    (lambda raw: raw[:4] + b"\xfe\x00\x00\x01\x00" + raw[5:], "supported bound"),
    (lambda raw: raw[:4] + b"\xff" + (1 << 53).to_bytes(8, "little") + raw[5:], "supported bound"),
    (lambda raw: raw[:-1], "truncated"),
    (lambda raw: raw + b"\x00", "trailing"),
])
def test_malformed_funding_refuses_before_hashing(monkeypatch, mutate, error):
    """B03: decode/range/full-consumption failures precede the txid hash."""
    def forbidden_hash(_):
        raise AssertionError("malformed funding must not reach hashing")
    monkeypatch.setattr(bitcoin, "_double_sha256", forbidden_hash)
    prevout, tx = _bound_case()
    prevout["parentTransaction"] = mutate(bytes.fromhex(prevout["parentTransaction"])).hex()
    with pytest.raises(bitcoin.BitcoinContextError, match=error):
        bitcoin.validate_legacy_funding(prevout, tx, expected_script=SCRIPT)


@pytest.mark.parametrize("count", [1, 128, 129])
def test_funding_member_count_bound(count):
    raw = _parent(scripts=(b"",) * count, outputs=((0, b""),) * count)
    if count <= 128:
        assert len(bitcoin.parse_funding_transaction(raw.hex()).outputs) == count
    else:
        with pytest.raises(bitcoin.BitcoinContextError, match="supported bound"):
            bitcoin.parse_funding_transaction(raw.hex())


@pytest.mark.parametrize("length", [252, 253, 10_000, 10_001])
def test_funding_compact_script_bound(length):
    raw = _parent(scripts=(b"\xab" * length,), outputs=((0, b"\xcd" * length),))
    if length <= 10_000:
        assert bitcoin.parse_funding_transaction(raw.hex()).outputs[0].script_pub_key == b"\xcd" * length
    else:
        with pytest.raises(bitcoin.BitcoinContextError, match="script length exceeds"):
            bitcoin.parse_funding_transaction(raw.hex())


def test_funding_output_money_range_and_total():
    for outputs in [((bitcoin.MONEY_RANGE_SATS + 1, b""),), ((bitcoin.MONEY_RANGE_SATS, b""), (1, b""))]:
        with pytest.raises(bitcoin.BitcoinContextError, match="amount|money range"):
            bitcoin.parse_funding_transaction(_parent(outputs=outputs).hex())


def test_maximum_parent_capacity_is_inclusive_without_larger_copy():
    """B03: the legal1MiB capacity is real; script/count caps are not lower proxies."""
    scripts = [b""] * 128
    remaining = bitcoin.MAX_FUNDING_BYTES - len(_parent(scripts=tuple(scripts), outputs=((0, b""),)))
    for index in range(128):
        if remaining == 0:
            break
        size = min(10_000, remaining - 2) if remaining >= 255 else remaining
        scripts[index] = b"\x00" * size
        remaining -= size + (2 if size >= 253 else 0)
    assert remaining == 0
    raw = _parent(scripts=tuple(scripts), outputs=((0, b""),))
    assert len(raw) == 1_048_576
    assert bitcoin.parse_funding_transaction(raw.hex()).raw == raw
    with pytest.raises(bitcoin.BitcoinContextError, match="at most 1,048,576"):
        bitcoin.parse_funding_transaction((raw + b"\x00").hex())


def test_bip143_amount_commits_but_witness_program_is_not_script_code():
    case = next(v for v in VECTORS if v["address_type"] == "p2wpkh")
    tx = bitcoin.parse_unsigned_transaction(case["unsigned_transaction"])
    assert bitcoin.compute_bip143_sighash(
        tx, script_code=bytes.fromhex(case["script_code"]), prevout_amount_sats=case["prevout_amount_satoshis"] + 1,
    ).hex() != case["digest"]
    with pytest.raises(bitcoin.BitcoinContextError, match="scriptCode"):
        bitcoin.compute_bip143_sighash(
            tx, script_code=bytes.fromhex(case["prevout_script_pubkey"]),
            prevout_amount_sats=case["prevout_amount_satoshis"],
        )

"""Strict, bounded Bitcoin Taproot key-path approval context validation.

This module is the shared Avibe service boundary for the protected Bitcoin V1
workflow. It intentionally parses only the one-input, one-or-two-output,
non-witness transaction grammar described by ``docs/plans/vault-protected-bitcoin.md``.
It is not a general Bitcoin transaction or script parser.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from storage.vault_addresses import _hash160 as _vault_hash160, taproot_output_key_xonly

BITCOIN_CONTEXT_KIND = "bitcoin-taproot-keypath-v1"
BITCOIN_DIGEST_ALGORITHM = "bip341-sighash-default-keypath"
BITCOIN_SCHEME = "schnorr-secp256k1-bip341-keypath"
BITCOIN_NETWORKS = frozenset({"bitcoin", "testnet", "signet", "regtest"})
MONEY_RANGE_SATS = 21_000_000 * 100_000_000
MIN_PREVOUT_SATS = 1
MAX_TRANSACTION_BYTES = 137
MAX_TRANSACTION_HEX = MAX_TRANSACTION_BYTES * 2
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BitcoinContextError(ValueError):
    """The typed protected Bitcoin context is invalid."""


@dataclass(frozen=True)
class BitcoinOutput:
    amount_sats: int
    script_pub_key: bytes
    kind: str


@dataclass(frozen=True)
class BitcoinTransaction:
    raw: bytes
    version: int
    prevout_hash_le: bytes
    prevout_index: int
    sequence: int
    outputs: tuple[BitcoinOutput, ...]
    locktime: int

    @property
    def unsigned_txid(self) -> str:
        return _double_sha256(self.raw)[::-1].hex()


@dataclass(frozen=True)
class ValidatedBitcoinContext:
    context: dict[str, Any]
    transaction: BitcoinTransaction
    digest: str
    output_key_xonly: bytes
    same_key_output_indexes: tuple[int, ...]
    fee_sats: int
    signed_weight: int
    signed_vsize: int
    fee_rate_numerator: int
    high_fee_warning: bool
    rbf_signals: bool

    @property
    def net_value_leaving_key_sats(self) -> int:
        returned = sum(
            self.transaction.outputs[index].amount_sats for index in self.same_key_output_indexes
        )
        return self.context["prevout"]["amountSats"] - returned


_SCRIPT_KIND_LENGTHS = {
    "p2pkh": 25,
    "p2sh": 23,
    "p2wpkh": 22,
    "p2wsh": 34,
    "p2tr": 34,
}
_DUST_FLOORS = {
    "p2pkh": 546,
    "p2sh": 540,
    "p2wpkh": 294,
    "p2wsh": 330,
    "p2tr": 330,
}


def _fail(message: str) -> None:
    raise BitcoinContextError(message)


def _ensure_exact_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = set(value) - allowed
    if extra:
        _fail(f"{label} contains unsupported fields: {', '.join(sorted(str(item) for item in extra))}")
    missing = allowed - set(value)
    if missing:
        _fail(f"{label} is missing required fields: {', '.join(sorted(missing))}")


def _ensure_lower_hex(value: Any, *, field: str, length: int | None = None) -> str:
    if not isinstance(value, str) or (length is not None and len(value) != length) or not value or not _HEX_RE.fullmatch(value):
        suffix = f" of length {length}" if length is not None else ""
        _fail(f"{field} must be lowercase hexadecimal{suffix}")
    return value


def _ensure_integer(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{field} must be an integer in {minimum}..{maximum}")
    return value


def _read_compact_size(raw: bytes, offset: int, field: str) -> tuple[int, int]:
    if offset >= len(raw):
        _fail(f"transaction is truncated before {field}")
    value = raw[offset]
    if value >= 0xFD:
        _fail(f"{field} must use a single-byte CompactSize")
    return value, offset + 1


def _read_exact(raw: bytes, offset: int, length: int, field: str) -> tuple[bytes, int]:
    end = offset + length
    if end > len(raw):
        _fail(f"transaction is truncated while reading {field}")
    return raw[offset:end], end


def _script_kind(script: bytes) -> str:
    if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[-2:] == b"\x88\xac":
        return "p2pkh"
    if len(script) == 23 and script[:2] == b"\xa9\x14" and script[-1:] == b"\x87":
        return "p2sh"
    if len(script) == 22 and script[:2] == b"\x00\x14":
        return "p2wpkh"
    if len(script) == 34 and script[:2] == b"\x00\x20":
        return "p2wsh"
    if len(script) == 34 and script[:2] == b"\x51\x20":
        return "p2tr"
    _fail("transaction output uses an unsupported script template")


def parse_unsigned_transaction(unsigned_transaction: str) -> BitcoinTransaction:
    if not isinstance(unsigned_transaction, str):
        _fail("unsignedTransaction must be a string")
    if len(unsigned_transaction) == 0 or len(unsigned_transaction) % 2 or len(unsigned_transaction) > MAX_TRANSACTION_HEX:
        _fail("unsignedTransaction must be lowercase hex and at most 137 bytes")
    if not _HEX_RE.fullmatch(unsigned_transaction):
        _fail("unsignedTransaction must be lowercase hex without 0x or whitespace")
    raw = bytes.fromhex(unsigned_transaction)
    if len(raw) > MAX_TRANSACTION_BYTES:
        _fail("unsignedTransaction exceeds the 137-byte V1 limit")
    if len(raw) < 4:
        _fail("transaction is truncated before version")
    if raw[:4] != b"\x02\x00\x00\x00":
        _fail("transaction version must be exactly 2")
    offset = 4
    input_count, offset = _read_compact_size(raw, offset, "input count")
    if input_count != 1:
        _fail("transaction must contain exactly one input")
    prevout_hash, offset = _read_exact(raw, offset, 32, "outpoint hash")
    if prevout_hash == bytes(32):
        _fail("outpoint hash must not be null")
    prevout_index_raw, offset = _read_exact(raw, offset, 4, "outpoint index")
    prevout_index = int.from_bytes(prevout_index_raw, "little")
    script_sig_length, offset = _read_compact_size(raw, offset, "scriptSig length")
    if script_sig_length != 0:
        _fail("scriptSig must be empty")
    sequence_raw, offset = _read_exact(raw, offset, 4, "sequence")
    sequence = int.from_bytes(sequence_raw, "little")
    if sequence not in {0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF}:
        _fail("sequence must be 0xfffffffd, 0xfffffffe, or 0xffffffff")
    output_count, offset = _read_compact_size(raw, offset, "output count")
    if output_count not in {1, 2}:
        _fail("transaction must contain one or two outputs")
    outputs: list[BitcoinOutput] = []
    for index in range(output_count):
        amount_raw, offset = _read_exact(raw, offset, 8, f"output {index} amount")
        amount = int.from_bytes(amount_raw, "little")
        _ensure_integer(amount, field=f"output {index} amount", minimum=1, maximum=MONEY_RANGE_SATS)
        script_length, offset = _read_compact_size(raw, offset, f"output {index} script length")
        script, offset = _read_exact(raw, offset, script_length, f"output {index} script")
        kind = _script_kind(script)
        if script_length != _SCRIPT_KIND_LENGTHS[kind]:
            _fail(f"output {index} script length is not canonical for {kind}")
        if amount < _DUST_FLOORS[kind]:
            _fail(f"output {index} is below the V1 {kind} dust floor")
        outputs.append(BitcoinOutput(amount_sats=amount, script_pub_key=script, kind=kind))
    locktime_raw, offset = _read_exact(raw, offset, 4, "locktime")
    locktime = int.from_bytes(locktime_raw, "little")
    if locktime != 0:
        _fail("locktime must be exactly zero")
    if offset != len(raw):
        _fail("transaction has trailing bytes")
    total = sum(output.amount_sats for output in outputs)
    _ensure_integer(total, field="output total", minimum=1, maximum=MONEY_RANGE_SATS)
    return BitcoinTransaction(
        raw=raw,
        version=2,
        prevout_hash_le=prevout_hash,
        prevout_index=prevout_index,
        sequence=sequence,
        outputs=tuple(outputs),
        locktime=locktime,
    )


def _sha256(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _double_sha256(value: bytes) -> bytes:
    return _sha256(_sha256(value))


def _tagged_hash(tag: str, value: bytes) -> bytes:
    tag_hash = _sha256(tag.encode("ascii"))
    return _sha256(tag_hash + tag_hash + value)


def _output_serialization(output: BitcoinOutput) -> bytes:
    return output.amount_sats.to_bytes(8, "little") + bytes([len(output.script_pub_key)]) + output.script_pub_key


def compute_taproot_sighash(tx: BitcoinTransaction, *, prevout_amount_sats: int, prevout_script: bytes) -> bytes:
    prevouts = tx.prevout_hash_le + tx.prevout_index.to_bytes(4, "little")
    amounts = prevout_amount_sats.to_bytes(8, "little")
    script_pubkeys = bytes([len(prevout_script)]) + prevout_script
    sequences = tx.sequence.to_bytes(4, "little")
    outputs = b"".join(_output_serialization(output) for output in tx.outputs)
    message = b"".join(
        (
            b"\x00",  # epoch
            b"\x00",  # SIGHASH_DEFAULT
            tx.version.to_bytes(4, "little"),
            tx.locktime.to_bytes(4, "little"),
            _sha256(prevouts),
            _sha256(amounts),
            _sha256(script_pubkeys),
            _sha256(sequences),
            _sha256(outputs),
            b"\x00",  # no annex, key-path spend
            (0).to_bytes(4, "little"),
        )
    )
    return _tagged_hash("TapSighash", message)


def _hash160(value: bytes) -> bytes:
    return _vault_hash160(value)


def _same_key_legacy_scripts(compressed_public_key: bytes) -> set[bytes]:
    key_hash = _hash160(compressed_public_key)
    p2pkh = b"\x76\xa9\x14" + key_hash + b"\x88\xac"
    p2wpkh = b"\x00\x14" + key_hash
    redeem = b"\x00\x14" + key_hash
    p2sh_p2wpkh = b"\xa9\x14" + _hash160(redeem) + b"\x87"
    return {p2pkh, p2wpkh, p2sh_p2wpkh}


def validate_context_shape(context: Any, *, expected_digest: str) -> dict[str, Any]:
    if not isinstance(context, dict):
        _fail("signing_context must be an object")
    _ensure_exact_fields(
        context,
        {"kind", "network", "unsignedTransaction", "prevout", "digestAlgorithm", "digest"},
        "bitcoin signing_context",
    )
    if context.get("kind") != BITCOIN_CONTEXT_KIND:
        _fail("unsupported Bitcoin signing_context kind")
    network = context.get("network")
    if not isinstance(network, str) or network not in BITCOIN_NETWORKS:
        _fail("bitcoin signing_context network is invalid")
    unsigned_transaction = _ensure_lower_hex(
        context.get("unsignedTransaction"), field="unsignedTransaction"
    )
    if len(unsigned_transaction) > MAX_TRANSACTION_HEX:
        _fail("unsignedTransaction exceeds the 137-byte V1 limit")
    prevout = context.get("prevout")
    if not isinstance(prevout, dict):
        _fail("bitcoin signing_context.prevout must be an object")
    _ensure_exact_fields(prevout, {"amountSats", "scriptPubKey"}, "bitcoin signing_context.prevout")
    _ensure_integer(
        prevout.get("amountSats"),
        field="prevout.amountSats",
        minimum=MIN_PREVOUT_SATS,
        maximum=MONEY_RANGE_SATS,
    )
    script = _ensure_lower_hex(prevout.get("scriptPubKey"), field="prevout.scriptPubKey", length=68)
    if not script.startswith("5120"):
        _fail("prevout.scriptPubKey must be a lowercase P2TR output key script")
    if context.get("digestAlgorithm") != BITCOIN_DIGEST_ALGORITHM:
        _fail("unsupported Bitcoin digest algorithm")
    digest = _ensure_lower_hex(context.get("digest"), field="signing_context.digest", length=64)
    if expected_digest != digest:
        _fail("Bitcoin signing_context digest must match the sign request digest exactly")
    # Keep a shallow copy so callers never persist a caller-owned mutable object.
    return {
        **context,
        "prevout": dict(prevout),
    }


def validate_context(
    context: Any,
    *,
    expected_digest: str,
    compressed_public_key: str | None = None,
) -> ValidatedBitcoinContext:
    normalized = validate_context_shape(context, expected_digest=expected_digest)
    tx = parse_unsigned_transaction(normalized["unsignedTransaction"])
    prevout_script = bytes.fromhex(normalized["prevout"]["scriptPubKey"])
    prevout_amount = normalized["prevout"]["amountSats"]
    if compressed_public_key is None:
        output_key = prevout_script[2:]
        public_key_bytes = None
    else:
        if not isinstance(compressed_public_key, str):
            _fail("signing public key must be compressed secp256k1")
        try:
            public_key_bytes = bytes.fromhex(compressed_public_key)
            if len(public_key_bytes) != 33 or public_key_bytes[0] not in {2, 3}:
                raise ValueError
            output_key = taproot_output_key_xonly(compressed_public_key).to_bytes(32, "big")
        except (ValueError, TypeError) as exc:
            _fail("signing public key must be compressed secp256k1")
        if prevout_script != b"\x51\x20" + output_key:
            _fail("prevout.scriptPubKey does not bind to the signing key's BIP86 output key")
    if public_key_bytes is not None:
        forbidden_scripts = _same_key_legacy_scripts(public_key_bytes)
        for index, output in enumerate(tx.outputs):
            if output.script_pub_key in forbidden_scripts:
                _fail(f"output {index} uses a same-key legacy or SegWit-v0 address that this workflow cannot spend")
    same_key_indexes = tuple(
        index for index, output in enumerate(tx.outputs) if output.script_pub_key == prevout_script
    )
    if len(same_key_indexes) > 1:
        _fail("transaction may contain at most one same-key P2TR return output")
    output_total = sum(output.amount_sats for output in tx.outputs)
    fee = prevout_amount - output_total
    if fee <= 0:
        _fail("transaction fee must be positive")
    signed_weight = len(tx.raw) * 4 + 68
    signed_vsize = (signed_weight + 3) // 4
    if fee > 10_000 * signed_vsize:
        _fail("transaction fee rate exceeds the 10,000 sat/vB V1 safety ceiling")
    digest_bytes = compute_taproot_sighash(
        tx,
        prevout_amount_sats=prevout_amount,
        prevout_script=prevout_script,
    )
    digest = digest_bytes.hex()
    if digest != normalized["digest"]:
        _fail("Bitcoin signing_context digest does not match the independently computed BIP341 digest")
    return ValidatedBitcoinContext(
        context=normalized,
        transaction=tx,
        digest=digest,
        output_key_xonly=output_key,
        same_key_output_indexes=same_key_indexes,
        fee_sats=fee,
        signed_weight=signed_weight,
        signed_vsize=signed_vsize,
        fee_rate_numerator=fee,
        high_fee_warning=fee > 100 * signed_vsize or fee * 100 > prevout_amount * 2,
        rbf_signals=tx.sequence == 0xFFFFFFFD,
    )


def validate_scheme_pairing(
    context: Any,
    *,
    expected_digest: str,
    scheme: str,
    compressed_public_key: str | None = None,
) -> ValidatedBitcoinContext:
    if isinstance(context, dict) and context.get("kind") == BITCOIN_CONTEXT_KIND:
        if scheme != BITCOIN_SCHEME:
            _fail("Bitcoin Taproot signing_context requires schnorr-secp256k1-bip341-keypath")
        return validate_context(
            context,
            expected_digest=expected_digest,
            compressed_public_key=compressed_public_key,
        )
    if scheme == BITCOIN_SCHEME:
        _fail("schnorr-secp256k1-bip341-keypath requires a bitcoin-taproot-keypath-v1 signing_context")
    return None  # type: ignore[return-value]

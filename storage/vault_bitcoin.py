"""Strict, bounded protected Bitcoin transaction context validation.

The shipped Taproot path and the ECDSA address-type extension share only the
narrow spending grammar. Legacy's funding proof has a distinct opaque-script
parser: it must not inherit the spending transaction's version or dust policy.
Neither parser proves consensus validity, chain inclusion or unspentness.
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
MAX_FUNDING_BYTES = 1_048_576
MAX_FUNDING_HEX = MAX_FUNDING_BYTES * 2
MAX_FUNDING_MEMBERS = 128
MAX_FUNDING_SCRIPT_BYTES = 10_000
MAX_SAFE_INTEGER = (1 << 53) - 1
ECDSA_CONTEXT_TYPES = {"bitcoin-p2pkh-v1": "p2pkh", "bitcoin-p2wpkh-v1": "p2wpkh"}
ECDSA_ALGORITHMS = {"p2pkh": "bitcoin-legacy-sighash-all", "p2wpkh": "bitcoin-bip143-sighash-all"}
ECDSA_SCHEME = "ecdsa-secp256k1-der"
MAX_CONTEXT_BYTES = 2_162_688
MAX_OPERATION_BYTES = 4_718_592
MAX_NON_PROOF_BYTES = 524_288
MAX_MATERIAL_STRING_CHARS = 262_144
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class BitcoinContextError(ValueError):
    """The typed protected Bitcoin context is invalid."""


class BitcoinTypeMismatchError(BitcoinContextError):
    code = "protected_bitcoin_type_mismatch"


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
class FundingOutput:
    amount_sats: int
    script_pub_key: bytes


@dataclass(frozen=True)
class FundingTransaction:
    raw: bytes
    outputs: tuple[FundingOutput, ...]

    @property
    def txid_digest(self) -> bytes:
        # Construction follows a complete successful parse; never hash malformed
        # or oversized proof bytes to discover whether they might be usable.
        return _double_sha256(self.raw)


@dataclass(frozen=True)
class ValidatedEcdsaContext:
    context: dict[str, Any]
    transaction: BitcoinTransaction
    public_key: str
    digest: str
    same_account_output_indexes: tuple[int, ...]
    fee_sats: int
    vsize_min: int
    vsize_max: int
    high_fee_warning: bool

    @property
    def rate_hundredths(self) -> tuple[int, int]:
        fee100 = self.fee_sats * 100
        return fee100 // self.vsize_max, (fee100 + self.vsize_min - 1) // self.vsize_min

    @property
    def net_value_leaving_sats(self) -> int:
        return self.context["prevout"]["amountSats"] - sum(
            self.transaction.outputs[i].amount_sats for i in self.same_account_output_indexes
        )


@dataclass(frozen=True)
class EcdsaClosure:
    signed_transaction: bytes
    txid: str
    wtxid: str
    weight: int
    vsize: int
    signature: dict[str, str]


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


def _funding_compact_size(raw: bytes, offset: int, *, field: str, maximum: int) -> tuple[int, int]:
    prefix, offset = _read_exact(raw, offset, 1, field)
    tag = prefix[0]
    if tag < 0xFD:
        value = tag
    else:
        width = {0xFD: 2, 0xFE: 4, 0xFF: 8}[tag]
        encoded, offset = _read_exact(raw, offset, width, field)
        value = int.from_bytes(encoded, "little")
        if value < {0xFD: 253, 0xFE: 65_536, 0xFF: 4_294_967_296}[tag]:
            _fail(f"{field} must use minimal CompactSize")
    if value > MAX_SAFE_INTEGER or value > maximum:
        _fail(f"{field} exceeds its supported bound")
    return value, offset


def parse_funding_transaction(parent_transaction: Any) -> FundingTransaction:
    """Parse bounded stripped txid serialization, without executing funding scripts."""
    if (
        not isinstance(parent_transaction, str)
        or not 2 <= len(parent_transaction) <= MAX_FUNDING_HEX
        or len(parent_transaction) % 2
    ):
        _fail("parentTransaction must be even lowercase hex of at most 1,048,576 bytes")
    if not _HEX_RE.fullmatch(parent_transaction):
        _fail("parentTransaction must be lowercase hex without 0x or whitespace")
    raw = bytes.fromhex(parent_transaction)
    _, offset = _read_exact(raw, 0, 4, "funding version")
    count, offset = _funding_compact_size(
        raw, offset, field="funding input count", maximum=MAX_FUNDING_MEMBERS,
    )
    if count == 0:
        _fail("funding input count must be in 1..128; witness serialization is unsupported")
    for index in range(count):
        _, offset = _read_exact(raw, offset, 36, f"funding input {index} outpoint")
        size, offset = _funding_compact_size(
            raw, offset, field=f"funding input {index} script length", maximum=MAX_FUNDING_SCRIPT_BYTES,
        )
        _, offset = _read_exact(raw, offset, size, f"funding input {index} script")
        _, offset = _read_exact(raw, offset, 4, f"funding input {index} sequence")
    count, offset = _funding_compact_size(
        raw, offset, field="funding output count", maximum=MAX_FUNDING_MEMBERS,
    )
    if count == 0:
        _fail("funding output count must be in 1..128")
    outputs: list[FundingOutput] = []
    total = 0
    for index in range(count):
        amount_raw, offset = _read_exact(raw, offset, 8, f"funding output {index} amount")
        amount = int.from_bytes(amount_raw, "little")
        _ensure_integer(amount, field=f"funding output {index} amount", minimum=0, maximum=MONEY_RANGE_SATS)
        total += amount
        if total > MONEY_RANGE_SATS:
            _fail("funding output total exceeds the money range")
        size, offset = _funding_compact_size(
            raw, offset, field=f"funding output {index} script length", maximum=MAX_FUNDING_SCRIPT_BYTES,
        )
        script, offset = _read_exact(raw, offset, size, f"funding output {index} script")
        outputs.append(FundingOutput(amount_sats=amount, script_pub_key=script))
    _, offset = _read_exact(raw, offset, 4, "funding locktime")
    if offset != len(raw):
        _fail("funding transaction has trailing bytes")
    return FundingTransaction(raw=raw, outputs=tuple(outputs))


def _ecdsa_exact_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    # File input diagnostics must never echo caller-supplied keys/proof content.
    if set(value) != allowed:
        _fail(f"{label} has invalid fields")


def validate_legacy_funding(
    prevout: dict[str, Any], tx: BitcoinTransaction, *, expected_script: bytes,
) -> FundingOutput:
    """Bind every claimed Legacy prevout field to the complete parsed parent."""
    if not isinstance(prevout, dict):
        _fail("Legacy prevout must be an object")
    _ecdsa_exact_fields(
        prevout, {"parentTransaction", "txid", "vout", "amountSats", "scriptPubKey"}, "Legacy prevout",
    )
    txid = _ensure_lower_hex(prevout["txid"], field="prevout.txid", length=64)
    vout = _ensure_integer(prevout["vout"], field="prevout.vout", minimum=0, maximum=0xFFFFFFFF)
    amount = _ensure_integer(
        prevout["amountSats"], field="prevout.amountSats", minimum=1, maximum=MONEY_RANGE_SATS,
    )
    script = _ensure_lower_hex(prevout["scriptPubKey"], field="prevout.scriptPubKey", length=50)
    funding = parse_funding_transaction(prevout["parentTransaction"])
    digest = funding.txid_digest
    if tx.prevout_hash_le != digest or txid != digest[::-1].hex():
        _fail("Legacy funding txid does not match the spending outpoint")
    if vout != tx.prevout_index or vout >= len(funding.outputs):
        _fail("Legacy funding vout does not match the selected outpoint")
    selected = funding.outputs[vout]
    if selected.amount_sats <= 0 or amount != selected.amount_sats:
        _fail("Legacy claimed amount does not match the funding output")
    if selected.script_pub_key != bytes.fromhex(script) or selected.script_pub_key != expected_script:
        _fail("Legacy funding script does not match the selected signing key")
    return selected


def _require_p2pkh_script_code(script_code: bytes) -> None:
    if not isinstance(script_code, bytes) or _script_kind(script_code) != "p2pkh":
        _fail("ECDSA scriptCode must be the selected 25-byte P2PKH script")


def compute_legacy_sighash(tx: BitcoinTransaction, *, script_code: bytes) -> bytes:
    """One-input legacy SIGHASH_ALL; no direct previous-amount commitment."""
    _require_p2pkh_script_code(script_code)
    preimage = b"".join((
        tx.version.to_bytes(4, "little"), b"\x01", tx.prevout_hash_le,
        tx.prevout_index.to_bytes(4, "little"), bytes([len(script_code)]), script_code,
        tx.sequence.to_bytes(4, "little"), bytes([len(tx.outputs)]),
        b"".join(_output_serialization(output) for output in tx.outputs),
        tx.locktime.to_bytes(4, "little"), b"\x01\x00\x00\x00",
    ))
    return _double_sha256(preimage)


def compute_bip143_sighash(
    tx: BitcoinTransaction, *, script_code: bytes, prevout_amount_sats: int,
) -> bytes:
    """One-input BIP143 SIGHASH_ALL with exact amount and length-prefixed scriptCode."""
    _require_p2pkh_script_code(script_code)
    _ensure_integer(
        prevout_amount_sats, field="prevout.amountSats", minimum=1, maximum=MONEY_RANGE_SATS,
    )
    outpoint = tx.prevout_hash_le + tx.prevout_index.to_bytes(4, "little")
    sequence = tx.sequence.to_bytes(4, "little")
    outputs = b"".join(_output_serialization(output) for output in tx.outputs)
    preimage = b"".join((
        tx.version.to_bytes(4, "little"), _double_sha256(outpoint), _double_sha256(sequence),
        outpoint, bytes([len(script_code)]), script_code, prevout_amount_sats.to_bytes(8, "little"),
        sequence, _double_sha256(outputs), tx.locktime.to_bytes(4, "little"), b"\x01\x00\x00\x00",
    ))
    return _double_sha256(preimage)


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


def _proof_preflight(context: Any) -> str | None:
    """Bound a proof BEFORE any kind dispatch or general JSON traversal."""
    if type(context) is not dict:
        return None
    prevout = context.get("prevout")
    if type(prevout) is not dict or "parentTransaction" not in prevout:
        return None
    proof = prevout["parentTransaction"]
    if (
        type(proof) is not str or not 2 <= len(proof) <= MAX_FUNDING_HEX
        or len(proof) % 2
    ):
        _fail("parentTransaction must be even lowercase hex of at most 1,048,576 bytes")
    if _HEX_RE.fullmatch(proof) is None:
        _fail("parentTransaction must be lowercase hex without 0x or whitespace")
    return proof


def needs_ecdsa_preflight(context: Any) -> bool:
    if type(context) is not dict:
        return False
    kind = context.get("kind")
    prevout = context.get("prevout")
    return (type(kind) is str and kind in ECDSA_CONTEXT_TYPES) or (
        type(prevout) is dict and "parentTransaction" in prevout
    )


def _bounded_json_size(
    value: Any, *, maximum: int, proof_paths: frozenset[tuple[Any, ...]] = frozenset(),
    context_paths: frozenset[tuple[Any, ...]] | None = None,
) -> int:
    """Count compact UTF-8 JSON iteratively, without encoding/canonical copies.

    Only exact, previously checked proof LOCATIONS get the proof allowance.
    Object/string identity is not authority: shared objects still serialize at
    every location. Object keys are measured but are not value nodes.
    """
    active: set[int] = set()
    stack = [(value, 0, (), False, False)]
    total = non_proof = nodes = proof_copies = 0
    while stack:
        item, depth, path, is_key, exiting = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        if depth > 16:
            _fail("new Bitcoin operation exceeds JSON depth 16")
        if not is_key:
            nodes += 1
            if nodes > 4096:
                _fail("new Bitcoin operation exceeds 4096 JSON nodes")
        kind = type(item)
        proof_bytes = 0
        if item is None:
            size = 4
        elif kind is bool:
            size = 4 if item else 5
        elif kind is int:
            if abs(item) > MAX_SAFE_INTEGER:
                _fail("new Bitcoin operation contains an unsafe integer")
            size = len(str(item))
        elif kind is str:
            if not is_key and path in proof_paths:
                proof_copies += 1
                if proof_copies > 2:
                    _fail("new Bitcoin operation exceeds two proof copies")
                size = len(item) + 2
                proof_bytes = len(item)
            else:
                if len(item) > MAX_NON_PROOF_BYTES:
                    _fail("new Bitcoin operation string exceeds its non-proof budget")
                size = 2
                for ch in item:
                    cp = ord(ch)
                    if 0xD800 <= cp <= 0xDFFF:
                        _fail("new Bitcoin operation contains an unpaired surrogate")
                    size += (
                        2 if ch in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}
                        else 6 if cp < 32
                        else 1 if cp < 128 else 2 if cp < 2048 else 3 if cp < 65_536 else 4
                    )
                    if non_proof + size > MAX_NON_PROOF_BYTES or total + size > maximum:
                        _fail("new Bitcoin operation exceeds its JSON byte budget")
        elif kind in (dict, list):
            if kind is dict and context_paths is not None and needs_ecdsa_preflight(item) and path not in context_paths:
                _fail("new Bitcoin operation contains an additional embedded context copy")
            if id(item) in active:
                _fail("new Bitcoin operation contains a cycle")
            if len(item) > (128 if kind is dict else 2048):
                _fail("new Bitcoin operation exceeds object/array member bounds")
            active.add(id(item))
            stack.append((item, depth, path, False, True))
            size = 2 + max(0, len(item) - 1)
            if kind is dict:
                size += len(item)  # colons
                for key, child in item.items():
                    if type(key) is not str:
                        _fail("new Bitcoin operation contains a non-string key")
                    if key == "parentTransaction" and (*path, key) not in proof_paths:
                        _fail("new Bitcoin operation contains an additional embedded proof copy")
                    if key in {"ciphertext", "nonce", "wrap_meta", "wrapMeta"} and (
                        type(child) is str and len(child) > MAX_MATERIAL_STRING_CHARS
                    ):
                        _fail("new Bitcoin material string exceeds its bound")
                    stack.append((child, depth + 1, (*path, key), False, False))
                    stack.append((key, depth + 1, path, True, False))
            else:
                stack.extend((child, depth + 1, (*path, i), False, False) for i, child in enumerate(item))
        else:
            _fail("new Bitcoin operation requires plain lossless JSON values")
        total += size
        non_proof += size - proof_bytes
        if total > maximum or non_proof > MAX_NON_PROOF_BYTES:
            _fail("new Bitcoin operation exceeds its JSON byte budget")
    return total


def _ecdsa_small_fields_preflight(context: Any) -> None:
    if type(context) is not dict:
        return
    # Run before the general walker even for malformed/missing-kind proof
    # contexts. Exact fields/hex/type pairing are validated afterwards.
    for key, maximum in (("unsignedTransaction", MAX_TRANSACTION_HEX), ("digest", 64)):
        if key in context and (type(context[key]) is not str or len(context[key]) > maximum):
            _fail(f"ECDSA context {key} exceeds its primitive bound")
    prevout = context.get("prevout")
    if type(prevout) is dict:
        for key, maximum in (("txid", 64), ("scriptPubKey", 50)):
            if key in prevout and (type(prevout[key]) is not str or len(prevout[key]) > maximum):
                _fail(f"ECDSA prevout {key} exceeds its primitive bound")


def preflight_ecdsa_context(context: Any) -> None:
    proof = _proof_preflight(context)
    _ecdsa_small_fields_preflight(context)
    native = type(context) is dict and context.get("kind") == "bitcoin-p2wpkh-v1"
    if native and proof is not None:
        _fail("Native prevout must not include a parent proof")
    paths = frozenset({("prevout", "parentTransaction")}) if proof is not None else frozenset()
    _bounded_json_size(context, maximum=4096 if native else MAX_CONTEXT_BYTES, proof_paths=paths)


def _ecdsa_context_locations(value: dict[str, Any]) -> list[tuple[tuple[str, ...], Any]]:
    roots = [((), value)]
    if type(value.get("request")) is dict:
        roots.append((("request",), value["request"]))
    contexts = []
    for path, root in roots:
        contexts.extend(((*path, key), root[key]) for key in ("signingContext", "signing_context") if key in root)
        for key in ("context", "operation_context"):
            holder = root.get(key)
            if type(holder) is dict and type(holder.get("release")) is dict:
                contexts.append(((*path, key, "release", "signingContext"), holder["release"].get("signingContext")))
        delivery = root.get("delivery")
        if type(delivery) is dict:
            contexts.extend(((*path, "delivery", key), delivery[key]) for key in ("signingContext", "signing_context") if key in delivery)
            operation = delivery.get("operation_context")
            if type(operation) is dict and type(operation.get("release")) is dict:
                contexts.append(((*path, "delivery", "operation_context", "release", "signingContext"), operation["release"].get("signingContext")))
    return contexts


def preflight_ecdsa_operation(value: Any) -> bool:
    """Inspect fixed wire locations before any generic operation clone/hash.

    This owns ONE operation, not a request collection. It is deliberately not a
    generic recursive context discovery mechanism.
    """
    if type(value) is not dict:
        return False
    locations = _ecdsa_context_locations(value)
    if not any(needs_ecdsa_preflight(context) for _, context in locations):
        return False
    # Check ALL effective proofs before any context's general walk. One malformed
    # direct copy cannot hide an oversized nested one, or vice versa.
    proofs = [(*path, "prevout", "parentTransaction") for path, context in locations if _proof_preflight(context) is not None]
    if len(proofs) > 2:
        _fail("new Bitcoin operation exceeds two effective proof copies")
    for _, context in locations:
        if needs_ecdsa_preflight(context):
            preflight_ecdsa_context(context)
    _bounded_json_size(
        value, maximum=MAX_OPERATION_BYTES, proof_paths=frozenset(proofs),
        context_paths=frozenset(path for path, _ in locations),
    )
    return True


def preflight_ecdsa_intake(payload: Any) -> bool:
    """Reject ambiguous contexts and caller-supplied capture authority, un-cloned."""
    if not preflight_ecdsa_operation(payload):
        return False
    supplied = sum(key in payload for key in ("signing_context", "signingContext"))
    delivery = payload.get("delivery")
    if type(delivery) is dict:
        supplied += sum(key in delivery for key in ("signing_context", "signingContext"))
    if supplied != 1:
        _fail("new Bitcoin intake requires exactly one signing-context alias")
    reserved = {"operation_context", "signing_key", "bitcoin_identity", "bitcoin_cohort", "card", "context"}
    if reserved.intersection(payload) or (type(delivery) is dict and reserved.intersection(delivery)):
        _fail("new Bitcoin intake contains reserved server-owned authority")
    return True


def validate_ecdsa_context_shape(context: Any, *, expected_digest: str) -> dict[str, Any]:
    preflight_ecdsa_context(context)
    if type(context) is not dict:
        _fail("ECDSA signing_context must be an object")
    _ecdsa_exact_fields(context, {
        "kind", "addressType", "network", "unsignedTransaction", "prevout", "sighashType", "digestAlgorithm", "digest",
    }, "ECDSA signing_context")
    kind = context.get("kind")
    address_type = ECDSA_CONTEXT_TYPES.get(kind) if type(kind) is str else None
    if address_type is None:
        _fail("unsupported ECDSA Bitcoin context kind")
    if context["addressType"] != address_type or context["digestAlgorithm"] != ECDSA_ALGORITHMS[address_type]:
        raise BitcoinTypeMismatchError("ECDSA context kind, addressType and digest algorithm must match")
    if type(context["sighashType"]) is not int or context["sighashType"] != 1:
        _fail("ECDSA Bitcoin requires SIGHASH_ALL (1)")
    if type(context["network"]) is not str or context["network"] not in BITCOIN_NETWORKS:
        _fail("ECDSA Bitcoin context network is invalid")
    raw = context["unsignedTransaction"]
    if type(raw) is not str or not 2 <= len(raw) <= MAX_TRANSACTION_HEX or len(raw) % 2:
        _fail("unsignedTransaction must be even lowercase hex of at most 137 bytes")
    _ensure_lower_hex(raw, field="unsignedTransaction")
    digest = _ensure_lower_hex(context["digest"], field="signing_context.digest", length=64)
    if digest != expected_digest:
        _fail("ECDSA Bitcoin digest must match the sign request digest exactly")
    prevout = context["prevout"]
    if type(prevout) is not dict:
        _fail("ECDSA prevout must be an object")
    fields = {"amountSats", "scriptPubKey"}
    if address_type == "p2pkh":
        fields |= {"parentTransaction", "txid", "vout"}
    _ecdsa_exact_fields(prevout, fields, "ECDSA prevout")
    _ensure_integer(prevout["amountSats"], field="prevout.amountSats", minimum=1, maximum=MONEY_RANGE_SATS)
    _ensure_lower_hex(prevout["scriptPubKey"], field="prevout.scriptPubKey", length=50 if address_type == "p2pkh" else 44)
    if address_type == "p2pkh":
        _ensure_lower_hex(prevout["txid"], field="prevout.txid", length=64)
        _ensure_integer(prevout["vout"], field="prevout.vout", minimum=0, maximum=0xFFFFFFFF)
        _proof_preflight(context)
    return {**context, "prevout": dict(prevout)}


def ecdsa_key_scripts(public_key: str) -> dict[str, bytes]:
    """Public script identity only; callers own ordinary-vs-HD point selection."""
    from cryptography.hazmat.primitives.asymmetric import ec

    _ensure_lower_hex(public_key, field="signing public key", length=66)
    raw = bytes.fromhex(public_key)
    try:
        if raw[0] not in (2, 3):
            raise ValueError
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), raw)
        taproot = taproot_output_key_xonly(public_key).to_bytes(32, "big")
    except (ValueError, TypeError) as exc:
        raise BitcoinContextError("signing public key must be a valid compressed secp256k1 point") from exc
    key_hash = _hash160(raw)
    return {"p2pkh": b"\x76\xa9\x14" + key_hash + b"\x88\xac", "p2wpkh": b"\x00\x14" + key_hash, "p2tr": b"\x51\x20" + taproot}


def validate_ecdsa_context(
    context: Any, *, expected_digest: str, public_key: str, return_scripts: set[bytes] | None = None,
) -> ValidatedEcdsaContext:
    normalized = validate_ecdsa_context_shape(context, expected_digest=expected_digest)
    tx = parse_unsigned_transaction(normalized["unsignedTransaction"])
    scripts = ecdsa_key_scripts(public_key)
    address_type = normalized["addressType"]
    prevout = normalized["prevout"]
    if address_type == "p2pkh":
        validate_legacy_funding(prevout, tx, expected_script=scripts["p2pkh"])
        digest = compute_legacy_sighash(tx, script_code=scripts["p2pkh"]).hex()
    else:
        if bytes.fromhex(prevout["scriptPubKey"]) != scripts["p2wpkh"]:
            _fail("Native prevout script does not match the selected signing key")
        digest = compute_bip143_sighash(
            tx, script_code=scripts["p2pkh"], prevout_amount_sats=prevout["amountSats"],
        ).hex()
    if digest != normalized["digest"]:
        _fail("ECDSA Bitcoin digest does not match the independently computed sighash")
    returns = set(scripts.values()) if return_scripts is None else return_scripts
    indexes = tuple(i for i, output in enumerate(tx.outputs) if output.script_pub_key in returns)
    if len(indexes) > 1:
        _fail("transaction may contain at most one same-account return output")
    fee = prevout["amountSats"] - sum(o.amount_sats for o in tx.outputs)
    if fee <= 0:
        _fail("transaction fee must be positive")
    n = len(tx.raw)
    minimum, maximum = (n + 44, n + 107) if address_type == "p2pkh" else ((4 * n + 50) // 4, (4 * n + 113) // 4)
    if fee > 10_000 * minimum:
        _fail("transaction fee rate exceeds the 10,000 sat/vB safety ceiling")
    return ValidatedEcdsaContext(
        context=normalized, transaction=tx, public_key=public_key, digest=digest,
        same_account_output_indexes=indexes, fee_sats=fee, vsize_min=minimum, vsize_max=maximum,
        high_fee_warning=fee > 100 * minimum or fee * 100 > prevout["amountSats"] * 2,
    )


def verify_ecdsa_result(validated: ValidatedEcdsaContext, signature: Any) -> EcdsaClosure:
    """Public verification and exact supported assembly, never signing/broadcast."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    if type(signature) is not dict or set(signature) - {"signature", "recovery_id"}:
        _fail("ECDSA result has invalid fields")
    if signature.get("recovery_id") is not None:
        _fail("ECDSA DER result must not include a recovery ID")
    encoded = signature.get("signature")
    if type(encoded) is not str or not 16 <= len(encoded) <= 142 or len(encoded) % 2:
        _fail("ECDSA result must contain DER of length 8..71 bytes")
    _ensure_lower_hex(encoded, field="ECDSA DER")
    der = bytes.fromhex(encoded)
    order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    try:
        r, s = utils.decode_dss_signature(der)
        if not 1 <= r < order or not 1 <= s <= order // 2:
            raise ValueError
        if utils.encode_dss_signature(r, s) != der:
            raise ValueError
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), bytes.fromhex(validated.public_key))
        key.verify(der, bytes.fromhex(validated.digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise BitcoinContextError("ECDSA DER must be minimal, low-S and verify under the selected point/digest") from exc
    raw = validated.transaction.raw
    public_key = bytes.fromhex(validated.public_key)
    if validated.context["addressType"] == "p2pkh":
        script_sig = bytes([len(der) + 1]) + der + b"\x01\x21" + public_key
        signed = raw[:41] + bytes([len(script_sig)]) + script_sig + raw[42:]
        weight = len(signed) * 4
        stripped = signed
    else:
        witness = b"\x02" + bytes([len(der) + 1]) + der + b"\x01\x21" + public_key
        signed = raw[:4] + b"\x00\x01" + raw[4:-4] + witness + raw[-4:]
        weight = len(raw) * 4 + len(witness) + 2
        stripped = raw
    vsize = (weight + 3) // 4
    if not validated.vsize_min <= vsize <= validated.vsize_max:
        _fail("ECDSA final assembly size is inconsistent with the reviewed bounds")
    if validated.fee_sats > 10_000 * vsize:
        _fail("ECDSA final fee rate exceeds the safety ceiling")
    return EcdsaClosure(
        signed_transaction=signed, txid=_double_sha256(stripped)[::-1].hex(),
        wtxid=_double_sha256(signed)[::-1].hex(), weight=weight, vsize=vsize,
        signature={"signature": encoded},
    )


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
) -> ValidatedBitcoinContext | None:
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
    return None

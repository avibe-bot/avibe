"""Generate PUBLIC policy fixtures from unchanged Bitcoin Core v31.1 framework.

Usage: python generate_policy.py --framework /path/to/core/test/functional
No product imports, network, wallet or RPC. NEVER fund these public keys.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).parent
    # Core modules are read only and imported intact, not copied crypto routines.
    pins = {
        "test_framework/messages.py": "c786651a517a49dd084b1d76072f755d40c5483f5be53b7322ca4f3403548b26",
        "test_framework/script.py": "24bdbfc18cfa1084c1c985218c0067c2e4c06cbd83488e5c08f2167a12bf3abf",
    }
    for name, sha in pins.items():
        assert hashlib.sha256((args.framework / name).read_bytes()).hexdigest() == sha
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.framework))
    from test_framework import messages as wire, script

    core = json.loads((root / "core-fixtures.json").read_text())
    base = core["cases"][0]
    public_key = core["keys"][0]["internal_pubkey_compressed_hex"]
    template = {
        "kind": "bitcoin-taproot-keypath-v1", "network": "regtest",
        "unsignedTransaction": base["unsigned_tx_hex"],
        "prevout": {"amountSats": 100000, "scriptPubKey": base["funding"]["spent_output_script_hex"]},
        "digestAlgorithm": "bip341-sighash-default-keypath", "digest": base["expected"]["taproot_digest_hex"],
    }
    valid, invalid = [], []

    def policy(name, amount, fee, sequence=0xfffffffd, error=None):
        tx = wire.tx_from_hex(base["unsigned_tx_hex"])
        tx.vin[0].nSequence = sequence
        tx.vout[0].nValue = amount - fee
        prevout = wire.CTxOut(amount, bytes.fromhex(template["prevout"]["scriptPubKey"]))
        context = copy.deepcopy(template)
        context["unsignedTransaction"] = tx.serialize_without_witness().hex()
        context["prevout"]["amountSats"] = amount
        context["digest"] = script.TaprootSignatureHash(tx, [prevout], 0, input_index=0).hex()
        case = {"id": name, "context": context}
        if error:
            case["error"] = error
            invalid.append(case)
        else:
            case["expected"] = {"feeSats": fee, "vsize": 111, "signalsRbf": sequence == 0xfffffffd,
                                "highFee": fee > 11100 or fee * 100 > amount * 2, "txid": tx.txid_hex}
            valid.append(case)

    for sequence in (0xfffffffd, 0xfffffffe, 0xffffffff):
        policy(f"sequence-{sequence:x}", 100000, 222, sequence)
    for name, amount, fee in (("rate-equal-warning", 1000000, 11100), ("rate-above-warning", 1000000, 11101),
                              ("percent-equal-warning", 100000, 2000), ("percent-above-warning", 100000, 2001),
                              ("rate-at-ceiling", 100000000, 1110000), ("dust-at-floor", 552, 222)):
        policy(name, amount, fee)
    policy("rate-above-ceiling", 100000000, 1110001, error="10,000")
    policy("dust-below-floor", 551, 222, error="dust")
    policy("zero-fee", 100000, 0, error="positive")
    policy("negative-fee", 100000, -1, error="positive")

    def bad_tx(name, raw, error):
        context = copy.deepcopy(template)
        context["unsignedTransaction"] = raw
        invalid.append({"id": name, "context": context, "error": error})

    raw = base["unsigned_tx_hex"]
    def change(offset, size, value):
        return raw[:offset * 2] + value + raw[(offset + size) * 2:]
    mutations = [
        ("version", 0, 4, "01000000", "version"),
        ("witness-marker", 4, 0, "0001", "one input"),
        ("multiple-inputs", 4, 1, "02", "one input"),
        ("null-outpoint", 5, 32, "00" * 32, "null"),
        ("scriptsig", 41, 1, "01", "scriptSig"),
        ("sequence", 42, 4, "fcffffff", "sequence"),
        ("zero-outputs", 46, 1, "00", "one or two"),
        ("three-outputs", 46, 1, "03", "one or two"),
        ("amount-zero", 47, 8, "00" * 8, "amount"),
        ("amount-overflow", 47, 8, "ff" * 8, "amount"),
        ("unknown-output", 56, 1, "6a", "unsupported script"),
        ("short-template", 55, 1, "21", "unsupported script"),
        ("locktime", 90, 4, "01000000", "locktime"),
    ]
    for name, offset, size, value, error in mutations:
        bad_tx(name, change(offset, size, value), error)
    for name, offset, value in (("input-count", 4, 1), ("scriptsig-size", 41, 0), ("output-count", 46, 1), ("output-size", 55, 34)):
        bad_tx(f"nonminimal-{name}", change(offset, 1, "fd" + value.to_bytes(2, "little").hex()), "CompactSize")
    for size in (1, 37, 41, 42, 46, 47, 50, 56, 80, 93):
        bad_tx(f"truncated-{size}", raw[:size * 2], "truncated|version")
    bad_tx("oversized", raw + "00" * 44, "137")
    bad_tx("trailing", raw + "00", "trailing")
    bad_tx("uppercase", raw.upper(), "lowercase")
    bad_tx("odd-hex", raw[:-1], "hex")
    bad_tx("non-hex", "gg" + raw[2:], "hex")
    bad_tx("prefixed-hex", "0x" + raw, "hex")
    bad_tx("duplicate-return", change(46, 1, "02")[:-8] + raw[47*2:90*2] + raw[-8:], "at most one")
    tx = wire.tx_from_hex(raw)
    tx.vout = [wire.CTxOut(2100000000000000, tx.vout[0].scriptPubKey)] * 2
    bad_tx("aggregate-overflow", tx.serialize_without_witness().hex(), "output total")
    for name, path, value, error in (
        ("bool-amount", ["prevout", "amountSats"], True, "integer"),
        ("fractional-amount", ["prevout", "amountSats"], 1.5, "integer"),
        ("out-of-range-amount", ["prevout", "amountSats"], 2100000000000001, "integer"),
        ("zero-prevout", ["prevout", "amountSats"], 0, "integer"),
        ("malformed-network", ["network"], {}, "network"),
        ("unknown-network", ["network"], "mainnet", "network"),
        ("extra-prevout-field", ["prevout", "parent"], "", "unsupported fields"),
        ("extra-field", ["inputIndex"], 0, "unsupported fields"),
        ("wrong-algorithm", ["digestAlgorithm"], "sha256", "algorithm"),
        ("wrong-q", ["prevout", "scriptPubKey"], core["keys"][1]["script_pubkey_hex"], "bind"),
        ("false-amount", ["prevout", "amountSats"], 100001, "computed BIP341"),
    ):
        context = copy.deepcopy(template)
        target = context
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        invalid.append({"id": name, "context": context, "error": error})
    for field in template:
        context = copy.deepcopy(template)
        del context[field]
        invalid.append({"id": f"missing-{field}", "context": context, "error": "missing"})
    result = {"warning": "PUBLIC TEST DATA. NEVER FUND. Not chain acceptance.",
              "provenance": {"coreCommit": "9be056a8a72b624dae9623b2f7bded92c2a21c91", "pins": pins},
              "publicKey": public_key, "valid": valid, "invalid": invalid}
    encoded = json.dumps(result, indent=2) + "\n"
    path = root / "policy-fixtures.json"
    if args.check:
        assert path.read_text() == encoded
    else:
        path.write_text(encoded)
    print(f"{len(valid)} valid; {len(invalid)} invalid; SHA256 {hashlib.sha256(encoded.encode()).hexdigest()}")


if __name__ == "__main__":
    main()

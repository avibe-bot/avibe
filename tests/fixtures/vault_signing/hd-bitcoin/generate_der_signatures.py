#!/usr/bin/env python3
"""Independent PUBLIC NEVER-FUND DER fixture; uses the existing oracle-only lock.

Run with the interpreter/dependencies in ../hd-corpus-generator/requirements.lock.
No product imports, network, services or custody; the seed is deliberately public.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from coincurve import PrivateKey, PublicKey
from embit import bip32

ROOT = Path(__file__).resolve().parent


def generate() -> bytes:
    raw = (ROOT / "corpus.json").read_bytes()
    corpus = json.loads(raw)
    entry = next(item for item in corpus["entries"] if item["id"] == "bip86-abandon-no-pass")
    master = bip32.HDKey.from_seed(bytes.fromhex(entry["seed_hex"]))
    digest = bytes.fromhex("01" * 32)
    signatures = []
    for account in entry["accounts"]:
        for kind in ("p2pkh", "p2wpkh"):
            field = "bitcoin_" + kind
            child = master.derive(account["paths"][field])
            private = PrivateKey(child.key.secret)
            public = private.public_key.format(compressed=True).hex()
            if public != account["public_keys"][field]:
                raise ValueError("independent private/public derivation disagrees with frozen corpus")
            signature = private.sign(digest, hasher=None)
            if not PublicKey(bytes.fromhex(public)).verify(signature, digest, hasher=None):
                raise ValueError("independent signature failed")
            signatures.append({"account": account["account"], "address_type": kind,
                               "public_key": public, "signature": signature.hex(), "recovery_id": None})
    return (json.dumps({"warning": "PUBLIC NEVER FUND. Independent embit + coincurve test signatures, not custody evidence.",
                        "corpus_sha256": hashlib.sha256(raw).hexdigest(), "id": entry["id"],
                        "seed_sha256": entry["seed_sha256"], "digest": digest.hex(), "signatures": signatures},
                       indent=2) + "\n").encode()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    data = generate()
    target = ROOT / "der-signatures.json"
    if args.verify:
        if target.read_bytes() != data:
            raise SystemExit("signature fixture differs")
        print("PASS: 40 independent low-S DER fixtures are byte-identical")
    else:
        target.write_bytes(data)

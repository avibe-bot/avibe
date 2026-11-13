#!/usr/bin/env python3
"""Recheck golden PUBLIC Bitcoin data using only the existing oracle dependency lock.

NEVER FUND. No product import, custody, network, wallet, service, or derived private
key output. This verifier derives independent embit/bip_utils points from public
seeds and checks addresses/transaction digests/DER with independent libraries.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from bip_utils import Bip32Slip10Secp256k1
from coincurve import PublicKey
from embit import bip32, ec, script, transaction
from embit.networks import NETWORKS

ROOT = Path(__file__).resolve().parent


def require(ok: bool, why: str) -> None:
    if not ok:
        raise ValueError(why)


def verify() -> None:
    raw = (ROOT / "corpus.json").read_bytes()
    require(hashlib.sha256(raw).hexdigest() == "5d44457c74b901dd5e4f3eeff877c6a9f47acf416fbbde27591af350e02501fb", "changed independent corpus")
    corpus = json.loads(raw)
    restored = copy.deepcopy(corpus)
    del restored["bitcoin_extension"]
    for entry in restored["entries"]:
        for account in entry["accounts"]:
            for kind in ("p2pkh", "p2wpkh"):
                del account["public_keys"]["bitcoin_" + kind]
                del account["paths"]["bitcoin_" + kind]
                for field in ("btc_" + kind, "btc_" + kind + "_testnet4", "bitcoin_" + kind + "_script_pubkey"):
                    del account[field]
            del account["btc_taproot_testnet4"]
    restored_bytes = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode()
    require(restored_bytes == (ROOT.parent / "hd-seed-corpus.json").read_bytes(), "old identity/provenance bytes changed")
    rows = 0
    for entry in corpus["entries"]:
        seed = bytes.fromhex(entry["seed_hex"])
        require(hashlib.sha256(seed).hexdigest() == entry["seed_sha256"], "seed identity changed")
        embit_root = bip32.HDKey.from_seed(seed)
        bip_utils_root = Bip32Slip10Secp256k1.FromSeed(seed)
        for account in entry["accounts"]:
            for kind, purpose in (("p2pkh", 44), ("p2wpkh", 84)):
                field = "bitcoin_" + kind
                path = f"m/{purpose}'/0'/0'/0/{account['account']}"
                pub = embit_root.derive(path).key.get_public_key().sec()
                require(pub == bip_utils_root.DerivePath(path).PublicKey().RawCompressed().ToBytes(), "independent BIP32 mismatch")
                require(pub.hex() == account["public_keys"][field] and path == account["paths"][field], "wrong frozen purpose key/path")
                spk = script.p2pkh(ec.PublicKey.parse(pub)) if kind == "p2pkh" else script.p2wpkh(ec.PublicKey.parse(pub))
                require(spk.data.hex() == account[field + "_script_pubkey"], "wrong script")
                for suffix, network in (("", "main"), ("_testnet4", "test")):
                    require(spk.address(NETWORKS[network]) == account["btc_" + kind + suffix], "wrong address encoding")
                rows += 1
    require(rows == 1480, "wrong corpus dimensions")
    vectors = json.loads((ROOT / "transaction-signing-vectors.json").read_text())
    for vector in vectors["synthetic_public_corpus_vectors"]:
        tx = transaction.Transaction.parse(bytes.fromhex(vector["unsigned_transaction"]))
        pub = bytes.fromhex(vector["public_key"])
        code = script.p2pkh(ec.PublicKey.parse(pub))
        digest = (tx.sighash_legacy(vector["input_index"], code, 1) if vector["address_type"] == "p2pkh" else
                  tx.sighash_segwit(vector["input_index"], code, vector["prevout_amount_satoshis"], 1))
        require(digest.hex() == vector["digest"], "independent transaction digest mismatch")
        require(PublicKey(pub).verify(bytes.fromhex(vector["der"]), digest, hasher=None), "independent transaction DER mismatch")
        signed = transaction.Transaction.parse(bytes.fromhex(vector["signed_transaction"]))
        require(signed.serialize().hex() == vector["signed_transaction"], "transaction roundtrip mismatch")
        txin = signed.vin[vector["input_index"]]
        if vector["address_type"] == "p2wpkh":
            require(txin.script_sig.data == b"" and txin.witness.items == [bytes.fromhex(vector["der_with_sighash_byte"]), pub], "wrong native witness")
        else:
            require(txin.script_sig.data.hex() == vector["script_sig"] and not txin.witness.items, "wrong legacy scriptSig")
    published = vectors["official_bip143"]
    unsigned = transaction.Transaction.parse(bytes.fromhex(published["unsigned_transaction"]))
    digest = unsigned.sighash_segwit(published["input_index"], script.Script(bytes.fromhex(published["script_code"])), published["prevout_amount_satoshis"], 1)
    require(digest.hex() == published["published_digest"], "published BIP143 digest changed")
    require(PublicKey(bytes.fromhex(published["public_key"])).verify(bytes.fromhex(published["published_der_with_sighash_byte"])[:-1], digest, hasher=None), "published BIP143 signature failed")
    print("PASS: original corpus bytes, 1480 purpose44/84 points, 2960 address encodings, four transaction digests/DER/assemblies, published BIP143")


if __name__ == "__main__":
    verify()

#!/usr/bin/env python3
"""Generate PUBLIC NEVER-FUND protected-HD contract vectors, offline.

Use the existing hd-corpus-generator locked environment (Python 3.13.11).
No Avibe runtime/DB, credential store, WebAuthn, signing or network is used.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unicodedata

from Crypto.Cipher import AES

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hd-corpus-generator"))
from generate_corpus import (  # noqa: E402
    Bip32Slip10Secp256k1, Bip39MnemonicGenerator, checked_seed, derive_accounts,
    bip32, script, NETWORKS,
)
from bip_utils import P2PKHAddrEncoder, P2WPKHAddrEncoder  # noqa: E402

WARNING = "PUBLIC DETERMINISTIC TEST DATA ONLY. All entropy, words, passphrases, seeds, VMKs and DEKs are PUBLIC. NEVER FUND OR USE THESE KEYS."
MARKER = b"avibe:protected-hd-wallet:v1\n"
assert len(MARKER) == 29


def compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def stable(value: object) -> str:
    # AAD uses only ASCII field names and bounded scalar-valid JSON; strings
    # retain literal Unicode. This is NOT the ensure_ascii fingerprint codec.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(envelope: dict) -> str:
    text = json.dumps([envelope[k] for k in ("ciphertext", "nonce", "wrap_meta")],
                      ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


def encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return ciphertext + tag


def compress_evm(value: str) -> str:
    raw = bytes.fromhex(value)
    assert len(raw) == 65 and raw[0] == 4
    return bytes([2 + (raw[-1] & 1)]).hex() + raw[1:33].hex()


def derive_bitcoin_precommitments(seed: bytes) -> list[dict]:
    """Two independent BIP32/address implementations, public test seeds only."""
    primary = Bip32Slip10Secp256k1.FromSeed(seed)
    reference = bip32.HDKey.from_seed(seed)
    rows = []
    for i in range(20):
        row = {"account": i}
        for purpose, field, address_field, script_fn, encoder in (
            (44, "bitcoin_p2pkh", "btc_legacy", script.p2pkh, P2PKHAddrEncoder),
            (84, "bitcoin_p2wpkh", "btc_segwit", script.p2wpkh, P2WPKHAddrEncoder),
        ):
            path = f"m/{purpose}'/0'/0'/0/{i}"
            public = primary.DerivePath(path).PublicKey().RawCompressed().ToBytes()
            other = reference.derive(path).key.get_public_key()
            assert public == other.sec(), f"independent BIP{purpose} public mismatch"
            address = script_fn(other).address(NETWORKS["main"])
            options = {"net_ver": b"\x00"} if purpose == 44 else {"hrp": "bc"}
            assert encoder.EncodeKey(public, **options) == address
            row[field] = public.hex()
            row[address_field] = address
        rows.append(row)
    return rows


def official_bip84_vectors() -> list[dict]:
    # Public receiving examples from CC0 BIP84, fetched2026-11-13. No signing.
    words = " ".join(["abandon"] * 11 + ["about"])
    rows = derive_bitcoin_precommitments(checked_seed(words, ""))
    expected = [
        {"account": 0, "path": "m/84'/0'/0'/0/0",
         "public_key": "0330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5f0753bf5beef9c2d91af3c",
         "address": "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"},
        {"account": 1, "path": "m/84'/0'/0'/0/1",
         "public_key": "03e775fd51f0dfb8cd865d9ff1cca2a158cf651fe997fdc9fee9c1d3b5e995ea77",
         "address": "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"},
    ]
    for item in expected:
        actual = rows[item["account"]]
        assert actual["bitcoin_p2wpkh"] == item["public_key"]
        assert actual["btc_segwit"] == item["address"]
    return expected


def make_case(length: int, passphrase: str) -> dict:
    entropy = bytes(range(length))
    words = str(Bip39MnemonicGenerator().FromEntropy(entropy))
    seed = checked_seed(words, passphrase)  # bip_utils, embit, stdlib PBKDF2 agree
    derived = derive_accounts(seed)  # independent bip_utils / embit / SLIP10 checks
    additional = derive_bitcoin_precommitments(seed)
    accounts = [{"account": a["account"], "evm": compress_evm(a["public_keys"]["evm"]),
                 "bitcoin": a["public_keys"]["bitcoin"], "solana": a["public_keys"]["solana"],
                 "bitcoin_p2pkh": additional[a["account"]]["bitcoin_p2pkh"],
                 "bitcoin_p2wpkh": additional[a["account"]]["bitcoin_p2wpkh"]}
                for a in derived]
    record = {"kind": "hd-seed", "profile": "bitcoin-address-types-v1", "accounts": accounts}
    name = f"PUBLIC_PROTECTED_HD_{length}"
    aad = stable({"domain": "avault:protected-record:aad:v2", "name": name,
                  "scheme": "machine-aesgcm-v1", "version": 1, "record": record}).encode()
    flag = int(bool(unicodedata.normalize("NFKD", passphrase)))
    plaintext = MARKER + bytes([length]) + entropy + seed + bytes([flag])
    assert len(plaintext) == 95 + length
    vmk = bytes(range(32))
    dek = bytes(range(32, 64))
    nonce = bytes(range(12))
    dek_nonce = bytes(range(12, 24))
    ciphertext = encrypt(dek, nonce, plaintext, aad)
    wrapped_dek = encrypt(vmk, dek_nonce, dek, aad)
    # Deliberately codec-only root: copies:[] cannot unlock. VMK is explicitly
    # supplied above for deterministic AEAD testing, not a valid setup fixture.
    wrap = {"v": 1, "copies": [], "scheme": "machine-aesgcm-v1", "record_meta": record,
            "dek_nonce": b64(dek_nonce), "wrapped_dek": b64(wrapped_dek),
            "public_vector_label": "用户 🚀 \"quote\" \\ slash /\u2028end"}
    envelope = {"ciphertext": b64(ciphertext), "nonce": b64(nonce), "wrap_meta": compact(wrap)}
    reordered = {"public_vector_label": wrap["public_vector_label"], **{k: v for k, v in wrap.items() if k != "public_vector_label"}}
    alternate = {**envelope, "wrap_meta": compact(reordered)}
    assert fingerprint(envelope) != fingerprint(alternate)
    expected0 = {"eth": derived[0]["eth"], "btc_legacy": additional[0]["btc_legacy"],
                 "btc_segwit": additional[0]["btc_segwit"], "btc_taproot": derived[0]["btc_taproot"],
                 "solana": derived[0]["solana"]}
    return {"id": f"entropy-{length}", "name": name, "words": words, "passphrase": passphrase,
            "entropy_hex": entropy.hex(), "seed_hex": seed.hex(), "passphrase_required": flag,
            "payload_hex": plaintext.hex(), "payload_bytes": len(plaintext), "ciphertext_bytes": len(ciphertext),
            "vmk_hex": vmk.hex(), "dek_hex": dek.hex(), "value_nonce_hex": nonce.hex(), "dek_nonce_hex": dek_nonce.hex(),
            "record_meta": record, "expected_account0": expected0, "aad": aad.decode(), "aad_hex": aad.hex(),
            "envelope": envelope, "envelope_sha256": fingerprint(envelope),
            "raw_wrap_reordered": alternate["wrap_meta"], "reordered_envelope_sha256": fingerprint(alternate)}


def main() -> None:
    cases = [make_case(n, p) for n, p in zip((16, 20, 24, 28, 32), ("", "PUBLIC", "café", "  PUBLIC é 🧀  ", "㍍ガバヴァぱばぐゞちぢ十人十色"), strict=True)]
    source = ROOT / "hd-seed-corpus.json"
    # Legacy AAD text is frozen separately; absent secp curve must stay absent.
    key = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    legacy_inputs = [
        {"id": "untyped", "name": "PUBLIC_STATIC", "record": None},
        {"id": "static-field", "name": "PUBLIC_LOGIN.username", "record": {"kind": "static"}},
        {"id": "secp-absent-curve", "name": "PUBLIC_KEY", "record": {"kind": "keypair", "public_key": key}},
        {"id": "ed25519-explicit-curve", "name": "PUBLIC_SOL", "record": {"kind": "keypair", "public_key": "11" * 32, "curve": "ed25519"}},
    ]
    for case in legacy_inputs:
        aad = stable({"domain": "avault:protected-record:aad:v2", "name": case["name"],
                      "record": case["record"], "scheme": "machine-aesgcm-v1", "version": 1})
        case.update(aad=aad, aad_hex=aad.encode().hex())
    negatives = [
        {"id": "payload-truncated", "base": "entropy-16", "payload_hex": cases[0]["payload_hex"][:-2], "reject_at": "payload-codec"},
        {"id": "payload-trailing-byte", "base": "entropy-16", "payload_hex": cases[0]["payload_hex"] + "00", "reject_at": "payload-codec"},
        {"id": "payload-bad-marker", "base": "entropy-16", "payload_hex": "00" + cases[0]["payload_hex"][2:], "reject_at": "payload-codec"},
        {"id": "payload-bad-entropy-length", "base": "entropy-16", "payload_hex": (MARKER + b"\x11" + bytes.fromhex(cases[0]["payload_hex"])[30:]).hex(), "reject_at": "payload-codec"},
        {"id": "payload-bad-passphrase-bit", "base": "entropy-16", "payload_hex": cases[0]["payload_hex"][:-2] + "02", "reject_at": "payload-codec"},
        {"id": "wrong-entropy-right-seed", "base": "entropy-16", "payload_hex": (MARKER + b"\x10" + b"\xff" + bytes.fromhex(cases[0]["entropy_hex"])[1:] + bytes.fromhex(cases[0]["seed_hex"]) + b"\0").hex(), "reject_at": "creation-reopen-equality", "note": "Length-valid. Signing cannot check entropy/seed relation with an unstored nonempty passphrase."},
        {"id": "wrong-name", "base": "entropy-16", "replace_name": "PUBLIC_PROTECTED_HD_CHANGED", "reject_at": "wrapped-dek-and-payload-aead"},
        {"id": "wrong-account-key", "base": "entropy-16", "replace_record_account0_evm": key, "reject_at": "wrapped-dek-and-payload-aead"},
        {"id": "wrong-precommitted-p2pkh", "base": "entropy-16", "replace_record_account0_field": {"bitcoin_p2pkh": key}, "reject_at": "wrapped-dek-and-payload-aead"},
        {"id": "wrong-precommitted-p2wpkh", "base": "entropy-16", "replace_record_account0_field": {"bitcoin_p2wpkh": key}, "reject_at": "wrapped-dek-and-payload-aead"},
        {"id": "missing-precommitted-p2pkh", "base": "entropy-16", "remove_account0_field": "bitcoin_p2pkh", "reject_at": "typed-metadata-before-crypto"},
        {"id": "unknown-address-type-field", "base": "entropy-16", "add_account0_field": {"bitcoin_p2sh": key}, "reject_at": "typed-metadata-before-crypto"},
        {"id": "wrong-profile", "base": "entropy-16", "replace_profile": "unsupported-v2", "reject_at": "typed-metadata-before-crypto"},
        {"id": "bool-account", "base": "entropy-16", "replace_account0_index": False, "reject_at": "typed-metadata-before-crypto"},
        {"id": "out-of-order", "base": "entropy-16", "swap_indices": [0, 1], "reject_at": "typed-metadata-before-crypto"},
        {"id": "mutable-fields-in-aad", "base": "entropy-16", "add_account0_field": {"enabled": True}, "reject_at": "typed-metadata-before-crypto"},
    ]
    result = {"version": 1, "status": "PROPOSED revision0.4 three-BTC contract vectors; not product implementation or acceptance", "warning": WARNING,
              "provenance": {"generator": "protected-hd-generator/generate_vectors.py", "oracle": "existing hd-corpus-generator locked bip_utils/embit/PyNaCl environment; PyCryptodome AES-GCM; added BIP44/84 cross-checks in bip_utils+embit", "baseline_corpus_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                             "bip84_source": "https://raw.githubusercontent.com/bitcoin/bips/master/bip-0084.mediawiki", "bip84_source_license": "CC0-1.0", "bip84_checked_at": "2026-11-13"},
              "profile": "bitcoin-address-types-v1",
              "paths": {"evm": "m/44'/60'/0'/0/i", "bitcoin": "m/86'/0'/0'/0/i", "solana": "m/44'/501'/i'/0'", "bitcoin_p2pkh": "m/44'/0'/0'/0/i", "bitcoin_p2wpkh": "m/84'/0'/0'/0/i"},
              "bitcoin_address_types": {"p2pkh": "bitcoin_p2pkh", "p2wpkh": "bitcoin_p2wpkh", "p2tr": "bitcoin"},
              "scope": "Fixed payload codec, full typed three-BTC HD AAD, deterministic value+DEK AEAD, raw-wrap fingerprint, and current actual sandbox v2 AAD bytes. Empty VMK copies make these codec/crypto fixtures, NOT accepted setup/restore envelopes. BIP44/84 keys are PUBLIC PRECOMMIT ONLY; no non-Taproot protected display/signing acceptance. No signing or ceremony attestation.",
              "official_bip84_receiving": official_bip84_vectors(),
              "marker_hex": MARKER.hex(), "cases": cases, "legacy_aad": legacy_inputs, "negative_cases": negatives}
    out = ROOT / "protected-hd-codec-vectors.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(out), "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "cases": len(cases), "legacy_aad": len(legacy_inputs), "negative_cases": len(negatives), "python": sys.version.split()[0]}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PUBLIC TEST FIXTURES ONLY. Never fund, import into a real wallet, or reuse.

Offline reproducible once random-inputs.json and sources/ have been captured.
No network, product imports, custody calls, threads, or subprocesses are used.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import hmac
import importlib.metadata
import json
from pathlib import Path
import secrets
import sys
import unicodedata

from bip_utils import (
    Bip32Slip10Ed25519, Bip32Slip10Secp256k1, Bip39Languages,
    Bip39MnemonicDecoder, Bip39MnemonicGenerator, Bip39SeedGenerator,
    EthAddrEncoder, SolAddrEncoder,
)
from Crypto.Hash import keccak
from embit import base58, bech32, bip32, bip39, ec, script
from embit.networks import NETWORKS
from nacl.signing import SigningKey

ROOT = Path(__file__).resolve().parent
WARNING = (
    "PUBLIC SYNTHETIC / PUBLISHED REFERENCE TEST FIXTURES ONLY. Synthetic entropy "
    "was generated exclusively for this corpus and has never intentionally been funded. "
    "All words, passphrases and seeds are intentionally public: NEVER RECEIVE FUNDS, "
    "never import into a real wallet, and never reuse these keys. Published reference "
    "addresses may already have third-party on-chain activity; no chain lookup was performed."
)
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
PASSPHRASES = ["PUBLIC-test-only", "caf\u00e9", "A\u030angstro\u0308m", "\u334d\u30ac\u30d0\u30f4\u30a1\u3071\u3070\u3050\u309e\u3061\u3062\u5341\u4eba\u5341\u8272", "  PUBLIC \u00e9 \U0001f9c0  "]


def check(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def nfkd(text: str) -> str:
    return unicodedata.normalize("NFKD", text)


def dump(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed_from_words(words: str, passphrase: str) -> bytes:
    # Both operands are explicitly normalized. In particular embit does NOT do this.
    return hashlib.pbkdf2_hmac("sha512", nfkd(words).encode(), ("mnemonic" + nfkd(passphrase)).encode(), 2048, 64)


def checked_seed(words: str, passphrase: str) -> bytes:
    seed = seed_from_words(words, passphrase)
    check(len(seed) == 64, "BIP39 seed must be 64 bytes")
    check(Bip39SeedGenerator(words, Bip39Languages.ENGLISH).Generate(passphrase) == seed, "bip_utils seed mismatch")
    check(bip39.mnemonic_to_seed(nfkd(words), nfkd(passphrase)) == seed, "embit seed mismatch")
    return seed


def bitcoin_output(pub: ec.PublicKey) -> dict:
    # Explicit BIP86: lift_x gives EVEN y regardless of original SEC parity.
    xonly = pub.xonly()
    even_point = ec.secp256k1.ec_pubkey_parse(b"\x02" + xonly)
    tag = hashlib.sha256(b"TapTweak").digest()
    tweak = hashlib.sha256(tag + tag + xonly).digest()  # no script tree
    check(int.from_bytes(tweak, "big") < ORDER, "BIP341 tweak out of range")
    if int.from_bytes(tweak, "big") == 0:
        output_sec = b"\x02" + xonly
    else:
        tweaked = ec.secp256k1.ec_pubkey_add(even_point, tweak)
        output_sec = ec.secp256k1.ec_pubkey_serialize(tweaked)
    output_key = output_sec[1:33]
    output_script = script.Script(b"\x51\x20" + output_key)
    check(script.p2tr(pub).data == output_script.data, "explicit BIP86 / embit p2tr mismatch")
    addresses = {}
    for network, label in [("main", "btc_taproot"), ("test", "btc_taproot_testnet")]:
        address = output_script.address(NETWORKS[network])
        hrp = NETWORKS[network]["bech32"]
        encoding, decoded_hrp, _ = bech32.bech32_decode(address)
        check(encoding == bech32.Encoding.BECH32M and decoded_hrp == hrp, "not bech32m")
        version, program = bech32.decode(hrp, address)
        check(version == 1 and bytes(program) == output_key, "wrong witness output")
        check(script.address_to_scriptpubkey(address).data == output_script.data, "address roundtrip failed")
        addresses[label] = address
    return {**addresses, "bitcoin_output_key": output_key.hex(), "bitcoin_script_pubkey": output_script.data.hex()}


def slip10_ed25519_public(seed: bytes, account: int) -> bytes:
    # Separate stdlib HMAC SLIP-0010 implementation, checked against bip_utils.
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    secret, chain = digest[:32], digest[32:]
    for component in (44, 501, account, 0):
        data = b"\0" + secret + (component + 0x80000000).to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        secret, chain = digest[:32], digest[32:]
    return bytes(SigningKey(secret).verify_key)


def derive_accounts(seed: bytes) -> list[dict]:
    secp_root = Bip32Slip10Secp256k1.FromSeed(seed)
    sol_root = Bip32Slip10Ed25519.FromSeed(seed)
    embit_root = bip32.HDKey.from_seed(seed)
    accounts = []
    for i in range(20):
        paths = {"evm": f"m/44'/60'/0'/0/{i}", "bitcoin": f"m/86'/0'/0'/0/{i}", "solana": f"m/44'/501'/{i}'/0'"}
        evm_key = secp_root.DerivePath(paths["evm"]).PublicKey()
        evm_public = evm_key.RawUncompressed().ToBytes()
        check(embit_root.derive(paths["evm"]).key.get_public_key().sec() == evm_key.RawCompressed().ToBytes(), "EVM BIP32 implementation mismatch")
        eth = EthAddrEncoder.EncodeKey(evm_public)
        address_hash = keccak.new(digest_bits=256, data=evm_public[1:]).digest()[-20:]
        check(eth.lower() == "0x" + address_hash.hex(), "EVM Keccak mismatch")
        bitcoin_public = embit_root.derive(paths["bitcoin"]).key.get_public_key()
        check(secp_root.DerivePath(paths["bitcoin"]).PublicKey().RawCompressed().ToBytes() == bitcoin_public.sec(), "BTC BIP32 implementation mismatch")
        sol_public_prefixed = sol_root.DerivePath(paths["solana"]).PublicKey().RawCompressed().ToBytes()
        check(len(sol_public_prefixed) == 33 and sol_public_prefixed[0] == 0, "unexpected bip_utils Ed25519 representation")
        sol_public = sol_public_prefixed[1:]
        check(sol_public == slip10_ed25519_public(seed, i), "Solana independent SLIP10 mismatch")
        solana = SolAddrEncoder.EncodeKey(sol_public_prefixed)
        check(solana == base58.encode(sol_public), "Solana base58 mismatch")
        accounts.append({"account": i, "eth": eth, **bitcoin_output(bitcoin_public), "solana": solana, "paths": paths,
                         "public_keys": {"evm": evm_public.hex(), "bitcoin": bitcoin_public.sec().hex(), "solana": sol_public.hex()}})
    return accounts


def official_self_checks() -> dict:
    vectors = json.loads((ROOT / "sources/vectors.json").read_text())
    for entropy, words, expected_seed, expected_xprv in vectors["english"]:
        seed = checked_seed(words, "TREZOR")
        check(seed.hex() == expected_seed, "official English BIP39 seed mismatch")
        check(Bip39MnemonicDecoder(Bip39Languages.ENGLISH).Decode(words).hex() == entropy, "official entropy mismatch")
        check(bip32.HDKey.from_seed(seed).to_base58() == expected_xprv, "official English BIP32 root mismatch")
    japanese = json.loads((ROOT / "sources/test_JP_BIP39.json").read_text())
    for row in japanese:
        seed = seed_from_words(row["mnemonic"], row["passphrase"])
        check(seed.hex() == row["seed"], "official Japanese NFKD seed mismatch")
        check(bip39.mnemonic_to_seed(nfkd(row["mnemonic"]), nfkd(row["passphrase"]), wordlist=None) == seed, "embit normalized Japanese mismatch")
        check(bip32.HDKey.from_seed(seed).to_base58() == row["bip32_xprv"], "official Japanese BIP32 root mismatch")
    # All three literal official BIP86 examples, including the change path outside corpus scope.
    text = (ROOT / "sources/bip-0086.mediawiki").read_text()
    words = "abandon " * 11 + "about"
    root = bip32.HDKey.from_seed(checked_seed(words, ""))
    import re
    count = 0
    for block in text.split("// Account 0,")[2:]:
        path = re.search(r"address = (m/[0-9'/]+)", block).group(1)
        expected = dict(re.findall(r"^(internal_key|output_key|scriptPubKey|address)\s*=\s*(\S+)", block, re.MULTILINE))
        pub = root.derive(path).key.get_public_key()
        actual = bitcoin_output(pub)
        check(pub.xonly().hex() == expected["internal_key"], "official BIP86 internal key mismatch")
        for actual_field, expected_field in [("bitcoin_output_key", "output_key"), ("bitcoin_script_pubkey", "scriptPubKey"), ("btc_taproot", "address")]:
            check(actual[actual_field] == expected[expected_field], f"official BIP86 {actual_field} mismatch")
        count += 1
    check(count == 3, "did not test all BIP86 examples")
    return {"english_bip39_known_seeds_and_xprvs": len(vectors["english"]), "japanese_bip39_known_seeds_and_xprvs": len(japanese), "bip86_published_examples": count}


def init_random_inputs() -> None:
    rows = []
    for word_count, size in [(12, 16), (24, 32)]:
        for with_pass in (False, True):
            for i in range(5):
                rows.append({"id": f"synthetic-{word_count}w-{'pass' if with_pass else 'no-pass'}-{i:02d}",
                             "entropy_hex": secrets.token_bytes(size).hex(), "passphrase": PASSPHRASES[i] if with_pass else ""})
    with (ROOT / "random-inputs.json").open("x", encoding="utf-8") as f:
        json.dump({"warning": WARNING, "entropy_source": "Python secrets.token_bytes / OS CSPRNG, generated once and frozen for reproducibility", "entries": rows}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build() -> tuple[dict, dict]:
    manifest = json.loads((ROOT / "sources/manifest.json").read_text())
    for source in manifest:
        check(hashlib.sha256((ROOT / "sources" / source["path"]).read_bytes()).hexdigest() == source["sha256"], "source content hash mismatch")
    report = official_self_checks()
    random_rows = json.loads((ROOT / "random-inputs.json").read_text())["entries"]
    check(len(random_rows) == 20, "need exactly 20 synthetic entropy cases")
    check(len({r["entropy_hex"] for r in random_rows}) == 20, "duplicate synthetic entropy")
    rows = []
    for row in random_rows:
        words = Bip39MnemonicGenerator(Bip39Languages.ENGLISH).FromEntropy(bytes.fromhex(row["entropy_hex"])).ToStr()
        rows.append({**row, "words": words})
    strata = Counter((len(r["words"].split()), bool(r["passphrase"])) for r in rows)
    check(strata == {(12, False): 5, (12, True): 5, (24, False): 5, (24, True): 5}, "incorrect synthetic strata")
    english = json.loads((ROOT / "sources/vectors.json").read_text())["english"]
    refs = []
    for i, (entropy, words, expected, _) in enumerate(english):
        if len(words.split()) in (12, 24):
            identifier = f"bip39-english-{i:02d}"
            rows.append({"id": identifier, "words": words, "passphrase": "TREZOR", "entropy_hex": entropy})
            refs.append({"id": identifier, "source": "sources/vectors.json", "language": "english", "index": i, "expected_seed_hex": expected})
    rows.append({"id": "bip86-abandon-no-pass", "words": "abandon " * 11 + "about", "passphrase": "", "entropy_hex": "00" * 16})
    entries, normalization_checks = [], []
    for row in rows:
        words, passphrase = row["words"], row["passphrase"]
        check(words == " ".join(words.split()) == words.lower() == nfkd(words), "corpus words must stay canonical English")
        check(Bip39MnemonicDecoder(Bip39Languages.ENGLISH).Decode(words).hex() == row["entropy_hex"], "entropy decode mismatch")
        check(bip39.mnemonic_to_bytes(words).hex() == row["entropy_hex"], "embit mnemonic checksum mismatch")
        seed = checked_seed(words, passphrase)
        entries.append({"id": row["id"], "words": words, "passphrase": passphrase, "entropy_hex": row["entropy_hex"], "seed_hex": seed.hex(), "seed_sha256": hashlib.sha256(seed).hexdigest(), "accounts": derive_accounts(seed)})
        if passphrase and any(ord(c) > 127 for c in passphrase):
            variants = {"NFC": unicodedata.normalize("NFC", passphrase), "NFD": unicodedata.normalize("NFD", passphrase), "NFKD": nfkd(passphrase)}
            for variant in variants.values():
                check(checked_seed(words, variant) == seed, "equivalent Unicode passphrase changed seed")
            different = passphrase + "!"
            check(checked_seed(words, different) != seed, "different passphrase did not change seed")
            raw_seed = hashlib.pbkdf2_hmac("sha512", words.encode(), ("mnemonic" + passphrase).encode(), 2048, 64)
            if nfkd(passphrase) != passphrase:
                check(raw_seed != seed, "non-NFKD PBKDF2 unexpectedly matched")
            normalization_checks.append({"id": row["id"], "equivalent_passphrases": variants, "expected_seed_sha256": hashlib.sha256(seed).hexdigest(), "different_passphrase": different, "different_passphrase_seed_sha256": hashlib.sha256(checked_seed(words, different)).hexdigest(), "non_nfkd_pbkdf2_differs": raw_seed != seed})
    check(len({e["id"] for e in entries}) == len(entries), "duplicate case id")
    check(len({e["seed_hex"] for e in entries}) == len(entries), "duplicate case seed")
    for e in entries:
        check([a["account"] for a in e["accounts"]] == list(range(20)), "missing account indices")
    for chain in ("evm", "bitcoin", "solana"):
        keys = [a["public_keys"][chain] for e in entries for a in e["accounts"]]
        check(len(set(keys)) == len(keys), f"duplicate {chain} case/account key")
    for chain in ("eth", "btc_taproot", "btc_taproot_testnet", "solana"):
        addresses = [a[chain] for e in entries for a in e["accounts"]]
        check(len(set(addresses)) == len(addresses), f"duplicate {chain} address")
    parities = Counter(a["public_keys"]["bitcoin"][:2] for e in entries for a in e["accounts"])
    check(parities["02"] > 0 and parities["03"] > 0, "missing even/odd internal-key cases")
    generator = {"script": "generate_corpus.py", "python": ".".join(map(str, sys.version_info[:3])), "libraries": {name: importlib.metadata.version(name) for name in ("bip-utils", "embit", "coincurve", "pycryptodome", "PyNaCl")}}
    provenance = {
        "sources": manifest, "official_entries": refs,
        "synthetic_entropy": "Exactly 20 unique OS-CSPRNG entropy values; five each 12-word/no-pass, 12-word/pass, 24-word/no-pass, 24-word/pass. Frozen in random-inputs.json.",
        "synthetic_inputs_sha256": hashlib.sha256((ROOT / "random-inputs.json").read_bytes()).hexdigest(),
        "seed_derivation": "PBKDF2-HMAC-SHA512, 2048 iterations, 64 bytes; NFKD(words) UTF-8 and salt UTF-8('mnemonic' + NFKD(passphrase)). Cross-checked bip_utils and explicitly normalized embit.",
        "paths": {"evm": "m/44'/60'/0'/0/i", "bitcoin": "m/86'/0'/0'/0/i", "solana": "m/44'/501'/i'/0'"},
        "account_semantics": "i is the application account index, NOT BIP44 account for EVM/BTC. All paths are explicit; no coin-class account defaults.",
        "bitcoin": "embit BIP32, checked against bip_utils; even-y lift_x of internal key; explicit tagged SHA256 TapTweak, Q=P+tG; 0x5120||x(Q); bech32m v1 mainnet bc1p.",
        "btc_taproot_testnet": "TEST ENCODING ONLY: tb1p is the exact same output key/script from m/86'/0'/0'/0/i, not a coin_type=1 derivation.",
        "public_key_formats": {"evm": "65-byte uncompressed SEC1 hex including 04", "bitcoin": "33-byte compressed SEC1 internal/untweaked key hex, including actual 02/03 parity", "solana": "raw 32-byte Ed25519 hex, no bip_utils 00 prefix", "bitcoin_output_key": "32-byte tweaked x-only key hex"},
        "address_formats": {"eth": "EIP-55 checksum 0x hex", "btc_taproot": "BIP350 bech32m bc1p", "solana": "base58 raw Ed25519 public key"},
        "unicode": "Canonical English words only in entries. Unicode NFC/NFD/NFKD passphrase equivalence and genuinely different passphrases are checked separately in self-checks.json. No whitespace/case acceptance or product rejection policy is implied.",
        "independence": "No Avibe, avault, scure, noble, custody, runtime, or real wallet inputs/calls. Public reference artifacts and Python dependencies only.",
    }
    report.update({"status": "PASS", "corpus_entries": len(entries), "synthetic_entries": 20, "official_bip39_entries": len(refs), "bip86_entries": 1, "accounts_per_entry": 20, "total_accounts": len(entries) * 20, "synthetic_strata": {f"{words}w-{'pass' if passed else 'no-pass'}": count for (words, passed), count in sorted(strata.items())}, "bitcoin_internal_parities": dict(parities), "unicode_passphrase_checks": normalization_checks, "all_seeds_64_bytes": True, "all_seeds_and_chain_case_account_keys_distinct": True})
    return {"version": 1, "generator": generator, "warning": WARNING, "provenance": provenance, "entries": entries}, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-random", action="store_true", help="Create frozen OS-CSPRNG inputs, refusing to overwrite an existing file")
    parser.add_argument("--verify", action="store_true", help="Rebuild in memory and check existing JSON byte-for-byte; never overwrite")
    args = parser.parse_args()
    check(not (args.init_random and args.verify), "choose initialization or verification")
    if args.init_random:
        init_random_inputs()
    corpus, report = build()
    for filename, obj in [("hd-seed-corpus.json", corpus), ("self-checks.json", report)]:
        path = (ROOT.parent if filename == "hd-seed-corpus.json" else ROOT) / filename
        if args.verify:
            expected = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
            check(path.read_bytes() == expected, f"nonreproducible {filename}")
        else:
            dump(path, obj)
    print(json.dumps({k: v for k, v in report.items() if k != "unicode_passphrase_checks"}, indent=2))
    for filename in ("hd-seed-corpus.json", "generate_corpus.py", "random-inputs.json", "self-checks.json"):
        path = (ROOT.parent if filename == "hd-seed-corpus.json" else ROOT) / filename
        print(hashlib.sha256(path.read_bytes()).hexdigest(), filename)


if __name__ == "__main__":
    main()

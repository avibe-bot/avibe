#!/usr/bin/env python3
"""Read-only independent custody-evidence verification. No signing/private derivation.

Usage: verify_signatures.py --corpus FILE --evidence FILE
       verify_signatures.py --corpus FILE --self-test

Success stdout contains counts and the SHA256 of the exact evidence bytes only.
Failure exits 1 with a concise reason on stderr, never signature/seed material.
"""
from __future__ import annotations
import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

from coincurve import PublicKey, PublicKeyXOnly
from coincurve.ecdsa import cdata_to_der, deserialize_compact
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SCHEMES = {"evm": "ecdsa-secp256k1-recoverable", "bitcoin": "schnorr-secp256k1-bip341-keypath", "solana": "ed25519"}
VECTOR_HASH = "b37e8826dd85eca084361dd6dc0c95ea1111ec18f4449d1bf0f6c9a44805e083"


class Rejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def strict_json(text):
    def bad_constant(_):
        raise Rejected("non-finite JSON number")
    try:
        return json.loads(text, object_pairs_hook=unique_object, parse_constant=bad_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Rejected("malformed JSON/UTF-8") from exc


def hex_bytes(value, length, name):
    require(type(value) is str and re.fullmatch(r"[0-9a-fA-F]*", value) is not None, f"invalid {name} hex")
    require(len(value) == 2 * length, f"wrong {name} length")
    return bytes.fromhex(value)


def corpus_keys(corpus):
    require(type(corpus) is dict and type(corpus.get("version")) is int and corpus["version"] == 1, "unsupported corpus")
    entries = corpus.get("entries")
    require(type(entries) is list and len(entries) > 0, "empty/malformed corpus entries")
    keys, ids = {}, set()
    for entry in entries:
        require(type(entry) is dict and type(entry.get("id")) is str and bool(entry["id"]), "invalid corpus id")
        identifier = entry["id"]
        require(identifier not in ids, "duplicate corpus id")
        ids.add(identifier)
        accounts = entry.get("accounts")
        require(type(accounts) is list and len(accounts) == 20, "corpus must contain 20 accounts per id")
        seen = set()
        for account in accounts:
            require(type(account) is dict and type(account.get("account")) is int, "invalid corpus account")
            i = account["account"]
            require(0 <= i < 20 and i not in seen, "invalid/duplicate corpus account")
            seen.add(i)
            pubs = account.get("public_keys")
            require(type(pubs) is dict, "missing corpus public keys")
            evm = hex_bytes(pubs.get("evm"), 65, "corpus EVM public key")
            require(evm[0] == 4, "corpus EVM key not uncompressed SEC1")
            btc = hex_bytes(account.get("bitcoin_output_key"), 32, "corpus Bitcoin output key")
            sol = hex_bytes(pubs.get("solana"), 32, "corpus Solana public key")
            try:
                PublicKey(evm)
                PublicKeyXOnly(btc)
                VerifyKey(sol)
            except ValueError as exc:
                raise Rejected("invalid corpus public key") from exc
            for chain, public_key in [("evm", evm), ("bitcoin", btc), ("solana", sol)]:
                keys[(identifier, i, chain)] = public_key
    return keys


def expected_input(identifier, account, chain):
    preimage = f"avibe:hd-acceptance:v1:{identifier}:{chain}:{account}".encode("utf-8")
    return preimage if chain == "solana" else hashlib.sha256(preimage).digest()


def parse_evidence(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Rejected("evidence is not UTF-8") from exc
    # Single terminal LF is permitted; any genuinely empty record is not.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    records = []
    for line in lines:
        require(bool(line.strip()), "blank evidence record")
        records.append(strict_json(line))
    return records


def validate_records(records, keys):
    seen, prepared = set(), []
    for line, record in enumerate(records, 1):
        require(type(record) is dict, f"line {line}: record must be object")
        identifier, account, chain = record.get("id"), record.get("account"), record.get("chain")
        require(type(identifier) is str and type(account) is int and type(chain) is str, f"line {line}: invalid id/account/chain types")
        require(chain in SCHEMES, f"line {line}: unsupported chain")
        input_field = "message_hex" if chain == "solana" else "digest"
        require(set(record) == {"id", "account", "chain", "scheme", input_field, "signature"}, f"line {line}: wrong record fields")
        key = (identifier, account, chain)
        require(key in keys, f"line {line}: unknown id/account/chain")
        require(key not in seen, f"line {line}: duplicate id/account/chain")
        seen.add(key)
        require(record["scheme"] == SCHEMES[chain], f"line {line}: wrong scheme")
        message = expected_input(identifier, account, chain)
        require(type(record[input_field]) is str and record[input_field] == message.hex(), f"line {line}: wrong deterministic input")
        signature = record["signature"]
        require(type(signature) is dict and set(signature) == {"signature", "recovery_id"}, f"line {line}: wrong signature fields")
        compact = hex_bytes(signature["signature"], 64, "signature")
        recovery_id = signature["recovery_id"]
        if chain == "evm":
            require(type(recovery_id) is int and 0 <= recovery_id <= 3, f"line {line}: invalid recovery_id")
        else:
            require(recovery_id is None, f"line {line}: non-EVM recovery_id must be null")
        prepared.append((chain, keys[key], message, compact, recovery_id))
    require(seen == set(keys), f"incomplete evidence: expected {len(keys)}, found {len(seen)}, missing {len(set(keys) - seen)}")
    return prepared


def verify_crypto(chain, public_key, message, signature, recovery_id):
    require(len(signature) == 64, "wrong signature length")
    try:
        if chain == "evm":
            require(len(message) == 32, "wrong EVM digest length")
            require(type(recovery_id) is int and 0 <= recovery_id <= 3, "invalid EVM recovery_id")
            r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            require(0 < r < ORDER and 0 < s <= ORDER // 2, "invalid EVM scalar or non-low-S signature")
            recovered = PublicKey.from_signature_and_message(signature + bytes([recovery_id]), message, hasher=None)
            require(recovered.format(compressed=False) == public_key, "EVM recovered public key mismatch")
            require(PublicKey(public_key).verify(cdata_to_der(deserialize_compact(signature)), message, hasher=None), "EVM signature verification failed")
        elif chain == "bitcoin":
            require(len(message) == 32 and recovery_id is None, "invalid Bitcoin digest/recovery_id")
            require(PublicKeyXOnly(public_key).verify(signature, message), "Bitcoin output-key Schnorr verification failed")
        elif chain == "solana":
            require(recovery_id is None, "invalid Solana recovery_id")
            VerifyKey(public_key).verify(message, signature)
        else:
            raise Rejected("unsupported chain")
    except Rejected:
        raise
    except (ValueError, BadSignatureError) as exc:
        raise Rejected(f"{chain} signature verification failed") from exc


def verify_evidence(data, keys):
    prepared = validate_records(parse_evidence(data), keys)
    for index, row in enumerate(prepared, 1):
        try:
            verify_crypto(*row)
        except Rejected as exc:
            raise Rejected(f"record {index}: {exc}") from exc
    return dict(Counter(row[0] for row in prepared))


def self_test(keys):
    path = Path(__file__).resolve().parent / "signature-sources/public-signature-vectors.json"
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == VECTOR_HASH, "public vector source integrity failure")
    vectors = strict_json(raw)
    positive, negative = 0, 0

    def rejects(function, *args):
        nonlocal negative
        try:
            function(*args)
        except Rejected:
            negative += 1
        else:
            raise Rejected("self-test failed to reject a known bad input")

    e = vectors["evm"]
    evm = ("evm", bytes.fromhex(e["public_key"]), bytes.fromhex(e["digest"]), bytes.fromhex(e["signature"]), e["recovery_id"])
    verify_crypto(*evm)
    positive += 1
    rejects(verify_crypto, *evm[:2], bytes(32), *evm[3:])
    rejects(verify_crypto, *evm[:3], bytes(64), evm[4])
    rejects(verify_crypto, *evm[:4], evm[4] ^ 1)
    rejects(verify_crypto, *evm[:4], True)
    high_s = evm[3][:32] + (ORDER - int.from_bytes(evm[3][32:], "big")).to_bytes(32, "big")
    rejects(verify_crypto, *evm[:3], high_s, evm[4] ^ 1)
    other_evm = next(pub for key, pub in keys.items() if key[2] == "evm" and pub != evm[1])
    rejects(verify_crypto, "evm", other_evm, *evm[2:])
    btc_positive = None
    for v in vectors["bitcoin"]:
        args = ("bitcoin", bytes.fromhex(v["public_key"]), bytes.fromhex(v["digest"]), bytes.fromhex(v["signature"]), None)
        if v["valid"]:
            verify_crypto(*args)
            positive += 1
            btc_positive = args
        else:
            rejects(verify_crypto, *args)
    require(btc_positive is not None, "missing positive Schnorr vector")
    rejects(verify_crypto, *btc_positive[:2], bytes(32), *btc_positive[3:])
    other_btc = next(pub for key, pub in keys.items() if key[2] == "bitcoin" and pub != btc_positive[1])
    rejects(verify_crypto, "bitcoin", other_btc, *btc_positive[2:])
    s = vectors["solana"]
    sol = ("solana", bytes.fromhex(s["public_key"]), bytes.fromhex(s["message_hex"]), bytes.fromhex(s["signature"]), None)
    verify_crypto(*sol)
    positive += 1
    rejects(verify_crypto, *sol[:2], b"changed", *sol[3:])
    rejects(verify_crypto, *sol[:3], bytes(64), None)
    other_sol = next(pub for key, pub in keys.items() if key[2] == "solana" and pub != sol[1])
    rejects(verify_crypto, "solana", other_sol, *sol[2:])

    # Full required tuple universe: only structural placeholders, NEVER asserted
    # cryptographically valid. No signing/private derivation is needed or used.
    records = []
    for identifier, account, chain in keys:
        records.append({"id": identifier, "account": account, "chain": chain, "scheme": SCHEMES[chain],
                        "message_hex" if chain == "solana" else "digest": expected_input(identifier, account, chain).hex(),
                        "signature": {"signature": "00" * 64, "recovery_id": 0 if chain == "evm" else None}})
    validate_records(records, keys)
    positive += 1
    rejects(validate_records, records[:-1], keys)
    rejects(validate_records, records + [records[0]], keys)
    rejects(validate_records, [], keys)
    mutations = [
        lambda r: r.update(id="not-a-corpus-id"), lambda r: r.update(account=20),
        lambda r: r.update(account=True), lambda r: r.update(account=0.0),
        lambda r: r.update(chain="ethereum"), lambda r: r.update(scheme="wrong"),
        lambda r: r.update(digest="00" * 32), lambda r: r.update(digest=r["digest"].upper()),
        lambda r: r.update(public_key=e["public_key"]), lambda r: r.update(message_hex="00"),
        lambda r: r["signature"].update(signature="00" * 63),
        lambda r: r["signature"].update(signature="0x" + "00" * 64),
        lambda r: r["signature"].update(signature="zz" * 64),
        lambda r: r["signature"].update(recovery_id=True),
        lambda r: r["signature"].update(recovery_id=4),
        lambda r: r["signature"].update(extra=0),
    ]
    # keys insertion order begins with EVM; ensure mutation target is EVM.
    require(records[0]["chain"] == "evm", "self-test expected EVM first")
    for mutate in mutations:
        bad = copy.deepcopy(records[0]); mutate(bad)
        rejects(validate_records, [bad] + records[1:], keys)
    bad_btc = copy.deepcopy(records[1]); bad_btc["signature"]["recovery_id"] = 0
    rejects(validate_records, [records[0], bad_btc] + records[2:], keys)
    rejects(strict_json, '{"id":"a","id":"b"}')
    rejects(strict_json, '{"account":NaN}')
    rejects(parse_evidence, b"\xff")
    rejects(parse_evidence, b"\n")
    rejects(parse_evidence, b"{broken}\n")
    rejects(parse_evidence, (json.dumps(records[0]) + "\n\n").encode())
    data = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
    rejects(verify_evidence, data, keys)  # full counts but invalid signatures
    return {"positive_checks": positive, "rejection_checks": negative, "required_signatures": len(keys)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--evidence", type=Path)
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        keys = corpus_keys(strict_json(args.corpus.read_bytes()))
        if args.self_test:
            print(json.dumps(self_test(keys), sort_keys=True))
            return 0
        data = args.evidence.read_bytes()
        counts = verify_evidence(data, keys)
        print(json.dumps({"counts": counts, "total": sum(counts.values()), "evidence_sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True))
        return 0
    except (Rejected, OSError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

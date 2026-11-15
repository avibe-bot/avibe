"""Public consistency checks for browser-custodied HD records; never decrypts.

The raw envelope/AAD table is authority over the server's immutable projection.
These checks are not attestation of the encrypted seed or of a browser ceremony.
Standard HD's envelope, omitted-type binding and public-table rules stay separate.
"""
from __future__ import annotations

import base64
import json
import math
import re
from typing import Any

from storage import vault_hd, vault_policy, vault_setup, vault_signatures, vault_solana
from storage.vault_crypto import Sealed, is_valid_secret_name

MAX_WRAP_BYTES = 65_536
PUBLIC_FIELDS = ("account", "evm", "bitcoin", "solana", "bitcoin_p2pkh", "bitcoin_p2wpkh")
SETTING_FIELDS = ("enabled", "name", "always_ask", "last_signer")


def parse_wrap(raw: Any) -> dict[str, Any]:
    """Bounded scalar-valid JSON; effective duplicate members are last-wins.

    Keep the original string outside this function for the existing HD fingerprint.
    Unknown root extensions survive; only the new record/table has exact fields.
    """
    try:
        if not isinstance(raw, str) or not raw or len(raw) > MAX_WRAP_BYTES or len(raw.encode("utf-8")) > MAX_WRAP_BYTES:
            raise ValueError("invalid protected HD wrap metadata size/type")
        value = json.loads(raw)

        def visit(item: Any, depth: int) -> None:
            if isinstance(item, str):
                item.encode("utf-8", errors="strict")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("non-finite protected HD metadata")
            elif isinstance(item, (dict, list)):
                if depth > 64:
                    raise ValueError("protected HD metadata is too deep")
                children = item.items() if isinstance(item, dict) else enumerate(item)
                for key, child in children:
                    if isinstance(key, str):
                        key.encode("utf-8", errors="strict")
                    visit(child, depth + 1)

        visit(value, 1)
        if not isinstance(value, dict):
            raise ValueError("protected HD wrap metadata must be an object")
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("invalid protected HD wrap metadata") from exc


def declares_record(sealed: Sealed) -> bool:
    try:
        wrap = parse_wrap(sealed.wrap_meta)
    except ValueError:
        return False
    record = wrap.get("record_meta")
    return isinstance(record, dict) and record.get("kind") == vault_hd.SIGNER_KIND


def _base64(value: Any, lengths: tuple[int, ...]) -> None:
    if not isinstance(value, str) or len(value) > ((max(lengths) + 2) // 3) * 4:
        raise ValueError("invalid protected HD envelope encoding")
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) not in lengths or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("invalid protected HD envelope length/encoding")
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid protected HD envelope encoding") from exc


def root_meta(raw: Any) -> dict[str, Any]:
    """Full reusable root projection with the new branch's bounded JSON grammar.

    Do not use the policy context canonicalizer: permitted finite extension
    numbers/depth are broader than that unrelated proof codec.
    """
    wrap = parse_wrap(raw)
    if type(wrap.get("v")) is not int or wrap["v"] != 1:
        raise ValueError("invalid protected HD root version")
    copies = wrap.get("copies")
    if not isinstance(copies, list) or not copies:
        raise ValueError("protected HD requires an existing Passkey root")
    for copy in copies:
        if (not isinstance(copy, dict) or copy.get("kind") != "passkey"
                or copy.get("kdf") != "webauthn-prf-hkdf-sha256"):
            raise ValueError("invalid protected HD root copy")
        for field, length in (("prf_salt", 32), ("nonce", 12), ("wrapped", 48)):
            _base64(copy.get(field), (length,))
        if "credential_id" in copy:
            vault_policy.credential_bytes(copy["credential_id"])
    return {key: child for key, child in wrap.items() if key not in vault_setup.RECORD_FIELDS}


def same_root(left: Any, right: Any) -> bool:
    def same(a: Any, b: Any) -> bool:
        # JSON numbers are one semantic type, but booleans are not numbers.
        if type(a) in (int, float) and type(b) in (int, float):
            return a == b
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b
    return same(root_meta(left), root_meta(right))


def public_table(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != vault_hd.ACCOUNT_LIMIT:
        raise ValueError("protected HD requires exactly 20 public accounts")
    result = []
    for index, item in enumerate(value):
        if (not isinstance(item, dict) or set(item) != set(PUBLIC_FIELDS)
                or type(item.get("account")) is not int or item["account"] != index):
            raise ValueError("protected HD accounts require exact public fields ordered 0..19")
        # No extra cross-row uniqueness rule on this authenticated protected table.
        result.append({"account": index, **{
            field: vault_hd._public_key(item[field], field) for field in PUBLIC_FIELDS if field != "account"
        }})
    return result


def envelope_table(name: Any, sealed: Sealed) -> list[dict[str, Any]]:
    if not isinstance(name, str) or not is_valid_secret_name(name):
        raise ValueError("invalid protected HD record name")
    wrap = parse_wrap(sealed.wrap_meta)
    if type(wrap.get("v")) is not int or wrap["v"] != 1 or wrap.get("scheme") != "machine-aesgcm-v1":
        raise ValueError("invalid protected HD envelope version/scheme")
    record = wrap.get("record_meta")
    if (not isinstance(record, dict) or set(record) != {"kind", "profile", "accounts"}
            or record["kind"] != vault_hd.SIGNER_KIND or record["profile"] != vault_hd.BITCOIN_PROFILE):
        raise ValueError("invalid protected HD record/profile")
    _base64(sealed.ciphertext, (127, 131, 135, 139, 143))
    _base64(sealed.nonce, (12,))
    _base64(wrap.get("dek_nonce"), (12,))
    _base64(wrap.get("wrapped_dek"), (48,))
    root_meta(sealed.wrap_meta)
    return public_table(record["accounts"])


def initial_meta(name: str, sealed: Sealed, accounts: Any, expected_account0: Any) -> dict[str, Any]:
    table = envelope_table(name, sealed)
    # Typed validation before equality avoids Python bool/int equality accepting a caller index.
    if public_table(accounts) != table:
        raise ValueError("protected HD public accounts disagree with the envelope")
    expected = vault_hd.validate_expected_account0(expected_account0, profile=vault_hd.BITCOIN_PROFILE)
    result = {"bitcoin_profile": vault_hd.BITCOIN_PROFILE, "hd_accounts": [
        {**item, "enabled": item["account"] == 0, "name": None, "always_ask": True, "last_signer": None}
        for item in table
    ]}
    actual = account_addresses(table[0])
    if any(actual[field] != value for field, value in expected.items()):
        raise ValueError("protected HD account-0 addresses disagree with the expected addresses")
    return result


def stored_accounts(name: str, sealed: Sealed, meta: dict[str, Any]) -> list[dict[str, Any]]:
    table = envelope_table(name, sealed)
    accounts = meta.get("hd_accounts")
    if meta.get("bitcoin_profile") != vault_hd.BITCOIN_PROFILE or not isinstance(accounts, list) or len(accounts) != 20:
        raise ValueError("invalid protected HD cached profile/table")
    disabled = False
    for index, item in enumerate(accounts):
        if (not isinstance(item, dict) or set(item) != {*PUBLIC_FIELDS, *SETTING_FIELDS}
                or type(item.get("account")) is not int or item["account"] != index
                or {key: item[key] for key in PUBLIC_FIELDS} != table[index]
                or type(item.get("enabled")) is not bool or type(item.get("always_ask")) is not bool):
            raise ValueError("protected HD cached accounts disagree with the envelope")
        label = item["name"]
        if label is not None and (not isinstance(label, str) or not label or label != label.strip() or len(label) > 80):
            raise ValueError("invalid protected HD account label")
        signer = item["last_signer"]
        if signer is not None and (
            not isinstance(signer, dict) or set(signer) != {"session_id", "at"}
            or (signer["session_id"] is not None and (not isinstance(signer["session_id"], str) or not signer["session_id"]))
            or not isinstance(signer["at"], str) or not signer["at"]
        ):
            raise ValueError("invalid protected HD account provenance")
        if not item["enabled"]:
            disabled = True
        elif disabled:
            raise ValueError("protected HD accounts must be enabled in order")
    if not accounts[0]["enabled"]:
        raise ValueError("protected HD account 0 must be enabled")
    return accounts


def selection(value: Any, *, intake: bool = False) -> dict[str, Any]:
    checked = vault_hd.validate_derivation(value)
    if checked["chain"] == "bitcoin":
        if intake and "address_type" not in checked:
            checked["address_type"] = "p2tr"
        if checked.get("address_type") != "p2tr":
            raise ValueError("protected HD Bitcoin supports only explicit p2tr")
    return checked


def validate_context(derivation: Any, context: Any, *, digest: str | None, message_hex: str | None) -> None:
    checked = selection(derivation)
    allowed = {"evm": {"evm-transaction", "eip-712-typed-data"},
               "bitcoin": {"bitcoin-taproot-keypath-v1"}, "solana": {"solana-transaction-v1"}}
    if not isinstance(context, dict) or not isinstance(context.get("kind"), str) or context["kind"] not in allowed[checked["chain"]]:
        raise ValueError("protected HD requires the verifiable signing context for its selected chain")
    if checked["chain"] == "solana":
        vault_solana.validate_context(context, message_hex=message_hex or "")
    elif checked["chain"] == "evm":
        fields = ({"kind", "digestAlgorithm", "chainId", "unsignedTransaction", "digest"}
                  if context["kind"] == "evm-transaction" else {"kind", "digestAlgorithm", "typedData", "digest"})
        if set(context) != fields or context.get("digest") != digest:
            raise ValueError("protected HD EVM context must bind the exact digest")
        if context["kind"] == "evm-transaction":
            if context["digestAlgorithm"] != "keccak256" or not isinstance(context["chainId"], str):
                raise ValueError("invalid protected HD EVM context")
        elif context["digestAlgorithm"] != "eip712":
            raise ValueError("invalid protected HD EIP-712 context")


def verify_signature(public_key: dict[str, Any], derivation: Any, *, digest: str | None,
                     message_hex: str | None, signature: Any) -> None:
    checked = selection(derivation)
    if (not isinstance(signature, dict) or set(signature) - {"signature", "recovery_id"}
            or not isinstance(signature.get("signature"), str)
            or re.fullmatch(r"[0-9a-f]{128}", signature["signature"]) is None):
        raise ValueError("protected HD requires an exact canonical public signature")
    if checked["chain"] == "solana":
        if signature.get("recovery_id") is not None:
            raise ValueError("Ed25519 signatures have no recovery id")
        vault_solana.verify_signature(vault_solana.require_public_key(public_key), message_hex or "", signature["signature"])
    else:
        arguments = {"public_key_bytes": bytes.fromhex(public_key["public_key"]),
                     "digest_bytes": bytes.fromhex(digest or ""), "signature": signature}
        if checked["chain"] == "bitcoin":
            vault_signatures.verify_taproot(**arguments)
        else:
            vault_signatures.verify_ecdsa(**arguments, scheme="ecdsa-secp256k1-recoverable")


def account_addresses(item: dict[str, Any]) -> dict[str, str]:
    eth, btc, solana = vault_hd._addresses(item["evm"], item["bitcoin"], item["solana"])
    legacy, segwit = vault_hd._bitcoin_addresses(item["bitcoin_p2pkh"], item["bitcoin_p2wpkh"])
    return {"eth": eth, "btc_taproot": btc, "solana": solana, "btc_legacy": legacy, "btc_segwit": segwit}


def public_accounts(name: str, sealed: Sealed, meta: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in stored_accounts(name, sealed, meta):
        if item["enabled"]:
            addresses = account_addresses(item)
            # Extra BTC keys are immutable precommitments, not enabled receive choices.
            result.append({"account": item["account"], "name": item["name"],
                           **{key: addresses[key] for key in ("eth", "btc_taproot", "solana")},
                           "paths": vault_hd.paths(item["account"]), "always_ask": True,
                           "last_signer": item["last_signer"], "bitcoin_address_types": ["p2tr"]})
    return result


def selected_account(name: str, sealed: Sealed, meta: dict[str, Any], derivation: Any) -> dict[str, Any]:
    checked = selection(derivation)
    item = stored_accounts(name, sealed, meta)[checked["account"]]
    if not item["enabled"]:
        raise ValueError("protected HD account is not enabled")
    return item


def signing_public_key(name: str, sealed: Sealed, meta: dict[str, Any], derivation: Any) -> dict[str, str]:
    checked = selection(derivation)
    item = selected_account(name, sealed, meta, checked)
    chain = checked["chain"]
    return {"curve": "ed25519", "chain": "solana", "public_key": item[chain]} if chain == "solana" else {
        "curve": "secp256k1", "public_key": item[chain],
    }

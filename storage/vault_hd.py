"""Public HD-wallet metadata and fixed chain selection; no seed/key derivation.

avault alone imports, decrypts and derives the seed. This module validates its
public output and formats addresses using the existing public-key formatters.
"""
from __future__ import annotations

import base64
import json
import re
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from storage import vault_solana
from storage.vault_addresses import derive_addresses
from storage.vault_crypto import Sealed

SIGNER_KIND = "hd-seed"
ACCOUNT_LIMIT = 20
WRAP_SCHEME = "machine-hd-seed-aesgcm-v1"
SCHEMES = {
    "evm": "ecdsa-secp256k1-recoverable",
    "bitcoin": "schnorr-secp256k1-bip341-keypath",
    "solana": "ed25519",
}


def derivation(chain: Any, account: Any = 0) -> dict[str, Any]:
    if not isinstance(chain, str) or chain not in SCHEMES:
        raise ValueError("HD signing requires chain evm, bitcoin or solana (--chain)")
    if type(account) is not int or not 0 <= account < ACCOUNT_LIMIT:
        raise ValueError("HD account must be an integer from 0 to 19 (--account)")
    return {"chain": chain, "account": account}


def validate_derivation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"chain", "account"}:
        raise ValueError("HD signing requires derivation with chain and account")
    return derivation(value["chain"], value["account"])


def paths(account: int) -> dict[str, str]:
    return {
        "evm": f"m/44'/60'/0'/0/{account}",
        "bitcoin": f"m/86'/0'/0'/0/{account}",
        "solana": f"m/44'/501'/{account}'/0'",
    }


def _public_key(value: Any, chain: str) -> str:
    size = 32 if chain == "solana" else 33
    if not isinstance(value, str) or len(value) != size * 2 or value != value.lower():
        raise ValueError(f"invalid HD {chain} public key")
    try:
        raw = bytes.fromhex(value)
        if len(raw) != size:
            raise ValueError("wrong key length")
        if chain == "solana":
            vault_solana.require_public_key({"curve": "ed25519", "chain": "solana", "public_key": value})
        else:
            if raw[0] not in {2, 3}:
                raise ValueError("not compressed")
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), raw)
    except (ValueError, vault_solana.SolanaSigningError) as exc:
        raise ValueError(f"invalid HD {chain} public key") from exc
    return value


def import_accounts(value: Any) -> list[dict[str, Any]]:
    """Accept only avault's complete, ordered set; caller metadata cannot enable accounts."""
    if not isinstance(value, list) or len(value) != ACCOUNT_LIMIT:
        raise ValueError("HD import requires exactly 20 accounts")
    result = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict) or set(item) != {"account", *SCHEMES}
            or type(item.get("account")) is not int or item["account"] != index
        ):
            raise ValueError("HD accounts must be numbered 0 through 19 in order")
        result.append({
            "account": index,
            **{chain: _public_key(item[chain], chain) for chain in SCHEMES},
            "enabled": index == 0,
            "name": None,
            "always_ask": False,
            "last_signer": None,
        })
    return result


def validate_expected_account0(value: Any) -> dict[str, str]:
    """A browser's public consistency check, never a source of stored key material."""
    patterns = {
        "eth": r"0x[0-9a-fA-F]{40}",
        "btc_taproot": r"bc1p[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}",
        "solana": r"[1-9A-HJ-NP-Za-km-z]{32,44}",
    }
    if not isinstance(value, dict) or set(value) != set(patterns):
        raise ValueError("expected_account0 must contain exactly eth, btc_taproot and solana")
    for field, pattern in patterns.items():
        address = value[field]
        if not isinstance(address, str) or len(address) > 62 or re.fullmatch(pattern, address) is None:
            raise ValueError(f"expected_account0.{field} must be a canonical public address")
    return dict(value)


def validate_envelope(sealed: Sealed) -> None:
    try:
        wrap = json.loads(sealed.wrap_meta)
        if (
            not isinstance(wrap, dict) or wrap.get("scheme") != WRAP_SCHEME
            or type(wrap.get("v")) is not int or wrap["v"] != 1 or "record_meta" in wrap
        ):
            raise ValueError("wrong HD envelope type")
        for value, size in (
            (sealed.ciphertext, 80), (sealed.nonce, 12),
            (wrap.get("wrapped_dek"), 48), (wrap.get("dek_nonce"), 12),
        ):
            if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) != size:
                raise ValueError("wrong HD envelope field size")
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid HD seed envelope") from exc


def stored_accounts(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Fail closed for malformed state; the list caller may degrade to an empty list."""
    accounts = meta.get("hd_accounts")
    if not isinstance(accounts, list) or len(accounts) != ACCOUNT_LIMIT:
        raise ValueError("invalid HD account metadata")
    disabled_seen = False
    for index, item in enumerate(accounts):
        if (
            not isinstance(item, dict) or type(item.get("account")) is not int or item["account"] != index
            or type(item.get("enabled")) is not bool or type(item.get("always_ask")) is not bool
        ):
            raise ValueError("invalid HD account metadata")
        if not item["enabled"]:
            disabled_seen = True
        elif disabled_seen:
            raise ValueError("HD accounts must be enabled in order")
    if not accounts[0]["enabled"]:
        raise ValueError("HD account 0 must be enabled")
    return accounts


def selected_account(meta: dict[str, Any], selection: Any) -> dict[str, Any]:
    selected = validate_derivation(selection)
    account = stored_accounts(meta)[selected["account"]]
    if not account["enabled"]:
        raise ValueError(f"HD account {selected['account']} is not enabled; ask the user to add it in Vault")
    return account


def signing_public_key(meta: dict[str, Any], selection: Any) -> dict[str, str]:
    account = selected_account(meta, selection)
    chain = selection["chain"]
    key = _public_key(account.get(chain), chain)
    return {"curve": "ed25519", "chain": "solana", "public_key": key} if chain == "solana" else {
        "curve": "secp256k1", "public_key": key,
    }


@lru_cache(maxsize=1024)
def _addresses(evm: str, bitcoin: str, solana: str) -> tuple[str, str, str]:
    # Public-only cache: account names, policy and audit provenance are never cached.
    return (
        derive_addresses(_public_key(evm, "evm"))["eth"],
        derive_addresses(_public_key(bitcoin, "bitcoin"))["btc_taproot"],
        vault_solana.address(_public_key(solana, "solana")),
    )


def public_accounts(meta: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in stored_accounts(meta):
        if not item["enabled"]:
            continue
        if not all(isinstance(item.get(chain), str) for chain in SCHEMES):
            raise ValueError("invalid HD public keys")
        eth, btc, solana = _addresses(item["evm"], item["bitcoin"], item["solana"])
        result.append({
            "account": item["account"], "name": item.get("name"),
            "eth": eth, "btc_taproot": btc, "solana": solana,
            "paths": paths(item["account"]), "always_ask": item["always_ask"],
            "last_signer": item.get("last_signer"),
        })
    return result

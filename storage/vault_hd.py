"""Public HD-wallet metadata and fixed selections; private derivation stays in avault."""
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
BITCOIN_PROFILE = "bitcoin-address-types-v1"
BITCOIN_TYPES = ("p2pkh", "p2wpkh", "p2tr")
BITCOIN_KEYS = {"p2pkh": "bitcoin_p2pkh", "p2wpkh": "bitcoin_p2wpkh", "p2tr": "bitcoin"}
SCHEMES = {
    "evm": "ecdsa-secp256k1-recoverable",
    "bitcoin": "schnorr-secp256k1-bip341-keypath",
    "solana": "ed25519",
}
_UNSET = object()


def derivation(chain: Any, account: Any = 0, address_type: Any = _UNSET) -> dict[str, Any]:
    if not isinstance(chain, str) or chain not in SCHEMES:
        raise ValueError("HD signing requires chain evm, bitcoin or solana (--chain)")
    if type(account) is not int or not 0 <= account < ACCOUNT_LIMIT:
        raise ValueError("HD account must be an integer from 0 to 19 (--account)")
    result = {"chain": chain, "account": account}
    if address_type is not _UNSET:
        if chain != "bitcoin" or not isinstance(address_type, str) or address_type not in BITCOIN_TYPES:
            raise ValueError("address_type requires chain bitcoin and p2pkh, p2wpkh or p2tr (--address-type)")
        result["address_type"] = address_type
    return result


def validate_derivation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) not in ({"chain", "account"}, {"chain", "account", "address_type"}):
        raise ValueError("HD signing requires derivation with chain, account and optional Bitcoin address_type")
    return derivation(value["chain"], value["account"], **(
        {"address_type": value["address_type"]} if "address_type" in value else {}
    ))


def signing_scheme(selection: Any) -> str:
    selection = validate_derivation(selection)
    if selection.get("address_type") in {"p2pkh", "p2wpkh"}:
        return "ecdsa-secp256k1-der"
    return SCHEMES[selection["chain"]]


def paths(account: int) -> dict[str, str]:
    return {
        "evm": f"m/44'/60'/0'/0/{account}",
        "bitcoin": f"m/86'/0'/0'/0/{account}",
        "solana": f"m/44'/501'/{account}'/0'",
    }


def bitcoin_paths(account: int) -> dict[str, str]:
    return {kind: f"m/{purpose}'/0'/0'/0/{account}" for kind, purpose in zip(BITCOIN_TYPES, (44, 84, 86))}


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


def public_fields(profile: Any = None) -> tuple[str, ...]:
    if profile is None:
        return tuple(SCHEMES)
    if profile != BITCOIN_PROFILE:
        raise ValueError("unsupported HD public profile")
    return (*SCHEMES, "bitcoin_p2pkh", "bitcoin_p2wpkh")


def import_accounts(value: Any, *, profile: Any = None) -> list[dict[str, Any]]:
    """Only avault's complete ordered table, never caller-supplied account settings."""
    fields = public_fields(profile)
    if not isinstance(value, list) or len(value) != ACCOUNT_LIMIT:
        raise ValueError("HD import requires exactly 20 accounts")
    result = []
    seen = {field: set() for field in fields}
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict) or set(item) != {"account", *fields}
            or type(item.get("account")) is not int or item["account"] != index
        ):
            raise ValueError("HD accounts must be numbered 0 through 19 in order with exact public fields")
        keys = {field: _public_key(item[field], field) for field in fields}
        for field, key in keys.items():
            # Profiled Bitcoin purposes must be distinct across the entire table,
            # not just within one purpose column. Legacy imports keep their shape.
            group = "bitcoin" if field in BITCOIN_KEYS.values() else field
            if profile is not None and key in seen[group]:
                raise ValueError("duplicate HD public key")
            seen[group].add(key)
        result.append({
            "account": index, **keys, "enabled": index == 0, "name": None,
            "always_ask": False, "last_signer": None,
        })
    return result


def validate_expected_account0(value: Any, *, profile: Any = None) -> dict[str, str]:
    """Browser expectations reject disagreements; they never supply key material."""
    public_fields(profile)
    patterns = {
        "eth": r"0x[0-9a-fA-F]{40}",
        "btc_taproot": r"bc1p[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}",
        "solana": r"[1-9A-HJ-NP-Za-km-z]{32,44}",
    }
    if profile is not None:
        patterns.update({"btc_legacy": r"1[1-9A-HJ-NP-Za-km-z]{25,34}",
                         "btc_segwit": r"bc1q[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{38}"})
    if not isinstance(value, dict) or set(value) != set(patterns):
        raise ValueError("expected_account0 must contain exactly " + ", ".join(patterns))
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
    """Validate persisted table/profile together, without changing mutable account state."""
    accounts = meta.get("hd_accounts")
    if not isinstance(accounts, list) or len(accounts) != ACCOUNT_LIMIT:
        raise ValueError("invalid HD account metadata")
    profile = meta.get("bitcoin_profile")
    fields = public_fields(profile)
    # Reuse the same strict public-table validator on ALL rows, including disabled ones.
    import_accounts([{key: item.get(key) for key in ("account", *fields)} if isinstance(item, dict) else item
                     for item in accounts], profile=profile)
    disabled_seen = False
    for index, item in enumerate(accounts):
        if (
            type(item.get("enabled")) is not bool or type(item.get("always_ask")) is not bool
            or (profile is None and any(key in item for key in ("bitcoin_p2pkh", "bitcoin_p2wpkh")))
        ):
            raise ValueError("invalid HD account metadata")
        if not item["enabled"]:
            disabled_seen = True
        elif disabled_seen:
            raise ValueError("HD accounts must be enabled in order")
    if not accounts[0]["enabled"]:
        raise ValueError("HD account 0 must be enabled")
    return accounts


def base_identities(meta: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"account": item["account"], **{chain: item[chain] for chain in SCHEMES}}
            for item in stored_accounts(meta)]


def selected_account(meta: dict[str, Any], selection: Any) -> dict[str, Any]:
    selected = validate_derivation(selection)
    account = stored_accounts(meta)[selected["account"]]
    if not account["enabled"]:
        raise ValueError(f"HD account {selected['account']} is not enabled; ask the user to add it in Vault")
    if selected.get("address_type") in {"p2pkh", "p2wpkh"} and meta.get("bitcoin_profile") != BITCOIN_PROFILE:
        raise ValueError("HD Bitcoin address types are not prepared; ask the user to open this wallet's Accounts in Vault")
    return account


def signing_public_key(meta: dict[str, Any], selection: Any) -> dict[str, str]:
    selection = validate_derivation(selection)
    account = selected_account(meta, selection)
    chain = selection["chain"]
    field = BITCOIN_KEYS[selection.get("address_type", "p2tr")] if chain == "bitcoin" else chain
    key = _public_key(account.get(field), chain)
    return {"curve": "ed25519", "chain": "solana", "public_key": key} if chain == "solana" else {
        "curve": "secp256k1", "public_key": key,
    }


@lru_cache(maxsize=1024)
def _addresses(evm: str, bitcoin: str, solana: str) -> tuple[str, str, str]:
    return (
        derive_addresses(_public_key(evm, "evm"))["eth"],
        derive_addresses(_public_key(bitcoin, "bitcoin"))["btc_taproot"],
        vault_solana.address(_public_key(solana, "solana")),
    )


@lru_cache(maxsize=1024)
def _bitcoin_addresses(legacy: str, segwit: str) -> tuple[str, str]:
    return (derive_addresses(_public_key(legacy, "bitcoin"))["btc_legacy"],
            derive_addresses(_public_key(segwit, "bitcoin"))["btc_segwit"])


def public_accounts(meta: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    extended = meta.get("bitcoin_profile") == BITCOIN_PROFILE
    for item in stored_accounts(meta):
        if not item["enabled"]:
            continue
        eth, btc, solana = _addresses(item["evm"], item["bitcoin"], item["solana"])
        public = {
            "account": item["account"], "name": item.get("name"),
            "eth": eth, "btc_taproot": btc, "solana": solana,
            "paths": paths(item["account"]), "always_ask": item["always_ask"],
            "last_signer": item.get("last_signer"),
            # Runtime capability is added OUTSIDE storage serialization by API/CLI.
            "bitcoin_address_types": ["p2tr"],
        }
        if extended:
            legacy, segwit = _bitcoin_addresses(item["bitcoin_p2pkh"], item["bitcoin_p2wpkh"])
            public.update({"btc_legacy": legacy, "btc_segwit": segwit, "bitcoin_paths": bitcoin_paths(item["account"])})
        result.append(public)
    return result

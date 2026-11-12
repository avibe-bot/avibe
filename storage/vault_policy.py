"""Public-only wire and identity contract for a signing-policy downgrade.

The storage service owns authorization and atomic mutation. This module only
validates/encodes public context and derives public identity; it never opens an
engine, calls custody, enrolls a factor, or treats client addresses as key data.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from storage import vault_addresses, vault_crypto, vault_hd, vault_solana

PURPOSE = "vault-signing-policy-downgrade"
OPERATION = "vault_signing_policy_downgrade_v1"
DOMAIN = b"avibe:vault:signing-policy-downgrade:v1\n"
REVISION_KEY = "_signing_policy_revision"
MAX_CONTEXT_BYTES = 65_536
TTL_SECONDS = 120
ADDRESS_FIELDS = frozenset({"eth", "btc_legacy", "btc_segwit", "btc_taproot", "solana"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_EXPIRES = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", re.ASCII)


class PolicyContextError(ValueError):
    """Malformed, unsupported or inconsistent public policy context."""


def _object(value: Any, keys: set[str] | frozenset[str], field: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise PolicyContextError(f"{field} has invalid fields")
    return value


def _string(value: Any, field: str, *, maximum: int = MAX_CONTEXT_BYTES) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise PolicyContextError(f"{field} must be a nonempty bounded string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PolicyContextError(f"{field} contains an unpaired surrogate") from exc
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise PolicyContextError(f"{field} is not a valid identifier")
    return value


def credential_bytes(value: Any, *, nonce: bool = False) -> bytes:
    """Strict context base64; assertion compatibility decoding stays separate."""
    if not isinstance(value, str) or not 1 <= len(value) <= (44 if nonce else 1364):
        raise PolicyContextError("invalid context credential/nonce encoding")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PolicyContextError("invalid context base64") from exc
    if base64.b64encode(raw).decode("ascii") != value or not (len(raw) == 32 if nonce else 1 <= len(raw) <= 1023):
        raise PolicyContextError("context base64 is noncanonical or out of bounds")
    return raw


def expiry_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or _EXPIRES.fullmatch(value) is None:
        raise PolicyContextError("expiresAt must be millisecond UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PolicyContextError("expiresAt is not a valid date") from exc
    if result.isoformat(timespec="milliseconds").replace("+00:00", "Z") != value:
        raise PolicyContextError("expiresAt is not canonical UTC")
    return result


def canonical_json(value: Any) -> str:
    """Match the sandbox's lossless, UTF-16-key-sorted JSON convention."""
    def ordered(item: Any, depth: int = 0) -> Any:
        if depth > 12:
            raise PolicyContextError("canonical data is too deeply nested")
        if item is None or type(item) is bool:
            return item
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise PolicyContextError("canonical string contains an unpaired surrogate") from exc
            return item
        if type(item) is int and abs(item) <= 2**53 - 1:
            return item
        if isinstance(item, list):
            return [ordered(child, depth + 1) for child in item]
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for key in item:
                ordered(key, depth + 1)
            return {key: ordered(item[key], depth + 1) for key in sorted(item, key=lambda key: key.encode("utf-16-be"))}
        raise PolicyContextError("canonical data must contain lossless JSON values only")
    return json.dumps(ordered(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def validate_context(value: Any) -> dict:
    """Validate v1.1 without accepting an alternate representation of bound facts."""
    context = _object(value, {"v", "purpose", "challengeId", "nonce", "expiresAt", "rpId", "origin", "target", "change", "factors"}, "context")
    if type(context["v"]) is not int or context["v"] != 1 or context["purpose"] != PURPOSE:
        raise PolicyContextError("unsupported policy context version/purpose")
    _id(context["challengeId"], "challengeId")
    credential_bytes(context["nonce"], nonce=True)
    expiry_datetime(context["expiresAt"])
    _string(context["rpId"], "rpId", maximum=253)
    _string(context["origin"], "origin", maximum=2048)
    target = _object(context["target"], {"secretId", "name", "protection", "signerKind", "account", "accountName", "addresses", "identitySha256"}, "target")
    _id(target["secretId"], "secretId")
    if not vault_crypto.is_valid_secret_name(_string(target["name"], "name")):
        raise PolicyContextError("invalid target name")
    if target["protection"] not in ("standard", "protected") or target["signerKind"] not in ("local", "hd-seed"):
        raise PolicyContextError("unsupported target kind/tier")
    if target["signerKind"] == "hd-seed":
        if target["protection"] != "standard" or type(target["account"]) is not int or not 0 <= target["account"] < 20:
            raise PolicyContextError("invalid HD policy account")
        if target["accountName"] is not None:
            _string(target["accountName"], "accountName", maximum=80)
    elif target["account"] is not None or target["accountName"] is not None:
        raise PolicyContextError("ordinary policy has no account/name")
    identity = target["identitySha256"]
    if not isinstance(identity, str) or _HEX64.fullmatch(identity) is None:
        raise PolicyContextError("invalid target identity digest")
    addresses = _object(target["addresses"], ADDRESS_FIELDS, "addresses")
    nonnull = {field for field, address in addresses.items() if address is not None}
    expected = {"eth", "btc_taproot", "solana"} if target["signerKind"] == "hd-seed" else (
        {"solana"} if addresses["solana"] is not None else {"eth", "btc_legacy", "btc_segwit", "btc_taproot"}
    )
    if nonnull != expected:
        raise PolicyContextError("address fields do not match the target variant")
    for field in nonnull:
        try:
            vault_addresses.validate_public_address(field, addresses[field])
        except ValueError as exc:
            raise PolicyContextError(str(exc)) from exc
    change = _object(context["change"], {"field", "from", "to", "revision"}, "change")
    if change["field"] != "always_ask" or change["from"] is not True or change["to"] is not False:
        raise PolicyContextError("policy authorization supports only true-to-false always_ask")
    revision = change["revision"]
    if not isinstance(revision, str) or revision != "legacy" and _HEX32.fullmatch(revision) is None:
        raise PolicyContextError("invalid policy revision")
    factors = context["factors"]
    # Every legal descriptor encodes to more than16 bytes; this early bound
    # cannot reject a context satisfying the final canonical-byte budget.
    if not isinstance(factors, list) or not 1 <= len(factors) <= MAX_CONTEXT_BYTES // 16:
        raise PolicyContextError("invalid eligible factor list")
    ids, credentials = [], set()
    for factor in factors:
        _object(factor, {"factorId", "credentialId"}, "factor")
        ids.append(_id(factor["factorId"], "factorId"))
        credential = credential_bytes(factor["credentialId"])
        if credential in credentials:
            raise PolicyContextError("duplicate eligible credential")
        credentials.add(credential)
    if ids != sorted(set(ids)):
        raise PolicyContextError("eligible factor IDs must be unique and sorted")
    if len(canonical_json(context).encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise PolicyContextError("policy context exceeds the64KiB limit")
    return context


def challenge_bytes(context: Any) -> bytes:
    return hashlib.sha256(DOMAIN + canonical_json(validate_context(context)).encode("utf-8")).digest()


def identity_hash(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def stored_revision(metadata: dict) -> str:
    if REVISION_KEY not in metadata:
        return "legacy"
    value = metadata[REVISION_KEY]
    if not isinstance(value, str) or _HEX32.fullmatch(value) is None:
        raise PolicyContextError("invalid stored policy revision")
    return value


def target_state(row: dict, account: int | None) -> tuple[dict, bool, str]:
    """Re-derive target and identity from storage, never client key/address fields."""
    try:
        meta = json.loads(row.get("public_meta") or "{}")
        policy = json.loads(row.get("policy") or "{}")
    except (ValueError, TypeError) as exc:
        raise PolicyContextError("invalid stored policy metadata") from exc
    if not isinstance(meta, dict) or not isinstance(policy, dict) or row.get("kind") != "keypair":
        raise PolicyContextError("policy target must be a keypair")
    signer = "local" if row.get("signer_kind") is None else row["signer_kind"]
    addresses = dict.fromkeys(sorted(ADDRESS_FIELDS))
    name = None
    if signer == vault_hd.SIGNER_KIND:
        if row.get("protection") != "standard" or bool(policy.get("always_ask")):
            raise PolicyContextError("unsupported HD tier or row-wide confirmation override")
        try:
            selected = vault_hd.selected_account(meta, vault_hd.derivation("evm", account))
            keys = {}
            for chain in vault_hd.SCHEMES:
                key = vault_hd.signing_public_key(meta, vault_hd.derivation(chain, account))
                keys[chain] = {"curve": key["curve"], "publicKey": key["public_key"], **({"chain": "solana"} if chain == "solana" else {})}
            addresses.update(eth=vault_addresses.derive_addresses(keys["evm"]["publicKey"])["eth"],
                             btc_taproot=vault_addresses.derive_addresses(keys["bitcoin"]["publicKey"])["btc_taproot"],
                             solana=vault_solana.address(keys["solana"]["publicKey"]))
        except ValueError as exc:
            raise PolicyContextError(str(exc)) from exc
        name, current, revision = selected.get("name"), selected["always_ask"], stored_revision(selected)
    elif signer == "local" and account is None and row.get("protection") in ("standard", "protected"):
        key = meta.get("signing_public_key")
        if not isinstance(key, dict):
            raise PolicyContextError("policy requires a pinned public signing key")
        try:
            if vault_solana.is_ed25519_key(key):
                public = vault_solana.require_public_key(key)
                keys = {"signing": {"curve": "ed25519", "chain": "solana", "publicKey": public}}
                addresses["solana"] = vault_solana.address(public)
            elif key.get("curve") == "secp256k1" and isinstance(key.get("public_key"), str) and len(key["public_key"]) == 66:
                public = key["public_key"].lower()
                addresses.update(vault_addresses.derive_addresses(public))
                keys = {"signing": {"curve": "secp256k1", "publicKey": public}}
            else:
                raise ValueError("policy requires a valid compressed public signing key")
        except ValueError as exc:
            raise PolicyContextError(str(exc)) from exc
        current, revision = bool(policy.get("always_ask")), stored_revision(policy)
    else:
        raise PolicyContextError("unsupported policy target/account")
    envelope = {"ciphertext": row.get("ciphertext"), "nonce": row.get("nonce"), "wrapMeta": row.get("wrap_meta"), "revision": row.get("updated_at")}
    if not all(isinstance(value, str) and value for value in envelope.values()):
        raise PolicyContextError("policy requires a complete stored envelope identity")
    snapshot = {"v": 1, "kind": "keypair", "protection": row["protection"], "signerKind": signer, "envelope": envelope, "keys": keys}
    target = {"secretId": row["id"], "name": row["name"], "protection": row["protection"], "signerKind": signer,
              "account": account, "accountName": name, "addresses": addresses, "identitySha256": identity_hash(snapshot)}
    return target, current, revision

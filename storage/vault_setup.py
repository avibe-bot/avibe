"""Opaque shared-root projection; no VMK, PRF output or private key handling."""
from __future__ import annotations

import json
from typing import Any

from storage import vault_policy, vault_webauthn

META_KEY = "vault.setup.v1"
RECORD_FIELDS = {"scheme", "record_meta", "wrapped_dek", "dek_nonce"}


class SetupMetadataError(ValueError):
    pass


def base_wrap_meta(value: Any) -> dict[str, Any]:
    """Remove only record-specific fields; preserve base/AAD metadata exactly.

    Comparison uses parsed JSON, not whitespace. Values are never normalized or
    rewritten inside a record. Unknown base fields survive for AAD compatibility.
    """
    try:
        meta = json.loads(value) if isinstance(value, str) else value
        if not isinstance(meta, dict) or type(meta.get("v")) is not int or meta["v"] != 1:
            raise SetupMetadataError("invalid shared Vault root version")
        copies = meta.get("copies")
        if not isinstance(copies, list) or not copies:
            raise SetupMetadataError("shared Vault root requires a Passkey copy")
        for item in copies:
            if (not isinstance(item, dict) or item.get("kind") != "passkey"
                    or item.get("kdf") != "webauthn-prf-hkdf-sha256"):
                raise SetupMetadataError("unsupported shared Vault root copy")
            for key, size in (("prf_salt", 32), ("nonce", 12), ("wrapped", 48)):
                if not isinstance(item.get(key), str) or len(vault_webauthn.b64decode(item[key])) != size:
                    raise SetupMetadataError("invalid shared Vault root copy encoding")
            if "credential_id" in item:
                vault_policy.credential_bytes(item["credential_id"])
        base = {key: child for key, child in meta.items() if key not in RECORD_FIELDS}
        if len(vault_policy.canonical_json(base).encode()) > vault_policy.MAX_CONTEXT_BYTES:
            raise SetupMetadataError("shared Vault root metadata is too large")
        return base
    except (ValueError, TypeError, vault_webauthn.WebAuthnVerificationError) as exc:
        raise SetupMetadataError(str(exc)) from exc


def credential_ids(base: dict[str, Any]) -> set[bytes]:
    return {vault_policy.credential_bytes(item["credential_id"]) for item in base["copies"] if "credential_id" in item}


def registration_digest(value: dict[str, Any]) -> str:
    return vault_policy.identity_hash(value)

"""Envelope encryption for Vaults secrets (design: docs/plans/vaults.md §7).

P0 implements the **standard tier**: each secret value is sealed under a random
per-secret data encryption key (DEK), and the DEK is wrapped under a machine key held
on the box. The machine key lives at ``<state>/vault/machine.key`` (mode 0600); since
it sits inside ``~/.avibe`` it travels with backups/migration of the state dir, so
there is no new loss mode beyond losing the database itself.

This module is intentionally narrow — it only knows how to seal/open bytes. Name
validation, storage, and policy live in the service layer. The protected tier
(VMK + password/passkey, browser-side decryption) and scope grants are P1 and are
deliberately not implemented here yet.

Wire format (``wrap_meta`` JSON):
    {"v": 1, "scheme": "machine-aesgcm-v1", "wrapped_dek": <b64>, "dek_nonce": <b64>}
``ciphertext`` and ``nonce`` are base64 text (the DB stores text, not blobs).
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import paths

WRAP_SCHEME = "machine-aesgcm-v1"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # AES-GCM standard nonce
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class VaultCryptoError(Exception):
    """Raised on a corrupt envelope or a missing/mismatched machine key."""


@dataclass(frozen=True)
class Sealed:
    """An envelope-encrypted value, ready to persist as ``vault_secrets`` columns."""

    ciphertext: str  # base64
    nonce: str  # base64
    wrap_meta: str  # JSON


def is_valid_secret_name(name: str | None) -> bool:
    """ENV-style names only: an uppercase letter then uppercase/digit/underscore."""
    return bool(name and _NAME_RE.match(name))


def machine_key_path() -> Path:
    return paths.get_state_dir() / "vault" / "machine.key"


def get_or_create_machine_key(key_path: Path | None = None) -> bytes:
    """Return the 32-byte machine key, generating it (0600) on first use.

    Generation is atomic (``O_CREAT | O_EXCL``) so two racing callers can't clobber
    each other's key.
    """
    path = key_path or machine_key_path()
    if path.exists():
        key = path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise VaultCryptoError(f"machine key at {path} is {len(key)} bytes, expected {_KEY_BYTES}")
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    key = os.urandom(_KEY_BYTES)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost a race; read the winner's key.
        existing = path.read_bytes()
        if len(existing) != _KEY_BYTES:
            raise VaultCryptoError(f"machine key at {path} is {len(existing)} bytes, expected {_KEY_BYTES}")
        return existing
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def seal_standard(value: bytes, *, machine_key: bytes | None = None, key_path: Path | None = None) -> Sealed:
    """Seal ``value`` under a fresh DEK, wrapping the DEK with the machine key."""
    key = machine_key if machine_key is not None else get_or_create_machine_key(key_path)
    dek = os.urandom(_KEY_BYTES)
    value_nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(value_nonce, value, None)

    dek_nonce = os.urandom(_NONCE_BYTES)
    wrapped_dek = AESGCM(key).encrypt(dek_nonce, dek, None)
    wrap_meta = {
        "v": 1,
        "scheme": WRAP_SCHEME,
        "wrapped_dek": _b64(wrapped_dek),
        "dek_nonce": _b64(dek_nonce),
    }
    return Sealed(ciphertext=_b64(ciphertext), nonce=_b64(value_nonce), wrap_meta=json.dumps(wrap_meta))


def open_standard(sealed: Sealed, *, machine_key: bytes | None = None, key_path: Path | None = None) -> bytes:
    """Reverse :func:`seal_standard`. Raises :class:`VaultCryptoError` on any failure.

    AES-GCM authentication means a wrong machine key (or tampered ciphertext) fails
    loudly here rather than returning garbage.
    """
    key = machine_key if machine_key is not None else get_or_create_machine_key(key_path)
    try:
        meta = json.loads(sealed.wrap_meta)
    except (TypeError, ValueError) as exc:
        raise VaultCryptoError("wrap_meta is not valid JSON") from exc
    if meta.get("scheme") != WRAP_SCHEME:
        raise VaultCryptoError(f"unsupported wrap scheme: {meta.get('scheme')!r}")
    try:
        dek = AESGCM(key).decrypt(_unb64(meta["dek_nonce"]), _unb64(meta["wrapped_dek"]), None)
        return AESGCM(dek).decrypt(_unb64(sealed.nonce), _unb64(sealed.ciphertext), None)
    except (InvalidTag, KeyError, ValueError, TypeError) as exc:
        raise VaultCryptoError("decryption failed (wrong/missing machine key or corrupt data)") from exc

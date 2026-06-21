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
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from config import paths

WRAP_SCHEME = "machine-aesgcm-v1"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # AES-GCM standard nonce
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Scrypt parameters for passphrase-wrapped machine-key export (§7.2). Scrypt is the
# zero-new-dependency KDF (ships in `cryptography`); the export blob records the KDF +
# params so a future Argon2id variant is just another ``kdf`` value (forward-compatible).
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
EXPORT_SCHEME = "machine-key-export-v1"


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


def get_machine_key(key_path: Path | None = None) -> bytes:
    """Return the existing machine key, or raise — never create one.

    Decryption must use this (not :func:`get_or_create_machine_key`): if the key is
    missing (state restore / partial backup / file removed), creating a fresh wrong key
    would mask the real "key missing" problem and make a later ``vibe vault key import``
    refuse without ``--force``.
    """
    path = key_path or machine_key_path()
    if not path.exists():
        raise VaultCryptoError(f"machine key not found at {path} (restore it with: vibe vault key import <backup>)")
    key = path.read_bytes()
    if len(key) != _KEY_BYTES:
        raise VaultCryptoError(f"machine key at {path} is {len(key)} bytes, expected {_KEY_BYTES}")
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
    key = machine_key if machine_key is not None else get_machine_key(key_path)
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


def _derive_kek_scrypt(passphrase: bytes, salt: bytes, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> bytes:
    return Scrypt(salt=salt, length=_KEY_BYTES, n=n, r=r, p=p).derive(passphrase)


def _validate_scrypt_params(n: int, r: int, p: int) -> None:
    """Bound file-controlled KDF params so a corrupt/hostile export can't OOM or hang the
    import before authentication ever fails. N must be a power of two ≤ 2^17 (~256 MB at
    r=8); r and p stay small."""
    if not (isinstance(n, int) and n >= 2 and (n & (n - 1)) == 0 and n <= 2**17):
        raise VaultCryptoError(f"scrypt N out of bounds: {n!r}")
    if not (isinstance(r, int) and 1 <= r <= 16):
        raise VaultCryptoError(f"scrypt r out of bounds: {r!r}")
    if not (isinstance(p, int) and 1 <= p <= 16):
        raise VaultCryptoError(f"scrypt p out of bounds: {p!r}")


def export_machine_key(passphrase: str, *, key_path: Path | None = None) -> dict:
    """Export the machine key as a passphrase-wrapped blob (§7.2).

    Lets the user back up / migrate the vault's machine key independently of the state
    dir (needed once the key moves to the OS keychain). The blob is safe to store: the
    key is wrapped under a Scrypt-derived KEK + AES-256-GCM.
    """
    if not passphrase:
        raise VaultCryptoError("a non-empty passphrase is required")
    # Export must back up an existing key, never mint one: minting here would write a fresh
    # random key to disk as a side effect of "export", silently orphaning any secrets that
    # were sealed under a key the user expected to still be present.
    key = get_machine_key(key_path)
    salt = os.urandom(16)
    nonce = os.urandom(_NONCE_BYTES)
    kek = _derive_kek_scrypt(passphrase.encode("utf-8"), salt)
    ciphertext = AESGCM(kek).encrypt(nonce, key, None)
    return {
        "scheme": EXPORT_SCHEME,
        "kdf": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }


def import_machine_key(blob: dict, passphrase: str, *, key_path: Path | None = None, force: bool = False) -> None:
    """Restore a machine key from :func:`export_machine_key` output.

    Refuses to overwrite an existing key unless ``force`` (overwriting would orphan every
    secret encrypted under the current key).
    """
    path = key_path or machine_key_path()
    if path.exists() and not force:
        raise VaultCryptoError(f"machine key already exists at {path}; pass force=True to overwrite")
    if not isinstance(blob, dict) or blob.get("scheme") != EXPORT_SCHEME or blob.get("kdf") != "scrypt":
        raise VaultCryptoError("unrecognized machine-key export blob")
    try:
        n, r, p = int(blob["n"]), int(blob["r"]), int(blob["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultCryptoError("invalid scrypt parameters in export blob") from exc
    _validate_scrypt_params(n, r, p)  # bound before deriving — a hostile blob can't OOM/hang us
    try:
        kek = _derive_kek_scrypt(passphrase.encode("utf-8"), _unb64(blob["salt"]), n=n, r=r, p=p)
        key = AESGCM(kek).decrypt(_unb64(blob["nonce"]), _unb64(blob["ciphertext"]), None)
    except (InvalidTag, KeyError, ValueError, TypeError) as exc:
        raise VaultCryptoError("import failed (wrong passphrase or corrupt export)") from exc
    if len(key) != _KEY_BYTES:
        raise VaultCryptoError(f"imported key is {len(key)} bytes, expected {_KEY_BYTES}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # Write to a 0600 temp file and atomically swap it in. With ``force`` overwriting the live
    # key, a crash / disk-full / failed write must never truncate it — a truncated machine key
    # would orphan every secret sealed under the old key. ``mkstemp`` creates the temp 0600 from
    # the start; ``os.replace`` is atomic on POSIX.
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

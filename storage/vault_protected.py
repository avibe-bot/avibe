"""Protected-tier envelope — canonical wire-format reference (design: vaults.md §7.1).

A **Vault Master Key (VMK)** is the protected-tier root. The VMK is wrapped
*independently* by one or more factors (password now; WebAuthn-PRF passkey copies are
added browser-side later, with the same copy shape) so any single factor can unwrap it
and losing one factor — while another remains — doesn't lose the vault. Each protected
secret's data key (DEK) is wrapped by the VMK.

    VMK  --wrapped by--> KEK_password = KDF(password, salt)     (0..N copies)
    secret: value --AES-256-GCM(DEK)--> ciphertext;  DEK --AES-256-GCM(VMK)--> wrapped

**IMPORTANT — production decryption is BROWSER-SIDE (§8.4):** the vault password never
reaches the daemon. This module is the *canonical format definition + test vectors* the
browser implementation mirrors. Its ``unwrap``/``open`` path exists for tests and
offline tooling and must NOT be wired into a daemon-side resolve. The daemon only ever
stores the opaque ``wrap_meta`` + ciphertext the browser produces.

KDF note: this reference uses Scrypt (zero-dep, ships in ``cryptography``); each wrap
copy records its ``kdf`` + params, so an Argon2id variant is just another ``kdf`` value
(the protected-tier KDF is still open Q7). The ``kind`` field distinguishes
``password`` copies from future ``passkey`` copies.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

WRAP_META_VERSION = 1
_KEY_BYTES = 32
_NONCE_BYTES = 12
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1


class ProtectedFormatError(Exception):
    pass


@dataclass(frozen=True)
class ProtectedSealed:
    """A protected secret's ciphertext (DEK wrapped by the VMK). Base64 text fields."""

    ciphertext: str
    nonce: str
    dek_nonce: str
    wrapped_dek: str


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def new_vmk() -> bytes:
    return os.urandom(_KEY_BYTES)


def _kek_password(password: str, salt: bytes, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> bytes:
    return Scrypt(salt=salt, length=_KEY_BYTES, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _validate_scrypt_params(n: int, r: int, p: int) -> None:
    """Bound copy-controlled KDF params so a hostile/corrupt wrap_meta can't OOM or hang
    the unwrap before authentication fails. N is a power of two ≤ 2^17 (~256 MB at r=8)."""
    if not (isinstance(n, int) and n >= 2 and (n & (n - 1)) == 0 and n <= 2**17):
        raise ProtectedFormatError(f"scrypt N out of bounds: {n!r}")
    if not (isinstance(r, int) and 1 <= r <= 16):
        raise ProtectedFormatError(f"scrypt r out of bounds: {r!r}")
    if not (isinstance(p, int) and 1 <= p <= 16):
        raise ProtectedFormatError(f"scrypt p out of bounds: {p!r}")


def _password_copy(vmk: bytes, password: str) -> dict:
    salt = os.urandom(16)
    nonce = os.urandom(_NONCE_BYTES)
    kek = _kek_password(password, salt)
    wrapped = AESGCM(kek).encrypt(nonce, vmk, None)
    return {
        "kind": "password",
        "kdf": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "wrapped": _b64(wrapped),
    }


def build_wrap_meta(vmk: bytes, passwords: list[str]) -> str:
    """Build a ``wrap_meta`` JSON wrapping ``vmk`` under each password (one copy each)."""
    if not passwords:
        raise ProtectedFormatError("at least one factor (password) is required")
    copies = [_password_copy(vmk, pw) for pw in passwords]
    return json.dumps({"v": WRAP_META_VERSION, "copies": copies})


def add_password_copy(wrap_meta: str, vmk: bytes, password: str) -> str:
    """Append a new password wrap copy of the same VMK (e.g. a second device / rotation)."""
    meta = json.loads(wrap_meta)
    meta.setdefault("copies", []).append(_password_copy(vmk, password))
    return json.dumps(meta)


def unwrap_vmk(wrap_meta: str, password: str) -> bytes:
    """Recover the VMK by trying the password against each ``password`` copy.

    Reference/test path only — production unwrap is browser-side (§8.4).
    """
    try:
        meta = json.loads(wrap_meta)
    except (TypeError, ValueError) as exc:
        raise ProtectedFormatError("wrap_meta is not valid JSON") from exc
    for copy in meta.get("copies", []):
        if copy.get("kind") != "password" or copy.get("kdf") != "scrypt":
            continue
        try:
            n, r, p = int(copy["n"]), int(copy["r"]), int(copy["p"])
            _validate_scrypt_params(n, r, p)  # bound before deriving; skip a copy with hostile params
            kek = _kek_password(password, _unb64(copy["salt"]), n=n, r=r, p=p)
            return AESGCM(kek).decrypt(_unb64(copy["nonce"]), _unb64(copy["wrapped"]), None)
        except (InvalidTag, KeyError, ValueError, TypeError, ProtectedFormatError):
            continue
    raise ProtectedFormatError("no password copy could be unwrapped (wrong password?)")


def seal_protected(value: bytes, vmk: bytes) -> ProtectedSealed:
    """Seal a secret value: fresh DEK encrypts the value, the VMK wraps the DEK."""
    dek = os.urandom(_KEY_BYTES)
    value_nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(value_nonce, value, None)
    dek_nonce = os.urandom(_NONCE_BYTES)
    wrapped_dek = AESGCM(vmk).encrypt(dek_nonce, dek, None)
    return ProtectedSealed(
        ciphertext=_b64(ciphertext),
        nonce=_b64(value_nonce),
        dek_nonce=_b64(dek_nonce),
        wrapped_dek=_b64(wrapped_dek),
    )


def open_protected(sealed: ProtectedSealed, vmk: bytes) -> bytes:
    """Reverse :func:`seal_protected` (reference/test path; production is browser-side)."""
    try:
        dek = AESGCM(vmk).decrypt(_unb64(sealed.dek_nonce), _unb64(sealed.wrapped_dek), None)
        return AESGCM(dek).decrypt(_unb64(sealed.nonce), _unb64(sealed.ciphertext), None)
    except (InvalidTag, ValueError, TypeError) as exc:
        raise ProtectedFormatError("protected decryption failed (wrong VMK or corrupt data)") from exc

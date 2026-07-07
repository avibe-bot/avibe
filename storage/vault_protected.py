"""Protected-tier envelope reference.

A **Vault Master Key (VMK)** is the protected-tier root. The VMK is wrapped
by one or more WebAuthn-PRF passkey copies in browser-owned ``wrap_meta``. There
is no password factor and no daemon-side VMK unwrap path. Each protected secret's
data key (DEK) is wrapped by the VMK.

    VMK  --wrapped by--> KEK_passkey = HKDF(WebAuthn PRF output, salt)  (1..N copies)
    secret: value --AES-256-GCM(DEK)--> ciphertext;  DEK --AES-256-GCM(VMK)--> wrapped

**IMPORTANT — production decryption is BROWSER-SIDE:** passkey PRF output, the
VMK, and plaintext never reach the daemon. This module keeps only the DEK/VMK
envelope reference used by tests and offline tooling. The daemon stores the
opaque ``wrap_meta`` + ciphertext the browser produces.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_BYTES = 32
_NONCE_BYTES = 12


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

"""Solana / Ed25519 signing contract shared by the Vault service, API and CLI.

An Ed25519 key signs the exact serialized message bytes, never a digest. This
module owns the public, value-free side of that contract: the message encoding,
the pinned public key, the typed ``solana-transaction-v1`` signing context, and
verification of a returned signature. It never parses a Solana transaction;
Python treats the message as opaque bytes, and decoding belongs to the sandbox.
Key material and signing stay in avault.
"""

from __future__ import annotations

import re
from typing import Any

from storage.vault_addresses import base58_encode

CURVE = "ed25519"
CHAIN = "solana"
SCHEME = "ed25519"
CONTEXT_KIND = "solana-transaction-v1"
# avault's bound for one exact message (``MAX_ED25519_MESSAGE_BYTES``).
MAX_MESSAGE_BYTES = 1024 * 1024
IMPORT_FORMATS = frozenset({"solana-cli-json", "base58-secret"})
_PUBLIC_KEY_RE = re.compile(r"[0-9a-f]{64}")
_SIGNATURE_RE = re.compile(r"[0-9a-fA-F]{128}")
_HEX_RE = re.compile(r"[0-9a-fA-F]*")
_LOWER_HEX_RE = re.compile(r"[0-9a-f]+")


class SolanaSigningError(ValueError):
    """An Ed25519 signing input, key or context breaks the contract."""


def normalize_message_hex(value: Any) -> str:
    """Return the exact message as lower-case hex.

    Either hex case is accepted, as avault accepts it, and the empty message is
    valid: pure Ed25519 signs zero bytes.
    """
    if not isinstance(value, str):
        raise SolanaSigningError("message_hex must be a hex string of the exact message bytes")
    # The size bound comes first, so an oversized input is refused before it is scanned or copied.
    if len(value) > MAX_MESSAGE_BYTES * 2:
        raise SolanaSigningError("message_hex exceeds the 1 MiB message limit")
    if len(value) % 2 or not _HEX_RE.fullmatch(value):
        raise SolanaSigningError("message_hex must be an even number of hexadecimal characters")
    return value.lower()


def is_ed25519_key(signing_key: Any) -> bool:
    return isinstance(signing_key, dict) and signing_key.get("curve") == CURVE


def require_public_key(signing_key: Any) -> str:
    """Return the pinned canonical public key: 32 bytes as lower-case hex."""
    public_key = signing_key.get("public_key") if is_ed25519_key(signing_key) else None
    if (
        not isinstance(public_key, str)
        or not _PUBLIC_KEY_RE.fullmatch(public_key)
        or signing_key.get("chain") != CHAIN
    ):
        raise SolanaSigningError("ed25519 signing requires a pinned 32-byte Solana public key")
    return public_key


def address(public_key: str) -> str:
    """A Solana address is the base58 encoding of the 32-byte public key."""
    if not isinstance(public_key, str) or not _PUBLIC_KEY_RE.fullmatch(public_key):
        raise SolanaSigningError("expected a 32-byte Ed25519 public key as lower-case hex")
    return base58_encode(bytes.fromhex(public_key))


def validate_context(context: Any, *, message_hex: str) -> dict[str, Any]:
    """Check a ``solana-transaction-v1`` context against the request's message.

    The context is signed into the operation binding as given, so its
    ``messageHex`` must already be lower case, and a transaction is never empty.
    This check is bounded: it reads two fixed keys and tests the message length
    before looking at its content, so it is safe to run on an untrusted context
    before any pass that walks or encodes every value.
    """
    if not isinstance(context, dict):
        raise SolanaSigningError("signing_context must be an object")
    if context.get("kind") != CONTEXT_KIND:
        raise SolanaSigningError(f"ed25519 signing accepts only a {CONTEXT_KIND} signing_context")
    if set(context) != {"kind", "messageHex"}:
        # Field names are not echoed: they are caller-supplied and unbounded.
        raise SolanaSigningError(f"a {CONTEXT_KIND} signing_context has exactly the fields kind and messageHex")
    context_message = context["messageHex"]
    if isinstance(context_message, str) and len(context_message) > MAX_MESSAGE_BYTES * 2:
        raise SolanaSigningError(f"{CONTEXT_KIND} messageHex exceeds the 1 MiB message limit")
    if not isinstance(context_message, str) or len(context_message) % 2 or not _LOWER_HEX_RE.fullmatch(context_message):
        raise SolanaSigningError(
            f"{CONTEXT_KIND} messageHex must be a non-empty, even number of lower-case hexadecimal characters"
        )
    if context_message != message_hex:
        raise SolanaSigningError(f"{CONTEXT_KIND} messageHex must equal the sign request message_hex exactly")
    return {"kind": CONTEXT_KIND, "messageHex": context_message}


def verify_signature(public_key: str, message_hex: str, signature: Any) -> None:
    """Verify a pure Ed25519 signature over the exact message bytes."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        raise SolanaSigningError("ed25519 signatures must be 64 bytes of hex")
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature), bytes.fromhex(message_hex)
        )
    except (InvalidSignature, ValueError) as exc:
        raise SolanaSigningError("ed25519 signature does not verify against the pinned public key") from exc

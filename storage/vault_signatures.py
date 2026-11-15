"""Public secp256k1 signature verification shared by custody consumers.

No key custody or signing: ECDSA verifies low-S and the recoverable public point;
BIP340 verifies the point passed to it, BIP341 verifies the BIP86 tweaked point.
Extracted from the existing API verifier without changing its algorithms.
"""
from __future__ import annotations

import hashlib
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from storage.vault_addresses import taproot_output_key_xonly

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
Point = tuple[int, int] | None


def mod_sqrt(value: int) -> int | None:
    root = pow(value, (P + 1) // 4, P)
    return root if pow(root, 2, P) == value % P else None


def decompress(public_key: bytes) -> Point:
    if len(public_key) == 33 and public_key[0] in {2, 3}:
        x = int.from_bytes(public_key[1:], "big")
        if x >= P:
            return None
        y = mod_sqrt((pow(x, 3, P) + 7) % P)
        if y is None:
            return None
        return x, P - y if y % 2 != public_key[0] & 1 else y
    if len(public_key) == 65 and public_key[0] == 4:
        x, y = int.from_bytes(public_key[1:33], "big"), int.from_bytes(public_key[33:], "big")
        if x >= P or y >= P or (pow(y, 2, P) - pow(x, 3, P) - 7) % P:
            return None
        return x, y
    return None


def lift_x(x: int) -> Point:
    if x >= P:
        return None
    y = mod_sqrt((pow(x, 3, P) + 7) % P)
    return None if y is None else (x, y if y % 2 == 0 else P - y)


def add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    slope = ((3 * x1 * x1 * pow(2 * y1, -1, P)) if a == b else ((y2 - y1) * pow(x2 - x1, -1, P))) % P
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def multiply(scalar: int, point: Point) -> Point:
    result = None
    while scalar:
        if scalar & 1:
            result = add(result, point)
        point = add(point, point)
        scalar >>= 1
    return result


def recover_ecdsa(r: int, s: int, digest: bytes, recovery_id: int) -> Point:
    if not (1 <= r < N and 1 <= s < N):
        return None
    x = r + (recovery_id >> 1) * N
    if x >= P:
        return None
    y = mod_sqrt((pow(x, 3, P) + 7) % P)
    if y is None:
        return None
    if y % 2 != recovery_id & 1:
        y = P - y
    point_r = x, y
    if multiply(N, point_r) is not None:
        return None
    z = int.from_bytes(digest, "big") % N
    return multiply(pow(r, -1, N), add(multiply(s, point_r), multiply(z, (G[0], -G[1] % P))))


def tagged_hash(tag: str, payload: bytes) -> bytes:
    hashed = hashlib.sha256(tag.encode("utf-8")).digest()
    return hashlib.sha256(hashed + hashed + payload).digest()


def _signature_bytes(signature: dict[str, Any]) -> bytes:
    raw = signature.get("signature")
    if not isinstance(raw, str) or not raw:
        raise ValueError("signature payload requires a non-empty signature")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("signature must be hex-encoded bytes") from exc


def verify_ecdsa(*, public_key_bytes: bytes, digest_bytes: bytes, scheme: str, signature: dict[str, Any]) -> None:
    encoded = _signature_bytes(signature)
    recovery_id = signature.get("recovery_id")
    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), public_key_bytes)
    except ValueError as exc:
        raise ValueError("protected signing public key is not a valid secp256k1 point") from exc
    if scheme == "ecdsa-secp256k1-recoverable":
        if len(encoded) != 64:
            raise ValueError("recoverable secp256k1 signatures must be 64 bytes")
        if type(recovery_id) is not int or recovery_id not in {0, 1, 2, 3}:
            raise ValueError("recoverable secp256k1 signatures require recovery_id 0..3")
        r, s = int.from_bytes(encoded[:32], "big"), int.from_bytes(encoded[32:], "big")
        if not (1 <= r < N and 1 <= s <= N // 2):
            raise ValueError("secp256k1 signatures must use valid r/s and low-S")
        encoded = utils.encode_dss_signature(r, s)
    else:
        try:
            r, s = utils.decode_dss_signature(encoded)
        except (ValueError, TypeError) as exc:
            raise ValueError("DER secp256k1 signatures must be valid DER") from exc
        if not (1 <= r < N and 1 <= s <= N // 2):
            raise ValueError("secp256k1 signatures must use valid r/s and low-S")
        if recovery_id is not None:
            raise ValueError("DER secp256k1 signatures must not include recovery_id")
    try:
        public_key.verify(encoded, digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    except InvalidSignature as exc:
        raise ValueError("browser signature does not verify against signing_public_key") from exc
    if scheme == "ecdsa-secp256k1-recoverable" and recover_ecdsa(r, s, digest_bytes, recovery_id) != decompress(public_key_bytes):
        raise ValueError("recoverable signature recovery_id does not match signing_public_key")


def _xonly(public_key: bytes) -> bytes:
    if len(public_key) == 32:
        xonly = public_key
    elif len(public_key) == 33 and public_key[0] in {2, 3}:
        xonly = public_key[1:]
    elif len(public_key) == 65 and public_key[0] == 4:
        xonly = public_key[1:33]
        if decompress(public_key) is None:
            raise ValueError("protected signing public key is not a valid secp256k1 point")
    else:
        raise ValueError("protected signing public key must be compressed, uncompressed, or x-only secp256k1")
    if lift_x(int.from_bytes(xonly, "big")) is None:
        raise ValueError("protected signing public key is not a valid secp256k1 point")
    return xonly


def verify_schnorr(*, public_key_bytes: bytes, digest_bytes: bytes, signature: dict[str, Any]) -> None:
    encoded = _signature_bytes(signature)
    if len(encoded) != 64:
        raise ValueError("BIP340 Schnorr signatures must be 64 bytes")
    if signature.get("recovery_id") is not None:
        raise ValueError("BIP340 Schnorr signatures must not include recovery_id")
    xonly = _xonly(public_key_bytes)
    r, s = int.from_bytes(encoded[:32], "big"), int.from_bytes(encoded[32:], "big")
    if r >= P or s >= N:
        raise ValueError("browser signature does not verify against signing_public_key")
    point = lift_x(int.from_bytes(xonly, "big"))
    if point is None:
        raise ValueError("protected signing public key is not a valid secp256k1 point")
    challenge = int.from_bytes(tagged_hash("BIP0340/challenge", encoded[:32] + xonly + digest_bytes), "big") % N
    nonce = add(multiply(s, G), multiply(challenge, (point[0], -point[1] % P)))
    if nonce is None or nonce[1] % 2 != 0 or nonce[0] != r:
        raise ValueError("browser signature does not verify against signing_public_key")


def verify_taproot(*, public_key_bytes: bytes, digest_bytes: bytes, signature: dict[str, Any]) -> None:
    try:
        output = taproot_output_key_xonly(public_key_bytes).to_bytes(32, "big")
    except ValueError as exc:
        raise ValueError("invalid Taproot signing public key") from exc
    verify_schnorr(public_key_bytes=output, digest_bytes=digest_bytes, signature=signature)

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from storage import vault_webauthn


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value)


def _cbor_uint(value: int) -> bytes:
    if value < 24:
        return bytes([value])
    if value < 256:
        return b"\x18" + value.to_bytes(1, "big")
    if value < 65536:
        return b"\x19" + value.to_bytes(2, "big")
    return b"\x1a" + value.to_bytes(4, "big")


def _cbor_int(value: int) -> bytes:
    if value >= 0:
        return _cbor_uint(value)
    encoded = -1 - value
    raw = _cbor_uint(encoded)
    return bytes([raw[0] | 0x20]) + raw[1:]


def _cbor_bytes(value: bytes) -> bytes:
    raw = _cbor_uint(len(value))
    return bytes([raw[0] | 0x40]) + raw[1:] + value


def _cbor_text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    raw = _cbor_uint(len(encoded))
    return bytes([raw[0] | 0x60]) + raw[1:] + encoded


def _cbor_array(value: list) -> bytes:
    raw = _cbor_uint(len(value))
    return bytes([raw[0] | 0x80]) + raw[1:] + b"".join(_cbor(item) for item in value)


def _cbor_map(value: dict) -> bytes:
    raw = _cbor_uint(len(value))
    return bytes([raw[0] | 0xA0]) + raw[1:] + b"".join(_cbor(key) + _cbor(item) for key, item in value.items())


def _cbor(value) -> bytes:
    if isinstance(value, int):
        return _cbor_int(value)
    if isinstance(value, bytes):
        return _cbor_bytes(value)
    if isinstance(value, str):
        return _cbor_text(value)
    if isinstance(value, list):
        return _cbor_array(value)
    if isinstance(value, dict):
        return _cbor_map(value)
    raise TypeError(f"unsupported CBOR test value: {type(value).__name__}")


def _client_data(*, typ: str, challenge_b64: str, origin: str) -> bytes:
    return json.dumps(
        {
            "type": typ,
            "challenge": _b64url(_b64decode(challenge_b64)),
            "origin": origin,
            "crossOrigin": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass
class WebAuthnTestCredential:
    rp_id: str = "alex-app.avibe.bot"
    origin: str = "https://alex-app.avibe.bot"
    credential_id: bytes = b"test-credential-id"

    def __post_init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())

    @property
    def credential_id_b64(self) -> str:
        return _b64(self.credential_id)

    @staticmethod
    def es256_public_key_cose(x: int, y: int) -> bytes:
        return _cbor(
            {
                1: 2,
                3: vault_webauthn.ALG_ES256,
                -1: 1,
                -2: x.to_bytes(32, "big"),
                -3: y.to_bytes(32, "big"),
            }
        )

    def public_key_cose(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return self.es256_public_key_cose(numbers.x, numbers.y)

    def registration_payload(
        self,
        *,
        challenge_id: str,
        challenge_b64: str,
        sign_count: int = 1,
        public_key_cose: bytes | None = None,
    ) -> dict:
        client_data = _client_data(
            typ=vault_webauthn.WEBAUTHN_CREATE_TYPE,
            challenge_b64=challenge_b64,
            origin=self.origin,
        )
        flags = 0x01 | 0x04 | 0x40
        auth_data = (
            hashlib.sha256(self.rp_id.encode("utf-8")).digest()
            + bytes([flags])
            + sign_count.to_bytes(4, "big")
            + (b"\x00" * 16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + (public_key_cose or self.public_key_cose())
        )
        attestation_object = _cbor({"fmt": "none", "authData": auth_data, "attStmt": {}})
        return {
            "challenge_id": challenge_id,
            "credential": {
                "id": _b64url(self.credential_id),
                "rawId": self.credential_id_b64,
                "type": "public-key",
                "response": {
                    "clientDataJSON": _b64(client_data),
                    "attestationObject": _b64(attestation_object),
                    "transports": ["internal"],
                },
            },
        }

    def assertion_authz(
        self,
        *,
        challenge_id: str,
        factor_id: str,
        challenge_b64: str,
        sign_count: int = 2,
    ) -> dict:
        client_data = _client_data(
            typ=vault_webauthn.WEBAUTHN_GET_TYPE,
            challenge_b64=challenge_b64,
            origin=self.origin,
        )
        auth_data = (
            hashlib.sha256(self.rp_id.encode("utf-8")).digest()
            + bytes([0x01 | 0x04])
            + sign_count.to_bytes(4, "big")
        )
        signature = self.private_key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "kind": "webauthn",
            "challenge_id": challenge_id,
            "factor_id": factor_id,
            "assertion": {
                "id": _b64url(self.credential_id),
                "rawId": self.credential_id_b64,
                "type": "public-key",
                "response": {
                    "clientDataJSON": _b64(client_data),
                    "authenticatorData": _b64(auth_data),
                    "signature": _b64(signature),
                    "userHandle": None,
                },
            },
        }

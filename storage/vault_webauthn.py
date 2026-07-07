"""Minimal WebAuthn registration/assertion verification for Vault authz.

This module intentionally implements only the server-verifiable pieces Vaults
need today: passkey registration public-key capture and fresh assertion checks
for protected operations. Passkeys commonly use ``none`` attestation, so device
provenance and the registration signature are not a trust anchor here. The
authorization factor trust anchor is instead enforced by ``vault_service``:
the first factor is bound to the one-time protected-vault establishment
transaction, and later factors must chain from a fresh assertion by an existing
factor.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

WEBAUTHN_CREATE_TYPE = "webauthn.create"
WEBAUTHN_GET_TYPE = "webauthn.get"
ALG_ES256 = -7
ALG_RS256 = -257
SUPPORTED_ALGS = {ALG_ES256, ALG_RS256}

_FLAG_UP = 0x01
_FLAG_UV = 0x04
_FLAG_AT = 0x40


class WebAuthnVerificationError(ValueError):
    """Raised when a WebAuthn ceremony cannot be verified."""


@dataclass(frozen=True)
class WebAuthnRpContext:
    origin: str
    rp_id: str


@dataclass(frozen=True)
class WebAuthnRegistration:
    credential_id: str
    public_key: str
    alg: int
    sign_count: int


@dataclass(frozen=True)
class WebAuthnAssertion:
    credential_id: str
    sign_count: int


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(value: str) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise WebAuthnVerificationError("missing base64 field")
    padded = raw + "=" * (-len(raw) % 4)
    try:
        if "-" in raw or "_" in raw:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except Exception as exc:
        try:
            return base64.urlsafe_b64decode(padded)
        except Exception:
            raise WebAuthnVerificationError("invalid base64 field") from exc


def challenge_hash(challenge: bytes) -> str:
    return hashlib.sha256(challenge).hexdigest()


def rp_context_from_origin(origin: str) -> WebAuthnRpContext:
    parsed = urlsplit(str(origin or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        raise WebAuthnVerificationError("invalid WebAuthn origin")
    if host == "localhost":
        if scheme not in {"http", "https"}:
            raise WebAuthnVerificationError("localhost WebAuthn origin must be http or https")
    elif scheme != "https":
        raise WebAuthnVerificationError("WebAuthn origin must be https outside localhost")
    if _is_raw_ip(host) or ":" in host or (host != "localhost" and "." not in host):
        raise WebAuthnVerificationError("WebAuthn RP id must be localhost or a domain")
    netloc = parsed.netloc.lower()
    if not netloc:
        raise WebAuthnVerificationError("invalid WebAuthn origin")
    return WebAuthnRpContext(origin=f"{scheme}://{netloc}", rp_id=host)


def _is_raw_ip(host: str) -> bool:
    parts = host.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


class _CborReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    @property
    def pos(self) -> int:
        return self._pos

    def decode(self) -> Any:
        initial = self._read_byte()
        major = initial >> 5
        add = initial & 0x1F
        if major == 0:
            return self._read_uint(add)
        if major == 1:
            return -1 - self._read_uint(add)
        if major == 2:
            return self._read(self._read_uint(add))
        if major == 3:
            return self._read(self._read_uint(add)).decode("utf-8")
        if major == 4:
            return [self.decode() for _ in range(self._read_uint(add))]
        if major == 5:
            return {self.decode(): self.decode() for _ in range(self._read_uint(add))}
        if major == 6:
            self._read_uint(add)
            return self.decode()
        if major == 7:
            if add == 20:
                return False
            if add == 21:
                return True
            if add == 22:
                return None
            raise WebAuthnVerificationError("unsupported CBOR simple value")
        raise WebAuthnVerificationError("unsupported CBOR value")

    def _read_uint(self, add: int) -> int:
        if add < 24:
            return add
        if add == 24:
            return self._read_byte()
        if add == 25:
            return int.from_bytes(self._read(2), "big")
        if add == 26:
            return int.from_bytes(self._read(4), "big")
        if add == 27:
            return int.from_bytes(self._read(8), "big")
        raise WebAuthnVerificationError("indefinite CBOR values are not supported")

    def _read_byte(self) -> int:
        return self._read(1)[0]

    def _read(self, length: int) -> bytes:
        end = self._pos + length
        if end > len(self._data):
            raise WebAuthnVerificationError("truncated CBOR value")
        out = self._data[self._pos : end]
        self._pos = end
        return out


def _cbor_decode(data: bytes) -> Any:
    reader = _CborReader(data)
    value = reader.decode()
    if reader.pos != len(data):
        raise WebAuthnVerificationError("trailing CBOR bytes")
    return value


def _cbor_decode_first(data: bytes) -> Any:
    reader = _CborReader(data)
    return reader.decode()


def _client_data(client_data_json_b64: str, *, expected_type: str, expected_challenge_hash: str, expected_origin: str) -> tuple[dict[str, Any], bytes]:
    client_data_json = b64decode(client_data_json_b64)
    try:
        client_data = json.loads(client_data_json)
    except json.JSONDecodeError as exc:
        raise WebAuthnVerificationError("invalid clientDataJSON") from exc
    if client_data.get("type") != expected_type:
        raise WebAuthnVerificationError("unexpected WebAuthn client data type")
    if client_data.get("origin") != expected_origin:
        raise WebAuthnVerificationError("unexpected WebAuthn origin")
    if client_data.get("crossOrigin") not in {None, False}:
        raise WebAuthnVerificationError("cross-origin WebAuthn assertions are not accepted")
    challenge = b64decode(str(client_data.get("challenge") or ""))
    if challenge_hash(challenge) != expected_challenge_hash:
        raise WebAuthnVerificationError("WebAuthn challenge mismatch")
    return client_data, client_data_json


def _parse_authenticator_data(auth_data: bytes, *, rp_id: str, require_attested: bool) -> tuple[int, int, bytes | None, bytes | None]:
    if len(auth_data) < 37:
        raise WebAuthnVerificationError("authenticatorData is too short")
    rp_id_hash = auth_data[:32]
    expected_rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    if rp_id_hash != expected_rp_id_hash:
        raise WebAuthnVerificationError("WebAuthn RP id hash mismatch")
    flags = auth_data[32]
    if flags & _FLAG_UP != _FLAG_UP:
        raise WebAuthnVerificationError("WebAuthn user presence is required")
    if flags & _FLAG_UV != _FLAG_UV:
        raise WebAuthnVerificationError("WebAuthn user verification is required")
    sign_count = int.from_bytes(auth_data[33:37], "big")
    if not require_attested:
        return flags, sign_count, None, None
    if flags & _FLAG_AT != _FLAG_AT:
        raise WebAuthnVerificationError("WebAuthn attested credential data is required")
    offset = 37 + 16
    if len(auth_data) < offset + 2:
        raise WebAuthnVerificationError("WebAuthn credential id is truncated")
    credential_id_len = int.from_bytes(auth_data[offset : offset + 2], "big")
    offset += 2
    credential_id = auth_data[offset : offset + credential_id_len]
    if len(credential_id) != credential_id_len:
        raise WebAuthnVerificationError("WebAuthn credential id is truncated")
    offset += credential_id_len
    public_key = auth_data[offset:]
    if not public_key:
        raise WebAuthnVerificationError("WebAuthn credential public key is missing")
    _public_key_from_cose(public_key)
    return flags, sign_count, credential_id, public_key


def _public_key_from_cose(public_key_cose: bytes):
    # Authenticator data may include extension output bytes after the COSE key.
    # The first CBOR item is the credentialPublicKey; trailing bytes belong to
    # extension data and should not make registration fail.
    cose = _cbor_decode_first(public_key_cose)
    if not isinstance(cose, dict):
        raise WebAuthnVerificationError("invalid COSE public key")
    alg = cose.get(3)
    if alg not in SUPPORTED_ALGS:
        raise WebAuthnVerificationError("unsupported WebAuthn public key algorithm")
    kty = cose.get(1)
    if alg == ALG_ES256:
        if kty != 2 or cose.get(-1) != 1 or not isinstance(cose.get(-2), bytes) or not isinstance(cose.get(-3), bytes):
            raise WebAuthnVerificationError("invalid ES256 COSE public key")
        x = int.from_bytes(cose[-2], "big")
        y = int.from_bytes(cose[-3], "big")
        try:
            return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key(), alg
        except Exception as exc:
            raise WebAuthnVerificationError("invalid ES256 COSE public key") from exc
    if kty != 3 or not isinstance(cose.get(-1), bytes) or not isinstance(cose.get(-2), bytes):
        raise WebAuthnVerificationError("invalid RS256 COSE public key")
    n = int.from_bytes(cose[-1], "big")
    e = int.from_bytes(cose[-2], "big")
    try:
        return rsa.RSAPublicNumbers(e, n).public_key(), alg
    except Exception as exc:
        raise WebAuthnVerificationError("invalid RS256 COSE public key") from exc


def verify_registration(
    credential: dict[str, Any],
    *,
    expected_challenge_hash: str,
    expected_origin: str,
    rp_id: str,
) -> WebAuthnRegistration:
    if not isinstance(credential, dict):
        raise WebAuthnVerificationError("WebAuthn credential must be an object")
    response = credential.get("response")
    if not isinstance(response, dict):
        raise WebAuthnVerificationError("WebAuthn registration response is missing")
    _client_data(
        str(response.get("clientDataJSON") or ""),
        expected_type=WEBAUTHN_CREATE_TYPE,
        expected_challenge_hash=expected_challenge_hash,
        expected_origin=expected_origin,
    )
    attestation_object = _cbor_decode(b64decode(str(response.get("attestationObject") or "")))
    if not isinstance(attestation_object, dict) or not isinstance(attestation_object.get("authData"), bytes):
        raise WebAuthnVerificationError("invalid WebAuthn attestation object")
    _flags, sign_count, credential_id, public_key = _parse_authenticator_data(
        attestation_object["authData"],
        rp_id=rp_id,
        require_attested=True,
    )
    assert credential_id is not None and public_key is not None
    raw_id = b64decode(str(credential.get("rawId") or ""))
    if raw_id != credential_id:
        raise WebAuthnVerificationError("WebAuthn credential id mismatch")
    _public_key, alg = _public_key_from_cose(public_key)
    return WebAuthnRegistration(
        credential_id=b64encode(credential_id),
        public_key=b64encode(public_key),
        alg=alg,
        sign_count=sign_count,
    )


def verify_assertion(
    assertion: dict[str, Any],
    *,
    credential_id: str,
    public_key: str,
    alg: int,
    stored_sign_count: int,
    expected_challenge_hash: str,
    expected_origin: str,
    rp_id: str,
) -> WebAuthnAssertion:
    if not isinstance(assertion, dict):
        raise WebAuthnVerificationError("WebAuthn assertion must be an object")
    response = assertion.get("response")
    if not isinstance(response, dict):
        raise WebAuthnVerificationError("WebAuthn assertion response is missing")
    raw_id = b64decode(str(assertion.get("rawId") or ""))
    if b64encode(raw_id) != credential_id:
        raise WebAuthnVerificationError("WebAuthn assertion credential mismatch")
    _client_data_payload, client_data_json = _client_data(
        str(response.get("clientDataJSON") or ""),
        expected_type=WEBAUTHN_GET_TYPE,
        expected_challenge_hash=expected_challenge_hash,
        expected_origin=expected_origin,
    )
    auth_data = b64decode(str(response.get("authenticatorData") or ""))
    _flags, sign_count, _credential_id, _public_key = _parse_authenticator_data(
        auth_data,
        rp_id=rp_id,
        require_attested=False,
    )
    if stored_sign_count > 0 and sign_count <= stored_sign_count:
        raise WebAuthnVerificationError("WebAuthn signature counter replay")
    signature = b64decode(str(response.get("signature") or ""))
    verifier, parsed_alg = _public_key_from_cose(b64decode(public_key))
    if parsed_alg != alg:
        raise WebAuthnVerificationError("WebAuthn public key algorithm mismatch")
    signed = auth_data + hashlib.sha256(client_data_json).digest()
    try:
        if alg == ALG_ES256:
            verifier.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == ALG_RS256:
            verifier.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise WebAuthnVerificationError("unsupported WebAuthn public key algorithm")
    except InvalidSignature as exc:
        raise WebAuthnVerificationError("invalid WebAuthn assertion signature") from exc
    return WebAuthnAssertion(credential_id=credential_id, sign_count=sign_count)

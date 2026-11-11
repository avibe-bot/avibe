"""Builders for Model Hub engine channel tests.

The signer holds a throwaway secp256k1 key made for one test. It exists so
tests can produce envelopes a real verifier accepts; it is never a trust root
outside the test that created it. ``LocalVerifier`` checks signatures with the
``cryptography`` package under the rules avault's ``verify`` documents (strict
DER, low-S only), so a tampered byte fails for the real reason.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from core import model_hub_engine_channel as channel
from core.model_hub_engine_manifest import EXPECTED_PLATFORMS, OWNED_RELEASE_ROOT, UPSTREAM_PLATFORM_NAMES

_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
NOW = datetime(2026, 11, 10, 6, 0, 0, tzinfo=timezone.utc)
KEY_ID = "test-channel-key"


class Signer:
    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256K1())
        self.public_key = self._key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
        ).hex()

    def sign(self, digest: str, *, high_s: bool = False) -> str:
        r, s = utils.decode_dss_signature(
            self._key.sign(bytes.fromhex(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        )
        low = min(s, _ORDER - s)
        return utils.encode_dss_signature(r, _ORDER - low if high_s else low).hex()

    def roots(self, *, key_id: str = KEY_ID, floor: int = 0) -> channel.TrustRoots:
        return channel.TrustRoots(roots=(channel.TrustRoot(key_id, self.public_key),), sequence_floor=floor)


class LocalVerifier:
    """A real signature check, counting calls so tests can prove none happened."""

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, *, public_key: str, digest: str, signature: str) -> None:
        self.calls += 1
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), bytes.fromhex(public_key))
        try:
            _r, s = utils.decode_dss_signature(bytes.fromhex(signature))
            if s > _ORDER // 2:
                raise InvalidSignature
            key.verify(bytes.fromhex(signature), bytes.fromhex(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        except (InvalidSignature, ValueError) as exc:
            raise channel.ChannelError("signature_invalid", "the signature does not verify") from exc


def manifest_bytes(version: str = "v8.0.63", build: int = 2, *, salt: str = "") -> bytes:
    """A publishable six-platform manifest; ``salt`` changes its bytes, not its identity."""

    tag = f"model-hub-engine-{version}-{build}"
    source_sha = "6db7d4b839d86c5b24e17bbe73bcd4db18ef1b1a"
    assets = []
    for platform in sorted(EXPECTED_PLATFORMS):
        name = f"CLIProxyAPI_{version.removeprefix('v')}_{UPSTREAM_PLATFORM_NAMES[platform]}.tar.gz"
        assets.append(
            {
                "platform": platform,
                "url": f"{OWNED_RELEASE_ROOT}/{tag}/{name}",
                "size_bytes": 1000 + build,
                "sha256": hashlib.sha256(f"{tag}/{platform}/archive{salt}".encode()).hexdigest(),
                "binary_sha256": hashlib.sha256(f"{tag}/{platform}/binary{salt}".encode()).hexdigest(),
                "bin_path": "cli-proxy-api.exe" if platform.startswith("windows-") else "cli-proxy-api",
            }
        )
    document = {
        "schema_version": 1,
        "name": "cliproxyapi",
        "version": version,
        "source": "router-for-me/CLIProxyAPI",
        "source_url": f"https://github.com/router-for-me/CLIProxyAPI/tree/{source_sha}",
        "source_sha": source_sha,
        "release_tag": version,
        "asset_release_tag": tag,
        "license": "MIT",
        "assets": assets,
        "build": {"builder": "scripts/build_model_hub_engine.py", "go_version": "go1.26.10", "patches": []},
    }
    return (json.dumps(document, indent=2) + "\n").encode()


def release_entry(manifest: bytes, minimum: str = "3.2.6") -> dict:
    return {
        "min_avibe_version": minimum,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "manifest_base64": base64.b64encode(manifest).decode(),
    }


def tombstone(tag: str, rollback_to: str, reason: str = "regression in streaming") -> dict:
    return {"asset_release_tag": tag, "rollback_to": rollback_to, "reason": reason}


def payload_document(
    sequence: int = 1,
    *,
    releases: list[dict] | None = None,
    revoked: list[dict] | None = None,
    issued_at: datetime = NOW,
    lifetime: timedelta = timedelta(days=7),
) -> dict:
    return {
        "repository": channel.CHANNEL_REPOSITORY,
        "channel": channel.CHANNEL_NAME,
        "sequence": sequence,
        "issued_at": channel.format_timestamp(issued_at),
        "expires_at": channel.format_timestamp(issued_at + lifetime),
        "releases": [release_entry(manifest_bytes())] if releases is None else releases,
        "revoked": revoked or [],
    }


def encode(document: object) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode()


def envelope_bytes(signer: Signer, payload: bytes, *, key_id: str = KEY_ID) -> bytes:
    return channel.encode_envelope(
        key_id=key_id, payload=payload, signature=signer.sign(channel.channel_digest(payload))
    )


def signed(signer: Signer, sequence: int = 1, **fields) -> bytes:
    return envelope_bytes(signer, encode(payload_document(sequence, **fields)))

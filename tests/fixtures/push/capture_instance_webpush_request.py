"""Capture the instance's real Web Push request for the iOS relay fixture.

This is a test-only generator. It uses the production serializer and the
published pywebpush dependency, while replacing cryptographic randomness with
fixed fixture entropy so the captured request is reproducible. The private
receiver and VAPID keys in this module are test material only and must stay on
the Avibe side of the cross-repository fixture boundary.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

FIXTURE_ID = "instance-webpush-nonascii-v1"
TEST_CLOCK = 1_794_096_000
ENDPOINT = "https://avibe.bot/api/push/apns/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
AUTH_SECRET = "BTBZMqHH6r4Tts7J_aSIgg"
RECEIVER_PRIVATE_KEY = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
VAPID_PRIVATE_KEY = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
FIXED_SALT = bytes(range(16))
FIXED_ECDH_PRIVATE_SCALAR = bytes.fromhex(
    "4f6f6e657374796c652d707573682d656364682d666978747572652d7631"
).ljust(32, b"\x00")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _receiver_public_key() -> str:
    private_key = ec.derive_private_key(
        int.from_bytes(_b64url_decode(RECEIVER_PRIVATE_KEY), "big"),
        ec.SECP256R1(),
    )
    return _b64url_encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )


def _vapid_keys() -> Any:
    private_key = ec.derive_private_key(
        int.from_bytes(_b64url_decode(VAPID_PRIVATE_KEY), "big"),
        ec.SECP256R1(),
    )
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    from core.web_push import VapidKeys

    return VapidKeys(
        public_key=_receiver_public_key_from_private(private_key),
        private_key_pem=pem.decode("ascii"),
    )


def _receiver_public_key_from_private(private_key: Any) -> str:
    return _b64url_encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )


def capture_request() -> dict[str, Any]:
    import http_ece
    import py_vapid
    import pywebpush

    from core.web_push import send_web_push

    original_generate_private_key = pywebpush.ec.generate_private_key
    original_entropy = http_ece.os.urandom
    original_clock = pywebpush.time.time
    original_post = pywebpush.requests.post
    original_from_pem_bound = py_vapid.Vapid.from_pem
    fixed_ecdh_private_key = ec.derive_private_key(
        int.from_bytes(FIXED_ECDH_PRIVATE_SCALAR, "big"),
        ec.SECP256R1(),
    )

    class Response:
        status_code = 201
        text = ""
        reason = "Created"
        headers: dict[str, str] = {}

    calls: list[tuple[str, dict[str, Any]]] = []

    def fixed_generate_private_key(curve: Any, backend: Any = None) -> Any:
        if curve.name == "secp256r1":
            return fixed_ecdh_private_key
        return original_generate_private_key(curve, backend)

    def capture_post(endpoint: str, **kwargs: Any) -> Response:
        calls.append((endpoint, kwargs))
        return Response()

    class DeterministicPrivateKey:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        def public_key(self) -> Any:
            return self.delegate.public_key()

        def sign(self, data: bytes, algorithm: Any) -> bytes:
            del algorithm
            return self.delegate.sign(
                data,
                ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
            )

    def deterministic_from_pem(_cls: Any, private_key: bytes) -> Any:
        signer = original_from_pem_bound(private_key)
        signer._private_key = DeterministicPrivateKey(signer.private_key)
        return signer

    pywebpush.ec.generate_private_key = fixed_generate_private_key
    http_ece.os.urandom = lambda size: FIXED_SALT if size == 16 else original_entropy(size)
    pywebpush.time.time = lambda: TEST_CLOCK
    pywebpush.requests.post = capture_post
    py_vapid.Vapid.from_pem = classmethod(deterministic_from_pem)
    try:
        payload = {
            "title": "任务完成",
            "body": "已完成：你好，世界 🌏",
            "url": "/chat/ses_123?msg=msg_456&from=notification",
            "tag": "session:ses_123",
            "message_id": "msg_456",
            "session_id": "ses_123",
            "badge_count": 3,
            "category": "sessions",
        }
        send_web_push(
            subscription={
                "endpoint": ENDPOINT,
                "p256dh": _receiver_public_key(),
                "auth": AUTH_SECRET,
            },
            payload=payload,
            vapid_keys=_vapid_keys(),
        )
        if len(calls) != 1:
            raise AssertionError(f"expected one captured request, got {len(calls)}")
        endpoint, request = calls[0]
        return {
            "fixture_id": FIXTURE_ID,
            "test_clock": TEST_CLOCK,
            "method": "POST",
            "endpoint": endpoint,
            "headers": dict(request["headers"]),
            "body_base64url": _b64url_encode(request["data"]),
        }
    finally:
        pywebpush.ec.generate_private_key = original_generate_private_key
        http_ece.os.urandom = original_entropy
        pywebpush.time.time = original_clock
        pywebpush.requests.post = original_post
        del py_vapid.Vapid.from_pem


def serialize_request(request: dict[str, Any]) -> bytes:
    return (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    captured = capture_request()
    serialized = serialize_request(captured)
    if args.output:
        args.output.write_bytes(serialized)
    else:
        print(serialized.decode("utf-8"), end="")


if __name__ == "__main__":
    main()

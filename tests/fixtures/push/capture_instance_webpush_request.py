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
from cryptography.hazmat.primitives.asymmetric import ec, utils

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


def _vapid_private_key() -> Any:
    private_key = ec.derive_private_key(
        int.from_bytes(_b64url_decode(VAPID_PRIVATE_KEY), "big"),
        ec.SECP256R1(),
    )
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    from py_vapid import Vapid

    return Vapid.from_pem(pem)


def _deterministic_vapid_sign(claims: dict[str, Any], key: Any) -> str:
    """Sign RFC 8292 claims with cryptography's deterministic ECDSA mode."""

    from py_vapid.utils import b64urlencode, num_to_bytes

    header = b64urlencode(b'{"typ":"JWT","alg":"ES256"}')
    encoded_claims = b64urlencode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    token = f"{header}.{encoded_claims}"
    signature = key.sign(
        token.encode("utf-8"),
        ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
    )
    r, s = utils.decode_dss_signature(signature)
    return f"{token}.{b64urlencode(num_to_bytes(r, 32) + num_to_bytes(s, 32))}"


def capture_request() -> dict[str, Any]:
    import http_ece
    import py_vapid
    import pywebpush

    from core.web_push import serialize_notification_payload

    original_generate_private_key = pywebpush.ec.generate_private_key
    original_entropy = http_ece.os.urandom
    original_clock = pywebpush.time.time
    original_vapid_sign = py_vapid.sign
    fixed_ecdh_private_key = ec.derive_private_key(
        int.from_bytes(FIXED_ECDH_PRIVATE_SCALAR, "big"),
        ec.SECP256R1(),
    )

    class Response:
        status_code = 201
        text = ""
        reason = "Created"
        headers: dict[str, str] = {}

    class CaptureSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def post(self, endpoint: str, **kwargs: Any) -> Response:
            self.calls.append((endpoint, kwargs))
            return Response()

    def fixed_generate_private_key(curve: Any, backend: Any = None) -> Any:
        if curve.name == "secp256r1":
            return fixed_ecdh_private_key
        return original_generate_private_key(curve, backend)

    pywebpush.ec.generate_private_key = fixed_generate_private_key
    http_ece.os.urandom = lambda size: FIXED_SALT if size == 16 else original_entropy(size)
    pywebpush.time.time = lambda: TEST_CLOCK
    py_vapid.sign = _deterministic_vapid_sign
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
        session = CaptureSession()
        pywebpush.webpush(
            subscription_info={
                "endpoint": ENDPOINT,
                "keys": {
                    "p256dh": _receiver_public_key(),
                    "auth": AUTH_SECRET,
                },
            },
            data=serialize_notification_payload(payload),
            vapid_private_key=_vapid_private_key(),
            vapid_claims={
                "sub": "mailto:notifications@avibe.bot",
                "aud": "https://avibe.bot",
                "exp": TEST_CLOCK + 4 * 60 * 60,
            },
            ttl=43_200,
            headers={"Urgency": "normal", "Topic": "session:ses_123"},
            requests_session=session,
        )
        if len(session.calls) != 1:
            raise AssertionError(f"expected one captured request, got {len(session.calls)}")
        endpoint, request = session.calls[0]
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
        py_vapid.sign = original_vapid_sign


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

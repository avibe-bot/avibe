#!/usr/bin/env python3
"""Generate the request-only native badge relay contract fixture.

The private scalar below is deliberately public test material. It is not used by
any Avibe registration, deployment, or user device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core import web_push

FIXED_NOW = 1_795_000_000
REGISTRATION_ID = "A" * 43
BADGES = (0, 7)
TEST_PRIVATE_SCALAR = 1


def _test_vapid_keys() -> web_push.VapidKeys:
    private_key = ec.derive_private_key(TEST_PRIVATE_SCALAR, ec.SECP256R1())
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return web_push.VapidKeys(
        private_key_pem=private_pem,
        public_key=web_push._b64url_no_padding(public_key),
    )


def build_fixture() -> dict[str, object]:
    captured: list[httpx.Request] = []

    def relay(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        badge = json.loads(request.content)["badge"]
        return httpx.Response(
            201,
            request=request,
            json={
                "ok": True,
                "registration_id": REGISTRATION_ID,
                "badge": badge,
                "apns_status": 200,
            },
        )

    original_time = web_push.time.time
    web_push.time.time = lambda: FIXED_NOW
    try:
        with httpx.Client(transport=httpx.MockTransport(relay)) as client:
            for badge in BADGES:
                web_push.send_badge_update(
                    subscription={
                        "endpoint": (
                            "https://avibe.bot/api/push/apns/"
                            f"{REGISTRATION_ID}"
                        )
                    },
                    badge=badge,
                    vapid_keys=_test_vapid_keys(),
                    client=client,
                )
    finally:
        web_push.time.time = original_time

    return {
        "registration_id": REGISTRATION_ID,
        "aud_origin": web_push.NATIVE_PUSH_RELAY_ORIGIN,
        "test_clock": FIXED_NOW,
        "exp": FIXED_NOW + web_push.DEFAULT_BADGE_VAPID_EXPIRY_SECONDS,
        "requests": [
            {
                "method": request.method,
                "path": request.url.path,
                "headers": {
                    "authorization": request.headers["authorization"],
                    "content-type": request.headers["content-type"],
                },
                "body_utf8": request.content.decode("utf-8"),
            }
            for request in captured
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/web_push/badge_request_fixture.json"),
    )
    args = parser.parse_args()

    content = json.dumps(build_fixture(), indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    args.output.with_suffix(".sha256").write_text(
        f"{digest}  {args.output.name}\n",
        encoding="ascii",
    )
    print(digest)


if __name__ == "__main__":
    main()

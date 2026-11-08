from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import http_ece


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "push"
REQUEST_PATH = FIXTURE_DIR / "instance-webpush-request.json"
RECEIVER_PATH = FIXTURE_DIR / "instance-webpush-receiver.json"
GENERATOR_PATH = FIXTURE_DIR / "capture_instance_webpush_request.py"
NATIVE_SUBSCRIPTION_PATH = FIXTURE_DIR / "native-subscription-key.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("push_fixture_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load push fixture generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _header(request: dict, name: str) -> str:
    expected = name.lower()
    for key, value in request["headers"].items():
        if key.lower() == expected:
            return value
    raise AssertionError(f"missing header {name}")


def test_frozen_request_links_to_private_receiver_companion():
    request_bytes = REQUEST_PATH.read_bytes()
    request = json.loads(request_bytes)
    receiver = json.loads(RECEIVER_PATH.read_text(encoding="utf-8"))
    body = _b64url_decode(request["body_base64url"])

    assert hashlib.sha256(request_bytes).hexdigest() == receiver["request_sha256"]
    assert hashlib.sha256(body).hexdigest() == receiver["request_body_sha256"]
    assert receiver["request_fixture"] == "tests/fixtures/push/instance-webpush-request.json"
    assert receiver["fixture_id"] == request["fixture_id"]
    assert receiver["expected"]["body"].encode("utf-8") not in body
    assert receiver["receiver_private_key"] not in request["body_base64url"]
    assert receiver["auth_secret"] not in request["body_base64url"]


def test_locked_generator_reproduces_frozen_request_bytes():
    generator = _load_generator()
    captured = generator.capture_request()

    assert generator.serialize_request(captured) == REQUEST_PATH.read_bytes()


def test_real_serializer_and_pywebpush_capture_is_decryptable():
    generator = _load_generator()
    captured = generator.capture_request()
    receiver = json.loads(RECEIVER_PATH.read_text(encoding="utf-8"))
    body = _b64url_decode(captured["body_base64url"])

    plaintext = http_ece.decrypt(
        body,
        private_key=generator.ec.derive_private_key(
            int.from_bytes(generator._b64url_decode(receiver["receiver_private_key"]), "big"),
            generator.ec.SECP256R1(),
        ),
        auth_secret=generator._b64url_decode(receiver["auth_secret"]),
        version="aes128gcm",
    )

    assert captured["method"] == "POST"
    assert captured["endpoint"] == generator.ENDPOINT
    assert _header(captured, "content-encoding") == "aes128gcm"
    assert _header(captured, "ttl") == "43200"
    assert "urgency" not in {key.lower() for key in captured["headers"]}
    assert "topic" not in {key.lower() for key in captured["headers"]}
    authorization = _header(captured, "authorization")
    token = authorization.split(" t=", 1)[1].split(",", 1)[0]
    claims = json.loads(_b64url_decode(token.split(".")[1]))
    assert claims == {
        "aud": "https://avibe.bot",
        "exp": generator.TEST_CLOCK + 12 * 60 * 60,
        "sub": "mailto:notifications@avibe.bot",
    }
    assert json.loads(plaintext.decode("utf-8")) == receiver["expected"]


def test_production_sender_encrypts_to_native_x963_subscription_key(monkeypatch):
    native = json.loads(NATIVE_SUBSCRIPTION_PATH.read_text(encoding="utf-8"))
    public_key = _b64url_decode(native["p256dh"])
    assert len(public_key) == 65
    assert public_key[0] == 4

    generator = _load_generator()
    import pywebpush

    calls = []

    class Response:
        status_code = 201
        text = ""
        reason = "Created"
        headers = {}

    def capture_post(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return Response()

    monkeypatch.setattr(pywebpush.requests, "post", capture_post)

    from core.web_push import send_web_push

    send_web_push(
        subscription={
            "endpoint": generator.ENDPOINT,
            "p256dh": native["p256dh"],
            "auth": generator.AUTH_SECRET,
        },
        payload={"title": "Native key shape", "body": "X9.63"},
        vapid_keys=generator._vapid_keys(),
    )

    assert len(calls) == 1
    assert calls[0][0] == generator.ENDPOINT
    assert calls[0][1]["data"]

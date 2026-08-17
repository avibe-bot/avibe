from __future__ import annotations

import time
import threading
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.ui_server_test_helpers import _save_config
from vibe import show_identity


def _identity_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.issuer = "https://backend.test"
    cloud.jwks_uri = "https://backend.test/oauth/jwks.json"
    config.save()
    return config


def test_show_identity_state_round_trips_only_for_its_callback_origin(monkeypatch, tmp_path):
    config = _identity_config(monkeypatch, tmp_path)
    authorization_url = show_identity.begin_show_identity_authorization(
        config,
        callback_origin="https://alex.avibe.bot",
        share_id="shared-page",
        return_target="/p/shared-page/reports/daily?tab=1",
        now=100,
    )
    parsed = urlsplit(authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.path == ("/api/v1/instances/inst_123/show-identity/authorize")
    assert query["redirect_uri"] == ["https://alex.avibe.bot/auth/show-identity/callback"]
    state = show_identity.read_show_identity_state(
        config,
        query["state"][0],
        callback_origin="https://alex.avibe.bot",
        now=101,
    )
    assert state.share_id == "shared-page"
    assert state.return_target == "/p/shared-page/reports/daily?tab=1"
    assert query["nonce"] == [state.nonce]

    with pytest.raises(show_identity.ShowIdentityError):
        show_identity.read_show_identity_state(
            config,
            query["state"][0],
            callback_origin="https://other.example",
            now=101,
        )


def test_show_identity_state_rejects_tampering_and_expiry(monkeypatch, tmp_path):
    config = _identity_config(monkeypatch, tmp_path)
    authorization_url = show_identity.begin_show_identity_authorization(
        config,
        callback_origin="https://alex.avibe.bot",
        share_id="shared-page",
        return_target="/p/shared-page/",
        now=100,
    )
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]

    with pytest.raises(show_identity.ShowIdentityError):
        show_identity.read_show_identity_state(
            config,
            f"{state[:-1]}{'A' if state[-1] != 'A' else 'B'}",
            callback_origin="https://alex.avibe.bot",
            now=101,
        )
    with pytest.raises(show_identity.ShowIdentityError, match="expired_state"):
        show_identity.read_show_identity_state(
            config,
            state,
            callback_origin="https://alex.avibe.bot",
            now=100 + show_identity.STATE_TTL_SECONDS,
        )


def test_show_identity_assertion_verifies_paired_issuer_instance_and_nonce(
    monkeypatch,
    tmp_path,
):
    config = _identity_config(monkeypatch, tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issued_at = int(time.time())
    assertion = jwt.encode(
        {
            "iss": "https://backend.test",
            "aud": "avibe-show-identity:vr_client_123",
            "sub": "user-1",
            "iat": issued_at,
            "exp": issued_at + 300,
            "jti": "assertion-1",
            "nonce": "nonce-1",
            "instance_id": "inst_123",
            "verified_email": " Viewer@Example.com ",
        },
        private_key,
        algorithm="RS256",
        headers={"typ": "JWT", "kid": "current"},
    )

    class JwkClientStub:
        def __init__(self, uri, *, timeout):
            assert uri == "https://backend.test/oauth/jwks.json"
            assert timeout == 5

        def get_signing_key_from_jwt(self, token):
            assert token == assertion
            return type("SigningKey", (), {"key": private_key.public_key()})()

    monkeypatch.setattr(show_identity, "PyJWKClient", JwkClientStub)

    identity = show_identity.verify_show_identity_assertion(
        config,
        assertion,
        expected_nonce="nonce-1",
    )
    assert identity.subject == "user-1"
    assert identity.normalized_email == "viewer@example.com"
    assert identity.assertion_id == "assertion-1"
    assert identity.expires_at == issued_at + 300

    with pytest.raises(show_identity.ShowIdentityError, match="invalid_assertion"):
        show_identity.verify_show_identity_assertion(
            config,
            assertion,
            expected_nonce="other-nonce",
        )


def test_show_identity_assertion_reports_jwks_outage(monkeypatch, tmp_path):
    config = _identity_config(monkeypatch, tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issued_at = int(time.time())
    assertion = jwt.encode(
        {
            "iss": "https://backend.test",
            "aud": "avibe-show-identity:vr_client_123",
            "sub": "user-1",
            "iat": issued_at,
            "exp": issued_at + 300,
            "jti": "unavailable-assertion",
            "nonce": "nonce-1",
            "instance_id": "inst_123",
            "verified_email": "viewer@example.com",
        },
        private_key,
        algorithm="RS256",
        headers={"typ": "JWT", "kid": "current"},
    )

    class UnavailableJwkClient:
        def __init__(self, _uri, *, timeout):
            assert timeout == 5

        def get_signing_key_from_jwt(self, _token):
            raise show_identity.PyJWKClientConnectionError("offline")

    monkeypatch.setattr(show_identity, "PyJWKClient", UnavailableJwkClient)

    with pytest.raises(show_identity.ShowIdentityError, match="identity_unavailable"):
        show_identity.verify_show_identity_assertion(
            config,
            assertion,
            expected_nonce="nonce-1",
        )


def test_verified_show_identity_is_consumed_atomically_once():
    identity = show_identity.VerifiedShowIdentity(
        subject="user-1",
        normalized_email="viewer@example.com",
        assertion_id=f"atomic-{time.time_ns()}",
        expires_at=int(time.time()) + 300,
    )
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def consume():
        barrier.wait()
        try:
            show_identity.consume_verified_show_identity(identity)
        except show_identity.ShowIdentityError as exc:
            outcomes.append(exc.reason)
        else:
            outcomes.append("accepted")

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["accepted", "replayed_assertion"]


def test_show_guest_lease_is_page_and_share_bound_without_live_membership_state(
    monkeypatch,
    tmp_path,
):
    config = _identity_config(monkeypatch, tmp_path)
    token = show_identity.make_show_guest_lease(
        config,
        page_id="page-1",
        share_id="shared-page",
        normalized_email="viewer@example.com",
    )
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.backend_url = ""
    config.remote_access.vibe_cloud.issuer = ""
    config.remote_access.vibe_cloud.jwks_uri = ""

    lease = show_identity.read_show_guest_lease(
        config,
        token,
        expected_share_id="shared-page",
    )
    assert lease.page_id == "page-1"
    assert lease.normalized_email == "viewer@example.com"

    with pytest.raises(show_identity.ShowIdentityError):
        show_identity.read_show_guest_lease(
            config,
            token,
            expected_share_id="other-page",
        )

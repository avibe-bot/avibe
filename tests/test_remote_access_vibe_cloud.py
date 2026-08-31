from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from config import paths
from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from storage.db import create_sqlite_engine
from storage.models import remote_access_authorizations
from tests.ui_server_test_helpers import remote_session_cookie
from vibe import api, model_service, remote_access, ui_server
from vibe import runtime


@pytest.fixture(autouse=True)
def _resolve_backend_test_to_public_address(monkeypatch):
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)

    def resolve(hostname: str, port: int):
        if hostname == "backend.test":
            return (ipaddress.ip_address("93.184.216.34"),)
        return ()

    monkeypatch.setattr(remote_access, "_resolve_pairing_backend_addresses", resolve)


def _config() -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(),
        remote_access=RemoteAccessConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.instance_id = "inst_123"
    cloud.client_id = "vr_client_123"
    cloud.public_url = "https://alex.avibe.bot"
    cloud.session_secret = "session-secret"
    cloud.token_endpoint = "https://backend.test/oauth/token"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    cloud.jwks_uri = "https://backend.test/oauth/jwks.json"
    cloud.issuer = "https://backend.test"
    return config


def _session_claims(config: V2Config, *, role: str = "owner") -> dict[str, str | int]:
    claims = {
        "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
        "vibe_instance_role": role,
        "vibe_instance_access_source": "owner",
    }
    cloud = config.remote_access.vibe_cloud
    if cloud.instance_secret and cloud.backend_url:
        claims["vibe_instance_authorization_revision"] = 1
    return claims


def _session_cookie(
    config: V2Config,
    email: str = "alex@example.com",
    subject: str = "user-1",
    *,
    role: str = "owner",
) -> str:
    return remote_access.make_session_cookie(
        config,
        email,
        subject,
        session_claims=_session_claims(config, role=role),
    )


def test_ra_tq_026_remote_status_uses_cf_ray_on_paired_public_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.save()
    observed = []

    def status(
        loaded_config=None,
        *,
        client_colo=None,
        client_access="local",
        include_network_path=False,
    ):
        observed.append((loaded_config, client_colo, client_access, include_network_path))
        return {"ok": True, "client_colo": client_colo}

    monkeypatch.setattr(remote_access, "status", status)
    with ui_server.app.test_request_context(
        "/api/remote-access/status",
        base_url="https://alex.avibe.bot",
        headers={"CF-Ray": "9f1234567890abcd-SIN"},
    ):
        response = ui_server.remote_access_status()

    assert response.status_code == 200
    assert observed[0][0].remote_access.vibe_cloud.public_url == config.remote_access.vibe_cloud.public_url
    assert observed[0][1:] == ("SIN", "remote", True)


def test_ra_tq_026_remote_status_ignores_spoofed_cf_ray(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.save()
    observed = []

    def status(
        loaded_config=None,
        *,
        client_colo=None,
        client_access="local",
        include_network_path=False,
    ):
        observed.append((loaded_config, client_colo, client_access, include_network_path))
        return {"ok": True, "client_colo": client_colo}

    monkeypatch.setattr(remote_access, "status", status)
    with ui_server.app.test_request_context(
        "/api/remote-access/status",
        base_url="http://127.0.0.1:5123",
        headers={"CF-Ray": "9f1234567890abcd-NRT"},
    ):
        response = ui_server.remote_access_status()

    assert response.status_code == 200
    assert observed[0][1:] == (None, "local", True)
    assert config.remote_access.vibe_cloud.public_url == "https://alex.avibe.bot"


def test_ra_tq_032_remote_status_keeps_page_fields_and_drops_host_internals(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.save()
    full_payload = {
        "ok": True,
        "provider": "vibe_cloud",
        "enabled": True,
        "public_url": "https://alex.avibe.bot",
        "paired": True,
        "running": True,
        "pid": 4711,
        "pid_state": "cloudflared",
        "binary_found": True,
        "binary_path": "/usr/local/bin/cloudflared",
        "binary_version": "2024.1.0",
        "transport_protocol": "http2",
        "settings": {
            "transport_protocol": "http2",
            "auto_recovery": True,
            "optimization_profile": "balanced",
            "edge_ip_version": "4",
            "edge_bind_address": "192.168.1.5",
        },
        "tunnel_quality": {"sampled_at": "2026-08-10T10:00:00Z", "grade": "good"},
        "network_path": {
            "schema_version": 1,
            "provider": "Cloudflare",
            "asn": 13335,
            "client_access": "remote",
        },
    }
    monkeypatch.setattr(remote_access, "status", lambda *a, **k: dict(full_payload))

    client = ui_server.app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _session_cookie(config),
        domain="alex.avibe.bot",
    )
    remote_response = client.get(
        "/api/remote-access/status",
        base_url="https://alex.avibe.bot",
    )
    local_response = client.get(
        "/api/remote-access/status",
        base_url="http://127.0.0.1:5123",
    )

    assert remote_response.status_code == 200
    remote_body = remote_response.get_json()
    assert set(remote_body) == set(ui_server._REMOTE_ACCESS_STATUS_PUBLIC_FIELDS)
    for sensitive in ("pid", "binary_found", "binary_path", "binary_version"):
        assert sensitive not in remote_body
    assert remote_body["network_path"] == full_payload["network_path"]
    assert remote_body["settings"] == full_payload["settings"]
    assert remote_body["pid_state"] == "cloudflared"

    assert local_response.status_code == 200
    assert local_response.get_json() == full_payload


def test_session_cookie_roundtrip() -> None:
    config = _config()

    cookie = _session_cookie(config)

    assert remote_access.validate_session_cookie(config, cookie) is True
    assert remote_access.validate_session_cookie(config, cookie + "x") is False


def test_session_cookie_rejects_empty_session_secret() -> None:
    config = _config()
    config.remote_access.vibe_cloud.session_secret = ""

    assert remote_access.validate_session_cookie(config, "payload.signature") is False


def test_parse_session_cookie_returns_payload_for_fresh_token() -> None:
    config = _config()
    cookie = _session_cookie(config)

    payload = remote_access.parse_session_cookie(config, cookie)

    assert payload is not None
    assert payload["email"] == "alex@example.com"
    assert payload["sub"] == "user-1"
    assert payload["instance_id"] == "inst_123"
    assert payload["vibe_instance_role"] == "owner"


def test_parse_session_cookie_rejects_legacy_roleless_payload() -> None:
    config = _config()
    issued_at = int(time.time())
    payload = {
        "email": "alex@example.com",
        "sub": "user-1",
        "instance_id": "inst_123",
        "vibe_instance_id": "inst_123",
        "vibe_instance_access_source": "owner",
        "iat": issued_at,
        "exp": issued_at + remote_access.SESSION_TTL_SECONDS,
    }
    payload_text = urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe="")
    signature = remote_access._session_signature(config.remote_access.vibe_cloud.session_secret, payload_text)

    assert remote_access.parse_session_cookie(config, f"{payload_text}.{signature}") is None


def test_session_claims_reject_missing_or_unknown_instance_role() -> None:
    config = _config()
    base_claims = {
        "vibe_instance_id": "inst_123",
        "vibe_instance_access_source": "owner",
    }

    with pytest.raises(remote_access.OAuthCodeExchangeError, match="invalid_instance_role"):
        remote_access.session_claims_from_oidc(config, base_claims)
    with pytest.raises(remote_access.OAuthCodeExchangeError, match="invalid_instance_role"):
        remote_access.session_claims_from_oidc(config, {**base_claims, "vibe_instance_role": "admin"})
    claims = remote_access.session_claims_from_oidc(
        config, {**base_claims, "vibe_instance_role": "member"}
    )
    assert claims["vibe_instance_role"] == "member"


def test_session_cookie_persists_validated_organization_claims() -> None:
    config = _config()
    cookie = remote_session_cookie(
        config,
        "member@example.com",
        "user-1",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-engineering"],
            "vibe_membership_version": "membership-v2",
        },
    )

    payload = remote_access.parse_session_cookie(config, cookie)

    assert payload is not None
    assert payload["vibe_organization_id"] == "org-1"
    assert payload["vibe_group_ids"] == ["group-engineering"]
    assert payload["vibe_membership_version"] == "membership-v2"
    assert remote_access.session_needs_authorization_refresh(
        payload,
        now=int(payload["claims_issued_at"]) + remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS,
    ) is True


def test_session_cookie_rejects_organization_claims_over_supported_group_limit() -> None:
    config = _config()
    group_ids = [f"group-{index}" for index in range(257)]

    with pytest.raises(remote_access.OAuthCodeExchangeError) as error:
        remote_access.make_session_cookie(
            config,
            "member@example.com",
            "user-1",
            session_claims={
                "vibe_instance_id": "inst_123",
                "vibe_instance_role": "viewer",
                "vibe_instance_access_source": "organization_group",
                "vibe_organization_id": "org-1",
                "vibe_organization_member_id": "member-1",
                "vibe_organization_role": "member",
                "vibe_group_ids": group_ids,
            },
        )

    assert error.value.reason == "invalid_organization_claims"


def test_session_claims_accept_organization_member_authorized_by_email() -> None:
    config = _config()

    claims = remote_access.session_claims_from_oidc(
        config,
        {
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "email_domain",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-engineering"],
        },
    )

    assert claims["vibe_organization_id"] == "org-1"


def test_session_claims_reject_partial_organization_context() -> None:
    config = _config()

    with pytest.raises(remote_access.OAuthCodeExchangeError, match="invalid_organization_claims"):
        remote_access.session_claims_from_oidc(
            config,
            {
                "vibe_instance_id": "inst_123",
                "vibe_instance_role": "owner",
                "vibe_instance_access_source": "owner",
                "vibe_organization_id": None,
            },
        )


def test_exchange_oauth_code_returns_claims_safe_for_the_local_session(monkeypatch) -> None:
    config = _config()

    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id_token": "id-token"}

    class JwkClientStub:
        def __init__(self, uri):
            self.uri = uri

        def get_signing_key_from_jwt(self, id_token):
            assert id_token == "id-token"
            return type("SigningKey", (), {"key": "public-key"})()

    monkeypatch.setattr(remote_access.requests, "post", lambda *args, **kwargs: ResponseStub())
    monkeypatch.setattr(remote_access, "PyJWKClient", JwkClientStub)
    monkeypatch.setattr(
        remote_access.jwt,
        "decode",
        lambda *args, **kwargs: {
            "email": "member@example.com",
            "sub": "user-1",
            "email_verified": True,
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "viewer",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org-1",
            "vibe_organization_member_id": "member-1",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["group-engineering"],
        },
    )

    result = remote_access.exchange_oauth_code(config, "code-1", "verifier-1")
    cookie = remote_session_cookie(
        config,
        result["claims"]["email"],
        result["claims"]["sub"],
        session_claims=result["session_claims"],
    )
    payload = remote_access.parse_session_cookie(config, cookie)

    assert result["session_claims"] == {
        "vibe_instance_id": "inst_123",
        "vibe_instance_role": "viewer",
        "vibe_instance_access_source": "organization_group",
        "vibe_organization_id": "org-1",
        "vibe_organization_member_id": "member-1",
        "vibe_organization_role": "member",
        "vibe_group_ids": ["group-engineering"],
    }
    assert payload is not None
    assert payload["vibe_organization_id"] == "org-1"


def test_parse_session_cookie_rejects_tampered_signature() -> None:
    config = _config()
    cookie = _session_cookie(config)

    assert remote_access.parse_session_cookie(config, cookie + "x") is None


def test_organization_session_cookie_uses_server_side_claims_for_large_memberships() -> None:
    config = _config()
    group_ids = [f"grp-{index:03d}-" + ("x" * 192) for index in range(256)]
    cookie = remote_access.make_session_cookie(
        config,
        "member@example.com",
        "user-large-membership",
        session_claims={
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org_123",
            "vibe_organization_member_id": "member_123",
            "vibe_organization_role": "member",
            "vibe_group_ids": group_ids,
            "vibe_membership_version": "membership-v1",
        },
    )

    assert len(cookie.encode("ascii")) <= remote_access.SESSION_COOKIE_MAX_VALUE_BYTES
    assert group_ids[0] not in cookie
    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None
    assert payload["vibe_group_ids"] == group_ids

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        assert conn.execute(
            select(func.count()).select_from(remote_access_authorizations)
        ).scalar_one() == 1
        conn.execute(remote_access_authorizations.delete())
    assert remote_access.parse_session_cookie(config, cookie) is None


def test_parse_session_cookie_accepts_legacy_inline_organization_claims() -> None:
    config = _config()
    issued_at = int(time.time())
    cookie = remote_access._encode_session_cookie(
        config.remote_access.vibe_cloud.session_secret,
        {
            "email": "legacy@example.com",
            "sub": "legacy-user",
            "instance_id": "inst_123",
            "iat": issued_at,
            "exp": issued_at + remote_access.SESSION_TTL_SECONDS,
            "claims_issued_at": issued_at,
            "vibe_instance_id": "inst_123",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "organization_group",
            "vibe_organization_id": "org_legacy",
            "vibe_organization_member_id": "member_legacy",
            "vibe_organization_role": "member",
            "vibe_group_ids": ["grp_legacy"],
        },
    )

    payload = remote_access.parse_session_cookie(config, cookie)
    assert payload is not None
    assert payload["vibe_group_ids"] == ["grp_legacy"]


def test_session_needs_renewal_only_after_half_ttl() -> None:
    now = 1_700_000_000
    fresh = {"exp": now + remote_access.SESSION_TTL_SECONDS}
    half_minus_one = {"exp": now + remote_access.SESSION_TTL_SECONDS // 2 - 1}

    assert remote_access.session_needs_renewal(fresh, now=now) is False
    assert remote_access.session_needs_renewal(half_minus_one, now=now) is True


def test_make_session_cookie_requires_session_secret() -> None:
    config = _config()
    config.remote_access.vibe_cloud.session_secret = ""

    with pytest.raises(ValueError, match="session secret"):
        _session_cookie(config)


def test_session_cookie_requires_signed_instance_role() -> None:
    config = _config()

    with pytest.raises(remote_access.OAuthCodeExchangeError, match="invalid_instance_role"):
        remote_access.make_session_cookie(
            config,
            "alex@example.com",
            "user-1",
            session_claims={
                "vibe_instance_id": "inst_123",
                "vibe_instance_access_source": "owner",
            },
        )


def test_authorization_claims_require_oidc_refresh() -> None:
    now = 1_700_000_000

    assert remote_access.session_authorization_refresh_deadline({"claims_issued_at": now}) == (
        now + remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS
    )
    assert remote_access.session_authorization_refresh_deadline({"claims_issued_at": "invalid"}) is None
    assert remote_access.session_needs_authorization_refresh(
        {"claims_issued_at": now}, now=now + remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS - 1
    ) is False
    assert remote_access.session_needs_authorization_refresh(
        {"claims_issued_at": now}, now=now + remote_access.SESSION_AUTHORIZATION_REFRESH_SECONDS
    ) is True


def test_exchange_oauth_code_wraps_token_endpoint_rejection(monkeypatch) -> None:
    config = _config()

    class ResponseStub:
        text = '{"error":"invalid_code"}'

        def raise_for_status(self):
            raise remote_access.requests.HTTPError("400 Client Error")

        def json(self):
            return {"error": "invalid_code"}

    monkeypatch.setattr(remote_access.requests, "post", lambda *args, **kwargs: ResponseStub())

    with pytest.raises(remote_access.OAuthCodeExchangeError) as exc_info:
        remote_access.exchange_oauth_code(config, "code-1", "verifier-1")

    assert exc_info.value.reason == "token_endpoint_rejected"
    assert exc_info.value.detail == "invalid_code"


def test_exchange_oauth_code_uses_flow_redirect_uri(monkeypatch) -> None:
    config = _config()
    captured = {}

    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def post(url, *, data, headers, timeout):
        captured.update({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return ResponseStub()

    monkeypatch.setattr(remote_access.requests, "post", post)

    with pytest.raises(remote_access.OAuthCodeExchangeError, match="missing_id_token"):
        remote_access.exchange_oauth_code(
            config,
            "code-1",
            "verifier-1",
            redirect_uri="https://max.fileguard.io/auth/callback",
        )

    assert captured["data"]["redirect_uri"] == "https://max.fileguard.io/auth/callback"


def test_oauth_code_exchange_error_string_omits_rejection_detail() -> None:
    error = remote_access.OAuthCodeExchangeError("token_endpoint_rejected", '{"code":"secret-code"}')

    assert str(error) == "token_endpoint_rejected"
    assert "secret-code" not in str(error)


def test_exchange_oauth_code_reports_instance_mismatch(monkeypatch) -> None:
    config = _config()

    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id_token": "id-token"}

    class JwkClientStub:
        def __init__(self, uri):
            self.uri = uri

        def get_signing_key_from_jwt(self, id_token):
            return type("SigningKey", (), {"key": "secret"})()

    monkeypatch.setattr(remote_access.requests, "post", lambda *args, **kwargs: ResponseStub())
    monkeypatch.setattr(remote_access, "PyJWKClient", JwkClientStub)
    monkeypatch.setattr(
        remote_access.jwt,
        "decode",
        lambda *args, **kwargs: {
            "sub": "user-1",
            "vibe_instance_id": "inst_other",
            "email_verified": True,
        },
    )

    with pytest.raises(remote_access.OAuthCodeExchangeError) as exc_info:
        remote_access.exchange_oauth_code(config, "code-1", "verifier-1")

    assert exc_info.value.reason == "invalid_instance_id"


def test_exchange_oauth_code_reports_immature_token_as_clock_mismatch(monkeypatch) -> None:
    config = _config()

    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id_token": "id-token"}

    class JwkClientStub:
        def __init__(self, uri):
            self.uri = uri

        def get_signing_key_from_jwt(self, id_token):
            return type("SigningKey", (), {"key": "secret"})()

    def decode(*args, **kwargs):
        raise remote_access.jwt.ImmatureSignatureError("The token is not yet valid")

    monkeypatch.setattr(remote_access.requests, "post", lambda *args, **kwargs: ResponseStub())
    monkeypatch.setattr(remote_access, "PyJWKClient", JwkClientStub)
    monkeypatch.setattr(remote_access.jwt, "decode", decode)

    with pytest.raises(remote_access.OAuthCodeExchangeError) as exc_info:
        remote_access.exchange_oauth_code(config, "code-1", "verifier-1")

    assert exc_info.value.reason == "immature_id_token"


@pytest.mark.parametrize(
    ("issued_at_offset", "expected_reason"),
    (
        pytest.param(30, None, id="within-leeway"),
        pytest.param(60, "immature_id_token", id="beyond-leeway"),
    ),
)
def test_exchange_oauth_code_allows_30_seconds_of_clock_skew(
    monkeypatch,
    issued_at_offset: int,
    expected_reason: str | None,
) -> None:
    config = _config()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issued_at = int(time.time()) + issued_at_offset
    id_token = remote_access.jwt.encode(
        {
            "sub": "user-1",
            "aud": config.remote_access.vibe_cloud.client_id,
            "iss": config.remote_access.vibe_cloud.issuer,
            "iat": issued_at,
            "exp": issued_at + 300,
            "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
            "vibe_instance_role": "owner",
            "vibe_instance_access_source": "owner",
            "email_verified": True,
        },
        private_key,
        algorithm="RS256",
    )

    class ResponseStub:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id_token": id_token}

    class JwkClientStub:
        def __init__(self, uri):
            self.uri = uri

        def get_signing_key_from_jwt(self, token):
            assert token == id_token
            return type("SigningKey", (), {"key": private_key.public_key()})()

    monkeypatch.setattr(remote_access.requests, "post", lambda *args, **kwargs: ResponseStub())
    monkeypatch.setattr(remote_access, "PyJWKClient", JwkClientStub)

    if expected_reason is None:
        result = remote_access.exchange_oauth_code(config, "code-1", "verifier-1")
        assert result["claims"]["sub"] == "user-1"
    else:
        with pytest.raises(remote_access.OAuthCodeExchangeError) as exc_info:
            remote_access.exchange_oauth_code(config, "code-1", "verifier-1")
        assert exc_info.value.reason == expected_reason


def test_pair_redeems_key_and_starts_connector(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.session_secret = ""
    config.save()

    def fake_request(url: str, payload: dict, timeout: float = 20.0, **kwargs):
        assert url == "https://backend.test/api/v1/pairing/redeem"
        assert payload["pairing_key"] == "vrp_test"
        assert payload["origin_service"] == "http://127.0.0.1:5123"
        assert kwargs["connection_target"].hostname == "backend.test"
        assert kwargs["connection_target"].connect_host == "93.184.216.34"
        return {
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://backend.test",
            "authorization_endpoint": "https://backend.test/oauth/authorize",
            "token_endpoint": "https://backend.test/oauth/token",
            "jwks_uri": "https://backend.test/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
        }

    monkeypatch.setattr(remote_access, "_json_request", fake_request)
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True, "running": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": True, "paired": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})
    refreshes: list[bool] = []
    monkeypatch.setattr(
        model_service,
        "request_model_service_refresh",
        lambda: refreshes.append(True),
    )

    result = remote_access.pair("vrp_test", "https://backend.test")
    saved_payload = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["pairing"]["ok"] is True
    assert result["start"]["ok"] is True
    assert saved_payload["remote_access"]["vibe_cloud"]["enabled"] is True
    assert saved_payload["remote_access"]["vibe_cloud"]["tunnel_token"] == "tunnel-token"
    assert saved_payload["remote_access"]["vibe_cloud"]["session_secret"]
    assert refreshes == [True]


def test_disabling_pairing_requests_an_immediate_model_service_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.test"
    cloud.instance_id = "inst_123"
    cloud.instance_secret = "instance-secret"
    config.save()
    refreshes: list[bool] = []
    monkeypatch.setattr(
        model_service,
        "request_model_service_refresh",
        lambda: refreshes.append(True),
    )

    saved = api.save_config(
        {"remote_access": {"vibe_cloud": {"enabled": False}}},
        validate_remote_access_network=False,
    )

    assert saved.remote_access.vibe_cloud.runtime_credentials() is None
    assert refreshes == [True]


def test_pair_origin_service_follows_effective_ui_port(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_UI_PORT", "15130")
    config = _config()
    config.ui.setup_host = "0.0.0.0"
    config.ui.setup_port = 5123
    config.save()

    assert remote_access.origin_service_for_pairing() == "http://127.0.0.1:15130"


@pytest.mark.parametrize(
    ("backend_url", "expected_error"),
    [
        ("http://avibe.bot", "invalid_pairing_backend_url"),
        ("https://[::1", "invalid_pairing_backend_url"),
        ("https://127.0.0.1", "pairing_backend_url_not_allowed"),
        ("https://[::1]", "pairing_backend_url_not_allowed"),
        ("https://10.0.0.5", "pairing_backend_url_not_allowed"),
        ("https://192.168.1.5", "pairing_backend_url_not_allowed"),
        ("https://100.64.0.1", "pairing_backend_url_not_allowed"),
        ("https://169.254.169.254", "pairing_backend_url_not_allowed"),
        ("https://metadata.google.internal", "pairing_backend_url_not_allowed"),
    ],
)
def test_pair_rejects_unsafe_backend_urls_without_request(monkeypatch, backend_url: str, expected_error: str) -> None:
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsafe backend must not be requested")),
    )

    result = remote_access.pair("vrp_test", backend_url)

    assert result == {"ok": False, "error": expected_error}


def test_pair_rejects_backend_hostname_that_resolves_private(monkeypatch) -> None:
    monkeypatch.setattr(
        remote_access,
        "_resolve_pairing_backend_addresses",
        lambda hostname, port: {ipaddress.ip_address("10.0.0.5")},
    )
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private backend must not be requested")),
    )

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result == {"ok": False, "error": "pairing_backend_url_not_allowed"}


def test_pair_uses_validated_backend_address_for_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    captured: dict[str, str] = {}

    def fake_request(url: str, payload: dict, timeout: float = 20.0, **kwargs):
        target = kwargs["connection_target"]
        captured["url"] = url
        captured["hostname"] = target.hostname
        captured["host_header"] = target.host_header
        captured["connect_host"] = target.connect_host
        return {
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://backend.test",
            "authorization_endpoint": "https://backend.test/oauth/authorize",
            "token_endpoint": "https://backend.test/oauth/token",
            "jwks_uri": "https://backend.test/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
        }

    monkeypatch.setattr(remote_access, "_json_request", fake_request)
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True, "running": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": True, "paired": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is True
    assert captured == {
        "url": "https://backend.test/api/v1/pairing/redeem",
        "hostname": "backend.test",
        "host_header": "backend.test",
        "connect_host": "93.184.216.34",
    }


def test_pair_preserves_proxy_only_dns_for_default_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    captured: dict[str, object] = {}

    def fake_request(url: str, payload: dict, timeout: float = 20.0, **kwargs):
        target = kwargs["connection_target"]
        captured["url"] = url
        captured["hostname"] = target.hostname
        captured["connect_host"] = target.connect_host
        captured["requires_proxy"] = target.requires_proxy
        return {
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://avibe.bot",
            "authorization_endpoint": "https://avibe.bot/oauth/authorize",
            "token_endpoint": "https://avibe.bot/oauth/token",
            "jwks_uri": "https://avibe.bot/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
        }

    monkeypatch.setattr(remote_access, "_json_request", fake_request)
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True, "running": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": True, "paired": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    result = remote_access.pair("vrp_test", "https://avibe.bot")

    assert result["ok"] is True
    assert captured == {
        "url": "https://avibe.bot/api/v1/pairing/redeem",
        "hostname": "avibe.bot",
        "connect_host": "avibe.bot",
        "requires_proxy": True,
    }


def test_pair_rejects_custom_proxy_only_dns_backend(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unresolved backend must not be requested")),
    )

    result = remote_access.pair("vrp_test", "https://custom-backend.example")

    assert result == {"ok": False, "error": "pairing_backend_unresolvable"}


def test_validated_backend_request_connects_to_pinned_ip_without_hostname_dns(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(remote_access, "_validated_backend_proxy_url", lambda _url: None)

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"ok": true}'

    class FakeConnection:
        def __init__(self, host: str, port: int, *, server_hostname: str, timeout: float, context):
            captured["host"] = host
            captured["port"] = port
            captured["server_hostname"] = server_hostname
            captured["timeout"] = timeout
            captured["context"] = context

        def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            captured["headers"] = headers

        def getresponse(self):
            return FakeResponse()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(remote_access, "_PinnedHTTPSConnection", FakeConnection)
    target = remote_access._ValidatedPairingBackend(
        base_url="https://backend.test",
        hostname="backend.test",
        port=443,
        host_header="backend.test",
        connect_hosts=("93.184.216.34",),
    )

    result = remote_access._json_request_to_validated_backend(
        "https://backend.test/api/v1/pairing/redeem",
        {"pairing_key": "vrp_test"},
        target,
        timeout=3.0,
    )

    assert result == {"ok": True}
    assert captured["host"] == "93.184.216.34"
    assert captured["server_hostname"] == "backend.test"
    assert captured["headers"]["Host"] == "backend.test"
    assert captured["path"] == "/api/v1/pairing/redeem"
    assert captured["closed"] is True


def test_validated_backend_request_retries_next_pinned_address(monkeypatch) -> None:
    attempts: list[str] = []
    monkeypatch.setattr(remote_access, "_validated_backend_proxy_url", lambda _url: None)

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"ok": true}'

    class FakeConnection:
        def __init__(self, host: str, port: int, *, server_hostname: str, timeout: float, context):
            self.host = host
            attempts.append(host)

        def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
            if self.host == "93.184.216.34":
                raise OSError("first address unavailable")

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(remote_access, "_PinnedHTTPSConnection", FakeConnection)
    target = remote_access._ValidatedPairingBackend(
        base_url="https://backend.test",
        hostname="backend.test",
        port=443,
        host_header="backend.test",
        connect_hosts=("93.184.216.34", "93.184.216.35"),
    )

    result = remote_access._json_request_to_validated_backend(
        "https://backend.test/api/v1/pairing/redeem",
        {"pairing_key": "vrp_test"},
        target,
        timeout=3.0,
    )

    assert result == {"ok": True}
    assert attempts == ["93.184.216.34", "93.184.216.35"]


def test_validated_backend_request_uses_https_proxy_connect_to_pinned_ip(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@proxy.test:8080")
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"ok": true}'

    class FakeProxyConnection:
        def __init__(
            self,
            proxy_host: str,
            proxy_port: int,
            *,
            proxy_scheme: str,
            connect_host: str,
            connect_port: int,
            server_hostname: str,
            proxy_headers: dict[str, str] | None,
            timeout: float,
            context,
            proxy_context=None,
        ):
            captured["proxy_host"] = proxy_host
            captured["proxy_port"] = proxy_port
            captured["proxy_scheme"] = proxy_scheme
            captured["connect_host"] = connect_host
            captured["connect_port"] = connect_port
            captured["server_hostname"] = server_hostname
            captured["proxy_headers"] = proxy_headers
            captured["timeout"] = timeout
            captured["context"] = context

        def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
            captured["method"] = method
            captured["path"] = path
            captured["headers"] = headers

        def getresponse(self):
            return FakeResponse()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(remote_access, "_PinnedHTTPSProxyConnection", FakeProxyConnection)
    monkeypatch.setattr(
        remote_access,
        "_PinnedHTTPSConnection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("proxy path must not direct-connect")),
    )
    target = remote_access._ValidatedPairingBackend(
        base_url="https://backend.test",
        hostname="backend.test",
        port=443,
        host_header="backend.test",
        connect_hosts=("93.184.216.34",),
    )

    result = remote_access._json_request_to_validated_backend(
        "https://backend.test/api/v1/pairing/redeem",
        {"pairing_key": "vrp_test"},
        target,
        timeout=3.0,
    )

    assert result == {"ok": True}
    assert captured["proxy_host"] == "proxy.test"
    assert captured["proxy_port"] == 8080
    assert captured["proxy_scheme"] == "http"
    assert captured["connect_host"] == "93.184.216.34"
    assert captured["connect_port"] == 443
    assert captured["server_hostname"] == "backend.test"
    assert captured["proxy_headers"] == {"Proxy-Authorization": "Basic dXNlcjpwYXNz"}
    assert captured["headers"]["Host"] == "backend.test"
    assert captured["closed"] is True


def test_validated_backend_connection_loads_requests_ca_bundle(monkeypatch, tmp_path) -> None:
    ca_bundle = tmp_path / "corp-ca.pem"
    ca_bundle.write_text("test ca", encoding="utf-8")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_bundle))
    captured: dict[str, object] = {}

    class FakeContext:
        pass

    class FakeConnection:
        def __init__(self, host: str, port: int, *, server_hostname: str, timeout: float, context):
            captured["host"] = host
            captured["context"] = context

    def fake_create_default_context(*, cafile=None, capath=None):
        captured["cafile"] = cafile
        captured["capath"] = capath
        return FakeContext()

    monkeypatch.setattr(remote_access.ssl, "create_default_context", fake_create_default_context)
    monkeypatch.setattr(remote_access, "_PinnedHTTPSConnection", FakeConnection)
    target = remote_access._ValidatedPairingBackend(
        base_url="https://backend.test",
        hostname="backend.test",
        port=443,
        host_header="backend.test",
        connect_hosts=("93.184.216.34",),
    )

    connection = remote_access._validated_backend_connection("93.184.216.34", target, 3.0, None)

    assert isinstance(connection, FakeConnection)
    assert captured["host"] == "93.184.216.34"
    assert captured["cafile"] == str(ca_bundle)
    assert captured["capath"] is None
    assert captured["context"].__class__ is FakeContext


def test_json_request_disables_redirects(monkeypatch) -> None:
    calls = []

    class RedirectResponse:
        status_code = 302
        text = ""

        def raise_for_status(self):
            raise AssertionError("redirects must be blocked before status handling")

        def json(self):
            return {}

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        return RedirectResponse()

    monkeypatch.setattr(remote_access.requests, "post", fake_post)

    with pytest.raises(remote_access.BackendRequestError) as exc_info:
        remote_access._json_request("https://backend.test/api/v1/pairing/redeem", {})

    assert exc_info.value.status == 302
    assert exc_info.value.payload["error"] == "backend_http_redirect_blocked"
    assert calls[0]["allow_redirects"] is False


def test_pair_origin_service_ignores_configured_ui_host(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.ui.setup_host = "192.168.2.3"
    config.ui.setup_port = 15130
    config.save()

    assert remote_access.origin_service_for_pairing() == "http://127.0.0.1:15130"


def test_pair_origin_service_uses_ipv4_loopback_when_localhost_resolves_dual_stack(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "resolve_localhost_family", lambda: "inet")
    config = _config()
    config.ui.setup_host = "localhost"
    config.ui.setup_port = 15130
    config.save()

    # cloudflared and the UI server each resolve "localhost" independently, so we
    # hand cloudflared a literal IPv4 loopback to match the bind family and
    # avoid the ::1 vs 127.0.0.1 race that surfaces as a 502.
    assert remote_access.origin_service_for_pairing() == "http://127.0.0.1:15130"


def test_pair_origin_service_uses_ipv6_loopback_when_localhost_resolves_v6_only(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(runtime, "resolve_localhost_family", lambda: "inet6")
    config = _config()
    config.ui.setup_host = "localhost"
    config.ui.setup_port = 15130
    config.save()

    # On IPv6-only hosts where ``localhost`` only resolves to ::1, the
    # cloudflared origin must follow into v6 so it can reach the v6
    # wildcard bind. Otherwise the tunnel dials an unreachable v4 socket.
    assert remote_access.origin_service_for_pairing() == "http://[::1]:15130"


def test_pair_origin_service_preserves_ipv6_loopback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.ui.setup_host = "::1"
    config.ui.setup_port = 15130
    config.save()

    assert remote_access.origin_service_for_pairing() == "http://[::1]:15130"


def test_pair_origin_service_preserves_bracketed_ipv6_loopback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.ui.setup_host = "[::1]"
    config.ui.setup_port = 15130
    config.save()

    assert remote_access.origin_service_for_pairing() == "http://[::1]:15130"


def test_pair_origin_service_uses_ipv6_loopback_for_ipv6_wildcard(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.ui.setup_host = "::"
    config.ui.setup_port = 15130
    config.save()

    assert remote_access.origin_service_for_pairing() == "http://[::1]:15130"


def test_ra_tq_007_runtime_status_payload_includes_tunnel_quality(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.ui.setup_host = "100.97.103.112"
    config.save()
    monkeypatch.setattr(remote_access, "_local_ui_healthy", lambda cfg: True)
    monkeypatch.setattr(remote_access, "_observed_cloudflared_origin_service", lambda: "http://100.97.103.112:5123")
    monkeypatch.setattr(
        remote_access,
        "status",
        lambda cfg=None: {
            "ok": True,
            "running": True,
            "binary_found": True,
            "network_path": {
                "schema_version": 1,
                "connector": {"edge_ips": ["198.41.192.47"]},
            },
            "settings": {"edge_bind_address": "192.0.2.99"},
            "tunnel_quality": {
                "schema_version": 1,
                "state": "healthy",
                "grade": "good",
                "sampled_at": "2026-07-15T03:22:00Z",
            },
        },
    )

    payload = remote_access.runtime_status_payload(config, event="heartbeat")

    assert payload["event"] == "heartbeat"
    assert payload["ui_healthy"] is True
    assert payload["tunnel_running"] is True
    assert payload["cloudflared_found"] is True
    assert payload["expected_origin_service"] == "http://127.0.0.1:5123"
    assert payload["observed_origin_service"] == "http://100.97.103.112:5123"
    assert payload["tunnel_quality"]["grade"] == "good"
    assert "network_path" not in payload
    assert "198.41.192.47" not in json.dumps(payload)
    assert "settings" not in payload
    assert "192.0.2.99" not in json.dumps(payload)


def test_ra_tq_014_runtime_status_payload_includes_v2_request_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.save()
    monkeypatch.setattr(remote_access, "_local_ui_healthy", lambda cfg: True)
    monkeypatch.setattr(remote_access, "_observed_cloudflared_origin_service", lambda: "http://127.0.0.1:5123")
    monkeypatch.setattr(
        remote_access,
        "status",
        lambda cfg=None: {
            "ok": True,
            "running": True,
            "binary_found": True,
            "tunnel_quality": {
                "schema_version": 2,
                "state": "degraded",
                "grade": "critical",
                "sampled_at": "2026-08-03T12:45:00Z",
                "request_path": {
                    "confidence": "high",
                    "latency_ms": {"p50": 202, "p95": 1100, "p99": 2300, "max": 2700},
                },
            },
        },
    )

    payload = remote_access.runtime_status_payload(config, event="tunnel_quality")

    assert payload["tunnel_quality"]["schema_version"] == 2
    assert payload["tunnel_quality"]["request_path"]["latency_ms"]["p95"] == 1100
def test_runtime_status_payload_omits_unknown_observed_origin(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    monkeypatch.setattr(remote_access, "_local_ui_healthy", lambda cfg: True)
    monkeypatch.setattr(remote_access, "status", lambda cfg: {"running": True, "binary_found": True})
    monkeypatch.setattr(remote_access, "_observed_cloudflared_origin_service", lambda: None)

    payload = remote_access.runtime_status_payload(config, event="heartbeat")

    assert "observed_origin_service" not in payload


def test_observed_cloudflared_origin_service_reads_only_log_tail(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    remote_access._cloudflared_stderr_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._cloudflared_stderr_path().write_bytes(
        b'originService=http://old.local:5123\n'
        + (b"x" * (remote_access.STATUS_LOG_TAIL_BYTES + 1024))
        + b'originService=http://new.local:5123\n'
    )
    monkeypatch.setattr(remote_access.Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full read")))

    assert remote_access._observed_cloudflared_origin_service() == "http://new.local:5123"


def test_observed_cloudflared_origin_service_uses_latest_mixed_log_format(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    remote_access._cloudflared_stderr_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._cloudflared_stderr_path().write_text(
        'ERR originService=http://100.97.103.112:5123\n'
        'INF Updated to new configuration config="{\\"ingress\\":[{\\"service\\":\\"http://127.0.0.1:5123\\"}]}"\n',
        encoding="utf-8",
    )

    assert remote_access._observed_cloudflared_origin_service() == "http://127.0.0.1:5123"


def test_report_runtime_status_posts_to_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    config.save()
    monkeypatch.setattr(remote_access, "_local_ui_healthy", lambda cfg: True)
    monkeypatch.setattr(remote_access, "_observed_cloudflared_origin_service", lambda: "http://127.0.0.1:5123")
    monkeypatch.setattr(remote_access, "status", lambda cfg=None: {"ok": True, "running": False, "binary_found": True})
    calls = []

    def fake_request(url: str, payload: dict, timeout: float = 20.0):
        calls.append((url, payload, timeout))
        return {"ok": True}

    monkeypatch.setattr(remote_access, "_json_request", fake_request)

    result = remote_access.report_runtime_status(config, event="stop")

    assert result["ok"] is True
    assert calls == [
        (
            "https://backend.test/api/v1/instances/inst_123/runtime-status",
            {
                "instance_secret": "instance-secret",
                "event": "stop",
                "local_version": "dev",
                "ui_healthy": True,
                "tunnel_running": False,
                "cloudflared_found": True,
                "expected_origin_service": "http://127.0.0.1:5123",
                "observed_origin_service": "http://127.0.0.1:5123",
            },
            5.0,
        )
    ]


def test_report_runtime_status_replaces_normalized_active_hostnames(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(remote_access.time, "time", lambda: 1_700_000_000.25)
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {
            "ok": True,
            "active_hostnames": [
                " Max-App.Avibe.Tech ",
                "MAX.FILEGUARD.IO.",
                "max.fileguard.io",
                "bad.example:443",
                "https://evil.example",
                "",
                "   ",
                7,
                None,
            ],
        },
    )

    result = remote_access.report_runtime_status(config)
    persisted = json.loads(remote_access._active_hostnames_state_path().read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert remote_access.active_hostnames(config) == frozenset({"max-app.avibe.tech", "max.fileguard.io"})
    assert persisted == {
        "schema_version": 1,
        "instance_id": "inst_123",
        "active_hostnames": ["max-app.avibe.tech", "max.fileguard.io"],
        "source_updated_at": 1_700_000_000.25,
    }


def test_report_runtime_status_backfills_instance_kind_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    config.save()
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {"ok": True, "instance_kind": "organization"},
    )
    migration_calls = []
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: migration_calls.append(True),
    )
    real_save_config = remote_access.api.save_config
    saves = []

    def tracked_save_config(payload, **kwargs):
        saves.append(payload)
        return real_save_config(payload, **kwargs)

    monkeypatch.setattr(remote_access.api, "save_config", tracked_save_config)

    assert remote_access.report_runtime_status(config)["ok"] is True
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "organization"
    assert remote_access.report_runtime_status(V2Config.load())["ok"] is True
    assert migration_calls == [True]
    assert saves == [
        {"remote_access": {"vibe_cloud": {"instance_kind": "organization"}}}
    ]


def test_report_runtime_status_binds_pending_deferred_contexts_through_the_real_path(
    monkeypatch,
    tmp_path,
) -> None:
    """A long-running upgraded service must not need another initialization.

    The heartbeat is the only place the authoritative kind arrives, so it owns
    rerunning the pending migration itself rather than relying on a later
    ``ensure_sqlite_state()`` call.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    cloud.instance_kind = ""
    config.save()

    from storage.db import get_cached_sqlite_engine
    from storage.importer import _run_sqlite_data_migrations, ensure_sqlite_state
    from storage.models import run_definitions, state_meta
    from storage.resource_access_service import (
        LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
        RESOURCE_USER_CONTEXT_METADATA_KEY,
        resource_user_context_from_metadata,
    )

    legacy_metadata = {
        RESOURCE_USER_CONTEXT_METADATA_KEY: {
            "sub": "legacy-editor",
            "vibe_instance_role": "editor",
            "vibe_instance_access_source": "email",
            "claims_issued_at": 1_700_000_000,
        }
    }
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as connection:
        connection.execute(
            run_definitions.insert().values(
                id="legacy-task",
                definition_type="scheduled",
                name="legacy task",
                message="run",
                schedule_type="interval",
                enabled=1,
                created_at="2026-08-20T00:00:00Z",
                updated_at="2026-08-20T00:00:00Z",
                metadata_json=json.dumps(legacy_metadata),
            )
        )
        counts = _run_sqlite_data_migrations(connection)
        assert {
            key: counts[key]
            for key in (
                "legacy_deferred_definitions",
                "legacy_deferred_runs",
                "legacy_deferred_deliveries",
            )
        } == {
            "legacy_deferred_definitions": 0,
            "legacy_deferred_runs": 0,
            "legacy_deferred_deliveries": 0,
        }
        marker = json.loads(
            connection.execute(
                select(state_meta.c.value_json).where(
                    state_meta.c.key == LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY
                )
            ).scalar_one()
        )
    assert marker["state"] == "pending"
    assert marker["instance_id"] == "inst_123"

    monkeypatch.setattr(
        remote_access,
        "runtime_status_payload",
        lambda *args, **kwargs: {"event": "heartbeat"},
    )
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {"ok": True, "instance_kind": "personal"},
    )

    assert remote_access.report_runtime_status(config)["ok"] is True

    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "personal"
    with engine.connect() as connection:
        metadata = json.loads(
            connection.execute(
                select(run_definitions.c.metadata_json).where(
                    run_definitions.c.id == "legacy-task"
                )
            ).scalar_one()
        )
    snapshot = metadata[RESOURCE_USER_CONTEXT_METADATA_KEY]
    assert snapshot["vibe_instance_id"] == "inst_123"
    assert snapshot["vibe_instance_kind"] == "personal"
    assert resource_user_context_from_metadata(metadata) is not None


@pytest.mark.parametrize("reported_kind", [None, "enterprise", "", 7])
def test_report_runtime_status_ignores_invalid_instance_kind(
    monkeypatch,
    tmp_path,
    reported_kind,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    cloud.instance_kind = "personal"
    config.save()
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {"ok": True, "instance_kind": reported_kind},
    )
    monkeypatch.setattr(
        remote_access.api,
        "save_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("invalid kind must not persist")),
    )

    assert remote_access.report_runtime_status(config)["ok"] is True
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "personal"


def test_report_runtime_status_does_not_backfill_a_replaced_instance(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    config.save()
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})

    def replace_instance_before_response(*args, **kwargs):
        current = V2Config.load()
        current.remote_access.vibe_cloud.instance_id = "inst_replacement"
        current.save()
        return {"ok": True, "instance_kind": "organization"}

    monkeypatch.setattr(remote_access, "_json_request", replace_instance_before_response)
    monkeypatch.setattr(
        remote_access.api,
        "save_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale heartbeat must not persist")),
    )

    assert remote_access.report_runtime_status(config)["ok"] is True
    persisted = V2Config.load().remote_access.vibe_cloud
    assert persisted.instance_id == "inst_replacement"
    assert persisted.instance_kind == ""


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"ok": True}, id="legacy-response"),
        pytest.param({"ok": True, "active_hostnames": "max.fileguard.io"}, id="invalid-type"),
    ],
)
def test_report_runtime_status_missing_or_invalid_hostnames_clears_snapshot(
    monkeypatch,
    tmp_path,
    response,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: response)

    result = remote_access.report_runtime_status(config)
    persisted = json.loads(remote_access._active_hostnames_state_path().read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert remote_access.active_hostnames(config) == frozenset()
    assert persisted["active_hostnames"] == []


def test_report_runtime_status_does_not_consume_hostnames_from_http_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "http://backend.test"
    cloud.instance_secret = "instance-secret"
    calls = []
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: calls.append(args))

    result = remote_access.report_runtime_status(config)

    assert result == {"ok": False, "error": "remote_status_backend_url_invalid"}
    assert calls == []
    assert remote_access.active_hostnames(config) == frozenset()


def test_report_runtime_status_posts_when_remote_access_is_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = False
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    config.save()
    monkeypatch.setattr(remote_access, "_local_ui_healthy", lambda cfg: True)
    monkeypatch.setattr(remote_access, "_observed_cloudflared_origin_service", lambda: None)
    monkeypatch.setattr(remote_access, "status", lambda cfg=None: {"ok": True, "running": False, "binary_found": True})
    calls = []

    def fake_request(url: str, payload: dict, timeout: float = 20.0):
        calls.append((url, payload, timeout))
        return {"ok": True}

    monkeypatch.setattr(remote_access, "_json_request", fake_request)

    result = remote_access.report_runtime_status(config, event="stop")

    assert result["ok"] is True
    assert calls[0][0] == "https://backend.test/api/v1/instances/inst_123/runtime-status"
    assert calls[0][1]["event"] == "stop"
    assert calls[0][1]["tunnel_running"] is False


def test_pair_persists_with_locked_incremental_config_save(monkeypatch) -> None:
    config = _config()
    save_payloads = []

    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {
            "instance_id": "inst_123",
            "instance_kind": "organization",
            "client_id": "vr_client_123",
            "issuer": "https://backend.test",
            "authorization_endpoint": "https://backend.test/oauth/authorize",
            "token_endpoint": "https://backend.test/oauth/token",
            "jwks_uri": "https://backend.test/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
        },
    )
    monkeypatch.setattr(remote_access.api, "save_config", lambda payload: save_payloads.append(payload) or config)
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: {"legacy_deferred_definitions": 0, "legacy_deferred_runs": 0, "legacy_deferred_deliveries": 0, "binding_status": "sealed"},
    )
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True, "running": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": True, "paired": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is True
    assert save_payloads
    assert set(save_payloads[0]) == {"remote_access"}
    cloud_payload = save_payloads[0]["remote_access"]["vibe_cloud"]
    assert cloud_payload["enabled"] is True
    assert cloud_payload["instance_kind"] == "organization"
    assert cloud_payload["tunnel_token"] == "tunnel-token"
    assert cloud_payload["session_secret"]


@pytest.mark.parametrize("reported_kind", [None, "enterprise"])
def test_pair_accepts_legacy_or_invalid_instance_kind_as_unknown(monkeypatch, reported_kind) -> None:
    config = _config()
    save_payloads = []
    response = {
        "instance_id": "inst_456",
        "client_id": "vr_client_456",
        "issuer": "https://backend.test",
        "authorization_endpoint": "https://backend.test/oauth/authorize",
        "token_endpoint": "https://backend.test/oauth/token",
        "jwks_uri": "https://backend.test/oauth/jwks.json",
        "public_url": "https://new.avibe.bot",
        "redirect_uri": "https://new.avibe.bot/auth/callback",
        "tunnel_token": "tunnel-token",
        "instance_secret": "instance-secret",
    }
    if reported_kind is not None:
        response["instance_kind"] = reported_kind
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: response)

    def fake_save_config(payload, **kwargs):
        save_payloads.append(payload)
        # Mirror the real save: the returned config carries the persisted
        # pairing identity, which pair() verifies before publishing a binding.
        config.remote_access.vibe_cloud.instance_id = payload["remote_access"]["vibe_cloud"]["instance_id"]
        return config

    monkeypatch.setattr(remote_access.api, "save_config", fake_save_config)
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: {"legacy_deferred_definitions": 0, "legacy_deferred_runs": 0, "legacy_deferred_deliveries": 0, "binding_status": "sealed"},
    )
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    assert remote_access.pair("vrp_test", "https://backend.test")["ok"] is True
    assert save_payloads[0]["remote_access"]["vibe_cloud"]["instance_kind"] == ""


def test_session_projects_instance_kind_for_local_and_authenticated_remote(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.instance_kind = "organization"
    config.save()

    local = ui_server.app.test_client().get(
        "/api/session",
        base_url="http://localhost",
    )
    assert local.get_json()["instance_kind"] == "organization"

    remote = ui_server.app.test_client()
    remote.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _session_cookie(config),
        domain="alex.avibe.bot",
    )
    authenticated = remote.get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    )
    assert authenticated.get_json()["instance_kind"] == "organization"

    unauthenticated = ui_server.app.test_client().get(
        "/api/session",
        base_url="https://alex.avibe.bot",
    )
    assert "instance_kind" not in unauthenticated.get_json()


def test_pair_reports_success_when_connector_start_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://backend.test",
            "authorization_endpoint": "https://backend.test/oauth/authorize",
            "token_endpoint": "https://backend.test/oauth/token",
            "jwks_uri": "https://backend.test/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
        },
    )
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": False, "error": "cloudflared_spawn_failed"})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": False, "paired": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    result = remote_access.pair("vrp_test", "https://backend.test")
    saved_payload = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["pairing"]["ok"] is True
    assert result["start"]["ok"] is False
    assert result["start"]["error"] == "cloudflared_spawn_failed"
    assert saved_payload["remote_access"]["vibe_cloud"]["tunnel_token"] == "tunnel-token"


def _pair_redeem_response(*, instance_id: str = "inst_new") -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "instance_kind": "personal",
        "client_id": "vr_client_new",
        "issuer": "https://backend.test",
        "authorization_endpoint": "https://backend.test/oauth/authorize",
        "token_endpoint": "https://backend.test/oauth/token",
        "jwks_uri": "https://backend.test/oauth/jwks.json",
        "public_url": "https://new.avibe.bot",
        "redirect_uri": "https://new.avibe.bot/auth/callback",
        "tunnel_token": "new-tunnel-token",
        "instance_secret": "new-instance-secret",
    }


def test_pair_aborts_when_legacy_provenance_cannot_be_read(monkeypatch, tmp_path) -> None:
    """An unavailable pre-pair config read must not replace the pairing."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.instance_id = ""
    config.remote_access.vibe_cloud.instance_secret = ""
    config.save()
    original = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))

    redeem_calls: list[str] = []

    def fake_request(url: str, payload: dict, timeout: float = 20.0, **kwargs):
        redeem_calls.append(url)
        return _pair_redeem_response()

    monkeypatch.setattr(remote_access, "_json_request", fake_request)
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: (_ for _ in ()).throw(RuntimeError("legacy_deferred_context_provenance_unavailable")),
    )
    saves: list[dict] = []
    monkeypatch.setattr(
        remote_access.api,
        "save_config",
        lambda payload, **kwargs: saves.append(payload) or config,
    )
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True})

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is False
    assert result["error"] == "pairing_provenance_unavailable"
    assert saves == []
    assert redeem_calls == []
    assert json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8")) == original


def test_pair_aborts_when_pending_migration_fails(monkeypatch, tmp_path) -> None:
    """A failing deferred migration must not let pair() stamp a new identity."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.remote_access.vibe_cloud.instance_id = ""
    config.save()
    original = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))

    redeem_calls: list[str] = []

    def fake_request(url: str, payload: dict, timeout: float = 20.0, **kwargs):
        redeem_calls.append(url)
        return _pair_redeem_response()

    monkeypatch.setattr(remote_access, "_json_request", fake_request)
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: (_ for _ in ()).throw(RuntimeError("sqlite locked")),
    )
    saves: list[dict] = []
    monkeypatch.setattr(
        remote_access.api,
        "save_config",
        lambda payload, **kwargs: saves.append(payload) or config,
    )

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is False
    assert result["error"] == "pairing_provenance_unavailable"
    assert saves == []
    assert redeem_calls == []
    assert json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8")) == original


def test_pair_redeems_when_config_file_is_absent(monkeypatch, tmp_path) -> None:
    """A fresh install with no config is authoritative unpaired, not unavailable."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config_path = tmp_path / "config" / "config.json"
    if config_path.exists():
        config_path.unlink()

    redeem_calls: list[str] = []

    def fake_request(url: str, payload: dict, timeout: float = 20.0, **kwargs):
        redeem_calls.append(url)
        return _pair_redeem_response()

    monkeypatch.setattr(remote_access, "_json_request", fake_request)
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True, "running": True})
    monkeypatch.setattr(
        remote_access,
        "status",
        lambda next_config=None: {"ok": True, "running": True, "paired": True},
    )
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(remote_access, "_transition_instance_binding", lambda **kwargs: {"ok": True, "ready": True})

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is True
    assert redeem_calls
    assert "/pairing/redeem" in redeem_calls[0]


def test_binding_transition_initializes_sqlite_before_taking_config_lock(monkeypatch, tmp_path) -> None:
    """Lock order: ensure_sqlite_state must complete before config_file_lock."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _config().save()
    order: list[str] = []
    real_ensure = __import__("storage.importer", fromlist=["ensure_sqlite_state"]).ensure_sqlite_state
    real_lock = __import__("config.v2_config", fromlist=["config_file_lock"]).config_file_lock

    def tracking_ensure(*args, **kwargs):
        order.append("sqlite")
        return real_ensure(*args, **kwargs)

    from contextlib import contextmanager

    @contextmanager
    def tracking_lock(*args, **kwargs):
        order.append("config")
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr("storage.importer.ensure_sqlite_state", tracking_ensure)
    monkeypatch.setattr("config.v2_config.config_file_lock", tracking_lock)
    monkeypatch.setattr(
        "storage.remote_access_authorization_service._ensure_sqlite_state",
        lambda: tracking_ensure(),
    )

    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="personal",
    )

    assert "sqlite" in order
    assert "config" in order
    assert order.index("sqlite") < order.index("config")



def test_pair_rejects_origin_update_failure_before_saving_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://backend.test",
            "authorization_endpoint": "https://backend.test/oauth/authorize",
            "token_endpoint": "https://backend.test/oauth/token",
            "jwks_uri": "https://backend.test/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
            "tunnel_origin_update": {"ok": False, "error": "tunnel_origin_update_failed"},
        },
    )
    monkeypatch.setattr(
        remote_access.api,
        "save_config",
        lambda payload: (_ for _ in ()).throw(AssertionError("failed origin update must not persist pairing")),
    )
    monkeypatch.setattr(
        remote_access,
        "start",
        lambda next_config: (_ for _ in ()).throw(AssertionError("failed origin update must not start tunnel")),
    )

    result = remote_access.pair("vrp_test", "https://backend.test")
    saved_payload = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))

    assert result["ok"] is False
    assert result["error"] == "tunnel_origin_update_failed"
    assert saved_payload["remote_access"]["vibe_cloud"]["enabled"] is False


def test_pair_returns_structured_error_when_backend_request_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: {"legacy_deferred_definitions": 0, "legacy_deferred_runs": 0, "legacy_deferred_deliveries": 0, "binding_status": "sealed"},
    )
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is False
    assert result["error"] == "pairing_request_failed"
    assert "offline" in result["detail"]


def test_pair_preserves_backend_error_response(monkeypatch) -> None:
    def fake_request(*args, **kwargs):
        raise remote_access.BackendRequestError(400, {"error": "invalid_pairing_key"})

    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: {"legacy_deferred_definitions": 0, "legacy_deferred_runs": 0, "legacy_deferred_deliveries": 0, "binding_status": "sealed"},
    )
    monkeypatch.setattr(remote_access, "_json_request", fake_request)

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result == {"ok": False, "error": "invalid_pairing_key", "status": 400}


def test_pair_queues_lifecycle_status_for_drain(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    reports = []

    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {
            "instance_id": "inst_123",
            "client_id": "vr_client_123",
            "issuer": "https://backend.test",
            "authorization_endpoint": "https://backend.test/oauth/authorize",
            "token_endpoint": "https://backend.test/oauth/token",
            "jwks_uri": "https://backend.test/oauth/jwks.json",
            "public_url": "https://alex.avibe.bot",
            "redirect_uri": "https://alex.avibe.bot/auth/callback",
            "tunnel_token": "tunnel-token",
            "instance_secret": "instance-secret",
        },
    )
    monkeypatch.setattr(remote_access.api, "save_config", lambda payload: config)
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: {"legacy_deferred_definitions": 0, "legacy_deferred_runs": 0, "legacy_deferred_deliveries": 0, "binding_status": "sealed"},
    )
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": False, "error": "cloudflared_spawn_failed"})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": False, "paired": True})
    monkeypatch.setattr(
        remote_access,
        "report_runtime_status",
        lambda cfg, event="heartbeat", last_error=None: reports.append((event, last_error)) or {"ok": True},
    )

    result = remote_access.pair("vrp_test", "https://backend.test")
    remote_access.drain_runtime_status_reports(timeout_seconds=1.0)

    assert result["ok"] is True
    assert reports == [("pair", "cloudflared_spawn_failed")]


def test_lifecycle_status_report_does_not_block_stop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    started = threading.Event()
    release = threading.Event()

    def blocking_report(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return {"ok": True}

    monkeypatch.setattr(remote_access, "report_runtime_status", blocking_report)

    before = time.monotonic()
    result = remote_access.stop(_config())
    elapsed = time.monotonic() - before

    assert result["ok"] is True
    assert elapsed < 0.5
    assert started.wait(timeout=1)
    assert remote_access._CONNECTOR_LOCK.acquire(blocking=False)
    remote_access._CONNECTOR_LOCK.release()
    release.set()
    remote_access.drain_runtime_status_reports(timeout_seconds=1.0)


def test_async_runtime_status_reporter_serializes_and_coalesces(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    events = []

    def blocking_report(config=None, event="heartbeat", last_error=None):
        events.append(event)
        if len(events) == 1:
            started.set()
            release.wait(timeout=2)
        return {"ok": True}

    with remote_access._STATUS_REPORT_LOCK:
        remote_access._STATUS_REPORT_THREADS.clear()
        remote_access._STATUS_REPORT_PENDING = None
    monkeypatch.setattr(remote_access, "report_runtime_status", blocking_report)

    remote_access._report_runtime_status_async(event="heartbeat")
    assert started.wait(timeout=1)
    remote_access._report_runtime_status_async(event="quality-old")
    remote_access._report_runtime_status_async(event="quality-new")
    release.set()
    remote_access.drain_runtime_status_reports(timeout_seconds=1.0)

    assert events == ["heartbeat", "quality-new"]


def test_lifecycle_status_thread_start_failure_is_best_effort(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    with remote_access._STATUS_REPORT_LOCK:
        remote_access._STATUS_REPORT_THREADS.clear()

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread limit reached")

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(remote_access.threading, "Thread", FailingThread)

    result = remote_access.stop(_config())

    assert result["ok"] is True
    with remote_access._STATUS_REPORT_LOCK:
        assert remote_access._STATUS_REPORT_THREADS == set()


def test_status_heartbeat_can_retry_after_thread_start_failure(monkeypatch) -> None:
    remote_access._STATUS_HEARTBEAT_STARTED = False
    starts = []

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread limit reached")

    class SuccessfulThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            starts.append(True)

    monkeypatch.setattr(remote_access.threading, "Thread", FailingThread)

    try:
        remote_access.start_status_heartbeat(interval_seconds=1)
        assert remote_access._STATUS_HEARTBEAT_STARTED is False

        monkeypatch.setattr(remote_access.threading, "Thread", SuccessfulThread)
        remote_access.start_status_heartbeat(interval_seconds=1)
        assert remote_access._STATUS_HEARTBEAT_STARTED is True
        assert starts == [True]
    finally:
        remote_access._STATUS_HEARTBEAT_STARTED = False


def test_stop_ui_continues_when_remote_access_stop_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    stop_calls = []
    timings = {}

    monkeypatch.setattr(remote_access, "stop", lambda: {"ok": False, "error": "cloudflared_stop_failed"})
    monkeypatch.setattr(runtime, "stop_process", lambda pid_path: stop_calls.append(pid_path) or True)

    assert runtime.stop_ui(timings) is False
    assert stop_calls == [paths.get_runtime_ui_pid_path()]
    assert "stop_remote_access_seconds" in timings
    assert "stop_ui_process_seconds" in timings
    assert "stop_ui_seconds" in timings


def test_stop_ui_can_skip_remote_access_stop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    stop_calls = []
    timings = {}

    monkeypatch.setattr(
        remote_access,
        "stop",
        lambda: (_ for _ in ()).throw(AssertionError("remote access should stay running")),
    )
    monkeypatch.setattr(runtime, "stop_process", lambda pid_path: stop_calls.append(pid_path) or True)

    assert runtime.stop_ui(timings, stop_remote_access=False) is True
    assert stop_calls == [paths.get_runtime_ui_pid_path()]
    assert timings["stop_remote_access_seconds"] == 0.0
    assert timings["stop_remote_access_skipped"] is True


def test_cloudflared_pid_detection_handles_quoted_paths_with_spaces(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: '"C:\\Program Files\\Cloudflare\\cloudflared.exe" tunnel --no-autoupdate run',
    )

    assert remote_access._is_cloudflared_pid(123) is True


def test_cloudflared_pid_detection_handles_posix_quoted_paths_with_single_quotes(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: "'/tmp/O'\"'\"'Reilly/cloudflared' tunnel --no-autoupdate run",
    )

    assert remote_access._is_cloudflared_pid(123) is True


def test_cloudflared_pid_detection_handles_unquoted_paths_with_spaces(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: "/tmp/Vibe Tools/cloudflared tunnel --no-autoupdate run",
    )

    assert remote_access._is_cloudflared_pid(123) is True


def test_cloudflared_pid_detection_rejects_non_cloudflared_paths(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: "/tmp/Vibe Tools/not-cloudflared tunnel --no-autoupdate run",
    )

    assert remote_access._cloudflared_pid_state(123) == "other"


def test_stop_preserves_pid_file_when_process_stop_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    pid = 123
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    remote_access._state_path().write_text('{"pid": 123}', encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda candidate: "cloudflared tunnel run")
    monkeypatch.setattr(runtime, "stop_pid", lambda candidate, timeout=8: False)

    result = remote_access.stop()
    remote_access.drain_runtime_status_reports(timeout_seconds=1.0)

    assert result["ok"] is False
    assert result["error"] == "cloudflared_stop_failed"
    assert remote_access._pid_path().read_text(encoding="utf-8") == str(pid)
    assert remote_access._state_path().exists()


def test_stop_preserves_pid_file_when_stop_reports_success_but_process_survives(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    pid = 123
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    remote_access._state_path().write_text('{"pid": 123}', encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda candidate: "cloudflared tunnel run")
    monkeypatch.setattr(runtime, "stop_pid", lambda candidate, timeout=8: True)
    reports = []
    monkeypatch.setattr(
        remote_access,
        "report_runtime_status",
        lambda config=None, event="heartbeat", last_error=None: reports.append((event, last_error)),
    )

    result = remote_access.stop()
    remote_access.drain_runtime_status_reports(timeout_seconds=1.0)

    assert result["ok"] is False
    assert result["error"] == "cloudflared_stop_failed"
    assert reports == [("stop_failed", "cloudflared_stop_failed")]
    assert remote_access._pid_path().read_text(encoding="utf-8") == str(pid)
    assert remote_access._state_path().exists()


def test_status_preserves_pid_file_when_process_command_is_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    pid = 123
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    remote_access._state_path().write_text('{"pid": 123}', encoding="utf-8")

    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda candidate: None)

    result = remote_access.status(_config())

    assert result["running"] is False
    assert result["pid"] == pid
    assert result["pid_state"] == "unknown"
    assert remote_access._pid_path().read_text(encoding="utf-8") == str(pid)
    assert remote_access._state_path().exists()


def test_ra_tq_025_status_computes_network_path_only_for_local_api(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    pid = 123
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    runtime.write_json(
        remote_access._state_path(),
        {
            "pid": pid,
            "active": {"pid": pid, "metrics_url": "http://127.0.0.1:29001"},
        },
    )
    runtime.write_json(
        remote_access._quality_state_path(),
        {"schema_version": 2, "edge_locations": ["sin09", "sin12"]},
    )
    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda candidate: "cloudflared tunnel run")
    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: "/usr/local/bin/cloudflared")
    monkeypatch.setattr(
        remote_access.tunnel_quality,
        "scrape_metrics",
        lambda metrics_url: SimpleNamespace(edge_locations=("sin09", "sin12")),
    )
    calls = []
    monkeypatch.setattr(
        remote_access.cloudflare_network,
        "network_path_snapshot",
        lambda locations, metrics_url, *, client_colo=None, client_access="local": calls.append(
            (locations, metrics_url, client_colo, client_access)
        )
        or {"schema_version": 1},
    )

    internal_status = remote_access.status(config)
    local_status = remote_access.status(
        config,
        client_colo="SIN",
        client_access="remote",
        include_network_path=True,
    )

    assert "network_path" not in internal_status
    assert local_status["network_path"] == {"schema_version": 1}
    assert calls == [(["sin09", "sin12"], "http://127.0.0.1:29001", "SIN", "remote")]


def test_ra_tq_025_status_rejects_edge_locations_when_live_scrape_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    pid = 123
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    runtime.write_json(
        remote_access._state_path(),
        {
            "pid": pid,
            "active": {"pid": pid, "metrics_url": "http://127.0.0.1:29001"},
        },
    )
    runtime.write_json(
        remote_access._quality_state_path(),
        {"schema_version": 2, "edge_locations": ["nrt01"]},
    )
    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda candidate: "cloudflared tunnel run")
    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: "/usr/local/bin/cloudflared")

    def unavailable_metrics(_metrics_url):
        raise OSError("metrics unavailable")

    monkeypatch.setattr(remote_access.tunnel_quality, "scrape_metrics", unavailable_metrics)
    observed_locations = []
    monkeypatch.setattr(
        remote_access.cloudflare_network,
        "network_path_snapshot",
        lambda locations, metrics_url, **kwargs: observed_locations.append(locations)
        or {"schema_version": 1},
    )

    result = remote_access.status(config, include_network_path=True)

    assert result["network_path"] == {"schema_version": 1}
    assert observed_locations == [[]]


def test_start_refuses_duplicate_connector_when_process_command_is_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    pid = 123
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    remote_access._state_path().write_text('{"pid": 123}', encoding="utf-8")
    spawn_calls = []

    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda candidate: None)
    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: "/usr/local/bin/cloudflared")
    monkeypatch.setattr(remote_access, "_version", lambda path: "cloudflared test")
    monkeypatch.setattr(runtime, "spawn_background", lambda *args, **kwargs: spawn_calls.append(args) or 456)

    result = remote_access.start(config)

    assert result["ok"] is False
    assert result["error"] == "cloudflared_process_unknown"
    assert spawn_calls == []
    assert remote_access._pid_path().read_text(encoding="utf-8") == str(pid)
    assert remote_access._state_path().exists()


def test_start_returns_failure_when_remote_access_is_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    monkeypatch.setattr(remote_access, "stop", lambda config=None: {"ok": True, "stopped": False})

    result = remote_access.start(config)

    assert result["ok"] is False
    assert result["error"] == "remote_access_disabled"


def test_start_revalidates_config_after_connector_lock(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    load_lock_states = []

    def load_config():
        load_lock_states.append(remote_access._CONNECTOR_LOCK._is_owned())
        return config

    monkeypatch.setattr(remote_access.V2Config, "load", load_config)
    monkeypatch.setattr(remote_access, "stop", lambda loaded_config=None: {"ok": True, "stopped": False})

    result = remote_access.start()

    assert result["ok"] is False
    assert result["error"] == "remote_access_disabled"
    assert load_lock_states == [False, True]


def test_start_returns_structured_error_when_initial_config_load_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def fail_load():
        raise ValueError("corrupt config")

    monkeypatch.setattr(remote_access.V2Config, "load", fail_load)

    result = remote_access.start()

    assert result["ok"] is False
    assert result["error"] == "remote_access_config_load_failed"
    assert result["started"] is False
    assert "corrupt config" in result["detail"]


def test_start_uses_current_persisted_config_over_stale_argument(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    stale_config = _config()
    stale_config.remote_access.vibe_cloud.tunnel_token = "stale-token"
    persisted_config = _config()
    persisted_config.remote_access.vibe_cloud.enabled = False
    persisted_config.remote_access.vibe_cloud.tunnel_token = ""
    persisted_config.save()

    monkeypatch.setattr(remote_access, "stop", lambda config=None: {"ok": True, "stopped": False})
    monkeypatch.setattr(
        remote_access.runtime,
        "spawn_background",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale config should not start cloudflared")),
    )

    result = remote_access.start(stale_config)

    assert result["ok"] is False
    assert result["error"] == "remote_access_disabled"


def test_stop_loads_config_before_connector_lock(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    load_lock_states = []

    def load_config():
        load_lock_states.append(remote_access._CONNECTOR_LOCK._is_owned())
        return config

    monkeypatch.setattr(remote_access.V2Config, "load", load_config)

    result = remote_access.stop()

    assert result["ok"] is True
    assert load_lock_states == [False]


def test_reconcile_stops_when_remote_access_is_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.enabled = False

    monkeypatch.setattr(remote_access, "stop", lambda next_config=None: {"ok": True, "stopped": True})

    result = remote_access.reconcile(config)

    assert result == {"ok": True, "stopped": True}


def test_start_restarts_when_runtime_signature_changes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "new-token"
    config.save()
    binary = "/usr/local/bin/cloudflared"
    old_pid = 111
    new_pid = 222
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(old_pid), encoding="utf-8")
    remote_access._state_path().write_text(
        json.dumps(
            {
                "pid": old_pid,
                "provider": "vibe_cloud",
                "binary_path": binary,
                "public_url": "https://alex.avibe.bot",
                "tunnel_token_sha256": "old-token-hash",
            }
        ),
        encoding="utf-8",
    )
    alive = {old_pid, new_pid}

    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: binary)
    monkeypatch.setattr(remote_access, "_version", lambda path: "cloudflared test")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"{binary} tunnel run")

    def stop_pid(pid, timeout=8):
        alive.discard(pid)
        return True

    monkeypatch.setattr(runtime, "stop_pid", stop_pid)
    def spawn_background(args, pid_path, stdout_name, stderr_name, env=None):
        pid_path.write_text(str(new_pid), encoding="utf-8")
        return new_pid

    monkeypatch.setattr(runtime, "spawn_background", spawn_background)

    result = remote_access.start(config)
    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["started"] is True
    assert result["pid"] == new_pid
    assert old_pid not in alive
    assert state["tunnel_token_sha256"] == "348e9df2a42bd6e3c6356ca9c95c5f1fe9a6b3e5cd25f4ae58df0f09049c3209"


def test_start_restarts_matching_legacy_connector_without_metrics(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    binary = "/usr/local/bin/cloudflared"
    old_pid = 111
    new_pid = 222
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(old_pid), encoding="utf-8")
    runtime.write_json(
        remote_access._state_path(),
        {
            "pid": old_pid,
            **remote_access._runtime_signature(config, binary),
        },
    )
    alive = {old_pid, new_pid}
    stopped = []

    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: binary)
    monkeypatch.setattr(remote_access, "_version", lambda path: "cloudflared test")
    monkeypatch.setattr(remote_access, "_allocate_metrics_url", lambda: "http://127.0.0.1:29999")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"{binary} tunnel run")

    def stop_pid(pid, timeout=8):
        stopped.append(pid)
        alive.discard(pid)
        return True

    def spawn_background(args, pid_path, stdout_name, stderr_name, env=None):
        pid_path.write_text(str(new_pid), encoding="utf-8")
        return new_pid

    monkeypatch.setattr(runtime, "stop_pid", stop_pid)
    monkeypatch.setattr(runtime, "spawn_background", spawn_background)

    result = remote_access.start(config)
    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["started"] is True
    assert stopped == [old_pid]
    assert state["active"]["metrics_url"] == "http://127.0.0.1:29999"


def test_start_keeps_matching_connector_with_metrics(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    binary = "/usr/local/bin/cloudflared"
    pid = 111
    metrics_url = "http://127.0.0.1:29999"
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(pid), encoding="utf-8")
    remote_access._write_state(pid, config, binary, metrics_url)

    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: binary)
    monkeypatch.setattr(remote_access, "_version", lambda path: "cloudflared test")
    monkeypatch.setattr(runtime, "pid_alive", lambda candidate: candidate == pid)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda candidate: f"{binary} tunnel --metrics 127.0.0.1:29999 --no-autoupdate run",
    )
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("matching connector must not restart")),
    )

    result = remote_access.start(config)

    assert result["ok"] is True
    assert result["started"] is False
    assert result["pid"] == pid


def test_start_clears_previous_cloudflared_logs_before_spawn(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    binary = "/usr/local/bin/cloudflared"
    new_pid = 222
    remote_access._cloudflared_stdout_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._cloudflared_stdout_path().write_text("old stdout", encoding="utf-8")
    remote_access._cloudflared_stderr_path().write_text("originService=http://old.local:5123\n", encoding="utf-8")

    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: binary)
    monkeypatch.setattr(remote_access, "_version", lambda path: "cloudflared test")
    monkeypatch.setattr(remote_access, "_allocate_metrics_url", lambda: "http://127.0.0.1:29999")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == new_pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"{binary} tunnel run")
    spawn_args = []

    def spawn_background(args, pid_path, stdout_name, stderr_name, env=None):
        assert not remote_access._cloudflared_stdout_path().exists()
        assert not remote_access._cloudflared_stderr_path().exists()
        spawn_args.append(args)
        pid_path.write_text(str(new_pid), encoding="utf-8")
        return new_pid

    monkeypatch.setattr(runtime, "spawn_background", spawn_background)

    result = remote_access.start(config)

    assert result["ok"] is True
    assert result["started"] is True
    assert spawn_args == [[binary, "tunnel", "--metrics", "127.0.0.1:29999", "--no-autoupdate", "run"]]


def test_start_falls_back_to_auto_when_preferred_protocol_is_not_ready(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    binary = "/usr/local/bin/cloudflared"
    preferred_pid = 222
    fallback_pid = 333
    alive = {preferred_pid, fallback_pid}
    spawned_protocols = []
    spawned_pids = iter([preferred_pid, fallback_pid])
    metrics_urls = iter(["http://127.0.0.1:29998", "http://127.0.0.1:29999"])

    monkeypatch.setattr(remote_access, "_resolve_binary", lambda cfg: binary)
    monkeypatch.setattr(remote_access, "_version", lambda path: "cloudflared test")
    monkeypatch.setattr(remote_access, "_allocate_metrics_url", lambda: next(metrics_urls))
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"{binary} tunnel run")
    monkeypatch.setattr(remote_access, "_wait_connector_ready", lambda *args, **kwargs: False)

    def spawn_background(args, pid_path, stdout_name, stderr_name, env=None):
        pid = next(spawned_pids)
        spawned_protocols.append(env["TUNNEL_TRANSPORT_PROTOCOL"])
        pid_path.write_text(str(pid), encoding="utf-8")
        return pid

    def stop_pid(pid, timeout=8):
        alive.discard(pid)
        return True

    monkeypatch.setattr(runtime, "spawn_background", spawn_background)
    monkeypatch.setattr(runtime, "stop_pid", stop_pid)
    previous_preference = remote_access._PREFERRED_PROTOCOL
    remote_access._PREFERRED_PROTOCOL = "http2"
    try:
        result = remote_access.start(config)
    finally:
        remote_access._PREFERRED_PROTOCOL = previous_preference

    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["pid"] == fallback_pid
    assert spawned_protocols == ["http2", "auto"]
    assert preferred_pid not in alive
    assert state["active"]["requested_protocol"] == "auto"


def test_effective_ui_bind_host_uses_setup_host_when_tunnel_disabled() -> None:
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = "100.97.103.112"

    assert runtime.effective_ui_bind_host(config) == "100.97.103.112"


def test_effective_ui_bind_host_overrides_to_wildcard_when_tunnel_enabled() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "100.97.103.112"

    assert runtime.effective_ui_bind_host(config) == "0.0.0.0"


def test_effective_ui_bind_host_preserves_loopback_when_tunnel_disabled() -> None:
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = "127.0.0.1"

    assert runtime.effective_ui_bind_host(config) == "127.0.0.1"


def test_effective_ui_bind_host_preserves_loopback_when_tunnel_enabled() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "127.0.0.1"

    assert runtime.effective_ui_bind_host(config) == "127.0.0.1"


def test_effective_ui_bind_host_falls_back_to_loopback_when_setup_host_blank() -> None:
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = ""

    assert runtime.effective_ui_bind_host(config) == "127.0.0.1"


def test_effective_ui_bind_host_falls_back_to_loopback_when_tunnel_enabled_and_setup_host_blank() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = ""

    assert runtime.effective_ui_bind_host(config) == "127.0.0.1"


def test_effective_ui_bind_host_uses_v6_wildcard_for_ipv6_setup_host() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "::"

    assert runtime.effective_ui_bind_host(config) == "::"


def test_effective_ui_bind_host_preserves_v6_loopback_when_tunnel_enabled() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "::1"

    assert runtime.effective_ui_bind_host(config) == "::1"


def test_effective_ui_bind_host_uses_v6_wildcard_for_bracketed_ipv6_loopback() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "[::1]"

    assert runtime.effective_ui_bind_host(config) == "::1"


def test_effective_ui_bind_host_prefers_requested_host_over_persisted_setup_host() -> None:
    config = _config()
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = "127.0.0.1"

    assert runtime.effective_ui_bind_host(config, requested_host="192.168.1.10") == "192.168.1.10"


def test_effective_ui_bind_host_requested_host_yields_to_tunnel_override() -> None:
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "127.0.0.1"

    assert runtime.effective_ui_bind_host(config, requested_host="100.97.103.112") == "0.0.0.0"


def test_effective_ui_bind_host_overrides_to_ipv4_wildcard_when_localhost_resolves_dual_stack(
    monkeypatch,
) -> None:
    # Pairs with _origin_host_for_pairing returning 127.0.0.1 for "localhost":
    # the bind host must be the same IPv4 loopback so cloudflared can reach the UI.
    monkeypatch.setattr(runtime, "resolve_localhost_family", lambda: "inet")
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "localhost"

    assert runtime.effective_ui_bind_host(config) == "127.0.0.1"


def test_effective_ui_bind_host_overrides_to_ipv6_wildcard_when_localhost_resolves_v6_only(
    monkeypatch,
) -> None:
    # On IPv6-only hosts where "localhost" only resolves to ::1, forcing
    # IPv4 would unbind the UI from the only loopback the OS exposes;
    # follow the same family the cloudflared origin will use.
    monkeypatch.setattr(runtime, "resolve_localhost_family", lambda: "inet6")
    config = _config()
    assert config.remote_access.vibe_cloud.enabled is True
    config.ui.setup_host = "localhost"

    assert runtime.effective_ui_bind_host(config) == "::1"


def test_resolve_localhost_family_prefers_ipv4_when_both_resolve(monkeypatch) -> None:
    import socket as _socket

    def fake_getaddrinfo(host, port, *, type=None):  # noqa: A002 - shadowing matches stdlib
        return [
            (_socket.AF_INET6, _socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0)),
            (_socket.AF_INET, _socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr("vibe.runtime.socket.getaddrinfo", fake_getaddrinfo)
    assert runtime.resolve_localhost_family() == "inet"


def test_resolve_localhost_family_returns_inet6_when_only_v6_resolves(monkeypatch) -> None:
    import socket as _socket

    def fake_getaddrinfo(host, port, *, type=None):  # noqa: A002
        return [(_socket.AF_INET6, _socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0))]

    monkeypatch.setattr("vibe.runtime.socket.getaddrinfo", fake_getaddrinfo)
    assert runtime.resolve_localhost_family() == "inet6"


def test_resolve_localhost_family_falls_back_to_inet_on_resolution_failure(monkeypatch) -> None:
    import socket as _socket

    def fake_getaddrinfo(host, port, *, type=None):  # noqa: A002
        raise _socket.gaierror("simulated")

    monkeypatch.setattr("vibe.runtime.socket.getaddrinfo", fake_getaddrinfo)
    assert runtime.resolve_localhost_family() == "inet"


def _cloud_broker_config() -> V2Config:
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.instance_secret = "device-secret"
    cloud.backend_url = "https://avibe.bot"
    config.save()
    return config


def test_mint_cloud_token_posts_with_device_secret_header(monkeypatch) -> None:
    config = _cloud_broker_config()
    captured: dict = {}

    def fake_json_request(url, payload, timeout=20.0, headers=None):
        captured.update(url=url, payload=payload, headers=headers)
        return {"access_token": "ct_abc", "token_type": "Bearer", "expires_in": 43200}

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)

    minted = remote_access.mint_cloud_token(config, sub="user-1", email="alex@example.com", scope="asr")

    assert minted == {"access_token": "ct_abc", "token_type": "Bearer", "expires_in": 43200}
    assert captured["url"] == "https://avibe.bot/api/v1/instances/inst_123/user-token"
    assert captured["payload"] == {"sub": "user-1", "email": "alex@example.com", "scope": "asr"}
    assert captured["headers"] == {"X-Vibe-Device-Secret": "device-secret"}


def test_mint_cloud_token_returns_none_when_not_configured(monkeypatch) -> None:
    config = _config()  # no instance_secret / backend_url
    called = False

    def fake_json_request(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)

    assert remote_access.mint_cloud_token(config, sub="u", email="e@x.com") is None
    assert called is False  # short-circuits before any network call


def test_mint_cloud_token_returns_none_on_backend_error(monkeypatch) -> None:
    config = _cloud_broker_config()

    def fake_json_request(*args, **kwargs):
        raise remote_access.BackendRequestError(403, {"error": "user_not_authorized"})

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)

    assert remote_access.mint_cloud_token(config, sub="u", email="e@x.com") is None


@pytest.mark.parametrize("role", ("editor", "owner"))
def test_cloud_token_for_request_mints_for_authorized_remote_asr(
    monkeypatch,
    role,
) -> None:
    config = _cloud_broker_config()
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *a, **k: 1)
    cookie = _session_cookie(config, role=role)
    captured: dict = {}

    def fake_json_request(*args, **kwargs):
        captured["payload"] = args[1]
        return {"access_token": "ct_xyz", "expires_in": 43200}

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)

    token = remote_access.cloud_token_for_request(config, cookie)

    assert token is not None
    assert token["base_url"] == "https://avibe.bot"
    assert token["token"] == "ct_xyz"
    assert token["scope"] == "asr"
    assert token["expires_at"] > int(time.time())
    assert captured["payload"] == {
        "sub": "user-1",
        "email": "alex@example.com",
        "scope": "asr",
    }


def test_cloud_token_for_request_rejects_non_asr_scope(monkeypatch) -> None:
    config = _cloud_broker_config()
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *a, **k: 1)
    cookie = _session_cookie(config, role="owner")
    called = False

    def fake_json_request(*args, **kwargs):
        nonlocal called
        called = True
        return {"access_token": "ct_xyz", "expires_in": 43200}

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)

    assert remote_access.cloud_token_for_request(config, cookie, scope="future") is None
    assert called is False


def test_cloud_token_for_request_returns_none_without_valid_session(monkeypatch) -> None:
    config = _cloud_broker_config()
    monkeypatch.setattr(
        remote_access, "_json_request", lambda *a, **k: {"access_token": "x", "expires_in": 1}
    )

    assert remote_access.cloud_token_for_request(config, None) is None
    assert remote_access.cloud_token_for_request(config, "bogus.cookie") is None


def test_cloud_token_for_request_uses_shared_personal_authorization_policy(monkeypatch) -> None:
    config = _cloud_broker_config()
    config.remote_access.vibe_cloud.instance_kind = "personal"
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *a, **k: 1)
    cookie = _session_cookie(config)
    called = False

    def fake_json_request(*args, **kwargs):
        nonlocal called
        called = True
        return {"access_token": "x", "expires_in": 1}

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)
    monkeypatch.setattr(
        remote_access,
        "session_needs_authorization_refresh",
        lambda payload: True,
    )

    assert remote_access.cloud_token_for_request(config, cookie) is not None
    assert called is True


def test_cloud_token_for_request_requires_editor_role(monkeypatch) -> None:
    config = _cloud_broker_config()
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *a, **k: 1)
    cookie = _session_cookie(config, role="viewer")
    called = False

    def fake_json_request(*args, **kwargs):
        nonlocal called
        called = True
        return {"access_token": "x", "expires_in": 1}

    monkeypatch.setattr(remote_access, "_json_request", fake_json_request)

    assert remote_access.cloud_token_for_request(config, cookie) is None
    assert called is False


def test_cloud_token_endpoint_local_origin_without_session_degrades_to_unavailable(
    monkeypatch,
    tmp_path,
) -> None:
    """Issue #1491: a local-origin request has no avibe.bot session cookie by
    design, so the endpoint must return the documented 503 ``cloud_unavailable``
    fallback instead of the login-required signal that triggers the frontend's
    full-page redirect to ``/auth/login`` (which the local host rejects)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _cloud_broker_config()
    config.save()

    response = ui_server.app.test_client().get(
        "/api/cloud/token",
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 503
    assert response.get_json() == {"error": "cloud_unavailable"}


def test_cloud_token_endpoint_remote_origin_without_session_requires_login(
    monkeypatch,
    tmp_path,
) -> None:
    """On the genuine remote-access host, a missing session still means the
    browser must log in, so the recovery signal is preserved there."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _cloud_broker_config()
    config.save()

    response = ui_server.app.test_client().get(
        "/api/cloud/token",
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "remote_access_login_required"}


def test_cloud_token_endpoint_remote_origin_with_session_mints_token(
    monkeypatch,
    tmp_path,
) -> None:
    """An authenticated remote request still reaches the mint path after the
    local-origin degrade branch is added in front of it."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _cloud_broker_config()
    config.save()
    monkeypatch.setattr(remote_access, "current_authorization_revision", lambda *a, **k: 1)
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *a, **k: {"access_token": "ct_abc", "token_type": "Bearer", "expires_in": 43200},
    )

    client = ui_server.app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _session_cookie(config),
        domain="alex.avibe.bot",
    )
    response = client.get(
        "/api/cloud/token",
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["token"] == "ct_abc"
    assert payload["scope"] == "asr"
    assert response.headers["Cache-Control"] == "no-store, private"


def test_ra_tq_029_connector_environment_applies_ip_and_interface_controls() -> None:
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.tunnel_token = "tunnel-token"
    cloud.edge_ip_version = "4"
    cloud.edge_bind_address = "192.0.2.10"

    environment = remote_access._connector_environment(config, "http2")

    assert environment["TUNNEL_TOKEN"] == "tunnel-token"
    assert environment["TUNNEL_TRANSPORT_PROTOCOL"] == "http2"
    assert environment["TUNNEL_EDGE_IP_VERSION"] == "4"
    assert environment["TUNNEL_EDGE_BIND_ADDRESS"] == "192.0.2.10"


def test_ra_tq_029_network_interfaces_exclude_unusable_addresses(monkeypatch) -> None:
    monkeypatch.setattr(
        remote_access.psutil,
        "net_if_stats",
        lambda: {
            "en0": SimpleNamespace(isup=True),
            "lo0": SimpleNamespace(isup=True),
            "down0": SimpleNamespace(isup=False),
        },
    )
    monkeypatch.setattr(
        remote_access.psutil,
        "net_if_addrs",
        lambda: {
            "en0": [
                SimpleNamespace(family=remote_access.socket.AF_INET, address="192.0.2.10"),
                SimpleNamespace(family=remote_access.socket.AF_INET6, address="2001:db8::10%en0"),
                SimpleNamespace(family=remote_access.socket.AF_INET6, address="fe80::1%en0"),
            ],
            "lo0": [SimpleNamespace(family=remote_access.socket.AF_INET, address="127.0.0.1")],
            "down0": [SimpleNamespace(family=remote_access.socket.AF_INET, address="198.51.100.2")],
        },
    )

    result = remote_access.network_interfaces()

    assert result == {
        "ok": True,
        "interfaces": [
            {"id": "en0:192.0.2.10", "name": "en0", "address": "192.0.2.10", "ip_version": "4"},
            {"id": "en0:2001:db8::10", "name": "en0", "address": "2001:db8::10", "ip_version": "6"},
        ],
    }


def test_ra_tq_030_connectivity_diagnostics_do_not_guess_quic_reachability(monkeypatch) -> None:
    config = _config()
    monkeypatch.setattr(
        remote_access,
        "status",
        lambda loaded=None: {
            "running": True,
            "binary_version": "2026.3.0",
            "tunnel_quality": {"protocol": "http2", "transport": {"effective": "http2"}},
        },
    )
    monkeypatch.setattr(
        remote_access,
        "_bounded_tunnel_addresses",
        lambda: [(remote_access.socket.AF_INET, "198.51.100.10")],
    )
    monkeypatch.setattr(remote_access, "_tcp_tunnel_reachable", lambda *args, **kwargs: True)

    result = remote_access.connectivity_diagnostics(config)

    assert result["dns"]["status"] == "available"
    assert result["http2"]["status"] == "available"
    assert result["quic"] == {"status": "unknown", "source": "not_observed"}


@pytest.mark.parametrize(
    "sample_age_before_restart",
    [remote_access.QUALITY_COMPARISON_MAX_AGE_SECONDS + 1, 1],
)
def test_ra_tq_030_connectivity_diagnostics_requires_fresh_active_protocol(
    monkeypatch,
    sample_age_before_restart,
) -> None:
    config = _config()
    now = time.time()
    monkeypatch.setattr(
        remote_access,
        "status",
        lambda loaded=None: {
            "running": True,
            "pid": 222,
            "binary_version": "2026.3.0",
            "tunnel_quality": {
                "protocol": "quic",
                "transport": {"effective": "quic"},
                "sampled_at": remote_access.tunnel_quality.utc_timestamp(
                    now - sample_age_before_restart
                ),
            },
        },
    )
    monkeypatch.setattr(
        remote_access,
        "_state_connector",
        lambda name: {"pid": 222, "started_at": now} if name == "active" else None,
    )
    monkeypatch.setattr(
        remote_access,
        "_bounded_tunnel_addresses",
        lambda: [(remote_access.socket.AF_INET, "198.51.100.10")],
    )
    monkeypatch.setattr(remote_access, "_tcp_tunnel_reachable", lambda *args, **kwargs: False)

    result = remote_access.connectivity_diagnostics(config)

    assert result["effective_protocol"] == "unknown"
    assert result["quic"] == {"status": "unknown", "source": "not_observed"}
    assert result["http2"] == {"status": "unavailable", "source": "tcp_probe"}


def test_ra_tq_030_connectivity_diagnostics_filters_dns_by_selected_family(
    monkeypatch,
) -> None:
    config = _config()
    config.remote_access.vibe_cloud.edge_ip_version = "6"
    monkeypatch.setattr(
        remote_access,
        "status",
        lambda loaded=None: {
            "running": False,
            "binary_version": "2026.3.0",
        },
    )
    monkeypatch.setattr(
        remote_access,
        "_bounded_tunnel_addresses",
        lambda: [(remote_access.socket.AF_INET, "198.51.100.10")],
    )
    monkeypatch.setattr(
        remote_access,
        "_tcp_tunnel_reachable",
        lambda *args, **kwargs: pytest.fail("no selected-family address is available"),
    )

    result = remote_access.connectivity_diagnostics(config)

    assert result["dns"]["status"] == "unavailable"
    assert result["http2"]["status"] == "unknown"


def test_unknown_kind_pairing_stays_usable_after_pair(monkeypatch, tmp_path) -> None:
    """Regression PR #1606 r1: no-kind pairings must not park in reconciling."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    response = {
        "instance_id": "inst_456",
        "client_id": "vr_client_456",
        "issuer": "https://backend.test",
        "authorization_endpoint": "https://backend.test/oauth/authorize",
        "token_endpoint": "https://backend.test/oauth/token",
        "jwks_uri": "https://backend.test/oauth/jwks.json",
        "public_url": "https://new.avibe.bot",
        "redirect_uri": "https://new.avibe.bot/auth/callback",
        "tunnel_token": "tunnel-token",
        "instance_secret": "instance-secret",
    }
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: response)

    def fake_save_config(payload, **kwargs):
        config.remote_access.vibe_cloud.instance_id = payload["remote_access"]["vibe_cloud"]["instance_id"]
        return config

    monkeypatch.setattr(remote_access.api, "save_config", fake_save_config)
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    assert remote_access.pair("vrp_test", "https://backend.test")["ok"] is True

    from storage import remote_access_authorization_service

    state = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    assert state is not None
    assert state["state"] == remote_access_authorization_service.INSTANCE_BINDING_STATE_PENDING_KIND
    assert state["instance_id"] == "inst_456"
    # The legacy no-kind path stays usable for every authorization consumer.
    assert remote_access_authorization_service.binding_is_ready_for_pairing(
        instance_id="inst_456",
        instance_kind=None,
        ensure=False,
    ) is True
    # A kind-specific bypass is still not claimable from this state.
    assert remote_access_authorization_service.binding_is_ready_for_pairing(
        instance_id="inst_456",
        instance_kind="personal",
        ensure=False,
    ) is False


def test_pair_refuses_to_publish_binding_for_a_replaced_instance(monkeypatch, tmp_path) -> None:
    """Regression PR #1606 r1: pairing save + transition are one critical section."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: _pair_redeem_response())

    def racing_save_config(payload, **kwargs):
        # Simulate a peer's pairing landing between our save and verification:
        # the persisted config no longer names the instance we redeemed.
        config.remote_access.vibe_cloud.instance_id = "inst_other"
        return config

    monkeypatch.setattr(remote_access.api, "save_config", racing_save_config)
    monkeypatch.setattr(
        remote_access,
        "_run_pending_deferred_context_migration",
        lambda: {"legacy_deferred_definitions": 0, "legacy_deferred_runs": 0, "legacy_deferred_deliveries": 0, "binding_status": "sealed"},
    )
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True})

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is False
    assert result["error"] == "pairing_reconciliation_failed"
    assert result["detail"] == "persisted_instance_mismatch"

    from storage import remote_access_authorization_service

    state = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    redeemed = _pair_redeem_response()["instance_id"]
    assert state is None or state.get("instance_id") != redeemed


def test_overlapping_heartbeat_refuses_stale_personal_kind(monkeypatch, tmp_path) -> None:
    """Regression PR #1606 r4: capture generation BEFORE the network call.

    Two overlapping runtime-status requests: the newer Organization response
    lands first; the older Personal response must CAS-fail and leave the
    binding Organization.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    cloud = config.remote_access.vibe_cloud
    cloud.backend_url = "https://backend.test"
    cloud.instance_secret = "instance-secret"
    cloud.instance_kind = "personal"
    config.save()
    remote_access._transition_instance_binding(
        instance_id="inst_123",
        instance_kind="personal",
    )
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {"ok": True, "instance_kind": "organization"},
    )

    from storage import remote_access_authorization_service

    captured = remote_access_authorization_service.current_instance_binding_generation(
        ensure=False
    )
    # Newer Organization heartbeat lands first.
    assert remote_access.report_runtime_status(config)["ok"] is True
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "organization"
    # Older in-flight Personal response, captured before the Org persist,
    # must CAS-fail against the newer generation.
    refused = remote_access._persist_instance_kind(
        "inst_123",
        "personal",
        reconcile=True,
        expected_binding_generation=captured,
    )
    assert refused is False
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "organization"
    state = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    assert state is not None
    assert state["instance_kind"] == "organization"
    assert state["generation"] > captured
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: {"ok": True, "instance_kind": "organization"},
    )
    assert remote_access.report_runtime_status(V2Config.load())["ok"] is True
    assert V2Config.load().remote_access.vibe_cloud.instance_kind == "organization"

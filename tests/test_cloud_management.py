from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
from vibe import cloud_management, remote_access
from vibe.ui_server import app


REMOTE_ORIGIN = "https://alex.avibe.bot"


@pytest.fixture(autouse=True)
def _isolated_management_state() -> None:
    cloud_management.reset_for_tests()
    yield
    cloud_management.reset_for_tests()


def _save_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> V2Config:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
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
    cloud.backend_url = "https://avibe.bot"
    cloud.public_url = REMOTE_ORIGIN
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.session_secret = "session-secret"
    cloud.issuer = "https://avibe.bot"
    cloud.jwks_uri = "https://avibe.bot/.well-known/jwks.json"
    cloud.authorization_endpoint = "https://avibe.bot/oauth/authorize"
    cloud.token_endpoint = "https://avibe.bot/oauth/token"
    cloud.redirect_uri = f"{REMOTE_ORIGIN}/auth/callback"
    config.save()
    return config


def _remote_client(config: V2Config, *, role: str = "viewer"):
    client = app.test_client()
    cookie = remote_session_cookie(
        config,
        "alex@example.com",
        "user-1",
        role=role,
        access_source="email",
    )
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")
    return client


def _grant(browser_id: str = "browser-1", *, subject: str = "user-1") -> cloud_management.ManagementGrant:
    return cloud_management.ManagementGrant(
        handle="grant-1",
        browser_id=browser_id,
        subject=subject,
        email="alex@example.com",
        token="secret-bearer-token",
        expires_at=time.time() + 300,
    )


def test_local_start_uses_https_handoff_and_never_exposes_token(monkeypatch, tmp_path) -> None:
    _save_config(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_begin(config, **kwargs):
        captured.update(kwargs)
        return "https://avibe.bot/oauth/management/authorize?state=state-1", "state-1"

    monkeypatch.setattr(cloud_management, "begin_authorization", fake_begin)
    client = app.test_client()
    response = client.post(
        "/api/cloud-management/session/start",
        json={"mode": "interactive", "next": "/admin/organization/members"},
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 202
    assert response.get_json() == {
        "ok": True,
        "authorize_url": f"{REMOTE_ORIGIN}/auth/organization/start?state=state-1",
        "mode": "interactive",
    }
    assert captured["remote_subject"] is None
    assert captured["callback_origin"] == REMOTE_ORIGIN
    assert captured["next_path"] == "/admin/organization/members"
    assert "secret-bearer-token" not in response.text
    browser_cookie = next(
        value
        for value in response.headers.getlist("set-cookie")
        if value.startswith(f"{cloud_management.BROWSER_COOKIE_NAME}=")
    )
    assert "HttpOnly" in browser_cookie


def test_remote_callback_binds_same_remote_subject(monkeypatch, tmp_path) -> None:
    config = _save_config(monkeypatch, tmp_path)
    client = _remote_client(config)
    client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
    captured: dict[str, object] = {}

    def fake_complete(config, **kwargs):
        captured.update(kwargs)
        return _grant(), "/admin/organization/overview"

    monkeypatch.setattr(cloud_management, "complete_authorization", fake_complete)
    response = client.get(
        "/auth/organization/callback?code=code-1&state=state-1",
        base_url=REMOTE_ORIGIN,
    )

    assert response.status_code == 302
    assert response.headers["location"].endswith("/admin/organization/overview")
    assert captured["remote_subject"] == "user-1"
    assert captured["browser_id"] == "browser-1"
    handle_cookie = next(
        value
        for value in response.headers.getlist("set-cookie")
        if value.startswith(f"{cloud_management.HANDLE_COOKIE_NAME}=")
    )
    assert "HttpOnly" in handle_cookie
    assert "Secure" in handle_cookie


def test_subject_mismatch_is_terminal_and_clears_grant(monkeypatch, tmp_path) -> None:
    config = _save_config(monkeypatch, tmp_path)
    client = _remote_client(config)
    client.set_cookie(cloud_management.HANDLE_COOKIE_NAME, "grant-1", domain="alex.avibe.bot")
    client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
    monkeypatch.setattr(
        cloud_management,
        "resolve_grant",
        lambda *args, **kwargs: (None, "cloud_management_subject_mismatch"),
    )

    response = client.get("/api/cloud-management/session", base_url=REMOTE_ORIGIN)

    assert response.status_code == 409
    assert response.get_json() == {
        "connected": False,
        "state": "subject_mismatch",
        "error": "cloud_management_subject_mismatch",
    }
    cookies = response.headers.getlist("set-cookie")
    assert any(
        value.startswith(f"{cloud_management.HANDLE_COOKIE_NAME}=") and "Max-Age=0" in value
        for value in cookies
    )
    assert any(value.startswith(f"{cloud_management.MANUAL_COOKIE_NAME}=") for value in cookies)


def test_explicit_logout_suppresses_silent_reauthorization(monkeypatch, tmp_path) -> None:
    config = _save_config(monkeypatch, tmp_path)
    client = _remote_client(config)
    client.set_cookie(cloud_management.HANDLE_COOKIE_NAME, "grant-1", domain="alex.avibe.bot")
    client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
    cloud_management._browser_subjects["browser-1"] = "user-1"  # noqa: SLF001

    logout = client.delete(
        "/api/cloud-management/session",
        base_url=REMOTE_ORIGIN,
        headers=csrf_headers(client, REMOTE_ORIGIN),
    )
    assert logout.status_code == 200

    session = client.get("/api/cloud-management/session", base_url=REMOTE_ORIGIN)
    assert session.status_code == 200
    assert session.get_json() == {
        "connected": False,
        "state": "authorization_required",
        "can_silent_reauthorize": False,
    }
    assert cloud_management._bound_subject("browser-1") is None  # noqa: SLF001


def test_remote_viewer_can_use_identity_gate_but_only_allowlisted_proxy_paths(
    monkeypatch, tmp_path
) -> None:
    config = _save_config(monkeypatch, tmp_path)
    client = _remote_client(config, role="viewer")
    client.set_cookie(cloud_management.HANDLE_COOKIE_NAME, "grant-1", domain="alex.avibe.bot")
    client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
    monkeypatch.setattr(cloud_management, "resolve_grant", lambda *args, **kwargs: (_grant(), None))
    captured: dict[str, object] = {}

    def fake_proxy(config, **kwargs):
        captured.update(kwargs)
        return 200, {"ok": True}

    monkeypatch.setattr(cloud_management, "proxy_request", fake_proxy)
    response = client.put(
        "/api/cloud-management/instances/inst_123/projects/proj_1/access?view=full",
        base_url=REMOTE_ORIGIN,
        headers=csrf_headers(client, REMOTE_ORIGIN),
        json={"mode": "inherit", "bindings": [], "if_match_revision": 2},
    )

    assert response.status_code == 200
    assert captured["upstream_path"] == "/api/instances/inst_123/projects/proj_1/access"
    assert captured["query"] == [("view", "full")]
    assert captured["json_body"] == {
        "mode": "inherit",
        "bindings": [],
        "if_match_revision": 2,
    }
    assert "secret-bearer-token" not in response.text
    assert not cloud_management.proxy_path_allowed(
        "GET", "/api/organizations/org_1/domains"
    )


@pytest.mark.parametrize(
    ("status", "content_type", "body", "expected_code"),
    [
        (302, "application/json", b"{}", "cloud_management_redirect_blocked"),
        (200, "text/html", b"{}", "cloud_management_invalid_response"),
        (200, "application/json", b"{", "cloud_management_invalid_response"),
        (
            200,
            "application/json",
            b"x" * (cloud_management.MAX_RESPONSE_BYTES + 1),
            "cloud_management_response_too_large",
        ),
    ],
)
def test_backend_transport_rejects_unsafe_responses(
    monkeypatch,
    tmp_path,
    status: int,
    content_type: str,
    body: bytes,
    expected_code: str,
) -> None:
    config = _save_config(monkeypatch, tmp_path)

    class FakeResponse:
        def read(self, _limit: int) -> bytes:
            return body

        def getheader(self, name: str) -> str | None:
            return content_type if name.lower() == "content-type" else None

    class FakeConnection:
        def request(self, *args, **kwargs) -> None:
            return None

        def getresponse(self):
            response = FakeResponse()
            response.status = status
            return response

        def close(self) -> None:
            return None

    backend = SimpleNamespace(
        base_url="https://avibe.bot",
        host_header="avibe.bot",
        requires_proxy=False,
        connect_hosts=("203.0.113.1",),
    )
    monkeypatch.setattr(cloud_management, "_validated_backend", lambda _config: backend)
    monkeypatch.setattr(remote_access, "_validated_backend_proxy_url", lambda _url: None)
    monkeypatch.setattr(
        remote_access,
        "_validated_backend_connection",
        lambda *args, **kwargs: FakeConnection(),
    )

    with pytest.raises(cloud_management.CloudManagementError) as captured:
        cloud_management._backend_request(config, "GET", "/api/organizations")  # noqa: SLF001
    assert captured.value.code == expected_code


def test_proxy_upstream_401_clears_grant_and_requires_manual_sign_in(
    monkeypatch, tmp_path
) -> None:
    config = _save_config(monkeypatch, tmp_path)
    client = _remote_client(config)
    client.set_cookie(cloud_management.HANDLE_COOKIE_NAME, "grant-1", domain="alex.avibe.bot")
    client.set_cookie(cloud_management.BROWSER_COOKIE_NAME, "browser-1", domain="alex.avibe.bot")
    monkeypatch.setattr(cloud_management, "resolve_grant", lambda *args, **kwargs: (_grant(), None))
    monkeypatch.setattr(
        cloud_management,
        "proxy_request",
        lambda *args, **kwargs: (401, {"error": "unauthorized"}),
    )

    response = client.get(
        "/api/cloud-management/organizations",
        base_url=REMOTE_ORIGIN,
    )

    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}
    cookies = response.headers.getlist("set-cookie")
    assert any(
        value.startswith(f"{cloud_management.HANDLE_COOKIE_NAME}=") and "Max-Age=0" in value
        for value in cookies
    )
    assert any(value.startswith(f"{cloud_management.MANUAL_COOKIE_NAME}=") for value in cookies)


def test_callback_state_and_code_fail_with_distinct_errors(monkeypatch, tmp_path) -> None:
    config = _save_config(monkeypatch, tmp_path)
    with pytest.raises(cloud_management.CloudManagementError) as invalid_state:
        cloud_management.complete_authorization(
            config,
            state="missing-state",
            code="code-1",
            browser_id="browser-1",
            remote_subject=None,
        )
    assert invalid_state.value.code == "invalid_cloud_management_state"

    backend = SimpleNamespace(base_url="https://avibe.bot")
    monkeypatch.setattr(cloud_management, "_validated_backend", lambda _config: backend)
    _, state = cloud_management.begin_authorization(
        config,
        browser_id="browser-1",
        remote_subject=None,
        callback_origin=REMOTE_ORIGIN,
        next_path="/admin/organization/overview",
        silent=False,
    )
    with pytest.raises(cloud_management.CloudManagementError) as invalid_code:
        cloud_management.complete_authorization(
            config,
            state=state,
            code="",
            browser_id="browser-1",
            remote_subject=None,
        )
    assert invalid_code.value.code == "invalid_cloud_management_code"


def test_management_token_is_verified_with_real_rsa_jwks(monkeypatch, tmp_path) -> None:
    config = _save_config(monkeypatch, tmp_path)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "management-key", "alg": "RS256", "use": "sig"})
    now = int(time.time())
    claims = {
        "iss": "https://avibe.bot",
        "aud": cloud_management.MANAGEMENT_AUDIENCE,
        "sub": "user-1",
        "email": "alex@example.com",
        "vibe_instance_id": "inst_123",
        "scope": cloud_management.MANAGEMENT_SCOPE,
        "jti": "management-jti",
        "iat": now,
        "exp": now + 600,
    }
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "management-key"},
    )
    monkeypatch.setattr(
        cloud_management,
        "_backend_request",
        lambda *args, **kwargs: (200, {"keys": [public_jwk]}),
    )

    assert cloud_management._validate_management_token(config, token) == claims  # noqa: SLF001

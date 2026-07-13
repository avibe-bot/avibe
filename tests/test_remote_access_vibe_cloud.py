from __future__ import annotations

import ipaddress
import json
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from config import paths
from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from vibe import remote_access
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


def test_session_cookie_roundtrip() -> None:
    config = _config()

    cookie = remote_access.make_session_cookie(config, "alex@example.com", "user-1")

    assert remote_access.validate_session_cookie(config, cookie) is True
    assert remote_access.validate_session_cookie(config, cookie + "x") is False


def test_session_cookie_rejects_empty_session_secret() -> None:
    config = _config()
    config.remote_access.vibe_cloud.session_secret = ""

    assert remote_access.validate_session_cookie(config, "payload.signature") is False


def test_parse_session_cookie_returns_payload_for_fresh_token() -> None:
    config = _config()
    cookie = remote_access.make_session_cookie(config, "alex@example.com", "user-1")

    payload = remote_access.parse_session_cookie(config, cookie)

    assert payload is not None
    assert payload["email"] == "alex@example.com"
    assert payload["sub"] == "user-1"
    assert payload["instance_id"] == "inst_123"


def test_parse_session_cookie_rejects_tampered_signature() -> None:
    config = _config()
    cookie = remote_access.make_session_cookie(config, "alex@example.com", "user-1")

    assert remote_access.parse_session_cookie(config, cookie + "x") is None


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
        remote_access.make_session_cookie(config, "alex@example.com", "user-1")


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

    result = remote_access.pair("vrp_test", "https://backend.test")
    saved_payload = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["pairing"]["ok"] is True
    assert result["start"]["ok"] is True
    assert saved_payload["remote_access"]["vibe_cloud"]["enabled"] is True
    assert saved_payload["remote_access"]["vibe_cloud"]["tunnel_token"] == "tunnel-token"
    assert saved_payload["remote_access"]["vibe_cloud"]["session_secret"]


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


def test_runtime_status_payload_reports_local_origin_and_tunnel_state(monkeypatch, tmp_path) -> None:
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
        },
    )

    payload = remote_access.runtime_status_payload(config, event="heartbeat")

    assert payload["event"] == "heartbeat"
    assert payload["ui_healthy"] is True
    assert payload["tunnel_running"] is True
    assert payload["cloudflared_found"] is True
    assert payload["expected_origin_service"] == "http://127.0.0.1:5123"
    assert payload["observed_origin_service"] == "http://100.97.103.112:5123"


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
    monkeypatch.setattr(remote_access, "start", lambda next_config: {"ok": True, "running": True})
    monkeypatch.setattr(remote_access, "status", lambda next_config=None: {"ok": True, "running": True, "paired": True})
    monkeypatch.setattr(remote_access, "report_runtime_status", lambda *args, **kwargs: {"ok": True})

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is True
    assert save_payloads
    assert set(save_payloads[0]) == {"remote_access"}
    cloud_payload = save_payloads[0]["remote_access"]["vibe_cloud"]
    assert cloud_payload["enabled"] is True
    assert cloud_payload["tunnel_token"] == "tunnel-token"
    assert cloud_payload["session_secret"]


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
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    result = remote_access.pair("vrp_test", "https://backend.test")

    assert result["ok"] is False
    assert result["error"] == "pairing_request_failed"
    assert "offline" in result["detail"]


def test_pair_preserves_backend_error_response(monkeypatch) -> None:
    def fake_request(*args, **kwargs):
        raise remote_access.BackendRequestError(400, {"error": "invalid_pairing_key"})

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
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == new_pid)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"{binary} tunnel run")

    def spawn_background(args, pid_path, stdout_name, stderr_name, env=None):
        assert not remote_access._cloudflared_stdout_path().exists()
        assert not remote_access._cloudflared_stderr_path().exists()
        pid_path.write_text(str(new_pid), encoding="utf-8")
        return new_pid

    monkeypatch.setattr(runtime, "spawn_background", spawn_background)

    result = remote_access.start(config)

    assert result["ok"] is True
    assert result["started"] is True


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


def test_cloud_token_for_request_mints_for_authenticated_user(monkeypatch) -> None:
    config = _cloud_broker_config()
    cookie = remote_access.make_session_cookie(config, "alex@example.com", "user-1")

    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *a, **k: {"access_token": "ct_xyz", "expires_in": 43200},
    )

    before = int(time.time())
    result = remote_access.cloud_token_for_request(config, cookie)
    after = int(time.time())

    assert result is not None
    assert result["base_url"] == "https://avibe.bot"
    assert result["token"] == "ct_xyz"
    assert result["scope"] == "asr"
    assert before + 43200 <= result["expires_at"] <= after + 43200


def test_cloud_token_for_request_returns_none_without_valid_session(monkeypatch) -> None:
    config = _cloud_broker_config()
    monkeypatch.setattr(
        remote_access, "_json_request", lambda *a, **k: {"access_token": "x", "expires_in": 1}
    )

    assert remote_access.cloud_token_for_request(config, None) is None
    assert remote_access.cloud_token_for_request(config, "bogus.cookie") is None

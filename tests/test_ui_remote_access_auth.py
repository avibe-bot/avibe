from __future__ import annotations

import ipaddress
import json
import logging
import socket
import asyncio
from collections import namedtuple

import httpx
import pytest

from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from config.v2_config import CONFIG_LOCK
from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
from vibe import api
from vibe import remote_access
from vibe import ui_server
from vibe.ui_server import app
from starlette.websockets import WebSocketDisconnect


_FakeSnicaddr = namedtuple("snicaddr", ["family", "address", "netmask", "broadcast", "ptp"])


def _mock_interface(monkeypatch, ip: str, prefix: int, name: str = "en0") -> None:
    """Make ``psutil.net_if_addrs()`` report ``ip`` with the given prefix
    length so ``_local_interface_network`` returns the expected subnet.
    Tests that exercise the RFC1918/ULA trust path need this because the
    real test runner does not have the synthetic addresses (192.168.2.3
    etc.) configured on any interface."""
    address = ipaddress.ip_address(ip)
    if address.version == 4:
        family = socket.AF_INET
        netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
    else:
        family = socket.AF_INET6
        netmask = str(ipaddress.IPv6Network(f"::/{prefix}").netmask)
    snic = _FakeSnicaddr(family=family, address=ip, netmask=netmask, broadcast=None, ptp=None)
    monkeypatch.setattr("vibe.ui_server.psutil.net_if_addrs", lambda: {name: [snic]})


def _mock_no_interfaces(monkeypatch) -> None:
    monkeypatch.setattr("vibe.ui_server.psutil.net_if_addrs", lambda: {})


def _mock_tailscale_whois(
    monkeypatch,
    peer: str,
    *,
    addresses: list[str] | None = None,
    allowed_ips: list[str] | None = None,
    payload_key: str = "Node",
) -> None:
    """Stub ``_tailscale_whois`` with the modern ``Node`` payload shape by
    default (host-CIDR address strings, as emitted by current ``tailscale
    whois --json``); pass ``payload_key="Machine"`` for the legacy shape."""
    peer_address = ipaddress.ip_address(peer)
    prefix = peer_address.max_prefixlen
    monkeypatch.setattr(ui_server, "_TAILSCALE_PEER_CACHE", {})
    monkeypatch.setattr(
        ui_server,
        "_tailscale_whois",
        lambda address: {
            payload_key: {
                "Addresses": addresses or [f"{peer_address}/{prefix}"],
                "AllowedIPs": allowed_ips or [f"{peer_address}/{prefix}"],
            }
        }
        if address == peer_address
        else None,
    )


def _save_config(tmp_path) -> V2Config:
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
    cloud.public_url = "https://alex.avibe.bot"
    cloud.client_id = "vr_client_123"
    cloud.instance_id = "inst_123"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    config.save()
    return config


def _oauth_exchange_result(config: V2Config, *, nonce: str) -> dict:
    return {
        "claims": {
            "email": "alex@example.com",
            "sub": "user-1",
            "nonce": nonce,
        },
        "session_claims": {
            "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
            "vibe_instance_role": "owner",
            "vibe_instance_access_source": "owner",
        },
    }


def _remote_peer() -> dict[str, str]:
    return {"REMOTE_ADDR": "203.0.113.10"}


def _cloudflare_headers() -> dict[str, str]:
    return {"CF-Connecting-IP": "198.51.100.10", "CF-Ray": "test-ray"}


def test_remote_host_redirects_to_vibe_cloud_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")
    state = httpx.URL(response.headers["Location"]).params["state"]
    state_payload = ui_server._read_oauth_state(config.remote_access.vibe_cloud.session_secret, state)
    assert state_payload is not None
    assert state_payload["next"] == "/dashboard"
    assert state_payload["retry"] is False


def test_custom_hostname_uses_remote_auth_until_heartbeat_removes_it(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.instance_secret = "instance-secret"
    responses = iter(
        [
            {"ok": True, "active_hostnames": ["max.fileguard.io"]},
            {"ok": True, "active_hostnames": []},
        ]
    )
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *args, **kwargs: {"event": "heartbeat"})
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: next(responses))
    client = app.test_client()

    assert remote_access.report_runtime_status(config)["ok"] is True
    allowed = client.get(
        "/dashboard",
        base_url="https://max.fileguard.io",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert allowed.status_code == 302
    assert httpx.URL(allowed.headers["Location"]).params["redirect_uri"] == (
        "https://max.fileguard.io/auth/callback"
    )

    assert remote_access.report_runtime_status(config)["ok"] is True
    removed = client.get(
        "/dashboard",
        base_url="https://max.fileguard.io",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert removed.status_code == 503
    assert removed.get_json()["error"] == "remote_access_host_mismatch"


def test_persisted_custom_hostname_is_allowed_before_process_cache_warms(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    remote_access._clear_active_hostnames_cache()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://max.fileguard.io",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert remote_access.active_hostnames(config) == frozenset({"max.fileguard.io"})


def test_custom_hostname_oauth_flow_reuses_redirect_uri_for_exchange(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    client = app.test_client()

    login = client.get(
        "/dashboard",
        base_url="https://max.fileguard.io",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )
    authorize_params = httpx.URL(login.headers["Location"]).params
    oauth_cookie_header = next(
        header
        for header in login.headers.getlist("Set-Cookie")
        if header.startswith(f"{ui_server.REMOTE_OAUTH_COOKIE_NAME}=")
    )
    oauth_cookie = oauth_cookie_header.split(";", 1)[0].split("=", 1)[1]
    handshake = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)
    assert handshake is not None
    state_payload = ui_server._read_oauth_state(
        config.remote_access.vibe_cloud.session_secret,
        authorize_params["state"],
    )
    assert state_payload is not None
    stored = remote_access._oauth_handshakes[state_payload["r"]]

    assert authorize_params["redirect_uri"] == "https://max.fileguard.io/auth/callback"
    assert handshake["redirect_uri"] == "https://max.fileguard.io/auth/callback"
    assert stored["redirect_uri"] == "https://max.fileguard.io/auth/callback"

    exchanged = {}

    def exchange(cfg, code, verifier, redirect_uri=None):
        exchanged.update({"code": code, "verifier": verifier, "redirect_uri": redirect_uri})
        return _oauth_exchange_result(cfg, nonce=handshake["nonce"])

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="max.fileguard.io")

    callback = client.get(
        f"/auth/callback?code=test-code&state={authorize_params['state']}",
        base_url="https://max.fileguard.io",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert callback.headers["Location"] == "/dashboard"
    assert exchanged == {
        "code": "test-code",
        "verifier": handshake["code_verifier"],
        "redirect_uri": "https://max.fileguard.io/auth/callback",
    }


def test_oauth_redirect_uri_falls_back_for_unlisted_request_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    with app.test_request_context("/dashboard", base_url="https://unlisted.example"):
        response = ui_server._redirect_to_vibe_cloud_login(config)

    params = httpx.URL(response.headers["Location"]).params
    assert params["redirect_uri"] == config.remote_access.vibe_cloud.redirect_uri


def test_login_redirect_sets_persistent_handshake_cookie(monkeypatch, tmp_path):
    # iOS standalone PWAs drop session-scoped cookies (no Max-Age) across the
    # cross-origin authorize excursion, so the callback can't read the handshake
    # back and deterministically fails with invalid_oauth_state. The handshake
    # cookie must be persistent. Regression guard for the PWA login dead-end.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        response = ui_server._redirect_to_vibe_cloud_login(config)

    set_cookie = response.headers["Set-Cookie"]
    assert set_cookie.startswith(f"{ui_server.REMOTE_OAUTH_COOKIE_NAME}=")
    assert f"Max-Age={ui_server.REMOTE_OAUTH_HANDSHAKE_TTL_SECONDS}" in set_cookie


def test_login_redirect_sets_stable_device_binding_cookie(monkeypatch, tmp_path):
    # The store-fallback recovery is bound to this persistent per-browser device
    # cookie, which (unlike the per-flow handshake state) survives the iOS authorize
    # excursion. The login redirect must seed it, long-lived.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        response = ui_server._redirect_to_vibe_cloud_login(config)

    device_cookies = [
        c for c in response.headers.getlist("Set-Cookie")
        if c.startswith(f"{ui_server.REMOTE_OAUTH_DEVICE_COOKIE_NAME}=")
    ]
    assert len(device_cookies) == 1
    assert f"Max-Age={ui_server.REMOTE_OAUTH_DEVICE_TTL_SECONDS}" in device_cookies[0]
    assert "HttpOnly" in device_cookies[0]
    assert "Secure" in device_cookies[0]


def test_remote_setup_route_requires_vibe_cloud_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    response = app.test_client().get(
        "/setup",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")
    state = httpx.URL(response.headers["Location"]).params["state"]
    state_payload = ui_server._read_oauth_state(config.remote_access.vibe_cloud.session_secret, state)
    assert state_payload is not None
    assert state_payload["next"] == "/setup"


def test_remote_api_get_without_session_returns_login_required(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/api/config",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "remote_access_login_required"
    assert response.headers.get("Location") is None


def test_api_config_blocked_host_returns_machine_readable_error(monkeypatch, tmp_path):
    """Contract the SPA AuthGuard depends on: a blocked GET /api/config returns
    503 with a machine-readable ``error`` code (not a redirect, not an opaque
    body). The guard reads this to show an explicit "access blocked" screen
    instead of bouncing the visitor to the setup wizard."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/api/config",
        base_url="https://old-alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_remote_host_strips_retry_marker_from_oauth_next(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    response = app.test_client().get(
        f"/show/ses123/?foo=bar&{ui_server.REMOTE_OAUTH_RETRY_PARAM}=1",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    state = httpx.URL(response.headers["Location"]).params["state"]
    state_payload = ui_server._read_oauth_state(config.remote_access.vibe_cloud.session_secret, state)
    assert state_payload is not None
    assert state_payload["next"] == "/show/ses123/?foo=bar"
    assert state_payload["retry"] is True


def test_remote_host_with_explicit_port_still_requires_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get("/dashboard", base_url="https://alex.avibe.bot:443", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")


def test_remote_host_with_trailing_dot_still_requires_login(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get("/dashboard", base_url="https://alex.avibe.bot.", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")


def test_remote_health_does_not_require_remote_access_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_localhost_does_not_require_remote_access_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get("/health", base_url="http://127.0.0.1:5123")

    assert response.status_code == 200


def test_live_request_cannot_spoof_test_remote_addr_header(monkeypatch, tmp_path):
    """The compatibility test-client shim accepts an environ_base REMOTE_ADDR,
    but the transport header it uses must not be honored on live ASGI traffic."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    async def _exercise():
        transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5123") as client:
            return await client.get(
                "/dashboard",
                headers={"X-Vibe-Test-Remote-Addr": "127.0.0.1"},
                follow_redirects=False,
            )

    response = asyncio.run(_exercise())

    assert response.status_code == 503
    assert response.json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_host_requires_explicit_trust(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", raising=False)
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_health_probe_is_allowed_when_explicitly_trusted(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 200


def test_docker_loopback_status_probe_is_allowed_when_explicitly_trusted(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/status",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 200


def test_docker_loopback_probe_accepts_ipv4_mapped_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "::ffff:172.17.0.1"},
    )

    assert response.status_code == 200


def test_docker_loopback_trust_does_not_bypass_ui_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 200
    assert "<!doctype html>" in response.text


def test_docker_loopback_trust_requires_loopback_port_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "0.0.0.0")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_ui_requires_loopback_port_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "0.0.0.0")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_trust_still_rejects_non_local_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="https://old-alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_trust_rejects_untrusted_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_trust_requires_configured_peer_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.1"},
    )

    assert response.status_code == 200


def test_docker_loopback_trust_accepts_runtime_default_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", raising=False)
    monkeypatch.setattr(
        ui_server,
        "_docker_route_table_lines",
        lambda: [
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT",
            "eth0\t00000000\t010013AC\t0003\t0\t0\t0\t00000000\t0\t0\t0",
        ],
    )
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.19.0.1"},
    )

    assert response.status_code == 200
    assert "<!doctype html>" in response.text


def test_docker_loopback_trust_rejects_same_network_container_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.17.0.2"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_trust_rejects_non_gateway_peer_on_dynamic_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", raising=False)
    monkeypatch.setattr(
        ui_server,
        "_docker_route_table_lines",
        lambda: [
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT",
            "eth0\t00000000\t010013AC\t0003\t0\t0\t0\t00000000\t0\t0\t0",
        ],
    )
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "172.19.0.2"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_docker_loopback_trust_accepts_ipv4_mapped_configured_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.17.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:15130",
        environ_base={"REMOTE_ADDR": "::ffff:172.17.0.1"},
    )

    assert response.status_code == 200


def test_unmatched_non_local_host_fails_closed_when_remote_access_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://old-alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_loopback_proxy_with_public_host_mismatch_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://old-alex.avibe.bot",
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_loopback_proxy_with_partial_forwarded_metadata_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://old-alex.avibe.bot",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Real-IP": "203.0.113.10"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_loopback_origin_proxy_with_loopback_host_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:15131",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "vibe.example",
        },
        follow_redirects=False,
    )

    assert response.status_code != 503


def test_remote_host_allows_valid_remote_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, remote_session_cookie(config, "alex@example.com", "user-1"), domain="alex.avibe.bot")

    response = client.get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    assert response.status_code != 302


def test_remote_generic_config_omits_memory_projection(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.memory.processing.llm.base_url = "https://llm.example.test/v1"
    config.memory.processing.llm.model = "private-model"
    config.save()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.get(
        "/api/config",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert "memory" not in response.get_json()


def test_remote_session_info_includes_authenticated_subject(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.get("/api/session", base_url="https://alex.avibe.bot")

    assert response.status_code == 200
    assert response.get_json() == {
        "remote": True,
        "authenticated": True,
        "email": "alex@example.com",
        "sub": "user-1",
        "instance_role": "owner",
        "capabilities": {
            "is_instance_owner": True,
            "can_read_instance": True,
            "can_chat": False,
            "can_manage_projects": True,
            "can_manage_agents": True,
            "can_manage_instance": True,
            "can_use_agents": True,
            "can_use_skills": True,
            "can_use_vault_secrets": True,
            "can_use_show_pages": True,
            "can_use_terminal_files": False,
            "can_use_terminal": False,
            "can_use_files": False,
            "can_use_system": False,
        },
    }


def test_remote_file_api_is_blocked_while_local_file_browsing_still_works(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    remote_response = client.get(
        "/api/files/list",
        params={"path": str(tmp_path)},
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    local_response = app.test_client().get(
        "/api/files/list",
        params={"path": str(tmp_path)},
        base_url="http://localhost",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert remote_response.status_code == 403
    assert remote_response.get_json()["code"] == "remote_execution_disabled"
    assert local_response.status_code == 200
    assert local_response.get_json()["path"] == str(tmp_path.resolve())


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("PATCH", "/api/harness/tasks/task-1", {"enabled": False}),
        ("DELETE", "/api/harness/tasks/task-1", None),
        ("PATCH", "/api/harness/watches/watch-1", {"enabled": False}),
        ("DELETE", "/api/harness/watches/watch-1", None),
    ],
)
def test_remote_harness_mutations_are_blocked_before_store_access(
    monkeypatch,
    tmp_path,
    method,
    path,
    json_body,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    response = client.request(
        method,
        path,
        json=json_body,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("path", "json_body", "controller_method"),
    [
        ("/api/sessions/ses-local/cancel", {}, "cancel_dispatch"),
        (
            "/api/running-agents/end",
            {"backend": "codex", "base_session_id": "local-run"},
            "end_running_agent",
        ),
    ],
)
def test_remote_agent_termination_is_blocked_before_controller_access(
    monkeypatch,
    tmp_path,
    path,
    json_body,
    controller_method,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    async def unexpected_controller_call(*args, **kwargs):
        raise AssertionError("remote termination reached the local controller")

    monkeypatch.setattr(
        f"vibe.internal_client.{controller_method}",
        unexpected_controller_call,
    )
    response = client.post(
        path,
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    "json_body",
    [
        {"agent_name": "codex"},
        {"model": "gpt-5"},
        {"reasoning_effort": "high"},
        {"scope_id": "scope-local"},
    ],
)
def test_remote_session_execution_settings_are_blocked_before_store_access(
    monkeypatch,
    tmp_path,
    json_body,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_session_update(*args, **kwargs):
        raise AssertionError("remote session routing reached the local session store")

    monkeypatch.setattr(
        "core.services.sessions.update_session",
        unexpected_session_update,
    )
    response = client.patch(
        "/api/sessions/ses-local",
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", "/api/projects", {"folder_path": "/tmp/remote-project"}),
        ("PATCH", "/api/projects/proj-local", {"folder_path": "/tmp/remote-project"}),
        ("PATCH", "/api/projects/proj-local", {"agent_name": "codex"}),
        ("PATCH", "/api/projects/proj-local", {"model": "gpt-5"}),
    ],
)
def test_remote_project_execution_settings_are_blocked_before_store_access(
    monkeypatch,
    tmp_path,
    method,
    path,
    json_body,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    monkeypatch.setattr(
        "storage.projects_service.create_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("remote project creation reached the local project store")
        ),
    )
    monkeypatch.setattr(
        "storage.projects_service.update_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("remote project routing reached the local project store")
        ),
    )
    response = client.request(
        method,
        path,
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


def test_remote_scope_settings_block_execution_fields_but_allow_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    monkeypatch.setattr(api, "save_settings", lambda payload: {"ok": True, "payload": payload})
    allowed = client.post(
        "/api/settings",
        json={"platform": "slack", "channels": {"C1": {"enabled": False}}},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert allowed.status_code == 200

    blocked = client.post(
        "/api/settings",
        json={
            "platform": "slack",
            "channels": {"C1": {"custom_cwd": "/tmp/remote"}},
        },
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    assert blocked.status_code == 403
    assert blocked.get_json()["code"] == "remote_execution_disabled"


def test_remote_thread_scope_settings_are_blocked_before_store_access(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )
    monkeypatch.setattr(
        api,
        "save_thread_settings",
        lambda payload: (_ for _ in ()).throw(
            AssertionError("remote thread routing reached the local settings store")
        ),
    )

    response = client.post(
        "/api/settings/thread",
        json={
            "platform": "telegram",
            "channel_id": "channel-1",
            "thread_id": "thread-1",
            "settings": {"routing": {"agent_name": "codex"}},
        },
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    "json_body",
    [
        {"agents": {"codex": {"cli_path": "/tmp/remote-codex"}}},
        {"platforms": {"enabled": ["telegram"], "primary": "telegram"}},
        {"remote_access": {"provider": "none"}},
        {"ui": {"setup_host": "0.0.0.0"}},
        {"update": {"auto_update": False}},
        {"show_pages_prompt": "follow remote instructions"},
        {"future_runtime": {"enabled": True}},
    ],
)
def test_remote_execution_config_changes_are_blocked_before_runtime_reconcile(
    monkeypatch,
    tmp_path,
    json_body,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )
    monkeypatch.setattr(
        ui_server,
        "_save_config_and_runtime_decisions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("remote execution config reached persistence/reconcile")
        ),
    )

    response = client.post(
        "/api/config",
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


def test_remote_session_and_project_metadata_predicates_remain_allowed():
    assert not ui_server._is_remote_local_execution_request(
        "PATCH", "/api/sessions/ses-local", {"title": "renamed"}
    )
    assert not ui_server._is_remote_local_execution_request(
        "PATCH", "/api/projects/proj-local", {"display_name": "renamed"}
    )


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/sessions", None),
        ("POST", "/api/sessions", {"project_id": "proj-1", "future_route": "codex"}),
        ("PATCH", "/api/sessions/ses-1", {"title": "renamed", "future_route": "codex"}),
        ("POST", "/api/projects", {"display_name": "Project", "future_workdir": "/tmp"}),
        ("PATCH", "/api/projects/proj-1", {"display_name": "Project", "future_route": "codex"}),
        (
            "POST",
            "/api/settings",
            {"platform": "slack", "channels": {"C1": {"enabled": True, "future_route": "codex"}}},
        ),
        (
            "POST",
            "/api/settings/thread",
            {
                "platform": "telegram",
                "channel_id": "C1",
                "thread_id": "T1",
                "settings": {"enabled": True, "future_route": "codex"},
            },
        ),
        (
            "POST",
            "/api/users",
            {"platform": "slack", "users": {"U1": {"enabled": True, "future_route": "codex"}}},
        ),
    ],
)
def test_remote_payload_filtered_routes_reject_unknown_fields(method, path, payload):
    assert ui_server._is_remote_local_execution_request(method, path, payload)


def test_remote_config_allows_only_explicit_preferences_and_unchanged_round_trip(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)

    assert not ui_server._is_remote_local_execution_request(
        "POST",
        "/api/config",
        {"language": "zh", "ui": {"instance_name": "Remote label"}},
    )
    assert not ui_server._is_remote_local_execution_request(
        "POST",
        "/api/config",
        json.loads(json.dumps(api.config_to_payload(config))),
    )
    assert ui_server._is_remote_local_execution_request(
        "POST",
        "/api/config",
        {"ui": {"future_setting": True}},
    )


def test_remote_config_strips_protected_round_trip_fields_before_persistence(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )
    payload = json.loads(json.dumps(api.config_to_payload(config)))
    payload["language"] = "zh"
    observed = []

    def save_config(remote_payload):
        observed.append(remote_payload)
        return config, False, False, []

    monkeypatch.setattr(ui_server, "_save_config_and_runtime_decisions", save_config)
    monkeypatch.setattr(ui_server, "_ensure_remote_access_monitoring", lambda _config: None)
    response = client.post(
        "/api/config",
        json=payload,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert len(observed) == 1
    assert observed[0]["language"] == "zh"
    assert observed[0]["ui"] == {
        field: payload["ui"][field]
        for field in ui_server._REMOTE_UI_CONFIG_MUTABLE_FIELDS
    }
    assert set(observed[0]).issubset(
        ui_server._REMOTE_CONFIG_MUTABLE_FIELDS | {"ui"}
    )
    assert "runtime" not in observed[0]
    assert "agents" not in observed[0]
    assert "remote_access" not in observed[0]


def test_remote_session_archive_is_blocked_before_store_access(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_archive(*args, **kwargs):
        raise AssertionError("remote archive reached the local session store")

    monkeypatch.setattr(
        "core.services.sessions.archive_session",
        unexpected_archive,
    )
    response = client.delete(
        "/api/sessions/ses-local",
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("path", "json_body", "call_target"),
    [
        ("/api/control", {"action": "restart"}, "vibe.runtime.read_status"),
        ("/api/upgrade", {}, "vibe.api.do_upgrade"),
        ("/api/agent/codex/install", {}, "vibe.api.start_agent_install_job"),
        (
            "/api/dependencies/askill/install",
            {},
            "vibe.api.start_dependency_install_job",
        ),
    ],
)
def test_remote_system_operations_are_blocked_before_runtime_calls(
    monkeypatch,
    tmp_path,
    path,
    json_body,
    call_target,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_runtime_call(*args, **kwargs):
        raise AssertionError("remote system operation reached the local runtime")

    monkeypatch.setattr(call_target, unexpected_runtime_call)
    response = client.post(
        path,
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("method", "path", "json_body", "call_target"),
    [
        ("GET", "/api/doctor", None, "vibe.ui_server.paths.get_runtime_doctor_path"),
        ("POST", "/api/doctor", {}, "vibe.cli._doctor"),
        ("POST", "/api/logs", {}, "vibe.ui_server._resolve_log_sources"),
        ("POST", "/api/ui/reload", {"host": "127.0.0.1", "port": 5123}, "vibe.runtime.read_status"),
        ("POST", "/api/opencode/options", {"cwd": "/tmp/remote"}, "vibe.api.opencode_options_async"),
        ("POST", "/api/opencode/setup-permission", {}, "vibe.api.setup_opencode_permission"),
    ],
)
def test_remote_diagnostics_and_legacy_system_helpers_are_blocked_before_local_access(
    monkeypatch,
    tmp_path,
    method,
    path,
    json_body,
    call_target,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_local_access(*args, **kwargs):
        raise AssertionError(f"remote request reached local capability: {path}")

    monkeypatch.setattr(call_target, unexpected_local_access)
    request_kwargs = {
        "headers": csrf_headers(client, "https://alex.avibe.bot"),
        "base_url": "https://alex.avibe.bot",
        "environ_base": _remote_peer(),
    }
    if json_body is not None:
        request_kwargs["json"] = json_body
    response = client.request(method, path, **request_kwargs)

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("path", "call_target"),
    [
        ("/api/models/runtime/start", "vibe.ui_server._model_hub_service"),
        ("/api/models/agents/codex/probe", "vibe.ui_server._model_hub_service"),
        ("/api/backend/codex/restart", "vibe.api.restart_backend"),
        ("/api/backend/codex/auth/test", "vibe.api.test_backend_auth_async"),
    ],
)
def test_remote_model_and_backend_operations_are_blocked_before_runtime_calls(
    monkeypatch,
    tmp_path,
    path,
    call_target,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_runtime_call(*args, **kwargs):
        raise AssertionError("remote backend operation reached the local runtime")

    monkeypatch.setattr(call_target, unexpected_runtime_call)
    response = client.post(
        path,
        json={},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


def test_remote_show_page_icon_upload_is_blocked_before_filesystem_access(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_icon_write(*args, **kwargs):
        raise AssertionError("remote icon upload reached the local filesystem")

    monkeypatch.setattr(api, "upload_show_page_icon", unexpected_icon_write)
    response = client.post(
        "/api/show-pages/ses-local/icon",
        files={"file": ("icon.svg", b"<svg/>", "image/svg+xml")},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    "path",
    [
        "/api/models/runtime/status",
        "/api/models/agents",
        "/api/backend/codex/runtime",
        "/api/backend/codex/auth",
    ],
)
def test_remote_model_and_backend_reads_remain_available(path):
    assert not ui_server._is_remote_local_execution_request("GET", path)


@pytest.mark.parametrize(
    ("method", "path", "json_body", "api_method"),
    [
        ("POST", "/api/agents", {"name": "remote"}, "create_vibe_agent"),
        ("POST", "/api/agents/import", {}, "import_vibe_agents"),
        ("POST", "/api/agents/default", {"name": "remote"}, "set_default_vibe_agent"),
        ("PATCH", "/api/agents/remote", {"enabled": False}, "update_vibe_agent"),
        ("DELETE", "/api/agents/remote", None, "remove_vibe_agent"),
    ],
)
def test_remote_agent_definition_mutations_are_blocked_before_api_calls(
    monkeypatch,
    tmp_path,
    method,
    path,
    json_body,
    api_method,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_agent_call(*args, **kwargs):
        raise AssertionError("remote Agent mutation reached the local Agent API")

    monkeypatch.setattr(api, api_method, unexpected_agent_call)
    response = client.request(
        method,
        path,
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("method", "path", "json_body", "api_method"),
    [
        ("POST", "/api/skills", {"source": "gh:owner/repo"}, "add_skill"),
        ("POST", "/api/skills/update", {"name": "demo"}, "update_skill"),
        ("POST", "/api/skills/upload", {"content_base64": ""}, "upload_skill_zip"),
        ("DELETE", "/api/skills/demo", None, "remove_skill"),
    ],
)
def test_remote_skill_mutations_are_blocked_before_api_calls(
    monkeypatch,
    tmp_path,
    method,
    path,
    json_body,
    api_method,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    async def unexpected_skill_call(*args, **kwargs):
        raise AssertionError("remote skill mutation reached the local Skills API")

    monkeypatch.setattr(api, api_method, unexpected_skill_call)
    response = client.request(
        method,
        path,
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


@pytest.mark.parametrize(
    ("path", "json_body", "write_target"),
    [
        (
            "/api/projects/proj-local/agents-md",
            {"content": "remote instructions"},
            "vibe.project_agents_md.save_agents_md",
        ),
        (
            "/api/global-prompts",
            {"content": "remote instructions", "backends": ["codex"]},
            "vibe.global_agents_md.write_many_global_agents_md",
        ),
    ],
)
def test_remote_agent_instruction_writes_are_blocked_before_filesystem_access(
    monkeypatch,
    tmp_path,
    path,
    json_body,
    write_target,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_instruction_write(*args, **kwargs):
        raise AssertionError("remote instruction mutation reached the local filesystem")

    monkeypatch.setattr(write_target, unexpected_instruction_write)
    response = client.put(
        path,
        json=json_body,
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


def test_remote_queue_deletion_is_blocked_before_store_access(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    def unexpected_queue_retirement(*args, **kwargs):
        raise AssertionError("remote queue deletion reached the local delivery store")

    monkeypatch.setattr(
        "storage.message_deliveries.retire_queued_with_run",
        unexpected_queue_retirement,
    )
    response = client.delete(
        "/api/sessions/ses-local/queue/del-local",
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


def test_remote_owner_can_still_read_agent_instructions_and_queue(
    monkeypatch,
    tmp_path,
):
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    ensure_sqlite_state()
    monkeypatch.setattr(ui_server, "_resolve_project_dir", lambda project_id: str(tmp_path))
    monkeypatch.setattr(
        "vibe.global_agents_md.read_all_global_agents_md",
        lambda: [],
    )
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )

    project_response = client.get(
        "/api/projects/proj-local/agents-md",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    global_response = client.get(
        "/api/global-prompts",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )
    queue_response = client.get(
        "/api/sessions/ses-local/queue",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert project_response.status_code == 200
    assert project_response.get_json()["source"] == "none"
    assert global_response.status_code == 200
    assert global_response.get_json() == {"backends": []}
    assert queue_response.status_code == 200
    assert queue_response.get_json() == {"queued": []}
    assert not ui_server._is_remote_local_execution_request("POST", "/api/skills/preview")


def test_remote_show_dispatch_is_rejected_before_event_reservation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )
    monkeypatch.setattr(ui_server, "_is_cli_show_event_request", lambda: True)

    def unexpected_store():
        raise AssertionError("remote Show dispatch must not reserve an event")

    monkeypatch.setattr(ui_server, "_show_session_event_store", unexpected_store)
    response = client.post(
        "/api/show/sessions/ses-remote/events",
        json={
            "type": "human.intent.submitted",
            "actor": "human",
            "payload": {"intent": "choose", "dispatch": True},
        },
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "remote_execution_disabled"


def test_remote_owner_can_still_read_authorized_session_history(
    monkeypatch,
    tmp_path,
):
    from storage import messages_service, workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.settings_service import upsert_scope

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_remote_history",
            now="2026-08-04T00:00:00Z",
        )
        session = workbench_sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="codex",
            agent_name="worker",
        )
        messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session["id"],
            platform="avibe",
            author="user",
            message_type="user",
            text="readable history",
        )

    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "owner@example.com", "user-owner"),
        domain="alex.avibe.bot",
    )
    response = client.get(
        f"/api/sessions/{session['id']}/messages",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200
    assert [row["text"] for row in response.get_json()["messages"]] == [
        "readable history"
    ]


def test_remote_viewer_can_read_but_cannot_use_management_api(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "viewer@example.com",
            "user-viewer",
            role="viewer",
            access_source="owner",
        ),
        domain="alex.avibe.bot",
    )

    read_response = client.get("/api/version", base_url="https://alex.avibe.bot")
    manage_response = client.get("/api/remote-access/status", base_url="https://alex.avibe.bot")
    session_response = client.get("/api/session", base_url="https://alex.avibe.bot")

    assert read_response.status_code == 200
    assert manage_response.status_code == 403
    assert manage_response.get_json()["error"] == "instance_access_forbidden"
    session_payload = session_response.get_json()
    assert session_payload["instance_role"] == "viewer"
    assert session_payload["capabilities"]["can_read_instance"] is True
    assert session_payload["capabilities"]["can_chat"] is False
    assert session_payload["capabilities"]["is_instance_owner"] is False


@pytest.mark.parametrize(
    (
        "role",
        "agents_status",
        "skills_status",
        "vault_status",
        "show_pages_status",
        "conversation_status",
        "project_status",
    ),
    [
        ("viewer", 403, 403, 403, 200, 403, 403),
        ("editor", 200, 200, 200, 200, 400, 403),
        ("owner", 200, 200, 200, 200, 400, 400),
    ],
)
def test_remote_instance_role_route_matrix(
    monkeypatch,
    tmp_path,
    role,
    agents_status,
    skills_status,
    vault_status,
    show_pages_status,
    conversation_status,
    project_status,
):
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    ensure_sqlite_state()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, f"{role}@example.com", f"user-{role}", role=role),
        domain="alex.avibe.bot",
    )
    headers = csrf_headers(client, "https://alex.avibe.bot")

    read_response = client.get("/api/projects", base_url="https://alex.avibe.bot")
    agents_response = client.get("/api/agents", base_url="https://alex.avibe.bot")
    skills_response = client.get("/api/skills", base_url="https://alex.avibe.bot")
    vault_response = client.get("/api/vault/secrets", base_url="https://alex.avibe.bot")
    show_pages_response = client.get("/api/show-pages", base_url="https://alex.avibe.bot")
    config_response = client.get("/api/config", base_url="https://alex.avibe.bot")
    prefs_read_response = client.get("/api/workbench/prefs", base_url="https://alex.avibe.bot")
    prefs_write_response = client.put(
        "/api/workbench/prefs",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={"background_work_banner_enabled": False},
    )
    conversation_response = client.post(
        "/api/sessions",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={},
    )
    project_response = client.post(
        "/api/projects",
        base_url="https://alex.avibe.bot",
        headers=headers,
        json={},
    )

    assert read_response.status_code == 200
    assert agents_response.status_code == agents_status
    assert skills_response.status_code == skills_status
    assert vault_response.status_code == vault_status
    assert show_pages_response.status_code == show_pages_status
    assert config_response.status_code == 200
    assert prefs_read_response.status_code == 200
    assert prefs_read_response.get_json()["background_work_banner_enabled"] is True
    assert prefs_write_response.status_code == (200 if role == "owner" else 403)
    if role == "owner":
        assert "runtime" in config_response.get_json()
    else:
        assert set(config_response.get_json()) == {
            "capabilities",
            "language",
            "mode",
            "setup_state",
            "ui",
            "version",
        }
    assert conversation_response.status_code == conversation_status
    assert project_response.status_code == project_status
    if role == "viewer":
        assert conversation_response.get_json()["error"] == "instance_access_forbidden"
    if role != "owner":
        assert project_response.get_json()["error"] == "instance_access_forbidden"


def _forged_session_cookie(config: V2Config, exp: int, *, email: str = "alex@example.com", subject: str = "user-1") -> str:
    import json
    import urllib.parse

    cloud = config.remote_access.vibe_cloud
    payload = {
        "email": email,
        "sub": subject,
        "instance_id": cloud.instance_id,
        "vibe_instance_id": cloud.instance_id,
        "vibe_instance_role": "owner",
        "vibe_instance_access_source": "owner",
        "iat": exp - remote_access.SESSION_TTL_SECONDS,
        "exp": exp,
        "claims_issued_at": exp - remote_access.SESSION_TTL_SECONDS,
    }
    payload_text = urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe="")
    signature = remote_access._session_signature(cloud.session_secret, payload_text)
    return f"{payload_text}.{signature}"


def test_remote_api_get_with_expired_session_returns_login_required(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _forged_session_cookie(config, int(_time.time()) - 60),
        domain="alex.avibe.bot",
    )

    response = client.get(
        "/api/config",
        base_url="https://alex.avibe.bot",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "remote_access_login_required"
    assert response.headers.get("Location") is None


def test_remote_session_probe_reports_unauthenticated_when_authorization_refresh_required(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _forged_session_cookie(config, near_exp),
        domain="alex.avibe.bot",
    )

    response = client.get("/api/session", base_url="https://alex.avibe.bot")

    assert response.status_code == 200
    assert response.get_json() == {
        "remote": True,
        "authenticated": False,
        "authorization_refresh_required": True,
    }


def test_cloud_token_requires_authorization_refresh_before_mint(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    mint_called = False

    def fake_mint(*args, **kwargs):
        nonlocal mint_called
        mint_called = True
        return {"access_token": "must-not-mint", "expires_in": 60}

    monkeypatch.setattr(remote_access, "mint_cloud_token", fake_mint)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        _forged_session_cookie(config, near_exp),
        domain="alex.avibe.bot",
    )

    response = client.get("/api/cloud/token", base_url="https://alex.avibe.bot")

    assert response.status_code == 401
    assert response.get_json()["error"] == "remote_access_authorization_refresh_required"
    assert mint_called is False


def test_remote_host_does_not_renew_fresh_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    set_cookie_headers = response.headers.getlist("Set-Cookie")
    assert not any(h.startswith(f"{remote_access.SESSION_COOKIE_NAME}=") for h in set_cookie_headers)


def test_remote_page_refreshes_authorization_past_half_ttl(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    cookie = _forged_session_cookie(config, near_exp)
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")

    response = client.get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith(config.remote_access.vibe_cloud.authorization_endpoint)
    assert not any(
        header.startswith(f"{remote_access.SESSION_COOKIE_NAME}=")
        for header in response.headers.getlist("Set-Cookie")
    )


def test_remote_api_requests_signal_authorization_refresh(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    cookie = _forged_session_cookie(config, near_exp)
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")

    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"enabled": False}}},
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "remote_access_authorization_refresh_required"
    refreshed = next(
        (h for h in response.headers.getlist("Set-Cookie") if h.startswith(f"{remote_access.SESSION_COOKIE_NAME}=")),
        None,
    )
    assert refreshed is None


def test_remote_api_get_requires_top_level_authorization_refresh(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    cookie = _forged_session_cookie(config, near_exp)
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")

    response = client.get(
        "/api/config",
        base_url="https://alex.avibe.bot",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "remote_access_authorization_refresh_required"
    assert response.headers.get("Location") is None


def test_remote_host_fails_closed_when_config_load_fails(monkeypatch):
    def fail_load():
        raise ValueError("corrupt config")

    monkeypatch.setattr(ui_server.V2Config, "load", fail_load)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_config_unavailable"


def test_host_starting_with_127_but_not_ip_is_not_local_when_config_load_fails(monkeypatch):
    def fail_load():
        raise ValueError("corrupt config")

    monkeypatch.setattr(ui_server.V2Config, "load", fail_load)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://127.attacker.example",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_config_unavailable"


def test_loopback_peer_with_arbitrary_host_is_not_local_when_config_load_fails(monkeypatch):
    def fail_load():
        raise ValueError("corrupt config")

    monkeypatch.setattr(ui_server.V2Config, "load", fail_load)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://attacker.example",
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_config_unavailable"


def test_spoofed_loopback_host_is_not_local_when_peer_is_remote(monkeypatch):
    def fail_load():
        raise ValueError("corrupt config")

    monkeypatch.setattr(ui_server.V2Config, "load", fail_load)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://127.0.0.1",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_config_unavailable"


def test_cloudflare_forwarded_request_with_loopback_host_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="https://127.0.0.1",
        headers=_cloudflare_headers(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_trusted_proxy_forwarded_host_routes_remote_access(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    config = _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:5123",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "alex.avibe.bot",
            "X-Forwarded-For": "203.0.113.10",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith(config.remote_access.vibe_cloud.authorization_endpoint)


def test_ra_tq_026_remote_status_uses_cf_ray_on_paired_public_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
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
    with app.test_request_context(
        "/api/remote-access/status",
        base_url="https://alex.avibe.bot",
        headers={"CF-Ray": "9f1234567890abcd-SIN"},
    ):
        response = ui_server.remote_access_status()

    assert response.status_code == 200
    assert observed[0][0].remote_access.vibe_cloud.public_url == config.remote_access.vibe_cloud.public_url
    assert observed[0][1] == "SIN"
    assert observed[0][2] == "remote"
    assert observed[0][3] is True


def test_ra_tq_026_remote_status_ignores_spoofed_cf_ray(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
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
    with app.test_request_context(
        "/api/remote-access/status",
        base_url="http://127.0.0.1:5123",
        headers={"CF-Ray": "9f1234567890abcd-NRT"},
    ):
        response = ui_server.remote_access_status()

    assert response.status_code == 200
    assert observed[0][1] is None
    assert observed[0][2] == "local"
    assert observed[0][3] is True
    assert config.remote_access.vibe_cloud.public_url == "https://alex.avibe.bot"


def test_trusted_proxy_missing_forwarded_host_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:5123",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "8.8.8.8",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_trusted_proxy_unmatched_forwarded_host_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:5123",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-For": "8.8.8.8",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_trusted_proxy_malformed_forwarded_host_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    _save_config(tmp_path)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://127.0.0.1:5123",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "[bad",
            "X-Forwarded-For": "8.8.8.8",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_remote_host_fails_closed_when_disabled_but_hostname_still_matches(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_disabled"


def test_unmatched_non_local_host_fails_closed_when_remote_access_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://old-alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_remote_host_fails_closed_when_public_url_is_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.public_url = "alex.avibe.bot"
    config.save()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_public_url_invalid"


def test_remote_host_fails_closed_when_public_url_is_http(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.public_url = "http://alex.avibe.bot"
    config.save()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_public_url_invalid"


def test_remote_host_fails_closed_when_public_url_contains_userinfo(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.public_url = "https://user:pass@alex.avibe.bot"
    config.save()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_public_url_invalid"


def test_remote_host_fails_closed_when_public_url_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.public_url = ""
    config.save()

    response = app.test_client().get(
        "/dashboard",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_public_url_invalid"


def test_remote_host_fails_closed_when_session_secret_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.session_secret = ""
    config.save()

    response = app.test_client().get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_session_secret_missing"


def test_config_post_rotates_session_secret_when_remote_access_is_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    old_secret = config.remote_access.vibe_cloud.session_secret
    client = app.test_client()

    monkeypatch.setattr(remote_access, "reconcile", lambda: {"ok": True, "stopped": True})

    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"enabled": False}}},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )
    saved = V2Config.load()

    assert response.status_code == 200
    assert saved.remote_access.vibe_cloud.enabled is False
    assert saved.remote_access.vibe_cloud.session_secret
    assert saved.remote_access.vibe_cloud.session_secret != old_secret


def test_config_post_skips_reconcile_when_remote_access_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    reconcile_calls = []

    monkeypatch.setattr(remote_access, "reconcile", lambda: reconcile_calls.append(True) or {"ok": True})

    response = client.post(
        "/api/config",
        json=api.config_to_payload(config),
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert reconcile_calls == []


def test_remote_config_post_accepts_public_origin_default_https_port(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.public_url = "https://alex.avibe.bot:443"
    config.save()
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )
    headers = csrf_headers(client, "https://alex.avibe.bot")

    response = client.post(
        "/api/config",
        json=api.config_to_payload(config),
        headers=headers,
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200


def test_custom_hostname_config_post_accepts_same_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(
            config,
            "alex@example.com",
            "user-1",
            session_claims=_oauth_exchange_result(config, nonce="unused")["session_claims"],
        ),
        domain="max.fileguard.io",
    )
    headers = csrf_headers(client, "https://max.fileguard.io")

    response = client.post(
        "/api/config",
        json=api.config_to_payload(config),
        headers=headers,
        base_url="https://max.fileguard.io",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 200


def test_config_post_returns_saved_config_when_remote_reconcile_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    old_secret = config.remote_access.vibe_cloud.session_secret
    client = app.test_client()

    monkeypatch.setattr(remote_access, "reconcile", lambda: {"ok": False, "error": "cloudflared_stop_failed"})

    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"enabled": False}}},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )
    saved = V2Config.load()
    body = response.get_json()

    assert response.status_code == 200
    assert body["remote_access_runtime"]["ok"] is False
    assert body["remote_access_runtime"]["error"] == "cloudflared_stop_failed"
    assert saved.remote_access.vibe_cloud.enabled is False
    assert saved.remote_access.vibe_cloud.session_secret != old_secret


def test_config_post_reconciles_after_releasing_config_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()
    lock_states = []

    def reconcile():
        lock_states.append(CONFIG_LOCK._is_owned())
        return {"ok": True, "stopped": True}

    monkeypatch.setattr(remote_access, "reconcile", reconcile)

    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"enabled": False}}},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert lock_states == [False]


def test_config_post_reconciles_from_fresh_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()
    reconcile_args = []

    def reconcile(*args):
        reconcile_args.append(args)
        return {"ok": True, "stopped": True}

    monkeypatch.setattr(remote_access, "reconcile", reconcile)

    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"enabled": False}}},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert reconcile_args == [()]


def test_remote_callback_rejects_nonce_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    monkeypatch.setattr(
        remote_access,
        "exchange_oauth_code",
        lambda cfg, code, verifier, redirect_uri=None: _oauth_exchange_result(
            cfg,
            nonce="wrong-nonce",
        ),
    )

    state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)["state"]
    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    assert "text/html" in response.headers["Content-Type"]
    assert "invalid_oauth_nonce" in response.text
    assert "Sign in again" in response.text
    # Re-login button points back at the original destination from the handshake.
    assert 'href="/dashboard"' in response.text


def test_remote_callback_externalizes_large_organization_claims(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")
    oauth_state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)
    group_ids = [f"00000000-0000-4000-8000-{index:012d}" for index in range(100)]

    monkeypatch.setattr(
        remote_access,
        "exchange_oauth_code",
        lambda cfg, code, verifier, redirect_uri=None: {
            "claims": {
                "email": "member@example.com",
                "sub": "user-1",
                "nonce": oauth_state["nonce"],
            },
            "session_claims": {
                "vibe_instance_id": "inst_123",
                "vibe_instance_role": "viewer",
                "vibe_instance_access_source": "organization_group",
                "vibe_organization_id": "org-1",
                "vibe_organization_member_id": "member-1",
                "vibe_organization_role": "member",
                "vibe_group_ids": group_ids,
            },
        },
    )

    response = client.get(
        f"/auth/callback?code=test-code&state={oauth_state['state']}",
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard"
    session_header = next(
        header
        for header in response.headers.getlist("Set-Cookie")
        if header.startswith(f"{remote_access.SESSION_COOKIE_NAME}=")
    )
    session_cookie = session_header.split(";", 1)[0].split("=", 1)[1]
    assert len(session_cookie.encode("ascii")) <= remote_access.SESSION_COOKIE_MAX_VALUE_BYTES
    payload = remote_access.parse_session_cookie(config, session_cookie)
    assert payload is not None
    assert payload["vibe_group_ids"] == group_ids


def test_remote_callback_explains_pairing_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    def exchange(cfg, code, verifier, redirect_uri=None):
        raise remote_access.OAuthCodeExchangeError("invalid_instance_id")

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)

    state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)["state"]
    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    assert "text/html" in response.headers["Content-Type"]
    assert "remote_pairing_mismatch" in response.text
    assert "Reconnect this Avibe" in response.text
    assert "pair Remote Access again" in response.text
    assert "Technical details" in response.text
    assert "reason: invalid_instance_id" in response.text
    assert "error: remote_pairing_mismatch" in response.text


def test_remote_callback_explains_clock_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    def exchange(cfg, code, verifier, redirect_uri=None):
        raise remote_access.OAuthCodeExchangeError("expired_id_token")

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)

    state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)["state"]
    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    assert "oauth_time_mismatch" in response.text
    assert "Check this machine&#x27;s clock" in response.text
    assert "reason: expired_id_token" in response.text


def test_remote_callback_redacts_quoted_oauth_details(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    def exchange(cfg, code, verifier, redirect_uri=None):
        raise remote_access.OAuthCodeExchangeError(
            "token_endpoint_rejected",
            '{"code":"secret-code","code_verifier":"secret-verifier","detail":"bad code"}',
        )

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)

    state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)["state"]
    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    assert "detail:" in response.text
    assert "code=&lt;redacted&gt;" in response.text
    assert "code_verifier=&lt;redacted&gt;" in response.text
    assert "secret-code" not in response.text
    assert "secret-verifier" not in response.text
    assert "test-code" not in response.text


def test_remote_callback_log_omits_raw_oauth_rejection_detail(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    def exchange(cfg, code, verifier, redirect_uri=None):
        raise remote_access.OAuthCodeExchangeError("token_endpoint_rejected", '{"code":"secret-code"}')

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)
    with ui_server._oauth_diag_log_lock:
        ui_server._oauth_diag_log_state.pop("exchange_failed", None)
    caplog.set_level(logging.WARNING, logger="vibe.ui_server")

    state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)["state"]
    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=token_endpoint_rejected" in messages
    assert "secret-code" not in messages
    assert "test-code" not in messages


def test_remote_callback_rejects_when_remote_access_is_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    oauth_cookie = ui_server._make_oauth_cookie(
        config.remote_access.vibe_cloud.session_secret,
        {
            "state": "state-1",
            "nonce": "nonce-1",
            "code_verifier": "verifier-1",
            "next": "/dashboard",
            "exp": int(ui_server.datetime.now().timestamp()) + 300,
        },
    )
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    exchange_calls = []
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    monkeypatch.setattr(
        remote_access,
        "exchange_oauth_code",
        lambda *args, **kwargs: exchange_calls.append(args) or {"claims": {"nonce": "nonce-1"}},
    )

    response = client.get("/auth/callback?code=test-code&state=state-1", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    assert response.get_json()["error"] == "remote_access_disabled"
    assert exchange_calls == []


def test_remote_callback_restarts_oauth_when_state_cookie_was_lost(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    state = ui_server._make_oauth_state(
        config.remote_access.vibe_cloud.session_secret,
        next_target="/show/ses123/?tab=flow",
    )
    exchange_calls = []
    monkeypatch.setattr(remote_access, "exchange_oauth_code", lambda *args, **kwargs: exchange_calls.append(args))

    response = client.get(
        f"/auth/callback?code=test-code&state={state}",
        base_url="https://alex.avibe.bot",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == f"/show/ses123/?tab=flow&{ui_server.REMOTE_OAUTH_RETRY_PARAM}=1"
    assert ui_server.REMOTE_OAUTH_COOKIE_NAME in response.headers["Set-Cookie"]
    assert exchange_calls == []


def test_remote_callback_does_not_restart_oauth_twice(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    state = ui_server._make_oauth_state(
        config.remote_access.vibe_cloud.session_secret,
        next_target="/show/ses123/",
        retry=True,
    )

    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    # Auto-retry already spent: render the friendly re-login page, not raw JSON.
    assert response.status_code == 400
    assert "text/html" in response.headers["Content-Type"]
    assert "invalid_oauth_state" in response.text
    assert "Sign in again" in response.text
    # Retry recovers the original destination from the signed state param.
    assert 'href="/show/ses123/"' in response.text


def test_remote_callback_renders_relogin_page_for_legacy_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()

    response = client.get("/auth/callback?code=test-code&state=state-1", base_url="https://alex.avibe.bot")

    # Undecodable state has no recoverable destination, so the retry button
    # falls back to the home page.
    assert response.status_code == 400
    assert "text/html" in response.headers["Content-Type"]
    assert "invalid_oauth_state" in response.text
    assert "Sign in again" in response.text
    assert 'href="/"' in response.text


def test_remote_callback_diagnostics_do_not_expose_oauth_parameters(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()

    response = client.get(
        "/auth/callback?code=secret-code&state=secret-state",
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 400
    assert "invalid_oauth_state" in response.text
    assert "Technical details" in response.text
    assert "error: invalid_oauth_state" in response.text
    assert "host: alex.avibe.bot" in response.text
    assert "secret-code" not in response.text
    assert "secret-state" not in response.text


def test_remote_callback_recovers_via_store_when_cookie_state_desyncs(monkeypatch, tmp_path):
    # iOS standalone PWA: the handshake cookie carries a *different* (but valid)
    # state than the one the user approved, because the cross-origin authorize step
    # runs in a separate in-app-browser context. The callback must still complete by
    # recovering the PKCE secrets from the server-side store, keyed by the signed URL
    # state. Regression guard for the deterministic PWA invalid_oauth_state dead-end.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    secret = config.remote_access.vibe_cloud.session_secret
    client = app.test_client()

    # The flow the user actually approved: a signed state plus its server-side record,
    # bound to this browser's stable device id.
    rid = "approvedrid000"
    device_id = "device-abc-123"
    state_url = ui_server._make_oauth_state(secret, next_target="/dashboard", rid=rid)
    remote_access.store_oauth_handshake(
        rid,
        nonce="nonce-approved",
        code_verifier="verifier-approved",
        next_target="/dashboard",
        device_hash=ui_server._oauth_device_hash(secret, device_id),
        redirect_uri="https://alex.avibe.bot/auth/callback",
    )

    # A stale-but-valid cookie from a *different* GET / generation (different state).
    stale_cookie = ui_server._make_oauth_cookie(
        secret,
        {
            "state": ui_server._make_oauth_state(secret, next_target="/", rid="stalerid0000"),
            "nonce": "nonce-stale",
            "code_verifier": "verifier-stale",
            "next": "/",
            "exp": int(ui_server.datetime.now().timestamp()) + 300,
        },
    )
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, stale_cookie, domain="alex.avibe.bot")
    # The device cookie is stable across the excursion and matches the record's bind.
    client.set_cookie(ui_server.REMOTE_OAUTH_DEVICE_COOKIE_NAME, device_id, domain="alex.avibe.bot")

    captured = {}

    def exchange(cfg, code, verifier, redirect_uri=None):
        captured["verifier"] = verifier
        captured["redirect_uri"] = redirect_uri
        return _oauth_exchange_result(cfg, nonce="nonce-approved")

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)

    response = client.get(
        f"/auth/callback?code=test-code&state={state_url}",
        base_url="https://alex.avibe.bot",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard"
    # Used the server-side record's verifier, not the stale cookie's.
    assert captured["verifier"] == "verifier-approved"
    assert captured["redirect_uri"] == "https://alex.avibe.bot/auth/callback"
    # Handshake is single-use: consumed by the callback.
    assert remote_access.pop_oauth_handshake(rid) is None


def test_oauth_handshake_store_is_single_use_and_expires(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    remote_access.store_oauth_handshake("rid-abc", nonce="n", code_verifier="v", next_target="/x")
    first = remote_access.pop_oauth_handshake("rid-abc")
    assert first is not None
    assert first["code_verifier"] == "v"
    assert first["next"] == "/x"
    # Single-use: a second pop finds nothing.
    assert remote_access.pop_oauth_handshake("rid-abc") is None

    # An expired record is treated as absent.
    remote_access.store_oauth_handshake("rid-exp", nonce="n", code_verifier="v", next_target="/x")
    remote_access._oauth_handshakes["rid-exp"]["exp"] = 0
    assert remote_access.pop_oauth_handshake("rid-exp") is None

    # Invalid ids are rejected, never touching the filesystem.
    assert remote_access.pop_oauth_handshake("bad/rid") is None
    assert remote_access.pop_oauth_handshake(None) is None


def test_oauth_handshake_store_caps_entries(monkeypatch, tmp_path):
    # The store is written on every unauthenticated redirect; a hard cap prevents
    # unbounded inode growth under a burst. At capacity, new writes are shed and
    # existing in-flight entries are preserved.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr(remote_access, "OAUTH_HANDSHAKE_MAX_ENTRIES", 3)

    for i in range(3):
        remote_access.store_oauth_handshake(f"rid-{i}", nonce="n", code_verifier="v", next_target="/")
    remote_access.store_oauth_handshake("rid-overflow", nonce="n", code_verifier="v", next_target="/")

    assert remote_access.pop_oauth_handshake("rid-overflow") is None
    assert remote_access.pop_oauth_handshake("rid-0") is not None


def test_oauth_handshake_cap_holds_under_concurrency(monkeypatch, tmp_path):
    # Atomic admission: a concurrent burst must not blow past the cap. Without the
    # lock, many threads could pass the count check before any writes.
    import threading

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr(remote_access, "OAUTH_HANDSHAKE_MAX_ENTRIES", 5)

    barrier = threading.Barrier(20)

    def worker(i):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        remote_access.store_oauth_handshake(f"rid-{i:03d}", nonce="n", code_verifier="v", next_target="/")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(remote_access._oauth_handshakes) <= 5


def test_unauthenticated_auth_requests_are_rate_limited(monkeypatch, tmp_path):
    # Root-level bound: a flood of unauthenticated login-start requests from one
    # client is 429'd, instead of each one doing handshake/cookie/log work.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr(ui_server, "_AUTH_RATELIMIT_MAX_PER_WINDOW", 3)
    client = app.test_client()

    statuses = [
        client.get(
            "/dashboard",
            base_url="https://alex.avibe.bot",
            environ_base={"REMOTE_ADDR": "203.0.113.77"},
            follow_redirects=False,
        ).status_code
        for _ in range(5)
    ]
    assert statuses[:3] == [302, 302, 302]  # within budget -> redirect to login
    assert statuses[3:] == [429, 429]  # over budget -> throttled


def test_auth_rate_limit_ignores_untrusted_forwarded_ip(monkeypatch, tmp_path):
    # A direct (non-loopback) peer can't dodge the limit by rotating CF-Connecting-IP:
    # the forwarded IP is trusted only from the loopback tunnel peer, so such a peer
    # is keyed by its real address and the rotating header is ignored.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr(ui_server, "_AUTH_RATELIMIT_MAX_PER_WINDOW", 3)
    client = app.test_client()

    statuses = [
        client.get(
            "/dashboard",
            base_url="https://alex.avibe.bot",
            environ_base={"REMOTE_ADDR": "203.0.113.90"},
            headers={"CF-Connecting-IP": f"9.9.9.{i}"},  # rotated each request
            follow_redirects=False,
        ).status_code
        for i in range(5)
    ]
    assert statuses[:3] == [302, 302, 302]
    assert statuses[3:] == [429, 429]  # still limited despite the rotating header


def test_auth_rate_limit_keys_trusted_proxy_by_forwarded_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    _save_config(tmp_path)
    monkeypatch.setattr(ui_server, "_AUTH_RATELIMIT_MAX_PER_WINDOW", 3)
    with ui_server._auth_ratelimit_lock:
        ui_server._auth_ratelimit.clear()
    client = app.test_client()

    def get_status(client_ip: str) -> int:
        return client.get(
            "/dashboard",
            base_url="http://127.0.0.1:5123",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "alex.avibe.bot",
                "X-Forwarded-For": client_ip,
            },
            follow_redirects=False,
        ).status_code

    first_client = [get_status("203.0.113.10") for _ in range(3)]
    second_client = [get_status("203.0.113.11") for _ in range(3)]
    first_client_over_budget = get_status("203.0.113.10")

    assert first_client == [302, 302, 302]
    assert second_client == [302, 302, 302]
    assert first_client_over_budget == 429


def test_auth_rate_limit_table_is_bounded(monkeypatch, tmp_path):
    # The limiter's own table is hard-capped (LRU eviction), so a burst of distinct
    # clients can't drive unbounded in-process memory growth.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    monkeypatch.setattr(ui_server, "_AUTH_RATELIMIT_MAX_TRACKED_CLIENTS", 3)
    client = app.test_client()

    for i in range(10):  # 10 distinct peers
        client.get(
            "/dashboard",
            base_url="https://alex.avibe.bot",
            environ_base={"REMOTE_ADDR": f"198.51.100.{i}"},
            follow_redirects=False,
        )
    assert len(ui_server._auth_ratelimit) <= 3


def test_oauth_diag_log_is_rate_limited(monkeypatch):
    # The unauthenticated callback failure path must not grow the log without bound:
    # repeated hits within the window emit once, with the suppressed count folded in.
    clock = {"t": 1000.0}
    monkeypatch.setattr(ui_server.time, "monotonic", lambda: clock["t"])
    ui_server._oauth_diag_log_state.pop("test_key", None)

    emitted = []
    monkeypatch.setattr(ui_server.logger, "warning", lambda msg, *a: emitted.append(msg % a if a else msg))

    for _ in range(5):
        ui_server._log_oauth_diag("test_key", "boom x=%s", 1)
    assert len(emitted) == 1  # only the first hit in the window is logged

    clock["t"] += ui_server._OAUTH_DIAG_LOG_INTERVAL_SECONDS + 1
    ui_server._log_oauth_diag("test_key", "boom x=%s", 1)
    assert len(emitted) == 2
    assert "suppressed" in emitted[1]  # the 4 suppressed hits are reported


def test_oauth_error_page_localizes_from_accept_language(monkeypatch, tmp_path):
    # The re-login page copy must come from vibe/i18n and honor the browser's
    # Accept-Language (the only server-readable locale signal pre-auth).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    client = app.test_client()

    response = client.get(
        "/auth/callback?code=test-code&state=state-1",
        base_url="https://alex.avibe.bot",
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )

    assert response.status_code == 400
    body = response.text
    assert '<html lang="zh"' in body
    assert "登录会话已过期" in body  # invalid_oauth_state_title (zh)
    assert "重新登录" in body  # sign_in_again (zh)
    assert "Your sign-in session expired" not in body  # not the English copy


def test_remote_callback_refuses_store_fallback_without_device_binding(monkeypatch, tmp_path):
    # Login-CSRF block: a code+state callback URL must not complete in a browser that
    # isn't the one that started the flow. The store record is bound to the attacker's
    # device id; the victim's browser presents its own (different) device cookie plus a
    # stale handshake cookie, so the store-fallback must refuse — no token exchange.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    secret = config.remote_access.vibe_cloud.session_secret
    client = app.test_client()

    rid = "victimrid0001"
    state_url = ui_server._make_oauth_state(secret, next_target="/dashboard", rid=rid)
    remote_access.store_oauth_handshake(
        rid,
        nonce="n",
        code_verifier="v",
        next_target="/dashboard",
        device_hash=ui_server._oauth_device_hash(secret, "attacker-device"),
    )

    # Victim browser: a valid-but-stale handshake cookie and its OWN device cookie.
    stale_cookie = ui_server._make_oauth_cookie(
        secret,
        {
            "state": ui_server._make_oauth_state(secret, next_target="/", rid="victimst0000"),
            "nonce": "x",
            "code_verifier": "x",
            "next": "/",
            "exp": int(ui_server.datetime.now().timestamp()) + 300,
        },
    )
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, stale_cookie, domain="alex.avibe.bot")
    client.set_cookie(ui_server.REMOTE_OAUTH_DEVICE_COOKIE_NAME, "victim-device", domain="alex.avibe.bot")

    exchanged = []
    monkeypatch.setattr(
        remote_access, "exchange_oauth_code", lambda *a, **k: exchanged.append(a) or {"claims": {}}
    )

    response = client.get(
        f"/auth/callback?code=test-code&state={state_url}",
        base_url="https://alex.avibe.bot",
        follow_redirects=False,
    )

    # Never exchanged the code, and never redirected the browser to the target.
    assert exchanged == []
    assert response.headers.get("Location") != "/dashboard"


def test_oauth_handshake_pop_is_atomic_single_use_under_concurrency(monkeypatch, tmp_path):
    import threading
    from unittest import mock

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    remote_access.store_oauth_handshake("race-rid000", nonce="n", code_verifier="v", next_target="/")

    barrier = threading.Barrier(2)
    orig_replace = remote_access.os.replace

    def delayed_replace(src, dst):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return orig_replace(src, dst)

    results = []

    def worker():
        results.append(remote_access.pop_oauth_handshake("race-rid000"))

    with mock.patch.object(remote_access.os, "replace", delayed_replace):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # The atomic claim guarantees exactly one racer gets the record.
    assert sum(1 for r in results if r is not None) == 1


def test_remote_callback_accepts_html_escaped_state_separator(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    oauth_cookie = ui_server._make_oauth_cookie(
        config.remote_access.vibe_cloud.session_secret,
        {
            "state": "state-1",
            "nonce": "nonce-1",
            "code_verifier": "verifier-1",
            "next": "/dashboard",
            "exp": int(ui_server.datetime.now().timestamp()) + 300,
        },
    )
    exchange_calls = []
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    def exchange(cfg, code, verifier, redirect_uri=None):
        exchange_calls.append((code, verifier))
        return _oauth_exchange_result(cfg, nonce="nonce-1")

    monkeypatch.setattr(remote_access, "exchange_oauth_code", exchange)

    response = client.get("/auth/callback?code=test-code&amp;state=state-1", base_url="https://alex.avibe.bot")

    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard"
    assert exchange_calls == [("test-code", "verifier-1")]


def test_remote_callback_sanitizes_protocol_relative_next(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    oauth_cookie = ui_server._make_oauth_cookie(
        config.remote_access.vibe_cloud.session_secret,
        {
            "state": "state-1",
            "nonce": "nonce-1",
            "code_verifier": "verifier-1",
            "next": "//attacker.example",
            "exp": int(ui_server.datetime.now().timestamp()) + 300,
        },
    )
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    monkeypatch.setattr(
        remote_access,
        "exchange_oauth_code",
        lambda cfg, code, verifier, redirect_uri=None: _oauth_exchange_result(
            cfg,
            nonce="nonce-1",
        ),
    )

    response = client.get("/auth/callback?code=test-code&state=state-1", base_url="https://alex.avibe.bot")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def _save_config_with_setup_host(tmp_path, host: str) -> V2Config:
    config = _save_config(tmp_path)
    config.ui.setup_host = host
    config.save()
    return config


def test_setup_host_lan_request_is_treated_as_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    response = app.test_client().get(
        "/health",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
    )

    assert response.status_code == 200


def test_setup_host_request_from_self_is_treated_as_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    response = app.test_client().get(
        "/health",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.3"},
    )

    assert response.status_code == 200


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_rejects_setup_host_origin_from_different_port(monkeypatch, tmp_path):
    # A private setup-host request is treated as local (accepted without a session
    # cookie), so the terminal Origin check must still pin it to the exact scheme+port —
    # otherwise a same-host page served on another port could open a cross-origin
    # terminal socket. Regression guard for the setup-host CSWSH gap: before passing the
    # config into _websocket_is_local_request, this request fell through to the remote
    # host-only relaxation and the mismatched port was accepted.
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={
                "host": "192.168.2.3:5123",
                "origin": "http://192.168.2.3:3000",
                "x-vibe-test-remote-addr": "192.168.2.5",
            },
        ):
            pass

    assert exc.value.code == 1008


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_accepts_setup_host_origin_from_same_port(monkeypatch, tmp_path):
    # The exact-origin counterpart: a setup-host terminal request whose Origin matches the
    # request scheme+port must still be accepted (the fix must not over-reject LAN setup).
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    accepted = False

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        nonlocal accepted
        accepted = True

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)

    with app.test_client().websocket_connect(
        "/api/terminal/test",
        headers={
            "host": "192.168.2.3:5123",
            "origin": "http://192.168.2.3:5123",
            "x-vibe-test-remote-addr": "192.168.2.5",
        },
    ):
        pass

    assert accepted is True


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_accepts_setup_host_from_trusted_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    _save_config_with_setup_host(tmp_path, "192.168.2.3")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    accepted = False

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        nonlocal accepted
        accepted = True

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)

    with app.test_client().websocket_connect(
        "/api/terminal/test",
        headers={
            "host": "127.0.0.1:5123",
            "origin": "http://192.168.2.3:5123",
            "x-forwarded-proto": "http",
            "x-forwarded-host": "192.168.2.3:5123",
            "x-forwarded-for": "192.168.2.5",
            "x-vibe-test-remote-addr": "127.0.0.1",
        },
    ):
        pass

    assert accepted is True


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_accepts_trusted_public_origin_from_proxy_when_remote_access_disabled(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, "https://avibe.example.com")
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    accepted = False

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        nonlocal accepted
        accepted = True

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)

    with app.test_client().websocket_connect(
        "/api/terminal/test",
        headers={
            "host": "127.0.0.1:5123",
            "origin": "https://avibe.example.com",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "avibe.example.com",
            "x-forwarded-for": "203.0.113.10",
            "x-vibe-test-remote-addr": "127.0.0.1",
        },
    ):
        pass

    assert accepted is True


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_rejects_unlisted_public_origin_from_trusted_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={
                "host": "127.0.0.1:5123",
                "origin": "https://avibe.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "avibe.example.com",
                "x-forwarded-for": "203.0.113.10",
                "x-vibe-test-remote-addr": "127.0.0.1",
            },
        ):
            pass

    assert exc.value.code == 1008


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_rejects_loopback_trusted_public_origin_when_remote_access_enabled(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    monkeypatch.setenv(ui_server.TRUSTED_PUBLIC_ORIGINS_ENV, "https://alex.avibe.bot")
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.public_url = "https://alex.avibe.bot"
    config.remote_access.vibe_cloud.enabled = True
    config.save()

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect(
            "/api/terminal/test",
            headers={
                "host": "127.0.0.1:5123",
                "origin": "https://alex.avibe.bot",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "alex.avibe.bot",
                "x-forwarded-for": "203.0.113.10",
                "x-vibe-test-remote-addr": "127.0.0.1",
            },
        ):
            pass

    assert exc.value.code == 1008


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_rejects_remote_same_host_different_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "wss://alex.avibe.bot/api/terminal/test",
            headers={
                "host": "alex.avibe.bot",
                "origin": "https://alex.avibe.bot:8443",
                "x-forwarded-for": "203.0.113.10",
            },
        ):
            pass

    assert exc.value.code == 1008


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
@pytest.mark.parametrize("origin", ["https://alex.avibe.bot", "https://alex.avibe.bot:443"])
def test_terminal_websocket_rejects_remote_exact_trusted_origin(monkeypatch, tmp_path, origin):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    accepted = False

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        nonlocal accepted
        accepted = True

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "wss://alex.avibe.bot/api/terminal/test",
            headers={
                "host": "alex.avibe.bot",
                "origin": origin,
                "x-forwarded-for": "203.0.113.10",
            },
        ):
            pass

    assert exc.value.code == 1008
    assert accepted is False


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_rejects_remote_viewer(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            "viewer@example.com",
            "user-viewer",
            role="viewer",
            access_source="email",
        ),
        domain="alex.avibe.bot",
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "wss://alex.avibe.bot/api/terminal/test",
            headers={
                "host": "alex.avibe.bot",
                "origin": "https://alex.avibe.bot",
                "x-forwarded-for": "203.0.113.10",
            },
        ):
            pass

    assert exc.value.code == 1008


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_rejects_remote_active_custom_hostname(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    remote_access._replace_active_hostnames(config, ["max.fileguard.io"])
    accepted = False

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        nonlocal accepted
        accepted = True

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(
            config,
            "alex@example.com",
            "user-1",
            session_claims=_oauth_exchange_result(config, nonce="unused")["session_claims"],
        ),
        domain="max.fileguard.io",
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "wss://max.fileguard.io/api/terminal/test",
            headers={
                "host": "max.fileguard.io",
                "origin": "https://max.fileguard.io",
                "x-forwarded-for": "203.0.113.10",
            },
        ):
            pass

    assert exc.value.code == 1008
    assert accepted is False


def test_terminal_effective_session_id_scopes_remote_subjects():
    client_id = "shared-session"

    user_one_first = ui_server._terminal_effective_session_id(client_id, "user-1")
    user_one_second = ui_server._terminal_effective_session_id(client_id, "user-1")
    user_two = ui_server._terminal_effective_session_id(client_id, "user-2")

    assert user_one_first == user_one_second
    assert user_one_first != user_two
    assert user_one_first.endswith("-shared-session")
    assert ui_server._terminal_effective_session_id(client_id, None) == client_id


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_never_starts_a_remote_subject_session(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    handled_session_ids: list[str] = []

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        handled_session_ids.append(session_id)

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)

    def connect_as(subject: str) -> None:
        client = app.test_client()
        client.set_cookie(
            remote_access.SESSION_COOKIE_NAME,
            remote_session_cookie(config, f"{subject}@example.com", subject),
            domain="alex.avibe.bot",
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "wss://alex.avibe.bot/api/terminal/shared-session",
                headers={
                    "host": "alex.avibe.bot",
                    "origin": "https://alex.avibe.bot",
                    "x-forwarded-for": "203.0.113.10",
                },
            ):
                pass
        assert exc.value.code == 1008

    connect_as("user-1")
    connect_as("user-1")
    connect_as("user-2")

    assert handled_session_ids == []


@pytest.mark.skipif(not ui_server.TERMINAL_SUPPORTED, reason="terminal requires a POSIX pty")
def test_terminal_websocket_keeps_local_session_id_unscoped(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")
    _mock_interface(monkeypatch, "192.168.2.3", 24)
    handled_session_ids: list[str] = []

    async def fake_handle_websocket(websocket, session_id, *, initial_cwd=None):
        handled_session_ids.append(session_id)

    monkeypatch.setattr(ui_server.get_terminal_service(), "handle_websocket", fake_handle_websocket)

    with app.test_client().websocket_connect(
        "/api/terminal/local-session",
        headers={
            "host": "192.168.2.3:5123",
            "origin": "http://192.168.2.3:5123",
            "x-vibe-test-remote-addr": "192.168.2.5",
        },
    ):
        pass

    assert handled_session_ids == ["local-session"]


def test_setup_host_with_public_peer_is_not_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_lan_peer_with_tailscale_setup_is_not_local(monkeypatch, tmp_path):
    """Wildcard-bind regression guard: a LAN peer cannot inherit setup-host
    trust by spoofing the Host header to a Tailscale setup_host that lives
    in a different private block."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "100.97.103.112")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "192.168.1.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_tailscale_peer_with_lan_setup_is_not_local(monkeypatch, tmp_path):
    """Inverse of the LAN-vs-Tailscale check: a Tailscale peer cannot inherit
    setup-host trust by spoofing the Host header to a LAN setup_host."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_tailscale_peer_with_tailscale_setup_is_local(monkeypatch, tmp_path):
    """Same-block trust still works: a Tailscale peer can inherit setup-host
    trust when setup_host is also in 100.64/10."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "100.97.103.112")

    response = app.test_client().get(
        "/health",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
    )

    assert response.status_code == 200


def test_setup_host_rfc1918_peer_outside_interface_subnet_is_not_local(monkeypatch, tmp_path):
    """RFC1918 trust must not span the entire /8: a 10.50/16 peer cannot
    inherit setup-host trust from a 10.1.2.3 setup_host configured with a
    /24 mask. Pre-wildcard, the kernel only let in peers on the same
    interface subnet — _local_interface_network restores that scoping
    using the actual netmask."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "10.1.2.3")
    _mock_interface(monkeypatch, "10.1.2.3", 24)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "10.50.0.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_rfc1918_peer_in_same_interface_subnet_is_local(monkeypatch, tmp_path):
    """Same-subnet RFC1918 peer still inherits trust (typical home/office LAN)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "10.1.2.3")
    _mock_interface(monkeypatch, "10.1.2.3", 24)

    response = app.test_client().get(
        "/health",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "10.1.2.50"},
    )

    assert response.status_code == 200


def test_setup_host_192168_peer_outside_interface_subnet_is_not_local(monkeypatch, tmp_path):
    """A peer on 192.168.2/24 cannot spoof Host=192.168.1.5 when the
    interface mask is /24."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.1.5")
    _mock_interface(monkeypatch, "192.168.1.5", 24)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.1.5:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_with_16_prefix_includes_peer_in_same_16(monkeypatch, tmp_path):
    """When the interface mask is /16, a peer on a different /24 within
    the same /16 still inherits trust — fixed-/24 estimates were too
    narrow for /16 LANs."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.1.5")
    _mock_interface(monkeypatch, "192.168.1.5", 16)

    response = app.test_client().get(
        "/health",
        base_url="http://192.168.1.5:5123",
        environ_base={"REMOTE_ADDR": "192.168.7.20"},
    )

    assert response.status_code == 200


def test_setup_host_with_20_prefix_includes_peer_in_same_20(monkeypatch, tmp_path):
    """/20 corporate networks (4096 addresses) are honored without
    artificially narrowing to /24."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "10.1.16.5")
    _mock_interface(monkeypatch, "10.1.16.5", 20)

    response = app.test_client().get(
        "/health",
        base_url="http://10.1.16.5:5123",
        environ_base={"REMOTE_ADDR": "10.1.31.250"},
    )

    assert response.status_code == 200


def test_setup_host_with_20_prefix_excludes_peer_outside_20(monkeypatch, tmp_path):
    """/20 still excludes peers outside the /20 (peer in next /20 is not
    on the same routed subnet)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "10.1.16.5")
    _mock_interface(monkeypatch, "10.1.16.5", 20)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://10.1.16.5:5123",
        environ_base={"REMOTE_ADDR": "10.1.32.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_unknown_to_local_interfaces_is_not_local(monkeypatch, tmp_path):
    """If setup_host is not configured on any local interface, deny trust
    rather than guess a subnet — this preserves the kernel's pre-wildcard
    "no matching interface, no traffic" semantics."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.99.99")
    _mock_no_interfaces(monkeypatch)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.99.99:5123",
        environ_base={"REMOTE_ADDR": "192.168.99.50"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_ipv6_with_56_prefix_includes_peer_in_same_56(monkeypatch, tmp_path):
    """A non-/64 IPv6 LAN (e.g. /56 prefix delegated to the home network)
    is honored without artificially narrowing to /64."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "fd00:0:0:1::5")
    _mock_interface(monkeypatch, "fd00:0:0:1::5", 56)

    response = app.test_client().get(
        "/health",
        base_url="http://[fd00:0:0:1::5]:5123",
        environ_base={"REMOTE_ADDR": "fd00:0:0:7::20"},
    )

    assert response.status_code == 200


def test_setup_host_ipv6_with_64_prefix_excludes_peer_outside_64(monkeypatch, tmp_path):
    """Default IPv6 LAN /64 still scopes peers correctly."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "fd00::5")
    _mock_interface(monkeypatch, "fd00::5", 64)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://[fd00::5]:5123",
        environ_base={"REMOTE_ADDR": "fd00:0:0:1::20"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def _save_config_tunnel_off_with_setup_host(tmp_path, host: str) -> V2Config:
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.ui.setup_host = host
    config.save()
    return config


def test_setup_host_tunnel_off_allows_routed_peer_outside_interface_subnet(monkeypatch, tmp_path):
    """When the tunnel is off, the UI binds directly to setup_host and the
    kernel already enforces interface filtering — a routed peer reaching
    setup_host across a /16 corporate or campus net must have been routed
    legitimately, so the application layer should not add a second-pass
    subnet gate (regression noted in Codex review of #252)."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_tunnel_off_with_setup_host(tmp_path, "10.1.2.3")
    _mock_interface(monkeypatch, "10.1.2.3", 24)

    response = app.test_client().get(
        "/health",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "10.50.0.5"},
    )

    assert response.status_code == 200


def test_setup_host_tunnel_off_still_rejects_public_peer(monkeypatch, tmp_path):
    """Tunnel-off relaxation of the subnet gate must not relax the
    private-peer requirement: a public peer is still untrusted."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_tunnel_off_with_setup_host(tmp_path, "10.1.2.3")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_tunnel_on_still_enforces_subnet_gate(monkeypatch, tmp_path):
    """Mirror of the tunnel-off test above: with the tunnel on, the
    wildcard bind requires the application-layer subnet gate, so the same
    cross-subnet peer that is allowed when the tunnel is off must be
    rejected when the tunnel is on."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "10.1.2.3")
    _mock_interface(monkeypatch, "10.1.2.3", 24)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://10.1.2.3:5123",
        environ_base={"REMOTE_ADDR": "10.50.0.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_mismatched_host_header_is_not_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://10.0.0.5:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_allows_actual_lan_interface_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "_is_containerized_runtime", lambda: False)
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    response = app.test_client().get(
        "/health",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
    )

    assert response.status_code == 200


def test_setup_host_wildcard_allows_bare_metal_eth_lan_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "_is_containerized_runtime", lambda: False)
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.2.3", 24, name="eth0")

    response = app.test_client().get(
        "/health",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
    )

    assert response.status_code == 200


def test_setup_host_wildcard_does_not_trust_container_eth_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "_is_containerized_runtime", lambda: True)
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.2.3", 24, name="eth0")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_does_not_trust_unconfigured_lan_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_no_interfaces(monkeypatch)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_does_not_trust_docker_bridge_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "172.17.0.1", 16, name="docker0")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://172.17.0.1:5123",
        environ_base={"REMOTE_ADDR": "172.17.0.2"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_does_not_trust_cni_bridge_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.2.3", 24, name="cni0")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_does_not_trust_flannel_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "10.244.0.1", 24, name="flannel.1")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://10.244.0.1:5123",
        environ_base={"REMOTE_ADDR": "10.244.0.2"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_does_not_trust_bridge_interface_in_cgnat_range(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "100.97.103.112", 32, name="docker0")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_rejects_peer_outside_interface_subnet(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.1.5", 24)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.1.5:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_rejects_public_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_with_reverse_proxy_header_is_not_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "192.168.2.3", 24)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        headers={"X-Forwarded-For": "203.0.113.10"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_with_reverse_proxy_header_skips_interface_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    monkeypatch.setattr(
        ui_server,
        "_local_interface_network",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("interface probe should be skipped")),
    )

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        headers={"X-Forwarded-For": "203.0.113.10"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_allows_actual_tailscale_interface_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "100.97.103.112", 32, name="tailscale0")
    _mock_tailscale_whois(monkeypatch, "100.97.103.5")

    response = app.test_client().get(
        "/health",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
    )

    assert response.status_code == 200


def test_setup_host_wildcard_rejects_tailscale_peer_without_whois(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "100.97.103.112", 32, name="tailscale0")
    monkeypatch.setattr(ui_server, "_TAILSCALE_PEER_CACHE", {})
    monkeypatch.setattr(ui_server, "_tailscale_whois", lambda address: None)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_rejects_tailscale_subnet_router_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "100.97.103.112", 32, name="tailscale0")
    _mock_tailscale_whois(monkeypatch, "100.97.103.5", allowed_ips=["100.97.103.5/32", "192.168.50.0/24"])

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_trusted_tailscale_peer_accepts_modern_node_whois_payload(monkeypatch):
    """Current ``tailscale whois --json`` nests the peer under ``Node`` with
    host-CIDR address strings; the top-level ``Machine`` object is gone."""
    peer = ipaddress.ip_address("100.97.103.5")
    monkeypatch.setattr(ui_server, "_TAILSCALE_PEER_CACHE", {})
    monkeypatch.setattr(
        ui_server,
        "_tailscale_whois",
        lambda address: {
            "Node": {
                "Name": "iphone.example.ts.net.",
                "Machine": "mkey:0123456789abcdef",
                "Addresses": ["100.97.103.5/32", "fd7a:115c:a1e0::5/128"],
                "AllowedIPs": ["100.97.103.5/32", "fd7a:115c:a1e0::5/128"],
            },
            "UserProfile": {"LoginName": "user@example.com"},
            "CapMap": {},
        },
    )

    assert ui_server._is_trusted_tailscale_peer(peer) is True


def test_trusted_tailscale_peer_accepts_legacy_machine_whois_payload(monkeypatch):
    peer = ipaddress.ip_address("100.97.103.5")
    _mock_tailscale_whois(
        monkeypatch,
        "100.97.103.5",
        addresses=["100.97.103.5"],
        payload_key="Machine",
    )

    assert ui_server._is_trusted_tailscale_peer(peer) is True


def test_trusted_tailscale_peer_rejects_node_payload_with_subnet_route(monkeypatch):
    peer = ipaddress.ip_address("100.97.103.5")
    _mock_tailscale_whois(
        monkeypatch,
        "100.97.103.5",
        allowed_ips=["100.97.103.5/32", "192.168.50.0/24"],
    )

    assert ui_server._is_trusted_tailscale_peer(peer) is False


def test_trusted_tailscale_peer_ignores_non_host_address_entries(monkeypatch):
    """Address entries that are not host CIDRs must not satisfy the peer
    match, so a payload claiming a whole range cannot vouch for the peer."""
    peer = ipaddress.ip_address("100.97.103.5")
    _mock_tailscale_whois(
        monkeypatch,
        "100.97.103.5",
        addresses=["100.97.103.0/24"],
    )

    assert ui_server._is_trusted_tailscale_peer(peer) is False


def test_setup_host_wildcard_does_not_trust_unconfigured_tailscale_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_no_interfaces(monkeypatch)

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_ipv6_wildcard_allows_actual_private_interface_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server, "_is_containerized_runtime", lambda: False)
    _save_config_with_setup_host(tmp_path, "::")
    _mock_interface(monkeypatch, "fd00::5", 64)

    response = app.test_client().get(
        "/health",
        base_url="http://[fd00::5]:5123",
        environ_base={"REMOTE_ADDR": "fd00::20"},
    )

    assert response.status_code == 200


def test_setup_host_ipv6_wildcard_allows_tailscale_ula_interface_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "::")
    _mock_interface(monkeypatch, "fd7a:115c:a1e0::5", 128, name="tailscale0")
    _mock_tailscale_whois(monkeypatch, "fd7a:115c:a1e0::20")

    response = app.test_client().get(
        "/health",
        base_url="http://[fd7a:115c:a1e0::5]:5123",
        environ_base={"REMOTE_ADDR": "fd7a:115c:a1e0::20"},
    )

    assert response.status_code == 200


def test_setup_host_ipv6_wildcard_does_not_trust_bridge_in_tailscale_ula_range(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "::")
    _mock_interface(monkeypatch, "fd7a:115c:a1e0::5", 64, name="docker0")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://[fd7a:115c:a1e0::5]:5123",
        environ_base={"REMOTE_ADDR": "fd7a:115c:a1e0::20"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_does_not_trust_generic_utun_tunnel(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    _mock_interface(monkeypatch, "100.97.103.112", 32, name="utun4")
    monkeypatch.setattr(ui_server, "_tailscale_local_addresses", lambda: frozenset())

    response = app.test_client().get(
        "/dashboard",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_wildcard_trusts_utun_when_tailscale_reports_local_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "0.0.0.0")
    address = ipaddress.ip_address("100.97.103.112")
    _mock_interface(monkeypatch, str(address), 32, name="utun4")
    monkeypatch.setattr(ui_server, "_tailscale_local_addresses", lambda: frozenset({address}))
    _mock_tailscale_whois(monkeypatch, "100.97.103.5")

    response = app.test_client().get(
        "/health",
        base_url="http://100.97.103.112:5123",
        environ_base={"REMOTE_ADDR": "100.97.103.5"},
    )

    assert response.status_code == 200


def test_setup_host_ipv6_wildcard_does_not_trust_generic_utun_tunnel(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "::")
    _mock_interface(monkeypatch, "fd7a:115c:a1e0::5", 128, name="utun4")
    monkeypatch.setattr(ui_server, "_tailscale_local_addresses", lambda: frozenset())

    response = app.test_client().get(
        "/dashboard",
        base_url="http://[fd7a:115c:a1e0::5]:5123",
        environ_base={"REMOTE_ADDR": "fd7a:115c:a1e0::20"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_ipv6_wildcard_trusts_utun_when_tailscale_reports_local_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "::")
    address = ipaddress.ip_address("fd7a:115c:a1e0::5")
    _mock_interface(monkeypatch, str(address), 128, name="utun4")
    monkeypatch.setattr(ui_server, "_tailscale_local_addresses", lambda: frozenset({address}))
    _mock_tailscale_whois(monkeypatch, "fd7a:115c:a1e0::20")

    response = app.test_client().get(
        "/health",
        base_url="http://[fd7a:115c:a1e0::5]:5123",
        environ_base={"REMOTE_ADDR": "fd7a:115c:a1e0::20"},
    )

    assert response.status_code == 200


def test_setup_host_with_cloudflare_metadata_is_not_local(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "192.168.2.5"},
        headers=_cloudflare_headers(),
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_with_reverse_proxy_header_is_not_local(monkeypatch, tmp_path):
    """A non-Cloudflare reverse proxy on the same host (nginx, Caddy, ...)
    fronts vibe and an attacker spoofs Host=setup_host. The app sees a private
    peer (the proxy) and the Host matches setup_host, so the host+peer pair
    looks "local" — but X-Forwarded-For (or any other forwarded header) tells
    us the actual client is unknown, so the request must not be trusted.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config_with_setup_host(tmp_path, "192.168.2.3")

    response = app.test_client().get(
        "/dashboard",
        base_url="http://192.168.2.3:5123",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-For": "203.0.113.10"},
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_setup_host_trusts_forwarded_host_from_explicit_trusted_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    config = _save_config_with_setup_host(tmp_path, "192.168.2.3")
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:5123",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "192.168.2.3",
            "X-Forwarded-For": "192.168.2.5",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200


def test_trusted_public_origin_can_come_from_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    config = _save_config(tmp_path)
    config.remote_access.vibe_cloud.enabled = False
    config.ui.trusted_public_origins = ["https://avibe.example.com"]
    config.save()

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:5123",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "avibe.example.com",
            "X-Forwarded-For": "203.0.113.10",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200


def test_setup_host_rejects_public_forwarded_client_from_trusted_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    config = _save_config_with_setup_host(tmp_path, "192.168.2.3")
    config.remote_access.vibe_cloud.enabled = False
    config.save()

    response = app.test_client().get(
        "/health",
        base_url="http://127.0.0.1:5123",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "192.168.2.3",
            "X-Forwarded-For": "8.8.8.8",
        },
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_settings_get_serves_json_even_for_browser_accept(monkeypatch, tmp_path):
    """After the /api/* migration the settings JSON API lives at /api/settings
    and no longer content-negotiates a redirect. Even a browser-style
    Accept: text/html request receives the JSON payload; SPA routing for the
    user-facing /settings URL is handled by the static catch-all instead, so
    the API path itself never collides with a UI route anymore.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/api/settings",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.is_json


def test_settings_get_returns_json_for_fetch_callers(monkeypatch, tmp_path):
    """fetch() from the SPA hits /settings without an explicit text/html in
    Accept; the handler must keep returning JSON so getSettings() works.
    """
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/api/settings",
        base_url="http://127.0.0.1:5123",
        headers={"Accept": "*/*"},
    )

    assert response.status_code == 200
    assert response.is_json

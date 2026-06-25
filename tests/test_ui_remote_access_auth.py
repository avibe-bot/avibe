from __future__ import annotations

import ipaddress
import logging
import socket
import asyncio
from collections import namedtuple

import httpx
import pytest

from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from config.v2_config import CONFIG_LOCK
from tests.ui_server_test_helpers import csrf_headers
from vibe import api
from vibe import remote_access
from vibe import ui_server
from vibe.ui_server import app


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
) -> None:
    peer_address = ipaddress.ip_address(peer)
    prefix = peer_address.max_prefixlen
    monkeypatch.setattr(ui_server, "_TAILSCALE_PEER_CACHE", {})
    monkeypatch.setattr(
        ui_server,
        "_tailscale_whois",
        lambda address: {
            "Machine": {
                "Addresses": addresses or [str(peer_address)],
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


def test_remote_config_get_without_session_returns_login_required(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().get(
        "/api/config",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://backend.test/oauth/authorize?")


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
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, remote_access.make_session_cookie(config, "alex@example.com", "user-1"), domain="alex.avibe.bot")

    response = client.get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    assert response.status_code != 302


def _forged_session_cookie(config: V2Config, exp: int, *, email: str = "alex@example.com", subject: str = "user-1") -> str:
    import json
    import urllib.parse

    cloud = config.remote_access.vibe_cloud
    payload = {
        "email": email,
        "sub": subject,
        "instance_id": cloud.instance_id,
        "iat": exp - remote_access.SESSION_TTL_SECONDS,
        "exp": exp,
    }
    payload_text = urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe="")
    signature = remote_access._session_signature(cloud.session_secret, payload_text)
    return f"{payload_text}.{signature}"


def test_remote_host_does_not_renew_fresh_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_access.make_session_cookie(config, "alex@example.com", "user-1"),
        domain="alex.avibe.bot",
    )

    response = client.get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    set_cookie_headers = response.headers.getlist("Set-Cookie")
    assert not any(h.startswith(f"{remote_access.SESSION_COOKIE_NAME}=") for h in set_cookie_headers)


def test_remote_host_renews_cookie_past_half_ttl(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    cookie = _forged_session_cookie(config, near_exp)
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")

    response = client.get("/dashboard", base_url="https://alex.avibe.bot", follow_redirects=False)

    refreshed = next(
        (h for h in response.headers.getlist("Set-Cookie") if h.startswith(f"{remote_access.SESSION_COOKIE_NAME}=")),
        None,
    )
    assert refreshed is not None
    assert "HttpOnly" in refreshed
    assert "Secure" in refreshed
    new_value = refreshed.split(";", 1)[0].split("=", 1)[1]
    assert new_value != cookie
    payload = remote_access.parse_session_cookie(config, new_value)
    assert payload is not None
    assert payload["email"] == "alex@example.com"
    assert payload["sub"] == "user-1"
    assert payload["exp"] > near_exp


def test_remote_host_does_not_renew_cookie_on_rejected_post(monkeypatch, tmp_path):
    """A near-expiry cookie must NOT be slid by a request that later fails
    a guard like CSRF/origin. Otherwise repeated rejected mutations could
    keep a stolen session alive indefinitely."""
    import time as _time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    near_exp = int(_time.time()) + (remote_access.SESSION_TTL_SECONDS // 2) - 60
    cookie = _forged_session_cookie(config, near_exp)
    client = app.test_client()
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, cookie, domain="alex.avibe.bot")

    # POST /config without CSRF/origin headers — protect_mutating_ui_requests
    # will reject this with 403 inside the same request lifecycle that already
    # set g.remote_session_renew in enforce_remote_access_cookie.
    response = client.post(
        "/api/config",
        json={"remote_access": {"vibe_cloud": {"enabled": False}}},
        base_url="https://alex.avibe.bot",
    )

    assert response.status_code == 403
    refreshed = next(
        (h for h in response.headers.getlist("Set-Cookie") if h.startswith(f"{remote_access.SESSION_COOKIE_NAME}=")),
        None,
    )
    assert refreshed is None


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
        lambda cfg, code, verifier: {
            "claims": {
                "email": "alex@example.com",
                "sub": "user-1",
                "nonce": "wrong-nonce",
            }
        },
    )

    state = ui_server._read_oauth_cookie(config.remote_access.vibe_cloud.session_secret, oauth_cookie)["state"]
    response = client.get(f"/auth/callback?code=test-code&state={state}", base_url="https://alex.avibe.bot")

    assert response.status_code == 400
    assert "text/html" in response.headers["Content-Type"]
    assert "invalid_oauth_nonce" in response.text
    assert "Sign in again" in response.text
    # Re-login button points back at the original destination from the handshake.
    assert 'href="/dashboard"' in response.text


def test_remote_callback_explains_pairing_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    client = app.test_client()

    with app.test_request_context("/dashboard", base_url="https://alex.avibe.bot"):
        redirect = ui_server._redirect_to_vibe_cloud_login(config)
    oauth_cookie = redirect.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    client.set_cookie(ui_server.REMOTE_OAUTH_COOKIE_NAME, oauth_cookie, domain="alex.avibe.bot")

    def exchange(cfg, code, verifier):
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

    def exchange(cfg, code, verifier):
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

    def exchange(cfg, code, verifier):
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

    def exchange(cfg, code, verifier):
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

    def exchange(cfg, code, verifier):
        captured["verifier"] = verifier
        return {"claims": {"email": "alex@example.com", "sub": "user-1", "nonce": "nonce-approved"}}

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

    def exchange(cfg, code, verifier):
        exchange_calls.append((code, verifier))
        return {
            "claims": {
                "email": "alex@example.com",
                "sub": "user-1",
                "nonce": "nonce-1",
            }
        }

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
        lambda cfg, code, verifier: {
            "claims": {
                "email": "alex@example.com",
                "sub": "user-1",
                "nonce": "nonce-1",
            }
        },
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

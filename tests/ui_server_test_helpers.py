from __future__ import annotations

import ipaddress
import socket
from collections import namedtuple
from urllib.parse import urlparse

from vibe import remote_access


_FakeSnicaddr = namedtuple("snicaddr", ["family", "address", "netmask", "broadcast", "ptp"])


def _remote_peer() -> dict[str, str]:
    return {"REMOTE_ADDR": "203.0.113.10"}


def remote_peer() -> dict[str, str]:
    """Return the canonical remote test peer environment."""

    return _remote_peer()


def _save_config(
    tmp_path,
    *,
    paired: bool = False,
    instance_kind: str = "organization",
):
    from config.v2_config import (
        AgentsConfig,
        PlatformsConfig,
        RemoteAccessConfig,
        RuntimeConfig,
        SlackConfig,
        UiConfig,
        V2Config,
    )

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
    cloud.instance_secret = "instance-secret"
    cloud.session_secret = "session-secret"
    cloud.authorization_endpoint = "https://backend.test/oauth/authorize"
    cloud.redirect_uri = "https://alex.avibe.bot/auth/callback"
    if paired:
        cloud.backend_url = "https://backend.test"
        cloud.instance_kind = instance_kind
    else:
        cloud.instance_secret = ""
    config.save()
    if paired:
        # A complete paired fixture also needs the device authorization
        # watermark; production session cookies must carry it once runtime
        # credentials exist.
        remote_access._replace_authorization_revision(config, 0)
    return config


def save_config(tmp_path):
    """Return the canonical V2 test configuration."""

    return _save_config(tmp_path)


def _mock_interface(monkeypatch, ip: str, prefix: int, name: str = "en0") -> None:
    address = ipaddress.ip_address(ip)
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    network = ipaddress.IPv4Network if address.version == 4 else ipaddress.IPv6Network
    netmask = str(network(f"0.0.0.0/{prefix}" if address.version == 4 else f"::/{prefix}").netmask)
    snic = _FakeSnicaddr(family=family, address=ip, netmask=netmask, broadcast=None, ptp=None)
    monkeypatch.setattr("vibe.ui_server.psutil.net_if_addrs", lambda: {name: [snic]})


def remote_session_cookie(
    config,
    email: str,
    subject: str,
    *,
    role: str = "owner",
    access_source: str = "owner",
    session_claims: dict | None = None,
    organization_id: str | None = None,
    organization_member_id: str | None = None,
    organization_role: str | None = None,
    group_ids: list[str] | None = None,
) -> str:
    claims = session_claims
    if claims is None:
        claims = {
            "vibe_instance_id": config.remote_access.vibe_cloud.instance_id,
            "vibe_instance_role": role,
            "vibe_instance_access_source": access_source,
        }
        if remote_access._authorization_revision_sync_configured(config):
            claims["vibe_instance_authorization_revision"] = (
                remote_access.current_authorization_revision(config) or 0
            )
        if organization_id is not None:
            claims["vibe_organization_id"] = organization_id
        if organization_member_id is not None:
            claims["vibe_organization_member_id"] = organization_member_id
        if organization_role is not None:
            claims["vibe_organization_role"] = organization_role
        if group_ids is not None:
            claims["vibe_group_ids"] = group_ids
    elif remote_access._authorization_revision_sync_configured(config) and (
        "vibe_instance_authorization_revision" not in claims
    ):
        claims = {
            **claims,
            "vibe_instance_authorization_revision": (
                remote_access.current_authorization_revision(config) or 0
            ),
        }
    return remote_access.make_session_cookie(
        config,
        email,
        subject,
        session_claims=claims,
    )


def csrf_headers(client, base_url: str = "http://localhost") -> dict[str, str]:
    response = client.get("/api/csrf-token", base_url=base_url)
    assert response.status_code == 200
    token = response.get_json()["csrf_token"]
    hostname = urlparse(base_url).hostname or "localhost"
    client.set_cookie("vibe_csrf_token", token, domain=hostname)
    if hostname == "localhost":
        client.set_cookie("vibe_csrf_token", token, domain="testserver")
    return {
        "Origin": base_url,
        "X-Vibe-CSRF-Token": token,
    }

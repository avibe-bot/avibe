from __future__ import annotations

import ipaddress
from http.cookies import SimpleCookie

from config.v2_config import AgentsConfig, PlatformsConfig, RemoteAccessConfig, RuntimeConfig, SlackConfig, V2Config
from vibe import ui_server
from vibe.ui_compat import TEST_REMOTE_ADDR_HEADER
from vibe.ui_server import app, protect_mutating_ui_requests

from tests.ui_server_test_helpers import csrf_headers


def test_csrf_token_endpoint_returns_cookie_and_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = app.test_client()
    response = client.get("/api/csrf-token", base_url="http://127.0.0.1:15131")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload["csrf_token"], str)
    assert payload["csrf_token"]
    cookie_header = response.headers.get("Set-Cookie", "")
    assert "vibe_csrf_token=" in cookie_header
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    assert cookie["vibe_csrf_token"].value == payload["csrf_token"]


def test_config_post_rejects_cross_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    headers["Origin"] = "http://evil.example"

    response = client.post(
        "/api/config",
        json={"mode": "self_host"},
        headers=headers,
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid origin"


def test_config_post_rejects_missing_csrf_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"mode": "self_host"},
        headers={"Origin": "http://127.0.0.1:15131"},
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid csrf token"


def test_config_post_rejects_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")

    response = client.post(
        "/api/config",
        content="{",
        headers={**headers, "Content-Type": "application/json"},
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 400


def test_config_post_rejects_host_mismatch_before_parsing_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")

    response = client.post(
        "/api/config",
        content="{",
        headers={**headers, "Content-Type": "application/json"},
        base_url="https://old-alex.avibe.bot",
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_config_post_accepts_vendor_json_content_type(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")

    response = client.post(
        "/api/config",
        content='{"mode":"self_host"}',
        headers={**headers, "Content-Type": "application/vnd.api+json"},
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["mode"] == "self_host"


def test_config_post_rejects_untrusted_forwarded_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    headers["Origin"] = "https://vibe.example"
    headers["X-Forwarded-Proto"] = "HTTPS"
    headers["X-Forwarded-Host"] = "vibe.example"

    response = client.post(
        "/api/config",
        json={
            "mode": "self_host",
            "runtime": {"default_cwd": "/tmp/test"},
            "agents": {
                "default_backend": "opencode",
                "opencode": {"enabled": True, "cli_path": "opencode"},
                "claude": {"enabled": False, "cli_path": "claude"},
                "codex": {"enabled": False, "cli_path": "codex"},
            },
        },
        headers=headers,
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid origin"


def test_config_post_allows_forwarded_origin_from_explicit_trusted_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    )
    config.ui.setup_host = "192.168.2.3"
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    headers["Origin"] = "http://192.168.2.3"
    headers["X-Forwarded-Proto"] = "http"
    headers["X-Forwarded-Host"] = "192.168.2.3"
    headers["X-Forwarded-For"] = "192.168.2.5"

    response = client.post(
        "/api/config",
        json={
            "mode": "self_host",
            "runtime": {"default_cwd": "/tmp/test"},
            "agents": {
                "default_backend": "opencode",
                "opencode": {"enabled": True, "cli_path": "opencode"},
                "claude": {"enabled": False, "cli_path": "claude"},
                "codex": {"enabled": False, "cli_path": "codex"},
            },
        },
        headers=headers,
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 200


def test_current_origin_uses_configured_remote_public_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        remote_access=RemoteAccessConfig(),
    )
    config.remote_access.vibe_cloud.enabled = True
    config.remote_access.vibe_cloud.public_url = "https://alex.avibe.bot"
    config.save()

    with app.test_request_context(
        "/api/config",
        base_url="https://alex.avibe.bot",
        headers={
            TEST_REMOTE_ADDR_HEADER: "203.0.113.10",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.example",
        },
    ):
        assert ui_server._current_origin() == "https://alex.avibe.bot"


def test_current_origin_uses_trusted_forwarded_port(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv(ui_server.TRUSTED_PROXY_IPS_ENV, "127.0.0.1")
    monkeypatch.setattr(ui_server, "_request_peer_address", lambda: ipaddress.ip_address("127.0.0.1"))
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()

    with app.test_request_context(
        "/api/config",
        base_url="http://127.0.0.1:15131",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "proxy.example",
            "X-Forwarded-Port": "8443",
        },
    ):
        assert ui_server._current_origin() == "https://proxy.example:8443"


def test_config_post_returns_400_for_enabled_platform_missing_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config(
        mode="self_host",
        version="v2",
        platform="avibe",
        platforms=PlatformsConfig(enabled=[], primary="avibe"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
    ).save()
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")

    response = client.post(
        "/api/config",
        json={
            "platform": "lark",
            "platforms": {"enabled": ["lark"], "primary": "lark"},
            "lark": {"domain": "feishu"},
        },
        headers=headers,
        base_url="http://127.0.0.1:15131",
    )

    body = response.get_json()
    assert response.status_code == 400
    assert "lark.app_id" in body["error"]
    assert body["message"] == body["error"]


def test_mutation_guard_exempts_e2e_simulation_endpoint(monkeypatch):
    monkeypatch.setenv("E2E_TEST_MODE", "true")
    with app.test_request_context("/e2e/simulate-interaction", method="POST"):
        assert protect_mutating_ui_requests() is None

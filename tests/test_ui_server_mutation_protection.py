from __future__ import annotations

from http.cookies import SimpleCookie

from config.v2_config import AgentsConfig, PlatformsConfig, RuntimeConfig, SlackConfig, V2Config
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


def test_config_post_allows_forwarded_origin(monkeypatch, tmp_path):
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
    headers["X-Forwarded-Proto"] = "https"
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

    assert response.status_code == 200


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

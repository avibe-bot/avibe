from __future__ import annotations

from fastapi.testclient import TestClient

from config.v2_config import UiConfig, V2Config
from tests.test_api_save_config_merge import _full_config_payload
from vibe import runtime
from vibe import ui_server


def test_vault_sandbox_serves_bundle_with_hardened_csp(monkeypatch, tmp_path):
    sandbox_dist = tmp_path / "dist-sandbox"
    assets = sandbox_dist / "assets"
    assets.mkdir(parents=True)
    (sandbox_dist / "index.html").write_text(
        '<div id="root"></div><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("postMessage({ready:true}, '*')", encoding="utf-8")
    monkeypatch.setattr(ui_server, "get_ui_sandbox_dist_path", lambda: sandbox_dist)
    monkeypatch.setattr(ui_server, "VAULT_SANDBOX_MAIN_ORIGIN", "http://localhost:5123")

    client = TestClient(ui_server.vault_sandbox_app, base_url="http://localhost:5124")
    index = client.get("/")
    asset = client.get("/assets/app.js")

    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store, private"
    assert asset.status_code == 200
    assert asset.text == "postMessage({ready:true}, '*')"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

    csp = index.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self' 'wasm-unsafe-eval'" in csp
    assert "connect-src 'none'" in csp
    assert "media-src 'none'" in csp
    assert "child-src 'none'" in csp
    assert "frame-src 'none'" in csp
    assert "manifest-src 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    assert "navigate-to 'none'" in csp
    assert csp.endswith("frame-ancestors http://localhost:5123")
    assert "unsafe-inline" not in csp
    assert index.headers["x-content-type-options"] == "nosniff"
    assert index.headers["referrer-policy"] == "no-referrer"
    assert index.headers["cross-origin-resource-policy"] == "cross-origin"
    assert "publickey-credentials-get=(self)" in index.headers["permissions-policy"]


def test_vault_sandbox_does_not_expose_main_app_routes_or_api(monkeypatch, tmp_path):
    sandbox_dist = tmp_path / "dist-sandbox"
    sandbox_dist.mkdir()
    (sandbox_dist / "index.html").write_text("<html>sandbox</html>", encoding="utf-8")
    monkeypatch.setattr(ui_server, "get_ui_sandbox_dist_path", lambda: sandbox_dist)

    client = TestClient(ui_server.vault_sandbox_app, base_url="http://localhost:5124")

    assert client.get("/api/vault/sign").status_code == 404
    assert client.post("/api/vault/sign", json={}).status_code == 404
    assert client.get("/dashboard").status_code == 404


def test_vault_sandbox_missing_bundle_is_service_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_server, "get_ui_sandbox_dist_path", lambda: tmp_path / "missing")

    response = TestClient(ui_server.vault_sandbox_app, base_url="http://localhost:5124").get("/")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["content-security-policy"].endswith(f"frame-ancestors {ui_server.VAULT_SANDBOX_MAIN_ORIGIN}")


def test_vault_sandbox_port_defaults_and_config_override():
    assert ui_server._configured_vault_sandbox_port(None) == 5124
    assert ui_server._configured_vault_sandbox_port(type("Config", (), {"ui": UiConfig(vault_sandbox_port=6124)})()) == 6124


def test_vault_sandbox_port_config_is_normalized():
    base_payload = _full_config_payload()
    base_payload["ui"]["vault_sandbox_port"] = "6124"

    assert V2Config.from_payload(base_payload).ui.vault_sandbox_port == 6124

    invalid_payload = _full_config_payload()
    invalid_payload["ui"]["vault_sandbox_port"] = 70000
    assert V2Config.from_payload(invalid_payload).ui.vault_sandbox_port == 5124


def test_vault_sandbox_fresh_config_defaults_to_localhost():
    assert UiConfig().setup_host == "localhost"
    config = type("Config", (), {"ui": UiConfig(), "remote_access": None})()
    assert runtime.effective_ui_bind_host(config) == "127.0.0.1"


def test_vault_sandbox_main_origin_uses_configured_loopback_host():
    assert ui_server._vault_sandbox_main_origin(None, "localhost", 5123) == "http://localhost:5123"
    assert ui_server._vault_sandbox_main_origin(None, "127.0.0.1", 5123) == "http://127.0.0.1:5123"
    assert ui_server._vault_sandbox_main_origin(None, "::1", 5123) == "http://[::1]:5123"
    assert ui_server._vault_sandbox_main_origin(None, "bad host", 5123) == "http://localhost:5123"


def test_vault_sandbox_main_origin_can_be_overridden_for_regression_proxy(monkeypatch):
    monkeypatch.setenv(ui_server.VAULT_SANDBOX_MAIN_ORIGIN_ENV, "http://localhost:15130")

    assert ui_server._vault_sandbox_main_origin(None, "127.0.0.1", 5123) == "http://localhost:15130"

    monkeypatch.setenv(ui_server.VAULT_SANDBOX_MAIN_ORIGIN_ENV, "javascript:alert(1)")
    assert ui_server._vault_sandbox_main_origin(None, "localhost", 5123) == "http://localhost:5123"


def test_vault_sandbox_lifespan_starts_second_listener(monkeypatch):
    calls = []
    config = type("Config", (), {"ui": UiConfig(vault_sandbox_port=6124)})()
    monkeypatch.setattr(ui_server, "_vault_sandbox_config", config)
    monkeypatch.setattr(ui_server, "_vault_sandbox_ui_host", "127.0.0.1")
    monkeypatch.setattr(ui_server, "_vault_sandbox_ui_port", 5123)
    monkeypatch.setattr(
        ui_server,
        "_start_vault_sandbox_server",
        lambda next_config, *, ui_host, ui_port: calls.append((next_config, ui_host, ui_port)),
    )

    with TestClient(ui_server.app):
        pass

    assert calls == [(config, "127.0.0.1", 5123)]

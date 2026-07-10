from __future__ import annotations

import threading

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from vibe import runtime
from vibe.ui_server import app

from tests.ui_server_test_helpers import csrf_headers


def _config_with_tunnel(enabled: bool, setup_host: str = "127.0.0.1") -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        platform="slack",
        platforms=PlatformsConfig(enabled=["slack"], primary="slack"),
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(setup_host=setup_host),
        remote_access=RemoteAccessConfig(),
    )
    config.remote_access.vibe_cloud.enabled = enabled
    return config


class _NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **_extra):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        # Skip the actual subprocess respawn; the unit test only asserts
        # the bind host computed before the thread is started.
        return None


class _ImmediateThread(_NoopThread):
    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


def test_ui_reload_overrides_bind_host_when_tunnel_enabled(monkeypatch):
    captured_calls: list[dict] = []
    original = runtime.effective_ui_bind_host

    def _spy(config, requested_host=None):
        captured_calls.append({"config": config, "requested_host": requested_host})
        return original(config, requested_host=requested_host)

    monkeypatch.setattr(runtime, "effective_ui_bind_host", _spy)
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *a, **k: _config_with_tunnel(enabled=True),
    )
    monkeypatch.setattr(threading, "Thread", _NoopThread)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "100.97.103.112", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    # Response echoes the user-facing host (what the browser should redirect to).
    assert body["host"] == "100.97.103.112"
    assert body["port"] == 5123

    assert captured_calls, "effective_ui_bind_host was not invoked"
    call = captured_calls[-1]
    assert call["requested_host"] == "100.97.103.112"
    assert call["config"].remote_access.vibe_cloud.enabled is True


def test_ui_reload_rejects_non_string_host(monkeypatch):
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *a, **k: _config_with_tunnel(enabled=True),
    )
    monkeypatch.setattr(threading, "Thread", _NoopThread)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": 123, "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_host"}


def test_ui_reload_uses_requested_host_when_tunnel_disabled(monkeypatch):
    captured: dict = {}

    original = runtime.effective_ui_bind_host

    def _spy(config, requested_host=None):
        captured["requested_host"] = requested_host
        captured["enabled"] = config.remote_access.vibe_cloud.enabled
        return original(config, requested_host=requested_host)

    monkeypatch.setattr(runtime, "effective_ui_bind_host", _spy)
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *a, **k: _config_with_tunnel(enabled=False),
    )
    monkeypatch.setattr(threading, "Thread", _NoopThread)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "192.168.1.5", "port": 6000},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert captured["requested_host"] == "192.168.1.5"
    assert captured["enabled"] is False


def test_ui_reload_routes_replacement_output_through_runtime_log_sinks(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *a, **k: _config_with_tunnel(enabled=False),
    )
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: {"state": "running", "service_pid": 111})
    monkeypatch.setattr(runtime, "write_status", lambda *args: captured.setdefault("status", args))

    def fake_spawn(args, pid_path, stdout_name, stderr_name, env=None):
        captured["spawn"] = (args, pid_path, stdout_name, stderr_name, env)
        return 222

    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert captured["spawn"][1] == runtime.paths.get_runtime_ui_pid_path()
    assert captured["spawn"][2:4] == ("ui_stdout.log", "ui_stderr.log")
    assert captured["status"][-1] == 222

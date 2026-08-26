from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext

import pytest

from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from storage.lock import MigrationLockTimeout
from vibe import runtime, ui_server
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
    memory_ui_secret = "test-memory-ui-secret"
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *a, **k: _config_with_tunnel(enabled=False),
    )
    monkeypatch.setattr("vibe.memory_ui_access._process_secret", memory_ui_secret)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: {"state": "running", "service_pid": 111})
    monkeypatch.setattr(runtime, "write_status", lambda *args: captured.setdefault("status", args))
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda: True)

    def fake_spawn(
        args,
        pid_path,
        stdout_name,
        stderr_name,
        env=None,
        memory_ui_secret=None,
    ):
        captured["spawn"] = (
            args,
            pid_path,
            stdout_name,
            stderr_name,
            env,
            memory_ui_secret,
        )
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
    assert captured["spawn"][5] == memory_ui_secret
    assert captured["status"][-1] == 222


def test_ui_reload_stops_old_server_and_waits_for_replacement_inside_package_lease(
    monkeypatch,
):
    events: list[object] = []
    mutation_lease_held = False

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        nonlocal mutation_lease_held
        mutation_lease_held = True
        events.append(("lock-enter", timeout_seconds))
        try:
            yield
        finally:
            events.append(("lock-exit", timeout_seconds))
            mutation_lease_held = False

    class _Server:
        _should_exit = False

        @property
        def should_exit(self):
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value):
            assert mutation_lease_held is True
            events.append("stop-old")
            self._should_exit = value

    server = _Server()
    monkeypatch.setattr(ui_server, "package_mutation_lock", mutation_lock)
    monkeypatch.setattr(ui_server, "_server", server)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: {"state": "running", "service_pid": 111})
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *args, **kwargs: events.append(("spawn", mutation_lease_held)) or 222,
    )
    monkeypatch.setattr(
        runtime,
        "write_status",
        lambda *args: events.append(("write-status", mutation_lease_held)),
    )
    identities = iter([False, True])
    monkeypatch.setattr(
        runtime,
        "ui_pid_file_points_to_running_ui",
        lambda: events.append(("identity", mutation_lease_held)) or next(identities),
    )
    monkeypatch.setattr(
        "vibe.ui_server.time.sleep",
        lambda delay: events.append(("sleep", delay, mutation_lease_held)),
    )

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert events == [
        ("lock-enter", None),
        "stop-old",
        ("spawn", True),
        ("write-status", True),
        ("identity", True),
        ("sleep", 0.2, True),
        ("identity", True),
        ("lock-exit", None),
    ]
    assert server.should_exit is True


def test_ui_reload_same_port_readiness_uses_replacement_identity_not_health(monkeypatch):
    identity_checks: list[bool] = []

    class _Server:
        should_exit = False

    monkeypatch.setattr(ui_server, "package_mutation_lock", nullcontext)
    monkeypatch.setattr(ui_server, "_server", _Server())
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: {"state": "running", "service_pid": 111})
    monkeypatch.setattr(runtime, "spawn_background", lambda *args, **kwargs: 222)
    monkeypatch.setattr(runtime, "write_status", lambda *args: None)
    monkeypatch.setattr(
        runtime,
        "ui_server_healthy",
        lambda *args, **kwargs: pytest.fail("same-port health can belong to the old UI"),
    )
    monkeypatch.setattr(
        runtime,
        "ui_pid_file_points_to_running_ui",
        lambda: identity_checks.append(True) or True,
    )

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "host": "127.0.0.1", "port": 5123}
    assert identity_checks == [True]


def test_ui_reload_identity_timeout_writes_error_after_one_spawn(monkeypatch, caplog):
    events: list[object] = []

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    class _Server:
        should_exit = False

    server = _Server()
    monkeypatch.setattr(ui_server, "package_mutation_lock", mutation_lock)
    monkeypatch.setattr(ui_server, "_server", server)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        runtime,
        "read_status",
        lambda: {"state": "running", "service_pid": 111, "ui_pid": 110},
    )
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *args, **kwargs: events.append("spawn") or 222,
    )
    monkeypatch.setattr(runtime, "write_status", lambda *args: events.append(("status", args)))
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda: False)
    monkeypatch.setattr("vibe.ui_server.time.sleep", lambda delay: events.append(("sleep", delay)))

    client = app.test_client()
    with caplog.at_level("WARNING"):
        response = client.post(
            "/api/ui/reload",
            json={"host": "127.0.0.1", "port": 5123},
            headers=csrf_headers(client, "http://127.0.0.1:5123"),
            base_url="http://127.0.0.1:5123",
        )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "host": "127.0.0.1", "port": 5123}
    assert server.should_exit is True
    assert events.count("spawn") == 1
    assert events[-2:] == [("status", ("error", "ui_reload_timeout", 111, 222)), "lock-exit"]
    assert events.count(("sleep", 0.2)) == 50
    assert "ui_reload_timeout" in caplog.text


def test_ui_reload_spawn_failure_writes_error_without_retry(monkeypatch, caplog):
    events: list[object] = []

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    class _Server:
        should_exit = False

    server = _Server()
    monkeypatch.setattr(ui_server, "package_mutation_lock", mutation_lock)
    monkeypatch.setattr(ui_server, "_server", server)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        runtime,
        "read_status",
        lambda: {"state": "running", "service_pid": 111, "ui_pid": 110},
    )

    def fail_spawn(*args, **kwargs):
        events.append("spawn")
        raise RuntimeError("popen failed")

    monkeypatch.setattr(runtime, "spawn_background", fail_spawn)
    monkeypatch.setattr(runtime, "write_status", lambda *args: events.append(("status", args)))
    monkeypatch.setattr(
        runtime,
        "ui_pid_file_points_to_running_ui",
        lambda: pytest.fail("identity must not be checked when Popen fails"),
    )

    client = app.test_client()
    with caplog.at_level("ERROR"):
        response = client.post(
            "/api/ui/reload",
            json={"host": "127.0.0.1", "port": 5123},
            headers=csrf_headers(client, "http://127.0.0.1:5123"),
            base_url="http://127.0.0.1:5123",
        )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "host": "127.0.0.1", "port": 5123}
    assert server.should_exit is True
    assert events == [
        "lock-enter",
        "spawn",
        ("status", ("error", "ui_reload_failed", 111, 110)),
        "lock-exit",
    ]
    assert "ui_reload_failed" in caplog.text


def test_ui_reload_retries_busy_worker_then_leaves_current_server_running(
    monkeypatch,
    caplog,
):
    lock_calls: list[float | None] = []
    sleeps: list[float] = []
    spawned: list[bool] = []
    status_writes: list[bool] = []

    @contextmanager
    def blocked_mutation_lock(*, timeout_seconds=None):
        lock_calls.append(timeout_seconds)
        raise MigrationLockTimeout("package mutation is still running")
        yield

    class _Server:
        should_exit = False

    server = _Server()
    monkeypatch.setattr(ui_server, "package_mutation_lock", blocked_mutation_lock)
    monkeypatch.setattr(ui_server, "_server", server)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: {"state": "running", "service_pid": 111})
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *args, **kwargs: spawned.append(True) or 222,
    )
    monkeypatch.setattr(runtime, "write_status", lambda *args: status_writes.append(True))
    monkeypatch.setattr("vibe.ui_server.time.sleep", lambda delay: sleeps.append(delay))

    client = app.test_client()
    with caplog.at_level("WARNING"):
        response = client.post(
            "/api/ui/reload",
            json={"host": "127.0.0.1", "port": 5123},
            headers=csrf_headers(client, "http://127.0.0.1:5123"),
            base_url="http://127.0.0.1:5123",
        )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "host": "127.0.0.1", "port": 5123}
    assert lock_calls == [None, None]
    assert sleeps == [1.0]
    assert spawned == []
    assert status_writes == []
    assert server.should_exit is False
    assert "restart_not_scheduled_package_busy" in caplog.text

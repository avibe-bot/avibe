from __future__ import annotations

import os
import sys
import threading
import types
from contextlib import contextmanager

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


@pytest.fixture(autouse=True)
def _isolated_reload_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime.paths.ensure_data_dirs()
    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: False)


def _write_replacement_facts(pid: int) -> None:
    runtime.paths.get_runtime_ui_pid_path().write_text(str(pid), encoding="utf-8")
    runtime.write_json(
        runtime.paths.get_runtime_dir() / "ui_server_ready.json",
        {"pid": pid, "bound_at": 1.0},
    )


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


def test_ui_reload_admission_refuses_when_restart_is_already_in_flight(monkeypatch):
    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: True)
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *args, **kwargs: pytest.fail("worker must not be constructed"),
    )
    monkeypatch.setattr(
        ui_server,
        "package_mutation_lock",
        lambda **kwargs: pytest.fail("already-live restart should refuse before lock"),
    )

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "restart_in_progress"


def test_ui_reload_admission_reports_package_busy_without_constructing_worker(monkeypatch):
    lock_calls: list[int] = []

    @contextmanager
    def blocked_lock(*, timeout_seconds=None):
        lock_calls.append(timeout_seconds)
        raise MigrationLockTimeout("package mutation is still running")
        yield

    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: False)
    monkeypatch.setattr(ui_server, "package_mutation_lock", blocked_lock)
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *args, **kwargs: pytest.fail("busy admission must not construct worker"),
    )

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "code": "restart_not_scheduled_package_busy",
        "error": "a package operation is in progress; restart was not scheduled",
        "restart": {},
    }
    assert lock_calls == [0]


def test_ui_reload_admission_timeout_distinguishes_restart_that_started(monkeypatch):
    in_flight = iter([False, True])

    @contextmanager
    def blocked_lock(*, timeout_seconds=None):
        raise MigrationLockTimeout("package mutation is still running")
        yield

    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: next(in_flight))
    monkeypatch.setattr(ui_server, "package_mutation_lock", blocked_lock)
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *args, **kwargs: pytest.fail("timeout admission must not construct worker"),
    )

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "restart_in_progress"


def test_ui_reload_admission_releases_predicate_lease_before_spawning(monkeypatch):
    lock_calls: list[int] = []
    threads: list[object] = []

    @contextmanager
    def admission_lock(*, timeout_seconds=None):
        lock_calls.append(timeout_seconds)
        yield

    class _Thread(_NoopThread):
        def __init__(self, *args, **kwargs):
            threads.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ui_server, "package_mutation_lock", admission_lock)
    monkeypatch.setattr(threading, "Thread", _Thread)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "host": "127.0.0.1", "port": 5123}
    assert lock_calls == [0]
    assert len(threads) == 1


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
    status = {"state": "running", "service_pid": 111}
    memory_ui_secret = "test-memory-ui-secret"
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *a, **k: _config_with_tunnel(enabled=False),
    )
    monkeypatch.setattr("vibe.memory_ui_access._process_secret", memory_ui_secret)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: status.copy())
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
        status.update(state="starting", detail="fresh", service_pid=333)
        _write_replacement_facts(222)
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
    assert captured["status"] == ("starting", "fresh", 333, 222)


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
    def spawn(*args, **kwargs):
        events.append(("spawn", mutation_lease_held))
        _write_replacement_facts(222)
        return 222

    monkeypatch.setattr(runtime, "spawn_background", spawn)
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
        ("lock-enter", 0),
        ("lock-exit", 0),
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


def test_ui_reload_waits_for_child_ready_marker_matching_replacement_pid(monkeypatch):
    readiness_events: list[str] = []

    class _Server:
        should_exit = False

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        yield

    monkeypatch.setattr(ui_server, "package_mutation_lock", mutation_lock)
    monkeypatch.setattr(ui_server, "_server", _Server())
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(runtime, "read_status", lambda: {"state": "running", "service_pid": 111})
    def spawn(*args, **kwargs):
        runtime.paths.get_runtime_ui_pid_path().write_text("222", encoding="utf-8")
        runtime.write_json(
            runtime.paths.get_runtime_dir() / "ui_server_ready.json",
            {"pid": 111, "bound_at": 1.0},
        )
        return 222

    monkeypatch.setattr(runtime, "spawn_background", spawn)
    monkeypatch.setattr(runtime, "write_status", lambda *args: None)
    monkeypatch.setattr(
        runtime,
        "ui_pid_file_points_to_running_ui",
        lambda: readiness_events.append("identity") or True,
    )
    monkeypatch.setattr(
        runtime,
        "ui_server_healthy",
        lambda **kwargs: pytest.fail("same-port health must not satisfy readiness"),
    )

    def sleep(_delay):
        readiness_events.append("sleep")
        runtime.write_json(
            runtime.paths.get_runtime_dir() / "ui_server_ready.json",
            {"pid": 222, "bound_at": 2.0},
        )

    monkeypatch.setattr("vibe.ui_server.time.sleep", sleep)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "host": "127.0.0.1", "port": 5123}
    assert readiness_events == ["identity", "sleep", "identity"]


def test_ui_reload_identity_timeout_writes_error_after_one_spawn(monkeypatch, caplog):
    events: list[object] = []
    statuses = iter(
        [
            {"state": "running", "service_pid": 111, "ui_pid": 110},
            {"state": "stopping", "service_pid": 333, "ui_pid": 221},
        ]
    )

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
        lambda: next(statuses),
    )

    def spawn(*args, **kwargs):
        events.append("spawn")
        runtime.paths.get_runtime_ui_pid_path().write_text("222", encoding="utf-8")
        runtime.write_json(
            runtime.paths.get_runtime_dir() / "ui_server_ready.json",
            {"pid": 110, "bound_at": 1.0},
        )
        return 222

    monkeypatch.setattr(runtime, "spawn_background", spawn)
    monkeypatch.setattr(runtime, "write_status", lambda *args: events.append(("status", args)))
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda: True)
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
    assert events[-2:] == [("status", ("error", "ui_reload_timeout", 333, 222)), "lock-exit"]
    assert events.count(("sleep", 0.2)) == 50
    assert "ui_reload_timeout" in caplog.text


def test_ui_reload_retries_one_spawn_failure_inside_the_same_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime.paths.ensure_data_dirs()
    events: list[object] = []
    mutation_lease_held = False

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        nonlocal mutation_lease_held
        mutation_lease_held = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            mutation_lease_held = False

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
    pid_path = runtime.paths.get_runtime_ui_pid_path()
    pid_path.write_text("110", encoding="utf-8")
    spawn_attempts = 0

    def flaky_spawn(args, received_pid_path, *spawn_args, **spawn_kwargs):
        nonlocal spawn_attempts
        spawn_attempts += 1
        assert mutation_lease_held is True
        assert received_pid_path == pid_path
        events.append(("spawn", spawn_attempts))
        if spawn_attempts == 1:
            received_pid_path.write_text("999", encoding="utf-8")
            raise RuntimeError("first popen failed")
        assert received_pid_path.read_text(encoding="utf-8") == "110"
        received_pid_path.write_text("222", encoding="utf-8")
        runtime.write_json(
            runtime.paths.get_runtime_dir() / "ui_server_ready.json",
            {"pid": 222, "bound_at": 1.0},
        )
        return 222

    monkeypatch.setattr(runtime, "spawn_background", flaky_spawn)
    monkeypatch.setattr(runtime, "write_status", lambda *args: events.append(("status", args)))
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda: True)

    client = app.test_client()
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
        "lock-exit",
        "lock-enter",
        ("spawn", 1),
        ("spawn", 2),
        ("status", ("running", None, 111, 222)),
        "lock-exit",
    ]
    assert pid_path.read_text(encoding="utf-8") == "222"


def test_ui_reload_two_spawn_failures_write_error_without_further_retry(
    monkeypatch,
    tmp_path,
    caplog,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime.paths.ensure_data_dirs()
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
        lambda: {"state": "stopping", "service_pid": 333, "ui_pid": 444},
    )
    pid_path = runtime.paths.get_runtime_ui_pid_path()
    pid_path.write_text("110", encoding="utf-8")

    def fail_spawn(args, received_pid_path, *spawn_args, **spawn_kwargs):
        attempt = len([event for event in events if event == "spawn"]) + 1
        events.append("spawn")
        assert received_pid_path.read_text(encoding="utf-8") == "110"
        received_pid_path.write_text(str(900 + attempt), encoding="utf-8")
        raise RuntimeError(f"popen failed {attempt}")

    monkeypatch.setattr(runtime, "spawn_background", fail_spawn)
    monkeypatch.setattr(runtime, "write_status", lambda *args: events.append(("status", args)))
    monkeypatch.setattr(
        runtime,
        "ui_pid_file_points_to_running_ui",
        lambda: pytest.fail("identity must not be checked when both Popen attempts fail"),
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
        "lock-exit",
        "lock-enter",
        "spawn",
        "spawn",
        ("status", ("error", "ui_reload_failed", 333, 444)),
        "lock-exit",
    ]
    assert pid_path.read_text(encoding="utf-8") == "110"
    assert "ui_reload_failed" in caplog.text


def test_ui_reload_spawn_retry_and_readiness_share_one_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime.paths.ensure_data_dirs()
    now = [100.0]
    spawn_attempts = 0
    sleeps: list[float] = []
    statuses: list[tuple] = []

    class _Server:
        should_exit = False

    def spawn(*args, **kwargs):
        nonlocal spawn_attempts
        spawn_attempts += 1
        if spawn_attempts == 1:
            now[0] = 109.9
            raise RuntimeError("slow first failure")
        runtime.paths.get_runtime_ui_pid_path().write_text("222", encoding="utf-8")
        runtime.write_json(
            runtime.paths.get_runtime_dir() / "ui_server_ready.json",
            {"pid": 110, "bound_at": 1.0},
        )
        return 222

    def sleep(delay: float):
        sleeps.append(delay)
        now[0] += delay

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        yield

    monkeypatch.setattr(ui_server, "package_mutation_lock", mutation_lock)
    monkeypatch.setattr(ui_server, "_server", _Server())
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    statuses_to_read = iter(
        [
            {"state": "running", "service_pid": 111, "ui_pid": 110},
            {"state": "stopping", "service_pid": 333, "ui_pid": 221},
        ]
    )
    monkeypatch.setattr(runtime, "read_status", lambda: next(statuses_to_read))
    monkeypatch.setattr(runtime, "spawn_background", spawn)
    monkeypatch.setattr(runtime, "write_status", lambda *args: statuses.append(args))
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", lambda: True)
    monkeypatch.setattr("vibe.ui_server.time.monotonic", lambda: now[0])
    monkeypatch.setattr("vibe.ui_server.time.sleep", sleep)

    client = app.test_client()
    response = client.post(
        "/api/ui/reload",
        json={"host": "127.0.0.1", "port": 5123},
        headers=csrf_headers(client, "http://127.0.0.1:5123"),
        base_url="http://127.0.0.1:5123",
    )

    assert response.status_code == 200
    assert spawn_attempts == 2
    assert sleeps == pytest.approx([0.1])
    assert statuses[-1] == ("error", "ui_reload_timeout", 333, 222)


def test_ui_reload_refuses_restart_in_flight_before_stopping_current_server(
    monkeypatch,
    caplog,
):
    events: list[str] = []

    @contextmanager
    def mutation_lock(*, timeout_seconds=None):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    class _Server:
        _should_exit = False

        @property
        def should_exit(self):
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value):
            events.append("stop")
            self._should_exit = value

    server = _Server()
    monkeypatch.setattr(ui_server, "package_mutation_lock", mutation_lock)
    in_flight = iter([False, False, True])
    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: next(in_flight))
    monkeypatch.setattr(ui_server, "_server", server)
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *args, **kwargs: pytest.fail("restart overlap must not spawn"),
    )
    monkeypatch.setattr(
        runtime,
        "write_status",
        lambda *args: pytest.fail("restart overlap must not write status"),
    )

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
    assert events == [
        "lock-enter",
        "lock-exit",
        "lock-enter",
        "lock-exit",
    ]
    assert server.should_exit is False
    assert "restart_in_progress" in caplog.text


def test_ui_reload_retries_busy_worker_then_leaves_current_server_running(
    monkeypatch,
    caplog,
):
    lock_calls: list[float | None] = []
    sleeps: list[float] = []
    spawned: list[bool] = []
    status_writes: list[bool] = []

    lock_attempts = 0

    @contextmanager
    def blocked_mutation_lock(*, timeout_seconds=None):
        nonlocal lock_attempts
        lock_attempts += 1
        lock_calls.append(timeout_seconds)
        if lock_attempts > 1:
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
    assert lock_calls == [0, None, None]
    assert sleeps == [1.0]
    assert spawned == []
    assert status_writes == []
    assert server.should_exit is False
    assert "restart_not_scheduled_package_busy" in caplog.text


def test_run_ui_server_writes_child_ready_marker_after_bind_before_run(monkeypatch):
    events: list[str] = []
    actual_write_json = runtime.write_json

    class _Socket:
        pass

    class _Server:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, sockets=None):
            events.append("run")

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = lambda *args, **kwargs: (args, kwargs)
    fake_uvicorn.Server = _Server
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(
        "vibe.memory_ui_access.initialize_process_ui_read_secret",
        lambda: None,
    )
    monkeypatch.setattr(
        "core.services.settings.load_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(threading, "Thread", _NoopThread)
    monkeypatch.setattr(
        ui_server,
        "_bind_ui_socket",
        lambda host, port: events.append("bind") or _Socket(),
    )

    def write_json(path, payload):
        events.append("marker")
        actual_write_json(path, payload)

    monkeypatch.setattr(runtime, "write_json", write_json)

    ui_server.run_ui_server("127.0.0.1", 5123)

    marker = runtime.read_json(runtime.paths.get_runtime_dir() / "ui_server_ready.json")
    assert events == ["bind", "marker", "run"]
    assert marker["pid"] == os.getpid()
    assert isinstance(marker["bound_at"], float)

from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
import urllib.error
from types import SimpleNamespace

import pytest

from config import paths
from config.v2_config import (
    AgentsConfig,
    PlatformsConfig,
    RemoteAccessConfig,
    RuntimeConfig,
    SlackConfig,
    UiConfig,
    V2Config,
)
from vibe import cli, runtime
from vibe.desktop_runtime import (
    desktop_endpoint_payload,
    desktop_origin,
    requires_desktop_loopback_listener,
    ui_listener_hosts,
)
from vibe.ui_server import _bind_ui_sockets, app


@pytest.mark.parametrize(
    ("bind_host", "expected_origin", "expected_listeners"),
    [
        ("127.0.0.1", "http://127.0.0.1:5123", ("127.0.0.1",)),
        ("127.0.0.2", "http://127.0.0.1:5123", ("127.0.0.2", "127.0.0.1")),
        ("0.0.0.0", "http://127.0.0.1:5123", ("0.0.0.0",)),
        ("192.168.1.20", "http://127.0.0.1:5123", ("192.168.1.20", "127.0.0.1")),
        ("100.97.103.112", "http://127.0.0.1:5123", ("100.97.103.112", "127.0.0.1")),
        ("::1", "http://[::1]:5123", ("::1",)),
        ("::", "http://[::1]:5123", ("::",)),
        ("fd7a:115c:a1e0::42", "http://[::1]:5123", ("fd7a:115c:a1e0::42", "::1")),
    ],
)
def test_desktop_origin_and_listener_contract(bind_host, expected_origin, expected_listeners):
    assert desktop_origin(bind_host, 5123) == expected_origin
    assert desktop_endpoint_payload(bind_host, 5123) == {
        "schema_version": 1,
        "origin": expected_origin,
    }
    assert ui_listener_hosts(bind_host) == expected_listeners


def test_specific_hostname_gets_ipv4_desktop_listener(monkeypatch):
    def unresolved(*_args, **_kwargs):
        raise socket.gaierror("unresolved test hostname")

    monkeypatch.setattr(socket, "getaddrinfo", unresolved)

    assert requires_desktop_loopback_listener("192.0.2.20.example.invalid") is True
    assert ui_listener_hosts("192.0.2.20.example.invalid") == (
        "192.0.2.20.example.invalid",
        "127.0.0.1",
    )


def test_localhost_does_not_add_duplicate_listener(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ],
    )

    assert requires_desktop_loopback_listener("localhost") is False
    assert ui_listener_hosts("localhost") == ("localhost",)


def test_hostname_resolving_to_loopback_does_not_add_duplicate_listener(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ],
    )
    assert requires_desktop_loopback_listener("avibe-loopback.test") is False
    assert ui_listener_hosts("avibe-loopback.test") == ("avibe-loopback.test",)


def test_hostname_resolving_to_another_ipv4_loopback_adds_advertised_listener(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", 0))
        ],
    )
    assert requires_desktop_loopback_listener("avibe-other-loopback.test") is True
    assert ui_listener_hosts("avibe-other-loopback.test") == (
        "avibe-other-loopback.test",
        "127.0.0.1",
    )


def test_numeric_string_port_is_normalized_for_endpoint_and_health_urls():
    assert desktop_origin("127.0.0.1", "05123") == "http://127.0.0.1:5123"
    assert desktop_endpoint_payload("127.0.0.1", "5123") == {
        "schema_version": 1,
        "origin": "http://127.0.0.1:5123",
    }
    assert runtime._ui_health_urls("127.0.0.1", "5123") == (
        "http://127.0.0.1:5123/health",
        "http://127.0.0.1:5123/ready",
    )


@pytest.mark.parametrize(
    "port",
    [
        0,
        -1,
        65536,
        True,
        None,
        5123.0,
        "",
        "0",
        "65536",
        " 5123",
        "+5123",
        "5123.0",
        "１２３４",
    ],
)
def test_desktop_origin_rejects_invalid_ports(port):
    with pytest.raises(ValueError, match="between 1 and 65535"):
        desktop_origin("127.0.0.1", port)


def test_bind_ui_sockets_adds_same_port_loopback_for_specific_bind(monkeypatch):
    calls = []
    sockets = [object(), object()]

    def fake_bind(host, port):
        calls.append((host, port))
        return sockets[len(calls) - 1]

    monkeypatch.setattr("vibe.ui_server._bind_ui_socket", fake_bind)

    assert _bind_ui_sockets("100.97.103.112", 5123) == sockets
    assert calls == [("100.97.103.112", 5123), ("127.0.0.1", 5123)]


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "127.0.0.1", "::", "::1", "*"])
def test_bind_ui_sockets_does_not_duplicate_wildcard_or_loopback(bind_host, monkeypatch):
    calls = []

    def fake_bind(host, port):
        calls.append((host, port))
        return object()

    monkeypatch.setattr("vibe.ui_server._bind_ui_socket", fake_bind)

    assert len(_bind_ui_sockets(bind_host, 5123)) == 1
    assert calls == [(bind_host, 5123)]


def test_bind_ui_sockets_closes_primary_when_loopback_bind_fails(monkeypatch):
    class FakeSocket:
        closed = False

        def close(self):
            self.closed = True

    primary = FakeSocket()

    def fake_bind(host, _port):
        if host == "127.0.0.1":
            raise OSError("loopback unavailable")
        return primary

    monkeypatch.setattr("vibe.ui_server._bind_ui_socket", fake_bind)

    with pytest.raises(OSError, match="loopback unavailable"):
        _bind_ui_sockets("192.168.1.20", 5123)

    assert primary.closed is True


def test_ui_health_urls_require_primary_and_desktop_listener():
    assert runtime._ui_health_urls("100.97.103.112", 5123) == (
        "http://100.97.103.112:5123/health",
        "http://127.0.0.1:5123/ready",
    )
    assert runtime._ui_health_urls("fd7a:115c:a1e0::42", 5123) == (
        "http://[fd7a:115c:a1e0::42]:5123/health",
        "http://[::1]:5123/ready",
    )


def test_ui_health_urls_require_ready_identity_on_default_loopback_bind():
    assert runtime._ui_health_urls("127.0.0.1", 5123) == (
        "http://127.0.0.1:5123/health",
        "http://127.0.0.1:5123/ready",
    )
    assert runtime._ui_health_urls("0.0.0.0", 5123) == (
        "http://127.0.0.1:5123/health",
        "http://127.0.0.1:5123/ready",
    )


def test_ui_server_health_fails_when_old_specific_bind_lacks_desktop_listener(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload=b""):
            self.status = 200
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self._payload

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        if url == "http://127.0.0.1:5123/ready":
            raise OSError("connection refused")
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime.ui_server_healthy("100.97.103.112", 5123) is False
    assert calls == [
        ("http://100.97.103.112:5123/health", 0.5),
        ("http://127.0.0.1:5123/ready", 0.5),
    ]


def test_ui_server_health_requires_versioned_ready_identity_for_companion_listener(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.status = 200
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self._payload

    def fake_urlopen(url, timeout):
        del timeout
        if url.endswith("/ready"):
            return Response(b'{\"ready\":true}')
        return Response(b"")

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime.ui_server_healthy("100.97.103.112", 5123) is False


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (
            200,
            {
                "schema_version": 1,
                "product": "avibe",
                "ready": 1,
            },
        ),
        (
            503,
            {
                "schema_version": True,
                "product": "avibe",
                "ready": False,
                "code": "service_starting",
            },
        ),
        (
            503,
            {
                "schema_version": 1,
                "product": "avibe",
                "ready": 0,
                "code": "service_starting",
            },
        ),
    ],
)
def test_ready_identity_rejects_bool_integer_equivalence(status, payload):
    class Response:
        def __init__(self):
            self.status = status

        def read(self):
            return json.dumps(payload).encode("utf-8")

    assert runtime._ui_ready_identity_state(Response()) is None


def test_ui_server_compatibility_accepts_versioned_not_ready_identity(monkeypatch):
    payload = json.dumps(
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": False,
            "code": "controller_unavailable",
        }
    ).encode("utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(url, timeout):
        del timeout
        if url.endswith("/ready"):
            raise urllib.error.HTTPError(
                url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(payload),
            )
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime.ui_server_healthy("100.97.103.112", 5123) is False
    assert runtime._ui_server_compatible("100.97.103.112", 5123) is True


@pytest.mark.parametrize(
    "payload",
    [
        {"ready": False, "code": "controller_unavailable"},
        {
            "schema_version": 1,
            "product": "other",
            "ready": False,
            "code": "controller_unavailable",
        },
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": False,
        },
    ],
)
def test_ui_server_compatibility_rejects_invalid_not_ready_identity(monkeypatch, payload):
    encoded_payload = json.dumps(payload).encode("utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(url, timeout):
        del timeout
        if url.endswith("/ready"):
            raise urllib.error.HTTPError(
                url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(encoded_payload),
            )
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime._ui_server_compatible("100.97.103.112", 5123) is False


def test_start_ui_restarts_old_specific_bind_without_desktop_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")
    stopped = []

    class Response:
        def __init__(self, payload=b""):
            self.status = 200
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self._payload

    def fake_urlopen(url, timeout):
        del timeout
        if url == "http://127.0.0.1:5123/ready":
            raise OSError("old UI has no loopback listener")
        return Response()

    def fake_spawn(_args, pid_path, _stdout_name, _stderr_name, env=None):
        del env
        pid_path.write_text("67890", encoding="utf-8")
        return 67890

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: "from vibe.ui_server import run_ui_server; run_ui_server('100.97.103.112', 5123)"
        if pid == 12345
        else None,
    )
    monkeypatch.setattr(runtime, "stop_pid", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda _host, _port: True)

    assert runtime.start_ui("100.97.103.112", 5123) == 67890
    assert stopped == [12345]


def test_start_ui_adopts_versioned_not_ready_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")
    payload = json.dumps(
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": False,
            "code": "service_starting",
        }
    ).encode("utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    probe_timeouts = []

    def fake_urlopen(url, timeout):
        probe_timeouts.append(timeout)
        if url.endswith("/ready"):
            raise urllib.error.HTTPError(
                url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(payload),
            )
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: "from vibe.ui_server import run_ui_server; run_ui_server('100.97.103.112', 5123)"
        if pid == 12345
        else None,
    )
    monkeypatch.setattr(runtime, "stop_pid", lambda _pid: pytest.fail("compatible UI must not be stopped"))
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *_args, **_kwargs: pytest.fail("compatible UI must not be replaced"),
    )

    assert runtime.start_ui("100.97.103.112", 5123) == 12345
    assert probe_timeouts == [runtime.UI_ADOPTION_PROBE_TIMEOUT_SECONDS] * 2


def test_start_ui_normalizes_numeric_string_port_before_spawning(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    spawned = []

    def fake_spawn(args, pid_path, stdout_name, stderr_name, env=None):
        spawned.append((args, pid_path, stdout_name, stderr_name, env))
        return 67890

    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)

    assert runtime.start_ui("127.0.0.1", "05123", wait_for_ready=False) == 67890
    assert spawned[0][0] == [
        runtime.sys.executable,
        "-c",
        "from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5123)",
    ]


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_start_ui_restarts_old_default_or_wildcard_ui_without_ready_identity(tmp_path, monkeypatch, host):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")
    stopped = []

    class Response:
        def __init__(self, payload=b""):
            self.status = 200
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self._payload

    def fake_urlopen(url, timeout):
        del timeout
        if url.endswith("/ready"):
            raise OSError("old UI lacks ready contract")
        return Response()

    def fake_spawn(_args, pid_path, _stdout_name, _stderr_name, env=None):
        del env
        pid_path.write_text("67890", encoding="utf-8")
        return 67890

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: f"from vibe.ui_server import run_ui_server; run_ui_server('{host}', 5123)"
        if pid == 12345
        else None,
    )
    monkeypatch.setattr(runtime, "stop_pid", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(runtime, "spawn_background", fake_spawn)
    monkeypatch.setattr(runtime, "wait_for_ui_server", lambda _host, _port: True)

    assert runtime.start_ui(host, 5123) == 67890
    assert stopped == [12345]


def test_desktop_endpoint_cli_emits_only_schema_v1_json(monkeypatch, capsys):
    config = SimpleNamespace(ui=SimpleNamespace(setup_port=6123))
    monkeypatch.setattr(cli, "_guard_cli_default_state_migration", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda _config: "100.97.103.112")
    monkeypatch.setattr(cli, "_open_browser", lambda _url: pytest.fail("desktop endpoint must not open a browser"))

    assert cli.cmd_desktop_endpoint() == 0

    captured = capsys.readouterr()
    assert captured.out == '{"schema_version":1,"origin":"http://127.0.0.1:6123"}\n'
    assert captured.err == ""


def test_desktop_endpoint_cli_parser_requires_explicit_json():
    args = cli.build_parser().parse_args(["desktop", "endpoint", "--json"])
    assert args.command == "desktop"
    assert args.desktop_command == "endpoint"
    assert args.json is True

    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["desktop", "endpoint"])
    assert exc.value.code == 2


def _ready_response(monkeypatch, owners, *, controller_ready):
    owner_iter = iter(owners)

    def resolve_owner(*, include_starting):
        return next(owner_iter)

    async def health():
        return controller_ready

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr("vibe.internal_client.health", health)
    return app.test_client().get("/ready", base_url="http://127.0.0.1:5123")


def test_ready_reports_service_starting(monkeypatch):
    response = _ready_response(monkeypatch, [None, 1234], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "service_starting",
    }


def test_ready_reports_service_unavailable(monkeypatch):
    response = _ready_response(monkeypatch, [None, None], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "service_unavailable",
    }


def test_ready_reports_controller_unavailable(monkeypatch):
    response = _ready_response(monkeypatch, [1234, 1234], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "controller_unavailable",
    }


def test_ready_reports_owner_race_after_controller_probe(monkeypatch):
    response = _ready_response(monkeypatch, [1234, 5678], controller_ready=True)
    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "ownership_lost",
    }


def test_ready_reports_owner_loss_even_when_controller_probe_fails(monkeypatch):
    response = _ready_response(monkeypatch, [1234, None], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "ownership_lost",
    }


def test_ready_requires_stable_owner_and_healthy_controller(monkeypatch):
    response = _ready_response(monkeypatch, [1234, 1234], controller_ready=True)
    assert response.status_code == 200
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": True,
    }
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    ("owners", "controller_ready"),
    [
        ([OSError("lock unreadable")], False),
        ([None, OSError("lock unreadable")], False),
        ([1234, OSError("lock unreadable")], True),
    ],
)
def test_ready_preserves_schema_when_owner_probe_fails(monkeypatch, owners, controller_ready):
    owner_iter = iter(owners)

    def resolve_owner(*, include_starting):
        del include_starting
        result = next(owner_iter)
        if isinstance(result, Exception):
            raise result
        return result

    async def health():
        return controller_ready

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr("vibe.internal_client.health", health)

    response = app.test_client().get("/ready", base_url="http://127.0.0.1:5123")

    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "owner_probe_failed",
    }


def test_ready_offloads_all_service_owner_probes(monkeypatch):
    owner_iter = iter([1234, 1234, None, 5678])
    owner_calls = []
    offloaded_calls = []
    original_to_thread = asyncio.to_thread

    def resolve_owner(*, include_starting):
        owner_calls.append((include_starting, threading.get_ident()))
        return next(owner_iter)

    async def health():
        return True

    async def track_to_thread(func, *args, **kwargs):
        event_loop_thread = threading.get_ident()
        result = await original_to_thread(func, *args, **kwargs)
        offloaded_calls.append((func, kwargs, event_loop_thread))
        return result

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr("vibe.internal_client.health", health)
    monkeypatch.setattr("vibe.ui_server.asyncio.to_thread", track_to_thread)

    client = app.test_client()
    ready_response = client.get("/ready", base_url="http://127.0.0.1:5123")
    starting_response = client.get("/ready", base_url="http://127.0.0.1:5123")

    assert ready_response.status_code == 200
    assert starting_response.status_code == 503
    assert [include_starting for include_starting, _thread in owner_calls] == [
        False,
        False,
        False,
        True,
    ]
    assert [kwargs["include_starting"] for _func, kwargs, _thread in offloaded_calls] == [
        False,
        False,
        False,
        True,
    ]
    assert all(func is resolve_owner for func, _kwargs, _thread in offloaded_calls)
    assert all(
        worker_thread != event_loop_thread
        for (_include_starting, worker_thread), (_func, _kwargs, event_loop_thread) in zip(
            owner_calls,
            offloaded_calls,
            strict=True,
        )
    )


def _save_remote_access_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
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


@pytest.mark.parametrize(
    ("base_url", "remote_addr", "headers"),
    [
        ("http://attacker.example", "127.0.0.1", {}),
        ("http://127.0.0.1:5123", "203.0.113.10", {}),
        ("http://127.0.0.1.example", "127.0.0.1", {}),
        (
            "http://127.0.0.1:5123",
            "127.0.0.1",
            {"X-Forwarded-For": "203.0.113.10"},
        ),
    ],
)
def test_desktop_runtime_host_header_contract_rejects_non_loopback_local_trust(
    monkeypatch,
    tmp_path,
    base_url,
    remote_addr,
    headers,
):
    _save_remote_access_config(monkeypatch, tmp_path)

    async def fail_health():
        pytest.fail("blocked Host must not reach the readiness route")

    monkeypatch.setattr("vibe.internal_client.health", fail_health)

    response = app.test_client().get(
        "/ready",
        base_url=base_url,
        environ_base={"REMOTE_ADDR": remote_addr},
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "remote_access_host_mismatch"


def test_bind_ui_socket_uses_ipv6_family(monkeypatch):
    created_families = []

    class FakeSocket:
        def setsockopt(self, *_args):
            return None

        def bind(self, address):
            assert address == ("::1", 5123)

        def set_inheritable(self, _value):
            return None

    def fake_socket(family):
        created_families.append(family)
        return FakeSocket()

    monkeypatch.setattr(socket, "socket", fake_socket)

    from vibe.ui_server import _bind_ui_socket

    _bind_ui_socket("::1", 5123)
    assert created_families == [socket.AF_INET6]

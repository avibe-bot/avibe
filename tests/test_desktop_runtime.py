from __future__ import annotations

import json
import socket
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


def test_specific_hostname_gets_ipv4_desktop_listener():
    assert requires_desktop_loopback_listener("192.0.2.20.example.invalid") is True
    assert ui_listener_hosts("192.0.2.20.example.invalid") == (
        "192.0.2.20.example.invalid",
        "127.0.0.1",
    )


def test_localhost_does_not_add_duplicate_listener():
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


@pytest.mark.parametrize("port", [0, -1, 65536, True, "5123"])
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
        "http://127.0.0.1:5123/health",
    )
    assert runtime._ui_health_urls("fd7a:115c:a1e0::42", 5123) == (
        "http://[fd7a:115c:a1e0::42]:5123/health",
        "http://[::1]:5123/health",
    )


def test_ui_server_health_fails_when_old_specific_bind_lacks_desktop_listener(monkeypatch):
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        if url == "http://127.0.0.1:5123/health":
            raise OSError("connection refused")
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime.ui_server_healthy("100.97.103.112", 5123) is False
    assert calls == [
        ("http://100.97.103.112:5123/health", 0.5),
        ("http://127.0.0.1:5123/health", 0.5),
    ]


def test_start_ui_restarts_old_specific_bind_without_desktop_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")
    stopped = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(url, timeout):
        del timeout
        if url == "http://127.0.0.1:5123/health":
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
    assert response.get_json() == {"ready": False, "code": "service_starting"}


def test_ready_reports_service_unavailable(monkeypatch):
    response = _ready_response(monkeypatch, [None, None], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {"ready": False, "code": "service_unavailable"}


def test_ready_reports_controller_unavailable(monkeypatch):
    response = _ready_response(monkeypatch, [1234, 1234], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {"ready": False, "code": "controller_unavailable"}


def test_ready_reports_owner_race_after_controller_probe(monkeypatch):
    response = _ready_response(monkeypatch, [1234, 5678], controller_ready=True)
    assert response.status_code == 503
    assert response.get_json() == {"ready": False, "code": "ownership_lost"}


def test_ready_reports_owner_loss_even_when_controller_probe_fails(monkeypatch):
    response = _ready_response(monkeypatch, [1234, None], controller_ready=False)
    assert response.status_code == 503
    assert response.get_json() == {"ready": False, "code": "ownership_lost"}


def test_ready_requires_stable_owner_and_healthy_controller(monkeypatch):
    response = _ready_response(monkeypatch, [1234, 1234], controller_ready=True)
    assert response.status_code == 200
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": True,
    }
    assert response.headers["Cache-Control"] == "no-store"


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

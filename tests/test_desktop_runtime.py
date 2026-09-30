from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
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
from vibe import cli, internal_client, runtime
from vibe.desktop_runtime import (
    desktop_caller_provenance,
    desktop_runtime_id,
    desktop_endpoint_payload,
    desktop_origin,
    is_private_desktop_runtime_path,
    private_desktop_backends_root,
    private_desktop_node_bin,
    private_desktop_npm_cli,
    private_desktop_runtime_root,
    requires_desktop_loopback_listener,
    ui_listener_hosts,
)
from vibe.ui_server import _bind_ui_sockets, app


READY_EXTERNAL_CONTROLLER_BUNDLED_UI_FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "desktop_ready_external_controller_bundled_ui.json"
    ).read_text(encoding="utf-8")
)


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
        ("*", "http://127.0.0.1:5123", ("0.0.0.0",)),
        ("[::1]", "http://[::1]:5123", ("::1",)),
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


def test_desktop_runtime_id_accepts_only_lowercase_sha256():
    runtime_id = "a" * 64

    assert desktop_runtime_id({"AVIBE_DESKTOP_RUNTIME_ID": runtime_id}) == runtime_id
    assert desktop_runtime_id({"AVIBE_DESKTOP_RUNTIME_ID": "A" * 64}) is None
    assert desktop_runtime_id({"AVIBE_DESKTOP_RUNTIME_ID": "short"}) is None
    assert desktop_runtime_id({}) is None


def _host_marker(runtime_id: str) -> dict[str, object]:
    """The marker the desktop host writes into a private tree it unpacks.

    `write_marker` in desktop/runtime-host/src/private_runtime.rs serialises
    `RuntimeBundleManifest` into `.avibe-runtime.json` at the tree root,
    `<install root>/<runtime_version>/<archive_sha256[:16]>/`. Field names and
    values are copied from that struct and its `write_bundle` test fixture.
    """

    return {
        "schema_version": 2,
        "runtime_version": "3.0.0-test",
        "os": "macos",
        "arch": "aarch64",
        "archive": "runtime.zip",
        "archive_sha256": runtime_id,
        "archive_size": 1234,
        "unpacked_size": 21,
        "entry_count": 3,
        "tree_sha256": "d" * 64,
        "python_entrypoint": "python/bin/python3",
        "node_entrypoint": "tools/bin/node",
        "npm_entrypoint": "tools/npm/bin/npm-cli.js",
        "python_distribution": {"url": "https://example.invalid/python", "sha256": "a" * 64},
        "node_distribution": {"url": "https://example.invalid/node", "sha256": "b" * 64},
        "npm_version": "10.9.8",
        "avibe_wheel": {"name": "avibe_os-3.0.0-py3-none-any.whl", "sha256": "c" * 64},
    }


def _write_marker(path: Path, marker: object) -> None:
    # serde_json::to_vec writes compact JSON.
    path.write_text(json.dumps(marker, separators=(",", ":")), encoding="utf-8")


@pytest.fixture
def desktop_tree(tmp_path):
    """An unpacked private tree: `python/bin/python3` links to `python3.12`, as in the bundle."""

    if os.name == "nt":
        pytest.skip("the POSIX tree layout links python3 to python3.12")
    runtime_id = "e" * 64
    root = tmp_path / "install" / "3.0.0-test" / runtime_id[:16]
    bin_dir = root / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.12").write_bytes(b"")
    (bin_dir / "python3").symlink_to("python3.12")
    _write_marker(root / ".avibe-runtime.json", _host_marker(runtime_id))
    return SimpleNamespace(root=root, interpreter=bin_dir / "python3", runtime_id=runtime_id)


@pytest.mark.parametrize("name", ["python3", "python3.12"])
def test_a_desktop_tree_interpreter_acts_for_its_tree(desktop_tree, monkeypatch, name):
    monkeypatch.setattr(sys, "executable", str(desktop_tree.interpreter.parent / name))
    other = "f" * 64

    assert desktop_caller_provenance() == {desktop_tree.runtime_id}
    monkeypatch.setenv("AVIBE_DESKTOP_RUNTIME_ID", desktop_tree.runtime_id)
    assert desktop_caller_provenance() == {desktop_tree.runtime_id}
    monkeypatch.setenv("AVIBE_DESKTOP_RUNTIME_ID", other)
    assert desktop_caller_provenance() == {desktop_tree.runtime_id, other}


def test_a_caller_outside_any_tree_has_only_its_env_provenance(desktop_tree, monkeypatch, tmp_path):
    outside = tmp_path / "venv" / "bin" / "python3"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(outside))

    assert desktop_caller_provenance() == frozenset()
    monkeypatch.setenv("AVIBE_DESKTOP_RUNTIME_ID", desktop_tree.runtime_id)
    assert desktop_caller_provenance() == {desktop_tree.runtime_id}


# Every interpreter layout a desktop bundle ships, from the bundle's own sources.
_BUNDLE_PYTHON_ENTRYPOINTS = sorted(
    {
        target["python_entrypoint"]
        for target in json.loads(
            (Path(__file__).resolve().parents[1] / "desktop" / "runtime-sources.json").read_text(encoding="utf-8")
        )["targets"].values()
    }
)


@pytest.fixture(params=_BUNDLE_PYTHON_ENTRYPOINTS)
def bundle_tree(request, tmp_path):
    """A private tree with the interpreter where one of the bundle targets puts it."""

    runtime_id = "e" * 64
    root = tmp_path / "install" / "3.0.0-test" / runtime_id[:16]
    interpreter = root / request.param
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    _write_marker(root / ".avibe-runtime.json", {**_host_marker(runtime_id), "python_entrypoint": request.param})
    return SimpleNamespace(root=root, interpreter=interpreter, entrypoint=request.param, runtime_id=runtime_id)


def test_every_bundle_interpreter_acts_for_its_tree(bundle_tree, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(bundle_tree.interpreter))

    assert desktop_caller_provenance() == {bundle_tree.runtime_id}


def test_a_marker_above_the_tree_root_names_no_tree(bundle_tree, monkeypatch):
    # Its entrypoint does lead to this interpreter, but from above the root.
    (bundle_tree.root / ".avibe-runtime.json").unlink()
    stray = {**_host_marker("f" * 64), "python_entrypoint": f"{bundle_tree.root.name}/{bundle_tree.entrypoint}"}
    _write_marker(bundle_tree.root.parent / ".avibe-runtime.json", stray)
    monkeypatch.setattr(sys, "executable", str(bundle_tree.interpreter))

    assert desktop_caller_provenance() == frozenset()


def _replace_marker(**fields):
    def write(root: Path, interpreter: Path, runtime_id: str) -> None:
        _write_marker(root / ".avibe-runtime.json", {**_host_marker(runtime_id), **fields})

    return write


def _absolute_entrypoint(root: Path, interpreter: Path, runtime_id: str) -> None:
    _replace_marker(python_entrypoint=str(interpreter))(root, interpreter, runtime_id)


def _missing_field(name: str):
    def write(root: Path, interpreter: Path, runtime_id: str) -> None:
        marker = _host_marker(runtime_id)
        del marker[name]
        _write_marker(root / ".avibe-runtime.json", marker)

    return write


def _raw_marker(raw: bytes):
    def write(root: Path, interpreter: Path, runtime_id: str) -> None:
        (root / ".avibe-runtime.json").write_bytes(raw)

    return write


def _oversized_marker(root: Path, interpreter: Path, runtime_id: str) -> None:
    raw = json.dumps(_host_marker(runtime_id)).encode()
    (root / ".avibe-runtime.json").write_bytes(raw + b" " * (32 * 1024 + 1 - len(raw)))


def _symlinked_marker(root: Path, interpreter: Path, runtime_id: str) -> None:
    elsewhere = root.parent / "marker.json"
    _write_marker(elsewhere, _host_marker(runtime_id))
    (root / ".avibe-runtime.json").symlink_to(elsewhere)


@pytest.mark.parametrize(
    "write_marker",
    [
        pytest.param(_replace_marker(archive_sha256="E" * 64), id="uppercase-id"),
        pytest.param(_replace_marker(archive_sha256="e" * 63), id="short-id"),
        pytest.param(_missing_field("archive_sha256"), id="no-id"),
        pytest.param(_missing_field("python_entrypoint"), id="no-entrypoint"),
        pytest.param(_replace_marker(python_entrypoint=["python", "bin", "python3"]), id="entrypoint-not-a-string"),
        pytest.param(_replace_marker(python_entrypoint="tools/bin/node"), id="entrypoint-elsewhere"),
        pytest.param(_absolute_entrypoint, id="absolute-entrypoint"),
        pytest.param(_raw_marker(b"[]"), id="not-an-object"),
        pytest.param(_raw_marker(b"{not json"), id="not-json"),
        pytest.param(_oversized_marker, id="oversized"),
        pytest.param(_symlinked_marker, id="symlinked"),
    ],
)
def test_an_invalid_root_marker_names_no_tree(desktop_tree, monkeypatch, write_marker):
    marker = desktop_tree.root / ".avibe-runtime.json"
    marker.unlink()
    write_marker(desktop_tree.root, desktop_tree.interpreter, desktop_tree.runtime_id)
    # A marker further up does not stand in for the broken one.
    _write_marker(desktop_tree.root.parent / ".avibe-runtime.json", _host_marker(desktop_tree.runtime_id))
    monkeypatch.setattr(sys, "executable", str(desktop_tree.interpreter))

    assert desktop_caller_provenance() == frozenset()


def test_private_desktop_runtime_path_is_confined_to_absolute_launcher_root(tmp_path):
    root = tmp_path / "runtime" / "3.1.0" / ("a" * 16)
    env = {"AVIBE_DESKTOP_RUNTIME_ROOT": str(root)}

    assert private_desktop_runtime_root(env) == root
    assert is_private_desktop_runtime_path(root / "tools" / "bin" / "codex", env)
    assert not is_private_desktop_runtime_path(tmp_path / "runtime-other" / "codex", env)
    assert not is_private_desktop_runtime_path("tools/bin/codex", env)
    assert private_desktop_runtime_root({"AVIBE_DESKTOP_RUNTIME_ROOT": "relative/runtime"}) is None


def test_private_desktop_backend_toolchain_paths_are_separate_and_confined(tmp_path):
    runtime_root = tmp_path / "runtime" / "3.1.0" / ("a" * 16)
    node = runtime_root / "tools" / "node"
    npm_cli = runtime_root / "tools" / "npm" / "bin" / "npm-cli.js"
    node.parent.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    node.write_text("node", encoding="utf-8")
    node.chmod(0o755)
    npm_cli.write_text("npm", encoding="utf-8")
    backends_root = tmp_path / "backends"
    env = {
        "AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime_root),
        "VIBE_SHOW_RUNTIME_NODE_BIN": str(node),
        "AVIBE_DESKTOP_NPM_CLI": str(npm_cli),
        "AVIBE_DESKTOP_BACKENDS_ROOT": str(backends_root),
    }

    assert private_desktop_node_bin(env) == node
    assert private_desktop_npm_cli(env) == npm_cli
    assert private_desktop_backends_root(env) == backends_root
    assert private_desktop_backends_root(
        {**env, "AVIBE_DESKTOP_BACKENDS_ROOT": str(runtime_root / "backends")}
    ) is None
    assert private_desktop_npm_cli(
        {**env, "AVIBE_DESKTOP_NPM_CLI": str(tmp_path / "outside-npm.js")}
    ) is None


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


@pytest.mark.parametrize(
    ("bind_host", "expected_listener"),
    [
        ("0.0.0.0", "0.0.0.0"),
        ("127.0.0.1", "127.0.0.1"),
        ("::", "::"),
        ("::1", "::1"),
        # The two spellings a person can configure that are not addresses: the
        # listener they produce has to be one a socket can be given.
        ("*", "0.0.0.0"),
        ("[::1]", "::1"),
    ],
)
def test_bind_ui_sockets_does_not_duplicate_wildcard_or_loopback(bind_host, expected_listener, monkeypatch):
    calls = []

    def fake_bind(host, port):
        calls.append((host, port))
        return object()

    monkeypatch.setattr("vibe.ui_server._bind_ui_socket", fake_bind)

    assert len(_bind_ui_sockets(bind_host, 5123)) == 1
    assert calls == [(expected_listener, 5123)]


def test_bind_ui_sockets_binds_the_wildcard_spelling_for_real():
    """A stubbed bind cannot tell an address from a word, which is how this hid.

    ``getaddrinfo`` is what rejects ``*``, so only a real bind shows whether the
    resolved listener host is one the kernel accepts. Port 0 keeps it ephemeral
    and the socket is closed straight away -- it is never listened on.
    """

    sockets = _bind_ui_sockets("*", 0)
    try:
        assert len(sockets) == 1
    finally:
        for sock in sockets:
            sock.close()


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
    assert runtime._ui_health_urls("*", 5123) == (
        "http://127.0.0.1:5123/health",
        "http://127.0.0.1:5123/ready",
    )


def test_ui_server_readiness_reuses_wildcard_listener(monkeypatch):
    calls = []

    class Response:
        status = 200

        def __init__(self, payload=b""):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.payload

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        if url.endswith("/ready"):
            return Response(json.dumps(READY_EXTERNAL_CONTROLLER_BUNDLED_UI_FIXTURE).encode("utf-8"))
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime._ui_server_readiness("*", 5123) is True
    assert calls == [
        ("http://127.0.0.1:5123/health", 0.5),
        ("http://127.0.0.1:5123/ready", 0.5),
    ]


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


def test_ui_server_health_accepts_the_external_controller_bundled_ui_shape(monkeypatch):
    class Response:
        status = 200

        def __init__(self, payload=b""):
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
            return Response(json.dumps(READY_EXTERNAL_CONTROLLER_BUNDLED_UI_FIXTURE).encode("utf-8"))
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    assert runtime.ui_server_healthy("100.97.103.112", 5123) is True


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


def test_ready_identity_accepts_valid_desktop_runtime_id():
    class Response:
        status = 200

        def read(self):
            return json.dumps(
                {
                    "schema_version": 1,
                    "product": "avibe",
                    "ready": True,
                    "desktop_runtime_id": "a" * 64,
                }
            ).encode("utf-8")

    assert runtime._ui_ready_identity_state(Response()) is True


def test_ready_identity_accepts_valid_external_controller_ui_runtime_id():
    class Response:
        status = 200

        def read(self):
            return json.dumps(
                {
                    "schema_version": 1,
                    "product": "avibe",
                    "ready": True,
                    "desktop_ui_runtime_id": "b" * 64,
                }
            ).encode("utf-8")

    assert runtime._ui_ready_identity_state(Response()) is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": True,
            "desktop_ui_runtime_id": "short",
        },
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": True,
            "desktop_runtime_id": "a" * 64,
            "desktop_ui_runtime_id": "b" * 64,
        },
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": True,
            "desktop_ui_runtime_id": None,
        },
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": True,
            "desktop_ui_runtime_id": "A" * 64,
        },
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": True,
            "desktop_ui_runtime_id": "b" * 64,
            "unexpected": True,
        },
    ],
)
def test_ready_identity_rejects_invalid_or_ambiguous_runtime_ids(payload):
    class Response:
        status = 200

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


def test_ui_server_compatibility_rejects_a_valid_runtime_identity_invalid_response(monkeypatch):
    payload = json.dumps(
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": False,
            "code": "runtime_identity_invalid",
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

    assert runtime._ui_server_compatible("100.97.103.112", 5123) is False


def test_start_ui_replaces_a_ui_with_an_invalid_runtime_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    paths.get_runtime_ui_pid_path().write_text("12345", encoding="utf-8")
    payload = json.dumps(
        {
            "schema_version": 1,
            "product": "avibe",
            "ready": False,
            "code": "runtime_identity_invalid",
        }
    ).encode("utf-8")
    stopped = []
    spawned = []

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

    def fake_spawn(_args, pid_path, _stdout_name, _stderr_name, env=None):
        del env
        spawned.append(True)
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
    assert spawned == [True]


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


def test_start_ui_refuses_to_replace_a_stale_ui_that_will_not_stop(tmp_path, monkeypatch):
    # The stale process still owns the configured listener when the stop fails, so a
    # replacement could only die on bind. Starting one anyway would also repoint the
    # pid record at that dead replacement and leave nothing naming the process that
    # actually has to be stopped.
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".avibe")
    runtime.ensure_dirs()
    pid_path = paths.get_runtime_ui_pid_path()
    pid_path.write_text("12345", encoding="utf-8")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b""

    def fake_urlopen(url, timeout):
        del timeout
        if url.endswith("/ready"):
            raise OSError("old UI lacks ready contract")
        return Response()

    stop_attempts = []

    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(
        runtime,
        "get_process_command",
        lambda pid: "from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5123)"
        if pid == 12345
        else None,
    )
    monkeypatch.setattr(runtime, "stop_pid", lambda pid: stop_attempts.append(pid) or False)
    monkeypatch.setattr(
        runtime,
        "spawn_background",
        lambda *_args, **_kwargs: pytest.fail("a UI that would not stop must not be replaced"),
    )

    assert runtime.start_ui("127.0.0.1", 5123) is None
    assert stop_attempts == [12345]
    assert pid_path.read_text(encoding="utf-8") == "12345"


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


def _ready_response(
    monkeypatch,
    owners,
    *,
    controller_ready,
    controller_runtime_id=None,
    ui_runtime_id=None,
):
    if ui_runtime_id is None:
        monkeypatch.delenv("AVIBE_DESKTOP_RUNTIME_ID", raising=False)
    else:
        monkeypatch.setenv("AVIBE_DESKTOP_RUNTIME_ID", ui_runtime_id)
    owner_iter = iter(owners)

    def resolve_owner(*, include_starting):
        return next(owner_iter)

    async def health_identity():
        if not controller_ready:
            return None
        identity = {"ok": True, "service": "vibe-remote-internal", "version": 1}
        if controller_runtime_id is not None:
            identity["desktop_runtime_id"] = controller_runtime_id
        return identity

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr("vibe.internal_client.health_identity", health_identity)
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


def test_ready_reports_desktop_runtime_identity(monkeypatch):
    response = _ready_response(
        monkeypatch,
        [1234, 1234],
        controller_ready=True,
        controller_runtime_id="a" * 64,
        ui_runtime_id="a" * 64,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": True,
        "desktop_runtime_id": "a" * 64,
    }


@pytest.mark.parametrize(
    ("controller_runtime_id", "ui_runtime_id"),
    [
        ("a" * 64, "b" * 64),
        ("a" * 64, None),
    ],
)
def test_ready_rejects_a_runtime_identity_mismatch(
    monkeypatch,
    controller_runtime_id,
    ui_runtime_id,
):
    response = _ready_response(
        monkeypatch,
        [1234, 1234],
        controller_ready=True,
        controller_runtime_id=controller_runtime_id,
        ui_runtime_id=ui_runtime_id,
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "runtime_identity_mismatch",
        **(
            {"desktop_runtime_id": controller_runtime_id}
            if controller_runtime_id is not None
            else {}
        ),
    }


def test_ready_adopts_a_healthy_untagged_controller_even_when_ui_inherits_bundle_identity(monkeypatch):
    response = _ready_response(
        monkeypatch,
        [1234, 1234],
        controller_ready=True,
        controller_runtime_id=None,
        ui_runtime_id="a" * 64,
    )

    assert response.status_code == 200
    assert response.get_json() == READY_EXTERNAL_CONTROLLER_BUNDLED_UI_FIXTURE


def test_ready_rejects_an_invalid_bundled_ui_identity(monkeypatch):
    response = _ready_response(
        monkeypatch,
        [1234, 1234],
        controller_ready=True,
        controller_runtime_id=None,
        ui_runtime_id="invalid",
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "schema_version": 1,
        "product": "avibe",
        "ready": False,
        "code": "runtime_identity_invalid",
    }


def test_cmd_start_reused_controller_starts_missing_ui_and_emits_untagged_ready(
    monkeypatch,
    capsys,
    request,
):
    """The reused-service path must start only the missing UI and become adoptable."""

    config = SimpleNamespace(
        has_configured_platform_credentials=lambda: True,
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
        language="en",
    )
    spawned = []

    monkeypatch.setenv("AVIBE_DESKTOP_RUNTIME_ID", "a" * 64)
    # The reused Controller is this Runtime's: a live process carrying its id,
    # which the start's claim reads before it reuses anything. Popen can return
    # while the child is still inside execve, before the kernel has published its
    # environment, so the test waits for the line it prints once it runs.
    controller = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE,
        text=True,
    )

    def stop_controller():
        controller.kill()
        controller.wait(timeout=10)
        controller.stdout.close()

    request.addfinalizer(stop_controller)
    assert controller.stdout.readline() == "ready\n"
    monkeypatch.setattr(cli, "_guard_cli_default_state_migration", lambda: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda: config)
    monkeypatch.setattr(cli, "_write_status", lambda *args: None)
    monkeypatch.setattr(cli, "_in_ssh_session", lambda: False)
    monkeypatch.setattr(cli.runtime, "effective_ui_bind_host", lambda _config: "127.0.0.1")
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda **_kwargs: controller.pid)
    monkeypatch.setattr(cli.runtime, "wait_for_service_ready", lambda pid, timeout: pid)
    monkeypatch.setattr(cli.runtime, "write_status", lambda *args: None)
    monkeypatch.setattr(cli, "_open_browser", lambda _url: pytest.fail("browser is disabled"))

    def reused_service(**kwargs):
        kwargs["start_info"].capture(controller.pid, reused=True)
        return controller.pid

    monkeypatch.setattr(cli.runtime, "start_service", reused_service)
    monkeypatch.setattr(
        internal_client,
        "health_identity_sync",
        lambda: {"ok": True, "service": "vibe-remote-internal", "version": 1},
    )

    async def health_identity():
        return {"ok": True, "service": "vibe-remote-internal", "version": 1}

    monkeypatch.setattr("vibe.internal_client.health_identity", health_identity)
    def spawn_ui(args, pid_path, *logs, **kwargs):
        # The spawn primitive records and captures the child before returning it.
        spawned.append((args, logs, kwargs))
        pid_path.write_text("5678", encoding="utf-8")
        kwargs["start_info"].capture(5678, reused=False)
        return 5678

    monkeypatch.setattr(cli.runtime, "spawn_background", spawn_ui)

    def wait_for_ui(host, port):
        response = app.test_client().get("/ready", base_url=f"http://{host}:{port}")
        assert response.status_code == 200
        assert response.get_json() == READY_EXTERNAL_CONTROLLER_BUNDLED_UI_FIXTURE
        return True

    monkeypatch.setattr(cli.runtime, "wait_for_ui_server", wait_for_ui)
    monkeypatch.setattr(cli.runtime, "stop_service", lambda **kwargs: pytest.fail("reused Controller must survive"))
    monkeypatch.setattr(
        cli.runtime,
        "stop_ui",
        lambda **_kwargs: pytest.fail("the missing UI path must not stop a surviving UI"),
    )

    assert cli.cmd_start(open_browser=False) == 0

    assert "  http://127.0.0.1:5123" in capsys.readouterr().out.splitlines()
    assert len(spawned) == 1


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

    async def health_identity():
        if not controller_ready:
            return None
        return {"ok": True, "service": "vibe-remote-internal", "version": 1}

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr("vibe.internal_client.health_identity", health_identity)

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

    async def health_identity():
        return {"ok": True, "service": "vibe-remote-internal", "version": 1}

    async def track_to_thread(func, *args, **kwargs):
        event_loop_thread = threading.get_ident()
        result = await original_to_thread(func, *args, **kwargs)
        offloaded_calls.append((func, kwargs, event_loop_thread))
        return result

    monkeypatch.setattr(runtime, "resolve_service_owner_pid", resolve_owner)
    monkeypatch.setattr("vibe.internal_client.health_identity", health_identity)
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

    async def fail_health_identity():
        pytest.fail("blocked Host must not reach the readiness route")

    monkeypatch.setattr("vibe.internal_client.health_identity", fail_health_identity)

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

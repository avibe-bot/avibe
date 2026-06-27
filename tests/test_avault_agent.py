from __future__ import annotations

import json
import stat
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from vibe.avault_agent import AvaultAgentClient, AvaultAgentError, AvaultAgentManager
from vibe.avault_agent import _ensure_agent_socket_parent, _remove_stale_agent_socket, default_agent_socket_path


class FakeAgentServer:
    def __init__(self, socket_path: Path, responses: list[dict[str, Any]]) -> None:
        self.socket_path = socket_path
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread.start()
        assert self._ready.wait(2)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._thread.join(2)

    def _serve(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.socket_path))
            listener.listen(8)
            self._ready.set()
            for response in self.responses:
                conn, _ = listener.accept()
                with conn:
                    self.requests.append(_read_frame(conn))
                    _write_frame(conn, response)


def _read_frame(conn: socket.socket) -> dict[str, Any]:
    length = int.from_bytes(conn.recv(4), "big")
    body = conn.recv(length)
    return json.loads(body.decode("utf-8"))


def _write_frame(conn: socket.socket, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    conn.sendall(len(body).to_bytes(4, "big"))
    conn.sendall(body)


def test_agent_client_round_trips_pubkey_grant_deliver_and_release(tmp_path):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-agent-", dir="/tmp")) / "s"
    responses = [
        {"ok": True, "result": {"public_key": "pk", "fingerprint": "fp"}},
        {"ok": True, "result": {"granted": 1, "ttl_secs": 300}},
        {"ok": True, "result": {"ok": True}},
        {"ok": True, "result": {"released": True}},
    ]
    with FakeAgentServer(socket_path, responses) as server:
        client = AvaultAgentClient(socket_path)

        assert client.pubkey() == {"public_key": "pk", "fingerprint": "fp"}
        assert client.grant(
            scope_type="secret",
            scope_ref="API_TOKEN",
            ttl_secs=300,
            deks=[
                {
                    "name": "API_TOKEN",
                    "dek_blindbox": {"scheme": "hpke-x25519-hkdfsha256-aes256gcm-v1", "enc": "enc", "ct": "ct"},
                    "approval": {"nonce": "bm9uY2UtMTIzNDU2", "expires_at_unix": 4102444800},
                }
            ],
        ) == {"granted": 1, "ttl_secs": 300}
        assert client.deliver_inject(
            scope_type="secret",
            scope_ref="API_TOKEN",
            path=str(tmp_path / "out.env"),
            fmt="dotenv",
            secrets=[
                {
                    "name": "API_TOKEN",
                    "key": "API_TOKEN",
                    "envelope": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
                }
            ],
        ) == {"ok": True}
        assert client.release(scope_type="secret", scope_ref="API_TOKEN") == {"released": True}

    assert [request["type"] for request in server.requests] == ["pubkey", "grant", "deliver", "release"]
    assert server.requests[1]["ttl_secs"] == 300
    assert "value" not in json.dumps(server.requests)


def test_agent_client_surfaces_agent_errors(tmp_path):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-agent-", dir="/tmp")) / "s"
    with FakeAgentServer(socket_path, [{"ok": False, "error": "grant is missing or expired"}]):
        client = AvaultAgentClient(socket_path)

        with pytest.raises(AvaultAgentError, match="grant is missing or expired"):
            client.deliver_run(
                scope_type="secret",
                scope_ref="API_TOKEN",
                command=["/bin/true"],
                secrets=[
                    {
                        "name": "API_TOKEN",
                        "env": "API_TOKEN",
                        "envelope": {"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
                    }
                ],
            )


def test_agent_socket_path_uses_avibe_home_and_secures_directories(tmp_path, monkeypatch):
    avibe_home = tmp_path / "avibe-home"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))

    socket_path = default_agent_socket_path()
    assert socket_path == avibe_home / "run" / "avault.sock"

    _ensure_agent_socket_parent(socket_path.parent)

    assert stat.S_IMODE(avibe_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700


def test_agent_manager_captures_only_per_request_output_in_memory(tmp_path, monkeypatch):
    manager = AvaultAgentManager(socket_path=tmp_path / "avault.sock")
    seen_timeout: list[float | None] = []
    monkeypatch.setattr(manager, "_ensure_owned_agent_running_locked", lambda: None)
    with manager._output_lock:
        manager._stdout.extend(b"old-out\n")
        manager._stderr.extend(b"old-err\n")

    def _request(client):
        seen_timeout.append(client.timeout)
        with manager._output_lock:
            manager._stdout.extend(b"new-out\n")
            manager._stderr.extend(b"new-err\n")
        return {"exit_code": 0}

    result, output = manager.request_with_output(_request)

    assert result == {"exit_code": 0}
    assert output == {
        "stdout": b"new-out\n",
        "stderr": b"new-err\n",
    }
    assert seen_timeout == [None]
    assert not (tmp_path / "stdout.log").exists()
    assert not (tmp_path / "stderr.log").exists()


def test_agent_manager_replaces_foreign_socket_for_output_capture(tmp_path, monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-owned-", dir="/tmp")) / "s"
    spawned = []

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        listener.listen(1)
        manager = AvaultAgentManager(socket_path=socket_path)

        def _spawn_locked():
            spawned.append(True)
            manager._owned_socket_identity = None

            class FakeProcess:
                def poll(self):
                    return None

            manager._process = FakeProcess()

        monkeypatch.setattr(manager, "_spawn_locked", _spawn_locked)
        monkeypatch.setattr(manager, "_wait_for_socket_locked", lambda: None)

        result, output = manager.request_with_output(lambda _client: {"exit_code": 0})

        assert result == {"exit_code": 0}
        assert output == {"stdout": b"", "stderr": b""}
        assert spawned == [True]
        assert not socket_path.exists()
    finally:
        listener.close()


def test_remove_stale_agent_socket_unlinks_dead_socket(tmp_path):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-stale-", dir="/tmp")) / "s"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
    finally:
        listener.close()

    assert socket_path.exists()
    _remove_stale_agent_socket(socket_path)
    assert not socket_path.exists()

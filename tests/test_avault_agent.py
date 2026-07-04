from __future__ import annotations

import json
import signal
import stat
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vibe.avault_agent import DEFAULT_AGENT_IDLE_TIMEOUT_SECS, AvaultAgentClient, AvaultAgentError, AvaultAgentManager
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
            grant_id="vgr_api",
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
            grant_id="vgr_api",
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
        assert client.release(grant_id="vgr_api") == {"released": True}

    assert [request["type"] for request in server.requests] == ["pubkey", "grant", "deliver.inject", "release"]
    assert server.requests[1]["purpose"] == "deliver"
    assert server.requests[1]["ttl_secs"] == 300
    assert "value" not in json.dumps(server.requests)


def test_agent_client_surfaces_agent_errors(tmp_path):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-agent-", dir="/tmp")) / "s"
    with FakeAgentServer(socket_path, [{"ok": False, "error": "grant is missing or expired"}]):
        client = AvaultAgentClient(socket_path)

        with pytest.raises(AvaultAgentError, match="grant is missing or expired"):
            client.deliver_run(
                grant_id="vgr_api",
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


def test_agent_default_idle_timeout_covers_longest_grant_ttl():
    from storage.vault_service import GRANT_TTL_OPTIONS_SECONDS

    assert DEFAULT_AGENT_IDLE_TIMEOUT_SECS >= max(GRANT_TTL_OPTIONS_SECONDS)


def test_agent_manager_preserves_live_socket_with_cached_grants(tmp_path, monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-live-", dir="/tmp")) / "s"
    with FakeAgentServer(socket_path, [{"ok": True, "result": {"public_key": "pk", "fingerprint": "fp"}}]) as server:
        manager = AvaultAgentManager(socket_path=socket_path)
        monkeypatch.setattr(manager, "_spawn_locked", lambda: pytest.fail("live agent socket should be reused"))

        manager.ensure_running()

    assert socket_path.exists()
    assert [request["type"] for request in server.requests] == ["pubkey"]


def test_agent_manager_does_not_kill_owned_agent_on_slow_liveness_probe(tmp_path, monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-slow-", dir="/tmp")) / "s"
    socket_path.touch()
    manager = AvaultAgentManager(socket_path=socket_path)
    manager._process = MockProcess()
    monkeypatch.setattr(manager, "_socket_responds", lambda: False)
    monkeypatch.setattr(manager, "_terminate_process_locked", lambda: pytest.fail("live agent should be preserved"))
    monkeypatch.setattr(manager, "_spawn_locked", lambda: pytest.fail("live agent should not be replaced"))

    manager.ensure_running()

    assert manager._process is not None


def test_agent_manager_recreates_owned_agent_when_socket_disappears(tmp_path, monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-missing-", dir="/tmp")) / "s"
    manager = AvaultAgentManager(socket_path=socket_path)
    old_process = MockProcess()
    replacement = MockProcess()
    manager._process = old_process
    spawned: list[bool] = []
    waited: list[bool] = []
    monkeypatch.setattr(manager, "_socket_responds", lambda: False)

    def _spawn() -> None:
        spawned.append(True)
        manager._process = replacement

    monkeypatch.setattr(manager, "_spawn_locked", _spawn)
    monkeypatch.setattr(manager, "_wait_for_socket_locked", lambda: waited.append(True))

    manager.ensure_running()

    assert old_process.terminated is True
    assert old_process.killed is False
    assert manager._process is replacement
    assert spawned == [True]
    assert waited == [True]


def test_agent_manager_reset_signals_process_group(tmp_path, monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-reset-", dir="/tmp")) / "s"
    manager = AvaultAgentManager(socket_path=socket_path)
    proc = MockProcess()
    manager._process = proc
    signals: list[int] = []

    def _signal_process_tree(process, sig, logger, label):
        assert process is proc
        assert label == "avault agent"
        signals.append(sig)

    monkeypatch.setattr("vibe.avault_agent.signal_process_tree", _signal_process_tree)

    manager.reset()

    assert signals == [signal.SIGTERM]
    assert proc.terminated is False
    assert proc.killed is False
    assert manager._process is None


def test_agent_manager_reset_kills_process_group_after_timeout(tmp_path, monkeypatch):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-reset-kill-", dir="/tmp")) / "s"
    manager = AvaultAgentManager(socket_path=socket_path)
    proc = MockProcess(wait_timeout=True)
    manager._process = proc
    signals: list[int] = []
    monkeypatch.setattr("vibe.avault_agent.signal_process_tree", lambda process, sig, logger, label: signals.append(sig))

    manager.reset()

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert proc.terminated is False
    assert proc.killed is False
    assert manager._process is None


def test_agent_client_does_not_retry_deliver_after_frame_write_timeout(tmp_path):
    socket_path = Path(tempfile.mkdtemp(prefix="avault-timeout-", dir="/tmp")) / "s"
    ready = threading.Event()
    requests: list[dict[str, Any]] = []

    def _serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            listener.listen(8)
            ready.set()
            conn, _ = listener.accept()
            with conn:
                requests.append(_read_frame(conn))
                time.sleep(0.2)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    ensure_calls = 0

    def _ensure() -> None:
        nonlocal ensure_calls
        ensure_calls += 1

    client = AvaultAgentClient(socket_path, timeout=0.05, ensure_agent=_ensure)
    with pytest.raises(AvaultAgentError, match="request failed"):
        client.deliver_fetch(
            grant_id="vgr_api",
            name="API_TOKEN",
            envelope={"ciphertext": "ct", "nonce": "n", "wrap_meta": "wm"},
            request={"headers": []},
        )

    thread.join(1)
    assert len(requests) == 1
    assert requests[0]["type"] == "deliver.fetch"
    assert ensure_calls == 0


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


class MockProcess:
    def __init__(self, *, wait_timeout: bool = False):
        self.terminated = False
        self.killed = False
        self.wait_timeout = wait_timeout
        self.wait_calls = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_timeout and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("mock", timeout)
        return None

    def kill(self):
        self.killed = True

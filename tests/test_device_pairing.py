"""Real filesystem/process tests of device pairing transactions and scope epochs."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading

import aiohttp
import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="The device plugin is a POSIX process")
PACKAGE = Path(__file__).parents[1] / "packaging" / "avibe-device"


@pytest.fixture(autouse=True)
def plugin(monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))


def child_env():
    return {**os.environ, "PYTHONPATH": str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", "")}


def pair_args(root, artifact):
    return ["--state-dir", str(root), "pair", "--device-id", "board", "--name", "My Vibey",
            "--websocket-url", "ws://127.0.0.1:8765/device/ws", "--provision-file", str(artifact)]


@pytest.mark.parametrize("first", ["enable", "revoke"])
def test_enable_and_revoke_serialize_across_processes(tmp_path, first):
    from avibe_device.pairing import Pairing

    pairing = Pairing(tmp_path / "state")
    token = pairing.pair("board")
    # Pause after the child read/check, immediately before publishing its mutation.
    code = '''
import sys
from pathlib import Path
from avibe_device.pairing import Pairing
p = Pairing(Path(sys.argv[1]))
write = p._write
def blocked_write(row):
    print("locked", flush=True)
    sys.stdin.readline()
    write(row)
p._write = blocked_write
p.enable(True) if sys.argv[2] == "enable" else p.revoke()
'''
    child = subprocess.Popen([sys.executable, "-c", code, str(pairing.root), first], env=child_env(),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 5)[0], "child did not reach publication boundary"
        assert child.stdout.readline().strip() == "locked"
        entered = threading.Event()

        def other_mutation():
            entered.set()
            try:
                pairing.revoke() if first == "enable" else pairing.enable(True)
                return "ok"
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor() as pool:
            future = pool.submit(other_mutation)
            assert entered.wait(2)
            try:
                # A different process owns the read/check/write lock. Neither a
                # mutator nor the auth reader may observe its intermediate state.
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
            finally:
                child.stdin.write("release\n")
                child.stdin.flush()
            assert future.result(timeout=5) == ("ok" if first == "enable" else "pair_device_first")
        assert child.wait(timeout=5) == 0
        assert not pairing.accepts("board", "Bearer " + token)
        assert not pairing.status()["paired"]
        with pytest.raises(ValueError, match="pair_device_first"):
            pairing.enable(True)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize("failure", [
    "write", "flush", "fsync", "link", "artifact_directory", "state", "committed", "persistent_state",
])
def test_pair_failure_is_recoverable_without_manual_revoke(tmp_path, monkeypatch, capsys, failure):
    from avibe_device import pairing as module
    from avibe_device.cli import main
    from avibe_device.pairing import Pairing

    root, artifact = tmp_path / "state", tmp_path / "provision.json"
    args = pair_args(root, artifact)
    fdopen, fsync, link = os.fdopen, os.fsync, os.link
    write, sync_directory = Pairing._write, Pairing._sync_directory
    fault_used = False

    def fail_once():
        nonlocal fault_used
        if not fault_used:
            fault_used = True
            raise OSError(28, "synthetic ENOSPC")

    def is_stage(fd):
        return any(path.stat().st_ino == os.fstat(fd).st_ino for path in tmp_path.glob(".avibe-provision-*"))

    class Output(io.TextIOWrapper):
        def write(self, value):
            if failure == "write":
                fail_once()
            return super().write(value)

        def flush(self):
            if failure == "flush":
                fail_once()
            return super().flush()

    def fault_fdopen(fd, mode):
        if is_stage(fd) and failure in {"write", "flush"}:
            return Output(fdopen(fd, "wb", buffering=0), encoding="utf-8")
        return fdopen(fd, mode)

    def fault_fsync(fd):
        if failure == "fsync" and is_stage(fd):
            fail_once()
        return fsync(fd)

    def fault_link(src, dst):
        if failure == "link":
            fail_once()
        return link(src, dst)

    def fault_directory(path):
        if failure == "artifact_directory" and path == tmp_path and artifact.exists():
            fail_once()
        return sync_directory(path)

    def fault_state(self, row):
        nonlocal fault_used
        if failure == "persistent_state" and not row.get("pending_provision"):
            fault_used = True
            raise OSError(28, "synthetic persistent ENOSPC")
        if failure == "state" and row.get("device_id"):
            fail_once()
        write(self, row)
        if failure == "committed" and row.get("device_id"):
            fail_once()

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fdopen", fault_fdopen)
        patch.setattr(module.os, "fsync", fault_fsync)
        patch.setattr(module.os, "link", fault_link)
        patch.setattr(Pairing, "_write", fault_state)
        patch.setattr(Pairing, "_sync_directory", staticmethod(fault_directory))
        assert main(args) == 1
    assert fault_used
    pairing = Pairing(root)
    assert pairing.status()["paired"] == (failure == "committed")
    assert artifact.exists() == (failure == "committed")
    assert main(args) == 0
    token = json.loads(artifact.read_text())["websocket"]["token"]
    pairing.enable(True)
    assert pairing.accepts("board", "Bearer " + token)
    assert token not in capsys.readouterr().out
    assert not list(tmp_path.glob(".avibe-provision-*"))
    assert "pending_provision" not in pairing.read() and "provision_stage" not in pairing.read()


@pytest.mark.parametrize("boundary", ["intent", "staged", "published", "committed"])
def test_pair_process_death_recovers_at_each_publication_boundary(tmp_path, capsys, boundary):
    from avibe_device.cli import main
    from avibe_device.pairing import Pairing

    root, artifact = tmp_path / "state", tmp_path / "provision.json"
    code = '''
import os, sys
from pathlib import Path
from avibe_device.cli import main
from avibe_device.pairing import Pairing
boundary = sys.argv[1]
write, link = Pairing._write, os.link
def crash_write(self, row):
    write(self, row)
    if (boundary == "intent" and row.get("pending_provision")) or (boundary == "committed" and row.get("device_id")):
        os._exit(73)
def crash_link(src, dst):
    if boundary == "staged":
        os._exit(73)
    link(src, dst)
    if boundary == "published":
        os._exit(73)
Pairing._write, os.link = crash_write, crash_link
main(sys.argv[2:])
'''
    args = pair_args(root, artifact)
    result = subprocess.run([sys.executable, "-c", code, boundary, *args], env=child_env(),
                            capture_output=True, timeout=10)
    assert result.returncode == 73
    # Inspect raw disk before recovery: an artifact without committed state has
    # no usable credential. A committed state always has its complete artifact.
    raw = json.loads((root / "device.json").read_text())
    assert bool(raw.get("device_id")) == (boundary == "committed")
    if raw.get("device_id"):
        assert json.loads(artifact.read_text())["websocket"]["version"] == 2
    pairing = Pairing(root)
    assert pairing.status()["paired"] == (boundary == "committed")
    assert artifact.exists() == (boundary == "committed")
    assert main(args) == 0
    token = json.loads(artifact.read_text())["websocket"]["token"]
    pairing.enable(True)
    assert pairing.accepts("board", "Bearer " + token)
    pairing.revoke()
    with pytest.raises(ValueError, match="pair_device_first"):
        pairing.enable(True)
    assert not pairing.accepts("board", "Bearer " + token)
    assert not list(tmp_path.glob(".avibe-provision-*"))
    assert token not in capsys.readouterr().out


def test_pair_never_removes_preexisting_artifact_or_existing_pair(tmp_path, capsys):
    from avibe_device.cli import main
    from avibe_device.pairing import Pairing

    root, artifact = tmp_path / "state", tmp_path / "provision.json"
    artifact.write_text("existing user file")
    assert main(pair_args(root, artifact)) == 1
    assert artifact.read_text() == "existing user file"
    assert not Pairing(root).status()["paired"]
    token = Pairing(root).pair("existing")
    Pairing(root).enable(True)
    assert main(pair_args(root, tmp_path / "new.json")) == 1
    assert Pairing(root).accepts("existing", "Bearer " + token)
    assert not (tmp_path / "new.json").exists()


@pytest.mark.asyncio
async def test_old_listener_epoch_refuses_both_discovery_paths_and_new_sockets(tmp_path):
    pytest.importorskip("audioop")
    from aiohttp import web
    from avibe_device.pairing import Pairing
    from avibe_device.provision import ProvisionWindow
    from avibe_device.server import create_app

    pairing = Pairing(tmp_path / "state")
    artifact = tmp_path / "provision.json"
    token = pairing.pair("board", provision_file=artifact, websocket_url="ws://127.0.0.1:8765/device/ws")
    pairing.enable(True)
    window = ProvisionWindow(pairing, artifact, bind="127.0.0.1", port=8765)
    runner = web.AppRunner(create_app(pairing, object(), provision=window), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        pairing.enable(False)
        pairing.enable(True)
        # Keep this old app alive deliberately: epoch checks must reject new
        # requests before the serve loop's next shutdown poll.
        async with aiohttp.ClientSession() as client:
            for headers in ({"Device-Id": "board"},
                            {"Device-Id": "board", "Authorization": "Bearer " + token}):
                async with client.post(url + "/device/ota", headers=headers) as response:
                    assert response.status == 403
                    assert token not in await response.text()
            with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                await client.ws_connect(url + "/device/ws", headers={
                    "Device-Id": "board", "Authorization": "Bearer " + token,
                })
            assert error.value.status == 401
        assert window.document is None
        assert pairing.accepts("board", "Bearer " + token)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_command", ["stop", "disable"])
async def test_stop_enable_cannot_revive_paused_server_socket_or_provision_window(tmp_path, stop_command):
    pytest.importorskip("opuslib_next")
    from avibe_device.cli import main
    from avibe_device.pairing import Pairing
    from avibe_device.server import AUDIO

    root, artifact = tmp_path / "state", tmp_path / "provision.json"
    pairing = Pairing(root)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    args = pair_args(root, artifact)
    args[args.index("--websocket-url") + 1] = f"ws://127.0.0.1:{port}/device/ws"
    assert main(args) == 0
    token = json.loads(artifact.read_text())["websocket"]["token"]
    pairing.enable(True)
    child = subprocess.Popen([sys.executable, "-m", "avibe_device.cli", "--state-dir", str(root),
                              "serve", "--port", str(port), "--provision-file", str(artifact)],
                             env=child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        async with aiohttp.ClientSession() as client:
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    async with client.post(url + "/device/ota", headers={"Device-Id": "board"}) as response:
                        assert response.status == 200
                    break
                except aiohttp.ClientConnectionError:
                    assert child.poll() is None
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("isolated listener did not start")
            async with client.ws_connect(url + "/device/ws", headers={
                "Device-Id": "board", "Authorization": "Bearer " + token,
            }) as ws:
                await ws.send_json({"type": "hello", "version": 2, "audio_params": AUDIO,
                                    "features": {"avibe_voice": 1}})
                assert (await ws.receive_json(timeout=2))["type"] == "hello"
                child.send_signal(signal.SIGSTOP)
                # Guarantee the old scope cannot poll between stop and enable.
                try:
                    assert main(["--state-dir", str(root), stop_command]) == 0
                    assert main(["--state-dir", str(root), "enable"]) == 0
                    assert pairing.status()["enabled"]
                finally:
                    child.send_signal(signal.SIGCONT)
                assert (await ws.receive(timeout=5)).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
            assert await asyncio.to_thread(child.wait, 5) == 0
            assert not pairing.running()
            with pytest.raises(aiohttp.ClientConnectionError):
                await client.post(url + "/device/ota", headers={"Device-Id": "board"})
            assert pairing.status()["paired"] and pairing.status()["enabled"]
            assert pairing.accepts("board", "Bearer " + token)  # Only a new explicit serve can use it.
    finally:
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.wait, 5)
        child.stderr.close()

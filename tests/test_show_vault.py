"""Show's server-only adapter: private discovery lifecycle and one submission.

The existing control IPC tests own Windows DACL/descriptor verification. Here
we exercise the Python snapshot owner and the actual shipped Node client.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import httpx
import pytest

from core import control_ipc
from core.caller_context import CALLER_CONTEXT_ENV_NAMES
from core.control_requests import result
from core.show_runtime import ShowRuntimeContext, ShowRuntimeManager, ShowRuntimeProtocolEnvelope
from core.show_vault import ShowVaultSnapshot
from vibe import internal_client

NODE = shutil.which("node")
MODULE = (Path(__file__).resolve().parents[1] / "vibe/show_server/vault.mjs").as_uri()


@pytest.mark.asyncio
@pytest.mark.skipif(not NODE, reason="Node is required for the shipped server adapter")
async def test_node_snapshot_recovers_same_process_and_never_replays(tmp_path, monkeypatch):
    """Absent -> A -> B works; malformed/stale/disconnected replies send once."""
    snapshot = ShowVaultSnapshot(tmp_path)
    requests = []
    state = {"generation": "a" * 32, "mode": "normal", "status": 200}

    async def receive(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(":", 1) for line in head.decode().split("\r\n")[1:] if ":" in line)
            raw = await reader.readexactly(int(headers["Content-Length"]))
            request = json.loads(raw)
            requests.append((request, headers))
            if state["mode"] == "drop":
                return
            payload = result({"ok": 200 <= state["status"] <= 299, "kind": "vault_fetch", "status": state["status"],
                              "headers": {}, "body": "upstream response"})
            data = b"{broken" if state["mode"] == "malformed" else json.dumps(payload).encode()
            generation = "f" * 32 if state["mode"] == "stale" else state["generation"]
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\nX-Avibe-Control-Instance: {generation}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    def endpoint():
        return control_ipc.ControlIpcClientEndpoint(transport="tcp", descriptor=control_ipc.ControlIpcDescriptor(
            schema_version=1, transport="tcp", host="127.0.0.1", port=port,
            instance_id=state["generation"], bearer_token="synthetic-token-" + state["generation"],
        ))

    monkeypatch.setattr(internal_client, "resolve_control_endpoint", endpoint)
    caller_env = {key: f"raw-{key}" for key in CALLER_CONTEXT_ENV_NAMES}
    caller_env["NOT_AN_INVOCATION_FIELD"] = "do-not-forward"
    script = f'''
        import {{ vaultFetch }} from {json.dumps(MODULE)};
        import fs from 'node:fs/promises';
        import {{ syncBuiltinESMExports }} from 'node:module';
        import {{ createInterface }} from 'node:readline';
        const originalLstat = fs.lstat;
        let replaceDuringDiscovery = false;
        fs.lstat = async (path, ...args) => {{
          const info = await originalLstat(path, ...args);
          if (replaceDuringDiscovery && path === process.env.AVIBE_SHOW_CONTROL_SNAPSHOT) {{
            replaceDuringDiscovery = false;
            await fs.copyFile(path, path + '.replacement');
            await fs.rename(path + '.replacement', path);
          }}
          return info;
        }};
        syncBuiltinESMExports();
        for await (const line of createInterface({{input:process.stdin}})) {{
          const {{replaceSnapshot, ...options}} = JSON.parse(line);
          replaceDuringDiscovery = !!replaceSnapshot;
          const value = await vaultFetch(options, {{callerEnv:{json.dumps(caller_env)}}});
          console.log(JSON.stringify(value));
        }}
    '''
    env = {key: os.environ[key] for key in ("PATH", "HOME", "AVIBE_HOME") if key in os.environ}
    env.update(snapshot.environment())
    child = await asyncio.create_subprocess_exec(
        NODE, "--input-type=module", "-e", script, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def fetch(*, replace_snapshot=False):
        child.stdin.write(json.dumps({"auth": "SYNTHETIC", "url": "https://api.example.com", "noApprovalWait": True,
                                     "replaceSnapshot": replace_snapshot}).encode() + b"\n")
        await child.stdin.drain()
        return json.loads(await asyncio.wait_for(child.stdout.readline(), 10))

    try:
        unavailable = await fetch()
        assert unavailable["execution"] == "not_executed"
        assert not requests
        await asyncio.to_thread(snapshot.refresh)
        assert (await fetch())["payload"]["body"] == "upstream response"
        state["generation"] = "b" * 32
        state["status"] = 403
        await asyncio.to_thread(snapshot.refresh)
        denied = await fetch()
        assert denied["exit_code"] == 1
        assert denied["payload"]["status"] == 403
        assert denied["payload"]["body"] == "upstream response"
        assert len(requests) == 2
        assert requests[0][1]["Authorization"].strip() == "Bearer synthetic-token-" + "a" * 32
        assert requests[1][1]["Authorization"].strip() == "Bearer synthetic-token-" + "b" * 32
        assert requests[1][0]["caller_env"] == {key: caller_env[key] for key in CALLER_CONTEXT_ENV_NAMES}
        assert requests[1][0]["arguments"]["caller_pid"] == child.pid
        # Another API request may atomically publish an equally valid snapshot
        # between the reader's lstat and open. Discovery must tolerate that race.
        before = len(requests)
        raced = await fetch(replace_snapshot=True)
        assert raced["payload"]["kind"] == "vault_fetch"
        assert len(requests) == before + 1
        for mode in ("malformed", "stale", "drop"):
            state["mode"] = mode
            before = len(requests)
            ambiguous = await fetch()
            assert ambiguous["execution"] == "execution_unknown"
            assert len(requests) == before + 1
            assert "synthetic-token" not in json.dumps(ambiguous)

        def unavailable_endpoint():
            raise internal_client.InternalServerUnavailable("synthetic-private-diagnostic")
        monkeypatch.setattr(internal_client, "resolve_control_endpoint", unavailable_endpoint)
        await asyncio.to_thread(snapshot.refresh)
        before = len(requests)
        assert (await fetch())["execution"] == "not_executed"
        assert len(requests) == before
    finally:
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 10)
        assert await child.stderr.read() == b""
        snapshot.close()
        server.close()
        await server.wait_closed()


def test_retired_snapshot_cannot_overwrite_successor_or_recreate_directory(tmp_path, monkeypatch):
    """Stop during discovery must fence the late worker, not just unlink its file."""
    old = ShowVaultSnapshot(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def delayed():
        entered.set()
        assert release.wait(5)
        raise internal_client.InternalServerUnavailable("test")

    monkeypatch.setattr(internal_client, "resolve_control_endpoint", delayed)

    def refresh():
        try:
            old.refresh()
        except OSError as exc:
            errors.append(exc)

    thread = threading.Thread(target=refresh)
    thread.start()
    try:
        assert entered.wait(5)
        old.close()  # must not wait for the resolver thread
        successor = ShowVaultSnapshot(tmp_path)
        saved = successor.path.read_bytes()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors
    assert not old.directory.exists()
    assert successor.path.read_bytes() == saved
    with pytest.raises(OSError):
        old.refresh()
    assert not old.directory.exists()
    successor.close()


@pytest.mark.asyncio
async def test_warm_show_api_refresh_failure_refuses_without_stale_transport_or_restart(tmp_path, monkeypatch):
    manager = ShowRuntimeManager(runtime_dir=tmp_path, auto_install=False, offline=True)
    snapshot = ShowVaultSnapshot(tmp_path)
    manager._vault_snapshot = snapshot
    manager._base_url = "http://127.0.0.1:12345"
    manager._process = SimpleNamespace(poll=lambda: 0)
    calls = []

    async def negotiate(*_):
        pass

    async def proxy(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(200, text="served")

    monkeypatch.setattr(manager, "_negotiate_context_key_capability", negotiate)
    monkeypatch.setattr(manager, "_request_runtime_transport", proxy)
    def publication_failed():
        raise OSError("private-snapshot-diagnostic")
    monkeypatch.setattr(snapshot, "refresh", publication_failed)
    envelope = ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE)
    try:
        failed = await manager.request("POST", "/sessions/ses1/app/api/data?view=1", envelope=envelope)
        assert failed.status_code == 503
        assert failed.json() == {"error": "show_server_unavailable"}
        assert calls == []
        assert manager._base_url == "http://127.0.0.1:12345"
        static = await manager.request("GET", "/sessions/ses1/app/main.tsx", envelope=envelope)
        assert static.status_code == 200
        assert len(calls) == 1
    finally:
        manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation_fails", [False, True])
async def test_cancelled_snapshot_preparation_lands_cleanup_and_preserves_cancellation(tmp_path, monkeypatch, preparation_fails):
    """A late preparation error must not replace cancellation or start a child."""
    manager = ShowRuntimeManager(command="synthetic-runtime", runtime_dir=tmp_path / "runtime",
                                 workspace_root=tmp_path / "show")
    entered = threading.Event()
    release = threading.Event()
    prepared = []

    def prepare(runtime_dir):
        entered.set()
        assert release.wait(5)
        if preparation_fails:
            raise OSError("synthetic private snapshot publication failure")
        snapshot = ShowVaultSnapshot(runtime_dir)
        prepared.append(snapshot)
        return snapshot

    monkeypatch.setattr("core.show_runtime._resolve_command", lambda command: [command])
    monkeypatch.setattr(manager, "_sweep_orphan_runtime_servers", lambda: None)
    monkeypatch.setattr("core.show_vault.ShowVaultSnapshot", prepare)
    monkeypatch.setattr("core.show_runtime.subprocess.Popen", lambda *a, **kw: pytest.fail("cancelled startup spawned a child"))
    task = asyncio.create_task(manager.ensure())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert manager._process is None
        assert manager._vault_snapshot is None
        assert all(not snapshot.directory.exists() for snapshot in prepared)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        manager.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not NODE, reason="Node is required for the shipped server adapter")
@pytest.mark.parametrize("outcome", ["approved", "malformed", "lost", "wrong_execution", "abort", "preabort"])
async def test_node_approval_continuation_validates_ownership_and_aborts_ipc(tmp_path, monkeypatch, outcome):
    """Only confirmed owning approval sends fetch again; abort closes real IPC."""
    snapshot = ShowVaultSnapshot(tmp_path)
    requests = []
    awaiting = asyncio.Event()
    disconnected = asyncio.Event()
    instance = "c" * 32

    async def receive(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(":", 1) for line in head.decode().split("\r\n")[1:] if ":" in line)
            request = json.loads(await reader.readexactly(int(headers["Content-Length"])))
            requests.append(request)
            if request["operation"] == "vault.fetch.await":
                awaiting.set()
                if outcome == "abort":
                    assert await reader.read() == b""
                    disconnected.set()
                    return
                if outcome == "lost":
                    return
                response = result({"ok": True, "kind": "vault_fetch_approved"},
                                  execution="completed" if outcome == "wrong_execution" else "not_executed")
            elif len(requests) == 1:
                response = result({"ok": False, "kind": "error", "code": "approval_required"}, execution="not_executed")
                response["approval"] = {"auth": "CANONICAL", "request_id": "request-1", "timeout": 5}
                if outcome == "malformed":
                    del response["approval"]["auth"]
            else:
                response = result({"ok": True, "kind": "vault_fetch", "status": 200, "body": "approved body", "headers": {}})
            data = json.dumps(response).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\nX-Avibe-Control-Instance: {instance}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(internal_client, "resolve_control_endpoint", lambda: control_ipc.ControlIpcClientEndpoint(
        transport="tcp", descriptor=control_ipc.ControlIpcDescriptor(
            schema_version=1, transport="tcp", host="127.0.0.1", port=port,
            instance_id=instance, bearer_token="synthetic-token",
        )))
    await asyncio.to_thread(snapshot.refresh)
    script = f'''
        import {{ vaultFetch }} from {json.dumps(MODULE)};
        import {{ createInterface }} from 'node:readline';
        const controller = new AbortController();
        const lines = createInterface({{input:process.stdin}});
        lines.on('line', () => controller.abort());
        if ({json.dumps(outcome)} === 'preabort') controller.abort();
        const value = await vaultFetch({{
          auth:'SYNTHETIC.value', url:'https://api.example.com', body:'original body', approvalWaitSeconds:5,
        }}, {{callerEnv:{{AVIBE_SESSION_ID:'session-1'}}, signal:controller.signal}});
        console.log(JSON.stringify(value));
        lines.close();
        process.stdin.pause();
    '''
    env = {key: os.environ[key] for key in ("PATH", "HOME", "AVIBE_HOME") if key in os.environ}
    env.update(snapshot.environment())
    child = await asyncio.create_subprocess_exec(
        NODE, "--input-type=module", "-e", script, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        if outcome == "abort":
            await asyncio.wait_for(awaiting.wait(), 5)
            child.stdin.write(b"abort\n")
            await child.stdin.drain()
        response = json.loads(await asyncio.wait_for(child.stdout.readline(), 10))
        if outcome == "approved":
            assert response["payload"]["body"] == "approved body"
            assert [request["operation"] for request in requests] == ["vault.fetch", "vault.fetch.await", "vault.fetch"]
            assert requests[2]["arguments"]["auth"] == "CANONICAL"
            assert requests[2]["arguments"]["body_b64"] == requests[0]["arguments"]["body_b64"]
            assert requests[2]["arguments"]["no_approval_wait"] is True
            assert all(request["caller_env"] == {"AVIBE_SESSION_ID": "session-1"} for request in requests)
        elif outcome == "preabort":
            assert response["execution"] == "not_executed"
            assert requests == []
        else:
            assert response["execution"] == "execution_unknown"
            assert len(requests) == (1 if outcome == "malformed" else 2)
            if outcome == "abort":
                await asyncio.wait_for(disconnected.wait(), 5)
        assert "synthetic-token" not in json.dumps(response)
    finally:
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 10)
        assert await child.stderr.read() == b""
        snapshot.close()
        server.close()
        await server.wait_closed()

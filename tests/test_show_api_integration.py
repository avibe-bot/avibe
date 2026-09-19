"""Real HTTP producer/consumer check, run by check_show_router.mjs in CI.

Set AVIBE_SHOW_API_RUNTIME_ROOT to a built checkout at the existing CI Runtime
pin to run this standalone. No installed runtime, credentials or services used.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap

import httpx
import pytest
import uvicorn

from config import paths
from core import show_runtime
from tests.test_show_api import create_ingress
from vibe import ui_server

RUNTIME_SHA = "5a9a6a52f2ae03611d617a659bfd0c1c32389478"


async def test_real_ui_runtime_signed_handler_inbox(tmp_path, monkeypatch):
    configured = os.environ.get("AVIBE_SHOW_API_RUNTIME_ROOT")
    if not configured:
        pytest.skip("Real Runtime is exercised by the pinned show-router-integration CI job")
    runtime = Path(configured).resolve()
    assert subprocess.check_output(["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True).strip() == RUNTIME_SHA
    cli = runtime / "packages/runtime/dist/cli.js"
    assert cli.is_file(), "Build the pinned Runtime first"
    ingress = create_ingress(tmp_path)
    inbox = tmp_path / "inbox.sqlite"
    secret = "synthetic-show-api-test-secret"
    receiver = tmp_path / "receiver.py"
    receiver.write_text(textwrap.dedent('''\
        import base64, hashlib, hmac, json, os, pathlib, sqlite3, sys
        envelope = json.load(sys.stdin)
        body = base64.b64decode(envelope["body"])
        signature = envelope["headers"].get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(os.environ["SHOW_API_FIXTURE_SECRET"].encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            print(401)
            sys.exit(0)
        payload = json.loads(body)
        assert payload["message"] == "中文 ☃"
        # Representative child writes remain inside this test's HOME/config/Vault.
        for name in ("HOME", "AVIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            root = pathlib.Path(os.environ[name])
            assert root.is_relative_to(pathlib.Path(os.environ["SHOW_API_FIXTURE_ROOT"]))
        marker = pathlib.Path(os.environ["AVIBE_HOME"]) / "vault" / "fixture-write"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("synthetic")
        with sqlite3.connect(os.environ["SHOW_API_FIXTURE_INBOX"]) as db:
            db.execute("create table if not exists inbox (digest text primary key, headers text)")
            db.execute("insert or ignore into inbox values (?, ?)", (hashlib.sha256(body).hexdigest(), json.dumps(envelope["headers"])))
        print(202)
    '''))
    (ingress.workspace / "api/receive.ts").write_text(textwrap.dedent(f'''\
        import {{ spawnSync }} from "node:child_process"
        export async function POST(request: Request) {{
          const body = Buffer.from(await request.arrayBuffer()).toString("base64")
          const headers = Object.fromEntries(request.headers)
          const result = spawnSync({json.dumps(sys.executable)}, [{json.dumps(str(receiver))}], {{
            input: JSON.stringify({{body, headers}}), encoding: "utf8"
          }})
          if (result.status !== 0) return Response.json({{error: "fixture failed"}}, {{status: 503}})
          return Response.json({{private: "must never reach the sender"}}, {{status: Number(result.stdout.trim())}})
        }}
    '''))
    # Child commands inherit only synthetic test state, never the Agent shell's
    # tokens, proxies, CLI/Vault settings, or runtime override variables.
    safe_env = {name: os.environ[name] for name in (
        "PATH", "HOME", "AVIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    )}
    for name in tuple(os.environ):
        monkeypatch.delenv(name, raising=False)
    for name, value in safe_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SHOW_API_FIXTURE_SECRET", secret)
    monkeypatch.setenv("SHOW_API_FIXTURE_ROOT", str(tmp_path))
    monkeypatch.setenv("SHOW_API_FIXTURE_INBOX", str(inbox))
    monkeypatch.setenv("AVIBE_ALLOW_DEV_STATE_MIGRATION", "1")
    proxy_requests = []

    async def proxy_trap(reader, writer):
        proxy_requests.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    proxy = await asyncio.start_server(proxy_trap, "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    node = shutil.which("node")
    assert node
    manager = show_runtime.ShowRuntimeManager(
        command=shlex.join([node, str(cli)]), workspace_root=paths.get_show_pages_dir(),
        runtime_dir=tmp_path / "runtime", auto_install=False, offline=True,
    )
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    # Authorized activation owns startup. Anonymous requests never do.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(ui_server.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    task = None
    try:
        ready = await manager.ensure()
        assert ready.available, ready.reason
        task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        body = '{  "message": "中文 ☃",\n "values": [ 1,  2 ] }\n'.encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            "Host": "alex.avibe.bot", "Content-Type": "application/json",
            "X-Hub-Signature-256": signature, "X-Github-Event": "push", "X-Github-Delivery": "fixture-delivery",
            "Authorization": "Bearer synthetic", "Cookie": "owner=synthetic",
            "X-Avibe-Show-Context": "private", "X-Vibe-Show-Base": "/show/owner/",
        }
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
            accepted = await client.post(ingress.path, headers=headers, content=body)
            assert accepted.status_code == 202, accepted.text
            assert accepted.json() == {"ok": True}
            assert accepted.headers["cache-control"] == "no-store"
            assert "set-cookie" not in accepted.headers
            with sqlite3.connect(inbox) as db:
                rows = db.execute("select digest, headers from inbox").fetchall()
            assert len(rows) == 1 and rows[0][0] == hashlib.sha256(body).hexdigest()
            forwarded = json.loads(rows[0][1])
            assert forwarded["x-avibe-show-context"] == "shared"
            assert forwarded["x-avibe-show-protocol"] == "1"
            assert forwarded["x-vibe-show-base"] == f"/p/{ingress.page.share_id}/"
            assert "authorization" not in forwarded and "cookie" not in forwarded
            tampered = await client.post(ingress.path, headers=headers, content=body + b" ")
            assert tampered.status_code == 401
            assert tampered.json() == {"error": "show_server_api_rejected"}
            with sqlite3.connect(inbox) as db:
                assert db.execute("select count(*) from inbox").fetchone()[0] == 1
            (ingress.workspace / ".show-api.json").unlink()
            revoked = await client.post(ingress.path, headers=headers, content=body)
            assert revoked.status_code == 403
        assert (paths.get_vibe_remote_dir() / "vault/fixture-write").read_text() == "synthetic"
        assert proxy_requests == [], "Runtime loopback traffic must ignore environment proxies"
    finally:
        server.should_exit = True
        if task is not None:
            await asyncio.wait_for(task, 10)
        listener.close()
        manager.stop()
        proxy.close()
        await proxy.wait_closed()

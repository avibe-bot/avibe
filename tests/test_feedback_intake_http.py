"""Actual UI → pinned packaged Runtime → TS → real Vault CLI → GitHub fake."""
import asyncio
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import socket

import httpx
import pytest
import uvicorn

from config import paths
from core import show_runtime
from tests.test_feedback_intake import APP, github, real_vault, make_payload  # noqa: F401
from tests.test_show_api import create_ingress
from vibe import ui_server

# Capture opt-in artifact paths before the hermetic fixture strips the environment.
MANIFEST = os.environ.get("AVIBE_FEEDBACK_RUNTIME_MANIFEST")
ARCHIVE = os.environ.get("AVIBE_FEEDBACK_RUNTIME_ARCHIVE")


async def test_real_http_packaged_runtime_receipts_and_private_ledger(real_vault, tmp_path, monkeypatch):
    if not MANIFEST or not ARCHIVE:
        pytest.skip("Set AVIBE_FEEDBACK_RUNTIME_MANIFEST and AVIBE_FEEDBACK_RUNTIME_ARCHIVE to SHA-verified deployment artifacts")
    spec = importlib.util.spec_from_file_location("prepare_feedback_runtime", APP / "integration/prepare-runtime.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    runtime = prepare.prepare(Path(MANIFEST), Path(ARCHIVE), tmp_path / "runtime-package")
    ingress = create_ingress(tmp_path)
    for name in ("api", "lib"):
        shutil.copytree(APP / name, ingress.workspace / name, dirs_exist_ok=True)
    for name in ("worker.py", ".show-api.json"):
        shutil.copyfile(APP / name, ingress.workspace / name)
    monkeypatch.setenv("AVIBE_FEEDBACK_APP_ROOT", str(ingress.workspace))
    monkeypatch.setenv("AVIBE_FEEDBACK_PYTHON", os.sys.executable)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    manager = show_runtime.ShowRuntimeManager(
        command=shlex.join([shutil.which("node"), str(runtime / "packages/runtime/dist/cli.js")]),
        workspace_root=paths.get_show_pages_dir(), runtime_dir=tmp_path / "runtime-state", auto_install=False, offline=True,
    )
    monkeypatch.setattr(show_runtime, "get_show_runtime_manager", lambda: manager)
    listener = socket.socket()
    listener.bind(("127.0.0.1",0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(ui_server.app, lifespan="off", log_level="error"))
    task = None
    prefix = f"/p/{ingress.page.share_id}"
    headers = {"Host":"alex.avibe.bot", "Content-Type":"application/json"}
    try:
        ready = await manager.ensure()
        assert ready.available, ready.reason
        task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=35) as client:
            payload = make_payload()
            import json
            request_id = json.loads(payload)["request_id"]
            posted = await client.post(prefix + "/api/feedback", content=payload, headers=headers)
            assert posted.status_code == 202, posted.text
            assert posted.json() == {"ok":True}, "Native POST must remain status only"
            found = await client.get(prefix + "/api/feedback-status", params={"request_id":request_id}, headers=headers)
            assert found.status_code == 200, found.text
            assert found.json() == dict(schema_version=1, request_id=request_id, state="created", issue_number=100,
                                        issue_url="https://github.com/avibe-bot/avibe/issues/100")
            assert found.headers["cache-control"] == "no-store"
            repeat = await client.post(prefix + "/api/feedback", content=payload, headers=headers)
            assert repeat.status_code == 202
            conflict = await client.post(prefix + "/api/feedback", content=make_payload(request_id, body="changed"), headers=headers)
            assert conflict.status_code == 409
            invalid = await client.post(prefix + "/api/feedback", content=b'\xff', headers=headers)
            assert invalid.status_code == 400
            huge = await client.post(prefix + "/api/feedback", content=b'x'*32769, headers=headers)
            assert huge.status_code == 413
            wrong_method = await client.put(prefix + "/api/feedback", content=payload, headers=headers)
            assert wrong_method.status_code in (403,404,405)
            absent = await client.get(prefix + "/api/feedback-status", params={"request_id":"1afe402a-a181-4487-8f65-42e22c3f7fab"}, headers=headers)
            assert absent.status_code == 404
            assert len(real_vault.issues) == 1
            # DB/WAL/SHM live outside every Show workspace. Public static, asset,
            # API and absolute filesystem attempts must never return their bytes.
            for suffix in ("", "-wal", "-shm"):
                database = os.environ["AVIBE_FEEDBACK_DATABASE"] + suffix
                for route in (f"/ledger.sqlite{suffix}", f"/assets/ledger.sqlite{suffix}", f"/api/ledger.sqlite{suffix}", f"/@fs{database}", f"/../../{database.lstrip('/')}"):
                    leak = await client.get(prefix + route, headers=headers)
                    assert "SQLite format" not in leak.text
                    assert "Observed: 中文" not in leak.text
                    assert "synthetic-feedback-pat" not in leak.text
            unknown_payload = make_payload(title="Write completed before disconnect")
            real_vault.mode = "disconnect"
            result = await client.post(prefix + "/api/feedback", content=unknown_payload, headers=headers)
            assert result.status_code == 503
            unknown_id = json.loads(unknown_payload)["request_id"]
            unknown = await client.get(prefix + "/api/feedback-status", params={"request_id":unknown_id}, headers=headers)
            assert unknown.json() == dict(schema_version=1, request_id=unknown_id, state="unknown")
            await client.post(prefix + "/api/feedback", content=unknown_payload, headers=headers)
            assert len(real_vault.issues) == 2
    finally:
        server.should_exit = True
        if task:
            await asyncio.wait_for(task,10)
        listener.close()
        manager.stop()

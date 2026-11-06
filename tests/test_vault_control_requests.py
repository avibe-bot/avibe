"""Vault control boundaries: DB-free callers and no replay after uncertain egress.

The existing Vault suites own policy, grant, field and audit semantics. These
cases cover the new process boundary those direct-command tests cannot reach.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from core import control_ipc
from core.control_requests import ControlRequest, result
from vibe import cli, internal_client


def _transport(monkeypatch, receive):
    endpoint = control_ipc.ControlIpcClientEndpoint(transport="unix", socket_path=Path("/unused-test.sock"))
    monkeypatch.setattr(internal_client, "_resolve_endpoint", lambda _: endpoint)
    monkeypatch.setattr(internal_client, "_sync_transport", lambda _: httpx.MockTransport(receive))


def _no_sqlite(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("Vault fetch client opened SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", forbidden)
    return attempts


@pytest.mark.parametrize("remote", [False, True])
def test_fetch_main_is_db_free_and_forwards_raw_context(monkeypatch, capfd, remote):
    attempts = _no_sqlite(monkeypatch)
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_fetch")
    if remote:
        monkeypatch.setenv("AVIBE_CALLER_REMOTE", "1")
        monkeypatch.setenv("AVIBE_CALLER_RESOURCE_CONTEXT", "{not-json")
    sent = []

    def receive(request):
        sent.append(ControlRequest.from_payload(json.loads(request.content)))
        return httpx.Response(200, json=result({"ok": True, "kind": "vault_fetch", "status": 200, "body": "reply", "headers": {}}))

    _transport(monkeypatch, receive)
    monkeypatch.setattr(sys, "argv", ["vibe", "vault", "fetch", "--auth", "TOKEN", "--url", "https://api.example.com"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    assert capfd.readouterr().out == "reply"
    assert attempts == []
    assert len(sent) == 1
    assert sent[0].operation == "vault.fetch"
    assert sent[0].caller_env["AVIBE_SESSION_ID"] == "ses_fetch"
    assert (sent[0].caller_env.get("AVIBE_CALLER_RESOURCE_CONTEXT") == "{not-json") is remote
    assert sent[0].arguments["caller_pid"] == os.getpid()


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_fetch_missing_service_does_not_open_database_or_start_process(monkeypatch, tmp_path, capfd, platform_name):
    attempts = _no_sqlite(monkeypatch)
    monkeypatch.setattr(internal_client, "_platform_name", lambda: platform_name)
    home = tmp_path / "absent-home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(tmp_path / "absent.sock"))
    starts = []

    def start(*args, **kwargs):
        starts.append((args, kwargs))
        raise AssertionError("Vault fetch tried to start a service")

    monkeypatch.setattr("subprocess.Popen", start)
    args = cli.build_parser().parse_args(["vault", "fetch", "--auth", "KEY", "--url", "https://api.example.com"])
    assert cli.cmd_vault_fetch(args) == 1
    payload = json.loads(capfd.readouterr().err)
    assert (payload["code"], payload["execution"]) == ("service_unavailable", "not_executed")
    assert attempts == []
    assert starts == []
    if platform_name == "nt":
        # Native Windows discovery may create its private descriptor lock even
        # when no endpoint exists. It must create nothing else, especially a DB.
        assert {path.relative_to(home).as_posix() for path in home.rglob("*")} == {
            "runtime", "runtime/control-ipc", "runtime/control-ipc/endpoint.json.lock",
        }
    else:
        assert not home.exists()


@pytest.mark.parametrize("source", ["file", "stdin", "fd"])
def test_fetch_materializes_body_in_invoking_process(monkeypatch, tmp_path, capfd, source):
    if source != "file" and os.name == "nt":
        pytest.skip("POSIX descriptor paths")
    attempts = _no_sqlite(monkeypatch)
    data = b'{"message":"client-owned"}'
    path = tmp_path / "input.json"
    path.write_bytes(data)
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=result({"ok": True, "kind": "vault_fetch", "status": 200, "body": "", "headers": {}}))

    _transport(monkeypatch, receive)
    with path.open("rb") as stream:
        saved_stdin = os.dup(0) if source == "stdin" else None
        try:
            if source == "stdin":
                os.dup2(stream.fileno(), 0)
            arg = str(path) if source == "file" else "/dev/stdin" if source == "stdin" else f"/dev/fd/{stream.fileno()}"
            args = cli.build_parser().parse_args(["vault", "fetch", "--auth", "TOKEN", "--url", "https://api.example.com", "--data-file", arg])
            assert cli.cmd_vault_fetch(args) == 0
        finally:
            if saved_stdin is not None:
                os.dup2(saved_stdin, 0)
                os.close(saved_stdin)
    assert attempts == []
    assert len(sent) == 1
    assert base64.b64decode(sent[0]["arguments"]["body_b64"]) == data
    assert "data_file" not in sent[0]["arguments"]
    capfd.readouterr()


async def _pending_fetch():
    from core.control_requests import new_request
    from core.handlers.cli_requests import handle_control_request
    from storage import vault_service
    from storage.vault_crypto import Sealed

    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        vault_service.create_secret(conn, name="WAIT_KEY", protection="protected",
                                    sealed=Sealed(ciphertext="test", nonce="n", wrap_meta="w"),
                                    policy={"allowed_hosts": ["api.example.com"]})
    request = new_request("vault.fetch", {"auth": "WAIT_KEY", "url": "https://api.example.com", "caller_pid": 123,
                                          "session_id": "ses_wait", "approval_wait": 5})
    response = await handle_control_request(SimpleNamespace(), request.to_payload())
    assert response["execution"] == "not_executed"
    return engine, response["approval"]["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during", ["arm", "sleep"])
async def test_cancelled_waiter_lands_arm_and_releases_on_different_worker(monkeypatch, cancel_during):
    """Cancellation must not lose the lock acquired by a still-running worker.

    Production run_in_executor drops ContextVars and offers no thread affinity;
    explicitly use different acquisition/release workers rather than to_thread.
    """
    from core.control_requests import new_request
    from core.handlers.cli_requests import handle_control_request
    from storage import vault_service
    from storage.lock import probe_lock_holder

    engine, request_id = await _pending_fetch()
    entered = threading.Event()
    release = threading.Event()
    threads = {}
    real_arm = vault_service.arm_request_waiter
    real_release = vault_service.release_request_waiter_lock

    def arm(*args, **kwargs):
        value = real_arm(*args, **kwargs)
        threads["arm"] = threading.get_ident()
        threads["waiter"] = kwargs["waiter_id"]
        entered.set()
        if cancel_during == "arm":
            assert release.wait(5)
        return value

    def unlock(*args, **kwargs):
        threads["release"] = threading.get_ident()
        return real_release(*args, **kwargs)

    monkeypatch.setattr(vault_service, "arm_request_waiter", arm)
    monkeypatch.setattr(vault_service, "release_request_waiter_lock", unlock)
    cancelling = False
    with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
        async def run_sync(fn):
            return await asyncio.get_running_loop().run_in_executor(second if cancelling else first, fn)
        payload = new_request("vault.fetch.await", {"request_id": request_id, "timeout": 5, "session_id": "ses_wait"}).to_payload()
        task = asyncio.create_task(handle_control_request(SimpleNamespace(), payload, run_sync=run_sync))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if cancel_during == "sleep":
                await asyncio.sleep(0.05)
            cancelling = True
            task.cancel()
            if cancel_during == "arm":
                await asyncio.sleep(0.02)
                assert not task.done(), "Cancellation escaped the unlanded arm transaction"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert threads["arm"] != threads["release"]
    lock_path = vault_service.request_waiter_lock_path(engine.url.database, threads["waiter"])
    assert lock_path.exists(), "The active row needs a free lock, not an unlinked lock"
    assert probe_lock_holder(lock_path) == "free"
    with engine.begin() as conn:
        request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
        option = request["card"]["grant_options"][0]
        vault_service.create_grant(conn, member_names=option["member_snapshot"], source_selector=option["source_selector"],
                                   purpose=option["purpose"], session_id="ses_wait", request_id=request_id)
        assert vault_service.claim_request_callback(conn, request_id).action == "deliver"


@pytest.mark.asyncio
async def test_http_disconnect_cancels_waiter_not_just_response(monkeypatch):
    from core.control_requests import new_request
    from core.internal_server import create_app
    from storage import vault_service
    from storage.lock import probe_lock_holder

    engine, request_id = await _pending_fetch()
    app = create_app(SimpleNamespace())
    disconnected = asyncio.Event()
    payload = new_request("vault.fetch.await", {"request_id": request_id, "timeout": 5}).to_payload()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}
        if disconnected.is_set():
            return {"type": "http.disconnect"}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/internal/control", "raw_path": b"/internal/control", "query_string": b"",
             "headers": [(b"content-type", b"application/json")], "server": ("localhost", 80), "client": ("localhost", 123)}
    task = asyncio.create_task(app(scope, receive, send))
    waiter = None
    try:
        for _ in range(100):
            def read_waiter():
                with engine.connect() as conn:
                    row = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
                    return row["delivery"].get("waiter")
            waiter = await asyncio.to_thread(read_waiter)
            if waiter:
                break
            await asyncio.sleep(0.01)
        assert waiter and waiter["status"] == "active"
        disconnected.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert probe_lock_holder(vault_service.request_waiter_lock_path(engine.url.database, waiter["id"])) == "free"
    finally:
        disconnected.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fetch_rechecks_deadline_after_executor_queue(monkeypatch):
    from core.control_requests import new_request
    from core.handlers.cli_requests import handle_control_request

    request = new_request("vault.fetch", {"auth": "KEY", "url": "https://api.example.com", "caller_pid": 123})
    now = request.deadline - 1
    calls = 0
    monkeypatch.setattr(time, "time", lambda: now)

    async def delayed(operation):
        nonlocal calls, now
        calls += 1
        if calls == 2:
            now = request.deadline + 1
        return await asyncio.to_thread(operation)

    response = await handle_control_request(SimpleNamespace(), request.to_payload(), run_sync=delayed)
    assert response["payload"]["code"] == "deadline_expired"
    assert response["execution"] == "not_executed"


@pytest.mark.asyncio
async def test_await_rechecks_deadline_in_arm_worker(monkeypatch):
    from core.control_requests import new_request
    from core.handlers.cli_requests import handle_control_request
    from storage import vault_service

    engine, request_id = await _pending_fetch()
    request = new_request("vault.fetch.await", {"request_id": request_id, "timeout": 0.01})
    now = request.deadline - 1
    calls = 0
    monkeypatch.setattr(time, "time", lambda: now)

    async def delayed(operation):
        nonlocal calls, now
        calls += 1
        if calls == 3:
            now = request.deadline + 1
        return await asyncio.to_thread(operation)

    response = await handle_control_request(SimpleNamespace(), request.to_payload(), run_sync=delayed)
    assert response["payload"]["code"] == "deadline_expired"
    with engine.connect() as conn:
        row = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
    assert "waiter" not in row["delivery"]


@pytest.mark.asyncio
async def test_disconnect_before_queued_completion_preserves_callback(monkeypatch):
    from core.control_requests import new_request
    from core.handlers.cli_requests import handle_control_request
    from storage import vault_service
    from storage.lock import probe_lock_holder

    engine, request_id = await _pending_fetch()
    polled = threading.Event()
    release = threading.Event()
    real_get = vault_service.get_request
    blocker = None
    waiting = None
    # One worker makes the queued-completion race deterministic. Poll queues
    # unrelated work first, so cancellation is known before completion starts.
    with ThreadPoolExecutor(max_workers=1) as executor:
        def observed_get(*args, **kwargs):
            nonlocal blocker
            row = real_get(*args, **kwargs)
            if row["status"] == "approved" and blocker is None:
                blocker = executor.submit(release.wait, 5)
                polled.set()
            return row

        monkeypatch.setattr(vault_service, "get_request", observed_get)

        async def run_sync(operation):
            return await asyncio.get_running_loop().run_in_executor(executor, operation)

        task = asyncio.create_task(handle_control_request(SimpleNamespace(), new_request(
            "vault.fetch.await", {"request_id": request_id, "timeout": 5},
        ).to_payload(), run_sync=run_sync))
        try:
            for _ in range(100):
                def read_waiter():
                    with engine.connect() as conn:
                        return real_get(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)["delivery"].get("waiter")
                waiting = await asyncio.to_thread(read_waiter)
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting
            def approve():
                with engine.begin() as conn:
                    row = real_get(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
                    option = row["card"]["grant_options"][0]
                    vault_service.create_grant(conn, member_names=option["member_snapshot"], source_selector=option["source_selector"],
                                               purpose=option["purpose"], session_id="ses_wait", request_id=request_id)
            await asyncio.to_thread(approve)
            assert await asyncio.to_thread(polled.wait, 5)
            await asyncio.sleep(0.03)  # finish is now behind the unrelated worker
            task.cancel()
            await asyncio.sleep(0.03)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    with engine.begin() as conn:
        row = real_get(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
        assert row["delivery"]["waiter"]["status"] == "active"
        assert vault_service.claim_request_callback(conn, request_id).action == "deliver"
    path = vault_service.request_waiter_lock_path(engine.url.database, waiting["id"])
    assert probe_lock_holder(path) in {"free", "missing"}


@pytest.mark.parametrize("broken", [None, [], {"request_id": "req", "timeout": 5},
                                    {"auth": "KEY", "request_id": "req", "timeout": True},
                                    {"auth": "KEY", "request_id": "req", "timeout": -1}])
def test_malformed_approval_response_never_consumes_continuation(monkeypatch, capfd, broken):
    calls = []

    def receive(request):
        calls.append(request)
        response = result({"ok": False, "kind": "error", "code": "approval_required", "error": "approve"}, execution="not_executed")
        response["approval"] = broken
        return httpx.Response(200, json=response)

    _transport(monkeypatch, receive)
    args = cli.build_parser().parse_args(["vault", "fetch", "--auth", "KEY", "--url", "https://api.example.com"])
    assert cli.cmd_vault_fetch(args) == 1
    assert len(calls) == 1, "Malformed approval consumed an owning await"
    assert json.loads(capfd.readouterr().err)["execution"] == "execution_unknown"


@pytest.mark.asyncio
async def test_payload_codec_and_concurrent_authorities_stay_inside_workers(monkeypatch):
    from core.control_requests import new_request
    from core.internal_server import create_app
    from storage import vault_service
    from storage.vault_crypto import Sealed
    from vibe import api
    from vibe.authorization import AuthorizationContext, current_invocation_authority

    names = ("CODEC_A", "CODEC_B")
    engine = cli._open_vault_engine()
    with engine.begin() as conn:
        for name in names:
            vault_service.create_secret(conn, name=name, sealed=Sealed(ciphertext="test", nonce="n", wrap_meta="w"),
                                        policy={"allowed_hosts": ["api.example.com"]})
    authorities = {name: AuthorizationContext(instance_role="owner", subject=name) for name in names}
    monkeypatch.setattr(cli, "_cli_invocation_authority", lambda env: authorities[env["AVIBE_SESSION_ID"]])
    before_env, before_cwd = dict(os.environ), os.getcwd()
    barrier = threading.Barrier(2)
    loop_thread = threading.get_ident()
    steps = []
    decode = base64.b64decode
    loads = json.loads
    render = __import__("starlette.responses", fromlist=["JSONResponse"]).JSONResponse.render
    get_meta = vault_service.get_secret_record_meta

    def decode_body(value, *args, **kwargs):
        if value == "Ym9keQ==":
            steps.append("body")
            assert threading.get_ident() != loop_thread
        return decode(value, *args, **kwargs)

    def parse(value, *args, **kwargs):
        if isinstance(value, bytes) and b'"body_b64"' in value:
            steps.append("json-in")
            assert threading.get_ident() != loop_thread
        return loads(value, *args, **kwargs)

    def encode(self, value):
        if isinstance(value, dict) and value.get("payload", {}).get("kind") == "vault_fetch":
            steps.append("json-out")
            assert threading.get_ident() != loop_thread
        return render(self, value)

    def meta(conn, name, *args, **kwargs):
        steps.append("authority")
        assert current_invocation_authority() is authorities[name]
        return get_meta(conn, name, *args, **kwargs)

    def deliver(name, *_args):
        barrier.wait(5)
        assert current_invocation_authority() is authorities[name]
        return {"status": 200, "body": name + " reply", "headers": {}}

    monkeypatch.setattr(base64, "b64decode", decode_body)
    monkeypatch.setattr(json, "loads", parse)
    monkeypatch.setattr(__import__("starlette.responses", fromlist=["JSONResponse"]).JSONResponse, "render", encode)
    monkeypatch.setattr(vault_service, "get_secret_record_meta", meta)
    monkeypatch.setattr(api, "avault_deliver_fetch", deliver)
    # Use the actual production executor, which unlike to_thread drops ContextVars.
    from core.runtime_work import RuntimeWorkSupervisor
    supervisor = RuntimeWorkSupervisor()
    app = create_app(SimpleNamespace(runtime_work_supervisor=supervisor))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://private") as client:
            payloads = []
            for name in names:
                payload = new_request("vault.fetch", {
                    "auth": name, "url": "https://api.example.com", "caller_pid": 123, "body_b64": "Ym9keQ==",
                }).to_payload()
                payload["caller_env"] = {"AVIBE_SESSION_ID": name}
                payloads.append(payload)
            responses = await asyncio.gather(*(client.post("/internal/control", json=payload) for payload in payloads))
        for name, response in zip(names, responses, strict=True):
            assert response.json()["payload"]["body"] == name + " reply", response.text
        assert {"body", "json-in", "json-out", "authority"}.issubset(steps)
        assert dict(os.environ) == before_env
        assert os.getcwd() == before_cwd
    finally:
        await supervisor.stop()

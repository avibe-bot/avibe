"""Control IPC contracts not covered by the existing enqueue behavior tests.

Protect DB-free clients, one-shot failure classification, queued deadline expiry,
and concurrent request-local state. Existing Agent Run tests own routing semantics.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core import control_ipc
from core.control_requests import ControlRequest, new_request, result
from core.handlers.cli_requests import _agent_run_arguments, handle_control_request
from vibe import cli, internal_client


def _transport(monkeypatch, handler):
    endpoint = control_ipc.ControlIpcClientEndpoint(transport="unix", socket_path=Path("/unused-test.sock"))
    monkeypatch.setattr(internal_client, "_resolve_endpoint", lambda _path: endpoint)
    monkeypatch.setattr(internal_client, "_sync_transport", lambda _endpoint: httpx.MockTransport(handler))


def _no_sqlite(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("Thin Agent Run client opened SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", forbidden)
    return attempts


@pytest.mark.parametrize("remote", [False, True])
def test_agent_run_main_is_db_free_and_forwards_raw_context(monkeypatch, capsys, remote):
    """Admission must not silently reintroduce SQLite in a remote CLI process."""
    attempts = _no_sqlite(monkeypatch)
    if remote:
        monkeypatch.setenv("AVIBE_CALLER_REMOTE", "1")
        # Intentionally malformed: the CLIENT must not resolve or downgrade it.
        monkeypatch.setenv("AVIBE_CALLER_RESOURCE_CONTEXT", "{not-json")
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_caller")
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=result({"ok": True, "kind": "agent_run", "run": {"id": "run-1"}}))

    _transport(monkeypatch, receive)
    monkeypatch.setattr(sys, "argv", ["vibe", "agent", "run", "--agent", "worker", "--message", "hi"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["run"]["id"] == "run-1"
    assert attempts == []
    assert len(sent) == 1
    request = ControlRequest.from_payload(sent[0])
    assert request.caller_env["AVIBE_SESSION_ID"] == "ses_caller"
    assert (request.caller_env.get("AVIBE_CALLER_RESOURCE_CONTEXT") == "{not-json") is remote
    assert "AVIBE_HOME" not in request.caller_env
    assert request.cwd == os.getcwd()
    assert not request.expired


def test_missing_service_is_not_executed_without_database_or_auto_start(monkeypatch, tmp_path, capsys):
    attempts = _no_sqlite(monkeypatch)
    state = tmp_path / "unused-home"
    monkeypatch.setenv("AVIBE_HOME", str(state))
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(tmp_path / "absent.sock"))
    args = cli.build_parser().parse_args(["agent", "run", "--agent", "worker", "--message", "hi"])
    assert cli.cmd_agent_run(args) == 1
    payload = json.loads(capsys.readouterr().err)
    assert (payload["code"], payload["execution"]) == ("service_unavailable", "not_executed")
    assert attempts == []
    assert not state.exists()


@pytest.mark.parametrize(
    ("failure_kind", "expected"),
    [("connect", "service_unavailable"), ("read", "execution_unknown"),
     ("write", "execution_unknown"), ("broken", "execution_unknown"),
     ("malformed", "execution_unknown"), ("old-service", "service_unavailable"),
     ("server-error", "execution_unknown")],
)
def test_control_transport_never_replays_or_claims_post_send_nonexecution(monkeypatch, capsys, failure_kind, expected):
    calls = []

    def receive(request):
        calls.append(request)
        errors = {"connect": httpx.ConnectError, "read": httpx.ReadTimeout,
                  "write": httpx.WriteError, "broken": httpx.RemoteProtocolError}
        if failure_kind in errors:
            raise errors[failure_kind]("test failure", request=request)
        if failure_kind == "old-service":
            return httpx.Response(404, json={"detail": "Not Found"})
        if failure_kind == "server-error":
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"unexpected": True})

    _transport(monkeypatch, receive)
    args = cli.build_parser().parse_args(["agent", "run", "--agent", "worker", "--message", "hi"])
    assert cli.cmd_agent_run(args) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == expected
    assert payload["execution"] == ("not_executed" if expected == "service_unavailable" else "execution_unknown")
    assert len(calls) == 1


def test_control_preparation_failure_is_not_executed(monkeypatch, capsys):
    endpoint = control_ipc.ControlIpcClientEndpoint(transport="unix", socket_path=Path("/unused-test.sock"))
    monkeypatch.setattr(internal_client, "_resolve_endpoint", lambda _path: endpoint)

    def unavailable(_endpoint):
        raise OSError("Transport construction failed before submission")

    monkeypatch.setattr(internal_client, "_sync_transport", unavailable)
    args = cli.build_parser().parse_args(["agent", "run", "--agent", "worker", "--message", "hi"])
    assert cli.cmd_agent_run(args) == 1
    assert json.loads(capsys.readouterr().err)["execution"] == "not_executed"


def test_agent_run_wire_fields_cover_parser_arguments():
    args = cli.build_parser().parse_args(["agent", "run", "--agent", "worker", "--message", "hi"])
    decoded = _agent_run_arguments(vars(args))
    assert vars(decoded) == vars(args)


@pytest.fixture
def warm_controller():
    from core.scheduled_tasks import TaskExecutionStore
    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    agents = VibeAgentStore()
    agents.create(name="worker", backend="codex")
    requests = TaskExecutionStore()
    yield SimpleNamespace(vibe_agent_store=agents, scheduled_task_service=SimpleNamespace(request_store=requests))
    agents.close()
    requests.sqlite_backend.close()


@pytest.mark.asyncio
async def test_expired_executor_queued_request_never_reaches_enqueue(monkeypatch):
    request = new_request("agent.run", {"agent": "worker", "message": "hi"})
    now = request.deadline - 1
    monkeypatch.setattr(time, "time", lambda: now)

    async def queued(operation):
        nonlocal now
        now = request.deadline + 1
        return await asyncio.to_thread(operation)

    # No stores: touching the controller would reveal that admission ran late.
    response = await handle_control_request(object(), request.to_payload(), run_sync=queued)
    assert response["execution"] == "not_executed"
    assert response["payload"]["code"] == "deadline_expired"


@pytest.mark.asyncio
async def test_concurrent_requests_keep_caller_cwd_message_and_warm_store_isolated(warm_controller, tmp_path):
    from core.services.sessions import reserve_standalone_agent_session
    from core.scheduled_tasks import resolve_session_id_target
    from core.internal_server import create_app

    agent = warm_controller.vibe_agent_store.require("worker")
    requests = []
    for index in range(2):
        cwd = tmp_path / str(index)
        cwd.mkdir()
        (cwd / "message.txt").write_text(f"message-{index}", encoding="utf-8")
        source = reserve_standalone_agent_session(
            agent_backend="codex", session_anchor=f"source-{index}", agent_id=agent.id,
            agent_name=agent.name, model=agent.model, workdir=str(cwd),
        )
        requests.append(ControlRequest(
            "agent.run", {"agent": "worker", "message_file": "message.txt"},
            {"AVIBE_SESSION_ID": source, "AVIBE_RUN_ID": f"parent-{index}"}, str(cwd), time.time() + 30,
        ))
    env_before, cwd_before = dict(os.environ), os.getcwd()
    app = create_app(warm_controller)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as client:
        replies = await asyncio.gather(*(client.post("/internal/control", json=r.to_payload()) for r in requests))
    ids = set()
    for request, reply in zip(requests, replies):
        assert reply.status_code == 200
        response = reply.json()
        assert response["exit_code"] == 0, response
        run = warm_controller.scheduled_task_service.request_store.get_run(response["payload"]["run"]["id"])
        ids.add(run["id"])
        assert run["source_actor"] == request.caller_env["AVIBE_SESSION_ID"]
        assert run["parent_run_id"] == request.caller_env["AVIBE_RUN_ID"]
        assert run["message"] == (Path(request.cwd) / "message.txt").read_text()
        assert resolve_session_id_target(run["session_id"]).workdir == request.cwd
    assert len(ids) == 2
    assert dict(os.environ) == env_before
    assert os.getcwd() == cwd_before
    assert warm_controller.vibe_agent_store.require("worker").id == agent.id


@pytest.mark.asyncio
async def test_sync_wait_does_not_pin_loop_or_worker(monkeypatch):
    """A waiting CLI must leave the controller able to complete the awaited Run."""
    loop_thread = threading.get_ident()
    finished = False
    reads = []

    class Store:
        def get_run(self, run_id):
            reads.append(threading.get_ident())
            return {"id": run_id, "status": "succeeded" if finished else "running"}

    async def finish():
        nonlocal finished
        await asyncio.sleep(0.01)
        finished = True

    waiter, _ = await asyncio.gather(cli._wait_for_run_result(Store(), "run-1", wait_timeout=1), finish())
    assert waiter["status"] == "succeeded"
    assert len(reads) >= 2
    assert all(thread != loop_thread for thread in reads)

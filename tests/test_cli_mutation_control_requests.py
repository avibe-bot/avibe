"""Control IPC contracts of the Task, Watch, Session and Run mutations.

Existing command suites own each command's semantics through ``in_process_cli_control``.
These cases own what moving them into the warm service could break on its own:
a DB-free client, the strict argument schema, admission after queueing,
request-local caller state on an executor that copies no context, the
controller's own stores, and waits that must not hold a runtime-work worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core import control_ipc
from core.control_requests import (
    ADMISSION_SECONDS,
    EXECUTION_SECONDS,
    OPERATION_WAIT_SECONDS,
    OPERATIONS,
    ControlRequest,
    result,
)
from core.handlers.cli_requests import _command_arguments, handle_control_request
from vibe import cli, internal_client

_ARGV = {
    "task.add": ["task", "add", "--cron", "0 * * * *", "--message", "hi"],
    "task.update": ["task", "update", "t1", "--name", "n"],
    "task.pause": ["task", "pause", "t1"],
    "task.resume": ["task", "resume", "t1"],
    "task.remove": ["task", "remove", "t1"],
    "task.run": ["task", "run", "t1"],
    "watch.add": ["watch", "add", "--shell", "true"],
    "watch.update": ["watch", "update", "w1", "--name", "n"],
    "watch.pause": ["watch", "pause", "w1"],
    "watch.resume": ["watch", "resume", "w1"],
    "watch.remove": ["watch", "remove", "w1"],
    "session.update": ["session", "update", "--session-id", "s1", "--title", "x"],
    "session.send-now": ["session", "send-now", "--session-id", "s1"],
    "session.queue.remove": ["session", "queue", "remove", "m1", "--session-id", "s1"],
    "runs.cancel": ["runs", "cancel", "r1"],
}


def _transport(monkeypatch, handler):
    endpoint = control_ipc.ControlIpcClientEndpoint(transport="unix", socket_path=Path("/unused-test.sock"))
    monkeypatch.setattr(internal_client, "_resolve_endpoint", lambda _path: endpoint)
    monkeypatch.setattr(internal_client, "_sync_transport", lambda _endpoint: httpx.MockTransport(handler))


def _no_sqlite(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("Thin mutation client opened SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", forbidden)
    return attempts


def _request(operation: str, argv: list[str], *, env=None, cwd: str | None = None) -> ControlRequest:
    arguments = cli._control_arguments(operation, cli.build_parser().parse_args(argv))
    return ControlRequest(
        operation, json.loads(json.dumps(arguments)), dict(env or {}),
        os.getcwd() if cwd is None else cwd, time.time() + 30,
    )


def test_every_named_mutation_is_covered():
    assert set(_ARGV) == set(OPERATIONS) - {"agent.run"}


@pytest.mark.parametrize("operation", sorted(_ARGV))
def test_main_submits_each_mutation_once_without_sqlite(monkeypatch, capsys, operation):
    """Admission would otherwise open SQLite in every CLI process again."""
    attempts = _no_sqlite(monkeypatch)
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_caller")
    monkeypatch.setenv("AVIBE_CALLER_REMOTE", "1")
    # Malformed on purpose: the client must forward, not resolve or downgrade it.
    monkeypatch.setenv("AVIBE_CALLER_RESOURCE_CONTEXT", "{not-json")
    sent, timeouts = [], []

    def receive(request):
        sent.append(json.loads(request.content))
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json=result({"ok": True, "kind": "probe"}))

    _transport(monkeypatch, receive)
    monkeypatch.setattr(sys, "argv", ["vibe", *_ARGV[operation]])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "probe"
    assert attempts == []
    assert len(sent) == 1
    request = ControlRequest.from_payload(sent[0])
    assert request.operation == operation
    assert request.caller_env["AVIBE_CALLER_RESOURCE_CONTEXT"] == "{not-json"
    assert request.cwd == os.getcwd()
    # Connecting is always bounded. Only Watch creation waits for a first cycle
    # whose bound the service computes from state this process cannot read.
    assert timeouts[0]["connect"] == 5.0
    expected_read = (
        None if operation == "watch.add"
        else ADMISSION_SECONDS + OPERATION_WAIT_SECONDS.get(operation, 0.0) + EXECUTION_SECONDS
    )
    assert timeouts[0]["read"] == expected_read


@pytest.mark.parametrize("platform_name", ["native", "nt"])
@pytest.mark.parametrize("operation", sorted(_ARGV))
def test_absent_service_executes_nothing(monkeypatch, tmp_path, capsys, operation, platform_name):
    attempts = _no_sqlite(monkeypatch)
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: launched.append(args) or pytest.fail("launched"))
    monkeypatch.setattr(cli, "cmd_start", lambda *args, **kwargs: pytest.fail("the service was started"))
    if platform_name == "nt":
        # Windows endpoint discovery, whatever this runner's own platform is.
        monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    state = tmp_path / "unused-home"
    monkeypatch.setenv("AVIBE_HOME", str(state))
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setattr(sys, "argv", ["vibe", *_ARGV[operation]])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 1
    payload = json.loads(capsys.readouterr().err)
    assert (payload["code"], payload["execution"]) == ("service_unavailable", "not_executed")
    assert attempts == [] and launched == []
    # Windows descriptor discovery reads under its private lock, and creates only
    # that lock's tree. Nothing else, and no state, exists afterwards.
    lock = Path("runtime", "control-ipc", "endpoint.json.lock")
    allowed = {lock, *lock.parents} - {Path(".")}
    created = {path.relative_to(state) for path in state.rglob("*")} if state.exists() else set()
    assert created <= allowed


@pytest.mark.parametrize(
    ("operation", "argv"),
    [
        ("task.add", ["task", "add", "--name", "n", "--cron", "0 * * * *", "--timezone", "UTC",
                      "--on-failure", "agent", "--message", "m", "--timeout", "inf", "--cwd", "d", "--shell", "true"]),
        ("task.update", ["task", "update", "t1", "--clear-agent", "--create-session", "--same-scope",
                         "--timeout", "5", "--", "echo", "--flag"]),
        ("watch.add", ["watch", "add", "--forever", "--retry-exit-code", "3", "--retry-exit-code", "4",
                       "--timeout=-inf", "--lifetime-timeout", "60", "--", "sleep", "1"]),
        ("watch.update", ["watch", "update", "w1", "--once", "--clear-cwd", "--retry-delay", "2.5",
                          "--", "sleep", "1"]),
        ("session.update", ["session", "update", "--title", "x", "--hidden", "--scope-id", "none"]),
        ("session.queue.remove", ["session", "queue", "remove", "m1"]),
        ("runs.cancel", ["runs", "cancel", "r1"]),
    ],
)
def test_the_service_decodes_exactly_what_the_parser_produced(operation, argv):
    args = cli.build_parser().parse_args(argv)
    wire = json.loads(json.dumps(cli._control_arguments(operation, args), allow_nan=False))
    decoded = vars(_command_arguments(operation, OPERATIONS[operation], wire))
    assert {key: decoded[key] for key in vars(args)} == vars(args)
    assert set(decoded) - set(vars(args)) <= {"message_file_input", "invocation_timezone", "invocation_home"}
    assert all(not isinstance(value, float) or not math.isnan(value) for value in decoded.values())


@pytest.mark.parametrize(
    "change",
    [
        {"surprise": True},
        {"name": 3},
        {"task_command": "remove"},
        {"on_failure": "always"},
        {"timeout": True},
        {"timeout": "soon"},
        {"command_argv": "echo"},
        {"message": None, "message_file": "brief.md"},  # a path the service must not open
        {"invocation_timezone": None},
    ],
)
@pytest.mark.asyncio
async def test_arguments_the_parser_could_not_produce_are_refused_unexecuted(change):
    request = _request("task.add", ["task", "add", "--cron", "0 * * * *", "--message", "hi"])
    request.arguments.update(change)
    # No controller: decoding a request must not reach it.
    response = await handle_control_request(object(), request.to_payload())
    assert (response["execution"], response["payload"]["code"]) == ("not_executed", "invalid_control_request")


@pytest.mark.parametrize(
    ("operation", "positional"),
    [
        ("task.update", "task_id"), ("task.pause", "task_id"), ("task.resume", "task_id"),
        ("task.remove", "task_id"), ("task.run", "task_id"), ("watch.update", "watch_id"),
        ("watch.pause", "watch_id"), ("watch.resume", "watch_id"), ("watch.remove", "watch_id"),
        ("session.queue.remove", "message_id"), ("runs.cancel", "run_id"),
    ],
)
@pytest.mark.asyncio
async def test_an_omitted_required_id_is_refused_unexecuted(operation, positional):
    request = _request(operation, _ARGV[operation])
    del request.arguments[positional]
    response = await handle_control_request(object(), request.to_payload())
    assert (response["execution"], response["payload"]["code"]) == ("not_executed", "invalid_control_request")


@pytest.mark.parametrize("operation", sorted(_ARGV))
@pytest.mark.asyncio
async def test_expiry_while_queued_is_refused_before_any_effect(monkeypatch, operation):
    request = _request(operation, _ARGV[operation])
    now = request.deadline - 1
    monkeypatch.setattr(time, "time", lambda: now)

    async def queued(operation):
        nonlocal now
        now = request.deadline + 1
        return await asyncio.to_thread(operation)

    response = await handle_control_request(object(), request.to_payload(), run_sync=queued)
    assert (response["execution"], response["payload"]["code"]) == ("not_executed", "deadline_expired")


def _parse(argv):
    return cli.build_parser().parse_args(argv)


def _err(capsys) -> dict:
    return json.loads(capsys.readouterr().err)


@pytest.mark.usefixtures("in_process_cli_control")
def test_a_message_file_flag_keeps_its_place_in_the_task_action_matrix(tmp_path, capsys):
    """Materializing the file must not erase that ``--message-file`` was given."""
    from core.vibe_agents import VibeAgentStore
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    agents = VibeAgentStore()
    agents.create(name="worker", backend="codex")
    agents.close()
    brief = tmp_path / "brief.md"
    brief.write_text("first brief", encoding="utf-8")

    # A pure command task has no Agent to read a message: refused, not silently stored.
    assert cli.cmd_task_add(_parse(["task", "add", "--cron", "0 * * * *", "--shell", "true",
                                    "--message-file", str(brief)])) == 1
    refusal = _err(capsys)
    assert (refusal["code"], refusal["details"]["flags"]) == ("message_without_consumer", ["--message-file"])

    assert cli.cmd_task_add(_parse(["task", "add", "--create-session-per-run", "--agent", "worker",
                                    "--cron", "0 * * * *", "--message", "inline"])) == 0
    task_id = json.loads(capsys.readouterr().out)["definition"]["id"]
    brief.write_text("second brief", encoding="utf-8")
    # The file is the only change, so it must still read as a requested change.
    assert cli.cmd_task_update(_parse(["task", "update", task_id, "--message-file", str(brief)])) == 0
    assert json.loads(capsys.readouterr().out)["definition"]["message"] == "second brief"
    assert cli.cmd_task_update(_parse(["task", "update", task_id, "--message-file", str(tmp_path / "gone")])) == 1
    assert _err(capsys)["code"] == "message_file_read_failed"


@pytest.fixture
def controller():
    """The stores a real controller owns, and nothing else."""
    from core.scheduled_tasks import ScheduledTaskStore, TaskExecutionStore
    from core.vibe_agents import VibeAgentStore
    from core.watches import ManagedWatchStore, WatchRuntimeStateStore
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()
    agents = VibeAgentStore()
    agents.create(name="worker", backend="codex")
    tasks, watches, runtime, requests = (
        ScheduledTaskStore(), ManagedWatchStore(), WatchRuntimeStateStore(), TaskExecutionStore()
    )
    owner = SimpleNamespace(
        vibe_agent_store=agents,
        scheduled_task_service=SimpleNamespace(store=tasks, request_store=requests),
        watch_service=SimpleNamespace(store=watches, runtime_store=runtime),
        session_turns=None,
    )
    yield owner
    if hasattr(owner, "cli_control_stores"):
        owner.cli_control_stores.close()
    agents.close()
    for backend in (tasks._sqlite, watches._sqlite, runtime._sqlite, requests.sqlite_backend):
        backend.close()


@pytest.mark.asyncio
async def test_a_relative_message_file_is_read_where_the_caller_stands(controller, tmp_path, monkeypatch):
    caller_dir, service_dir = tmp_path / "caller", tmp_path / "service"
    caller_dir.mkdir()
    service_dir.mkdir()
    (caller_dir / "brief.md").write_text("from the caller's directory", encoding="utf-8")
    monkeypatch.chdir(caller_dir)
    request = _request("watch.add", ["watch", "add", "--create-session-per-run", "--agent", "worker",
                                     "--message-file", "brief.md", "--shell", "true"])
    monkeypatch.chdir(service_dir)  # the service's own directory, where the path means nothing

    async def started(store, runtime_store, watch_id, **_kwargs):
        return store.get_watch(watch_id), None

    monkeypatch.setattr(cli, "_wait_for_watch_startup", started)
    reply = await handle_control_request(controller, request.to_payload())
    assert reply["exit_code"] == 0, reply
    watch = controller.watch_service.store.get_watch(reply["payload"]["definition"]["id"])
    assert watch.message == "from the caller's directory"
    assert watch.cwd == str(caller_dir)


def _no_context_copy():
    """The production runtime-work executor: a worker starts from its own context."""
    pool = ThreadPoolExecutor(max_workers=4)
    return pool, (lambda operation: asyncio.get_running_loop().run_in_executor(pool, operation))


@pytest.mark.asyncio
async def test_concurrent_callers_keep_their_own_identity_cwd_and_authority(controller, tmp_path):
    from core.caller_context import CallerContext
    from core.services.sessions import reserve_standalone_agent_session
    from tests.scenarios.permissions.test_cli_invocation_authority import _pair_instance, _snapshot

    _pair_instance()
    hidden = reserve_standalone_agent_session(
        agent_backend="codex", session_anchor="owner-only", agent_name="worker", workdir=str(tmp_path),
    )
    requests = []
    for index in range(2):
        cwd = tmp_path / f"caller-{index}"
        cwd.mkdir()
        requests.append(_request(
            "task.add", ["task", "add", "--cron", "0 * * * *", "--shell", "./sync.sh"],
            env={"AVIBE_SESSION_ID": f"ses-caller-{index}"}, cwd=str(cwd),
        ))
    # An admitted remote Editor naming a Session it cannot reach is refused only
    # if its own authority, not the service's Owner default, reaches the worker.
    remote = CallerContext(session_id="ses-remote", is_remote=True, resource_user_context=_snapshot("editor"))
    requests.append(_request(
        "task.add", ["task", "add", "--cron", "0 * * * *", "--message", "hi", "--session-id", hidden],
        env=remote.to_env(),
    ))
    env_before, cwd_before = dict(os.environ), os.getcwd()
    pool, run_sync = _no_context_copy()
    with pool:
        replies = await asyncio.gather(
            *(handle_control_request(controller, request.to_payload(), run_sync=run_sync) for request in requests)
        )
    for request, reply in zip(requests[:2], replies[:2]):
        assert reply["exit_code"] == 0, reply
        stored = controller.scheduled_task_service.store.get_task(reply["payload"]["definition"]["id"])
        assert stored.cwd == request.cwd
        assert stored.metadata["created_by"]["caller"]["session_id"] == request.caller_env["AVIBE_SESSION_ID"]
    assert replies[2]["payload"]["code"] == "session_not_found"
    assert len(controller.scheduled_task_service.store.list_tasks()) == 2
    assert dict(os.environ) == env_before
    assert os.getcwd() == cwd_before


@pytest.mark.asyncio
async def test_a_removed_invocation_cwd_refuses_only_work_that_needs_it(controller):
    created = await handle_control_request(
        controller, _request("task.add", ["task", "add", "--cron", "0 * * * *", "--shell", "true"]).to_payload()
    )
    task_id = created["payload"]["definition"]["id"]
    needs_cwd = _request("task.add", ["task", "add", "--cron", "0 * * * *", "--shell", "true"], cwd="")
    refused = await handle_control_request(controller, needs_cwd.to_payload())
    assert (refused["execution"], refused["payload"]["code"]) == ("completed", "cwd_not_found")
    paused = await handle_control_request(controller, _request("task.pause", ["task", "pause", task_id], cwd="").to_payload())
    assert paused["exit_code"] == 0, paused


@pytest.mark.asyncio
async def test_mutations_land_in_the_controllers_own_mirror_and_wake_reconciliation(controller, monkeypatch):
    """The scheduler reconciles from these mirrors when ``definitions.updated`` wakes it."""
    from core.inbox_events import DEFINITIONS_UPDATED_EVENT, bus

    readers = []
    make_reader = cli._definition_reader
    monkeypatch.setattr(cli, "_definition_reader", lambda: readers.append(make_reader()) or readers[-1])
    hints = []
    subscription = bus.subscribe_callback(lambda event, data: hints.append(data) if event == DEFINITIONS_UPDATED_EVENT else None)
    try:
        replies = [
            await handle_control_request(
                controller, _request("task.add", ["task", "add", "--cron", "0 * * * *", "--shell", "true"]).to_payload()
            )
            for _ in range(2)
        ]
    finally:
        bus.unsubscribe(subscription)
    scheduler_mirror = controller.scheduled_task_service.store
    assert {task.id for task in scheduler_mirror.list_tasks()} == {
        reply["payload"]["definition"]["id"] for reply in replies
    }
    assert hints.count({"definition_type": "scheduled"}) == 2
    # The projection reader is built once and kept warm, not opened per request.
    assert len(readers) == 1


@pytest.mark.asyncio
async def test_watch_startup_confirmation_holds_no_runtime_work_worker(controller, tmp_path):
    from core.services.sessions import reserve_standalone_agent_session

    session_id = reserve_standalone_agent_session(
        agent_backend="codex", session_anchor="watch-target", agent_name="worker", workdir=str(tmp_path),
    )
    pool = ThreadPoolExecutor(max_workers=1)  # one worker: a sleeping wait would starve the rest

    async def run_sync(operation):
        return await asyncio.get_running_loop().run_in_executor(pool, operation)

    watches = controller.watch_service.store
    with pool:
        adding = asyncio.create_task(handle_control_request(
            controller,
            _request("watch.add", ["watch", "add", "--session-id", session_id, "--shell", "true"]).to_payload(),
            run_sync=run_sync,
        ))
        while not await asyncio.to_thread(lambda: (watches.maybe_reload(), watches.list_watches())[1]):
            await asyncio.sleep(0.01)
        # The Watch exists and is waiting for its first cycle; other work still runs.
        other = await asyncio.wait_for(handle_control_request(
            controller,
            _request("task.add", ["task", "add", "--cron", "0 * * * *", "--shell", "true"]).to_payload(),
            run_sync=run_sync,
        ), timeout=3)
        assert other["exit_code"] == 0, other
        assert not adding.done()
        watch_id = watches.list_watches()[0].id
        started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        await asyncio.to_thread(controller.watch_service.runtime_store.write, {
            "watches": {watch_id: {"running": True, "pid": 4242, "started_at": started}},
        })
        added = await asyncio.wait_for(adding, timeout=5)
    assert added["exit_code"] == 0, added
    assert added["payload"]["definition"]["id"] == watch_id


def _running_agent_run(controller, session_id: str) -> str:
    requests = controller.scheduled_task_service.request_store
    run = requests.enqueue_agent_run(session_id=session_id, message="keep working", agent_name="worker")
    assert requests.claim(run.id) is not None
    return run.id


@pytest.mark.asyncio
async def test_live_cancel_asks_the_owner_in_process_and_keeps_its_transport_bound(controller, monkeypatch):
    from core import control_requests

    run_id = _running_agent_run(controller, "ses-silent")
    asked, answer = [], asyncio.Event()

    async def answers_late(session_id, *, agent_run_id=None):
        asked.append((session_id, agent_run_id))
        await answer.wait()
        return {"ok": True, "session_id": session_id, "status": "cancel_requested"}

    controller.session_turns = SimpleNamespace(cancel=answers_late)
    monkeypatch.setattr(control_requests, "LIVE_OWNER_SECONDS", 0.05)
    reply = await handle_control_request(controller, _request("runs.cancel", ["runs", "cancel", run_id]).to_payload())
    answer.set()
    assert asked == [("ses-silent", run_id)]
    assert reply["payload"]["cancel_result"]["reason_code"] == "internal_unavailable"
    saved = controller.scheduled_task_service.request_store.get_run(run_id)
    assert (saved["status"], saved["cancel_requested"]) == ("running", True)


@pytest.mark.parametrize(
    ("answer", "settled"),
    [
        # The owner stopped the turn: this command terminalizes the Run.
        ({"ok": True, "status": "cancel_requested"}, ("canceled", True)),
        # An interrupt still unconfirmed, or a stop whose outcome is unknown: the
        # owner records nothing new, and this command's fallback records the cancel.
        ({"ok": True, "status": "reconciling"}, ("running", True)),
        ({"ok": False, "code": "stop_unknown"}, ("running", True)),
    ],
    ids=["confirmed", "reconciling", "stop_unknown"],
)
@pytest.mark.asyncio
async def test_a_dropped_request_still_settles_the_cancel_it_started(controller, answer, settled):
    run_id = _running_agent_run(controller, "ses-stopping")
    entered, release = asyncio.Event(), asyncio.Event()
    outcome = []

    async def slow_stop(session_id, *, agent_run_id=None):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            outcome.append("interrupted")
            raise
        outcome.append("finished")
        return {"session_id": session_id, **answer}

    controller.session_turns = SimpleNamespace(cancel=slow_stop)
    request = asyncio.create_task(
        handle_control_request(controller, _request("runs.cancel", ["runs", "cancel", run_id]).to_payload())
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    request.cancel()
    await asyncio.sleep(0.05)  # the client is gone before the owner answers
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=5)
    assert outcome == ["finished"]
    saved = controller.scheduled_task_service.request_store.get_run(run_id)
    assert (saved["status"], saved["cancel_requested"]) == settled


def _projection(watch_id: str, *, failed: bool) -> dict:
    return {
        "id": watch_id, "name": "probe", "mode": "forever", "enabled": not failed,
        "last_error": "waiter crashed" if failed else None, "message": "report it",
    }


@pytest.mark.parametrize("case", ["failed_while_waiting", "failed_at_deadline", "unconfirmed"])
@pytest.mark.asyncio
async def test_startup_errors_project_the_watch_off_the_loop_with_the_lent_reader(monkeypatch, case):
    """An error's full projection is SQLite work: never on the loop, never a new reader."""
    loop_thread = threading.get_ident()
    failed = case != "unconfirmed"
    watch = SimpleNamespace(
        id="w-probe", name="probe", mode="forever", enabled=not failed,
        last_error="waiter crashed" if failed else None, last_started_at=None,
        last_finished_at=None, last_exit_code=None,
    )
    store = SimpleNamespace(sqlite_backend=None, maybe_reload=lambda: False, get_watch=lambda _id: watch)
    runtime = SimpleNamespace(load=lambda: {"watches": {}})
    read_on = []

    class Reader:
        def get_watch(self, watch_id):
            read_on.append(threading.get_ident())
            return _projection(watch_id, failed=failed)

    monkeypatch.setattr(cli, "_definition_reader", lambda: pytest.fail("a startup error opened its own reader"))
    with pytest.raises(cli.TaskCliError) as raised:
        await cli._wait_for_watch_startup(
            store, runtime, watch.id, reader=Reader(),
            timeout_seconds=1 if case == "failed_while_waiting" else 0,
        )
    assert raised.value.code == ("watch_startup_failed" if failed else "watch_startup_unconfirmed")
    assert raised.value.details["watch"] == cli._watch_projection_payload(_projection(watch.id, failed=failed))
    assert read_on and loop_thread not in read_on


@pytest.mark.asyncio
async def test_watch_startup_keeps_its_whole_computed_recovery_bound(controller, monkeypatch):
    """Hundreds of recovering waiters take longer than any fixed response budget."""
    recovering = 200
    assert cli._default_watch_startup_timeout_seconds(recovery_entry_count=recovering) > 901
    monkeypatch.setattr(cli, "_watch_recovery_entry_count", lambda _store: recovering)
    # Only this module's clock jumps 901 s, past any fixed cap; the loop's does not.
    ticks = iter([0.0, 901.0])
    monkeypatch.setattr(cli, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: next(ticks, 901.0)}))
    from storage.background import SQLiteBackgroundTaskStore

    started = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(SQLiteBackgroundTaskStore, "_watch_runtimes", staticmethod(lambda _conn, ids: {
        watch_id: {"running": True, "started_at": started} for watch_id in ids
    }))
    response = await handle_control_request(controller, _request(
        "watch.add", ["watch", "add", "--create-session-per-run", "--agent", "worker", "--shell", "true"]
    ).to_payload())
    assert response["exit_code"] == 0, response


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Validation details keep the value the caller gave, as the CLI printed it.
        (["--timeout=-inf"], ("invalid_watch_timeout", ["details", "timeout"], -math.inf)),
        # A stored timing value comes back as stored.
        (["--timeout", "inf"], ("run_definition", ["definition", "timeout_seconds"], math.inf)),
        # POSIX argv can contain undecodable filesystem bytes. The CLI escapes
        # these in JSON; a typed path error must not become a lost response.
        (["--cwd", "missing-\udcff"], ("invalid_watch_cwd", ["details", "cwd"], "missing-\udcff")),
    ],
    ids=["refused", "stored", "surrogate_path"],
)
@pytest.mark.asyncio
async def test_the_control_response_is_decoded_and_encoded_off_the_loop_losslessly(
    controller, monkeypatch, argv, expected,
):
    from core import internal_server

    async def started(store, runtime_store, watch_id, **_kwargs):
        return store.get_watch(watch_id), None

    monkeypatch.setattr(cli, "_wait_for_watch_startup", started)
    loop_thread = threading.get_ident()
    codec_threads = []
    loads, dumps = json.loads, json.dumps

    def decode(value, *args, **kwargs):
        if isinstance(value, (bytes, bytearray)) and b'"watch.add"' in value:
            codec_threads.append(("decode", threading.get_ident()))
        return loads(value, *args, **kwargs)

    def encode(value, *args, **kwargs):
        if isinstance(value, dict) and {"execution", "payload"} <= value.keys():
            codec_threads.append(("encode", threading.get_ident()))
        return dumps(value, *args, **kwargs)

    app = internal_server.create_app(controller)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/internal/control")
    field = getattr(route, "response_field", None)
    for name in ("serialize", "serialize_json") if field is not None else ():
        serialize = getattr(type(field), name, None)
        if serialize is not None:
            def track(self, value, *args, _serialize=serialize, **kwargs):
                codec_threads.append(("framework", threading.get_ident()))
                return _serialize(self, value, *args, **kwargs)

            monkeypatch.setattr(type(field), name, track)
    request = _request("watch.add", ["watch", "add", "--create-session-per-run", "--agent", "worker",
                                     "--shell", "true", *argv])
    monkeypatch.setattr(json, "loads", decode)
    monkeypatch.setattr(json, "dumps", encode)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        reply = await client.post("/internal/control", content=dumps(request.to_payload()).encode())
    monkeypatch.undo()
    assert reply.status_code == 200, reply.text
    kind, path, value = expected
    payload = loads(reply.content)["payload"]
    assert kind in (payload.get("code"), payload.get("kind")), payload
    for key in path[:-1]:
        payload = payload[key]
    assert payload[path[-1]] == value
    assert {stage for stage, _ in codec_threads} >= {"decode", "encode"}
    assert all(thread != loop_thread for _, thread in codec_threads), codec_threads


@pytest.mark.asyncio
async def test_cli_mutations_round_trip_through_a_real_windows_endpoint(controller, monkeypatch, tmp_path, capsys):
    """The descriptor, bearer token and response path, through the real server and ``main``."""
    from core import internal_server, session_turns
    from tests.test_control_ipc import _stop_server, _wait_until_ready

    monkeypatch.setattr(internal_client, "_platform_name", lambda: "nt")
    monkeypatch.setattr(session_turns.SessionTurnManager, "recover_persisted_agent_run_queue",
                        lambda _self: asyncio.sleep(0, result=[]))
    descriptor_path = control_ipc.default_descriptor_path()
    server = asyncio.create_task(internal_server.serve(controller, platform_name="nt", descriptor_path=descriptor_path))

    async def invoke(argv):
        monkeypatch.setattr(sys, "argv", ["vibe", *argv])

        def call():
            with pytest.raises(SystemExit) as exited:
                cli.main()
            return exited.value.code

        code = await asyncio.to_thread(call)
        output = capsys.readouterr()
        assert code == 0, output.err
        return json.loads(output.out)

    try:
        await _wait_until_ready(server, descriptor_path)
        reply = await invoke(["task", "add", "--name", "café 任务", "--cron", "0 * * * *", "--timezone", "UTC",
                              "--cwd", str(tmp_path), "--shell", "echo not-run"])
        task_id = reply["definition"]["id"]
        assert reply["definition"]["name"] == "café 任务"
        assert controller.scheduled_task_service.store.get_task(task_id).name == "café 任务"
        paused = await invoke(["task", "pause", task_id])
        assert paused["definition"]["enabled"] is False
        assert controller.scheduled_task_service.store.get_task(task_id).enabled is False
        removed = await invoke(["task", "remove", task_id])
        assert removed["removed_id"] == task_id
        assert controller.scheduled_task_service.store.get_task(task_id) is None
    finally:
        await _stop_server(server)
    assert not descriptor_path.exists()


@pytest.mark.parametrize("operation", ["task.add", "task.update", "watch.add", "watch.update", "agent.run"])
@pytest.mark.asyncio
async def test_a_leading_tilde_is_the_invoking_users_home(controller, monkeypatch, tmp_path, operation):
    """Only the cwd crosses the channel; ``~`` must not become the service's home."""
    caller_home, service_home = tmp_path / "caller-home", tmp_path / "service-home"
    (caller_home / "work").mkdir(parents=True)
    service_home.mkdir()

    async def started(store, runtime_store, watch_id, **_kwargs):
        return store.get_watch(watch_id), None

    monkeypatch.setattr(cli, "_wait_for_watch_startup", started)

    def home(path):
        for name in ("HOME", "USERPROFILE"):
            monkeypatch.setenv(name, str(path))

    async def create(operation, argv):
        reply = await handle_control_request(controller, _request(operation, argv).to_payload())
        assert reply["exit_code"] == 0, reply
        return reply["payload"]["definition"]["id"]

    kind = operation.split(".")[0]
    if operation == "task.update":
        target = await create("task.add", ["task", "add", "--cron", "0 * * * *", "--shell", "true"])
    elif operation == "watch.update":
        target = await create(
            "watch.add", ["watch", "add", "--create-session-per-run", "--agent", "worker", "--shell", "true"]
        )
    argv = {
        "task.add": ["task", "add", "--cron", "0 * * * *", "--shell", "true"],
        "task.update": ["task", "update", target if operation == "task.update" else ""],
        "watch.add": ["watch", "add", "--create-session-per-run", "--agent", "worker", "--shell", "true"],
        "watch.update": ["watch", "update", target if operation == "watch.update" else ""],
        "agent.run": ["agent", "run", "--agent", "worker", "--message", "hi"],
    }[operation]
    home(caller_home)
    request = _request(operation, [*argv, "--cwd", "~/work"])
    missing = _request(operation, [*argv, "--cwd", "~/missing"])
    home(service_home)
    reply = await handle_control_request(controller, request.to_payload())
    assert reply["exit_code"] == 0, reply
    if kind == "agent":
        from core.scheduled_tasks import resolve_session_id_target

        stored = resolve_session_id_target(reply["payload"]["run"]["session_id"]).workdir
    elif kind == "task":
        stored = controller.scheduled_task_service.store.get_task(reply["payload"]["definition"]["id"]).cwd
    else:
        stored = controller.watch_service.store.get_watch(reply["payload"]["definition"]["id"]).cwd
    assert Path(stored).resolve() == (caller_home / "work").resolve()
    refused = (await handle_control_request(controller, missing.to_payload()))["payload"]
    assert refused["code"] in {"cwd_not_found", "invalid_watch_cwd"}, refused
    # Each error still names the path the way it always did: definitions as typed,
    # Agent Run as resolved, from the caller's home either way.
    assert ("~/missing" if kind != "agent" else str(caller_home / "missing")) in refused["error"]


@pytest.mark.asyncio
async def test_an_unexpected_failure_keeps_its_traceback_in_the_service_log(caplog):
    class Broken:
        @property
        def store(self):
            raise RuntimeError("store exploded")

    controller = SimpleNamespace(scheduled_task_service=Broken())
    with caplog.at_level(logging.ERROR, logger="core.handlers.cli_requests"):
        reply = await handle_control_request(controller, _request("task.pause", ["task", "pause", "t1"]).to_payload())
    assert (reply["payload"]["code"], reply["payload"]["error"]) == ("task_command_failed", "store exploded")
    logged = [record for record in caplog.records if record.exc_info]
    assert [type(record.exc_info[1]) for record in logged] == [RuntimeError]
    assert "task.pause" in logged[0].getMessage()

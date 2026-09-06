from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from core.agent_input import AgentInputMetadata

from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    active_steer_identity,
    reconcile_steer_attempt,
    steer_active_turn,
)
from modules.agents.base import AgentRequest
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import (
    OpenCodeAgent,
    _OpenCodeSteerState,
    _SteeringAwareOpenCodeServer,
)
from modules.agents.opencode.poll_loop import OpenCodePollLoop
from modules.agents.opencode.server import OpenCodePromptRejectedError
from modules.im import MessageContext


STEER_TEXT = "补充：**不要改写**\n```python\nprint('λ')\n```"
ATTEMPT_ID = "atm_1234567890abcdef1234567890abcdef"


@pytest.fixture(params=["plain", "human", "harness"])
def native_input(request, monkeypatch):
    now = datetime(2026, 9, 6, 11, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("core.agent_input.datetime", SimpleNamespace(
        now=lambda: SimpleNamespace(astimezone=lambda: now)
    ))
    if request.param == "plain":
        return None, ""
    if request.param == "human":
        return AgentInputMetadata(user_id="incoming", user_name="Sender"), "[Now: 2026-09-06 11:10:00 UTC+00:00]\n[Sender<incoming>]\n"
    return AgentInputMetadata(source_session_id="source-session"), "[Now: 2026-09-06 11:10:00 UTC+00:00]\nFrom: #source-session\n"


def test_steer_outcomes_are_exhaustive() -> None:
    assert {outcome.value for outcome in SteerOutcome} == {
        "accepted",
        "not_active",
        "refused",
        "unknown",
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _CodexTurnRegistry:
    def __init__(self, base_session_id: str, turn_id: str) -> None:
        self.active_turns = {base_session_id: turn_id}

    def get_active_turn(self, base_session_id: str) -> str | None:
        return self.active_turns.get(base_session_id)


class _CodexSessionManager:
    def __init__(self, base_session_id: str, thread_id: str, cwd: str) -> None:
        self.base_session_id = base_session_id
        self.thread_id = thread_id
        self.cwd = cwd

    def get_thread_id(self, base_session_id: str) -> str | None:
        return self.thread_id if base_session_id == self.base_session_id else None

    def get_cwd(self, base_session_id: str) -> str | None:
        return self.cwd if base_session_id == self.base_session_id else None


class _CodexTransport:
    is_initialized = True

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response or {"turnId": "codex-turn"}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def send_request(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.response


class _ClaudeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.queries = [("primary", "runtime-key")]
        self._transport = SimpleNamespace(end_input=AsyncMock())

    async def query(self, text: str, *, session_id: str) -> None:
        self.queries.append((text, session_id))
        if self.error is not None:
            raise self.error


class _ClaudeSessionHandler:
    def __init__(self, runtime_key: str) -> None:
        self.active_sessions = {runtime_key}
        self.activity_touches: list[str] = []

    def touch_session_activity(self, runtime_key: str) -> None:
        self.activity_touches.append(runtime_key)


class _OpenCodeSessionManager:
    def __init__(self, base_session_id: str, native_session_id: str, cwd: str) -> None:
        self.base_session_id = base_session_id
        self.request_session = (native_session_id, cwd, "session-key")
        self.session_lock = asyncio.Lock()

    def get_request_session(self, base_session_id: str):
        return self.request_session if base_session_id == self.base_session_id else None

    def get_session_lock(self, _base_session_id: str):
        return self.session_lock


class _OpenCodeServer:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        list_error: Exception | None = None,
        messages: list[dict] | None = None,
        status: dict | None = None,
        status_error: Exception | None = None,
        status_responses: list[dict | None] | None = None,
        prompt_started: asyncio.Event | None = None,
        release_prompt: asyncio.Event | None = None,
    ) -> None:
        self.error = error
        self.list_error = list_error
        self.messages = messages or [
            {"info": {"id": "primary-user", "role": "user"}, "parts": []},
        ]
        self.status = status or {"type": "busy"}
        self.status_error = status_error
        self.status_responses = list(status_responses or [])
        self.prompt_started = prompt_started
        self.release_prompt = release_prompt
        self.list_calls = 0
        self.prompt_calls: list[dict] = []
        self.abort_calls: list[tuple] = []
        self.inactive_runs: list[str] = []

    async def prompt_async(self, **kwargs) -> None:
        self.prompt_calls.append(kwargs)
        if self.prompt_started is not None:
            self.prompt_started.set()
        if self.release_prompt is not None:
            await self.release_prompt.wait()
        if self.error is not None:
            raise self.error
        attempt_id = kwargs.get("attempt_id")
        part = {"type": "text", "text": kwargs["text"]}
        if attempt_id:
            part["id"] = f"prt_{attempt_id.removeprefix('atm_')}"
        self.messages.append(
            {
                "info": {
                    "id": f"steer-user-{len(self.prompt_calls)}",
                    "role": "user",
                },
                "parts": [part],
            }
        )

    async def list_messages(self, session_id: str, directory: str) -> list[dict]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.messages)

    async def get_session_status(self, session_id: str, directory: str) -> dict | None:
        if self.status_error is not None:
            raise self.status_error
        if self.status_responses:
            return self.status_responses.pop(0)
        return self.status

    async def get_message(self, session_id: str, message_id: str, directory: str) -> dict:
        return next(
            message
            for message in self.messages
            if message.get("info", {}).get("id") == message_id
        )

    async def abort_session(self, *args) -> bool:
        self.abort_calls.append(args)
        return True

    async def mark_run_inactive(self, session_id: str) -> None:
        self.inactive_runs.append(session_id)


def _primary_request(*, session_id: str = "avibe-session", backend: str) -> AgentRequest:
    context = MessageContext(
        user_id="user",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "agent_session_target": {"id": session_id, "agent_backend": backend},
            "agent_session_id": session_id,
            "workbench_session_id": session_id,
            "turn_token": "logical-turn",
            "agent_runtime_turn_token": "runtime-token",
        },
    )
    return AgentRequest(
        context=context,
        message="primary",
        user_message="primary",
        working_path="/tmp/steering-test",
        base_session_id=session_id,
        composite_session_id="runtime-key",
        session_key="scope-key",
    )


def _controller_with_active_gate(agent, primary: AgentRequest, gate_task: asyncio.Task):
    gate = SimpleNamespace(
        backend=agent.name,
        token="runtime-token",
        runtime_started=True,
        task=gate_task,
        request=primary,
        context=primary.context,
        agent=agent,
    )
    service = SimpleNamespace(agents={agent.name: agent}, _turn_gates={"runtime-key": gate})
    controller = SimpleNamespace(agent_service=service)
    agent.controller = controller
    return controller


def _steer_request(native_turn_id: str, *, logical_turn_id: str = "logical-turn", input_metadata=None) -> SteerRequest:
    return SteerRequest(
        target_session_id="avibe-session",
        expected_logical_turn_id=logical_turn_id,
        expected_native_turn_id=native_turn_id,
        text=STEER_TEXT,
        input_metadata=input_metadata,
    )


async def _held_task() -> asyncio.Task:
    event = asyncio.Event()
    return asyncio.create_task(event.wait())


async def _cancel_tasks(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_codex_steers_expected_active_turn_without_starting_another_turn(native_input) -> None:
    metadata, prefix = native_input
    primary = _primary_request(backend="codex")
    gate_task = await _held_task()
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent.config = SimpleNamespace(include_time_info=True, include_user_info=True)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "codex", "avibe-session")
        assert identity == ("logical-turn", "codex-turn")

        request = _steer_request(identity[1], input_metadata=metadata)
        receipt = await steer_active_turn(controller, "codex", request)
        assert request.text == STEER_TEXT
        assert primary.message == "primary"

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert transport.calls == [
            (
                "turn/steer",
                {
                    "threadId": "codex-thread",
                    "expectedTurnId": "codex-turn",
                    "input": [{"type": "text", "text": prefix + STEER_TEXT}],
                },
            )
        ]
        assert len(agent._turn_registry.active_turns) == 1
        assert primary.working_path in agent._transport_last_activity
        assert not gate_task.done()
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_codex_start_renders_now_after_runtime_prompt_preparation(monkeypatch):
    """Scenario: MESSAGE-DELIVERY-318."""
    current = datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("core.agent_input.datetime", SimpleNamespace(
        now=lambda: SimpleNamespace(astimezone=lambda: current)
    ))
    request = _primary_request(backend="codex")
    request.input_metadata = AgentInputMetadata(user_id="sender", user_name="Sender")
    agent = object.__new__(CodexAgent)
    agent.config = SimpleNamespace(include_time_info=True, include_user_info=True)
    agent.controller = SimpleNamespace()
    agent._prompt_state_agent_session_id = lambda _request: None
    agent._resolve_codex_agent_settings = lambda _request: (None, None, None, None)
    agent._read_persisted_prompt_strategy_marker = Mock(return_value=None)
    agent._write_caller_env_script = Mock()
    agent._turn_registry = SimpleNamespace(
        begin_turn_start=Mock(),
        finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
    )

    async def prepare_runtime(*_args, **_kwargs):
        nonlocal current
        await asyncio.sleep(0)
        current = datetime(2026, 9, 6, 11, 10, tzinfo=timezone.utc)

    agent._inject_thread_developer_instructions = AsyncMock(side_effect=prepare_runtime)
    transport = SimpleNamespace(send_request=AsyncMock(return_value={"turn": {"id": "native-turn"}}))
    await agent._start_turn(transport, request, "native-thread", developer_instructions="runtime instructions")
    agent._inject_thread_developer_instructions.assert_awaited_once()
    params = transport.send_request.await_args.args[1]
    assert params["input"] == [{"type": "text", "text": "[Now: 2026-09-06 11:10:00 UTC+00:00]\n[Sender<sender>]\nprimary"}]
    assert request.message == request.user_message == "primary"


@pytest.mark.anyio
async def test_shared_gate_remains_steerable_after_dispatch_task_returns() -> None:
    primary = _primary_request(backend="codex")
    completed_dispatch = asyncio.create_task(asyncio.sleep(0))
    await completed_dispatch
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, completed_dispatch)

    identity = active_steer_identity(controller, "codex", "avibe-session")
    receipt = await steer_active_turn(controller, "codex", _steer_request("codex-turn"))

    assert identity == ("logical-turn", "codex-turn")
    assert receipt.outcome is SteerOutcome.ACCEPTED
    assert [method for method, _params in transport.calls] == ["turn/steer"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("Codex RPC error: activeTurnNotSteerable"), SteerOutcome.REFUSED),
        (RuntimeError("Codex RPC error: no active turn to steer"), SteerOutcome.NOT_ACTIVE),
        (ConnectionError("Codex app-server transport is not available"), SteerOutcome.REFUSED),
        (TimeoutError("turn/steer timed out"), SteerOutcome.UNKNOWN),
        (ConnectionError("Codex app-server stdout closed"), SteerOutcome.UNKNOWN),
    ],
)
async def test_codex_maps_native_failures_without_turn_start(error: Exception, expected: SteerOutcome) -> None:
    primary = _primary_request(backend="codex")
    gate_task = await _held_task()
    transport = _CodexTransport(error=error)
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        receipt = await steer_active_turn(controller, "codex", _steer_request("codex-turn"))
        assert receipt.outcome is expected
        assert [method for method, _params in transport.calls] == ["turn/steer"]
        if expected is SteerOutcome.UNKNOWN:
            assert primary.working_path in agent._transport_last_activity
        else:
            assert primary.working_path not in getattr(agent, "_transport_last_activity", {})
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_codex_rejects_stale_native_turn_and_unavailable_runtime() -> None:
    primary = _primary_request(backend="codex")
    gate_task = await _held_task()
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        stale = await steer_active_turn(controller, "codex", _steer_request("stale-turn"))
        assert stale.outcome is SteerOutcome.NOT_ACTIVE
        assert transport.calls == []

        agent._transports.clear()
        unavailable = await steer_active_turn(controller, "codex", _steer_request("codex-turn"))
        assert unavailable.outcome is SteerOutcome.REFUSED
        assert unavailable.reason == "runtime_unavailable"
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_claude_uses_one_client_receiver_and_primary_result_owner(native_input) -> None:
    metadata, prefix = native_input
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient()
    agent = object.__new__(ClaudeAgent)
    agent.config = SimpleNamespace(include_time_info=True, include_user_info=True)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    primary_requests = [primary]
    agent._pending_requests = {"runtime-key": primary_requests}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None
        receiver_generation = identity[1]

        request = _steer_request(receiver_generation, input_metadata=metadata)
        receipt = await steer_active_turn(controller, "claude", request)
        assert request.text == STEER_TEXT

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert client.queries == [("primary", "runtime-key"), (prefix + STEER_TEXT, "runtime-key")]
        assert agent.receiver_tasks == {"runtime-key": receiver_task}
        assert agent._pending_requests["runtime-key"] is primary_requests
        assert agent._pending_requests["runtime-key"] == [primary]
        client._transport.end_input.assert_not_awaited()
        # Steering admission is not execution progress and must not refresh the
        # stuck-runtime clock before assistant/tool output arrives.
        assert agent.session_handler.activity_touches == []
        assert not gate_task.done()
        assert not receiver_task.done()
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("Not connected. Call connect() first."), SteerOutcome.REFUSED),
        (TimeoutError("write acknowledgement timed out"), SteerOutcome.UNKNOWN),
        (ConnectionError("Failed to write to process stdin"), SteerOutcome.UNKNOWN),
        (ValueError("unclassified SDK transport failure"), SteerOutcome.UNKNOWN),
    ],
)
async def test_claude_maps_query_failures_at_the_write_boundary(
    error: Exception,
    expected: SteerOutcome,
) -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient(error=error)
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "claude", _steer_request(identity[1]))
        assert receipt.outcome is expected
        assert agent._pending_requests["runtime-key"] == [primary]
        assert agent.receiver_tasks["runtime-key"] is receiver_task
        assert agent.session_handler.activity_touches == []
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.anyio
async def test_claude_refuses_steering_without_ambiguous_input_reconciliation() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient()
    client._transport = None
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None

        receipt = await steer_active_turn(controller, "claude", _steer_request(identity[1]))

        assert receipt.outcome is SteerOutcome.REFUSED
        assert receipt.reason == "native_input_reconciliation_unsupported"
        assert client.queries == [("primary", "runtime-key")]
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.anyio
async def test_claude_preserves_receiver_when_ambiguous_input_half_close_fails() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient(error=TimeoutError("write acknowledgement timed out"))
    client._transport.end_input = AsyncMock(side_effect=RuntimeError("stdin close failed"))
    client.disconnect = AsyncMock()
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None

        receipt = await steer_active_turn(controller, "claude", _steer_request(identity[1]))

        assert receipt.outcome is SteerOutcome.UNKNOWN
        client._transport.end_input.assert_awaited_once_with()
        client.disconnect.assert_not_awaited()
        assert not receiver_task.done()
        assert agent.steering_native_turn_id(
            ActiveSteerTarget(
                runtime_key="runtime-key",
                logical_turn_id="logical-turn",
                context=primary.context,
                agent_request=primary,
                agent=agent,
            )
        ) is None
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.anyio
async def test_claude_accepts_stable_steer_without_closing_native_input() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient()
    client._transport.end_input = AsyncMock(side_effect=RuntimeError("stdin close failed"))
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None

        receipt = await steer_active_turn(
            controller,
            "claude",
            _steer_request(identity[1]),
        )

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert client.queries[-1] == (STEER_TEXT, "runtime-key")
        client._transport.end_input.assert_not_awaited()
        assert not receiver_task.done()
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.anyio
async def test_shared_boundary_finishes_native_reconciliation_before_propagating_cancel() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    class _BlockingClaudeClient(_ClaudeClient):
        async def query(self, text: str, *, session_id: str) -> None:
            self.queries.append((text, session_id))
            query_started.set()
            await release_query.wait()

    client = _BlockingClaudeClient()
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None
        caller = asyncio.create_task(
            steer_active_turn(controller, "claude", _steer_request(identity[1]))
        )
        await query_started.wait()

        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()

        release_query.set()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert agent._steering_generation("runtime-key") == 1
        assert agent._pending_steering_input_state("runtime-key") == "accepted"
        assert agent.claude_sessions["runtime-key"] is client
        assert agent.receiver_tasks["runtime-key"] is receiver_task
        assert agent._pending_requests["runtime-key"] == [primary]
        assert not receiver_task.done()
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.anyio
async def test_claude_rejects_stale_receiver_generation_and_unavailable_runtime() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient()
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = _ClaudeSessionHandler("runtime-key")
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        stale = await steer_active_turn(controller, "claude", _steer_request("stale-receiver"))
        assert stale.outcome is SteerOutcome.NOT_ACTIVE
        assert client.queries == [("primary", "runtime-key")]

        agent.session_handler.active_sessions.clear()
        unavailable = await steer_active_turn(controller, "claude", _steer_request("stale-receiver"))
        assert unavailable.outcome is SteerOutcome.REFUSED
        assert unavailable.reason == "runtime_unavailable"
    finally:
        await _cancel_tasks(gate_task, receiver_task)


def _opencode_agent(primary: AgentRequest, task: asyncio.Task, server: _OpenCodeServer | None):
    agent = object.__new__(OpenCodeAgent)
    agent._active_requests = {primary.base_session_id: task}
    agent._session_manager = _OpenCodeSessionManager(
        primary.base_session_id,
        "opencode-session",
        primary.working_path,
    )
    agent._client_manager = SimpleNamespace(_server_manager=server)
    agent._user_stopped_sessions = set()
    agent._steering_states = {
        primary.base_session_id: _OpenCodeSteerState(
            task=task,
            base_session_id=primary.base_session_id,
            target_session_id="avibe-session",
            logical_turn_id="logical-turn",
            native_session_id="opencode-session",
            directory=primary.working_path,
            agent="build",
            model={"providerID": "openai", "modelID": "gpt-5"},
            reasoning_effort="high",
            system="primary system prompt",
            baseline_message_ids=set(),
        )
    }
    return agent


@pytest.mark.anyio
async def test_opencode_steers_existing_runner_without_abort_or_new_turn(native_input) -> None:
    metadata, prefix = native_input
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer()
    agent = _opencode_agent(primary, gate_task, server)
    agent.config = SimpleNamespace(include_time_info=True, include_user_info=True)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None

        request = _steer_request(identity[1], input_metadata=metadata)
        receipt = await steer_active_turn(controller, "opencode", request)
        assert request.text == STEER_TEXT

        assert receipt.outcome is SteerOutcome.ACCEPTED
        state = agent._steering_states[primary.base_session_id]
        assert state.awaiting_user_text == prefix + STEER_TEXT
        assert state.awaiting_prompt_accepted is True
        assert state.awaiting_prompt_activity_deadline is not None
        assert state.awaiting_prompt_activity_deadline > time.monotonic()
        assert state.awaiting_active_status_observed is False
        assert server.prompt_calls == [
            {
                "session_id": "opencode-session",
                "directory": primary.working_path,
                "text": prefix + STEER_TEXT,
                "agent": "build",
                "model": {"providerID": "openai", "modelID": "gpt-5"},
                "reasoning_effort": "high",
                "system": "primary system prompt",
                "tools": {"question": False, "skill": False},
            }
        ]
        assert server.abort_calls == []
        assert len(agent._active_requests) == 1
        assert not gate_task.done()
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_keeps_reconciliation_armed_after_post_write_idle() -> None:
    """HFR-434: a transient post-write idle stays reconcilable, not refused."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        status_responses=[{"type": "busy"}, {"type": "idle"}],
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None

        receipt = await steer_active_turn(
            controller,
            "opencode",
            _steer_request(identity[1]),
        )

        assert receipt.outcome is SteerOutcome.UNKNOWN
        assert receipt.reason == "native_turn_start_pending"
        assert state.awaiting_user_text == STEER_TEXT
        assert state.awaiting_after_message_ids == {"primary-user"}
        assert state.awaiting_prompt_accepted is True
        assert state.awaiting_prompt_activity_deadline is not None
        assert server.abort_calls == []
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_message_delivery_021_opencode_preserves_native_order_and_recovers_exact_part() -> None:
    """Scenario: MESSAGE-DELIVERY-021."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {"id": "primary-user", "role": "user"},
                "parts": [{"type": "text", "text": "primary"}],
            },
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "parentID": "primary-user",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "primary result"}],
            },
        ]
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    attempt_id = ATTEMPT_ID
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(
            controller,
            "opencode",
            SteerRequest(
                target_session_id="avibe-session",
                expected_logical_turn_id=identity[0],
                expected_native_turn_id=identity[1],
                text=STEER_TEXT,
                attempt_id=attempt_id,
            ),
        )
        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert server.prompt_calls[0]["attempt_id"] == ATTEMPT_ID
        assert "message_id" not in server.prompt_calls[0]
        inserted_user = server.messages[-1]
        server.messages.append(
            {
                "info": {
                    "id": "steer-assistant-1",
                    "role": "assistant",
                    "parentID": inserted_user["info"]["id"],
                    "time": {"completed": 2},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "steer result"}],
            }
        )

        assert [message["info"]["id"] for message in server.messages] == [
            "primary-user",
            "primary-assistant",
            "steer-user-1",
            "steer-assistant-1",
        ]
        assert inserted_user["parts"] == [
            {
                "type": "text",
                "text": STEER_TEXT,
                "id": "prt_1234567890abcdef1234567890abcdef",
            }
        ]
        assert server.messages[-1]["info"]["parentID"] == "steer-user-1"

        reconciled = await reconcile_steer_attempt(
            controller,
            "opencode",
            SteerReconcileRequest(
                target_session_id="avibe-session",
                expected_logical_turn_id=identity[0],
                expected_native_turn_id=identity[1],
                attempt_id=attempt_id,
            ),
        )

        assert reconciled.outcome is SteerOutcome.ACCEPTED
        assert reconciled.details["native_message_id"] == "steer-user-1"
        assert len(server.prompt_calls) == 1
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_stop_waits_for_in_flight_steering_write() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    prompt_started = asyncio.Event()
    release_prompt = asyncio.Event()
    server = _OpenCodeServer(prompt_started=prompt_started, release_prompt=release_prompt)
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    controller.emit_agent_message = AsyncMock()
    agent._get_server = AsyncMock(return_value=server)
    removed_polls: list[str] = []
    agent.sessions = SimpleNamespace(
        get_all_active_polls=lambda: {"opencode-session": object()},
        remove_active_poll=removed_polls.append,
    )
    state = agent._steering_states[primary.base_session_id]
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        steer_task = asyncio.create_task(
            steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        )
        await prompt_started.wait()

        stop_task = asyncio.create_task(agent.handle_stop(primary))
        await asyncio.sleep(0)
        assert server.abort_calls == []
        assert not gate_task.done()

        release_prompt.set()
        receipt = await steer_task
        stopped = await stop_task

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert stopped is True
        assert state.closing is True
        assert server.abort_calls == [("opencode-session", primary.working_path)]
        assert gate_task.cancelled()
        assert server.inactive_runs == ["opencode-session"]
        assert removed_polls == ["opencode-session"]
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_replacement_waits_for_in_flight_steering_write() -> None:
    primary = _primary_request(backend="opencode")
    replacement = _primary_request(backend="opencode")
    gate_task = await _held_task()
    prompt_started = asyncio.Event()
    release_prompt = asyncio.Event()
    server = _OpenCodeServer(prompt_started=prompt_started, release_prompt=release_prompt)
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    session_lock = asyncio.Lock()

    class _ReplacementSessionManager(_OpenCodeSessionManager):
        def get_session_lock(self, _base_session_id):
            return session_lock

        async def wait_for_session_idle(self, *_args):
            return None

        def pop_request_session(self, _base_session_id):
            return None

    agent._session_manager = _ReplacementSessionManager(
        primary.base_session_id,
        "opencode-session",
        primary.working_path,
    )
    agent.controller = controller
    agent._get_server = AsyncMock(return_value=server)
    agent._process_message = AsyncMock(return_value=None)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        steer_task = asyncio.create_task(
            steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        )
        await prompt_started.wait()

        replacement_task = asyncio.create_task(agent.handle_message(replacement))
        await asyncio.sleep(0)
        assert server.abort_calls == []
        assert not gate_task.done()

        release_prompt.set()
        receipt = await steer_task
        await replacement_task

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert server.abort_calls == [("opencode-session", primary.working_path)]
        assert gate_task.cancelled()
        agent._process_message.assert_awaited_once_with(replacement)
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_coordinator_error_aborts_through_steering_owner(
    monkeypatch, native_input,
) -> None:
    metadata, prefix = native_input
    primary = _primary_request(backend="opencode")
    primary.input_metadata = metadata
    poll_started = asyncio.Event()
    fail_poll = asyncio.Event()
    steer_started = asyncio.Event()
    release_steer = asyncio.Event()
    events: list[str] = []

    class _Server:
        prompt_count = 0

        def caller_context_binding_path(self):
            return "/old-avibe-home/runtime/opencode_caller_context.json"

        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return [{"info": {"id": "primary-user", "role": "user"}, "parts": []}]

        async def get_session_status(self, session_id, directory):
            return {"type": "busy"}

        async def prompt_async(self, **kwargs):
            self.prompt_count += 1
            if self.prompt_count == 1:
                assert kwargs["text"] == prefix + primary.message
                events.append("primary")
                return
            steer_started.set()
            await release_steer.wait()
            events.append("steer")

        async def abort_session(self, session_id, directory):
            events.append("abort")
            return True

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, agent):
            return None

        def get_agent_reasoning_effort_from_config(self, agent):
            return None

    class _SessionManager:
        request_session = None

        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "opencode-session"

        def set_request_session(self, base_session_id, session_id, directory, session_key):
            self.request_session = (session_id, directory, session_key)

        def set_agent_session_id(self, base_session_id, agent_session_id):
            return None

        def get_request_session(self, base_session_id):
            return self.request_session

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            return None

        def remove_active_poll(self, session_id):
            return None

    class _PollLoop:
        async def run_prompt_poll(self, *args, **kwargs):
            poll_started.set()
            await fail_poll.wait()
            raise RuntimeError("poll coordinator failed")

    server = _Server()
    controller = SimpleNamespace(
        config=SimpleNamespace(
            platform="avibe",
            reply_enhancements=False,
            show_pages_prompt=False,
            remote_access=None,
            language="en",
            opencode=SimpleNamespace(
                default_model=None,
                default_provider=None,
                default_reasoning_effort=None,
            ),
        ),
        model_hub_runtime=None,
        processing_indicator=SimpleNamespace(snapshot_request=lambda request: {}),
        get_opencode_overrides=lambda context: (None, None, None),
    )
    agent = object.__new__(OpenCodeAgent)
    agent.controller = controller
    agent.config = controller.config
    agent.sessions = _Sessions()
    agent._session_manager = _SessionManager()
    agent._poll_loop = _PollLoop()
    agent._steering_states = {}
    agent._active_requests = {}
    agent._client_manager = SimpleNamespace(_server_manager=server)
    agent._get_server = AsyncMock(return_value=server)
    agent._delete_ack = AsyncMock()
    agent._remove_ack_reaction = AsyncMock()
    agent._prepare_message_with_files = lambda request: request.message
    agent.mark_runtime_turn_started = lambda context, **kwargs: None
    agent.record_model_hub_native_failure = AsyncMock()
    monkeypatch.setattr(
        "modules.agents.opencode.agent.build_system_prompt_injection",
        lambda **kwargs: "system prompt",
    )
    binding_tokens: list[str] = []
    binding_paths: list[str] = []

    def bind_caller_context(*args, **kwargs):
        binding_tokens.append(kwargs["binding_token"])
        binding_paths.append(kwargs["path"])
        return True

    unbound: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "modules.agents.opencode.agent.bind_caller_context_session",
        bind_caller_context,
    )
    monkeypatch.setattr(
        "modules.agents.opencode.agent.unbind_caller_context_session",
        lambda session_id, *, binding_token, path: unbound.append(
            (session_id, binding_token, path)
        ),
    )
    backend_failure = AsyncMock()
    monkeypatch.setattr(
        "modules.agents.opencode.agent.emit_backend_failure",
        backend_failure,
    )

    process_task = asyncio.create_task(agent._process_message(primary))
    agent._active_requests[primary.base_session_id] = process_task
    await poll_started.wait()
    state = agent._steering_states[primary.base_session_id]
    assert state.awaiting_user_text == prefix + primary.message
    assert state.awaiting_prompt_accepted is True
    assert state.awaiting_prompt_activity_deadline is not None
    assert state.awaiting_prompt_activity_deadline > time.monotonic()
    assert state.awaiting_active_status_observed is False
    target = ActiveSteerTarget(
        runtime_key=primary.base_session_id,
        logical_turn_id="logical-turn",
        context=primary.context,
        agent_request=primary,
        agent=agent,
    )
    request = _steer_request(state.native_turn_id)
    steer_task = asyncio.create_task(agent.steer_active_turn(request, target))
    await steer_started.wait()
    fail_poll.set()
    await asyncio.sleep(0)
    assert events == ["primary"]

    release_steer.set()
    receipt = await steer_task
    await process_task

    assert receipt.outcome is SteerOutcome.ACCEPTED
    assert events == ["primary", "steer", "abort"]
    assert len(binding_tokens) == 1
    assert binding_paths == ["/old-avibe-home/runtime/opencode_caller_context.json"]
    assert unbound == [
        (
            "opencode-session",
            binding_tokens[0],
            "/old-avibe-home/runtime/opencode_caller_context.json",
        )
    ]
    backend_failure.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 409])
@pytest.mark.parametrize("reconciliation_fails", [False, True])
async def test_opencode_definitive_start_rejection_reconciles_before_poll_cleanup(
    monkeypatch,
    status: int,
    reconciliation_fails: bool,
) -> None:
    primary = _primary_request(backend="opencode")
    primary.context.platform_specific["delivery_start_attempt_id"] = ATTEMPT_ID
    events: list[str] = []

    class _Server:
        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return []

        async def prompt_async(self, **kwargs):
            events.append("prompt")
            raise OpenCodePromptRejectedError(status, "prompt refused")

        async def abort_session(self, session_id, directory):
            events.append("abort")
            return True

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, agent):
            return None

        def get_agent_reasoning_effort_from_config(self, agent):
            return None

    class _SessionManager:
        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "opencode-session"

        def set_request_session(self, base_session_id, session_id, directory, session_key):
            return None

        def set_agent_session_id(self, base_session_id, agent_session_id):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        def add_active_poll(self, **kwargs):
            events.append("persist_poll")

        def remove_active_poll(self, session_id):
            events.append("remove_poll")

    def reconcile_start_attempt_not_written(turn_id, attempt_id, *, backend):
        events.append(f"reconcile:{turn_id}:{attempt_id}:{backend}")
        if reconciliation_fails:
            raise RuntimeError("storage unavailable")
        return True

    def settle_start_attempt_invalid_input(turn_id, attempt_id, *, backend):
        events.append(f"invalid:{turn_id}:{attempt_id}:{backend}")
        if reconciliation_fails:
            raise RuntimeError("storage unavailable")
        return True

    server = _Server()
    controller = SimpleNamespace(
        config=SimpleNamespace(
            platform="avibe",
            reply_enhancements=False,
            show_pages_prompt=False,
            remote_access=None,
            language="en",
            opencode=SimpleNamespace(
                default_model=None,
                default_provider=None,
                default_reasoning_effort=None,
            ),
        ),
        model_hub_runtime=None,
        processing_indicator=SimpleNamespace(snapshot_request=lambda request: {}),
        get_opencode_overrides=lambda context: (None, None, None),
        session_turns=SimpleNamespace(
            reconcile_start_attempt_not_written=reconcile_start_attempt_not_written,
            settle_start_attempt_invalid_input=settle_start_attempt_invalid_input,
        ),
    )
    agent = object.__new__(OpenCodeAgent)
    agent.controller = controller
    agent.config = controller.config
    agent.sessions = _Sessions()
    agent._session_manager = _SessionManager()
    agent._poll_loop = SimpleNamespace()
    agent._steering_states = {}
    agent._active_requests = {}
    agent._client_manager = SimpleNamespace(_server_manager=server)
    agent._get_server = AsyncMock(return_value=server)
    agent._delete_ack = AsyncMock()
    agent._remove_ack_reaction = AsyncMock()
    agent._prepare_message_with_files = lambda request: request.message
    agent.record_model_hub_native_failure = AsyncMock()
    monkeypatch.setattr(
        "modules.agents.opencode.agent.build_system_prompt_injection",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "modules.agents.opencode.agent.bind_caller_context_session",
        lambda *args, **kwargs: None,
    )
    backend_failure = AsyncMock()
    monkeypatch.setattr(
        "modules.agents.opencode.agent.emit_backend_failure",
        backend_failure,
    )

    await agent._process_message(primary)

    expected_reconciliation = (
        f"invalid:logical-turn:{ATTEMPT_ID}:opencode"
        if status == 400
        else f"reconcile:logical-turn:{ATTEMPT_ID}:opencode"
    )
    assert events[:3] == [
        "persist_poll",
        "prompt",
        expected_reconciliation,
    ]
    assert "abort" not in events
    assert ("remove_poll" in events) is not reconciliation_fails
    backend_failure.assert_awaited_once()


@pytest.mark.anyio
async def test_opencode_ambiguous_start_failure_preserves_recovery_poll(
    monkeypatch,
) -> None:
    primary = _primary_request(backend="opencode")
    primary.context.platform_specific["delivery_start_attempt_id"] = ATTEMPT_ID
    events: list[str] = []

    class _Server:
        async def ensure_running(self):
            return None

        async def list_messages(self, session_id, directory):
            return []

        async def prompt_async(self, **kwargs):
            events.append("prompt")
            raise OSError("connection lost after prompt write")

        async def abort_session(self, session_id, directory):
            events.append("abort")
            raise OSError("abort status unknown")

        async def mark_run_active(self, session_id):
            return None

        async def mark_run_inactive(self, session_id):
            return None

        def get_default_agent_from_config(self):
            return None

        def get_agent_model_from_config(self, agent):
            return None

        def get_agent_reasoning_effort_from_config(self, agent):
            return None

    class _SessionManager:
        async def ensure_working_dir(self, path):
            return None

        async def get_or_create_session_id(self, request, server):
            return "opencode-session"

        def set_request_session(self, base_session_id, session_id, directory, session_key):
            return None

        def set_agent_session_id(self, base_session_id, agent_session_id):
            return None

        def mark_initialized(self, session_id):
            return False

    class _Sessions:
        active = False

        def add_active_poll(self, **kwargs):
            self.active = True
            events.append("persist_poll")

        def remove_active_poll(self, session_id):
            self.active = False
            events.append("remove_poll")

    server = _Server()
    controller = SimpleNamespace(
        config=SimpleNamespace(
            platform="avibe",
            reply_enhancements=False,
            show_pages_prompt=False,
            remote_access=None,
            language="en",
            opencode=SimpleNamespace(
                default_model=None,
                default_provider=None,
                default_reasoning_effort=None,
            ),
        ),
        model_hub_runtime=None,
        processing_indicator=SimpleNamespace(snapshot_request=lambda request: {}),
        get_opencode_overrides=lambda context: (None, None, None),
    )
    agent = object.__new__(OpenCodeAgent)
    agent.controller = controller
    agent.config = controller.config
    agent.sessions = _Sessions()
    agent._session_manager = _SessionManager()
    agent._poll_loop = SimpleNamespace()
    agent._steering_states = {}
    agent._active_requests = {}
    agent._client_manager = SimpleNamespace(_server_manager=server)
    agent._get_server = AsyncMock(return_value=server)
    agent._delete_ack = AsyncMock()
    agent._remove_ack_reaction = AsyncMock()
    agent._prepare_message_with_files = lambda request: request.message
    agent.record_model_hub_native_failure = AsyncMock()
    monkeypatch.setattr(
        "modules.agents.opencode.agent.build_system_prompt_injection",
        lambda **kwargs: "system prompt",
    )
    binding_tokens: list[str] = []

    def bind_caller_context(*args, **kwargs):
        binding_tokens.append(kwargs["binding_token"])
        return True

    unbound: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "modules.agents.opencode.agent.bind_caller_context_session",
        bind_caller_context,
    )
    monkeypatch.setattr(
        "modules.agents.opencode.agent.unbind_caller_context_session",
        lambda session_id, *, binding_token: unbound.append((session_id, binding_token)),
    )
    backend_failure = AsyncMock()
    monkeypatch.setattr(
        "modules.agents.opencode.agent.emit_backend_failure",
        backend_failure,
    )

    await agent._process_message(primary)

    assert events == ["persist_poll", "prompt", "abort"]
    assert agent.sessions.active is True
    assert len(binding_tokens) == 1
    assert unbound == []
    backend_failure.assert_awaited_once()


@pytest.mark.anyio
async def test_opencode_question_abort_claims_terminal_owner_before_steering() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {"id": "assistant-tool", "role": "assistant"},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "question",
                        "state": {"status": "pending"},
                    }
                ],
            }
        ]
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await poll_server.list_messages("opencode-session", primary.working_path)
        receipt = await steer_active_turn(
            controller,
            "opencode",
            _steer_request(state.native_turn_id),
        )
        await poll_server.abort_session("opencode-session", primary.working_path)

        assert messages[-1]["info"]["id"] == "assistant-tool"
        assert state.closing is True
        assert receipt.outcome is SteerOutcome.NOT_ACTIVE
        assert server.prompt_calls == []
        assert server.abort_calls == [("opencode-session", primary.working_path)]
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_poll_owner_observes_accepted_steer_before_terminalizing() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "first response"}],
            }
        ]
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    poll_task = None
    try:
        poll_task = asyncio.create_task(
            poll_server.list_messages("opencode-session", primary.working_path)
        )
        while server.list_calls == 0:
            await asyncio.sleep(0)
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        await asyncio.sleep(0.15)

        # The poll owner now receives the in-progress snapshot once the steered
        # user message is visible while the native session is busy, so the poll
        # loop keeps streaming live activity instead of blocking on busy.
        intermediate = await asyncio.wait_for(poll_task, timeout=1)
        assert intermediate[-1]["info"]["role"] == "user"
        assert state.closing is False
        server.messages.append(
            {
                "info": {
                    "id": "steered-assistant",
                    "role": "assistant",
                    "time": {"completed": 2},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "second response"}],
            }
        )
        server.status = {"type": "idle"}
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert messages[-1]["info"]["id"] == "steered-assistant"
        assert state.closing is True
        assert agent._active_requests == {primary.base_session_id: gate_task}
    finally:
        await _cancel_tasks(*(task for task in (poll_task, gate_task) if task is not None))


@pytest.mark.anyio
async def test_opencode_regular_turn_streams_snapshot_while_busy() -> None:
    """A plain turn (start-armed, like ``_process_message``) must hand the
    in-progress snapshot back to the poll loop while the native session is
    busy, so live tool activity streams instead of blocking until idle."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {"id": "baseline-user", "role": "user"},
                "parts": [{"type": "text", "text": "earlier prompt"}],
            },
            {
                "info": {"id": "primary-user", "role": "user"},
                "parts": [{"type": "text", "text": "primary"}],
            },
            {
                "info": {
                    "id": "running-assistant",
                    "role": "assistant",
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call-bash",
                        "state": {"status": "running", "input": {"command": "ls"}},
                    }
                ],
            },
        ],
        status={"type": "busy"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = {"baseline-user"}
    state.awaiting_user_text = "primary"
    state.awaiting_prompt_accepted = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        intermediate = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert intermediate[-1]["info"]["id"] == "running-assistant"
        assert state.awaiting_active_status_observed is True
        assert state.closing is False

        server.messages.append(
            {
                "info": {
                    "id": "final-assistant",
                    "role": "assistant",
                    "time": {"completed": 2},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "final response"}],
            }
        )
        server.status = {"type": "idle"}
        final = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert final[-1]["info"]["id"] == "final-assistant"
        assert state.closing is True
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_regular_turn_waits_for_unconfirmed_start() -> None:
    """While the inserted user message is not yet visible, a busy session must
    keep waiting within the start-confirmation deadline (the 204-then-busy
    race guard), not hand an empty snapshot back to the poll loop."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {"id": "primary-user", "role": "user"},
                "parts": [{"type": "text", "text": "primary"}],
            }
        ],
        status={"type": "busy"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = {"primary-user"}
    state.awaiting_user_text = "primary"
    state.awaiting_prompt_accepted = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    poll_task = None
    try:
        poll_task = asyncio.create_task(
            poll_server.list_messages("opencode-session", primary.working_path)
        )
        await asyncio.sleep(0.15)

        assert not poll_task.done()
        assert state.awaiting_active_status_observed is False

        server.messages.append(
            {
                "info": {"id": "steer-user-1", "role": "user"},
                "parts": [{"type": "text", "text": "primary"}],
            }
        )
        intermediate = await asyncio.wait_for(poll_task, timeout=1)

        assert intermediate[-1]["info"]["id"] == "steer-user-1"
        assert state.awaiting_active_status_observed is True
    finally:
        await _cancel_tasks(*(task for task in (poll_task, gate_task) if task is not None))


@pytest.mark.anyio
async def test_opencode_restored_busy_snapshot_requires_post_boundary_evidence() -> None:
    """A restored boundary-sampled poll must not receive a busy snapshot that
    only contains boundary messages (the pre-restore final answer): the
    restored poll loop would treat it as the final response and drop the
    still-running poll."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "old-final-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "old answer"}],
            }
        ],
        status={"type": "busy"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = {"old-final-assistant"}
    state.awaiting_user_text = None
    state.restored = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    poll_task = None
    try:
        poll_task = asyncio.create_task(
            poll_server.list_messages("opencode-session", primary.working_path)
        )
        await asyncio.sleep(0.15)

        assert not poll_task.done()
        assert state.closing is False

        server.messages.append(
            {
                "info": {
                    "id": "running-assistant",
                    "role": "assistant",
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call-bash",
                        "state": {"status": "running", "input": {"command": "ls"}},
                    }
                ],
            }
        )
        snapshot = await asyncio.wait_for(poll_task, timeout=1)

        assert snapshot[-1]["info"]["id"] == "running-assistant"
        assert state.closing is False
    finally:
        await _cancel_tasks(*(task for task in (poll_task, gate_task) if task is not None))


@pytest.mark.anyio
async def test_opencode_restored_empty_boundary_still_requires_new_evidence() -> None:
    """When the sampled reconciliation boundary is empty (only pre-prompt
    baseline messages existed at sample time), a busy snapshot holding only
    the baseline final answer must still be gated: an empty boundary is not
    automatic evidence."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "old-final-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "old answer"}],
            }
        ],
        status={"type": "busy"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.baseline_message_ids = {"old-final-assistant"}
    state.awaiting_after_message_ids = set()
    state.awaiting_user_text = None
    state.restored = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    poll_task = None
    try:
        poll_task = asyncio.create_task(
            poll_server.list_messages("opencode-session", primary.working_path)
        )
        await asyncio.sleep(0.15)

        assert not poll_task.done()
        assert state.closing is False

        server.messages.append(
            {
                "info": {
                    "id": "running-assistant",
                    "role": "assistant",
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call-bash",
                        "state": {"status": "running", "input": {"command": "ls"}},
                    }
                ],
            }
        )
        snapshot = await asyncio.wait_for(poll_task, timeout=1)

        assert snapshot[-1]["info"]["id"] == "running-assistant"
        assert state.closing is False
    finally:
        await _cancel_tasks(*(task for task in (poll_task, gate_task) if task is not None))


@pytest.mark.anyio
async def test_opencode_retry_keeps_completed_error_snapshots_gated() -> None:
    """A completed assistant error must stay gated while the native session
    still reports busy/retry: handing it to the poll loop would emit a
    terminal failure (or a competing "continue") while OpenCode's own retry
    is still active."""
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {"id": "primary-user", "role": "user"},
                "parts": [{"type": "text", "text": "primary"}],
            },
            {
                "info": {
                    "id": "failed-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "error": {"name": "ProviderError", "data": {"message": "boom"}},
                },
                "parts": [],
            },
        ],
        status={"type": "retry"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = set()
    state.awaiting_user_text = "primary"
    state.awaiting_prompt_accepted = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    poll_task = None
    try:
        poll_task = asyncio.create_task(
            poll_server.list_messages("opencode-session", primary.working_path)
        )
        await asyncio.sleep(0.15)

        assert not poll_task.done()
        assert state.awaiting_active_status_observed is True
        assert state.closing is False

        server.messages.append(
            {
                "info": {
                    "id": "retried-assistant",
                    "role": "assistant",
                    "time": {"completed": 2},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "recovered"}],
            }
        )
        server.status = {"type": "busy"}
        snapshot = await asyncio.wait_for(poll_task, timeout=1)

        assert snapshot[-1]["info"]["id"] == "retried-assistant"
        assert state.closing is True
    finally:
        await _cancel_tasks(*(task for task in (poll_task, gate_task) if task is not None))


@pytest.mark.anyio
async def test_opencode_idle_post_boundary_error_clears_awaiting_boundary() -> None:
    """A completed retry error must reach the poll owner, not a synthetic idle failure."""

    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {"id": "primary-user", "role": "user"},
                "parts": [{"type": "text", "text": "continue"}],
            },
            {
                "info": {
                    "id": "retry-error",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "error": {"name": "UnknownError", "data": {"message": "tls"}},
                },
                "parts": [],
            },
        ],
        status={"type": "idle"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = {"older-error"}
    state.awaiting_user_text = "continue"
    state.awaiting_active_status_observed = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        snapshot = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )
        assert snapshot[-1]["info"]["id"] == "retry-error"
        assert snapshot[-1]["info"]["error"]["name"] == "UnknownError"
        assert state.awaiting_after_message_ids is None
        assert state.closing is False
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_refuses_after_poll_owner_claims_terminal_result() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status={"type": "idle"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    native_turn_id = state.native_turn_id
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        await poll_server.list_messages("opencode-session", primary.working_path)
        receipt = await steer_active_turn(controller, "opencode", _steer_request(native_turn_id))

        assert state.closing is True
        assert receipt.outcome is SteerOutcome.NOT_ACTIVE
        assert server.prompt_calls == []
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_ambiguous_write_reconciles_unchanged_messages_when_idle() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        error=TimeoutError("response timed out"),
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status_responses=[{"type": "busy"}, {"type": "idle"}],
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert receipt.outcome is SteerOutcome.UNKNOWN
        assert messages[-1]["info"]["id"] == "primary-assistant"
        assert state.awaiting_after_message_ids is None
        assert state.closing is True
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_insert_reconciliation_survives_visible_user_until_idle() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status={"type": "busy"},
    )
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        assert server.messages[-1]["info"]["role"] == "user"

        server.messages.append(
            {
                "info": {
                    "id": "steered-tool-assistant",
                    "role": "assistant",
                    "time": {"completed": 2},
                    "finish": "tool-calls",
                },
                "parts": [],
            }
        )
        server.status = {"type": "idle"}
        messages = await poll_server.list_messages("opencode-session", primary.working_path)

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert messages[-1]["info"]["error"]["name"] == "NativeSessionEndedBeforeResult"
        assert state.awaiting_after_message_ids is None
        assert state.closing is True
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_accepted_prompt_waits_for_delayed_busy_registration() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()

    class _DelayedStartServer(_OpenCodeServer):
        async def list_messages(self, session_id: str, directory: str) -> list[dict]:
            if self.list_calls == 2:
                self.messages.append(
                    {
                        "info": {
                            "id": "follow-up-assistant",
                            "role": "assistant",
                            "time": {"completed": 2},
                            "finish": "stop",
                        },
                        "parts": [{"type": "text", "text": "answered"}],
                    }
                )
            return await super().list_messages(session_id, directory)

    server = _DelayedStartServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "primary"}],
            },
            {
                "info": {"id": "follow-up-user", "role": "user"},
                "parts": [{"type": "text", "text": "follow-up"}],
            },
        ],
        status_responses=[None, {"type": "idle"}, {"type": "busy"}, {"type": "idle"}],
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.baseline_message_ids = {"primary-assistant"}
    state.awaiting_after_message_ids = {"primary-assistant"}
    state.awaiting_user_text = "follow-up"
    state.awaiting_prompt_accepted = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        # The busy snapshot is handed back once the inserted user message is
        # visible; the third list lands the completed final assistant.
        assert messages[-1]["info"]["id"] == "follow-up-assistant"
        assert state.terminal_status_failure_messages is None
        assert state.awaiting_after_message_ids is None
        assert state.closing is True
        assert server.list_calls == 3
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_idle_waits_for_delayed_terminal_message_visibility() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()

    class _DelayedFinalServer(_OpenCodeServer):
        async def list_messages(self, session_id: str, directory: str) -> list[dict]:
            if self.list_calls == 2:
                self.messages.append(
                    {
                        "info": {
                            "id": "follow-up-assistant",
                            "role": "assistant",
                            "time": {"completed": 2},
                            "finish": "stop",
                        },
                        "parts": [{"type": "text", "text": "answered"}],
                    }
                )
            return await super().list_messages(session_id, directory)

    server = _DelayedFinalServer(
        messages=[
            {
                "info": {"id": "follow-up-user", "role": "user"},
                "parts": [{"type": "text", "text": "follow-up"}],
            }
        ],
        status_responses=[{"type": "busy"}, {"type": "idle"}, {"type": "idle"}],
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = set()
    state.awaiting_user_text = "follow-up"
    state.awaiting_prompt_accepted = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        intermediate = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        # Busy + inserted user visible → in-progress snapshot streams back.
        assert intermediate[-1]["info"]["id"] == "follow-up-user"
        assert state.awaiting_active_status_observed is True
        assert state.closing is False

        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert messages[-1]["info"]["id"] == "follow-up-assistant"
        assert state.terminal_status_failure_messages is None
        assert state.awaiting_after_message_ids is None
        assert state.closing is True
        assert server.list_calls == 3
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_accepted_prompt_stays_owned_until_delayed_result() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()

    class _DelayedAcceptedPromptServer(_OpenCodeServer):
        async def list_messages(self, session_id: str, directory: str) -> list[dict]:
            if self.list_calls == 1:
                self.messages.extend(
                    [
                        {
                            "info": {"id": "follow-up-user", "role": "user"},
                            "parts": [{"type": "text", "text": "follow-up"}],
                        },
                        {
                            "info": {
                                "id": "follow-up-assistant",
                                "role": "assistant",
                                "time": {"completed": 2},
                                "finish": "stop",
                            },
                            "parts": [{"type": "text", "text": "answered"}],
                        },
                    ]
                )
            return await super().list_messages(session_id, directory)

    server = _DelayedAcceptedPromptServer(status={"type": "idle"})
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = set()
    state.awaiting_user_text = "follow-up"
    state.awaiting_prompt_accepted = True
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert messages[-1]["info"]["id"] == "follow-up-assistant"
        assert state.terminal_status_failure_messages is None
        assert state.awaiting_after_message_ids is None
        assert state.closing is True
        assert server.list_calls == 2
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_accepted_prompt_without_activity_is_bounded() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(status={"type": "idle"})
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = set()
    state.awaiting_user_text = "follow-up"
    state.awaiting_prompt_accepted = True
    state.awaiting_prompt_activity_deadline = time.monotonic() - 1
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await poll_server.list_messages(
            "opencode-session",
            primary.working_path,
        )

        assert messages[-1]["info"]["error"]["name"] == (
            "NativeSessionEndedBeforeResult"
        )
        assert state.awaiting_after_message_ids is None
        assert state.closing is True
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_requires_final_assistant_after_inserted_user() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()

    class _RacingOpenCodeServer(_OpenCodeServer):
        async def prompt_async(self, **kwargs) -> None:
            self.prompt_calls.append(kwargs)
            self.messages.extend(
                [
                    {
                        "info": {
                            "id": "raced-primary-assistant",
                            "role": "assistant",
                            "time": {"completed": 1},
                            "finish": "stop",
                        },
                        "parts": [{"type": "text", "text": "primary"}],
                    },
                    {
                        "info": {"id": "steer-user", "role": "user"},
                        "parts": [{"type": "text", "text": kwargs["text"]}],
                    },
                ]
            )

    server = _RacingOpenCodeServer(status={"type": "busy"})
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    state = agent._steering_states[primary.base_session_id]
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        server.status = {"type": "idle"}

        messages = await poll_server.list_messages("opencode-session", primary.working_path)

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert messages[-1]["info"]["error"]["name"] == "NativeSessionEndedBeforeResult"
        assert state.closing is True
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_poll_keeps_owner_until_failed_status_probe_recovers() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status={"type": "idle"},
        status_error=ConnectionError("status unavailable"),
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    poll_task = asyncio.create_task(
        poll_server.list_messages("opencode-session", primary.working_path)
    )
    try:
        while server.list_calls < 2:
            await asyncio.sleep(0.05)
        assert not poll_task.done()
        assert state.closing is False

        server.status_error = None
        messages = await asyncio.wait_for(poll_task, timeout=1)

        assert messages[-1]["info"]["id"] == "primary-assistant"
        assert state.closing is True
    finally:
        await _cancel_tasks(poll_task, gate_task)


@pytest.mark.anyio
async def test_opencode_status_reconciliation_failures_are_bounded() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status_error=ConnectionError("status unavailable"),
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = {"primary-assistant"}
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )
        first_failure_id = messages[-1]["info"]["id"]
        await poll_server.prompt_async(
            session_id="opencode-session",
            directory=primary.working_path,
            text="continue",
        )
        repeated = await poll_server.list_messages("opencode-session", primary.working_path)

        assert server.list_calls == 3
        assert state.closing is True
        assert repeated[-1]["info"]["id"] != first_failure_id
        assert repeated[-1]["info"]["error"] == {
            "name": "StatusReconciliationError",
            "data": {"message": "status unavailable"},
        }
        assert server.prompt_calls == []
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_normal_poll_delivers_terminal_reconciliation_failure(
    monkeypatch,
) -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status_error=ConnectionError("status unavailable"),
    )
    agent = _opencode_agent(primary, gate_task, server)
    agent.controller = SimpleNamespace(config=SimpleNamespace(language="zh"))
    agent.opencode_config = SimpleNamespace(error_retry_limit=1)
    agent.record_model_hub_native_failure = AsyncMock()
    emit_failure = AsyncMock()
    monkeypatch.setattr("modules.agents.opencode.poll_loop.emit_backend_failure", emit_failure)

    original_sleep = asyncio.sleep

    async def _fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr("modules.agents.opencode.poll_loop.asyncio.sleep", _fast_sleep)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = {"primary-assistant"}
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        result = await asyncio.wait_for(
            OpenCodePollLoop(agent).run_prompt_poll(
                primary,
                poll_server,
                "opencode-session",
                agent_to_use="build",
                model_dict={"providerID": "openai", "modelID": "gpt-5"},
                reasoning_effort="high",
                baseline_message_ids=set(),
            ),
            timeout=1,
        )

        assert result == (None, False)
        assert server.prompt_calls == []
        emit_failure.assert_awaited_once()
        assert emit_failure.await_args.kwargs["failure_id"].endswith(":1")
        assert emit_failure.await_args.kwargs["display_text"] == (
            "OpenCode 错误：StatusReconciliationError - status unavailable"
        )
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_final_output_survives_unavailable_status_probe() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
        status_error=ConnectionError("status unavailable"),
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = set()
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )

        assert messages[-1]["info"]["id"] == "primary-assistant"
        assert state.terminal_status_failure_messages is None
        assert state.closing is True
        assert server.list_calls == 3
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_restored_opencode_status_reconciliation_becomes_terminal_snapshot() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            },
            {
                "info": {"id": "steer-user", "role": "user", "time": {}},
                "parts": [{"type": "text", "text": "follow-up"}],
            },
        ],
        status_error=ConnectionError("status unavailable"),
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.restored = True
    state.awaiting_after_message_ids = {"primary-assistant"}
    poll_server = _SteeringAwareOpenCodeServer(server, state)
    try:
        messages = await asyncio.wait_for(
            poll_server.list_messages("opencode-session", primary.working_path),
            timeout=1,
        )
        repeated = await poll_server.list_messages(
            "opencode-session",
            primary.working_path,
        )

        assert state.closing is True
        assert server.list_calls == 3
        assert repeated == messages
        assert messages[-1]["info"]["finish"] == "tool-calls"
        assert messages[-1]["info"]["error"] == {
            "name": "StatusReconciliationError",
            "data": {"message": "status unavailable"},
        }
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OpenCodePromptRejectedError(409, "busy input refused"), SteerOutcome.REFUSED),
        (OpenCodePromptRejectedError(404, "session missing"), SteerOutcome.NOT_ACTIVE),
        (TimeoutError("response timed out"), SteerOutcome.UNKNOWN),
        (aiohttp.ServerDisconnectedError("response disconnected"), SteerOutcome.UNKNOWN),
    ],
)
async def test_opencode_maps_async_prompt_failures(error: Exception, expected: SteerOutcome) -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer(error=error)
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))
        assert receipt.outcome is expected
        assert server.abort_calls == []
        assert len(server.prompt_calls) == 1
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_connector_refusal_preserves_reconciliation_boundary() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    connector_error = aiohttp.ClientConnectorError(
        ConnectionKey("127.0.0.1", 4096, False, False, None, None, None),
        OSError("connect failed"),
    )
    server = _OpenCodeServer(
        error=connector_error,
        messages=[
            {
                "info": {
                    "id": "primary-assistant",
                    "role": "assistant",
                    "time": {"completed": 1},
                    "finish": "stop",
                },
                "parts": [{"type": "text", "text": "done"}],
            }
        ],
    )
    agent = _opencode_agent(primary, gate_task, server)
    state = agent._steering_states[primary.base_session_id]
    state.awaiting_after_message_ids = set()
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None

        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))

        assert receipt.outcome is SteerOutcome.REFUSED
        assert receipt.reason == "runtime_unavailable"
        assert state.awaiting_after_message_ids == set()
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_opencode_rejects_stale_runner_and_unavailable_runtime() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer()
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        stale = await steer_active_turn(controller, "opencode", _steer_request("stale-runner"))
        assert stale.outcome is SteerOutcome.NOT_ACTIVE
        assert server.prompt_calls == []

        agent._client_manager._server_manager = None
        identity = agent.steering_native_turn_id(
            SimpleNamespace(runtime_key="runtime-key", agent_request=primary)
        )
        unavailable = await steer_active_turn(controller, "opencode", _steer_request(identity))
        assert unavailable.outcome is SteerOutcome.REFUSED
        assert unavailable.reason == "runtime_unavailable"
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_shared_guard_rejects_stale_logical_or_missing_active_turn() -> None:
    primary = _primary_request(backend="codex")
    gate_task = await _held_task()
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        stale = await steer_active_turn(
            controller,
            "codex",
            _steer_request("codex-turn", logical_turn_id="stale-logical"),
        )
        assert stale.outcome is SteerOutcome.NOT_ACTIVE
        assert transport.calls == []

        controller.agent_service._turn_gates.clear()
        missing = await steer_active_turn(controller, "codex", _steer_request("codex-turn"))
        assert missing.outcome is SteerOutcome.NOT_ACTIVE
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_shared_guard_requires_the_avibe_session_identity() -> None:
    primary = _primary_request(session_id="avibe-session", backend="codex")
    primary.base_session_id = "backend-anchor"
    gate_task = await _held_task()
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    request = _steer_request("codex-turn")
    request = SteerRequest(
        target_session_id="backend-anchor",
        expected_logical_turn_id=request.expected_logical_turn_id,
        expected_native_turn_id=request.expected_native_turn_id,
        text=request.text,
    )
    try:
        receipt = await steer_active_turn(controller, "codex", request)
        assert receipt.outcome is SteerOutcome.NOT_ACTIVE
        assert transport.calls == []
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_shared_service_uses_the_active_runtime_generation_after_registry_refresh() -> None:
    primary = _primary_request(backend="codex")
    gate_task = await _held_task()
    active_transport = _CodexTransport()
    active_agent = object.__new__(CodexAgent)
    active_agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    active_agent._session_mgr = _CodexSessionManager(
        primary.base_session_id,
        "codex-thread",
        primary.working_path,
    )
    active_agent._transports = {primary.working_path: active_transport}
    controller = _controller_with_active_gate(active_agent, primary, gate_task)

    replacement_agent = object.__new__(CodexAgent)
    replacement_agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "replacement-turn")
    replacement_agent._session_mgr = _CodexSessionManager(
        primary.base_session_id,
        "replacement-thread",
        primary.working_path,
    )
    replacement_agent._transports = {}
    controller.agent_service.agents["codex"] = replacement_agent
    try:
        identity = active_steer_identity(controller, "codex", "avibe-session")
        assert identity == ("logical-turn", "codex-turn")
        receipt = await steer_active_turn(controller, "codex", _steer_request("codex-turn"))
        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert [method for method, _params in active_transport.calls] == ["turn/steer"]
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_shared_guard_disambiguates_concurrent_gates_by_expected_turn_identity() -> None:
    first = _primary_request(backend="codex")
    second = _primary_request(backend="codex")
    second.context.platform_specific["turn_token"] = "logical-turn-2"
    second.context.platform_specific["agent_runtime_turn_token"] = "runtime-token-2"
    first_task = await _held_task()
    second_task = await _held_task()

    first_transport = _CodexTransport(response={"turnId": "codex-turn-1"})
    first_agent = object.__new__(CodexAgent)
    first_agent._turn_registry = _CodexTurnRegistry(first.base_session_id, "codex-turn-1")
    first_agent._session_mgr = _CodexSessionManager(first.base_session_id, "codex-thread-1", first.working_path)
    first_agent._transports = {first.working_path: first_transport}

    second_transport = _CodexTransport(response={"turnId": "codex-turn-2"})
    second_agent = object.__new__(CodexAgent)
    second_agent._turn_registry = _CodexTurnRegistry(second.base_session_id, "codex-turn-2")
    second_agent._session_mgr = _CodexSessionManager(
        second.base_session_id,
        "codex-thread-2",
        second.working_path,
    )
    second_agent._transports = {second.working_path: second_transport}

    gates = {
        "main": SimpleNamespace(
            backend="codex",
            token="runtime-token",
            runtime_started=True,
            task=first_task,
            request=first,
            context=first.context,
            agent=first_agent,
        ),
        "named-subagent": SimpleNamespace(
            backend="codex",
            token="runtime-token-2",
            runtime_started=True,
            task=second_task,
            request=second,
            context=second.context,
            agent=second_agent,
        ),
    }
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(agents={"codex": first_agent}, _turn_gates=gates)
    )
    first_agent.controller = controller
    second_agent.controller = controller
    try:
        assert active_steer_identity(controller, "codex", "avibe-session") is None
        identity = active_steer_identity(
            controller,
            "codex",
            "avibe-session",
            expected_logical_turn_id="logical-turn-2",
        )
        receipt = await steer_active_turn(
            controller,
            "codex",
            _steer_request("codex-turn-2", logical_turn_id="logical-turn-2"),
        )

        assert identity == ("logical-turn-2", "codex-turn-2")
        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert first_transport.calls == []
        assert [method for method, _params in second_transport.calls] == ["turn/steer"]
    finally:
        await _cancel_tasks(first_task, second_task)


@pytest.mark.anyio
async def test_shared_service_prefers_explicit_session_target_over_legacy_id() -> None:
    primary = _primary_request(session_id="current-session", backend="codex")
    primary.context.platform_specific["agent_session_id"] = "stale-session"
    gate_task = await _held_task()
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(
        primary.base_session_id,
        "codex-thread",
        primary.working_path,
    )
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        assert active_steer_identity(controller, "codex", "stale-session") is None
        assert active_steer_identity(controller, "codex", "current-session") == (
            "logical-turn",
            "codex-turn",
        )
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.anyio
async def test_shared_service_refuses_an_unavailable_backend() -> None:
    controller = SimpleNamespace(agent_service=SimpleNamespace(agents={}, _turn_gates={}))
    receipt = await steer_active_turn(controller, "claude", _steer_request("native-turn"))
    assert receipt.outcome is SteerOutcome.REFUSED
    assert receipt.reason == "runtime_unavailable"

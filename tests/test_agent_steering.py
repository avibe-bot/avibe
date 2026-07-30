from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from core.services.agent_steering import (
    SteerOutcome,
    SteerRequest,
    active_steer_identity,
    steer_active_turn,
)
from modules.agents.base import AgentRequest
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.server import OpenCodePromptRejectedError
from modules.im import MessageContext


STEER_TEXT = "补充：**不要改写**\n```python\nprint('λ')\n```"


def test_steer_outcomes_are_exhaustive() -> None:
    assert {outcome.value for outcome in SteerOutcome} == {
        "accepted",
        "not_active",
        "refused",
        "unknown",
    }


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

    async def query(self, text: str, *, session_id: str) -> None:
        self.queries.append((text, session_id))
        if self.error is not None:
            raise self.error


class _OpenCodeSessionManager:
    def __init__(self, base_session_id: str, native_session_id: str, cwd: str) -> None:
        self.base_session_id = base_session_id
        self.request_session = (native_session_id, cwd, "session-key")

    def get_request_session(self, base_session_id: str):
        return self.request_session if base_session_id == self.base_session_id else None


class _OpenCodeServer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.prompt_calls: list[dict] = []
        self.abort_calls: list[tuple] = []

    async def prompt_async(self, **kwargs) -> None:
        self.prompt_calls.append(kwargs)
        if self.error is not None:
            raise self.error

    async def abort_session(self, *args) -> bool:
        self.abort_calls.append(args)
        return True


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
    )
    service = SimpleNamespace(agents={agent.name: agent}, _turn_gates={"runtime-key": gate})
    controller = SimpleNamespace(agent_service=service)
    agent.controller = controller
    return controller


def _steer_request(native_turn_id: str, *, logical_turn_id: str = "logical-turn") -> SteerRequest:
    return SteerRequest(
        target_session_id="avibe-session",
        expected_logical_turn_id=logical_turn_id,
        expected_native_turn_id=native_turn_id,
        text=STEER_TEXT,
    )


async def _held_task() -> asyncio.Task:
    event = asyncio.Event()
    return asyncio.create_task(event.wait())


async def _cancel_tasks(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_codex_steers_expected_active_turn_without_starting_another_turn() -> None:
    primary = _primary_request(backend="codex")
    gate_task = await _held_task()
    transport = _CodexTransport()
    agent = object.__new__(CodexAgent)
    agent._turn_registry = _CodexTurnRegistry(primary.base_session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(primary.base_session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: transport}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "codex", "avibe-session")
        assert identity == ("logical-turn", "codex-turn")

        receipt = await steer_active_turn(controller, "codex", _steer_request(identity[1]))

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert transport.calls == [
            (
                "turn/steer",
                {
                    "threadId": "codex-thread",
                    "expectedTurnId": "codex-turn",
                    "input": [{"type": "text", "text": STEER_TEXT}],
                },
            )
        ]
        assert len(agent._turn_registry.active_turns) == 1
        assert not gate_task.done()
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("Codex RPC error: activeTurnNotSteerable"), SteerOutcome.REFUSED),
        (RuntimeError("Codex RPC error: no active turn to steer"), SteerOutcome.NOT_ACTIVE),
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
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_claude_uses_one_client_receiver_and_primary_result_owner() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient()
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = SimpleNamespace(active_sessions={"runtime-key"})
    primary_requests = [primary]
    agent._pending_requests = {"runtime-key": primary_requests}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None
        receiver_generation = identity[1]

        receipt = await steer_active_turn(controller, "claude", _steer_request(receiver_generation))

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert client.queries == [("primary", "runtime-key"), (STEER_TEXT, "runtime-key")]
        assert agent.receiver_tasks == {"runtime-key": receiver_task}
        assert agent._pending_requests["runtime-key"] is primary_requests
        assert agent._pending_requests["runtime-key"] == [primary]
        assert not gate_task.done()
        assert not receiver_task.done()
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.asyncio
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
    agent.session_handler = SimpleNamespace(active_sessions={"runtime-key"})
    agent._pending_requests = {"runtime-key": [primary]}
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "claude", "avibe-session")
        assert identity is not None
        receipt = await steer_active_turn(controller, "claude", _steer_request(identity[1]))
        assert receipt.outcome is expected
        assert agent._pending_requests["runtime-key"] == [primary]
        assert agent.receiver_tasks["runtime-key"] is receiver_task
    finally:
        await _cancel_tasks(gate_task, receiver_task)


@pytest.mark.asyncio
async def test_claude_rejects_stale_receiver_generation_and_unavailable_runtime() -> None:
    primary = _primary_request(backend="claude")
    gate_task = await _held_task()
    receiver_task = await _held_task()
    client = _ClaudeClient()
    agent = object.__new__(ClaudeAgent)
    agent.claude_sessions = {"runtime-key": client}
    agent.receiver_tasks = {"runtime-key": receiver_task}
    agent.session_handler = SimpleNamespace(active_sessions={"runtime-key"})
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
    return agent


@pytest.mark.asyncio
async def test_opencode_steers_existing_runner_without_abort_or_new_turn() -> None:
    primary = _primary_request(backend="opencode")
    gate_task = await _held_task()
    server = _OpenCodeServer()
    agent = _opencode_agent(primary, gate_task, server)
    controller = _controller_with_active_gate(agent, primary, gate_task)
    try:
        identity = active_steer_identity(controller, "opencode", "avibe-session")
        assert identity is not None

        receipt = await steer_active_turn(controller, "opencode", _steer_request(identity[1]))

        assert receipt.outcome is SteerOutcome.ACCEPTED
        assert server.prompt_calls == [
            {
                "session_id": "opencode-session",
                "directory": primary.working_path,
                "text": STEER_TEXT,
            }
        ]
        assert server.abort_calls == []
        assert len(agent._active_requests) == 1
        assert not gate_task.done()
    finally:
        await _cancel_tasks(gate_task)


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_shared_service_refuses_an_unavailable_backend() -> None:
    controller = SimpleNamespace(agent_service=SimpleNamespace(agents={}, _turn_gates={}))
    receipt = await steer_active_turn(controller, "claude", _steer_request("native-turn"))
    assert receipt.outcome is SteerOutcome.REFUSED
    assert receipt.reason == "runtime_unavailable"

"""Native-baseline lifecycle boundaries, using the production agent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from modules.agents.codex.agent import CodexAgent, CodexPromptRefreshUnavailableError


PROMPT_A = "完整的 Avibe baseline A.\nKeep the user's preferences."
PROMPT_B = "完整的 Avibe baseline B.\nA removed capability is unavailable."


def _request():
    return SimpleNamespace(
        session_key="contract",
        base_session_id="contract",
        composite_session_id="avibe:contract",
        working_path="/tmp/contract",
        subagent_name=None,
        subagent_model=None,
        subagent_reasoning_effort=None,
        ack_message_id=None,
        context=SimpleNamespace(platform_specific={}),
    )


def _agent(marker):
    agent = object.__new__(CodexAgent)
    agent.controller = SimpleNamespace(get_codex_overrides=Mock(return_value=(None, None, None)))
    agent.codex_config = SimpleNamespace(default_model=None)
    native_binding = {}

    def persist(*_args, **kwargs):
        marker.clear()
        marker.update(kwargs["value"] or {})
        return True

    agent.sessions = SimpleNamespace(
        get_agent_session_id=lambda *_: native_binding.get("id"),
        get_agent_session_runtime_marker=lambda *_args, **_kwargs: dict(marker) or None,
        set_agent_session_runtime_marker=Mock(side_effect=persist),
    )
    agent.ensure_agent_session_id = Mock(return_value="contract-session")
    agent.bind_agent_session_id = Mock(
        side_effect=lambda _request, thread: native_binding.update(id=thread) or "contract-session"
    )
    agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
    agent._inject_caller_env_config = Mock(return_value=("", False))
    agent._caller_env_for_request = Mock(return_value={})
    agent._build_input = Mock(return_value=[{"type": "text", "text": "Hello", "text_elements": []}])
    agent._write_caller_env_script = Mock()
    agent._turn_registry = SimpleNamespace(
        begin_turn_start=Mock(),
        get_bootstrapped_turn_id=Mock(return_value=None),
        finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
    )
    return agent


def _transport(config=None):
    async def rpc(method, params):
        if method == "config/read":
            return {"config": config or {}}
        if method in {"thread/start", "thread/resume", "thread/fork"}:
            return {"thread": {"id": "thread-contract"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-contract"}}
        assert method == "thread/inject_items", method
        return {}

    return SimpleNamespace(send_request=AsyncMock(side_effect=rpc))


@pytest.mark.asyncio
async def test_new_thread_native_baseline_deduplicates_and_records_catalog():
    marker = {}
    agent = _agent(marker)
    request = _request()
    request.skill_catalog_observation = {"catalog": "candidate"}
    transport = _transport({"developer_instructions": "User's native instructions.\n"})
    with patch("core.skill_observability.accept_catalog") as accept:
        thread_id = await agent._start_or_resume_thread(transport, request, developer_instructions=PROMPT_A)
        await agent._start_turn(transport, request, thread_id, developer_instructions=PROMPT_A)
        await agent._start_turn(transport, request, thread_id, developer_instructions=PROMPT_A)
    calls = transport.send_request.await_args_list
    assert [item.args[0] for item in calls] == ["config/read", "thread/start", "turn/start", "turn/start"]
    assert calls[0] == call("config/read", {"includeLayers": False, "cwd": request.working_path})
    assert calls[1].args[1]["developerInstructions"] == "User's native instructions.\n\n\n" + PROMPT_A
    assert marker == {
        "thread_id": thread_id,
        "strategy": "fallback",
        "sha256": agent._prompt_fingerprint(PROMPT_A),
    }
    accept.assert_called_once_with(
        agent.controller, request.context, request.skill_catalog_observation, backend="codex"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [None, [], {"developer_instructions": ["invalid"]}])
async def test_unreadable_native_configuration_fails_before_thread_mutation(config):
    agent = _agent({})
    transport = SimpleNamespace(send_request=AsyncMock(return_value={"config": config}))
    with pytest.raises(CodexPromptRefreshUnavailableError):
        await agent._start_thread(transport, _request(), developer_instructions=PROMPT_A)
    assert [item.args[0] for item in transport.send_request.await_args_list] == ["config/read"]
    agent.bind_agent_session_id.assert_not_called()
    agent.sessions.set_agent_session_runtime_marker.assert_not_called()


@pytest.mark.asyncio
async def test_native_marker_failure_repairs_before_dispatch_without_duplicate_injection():
    marker = {}
    agent = _agent(marker)
    persist = agent.sessions.set_agent_session_runtime_marker.side_effect
    agent.sessions.set_agent_session_runtime_marker.side_effect = RuntimeError("storage unavailable")
    request = _request()
    transport = _transport()
    with pytest.raises(CodexPromptRefreshUnavailableError, match="Could not persist"):
        await agent._start_thread(transport, request, developer_instructions=PROMPT_A)
    assert not marker
    assert agent._thread_prompt_strategies[request.base_session_id][1] == "injected_pending_persist"

    agent.sessions.set_agent_session_runtime_marker.side_effect = persist
    await agent._start_turn(transport, request, "thread-contract", developer_instructions=PROMPT_A)
    assert [item.args[0] for item in transport.send_request.await_args_list] == [
        "config/read", "thread/start", "turn/start"
    ]
    assert marker["sha256"] == agent._prompt_fingerprint(PROMPT_A)
    assert not agent._thread_unpersisted_prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_resume_preserves_history_fingerprint_even_after_native_override(changed):
    marker = {"thread_id": "thread-contract", "strategy": "fallback", "sha256": CodexAgent._prompt_fingerprint(PROMPT_A)}
    agent = _agent(marker)
    agent.sessions.get_agent_session_id = Mock(return_value="thread-contract")
    agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
    request = _request()
    transport = _transport()
    current = PROMPT_B if changed else PROMPT_A

    thread_id = await agent._start_or_resume_thread(transport, request, developer_instructions=current)
    assert marker["sha256"] == agent._prompt_fingerprint(PROMPT_A)
    assert not getattr(agent, "_thread_developer_instructions", {})
    assert transport.send_request.await_args.args[1]["developerInstructions"] == current
    await agent._start_turn(transport, request, thread_id, developer_instructions=current)
    methods = [item.args[0] for item in transport.send_request.await_args_list]
    assert methods.count("thread/inject_items") == int(changed)
    assert marker["sha256"] == agent._prompt_fingerprint(current)


@pytest.mark.asyncio
async def test_fork_sets_target_baseline_but_inherits_source_history_identity():
    marker = {}
    agent = _agent(marker)
    agent._fork_source_prompt_state = Mock(return_value=("fallback", agent._prompt_fingerprint(PROMPT_A), PROMPT_A))
    agent._should_rollback_forked_running_turn = AsyncMock(return_value=False)
    agent._inject_forked_session_correction = AsyncMock()
    agent._mark_fork_correction_pending = Mock()
    agent._clear_fork_correction_pending = Mock()
    request = _request()
    transport = _transport()
    thread_id = await agent._fork_thread(
        transport, request, {"source_native_session_id": "source"}, developer_instructions=PROMPT_B
    )
    assert marker["sha256"] == agent._prompt_fingerprint(PROMPT_A)
    assert transport.send_request.await_args.args[1]["developerInstructions"] == PROMPT_B
    await agent._start_turn(transport, request, thread_id, developer_instructions=PROMPT_B)
    assert marker["sha256"] == agent._prompt_fingerprint(PROMPT_B)
    assert [item.args[0] for item in transport.send_request.await_args_list] == [
        "config/read", "thread/fork", "thread/inject_items", "turn/start"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [PROMPT_A, None, ""])
@pytest.mark.parametrize("recover", [False, True])
async def test_dispatch_renders_once_even_for_empty_prompt_and_transport_recovery(prompt, recover):
    agent = _agent({})
    request = _request()
    agent._session_locks = {}
    agent._session_mgr = SimpleNamespace(
        set_session_key=Mock(), set_cwd=Mock(), get_thread_id=Mock(return_value=None)
    )
    agent._turn_registry.remember_request = Mock()
    agent._turn_registry.get_active_turn = Mock(return_value=None)
    agent._delete_ack = AsyncMock()
    agent._touch_transport_activity = Mock()
    transport = _transport()
    fresh = _transport()
    agent._get_or_create_transport = AsyncMock(side_effect=[transport, fresh])
    agent._drop_transport_after_failure = AsyncMock()
    agent._refresh_thread_developer_instructions_if_needed = AsyncMock()
    agent._bind_runtime_agent_session_id = Mock()
    agent._build_thread_developer_instructions = AsyncMock(return_value=prompt)
    agent._start_or_resume_thread = AsyncMock(
        side_effect=[ConnectionError("Codex app-server transport is not available"), "thread-contract"]
        if recover else ["thread-contract"]
    )
    agent._start_turn = AsyncMock()
    await agent.handle_message(request)
    agent._build_thread_developer_instructions.assert_awaited_once_with(request)
    assert agent._start_or_resume_thread.await_args_list == [
        call(transport, request, developer_instructions=prompt),
        *([call(fresh, request, developer_instructions=prompt)] if recover else []),
    ]
    agent._start_turn.assert_awaited_once_with(
        fresh if recover else transport, request, "thread-contract", developer_instructions=prompt
    )

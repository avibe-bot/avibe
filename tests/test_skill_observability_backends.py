"""Observe positively accepted prompt writes, not attempted or cached prompts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import _OpenCodeSteerState, _SteeringAwareOpenCodeServer


@pytest.mark.asyncio
async def test_codex_counts_only_positive_injection_acknowledgements(monkeypatch):
    agent = object.__new__(CodexAgent)
    agent.controller = SimpleNamespace()
    agent._read_persisted_prompt_strategy_marker = Mock(return_value=None)
    agent._persist_prompt_strategy = Mock(return_value=True)
    agent._thread_prompt_strategies = {}
    request = SimpleNamespace(
        base_session_id="ses", context=SimpleNamespace(), skill_catalog_observation={"entries": []}
    )
    transport = SimpleNamespace(send_request=AsyncMock(side_effect=TimeoutError))
    accepted = Mock()
    monkeypatch.setattr("core.skill_observability.accept_catalog", accepted)
    with pytest.raises(TimeoutError):
        await agent._inject_thread_developer_instructions(transport, request, "thread", "instructions")
    accepted.assert_not_called()
    transport.send_request.side_effect = None
    await agent._inject_thread_developer_instructions(transport, request, "thread", "instructions")
    accepted.assert_called_once_with(
        agent.controller, request.context, request.skill_catalog_observation, backend="codex"
    )
    # A fresh native thread is another actual exposure even on one retried request.
    await agent._inject_thread_developer_instructions(transport, request, "new-thread", "instructions")
    assert accepted.call_count == 2


@pytest.mark.asyncio
async def test_opencode_retry_counts_only_matching_accepted_catalog():
    accepted = Mock()
    state = _OpenCodeSteerState(
        task=asyncio.current_task(),
        base_session_id="base",
        target_session_id="target",
        logical_turn_id="turn",
        native_session_id="native",
        directory="/fixture",
        agent=None,
        model=None,
        reasoning_effort=None,
        system="catalog prompt",
        baseline_message_ids=set(),
        catalog_accepted=accepted,
    )
    native = SimpleNamespace(prompt_async=AsyncMock(side_effect=TimeoutError))
    wrapped = _SteeringAwareOpenCodeServer(native, state)
    kwargs = {"system": state.system, "awaiting_after_ids": set(), "text": "continue"}
    with pytest.raises(TimeoutError):
        await wrapped.prompt_async(**kwargs)
    accepted.assert_not_called()
    native.prompt_async.side_effect = None
    await wrapped.prompt_async(**kwargs)
    accepted.assert_called_once()
    await wrapped.prompt_async(**{**kwargs, "system": None})
    accepted.assert_called_once()
    state.terminal_status_failure_messages = [{"info": {"id": "terminal"}}]
    await wrapped.prompt_async(**kwargs)
    accepted.assert_called_once()

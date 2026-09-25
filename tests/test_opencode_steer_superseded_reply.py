"""A steer must not drop a completed OpenCode reply that it superseded (#2184)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.v2_sessions import ActivePollInfo
from core.processing_indicator import ProcessingIndicatorService
from modules.agents.base import AgentRequest
from modules.agents.opencode.poll_loop import OpenCodePollLoop
from modules.im import MessageContext

REPORT = "Weekly report: all 12 checks passed."
SILENT = "<silent>callback acknowledged</silent>"


def _assistant(message_id, text, *, finish="stop", completed=True):
    return {
        "info": {
            "id": message_id,
            "role": "assistant",
            "time": {"completed": 1} if completed else {},
            "finish": finish,
        },
        "parts": [{"type": "text", "text": text}] if text else [],
    }


def _user(message_id, text):
    return {"info": {"id": message_id, "role": "user"}, "parts": [{"type": "text", "text": text}]}


def _turn(reply_a, reply_b, *, finish_a="stop"):
    """Primary prompt, reply A, two steered callbacks, then reply B."""

    return [
        _user("primary", "repeat the staged report"),
        _assistant("reply-a", reply_a, finish=finish_a),
        _user("steer-1", "watch callback 1"),
        _user("steer-2", "watch callback 2"),
        _assistant("reply-b", reply_b),
    ]


def _agent(results, persisted=None):
    controller = SimpleNamespace(
        config=SimpleNamespace(language="en"),
        agent_auth_service=SimpleNamespace(
            maybe_emit_auth_recovery_message=AsyncMock(return_value=False)
        ),
        emit_agent_message=AsyncMock(),
    )
    controller.processing_indicator = ProcessingIndicatorService(controller)

    async def _emit_result(_context, text, **_kwargs):
        results.append(text)

    return SimpleNamespace(
        controller=controller,
        opencode_config=SimpleNamespace(error_retry_limit=1, active_turn_timeout_seconds=0),
        sessions=SimpleNamespace(
            update_active_poll_state=lambda _session_id, **state: (
                persisted.update(state) if persisted is not None else None
            )
        ),
        record_model_hub_native_failure=AsyncMock(),
        _extract_response_text=lambda m: "".join(p.get("text", "") for p in m["parts"]),
        emit_result_message=_emit_result,
        _remove_ack_reaction=AsyncMock(),
    )


def _server(messages):
    return SimpleNamespace(
        list_messages=AsyncMock(return_value=messages),
        get_session_status=AsyncMock(return_value=None),
        prompt_async=AsyncMock(),
    )


async def _live_poll(messages, agent):
    request = AgentRequest(
        context=MessageContext(user_id="user", channel_id="channel", platform="discord"),
        message="repeat the staged report",
        user_message="repeat the staged report",
        working_path="/tmp/opencode-steer-fixture",
        base_session_id="base",
        composite_session_id="base:fixture",
        session_key="discord::channel",
    )
    final_text, should_emit = await asyncio.wait_for(
        OpenCodePollLoop(agent).run_prompt_poll(
            request,
            _server(messages),
            "native-session",
            agent_to_use=None,
            model_dict=None,
            reasoning_effort=None,
            baseline_message_ids=set(),
        ),
        timeout=2,
    )
    assert should_emit is True
    return final_text


async def _restored_poll(messages, agent, results, *, emitted=()):
    server = _server(messages)
    agent._get_server = AsyncMock(return_value=server)
    poll = ActivePollInfo(
        opencode_session_id="native-session",
        base_session_id="base",
        channel_id="channel",
        thread_id="",
        settings_key="channel",
        working_path="/tmp/opencode-steer-fixture",
        platform="discord",
        user_id="user",
        emitted_assistant_messages=list(emitted),
        prompt_started_at=time.time(),
    )
    assert await asyncio.wait_for(OpenCodePollLoop(agent).run_restored_poll_loop(poll), timeout=2)
    assert len(results) == 1
    return results[0]


def _assistant_emits(agent):
    return [
        call.args[2]
        for call in agent.controller.emit_agent_message.await_args_list
        if call.args[1] == "assistant"
    ]


@pytest.mark.parametrize("path", ["live", "restored"])
@pytest.mark.parametrize(
    ("messages", "expected_result", "expected_emits"),
    [
        # A silent answer to the steer must not replace the visible reply.
        (_turn(REPORT, SILENT), REPORT, []),
        # A visible answer to the steer is the result; A still reaches the user.
        (_turn(REPORT, "Callbacks noted."), "Callbacks noted.", [REPORT]),
        # A silent reply superseded by a visible one is not re-sent.
        (_turn(SILENT, "Callbacks noted."), "Callbacks noted.", []),
        # When every reply is silent, the Turn stays silent.
        (_turn(SILENT, SILENT), SILENT, []),
    ],
    ids=["silent-steer-reply", "visible-steer-reply", "silent-superseded", "all-silent"],
)
async def test_steer_keeps_superseded_completed_reply(
    monkeypatch, path, messages, expected_result, expected_emits
):
    """MESSAGE-DELIVERY-321."""

    monkeypatch.setattr("modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0)
    results: list[str] = []
    agent = _agent(results)

    if path == "live":
        final_text = await _live_poll(messages, agent)
    else:
        final_text = await _restored_poll(messages, agent, results)

    assert final_text == expected_result
    assert _assistant_emits(agent) == expected_emits


async def test_tool_call_step_text_is_not_emitted_twice(monkeypatch):
    """A tool-calls step is already streamed as it completes; settlement must not repeat it."""

    monkeypatch.setattr("modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0)
    agent = _agent([])
    messages = _turn("Checking the report first.", SILENT, finish_a="tool-calls")

    final_text = await _live_poll(messages, agent)

    assert final_text == SILENT
    assert _assistant_emits(agent) == ["Checking the report first."]


async def test_restored_poll_does_not_resend_a_persisted_superseded_reply(monkeypatch):
    """A restart after the superseded reply was delivered must not deliver it again."""

    monkeypatch.setattr("modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0)
    messages = _turn(REPORT, "Callbacks noted.")
    persisted: dict = {}
    await _live_poll(messages, _agent([], persisted))

    results: list[str] = []
    restored = _agent(results)
    final_text = await _restored_poll(
        messages, restored, results, emitted=persisted["emitted_assistant_messages"]
    )

    assert final_text == "Callbacks noted."
    assert _assistant_emits(restored) == []

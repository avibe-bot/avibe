"""OpenCode consumes Hub finality without automatically replaying a Turn."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.agents.base import AgentRequest
from modules.agents.model_hub import ModelHubLaunch, bind_launch
from modules.agents.opencode.poll_loop import (
    OpenCodePollLoop,
    _is_model_hub_recovery_exhausted,
)
from modules.im import MessageContext
from vibe.i18n import t


def _context(channel="hub", backend="opencode"):
    context = MessageContext(user_id="user", channel_id="channel", platform="slack")
    if channel is not None:
        bind_launch(context, ModelHubLaunch(
            backend=backend,
            channel=channel,
            requested_model="model",
            target_model="model",
            runtime_model="model",
            source_id="source",
        ))
    return context


def _error():
    return {
        "name": "APIError",
        "data": {
            "statusCode": 424,
            "isRetryable": False,
            "message": "Automatic recovery has ended. Try again or choose another model.",
            "responseBody": json.dumps({
                "type": "error",
                "error": {
                    "type": "model_hub_recovery_exhausted",
                    "code": "model_hub_recovery_exhausted",
                    "message": "Automatic recovery has ended. Try again or choose another model.",
                },
            }),
        },
    }


def test_exact_native_error_recognizes_hub_finality():
    assert _is_model_hub_recovery_exhausted(_context(), _error())


@pytest.mark.parametrize("channel", [None, "direct", "native_cli"])
def test_same_error_cannot_change_non_hub_retry_policy(channel):
    assert not _is_model_hub_recovery_exhausted(_context(channel), _error())


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_other_backend_launch_does_not_prove_opencode_finality(backend):
    assert not _is_model_hub_recovery_exhausted(_context(backend=backend), _error())


@pytest.mark.parametrize("replacement", [None, [], "model_hub_recovery_exhausted", {}, {"name": "UnknownError"}])
def test_non_structured_native_error_retains_normal_policy(replacement):
    assert not _is_model_hub_recovery_exhausted(_context(), replacement)


@pytest.mark.parametrize("status", [None, "424", 424.0, True, 400, 429, 503])
def test_only_exact_terminal_http_status_qualifies(status):
    error = _error()
    error["data"]["statusCode"] = status
    assert not _is_model_hub_recovery_exhausted(_context(), error)


@pytest.mark.parametrize("body", [
    None, {}, "invalid", "[]", "null", "{}", "[" * 1100 + "]" * 1100,
    " " * 4097,
    '{"error":{"type":"model_hub_recovery_exhausted","code":"model_hub_recovery_exhausted"}}',
    '{"type":"error","error":{"code":"model_hub_recovery_exhausted"}}',
    '{"type":"error","error":{"type":"model_hub_recovery_exhausted"}}',
    '{"type":"error","error":{"type":"model_hub_recovery_exhausted","code":"server_error"}}',
])
def test_diagnostic_words_without_exact_wire_evidence_do_not_suppress_retry(body):
    error = _error()
    error["data"]["responseBody"] = body
    assert not _is_model_hub_recovery_exhausted(_context(), error)


async def _poll(monkeypatch, *, channel="hub", error=None, baseline=False, busy=False, language="en"):
    monkeypatch.setattr("modules.agents.opencode.poll_loop._POLL_INTERVAL_SECONDS", 0)
    controller = SimpleNamespace(
        config=SimpleNamespace(language=language),
        agent_auth_service=SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False)),
        emit_agent_message=AsyncMock(),
    )
    agent = SimpleNamespace(
        controller=controller,
        opencode_config=SimpleNamespace(error_retry_limit=2, active_turn_timeout_seconds=0),
        record_model_hub_native_failure=AsyncMock(),
        _extract_response_text=lambda message: message["parts"][0]["text"] if message["parts"] else "",
    )
    failed = {
        "info": {
            "id": "failed",
            "role": "assistant",
            "time": {"completed": 1},
            "error": copy.deepcopy(error if error is not None else _error()),
        },
        "parts": [],
    }
    success = {
        "info": {"id": "later", "role": "assistant", "time": {"completed": 2}, "finish": "stop"},
        "parts": [{"type": "text", "text": "later legitimate output"}],
    }
    first_snapshot = [failed]
    if busy:
        first_snapshot.append({
            "info": {"id": "pending-inject", "role": "user", "time": {}},
            "parts": [{"type": "text", "text": "new legitimate input"}],
        })
    server = SimpleNamespace(
        list_messages=AsyncMock(side_effect=[first_snapshot, [failed, success]]),
        get_session_status=AsyncMock(return_value={"type": "busy"} if busy else None),
        prompt_async=AsyncMock(),
    )
    request = AgentRequest(
        context=_context(channel),
        message="original prompt",
        user_message="original prompt",
        working_path="/tmp/model-hub-fixture",
        base_session_id="base",
        composite_session_id="base:fixture",
        session_key="slack::channel",
    )
    result = await asyncio.wait_for(
        OpenCodePollLoop(agent).run_prompt_poll(
            request,
            server,
            "native-session",
            agent_to_use=None,
            model_dict={"providerID": "provider", "modelID": "model"},
            reasoning_effort=None,
            baseline_message_ids={"failed"} if baseline else set(),
        ),
        timeout=2,
    )
    return result, server, agent, controller


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_hub_terminal_skips_automatic_continue_and_emits_one_failure(monkeypatch, language):
    """MH-RETRY-NATIVE-001: one Hub terminal settles without an automatic Turn replay."""
    result, server, agent, controller = await _poll(monkeypatch, language=language)
    assert result == (None, False)
    server.prompt_async.assert_not_awaited()
    agent.record_model_hub_native_failure.assert_awaited_once()
    calls = controller.emit_agent_message.await_args_list
    assert [call.args[1] for call in calls] == ["notify", "result"]
    assert sum(call.args[1] == "notify" for call in calls) == 1
    assert sum(bool(call.kwargs["output"].completes_turn) for call in calls) == 1
    # The shared OpenCode route need not have exact Turn/source attribution to
    # display the closed request result already proved by the native error.
    assert calls[0].args[2] == t("modelHub.recovery.ended", language)
    assert "APIError" not in calls[0].args[2]
    assert "APIError" in calls[-1].kwargs["terminal_error"]


@pytest.mark.parametrize("channel", [None, "direct", "native_cli"])
async def test_ordinary_native_failure_still_uses_existing_continue(monkeypatch, channel):
    result, server, agent, controller = await _poll(monkeypatch, channel=channel)
    assert result == ("later legitimate output", True)
    server.prompt_async.assert_awaited_once()
    assert server.prompt_async.await_args.kwargs["text"] == "continue"
    agent.record_model_hub_native_failure.assert_not_awaited()
    controller.emit_agent_message.assert_not_awaited()


async def test_unrelated_hub_error_keeps_existing_retry(monkeypatch):
    error = _error()
    error["data"]["statusCode"] = 503
    result, server, agent, _controller = await _poll(monkeypatch, error=error)
    assert result == ("later legitimate output", True)
    server.prompt_async.assert_awaited_once()
    agent.record_model_hub_native_failure.assert_not_awaited()


@pytest.mark.parametrize("guard", ["baseline", "busy"])
async def test_old_or_still_live_assistant_is_not_terminalized(monkeypatch, guard):
    result, server, agent, _controller = await _poll(monkeypatch, **{guard: True})
    assert result == ("later legitimate output", True)
    server.prompt_async.assert_not_awaited()
    agent.record_model_hub_native_failure.assert_not_awaited()

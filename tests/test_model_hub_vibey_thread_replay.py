"""A speculative failure must leave the same Source available for immediate plain replay."""

from __future__ import annotations

import aiohttp
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.provider import Done, ThreadObservation
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import TextBlock, UserMessage
from core.agent_core.tools.base import ToolSpec
from modules.agents.vibey.models import selection_from_hop
from tests.agent_core.ai.test_anthropic_prompt_cache import SUCCESS, _request
from dataclasses import replace
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_l3 import InvokeHandle, LiveInvokeHandle, _outcome, _source
from tests.test_model_hub_vibey_consumer import _vibey_service


@pytest.mark.asyncio
async def test_pinned_thread_rejection_keeps_provider_code_through_real_hub(tmp_path):
    import json
    from core.agent_core.ai.message_threads import record_http_error
    from core.handlers.model_hub.stream_wire import ProtocolObservation
    from vibe.model_hub_runtime.client import _reduce_protocol_observation

    primary = _source("src_primary01", "Claude", vendor="anthropic", protocol="anthropic", model_id="claude-test")
    envelope = {
        "error": {
            "type": "invalid_request_error",
            "message": "private fixture",
            "details": {"error_code": "thread_fingerprint_mismatch"},
        }
    }
    from types import SimpleNamespace

    outcome = _reduce_protocol_observation(
        ProtocolObservation(
            outcome="failed_terminal", error_payload=json.dumps(envelope).encode(), error_envelope_paths=(("error",),)
        ),
        source=SimpleNamespace(source_id=primary.id, protocol="anthropic"),
        model_id="claude-test",
        http_status=400,
        stream_started=False,
    )
    svc = _vibey_service(tmp_path, [primary], handles=[InvokeHandle(outcome)])
    gateway = ModelHubTurnGateway(svc)
    router = ModelHubRuntimeRouter(service=svc, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:thread", turn_id="turn-rejection")
        body = {
            "model": hop["runtime_model"],
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "thread": {"type": "continue", "previous_message_id": "msg_fixture"},
        }
        headers = {
            "x-api-key": hop["token"],
            **hop["request_headers"],
            "x-avibe-pin-source": primary.id,
            "anthropic-beta": "message-threads-2026-08-12",
            "x-avibe-thread-declarations": "[]",
        }
        async with (
            aiohttp.ClientSession() as client,
            client.post(hop["base_url"] + "/messages", json=body, headers=headers) as response,
        ):
            assert response.status == 400
            receipt = {}
            record_http_error(receipt, response.status, await response.text(), hub=True)
        assert receipt == {"error_type": "invalid_request_error", "error_code": "thread_fingerprint_mismatch"}
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_pinned_thread_failure_does_not_cool_the_replay_source(tmp_path):
    primary = _source("src_primary01", "Claude", vendor="anthropic", protocol="anthropic", model_id="claude-test")
    svc = _vibey_service(
        tmp_path,
        [primary],
        handles=[
            InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error")),
            LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id), (b"{}",)),
        ],
    )
    gateway = ModelHubTurnGateway(svc)
    router = ModelHubRuntimeRouter(service=svc, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:thread", turn_id="turn-thread")
        body = {"model": hop["runtime_model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        headers = {"x-api-key": hop["token"], **hop["request_headers"]}
        async with aiohttp.ClientSession() as client:
            async with client.post(
                hop["base_url"] + "/messages",
                json={**body, "thread": {"type": "create"}},
                headers={**headers, "x-avibe-pin-source": primary.id, "anthropic-beta": "message-threads-2026-08-12"},
            ) as r:
                assert r.status == 409
            assert svc.store.config.sources[0].state.status == "standby"
            async with client.post(hop["base_url"] + "/messages", json=body, headers=headers) as r:
                assert r.status == 200
        assert [hop[0] for hop in svc.adapter.invocations] == [primary.id, primary.id]
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_omitted_hosted_declaration_uses_normal_admission_and_stays_local(tmp_path):
    from tests.test_model_hub_hosted_pinning import _refuse

    primary = _source("src_primary01", "Claude", vendor="anthropic", protocol="anthropic", model_id="claude-test")
    svc = _vibey_service(tmp_path, [primary], handles=[LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS), (b"{}",))])
    gateway = ModelHubTurnGateway(svc)
    router = ModelHubRuntimeRouter(service=svc, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:tools", turn_id="turn-tools")
        headers = {"x-api-key": hop["token"], **hop["request_headers"], "x-avibe-pin-source": primary.id}
        body = {"model": hop["runtime_model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        delta = {**body, "thread": {"type": "continue", "previous_message_id": "msg_test"}}
        declaration = {"name": "web_search", "type": "web_search_20250305"}
        local = {"x-avibe-thread-declarations": '[["web_search","web_search_20250305"]]'}
        async with aiohttp.ClientSession() as client:

            async def send(body, extra):
                async with client.post(
                    hop["base_url"] + "/messages", json=body, headers={**headers, **extra}
                ) as response:
                    return response.status

            assert await send(delta, local) == 200
            forwarded = svc.adapter.requests[0]
            assert "tools" not in forwarded and not (forwarded.headers.keys() & local.keys())
            _refuse(svc, primary, "claude-test", "web_search", "call")
            assert await send({**body, "tools": [declaration]}, {}) == 409
            assert await send(delta, local) == 409
            assert len(svc.adapter.invocations) == 1
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["claude-test", "claude-模型"])
async def test_two_vibey_turns_use_same_parent_through_real_hub(tmp_path, model_id):
    primary = _source("src_primary01", "Claude", vendor="anthropic", protocol="anthropic", model_id=model_id)
    stream = SUCCESS.replace(
        '"usage":{"input_tokens":1}', '"id":"msg_wire","usage":{"input_tokens":3,"cache_read_input_tokens":23000}'
    )
    svc = _vibey_service(
        tmp_path,
        [primary],
        handles=[LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS), (stream.encode(),)) for _ in range(2)],
    )
    gateway = ModelHubTurnGateway(svc)
    router = ModelHubRuntimeRouter(service=svc, turn_gateway=gateway)
    adapter = AnthropicAdapter(gateway=True)
    try:
        messages = (UserMessage((TextBlock("old " * 20000),)),)
        for i in range(2):
            hop = await router.resolve_hop("menu-alias", process_scope="vibey:closed-loop", turn_id=f"turn-{i}")
            ep = selection_from_hop(hop).endpoint
            req = replace(
                _request(messages=messages),
                endpoint=ep,
                thread_observation=ThreadObservation("closed-loop"),
                tools=(ToolSpec("web_search", "Search", {"type": "object"}),),
            )
            if i == 1:
                from core.agent_core.ai.preview import preview_wire_request

                inert = await router.describe_vibey_request("menu-alias")
                preview_ep = selection_from_hop(inert.to_hop_resolution()).endpoint
                assert preview_ep.base_url != ep.base_url
                preview = await preview_wire_request(
                    replace(req, endpoint=preview_ep), gateway=True, session_id="closed-loop"
                )
                assert preview.wire.body["thread"] == {"type": "continue", "previous_message_id": "msg_wire"}
                assert len(svc.adapter.requests) == 1
            events = [e async for e in adapter.stream(req, CancelToken())]
            assert isinstance(events[-1], Done), (
                i,
                ep.model_id,
                ep.source_id,
                req.thread_observation.attempts,
                len(svc.adapter.requests),
                events[-1],
            )
            messages = (*messages, events[-1].message, UserMessage((TextBlock("next"),)))
            from core.run_settlement import SETTLED_BY_TERMINAL_RESULT

            completion = router.settle_turn(
                f"turn-{i}", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-10-02T08:00:00Z"
            )
            if completion:
                await completion
        create, cont = svc.adapter.requests
        assert create["thread"]["type"] == "create" and create["tools"][0]["type"] == "web_search_20250305"
        assert cont["thread"] == {"type": "continue", "previous_message_id": "msg_wire"} and len(cont["messages"]) == 1
        assert "tools" not in cont and cont.thread_calls == {("web_search", "web_search_20250305")}
        assert "x-avibe-thread-declarations" not in cont.headers
    finally:
        await adapter.aclose()
        await gateway.close()

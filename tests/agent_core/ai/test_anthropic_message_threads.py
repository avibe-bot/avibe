"""Wire contract: failing the optional create cannot remove context or fail a healthy Turn.

The prompt-cache tests cover a rejected TTL, not rejection of the thread field/beta or suppression
of another speculative request on the same route. Count actual HTTP sends, not adapter attempts.
"""

import json
from dataclasses import replace

import httpx

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.provider import Done
from core.agent_core.cancel import CancelToken
import pytest

from config.paths import get_state_dir
from core.agent_core.ai.provider import ThreadObservation
from core.agent_core.messages import UserMessage, TextBlock
from tests.agent_core.ai.test_anthropic_prompt_cache import SUCCESS, _breakpoints, _request

BETA = "message-threads-2026-08-12"


@pytest.mark.parametrize(
    "model", ["claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5-high", "claude-fable-5.2-high"]
)
def test_signed_create_carries_engine_thinking_policy_into_delta(tmp_path, monkeypatch, model):
    import time
    from core.agent_core.ai import message_threads as mt
    from core.agent_core.ai._common import WireRequest

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ep = _request(model_id=model, source_id="test-source").endpoint
    history = [
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "fixture", "signature": "fixture"}]},
        {"role": "user", "content": "previous"},
    ]
    base = {"model": model, "stream": True, "messages": history, "thinking": {"type": "adaptive"}}
    wire = WireRequest("https://fixture.invalid/messages", {}, base)
    route = mt.route_key(ep, True)
    create = mt.prepare(wire, ep, True, None, route, None)
    expected = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert create.wire.body["thinking"] == expected
    answer = {"role": "assistant", "content": "ok"}
    parent = mt.Parent(
        route,
        create.fixed,
        len(history),
        create.history,
        mt.message_digest([answer]),
        "msg_fixture",
        24000,
        time.monotonic() + 60,
        pinned_drop_block=create.pinned_drop_block,
    )
    follow = replace(wire, body={**base, "messages": [*history, answer, {"role": "user", "content": "next"}]})
    delta = mt.prepare(follow, ep, True, parent, route, None)
    assert delta.mode == "continue" and delta.pinned_drop_block
    assert delta.wire.body["thinking"] == expected and delta.wire.body["messages"] == follow.body["messages"][-1:]
    assert mt.prepare(follow, ep, True, None, route, "ordinary_replay").wire.body is follow.body
    assert (
        mt.prepare(wire, ep, False, None, route, None).wire.body["thinking"] == base["thinking"] == {"type": "adaptive"}
    )


def test_parent_budget_uses_tokens_for_tools_system_and_appended_tool_result(monkeypatch, tmp_path):
    import time
    from core.agent_core.ai import message_threads as mt
    from core.agent_core.ai._common import WireRequest

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ep = _request(source_id="test-source").endpoint
    route = mt.route_key(ep, True)
    history = [{"role": "user", "content": "prior conversation"}]
    answer = {"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "lookup", "input": {}}]}
    wire = WireRequest(
        "https://fixture.invalid/messages",
        {},
        {
            "model": ep.model_id,
            "system": "system instruction " * 600,
            "tools": [
                {"name": "lookup", "description": "lookup description " * 600, "input_schema": {"type": "object"}}
            ],
            "messages": history,
        },
    )
    create = mt.prepare(wire, ep, True, None, route, None)

    def follow(tokens, text, *, mark_result=False):
        parent = mt.Parent(
            route,
            create.fixed,
            1,
            create.history,
            mt.message_digest([answer]),
            "msg_fixture",
            tokens,
            time.monotonic() + 60,
        )
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call", "content": text}]}
        if mark_result:
            result["content"][0]["cache_control"] = {"type": "ephemeral"}
        return mt.prepare(
            replace(wire, body={**wire.body, "messages": [*history, answer, result]}), ep, True, parent, route, None
        )

    assert follow(36_406, "result " * 400).mode == "continue"
    assert follow(19_999, "ok").reason == "small_parent"
    # At this size the restored system/tools and suffix alone could explain the usage.
    assert follow(20_000, "result " * 400).reason == "restorable_text_risk"
    # 25,761 rendered bytes: exactly 75% of 34,348. Equality is safe to attempt.
    assert follow(34_347, "result " * 400).reason == "restorable_text_risk"
    assert follow(34_348, "result " * 400).mode == "continue"
    wire = replace(wire, body={**wire.body, "system": wire.body["system"] + "x" * 5_000})
    create = mt.prepare(wire, ep, True, None, route, None)
    # 30,761 UTF-8 bytes → 7,691 estimated tokens. Equality is sufficient.
    assert follow(23_072, "result " * 400).reason == "large_static_or_delta"
    assert follow(23_073, "result " * 400).reason == "restorable_text_risk"
    assert follow(36_406, "x" * 13_000).reason == "dense_delta_risk"
    assert follow(36_406, "结果 " * 8_000).reason == "large_static_or_delta"

    # Both decisions survive on an actual-send receipt: a rejected delta and a cache-safe full.
    marker = {"type": "ephemeral"}
    history = [
        {
            "role": "user",
            "content": [
                *({"type": "text", "text": "previous"} for _ in range(20)),
                {"type": "text", "text": "last", "cache_control": marker},
            ],
        }
    ]
    wire = replace(
        wire,
        body={
            **wire.body,
            "messages": history,
            "system": [{"type": "text", "text": wire.body["system"], "cache_control": marker}],
            "tools": [{**wire.body["tools"][0], "cache_control": marker}],
        },
    )
    create = mt.prepare(wire, ep, True, None, route, None)
    assert create.mode == "create"
    attempt = follow(36_406, "x" * 13_000, mark_result=True)
    assert attempt.mode == "full" and attempt.record["reason"] == "preserve_cache_breakpoints"
    assert attempt.record["delta_rejection"] == "dense_delta_risk"


async def test_create_rejection_replays_once_and_remembers_route():
    sent = []

    def handle(wire):
        body = json.loads(wire.content)
        betas = set(wire.headers.get("anthropic-beta", "").split(","))
        sent.append((body, betas))
        if "thread" in body or BETA in betas:
            return httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SUCCESS)

    request = _request(provider="third-party", source_id="test-source")
    request = replace(
        request,
        thread_observation=ThreadObservation("one"),
        endpoint=replace(request.endpoint, request_headers={"anthropic-beta": "keep-me"}),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        for _ in range(2):
            events = [event async for event in adapter.stream(request, CancelToken())]
            assert isinstance(events[-1], Done)
    assert len(sent) == 3  # create, full replay; next request immediately full
    (create, create_betas), (full, full_betas), next_full = sent
    assert create.pop("thread") == {"type": "create"}
    assert create_betas == full_betas | {BETA} and full_betas == {"keep-me"}
    assert create["system"] == full["system"] and create["tools"] == full["tools"]
    assert len(create["messages"]) == len(full["messages"]) == 3
    assert len(_breakpoints(create)) == 3 and len(_breakpoints(full)) == 4
    assert "thread" not in full and next_full == (full, full_betas)


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "low",
        "missing",
        "early_tool",
        "unknown",
        "fork",
        "edit",
        "model",
        "account",
        "expired",
        "disabled",
        "large_delta",
        "small_parent",
        "breakdown_only",
        "unknown_server_block",
        "oversize_id",
        "duplicate_start",
        "citation",
        "late_delta",
        "unclosed",
        "initial_content",
        "upstream_fallback",
        "thread_400",
    ],
)
async def test_continuation_admission_and_full_recovery(case, monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    clock = time.monotonic
    seen = []
    good = SUCCESS.replace(
        '"usage":{"input_tokens":1}', '"id":"msg_parent","usage":{"input_tokens":20,"cache_read_input_tokens":22000}'
    )

    def handle(wire):
        body = json.loads(wire.content)
        seen.append(body)
        text = good
        if len(seen) == 1 and case in ("duplicate_start", "unknown_server_block", "citation", "oversize_id"):
            start, tail = good.split("\n\n", 1)
            if case == "duplicate_start":
                text = start + "\n\n" + good
            if case == "oversize_id":
                text = good.replace("msg_parent", "m" * 300)
            if case == "unknown_server_block":
                extra = 'data: {"type":"content_block_start","index":1,"content_block":{"type":"server_tool_use","name":"code_execution","id":"t","input":{}}}'
                text = start + "\n\n" + extra + "\n\n" + tail
            if case == "citation":
                text = good.replace('"text":""', '"text":"","citations":[{"type":"page_location"}]')
        if len(seen) == 1:
            end = 'data: {"type":"content_block_stop","index":0}\n\n'
            if case == "late_delta":
                text = good.replace(
                    end,
                    end
                    + 'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"late"}}\n\n',
                )
            elif case == "unclosed":
                text = good.replace(end, "")
            elif case == "initial_content":
                text = good.replace(
                    '"id":"msg_parent"', '"id":"msg_parent","content":[{"type":"text","text":"ignored"}]'
                )
            elif case == "upstream_fallback":
                text = good.replace('"output_tokens":1', '"output_tokens":1,"iterations":[{"type":"fallback_message"}]')
        if len(seen) == 1 and case == "small_parent":
            text = good.replace("22000", "500")
        if body.get("thread", {}).get("type") == "continue":
            if case == "breakdown_only":
                text = good.replace(
                    '"cache_read_input_tokens":22000', '"cache_creation":{"ephemeral_1h_input_tokens":22000}'
                )
            if case == "thread_400":
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "code": "not_the_contract",
                            "message": "private fixture",
                            "details": {"error_code": "thread_fingerprint_mismatch"},
                        }
                    },
                )
            if case == "unknown":
                return httpx.Response(404, json={"error": {"details": {"error_code": "thread_not_found"}}})
            if case in ("low", "missing", "early_tool"):
                text = text.replace("22000", "10") if case == "low" else text
                if case == "missing":
                    text = text.replace('"input_tokens":20,"cache_read_input_tokens":22000', '"output_tokens":4')
                if case == "early_tool":
                    text = (
                        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"x","name":"lost"}}\n\n'
                        + text
                    )
                text = text.replace('"text":"ok"', '"text":"LOST"')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    first = UserMessage((TextBlock("old context " * 6000),))
    latest = UserMessage((TextBlock("next" * (24000 if case == "large_delta" else 1)),))
    req = replace(_request(messages=(first,)), thread_observation=ThreadObservation("root"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        initial = [e async for e in adapter.stream(req, CancelToken())]
        assert isinstance(initial[-1], Done)
        if case == "expired":
            monkeypatch.setattr(time, "monotonic", lambda: clock() + 310)
        if case == "disabled":
            path = get_state_dir() / "disable-message-threads"
            path.parent.mkdir(exist_ok=True, parents=True)
            path.touch()
        ep = req.endpoint
        if case == "model":
            ep = replace(ep, model_id="other")
        if case == "account":
            ep = replace(ep, source_identity="rotated")
        obs = ThreadObservation("fork" if case == "fork" else "root")
        request = replace(
            req,
            endpoint=ep,
            messages=(latest if case == "edit" else first, initial[-1].message, latest),
            thread_observation=obs,
        )
        if case == "ok":
            from core.agent_core.ai.preview import preview_wire_request

            before = await preview_wire_request(request, session_id=obs.session_id)
            again = await preview_wire_request(request, session_id=obs.session_id)
            assert before.body == again.body and len(seen) == 1 and not obs.attempts
        result = [e async for e in adapter.stream(request, CancelToken())]
        if case == "ok":
            assert json.loads(before.body) == seen[1]
    assert isinstance(result[-1], Done)
    assert "LOST" not in repr(result) and "lost" not in repr(result)
    if case == "unknown":
        assert obs.attempts[0]["error_code"] == "thread_not_found"
    if case == "thread_400":
        failed, replay = obs.attempts
        assert failed["failure"] == "http_400"
        assert failed["error_code"] == "thread_fingerprint_mismatch"
        assert failed["error_type"] == "invalid_request_error"
        assert "private fixture" not in repr(obs.attempts) and "error_code" not in replay
    if case in ("ok", "breakdown_only", "low", "missing", "early_tool", "unknown", "thread_400"):
        assert seen[1]["thread"] == {"type": "continue", "previous_message_id": "msg_parent"}
        assert "system" not in seen[1] and "tools" not in seen[1] and len(seen[1]["messages"]) == 1
        if case not in ("ok", "breakdown_only"):
            assert len(seen) == 3 and "thread" not in seen[-1]
        else:
            assert len(seen) == 2
    else:
        assert len(seen) == 2 and seen[1].get("thread", {}).get("type") != "continue"
    if case not in ("ok", "breakdown_only"):
        assert len(seen[-1]["messages"]) == 3
    if case == "disabled":
        assert "thread" not in seen[-1]
    assert len(obs.attempts) == len(seen) - 1
    if case == "low":
        assert obs.attempts[0]["discarded_usage"]["cache_read_tokens"] == 10
        assert obs.attempts[0]["failure"] == "thread_usage_mismatch"
    assert (
        obs.attempts[0]["full_bytes"] >= len("old context ") * 6000
        if case != "edit"
        else obs.attempts[0]["full_bytes"] > 0
    )


async def test_same_byte_carry_reanchors_then_binds_only_the_new_epoch(monkeypatch, tmp_path):
    from core.agent_core.ai.preview import preview_wire_request

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sends = []

    def handle(wire):
        sends.append(json.loads(wire.content))
        stream = SUCCESS.replace('"usage":{"input_tokens":1}', f'"id":"msg_{len(sends)}","usage":{{"input_tokens":22000}}')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)

    history = [UserMessage((TextBlock("same input"),))]
    base = _request(messages=tuple(history))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        for epoch in ("", "a" * 64, "a" * 64, "a" * 64):
            obs = ThreadObservation("carry-session", carry_projection=epoch)
            req = replace(base, messages=tuple(history), thread_observation=obs)
            preview = await preview_wire_request(req, session_id=obs.session_id)
            events = [event async for event in adapter.stream(req, CancelToken())]
            assert isinstance(events[-1], Done) and json.loads(preview.body) == sends[-1]
            history.extend([events[-1].message, UserMessage((TextBlock("next"),))])
    assert [body.get("thread") for body in sends] == [
        {"type": "create"}, None, {"type": "create"}, {"type": "continue", "previous_message_id": "msg_3"},
    ]


async def test_resume_preserves_overlapping_lookup_windows():
    # Last two marks are close, but only the earlier lookup can reach the warmed first user block.
    from core.agent_core.messages import AssistantMessage, Origin

    def blocks(n):
        return AssistantMessage(
            tuple(TextBlock("item") for _ in range(n)), Origin("anthropic", "anthropic", "claude-test"), "stop"
        )

    user = UserMessage((TextBlock("old history " * 4000),))
    request = replace(
        _request(messages=(user, blocks(14), user, blocks(9), user)), thread_observation=ThreadObservation("resume")
    )
    sent = []

    def handle(wire):
        sent.append(json.loads(wire.content))
        return httpx.Response(200, text=SUCCESS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = [e async for e in AnthropicAdapter(client).stream(request, CancelToken())]
    assert isinstance(events[-1], Done)
    assert len(sent) == 1 and "thread" not in sent[0] and len(_breakpoints(sent[0])) == 4
    assert request.thread_observation.attempts[0]["reason"] == "preserve_cache_breakpoints"


@pytest.mark.parametrize("handback", [False, True])
async def test_silence_outlasts_speculative_send_without_replay(monkeypatch, handback):
    import time
    from types import SimpleNamespace
    from core.agent_core.ai import _common
    from core.agent_core.ai.provider import ProviderError

    now = [time.monotonic()]
    monkeypatch.setattr(_common, "time", SimpleNamespace(monotonic=lambda: now[0]))
    seen = []

    def handle(wire):
        seen.append(json.loads(wire.content))
        now[0] += 601
        if handback:
            return httpx.Response(
                409, json={"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}
            )
        return httpx.Response(502, json={"error": {"message": "upstream timed out"}})

    request = replace(_request(source_id="src_silent001"), thread_observation=ThreadObservation("silent"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = [e async for e in AnthropicAdapter(client, gateway=handback).stream(request, CancelToken())]
    assert len(seen) == 1 and seen[0]["thread"] == {"type": "create"}
    assert isinstance(events[-1], ProviderError) and events[-1].silence_exhausted
    assert not events[-1].retryable and not events[-1].continuable and events[-1].refusal is None


@pytest.mark.parametrize("nested", [False, True])
async def test_image_suffix_uses_ordinary_even_when_wire_is_tiny(nested):
    import time
    from core.agent_core.ai import message_threads as threads
    from core.agent_core.ai._common import WireRequest

    ep = _request().endpoint
    route = threads.route_key(ep, False)
    history = [{"role": "user", "content": "long " * 16000}]
    assistant = {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/webp", "data": "A" * 116}}
    images = [image] * 15
    suffix = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "x", "content": images}] if nested else images,
    }
    wire = WireRequest("https://model.invalid/messages", {}, {"messages": [*history, assistant, suffix]})
    create = threads.prepare(wire, ep, False, None, route, None)
    parent = threads.Parent(
        route,
        create.fixed,
        1,
        threads.message_digest(history),
        threads.message_digest([assistant]),
        "parent",
        22002,
        time.monotonic() + 300,
    )
    assert len(threads.encoded(suffix)) * 3 < parent.tokens
    send = threads.prepare(wire, ep, False, parent, route, None)
    assert (send.mode, send.reason) == ("full", "image_delta") and send.wire.body == wire.body


@pytest.mark.parametrize(
    "caller,expected",
    [
        ({"type": "direct"}, "continue"),
        ({"type": "code_execution_20260120", "tool_id": "srv_1"}, "create"),
        ({"type": "direct", "extra": 1}, "create"),
    ],
)
async def test_client_tool_reply_carries_its_parent_into_next_inner_request(caller, expected):
    from core.agent_core.messages import ToolResultMessage
    from tests.agent_core.ai import test_hosted_pause_turn as pause

    req = _request(
        messages=(UserMessage((TextBlock("history " * 18000),)),), source_id=pause.SOURCE, model_id=pause.MODEL
    )
    ep = replace(req.endpoint, source_identity="tool-account")
    req = replace(req, endpoint=ep, thread_observation=ThreadObservation("tool-parent"))
    call = [
        {
            "type": "content_block_start",
            "content_block": {
                "type": "tool_use",
                "id": "toolu_ordinary",
                "name": "read",
                "input": {},
                "caller": caller,
            },
        },
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'}},
    ]
    first = pause._response(
        pause._thinking(), call, stop_reason="tool_use", input_tokens=54000, output_tokens=17
    ).replace('"message": {"usage"', '"message": {"id":"msg_parent", "usage"')
    seen = []

    def gateway(wire):
        seen.append(json.loads(wire.content))
        return httpx.Response(
            200,
            headers=pause._served(),
            text=first if len(seen) == 1 else SUCCESS.replace('"input_tokens":1', '"input_tokens":54020'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        initial = [e async for e in AnthropicAdapter(client, gateway=True).stream(req, CancelToken())]
        assert isinstance(initial[-1], Done) and initial[-1].message.stop_reason == "tool_use"
        follow = replace(
            req,
            messages=(
                *req.messages,
                initial[-1].message,
                ToolResultMessage("toolu_ordinary", "read", (TextBlock("found"),)),
            ),
            thread_observation=ThreadObservation("tool-parent"),
        )
        result = [e async for e in AnthropicAdapter(client, gateway=True).stream(follow, CancelToken())]
    assert isinstance(result[-1], Done)
    assert [b["thread"]["type"] for b in seen] == ["create", expected]
    prev = req.thread_observation.attempts[0]
    nxt = follow.thread_observation.attempts[0]
    assert prev["binding"] == ("bound" if expected == "continue" else "unsupported_tool_caller")
    assert nxt["parent_state"] == prev["binding"]


async def test_gateway_ttl_refusal_is_shared_by_live_and_inert_thread_plans(monkeypatch):
    import time
    from types import SimpleNamespace
    from core.agent_core.ai import anthropic, message_threads as threads
    from core.agent_core.ai.preview import preview_wire_request
    from tests.agent_core.ai.test_anthropic_prompt_cache import ANTHROPIC_REJECTION

    clock = [time.monotonic()]
    fake = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(anthropic, "time", fake)
    monkeypatch.setattr(threads, "time", fake)
    session = "ttl-preview"
    first = UserMessage((TextBlock("old context " * 6000),))
    req = _request(messages=(first,), source_id="src_ttlcheck01")
    ep = replace(req.endpoint, source_identity="ttl-account", via_hub=True)
    req = replace(req, endpoint=ep, thread_observation=ThreadObservation(session))
    inert = replace(ep, base_url="http://request-preview.invalid/vibey/v1", token="auth-unbound")
    good = SUCCESS.replace('"usage":{"input_tokens":1}', '"id":"msg_demo","usage":{"cache_read_input_tokens":22000}')
    served = {
        "x-avibe-served-hop": json.dumps(
            {"provider": ep.provider, "api": "anthropic", "source": ep.source_id, "model": ep.model_id}
        )
    }
    sent = []

    def handle(wire):
        sent.append(wire.content)
        if b'"ttl":"1h"' in wire.content:
            return httpx.Response(400, json=ANTHROPIC_REJECTION)
        return httpx.Response(200, headers=served, text=good)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client, gateway=True)

        async def send(request):
            events = [e async for e in adapter.stream(request, CancelToken())]
            assert isinstance(events[-1], Done)
            return events[-1].message

        await send(req)
        assert [b'"ttl":"1h"' in wire for wire in sent] == [True, True, False]
        assert anthropic.cache_ttl_s(replace(req, endpoint=inert)) == 300
        assert anthropic.cache_ttl_s(replace(req, endpoint=replace(inert, source_identity="other"))) == 3600
        assert anthropic.cache_ttl_s(replace(req, endpoint=replace(ep, via_hub=False))) == 3600
        clock[0] += threads.COOLDOWN_SECONDS + 1
        answer = await send(req)
        assert json.loads(sent[-1])["thread"] == {"type": "create"}
        follow = replace(req, messages=(first, answer, UserMessage((TextBlock("next"),))))
        preview = await preview_wire_request(replace(follow, endpoint=inert), gateway=True, session_id=session)
        assert len(sent) == 4 and preview.wire.body["thread"] == {"type": "continue", "previous_message_id": "msg_demo"}
        await send(follow)
        assert len(sent) == 5 and sent[-1] == preview.body


async def test_speculative_create_pin_does_not_authorize_unpinned_pause_followup():
    from tests.agent_core.ai import test_hosted_pause_turn as pause
    from core.agent_core.ai.provider import ProviderError

    req = replace(pause._request(), tools=(), thread_observation=ThreadObservation("pause-test"))

    def reply(number):
        assert number == 1
        return httpx.Response(
            200,
            headers=pause._served(),
            text=pause._response(pause._text("first"), stop_reason="pause_turn", input_tokens=100, output_tokens=1),
        )

    sent, events = await pause._stream(reply, req)
    assert len(sent) == 1 and json.loads(sent[0].content)["thread"] == {"type": "create"}
    assert isinstance(events[-1], ProviderError) and events[-1].message == pause.PAUSE_UNSERVED_MESSAGE
    assert events[-1].partial.content == (TextBlock("first"),)

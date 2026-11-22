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
    if case in ("ok", "breakdown_only", "low", "missing", "early_tool", "unknown"):
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

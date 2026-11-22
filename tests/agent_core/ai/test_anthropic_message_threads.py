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
        result = [e async for e in adapter.stream(request, CancelToken())]
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

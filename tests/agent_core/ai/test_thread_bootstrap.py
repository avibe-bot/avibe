"""Admission measured at HTTP: an unsent full body's cache markers do not describe its delta.
A warm create is bound to its own request; cache read changes are measurements, not admission state.
"""

import json
from dataclasses import replace

import httpx
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.preview import preview_wire_request
from core.agent_core.ai.provider import Done, ThreadObservation
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import AssistantMessage, Origin, TextBlock, UserMessage
from tests.agent_core.ai.test_anthropic_prompt_cache import SUCCESS, _breakpoints, _request


def user(text):
    return UserMessage((TextBlock(text),))


def long_history():
    return (
        user("old context " * 6000),
        AssistantMessage(
            tuple(TextBlock("chunk") for _ in range(24)), Origin("anthropic", "anthropic", "claude-test"), "stop"
        ),
        user("question"),
    )


def served(reads):
    return httpx.Response(
        200,
        text=SUCCESS.replace(
            '"usage":{"input_tokens":1}',
            f'"id":"msg_p","usage":{{"input_tokens":23000,"cache_read_input_tokens":{reads}}}',
        ),
    )


def many_blocks(text, count):
    start, content = text.split('data: {"type":"content_block_start"', 1)
    chunk, tail = ('data: {"type":"content_block_start"' + content).split('data: {"type":"message_delta"', 1)
    return (
        start
        + "".join(chunk.replace('"index":0', f'"index":{i}') for i in range(count))
        + 'data: {"type":"message_delta"'
        + tail
    )


async def send(adapter, request):
    events = [e async for e in adapter.stream(request, CancelToken())]
    assert isinstance(events[-1], Done)
    return replace(
        request,
        messages=(*request.messages, events[-1].message, user("next")),
        thread_observation=ThreadObservation(request.thread_observation.session_id),
    )


async def test_seven_independent_warm_routes_can_create_without_writing_state():
    from config.paths import get_state_dir

    sent = []

    def handle(wire):
        sent.append(json.loads(wire.content).get("thread", {}).get("type", "full"))
        return served(22000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        for i in range(7):
            first = replace(_request(messages=long_history()), thread_observation=ThreadObservation(f"fresh-{i}"))
            warm = await send(adapter, first)
            preview = await preview_wire_request(warm, session_id=f"fresh-{i}")
            assert preview.wire.body.get("thread") == {"type": "create"}
            await send(adapter, warm)
    assert sent == ["full", "create"] * 7
    assert not (get_state_dir() / "message-thread-bootstrap.sqlite").exists()


async def test_repeated_low_read_creates_are_observations_not_route_stops():
    observed = []

    def handle(wire):
        mode = json.loads(wire.content).get("thread", {}).get("type", "full")
        observed.append(mode)
        response = served(100 if mode == "create" else 22000)
        return httpx.Response(
            200,
            text=response.text.replace(
                '"input_tokens":23000', '"input_tokens":23000,"cache_creation_input_tokens":1234'
            ),
        )

    base = _request(messages=long_history())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        for i in range(3):
            req = replace(base, thread_observation=ThreadObservation(f"dip-{i}"))
            warm = await send(adapter, req)
            follow = await send(adapter, warm)
            await send(adapter, follow)
            assert warm.messages[-2].usage.cache_read_tokens == 22000
            assert follow.messages[-2].usage.cache_read_tokens == 100
    assert observed == ["full", "create", "continue"] * 3


@pytest.mark.parametrize("legacy", [False, True], ids=["missing", "locked"])
async def test_independent_warm_creates_ignore_pending_parent_and_retired_store(legacy, monkeypatch):
    import sqlite3
    from contextlib import ExitStack, closing
    from config.paths import get_state_dir

    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    base = _request(messages=long_history())
    with ExitStack() as stack:
        if legacy:
            path.parent.mkdir(parents=True, exist_ok=True)
            db = stack.enter_context(closing(sqlite3.connect(path)))
            db.execute("CREATE TABLE hint(id INTEGER PRIMARY KEY, day INTEGER, used INTEGER, loss BLOB, until REAL)")
            db.commit()
            db.execute("BEGIN EXCLUSIVE")

        def no_ledger(*args, **kwargs):
            pytest.fail("retired bootstrap metadata was opened")

        monkeypatch.setattr(sqlite3, "connect", no_ledger)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: served(22000))) as client:
            adapter = AnthropicAdapter(client)
            for session in ("owner-a", "owner-b"):
                warm = await send(adapter, replace(base, thread_observation=ThreadObservation(session)))
                predicted = await preview_wire_request(warm, session_id=session)
                await send(adapter, warm)  # The previous Session still owns its unused parent.
                assert predicted.wire.body.get("thread") == {"type": "create"}
                assert warm.thread_observation.attempts[0]["mode"] == "create"
        assert path.exists() == legacy


@pytest.mark.parametrize("reads", [None, 0, 17])
@pytest.mark.parametrize("append_size", [4, 18000])
async def test_full_create_needs_neither_cache_hits_nor_delta_budget(reads, append_size):
    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        text = served(reads or 0).text
        if reads is None:
            text = text.replace(',"cache_read_input_tokens":0', "")
        return httpx.Response(200, text=text)

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("full-root"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        witness = await send(adapter, req)
        root = replace(witness, messages=(*witness.messages[:-1], user("L" * append_size)))
        follow = await send(adapter, root)
        await send(adapter, follow)
    assert [b.get("thread", {}).get("type", "full") for b in bodies] == ["full", "create", "continue"]
    assert len(bodies[1]["messages"]) == len(root.messages) and len(bodies[2]["messages"]) == 1


async def test_origin_rejected_create_replays_without_binding():
    req = _request(messages=(user("small"),), source_id="src_receipt01")
    req = replace(
        req,
        endpoint=replace(req.endpoint, source_identity="fixture"),
        thread_observation=ThreadObservation("unattributed"),
    )
    sends = []

    def handle(wire):
        sends.append(json.loads(wire.content))
        text = served(29).text.replace(
            '"input_tokens":23000',
            '"input_tokens":23000,"cache_creation_input_tokens":51,"cache_creation":{"ephemeral_5m_input_tokens":51}',
        )
        return httpx.Response(200, text=text)  # No verified served-hop header.

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await send(AnthropicAdapter(client, gateway=True), req)
    facts = req.thread_observation.attempts[0]
    assert [s.get("thread", {}).get("type", "full") for s in sends] == ["create", "full"]
    assert facts["failure"] == "thread_origin_mismatch"


def stream_usage(start, delta):
    frames = []
    for line in served(22000).text.splitlines(keepends=True):
        if line.startswith("data: "):
            event = json.loads(line[6:])
            if event["type"] in ("message_start", "message_delta"):
                obj, fields = (event["message"], start) if event["type"] == "message_start" else (event, delta)
                obj.pop("usage", None)
                if fields is not None:
                    obj["usage"] = fields
                line = "data: " + json.dumps(event) + "\n"
        frames.append(line)
    return httpx.Response(200, text="".join(frames))


@pytest.mark.parametrize("envelope", [None, {}, {"output_tokens": 1}])
async def test_continue_without_first_event_input_replays_full_once(envelope):
    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        if len(bodies) == 2:
            # Even a large input report in a later delta would be too late.
            return stream_usage(envelope, {"input_tokens": 45000})
        return served(22000)

    req = replace(_request(messages=(user("large " * 11000),)), thread_observation=ThreadObservation("first-input"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        follow = await send(adapter, req)
        await send(adapter, follow)
    assert [body.get("thread", {}).get("type", "full") for body in bodies] == ["create", "continue", "full"]
    assert len(bodies[2]["messages"]) == len(follow.messages)
    assert follow.thread_observation.attempts[0]["failure"] == "thread_usage_mismatch"


@pytest.mark.parametrize("envelope", [None, {}, {"output_tokens": 1}])
async def test_usage_optional_for_exact_full_witness_not_for_delta(envelope):
    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        return stream_usage(envelope, None) if len(bodies) <= 2 else served(22000)

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("usage-witness"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        root = await send(adapter, req)
        follow = await send(adapter, root)
        await send(adapter, follow)
    assert req.thread_observation.attempts[0]["binding"] == "full_witness"
    assert bodies[1]["thread"] == {"type": "create"} and len(bodies[1]["messages"]) == len(root.messages)
    assert root.thread_observation.attempts[0]["binding"] == ("missing_usage" if envelope is None else "bound")
    assert bodies[2].get("thread", {}).get("type") != "continue"


async def test_first_child_of_full_history_create_still_needs_reachable_terminal_mark():
    modes = []

    def handle(wire):
        mode = json.loads(wire.content).get("thread", {}).get("type", "full")
        modes.append(mode)
        response = served(22000)
        return httpx.Response(200, text=many_blocks(response.text, 24)) if mode == "create" else response

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("root-mark"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        follow = await send(adapter, await send(adapter, req))
        await send(adapter, follow)
    assert modes == ["full", "create", "full"]
    assert follow.thread_observation.attempts[0]["reason"] == "terminal_marker_out_of_reach"


async def test_verified_delta_ignores_distant_ordinary_markers():
    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        return served(22000)

    # One user mark on create; the next full's two marks would span >20 blocks.
    req = replace(_request(messages=long_history()[:2]), thread_observation=ThreadObservation("delta"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        await send(adapter, await send(adapter, req))
    assert [body.get("thread", {}).get("type") for body in bodies] == ["create", "continue"]


@pytest.mark.parametrize("blocks,expected", [(18, "continue"), (19, "full")])
async def test_actual_terminal_mark_needs_reachable_delta_lookup(blocks, expected, monkeypatch):
    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        response = served(22000)
        if len(bodies) == 1:
            return httpx.Response(200, text=many_blocks(response.text, blocks))
        return response

    first = replace(_request(messages=long_history()[:1]), thread_observation=ThreadObservation("terminal"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        req = await send(adapter, first)
        await send(adapter, req)
    assert req.thread_observation.attempts[0]["mode"] == expected
    if expected == "full":
        assert req.thread_observation.attempts[0]["reason"] == "terminal_marker_out_of_reach"
        assert len(_breakpoints(bodies[-1])) == 4


@pytest.mark.parametrize("change", ["session", "model", "edit", "disabled"])
async def test_long_create_requires_an_immediately_preceding_bound_full(change):
    from config.paths import get_state_dir

    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        return served(22000)

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("warm-test"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        follow = await send(adapter, req)
        if change == "session":
            follow = replace(follow, thread_observation=ThreadObservation("fresh-test"))
        elif change == "model":
            follow = replace(follow, endpoint=replace(follow.endpoint, model_id="other"))
        elif change == "edit":
            follow = replace(follow, messages=(user("different"), *follow.messages[1:]))
        elif change == "disabled":
            get_state_dir().mkdir(parents=True, exist_ok=True)
            (get_state_dir() / "disable-message-threads").touch()
        await send(adapter, follow)
    assert len(bodies) == 2 and all("thread" not in b and len(_breakpoints(b)) == 4 for b in bodies)

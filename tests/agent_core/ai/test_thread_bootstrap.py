"""Admission measured at HTTP: an unsent full body's cache markers do not describe its delta.
A risky long create, unlike a delta, must earn its exception with paired cache/byte evidence.
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
    monkeypatch.setenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "0")
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


@pytest.mark.parametrize("loss", [None, "create", "continue"])
async def test_bootstrap_measures_create_and_next_accepted_delta(loss, monkeypatch):
    monkeypatch.setenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "1")
    bodies = []

    def handle(wire):
        body = json.loads(wire.content)
        bodies.append(body)
        return served(19000 if loss and body.get("thread", {}).get("type") == loss else 22000)

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("probe"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        warm = await send(adapter, req)
        for _ in range(2):
            preview = await preview_wire_request(warm, session_id="probe")
            assert preview.wire.body.get("thread") == {"type": "create"}
        follow = await send(adapter, warm)
        assert bodies[-1] == preview.wire.body and len(_breakpoints(bodies[-1])) == 3
        assert len(_breakpoints(bodies[0])) == 4
        assert warm.thread_observation.attempts[0]["bootstrap_outcome"] == (
            "bootstrap_read_loss" if loss == "create" else "awaiting_continuation"
        )
        await send(adapter, follow)
        if loss != "create":
            assert bodies[-1]["thread"]["type"] == "continue"
            facts = follow.thread_observation.attempts[0]
            assert facts["body_bytes"] < facts["full_bytes"]
            assert facts["bootstrap_outcome"] == ("bootstrap_read_loss" if loss else "retained_smaller")
            if not loss:
                from core.agent_core.ai import message_threads as threads

                route = threads.route_key(req.endpoint, False)
                assert threads.bootstrap.reserve(route, 22000, preview=True)[1] == "bootstrap_pending"
                await follow.thread_observation.after_commit()
        fresh = replace(req, thread_observation=ThreadObservation("another"))
        another = await send(adapter, fresh)
        await send(adapter, another)
        if loss:
            assert "thread" not in bodies[-1]
            assert another.thread_observation.attempts[0]["reason"] == "bootstrap_read_loss"
        else:
            assert bodies[-1]["thread"]["type"] == "create"  # proven route, exhausted daily budget


@pytest.mark.parametrize("rejected", [True, False])
async def test_only_committed_conversation_delta_earns_exemption(rejected):
    from core.agent_core.ai import message_threads as threads
    from tests.agent_core.agent.test_compaction import SELECTION, make_agent, reader, run
    from tests.agent_core.fakes import InMemoryTranscriptStore

    store = InMemoryTranscriptStore()
    first, prior, _ = long_history()
    await store.consume_input("session", "old", first)
    await store.append_response("session", prior, final=True, replied=True)
    ep = _request().endpoint
    modes = []

    def handle(wire):
        modes.append(json.loads(wire.content).get("thread", {}).get("type", "full"))
        text = served(22000).text.replace('"input_tokens":23000', '"input_tokens":1')
        if modes[-1] == "create":
            text = many_blocks(text, 24)  # This otherwise-unreachable lookup is the authorized paired measurement.
        if rejected and modes[-1] == "continue":
            text = text.replace(
                '"type":"text","text":""', '"type":"thinking","thinking":"","signature":"signed"'
            ).replace('"type":"text_delta","text":"ok"', '"type":"thinking_delta","thinking":"private"')
        return httpx.Response(200, text=text)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        agent = make_agent(
            AnthropicAdapter(client),
            store=store,
            selection=replace(
                SELECTION,
                endpoint=ep,
                capabilities=replace(SELECTION.capabilities, context_window=200000, input_limit=190000),
            ),
            system="fixed",
            tools=(reader("r"),),
        )
        for turn in ("warm", "trial", "delta"):
            await run(agent, value=turn, row=turn)
    assert modes == ["full", "create", "continue"]
    assert len([r for r in store.rows["session"] if r.kind == "response"]) == (3 if rejected else 4)
    assert threads.bootstrap.reserve(threads.route_key(ep, False), 22000, preview=True)[1] == (
        "bootstrap_pending" if rejected else "bootstrap_recheck"
    )


def test_shared_reservation_survives_restart_and_clock_boundaries(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from core.agent_core.ai import thread_bootstrap as ledger

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", raising=False)
    import time

    now = [time.time() - 100]
    monkeypatch.setattr(ledger, "time", SimpleNamespace(time=lambda: now[0]))
    routes = [f"route-{i}".encode() for i in range(6)]
    assert ledger.reserve(routes[0], 10, preview=True)[0] is not None and list(tmp_path.iterdir()) == []

    # No shared connection or Python lock: each reservation opens the same on-disk instance.
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda route: ledger.reserve(route, 10, preview=False), routes))
    allowed = [(r, t) for r, (t, _) in zip(routes, outcomes) if t]
    assert len(allowed) == 5 and [reason for t, reason in outcomes if not t] == ["bootstrap_daily_limit"]
    route, owner = allowed[0]
    assert ledger.reserve(route, 10, preview=False) == (None, "bootstrap_pending")
    now[0] += 100
    import sqlite3

    # A competing writer at observation time must not truncate 24h from *loss* to 24h from reserve.
    with sqlite3.connect(tmp_path / "state" / "message-thread-bootstrap.sqlite") as locked:
        locked.execute("BEGIN IMMEDIATE")
        assert ledger.settle(route, owner, "bootstrap_read_loss") == "bootstrap_read_loss"
    assert ledger.settle(route, owner, "bootstrap_proven") == "bootstrap_stale"
    now[0] = ledger._loss_marker(route).stat().st_mtime + ledger.DAY - 1
    assert ledger.reserve(route, 10, preview=False) == (None, "bootstrap_read_loss")
    now[0] += 1
    assert ledger.reserve(route, 10, preview=False)[0] is not None


@pytest.mark.parametrize("change", ["session", "model", "edit", "disabled", "cold"])
async def test_only_immediately_preceding_warm_bound_full_can_bootstrap(change):
    from config.paths import get_state_dir

    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        return served(0 if change == "cold" else 22000)

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

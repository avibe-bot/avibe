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


@pytest.fixture(autouse=True)
def isolated_controller(monkeypatch):
    from core.agent_core.ai import thread_bootstrap as ledger

    from weakref import WeakValueDictionary

    monkeypatch.setattr(ledger, "_pending", WeakValueDictionary())
    monkeypatch.setattr(ledger.thread_read_loss, "_live", {})


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


@pytest.mark.parametrize("loss", [None, "create", "continue"])
async def test_bootstrap_measures_create_and_next_accepted_delta(loss, monkeypatch):
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
        assert warm.thread_observation.attempts[0]["parent_state"] == "warm_full"
        assert warm.thread_observation.attempts[0]["binding"] == (
            "bootstrap_read_loss" if loss == "create" else "bound"
        )
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
            assert bodies[-1]["thread"]["type"] == "create"


@pytest.mark.parametrize("rejected", [True, False])
async def test_agent_commit_or_rejection_releases_only_the_measured_pair(rejected):
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
    assert threads.bootstrap.reserve(threads.route_key(ep, False), 22000, preview=True)[1] == "bootstrap_trial"


@pytest.mark.parametrize("orphan", ["expired", "evicted", "mismatched_claim"])
async def test_only_orphaned_pending_allows_another_warm_create(orphan, monkeypatch):
    import time
    from types import SimpleNamespace
    from core.agent_core.ai import message_threads as threads

    now = [time.monotonic()]
    monkeypatch.setattr(threads, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("core.agent_core.ai.anthropic.time", threads.time)
    first = replace(_request(messages=long_history()), thread_observation=ThreadObservation("owner"))
    fresh = replace(first, thread_observation=ThreadObservation("challenger"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: served(22000))) as client:
        adapter = AnthropicAdapter(client)
        follow = await send(adapter, await send(adapter, first))
        if orphan == "expired":
            now[0] += threads.PARENT_SECONDS - 1
        warm = await send(adapter, fresh)
        assert "thread" not in (await preview_wire_request(warm, session_id="challenger")).wire.body
        if orphan == "expired":
            now[0] += 1
        elif orphan == "evicted":
            threads.parents.pop("owner")
        else:
            await send(adapter, replace(follow, messages=long_history()))
        preview = await preview_wire_request(warm, session_id="challenger")
        assert preview.wire.body.get("thread") == {"type": "create"}
        await send(adapter, warm)
        assert warm.thread_observation.attempts[0]["reason"] == "bootstrap_trial"


@pytest.mark.parametrize("phase", ["create", "claim", "continue", "commit"])
async def test_live_pair_never_looks_orphaned_across_parent_timeout(phase, monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace
    from core.agent_core.ai import message_threads as threads

    now = [time.monotonic()]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(threads, "time", clock)
    monkeypatch.setattr("core.agent_core.ai.anthropic.time", clock)
    started, resume = asyncio.Event(), asyncio.Event()

    async def handle(wire):
        if json.loads(wire.content).get("thread", {}).get("type") == phase:
            started.set()
            await resume.wait()
        return served(22000)

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("active"))
    route = threads.route_key(req.endpoint, False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        warm = await send(adapter, req)
        if phase == "create":
            pending = asyncio.create_task(send(adapter, warm))
        else:
            follow = await send(adapter, warm)
            if phase == "claim":
                plan = threads.plan_send

                async def pause(*args, **kwargs):
                    started.set()
                    await resume.wait()
                    return await plan(*args, **kwargs)

                monkeypatch.setattr(threads, "plan_send", pause)
            pending = asyncio.create_task(send(adapter, follow))
        if phase == "commit":
            await pending  # Adapter closed; only the candidate awaiting commit remains.
        else:
            await asyncio.wait_for(started.wait(), 2)
        try:
            now[0] += threads.PARENT_SECONDS + 1
            assert threads.bootstrap.reserve(route, 100, False)[1] == "bootstrap_pending"
        finally:
            resume.set()
            await pending
        if phase == "commit":
            await follow.thread_observation.after_commit()
            assert threads.bootstrap.reserve(route, 100, True)[1] == "bootstrap_trial"
        if phase == "claim":
            assert threads.bootstrap.reserve(route, 100, True)[1] == "bootstrap_trial"


def test_stale_completion_does_not_release_another_pair_or_route():
    from core.agent_core.ai import thread_bootstrap as ledger

    a, b = b"a" * 32, b"b" * 32
    active = [True]
    old, _ = ledger.reserve(a, 100, False, lease=ledger.Lease(lambda: active[0]))
    other, _ = ledger.reserve(b, 100, False)
    active[0] = False
    new, reason = ledger.reserve(a, 100, False)
    assert reason == "bootstrap_trial"
    ledger.complete(a, old)
    assert ledger.owns(a, new) and ledger.owns(b, other)
    ledger.complete(a, new)
    assert ledger.reserve(a, 100, True)[1] == "bootstrap_trial" and ledger.owns(b, other)


def test_all_active_measured_stops_survive_restart_without_ids(monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from config.paths import get_state_dir
    from core.agent_core.ai import thread_bootstrap as ledger

    now = [100_000.0]
    stops = ledger.thread_read_loss
    monkeypatch.setattr(stops, "time", SimpleNamespace(time=lambda: now[0]))
    a, b, c = (bytes([i]) * 32 for i in range(3))
    ledger.measured_loss(a)
    now[0] += 10
    ledger.measured_loss(b)
    monkeypatch.setattr(stops, "_live", {})
    for route in (a, b):
        assert ledger.reserve(route, 100, True)[1] == ledger.reserve(route, 100, False)[1] == "bootstrap_read_loss"
    assert ledger.reserve(c, 100, False)[1] == "bootstrap_trial"
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("route_loss",)]
        assert [col[1] for col in db.execute("PRAGMA table_info(route_loss)")] == ["route", "until"]
        assert set(db.execute("SELECT route FROM route_loss")) == {(a,), (b,)}
    now[0] += stops.DAY - 10
    assert ledger.reserve(a, 100, True)[1] == "bootstrap_trial"
    assert ledger.reserve(b, 100, True)[1] == "bootstrap_read_loss"


async def test_warm_preview_and_send_respect_readable_legacy_loss_without_writing():
    import sqlite3
    import time
    from config.paths import get_state_dir
    from core.agent_core.ai import message_threads as threads

    bodies = []

    def handle(wire):
        bodies.append(json.loads(wire.content))
        return served(22000)

    req = replace(_request(messages=long_history()), thread_observation=ThreadObservation("legacy-loss"))
    route = threads.route_key(req.endpoint, False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = AnthropicAdapter(client)
        warm = await send(adapter, req)
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as writer:
            writer.execute(
                "CREATE TABLE hint(id INTEGER PRIMARY KEY, day INTEGER, used INTEGER, loss BLOB, until REAL)"
            )
            writer.execute("INSERT INTO hint VALUES (1,0,999,?,?)", (route, time.time() + 600))
            writer.commit()
            writer.execute("BEGIN IMMEDIATE")
            before = path.read_bytes()
            preview = await preview_wire_request(warm, session_id="legacy-loss")
            assert len(bodies) == 1 and path.read_bytes() == before
            await send(adapter, warm)
            assert preview.wire.body == bodies[-1] and "thread" not in bodies[-1]
            assert warm.thread_observation.attempts[0]["reason"] == "bootstrap_read_loss"


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

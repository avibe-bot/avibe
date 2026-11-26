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

    monkeypatch.setattr(ledger, "_state", None)


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
        "bootstrap_trial" if rejected else "bootstrap_recheck"
    )


@pytest.mark.parametrize("orphan", ["expired", "evicted", "mismatched_claim"])
async def test_only_orphaned_pending_allows_another_charged_warm_create(orphan, monkeypatch):
    import time
    from types import SimpleNamespace
    from core.agent_core.ai import message_threads as threads

    now = [time.monotonic()]
    monkeypatch.setattr(threads, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("core.agent_core.ai.anthropic.time", threads.time)
    monkeypatch.setenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "2")
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
        assert threads.bootstrap._state.used == 2


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
            assert threads.bootstrap.reserve(route, 100, True)[1] == "bootstrap_recheck"
        if phase == "claim":
            assert threads.bootstrap.reserve(route, 100, True)[1] == "bootstrap_trial"


def test_orphan_replacement_is_ticket_local_and_never_restores_prior_proof():
    from core.agent_core.ai import thread_bootstrap as ledger

    a, b = b"A" * 32, b"B" * 32
    original, _ = ledger.reserve(a, 100, False)
    ledger.settle(a, original, "bootstrap_proven")
    active = [True]
    recheck, reason = ledger.reserve(a, 100, False, lease=ledger.Lease(lambda: active[0]))
    other, _ = ledger.reserve(b, 100, False)
    assert reason == "bootstrap_recheck" and ledger._state.used == 2
    active[0] = False
    fresh, reason = ledger.reserve(a, 100, False)
    assert reason == "bootstrap_trial" and ledger._state.used == 3
    assert ledger.owns(b, other) and ledger.owns(a, fresh)
    assert ledger.settle(a, recheck, "bootstrap_proven") == "bootstrap_stale"
    assert ledger.settle(a, recheck, "bootstrap_read_loss") == "bootstrap_read_loss"
    assert ledger.settle(a, fresh, "bootstrap_proven") == "bootstrap_stale"
    assert ledger.owns(b, other)


def test_committed_versioned_proof_survives_restart_but_not_loss(monkeypatch):
    from core.agent_core.ai import thread_bootstrap as ledger

    monkeypatch.setenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "1")
    route = b"A" * 32
    trial, _ = ledger.reserve(route, 100, False, version="8.0.63")
    assert ledger.settle(route, trial, "bootstrap_proven") == "bootstrap_proven"
    monkeypatch.setattr(ledger, "_state", None)
    for other, version in ((route, None), (route, "8.1.2"), (b"B" * 32, "8.0.63")):
        assert ledger.reserve(other, 100, True, version=version)[1] == "bootstrap_daily_limit"
    current, reason = ledger.reserve(route, 100, False, version="8.0.63")
    assert reason == "bootstrap_recheck"
    ledger.settle(route, current, "bootstrap_read_loss")
    monkeypatch.setattr(ledger, "_state", None)
    assert ledger.reserve(route, 100, False, version="8.0.63")[1] == "bootstrap_read_loss"


@pytest.mark.parametrize("damage", ["busy", "missing_hint", "bad_count", "bad_version", "duplicate_hint"])
def test_partial_or_ambiguous_snapshot_cannot_restore_any_proof(monkeypatch, damage):
    import sqlite3
    from config.paths import get_state_dir
    from core.agent_core.ai import thread_bootstrap as ledger

    route = b"A" * 32
    trial, _ = ledger.reserve(route, 100, False, version="8.0.63")
    ledger.settle(route, trial, "bootstrap_proven")
    monkeypatch.setattr(ledger, "_state", None)
    with sqlite3.connect(get_state_dir() / "message-thread-bootstrap.sqlite") as db:
        if damage == "busy":
            db.execute("BEGIN EXCLUSIVE")
        else:
            db.execute(
                {
                    "missing_hint": "DELETE FROM hint",
                    "bad_count": "UPDATE hint SET used=-1",
                    "bad_version": "INSERT INTO proof VALUES (zeroblob(32), 'unknown')",
                    "duplicate_hint": "INSERT INTO hint SELECT 2, day, used, loss, until FROM hint",
                }[damage]
            )
            db.commit()
        assert ledger.reserve(route, 100, True, version="8.0.63")[1] == "bootstrap_trial"


def test_successful_proof_revocation_is_not_republished_by_stale_unrelated_writer(monkeypatch):
    import sqlite3
    from config.paths import get_state_dir
    from core.agent_core.ai import thread_bootstrap as ledger

    route = b"A" * 32
    trial, _ = ledger.reserve(route, 100, False, version="8.0.63")
    ledger.settle(route, trial, "bootstrap_proven")
    first_process = ledger._state
    monkeypatch.setattr(ledger, "_state", None)
    ledger.reserve(b"B" * 32, 100, False)  # Initialize a separate controller from the shared snapshot.
    ledger.settle(route, trial, "bootstrap_read_loss")
    monkeypatch.setattr(ledger, "_state", first_process)
    ledger.reserve(b"C" * 32, 100, False)
    with sqlite3.connect(get_state_dir() / "message-thread-bootstrap.sqlite") as db:
        assert db.execute("SELECT count(*) FROM proof").fetchone() == (0,)


def test_busy_recheck_and_loss_keep_live_cooldown_but_cannot_revoke_old_disk_proof(monkeypatch):
    import sqlite3
    from config.paths import get_state_dir
    from core.agent_core.ai import thread_bootstrap as ledger

    route = b"A" * 32
    trial, _ = ledger.reserve(route, 100, False, version="8.0.63")
    ledger.settle(route, trial, "bootstrap_proven")
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("BEGIN EXCLUSIVE")
        trial, reason = ledger.reserve(route, 100, False, version="8.0.63")
        assert reason == "bootstrap_recheck"
        assert ledger.settle(route, trial, "bootstrap_read_loss") == "bootstrap_read_loss"
        assert ledger.reserve(route, 100, False, version="8.0.63")[1] == "bootstrap_read_loss"
    monkeypatch.setattr(ledger, "_state", None)
    assert ledger.reserve(route, 100, False, version="8.0.63")[1] == "bootstrap_recheck"
    monkeypatch.setattr(ledger, "_state", None)  # Successful recheck precharge consumed the durable exemption.
    assert ledger.reserve(route, 100, False, version="8.0.63")[1] == "bootstrap_trial"


def test_local_budget_loss_and_proof_survive_failed_snapshot_writes(monkeypatch, tmp_path):
    import sqlite3
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from core.agent_core.ai import thread_bootstrap as ledger

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", raising=False)
    routes = [bytes([i]) * 32 for i in range(6)]
    assert ledger.reserve(routes[0], 100, True)[0] and list(tmp_path.iterdir()) == []
    path = tmp_path / "state" / "message-thread-bootstrap.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda r: ledger.reserve(r, 100, False), routes))
        assert sum(bool(trial) for trial, _ in results) == 5
        assert sum(reason == "bootstrap_daily_limit" for _, reason in results) == 1
        r, t = next((r, t) for r, (t, _) in zip(routes, results) if t)
        assert ledger.settle(r, t, "bootstrap_read_loss") == "bootstrap_read_loss"
        assert ledger.settle(r, t, "bootstrap_proven") == "bootstrap_stale"
        assert ledger.reserve(r, 100, True)[1] == "bootstrap_read_loss"
        # No shared lock, and no durable debit was possible: a new process has its own allowance.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.agent_core.ai import thread_bootstrap as b; assert sum(bool(b.reserve(bytes([i])*32,100,False)[0]) for i in range(6)) == 5",
            ],
            check=True,
            timeout=5,
        )
    assert ledger.reserve(r, 100, False)[1] == "bootstrap_read_loss"
    assert ledger.reserve(b"X" * 32, 100, False)[1] == "bootstrap_daily_limit"


def test_snapshot_can_restore_negative_evidence_but_never_proof(monkeypatch):
    from core.agent_core.ai import thread_bootstrap as ledger
    from types import SimpleNamespace
    import time

    now = [time.time()]
    monkeypatch.setattr(ledger, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "2")
    a, b = b"A" * 32, b"B" * 32
    one, _ = ledger.reserve(a, 100, False)
    two, _ = ledger.reserve(b, 100, False)
    assert ledger.settle(a, one, "bootstrap_proven") == "bootstrap_proven"
    assert ledger.settle(b, two, "bootstrap_read_loss") == "bootstrap_read_loss"
    assert ledger.reserve(a, 100, True)[1] == "bootstrap_recheck"
    monkeypatch.setattr(ledger, "_state", None)  # Fresh process, coherent hint was available.
    assert ledger.reserve(a, 100, True)[1] == "bootstrap_daily_limit"
    assert ledger.reserve(b, 100, True)[1] == "bootstrap_read_loss"
    now[0] += ledger.DAY
    assert ledger.reserve(b, 100, False)[1] == "bootstrap_trial"


def test_local_capacity_fails_closed_for_late_loss(monkeypatch):
    from core.agent_core.ai import thread_bootstrap as ledger
    from types import SimpleNamespace
    import time

    now = [time.time()]
    monkeypatch.setattr(ledger, "MAX_ROUTES", 1)
    monkeypatch.setattr(ledger, "time", SimpleNamespace(time=lambda: now[0]))
    a, b = b"A" * 32, b"B" * 32
    old, _ = ledger.reserve(a, 100, False)
    now[0] += ledger.DAY + 1
    current, _ = ledger.reserve(b, 100, False)
    assert ledger.settle(a, old, "bootstrap_read_loss") == "bootstrap_read_loss"
    assert ledger.reserve(a, 100, False)[1] == "bootstrap_capacity"
    assert ledger.settle(b, current, "bootstrap_proven") == "bootstrap_stale"
    now[0] += ledger.DAY
    assert ledger.reserve(a, 100, False)[1] == "bootstrap_trial"


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

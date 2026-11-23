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
            assert facts["bootstrap_outcome"] == ("bootstrap_read_loss" if loss else "bootstrap_proven")
        fresh = replace(req, thread_observation=ThreadObservation("another"))
        another = await send(adapter, fresh)
        await send(adapter, another)
        if loss:
            assert "thread" not in bodies[-1]
            assert another.thread_observation.attempts[0]["reason"] == "bootstrap_read_loss"
        else:
            assert bodies[-1]["thread"]["type"] == "create"  # proven route, exhausted daily budget


def test_shared_reservation_survives_restart_and_clock_boundaries(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from core.agent_core.ai import thread_bootstrap as ledger

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", raising=False)
    now = [10 * ledger.DAY + 1]
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
    assert ledger.settle(route, owner, "bootstrap_read_loss") == "bootstrap_read_loss"
    assert ledger.settle(route, owner, "bootstrap_proven") == "bootstrap_stale"
    now[0] += ledger.DAY - 1
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

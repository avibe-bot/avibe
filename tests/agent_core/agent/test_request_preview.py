"""Dry ticks protect the first-send boundary, not a second imitation of the loop.

Existing loop tests exercise effects; they cannot establish that diagnostics are
read-only or that exported bytes match the adapter's actual HTTPX request.
"""
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from core.agent_core.agent.hooks import Hooks
from core.agent_core.agent.loop import Agent
from core.agent_core.ai.provider import ModelCapabilities
from core.agent_core.ai.registry import ADAPTERS, create_adapter
from core.agent_core.harness.context import ContextConfig
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from tests.agent_core.fakes import (
    SELECTION, FakeJobHost, FakeModelRouter, FakeTool, InMemoryTranscriptStore,
    ScriptedProvider, assistant, input_row, user,
)


def agent_for(router, store, **kwargs):
    return Agent(
        session_id="session", models=router, tools=kwargs.pop("tools", ()),
        hooks=kwargs.pop("hooks", ()), store=store, jobs=FakeJobHost(),
        cwd="/test-owned", system="Keep Unicode: 中文 and café", **kwargs,
    )


@pytest.mark.parametrize("protocol", sorted(ADAPTERS))
async def test_dry_tick_matches_live_resume_transport_bytes_without_any_effect(protocol):
    sent = []

    def capture(request):
        sent.append(request)
        return httpx.Response(400, json={"error": {"message": "synthetic rejection"}})

    selected = replace(SELECTION, endpoint=replace(SELECTION.endpoint, protocol=protocol))
    store = InMemoryTranscriptStore()
    await store.consume_input("session", "input", user("Exact 中文 / quoted \" input"))
    tool = FakeTool("do_not_execute")
    before = deepcopy(store.__dict__)
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        adapter = create_adapter(protocol, client=client)
        router = FakeModelRouter(adapter, (selected,))
        agent = agent_for(router, store, tools=(tool,), context=ContextConfig())
        original_snapshot = agent.snapshot()
        preview = await agent.dry_tick(selected=selected)
        assert preview.status == "ready"
        assert preview.wire is not None
        assert preview.snapshot.context_seq == 1
        assert router.resolutions == 0 and router.protocols == [] and sent == []
        assert store.__dict__ == before and agent.snapshot() == original_snapshot
        assert tool.calls == []
        events = [event async for event in agent.resume(input_row("control", "not consumed"), turn_id="debug-test")]
        assert events[-1].reason == "error"
    assert len(sent) == 1
    assert sent[0].content == preview.wire.body
    assert sent[0].url == httpx.URL(preview.wire.wire.url)
    assert tool.calls == []


async def test_preview_cut_excludes_later_state_and_refusals_and_detaches_its_inputs():
    store = InMemoryTranscriptStore()
    await store.consume_input("session", "first", user("earlier"))
    await store.append_payload("session", "agent_state", {"version": 1, "state": {"safe": [1]}})
    await store.append_payload("session", "replay_refusal", {
        "version": 1, "source_identity": "identity", "payloads": ["hosted"],
    })
    await store.consume_input("session", "later", user("must not enter the earlier cut"))
    router = FakeModelRouter(ScriptedProvider([]))
    agent = agent_for(router, store)
    before = deepcopy(store.__dict__)
    early = await agent.dry_tick(selected=SELECTION, context_seq=2)
    now = await agent.dry_tick(selected=SELECTION)
    assert early.status == now.status == "ready"
    assert early.snapshot.state == {"safe": [1]}
    assert early.request.endpoint.opaque_refused == frozenset()
    assert now.request.endpoint.opaque_refused == frozenset({("identity", "hosted")})
    assert b"must not enter" not in early.wire.body
    assert b"must not enter" in now.wire.body
    early.snapshot.state["safe"].append(2)
    assert store.__dict__ == before
    assert router.resolutions == 0
    with pytest.raises(ValueError, match="beyond"):
        await agent.dry_tick(selected=SELECTION, context_seq=5)
    with pytest.raises(ValueError, match="nonnegative"):
        await agent.dry_tick(selected=SELECTION, context_seq=True)


@pytest.mark.parametrize("blocker", ["hook", "rehydrate", "pending"])
async def test_unpredictable_steps_are_reported_without_invoking_them(blocker):
    class UnsafeHook(Hooks):
        async def before_model(self, request, ctx):
            pytest.fail("dry_tick invoked a hook")

    def unsafe_rehydrate(state):
        pytest.fail("dry_tick invoked arbitrary rehydration")

    store = InMemoryTranscriptStore()
    await store.consume_input("session", "first", user("hello"))
    if blocker == "pending":
        await store.append_response("session", assistant("", calls=[ToolCallBlock("call", "side_effect", {})]), final=False)
    agent = agent_for(
        FakeModelRouter(ScriptedProvider([])), store,
        hooks=(UnsafeHook(),) if blocker == "hook" else (),
        rehydrate=unsafe_rehydrate if blocker == "rehydrate" else None,
    )
    before = deepcopy(store.__dict__)
    preview = await agent.dry_tick(selected=SELECTION)
    assert preview.status == ("pending_tools" if blocker == "pending" else "unsupported_hooks")
    assert preview.request is None and preview.wire is None
    assert store.__dict__ == before


@pytest.mark.parametrize("transition", ["checkpoint", "context_relief", "clear_tool_results"])
async def test_context_effects_are_planned_not_applied(transition):
    store = InMemoryTranscriptStore(clock=lambda: 1.0)
    selected = replace(SELECTION, capabilities=ModelCapabilities(
        context_window=64_000, input_limit=32_000, max_output_tokens=4096, supports_tools=True,
    ))
    if transition == "clear_tool_results":
        selected = replace(selected, capabilities=replace(
            selected.capabilities, context_window=160_000, input_limit=128_000,
        ))
        for index in range(9):
            await store.consume_input("session", f"input{index}", user("question"))
            await store.append_response("session", assistant("", calls=[ToolCallBlock(f"call{index}", "read", {})]), final=False)
            await store.append_tool_result("session", ToolResultMessage(f"call{index}", "read", (text("x" * 25_000),)), details={})
            await store.append_response("session", assistant("answer"), final=True)
    elif transition == "checkpoint":
        for index in range(4):
            await store.consume_input("session", f"input{index}", user("x" * 24_000))
            await store.append_response("session", assistant("answer"), final=True)
    else:
        await store.consume_input("session", "huge", user("x" * 200_000))
    provider = ScriptedProvider([])
    agent = agent_for(FakeModelRouter(provider), store, context=ContextConfig(
        clear_tool_results=transition == "clear_tool_results", clock=lambda: 10_000.0,
    ))
    before = deepcopy(store.__dict__)
    preview = await agent.dry_tick(selected=selected)
    assert preview.status == "needs_context_transition"
    assert preview.transition == transition
    assert preview.request is not None and preview.wire is None
    assert provider.requests == [] and store.__dict__ == before

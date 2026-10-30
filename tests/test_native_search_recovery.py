"""Transient native search cut-offs change one Turn's route, never capability evidence."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from core.agent_core.agent.models import ModelSelection, RetryPolicy
from core.agent_core.ai.provider import Done, HostedToolRoute, ModelCapabilities, ModelEndpoint, ProviderError
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    AssistantMessage, HostedAction, HostedNative, HostedRecord, HostedSummary,
    HostedToolBlock, Origin, ToolCallBlock,
)
from modules.agents.model_hub import ModelHubRuntimeRouter
from modules.agents.vibey.agent import VibeyAgent, _Run
from modules.agents.vibey.native_search_recovery import should_switch, turn_recovery_facts
from tests.agent_core.fakes import assistant
from tests.test_vibey_agent import (  # noqa: F401 -- shared real-store/Delivery harness and fixtures
    SESSION, _Harness, _NATIVE_SEARCH, continuation_policy, engine, published, session,
)


def _response(*, tool="web_search", error_kind="overloaded", pending=True, stop_reason="error", index=0):
    action = HostedAction("search", queries=("q",)) if tool == "web_search" else HostedAction("x_search", variant="keyword", query="q")
    block = HostedToolBlock(
        f"native-{index}", tool, action, HostedRecord("pending" if pending else "completed"),
        HostedNative("src_served00001", ({"type": "web_search_call"},)),
    )
    return AssistantMessage(
        (block,), Origin("openai", "openai_responses", "model"), stop_reason,
        error_message="Our servers are currently overloaded", hosted_summary=HostedSummary(), error_kind=error_kind,
    )


def _entry(message, seq=1):
    return ContextEntry("session", seq, "response", f"row-{seq}", message=message)


def _interruptions(entries):
    return turn_recovery_facts(({}, entry.message) for entry in entries)[0]


def test_counts_responses_not_calls_and_only_pending_native_calls_of_each_tool():
    repeated = _response()
    entries = [
        _entry(replace(repeated, content=(*repeated.content, replace(repeated.content[0], id="other")))),
        _entry(_response(tool="x_search"), 2),
        _entry(_response(error_kind="invalid_request"), 3),
        _entry(_response(pending=False), 4),
        _entry(_response(stop_reason="stop"), 5),
    ]
    assert _interruptions(entries) == {"web_search": 1, "x_search": 1}


@pytest.mark.parametrize("kind", ["overloaded", "server", "network", "stalled", "interrupted"])
def test_counts_transient_class_even_when_error_text_names_no_class(kind):
    message = replace(_response(error_kind=kind), error_message="upstream failed")
    assert _interruptions([_entry(message)]) == {"web_search": 1}


@pytest.mark.parametrize("kind", ["auth", "invalid_request", "rate_limit", "unknown", "aborted", "overflow"])
def test_nontransient_class_wins_over_provider_wording(kind):
    assert not _interruptions([_entry(_response(error_kind=kind))])


def test_released_explicit_overload_rows_still_count_but_ambiguous_errors_do_not():
    assert _interruptions([_entry(_response(error_kind=None))]) == {"web_search": 1}
    assert not _interruptions([_entry(replace(_response(error_kind=None), error_message="failed"))])


def test_switch_threshold_and_last_retry_override():
    assert [should_switch(count) for count in range(1, 5)] == [False, False, False, True]
    assert all(should_switch(count, last_permitted_retry=True) for count in range(1, 5))
    assert not should_switch(0, last_permitted_retry=True)


def _selection():
    return ModelSelection(ModelEndpoint(
        "openai_responses", "http://hub.invalid", "model", "test-token", provider="openai",
        hosted_tools={tool: HostedToolRoute("native", True, {"type": tool}) for tool in ("web_search", "x_search")},
    ), ModelCapabilities())


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("tool", ["web_search", "x_search"])
async def test_hop_resolution_is_tool_and_turn_scoped_without_refusal_writes(count, tool):
    selection = _selection()
    store = SimpleNamespace(load_turn_response_metadata=AsyncMock(side_effect=lambda sid, tid: (
        [({}, _response(tool=tool)) for _ in range(count)] if tid == "old" else []
    )))
    agent = object.__new__(VibeyAgent)
    agent.store = store
    candidate = Mock(return_value=True)
    agent.controller = SimpleNamespace(model_hub_runtime=SimpleNamespace(has_proxied_hosted_candidate=candidate))
    turn = _Run(None, "session", "old", "test")
    resolved = await agent._native_search_recovery_route(turn, selection)
    assert resolved.endpoint.hosted_tools[tool].mode == ("proxy" if count >= 4 else "native")
    assert resolved.endpoint.hosted_tools["x_search" if tool == "web_search" else "web_search"].mode == "native"
    assert not any(route.refused for route in resolved.endpoint.hosted_tools.values())
    assert all(route.mode == "native" for route in selection.endpoint.hosted_tools.values())
    turn.turn_id = "new"
    assert await agent._native_search_recovery_route(turn, selection) is selection


@pytest.mark.parametrize("available", [False, True])
async def test_last_permitted_hop_switches_only_with_a_frozen_proxy_candidate(available):
    agent = object.__new__(VibeyAgent)
    agent.store = SimpleNamespace(load_turn_response_metadata=AsyncMock(return_value=[({}, _response())]))
    agent.controller = SimpleNamespace(model_hub_runtime=SimpleNamespace(has_proxied_hosted_candidate=lambda *_: available))
    turn = _Run(None, "session", "turn", "test", continuations=5, continuation_started_at=0,
                agent=SimpleNamespace(last_attempt_response=_response(), retry=RetryPolicy()))
    route = (await agent._native_search_recovery_route(turn, _selection())).endpoint.hosted_tools["web_search"]
    assert route.mode == ("proxy" if available else "native")
    assert route.refused is False


@pytest.mark.parametrize("enabled", [False, True])
def test_proxy_candidate_read_uses_the_turns_frozen_enabled_executor_list(enabled):
    runtime = object.__new__(ModelHubRuntimeRouter)
    frozen = SimpleNamespace(executors={"web_search": (SimpleNamespace(enabled=enabled),)})
    runtime.turn_gateway = SimpleNamespace(correlation=SimpleNamespace(turn_hosted_tools=lambda _: frozen))
    assert runtime.has_proxied_hosted_candidate("turn", "web_search") is enabled
    assert not runtime.has_proxied_hosted_candidate("turn", "x_search")


@pytest.mark.parametrize("platform,language", [("avibe", "en"), ("telegram", "zh")])
@pytest.mark.parametrize("budget,failures", [("normal", 4), ("elapsed", 3), ("long-call", 2), ("cap", 1)])
async def test_actual_continuations_switch_keep_proxy_for_later_hops_and_reset_next_turn(
    engine, session, tmp_path, published, continuation_policy, monkeypatch, budget, failures, platform, language,
):
    """Real loop, persisted response metadata, fresh hops, gateway function and committed card/IM line."""
    continuation_policy(initial_delay_s=0)
    clock = [0.0]
    monkeypatch.setattr("modules.agents.vibey.agent.time", SimpleNamespace(monotonic=lambda: clock[0]))
    if budget == "cap":
        monkeypatch.setattr("modules.agents.vibey.agent._MAX_AUTO_CONTINUATIONS", 1)
    scripts = []
    for index in range(failures):
        async def fail(request, cancel, index=index):
            clock[0] += 67 if budget == "elapsed" else (121 if budget == "long-call" else 0)
            yield ProviderError("overloaded", "Our servers are currently overloaded", False,
                                partial=_response(index=index), continuable=True)
        scripts.append(fail)
    scripts += [
        [Done(assistant(calls=(ToolCallBlock("proxy-call", "web_search", {"query": "q"}),)))],
        [Done(assistant("recovered"))], [Done(assistant("next turn"))],
    ]
    answer = {"record": {"status": "completed", "sources": []}, "execution": {
        "mode": "proxy", "executor_origin": {"provider": "exa", "api": "search_api", "model": "exa-search"},
        "executor_source_id": "src_exakeyless", "executor_billing": "metered",
    }}
    proxy_requests = []

    def gateway(request):
        proxy_requests.append(request)
        return httpx.Response(200, json=answer)

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        harness = _Harness(engine, tmp_path, platform, scripts, language=language, gateway_client=client)
        harness.controller.hub_hosted_tools = {"web_search": _NATIVE_SEARCH}
        harness.controller.model_hub_runtime.has_proxied_hosted_candidate = lambda *_: True
        request = harness.request("search")
        await harness.agent.handle_message(request)
        assert not harness.controller.terminals[-1]["is_error"]
        restarted = harness.new_agent()
        resumed = _Run(None, SESSION, request.context.platform_specific["turn_token"], "restart")
        # A restarted adapter has no live episode budget. It reads only response
        # metadata, not canonical bodies or tool results, to retain an early switch.
        with monkeypatch.context() as patch:
            patch.setattr("storage.agent_transcript._message", Mock(side_effect=AssertionError("body decoded")))
            recovered = await restarted._native_search_recovery_route(resumed, _selection())
        assert recovered.endpoint.hosted_tools["web_search"].mode == "proxy"
        await restarted.handle_message(harness.request("next"))

    routes = [request.endpoint.hosted_tools["web_search"] for request in harness.provider.requests]
    assert [route.mode for route in routes] == ["native"] * failures + ["proxy", "proxy", "native"]
    assert not any(route.refused for route in routes)
    assert len(proxy_requests) == 1
    assert proxy_requests[0].url.path.endswith("/hosted-tools/web_search")
    assert all(entry.kind != "replay_refusal" for entry in await harness.context_rows())
    from sqlalchemy import select
    from storage.models import agent_events
    with engine.connect() as conn:
        events = list(conn.execute(
            select(agent_events.c.content_json, agent_events.c.content_text).where(agent_events.c.event_type == "tool_call")
        ))
    cards = [json.loads(row.content_json)["hosted"] for row in events]
    proxy = next(card for card in cards if card["mode"] == "proxy")
    assert proxy["status"] == "completed"
    assert proxy["route_switch"] == "native_interrupted" and "fallback_from" not in proxy
    expected = ("Native search was interrupted; switched to Exa (free)." if language == "en"
                else "自带搜索中断，已改用 Exa（免费）。")
    assert any(expected in row.content_text for row in events)


@pytest.mark.parametrize("proxy_available", [False, True])
async def test_exhausted_budget_does_not_reopen_after_a_proxy_failure(
    engine, session, tmp_path, published, continuation_policy, monkeypatch, proxy_available,
):
    continuation_policy(initial_delay_s=0)
    monkeypatch.setattr("modules.agents.vibey.agent._MAX_AUTO_CONTINUATIONS", 1)
    native = ProviderError("overloaded", "overloaded", False, partial=_response(), continuable=True)
    generic = ProviderError("server", "failed again", False, continuable=True)
    harness = _Harness(engine, tmp_path, "avibe", [[native], [generic]])
    harness.controller.hub_hosted_tools = {"web_search": _NATIVE_SEARCH}
    harness.controller.model_hub_runtime.has_proxied_hosted_candidate = lambda *_: proxy_available
    await harness.agent.handle_message(harness.request("search"))
    assert len(harness.provider.requests) == 2
    assert harness.controller.terminals[-1]["is_error"]


@pytest.mark.parametrize("native", [False, True])
async def test_success_ends_failure_episode_before_a_later_overload(
    engine, session, tmp_path, published, continuation_policy, monkeypatch, native,
):
    continuation_policy(initial_delay_s=0)
    monkeypatch.setattr("modules.agents.vibey.agent._MAX_AUTO_CONTINUATIONS", 2)
    clock = [0.0]
    monkeypatch.setattr("modules.agents.vibey.agent.time", SimpleNamespace(monotonic=lambda: clock[0]))
    first = ProviderError("overloaded", "overloaded", False,
                          partial=_response() if native else None, continuable=True)

    async def later(request, cancel):
        clock[0] += 130
        yield ProviderError("overloaded", "overloaded", False,
                            partial=_response(index=2) if native else None, continuable=True)

    harness = _Harness(engine, tmp_path, "avibe", [
        [first], [Done(assistant(calls=(ToolCallBlock("echo-1", "echo", {}),)))],
        later, [Done(assistant("recovered"))],
    ])
    harness.controller.hub_hosted_tools = {"web_search": _NATIVE_SEARCH}
    harness.controller.model_hub_runtime.has_proxied_hosted_candidate = lambda *_: True
    await harness.agent.handle_message(harness.request("work"))
    assert not harness.controller.terminals[-1]["is_error"]
    assert [r.endpoint.hosted_tools["web_search"].mode for r in harness.provider.requests] == ["native"] * 4


@pytest.mark.parametrize("partial", [False, True])
async def test_an_unrelated_latest_failure_gets_no_search_reserve(
    engine, session, tmp_path, published, continuation_policy, monkeypatch, partial,
):
    continuation_policy(initial_delay_s=0)
    clock = [0.0]
    monkeypatch.setattr("modules.agents.vibey.agent.time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def unrelated(request, cancel):
        clock[0] += 130
        yield ProviderError("server", "server error", False, continuable=True,
                            partial=replace(assistant("partial"), stop_reason="error", error_kind="server") if partial else None)

    harness = _Harness(engine, tmp_path, "avibe", [
        [ProviderError("overloaded", "overloaded", False, partial=_response(), continuable=True)], unrelated,
    ])
    harness.controller.hub_hosted_tools = {"web_search": _NATIVE_SEARCH}
    harness.controller.model_hub_runtime.has_proxied_hosted_candidate = lambda *_: True
    await harness.agent.handle_message(harness.request("work"))
    assert harness.controller.terminals[-1]["is_error"]
    assert len(harness.provider.requests) == 2
    assert all(r.endpoint.hosted_tools["web_search"].mode == "native" for r in harness.provider.requests)


@pytest.mark.parametrize("budget", ["cap", "elapsed"])
async def test_only_one_reserve_per_episode_even_if_a_different_native_tool_then_fails(
    engine, session, tmp_path, published, continuation_policy, monkeypatch, budget,
):
    continuation_policy(initial_delay_s=0)
    if budget == "cap":
        monkeypatch.setattr("modules.agents.vibey.agent._MAX_AUTO_CONTINUATIONS", 1)
    clock = [0.0]
    monkeypatch.setattr("modules.agents.vibey.agent.time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def native_failure(request, cancel):
        clock[0] += 130 if budget == "elapsed" else 0
        yield ProviderError("overloaded", "overloaded", False, partial=_response(), continuable=True)

    harness = _Harness(engine, tmp_path, "avibe", [
        [ProviderError("server", "server error", False, continuable=True)], native_failure,
        [ProviderError("overloaded", "overloaded", False, partial=_response(tool="x_search"), continuable=True)],
    ])
    harness.controller.hub_hosted_tools = {"web_search": _NATIVE_SEARCH, "x_search": _NATIVE_SEARCH}
    harness.controller.model_hub_runtime.has_proxied_hosted_candidate = lambda *_: True
    await harness.agent.handle_message(harness.request("work"))
    assert harness.controller.terminals[-1]["is_error"]
    assert len(harness.provider.requests) == 3
    assert [r.endpoint.hosted_tools["web_search"].mode for r in harness.provider.requests] == ["native", "native", "proxy"]
    assert all(r.endpoint.hosted_tools["x_search"].mode == "native" for r in harness.provider.requests)


@pytest.mark.parametrize("reserve", [False, True])
async def test_in_place_retry_cannot_restore_native_after_an_early_switch(
    engine, session, tmp_path, published, continuation_policy, monkeypatch, reserve,
):
    continuation_policy(initial_delay_s=0)
    monkeypatch.setattr("modules.agents.vibey.agent._MAX_AUTO_CONTINUATIONS", 1)
    native = ProviderError("overloaded", "overloaded", False, partial=_response(), continuable=True)
    scripts = [[ProviderError("server", "server error", False, continuable=True)]] if reserve else []
    scripts += [
        [native], [ProviderError("server", "503 before output", True)],
        [Done(assistant(calls=(ToolCallBlock("echo-after-retry", "echo", {}),)))],
        [Done(assistant("recovered"))],
    ]
    harness = _Harness(engine, tmp_path, "avibe", scripts)
    harness.controller.hub_hosted_tools = {"web_search": _NATIVE_SEARCH}
    harness.controller.model_hub_runtime.has_proxied_hosted_candidate = lambda *_: True
    await harness.agent.handle_message(harness.request("work"))
    assert not harness.controller.terminals[-1]["is_error"]
    assert [r.endpoint.hosted_tools["web_search"].mode for r in harness.provider.requests] == (
        ["native"] * (2 if reserve else 1) + ["proxy"] * 3
    )

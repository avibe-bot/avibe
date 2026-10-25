"""MH-HOSTED-GATEWAY-004/005: the gateway's proxy executor for hosted tools (``vibey-hosted-tools.md`` §3.4).

Each case posts a proxied call to the real gateway endpoint for a Vibey turn,
with a recording engine adapter playing the executor provider's own response
(shapes from the 2026-10-13 spike and a live check of these requests). The
answer must be the canonical record and execution, in the C-1 encoding of
``ToolResultMessage.hosted``.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
from dataclasses import replace
from datetime import timedelta

import aiohttp
import pytest

from config.v2_config import (
    KEYLESS_EXA_SOURCE_ID,
    ModelHubBackendModelConfig,
    ModelHubHostedToolConfig,
    ModelHubModelConfig,
)
from core.agent_core.messages import HostedCost, message_from_dict
from core.handlers.model_hub import search_chain as search_chain_module
from core.handlers.model_hub import service as service_module
from core.handlers.model_hub.adapter import DiscoveredModel, InvokeCancelledError, RawOutcomeKind, UpstreamRejection
from core.handlers.model_hub.capability_evidence import EvidenceFeature
from core.handlers.model_hub.hosted_executor import ResponsesReadout, anthropic_result, excerpt, responses_result
from core.handlers.model_hub import hosted_tools as hosted_tools_module
from core.handlers.model_hub.pricing import PriceTable
from core.handlers.model_hub.stream_wire import ProtocolSSEState, ProtocolUsageReport
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.handlers.model_hub.usage import BoundedUsageLedger, UsageWriter
from modules.agents.model_hub import ModelHubRuntimeRouter
from core.run_settlement import SETTLED_BY_STOPPED
from tests.test_model_hub_l3 import (
    NOW,
    BlockingLiveInvokeHandle,
    InvokeHandle,
    LiveInvokeHandle,
    _outcome,
    _source,
    _usage_of,
)
from tests.test_model_hub_hosted_evidence import latest_ok
from tests.test_model_hub_vibey_consumer import _vibey_service

XAI = "src_xaikey0001"
OPENAI = "src_openai0001"
ANTHROPIC = "src_anthkey0001"
POST_1 = "https://x.com/OpenAI/status/2107909792945832070"
POST_2 = "https://x.com/OpenAI/status/2107895006350791071"
PAGE_A = "https://github.com/router-for-me/CLIProxyAPI/releases"
PAGE_B = "https://newreleases.io/project/github/router-for-me/CLIProxyAPI/release/v8.0.27"


def _event(payload: dict) -> bytes:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()


def _cited(lines: list[tuple[str, str, int]]) -> dict:
    """An output_text whose lines each end in an xAI-style ``[[n]](url)`` citation."""

    text, notes = "", []
    for number, (sentence, url, _) in enumerate(lines, start=1):
        marker = f"[[{number}]]({url})"
        text += ("\n" if text else "") + f"{number}. {sentence}"
        notes.append({"type": "url_citation", "url": url, "start_index": len(text),
                      "end_index": len(text) + len(marker), "title": str(number)})
        text += marker
    return {"type": "output_text", "text": text, "annotations": notes}


def _responses(*items: dict, usage: dict | None = None) -> tuple[bytes, ...]:
    frames = [_event({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})]
    frames += [_event({"type": "response.output_item.done", "item": item}) for item in items]
    frames.append(_event({"type": "response.completed", "response": {
        "id": "resp_1", "status": "completed", "output": list(items), "usage": usage or {"input_tokens": 9},
    }}))
    return tuple(frames)


WEB_CALL = {"type": "web_search_call", "id": "ws_1", "status": "completed",
            "action": {"type": "search", "query": "CLIProxyAPI latest release",
                       "sources": [{"type": "url", "url": PAGE_A}, {"type": "url", "url": PAGE_B}]}}
WEB_ANSWER = {"type": "message", "role": "assistant", "content": [_cited([
    ("v8.0.27 is the latest release. It adds a streaming keepalive.", PAGE_B, 0),
])]}
# A citation without a URL is not a source.
WEB_ANSWER["content"][0]["annotations"].append(
    {"type": "url_citation", "url": "", "start_index": 0, "end_index": 2, "title": "0"},
)
X_CALL = {"type": "custom_tool_call", "name": "x_keyword_search", "call_id": "xs_1", "id": "ctc_1",
          "input": json.dumps({"query": "from:OpenAI", "limit": 5, "mode": "Latest"})}
X_ANSWER = {"type": "message", "role": "assistant", "content": [_cited([
    ("OpenAI shared progress on ChatGPT for Teens. They will keep building.", POST_1, 0),
    ("A page about the release.", PAGE_A, 0),
    ("GPT-6 with Intelligent UI rolls out globally.", POST_2, 0),
])]}


def _sources(*extra):
    xai = _source(XAI, "xAI key", vendor="xai", model_id="grok-4.6")
    openai = _source(OPENAI, "OpenAI key", vendor="openai", model_id="gpt-6-astra")
    anthropic = _source(ANTHROPIC, "Anthropic key", vendor="anthropic", protocol="anthropic",
                        model_id="claude-haiku-4-5")
    return [xai, openai, anthropic, *extra]


# The vendors' executor models these tests run with, in place of the bundled overlay's (vibey-hosted-tools.md §6).
_EXECUTOR_MODELS: dict[str, tuple[str, ...]] = {}


@pytest.fixture(autouse=True)
def _executor_models(monkeypatch):
    _EXECUTOR_MODELS.clear()
    monkeypatch.setattr(
        hosted_tools_module, "source_executor_models", lambda vendor, protocol: _EXECUTOR_MODELS.get(vendor, ()),
    )
    # These gateway tests exercise model executors and their fallback/error records. The active keyless Exa
    # service is covered at the owning search-chain boundary in test_model_hub_search_chain; keep it out of
    # this model-only fixture without changing the shipped activation predicate.
    original_service = hosted_tools_module.model_hub_search_service

    def without_keyless(source):
        if source.id == KEYLESS_EXA_SOURCE_ID:
            return None
        return original_service(source)

    monkeypatch.setattr(hosted_tools_module, "model_hub_search_service", without_keyless)
    monkeypatch.setattr(search_chain_module, "model_hub_search_service", without_keyless)


def _bind(service, tool, executor):
    """Turn the tool on with this Source first in its executor chain, on this executor model; None for no chain."""

    config = service.store.config
    config.hosted_tools[tool] = ModelHubHostedToolConfig(
        enabled=True, executor_order=(executor[0],) if executor else None,
    )
    if executor is None:
        return
    source = next(item for item in config.sources if item.id == executor[0])
    _EXECUTOR_MODELS[source.vendor] = (executor[1],)


async def _call(service, tool, body, *, token=None, after_launch=None, language="en"):
    gateway = ModelHubTurnGateway(service, language_provider=lambda: language)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:proxy", turn_id="turn-proxy")
        if after_launch is not None:
            after_launch(hop)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/hosted-tools/{tool}',
                headers={"Authorization": f'Bearer {token or hop["token"]}', **hop["request_headers"]},
                data=body if isinstance(body, bytes) else json.dumps(body),
            ) as response:
                return response.status, await response.json(content_type=None)
    finally:
        await gateway.close()


def _hosted_result(tool, answer):
    """The answer is exactly what Vibey stores as ``ToolResultMessage.hosted``."""

    hosted = {key: answer[key] for key in ("record", "execution") if key in answer}
    record = hosted["record"]
    if tool == "image_generation":
        hosted["record"] = {**record, "images": []}
    message = message_from_dict({
        "role": "tool_result", "tool_call_id": "call_1", "tool_name": tool,
        "content": [{"type": "text", "text": "rendered by Vibey"}],
        "is_error": record["status"] != "completed", "hosted": hosted,
    })
    return message.hosted


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "executor", "arguments", "frames", "body_fields", "sources"), [
    (
        "web_search", (XAI, "grok-4.6"), {"query": "CLIProxyAPI latest release"},
        _responses({"type": "reasoning", "id": "rs_1", "summary": []}, WEB_CALL, WEB_ANSWER),
        {"tools": [{"type": "web_search"}]},
        [{"url": PAGE_B, "snippet": "v8.0.27 is the latest release. It adds a streaming keepalive."}, {"url": PAGE_A}],
    ),
    (
        "web_search", (OPENAI, "gpt-6-astra"), {"query": "CLIProxyAPI latest release"},
        _responses(WEB_CALL, WEB_ANSWER),
        {"tools": [{"type": "web_search"}], "include": ["web_search_call.action.sources"]},
        [{"url": PAGE_B, "snippet": "v8.0.27 is the latest release. It adds a streaming keepalive."}, {"url": PAGE_A}],
    ),
    (
        # X search has no per-call results: the record lists the cited posts, and only posts.
        "x_search", (XAI, "grok-4.6"), {"query": "from:OpenAI within_time:7d", "limit": 5, "mode": "Latest"},
        _responses(X_CALL, {"type": "reasoning", "id": "tco_1", "encrypted_content": "opaque"}, X_ANSWER),
        {"tools": [{"type": "x_search"}]},
        [{"url": POST_1, "snippet": "OpenAI shared progress on ChatGPT for Teens. They will keep building."},
         {"url": POST_2, "snippet": "GPT-6 with Intelligent UI rolls out globally."}],
    ),
])
async def test_a_proxied_search_runs_one_native_request_and_answers_its_canonical_record(
    tmp_path, tool, executor, arguments, frames, body_fields, sources,
):
    """MH-HOSTED-GATEWAY-004: the chain's first executor, no effort, the overlay declaration forced, excerpts,
    metering, and the same positive evidence a native call writes."""

    usage = ProtocolUsageReport.of(input_tokens=5369, cached_input_tokens=256, output_tokens=307)
    served = replace(
        _outcome(RawOutcomeKind.SUCCESS, source_id=executor[0], usage=usage), hosted_tools=frozenset({tool}),
    )
    service = _vibey_service(tmp_path, _sources(), handles=[LiveInvokeHandle(served, frames)])
    _bind(service, tool, executor)
    admitting_threads = []
    admit = service._admitted_executor

    def recorded(*args):
        admitting_threads.append(threading.current_thread())
        return admit(*args)

    service._admitted_executor = recorded
    status, answer = await _call(service, tool, {"arguments": arguments})
    assert status == 200, answer
    # Every admission snapshot, including the active mixed-chain classification, stays off the loop.
    assert len(admitting_threads) >= 2 and threading.main_thread() not in admitting_threads
    assert service.adapter.invocations == [(executor[0], executor[1], "vibey")]
    sent = service.adapter.requests[0]
    assert sent.protocol == "openai_responses"
    assert "reasoning" not in sent
    # Forced: a model answering from memory runs no search.
    assert sent["tool_choice"] == "required"
    assert {key: sent[key] for key in body_fields} == body_fields
    assert set(sent) - set(body_fields) == {
        "model", "instructions", "input", "tool_choice", "store", "max_output_tokens",
    }
    query = sent["input"][0]["content"]
    assert query.startswith(arguments["query"])
    if tool == "x_search":
        assert "limit 5" in query and "mode Latest" in query
    assert answer == {
        "record": {"status": "completed", "sources": sources},
        "execution": {
            "mode": "proxy",
            "executor_origin": {"provider": "xai" if executor[0] == XAI else "openai",
                                "api": "openai_responses", "model": executor[1]},
            "executor_source_id": executor[0],
            "executor_billing": "metered",
        },
    }
    assert _hosted_result(tool, answer).execution.executor_source_id == executor[0]
    # Metered on the executor Source, like any gateway call.
    assert _usage_of(service, executor[0])
    assert latest_ok(service, tool) == (executor[0], executor[1], "executor")


@pytest.mark.asyncio
async def test_an_anthropic_executor_is_buffered_and_keeps_the_providers_results_and_cited_text(tmp_path):
    """MH-HOSTED-GATEWAY-004: Anthropic results keep their order and ages; a cited one gains its cited_text."""

    body = json.dumps({
        "type": "message", "role": "assistant", "stop_reason": "end_turn",
        "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": PAGE_A, "title": "Releases", "page_age": "0 days ago",
                 "encrypted_content": "opaque"},
                {"type": "web_search_result", "url": PAGE_B, "title": "v8.0.27", "encrypted_content": "opaque"},
            ]},
            {"type": "text", "text": "The latest is v8.0.27.", "citations": [
                {"type": "web_search_result_location", "url": PAGE_B, "title": "v8.0.27",
                 "cited_text": "v8.0.27  released  2026-10-12", "encrypted_index": "opaque"},
            ]},
        ],
        "usage": {"input_tokens": 2671, "output_tokens": 242, "server_tool_use": {"web_search_requests": 1}},
    }).encode()
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=ANTHROPIC), (body,)),
    ])
    _bind(service, "web_search", (ANTHROPIC, "claude-haiku-4-5"))
    status, answer = await _call(service, "web_search", {
        "arguments": {"query": "CLIProxyAPI latest release"},
    })
    assert status == 200
    sent = service.adapter.requests[0]
    assert sent.protocol == "anthropic"
    assert set(sent) == {"model", "max_tokens", "system", "messages", "tools"}
    assert sent["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    assert answer["record"] == {"status": "completed", "sources": [
        {"url": PAGE_A, "title": "Releases", "page_age": "0 days ago"},
        {"url": PAGE_B, "title": "v8.0.27", "snippet": "v8.0.27 released 2026-10-12"},
    ]}
    assert answer["execution"]["executor_origin"] == {
        "provider": "anthropic", "api": "anthropic", "model": "claude-haiku-4-5",
    }


ANTHROPIC_SEARCH = {
    "type": "message", "role": "assistant", "stop_reason": "end_turn",
    "content": [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": PAGE_A, "title": "Releases", "encrypted_content": "opaque"},
        ]},
        {"type": "text", "text": "See the releases page."},
    ],
    "usage": {"input_tokens": 2000, "output_tokens": 100, "server_tool_use": {"web_search_requests": 2}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "executor", "billing", "response", "cost"), [
    # xAI's own figure for the whole response, in ticks of 1e-10 USD.
    (
        "x_search", (XAI, "grok-4.6"), "metered",
        _responses(X_CALL, X_ANSWER, usage={"input_tokens": 2000, "output_tokens": 100, "cost_in_usd_ticks": 1_234_000_000}),
        {"basis": "reported", "usd": 0.1234},
    ),
    # The list price: 2,000 input and 100 output tokens at $1 and $5 per million,
    # plus the two searches Anthropic counted at $10 per thousand.
    ("web_search", (ANTHROPIC, "claude-haiku-4-5"), "metered", ANTHROPIC_SEARCH, {"basis": "list", "usd": 0.0225}),
    ("web_search", (ANTHROPIC, "claude-haiku-4-5"), "monthly", ANTHROPIC_SEARCH, {"basis": "included"}),
    # OpenAI reports no search counter, so its per-call fee, and so the call, cannot be valued.
    (
        "web_search", (OPENAI, "gpt-6-astra"), "metered",
        _responses(WEB_CALL, WEB_ANSWER, usage={"input_tokens": 2000, "output_tokens": 100}),
        None,
    ),
    # A provider's number past its bound is refused where it enters (#2572): the call still completes, and a cost
    # that needs the number is unknown, never an error. A search counter past the bound counts no searches to value.
    (
        "web_search", (ANTHROPIC, "claude-haiku-4-5"), "metered",
        {**ANTHROPIC_SEARCH, "usage": {"input_tokens": 2000, "output_tokens": 100,
                                       "server_tool_use": {"web_search_requests": 10**400}}},
        None,
    ),
    # xAI's own figure past the bound is no figure, and nothing else prices this call.
    (
        "x_search", (XAI, "grok-4.6"), "metered",
        _responses(X_CALL, X_ANSWER, usage={"input_tokens": 2000, "output_tokens": 100, "cost_in_usd_ticks": 10**400}),
        None,
    ),
])
async def test_a_proxied_call_carries_the_pricing_modules_cost(tmp_path, tool, executor, billing, response, cost):
    """MH-HOSTED-GATEWAY-004: the cost is included, the provider's own figure, or the list price; absent when unknown."""

    sources = [replace(source, billing=billing) if source.id == executor[0] else source for source in _sources()]
    frames = response if isinstance(response, tuple) else (json.dumps(response).encode(),)
    tokens = ProtocolUsageReport.of(input_tokens=2000, cached_input_tokens=0, output_tokens=100)
    service = _vibey_service(tmp_path, sources, handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=executor[0], usage=tokens), frames),
    ])
    prices = PriceTable(
        catalog={"anthropic": {"models": {"claude-haiku-4-5": {"cost": {"input": 1, "output": 5}}}},
                 "openai": {"models": {"gpt-6-astra": {"cost": {"input": 1, "output": 5}}}}},
        vendor_map={"families": [{"prefix": "claude-", "vendor_id": "anthropic"}, {"prefix": "gpt-", "vendor_id": "openai"}]},
        overrides={}, price_table_date="2026-10-13",
    )
    service.usage_writer = UsageWriter(service.usage, price_table=lambda: prices)
    _bind(service, tool, executor)
    status, answer = await _call(service, tool, {
        "arguments": {"query": "CLIProxyAPI latest release"},
    })
    assert status == 200, answer
    assert answer["record"]["status"] == "completed"
    assert answer["execution"].get("cost") == cost
    assert _hosted_result(tool, answer).execution.cost == (HostedCost(**cost) if cost else None)




FAILED_WITH_PARTIAL = {**WEB_CALL, "status": "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "bound", "handles", "error", "sources", "executed"), [
    # No Source can run it: the record says which providers' Sources could.
    ("no_candidate", None, [], "Add a search service in Models › Tools or an API key from a vendor that offers web search", [], False),
    ("source_removed", (XAI, "grok-4.6"), [], "the search service is unavailable", [], False),
    # A new base URL or credential is a new identity, which the turn's chain was not built for.
    ("source_identity_changed", (XAI, "grok-4.6"), [], "the search service is unavailable", [], False),
    # The only candidate failed: its failure, and the Sources tried.
    ("provider_refused", (XAI, "grok-4.6"),
     [InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=XAI))],
     "refused the call (HTTP 429 rate_limit_error). Tried: xAI key.", [], True),
    ("tool_never_ran", (XAI, "grok-4.6"),
     [LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_ANSWER))],
     "did not run web_search", [], True),
    # A failed call keeps the partial results it returned, but stays failed.
    ("call_failed_with_partial_results", (XAI, "grok-4.6"),
     [LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(FAILED_WITH_PARTIAL))],
     "web_search call failed", [{"url": PAGE_A}, {"url": PAGE_B}], True),
    # The call's outcome failed after its items arrived: the outcome wins.
    ("stream_failed_after_items", (XAI, "grok-4.6"),
     [LiveInvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=200, code="server_error", source_id=XAI,
                                stream_started=True), _responses(WEB_CALL, WEB_ANSWER)[:-1])],
     "is unavailable right now (HTTP 200 server_error)",
     [{"url": PAGE_B, "snippet": "v8.0.27 is the latest release. It adds a streaming keepalive."}, {"url": PAGE_A}],
     True),
])
async def test_a_proxied_call_that_cannot_complete_is_a_failed_record(
    tmp_path, case, bound, handles, error, sources, executed,
):
    """MH-HOSTED-GATEWAY-005: never a silent or partial success; an execution only when an executor ran."""

    service = _vibey_service(tmp_path, _sources(), handles=handles)
    _bind(service, "web_search", bound)

    def remove_executor(_hop):
        if case == "source_removed":
            service.store.config.sources = [item for item in service.store.config.sources if item.id != XAI]
        if case == "source_identity_changed":
            service.store.config.sources = [
                replace(item, base_url="https://relay.example/v1") if item.id == XAI else item
                for item in service.store.config.sources
            ]

    status, answer = await _call(
        service, "web_search", {"arguments": {"query": "q"}}, after_launch=remove_executor,
    )
    assert status == 200
    assert answer["record"]["status"] == "failed"
    assert error in answer["record"]["error"]
    # Each reason the gateway words carries the code its reader's surface shows it by (#2589).
    # Which providers to add is the call's own fact: that reason has no code and is shown as written.
    assert answer["record"].get("error_code") == {
        "no_candidate": None, "source_removed": "service_unavailable",
        "source_identity_changed": "service_unavailable", "provider_refused": "provider_refused",
        "tool_never_ran": "search_not_run", "call_failed_with_partial_results": "search_failed",
        "stream_failed_after_items": "provider_unavailable",
    }[case]
    assert answer["record"]["sources"] == sources
    assert ("execution" in answer) is executed
    assert len(service.adapter.invocations) == int(executed)
    assert _hosted_result("web_search", answer).record.status == "failed"


def _chain(service, tool, *executors):
    """Turn the tool on with these Sources as its executor chain, in this order, each on its executor model."""

    config = service.store.config
    config.hosted_tools[tool] = ModelHubHostedToolConfig(enabled=True, executor_order=tuple(e[0] for e in executors))
    for source_id, model_id in executors:
        _EXECUTOR_MODELS[next(item for item in config.sources if item.id == source_id).vendor] = (model_id,)


def _refusal(status, code, source_id, **fields):
    return _outcome(RawOutcomeKind.HTTP_ERROR, status=status, code=code, source_id=source_id, **fields)


def _served_by_openai():
    return LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=OPENAI), _responses(WEB_CALL, WEB_ANSWER))


BILLED = ProtocolUsageReport.of(input_tokens=900, cached_input_tokens=0, output_tokens=40)
OVERLOADED = "Our servers are currently overloaded. Please try again later."
NARROW = replace(
    _refusal(400, "invalid_request_error", XAI),
    rejection=UpstreamRejection(status=400, code="invalid_request_error", unsupported_tool_type="web_search"),
)


@pytest.mark.asyncio
async def test_a_frozen_executor_slot_runs_while_its_model_stays_listed(tmp_path):
    """MH-HOSTED-GATEWAY-005: a turn froze its slot on a fallback model; the Source then lists the model its vendor
    prefers. The frozen model is still listed and runnable, so the call runs on it instead of finding no executor."""

    body = json.dumps({
        "type": "message", "role": "assistant", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "no results"}], "usage": {"input_tokens": 10, "output_tokens": 2},
    }).encode()
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=ANTHROPIC), (body,)),
    ])
    _bind(service, "web_search", (ANTHROPIC, "claude-haiku-4-5"))
    _EXECUTOR_MODELS["anthropic"] = ("claude-haiku-5-5", "claude-haiku-4-5")

    def preferred_now_listed(_hop):
        source = next(item for item in service.store.config.sources if item.id == ANTHROPIC)
        source.models.append(ModelHubModelConfig(id="claude-haiku-5-5", provenance="discovered"))

    status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}}, after_launch=preferred_now_listed)
    assert status == 200
    assert service.adapter.invocations == [(ANTHROPIC, "claude-haiku-4-5", "vibey")]
    assert answer["execution"]["executor_origin"]["model"] == "claude-haiku-4-5"


# Each vendor's own answer for a model it does not serve, as its API sends it.
MODEL_NOT_SERVED = {
    "anthropic": (ANTHROPIC, "claude-haiku-4-5", "anthropic", {
        "type": "error", "error": {"type": "not_found_error", "message": "model: claude-haiku-5-5"},
    }),
    "openai": (OPENAI, "gpt-6-astra", "openai_responses", {
        "error": {"message": "The model `gpt-6-luna` does not exist or you do not have access to it.",
                  "type": "invalid_request_error", "param": None, "code": "model_not_found"},
    }),
}


async def _engine_outcome(protocol, status, body):
    """The outcome the real engine client reads from this answer."""

    from aiohttp import web

    from tests.test_model_hub_retry_advice import _engine, _source as _engine_source

    async def respond(_request):
        return web.json_response(body, status=status)

    async with _engine(respond) as (client, _requests):
        handle = await client.invoke(_engine_source(protocol), "model-a", {"input": "q"}, stream=False)
        return await handle.outcome()


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", sorted(MODEL_NOT_SERVED))
async def test_a_model_the_source_does_not_serve_moves_on_and_refuses_no_tool(tmp_path, vendor):
    """MH-HOSTED-GATEWAY-005: a model-not-found names the model, not the tool, so it is no refusal (§6 step 4).

    It used to write negative ``call`` evidence that never expires, silently dropping the Source from the chain.
    """

    source_id, model_id, protocol, body = MODEL_NOT_SERVED[vendor]
    not_served = replace(await _engine_outcome(protocol, 404, body), source_id=source_id)
    served = LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_CALL, WEB_ANSWER))
    service = _vibey_service(tmp_path, _sources(), handles=[InvokeHandle(not_served), served])
    _chain(service, "web_search", (source_id, model_id), (XAI, "grok-4.6"))
    status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}})
    assert (status, answer["record"]["status"], answer["execution"]["executor_source_id"]) == (200, "completed", XAI)
    source = next(item for item in service.store.config.sources if item.id == source_id)
    assert not service.hosted_evidence().unsupported(source, model_id, EvidenceFeature.hosted("web_search", "call"))
    chain = service.freeze_turn_hosted_tools(service.store.config).executors["web_search"]
    assert [slot.source_id for slot in chain] == [source_id, XAI]


def _stale_listing_service(tmp_path, handles=()):
    """Vibey with OpenAI's executor row on no route: its turns route to gpt-6-chat."""

    sources = _sources()
    for source in sources:
        # Listed recently, so only a stale mark makes a Source due.
        source.last_discovered_at = "2099-01-01T00:00:00+00:00"
    next(item for item in sources if item.id == OPENAI).models.insert(
        0, ModelHubModelConfig(id="gpt-6-chat", provenance="discovered"),
    )
    service = _vibey_service(tmp_path, sources, handles=handles)
    _chain(service, "web_search", (OPENAI, "gpt-6-astra"), (XAI, "grok-4.6"))
    service.adapter.stop = lambda: asyncio.sleep(0)
    return service


def _openai(service):
    return next(item for item in service.store.config.sources if item.id == OPENAI)


async def _settled(service):
    async with asyncio.timeout(2):
        await asyncio.sleep(0)
        while service._viewed_discovery_task is not None and not service._viewed_discovery_task.done():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("listed_again", [False, True], ids=["unlisted", "still_listed"])
async def test_a_refused_listed_executor_model_is_listed_again_and_leaves_once_unlisted(tmp_path, listed_again):
    """MH-HOSTED-GATEWAY-009: a model-not-found for an executor model its Source lists marks that Source (#2682).

    The schedule's own due rule lists a marked Source once its listing is older
    than the debounce. Only a model both the call and that listing disown
    leaves, and the rest of the listing merges as the schedule's always does.
    """

    source_id, model_id, protocol, body = MODEL_NOT_SERVED["openai"]
    not_served = replace(await _engine_outcome(protocol, 404, body), source_id=source_id)
    served = LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_CALL, WEB_ANSWER))
    service = _stale_listing_service(tmp_path, handles=[InvokeHandle(not_served), served])
    _openai(service).last_discovered_at = None
    listed = [DiscoveredModel(id="gpt-6-chat"), *([DiscoveredModel(id=model_id)] if listed_again else [])]
    listings = []

    async def discover_models(vendor, *_args, **_kwargs):
        listings.append(vendor)
        return listed

    service.adapter.discover_models = discover_models
    service.start_background_discovery()
    try:
        status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}})
        assert (status, answer["execution"]["executor_source_id"]) == (200, XAI)
        await _settled(service)
        assert listings == ["openai"]
        # The Source was just listed, so the same refusal again lists nothing.
        service.mark_stale_listing(_openai(service), model_id)
        await _settled(service)
        assert listings == ["openai"]
    finally:
        await service.stop()
    assert [model.id for model in _openai(service).models] == (["gpt-6-chat", model_id] if listed_again else ["gpt-6-chat"])
    chain = service.freeze_turn_hosted_tools(service.store.config).executors["web_search"]
    assert [slot.source_id for slot in chain] == ([source_id, XAI] if listed_again else [XAI])


@pytest.mark.asyncio
async def test_a_stale_mark_is_answered_by_a_running_listing_and_keeps_the_schedules_backoff(tmp_path):
    """MH-HOSTED-GATEWAY-009: a listing already running answers the mark, and a failing one keeps its backoff."""

    service = _stale_listing_service(tmp_path)
    _openai(service).last_discovered_at = None
    base = service.now()
    clock = [base]
    service.now = lambda: clock[0]
    release = asyncio.Event()
    fail = False
    listings = []

    async def discover_models(vendor, *_args, **_kwargs):
        listings.append(vendor)
        await release.wait()
        if fail:
            raise RuntimeError("listing unavailable")
        return [DiscoveredModel(id="gpt-6-chat")]

    service.adapter.discover_models = discover_models
    service.start_background_discovery()
    try:
        # A viewer's pass is listing the Source when the refusal arrives: that listing answers it.
        service.request_inventory_refresh()
        async with asyncio.timeout(2):
            while not listings:
                await asyncio.sleep(0.01)
        service.mark_stale_listing(_openai(service), "gpt-6-astra")
        release.set()
        await _settled(service)
        assert listings == ["openai"]
        assert [model.id for model in _openai(service).models] == ["gpt-6-chat"]
        # A failing listing backs off past the debounce, whoever marks it.
        fail = True
        for minutes in (16, 32):
            clock[0] = base + timedelta(minutes=minutes)
            service.mark_stale_listing(_openai(service), "gpt-6-chat")
            await _settled(service)
        clock[0] = base + timedelta(minutes=48)
        service.mark_stale_listing(_openai(service), "gpt-6-chat")
        await _settled(service)
        assert listings == ["openai"] * 3
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_a_stale_mark_drops_nothing_once_its_source_identity_changed(tmp_path):
    """MH-HOSTED-GATEWAY-009: a refusal belongs to the identity that gave it; a replaced key or URL voids it."""

    service = _stale_listing_service(tmp_path)
    _openai(service).last_discovered_at = None

    async def discover_models(*_args, **_kwargs):
        return [DiscoveredModel(id="gpt-6-chat")]

    service.adapter.discover_models = discover_models
    service.start_background_discovery()
    try:
        service.mark_stale_listing(_openai(service), "gpt-6-astra")
        _openai(service).base_url = "https://replaced.example/v1"
        await _settled(service)
    finally:
        await service.stop()
    assert [model.id for model in _openai(service).models] == ["gpt-6-chat", "gpt-6-astra"]
    assert service._stale_listings == {}


@pytest.mark.asyncio
async def test_every_refused_row_leaves_but_a_retired_tombstone_stays(tmp_path):
    """MH-HOSTED-GATEWAY-009: refusals of two executor models both count, and a row the user retired is kept."""

    service = _stale_listing_service(tmp_path)
    openai = _openai(service)
    openai.last_discovered_at = None
    openai.models.append(ModelHubModelConfig(id="gpt-6-luna", provenance="discovered"))
    openai.models.append(ModelHubModelConfig(id="gpt-6-sol", provenance="discovered"))

    async def discover_models(*_args, **_kwargs):
        return [DiscoveredModel(id="gpt-6-chat")]

    service.adapter.discover_models = discover_models
    service.start_background_discovery()
    try:
        # Two turns frozen on different executor models are each refused.
        service.mark_stale_listing(_openai(service), "gpt-6-astra")
        service.mark_stale_listing(_openai(service), "gpt-6-luna")
        service.mark_stale_listing(_openai(service), "gpt-6-sol")
        # The user retires gpt-6-sol before the listing returns.
        next(model for model in _openai(service).models if model.id == "gpt-6-sol").retired = True
        await _settled(service)
    finally:
        await service.stop()
    models = {model.id: bool(model.retired) for model in _openai(service).models}
    assert models == {"gpt-6-chat": False, "gpt-6-sol": True}


@pytest.mark.asyncio
async def test_a_disowned_row_leaves_even_when_the_rest_of_the_listing_must_wait(tmp_path, monkeypatch):
    """MH-HOSTED-GATEWAY-009: a supply change elsewhere in the listing does not hold back the disowned row."""

    service = _stale_listing_service(tmp_path)
    _openai(service).last_discovered_at = None

    async def discover_models(*_args, **_kwargs):
        return [DiscoveredModel(id="gpt-6-chat"), DiscoveredModel(id="gpt-6-new")]

    service.adapter.discover_models = discover_models
    # Admitting gpt-6-new is the change a manual refresh must confirm.
    monkeypatch.setattr(service, "_moves_supply", lambda _previous, updated: any(
        model.id == "gpt-6-new" for source in updated.sources for model in source.models
    ))
    service.start_background_discovery()
    try:
        service.mark_stale_listing(_openai(service), "gpt-6-astra")
        await _settled(service)
    finally:
        await service.stop()
    assert [model.id for model in _openai(service).models] == ["gpt-6-chat"]


@pytest.mark.asyncio
async def test_a_disowned_row_on_a_route_does_not_hold_back_one_on_none(tmp_path):
    """MH-HOSTED-GATEWAY-009: each disowned row leaves unless its own removal moves a route (#2777)."""

    service = _stale_listing_service(tmp_path)
    openai = _openai(service)
    openai.last_discovered_at = None
    openai.models.append(ModelHubModelConfig(id="gpt-6-luna", provenance="discovered"))
    # xAI lists gpt-6-luna too, so Vibey's gpt-6-luna routes to both Sources:
    # dropping OpenAI's row would remove OpenAI's hop.
    xai = next(item for item in service.store.config.sources if item.id == XAI)
    xai.models.append(ModelHubModelConfig(id="gpt-6-luna", provenance="discovered"))
    service.store.config.agents["vibey"].models.append(ModelHubBackendModelConfig(id="gpt-6-luna"))

    async def discover_models(*_args, **_kwargs):
        return [DiscoveredModel(id="gpt-6-chat")]

    service.adapter.discover_models = discover_models
    service.start_background_discovery()
    try:
        service.mark_stale_listing(_openai(service), "gpt-6-astra")
        service.mark_stale_listing(_openai(service), "gpt-6-luna")
        await _settled(service)
    finally:
        await service.stop()
    # Only a manual refresh removes OpenAI's gpt-6-luna.
    assert [model.id for model in _openai(service).models] == ["gpt-6-chat", "gpt-6-luna"]


@pytest.mark.asyncio
async def test_a_late_refusal_under_a_replaced_identity_keeps_the_current_mark(tmp_path):
    """MH-HOSTED-GATEWAY-009: a turn still running on a replaced key or URL never evicts the current mark (#2777)."""

    service = _stale_listing_service(tmp_path)
    _openai(service).last_discovered_at = None
    replaced = replace(_openai(service))
    _openai(service).base_url = "https://replaced.example/v1"

    async def discover_models(*_args, **_kwargs):
        return [DiscoveredModel(id="gpt-6-chat")]

    service.adapter.discover_models = discover_models
    service.start_background_discovery()
    try:
        service.mark_stale_listing(_openai(service), "gpt-6-astra")
        service.mark_stale_listing(replaced, "gpt-6-astra")
        await _settled(service)
    finally:
        await service.stop()
    assert [model.id for model in _openai(service).models] == ["gpt-6-chat"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "handles", "invoked", "status", "served_by", "failed_attempts"), [
    # A failure normal routing fails over from moves on; a refusal before output that reported no tokens is unbilled.
    ("rate_limited", lambda: [InvokeHandle(_refusal(429, "rate_limit_error", XAI)), _served_by_openai()],
     [XAI, OPENAI], "completed", OPENAI, []),
    # A failure mid-response moves on too, since nothing reached Vibey yet, and it was billed.
    ("failed_mid_response", lambda: [
        LiveInvokeHandle(_refusal(200, "server_error", XAI, stream_started=True, usage=BILLED), _responses(WEB_CALL)[:-1]),
        _served_by_openai(),
    ], [XAI, OPENAI], "completed", OPENAI, [XAI]),
    # An overload is transient: it moves on, and it is no refusal.
    ("overloaded", lambda: [InvokeHandle(_refusal(200, "server_error", XAI, message=OVERLOADED)), _served_by_openai()],
     [XAI, OPENAI], "completed", OPENAI, []),
    # A narrow rule's refusal names the tool: the Source leaves the chain until the refusal is cleared.
    ("narrow_rule_refused", lambda: [InvokeHandle(NARROW), _served_by_openai()], [XAI, OPENAI], "completed", OPENAI, []),
    # The model chose not to search: no Source failed, and moving on would double the cost.
    ("model_chose_not_to_run", lambda: [
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_ANSWER)),
    ], [XAI], "failed", XAI, []),
    # Any other outcome ends the chain.
    ("request_rejected", lambda: [InvokeHandle(_refusal(400, "invalid_request_error", XAI, message="Bad field"))],
     [XAI], "failed", XAI, []),
    ("every_candidate_failed", lambda: [
        InvokeHandle(_refusal(429, "rate_limit_error", XAI)), InvokeHandle(_refusal(503, "server_error", OPENAI)),
    ], [XAI, OPENAI], "failed", OPENAI, []),
])
async def test_the_chain_moves_on_only_past_a_source_failure(
    tmp_path, case, handles, invoked, status, served_by, failed_attempts,
):
    """MH-HOSTED-GATEWAY-005: the executor chain (§6 step 4), each attempt metered and listed with its own billing."""

    service = _vibey_service(tmp_path, _sources(), handles=handles())
    _chain(service, "web_search", (XAI, "grok-4.6"), (OPENAI, "gpt-6-astra"))
    answer_status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}})
    assert answer_status == 200
    assert [entry[0] for entry in service.adapter.invocations] == invoked
    assert answer["record"]["status"] == status
    assert answer["execution"]["executor_source_id"] == served_by
    assert [attempt["source_id"] for attempt in answer["execution"].get("failed_attempts", [])] == failed_attempts
    assert all(attempt["billing"] == "metered" for attempt in answer["execution"].get("failed_attempts", []))
    assert _hosted_result("web_search", answer).record.status == status
    if case == "every_candidate_failed":
        assert answer["record"]["error"].endswith("Tried: xAI key, OpenAI key.")
    xai = next(item for item in service.store.config.sources if item.id == XAI)
    refused = service.hosted_evidence().unsupported(xai, "grok-4.6", EvidenceFeature.hosted("web_search", "call"))
    assert refused is (case == "narrow_rule_refused")
    # The next turn's chain leaves a refused Source out.
    chain = service.freeze_turn_hosted_tools(service.store.config).executors["web_search"]
    assert [slot.source_id for slot in chain] == ([OPENAI] if refused else [XAI, OPENAI])


@pytest.mark.asyncio
async def test_a_call_reports_the_source_as_its_final_admission_saw_it(tmp_path):
    """MH-HOSTED-GATEWAY-005: a Source edited while the engine is prepared runs and is billed as it now stands."""

    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_CALL, WEB_ANSWER)),
    ])
    _bind(service, "web_search", (XAI, "grok-4.6"))
    prepare = service._prepare_engine_for_demand

    async def edited_meanwhile(*args, **kwargs):
        # Billing and the name are not the Source's identity, so it is still admitted.
        service.store.config.sources = [
            replace(item, billing="monthly", display_name="xAI team plan") if item.id == XAI else item
            for item in service.store.config.sources
        ]
        await prepare(*args, **kwargs)

    service._prepare_engine_for_demand = edited_meanwhile
    status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}})
    assert status == 200 and answer["record"]["status"] == "completed"
    assert (answer["execution"]["executor_billing"], answer["execution"]["cost"]) == ("monthly", {"basis": "included"})


@pytest.mark.asyncio
async def test_a_call_whose_executors_refused_the_tool_names_them_instead_of_asking_for_a_key(tmp_path):
    """MH-HOSTED-GATEWAY-005: an upstream refusal is not a missing credential (#2873): the record names the Sources
    that refused the tool and points at clearing that refusal, and nothing runs."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "web_search", (XAI, "grok-4.6"))
    xai = next(item for item in service.store.config.sources if item.id == XAI)
    await service.record_hosted_refusals(
        xai, [("grok-4.6", EvidenceFeature.hosted("web_search", "call"), "400 invalid_request_error")],
    )
    status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}})
    assert status == 200
    assert answer == {"record": {
        "status": "failed", "sources": [],
        "error": "xAI key refused web search, so no Source can run it right now. "
                 "Clear the refusal in Models › Tools to try again.",
    }}
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_failure_text_is_in_the_users_language(tmp_path):
    """MH-HOSTED-GATEWAY-005: the record's error is user-visible on the tool card, so it is localized."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "x_search", None)
    status, answer = await _call(
        service, "x_search", {"arguments": {"query": "q"}}, language="zh",
    )
    assert status == 200
    assert answer["record"]["error"] == "当前没有能运行 X 搜索的供应商。请在 模型 › 供应商与路由 中把 xAI 的 API Key 添加为供应商；存在 Vaults 里的密钥不会用于这里。"


@pytest.mark.asyncio
async def test_a_call_runs_only_a_tool_its_live_turn_froze(tmp_path):
    """MH-HOSTED-GATEWAY-005: a tool the turn did not declare is refused, unrun; turning it on mid-turn changes nothing."""

    service = _vibey_service(tmp_path, _sources())
    service.store.config.hosted_tools["web_search"] = ModelHubHostedToolConfig(enabled=False)

    def turned_on(_hop):
        _bind(service, "web_search", (XAI, "grok-4.6"))

    status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}}, after_launch=turned_on)
    assert (status, answer) == (409, {"type": "error", "error": {"type": "tool_not_enabled", "code": "tool_not_enabled"}})
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_a_call_is_metered_in_the_slice_it_ended_in(tmp_path):
    """MH-HOSTED-GATEWAY-004: a slow close of the executor's stream does not move the call's usage to a later day."""

    clock = [NOW]

    class SlowClose(LiveInvokeHandle):
        async def close_stream(self) -> None:
            clock[0] = NOW + timedelta(days=2)
            await super().close_stream()

    usage = ProtocolUsageReport.of(input_tokens=10, cached_input_tokens=0, output_tokens=1)
    service = _vibey_service(tmp_path, _sources(), handles=[
        SlowClose(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI, usage=usage), _responses(WEB_CALL, WEB_ANSWER)),
    ])
    # One clock for the call and the ledger, as in production.
    service.now = lambda: clock[0]
    service.usage = BoundedUsageLedger(tmp_path / "usage-clocked.json", now=lambda: clock[0])
    service.usage_writer = UsageWriter(service.usage)
    _bind(service, "web_search", (XAI, "grok-4.6"))
    status, _answer = await _call(service, "web_search", {
        "arguments": {"query": "q"},
    })
    assert status == 200
    rows = json.loads(service.usage.path.read_text(encoding="utf-8"))
    assert {row["day"] for row in rows} == {NOW.astimezone().date().isoformat()}


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic"])
def test_a_record_reads_a_bounded_number_of_citations(protocol):
    """MH-HOSTED-GATEWAY-004: however many citations an answer carries, its record reads a bounded number."""

    posts = [(f"Post {number}.", f"https://x.com/a/status/{number}", 0) for number in range(300)]

    def say(key, **_values):
        return key

    if protocol == "openai_responses":
        answer = {"type": "message", "role": "assistant", "content": [_cited(posts)]}
        readout = ResponsesReadout(items=[X_CALL, answer], response={"status": "completed"})
        record = responses_result("x_search", readout, say).record
    else:
        record = anthropic_result(json.dumps({"type": "message", "stop_reason": "end_turn", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
            {"type": "text", "text": "Posts.", "citations": [
                {"type": "web_search_result_location", "url": url, "cited_text": sentence} for sentence, url, _ in posts
            ]},
        ]}).encode(), say).record
    assert record["status"] == "completed"
    assert [source["url"] for source in record["sources"]] == [url for _, url, _ in posts[:256]]


@pytest.mark.asyncio
async def test_one_failed_anthropic_search_fails_the_call_and_keeps_the_results_that_arrived(tmp_path):
    """MH-HOSTED-GATEWAY-005: failure precedence is the same on both protocols."""

    body = json.dumps({
        "type": "message", "role": "assistant", "stop_reason": "end_turn",
        "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": PAGE_A, "title": "Releases", "encrypted_content": "opaque"},
            ]},
            {"type": "server_tool_use", "id": "srvtoolu_2", "name": "web_search", "input": {"query": "q2"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_2",
             "content": {"type": "web_search_tool_result_error", "error_code": "too_many_requests"}},
        ],
    }).encode()
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=ANTHROPIC), (body,)),
    ])
    _bind(service, "web_search", (ANTHROPIC, "claude-haiku-4-5"))
    status, answer = await _call(service, "web_search", {
        "arguments": {"query": "q"},
    })
    assert status == 200
    assert answer["record"]["status"] == "failed" and "too_many_requests" in answer["record"]["error"]
    assert answer["record"]["sources"] == [{"url": PAGE_A, "title": "Releases"}]


@pytest.mark.asyncio
async def test_a_turn_owns_its_proxied_calls(tmp_path):
    """MH-HOSTED-GATEWAY-005: no call starts once the turn stops admitting work, and stopping it ends one in flight."""

    blocked = BlockingLiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI))
    service = _vibey_service(tmp_path, _sources(), handles=[blocked])
    _bind(service, "web_search", (XAI, "grok-4.6"))
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    body = json.dumps({"arguments": {"query": "q"}})
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:own", turn_id="turn-own")
        async with aiohttp.ClientSession() as client:
            pending = asyncio.create_task(client.post(
                f'{hop["base_url"]}/hosted-tools/web_search',
                headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]}, data=body,
            ))
            await asyncio.wait_for(blocked.started.wait(), timeout=5)
            finished = []
            completion = gateway.finalize_turn("turn-own", settled_by=SETTLED_BY_STOPPED, finish=lambda: finished.append(1))
            assert completion is not None
            await asyncio.wait_for(completion, timeout=5)
            assert blocked.cancelled.is_set() and finished == [1]
            pending.cancel()
            # The turn no longer admits work: a new call is refused before anything runs, and before its body is
            # read, so even a malformed one is answered as the closed turn it came from.
            for data in (body, b"not json"):
                async with client.post(
                    f'{hop["base_url"]}/hosted-tools/web_search',
                    headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]}, data=data,
                ) as response:
                    assert response.status == 409
                    assert (await response.json())["error"]["code"] == "turn_not_live"
    finally:
        await gateway.close()
    assert len(service.adapter.invocations) == 1


INCOMPLETE = (
    _event({"type": "response.output_item.done", "item": WEB_CALL}),
    _event({"type": "response.incomplete", "response": {
        "id": "resp_1", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
    }}),
)
DEEP = "[" * 100_000 + "]" * 100_000
ANTHROPIC_FOUND = [{"type": "web_search_result", "url": PAGE_A, "title": "Releases"}]


def _anthropic(stop_reason, *, use_id="srvtoolu_1", found=ANTHROPIC_FOUND) -> tuple[bytes]:
    return (json.dumps({"type": "message", "stop_reason": stop_reason, "content": [
        {"type": "server_tool_use", "id": use_id, "name": "web_search", "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": found},
    ]}).encode(),)


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "executor", "chunks", "status", "error", "sources"), [
    # Success is an allow-list: a response that stopped early is incomplete and keeps what it found.
    ("responses_incomplete", (XAI, "grok-4.6"), INCOMPLETE, "incomplete", "max_output_tokens",
     [{"url": PAGE_A}, {"url": PAGE_B}]),
    ("anthropic_max_tokens", (ANTHROPIC, "claude-haiku-4-5"), (json.dumps({
        "type": "message", "stop_reason": "max_tokens", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": PAGE_A, "title": "Releases"}]},
        ]}).encode(),), "incomplete", "max_tokens", [{"url": PAGE_A, "title": "Releases"}]),
    # A finished Anthropic response must carry each search's result block.
    ("anthropic_search_without_its_result", (ANTHROPIC, "claude-haiku-4-5"), (json.dumps({
        "type": "message", "stop_reason": "end_turn", "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}},
            {"type": "text", "text": "Nothing found."},
        ]}).encode(),), "failed", "could not be read", []),
    ("responses_without_an_ending", (XAI, "grok-4.6"), INCOMPLETE[:1], "failed", "unknown",
     [{"url": PAGE_A}, {"url": PAGE_B}]),
    # Nesting past the decoder's recursion limit is an unreadable response, not a 500.
    ("responses_nested_too_deep", (XAI, "grok-4.6"), (f"data: {DEEP}\n\n".encode(),), "failed",
     "could not be read", []),
    ("anthropic_nested_too_deep", (ANTHROPIC, "claude-haiku-4-5"), (DEEP.encode(),), "failed",
     "could not be read", []),
    # A provider field that is not the text it should be reads as unknown, never as a 500.
    ("responses_status_not_text", (XAI, "grok-4.6"), (*INCOMPLETE[:1], _event({
        "type": "response.completed", "response": {"id": "resp_1", "status": {"value": "completed"}},
    })), "failed", "unknown", [{"url": PAGE_A}, {"url": PAGE_B}]),
    ("anthropic_stop_reason_not_text", (ANTHROPIC, "claude-haiku-4-5"), _anthropic({"value": "end_turn"}), "failed",
     "unknown", [{"url": PAGE_A, "title": "Releases"}]),
    ("anthropic_search_id_not_text", (ANTHROPIC, "claude-haiku-4-5"), _anthropic("end_turn", use_id={"id": 1}),
     "failed", "could not be read", [{"url": PAGE_A, "title": "Releases"}]),
    # Only a list, even an empty one, is a search's results.
    ("anthropic_results_not_a_list", (ANTHROPIC, "claude-haiku-4-5"), _anthropic("end_turn", found=None), "failed",
     "could not be read", []),
])
async def test_only_a_response_that_completed_completes_the_call(tmp_path, case, executor, chunks, status, error, sources):
    """MH-HOSTED-GATEWAY-005: completion is an allow-list on both protocols; anything unreadable fails cleanly."""

    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=executor[0]), chunks),
    ])
    _bind(service, "web_search", executor)
    answer_status, answer = await _call(service, "web_search", {
        "arguments": {"query": "q"},
    })
    assert answer_status == 200
    assert answer["record"]["status"] == status
    assert error in answer["record"]["error"]
    assert answer["record"]["error_code"] == ("provider_unreadable" if "could not be read" in error else "response_ended_early")
    assert answer["record"]["sources"] == sources
    assert _hosted_result("web_search", answer).record.status == status


@pytest.mark.asyncio
async def test_a_turn_keeps_its_tools_and_executor_order_until_the_next_turn(tmp_path):
    """MH-HOSTED-GATEWAY-005: settings apply from the next turn; later hops of a turn keep its tools and chain."""

    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_CALL, WEB_ANSWER)),
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=OPENAI), _responses(WEB_CALL, WEB_ANSWER)),
    ])
    _bind(service, "web_search", (XAI, "grok-4.6"))
    service.store.config.hosted_tools["x_search"] = ModelHubHostedToolConfig(enabled=False)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    async def search(hop, tool):
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{hop["base_url"]}/hosted-tools/{tool}',
                headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]},
                data=json.dumps({"arguments": {"query": "q"}}),
            ) as response:
                return response.status, await response.json()

    try:
        first = await router.resolve_hop("menu-alias", process_scope="vibey:freeze", turn_id="turn-freeze")
        # Mid-turn, the user puts OpenAI first for web search and turns X search on.
        _bind(service, "web_search", (OPENAI, "gpt-6-astra"))
        _bind(service, "x_search", (XAI, "grok-4.6"))
        again = await router.resolve_hop("menu-alias", process_scope="vibey:freeze", turn_id="turn-freeze")
        assert again["hosted_tools"].keys() == first["hosted_tools"].keys() == {"web_search", "image_generate"}
        status, answer = await search(again, "web_search")
        assert status == 200 and answer["execution"]["executor_source_id"] == XAI
        assert (await search(again, "x_search"))[0] == 409
        completion = router.settle_turn("turn-freeze", settled_by=SETTLED_BY_STOPPED, ts=NOW.isoformat())
        if completion is not None:
            await completion
        nxt = await router.resolve_hop("menu-alias", process_scope="vibey:freeze", turn_id="turn-next")
        assert {"web_search", "x_search"} <= nxt["hosted_tools"].keys()
        status, answer = await search(nxt, "web_search")
        assert status == 200 and answer["execution"]["executor_source_id"] == OPENAI
    finally:
        await gateway.close()
    assert [entry[0] for entry in service.adapter.invocations] == [XAI, OPENAI]


@pytest.mark.asyncio
async def test_a_proxy_call_body_is_bounded(tmp_path):
    """MH-HOSTED-GATEWAY-005: a call carries a few arguments; an oversized body is refused before it is parsed."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "web_search", None)
    status, answer = await _call(service, "web_search", json.dumps(
        {"arguments": {"query": "q" * (1024 * 1024)}},
    ).encode())
    assert (status, answer["error"]["code"]) == (400, "invalid_request_error")
    assert service.adapter.invocations == []




@pytest.mark.asyncio
@pytest.mark.parametrize("ended_by", ["caller", "deadline"])
async def test_a_call_cancelled_before_its_handle_is_still_metered(tmp_path, monkeypatch, ended_by):
    """MH-HOSTED-GATEWAY-005: wire facts observed after admission are billed, so they reach the usage ledger.

    The engine reports a cancellation after admission with its own error type;
    when the call's deadline caused it, the call is a timed-out record.
    """

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "web_search", (XAI, "grok-4.6"))
    observed = ProtocolSSEState("openai_responses")
    observed.observe(_responses(usage={"input_tokens": 1200, "output_tokens": 3})[-1])

    async def cancelled_after_admission(source_id, model_id, request, stream, origin, *, on_admitted=None):
        on_admitted()
        if ended_by == "deadline":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise InvokeCancelledError(observed) from None
        raise InvokeCancelledError(observed)

    if ended_by == "deadline":
        from core.handlers.model_hub import search_chain
        monkeypatch.setattr(search_chain, "call_timeout", lambda _tool: 0.05)
    service.adapter.invoke = cancelled_after_admission
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:cancel", turn_id="turn-cancel")
        call = service.run_hosted_tool(
            "web_search", {"arguments": {"query": "q"}}, turn=gateway.correlation.turn_hosted_tools("turn-cancel"),
        )
        if ended_by == "caller":
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            answer = await call
            assert answer["record"]["status"] == "failed" and "timed out" in answer["record"]["error"]
    finally:
        await gateway.close()
    assert _usage_of(service, XAI)


@pytest.mark.asyncio
async def test_an_encoded_proxy_call_body_is_refused(tmp_path):
    """MH-HOSTED-GATEWAY-005: the 1 MiB bound is on what is parsed, so an encoded body is refused before inflating."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "web_search", None)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:gzip", turn_id="turn-gzip")
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            async with client.post(
                f'{hop["base_url"]}/hosted-tools/web_search',
                headers={"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"], "Content-Encoding": "gzip"},
                data=gzip.compress(json.dumps({"arguments": {"query": "q"}}).encode()),
            ) as response:
                assert response.status == 400
    finally:
        await gateway.close()
    assert service.adapter.invocations == []


class _CountedHandle(LiveInvokeHandle):
    """A response whose chunks record when they are read."""

    def __init__(self, outcome, chunks):
        self.read: list[bytes] = []
        super().__init__(outcome, chunks)

    def _iterate(self, chunks):
        async def iterate():
            for chunk in chunks:
                self.read.append(chunk)
                yield chunk
        return iterate()


@pytest.mark.asyncio
async def test_a_buffered_executor_response_is_bounded(tmp_path, monkeypatch):
    """MH-HOSTED-GATEWAY-005: an oversized buffered body fails the attempt instead of growing without bound.

    Once refused, the response is not read any further, and the attempt is still metered from its outcome. Nothing
    reached Vibey, so the chain tries its next candidate; the oversized attempt is listed as billed.
    """

    monkeypatch.setattr(service_module, "EXECUTOR_RESPONSE_MAX_BYTES", 64)
    oversized = b"{" + b" " * 128
    usage = ProtocolUsageReport.of(input_tokens=900, cached_input_tokens=0, output_tokens=40)
    handle = _CountedHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=ANTHROPIC, usage=usage), (oversized, b"}"))
    service = _vibey_service(tmp_path, _sources(), handles=[handle, _served_by_openai()])
    _chain(service, "web_search", (ANTHROPIC, "claude-haiku-4-5"), (OPENAI, "gpt-6-astra"))
    status, answer = await _call(service, "web_search", {"arguments": {"query": "q"}})
    assert status == 200
    assert [entry[0] for entry in service.adapter.invocations] == [ANTHROPIC, OPENAI]
    assert answer["record"]["status"] == "completed"
    assert answer["execution"]["executor_source_id"] == OPENAI
    assert [attempt["source_id"] for attempt in answer["execution"]["failed_attempts"]] == [ANTHROPIC]
    assert handle.read == [oversized]
    assert _usage_of(service, ANTHROPIC)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "body", "status", "code"), [
    ("web_search", {"arguments": {"query": "q", "count": 3}}, 400, "invalid_request_error"),
    ("web_search", {"arguments": {"query": " "}}, 400, "invalid_request_error"),
    ("x_search", {"arguments": {"query": "q", "mode": "Oldest"}}, 400, "invalid_request_error"),
    # v1's executor field is gone: the turn's chain decides.
    ("web_search", {"arguments": {"query": "q"}, "executor": None}, 400, "invalid_request_error"),
    ("web_search", b"not json", 400, "invalid_request_error"),
    ("code_interpreter", {"arguments": {}}, 404, "not_found_error"),
])
async def test_a_malformed_proxy_call_is_refused_before_anything_runs(tmp_path, tool, body, status, code):
    """MH-HOSTED-GATEWAY-005: only a malformed call is an error status; nothing is invoked."""

    service = _vibey_service(tmp_path, _sources())
    answer_status, answer = await _call(service, tool, body)
    assert (answer_status, answer) == (status, {"type": "error", "error": {"type": code, "code": code}})
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_a_proxy_call_needs_the_turns_gateway_token(tmp_path):
    """MH-HOSTED-GATEWAY-005: the endpoint authenticates like every Vibey request."""

    service = _vibey_service(tmp_path, _sources())
    status, answer = await _call(
        service, "web_search", {"arguments": {"query": "q"}}, token="not-the-turn-token",
    )
    assert (status, answer["error"]["code"]) == (401, "authentication_error")


RECORDED_XAI_LIST = (
    "1. OpenAI is sharing progress on ChatGPT for Teens. They will keep building these tools."
    "[[1]](https://x.com/a)\n2. [[2]](https://x.com/b)GPT-6 rolls out today. It applies to Chat."
)
RECORDED_XAI_PROSE = (
    "The latest release is **v8.0.27**. It was released on October 12, 2026 (at 16:37 UTC)."
    "[[1]](https://a.example)[[2]](https://b.example)"
)
RECORDED_OPENAI_PROSE = (
    "Prices rose 5% in May. ([reuters.com](https://r.example)) Rates held steady ([ft.com](https://f.example)), "
    "analysts said."
)


def _spans(text: str, markers: list[tuple[str, str]]) -> list[tuple[int, int, str]]:
    return [(text.index(marker), text.index(marker) + len(marker), url) for marker, url in markers]


@pytest.mark.parametrize(("text", "markers", "snippets"), [
    (RECORDED_XAI_LIST, [("[[1]](https://x.com/a)", "https://x.com/a"), ("[[2]](https://x.com/b)", "https://x.com/b")],
     ["OpenAI is sharing progress on ChatGPT for Teens. They will keep building these tools.",
      "GPT-6 rolls out today. It applies to Chat."]),
    (RECORDED_XAI_PROSE, [("[[1]](https://a.example)", "https://a.example"),
                          ("[[2]](https://b.example)", "https://b.example")],
     ["It was released on October 12, 2026 (at 16:37 UTC).", "It was released on October 12, 2026 (at 16:37 UTC)."]),
    (RECORDED_OPENAI_PROSE, [("([reuters.com](https://r.example))", "https://r.example"),
                             ("([ft.com](https://f.example))", "https://f.example")],
     ["Prices rose 5% in May.", "Rates held steady, analysts said."]),
])
def test_an_excerpt_is_the_answer_text_its_citation_belongs_to(text, markers, snippets):
    """MH-HOSTED-GATEWAY-004: a list item citing one source, else its sentence; markers removed (§12 item 24)."""

    spans = _spans(text, markers)
    assert [excerpt(text, start, end, url, spans) for start, end, url in spans] == snippets

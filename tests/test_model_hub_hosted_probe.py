"""The hosted-tool probes (``vibey-hosted-tools.md`` §3.2, capability model §3.3). Scenario MH-HOSTED-007.

The real engine client invokes a loopback upstream that plays each provider's answer, and Vibey's own adapter
reads the response, so a probe recognizes a result only as the hosted call a real turn would keep. Only
``verified`` and ``unsupported`` are evidence; every other outcome leaves the record that was there.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web

from core.handlers.model_hub.capability_evidence import EvidenceFeature, EvidenceRecord, identity_digest
from core.handlers.model_hub.hosted_tools import HOSTED_PROBE_SHAPE
from core.handlers.model_hub.service import ModelHubError
from tests.test_model_hub_l3 import _assert_valid, _source
from tests.test_model_hub_retry_lifecycle import configured_service, loopback_engine
from vibe.backend_model_catalog import hosted_tool_spec

XAI = _source("src_probe0001", "xAI 密钥", vendor="xai", protocol="openai_responses", model_id="grok-4.6")
ANTHROPIC = _source("src_probe0002", "Anthropic 中转", vendor="anthropic", protocol="anthropic", model_id="claude-sonnet-4-6")
CALL = EvidenceFeature.hosted("web_search", "call")
REPLAY = EvidenceFeature.hosted("web_search", "replay")


def _records(service, feature) -> list:
    return [record for record in service.capability_evidence.records() if record.feature == feature]


def _sse(*events: dict[str, Any]) -> str:
    # Both providers name every event, as the engine's stream observer expects.
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


# A result the call returned that its visible answer never shows: only the replayed native payload holds it.
HIDDEN = "https://avibe.bot/docs/hosted-tools"
SEARCH = {
    "id": "ws_1",
    "type": "web_search_call",
    "status": "completed",
    "action": {
        "type": "search",
        "query": "Avibe local-first agent OS",
        "sources": [{"type": "url", "url": "https://avibe.bot/"}, {"type": "url", "url": HIDDEN}],
    },
}
ANSWER = {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "https://avibe.bot/", "annotations": []}]}


def _responses(*items: dict[str, Any]) -> str:
    events: list[dict[str, Any]] = [{"type": "response.created", "response": {"status": "in_progress"}}]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index, "item": {**item, "status": "in_progress"}})
        if item["type"] == "message":
            events.append({"type": "response.output_text.delta", "output_index": index, "delta": item["content"][0]["text"]})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    usage = {"input_tokens": 900, "output_tokens": 40, "server_side_tool_usage_details": {"web_search_calls": len(items) - 1}}
    events.append({"type": "response.completed", "response": {"status": "completed", "usage": usage}})
    return _sse(*events)


ANTHROPIC_SEARCH = _sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 900}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query": "Avibe"}'}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [
                {"type": "web_search_result", "title": "Avibe", "url": "https://avibe.bot/", "encrypted_content": "opaque"},
                {"type": "web_search_result", "title": "Hosted tools", "url": HIDDEN, "encrypted_content": "opaque"},
            ],
        },
    },
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "https://avibe.bot/"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40, "server_tool_use": {"web_search_requests": 1}}},
    {"type": "message_stop"},
)
def _anthropic_text(text: str) -> str:
    return _sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 1200}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    )


def _responses_text(text: str) -> str:
    return _responses({"id": "msg_2", "type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}]})


REFUSAL = _sse(
    {"type": "response.created", "response": {"status": "in_progress"}},
    {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_3", "type": "message", "status": "in_progress", "content": []}},
    {"type": "response.refusal.delta", "output_index": 0, "delta": f"I won't repeat {HIDDEN}."},
    {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {"id": "msg_3", "type": "message", "status": "completed", "content": [{"type": "refusal", "refusal": f"I won't repeat {HIDDEN}."}]},
    },
    {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 900, "output_tokens": 9}}},
)


def _replay_reply(protocol: str, request: dict) -> str:
    """A model answering the replay probe from what it was sent: it can name the result only the native payload
    holds when that payload came back, and nothing more otherwise."""

    text = HIDDEN if HIDDEN in json.dumps(request) else "I can't tell without searching again."
    return _anthropic_text(text) if protocol == "anthropic" else _responses_text(text)




def _stream(body: str) -> web.Response:
    return web.Response(body=body.encode(), content_type="text/event-stream")


# Output, then a server error in the stream: the call cannot be retried as a turn, but a probe can run again.
FAILED_AFTER_OUTPUT = _sse(
    {"type": "response.created", "response": {"status": "in_progress"}},
    {"type": "response.output_item.added", "output_index": 0, "item": {**ANSWER, "status": "in_progress"}},
    {"type": "response.output_text.delta", "output_index": 0, "delta": "https://"},
    {
        "type": "response.failed",
        "response": {"status": "failed", "error": {"code": "server_error", "message": "The server had an error."}},
    },
)
TOOL_REFUSED = {"error": {"message": "Unsupported tool type: web_search", "type": "invalid_request_error"}}
CHOICE_REFUSED = {"error": {"message": "tool_choice 'required' is not supported", "type": "invalid_request_error", "param": "tool_choice"}}
OVERLOADED = {"error": {"message": "The servers are overloaded", "type": "overloaded_error"}}

CASES = [
    # (case, Source, upstream status, upstream body, outcome, the record afterwards: written status or None for
    # kept, whether the response failed to complete)
    ("responses-result", XAI, 200, _responses(SEARCH, ANSWER), "verified", "verified", False),
    ("anthropic-result", ANTHROPIC, 200, ANTHROPIC_SEARCH, "verified", "verified", False),
    ("no-tool-item", XAI, 200, _responses(ANSWER), "inconclusive", None, False),
    ("tool-type-refused", XAI, 400, TOOL_REFUSED, "unsupported", "unsupported", True),
    ("another-400", XAI, 400, CHOICE_REFUSED, "inconclusive", None, True),
    ("overloaded", XAI, 503, OVERLOADED, "transient", None, True),
    ("failed-after-output", XAI, 200, FAILED_AFTER_OUTPUT, "transient", None, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,status,body,outcome,written,failed", [case[1:] for case in CASES], ids=[case[0] for case in CASES]
)
async def test_a_call_probe_sends_the_production_request_and_records_only_what_it_shows(
    tmp_path, source, status, body, outcome, written, failed
):
    """MH-HOSTED-007: the probe is Vibey's native request on exactly the probed Source, forced except on Anthropic;
    only a result or a refusal naming the tool is evidence, and anything else keeps the earlier record."""
    service, clock, _models = configured_service(tmp_path, sources=[source])
    model_id = source.models[0].id
    sent = []

    async def respond(request):
        sent.append(await request.json())
        if len(sent) > 1:
            return _stream(_replay_reply(source.protocol, sent[-1]))
        if isinstance(body, str):
            return web.Response(status=status, body=body.encode(), content_type="text/event-stream")
        return web.json_response(body, status=status)

    async with loopback_engine(tmp_path, service, respond):
        # A real refusal learned a day ago, for the Source as the engine now holds its credential.
        [live] = service.store.load().sources
        prior = EvidenceRecord(
            source_id=source.id,
            model_id=model_id,
            feature=CALL,
            status="unsupported",
            basis="observed",
            observed_at=clock.now() - timedelta(days=1),
            engine=service.engine_stamp,
            shape=HOSTED_PROBE_SHAPE,
            source_identity=identity_digest(live),
            detail="400 invalid_request_error",
        )
        service.capability_evidence.write(prior, live=[live])
        result = await service.probe_hosted_tool("web_search", {"source_id": source.id, "model_id": model_id})

    request = sent[0]
    declaration = hosted_tool_spec(source.vendor, source.protocol, model_id, "web_search").declaration
    assert request["tools"] == [declaration]
    assert "news article published today" in json.dumps(request, ensure_ascii=False)
    assert "reasoning" not in request and "output_config" not in request
    if source.protocol == "anthropic":
        assert "tool_choice" not in request
    else:
        assert request["tool_choice"] == "required"

    probe = result["probe"]
    _assert_valid("hosted-tool-probe-result.schema.json", probe)
    assert (probe["tool"], probe["source_id"], probe["model_id"]) == ("web_search", source.id, model_id)
    assert probe["call"]["outcome"] == outcome
    # Only a verified call is followed by the replay probe; its answer here names the first result without searching.
    assert len(sent) == (2 if written == "verified" else 1)
    assert probe["replay"] == ({"outcome": "verified", "error": None} if written == "verified" else None)
    # A response that failed says why, as the Source test does; a refusal naming the tool is the answer itself.
    assert (probe["call"]["error"] is not None) == (failed and outcome != "unsupported")

    [record] = _records(service, CALL)
    assert len(_records(service, REPLAY)) == (1 if written == "verified" else 0)
    if written is None:
        assert record == prior
    else:
        assert (record.status, record.basis, record.engine, record.shape) == (
            written, "probe", service.engine_stamp, HOSTED_PROBE_SHAPE
        )
        assert record.detail == ("400 invalid_request_error" if written == "unsupported" else None)
        assert record.observed_at > prior.observed_at
    # The response carries the Tools tab as it reads now.
    [candidate] = next(tool for tool in result["hosted_tools"]["tools"] if tool["name"] == "web_search")["candidates"]
    assert candidate["call"]["status"] == (written or "unsupported")


@pytest.mark.asyncio
async def test_a_probe_runs_only_on_a_target_the_tool_can_run_on(tmp_path):
    """MH-HOSTED-007: a refused target never reaches the upstream, so it costs nothing."""
    subscription = replace(ANTHROPIC, id="src_probe0003", kind="subscription", billing="monthly")
    google = _source("src_probe0004", "Gemini", vendor="google", protocol="google", model_id="gemini-3-pro")
    retired = _source("src_probe0007", "xAI retired", vendor="xai", protocol="openai_responses", model_id="grok-4.5")
    retired.models[0].retired = True
    chatgpt = replace(
        _source("src_probe0008", "ChatGPT", vendor="openai", protocol="openai_responses", model_id="gpt-6-sol"),
        kind="subscription",
        billing="monthly",
    )
    service, _clock, _models = configured_service(tmp_path, sources=[XAI, subscription, google, retired, chatgpt])
    sent = []

    async def invoke(*args, **kwargs):
        sent.append(args)
        raise AssertionError("a refused probe reached the upstream")

    service.adapter.invoke = invoke
    for tool, target, code, status in [
        # xAI's overlay declares no image tool, so there is nothing to probe.
        ("image_generation", {"source_id": XAI.id, "model_id": "grok-4.6"}, "invalid_parameter", 400),
        ("code_interpreter", {"source_id": XAI.id, "model_id": "grok-4.6"}, "invalid_parameter", 400),
        ("web_search", {"source_id": XAI.id}, "invalid_parameter", 400),
        ("web_search", {"source_id": XAI.id, "model_id": ["grok-4.6"]}, "invalid_parameter", 400),
        ("web_search", {"source_id": 7, "model_id": "grok-4.6"}, "invalid_parameter", 400),
        ("web_search", {"source_id": XAI.id, "model_id": "   "}, "invalid_parameter", 400),
        ("web_search", {"source_id": "src_gone0001", "model_id": "grok-4.6"}, "source_not_found", 404),
        ("web_search", {"source_id": XAI.id, "model_id": "grok-3"}, "invalid_parameter", 400),
        # Anthropic restricts OAuth credentials to its own apps.
        ("web_search", {"source_id": subscription.id, "model_id": "claude-sonnet-4-6"}, "invalid_parameter", 400),
        # Vibey reaches a google Source through a translation, never natively.
        ("web_search", {"source_id": google.id, "model_id": "gemini-3-pro"}, "invalid_parameter", 400),
        # A model discovery retired stays listed as a tombstone, and a tombstone never supplies work.
        ("web_search", {"source_id": retired.id, "model_id": "grok-4.5"}, "invalid_parameter", 400),
        # A subscription serves only its listed models; an API-key Source would serve this one as a passthrough.
        ("web_search", {"source_id": chatgpt.id, "model_id": "gpt-6-astra"}, "invalid_parameter", 400),
    ]:
        with pytest.raises(ModelHubError) as refused:
            await service.probe_hosted_tool(tool, target)
        assert (refused.value.code, refused.value.status) == (code, status), (tool, target)

    assert sent == []
    assert service.capability_evidence.records() == []
    # The same rule decides where the Tools tab lists the tool and what a turn binds as its executor, so even
    # verified evidence never makes a tombstone either.
    service.capability_evidence.write(
        EvidenceRecord(
            source_id=retired.id,
            model_id="grok-4.5",
            feature=CALL,
            status="verified",
            basis="probe",
            observed_at=service.now(),
            engine=service.engine_stamp,
            shape=HOSTED_PROBE_SHAPE,
            source_identity=identity_digest(retired),
        ),
        live=service.store.load().sources,
    )
    status = service.hosted_tools_status()["hosted_tools"]
    web_search = next(tool for tool in status["tools"] if tool["name"] == "web_search")
    assert [candidate["source_id"] for candidate in web_search["candidates"]] == [XAI.id, chatgpt.id]
    assert web_search["bound"] is None
    # Nor does the tombstone's record ask for a re-verify the Tools tab has no candidate for.
    service.engine_stamp = "model-hub-engine-next"
    assert service.hosted_tools_status()["hosted_tools"]["reverify"] == {"engine_upgraded": 0, "aged": 0}


def test_each_candidate_states_what_probing_it_would_cost(tmp_path):
    """MH-HOSTED-007: the Tools read prices a probe before it runs, the call and the replay that may follow it: at
    list price on a metered Source, included on a monthly one, and unknown (never zero) when the model has no token
    price."""
    from core.handlers.model_hub.usage import UsageWriter
    from tests.test_model_hub_pricing import _table

    monthly = replace(XAI, id="src_probe0005", display_name="xAI monthly", billing="monthly", credential_ref="cred_src_probe0005")
    unpriced = _source("src_probe0006", "xAI unlisted", vendor="xai", protocol="openai_responses", model_id="grok-4-unlisted")
    service, _clock, _models = configured_service(tmp_path, sources=[XAI, monthly, unpriced])
    # grok-4.6 at $2 / $6 per million tokens; xAI lists web search at $5 per 1k calls and X search at $5 per 1k posts.
    service.usage_writer = UsageWriter(service.usage, price_table=_table)

    status = service.hosted_tools_status()["hosted_tools"]

    _assert_valid("hosted-tools-status.schema.json", status)
    estimates = {
        (tool["name"], candidate["source_id"]): candidate["probe_estimate"]
        for tool in status["tools"]
        for candidate in tool["candidates"]
    }
    tokens = 8000 * 2 / 1e6 + 500 * 6 / 1e6
    # The replay sends the call's turn back and asks one question, using no tool, so it has no tool fee.
    replay = 12000 * 2 / 1e6 + 300 * 6 / 1e6
    assert estimates[("web_search", XAI.id)] == {"basis": "list", "usd": pytest.approx(tokens + 5 / 1000 + replay)}
    assert estimates[("x_search", XAI.id)] == {"basis": "list", "usd": pytest.approx(tokens + 10 * 5 / 1000 + replay)}
    assert estimates[("web_search", monthly.id)] == {"basis": "included"}
    assert estimates[("web_search", unpriced.id)] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        "cache_control.ttl: Extra inputs are not permitted",
        # A refusal that also names the tool type speaks only for the request it refused: the resend's result decides.
        "Unsupported tool type: web_search_20250305; cache_control.ttl: Extra inputs are not permitted",
    ],
    ids=["ttl", "ttl-and-tool-type"],
)
async def test_each_request_the_adapter_sends_is_its_own_pinned_call(tmp_path, monkeypatch, refusal):
    """MH-HOSTED-007: the probe runs Vibey's adapter unchanged. A relay that refuses Anthropic's one-hour cache
    lifetime gets the adapter's resend, as a real turn would, and each request is one call on the probed Source."""
    from core.agent_core.ai import anthropic as anthropic_adapter

    monkeypatch.setattr(anthropic_adapter, "_TTL_REJECTED_ROUTES", set())
    service, _clock, _models = configured_service(tmp_path, sources=[ANTHROPIC])
    sent = []

    async def respond(request):
        sent.append(await request.json())
        if len(sent) > 2:
            return _stream(_replay_reply("anthropic", sent[-1]))
        if '"ttl": "1h"' in json.dumps(sent[-1]):
            return web.json_response(
                {"type": "error", "error": {"type": "invalid_request_error", "message": refusal}},
                status=400,
            )
        return web.Response(body=ANTHROPIC_SEARCH.encode(), content_type="text/event-stream")

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool(
            "web_search", {"source_id": ANTHROPIC.id, "model_id": "claude-sonnet-4-6"}
        )

    first, resent, _replay = sent
    assert '"ttl": "1h"' in json.dumps(first) and '"ttl": "1h"' not in json.dumps(resent)
    assert first["tools"] == resent["tools"]
    assert result["probe"]["call"] == {"outcome": "verified", "error": None}
    [record] = _records(service, CALL)
    assert (record.source_id, record.status, record.basis) == (ANTHROPIC.id, "verified", "probe")


@pytest.mark.asyncio
async def test_a_probe_that_verifies_its_source_records_evidence_for_the_source_as_it_now_is(tmp_path):
    """MH-HOSTED-007: a new Source's first successful call retires its pending verification, which changes no
    identity; the probe's record follows the Source as it reads after that call."""
    marker = "vp_" + "0" * 32
    pending = replace(XAI, verification_pending=marker, credential_generation=marker)
    service, _clock, _models = configured_service(tmp_path, sources=[pending])

    async def respond(_request):
        return web.Response(body=_responses(SEARCH, ANSWER).encode(), content_type="text/event-stream")

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    [live] = service.store.load().sources
    assert live.verification_pending is None
    assert result["probe"]["call"]["outcome"] == "verified"
    [record] = _records(service, CALL)
    assert record.source_identity == identity_digest(live)
    [candidate] = next(tool for tool in result["hosted_tools"]["tools"] if tool["name"] == "web_search")["candidates"]
    assert candidate["call"]["status"] == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_after", [False, True], ids=["pending", "then-verified-by-another-call"])
async def test_a_probe_records_no_evidence_for_a_source_reauthenticated_mid_probe(tmp_path, verified_after):
    """MH-HOSTED-007: a reauthentication that lands mid-probe (same credential ref, a new credential generation)
    leaves the old grant's result without a record, also once another call has verified the new grant."""
    from core.handlers.model_hub.service import ModelHubService

    first = "vp_" + "1" * 32
    service, _clock, _models = configured_service(
        tmp_path, sources=[replace(XAI, verification_pending=first, credential_generation=first)],
    )

    async def respond(_request):
        config = service.store.load()
        ModelHubService._mark_source_unverified(config.sources[0])
        service.store.save(config)
        if verified_after:
            config = service.store.load()
            config.sources[0].verification_pending = None
            service.store.save(config)
        return web.Response(body=_responses(SEARCH, ANSWER).encode(), content_type="text/event-stream")

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert result["probe"]["call"]["outcome"] == "verified"
    assert service.store.load().sources[0].credential_generation not in (None, first)
    assert service.capability_evidence.records() == []


@pytest.mark.asyncio
async def test_the_probe_reads_its_calls_in_the_order_the_adapter_sent_them():
    """MH-HOSTED-007: a resend can settle before the request it replaces; the probe still reads the resend last."""
    import httpx

    from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
    from core.handlers.model_hub.hosted_probe import PinnedCallTransport
    from core.handlers.model_hub.service import UserTestCall

    first_may_settle = asyncio.Event()

    class Handle:
        stream = None

    async def call(body, read):
        await read(Handle())
        if body["n"] == 1:
            await first_may_settle.wait()
        else:
            first_may_settle.set()
        status = 400 if body["n"] == 1 else 503
        outcome = RawCallOutcome(RawOutcomeKind.HTTP_ERROR, status, None, None, False, "grok-4.6", XAI.id)
        return UserTestCall(XAI, outcome, 0)

    transport = PinnedCallTransport(call)
    async with httpx.AsyncClient(transport=transport) as client:
        first = asyncio.create_task(client.post("http://probe.invalid/", json={"n": 1}))
        await asyncio.sleep(0)
        await client.post("http://probe.invalid/", json={"n": 2})
        await first
    await transport.settle()

    assert [call.body["n"] for call in transport.calls] == [1, 2]


@pytest.mark.asyncio
async def test_the_probe_asks_for_no_more_output_than_a_turn_on_its_hop_would(tmp_path):
    """MH-HOSTED-007: a Vibey turn asks for its menu model's output budget; a relay model that declares a small
    output maximum is probed within it, so the probe is not refused where real turns succeed."""
    from config.v2_config import ModelHubBackendModelConfig, ModelHubRouteConfig, ModelHubRouteHopConfig

    service, _clock, _models = configured_service(tmp_path, sources=[XAI])
    vibey = service.store.config.agents["vibey"]
    vibey.models = [ModelHubBackendModelConfig(id="relay-grok", context_window=128000, max_output_tokens=1024)]
    vibey.sources.order = [XAI.id]
    vibey.routes["relay-grok"] = ModelHubRouteConfig((ModelHubRouteHopConfig(XAI.id, "grok-4.6"),))
    sent = []

    async def respond(request):
        sent.append(await request.json())
        return web.Response(body=_responses(SEARCH, ANSWER).encode(), content_type="text/event-stream")

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    # The replay probe that follows asks for the same output.
    assert [request["max_output_tokens"] for request in sent] == [1024, 1024]
    assert result["probe"]["call"]["outcome"] == "verified"


@pytest.mark.asyncio
async def test_an_api_key_source_is_probed_on_a_passthrough_model_it_does_not_list(tmp_path):
    """MH-HOSTED-007: an API-key Source serves models discovery never listed, so such a hop can be probed and become
    native like any other; the Tools tab lists only what discovery found."""
    service, _clock, _models = configured_service(tmp_path, sources=[XAI])
    sent = []

    async def respond(request):
        sent.append(await request.json())
        return web.Response(body=_responses(SEARCH, ANSWER).encode(), content_type="text/event-stream")

    async with loopback_engine(tmp_path, service, respond):
        # A padded id is the model routing knows: the probe sends and records the canonical one.
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": " grok-4.7 "})

    request = sent[0]
    # The engine addresses a passthrough model through the Source's credential.
    assert request["model"].rsplit("/", 1)[-1] == "grok-4.7"
    assert result["probe"]["call"]["outcome"] == "verified"
    [record] = _records(service, CALL)
    assert (record.source_id, record.model_id, record.status) == (XAI.id, "grok-4.7", "verified")
    # The Tools tab lists only what discovery found, so it never counts a re-verify it has no candidate for.
    service.engine_stamp = "model-hub-engine-next"
    assert service.hosted_tools_status()["hosted_tools"]["reverify"] == {"engine_upgraded": 0, "aged": 0}


@pytest.mark.asyncio
async def test_a_probe_that_times_out_before_its_handle_is_still_metered(tmp_path, monkeypatch):
    """MH-HOSTED-007: a probe that times out after the engine observed billed wire facts, but before the call handed
    back its handle, still reaches the usage ledger: the provider billed it."""
    from core.handlers.model_hub import service as service_module
    from core.handlers.model_hub.adapter import InvokeCancelledError
    from core.handlers.model_hub.stream_wire import ProtocolSSEState
    from tests.test_model_hub_l3 import _usage_of

    monkeypatch.setattr(service_module, "call_timeout", lambda _tool: 0.05)
    service, _clock, _models = configured_service(tmp_path, sources=[XAI])
    observed = ProtocolSSEState("openai_responses")
    observed.observe(
        _sse({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 1200, "output_tokens": 3}}}).encode()
    )

    async def stalls_after_admission(source_id, model_id, request, stream, origin, *, on_admitted=None):
        on_admitted()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise InvokeCancelledError(observed) from None

    service.adapter.invoke = stalls_after_admission
    service._engine_synced = True
    result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert result["probe"]["call"]["outcome"] == "transient"
    assert _usage_of(service, XAI.id)


@pytest.mark.asyncio
async def test_a_deadline_mid_stream_ends_the_probe_as_transient(tmp_path, monkeypatch):
    """MH-HOSTED-007: only the call's own task reads its stream, so a deadline that fires while the adapter waits
    for the next chunk ends the stream for the adapter and settles the call; the probe reports a transient failure."""
    from core.handlers.model_hub import service as service_module

    monkeypatch.setattr(service_module, "call_timeout", lambda _tool: 0.5)
    service, _clock, _models = configured_service(tmp_path, sources=[XAI])

    async def respond(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            _sse(
                {"type": "response.created", "response": {"status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**ANSWER, "status": "in_progress"}},
                {"type": "response.output_text.delta", "output_index": 0, "delta": "https://"},
            ).encode()
        )
        await asyncio.sleep(30)
        return response

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert result["probe"]["call"]["outcome"] == "transient"
    assert service.capability_evidence.records() == []


@pytest.mark.asyncio
async def test_a_probe_settles_when_the_adapter_is_done_even_if_the_body_stays_open(tmp_path, monkeypatch):
    """MH-HOSTED-007: once the adapter has the terminal event it closes the response, as production does, so the call
    settles as the success it was instead of waiting out the deadline with the body still open."""
    from core.handlers.model_hub import service as service_module

    monkeypatch.setattr(service_module, "call_timeout", lambda _tool: 5)
    service, _clock, _models = configured_service(tmp_path, sources=[replace(XAI, verification_pending="vp_" + "5" * 32)])

    async def respond(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_responses(SEARCH, ANSWER).encode())
        await asyncio.sleep(30)
        return response

    async with loopback_engine(tmp_path, service, respond):
        started = asyncio.get_running_loop().time()
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 5
    assert result["probe"]["call"]["outcome"] == "verified"
    assert service.store.load().sources[0].verification_pending is None
    [record] = _records(service, CALL)
    assert record.status == "verified"


PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,outcome",
    [
        (PNG, "verified"),
        ("bm90IGFuIGltYWdl", "inconclusive"),
        # A BMP is an image, but not one a turn's sink keeps, so it proves nothing a turn could use.
        (base64.b64encode(b"BM" + bytes(52)).decode(), "inconclusive"),
    ],
    ids=["image", "not-an-image", "bmp"],
)
async def test_an_image_probe_verifies_only_an_image_vibey_could_keep(tmp_path, result, outcome):
    """MH-HOSTED-007: the image call probe reads the generated image through Vibey's own parsing, which keeps it
    only when it is an image; a probe keeps no file, and an image has no replay."""
    openai = _source("src_probe0009", "OpenAI 密钥", vendor="openai", protocol="openai_responses", model_id="gpt-6-astra")
    service, _clock, _models = configured_service(tmp_path, sources=[openai])
    sent = []
    image_call = {
        "id": "ig_1",
        "type": "image_generation_call",
        "status": "completed",
        "result": result,
        "revised_prompt": "A blue circle on a white background",
        "size": "1024x1024",
    }

    async def respond(request):
        sent.append(await request.json())
        return _stream(_responses(image_call))

    async with loopback_engine(tmp_path, service, respond):
        result_payload = await service.probe_hosted_tool("image_generation", {"source_id": openai.id, "model_id": "gpt-6-astra"})

    [request] = sent
    declaration = hosted_tool_spec("openai", "openai_responses", "gpt-6-astra", "image_generation").declaration
    assert request["tools"] == [declaration] and request["tool_choice"] == "required"
    probe = result_payload["probe"]
    _assert_valid("hosted-tool-probe-result.schema.json", probe)
    assert (probe["call"]["outcome"], probe["replay"]) == (outcome, None)
    records = _records(service, EvidenceFeature.hosted("image_generation", "call"))
    assert [record.status for record in records] == (["verified"] if outcome == "verified" else [])


REPLAY_CASES = [
    # (case, Source, the call's response, the replay's response or status and body, replay outcome, record)
    ("names-a-result", XAI, _responses(SEARCH, ANSWER), "answer-from-payload", "verified", "verified"),
    ("anthropic-names-a-result", ANTHROPIC, ANTHROPIC_SEARCH, "answer-from-payload", "verified", "verified"),
    # Restating the answer it already gave proves nothing: that text came back in the visible transcript.
    ("restates-the-visible-answer", XAI, _responses(SEARCH, ANSWER), _responses_text("https://avibe.bot/"), "inconclusive", None),
    # A URL that only starts like the hidden result is a different URL, and so is one with another fragment.
    ("names-a-longer-url", XAI, _responses(SEARCH, ANSWER), _responses_text(HIDDEN + "-old"), "inconclusive", None),
    ("names-another-fragment", XAI, _responses(SEARCH, ANSWER), _responses_text(HIDDEN + "#other"), "inconclusive", None),
    # Linked text names the result as surely as the URL written out.
    ("cites-the-result", XAI, _responses(SEARCH, ANSWER), _responses({
        "id": "msg_2",
        "type": "message",
        "content": [{
            "type": "output_text",
            "text": "The hosted tools docs.",
            "annotations": [{"type": "url_citation", "url": HIDDEN, "start_index": 4, "end_index": 21, "title": "docs"}],
        }],
    }), "verified", "verified"),
    # A refusal is inconclusive, even one that quotes the result.
    ("refuses-quoting-the-result", XAI, _responses(SEARCH, ANSWER), REFUSAL, "inconclusive", None),
    # The way a sentence ends a URL does not change it.
    ("names-it-in-a-sentence", XAI, _responses(SEARCH, ANSWER), _responses_text(f"The other one was {HIDDEN}."), "verified", "verified"),
    # Searching again instead of using the results it holds is what a dropped replay looks like.
    ("searches-again", XAI, _responses(SEARCH, ANSWER), _responses(SEARCH, ANSWER), "unsupported", "unsupported"),
    ("names-no-result", XAI, _responses(SEARCH, ANSWER), _responses_text("I can't tell."), "inconclusive", None),
    (
        "anthropic-refuses-the-replayed-result",
        ANTHROPIC,
        ANTHROPIC_SEARCH,
        (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid encrypted_content in search_result block"}}),
        "unsupported",
        "unsupported",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,first,second,outcome,written", [case[1:] for case in REPLAY_CASES], ids=[case[0] for case in REPLAY_CASES]
)
async def test_a_verified_call_is_followed_by_the_replay_probe(tmp_path, source, first, second, outcome, written):
    """MH-HOSTED-007 (§3.2, §12 item 23): the replay probe sends the call's turn back with its native payload in
    place, asks for a result it returned, and records only a named result, a new call or a refusal of the payload."""
    service, _clock, _models = configured_service(tmp_path, sources=[source])
    model_id = source.models[0].id
    sent = []

    async def respond(request):
        sent.append(await request.json())
        if len(sent) == 1:
            return _stream(first)
        if isinstance(second, tuple):
            return web.json_response(second[1], status=second[0])
        if second == "answer-from-payload":
            return _stream(_replay_reply(source.protocol, sent[-1]))
        return _stream(second)

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": source.id, "model_id": model_id})

    call, replay = sent
    # The native declaration is still present, nothing is forced, and the call's own items go back verbatim.
    assert replay["tools"] == call["tools"] and "tool_choice" not in replay
    # Its instructions ask for an answer from the results it holds, never for the call the question rules out.
    system = "instructions" if source.protocol == "openai_responses" else "system"
    assert replay[system] != call[system] and "without using any tool" in json.dumps(replay[system])
    native = SEARCH if source.protocol == "openai_responses" else {"type": "web_search_tool_result"}
    assert json.dumps(native["type"]) in json.dumps(replay)
    assert "other results you found" in json.dumps(replay)
    assert HIDDEN in json.dumps(replay)
    probe = result["probe"]
    _assert_valid("hosted-tool-probe-result.schema.json", probe)
    assert probe["call"]["outcome"] == "verified"
    assert probe["replay"]["outcome"] == outcome
    # Every reply here completed (or named the payload in a refusal): the verdict is read from it, not from a failure.
    assert probe["replay"]["error"] is None
    assert [record.status for record in _records(service, REPLAY)] == ([written] if written else [])


@pytest.mark.asyncio
async def test_a_replay_sends_the_results_only_in_their_native_payload(tmp_path):
    """MH-HOSTED-007: the call's reasoning, readable or encrypted, can restate a result; it never goes back with the
    replay, so a Source that drops the payload has nowhere else to read the result from."""
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": f"The results were https://avibe.bot/ and {HIDDEN}."}],
        "encrypted_content": "opaque-reasoning",
    }
    service, _clock, _models = configured_service(tmp_path, sources=[XAI])
    sent = []

    async def respond(request):
        sent.append(await request.json())
        return _stream(_responses(reasoning, SEARCH, ANSWER) if len(sent) == 1 else _replay_reply("openai_responses", sent[-1]))

    async with loopback_engine(tmp_path, service, respond):
        await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    replay = sent[1]
    elsewhere = [item for item in replay["input"] if item.get("type") != "web_search_call"]
    assert any(item.get("type") == "web_search_call" for item in replay["input"])
    assert HIDDEN not in json.dumps(elsewhere) and "reasoning" not in json.dumps(elsewhere)


@pytest.mark.asyncio
async def test_no_replay_runs_when_the_visible_answer_shows_every_result(tmp_path):
    """MH-HOSTED-007: a replay is proved only by a result the visible answer does not show; with none, no replay is
    spent, and the replay stays unknown, so turns replay synthetically."""
    single = {**SEARCH, "action": {**SEARCH["action"], "sources": [{"type": "url", "url": "https://avibe.bot/"}]}}
    service, _clock, _models = configured_service(tmp_path, sources=[XAI])
    sent = []

    async def respond(request):
        sent.append(await request.json())
        return _stream(_responses(single, ANSWER))

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert len(sent) == 1
    assert (result["probe"]["call"]["outcome"], result["probe"]["replay"]) == ("verified", None)
    assert _records(service, REPLAY) == []


@pytest.mark.asyncio
async def test_a_replay_the_probe_has_no_time_left_for_reports_try_again(tmp_path, monkeypatch):
    """MH-HOSTED-007: the whole probe answers inside the UI's RPC wait; a replay that could not finish in what is left
    is not sent, and reads as a transient failure to try again."""
    from core.handlers.model_hub import service as service_module

    monkeypatch.setattr(service_module, "PROBE_BUDGET_SECONDS", 30.0)
    service, _clock, _models = configured_service(tmp_path, sources=[XAI])
    sent = []

    async def respond(request):
        sent.append(await request.json())
        return _stream(_responses(SEARCH, ANSWER))

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert len(sent) == 1
    assert result["probe"]["replay"] == {"outcome": "transient", "error": None}
    assert _records(service, REPLAY) == []


@pytest.mark.asyncio
async def test_a_replay_goes_only_to_the_source_identity_the_call_was_recorded_for(tmp_path, monkeypatch):
    """MH-HOSTED-007: the replay carries the call's provider-native payload, so a Source whose endpoint or credential
    changed after the call never receives it; the call's result stands and no replay is reported."""
    from core.handlers.model_hub import service as service_module

    # The fixture store keeps the Source object it is given, and this test edits it.
    service, _clock, _models = configured_service(tmp_path, sources=[replace(XAI)])
    sent = []
    build_replay = service_module.hosted_replay_request

    def replaced_endpoint_first(*args, **kwargs):
        config = service.store.load()
        config.sources[0].base_url = "https://relay.invalid/v1"
        service.store.save(config)
        return build_replay(*args, **kwargs)

    monkeypatch.setattr(service_module, "hosted_replay_request", replaced_endpoint_first)

    async def respond(request):
        sent.append(await request.json())
        return _stream(_responses(SEARCH, ANSWER) if len(sent) == 1 else _replay_reply("openai_responses", sent[-1]))

    async with loopback_engine(tmp_path, service, respond):
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert len(sent) == 1
    assert (result["probe"]["call"]["outcome"], result["probe"]["replay"]) == ("verified", None)
    assert [record.status for record in _records(service, CALL)] == ["verified"]
    assert _records(service, REPLAY) == []


@pytest.mark.parametrize(
    "stop_reason,text,outcome",
    [
        ("stop", HIDDEN, "verified"),
        ("safety", HIDDEN, "inconclusive"),
        ("refusal", HIDDEN, "inconclusive"),
        # Provider text that is not a well-formed URL compares as written and never fails the probe.
        ("stop", "https://[invalid", "inconclusive"),
    ],
)
def test_a_replay_answer_counts_only_when_it_completed_and_names_a_hidden_result(stop_reason, text, outcome):
    """MH-HOSTED-007: a blocked or refused answer proves nothing, whatever it quotes, and a malformed URL is just text."""
    assert _replay_verdict(text, {HIDDEN, "https://[also-invalid"}, stop_reason=stop_reason) == outcome


WIKI = "https://en.wikipedia.org/wiki/Search_(computing)"


@pytest.mark.parametrize(
    "asked,text,outcome",
    [
        # Prose and Markdown around a URL do not change it.
        (HIDDEN, f"See [the docs]({HIDDEN}).", "verified"),
        (HIDDEN, f"**{HIDDEN}**", "verified"),
        (WIKI, f"It is {WIKI}.", "verified"),
        (WIKI, f"(see {WIKI})", "verified"),
        # A result's own URL is exact: an answer without the punctuation it ends in names another URL.
        (HIDDEN + "!", f"It is {HIDDEN}, I think.", "inconclusive"),
        (HIDDEN + "!", f"It is {HIDDEN}!.", "verified"),
        (WIKI, "https://en.wikipedia.org/wiki/Search_", "inconclusive"),
        # Only the scheme and host are case-insensitive; a trailing slash or another user name is another URL.
        ("https://Example.COM/result", "HTTPS://example.com/result", "verified"),
        ("https://Alice:Secret@example.com/result", "https://alice:secret@example.com/result", "inconclusive"),
        (HIDDEN, f"It is {HIDDEN}/", "inconclusive"),
    ],
)
def test_a_replay_answer_names_a_result_by_its_exact_url(asked, text, outcome):
    """MH-HOSTED-007: only where prose ends a written URL is ambiguous; the result's URL is never trimmed."""
    assert _replay_verdict(text, {asked}) == outcome


def test_a_result_the_answer_shows_in_any_reading_is_not_hidden():
    """MH-HOSTED-007: a shown result taken for hidden could verify a replay that only restated the visible answer."""
    from core.agent_core.messages import (
        AssistantMessage, HostedAction, HostedNative, HostedRecord, HostedSource, HostedSummary, HostedToolBlock, Origin,
        TextBlock,
    )
    from core.handlers.model_hub.hosted_probe import hidden_result_urls

    call = HostedToolBlock(
        id="ws_1",
        tool="web_search",
        action=HostedAction(type="search", queries=("avibe",)),
        record=HostedRecord(status="completed", sources=(HostedSource(url=HIDDEN + "."), HostedSource(url=WIKI))),
        native=HostedNative(source_id=XAI.id, payload=({"type": "web_search_call", "id": "ws_1"},)),
    )
    for text, hidden in [
        (f"Read {HIDDEN}", {WIKI}),
        (f"Read {WIKI}.", {HIDDEN + "."}),
        (f"Read {WIKI}/", {HIDDEN + "."}),
        ("Read none", {HIDDEN + ".", WIKI}),
    ]:
        answer = AssistantMessage(
            content=(call, TextBlock(text=text)),
            origin=Origin("xai", "openai_responses", "grok-4.6"),
            stop_reason="stop",
            hosted_summary=HostedSummary(),
        )
        assert hidden_result_urls(answer) == hidden


def _replay_verdict(text, asked, *, stop_reason="stop"):
    from core.agent_core.ai.provider import Done
    from core.agent_core.messages import AssistantMessage, Origin, TextBlock
    from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
    from core.handlers.model_hub.hosted_probe import replay_outcome

    answer = AssistantMessage(
        content=(TextBlock(text=text),), origin=Origin("xai", "openai_responses", "grok-4.6"), stop_reason=stop_reason,
    )
    ok = RawCallOutcome(RawOutcomeKind.SUCCESS, 200, None, None, True, "grok-4.6", XAI.id)
    return replay_outcome("web_search", Done(answer), ok, None, asked=frozenset(asked), carried=True)


@pytest.mark.asyncio
async def test_a_re_probe_restores_replay_a_narrow_rule_demoted(tmp_path):
    """MH-HOSTED-007: a 400 on a replayed payload teaches 'replay unsupported' for this Source, also when the payload
    was minted under an identity it had before (a wrong attribution: it refuses a foreign blob, not replay). That
    record expires on its own and never outlives a user's re-probe, which replaces it."""
    service, clock, _models = configured_service(tmp_path, sources=[XAI])
    sent = []

    async def respond(request):
        sent.append(await request.json())
        return _stream(_responses(SEARCH, ANSWER) if len(sent) == 1 else _replay_reply("openai_responses", sent[-1]))

    async with loopback_engine(tmp_path, service, respond):
        [live] = service.store.load().sources
        service.capability_evidence.write(
            EvidenceRecord(
                source_id=XAI.id,
                model_id="grok-4.6",
                feature=REPLAY,
                status="unsupported",
                basis="observed",
                observed_at=clock.now(),
                engine=service.engine_stamp,
                shape=HOSTED_PROBE_SHAPE,
                source_identity=identity_digest(live),
                detail="400 invalid_request_error",
            ),
            live=[live],
        )
        result = await service.probe_hosted_tool("web_search", {"source_id": XAI.id, "model_id": "grok-4.6"})

    assert result["probe"]["replay"]["outcome"] == "verified"
    [record] = _records(service, REPLAY)
    assert (record.status, record.basis) == ("verified", "probe")

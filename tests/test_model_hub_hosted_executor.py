"""MH-HOSTED-GATEWAY-004/005: the gateway's proxy executor for hosted tools (``vibey-hosted-tools.md`` §3.4).

Each case posts a proxied call to the real gateway endpoint for a Vibey turn,
with a recording engine adapter playing the executor provider's own response
(shapes from the 2026-10-13 spike and a live check of these requests). The
answer must be the canonical record and execution, in the C-1 encoding of
``ToolResultMessage.hosted``.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import threading
from dataclasses import replace

import aiohttp
import pytest

from config.v2_config import ModelHubHostedToolConfig, ModelHubRouteHopConfig
from core.agent_core.messages import message_from_dict
from core.handlers.model_hub import hosted_executor as executor_module
from core.handlers.model_hub import service as service_module
from core.handlers.model_hub.adapter import InvokeCancelledError, RawOutcomeKind
from core.handlers.model_hub.capability_evidence import EvidenceFeature, EvidenceRecord, identity_digest
from core.handlers.model_hub.hosted_executor import excerpt
from core.handlers.model_hub.hosted_tools import HOSTED_PROBE_SHAPE, engine_stamp
from core.handlers.model_hub.stream_wire import ProtocolSSEState, ProtocolUsageReport
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
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


def _bind(service, tool, executor):
    """Turn the tool on with this executor verified, so the turn's resolution binds it."""

    config = service.store.config
    config.hosted_tools[tool] = ModelHubHostedToolConfig(
        enabled=True, executor=ModelHubRouteHopConfig(*executor) if executor else None,
    )
    if executor is None:
        return
    source = next(item for item in config.sources if item.id == executor[0])
    service.capability_evidence.write(
        EvidenceRecord(
            source_id=source.id, model_id=executor[1], feature=EvidenceFeature.hosted(tool, "call"),
            status="verified", basis="probe", observed_at=service.now(), engine=engine_stamp(),
            shape=HOSTED_PROBE_SHAPE, source_identity=identity_digest(source),
        ),
        live=config.sources,
    )


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
    """MH-HOSTED-GATEWAY-004: the bound executor only, no effort, the overlay declaration, excerpts, metering."""

    usage = ProtocolUsageReport.of(input_tokens=5369, cached_input_tokens=256, output_tokens=307)
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=executor[0], usage=usage), frames),
    ])
    _bind(service, tool, executor)
    status, answer = await _call(service, tool, {
        "arguments": arguments, "executor": {"source_id": executor[0], "model_id": executor[1]},
    })
    assert status == 200, answer
    assert service.adapter.invocations == [(executor[0], executor[1], "vibey")]
    sent = service.adapter.requests[0]
    assert sent.protocol == "openai_responses"
    assert "reasoning" not in sent and "tool_choice" not in sent
    assert {key: sent[key] for key in body_fields} == body_fields
    assert set(sent) - set(body_fields) == {"model", "instructions", "input", "store", "max_output_tokens"}
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
        "executor": {"source_id": ANTHROPIC, "model_id": "claude-haiku-4-5"},
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


@pytest.mark.asyncio
async def test_a_proxied_image_returns_its_bytes_once_for_vibey_to_save(tmp_path, monkeypatch):
    """MH-HOSTED-GATEWAY-004: the image comes back once, with the resolved prompt, size and quality.

    The final image is over Vibey's default 8 MiB SSE line, so the image stream's
    32 MiB line cap is what accepts it. Partials are previews and are dropped;
    the duplicate in ``response.completed`` is not returned.
    """

    image = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * (7 * 1024 * 4)
    encoded = base64.b64encode(image).decode()
    assert len(encoded) > 9 * 1024 * 1024
    call = {"type": "image_generation_call", "id": "ig_1", "status": "completed", "output_format": "png",
            "quality": "low", "size": "1536x1024", "revised_prompt": "A small red paper boat", "result": encoded}
    partial = _event({"type": "response.image_generation_call.partial_image", "partial_image_index": 0,
                      "partial_image_b64": base64.b64encode(b"preview").decode()})
    frames = _responses(call)
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=OPENAI), (frames[0], partial, *frames[1:])),
    ])
    _bind(service, "image_generation", (OPENAI, "gpt-6-astra"))
    encoding_threads = []
    dumps = json.dumps

    def recorded(value, *args, **kwargs):
        if isinstance(value, dict) and "images" in value:
            encoding_threads.append(threading.current_thread())
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", recorded)
    status, answer = await _call(service, "image_generation", {
        "arguments": {"prompt": "a small red paper boat", "size": "1024x1024"},
        "executor": {"source_id": OPENAI, "model_id": "gpt-6-astra"},
    })
    assert status == 200
    # Megabytes of base64 are never encoded on the controller loop.
    assert encoding_threads and threading.main_thread() not in encoding_threads
    sent = service.adapter.requests[0]
    assert sent["tools"] == [{
        "type": "image_generation", "partial_images": 2, "output_format": "jpeg", "output_compression": 90,
        "size": "1024x1024",
    }]
    assert sent["tool_choice"] == {"type": "image_generation"}
    assert "reasoning" not in sent
    assert answer["record"] == {"status": "completed", "sources": []}
    assert answer["generation"] == {"revised_prompt": "A small red paper boat", "size": "1536x1024", "quality": "low"}
    assert [(item["mime"], base64.b64decode(item["data"])) for item in answer["images"]] == [("image/png", image)]
    _hosted_result("image_generation", answer)


FAILED_WITH_PARTIAL = {**WEB_CALL, "status": "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "bound", "handles", "error", "sources", "executed"), [
    ("none_verified", None, [], "No Source is verified to run web_search", [], False),
    ("source_removed", (XAI, "grok-4.6"), [], "no longer available", [], False),
    # A new base URL or credential is a new identity: never verified, so never run.
    ("source_identity_changed", (XAI, "grok-4.6"), [], "no longer available", [], False),
    ("provider_refused", (XAI, "grok-4.6"),
     [InvokeHandle(_outcome(RawOutcomeKind.HTTP_ERROR, status=429, code="rate_limit_error", source_id=XAI))],
     "refused the call (HTTP 429 rate_limit_error)", [], True),
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
     "refused the call (HTTP 200 server_error)",
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

    executor = {"source_id": bound[0], "model_id": bound[1]} if bound else None
    status, answer = await _call(
        service, "web_search", {"arguments": {"query": "q"}, "executor": executor}, after_launch=remove_executor,
    )
    assert status == 200
    assert answer["record"]["status"] == "failed"
    assert error in answer["record"]["error"]
    assert answer["record"]["sources"] == sources
    assert ("execution" in answer) is executed
    assert len(service.adapter.invocations) == int(executed)
    assert _hosted_result("web_search", answer).record.status == "failed"


@pytest.mark.asyncio
async def test_failure_text_is_in_the_users_language(tmp_path):
    """MH-HOSTED-GATEWAY-005: the record's error is user-visible on the tool card, so it is localized."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "x_search", None)
    status, answer = await _call(
        service, "x_search", {"arguments": {"query": "q"}, "executor": None}, language="zh",
    )
    assert status == 200
    assert answer["record"]["error"] == "没有已验证可运行 x_search 的来源。请在「模型」中为某个来源验证它。"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["another_executor", "a_claude_subscription", "tool_not_enabled_this_turn"])
async def test_only_the_executor_the_live_turn_bound_may_run(tmp_path, case):
    """MH-HOSTED-GATEWAY-005: the token's turn bound one executor per tool; any other is refused, unrun."""

    subscription = replace(
        _source("src_claudesub01", "Claude subscription", vendor="anthropic", protocol="anthropic",
                model_id="claude-haiku-4-5"),
        kind="subscription", billing="monthly",
    )
    service = _vibey_service(tmp_path, _sources(subscription))
    if case != "tool_not_enabled_this_turn":
        _bind(service, "web_search", (XAI, "grok-4.6"))
    named = {
        "another_executor": (OPENAI, "gpt-6-astra"),
        "a_claude_subscription": ("src_claudesub01", "claude-haiku-4-5"),
        "tool_not_enabled_this_turn": (XAI, "grok-4.6"),
    }[case]
    status, answer = await _call(service, "web_search", {
        "arguments": {"query": "q"}, "executor": {"source_id": named[0], "model_id": named[1]},
    })
    assert (status, answer) == (409, {"type": "error", "error": {"type": "executor_not_bound", "code": "executor_not_bound"}})
    assert service.adapter.invocations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "call", "error"), [
    ("unreadable_base64", {"result": "not base64!"}, "could not be used"),
    ("one_of_two_calls_failed", None, "image_generation call failed"),
    ("too_many_images", None, "larger than allowed"),
    ("partials_over_the_byte_budget", None, "larger than allowed"),
])
async def test_an_image_the_executor_cannot_vouch_for_is_a_failed_record(tmp_path, monkeypatch, case, call, error):
    """MH-HOSTED-GATEWAY-005: an undecodable image, more image events, or more image bytes than allowed fail.

    Partials count toward the decoded-byte budget though they are never returned.
    """

    if case == "partials_over_the_byte_budget":
        monkeypatch.setattr(executor_module, "_IMAGES_TOTAL_MAX_BYTES", 64)

    final = {"type": "image_generation_call", "id": "ig_1", "status": "completed", "output_format": "png",
             "result": base64.b64encode(b"\x89PNG fixture").decode(), **(call or {})}
    partial = _event({"type": "response.image_generation_call.partial_image", "partial_image_index": 0,
                      "partial_image_b64": base64.b64encode(b"preview" * 8).decode()})
    frames = _responses(final) if case != "one_of_two_calls_failed" else _responses(
        {"type": "image_generation_call", "id": "ig_0", "status": "failed"}, final,
    )
    # Two partials were asked for; six are more than the final and the margin allow.
    stream = (frames[0], *([partial] * (6 if case == "too_many_images" else 1)), *frames[1:])
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=OPENAI), stream),
    ])
    _bind(service, "image_generation", (OPENAI, "gpt-6-astra"))
    status, answer = await _call(service, "image_generation", {
        "arguments": {"prompt": "a boat"}, "executor": {"source_id": OPENAI, "model_id": "gpt-6-astra"},
    })
    assert status == 200
    assert answer["record"]["status"] == "failed" and error in answer["record"]["error"]
    assert "images" not in answer and "execution" in answer


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
        "arguments": {"query": "q"}, "executor": {"source_id": ANTHROPIC, "model_id": "claude-haiku-4-5"},
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
    body = json.dumps({"arguments": {"query": "q"}, "executor": {"source_id": XAI, "model_id": "grok-4.6"}})
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:own", turn_id="turn-own")
        async with aiohttp.ClientSession() as client:
            pending = asyncio.create_task(client.post(
                f'{hop["base_url"]}/hosted-tools/web_search',
                headers={"Authorization": f'Bearer {hop["token"]}'}, data=body,
            ))
            await asyncio.wait_for(blocked.started.wait(), timeout=5)
            finished = []
            completion = gateway.finalize_turn("turn-own", settled_by=SETTLED_BY_STOPPED, finish=lambda: finished.append(1))
            assert completion is not None
            await asyncio.wait_for(completion, timeout=5)
            assert blocked.cancelled.is_set() and finished == [1]
            pending.cancel()
            # The turn no longer admits work: a new call is refused before anything runs.
            async with client.post(
                f'{hop["base_url"]}/hosted-tools/web_search',
                headers={"Authorization": f'Bearer {hop["token"]}'}, data=body,
            ) as response:
                assert response.status == 409
                assert (await response.json())["error"]["code"] == "executor_not_bound"
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
])
async def test_only_a_response_that_completed_completes_the_call(tmp_path, case, executor, chunks, status, error, sources):
    """MH-HOSTED-GATEWAY-005: completion is an allow-list on both protocols; anything unreadable fails cleanly."""

    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=executor[0]), chunks),
    ])
    _bind(service, "web_search", executor)
    answer_status, answer = await _call(service, "web_search", {
        "arguments": {"query": "q"}, "executor": {"source_id": executor[0], "model_id": executor[1]},
    })
    assert answer_status == 200
    assert answer["record"]["status"] == status
    assert error in answer["record"]["error"]
    assert answer["record"]["sources"] == sources
    assert _hosted_result("web_search", answer).record.status == status


@pytest.mark.asyncio
async def test_a_turn_keeps_its_hosted_binding_until_the_next_turn(tmp_path):
    """MH-HOSTED-GATEWAY-005: settings apply from the next turn; later attempts of a turn keep its executors."""

    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=XAI), _responses(WEB_CALL, WEB_ANSWER)),
    ])
    _bind(service, "web_search", (XAI, "grok-4.6"))
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        first = await router.resolve_hop("menu-alias", process_scope="vibey:freeze", turn_id="turn-freeze")
        # Mid-turn, the user moves web search to another executor and turns X search on.
        _bind(service, "web_search", (OPENAI, "gpt-6-astra"))
        _bind(service, "x_search", (XAI, "grok-4.6"))
        again = await router.resolve_hop("menu-alias", process_scope="vibey:freeze", turn_id="turn-freeze")
        assert again["hosted_tools"].keys() == first["hosted_tools"].keys() == {"web_search"}
        assert again["hosted_tools"]["web_search"]["executor"] == {"source_id": XAI, "model_id": "grok-4.6"}
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f'{again["base_url"]}/hosted-tools/web_search',
                headers={"Authorization": f'Bearer {again["token"]}'},
                data=json.dumps({"arguments": {"query": "q"}, "executor": again["hosted_tools"]["web_search"]["executor"]}),
            ) as response:
                assert response.status == 200
                assert (await response.json())["execution"]["executor_source_id"] == XAI
        completion = router.settle_turn("turn-freeze", settled_by=SETTLED_BY_STOPPED, ts=NOW.isoformat())
        if completion is not None:
            await completion
        nxt = await router.resolve_hop("menu-alias", process_scope="vibey:freeze", turn_id="turn-next")
        assert nxt["hosted_tools"]["web_search"]["executor"] == {"source_id": OPENAI, "model_id": "gpt-6-astra"}
        assert "x_search" in nxt["hosted_tools"]
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_a_proxy_call_body_is_bounded(tmp_path):
    """MH-HOSTED-GATEWAY-005: a call carries a few arguments; an oversized body is refused before it is parsed."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "web_search", None)
    status, answer = await _call(service, "web_search", json.dumps(
        {"arguments": {"query": "q" * (1024 * 1024)}, "executor": None},
    ).encode())
    assert (status, answer["error"]["code"]) == (400, "invalid_request_error")
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_items_only_the_terminal_response_carries_are_kept(tmp_path):
    """MH-HOSTED-GATEWAY-004: a relay that streams some items but not others still yields the terminal list's."""

    reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
    image = {"type": "image_generation_call", "id": "ig_1", "status": "completed", "output_format": "png",
             "result": base64.b64encode(b"\x89PNG fixture").decode(), "revised_prompt": "a boat"}
    frames = (
        _event({"type": "response.output_item.done", "item": reasoning}),
        _event({"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed", "output": [reasoning, image]}}),
    )
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=OPENAI), frames),
    ])
    _bind(service, "image_generation", (OPENAI, "gpt-6-astra"))
    status, answer = await _call(service, "image_generation", {
        "arguments": {"prompt": "a boat"}, "executor": {"source_id": OPENAI, "model_id": "gpt-6-astra"},
    })
    assert status == 200
    assert answer["record"]["status"] == "completed"
    assert [base64.b64decode(item["data"]) for item in answer["images"]] == [b"\x89PNG fixture"]


@pytest.mark.asyncio
async def test_a_call_cancelled_before_its_handle_is_still_metered(tmp_path):
    """MH-HOSTED-GATEWAY-005: wire facts observed after admission are billed, so they reach the usage ledger."""

    service = _vibey_service(tmp_path, _sources())
    _bind(service, "web_search", (XAI, "grok-4.6"))
    observed = ProtocolSSEState("openai_responses")
    observed.observe(_responses(usage={"input_tokens": 1200, "output_tokens": 3})[-1])

    async def cancelled_after_admission(source_id, model_id, request, stream, origin, *, on_admitted=None):
        on_admitted()
        raise InvokeCancelledError(observed)

    service.adapter.invoke = cancelled_after_admission
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    try:
        hop = await router.resolve_hop("menu-alias", process_scope="vibey:cancel", turn_id="turn-cancel")
        with pytest.raises(asyncio.CancelledError):
            await service.run_hosted_tool(
                "web_search",
                {"arguments": {"query": "q"}, "executor": {"source_id": XAI, "model_id": "grok-4.6"}},
                bindings=gateway.correlation.hosted_executor_bindings(backend="vibey", token=hop["token"])[1],
            )
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
                headers={"Authorization": f'Bearer {hop["token"]}', "Content-Encoding": "gzip"},
                data=gzip.compress(json.dumps({"arguments": {"query": "q"}, "executor": None}).encode()),
            ) as response:
                assert response.status == 400
    finally:
        await gateway.close()
    assert service.adapter.invocations == []


@pytest.mark.asyncio
async def test_a_buffered_executor_response_is_bounded(tmp_path, monkeypatch):
    """MH-HOSTED-GATEWAY-005: an oversized buffered body fails the call instead of growing without bound."""

    monkeypatch.setattr(service_module, "EXECUTOR_RESPONSE_MAX_BYTES", 64)
    service = _vibey_service(tmp_path, _sources(), handles=[
        LiveInvokeHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=ANTHROPIC), (b"{" + b" " * 128 + b"}",)),
    ])
    _bind(service, "web_search", (ANTHROPIC, "claude-haiku-4-5"))
    status, answer = await _call(service, "web_search", {
        "arguments": {"query": "q"}, "executor": {"source_id": ANTHROPIC, "model_id": "claude-haiku-4-5"},
    })
    assert status == 200
    assert answer["record"]["status"] == "failed" and "larger than allowed" in answer["record"]["error"]
    assert "execution" in answer


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "body", "status", "code"), [
    ("web_search", {"arguments": {"query": "q", "count": 3}, "executor": None}, 400, "invalid_request_error"),
    ("web_search", {"arguments": {"query": " "}, "executor": None}, 400, "invalid_request_error"),
    ("x_search", {"arguments": {"query": "q", "mode": "Oldest"}, "executor": None}, 400, "invalid_request_error"),
    ("web_search", {"arguments": {"query": "q"}}, 400, "invalid_request_error"),
    ("web_search", b"not json", 400, "invalid_request_error"),
    ("code_interpreter", {"arguments": {}, "executor": None}, 404, "not_found_error"),
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
        service, "web_search", {"arguments": {"query": "q"}, "executor": None}, token="not-the-turn-token",
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

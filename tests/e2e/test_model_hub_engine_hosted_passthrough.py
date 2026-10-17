"""Hosted tools preserve business payloads through the real engine on same-protocol paths.

These engine-boundary scenarios protect same-protocol hosted declarations,
``include`` values, output items and replayed history against CPA upgrades.
The existing mocked gateway and consumer tests do not run CPA's translators.
Each case sends one request to the real engine under Avibe's rendered config;
a synthetic loopback upstream records request bytes and replays fixed wire
fixtures. This is not a live provider or a complete Vibey-turn acceptance test.
With ``AVIBE_HOSTED_WIRE_EVIDENCE_DIR`` set, each case also writes both
directions there, so ``scripts/model_hub_hosted_wire_gate.py`` can compare two
engines' runs byte for byte.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

from aiohttp import web
import pytest

from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.test_model_hub_google import _server
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service


pytestmark = pytest.mark.e2e_model_hub

EVIDENCE_DIR_ENV = "AVIBE_HOSTED_WIRE_EVIDENCE_DIR"
MODEL = "hosted-model"
# Fernet-shaped, so the engine keeps it with its id (CPA drops other shapes).
_FERNET = "g" + "A" * 97
_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
WEB_SEARCH = {"type": "web_search"}
X_SEARCH = {"type": "x_search"}
IMAGE_GENERATION = {"type": "image_generation", "partial_images": 2, "output_format": "jpeg", "output_compression": 90}
FUNCTION = {"type": "function", "name": "read", "description": "fixture",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
INCLUDE = ["web_search_call.action.sources", "reasoning.encrypted_content"]

WEB_SEARCH_CALL = {
    "id": "ws_1",
    "type": "web_search_call",
    "status": "completed",
    "action": {
        "type": "search",
        "query": "CLIProxyAPI 版本 🦊",
        "queries": ["CLIProxyAPI 版本 🦊"],
        "sources": [{"type": "url", "url": "https://github.com/router-for-me/CLIProxyAPI/releases"}],
    },
}
X_SEARCH_CALL = {
    "type": "custom_tool_call",
    "id": "ctc_1",
    "call_id": "xs_call-1",
    "name": "x_keyword_search",
    "input": json.dumps({"query": "from:OpenAI within_time:7d", "limit": "10", "mode": "Latest"}),
    "status": "completed",
}
X_REASONING = {"type": "reasoning", "id": "tco_1", "summary": [], "encrypted_content": _FERNET}
# xAI's own ciphertext has no Fernet envelope. Without ``is-compat`` on the Source's models the codex
# executor drops its ``encrypted_content`` and ``id`` (CPA ``openai_responses_signature.go``); the
# replay case records what reaches the upstream rather than asserting it.
XAI_OPAQUE_REASONING = {"type": "reasoning", "id": "tco_2", "summary": [], "encrypted_content": "xai-opaque-ciphertext"}


def _xai_ciphertext(seed: str, length: int) -> str:
    """Opaque like xAI's own ciphertext, which has no Fernet envelope."""
    stream = b"".join(hashlib.sha256(f"{seed}-{block}".encode()).digest() for block in range(length // 32 + 1))
    return base64.b64encode(stream).decode()[:length]


# One xAI X-search turn in the 2026-10-13 spike's real shapes. The turn's reasoning is ``rs_<turn>``
# (39 runes). Each server-side tool call ``ctc_<turn>_<call>`` has its own ``tco_<turn>_<call>``
# reasoning; both of those ids are 84 runes.
XAI_TURN = "1994eace-959a-9bc5-87ca-e01dac5c7bfe"
XAI_CALL = "call-9696c08a-e2e1-4204-ab58-9d1d317c128f-2"
XAI_REASONING = {
    "id": f"rs_{XAI_TURN}",
    "summary": [{"type": "summary_text", "text": "I need the most recent post, so I fetch its thread."}],
    "type": "reasoning",
    "status": "completed",
    "encrypted_content": _xai_ciphertext("rs", 3211),
}
XAI_THREAD_FETCH = {
    "call_id": f"xs_{XAI_CALL}",
    "input": json.dumps({"post_id": "2107909792945832070"}),
    "name": "x_thread_fetch",
    "type": "custom_tool_call",
    "id": f"ctc_{XAI_TURN}_{XAI_CALL}",
    "status": "completed",
}
XAI_CALL_REASONING = {
    "id": f"tco_{XAI_TURN}_{XAI_CALL}",
    "summary": [],
    "type": "reasoning",
    "status": "completed",
    "encrypted_content": _xai_ciphertext("tco", 3696),
}
IMAGE_CALL = {
    "id": "ig_1",
    "type": "image_generation_call",
    "status": "completed",
    "result": _PNG_B64,
    "revised_prompt": "A red fox in snow",
    "output_format": "jpeg",
    "size": "1024x1024",
    "quality": "high",
    "background": "opaque",
}
ANSWER = "The latest release is v8.0.27.[[1]](https://github.com/router-for-me/CLIProxyAPI/releases)"
ANSWER_ITEM = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{
        "type": "output_text",
        "text": ANSWER,
        "annotations": [{"type": "url_citation", "url": "https://github.com/router-for-me/CLIProxyAPI/releases",
                         "start_index": 29, "end_index": len(ANSWER), "title": "1"}],
    }],
}
RESPONSES_USAGE = {
    "input_tokens": 22965,
    "output_tokens": 1402,
    "total_tokens": 24367,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 210},
    "cost_in_usd_ticks": 277316400,
    "server_side_tool_usage_details": {"web_search_calls": 1, "x_search_calls": 1},
}

HIT = {
    "type": "web_search_result",
    "title": "Releases · CLIProxyAPI 发布",
    "url": "https://github.com/router-for-me/CLIProxyAPI/releases",
    "encrypted_content": "EqoBCioIBxgCIiQ4ZGUxYzRhMS0wYjk0LTQ2NjktYjM1ZS1hYmFhNmRkODQ3OGI",
    "page_age": "15 days ago",
}
LOCATION = {
    "type": "web_search_result_location",
    "url": HIT["url"],
    "title": HIT["title"],
    "cited_text": "v8.0.24 Latest",
    "encrypted_index": "Eo8BCioIBxgCIiQ4ZGUxYzRhMS0wYjk0LTQ2NjktYjM1ZS1hYmFhNmRkODQ3OGI",
}
ANTHROPIC_WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
ANTHROPIC_TOOL = {"name": "read", "description": "fixture",
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


def _responses_stream(items: list[dict], *, partial_images: bool = False) -> bytes:
    """A Responses stream in the spike's shape: every item added, progress frames, done, then completed."""
    events: list[dict] = [{"type": "response.created", "response": {
        "id": "resp_hosted", "object": "response", "status": "in_progress", "model": MODEL, "output": []}}]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index,
                       "item": {key: value for key, value in item.items() if key != "result"} | {"status": "in_progress"}})
        if item["type"] == "web_search_call":
            for phase in ("in_progress", "searching", "completed"):
                events.append({"type": f"response.web_search_call.{phase}", "output_index": index, "item_id": item["id"]})
        if item["type"] == "image_generation_call":
            events.append({"type": "response.image_generation_call.in_progress", "output_index": index,
                           "item_id": item["id"]})
            events.append({"type": "response.image_generation_call.generating", "output_index": index,
                           "item_id": item["id"]})
            if partial_images:
                for partial in range(2):
                    events.append({"type": "response.image_generation_call.partial_image", "output_index": index,
                                   "item_id": item["id"], "partial_image_index": partial,
                                   "partial_image_b64": _PNG_B64, "output_format": "jpeg", "size": "1024x1024"})
            events.append({"type": "response.image_generation_call.completed", "output_index": index,
                           "item_id": item["id"]})
        if item["type"] == "message":
            events.append({"type": "response.output_text.delta", "output_index": index, "item_id": item["id"],
                           "content_index": 0, "delta": item["content"][0]["text"]})
            events.append({"type": "response.output_text.annotation.added", "output_index": index,
                           "item_id": item["id"], "content_index": 0, "annotation_index": 0,
                           "annotation": item["content"][0]["annotations"][0]})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed", "response": {
        "id": "resp_hosted", "object": "response", "status": "completed", "model": MODEL, "output": items,
        "usage": RESPONSES_USAGE}})
    return b"".join(_sse({**event, "sequence_number": number}) for number, event in enumerate(events))


def _anthropic_stream(blocks: list[list[dict]], *, server_tool_use: int) -> bytes:
    events: list[dict] = [{"type": "message_start", "message": {
        "id": "msg_hosted", "type": "message", "role": "assistant", "model": MODEL, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 6, "output_tokens": 0}}}]
    for index, frames in enumerate(blocks):
        events.extend({**frame, "index": index} for frame in frames)
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                   "usage": {"output_tokens": 519,
                             "server_tool_use": {"web_search_requests": server_tool_use, "web_fetch_requests": 0}}})
    events.append({"type": "message_stop"})
    return b"".join(_sse(event) for event in events)


def _anthropic_search(call_id: str, query: str) -> list[list[dict]]:
    half = len(query) // 2
    return [
        [{"type": "content_block_start", "content_block": {"type": "server_tool_use", "id": call_id,
                                                           "name": "web_search", "input": {}}},
         {"type": "content_block_delta", "delta": {"type": "input_json_delta",
                                                   "partial_json": f'{{"query": "{query[:half]}'}},
         {"type": "content_block_delta", "delta": {"type": "input_json_delta",
                                                   "partial_json": f'{query[half:]}"}}'}}],
        [{"type": "content_block_start", "content_block": {"type": "web_search_tool_result", "tool_use_id": call_id,
                                                           "content": [HIT]}}],
        [{"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
         {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "The latest release is v8.0.24."}},
         {"type": "content_block_delta", "delta": {"type": "citations_delta", "citation": LOCATION}}],
    ]


@asynccontextmanager
async def _engine(tmp_path, monkeypatch, protocol: str, reply: bytes, *, vendor: str = "custom"):
    """One Source of ``protocol`` on a capturing upstream; yields (base_url, token, model, captured)."""
    captured: list[dict] = []

    async def upstream(request):
        body = await request.read()
        captured.append({
            "path": request.path,
            "query": request.query_string,
            "headers": [(key.lower(), value) for key, value in request.headers.items()],
            "body": body,
        })
        return web.Response(body=reply, content_type="text/event-stream")

    with _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        async with _server(upstream) as root:
            source = _source("src_hosted001", "Hosted fixture", vendor=vendor, protocol=protocol, model_id=MODEL)
            source.base_url = root + ("/v1" if protocol == "openai_responses" else "")
            source.credential_ref = adapter.state_store.store_api_key(
                "sk-synthetic-hosted", vendor=vendor, protocol=protocol, base_url=source.base_url,
            )
            config = _vibey_service(tmp_path, [source]).store.config
            service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
            await service.runtime_start()
            connection = adapter.supervisor.client_if_running().connection
            prefix = adapter.state_store.list_sources()[0].prefix
            yield connection.base_url, connection.gateway_token, f"{prefix}/{MODEL}", captured


def _post(base_url: str, token: str, path: str, body: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(base_url + path, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "avibe-hosted-test/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _events(raw: bytes) -> list[dict]:
    parsed = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            try:
                parsed.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    return parsed


def _without_cache_control(item: dict) -> dict:
    """Ignore only engine-added metadata on one tool or content block, never nested business data."""
    return {key: value for key, value in item.items() if key != "cache_control"}


def _anthropic_history(messages: list[dict]) -> list[dict]:
    """Keep every message/block in order; normalize only text form and shallow cache metadata."""
    normalized = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        normalized.append({**message, "content": [_without_cache_control(block) for block in content]})
    return normalized


def _stable(body: bytes) -> bytes:
    """The request bytes with only the engine's per-run ``prompt_cache_key`` value masked, in place."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict) or "prompt_cache_key" not in payload:
        return body
    field = b'"prompt_cache_key":' + json.dumps(payload["prompt_cache_key"]).encode()
    assert body.count(field) == 1, "prompt_cache_key is not one compact field"
    return body.replace(field, b'"prompt_cache_key":"<per-run>"')


def _stable_header(name: str, value: str) -> str:
    """Mask what differs between two runs of one engine: its per-request session, and the upstream's port."""
    if name == "session-id":
        return "<per-run>"
    if name == "host":
        return re.sub(r":[0-9]+$", ":<per-run>", value)
    return value


def _record(captured: list[dict], status: int, raw: bytes) -> dict:
    """Return the one upstream call; with an evidence directory, also write both directions there."""
    [call] = captured
    directory = os.environ.get(EVIDENCE_DIR_ENV)
    if directory:
        case = os.environ["PYTEST_CURRENT_TEST"].split("::")[-1].split(" ")[0]
        wire = {
            "upstream": {
                "path": call["path"],
                "query": call["query"],
                "headers": sorted([name, _stable_header(name, value)] for name, value in call["headers"]),
                "body": _stable(call["body"]).decode(),
            },
            "downstream": {"status": status, "body": raw.decode()},
        }
        text = json.dumps(wire, ensure_ascii=False, indent=1, sort_keys=True)
        (Path(directory) / f"{case}.json").write_text(text, encoding="utf-8")
    return call


async def test_responses_hosted_declarations_and_items_pass_through(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-001: declarations, include values and hosted output items survive CPA."""
    items = [WEB_SEARCH_CALL, X_SEARCH_CALL, X_REASONING, IMAGE_CALL, ANSWER_ITEM]
    reply = _responses_stream(items, partial_images=True)
    body = {"model": None, "stream": True, "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "search and draw"}]}],
        "tools": [WEB_SEARCH, X_SEARCH, IMAGE_GENERATION, FUNCTION], "include": INCLUDE,
        "reasoning": {"effort": "high", "summary": "auto"}, "store": False}
    async with _engine(tmp_path, monkeypatch, "openai_responses", reply) as (base_url, token, model, captured):
        status, raw = await asyncio.to_thread(_post, base_url, token, "/v1/responses", {**body, "model": model})
    call = _record(captured, status, raw)
    sent = json.loads(call["body"])
    assert status == 200, raw[:500]
    assert sent["tools"] == body["tools"], sent["tools"]
    # CPA canonicalizes include order; compare the full multiset, not membership.
    assert sorted(sent["include"]) == sorted(INCLUDE), sent["include"]
    # Every decoded event must survive, including progress, deltas, citations and usage.
    assert _events(raw) == _events(reply)


async def test_responses_hosted_history_replays_verbatim(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-002: hosted replay keeps content, order and cardinality around messages."""
    history = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "search and draw"}]},
        WEB_SEARCH_CALL, X_SEARCH_CALL, X_REASONING, XAI_OPAQUE_REASONING, IMAGE_CALL, ANSWER_ITEM,
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "and again"}]},
    ]
    reply = _responses_stream([ANSWER_ITEM])
    body = {"model": None, "stream": True, "input": history, "tools": [WEB_SEARCH, X_SEARCH, IMAGE_GENERATION],
            "include": INCLUDE, "reasoning": {"effort": "high", "summary": "auto"}, "store": False}
    async with _engine(tmp_path, monkeypatch, "openai_responses", reply) as (base_url, token, model, captured):
        status, raw = await asyncio.to_thread(_post, base_url, token, "/v1/responses", {**body, "model": model})
    call = _record(captured, status, raw)
    assert status == 200, raw[:500]
    replayed = json.loads(call["body"])["input"]
    # Reasoning items are not hosted items: the engine prefixes their ids (``rs_``) and drops
    # ciphertext without a Fernet envelope unless the model is ``is-compat``. Recorded, not pinned.
    # Compare the rest as one sequence, including message anchors, so calls cannot be duplicated,
    # dropped, reordered, or moved across the assistant answer while still passing the contract.
    expected_history = [item for item in history if item.get("type") != "reasoning"]
    replayed_history = [item for item in replayed if item.get("type") != "reasoning"]
    assert replayed_history == expected_history, replayed
    assert _events(raw) == _events(reply)


async def test_image_generation_is_never_injected_or_answered_by_the_engine(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-003: passthrough mode never injects an undeclared image tool."""
    reply = _responses_stream([ANSWER_ITEM])
    body = {"model": None, "stream": True, "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "tools": [FUNCTION], "store": False}
    async with _engine(tmp_path, monkeypatch, "openai_responses", reply) as (base_url, token, model, captured):
        status, raw = await asyncio.to_thread(_post, base_url, token, "/v1/responses", {**body, "model": model})
    call = _record(captured, status, raw)
    assert status == 200, raw[:500]
    sent = json.loads(call["body"])
    assert sent["tools"] == body["tools"], sent.get("tools")
    assert _events(raw) == _events(reply)


async def test_anthropic_web_search_and_its_results_pass_through(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-004: Anthropic search declarations, results, citations and usage survive CPA."""
    reply = _anthropic_stream(_anthropic_search("srvtoolu_01", "CLIProxyAPI 版本 🦊"), server_tool_use=1)
    body = {"model": None, "stream": True, "max_tokens": 1024,
            "messages": [{"role": "user", "content": "search the latest CLIProxyAPI release"}],
            "tools": [ANTHROPIC_WEB_SEARCH, ANTHROPIC_TOOL]}
    async with _engine(tmp_path, monkeypatch, "anthropic", reply) as (base_url, token, model, captured):
        status, raw = await asyncio.to_thread(_post, base_url, token, "/v1/messages", {**body, "model": model})
    call = _record(captured, status, raw)
    assert status == 200, raw[:500]
    sent = json.loads(call["body"])
    assert [_without_cache_control(tool) for tool in sent["tools"]] == body["tools"], sent["tools"]
    assert _events(raw) == _events(reply)


async def test_anthropic_hosted_history_replays_verbatim(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-005: the entire Anthropic replay keeps messages, tools, results and citations."""
    use = {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search",
           "input": {"query": "CLIProxyAPI releases"}}
    result = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01", "content": [HIT]}
    text = {"type": "text", "text": "The latest release is v8.0.24.", "citations": [LOCATION]}
    reply = _anthropic_stream([[{"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
                                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}]],
                              server_tool_use=0)
    body = {"model": None, "stream": True, "max_tokens": 1024, "tools": [ANTHROPIC_WEB_SEARCH],
            "messages": [{"role": "user", "content": "search the latest CLIProxyAPI release"},
                         {"role": "assistant", "content": [use, result, text]},
                         {"role": "user", "content": "and the one before?"}]}
    async with _engine(tmp_path, monkeypatch, "anthropic", reply) as (base_url, token, model, captured):
        status, raw = await asyncio.to_thread(_post, base_url, token, "/v1/messages", {**body, "model": model})
    call = _record(captured, status, raw)
    assert status == 200, raw[:500]
    replayed = json.loads(call["body"])["messages"]
    assert _anthropic_history(replayed) == _anthropic_history(body["messages"]), replayed
    assert _events(raw) == _events(reply)


XAI_HISTORY = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "quote the latest post"}]},
    XAI_REASONING, XAI_THREAD_FETCH, XAI_CALL_REASONING, ANSWER_ITEM,
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "and the one before?"}]},
]


async def _replay_xai_turn(tmp_path, monkeypatch) -> tuple[list[dict], bytes, bytes]:
    """Replay ``XAI_HISTORY`` to an xAI Source as Avibe renders one; return what arrived and the reply sent."""
    reply = _responses_stream([ANSWER_ITEM])
    body = {"model": None, "stream": True, "input": XAI_HISTORY, "tools": [X_SEARCH], "include": INCLUDE,
            "reasoning": {"effort": "high", "summary": "auto"}, "store": False}
    async with _engine(tmp_path, monkeypatch, "openai_responses", reply, vendor="xai") as (
        base_url, token, model, captured,
    ):
        status, raw = await asyncio.to_thread(_post, base_url, token, "/v1/responses", {**body, "model": model})
    call = _record(captured, status, raw)
    assert status == 200, raw[:500]
    return json.loads(call["body"])["input"], raw, reply


async def test_responses_xai_history_replays_in_real_shapes(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-006: an xAI X-search turn replayed in xAI's real id and ciphertext shapes.

    The 84-rune ids exceed the engine's 64-rune input id limit, which the short ids above never reach.
    What the engine does with xAI reasoning and the over-limit call id is the next case's subject, so
    the gate compares those bytes here instead of pinning them. Everything else is pinned: the ordered
    non-reasoning sequence, the call's own fields, and the decoded response.
    """
    replayed, raw, reply = await _replay_xai_turn(tmp_path, monkeypatch)

    def without_call_id(items: list[dict]) -> list[dict]:
        return [{key: value for key, value in item.items() if not (item["type"] == "custom_tool_call" and key == "id")}
                for item in items if item.get("type") != "reasoning"]

    assert without_call_id(replayed) == without_call_id(XAI_HISTORY), replayed
    assert _events(raw) == _events(reply)


@pytest.mark.xfail(
    strict=True, reason="engine drops/shortens xAI over-64 ids under is-compat; fixed by the id patch in #2780",
)
async def test_responses_xai_over_limit_ids_replay_verbatim(tmp_path, monkeypatch) -> None:
    """MH-ENGINE-HOSTED-007: xAI's 84-rune tool-call reasoning and call ids reach xAI as it issued them.

    Under ``is-compat``, which Avibe renders for xAI, the pinned engine keeps the ``rs_`` reasoning but
    drops the ``tco_`` reasoning whole and shortens the ``ctc_`` id to 64 runes. An engine that keeps
    them turns this strict expected failure into a failure, so that change must flip the marker.
    """
    replayed, _raw, _reply = await _replay_xai_turn(tmp_path, monkeypatch)

    arrived = [item for item in replayed if item["type"] == "reasoning" and item.get("id") == XAI_CALL_REASONING["id"]]
    assert [item.get("encrypted_content") for item in arrived] == [XAI_CALL_REASONING["encrypted_content"]], replayed
    [thread_fetch] = [item for item in replayed if item["type"] == "custom_tool_call"]
    assert thread_fetch["id"] == XAI_THREAD_FETCH["id"], thread_fetch

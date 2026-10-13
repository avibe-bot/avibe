"""MH-HOSTED-GATEWAY-001: with the engine in passthrough, only Codex CLI gets the hosted image tool.

The engine used to add ``{"type": "image_generation", "output_format": "png"}``
to every request its Codex executor served, for any caller. It now forwards
declared tools unchanged, and the gateway adds the tool back for Codex CLI
alone, under the engine's own skip conditions. Each case drives the real
gateway and engine adapter against a loopback engine and compares the exact
body bytes it receives with what the engine itself would have produced
(CLIProxyAPI ``ensureImageGenerationTool`` at d318bcc3).
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _service, _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import EngineStateStore

IMAGE_TOOL = {"type": "image_generation", "output_format": "png"}
SHELL = {"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}}
DECLARED_IMAGE = {"type": "image_generation", "quality": "high"}
INPUT = [{"role": "user", "content": [{"type": "input_text", "text": "画一只猫 café"}]}]
LITE = "ws_request_header_x_openai_internal_codex_responses_lite"
COMPLETED = {
    "/v1/responses": (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed","response":'
        b'{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1}}}\n\n'
    ),
    "/v1/messages": (
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":'
        b'{"input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":1}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    ),
}


def _id_token(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


@asynccontextmanager
async def _engine(received: list[bytes]):
    async def respond(request: web.Request) -> web.Response:
        received.append(await request.read())
        return web.Response(body=COMPLETED[request.path], content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/{path:.*}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def _wire(service, tmp_path, root: str, grants: dict[str, dict]) -> EngineStateStore:
    """Bind every configured Source in a real engine state store and adapter at ``root``."""

    state = EngineStateStore(tmp_path / "engine")
    for configured in service.store.config.sources:
        if configured.id in grants:
            configured.credential_ref = state.bind_oauth_credential(configured.id, configured.vendor, "grant.json")
            state.write_oauth_auth_file("grant.json", {
                "type": "codex",
                "prefix": state.credential_metadata(configured.credential_ref)["prefix"],
                "access_token": "private-access-fixture",
                **grants[configured.id],
            })
        else:
            configured.credential_ref = state.store_api_key(
                "fixture-key", vendor=configured.vendor, protocol=configured.protocol,
            )
    state.sync_sources(service._bindings(service.store.load()))
    client = EngineClient(EngineConnection(root, "fixture-management", "fixture-gateway"))
    adapter = CLIProxyEngineAdapter(supervisor=SimpleNamespace(client=lambda: client), state_store=state)
    adapter.ensure_installed = service.adapter.ensure_installed
    service.adapter = adapter
    service._engine_synced = True
    return state


async def _post(url: str, token: str, body: dict) -> None:
    async with aiohttp.ClientSession() as downstream:
        async with downstream.post(url, json=body, headers={"Authorization": f"Bearer {token}"}) as response:
            assert response.status == 200, await response.text()
            await response.read()


def _subscription(source):
    return replace(source, kind="subscription", billing="monthly")


# (case, request fields after model/stream/input, hop model, ChatGPT grant or None for an API key,
#  the tools the engine must receive; ABSENT when the request has no tools key and gets none)
ABSENT = object()
CODEX_CASES = [
    ("no_tools", {}, "gpt-fixture", None, [IMAGE_TOOL]),
    ("tools_null", {"tools": None}, "gpt-fixture", None, [IMAGE_TOOL]),
    ("tools_object", {"tools": {"type": "function"}}, "gpt-fixture", None, [IMAGE_TOOL]),
    ("other_tools", {"tools": [SHELL]}, "gpt-fixture", None, [SHELL, IMAGE_TOOL]),
    ("image_tool_present", {"tools": [DECLARED_IMAGE, SHELL]}, "gpt-fixture", None, [DECLARED_IMAGE, SHELL]),
    (
        "image_gen_function",
        {"tools": [SHELL, {"type": "function", "name": "image_gen.imagegen"}]},
        "gpt-fixture", None,
        [SHELL, {"type": "function", "name": "image_gen.imagegen"}],
    ),
    (
        "image_gen_namespace",
        {"tools": [{"type": "namespace", "name": "image_gen", "tools": [{"type": "function", "name": "imagegen"}]}]},
        "gpt-fixture", None,
        [{"type": "namespace", "name": "image_gen", "tools": [{"type": "function", "name": "imagegen"}]}],
    ),
    (
        "namespace_without_imagegen",
        {"tools": [{"type": "namespace", "name": "image_gen", "tools": [{"type": "function", "name": "edit"}]}]},
        "gpt-fixture", None,
        [{"type": "namespace", "name": "image_gen", "tools": [{"type": "function", "name": "edit"}]}, IMAGE_TOOL],
    ),
    ("spark", {"tools": [SHELL]}, "gpt-fixture-codex-spark", None, [SHELL]),
    ("spark_with_thinking_suffix", {}, "gpt-fixture-spark(high)", None, ABSENT),
    ("plan_free", {}, "gpt-fixture", {"plan_type": " Free "}, ABSENT),
    ("plan_plus", {}, "gpt-fixture", {"plan_type": "plus"}, [IMAGE_TOOL]),
    ("token_plan_free", {}, "gpt-fixture", {"id_token": _id_token({"https://api.openai.com/auth": {}})}, ABSENT),
    (
        "token_plan_pro",
        {}, "gpt-fixture",
        {"id_token": _id_token({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}})},
        [IMAGE_TOOL],
    ),
    ("token_unreadable", {}, "gpt-fixture", {"id_token": "not-a-jwt"}, ABSENT),
    ("grant_without_plan", {}, "gpt-fixture", {}, [IMAGE_TOOL]),
    ("lite_true", {"client_metadata": {LITE: True}}, "gpt-fixture", None, ABSENT),
    ("lite_string", {"client_metadata": {LITE: " TRUE "}}, "gpt-fixture", None, ABSENT),
    ("lite_false", {"client_metadata": {LITE: "false"}}, "gpt-fixture", None, [IMAGE_TOOL]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("fields", "model", "grant", "tools"), [case[1:] for case in CODEX_CASES],
                         ids=[case[0] for case in CODEX_CASES])
async def test_codex_requests_reach_the_engine_with_the_engines_own_image_tool(
    tmp_path, fields, model, grant, tools,
):
    """MH-HOSTED-GATEWAY-001: one row per CPA condition; the engine receives exactly CPA's bytes."""
    configured = _source("src_codex001", "Codex", model_id=model)
    if grant is not None:
        configured = _subscription(configured)
    service = _service(tmp_path, sources=[configured])
    models = _canonicalize_fixed_test_routes(service)
    received: list[bytes] = []
    async with _engine(received) as root:
        state = _wire(service, tmp_path, root, {configured.id: grant} if grant is not None else {})
        gateway = ModelHubTurnGateway(service)
        try:
            base_url, token = await gateway.endpoint(
                "codex", process_scope="/repo", turn_id="turn_image_tool",
                requested_model_id=models["codex"], resolved_model_id=model, source_id=configured.id,
            )
            await _post(f"{base_url}/v1/responses", token, {"model": model, "stream": True, "input": INPUT, **fields})
        finally:
            await gateway.close()
    routed = f"{state.get_source(configured.id).prefix}/{model}"
    expected = {"model": routed, "stream": True, "input": INPUT, **fields}
    if tools is not ABSENT:
        expected["tools"] = tools
    # The engine appends a missing key at the end of the object and replaces an existing one in place.
    assert received == [json.dumps(expected).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "declared"])
@pytest.mark.parametrize(("backend", "path"), [
    ("claude", "messages"), ("opencode", "responses"), ("vibey", "responses"),
])
async def test_other_callers_reach_the_engine_with_exactly_their_declared_tools(tmp_path, backend, path, declared):
    """MH-HOSTED-GATEWAY-001: Claude, OpenCode and Vibey get no image tool they did not declare."""
    configured = _source("src_shared001", "Responses key")
    tools = [SHELL, DECLARED_IMAGE] if declared else [SHELL]
    if backend == "claude":
        tools = [{"name": "shell", "input_schema": {"type": "object"}}, *([DECLARED_IMAGE] if declared else [])]
    received: list[bytes] = []
    async with _engine(received) as root:
        if backend == "vibey":
            service = _vibey_service(tmp_path, [configured])
        else:
            service = _service(tmp_path, sources=[configured])
            _canonicalize_fixed_test_routes(service)
            service.store.requested_models["opencode"] = "shared-model"
        _wire(service, tmp_path, root, {})
        gateway = ModelHubTurnGateway(service)
        try:
            if backend == "vibey":
                router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
                hop = await router.resolve_hop("menu-alias", process_scope="vibey:image", turn_id="turn-image")
                url, token, model = f'{hop["base_url"]}/{path}', hop["token"], hop["runtime_model"]
            else:
                base_url, token = await gateway.endpoint(
                    backend, process_scope="/repo", turn_id="turn_image_tool",
                    requested_model_id=service.store.requested_models[backend],
                    resolved_model_id="shared-model", source_id=configured.id,
                )
                url, model = f"{base_url}/v1/{path}", "shared-model"
            await _post(url, token, {"model": model, "stream": True, "tools": tools, "input": INPUT})
        finally:
            await gateway.close()
    assert len(received) == 1
    assert json.loads(received[0])["tools"] == tools

"""Upstream response headers the engine passes through stop at Avibe's gateway (#2780, #2666).

The engine renders ``passthrough-headers: true``, so it forwards an upstream's
response headers to Avibe: on success with hop-by-hop ones removed, on an error
with nothing filtered. Avibe reads only ``ENGINE_RESPONSE_HEADER_ALLOW_LIST`` and
builds every response it sends from its own headers. Each case drives the real
gateway and engine adapter against a loopback engine that answers with injected
upstream headers, and checks what reaches the caller.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from tests.test_model_hub_declared_tools import COMPLETED, INPUT, _wire
from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _service, _source
from vibe.model_hub_runtime.client import (
    CLAUDE_UNIFIED_RATE_LIMIT_HEADER_NAMES,
    ENGINE_RESPONSE_HEADER_ALLOW_LIST,
    _allowed_claude_rate_limit_headers,
    _allowed_engine_header,
)
from core.handlers.model_hub.adapter import sanitize_response_correlation_ids

# What an upstream may send and the engine forwards: none of it belongs to Avibe's callers.
INJECTED = {
    "Set-Cookie": "upstream-session=secret; Path=/; HttpOnly",
    "Anthropic-Request-Id": "anthropic-request-1",
    "Anthropic-Organization-Id": "org-upstream",
    "Retry-After-Ms": "1500",
    "Anthropic-Ratelimit-Unified-Status": "rejected",
    "Anthropic-Ratelimit-Unified-5h-Status": "rejected",
    "Anthropic-Ratelimit-Unified-7d-Status": "allowed",
    "Anthropic-Ratelimit-Unified-7d_oi-Status": "allowed",
    "Anthropic-Ratelimit-Unified-Overage-Status": "rejected",
    "Anthropic-Ratelimit-Unified-Overage-Disabled-Reason": "plan_limit",
    "Anthropic-Ratelimit-Unified-Representative-Claim": "5h",
    "Anthropic-Ratelimit-Unified-5h-Utilization": "1.0",
    "Anthropic-Ratelimit-Unified-7d-Utilization": "0.25",
    "Anthropic-Ratelimit-Unified-5h-Reset": "1797000000",
    "Anthropic-Ratelimit-Unified-7d-Reset": "1798000000",
    "Anthropic-Ratelimit-Unified-7d_oi-Reset": "1798000000",
    "Anthropic-Ratelimit-Unified-Reset": "1798000000",
    "Anthropic-Ratelimit-Unified-Overage-Reset": "1798000000",
    "Anthropic-Ratelimit-Unified-Unknown": "must-not-cross",
    "X-Request-Id": "relay-request-1",
    "Request-Id": "provider-request-1",
    "X-Upstream-Injected": "1",
}
JSON_BODY = {
    "/v1/responses": b'{"id":"resp_1","object":"response","status":"completed","output":[],'
                     b'"usage":{"input_tokens":2,"output_tokens":1}}',
    "/v1/messages": b'{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"text","text":"ok"}],'
                    b'"stop_reason":"end_turn","usage":{"input_tokens":2,"output_tokens":1}}',
}
RATE_LIMITED = {
    "/v1/responses": b'{"error":{"type":"rate_limit_exceeded","code":"rate_limit_exceeded","message":"slow down"}}',
    "/v1/messages": b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}',
}


@asynccontextmanager
async def _engine(status: int, stream: bool):
    async def respond(request: web.Request) -> web.Response:
        await request.read()
        if status != 200:
            return web.Response(
                status=status, body=RATE_LIMITED[request.path], content_type="application/json",
                headers={**INJECTED, "Retry-After": "120"},
            )
        if stream:
            return web.Response(body=COMPLETED[request.path], content_type="text/event-stream", headers=INJECTED)
        return web.Response(body=JSON_BODY[request.path], content_type="application/json", headers=INJECTED)

    app = web.Application()
    app.router.add_post("/{path:.*}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def test_only_allowlisted_headers_are_read_from_an_engine_response():
    assert ENGINE_RESPONSE_HEADER_ALLOW_LIST == {
        "retry-after", "retry-after-ms", "request-id", "x-request-id",
        *CLAUDE_UNIFIED_RATE_LIMIT_HEADER_NAMES,
    }
    headers = {**INJECTED, "Retry-After": "120"}
    assert _allowed_engine_header(headers, "Retry-After") == "120"
    assert _allowed_engine_header({"rEtRy-AfTeR-Ms": "1500"}, "Retry-After-Ms") == "1500"
    assert _allowed_engine_header(headers, "Request-Id") == "provider-request-1"
    assert _allowed_engine_header(headers, "X-Request-Id") == "relay-request-1"
    assert _allowed_engine_header(headers, "Anthropic-Ratelimit-Unified-5H-Status") == "rejected"
    assert _allowed_engine_header(headers, "Anthropic-Ratelimit-Unified-Unknown") is None
    assert sanitize_response_correlation_ids(headers) == (
        ("request-id", "provider-request-1"),
        ("x-request-id", "relay-request-1"),
    )
    assert _allowed_engine_header(headers, "Set-Cookie") is None


def test_claude_unified_headers_are_exactly_projected_and_validated():
    headers = {
        "aNtHrOpIc-RaTeLiMiT-UnIfIeD-5h-StAtUs": "REJECTED",
        "Anthropic-Ratelimit-Unified-5h-Reset": "1797000000",
        "Anthropic-Ratelimit-Unified-Representative-Claim": "5h",
        "Anthropic-Ratelimit-Unified-Unknown": "private",
        "Anthropic-Ratelimit-Unified-7d-Reset": "not-a-date",
        "Anthropic-Ratelimit-Unified-7d-Status": "rejected\nforged",
        "Anthropic-Ratelimit-Unified-Overage-Status": "allowed",
    }
    assert _allowed_claude_rate_limit_headers(headers) == (
        ("anthropic-ratelimit-unified-5h-reset", "1797000000"),
        ("anthropic-ratelimit-unified-5h-status", "rejected"),
        ("anthropic-ratelimit-unified-overage-status", "allowed"),
        ("anthropic-ratelimit-unified-representative-claim", "5h"),
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Request-Id": "relay-1\nforged"}, ()),
        ({"X-Request-Id": "r" * 257}, ()),
        ({"X-Request-Id": "Bearer secret-token-value"}, ()),
        ({"X-Request-Id": "AIzaSyA1234567890abcdefghijklmnopqrstuv"}, ()),
        ({"X-Request-Id": "ghp_1234567890abcdefghijklmnopqrstuv"}, ()),
        ({"X-Request-Id": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"}, ()),
        ({"X-Request-Id": "sk-live_abcdefghijklmnop"}, ()),
        ({"X-Request-Id": "token=secret-value"}, ()),
        ({"Set-Cookie": "session=secret", "Organization-Id": "org-1"}, ()),
        ({"Request-Id": "provider-1", "Set-Cookie": "session=secret"}, (("request-id", "provider-1"),)),
    ],
)
def test_response_correlation_ids_are_bounded_and_credential_free(headers, expected):
    assert sanitize_response_correlation_ids(headers) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "stream"), [(200, True), (200, False), (429, True), (429, False)],
                         ids=["stream", "buffered", "stream-error", "buffered-error"])
@pytest.mark.parametrize(("backend", "path"), [("claude", "messages"), ("codex", "responses")])
async def test_no_upstream_header_the_engine_forwards_reaches_the_caller(tmp_path, backend, path, status, stream):
    configured = _source("src_headers01", "Responses key", model_id="shared-model")
    if backend == "claude":
        configured.vendor, configured.protocol = "anthropic", "anthropic"
    async with _engine(status, stream) as root:
        service = _service(tmp_path, sources=[configured])
        _canonicalize_fixed_test_routes(service)
        _wire(service, tmp_path, root, {})
        gateway = ModelHubTurnGateway(service)
        try:
            base_url, token = await gateway.endpoint(
                backend, process_scope="/repo", turn_id="turn_headers",
                requested_model_id=service.store.requested_models[backend],
                resolved_model_id="shared-model", source_id=configured.id,
            )
            body = {"model": "shared-model", "stream": stream, "max_tokens": 16}
            body |= {"messages": [{"role": "user", "content": "hi"}]} if path == "messages" else {"input": INPUT}
            async with aiohttp.ClientSession() as downstream:
                async with downstream.post(
                    f"{base_url}/v1/{path}", json=body, headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    await response.read()
                    received = {name.lower(): value for name, value in response.headers.items()}
                    served = response.status
        finally:
            await gateway.close()
    assert (served == 200) == (status == 200), (served, received)
    leaked = {name for name in received if name in {key.lower() for key in INJECTED}}
    assert leaked == set(), received
    # Retry advice reaches a caller only as the gateway's own whole-second header.
    assert received.get("retry-after", "120").isdigit(), received

"""The real gateway must commit only the winning Responses stream.

Existing recovery tests stop at the first-output barrier. These exercise the
exception after text/tool output, so a classification-only retry (duplicate
output), unmetered replay, or an unbounded replay loop cannot pass.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
import pytest

from core.handlers.model_hub.provenance import SERVED_HOP_HEADER
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from tests.test_model_hub_l3 import _source, _usage_of
from tests.test_model_hub_retry_lifecycle import configured_service, gateway_client, loopback_engine


OVERLOAD = "Our servers are currently overloaded. Please try again later."


def event(payload):
    return b"event: " + payload["type"].encode() + b"\ndata: " + json.dumps(payload).encode() + b"\n\n"


def output(label):
    return (
        event({"type": "response.output_text.delta", "delta": label})
        + event({"type": "response.output_item.added", "item": {
            "type": "function_call", "id": label, "call_id": label, "name": "fixture_tool", "arguments": "{}",
        }})
    )


def ending(*, error=None, message=OVERLOAD, envelope="response.failed", tokens=11, error_type=None):
    usage = {"input_tokens": tokens, "output_tokens": 2}
    if error and envelope == "error":
        return event({"type": "error", "code": error, "message": message})
    status = "failed" if error else "completed"
    return event({"type": f"response.{status}", "response": {
        "status": status, "usage": usage,
        **({"error": {"code": error, "message": message, **({"type": error_type} if error_type else {})}} if error else {}),
    }})


@pytest.mark.parametrize("vendor", ["openai", "custom"])
@pytest.mark.parametrize("envelope", ["response.failed", "error"])
@pytest.mark.parametrize("second_error", [None, "server_error", "permission_error"])
def test_buffered_overload_commits_only_final_attempt(tmp_path, vendor, envelope, second_error):
    """MH-RETRY-OVERLOAD-001: one replay, isolated bodies and per-call accounting."""
    async def run():
        source = _source("src_buffered01", "Buffered", vendor=vendor)
        source.base_url = "https://fixture.invalid"
        service, clock, models = configured_service(tmp_path, sources=[source])
        calls = []
        first = output("discarded-text-and-tool") + ending(error="server_error", envelope=envelope)
        final = output("final-text-and-tool") + ending(error=second_error, tokens=17)

        async def respond(request):
            calls.append(await request.json())
            assert len(calls) <= 2, "the second streamed failure must not replay"
            return web.Response(
                body=first if len(calls) == 1 else final, content_type="text/event-stream",
                headers={"x-request-id": f"attempt-{len(calls)}"},
            )

        async with loopback_engine(tmp_path, service, respond) as (adapter, _):
            async with gateway_client(service, "codex", models["codex"]) as (gateway, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": True}) as response:
                    assert response.status == 200
                    body = await response.read()
                    assert body == final
                    assert SERVED_HOP_HEADER not in response.headers  # Native callers do not receive Vibey origins.
                await gateway._drain_turn_requests("turn_lifecycle")
                assert len(calls) == 2 and calls[0] == calls[1]
                assert clock.delays == [30]
                assert adapter._active_transports == 0
                usage = _usage_of(service, source.id)
                assert usage["requests"] == 2
                assert usage["input_tokens"] == (28 if envelope == "response.failed" else 17)
                gateway.correlation.settle("turn_lifecycle", settled_by=SETTLED_BY_TERMINAL_RESULT)
                record = service.provenance.get("turn_lifecycle")
                assert record["failed_attempts"][0]["reason"] == "server_error"
                assert record["failed_attempts"][0]["response_correlation_ids"] == [
                    {"header": "x-request-id", "value": "attempt-1"},
                ]
                if second_error is None:
                    assert record["outcome"] == "served"
                    assert record["served"]["response_correlation_ids"] == [
                        {"header": "x-request-id", "value": "attempt-2"},
                    ]
                else:
                    assert record["outcome"] != "served"
                assert gateway.correlation.recovery_snapshot("turn_lifecycle") == []
    asyncio.run(run())


@pytest.mark.parametrize("error,error_type,message", [
    (None, None, OVERLOAD),
    ("server_error", None, "An internal error occurred."),
    ("server_error", None, OVERLOAD + " extra"),
    ("server_error", None, OVERLOAD.replace("currently overloaded", "currently  overloaded")),
    ("permission_error", None, OVERLOAD),
    ("overloaded_error", None, OVERLOAD),
    ("different_vendor_error", "server_error", OVERLOAD),
    ("internal_error", "server_error", OVERLOAD),
])
def test_other_response_endings_are_lossless_and_never_replayed(tmp_path, error, error_type, message):
    """MH-RETRY-OVERLOAD-001: exact typed envelope, not text/code heuristics."""
    async def run():
        service, _clock, models = configured_service(tmp_path)
        body = output("single-attempt") + ending(error=error, message=message, error_type=error_type)
        calls = 0

        async def respond(request):
            nonlocal calls
            calls += 1
            assert calls == 1
            return web.Response(body=body, content_type="text/event-stream")

        async with loopback_engine(tmp_path, service, respond):
            async with gateway_client(service, "codex", models["codex"]) as (_, client, url, headers):
                async with client.post(url, headers=headers, json={"model": "shared-model", "stream": True}) as response:
                    assert response.status == 200 and await response.read() == body
                if error == "server_error":
                    assert service.store.load().sources[0].state.status == "cooldown"
        assert calls == 1
    asyncio.run(run())

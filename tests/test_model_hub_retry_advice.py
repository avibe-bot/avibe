"""Retry advice crosses the adapter boundary without adding retry ownership."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from aiohttp import web
import pytest

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.retry import retry_after_deadline
from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from vibe.model_hub_runtime import client as client_module
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import SourceRecord


WIRE = {
    "anthropic": (
        "/v1/messages",
        b'{"type":"message","content":[{"type":"text","text":"ok"}]}',
        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
        b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        b'event: error\ndata: {"type":"error","error":{"type":"api_error"}}\n\n',
    ),
    "openai_responses": (
        "/v1/responses",
        b'{"output":[{"content":[{"type":"output_text","text":"ok"}]}]}',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
        b'event: response.failed\ndata: {"type":"response.failed","response":'
        b'{"error":{"type":"server_error"}}}\n\n',
    ),
    "openai_chat": (
        "/v1/chat/completions",
        b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}',
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
        b'data: {"error":{"type":"server_error"}}\n\n',
    ),
}
ERROR_BODY = (
    b'{"error":{"type":"server_error","message":"private body detail","retry_after":"999"}}'
)


def _source(protocol: str) -> SourceRecord:
    return SourceRecord(
        source_id="src_retryadvice",
        vendor="custom",
        protocol=protocol,
        base_url="https://unused.example.test",
        credential_ref="cred_retryadvice",
        allowed_origins=(),
        model_ids=("model-a",),
        prefix="retry-advice",
    )


@asynccontextmanager
async def _engine(respond, *, timeout: float = 2):
    requests = []

    async def handle(request: web.Request) -> web.StreamResponse:
        requests.append((request.path, await request.json()))
        return await respond(request)

    app = web.Application()
    app.router.add_post("/{path:.*}", handle)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        client = EngineClient(
            EngineConnection(f"http://127.0.0.1:{port}", "fixture-management", "fixture-gateway"),
            timeout=timeout,
        )
        yield client, requests
    finally:
        await runner.cleanup()


async def _invoke(client: EngineClient, protocol: str, stream: bool):
    cleanups = []
    handle = await client.invoke(
        _source(protocol),
        "model-a",
        {},
        stream=stream,
        on_transport_done=lambda: cleanups.append(True),
    )
    try:
        body = b"" if handle.stream is None else b"".join([chunk async for chunk in handle.stream])
        outcome = await handle.outcome()
    finally:
        await handle.close_stream()
    assert cleanups
    return outcome, body


@pytest.mark.parametrize("optional_count", [0, 3])
def test_existing_outcome_constructors_keep_retry_advice_optional(optional_count: int) -> None:
    usage = ProtocolUsageReport.of(input_tokens=12, cached_input_tokens=0, output_tokens=3)
    optional = ("server_error", ("server_error",), usage)
    outcome = RawCallOutcome(
        RawOutcomeKind.HTTP_ERROR, 503, None, None, False, "model-a", "src_retryadvice",
        *optional[:optional_count],
    )
    assert outcome.retry_after is None
    assert outcome.response_received_at is None
    assert outcome.usage == (usage if optional_count else None)


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
async def test_failed_response_carries_only_allowlisted_correlation_ids(protocol: str, stream: bool) -> None:
    async def respond(_request: web.Request) -> web.Response:
        return web.Response(
            status=400,
            body=ERROR_BODY,
            headers={
                "X-Request-Id": "relay-request-1",
                "Request-Id": "provider-request-1",
                "Set-Cookie": "session=secret",
                "Organization-Id": "org-private",
            },
            content_type="application/json",
        )

    async with _engine(respond) as (client, _requests):
        outcome, body = await _invoke(client, protocol, stream)

    assert outcome.kind is RawOutcomeKind.HTTP_ERROR
    assert outcome.response_correlation_ids == (
        ("request-id", "provider-request-1"),
        ("x-request-id", "relay-request-1"),
    )
    assert body == b""


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
async def test_success_response_carries_its_own_correlation_ids(protocol: str, stream: bool) -> None:
    _path, buffered, streamed, _stream_error = WIRE[protocol]

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            body=streamed if stream else buffered,
            headers={"X-Request-Id": "served-request-1"},
            content_type="text/event-stream" if stream else "application/json",
        )

    async with _engine(respond) as (client, _requests):
        outcome, _body = await _invoke(client, protocol, stream)

    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert outcome.response_correlation_ids == (("x-request-id", "served-request-1"),)

@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
@pytest.mark.parametrize(
    ("advice", "expected"),
    [
        pytest.param("60", "60", id="delay-seconds"),
        pytest.param("0", "0", id="zero"),
        pytest.param("Wed, 09 Sep 2026 04:20:00 GMT", "Wed, 09 Sep 2026 04:20:00 GMT", id="http-date"),
        pytest.param("Sun, 06 Nov 1994 08:49:37 GMT", "Sun, 06 Nov 1994 08:49:37 GMT", id="past-date"),
        pytest.param("-1", "-1", id="negative-left-to-policy"),
        pytest.param("1.5", "1.5", id="fraction-left-to-policy"),
        pytest.param("not-a-date", "not-a-date", id="malformed-left-to-policy"),
        pytest.param("9" * 128, "9" * 128, id="overflow-left-to-policy"),
        pytest.param("9" * 129, None, id="oversized-dropped-not-truncated"),
        pytest.param("60" + "x" * 127, None, id="oversized-valid-prefix"),
        pytest.param("１２", None, id="nonascii-digits"),
        pytest.param("60秒", None, id="nonascii-suffix"),
        pytest.param("", "", id="empty-left-to-policy"),
        pytest.param(None, None, id="missing"),
    ],
)
async def test_failed_http_response_carries_only_bounded_ascii_advice(
    protocol: str, stream: bool, advice: str | None, expected: str | None,
) -> None:
    async def respond(_request: web.Request) -> web.Response:
        headers = {
            "Authorization": "private response credential",
            "X-Retry-After": "888",
            "X-Private-Detail": "private header detail",
        }
        if advice is not None:
            headers["rEtRy-AfTeR"] = advice
        return web.Response(status=503, body=ERROR_BODY, headers=headers, content_type="application/json")

    async with _engine(respond) as (client, requests):
        before = datetime.now(timezone.utc)
        outcome, body = await _invoke(client, protocol, stream)
        after = datetime.now(timezone.utc)

    assert outcome.kind is RawOutcomeKind.HTTP_ERROR
    assert outcome.http_status == 503
    assert outcome.stream_started is False
    assert outcome.retry_after == expected
    assert outcome.response_received_at is not None
    assert outcome.response_received_at.tzinfo is timezone.utc
    assert before <= outcome.response_received_at <= after
    assert outcome.error_type == "server_error"
    assert outcome.error_candidates == ("server_error",)
    assert "private" not in repr(outcome)
    assert body == b""
    assert requests == [(WIRE[protocol][0], {"model": "retry-advice/model-a", "stream": stream})]


@pytest.mark.parametrize(("status", "error_type"), [(429, "rate_limit_error"), (503, "server_error")])
async def test_only_a_usage_cap_turns_its_body_reset_into_advice(status: int, error_type: str) -> None:
    """Reset fields in any other error body are not retry advice and cannot set a cooldown."""
    body = {"error": {"type": error_type, "resets_at": 4102444800, "resets_in_seconds": 600}}

    async def respond(_request: web.Request) -> web.Response:
        return web.json_response(body, status=status)

    async with _engine(respond) as (client, _requests):
        outcome, _body = await _invoke(client, "openai_responses", False)

    assert (outcome.http_status, outcome.error_type) == (status, error_type)
    assert outcome.retry_after is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("1500", 1500, id="milliseconds"),
        pytest.param("0", 0, id="zero"),
        pytest.param("-1", None, id="negative"),
        pytest.param("1.5", None, id="fraction"),
        pytest.param("１２", None, id="nonascii"),
        pytest.param("9" * 129, None, id="oversized"),
        pytest.param("9" * 128, None, id="overflow"),
        pytest.param("", None, id="empty"),
    ],
)
async def test_retry_after_ms_is_validated_without_becoming_retry_after_seconds(raw: str, expected: int | None) -> None:
    async def respond(_request: web.Request) -> web.Response:
        return web.Response(
            status=429,
            body=ERROR_BODY,
            headers={"rEtRy-AfTeR-Ms": raw},
            content_type="application/json",
        )

    async with _engine(respond) as (client, _requests):
        outcome, _body = await _invoke(client, "anthropic", False)

    assert outcome.retry_after is None
    assert outcome.retry_after_ms == expected
    if expected is not None:
        assert outcome.response_received_at is not None
        deadline = retry_after_deadline(
            None, outcome.response_received_at, milliseconds=expected,
        )
        assert deadline is not None
        assert (deadline - outcome.response_received_at).total_seconds() == expected / 1000


async def test_retry_after_and_retry_after_ms_use_the_later_valid_deadline() -> None:
    async def respond(_request: web.Request) -> web.Response:
        return web.Response(
            status=429,
            body=ERROR_BODY,
            headers={"Retry-After": "1", "Retry-After-Ms": "2500"},
            content_type="application/json",
        )

    async with _engine(respond) as (client, _requests):
        outcome, _body = await _invoke(client, "anthropic", False)

    assert outcome.retry_after == "1"
    assert outcome.retry_after_ms == 2500
    assert outcome.response_received_at is not None
    deadline = retry_after_deadline(
        outcome.retry_after,
        outcome.response_received_at,
        milliseconds=outcome.retry_after_ms,
    )
    assert deadline is not None
    assert (deadline - outcome.response_received_at).total_seconds() == 2.5


async def test_failed_claude_response_carries_only_bounded_unified_headers() -> None:
    async def respond(_request: web.Request) -> web.Response:
        return web.Response(
            status=429,
            body=ERROR_BODY,
            headers={
                "Anthropic-Ratelimit-Unified-Status": "rejected",
                "Anthropic-Ratelimit-Unified-5h-Reset": "1797000000",
                "Anthropic-Ratelimit-Unified-Representative-Claim": "5h",
                "Anthropic-Ratelimit-Unified-7d-Status": "bad",
                "Anthropic-Ratelimit-Unified-Unknown": "private",
                "Set-Cookie": "session=secret",
            },
            content_type="application/json",
        )

    async with _engine(respond) as (client, _requests):
        outcome, _body = await _invoke(client, "anthropic", False)

    assert outcome.claude_rate_limit_headers == (
        ("anthropic-ratelimit-unified-5h-reset", "1797000000"),
        ("anthropic-ratelimit-unified-representative-claim", "5h"),
        ("anthropic-ratelimit-unified-status", "rejected"),
    )
    assert "Set-Cookie" not in repr(outcome)


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
@pytest.mark.parametrize("body_mode", ["slow", "timeout", "disconnect", "malformed"])
async def test_retry_advice_survives_error_body_delays_and_failures(
    monkeypatch: pytest.MonkeyPatch, protocol: str, stream: bool, body_mode: str,
) -> None:
    """MH-RETRY-ADVICE-001: advice keeps the header receipt clock across body failures."""
    receipt = datetime(2026, 9, 9, 4, 15, tzinfo=timezone.utc)
    current_time = receipt
    clock_samples = []
    sampled = asyncio.Event()
    release_body = asyncio.Event()

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            clock_samples.append(current_time)
            sampled.set()
            return current_time

    monkeypatch.setattr(client_module, "datetime", Clock)

    async def respond(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=429,
            headers={
                "Retry-After": "60",
                "Content-Type": "application/json",
                "Content-Length": str(len(ERROR_BODY)),
            },
        )
        await response.prepare(request)
        await release_body.wait()
        if body_mode == "timeout":
            return response
        if body_mode == "disconnect":
            await response.write(ERROR_BODY[:5])
            assert request.transport is not None
            request.transport.close()
        else:
            await response.write(b"x" * len(ERROR_BODY) if body_mode == "malformed" else ERROR_BODY)
            await response.write_eof()
        return response

    async with _engine(respond, timeout=0.25 if body_mode == "timeout" else 2) as (client, requests):
        pending = asyncio.create_task(_invoke(client, protocol, stream))
        try:
            # The server cannot finish its body until the receipt clock is sampled.
            await asyncio.wait_for(sampled.wait(), timeout=2)
            assert not pending.done()
            current_time += timedelta(minutes=5)
            if body_mode != "timeout":
                release_body.set()
            outcome, body = await asyncio.wait_for(pending, timeout=3)
        finally:
            release_body.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert clock_samples == [receipt]
    assert outcome.response_received_at == receipt
    assert outcome.retry_after == "60"
    assert outcome.kind is RawOutcomeKind.HTTP_ERROR
    assert outcome.http_status == 429
    assert outcome.stream_started is False
    assert outcome.error_type == ("server_error" if body_mode == "slow" else None)
    assert body == b""
    assert len(requests) == 1


@pytest.mark.parametrize("protocol", WIRE)
@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streaming"])
@pytest.mark.parametrize(
    ("body_mode", "expected_kind"),
    [
        ("success", RawOutcomeKind.SUCCESS),
        ("shaped_error", RawOutcomeKind.HTTP_ERROR),
        ("empty", RawOutcomeKind.NETWORK_ERROR),
    ],
)
async def test_http_200_never_supplies_retry_advice(
    protocol: str, stream: bool, body_mode: str, expected_kind: RawOutcomeKind,
) -> None:
    _, buffered, streamed, stream_error = WIRE[protocol]
    payload = {
        "success": streamed if stream else buffered,
        "shaped_error": stream_error if stream else ERROR_BODY,
        "empty": b"",
    }[body_mode]

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(
            body=payload,
            headers={"Retry-After": "60"},
            content_type="text/event-stream" if stream else "application/json",
        )

    async with _engine(respond) as (client, requests):
        outcome, body = await _invoke(client, protocol, stream)

    assert outcome.kind is expected_kind
    assert outcome.http_status == 200
    assert outcome.retry_after is None
    assert outcome.response_received_at is None
    assert body == (payload if body_mode == "success" else b"")
    assert len(requests) == 1


async def test_connection_failure_has_no_response_receipt_or_retry_advice() -> None:
    # Reserve a local port without listening: no real service can own this target.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        client = EngineClient(
            EngineConnection(f"http://127.0.0.1:{port}", "fixture-management", "fixture-gateway"),
        )
        outcome, body = await _invoke(client, "openai_responses", False)

    assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
    assert outcome.http_status is None
    assert outcome.retry_after is None
    assert outcome.response_received_at is None
    assert body == b""


@pytest.mark.parametrize("protocol", WIRE)
async def test_local_registration_failure_keeps_its_identity_with_http_receipt(protocol: str) -> None:
    message = "unknown provider for model retry-advice/model-a"
    # CLIProxyAPI's local routing rejection, rebuilt as a Claude error on /v1/messages.
    body = (
        {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
        if protocol == "anthropic"
        else {"error": {"message": message, "type": "invalid_request_error", "code": "model_not_found", "param": "model"}}
    )

    async def respond(_request: web.Request) -> web.Response:
        return web.json_response(body, status=400, headers={"Retry-After": "60"})

    async with _engine(respond) as (client, requests):
        outcome, body = await _invoke(client, protocol, False)

    assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
    assert outcome.error_code == "engine_down"
    assert outcome.http_status == 400
    assert outcome.retry_after == "60"
    assert outcome.response_received_at is not None
    assert outcome.response_received_at.tzinfo is timezone.utc
    assert body == b""
    assert len(requests) == 1

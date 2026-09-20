"""Real loopback ingress for the authenticated Model Hub request byte budget."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
import pytest

from config import paths
from core.handlers.model_hub import turn_gateway
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.run_settlement import SETTLED_BY_STOPPED, SETTLED_BY_TERMINAL_RESULT
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    ScenarioCallResult,
    config_with_sources,
    fixed_model,
    service_for,
    source,
)
from vibe.i18n import t


MIB = 1024 * 1024
MODEL = "fixture-exact-model"
TURN = "turn_request_budget"
TEXT = "请检查这批合成图片的内容吧"
WIRE_CASES = (
    ("claude", "messages", "anthropic"),
    ("codex", "responses", "openai_responses"),
    ("opencode", "chat/completions", "openai_chat"),
)


@pytest.mark.parametrize(("version", "expected_limit"), [
    ("3.8.0", 4097),
    ("3.13.3", 4097),
    ("3.13.5", 4097),
    ("3.14.0", 4096),
    ("3.14.3", 4096),
])
async def test_aiohttp_request_limit_compatibility(tmp_path, monkeypatch, version, expected_limit):
    """Map the inclusive budget to each aiohttp reader's comparison semantics."""
    monkeypatch.setattr(aiohttp, "__version__", version)
    monkeypatch.setattr(turn_gateway, "_MAX_REQUEST_BYTES", 4096)
    paths.get_state_dir().relative_to(tmp_path)
    gateway = turn_gateway.ModelHubTurnGateway(SimpleNamespace())
    try:
        await gateway._ensure_started()
        assert gateway._runner.app._client_max_size == expected_limit
    finally:
        await gateway.close()


@pytest.fixture
def budget():
    return None


@pytest.fixture(params=WIRE_CASES, ids=["messages", "responses", "chat"])
async def ingress(request, tmp_path, monkeypatch, budget):
    if budget is not None:
        monkeypatch.setattr(turn_gateway, "_MAX_REQUEST_BYTES", budget)
    backend, endpoint, protocol = request.param
    first = source("src_budget001", [MODEL], vendor="custom", protocol=protocol)
    second = source("src_budget002", [MODEL], vendor="custom", protocol=protocol)
    menu_model = MODEL if backend == "opencode" else fixed_model(backend)
    store = MemoryModelHubStore(config_with_sources(
        [first, second], backend=backend, menu_model=menu_model,
        hops=[(first.id, MODEL), (second.id, MODEL)],
    ))
    adapter = ModelHubScenarioAdapter(invoke_results=[
        ScenarioCallResult(RawOutcomeKind.SUCCESS, status=200),
    ])
    service = service_for(tmp_path, store, adapter)
    gateway = turn_gateway.ModelHubTurnGateway(service)
    base_url, token = await gateway.endpoint(
        backend,
        process_scope="fixture-budget",
        turn_id=TURN,
        requested_model_id=menu_model,
        resolved_model_id=MODEL,
        source_id=first.id,
    )
    # Both explicitly supplied stores and implicit service-owned state are private.
    paths.get_state_dir().relative_to(tmp_path)
    before = store.load().to_payload()
    try:
        yield SimpleNamespace(
            gateway=gateway, service=service, store=store, adapter=adapter,
            before=before, backend=backend, protocol=protocol,
            url=f"{base_url}/v1/{endpoint}", token=token, first=first,
            menu_model=menu_model,
        )
    finally:
        await gateway.close()


def _payload(protocol: str, *, image_chars: int = 0) -> dict:
    text_type = "input_text" if protocol == "openai_responses" else "text"
    content = [{"type": text_type, "text": TEXT}]
    for index in range(20 if image_chars else 0):
        # Synthetic base64-shaped content, not images or history from a user.
        data = "iVBORw0KGgo" + chr(ord("A") + index) * image_chars + "="
        if protocol == "anthropic":
            image = {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": data,
            }}
        elif protocol == "openai_responses":
            image = {"type": "input_image", "image_url": f"data:image/png;base64,{data}"}
        else:
            image = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}
        content.append(image)
    field = "input" if protocol == "openai_responses" else "messages"
    return {"model": MODEL, field: [{"role": "user", "content": content}], "stream": False}


def _encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _chunks(body: bytes):
    # Split a multibyte character deliberately; framing must not change byte semantics.
    split = body.find(TEXT.encode("utf-8"))
    split = split + 1 if split >= 0 else 1
    yield body[:split]
    for offset in range(split, len(body), 64 * 1024):
        yield body[offset:offset + 64 * 1024]


async def _post(ingress, body: bytes, framing: str, *, token: str | None = None):
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            ingress.url,
            data=_chunks(body) if framing == "chunked" else body,
            headers={
                "Authorization": f"Bearer {ingress.token if token is None else token}",
                "Content-Type": "application/json; charset=utf-8",
                "anthropic-version": "2023-06-01",
                "openai-beta": "fixture",
            },
        ) as response:
            headers = response.request_info.headers
            if framing == "chunked":
                assert headers["Transfer-Encoding"] == "chunked"
                assert "Content-Length" not in headers
            else:
                assert int(headers["Content-Length"]) == len(body)
                assert "Transfer-Encoding" not in headers
            return response.status, await response.json(), response.headers


def _assert_not_admitted(ingress) -> None:
    assert ingress.adapter.invocations == []
    assert ingress.adapter.requests == []
    assert ingress.adapter.synced == []
    assert len(ingress.adapter.invoke_results) == 1
    assert ingress.store.saved_payloads == []
    assert ingress.store.load().to_payload() == ingress.before
    assert ingress.service.events.list() == []
    assert ingress.service.usage.window(days=1, now=ingress.service.now()) == []
    assert ingress.gateway.resource_leak_records == ()


def _settle(ingress, *, settled_by=SETTLED_BY_TERMINAL_RESULT):
    ingress.gateway.correlation.settle(TURN, settled_by=settled_by)
    return ingress.service.provenance.get(TURN)


def _assert_local_failure(ingress) -> None:
    _assert_not_admitted(ingress)
    if ingress.backend == "opencode":
        # Shared OpenCode processes intentionally cannot attribute one HTTP call.
        assert TURN not in ingress.gateway.correlation._traces
        assert _settle(ingress) is None
        return
    projection = ingress.gateway.correlation._traces[TURN].terminal_outcome
    assert projection.outcome == "failed_terminal"
    assert projection.discriminator == "request_nonfallback"
    record = _settle(ingress)
    assert record["outcome"] == "failed_terminal"
    assert record["requested_model_id"] == ingress.menu_model
    assert record["terminal_error"] == {
        "source_id": ingress.first.id,
        "configured_model_id": MODEL,
        "channel": "hub",
        "reason": "invalid_parameter",
        "stream_started": False,
    }
    assert record["served"] is None
    assert record["failed_attempts"] == []


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
async def test_large_multi_image_request_is_forwarded_exactly(ingress, framing):
    """MH-REQUEST-BUDGET-001: ~42 MiB of synthetic images survive real HTTP ingress."""
    assert turn_gateway._MAX_REQUEST_BYTES == 128 * MIB
    assert len(TEXT.encode("utf-8")) == 39
    payload = _payload(ingress.protocol, image_chars=42 * MIB // 20 // 4 * 4)
    body = _encode(payload)
    assert 42 * MIB <= len(body) < 43 * MIB
    status, _result, _headers = await _post(ingress, body, framing)
    assert status == 200
    assert ingress.adapter.invocations == [(ingress.first.id, MODEL, ingress.backend)]
    forwarded, = ingress.adapter.requests
    assert forwarded == payload
    assert _encode(forwarded) == body
    assert forwarded.protocol == ingress.protocol
    assert forwarded.headers == {"anthropic-version": "2023-06-01", "openai-beta": "fixture"}
    record = _settle(ingress)
    if ingress.backend == "opencode":
        assert record is None
    else:
        assert record["outcome"] == "served"
        assert record["served"]["configured_model_id"] == MODEL


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
@pytest.mark.parametrize("delta", [-1, 0, 1], ids=["below", "equal", "over"])
@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("budget", [4096])
async def test_request_budget_is_inclusive_and_local(ingress, caplog, framing, delta, language):
    """MH-REQUEST-BUDGET-002: equal bytes pass; +1 is a local 413 without admission."""
    ingress.gateway._language_provider = lambda: language
    payload = _payload(ingress.protocol)
    encoded = _encode(payload)
    body = encoded + b" " * (4096 + delta - len(encoded))
    assert len(body) == 4096 + delta
    assert len(body.decode("utf-8")) < 4096
    status, result, headers = await _post(ingress, body, framing)
    if delta <= 0:
        assert status == 200
        assert ingress.adapter.requests == [payload]
        assert ingress.adapter.requests[0].protocol == ingress.protocol
        return
    assert status == 413
    assert result == {"error": {
        "type": "request_too_large",
        "code": "request_too_large",
        "message": t("modelHub.errors.request_too_large", language, limit_mib="0.00390625"),
    }}
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Retry-After" not in headers
    assert TEXT not in json.dumps(result, ensure_ascii=False) + caplog.text
    assert ingress.token not in json.dumps(result) + caplog.text
    _assert_local_failure(ingress)


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
@pytest.mark.parametrize("body", [b"{", b"x" * 4097], ids=["malformed", "oversized"])
@pytest.mark.parametrize("budget", [4096])
async def test_bad_auth_does_not_parse_or_admit(ingress, monkeypatch, framing, body):
    """MH-REQUEST-BUDGET-003: invalid credentials never reach JSON parsing or admission."""
    async def unexpected_json(*args, **kwargs):
        pytest.fail("an unauthenticated request reached JSON parsing")

    monkeypatch.setattr(web.Request, "json", unexpected_json)
    status, result, _headers = await _post(ingress, body, framing, token="invalid-fixture-token")
    assert status == 401
    assert result["error"]["code"] == "authentication_error"
    _assert_not_admitted(ingress)


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
@pytest.mark.parametrize("body", [b"{", b'{"model":"\xff"}', b"[]"], ids=["json", "utf8", "array"])
async def test_malformed_json_is_a_correlated_local_400(ingress, framing, body):
    """Malformed JSON remains distinct from the local size boundary."""
    status, result, _headers = await _post(ingress, body, framing)
    assert status == 400
    assert result["error"]["code"] == "invalid_request_error"
    assert result["error"]["message"] == t("modelHub.launch.request_incompatible", "en")
    _assert_local_failure(ingress)


async def _open_partial_upload(ingress, framing: str, body: bytes, *, token: str | None = None):
    url = urlsplit(ingress.url)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    framing_header = "Transfer-Encoding: chunked" if framing == "chunked" else "Content-Length: 8192"
    headers = (
        f"POST {url.path} HTTP/1.1\r\n"
        f"Host: {url.netloc}\r\n"
        f"Authorization: Bearer {ingress.token if token is None else token}\r\n"
        f"{framing_header}\r\nContent-Type: application/json\r\n\r\n"
    ).encode("ascii")
    # Neither framing completes: the server must respond or cancel mid-upload.
    prefix = f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n" if framing == "chunked" else body
    writer.write(headers + prefix)
    await writer.drain()
    return reader, writer


async def _read_json_response(reader):
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:-2])
    body = await asyncio.wait_for(reader.readexactly(int(headers[b"Content-Length"])), timeout=2)
    return status, json.loads(body)


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
@pytest.mark.parametrize("budget", [4096])
async def test_oversize_rejection_does_not_wait_for_upload_eof(ingress, framing):
    """MH-REQUEST-BUDGET-002: exceeding the budget rejects an unfinished upload."""
    reader, writer = await _open_partial_upload(ingress, framing, b" " * 4097)
    try:
        status, result = await _read_json_response(reader)
        assert status == 413
        assert result["error"]["code"] == "request_too_large"
        _assert_local_failure(ingress)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
@pytest.mark.parametrize(
    ("ingress", "ending"),
    [(case, ending) for case in WIRE_CASES for ending in ("disconnect", "stop")
     if case[0] != "opencode" or ending == "disconnect"],
    indirect=["ingress"],
)
async def test_partial_upload_cancellation_releases_turn_without_admission(ingress, monkeypatch, framing, ending):
    """Cancellation during the real body read leaves no Source or usage facts."""
    entered = asyncio.Event()
    left = asyncio.Event()
    original_json = web.Request.json

    async def observe_read(request, **kwargs):
        entered.set()
        try:
            return await original_json(request, **kwargs)
        finally:
            left.set()

    monkeypatch.setattr(web.Request, "json", observe_read)
    _reader, writer = await _open_partial_upload(ingress, framing, b'{"model":')
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not left.is_set()
        if ending == "disconnect":
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(left.wait(), timeout=2)
            await ingress.gateway._drain_turn_requests(TURN)
            record = _settle(ingress, settled_by=SETTLED_BY_STOPPED)
        else:
            completion = ingress.gateway.finalize_turn(
                TURN,
                settled_by=SETTLED_BY_STOPPED,
                finish=lambda: _settle(ingress, settled_by=SETTLED_BY_STOPPED),
            )
            assert completion is not None
            await asyncio.wait_for(completion, timeout=2)
            assert left.is_set()
            record = ingress.service.provenance.get(TURN)
        _assert_not_admitted(ingress)
        assert ingress.gateway._turn_requests == {}
        if ingress.backend == "opencode":
            assert record is None
        else:
            assert record["outcome"] == "canceled"
            assert record["terminal_error"] is None
            # Stop freezes the prepared identity before teardown; a disconnect
            # clears it before settlement. Neither represents adapter admission.
            assert record["canceled_attempt"] == ({
                "source_id": ingress.first.id,
                "configured_model_id": MODEL,
                "channel": "hub",
            } if ending == "stop" else None)
    finally:
        writer.close()
        await writer.wait_closed()

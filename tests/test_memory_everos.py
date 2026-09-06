from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from avibe_memory import artifact as memory_artifact
from avibe_memory.everos import (
    PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS,
    _AGENTIC_ROUND_HEADER,
    _AGENTIC_TIMEOUT_HEADER,
    _ATTACHMENT_ADD_REJECTION_CODES_VALIDATED_EVEROS_VERSION,
    _PREFLIGHT_TIMEOUT_SECONDS,
    _chat_probe_response_issue,
    AddAck,
    AddRejected,
    AgenticRecallTelemetry,
    EverOSPort,
    FlushRejected,
    FlushRetryable,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderSystemFailure,
    ProviderAttachment,
    ProviderCapture,
    attachment_add_rejection_proves_no_write,
)
from avibe_memory.store import (
    MemoryStore,
    _provider_session_ref,
    derive_assistant_memory_owner_id,
)
from avibe_memory.types import (
    MemoryListItem,
    MemoryListPage,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    ProviderSessionRef,
)


PROJECT = "default"
PRINCIPAL = "owner-1"
SESSION_REF = ProviderSessionRef(
    principal_id=PRINCIPAL,
    epoch=7,
    project_ref=PROJECT,
    session_id="src--one--e1",
)
WIRE_SESSION_ID = "src--one--e1"


def test_attachment_rejection_no_write_proof_matches_pinned_everos_version() -> None:
    assert (
        _ATTACHMENT_ADD_REJECTION_CODES_VALIDATED_EVEROS_VERSION
        == memory_artifact.EVEROS_VERSION
    ), (
        "Revalidate that UNSUPPORTED_FORMAT and CAPABILITY_UNAVAILABLE still occur "
        "before every durable /add write in the newly pinned EverOS version"
    )


def _sidecar_transport(handler):
    return patch("avibe_memory.everos.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handler))


@pytest.mark.parametrize("language", ["en", "zh"])
def test_sender_name_crosses_automatic_and_explicit_capture_http_boundary(tmp_path, language) -> None:
    """MEMORY-SEARCH-019: accepted source identity survives named provider capture."""
    from types import SimpleNamespace

    from avibe_memory.capture_adapter import EnabledMemoryAdapter
    from avibe_memory.module import MIN_FREE_DISK_BYTES, MemoryModule
    from avibe_memory.types import CaptureRequest
    from avibe_memory.sidecar import _request_rejection
    from core.controller import Controller
    from core.handlers.message_handler import memory_turn_event
    from modules.im.base import MessageContext

    requests = []
    text = "原文\n`u-synthetic` https://example.invalid/u-synthetic"

    def handler(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/add"):
            assert _request_rejection(request.method, request.url.path, request.content) is None
            requests.append(payload)
        return httpx.Response(200, json={"request_id": "synthetic", "data": {"status": "accumulated"}})

    async def acquire(_session_id):
        return SimpleNamespace(release=lambda: None)

    async def run():
        store = MemoryStore(tmp_path / "state/memory/memory.sqlite", effective_home=tmp_path)
        module = MemoryModule(
            store, EverOSPort(tmp_path / "synthetic.sock"), enabled=True,
            disk_free_bytes=lambda: MIN_FREE_DISK_BYTES, effective_home=tmp_path,
        )
        controller = Controller.__new__(Controller)
        controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True), language=language)
        controller.memory_runtime = SimpleNamespace(available=True, module=module)
        bound_users = SimpleNamespace(
            maybe_reload=lambda: None,
            get_user=lambda *_args, **_kwargs: SimpleNamespace(enabled=True, display_name="小王 Élodie 🌱"),
        )
        controller.platform_settings_managers = {"slack": SimpleNamespace(get_store=lambda: bound_users)}
        adapter = EnabledMemoryAdapter(
            module=module, principals=store, is_enabled_user=lambda *_args: True,
            lifecycle_snapshot_matches=lambda *_args: True,
            acquire_lifecycle_admission=acquire,
            attachment_capture_status=lambda: asyncio.sleep(0, result="ready"),
            attachment_config_generation=lambda: 1,
        )
        adapter.start(task_factory=asyncio.create_task)
        expected_owners = []
        try:
            sources = [("avibe", source) for source in ("local", "remote:owner", "remote:other")]
            sources.extend([("slack", "user-1"), ("slack", "user-2")])
            for platform, source in sources:
                context = MessageContext(
                    user_id=source, channel_id="shared-project-session", platform=platform,
                    message_id=source,
                    platform_specific={"author_id": source, "author_name": "Untrusted", "is_dm": True},
                    is_original_human_text=True,
                )
                event = memory_turn_event(
                    context, text, "shared-project-session", 1,
                    sender_name=await controller.memory_sender_name_for_context(context),
                )
                adapter.offer(event)
                expected_owners.append(store.principal_for_user_key(f"{platform}:{source}"))
            await adapter.wait_idle_for_tests()
            principal = expected_owners[0]
            await controller.capture_memory(CaptureRequest(
                source_message_id="explicit", session_id="shared-project-session",
                principal_id=principal, project_id="default", provenance="agent",
                text=text, occurred_at_ms=1_725_000_001_234,
            ))
            await module.wait_writer_idle_for_tests()
            messages = [payload["messages"][0] for payload in requests]
            web_name = "用户" if language == "zh" else "User"
            names = [web_name, web_name, web_name, "小王 Élodie 🌱", "小王 Élodie 🌱", "Agent"]
            assert {message["sender_id"]: message["sender_name"] for message in messages} == dict(
                zip([*expected_owners, principal + "-agent"], names)
            )
            assert len(messages) == len({message["sender_id"] for message in messages}) == 6
            assert all(message["content"] == text and message["role"] == "user" for message in messages)
            assert all(isinstance(message["timestamp"], int) for message in messages)
        finally:
            await adapter.cancel_memory_capture_tasks()
            await module.close_writer()

    with _sidecar_transport(handler):
        asyncio.run(run())


@pytest.mark.parametrize("name", [None, "", "小王 Élodie 🌱"])
def test_sender_name_is_optional_in_provider_payload(name) -> None:
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"request_id": "synthetic", "data": {"status": "accumulated"}})

    with _sidecar_transport(handler):
        asyncio.run(EverOSPort(Path("/tmp/synthetic.sock")).add(ProviderCapture(
            session_ref=SESSION_REF, text="原文 stays", provider_timestamp_ms=1_725_000_001_234,
            sender_name=name,
        )))
    message = requests[0]["messages"][0]
    assert message == {
        "sender_id": PRINCIPAL, "role": "user", "timestamp": 1_725_000_001_234,
        "content": "原文 stays", **({"sender_name": name} if name is not None else {}),
    }


class _FailingResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        failure_type: type[httpx.TransportError],
        request: httpx.Request,
    ) -> None:
        self._failure_type = failure_type
        self._request = request

    async def __aiter__(self):
        raise self._failure_type("response body lost", request=self._request)
        yield b""  # pragma: no cover - keeps this an async generator


def _thinking_chat_completion(*, role: str = "assistant", finish_reason: str | None = "length") -> dict:
    message = {"role": role}
    choice: dict = {"finish_reason": finish_reason, "index": 0, "message": message}
    if finish_reason is None:
        del choice["finish_reason"]
    return {
        "choices": [choice],
        "usage": {"completion_tokens": 0, "prompt_tokens": 1, "total_tokens": 1},
    }


def _health_envelope() -> dict:
    return {
        "status": "ok",
        "version": "1.2.3",
        "capabilities": {
            "llm": True,
            "embed": True,
            "rerank": True,
            "multimodal_llm": True,
            "parser": True,
        },
        "disabled_features": [],
        "cascade": None,
    }


@pytest.mark.parametrize(
    ("rerank", "disabled_features"),
    [(False, ["agentic_search"]), (True, [])],
)
def test_health_accepts_additive_typed_capabilities_and_surfaces_rerank_state(
    rerank: bool,
    disabled_features: list[str],
) -> None:
    payload = _health_envelope()
    payload["capabilities"]["future_capability"] = False
    payload["capabilities"]["rerank"] = rerank
    payload["disabled_features"] = disabled_features

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run():
        return await EverOSPort(Path("/tmp/everos.sock")).health_snapshot()

    with _sidecar_transport(handler):
        snapshot = asyncio.run(run())

    assert snapshot.capabilities["rerank"] is rerank
    assert snapshot.capabilities["future_capability"] is False
    assert snapshot.disabled_features == tuple(disabled_features)


def test_health_rejects_a_truncated_core_capability_set() -> None:
    payload = _health_envelope()
    del payload["capabilities"]["parser"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async def run():
        return await EverOSPort(Path("/tmp/everos.sock")).health_snapshot()

    with _sidecar_transport(handler):
        with pytest.raises(MemoryProviderFailure) as raised:
            asyncio.run(run())

    assert raised.value.error == "memory_provider_response_invalid"


def test_add_and_flush_are_separate_and_parse_provider_envelopes() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/add"):
            return httpx.Response(
                200,
                json={"request_id": "add-request", "data": {"status": "accumulated"}},
            )
        return httpx.Response(
            200,
            json={"request_id": "flush-request", "data": {"status": "extracted"}},
        )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        ack = await provider.add(
            ProviderCapture(
                session_ref=SESSION_REF,
                text="remember this",
                provider_timestamp_ms=1_725_000_001_234,
            )
        )
        flushed = await provider.flush(SESSION_REF)
        return ack, flushed

    with _sidecar_transport(handler):
        ack, flushed = asyncio.run(run())

    assert ack == AddAck(request_id="add-request", status="accumulated")
    assert flushed == FlushSucceeded(request_id="flush-request", status="extracted")
    assert SESSION_REF.session_id == WIRE_SESSION_ID
    assert len(SESSION_REF.session_id.encode("utf-8")) <= 128

    assert requests == [
        (
            "/api/v2/memory/add",
            {
                "session_id": WIRE_SESSION_ID,
                "app_id": "avibe",
                "project_id": PROJECT,
                "messages": [
                    {
                        "sender_id": PRINCIPAL,
                        "role": "user",
                        "timestamp": 1_725_000_001_234,
                        "content": "remember this",
                    }
                ],
            },
        ),
        (
            "/api/v2/memory/flush",
            {
                "session_id": WIRE_SESSION_ID,
                "app_id": "avibe",
                "project_id": PROJECT,
            },
        ),
    ]


def test_provider_capture_has_one_canonical_session_identity() -> None:
    capture = ProviderCapture(SESSION_REF, "capture", 1)
    scope_key = b"s" * 32
    raw_session_id = "same-raw-session"
    same_raw_session_other_principal = ProviderSessionRef(
        principal_id="owner-2",
        epoch=SESSION_REF.epoch,
        project_ref=PROJECT,
        session_id=_provider_session_ref(
            scope_key,
            "owner-2",
            PROJECT,
            raw_session_id,
            SESSION_REF.epoch,
        ),
    )
    same_raw_session_next_epoch = ProviderSessionRef(
        principal_id=PRINCIPAL,
        epoch=SESSION_REF.epoch + 1,
        project_ref=PROJECT,
        session_id=_provider_session_ref(
            scope_key,
            PRINCIPAL,
            PROJECT,
            raw_session_id,
            SESSION_REF.epoch + 1,
        ),
    )
    current_session = _provider_session_ref(
        scope_key,
        PRINCIPAL,
        PROJECT,
        raw_session_id,
        SESSION_REF.epoch,
    )

    assert tuple(ProviderCapture.__dataclass_fields__) == (
        "session_ref",
        "text",
        "provider_timestamp_ms",
        "attachments",
        "sender_name",
    )
    assert capture.session_ref.principal_id == PRINCIPAL
    derived_session_ids = {
        current_session,
        same_raw_session_other_principal.session_id,
        same_raw_session_next_epoch.session_id,
    }
    assert len(derived_session_ids) == 3
    assert all(len(session_id.encode("utf-8")) <= 128 for session_id in derived_session_ids)


def test_add_marks_response_disconnect_as_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("response lost", request=request)

    async def run() -> None:
        provider = EverOSPort(Path("/tmp/everos.sock"))
        await provider.add(ProviderCapture(SESSION_REF, "capture", 1))

    with _sidecar_transport(handler):
        with pytest.raises(MemoryProviderSystemFailure) as raised:
            asyncio.run(run())

    assert raised.value.ambiguous is True


def test_add_keeps_connect_timeout_on_retryable_system_outage_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection stalled", request=request)

    async def run() -> None:
        provider = EverOSPort(Path("/tmp/everos.sock"))
        await provider.add(ProviderCapture(SESSION_REF, "capture", 1))

    with _sidecar_transport(handler):
        with pytest.raises(MemoryProviderSystemFailure) as raised:
            asyncio.run(run())

    assert raised.value.error == "memory_sidecar_unavailable"
    assert raised.value.ambiguous is False


@pytest.mark.parametrize("failure_type", [httpx.WriteError, httpx.CloseError])
def test_add_marks_post_submission_transport_failures_as_ambiguous(
    failure_type: type[httpx.TransportError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure_type("response lost", request=request)

    async def run() -> None:
        provider = EverOSPort(Path("/tmp/everos.sock"))
        await provider.add(ProviderCapture(SESSION_REF, "capture", 1))

    with _sidecar_transport(handler):
        with pytest.raises(MemoryProviderSystemFailure) as raised:
            asyncio.run(run())

    assert raised.value.ambiguous is True


def test_add_rejects_overlong_receipt_without_truncating() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"request_id": "x" * 129, "data": {"status": "accumulated"}},
        )

    async def run() -> AddAck:
        return await EverOSPort(Path("/tmp/everos.sock")).add(
            ProviderCapture(SESSION_REF, "capture", 1)
        )

    with _sidecar_transport(handler):
        ack = asyncio.run(run())

    assert ack == AddAck(request_id=None, status="accumulated")


@pytest.mark.parametrize(
    "status_code",
    [
        400,
        403,
        404,
        408,
        409,
        415,
        422,
        423,
        425,
        429,
        500,
        503,
    ],
)
def test_add_provider_rejection_is_terminal(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "request_id": "rejected-request",
                "error": {"code": "rejected"},
            },
        )

    async def run() -> AddRejected:
        result = await EverOSPort(Path("/tmp/everos.sock")).add(
            ProviderCapture(
                SESSION_REF,
                "remember this",
                1_725_000_001_234,
            )
        )
        assert isinstance(result, AddRejected)
        return result

    with _sidecar_transport(handler):
        rejection = asyncio.run(run())

    assert rejection == AddRejected(
        request_id="rejected-request",
        error_code="rejected",
        server_fault=status_code >= 500,
    )


def test_add_treats_missing_provider_configuration_as_terminal_rejection() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": {"code": "PROVIDER_NOT_CONFIGURED"}},
        )

    async def run() -> AddRejected:
        result = await EverOSPort(Path("/tmp/everos.sock")).add(
            ProviderCapture(SESSION_REF, "capture", 1)
        )
        assert isinstance(result, AddRejected)
        return result

    with _sidecar_transport(handler):
        rejection = asyncio.run(run())

    assert rejection == AddRejected(
        request_id=None,
        error_code="PROVIDER_NOT_CONFIGURED",
        server_fault=False,
    )


@pytest.mark.parametrize(
    ("status_code", "error_code", "expected"),
    [
        pytest.param(415, "UNSUPPORTED_FORMAT", True, id="unsupported-format"),
        pytest.param(503, "CAPABILITY_UNAVAILABLE", True, id="capability-unavailable"),
        pytest.param(400, "BAD_REQUEST", False, id="bad-request"),
        pytest.param(404, "NOT_FOUND", False, id="not-found"),
        pytest.param(409, "CONFLICT", False, id="conflict"),
        pytest.param(422, "INVALID_INPUT", False, id="invalid-input"),
        pytest.param(422, "EXTRACTION_EMPTY", False, id="extraction-empty"),
        pytest.param(
            422,
            "PROVIDER_NOT_CONFIGURED",
            False,
            id="provider-not-configured",
        ),
        pytest.param(
            503,
            "EXTERNAL_SERVICE_UNAVAILABLE",
            False,
            id="external-service-unavailable",
        ),
        pytest.param(418, "FUTURE_REJECTION", False, id="unknown-code"),
    ],
)
def test_attachment_add_rejection_requires_positive_no_write_proof(
    status_code: int,
    error_code: str,
    expected: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"code": error_code}},
        )

    capture = ProviderCapture(
        SESSION_REF,
        "remember this attachment",
        1,
        attachments=(
            ProviderAttachment(
                kind="doc",
                name="evidence.txt",
                uri="file:///owned/evidence.txt",
                ext="txt",
            ),
        ),
    )
    with _sidecar_transport(handler):
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).add(capture))

    assert attachment_add_rejection_proves_no_write(capture, result) is expected
    assert not attachment_add_rejection_proves_no_write(
        ProviderCapture(SESSION_REF, "text only", 1),
        result,
    )


def test_add_forwards_typed_workbench_attachments_without_reading_them() -> None:
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"status": "accumulated"}})

    capture = ProviderCapture(
        session_ref=SESSION_REF,
        text="remember this diagram",
        provider_timestamp_ms=1_725_000_001_234,
        attachments=(
            ProviderAttachment(
                kind="image",
                name="diagram.png",
                uri="file:///owned/attachments/diagram.png",
                ext="png",
            ),
        ),
    )

    with _sidecar_transport(handler):
        asyncio.run(EverOSPort(Path("/tmp/everos.sock")).add(capture))

    assert received["messages"][0]["content"] == [
        {"type": "text", "text": "remember this diagram"},
        {
            "type": "image",
            "name": "diagram.png",
            "uri": "file:///owned/attachments/diagram.png",
            "ext": "png",
        },
    ]


def test_flush_treats_unusable_2xx_body_as_unknown_without_replaying_write(caplog) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, content=b"not-json")

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        ack = await provider.add(ProviderCapture(SESSION_REF, "capture", 1))
        result = await provider.flush(SESSION_REF)
        return ack, result

    with _sidecar_transport(handler):
        ack, result = asyncio.run(run())

    assert ack == AddAck(request_id=None, status=None)
    assert result == FlushUnknown(reason="transport")
    assert requests == ["/api/v2/memory/add", "/api/v2/memory/flush"]
    assert "add returned 2xx with an unusable response body" in caplog.text
    assert "flush returned 2xx with an unusable response body" in caplog.text


def test_flush_treats_unsupported_2xx_status_as_unknown(caplog) -> None:
    responses = iter(
        [
            httpx.Response(200, json={"data": {"status": "future-add"}}),
            httpx.Response(200, json={"data": {"status": "future-flush"}}),
        ]
    )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        ack = await provider.add(ProviderCapture(SESSION_REF, "capture", 1))
        result = await provider.flush(SESSION_REF)
        return ack, result

    with _sidecar_transport(lambda _request: next(responses)):
        ack, result = asyncio.run(run())

    assert ack == AddAck(request_id=None, status=None)
    assert result == FlushUnknown(reason="transport")
    assert "add returned an unsupported status value" in caplog.text
    assert "flush returned an unsupported status value" in caplog.text


@pytest.mark.parametrize("request_id", ["", "x" * 129])
def test_flush_rejects_invalid_success_receipt(request_id: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"request_id": request_id, "data": {"status": "extracted"}},
        )

    with _sidecar_transport(handler):
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).flush(SESSION_REF))

    assert result == FlushUnknown(reason="transport")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(400, json={"request_id": "bad-request", "error": {"code": "INVALID_INPUT"}}),
            FlushRejected("bad-request", "INVALID_INPUT", server_fault=False),
        ),
        (
            httpx.Response(500, json={"request_id": "server-request", "error": {"code": "INTERNAL_ERROR"}}),
            FlushRejected("server-request", "INTERNAL_ERROR", server_fault=True),
        ),
    ],
)
def test_flush_maps_non_2xx_envelopes_to_rejected(response: httpx.Response, expected) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    with _sidecar_transport(handler):
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).flush(SESSION_REF))

    assert result == expected


@pytest.mark.parametrize(
    ("operation", "result_type", "route"),
    [
        ("add", AddRejected, "/api/v2/memory/add"),
        ("flush", FlushRejected, "/api/v2/memory/flush"),
    ],
)
@pytest.mark.parametrize(
    ("status_code", "failure_type", "server_fault"),
    [
        (422, httpx.ReadTimeout, False),
        (503, httpx.ReadError, True),
    ],
)
def test_write_preserves_non_2xx_verdict_when_response_body_is_lost(
    operation: str,
    result_type: type[AddRejected] | type[FlushRejected],
    route: str,
    status_code: int,
    failure_type: type[httpx.TransportError],
    server_fault: bool,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            status_code,
            stream=_FailingResponseStream(failure_type, request),
        )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        if operation == "add":
            return await provider.add(ProviderCapture(SESSION_REF, "capture", 1))
        return await provider.flush(SESSION_REF)

    with _sidecar_transport(handler):
        result = asyncio.run(run())

    assert isinstance(result, result_type)
    assert (result.request_id, result.error_code, result.server_fault) == (
        None,
        None,
        server_fault,
    )
    assert requests == [route]


@pytest.mark.parametrize(
    ("operation", "route"),
    [
        ("add", "/api/v2/memory/add"),
        ("flush", "/api/v2/memory/flush"),
    ],
)
def test_write_keeps_2xx_body_disconnect_unknown_without_replaying(
    operation: str,
    route: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            stream=_FailingResponseStream(httpx.ReadError, request),
        )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        if operation == "add":
            return await provider.add(ProviderCapture(SESSION_REF, "capture", 1))
        return await provider.flush(SESSION_REF)

    with _sidecar_transport(handler):
        if operation == "add":
            with pytest.raises(MemoryProviderSystemFailure) as raised:
                asyncio.run(run())
            assert raised.value.ambiguous is True
        else:
            assert asyncio.run(run()) == FlushUnknown(reason="transport")

    assert requests == [route]


@pytest.mark.parametrize(
    ("failure_type", "expected"),
    [
        (httpx.ReadTimeout, FlushUnknown("timeout")),
        (httpx.ReadError, FlushUnknown("transport")),
        (httpx.WriteError, FlushUnknown("transport")),
        (httpx.CloseError, FlushUnknown("transport")),
        (httpx.ConnectTimeout, FlushRetryable()),
        (httpx.PoolTimeout, FlushRetryable()),
        (httpx.ConnectError, FlushRetryable()),
    ],
)
def test_flush_preserves_pre_and_post_submission_failure_classification(
    failure_type,
    expected,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure_type("failed", request=request)

    with _sidecar_transport(handler):
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).flush(SESSION_REF))

    assert result == expected


def test_search_uses_public_search_only_and_maps_episode_and_nested_fact() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        return httpx.Response(
            200,
            json={
                "data": {
                    "episodes": [
                        {
                            "user_id": "owner-1",
                            "subject": "Preferred language",
                            "summary": "The owner uses Python.",
                            "created_at": "2026-07-22T11:00:00Z",
                            "atomic_facts": [
                                {"content": "Uses Python for automation.", "timestamp": 1_721_644_800_000}
                            ],
                        },
                        {"user_id": "someone-else", "summary": "must not leak"},
                    ]
                }
            },
        )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        return await provider.search(
            PRINCIPAL,
            PROJECT,
            "language",
            100,
            session_ref=SESSION_REF,
        )

    with _sidecar_transport(handler):
        items = asyncio.run(run())

    assert requests == [
        (
            "/api/v2/memory/search",
            {
                "user_id": PRINCIPAL,
                "app_id": "avibe",
                "project_id": PROJECT,
                "query": "language",
                "method": "hybrid",
                "top_k": 100,
                "include_profile": True,
                "enable_llm_rerank": False,
                "filters": {"session_id": WIRE_SESSION_ID},
            },
        )
    ]
    assert items[0].item.kind == "episode"
    assert items[0].item.text == "Preferred language\nThe owner uses Python."
    assert items[0].item.date == "2026-07-22"
    assert items[0].score is None
    assert items[0].episode_id is None
    assert items[0].provider_rank == 0
    assert items[0].queried_owner == PRINCIPAL
    assert items[1].item.kind == "fact"
    assert items[1].item.text == "Uses Python for automation."


def test_assistant_owner_crosses_add_search_and_profile_provider_contract() -> None:
    """MEMORY-SEARCH-008, MEMORY-SEARCH-009, MEMORY-SEARCH-011 stay scoped."""

    assistant_owner = "u-11111111111111111111111111111111-agent"
    session_ref = ProviderSessionRef(
        principal_id=assistant_owner,
        epoch=0,
        project_ref=PROJECT,
        session_id="src--assistant-owner--e0",
    )
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path.endswith("/add"):
            return httpx.Response(
                200,
                json={"request_id": "add-assistant", "data": {"status": "accumulated"}},
            )
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "episodes": [
                            {
                                "id": "episode-agent",
                                "user_id": assistant_owner,
                                "summary": "Agent-owned memory",
                                "score": 0.75,
                                "timestamp": "2026-08-21T10:00:00Z",
                            },
                            {"id": "wrong-owner", "user_id": PRINCIPAL, "summary": "must not leak"},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "profiles": [
                        {"user_id": assistant_owner, "profile_data": {"summary": "Agent profile"}},
                        {"user_id": PRINCIPAL, "profile_data": {"summary": "must not leak"}},
                    ]
                }
            },
        )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        added = await provider.add(ProviderCapture(session_ref, "记住发布计划", 1_725_000_001_234))
        searched = await provider.search(assistant_owner, PROJECT, "发布", 5)
        profile = await provider.profile(assistant_owner, PROJECT)
        return added, searched, profile

    with _sidecar_transport(handler):
        added, searched, profile = asyncio.run(run())

    assert added == AddAck(request_id="add-assistant", status="accumulated")
    assert requests[0][1]["messages"][0]["sender_id"] == assistant_owner
    assert requests[1][1]["user_id"] == assistant_owner
    assert requests[2][1]["user_id"] == assistant_owner
    assert len(searched) == 1
    assert searched[0].queried_owner == assistant_owner
    assert searched[0].score == 0.75
    assert searched[0].episode_id == "episode-agent"
    assert searched[0].timestamp == "2026-08-21T10:00:00Z"
    assert [item.text for item in profile] == ['{"summary":"Agent profile"}']


def test_agentic_search_retains_allowlisted_round_metadata() -> None:
    """MEMORY-SEARCH-010: agentic recall uses one bounded provider leg."""

    requests: list[dict] = []
    sidecar_timeouts: list[float] = []
    telemetry = AgenticRecallTelemetry()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        sidecar_timeouts.append(
            float(request.headers[_AGENTIC_TIMEOUT_HEADER])
        )
        return httpx.Response(
            200,
            headers={_AGENTIC_ROUND_HEADER: "round2"},
            json={"data": {"episodes": []}},
        )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        assert provider.agentic_budget_enforced is True
        return await provider.search(
            PRINCIPAL,
            PROJECT,
            "private multi-hop query",
            2,
            method="agentic",
            timeout_seconds=5,
            agentic_telemetry=telemetry,
        )

    with _sidecar_transport(handler):
        assert asyncio.run(run()) == ()

    assert requests == [
        {
            "user_id": PRINCIPAL,
            "app_id": "avibe",
            "project_id": PROJECT,
            "query": "private multi-hop query",
            "method": "agentic",
            "top_k": 2,
            "include_profile": True,
            "enable_llm_rerank": False,
        }
    ]
    assert sidecar_timeouts == [pytest.approx(4.95)]
    assert telemetry.round == "round2"


def test_agentic_search_retains_round_on_mapping_failure() -> None:
    telemetry = AgenticRecallTelemetry()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={_AGENTIC_ROUND_HEADER: "round1"},
            json={"data": {"episodes": "invalid"}},
        )

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).search(
                PRINCIPAL,
                PROJECT,
                "private multi-hop query",
                2,
                method="agentic",
                timeout_seconds=5,
                agentic_telemetry=telemetry,
            )
        return raised.value

    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.error == "memory_provider_response_invalid"
    assert telemetry.round == "round1"


def test_agentic_search_wall_clock_timeout_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EverOSPort(Path("/tmp/everos.sock"))
    telemetry = AgenticRecallTelemetry()

    async def slow_request(*_args, **_kwargs):
        await asyncio.sleep(1)
        return {"data": {"episodes": []}}

    monkeypatch.setattr(provider, "_sidecar_request", slow_request)

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await provider.search(
                PRINCIPAL,
                PROJECT,
                "private multi-hop query",
                2,
                method="agentic",
                timeout_seconds=0.01,
                agentic_telemetry=telemetry,
            )
        return raised.value

    failure = asyncio.run(run())

    assert failure.error == "memory_provider_timeout"
    assert telemetry.round == "unknown"


def test_agentic_search_maps_provider_422_to_closed_capability_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": {"code": "PROVIDER_NOT_CONFIGURED"}},
        )

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).search(
                PRINCIPAL,
                PROJECT,
                "connect the clues",
                2,
                method="agentic",
                timeout_seconds=5,
            )
        return raised.value

    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.error == "memory_capability_unavailable"
    assert "422" not in str(failure)


def test_agentic_search_retains_round_on_sidecar_deadline() -> None:
    telemetry = AgenticRecallTelemetry()

    def handler(request: httpx.Request) -> httpx.Response:
        assert float(request.headers[_AGENTIC_TIMEOUT_HEADER]) <= 5
        return httpx.Response(
            504,
            headers={_AGENTIC_ROUND_HEADER: "round2"},
            json={"detail": "memory_request_timed_out"},
        )

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).search(
                PRINCIPAL,
                PROJECT,
                "connect the clues",
                2,
                method="agentic",
                timeout_seconds=5,
                agentic_telemetry=telemetry,
            )
        return raised.value

    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.error == "memory_provider_timeout"
    assert telemetry.round == "round2"


def test_profile_uses_get_and_reports_empty_profile_as_non_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/memory/get"
        assert json.loads(request.content) == {
            "user_id": PRINCIPAL,
            "app_id": "avibe",
            "project_id": "default",
            "memory_type": "profile",
            "page": 1,
            "page_size": 1,
        }
        return httpx.Response(200, json={"data": {"profiles": []}})

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        return await provider.profile(PRINCIPAL, PROJECT)

    with _sidecar_transport(handler):
        items = asyncio.run(run())

    # A valid response with no profile payload is zero items, not a failure.
    # The provider keeps no per-read state for it: one EverOSPort serves every
    # principal, so a field here would be whichever read finished last.
    assert items == ()


def test_list_episodes_uses_exact_get_shape_and_projects_a_bounded_page() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "request_id": "get-page-2",
                "data": {
                    "episodes": [
                        {
                            "id": "episode-opaque-2",
                            "user_id": PRINCIPAL,
                            "app_id": "avibe",
                            "project_id": "notes",
                            "session_id": "provider-session",
                            "timestamp": "2026-08-14T10:11:12+08:00",
                            "sender_ids": [PRINCIPAL],
                            "summary": " A bounded summary. ",
                            "subject": " ",
                            "episode": "Processed episode body.",
                            "type": "Conversation",
                        }
                    ],
                    "profiles": [],
                    "agent_cases": [],
                    "agent_skills": [],
                    "total_count": 20_001,
                    "count": 1,
                },
            },
        )

    with _sidecar_transport(handler):
        result = asyncio.run(
            EverOSPort(Path("/tmp/everos.sock")).list_episodes(
                PRINCIPAL,
                "notes",
                2,
                1,
            )
        )

    assert requests == [
        {
            "user_id": PRINCIPAL,
            "app_id": "avibe",
            "project_id": "notes",
            "memory_type": "episode",
            "page": 2,
            "page_size": 1,
            "sort_by": "timestamp",
            "sort_order": "desc",
        }
    ]
    assert result == MemoryListPage(
        items=(
            MemoryListItem(
                id="episode-opaque-2",
                subject="",
                summary="A bounded summary.",
                body="Processed episode body.",
                timestamp="2026-08-14T02:11:12Z",
                project="notes",
            ),
        ),
        page=2,
        page_size=1,
        count=1,
        total_count=20_001,
        warnings=("memory_list_truncated",),
    )


def test_list_episodes_accepts_everos_max_page_size() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "request_id": "empty-page",
                "data": {
                    "episodes": [],
                    "profiles": [],
                    "agent_cases": [],
                    "agent_skills": [],
                    "total_count": 0,
                    "count": 0,
                },
            },
        )

    with _sidecar_transport(handler):
        result = asyncio.run(
            EverOSPort(Path("/tmp/everos.sock")).list_episodes(
                PRINCIPAL,
                PROJECT,
                1,
                100,
            )
        )

    assert requests[0]["page_size"] == 100
    assert result.page_size == 100


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["episodes"][0].update(user_id="u-" + "f" * 32),
        lambda data: data.update(count=0),
        lambda data: data.update(episodes=[], count=0, total_count=1),
        lambda data: data["profiles"].append({"profile_data": {}}),
        lambda data: data["episodes"][0].update(timestamp="not-a-timestamp"),
        lambda data: data["episodes"][0].update(
            timestamp="0001-01-01T00:00:00+23:59"
        ),
        lambda data: data["episodes"][0].update(summary="\ud800"),
        lambda data: (
            data["episodes"].append(dict(data["episodes"][0])),
            data.update(total_count=2, count=2),
        ),
    ],
)
def test_list_episodes_rejects_cross_scope_or_malformed_envelopes(mutation) -> None:
    data = {
        "episodes": [
            {
                "id": "episode-1",
                "user_id": PRINCIPAL,
                "app_id": "avibe",
                "project_id": PROJECT,
                "session_id": "provider-session",
                "timestamp": "2026-08-14T00:00:00Z",
                "sender_ids": [],
                "summary": "Summary",
                "subject": "Subject",
                "episode": "Body",
                "type": "Conversation",
            }
        ],
        "profiles": [],
        "agent_cases": [],
        "agent_skills": [],
        "total_count": 1,
        "count": 1,
    }
    mutation(data)

    def handler(_request: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {"request_id": "request", "data": data},
            ensure_ascii=True,
        ).encode("ascii")
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).list_episodes(
                PRINCIPAL,
                PROJECT,
                1,
                20,
            )
        assert raised.value.error == "memory_provider_response_invalid"

    with _sidecar_transport(handler):
        asyncio.run(run())


def test_list_episodes_rejects_nonempty_page_beyond_total_count() -> None:
    data = {
        "episodes": [
            {
                "id": "episode-impossible-page",
                "user_id": PRINCIPAL,
                "app_id": "avibe",
                "project_id": PROJECT,
                "session_id": "provider-session",
                "timestamp": "2026-08-14T00:00:00Z",
                "sender_ids": [],
                "summary": "Summary",
                "subject": "Subject",
                "episode": "Body",
                "type": "Conversation",
            }
        ],
        "profiles": [],
        "agent_cases": [],
        "agent_skills": [],
        "total_count": 1,
        "count": 1,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"request_id": "request", "data": data})

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).list_episodes(
                PRINCIPAL,
                PROJECT,
                3,
                5,
            )
        assert raised.value.error == "memory_provider_response_invalid"

    with _sidecar_transport(handler):
        asyncio.run(run())


def test_list_episodes_rejects_ascending_provider_page() -> None:
    def episode(episode_id: str, timestamp: str) -> dict[str, object]:
        return {
            "id": episode_id,
            "user_id": PRINCIPAL,
            "app_id": "avibe",
            "project_id": PROJECT,
            "session_id": "provider-session",
            "timestamp": timestamp,
            "sender_ids": [],
            "summary": "Summary",
            "subject": "Subject",
            "episode": "Body",
            "type": "Conversation",
        }

    data = {
        "episodes": [
            episode("episode-older", "2026-08-14T00:00:00Z"),
            episode("episode-newer", "2026-08-14T00:00:01Z"),
        ],
        "profiles": [],
        "agent_cases": [],
        "agent_skills": [],
        "total_count": 2,
        "count": 2,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"request_id": "request", "data": data})

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).list_episodes(
                PRINCIPAL,
                PROJECT,
                1,
                20,
            )
        assert raised.value.error == "memory_provider_response_invalid"

    with _sidecar_transport(handler):
        asyncio.run(run())


def test_profile_canonicalizes_structured_profile() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "profiles": [
                        {"user_id": "owner-1", "profile_data": {"language": "Python", "timezone": "UTC"}}
                    ]
                }
            },
        )

    async def run():
        return await EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT)

    with _sidecar_transport(handler):
        items = asyncio.run(run())

    assert items[0].kind == "profile"
    assert items[0].text == '{"language":"Python","timezone":"UTC"}'
    assert items[0].profile is None


@pytest.mark.parametrize(
    "owner_id",
    ["u-11111111111111111111111111111111", "u-11111111111111111111111111111111-agent"],
)
def test_profile_accepts_large_everos_payloads(owner_id: str) -> None:
    summary = "profile " * 10_000
    padding = "x" * (2 * 1024 * 1024)

    def handler(_request: httpx.Request) -> httpx.Response:
        response = {
            "data": {
                "profiles": [
                    {"user_id": owner_id, "profile_data": {"summary": summary}}
                ],
                "provider_metadata": padding,
            }
        }
        assert len(json.dumps(response).encode()) > 2 * 1024 * 1024
        return httpx.Response(200, json=response)

    with _sidecar_transport(handler):
        items = asyncio.run(
            EverOSPort(Path("/tmp/everos.sock")).profile(owner_id, PROJECT)
        )

    assert len(summary.encode()) > 64 * 1024
    assert items[0].profile is not None
    assert items[0].profile.summary == summary.strip()


def test_profile_accepts_everos_structures_beyond_legacy_avibe_limits() -> None:
    """MEMORY-SEARCH-018: profile structure is governed by EverOS."""

    nested: object = "value"
    for _index in range(12):
        nested = {"nested": nested}
    long_key = "k" * 129
    profile_data = {
        "summary": "Complete profile",
        "explicit_info": [
            {"description": f"fact-{index}"} for index in range(201)
        ],
        "implicit_traits": [
            {"description": f"trait-{index}"} for index in range(201)
        ],
        long_key: nested,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "profiles": [
                        {"user_id": "owner-1", "profile_data": profile_data}
                    ],
                    "provider_metadata": list(range(201)),
                }
            },
        )

    with _sidecar_transport(handler):
        items = asyncio.run(
            EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT)
        )

    assert items[0].profile is not None
    assert len(items[0].profile.explicit_info) == 201
    assert len(items[0].profile.implicit_traits) == 201
    assert json.loads(items[0].text)[long_key] == nested


def test_profile_maps_known_fields_without_collapsing_basis_and_evidence() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "profiles": [
                        {
                            "user_id": "owner-1",
                            "profile_data": {
                                "summary": "Prefers concise technical discussions.",
                                "explicit_info": [
                                    {
                                        "category": "communication",
                                        "description": "Prefers written updates.",
                                        "evidence": "Asked for a written summary.",
                                    },
                                    {"description": 42},
                                ],
                                "implicit_traits": [
                                    {
                                        "trait": "methodical",
                                        "description": "May prefer a clear sequence of steps.",
                                        "basis": "Repeatedly requested checklists.",
                                        "evidence": "Three recent planning discussions.",
                                    }
                                ],
                                "profile_timestamp_ms": 0,
                            },
                        }
                    ]
                }
            },
        )

    with _sidecar_transport(handler):
        items = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT))

    assert items[0].date == "1970-01-01"
    assert items[0].profile == MemoryProfile(
        summary="Prefers concise technical discussions.",
        explicit_info=(
            MemoryProfileExplicitInfo(
                category="communication",
                description="Prefers written updates.",
                evidence="Asked for a written summary.",
            ),
        ),
        implicit_traits=(
            MemoryProfileTrait(
                trait="methodical",
                description="May prefer a clear sequence of steps.",
                basis="Repeatedly requested checklists.",
                evidence="Three recent planning discussions.",
            ),
        ),
        updated_at="1970-01-01T00:00:00Z",
    )
    assert json.loads(items[0].text)["implicit_traits"][0]["basis"] == "Repeatedly requested checklists."
    assert json.loads(items[0].text)["implicit_traits"][0]["evidence"] == "Three recent planning discussions."


def test_profile_repairs_provider_summary_pinned_to_first_explicit_item() -> None:
    profile_data = {
        "summary": "Continue",
        "explicit_info": [
            {"category": "conversation", "description": "Continue"},
            {
                "category": "engineering",
                "description": "Prefers evidence-backed engineering decisions.",
            },
        ],
        "implicit_traits": [
            {
                "trait": "systematic",
                "description": "Builds a causal model before choosing scope.",
            }
        ],
        "profile_timestamp_ms": 1_757_000_000_000,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"profiles": [{"user_id": "owner-1", "profile_data": profile_data}]}},
        )

    with _sidecar_transport(handler):
        items = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT))

    assert items[0].profile is not None
    assert items[0].profile.summary == "Prefers evidence-backed engineering decisions."
    # The compatibility correction is read-only; raw provider data remains available.
    assert json.loads(items[0].text)["summary"] == "Continue"


def test_profile_preserves_provider_summary_that_is_not_the_first_item() -> None:
    profile_data = {
        "summary": "A concise portrait of the user's stable preferences.",
        "explicit_info": [
            {"description": "Uses Python."},
            {"description": "Prefers written updates."},
        ],
        "profile_timestamp_ms": 1_757_000_000_000,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"profiles": [{"user_id": "owner-1", "profile_data": profile_data}]}},
        )

    with _sidecar_transport(handler):
        items = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT))

    assert items[0].profile is not None
    assert items[0].profile.summary == profile_data["summary"]


def test_profile_timestamp_without_recognized_content_uses_the_raw_fallback() -> None:
    raw_profile = {
        "profile_timestamp_ms": 1_754_012_345_678,
        "future_provider_field": {"only": "opaque data"},
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "profiles": [
                        {
                            "user_id": "owner-1",
                            "profile_data": raw_profile,
                            "created_at": "2026-08-01T12:34:56Z",
                        }
                    ]
                }
            },
        )

    with _sidecar_transport(handler):
        items = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT))

    assert len(items) == 1
    assert items[0].profile is None
    assert items[0].date == "2026-08-01"
    assert json.loads(items[0].text) == raw_profile


def test_profile_rejects_wrong_shaped_known_collections() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "profiles": [
                        {
                            "user_id": "owner-1",
                            "profile_data": {"explicit_info": "not-a-list"},
                        }
                    ]
                }
            },
        )

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).profile("owner-1", PROJECT)
        assert raised.value.error == "memory_provider_response_invalid"

    with _sidecar_transport(handler):
        asyncio.run(run())


def test_invalid_search_envelope_is_closed_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"episodes": "not-a-list"}})

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).search("owner-1", PROJECT, "x", 1)
        assert raised.value.error == "memory_provider_response_invalid"

    with _sidecar_transport(handler):
        asyncio.run(run())


def test_processing_health_probes_both_authenticated_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": "provider-slot-model",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "", "role": "assistant"},
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    async def run() -> bool:
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat-model",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embedding-model",
            embedding_api_key="embedding-secret",
        ).processing_healthy()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        # The production adapter uses two client constructions: sidecar is not
        # used for processing probes, so return a normal mock transport client
        # through a small real-client factory instead of inspecting secrets.
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        assert asyncio.run(run()) is True

    assert [request.url.path for request in requests] == ["/v1/chat/completions", "/v1/embeddings"]
    assert all(request.headers["authorization"].startswith("Bearer ") for request in requests)
    assert json.loads(requests[0].content)["max_tokens"] == 8


def test_processing_health_serializes_identical_provider_credentials(monkeypatch) -> None:
    provider = EverOSPort(
        Path("/tmp/everos.sock"),
        llm_base_url="https://shared.example.test/v1",
        llm_model="chat-model",
        llm_api_key="shared-secret",
        embedding_base_url="https://shared.example.test/v1",
        embedding_model="embedding-model",
        embedding_api_key="embedding-secret",
        rerank_base_url="https://rerank.example.test/v1/inference",
        rerank_model="rerank-model",
        rerank_api_key="shared-secret",
        multimodal_base_url="https://shared.example.test/v1",
        multimodal_model="vision-model",
        multimodal_api_key="shared-secret",
    )
    first_wave_started = asyncio.Event()
    release = asyncio.Event()
    started: list[tuple[tuple[str, str], str]] = []
    active_by_group: dict[tuple[str, str], int] = {}
    max_active_by_group: dict[tuple[str, str], int] = {}
    max_total_active = 0

    async def probe(*, base_url, api_key, path, payload, **_kwargs) -> bool:
        nonlocal max_total_active
        group = (base_url, api_key)
        name = payload.get("model") or path
        active_by_group[group] = active_by_group.get(group, 0) + 1
        max_active_by_group[group] = max(
            max_active_by_group.get(group, 0),
            active_by_group[group],
        )
        max_total_active = max(max_total_active, sum(active_by_group.values()))
        started.append((group, name))
        if len(started) == 3:
            first_wave_started.set()
        try:
            await release.wait()
            return True
        finally:
            active_by_group[group] -= 1

    monkeypatch.setattr(provider, "_probe_processing_endpoint", probe)

    async def run() -> bool:
        task = asyncio.create_task(provider.processing_healthy())
        await asyncio.wait_for(first_wave_started.wait(), timeout=1.0)
        assert {name for _group, name in started} == {
            "chat-model",
            "embedding-model",
            "rerank-model",
        }
        release.set()
        return await task

    assert asyncio.run(run()) is True
    assert [name for _group, name in started].count("vision-model") == 1
    assert max_total_active == 3
    assert all(active == 1 for active in max_active_by_group.values())


def test_processing_preflight_projects_sanitized_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"code": "model_not_supported", "message": "secret-key should not leak"}})
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1", llm_model="chat", llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1", embedding_model="embed", embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs)
        result = asyncio.run(run())
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.error == "memory_llm_unavailable"
    assert result.failure.diagnostic.http_status == 404
    assert result.failure.diagnostic.provider_error_code == "model_not_supported"
    assert "secret" not in result.failure.diagnostic.message


@pytest.mark.parametrize("content", ["", None])
def test_processing_preflight_accepts_truncated_chat_completion_from_resolved_slot(content) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-probe",
                    "object": "chat.completion",
                    "model": "provider-slot-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "length",
                            "message": {
                                "role": "assistant",
                                "content": content,
                                "reasoning_content": "O",
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="client-alias",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    request_payload = json.loads(requests[0].content)
    assert request_payload["model"] == "client-alias"
    assert request_payload["max_tokens"] == 8


def test_preflight_timeout_budget_stays_above_health_probe_budget() -> None:
    assert _PREFLIGHT_TIMEOUT_SECONDS == 30.0
    assert PROCESSING_PROBE_REQUEST_TIMEOUT_SECONDS == 8.0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_thinking_chat_completion(), None),
        ({"choices": [{"message": {"content": "OK"}}]}, None),
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "OK"},
                    }
                ]
            },
            None,
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": ""},
                    }
                ]
            },
            None,
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": None},
                    }
                ]
            },
            None,
        ),
        (
            _thinking_chat_completion(finish_reason=None),
            "provider_response_missing_finish_reason",
        ),
        (
            _thinking_chat_completion(role="user"),
            "provider_response_invalid_role",
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {}}]},
            "provider_response_invalid_role",
        ),
        ({"choices": [{}]}, "provider_response_missing_message"),
    ],
)
def test_chat_probe_validator_accepts_thinking_model_and_openai_shapes(
    payload: dict, expected: str | None
) -> None:
    assert _chat_probe_response_issue(payload) == expected


def test_processing_preflight_accepts_thinking_model_chat_completions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(200, json=_thinking_chat_completion())

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            multimodal_base_url="https://vision.example.test/v1",
            multimodal_model="vision-model",
            multimodal_api_key="vision-secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    assert [request.url.path for request in requests] == [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/chat/completions",
    ]


def test_processing_health_accepts_thinking_model_chat_completions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        return httpx.Response(200, json=_thinking_chat_completion())

    async def run() -> bool:
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat-model",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embedding-model",
            embedding_api_key="embedding-secret",
            multimodal_base_url="https://vision.example.test/v1",
            multimodal_model="vision-model",
            multimodal_api_key="vision-secret",
        ).processing_healthy()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        assert asyncio.run(run()) is True


def test_processing_preflight_reports_the_rejected_2xx_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.diagnostic.http_status == 200
    assert result.failure.diagnostic.message == "provider_response_missing_message"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"content": ""}, "provider_response_invalid_role"),
        ({"role": "assistant", "content": ""}, "provider_response_missing_finish_reason"),
        ({"content": " "}, "provider_response_invalid_role"),
        ({"role": "assistant", "content": " "}, "provider_response_missing_finish_reason"),
    ],
)
def test_processing_preflight_requires_completion_metadata_for_empty_chat_content(
    message: dict[str, str], expected: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": message}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.diagnostic.message == expected


def test_processing_preflight_rejects_unhashable_finish_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": {},
                            "message": {"role": "assistant", "content": ""},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.diagnostic.http_status == 200
    assert result.failure.diagnostic.message == "provider_response_invalid_finish_reason"


def test_processing_preflight_probes_configured_rerank_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(200, json={"scores": [[0.9]]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            rerank_base_url="https://rerank.example.test/v1/inference",
            rerank_model="Qwen/Qwen3-Reranker-4B",
            rerank_api_key="rerank-secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    assert [request.url.path for request in requests] == [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/inference/Qwen/Qwen3-Reranker-4B",
    ]
    assert json.loads(requests[-1].content) == {
        "queries": ["OK"],
        "documents": ["OK"],
    }
    assert requests[-1].headers["authorization"] == "Bearer rerank-secret"


def test_processing_preflight_probes_vllm_rerank_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            rerank_base_url="http://localhost:8000/v1",
            rerank_model="Qwen/Qwen3-Reranker-4B",
            rerank_api_key="rerank-secret",
            rerank_provider="vllm",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    assert [request.url.path for request in requests][-1] == "/v1/rerank"
    assert json.loads(requests[-1].content) == {
        "model": "Qwen/Qwen3-Reranker-4B",
        "query": "OK",
        "documents": ["OK"],
    }


def test_processing_preflight_probes_dashscope_rerank_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(
            200,
            json={"output": {"results": [{"index": 0, "relevance_score": 0.9}]}},
        )

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            rerank_base_url="https://dashscope.aliyuncs.com",
            rerank_model="gte-rerank-v2",
            rerank_api_key="rerank-secret",
            rerank_provider="dashscope",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    assert [request.url.path for request in requests][-1] == (
        "/api/v1/services/rerank/text-rerank/text-rerank"
    )
    assert json.loads(requests[-1].content) == {
        "model": "gte-rerank-v2",
        "input": {"query": "OK", "documents": ["OK"]},
        "parameters": {"return_documents": False, "top_n": 1},
    }


def test_processing_preflight_infers_dashscope_from_maas_url_without_provider() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(
            200,
            json={"output": {"results": [{"index": 0, "relevance_score": 0.9}]}},
        )

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            rerank_base_url="https://llm-space.example.maas.aliyuncs.com",
            rerank_model="gte-rerank-v2",
            rerank_api_key="rerank-secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    assert [request.url.path for request in requests][-1] == (
        "/api/v1/services/rerank/text-rerank/text-rerank"
    )
    assert json.loads(requests[-1].content)["input"] == {"query": "OK", "documents": ["OK"]}


def test_processing_preflight_probes_configured_multimodal_endpoint() -> None:
    """MEMORY-IM-ATTACH-001: opt-in is admitted with synthetic image data only."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            multimodal_base_url="https://vision.example.test/v1",
            multimodal_model="vision-model",
            multimodal_api_key="vision-secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is True
    assert [request.url.path for request in requests] == [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/chat/completions",
    ]
    payload = json.loads(requests[-1].content)
    assert payload["model"] == "vision-model"
    assert payload["max_tokens"] == 8
    assert payload["messages"][0]["content"][0] == {
        "type": "text",
        "text": "Reply with OK.",
    }
    image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
    prefix, encoded_image = image_url.split(",", 1)
    assert prefix == "data:image/png;base64"
    assert len(image_url) < 256
    image_bytes = base64.b64decode(encoded_image, validate=True)
    assert len(image_bytes) == 153
    assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", image_bytes[16:24]) == (64, 64)
    assert hashlib.sha256(image_bytes).hexdigest() == (
        "da1cbcc0076a2b589fd4d5b79d7fd171d6dff91f4d708d8dec041f4a6e60734f"
    )
    assert requests[-1].headers["authorization"] == "Bearer vision-secret"


def test_processing_preflight_returns_typed_multimodal_failure() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        if call_count == 3:
            return httpx.Response(401, json={"error": {"code": "invalid_key"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            multimodal_base_url="https://vision.example.test/v1",
            multimodal_model="vision-model",
            multimodal_api_key="vision-secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.error == "memory_multimodal_unavailable"
    assert result.failure.diagnostic.side == "multimodal"
    assert result.failure.diagnostic.http_status == 401
    assert result.failure.diagnostic.provider_error_code == "invalid_key"


def test_processing_preflight_returns_typed_rerank_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
        return httpx.Response(401, json={"error": {"code": "invalid_key"}})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="embedding-secret",
            rerank_base_url="https://rerank.example.test/v1/inference",
            rerank_model="rerank-model",
            rerank_api_key="rerank-secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.error == "memory_rerank_unavailable"
    assert result.failure.diagnostic.side == "rerank"
    assert result.failure.diagnostic.http_status == 401
    assert result.failure.diagnostic.provider_error_code == "invalid_key"


def test_processing_preflight_scrubs_provider_error_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "https://embed.example.test/v1?api_key=secret"}},
        )

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())
    assert result.failure is not None
    assert "https://" not in result.failure.diagnostic.provider_error_code
    assert "secret" not in result.failure.diagnostic.provider_error_code


def test_processing_preflight_preserves_http_status_for_non_json_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>gateway failure</html>")

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())
    assert result.failure is not None
    assert result.failure.error == "memory_llm_unavailable"
    assert result.failure.diagnostic.http_status == 502
    assert result.failure.diagnostic.message == "HTTP 502"


def test_processing_preflight_accepts_large_bounded_embedding_vectors() -> None:
    vector = [0.123456789] * 16_384
    assert len(json.dumps({"data": [{"embedding": vector}]}).encode()) > 64 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        return httpx.Response(200, json={"data": [{"embedding": vector}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())
    assert result.ok is True


def test_processing_preflight_rejects_response_above_two_mibibytes() -> None:
    large_content = "x" * (2 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": large_content}}]},
            )
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.error == "memory_llm_unavailable"
    assert result.failure.diagnostic.message == "provider_response_too_large"


def test_processing_health_rejects_response_above_two_mibibytes() -> None:
    large_content = "x" * (2 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": large_content}}]},
            )
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

    async def run() -> bool:
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
        ).processing_healthy()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        assert asyncio.run(run()) is False


def test_processing_health_rejects_llm_probe_without_completion_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    async def run() -> bool:
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat-model",
            llm_api_key="llm-secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embedding-model",
            embedding_api_key="embedding-secret",
        ).processing_healthy()

    real_async_client = httpx.AsyncClient
    with patch("avibe_memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs)
        assert asyncio.run(run()) is False


def test_processing_health_uses_owned_child_callback_when_present() -> None:
    calls: list[None] = []

    async def check() -> bool:
        calls.append(None)
        return True

    provider = EverOSPort(Path("/tmp/everos.sock"), processing_health_check=check)
    assert asyncio.run(provider.processing_healthy()) is True
    assert calls == [None]


def test_health_ignores_retired_recorder_projection() -> None:
    payload = _health_envelope()
    payload["recorder"] = {"state": "future", "reason": "retired"}

    with _sidecar_transport(lambda _request: httpx.Response(200, json=payload)):
        snapshot = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).health_snapshot())

    assert not hasattr(snapshot, "recorder")
    assert "recorder" not in snapshot.payload()


def test_sidecar_failure_logs_never_contain_capture_or_response_canaries(caplog) -> None:
    capture_canary = "capture-canary-7d5d6b"
    response_canary = "response-canary-477ebd"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=response_canary.encode("utf-8"))

    async def run() -> AddRejected:
        result = await EverOSPort(Path("/tmp/everos.sock")).add(
            ProviderCapture(SESSION_REF, capture_canary, 1_725_000_001_234)
        )
        assert isinstance(result, AddRejected)
        return result

    with _sidecar_transport(handler):
        rejection = asyncio.run(run())

    assert rejection.error_code is None
    assert capture_canary not in caplog.text
    assert response_canary not in caplog.text

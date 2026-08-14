from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from core.memory.everos import (
    _AGENTIC_TIMEOUT_HEADER,
    AddAck,
    AddRejected,
    EverOSPort,
    FlushRejected,
    FlushRetryable,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderSystemFailure,
    ProviderAttachment,
    ProviderCapture,
)
from core.memory.store import _provider_session_ref
from core.memory.types import (
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


def _sidecar_transport(handler):
    return patch("core.memory.everos.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handler))


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


def _health_envelope(recorder) -> dict:
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
        "recorder": recorder,
    }


@pytest.mark.parametrize(
    ("rerank", "disabled_features"),
    [(False, ["agentic_search"]), (True, [])],
)
def test_health_accepts_additive_typed_capabilities_and_surfaces_rerank_state(
    rerank: bool,
    disabled_features: list[str],
) -> None:
    payload = _health_envelope({"state": "active", "reason": None})
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
    payload = _health_envelope({"state": "active", "reason": None})
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
            2,
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
                "top_k": 2,
                "include_profile": True,
                "enable_llm_rerank": False,
                "filters": {"session_id": WIRE_SESSION_ID},
            },
        )
    ]
    assert items[0].kind == "episode"
    assert items[0].text == "Preferred language\nThe owner uses Python."
    assert items[0].date == "2026-07-22"
    assert items[1].kind == "fact"
    assert items[1].text == "Uses Python for automation."


def test_agentic_search_uses_bounded_public_request_and_scrub_safe_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[dict] = []
    sidecar_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        sidecar_timeouts.append(
            float(request.headers[_AGENTIC_TIMEOUT_HEADER])
        )
        return httpx.Response(200, json={"data": {"episodes": []}})

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
        )

    caplog.set_level("INFO", logger="core.memory.everos")
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
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "mode=agentic" in telemetry[0]
    assert "success=true" in telemetry[0]
    assert "timeout=false" in telemetry[0]
    assert "private multi-hop query" not in telemetry[0]


def test_agentic_search_logs_mapping_failure_as_unsuccessful(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"episodes": "invalid"}})

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).search(
                PRINCIPAL,
                PROJECT,
                "private multi-hop query",
                2,
                method="agentic",
                timeout_seconds=5,
            )
        return raised.value

    caplog.set_level("INFO", logger="core.memory.everos")
    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.error == "memory_provider_response_invalid"
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "success=false" in telemetry[0]
    assert "timeout=false" in telemetry[0]
    assert "private multi-hop query" not in telemetry[0]


def test_agentic_search_wall_clock_timeout_is_typed_and_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = EverOSPort(Path("/tmp/everos.sock"))

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
            )
        return raised.value

    caplog.set_level("INFO", logger="core.memory.everos")
    failure = asyncio.run(run())

    assert failure.error == "memory_provider_timeout"
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "success=false" in telemetry[0]
    assert "timeout=true" in telemetry[0]
    assert "private multi-hop query" not in telemetry[0]


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


def test_agentic_search_maps_sidecar_deadline_to_typed_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert float(request.headers[_AGENTIC_TIMEOUT_HEADER]) <= 5
        return httpx.Response(
            504,
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
            )
        return raised.value

    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.error == "memory_provider_timeout"


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
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        # The production adapter uses two client constructions: sidecar is not
        # used for processing probes, so return a normal mock transport client
        # through a small real-client factory instead of inspecting secrets.
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        assert asyncio.run(run()) is True

    assert [request.url.path for request in requests] == ["/v1/chat/completions", "/v1/embeddings"]
    assert all(request.headers["authorization"].startswith("Bearer ") for request in requests)


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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs)
        result = asyncio.run(run())
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.error == "memory_llm_unavailable"
    assert result.failure.diagnostic.http_status == 404
    assert result.failure.diagnostic.provider_error_code == "model_not_supported"
    assert "secret" not in result.failure.diagnostic.message


def test_processing_preflight_probes_configured_rerank_endpoint() -> None:
    requests: list[httpx.Request] = []
    recorded: list[dict[str, object]] = []

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
            preflight_call_recorder=lambda **kwargs: recorded.append(kwargs),
        ).preflight()

    real_async_client = httpx.AsyncClient
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
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
    assert recorded[-1]["model"] == "Qwen/Qwen3-Reranker-4B"
    assert "model" not in recorded[-1]["request"]


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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())
    assert result.ok is True


def test_processing_preflight_records_actual_call_duration() -> None:
    recorded: list[dict[str, object]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "unavailable"}})

    async def run():
        return await EverOSPort(
            Path("/tmp/everos.sock"),
            llm_base_url="https://llm.example.test/v1",
            llm_model="chat",
            llm_api_key="secret",
            embedding_base_url="https://embed.example.test/v1",
            embedding_model="embed",
            embedding_api_key="secret",
            preflight_call_recorder=lambda **kwargs: recorded.append(kwargs),
        ).preflight()

    real_async_client = httpx.AsyncClient
    with (
        patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type,
        patch("core.memory.everos._elapsed_ms", return_value=321),
    ):
        client_type.side_effect = lambda **kwargs: real_async_client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        result = asyncio.run(run())

    assert result.ok is False
    assert len(recorded) == 2
    assert {item["duration_ms"] for item in recorded} == {321}
    assert all(isinstance(item["started_at_ms"], int) for item in recorded)


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
    with patch("core.memory.everos.httpx.AsyncClient", autospec=True) as client_type:
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


@pytest.mark.parametrize(
    ("recorder", "expected"),
    [
        ({"state": "active", "reason": None}, {"state": "active", "reason": None}),
        (
            {"state": "degraded", "reason": "call_log_corrupt"},
            {"state": "degraded", "reason": "call_log_corrupt"},
        ),
        (
            {"state": "disabled", "reason": "writer_failures"},
            {"state": "disabled", "reason": "writer_failures"},
        ),
        (
            {"state": "future", "reason": "new_reason"},
            {"state": "degraded", "reason": "writer_failures"},
        ),
        (None, {"state": "degraded", "reason": "writer_failures"}),
    ],
)
def test_recorder_health_validates_the_closed_sidecar_projection(recorder, expected) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_health_envelope(recorder))

    with _sidecar_transport(handler):
        health = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).recorder_health())

    assert health == expected


def test_recorder_health_degrades_transport_and_invalid_json() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(503, content=b"unavailable"),
        ]
    )

    with _sidecar_transport(lambda _request: next(responses)):
        provider = EverOSPort(Path("/tmp/everos.sock"))
        assert asyncio.run(provider.recorder_health()) == {
            "state": "degraded",
            "reason": "writer_failures",
        }
        assert asyncio.run(provider.recorder_health()) == {
            "state": "degraded",
            "reason": "writer_failures",
        }


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

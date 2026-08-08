from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from core.memory.everos import (
    AddAck,
    EverOSPort,
    FlushPreSubmission,
    FlushRejected,
    FlushSucceeded,
    FlushUnknown,
    MemoryProviderFailure,
    MemoryProviderPreSubmissionFailure,
    ProviderAttachment,
    ProviderCapture,
)
from core.memory.types import MemoryProfile, MemoryProfileExplicitInfo, MemoryProfileTrait


PROJECT = "p-22222222222222222222222222222222"


def _sidecar_transport(handler):
    return patch("core.memory.everos.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handler))


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
                principal_id="owner-1",
                session_ref="src--one--e1",
                text="remember this",
                provider_timestamp_ms=1_725_000_001_234,
                project_ref=PROJECT,
            )
        )
        flushed = await provider.flush("src--one--e1", PROJECT)
        return ack, flushed

    with _sidecar_transport(handler):
        ack, flushed = asyncio.run(run())

    assert ack == AddAck(request_id="add-request", status="accumulated")
    assert flushed == FlushSucceeded(request_id="flush-request", status="extracted")

    assert requests == [
        (
            "/api/v2/memory/add",
            {
                "session_id": "src--one--e1",
                "app_id": "avibe",
                "project_id": PROJECT,
                "messages": [
                    {
                        "sender_id": "owner-1",
                        "role": "user",
                        "timestamp": 1_725_000_001_234,
                        "content": "remember this",
                    }
                ],
            },
        ),
        (
            "/api/v2/memory/flush",
            {"session_id": "src--one--e1", "app_id": "avibe", "project_id": PROJECT},
        ),
    ]


@pytest.mark.parametrize(
    "status_code,retryable",
    [
        # These responses reject the request itself and cannot recover on replay.
        (415, False),
        (400, False),
        (403, False),
        (404, False),
        (422, False),
        # Statuses that describe a condition a later attempt may find cleared.
        (408, True),
        (409, True),
        (423, True),
        (425, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_add_rejection_is_retryable_only_when_a_replay_could_succeed(
    status_code: int,
    retryable: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"code": "rejected"}})

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).add(
                ProviderCapture(
                    "owner-1",
                    "src--one--e1",
                    "remember this",
                    1_725_000_001_234,
                    PROJECT,
                )
            )
        return raised.value

    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.error == "memory_processing_failed"
    assert failure.retryable is retryable


def test_add_treats_missing_provider_configuration_as_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": {"code": "PROVIDER_NOT_CONFIGURED"}},
        )

    async def run() -> MemoryProviderFailure:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).add(
                ProviderCapture("owner", "session", "capture", 1, PROJECT)
            )
        return raised.value

    with _sidecar_transport(handler):
        failure = asyncio.run(run())

    assert failure.retryable is True


def test_add_connect_timeout_is_classified_as_pre_submission_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=request)

    async def run() -> None:
        with pytest.raises(MemoryProviderPreSubmissionFailure):
            await EverOSPort(Path("/tmp/everos.sock")).add(
                ProviderCapture("owner", "session", "capture", 1, PROJECT)
            )

    with _sidecar_transport(handler):
        asyncio.run(run())


def test_add_forwards_typed_workbench_attachments_without_reading_them() -> None:
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"status": "accumulated"}})

    capture = ProviderCapture(
        principal_id="owner-1",
        session_ref="src--one--e1",
        text="remember this diagram",
        provider_timestamp_ms=1_725_000_001_234,
        project_ref=PROJECT,
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


def test_write_routes_degrade_unusable_2xx_bodies_without_replaying_writes(caplog) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, content=b"not-json")

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        ack = await provider.add(ProviderCapture("owner", "session", "capture", 1, PROJECT))
        result = await provider.flush("session", PROJECT)
        return ack, result

    with _sidecar_transport(handler):
        ack, result = asyncio.run(run())

    assert ack == AddAck(request_id=None, status=None)
    assert result == FlushSucceeded(request_id=None, status=None)
    assert requests == ["/api/v2/memory/add", "/api/v2/memory/flush"]
    assert "add returned 2xx with an unusable response body" in caplog.text
    assert "flush returned 2xx with an unusable response body" in caplog.text


def test_write_routes_log_and_drop_unsupported_status_values(caplog) -> None:
    responses = iter(
        [
            httpx.Response(200, json={"data": {"status": "future-add"}}),
            httpx.Response(200, json={"data": {"status": "future-flush"}}),
        ]
    )

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        ack = await provider.add(ProviderCapture("owner", "session", "capture", 1, PROJECT))
        result = await provider.flush("session", PROJECT)
        return ack, result

    with _sidecar_transport(lambda _request: next(responses)):
        ack, result = asyncio.run(run())

    assert ack == AddAck(request_id=None, status=None)
    assert result == FlushSucceeded(request_id=None, status=None)
    assert "add returned an unsupported status value" in caplog.text
    assert "flush returned an unsupported status value" in caplog.text


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(400, json={"request_id": "bad-request", "error": {"code": "INVALID_INPUT"}}),
            FlushRejected("bad-request", "INVALID_INPUT", server_fault=False, retryable=False),
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
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).flush("session", PROJECT))

    assert result == expected


@pytest.mark.parametrize(
    ("failure_type", "expected"),
    [
        (httpx.ReadTimeout, FlushUnknown("timeout")),
        (httpx.ConnectError, FlushPreSubmission(reason="transport")),
    ],
)
def test_flush_maps_read_timeout_and_connection_failure(failure_type, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure_type("failed", request=request)

    with _sidecar_transport(handler):
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).flush("session", PROJECT))

    assert result == expected


def test_flush_maps_connect_timeout_to_pre_submission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=request)

    with _sidecar_transport(handler):
        result = asyncio.run(EverOSPort(Path("/tmp/everos.sock")).flush("session", PROJECT))

    assert result == FlushPreSubmission(reason="timeout")


def test_search_uses_public_search_only_and_maps_episode_and_nested_fact() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        payload = json.loads(request.content)
        assert payload["top_k"] == 2
        assert payload["include_profile"] is True
        assert payload["enable_llm_rerank"] is False
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
        return await provider.search("owner-1", PROJECT, "language", 2)

    with _sidecar_transport(handler):
        items = asyncio.run(run())

    assert paths == ["/api/v2/memory/search"]
    assert items[0].kind == "episode"
    assert items[0].text == "Preferred language\nThe owner uses Python."
    assert items[0].date == "2026-07-22"
    assert items[1].kind == "fact"
    assert items[1].text == "Uses Python for automation."


def test_profile_uses_search_and_reports_empty_profile_as_non_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/memory/search"
        assert json.loads(request.content)["query"] == "profile"
        return httpx.Response(200, json={"data": {"profiles": [], "episodes": []}})

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        return await provider.profile("owner-1", PROJECT)

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
        return httpx.Response(200, json={"status": "ok", "recorder": recorder})

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

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure):
            await EverOSPort(Path("/tmp/everos.sock")).add(
                ProviderCapture("owner-1", "src--one--e1", capture_canary, 1_725_000_001_234, PROJECT)
            )

    with _sidecar_transport(handler):
        asyncio.run(run())

    assert capture_canary not in caplog.text
    assert response_canary not in caplog.text

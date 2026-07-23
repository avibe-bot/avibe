from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from core.memory.everos import EverOSPort, MemoryProviderFailure, ProviderCapture


def _sidecar_transport(handler):
    return patch("core.memory.everos.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handler))


def test_ingest_uses_add_then_flush_and_preserves_provider_timestamp() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": {}})

    async def run() -> None:
        provider = EverOSPort(Path("/tmp/everos.sock"))
        await provider.ingest(
            ProviderCapture(
                principal_id="owner-1",
                session_ref="src--one--e1",
                text="remember this",
                provider_timestamp_ms=1_725_000_001_234,
            )
        )

    with _sidecar_transport(handler):
        asyncio.run(run())

    assert requests == [
        (
            "/api/v1/memory/add",
            {
                "session_id": "src--one--e1",
                "app_id": "avibe",
                "project_id": "personal",
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
            "/api/v1/memory/flush",
            {"session_id": "src--one--e1", "app_id": "avibe", "project_id": "personal"},
        ),
    ]


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
        return await provider.search("owner-1", "language", 2)

    with _sidecar_transport(handler):
        items = asyncio.run(run())

    assert paths == ["/api/v1/memory/search"]
    assert items[0].kind == "episode"
    assert items[0].text == "Preferred language\nThe owner uses Python."
    assert items[0].date == "2026-07-22"
    assert items[1].kind == "fact"
    assert items[1].text == "Uses Python for automation."


def test_profile_uses_search_and_reports_empty_profile_as_non_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memory/search"
        assert json.loads(request.content)["query"] == "profile"
        return httpx.Response(200, json={"data": {"profiles": [], "episodes": []}})

    async def run():
        provider = EverOSPort(Path("/tmp/everos.sock"))
        items = await provider.profile("owner-1")
        return items, provider.profile_empty_warning

    with _sidecar_transport(handler):
        items, warning = asyncio.run(run())

    assert items == ()
    assert warning is True


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
        return await EverOSPort(Path("/tmp/everos.sock")).profile("owner-1")

    with _sidecar_transport(handler):
        items = asyncio.run(run())

    assert items[0].kind == "profile"
    assert items[0].text == '{"language":"Python","timezone":"UTC"}'


def test_invalid_search_envelope_is_closed_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"episodes": "not-a-list"}})

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure) as raised:
            await EverOSPort(Path("/tmp/everos.sock")).search("owner-1", "x", 1)
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


def test_sidecar_failure_logs_never_contain_capture_or_response_canaries(caplog) -> None:
    capture_canary = "capture-canary-7d5d6b"
    response_canary = "response-canary-477ebd"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=response_canary.encode("utf-8"))

    async def run() -> None:
        with pytest.raises(MemoryProviderFailure):
            await EverOSPort(Path("/tmp/everos.sock")).ingest(
                ProviderCapture("owner-1", "src--one--e1", capture_canary, 1_725_000_001_234)
            )

    with _sidecar_transport(handler):
        asyncio.run(run())

    assert capture_canary not in caplog.text
    assert response_canary not in caplog.text

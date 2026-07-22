from __future__ import annotations

from pathlib import Path

from memory_poc.provider import EverOSClient


class _Response:
    status_code = 200

    @staticmethod
    def json() -> dict[str, str]:
        return {"status": "ok"}


class _Client:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    @staticmethod
    def request(*_args: object, **_kwargs: object) -> _Response:
        return _Response()


def test_health_accepts_the_public_non_data_envelope(monkeypatch) -> None:
    monkeypatch.setattr("memory_poc.provider.httpx.HTTPTransport", lambda **_kwargs: object())
    monkeypatch.setattr("memory_poc.provider.httpx.Client", _Client)

    EverOSClient(Path("/tmp/everos.sock")).health()


def test_search_uses_hybrid_to_receive_nested_atomic_facts(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _SearchResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, dict[str, list[object]]]:
            return {"data": {"episodes": []}}

    class _SearchClient(_Client):
        @staticmethod
        def request(*_args: object, **kwargs: object) -> _SearchResponse:
            calls.append(kwargs["json"])  # type: ignore[arg-type]
            return _SearchResponse()

    monkeypatch.setattr("memory_poc.provider.httpx.HTTPTransport", lambda **_kwargs: object())
    monkeypatch.setattr("memory_poc.provider.httpx.Client", _SearchClient)

    EverOSClient(Path("/tmp/everos.sock")).search(owner_id="owner", query="synthetic")

    assert calls == [
        {
            "user_id": "owner",
            "app_id": "avibe",
            "project_id": "personal",
            "query": "synthetic",
            "method": "hybrid",
            "top_k": 8,
            "include_profile": True,
            "enable_llm_rerank": False,
        }
    ]


def test_get_is_available_only_through_the_explicit_research_helper(monkeypatch) -> None:
    headers: list[dict[str, str]] = []

    class _GetResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, dict[str, list[object]]]:
            return {"data": {"episodes": []}}

    class _GetClient(_Client):
        @staticmethod
        def request(*_args: object, **kwargs: object) -> _GetResponse:
            headers.append(kwargs["headers"])  # type: ignore[arg-type]
            return _GetResponse()

    monkeypatch.setattr("memory_poc.provider.httpx.HTTPTransport", lambda **_kwargs: object())
    monkeypatch.setattr("memory_poc.provider.httpx.Client", _GetClient)

    client = EverOSClient(Path("/tmp/everos.sock"))
    client.research_diagnostic_get(owner_id="owner", memory_type="episode")

    assert not hasattr(client, "get")
    assert headers == [{"X-Memory-Poc-Phase": "research"}]


def test_client_records_redacted_public_http_shapes(monkeypatch) -> None:
    headers: list[dict[str, str]] = []

    class _ShapeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "code": 0,
                "data": {
                    "episodes": [
                        {
                            "user_id": "synthetic-owner",
                            "atomic_facts": [{"id": "fact-1", "content": "synthetic fact"}],
                        }
                    ]
                },
                "message": "not persisted",
            }

    class _ShapeClient(_Client):
        @staticmethod
        def request(*_args: object, **kwargs: object) -> _ShapeResponse:
            headers.append(kwargs["headers"])  # type: ignore[arg-type]
            return _ShapeResponse()

    monkeypatch.setattr("memory_poc.provider.httpx.HTTPTransport", lambda **_kwargs: object())
    monkeypatch.setattr("memory_poc.provider.httpx.Client", _ShapeClient)

    client = EverOSClient(Path("/tmp/everos.sock"))
    client.search(owner_id="owner", query="synthetic")

    assert headers == [{"X-Memory-Poc-Phase": "read"}]
    assert client.observed_http_shapes[0].request_keys == (
        "app_id",
        "enable_llm_rerank",
        "include_profile",
        "method",
        "project_id",
        "query",
        "top_k",
        "user_id",
    )
    assert client.observed_http_shapes[0].response_keys == ("code", "data", "message")
    assert client.observed_http_shapes[0].data_keys == ("episodes",)
    assert client.observed_http_shapes[0].closed_code == 0
    assert "data.episodes[].atomic_facts[].id:string" in client.observed_http_shapes[0].response_schema_paths
    assert "data.episodes[].atomic_facts[].content:string" in client.observed_http_shapes[0].response_schema_paths
    assert "synthetic fact" not in " ".join(client.observed_http_shapes[0].response_schema_paths)

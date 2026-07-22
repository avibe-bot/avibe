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

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from core.memory import everos
from core.memory import sidecar
from core.memory.sidecar import _processing_healthy_from_child_environment, _request_rejection
from core.memory.types import MemoryProfilePageSource


PROJECT = "p-22222222222222222222222222222222"


def test_sidecar_server_bounds_graceful_shutdown(monkeypatch, tmp_path: Path) -> None:
    import uvicorn

    captured: dict[str, object] = {}

    class _App:
        def middleware(self, _kind: str):
            return lambda handler: handler

        def post(self, _path: str):
            return lambda handler: handler

    class _FactoryModule:
        @staticmethod
        def create_app() -> _App:
            return _App()

    class _Config:
        def __init__(self, _app, **kwargs):
            captured.update(kwargs)

    class _Server:
        def __init__(self, _config):
            return None

        def run(self) -> None:
            return None

    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.1")
    monkeypatch.setattr(sidecar.importlib, "import_module", lambda _module: _FactoryModule())
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))

    sidecar.serve(tmp_path / "everos.sock")

    assert captured["timeout_graceful_shutdown"] == 1


def test_sidecar_guard_allows_derived_principals_and_memory_scope() -> None:
    principal = "u-11111111111111111111111111111111"
    payload = (
        b'{"session_id":"src--one--e1","app_id":"avibe","project_id":"'
        + PROJECT.encode()
        + b'",'
        b'"messages":[{"sender_id":"u-11111111111111111111111111111111","role":"user","timestamp":1725000001234,'
        b'"content":"text"}]}'
    )

    search = json.dumps(
        {
            "user_id": principal,
            "app_id": "avibe",
            "project_id": PROJECT,
            "query": "profile",
            "method": "hybrid",
            "top_k": 1,
            "include_profile": True,
            "enable_llm_rerank": False,
        }
    ).encode()
    get = json.dumps(
        {
            "user_id": "u-22222222222222222222222222222222",
            "app_id": "avibe",
            "project_id": PROJECT,
            "memory_type": "profile",
            "page": 1,
            "page_size": 20,
            "sort_by": "timestamp",
            "sort_order": "desc",
        }
    ).encode()

    assert _request_rejection("GET", "/health", b"") is None
    assert _request_rejection("POST", "/api/v2/memory/add", payload) is None
    assert _request_rejection("POST", "/api/v2/memory/search", search) is None
    assert _request_rejection("POST", "/api/v2/memory/get", get) is None
    assert _request_rejection("POST", "/api/v2/memory/add", payload.replace(principal.encode(), b"owner-1")) == "add"
    assert _request_rejection("POST", "/api/v2/memory/add", payload.replace(PROJECT.encode(), b"personal")) == "add"
    assert _request_rejection("POST", "/api/v1/memory/add", payload) == "route"
    assert _request_rejection("GET", "/api/v2/memory/search", b"") == "route"
    assert _request_rejection("POST", "/unrelated", b"{}") == "route"


def test_sidecar_guard_allows_only_exact_profile_report_schema() -> None:
    payload = {
        "language": "en",
        "generated_at": "2026-08-03T05:12:30Z",
        "profile": {
            "summary": "Prefers concise updates.",
            "explicit_info": [
                {
                    "description": "Uses Python.",
                    "category": "technical",
                    "evidence": "Project notes.",
                }
            ],
            "implicit_traits": [
                {
                    "description": "May prefer checklists.",
                    "trait": "methodical",
                    "basis": "Requests ordered plans.",
                    "evidence": "Planning history.",
                }
            ],
            "updated_at": "2026-08-02T10:30:00Z",
        },
    }

    assert _request_rejection("POST", "/avibe/v1/profile-report", json.dumps(payload).encode()) is None
    assert (
        _request_rejection(
            "POST",
            "/avibe/v1/profile-report",
            json.dumps({**payload, "api_key": "must-not-cross-the-uds"}).encode(),
        )
        == "profile-report"
    )
    assert (
        _request_rejection(
            "POST",
            "/avibe/v1/profile-report",
            json.dumps({**payload, "language": "fr"}).encode(),
        )
        == "profile-report"
    )
    malformed = {**payload, "profile": {"summary": "only this field"}}
    assert _request_rejection("POST", "/avibe/v1/profile-report", json.dumps(malformed).encode()) == "profile-report"
    malformed_unicode = {
        **payload,
        "profile": {
            **payload["profile"],
            "summary": "\ud800",
        },
    }
    assert (
        _request_rejection("POST", "/avibe/v1/profile-report", json.dumps(malformed_unicode).encode())
        == "profile-report"
    )


def test_sidecar_report_route_builds_generator_from_child_environment_only(monkeypatch, tmp_path: Path) -> None:
    import uvicorn

    handlers: dict[str, object] = {}
    received: dict[str, object] = {}

    class _App:
        def middleware(self, _kind: str):
            return lambda handler: handler

        def post(self, path: str):
            def register(handler):
                handlers[path] = handler
                return handler

            return register

    class _FactoryModule:
        @staticmethod
        def create_app() -> _App:
            return _App()

    class _Config:
        def __init__(self, _app, **_kwargs):
            return None

    class _Server:
        def __init__(self, _config):
            return None

        def run(self) -> None:
            return None

    class _Generator:
        def __init__(self, **kwargs) -> None:
            received.update(kwargs)

        async def generate(self, profile, language, generated_at) -> MemoryProfilePageSource:
            received["profile"] = profile
            received["language"] = language
            received["generated_at"] = generated_at
            return MemoryProfilePageSource(
                index_html="<!doctype html><html><head></head><body></body></html>",
                styles_css="body { margin: 0; }",
            )

    class _Request:
        async def json(self):
            return {
                "language": "zh",
                "generated_at": "2026-08-03T05:12:30Z",
                "profile": {
                    "summary": "喜欢简洁更新。",
                    "explicit_info": [],
                    "implicit_traits": [],
                    "updated_at": "2026-08-02T10:30:00Z",
                },
            }

    monkeypatch.setattr(sidecar, "ProfileReportGenerator", _Generator)
    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.1")
    monkeypatch.setattr(sidecar.importlib, "import_module", lambda _module: _FactoryModule())
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    monkeypatch.setenv("EVEROS_LLM__BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("EVEROS_LLM__MODEL", "chat-model")
    monkeypatch.setenv("EVEROS_LLM__API_KEY", "llm-secret")

    sidecar.serve(tmp_path / "everos.sock")
    response = asyncio.run(handlers["/avibe/v1/profile-report"](_Request()))

    assert json.loads(response.body) == {
        "status": "ok",
        "source": {
            "index_html": "<!doctype html><html><head></head><body></body></html>",
            "styles_css": "body { margin: 0; }",
        },
    }
    assert {
        "base_url": received["base_url"],
        "model": received["model"],
        "api_key": received["api_key"],
        "language": received["language"],
        "generated_at": received["generated_at"],
    } == {
        "base_url": "https://llm.example.test/v1",
        "model": "chat-model",
        "api_key": "llm-secret",
        "language": "zh",
        "generated_at": "2026-08-03T05:12:30Z",
    }
    assert received["profile"].summary == "喜欢简洁更新。"


def test_sidecar_report_route_accepts_json_through_real_fastapi(monkeypatch, tmp_path: Path) -> None:
    from fastapi import FastAPI
    import uvicorn

    captured: dict[str, object] = {}

    class _FactoryModule:
        @staticmethod
        def create_app() -> FastAPI:
            return FastAPI()

    class _Config:
        def __init__(self, app, **_kwargs) -> None:
            captured["app"] = app

    class _Server:
        def __init__(self, _config) -> None:
            return None

        def run(self) -> None:
            return None

    class _Generator:
        def __init__(self, **_kwargs) -> None:
            return None

        async def generate(self, _profile, _language, _generated_at) -> MemoryProfilePageSource:
            return MemoryProfilePageSource(
                index_html="<!doctype html><html><head></head><body></body></html>",
                styles_css="body { margin: 0; }",
            )

    monkeypatch.setattr(sidecar, "ProfileReportGenerator", _Generator)
    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.1")
    monkeypatch.setattr(sidecar.importlib, "import_module", lambda _module: _FactoryModule())
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))

    sidecar.serve(tmp_path / "everos.sock")

    async def request_report() -> httpx.Response:
        transport = httpx.ASGITransport(app=captured["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://memory-sidecar") as client:
            return await client.post(
                "/avibe/v1/profile-report",
                json={
                    "language": "en",
                    "generated_at": "2026-08-03T05:12:30Z",
                    "profile": {
                        "summary": "Prefers concise updates.",
                        "explicit_info": [],
                        "implicit_traits": [],
                        "updated_at": "2026-08-02T10:30:00Z",
                    },
                },
            )

    response = asyncio.run(request_report())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "source": {
            "index_html": "<!doctype html><html><head></head><body></body></html>",
            "styles_css": "body { margin: 0; }",
        },
    }


def test_sidecar_guard_allows_workbench_attachment_file_uri_only(tmp_path: Path) -> None:
    attachments_root = tmp_path / "attachments" / "avibe"
    asset = attachments_root / "session-1" / "diagram.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    payload = {
        "session_id": "src--one--e1",
        "app_id": "avibe",
        "project_id": PROJECT,
        "messages": [
            {
                "sender_id": "u-11111111111111111111111111111111",
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": [
                    {"type": "text", "text": "diagram"},
                    {
                        "type": "image",
                        "name": "diagram.png",
                        "uri": asset.as_uri(),
                        "ext": "png",
                    },
                ],
            }
        ],
    }

    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/add",
            json.dumps(payload).encode(),
            attachments_root=attachments_root,
        )
        is None
    )
    payload["messages"][0]["content"][1]["uri"] = (tmp_path / "outside.png").as_uri()
    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/add",
            json.dumps(payload).encode(),
            attachments_root=attachments_root,
        )
        == "add"
    )

    link = attachments_root / "session-1" / "linked.png"
    link.symlink_to(asset)
    payload["messages"][0]["content"][1]["uri"] = link.as_uri()
    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/add",
            json.dumps(payload).encode(),
            attachments_root=attachments_root,
        )
        == "add"
    )


def test_sidecar_guard_rejects_an_extension_the_runtime_cannot_parse(tmp_path: Path) -> None:
    attachments_root = tmp_path / "attachments" / "avibe"
    asset = attachments_root / "session-1" / "export.json"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"{}")
    payload = {
        "session_id": "src--one--e1",
        "app_id": "avibe",
        "project_id": PROJECT,
        "messages": [
            {
                "sender_id": "u-11111111111111111111111111111111",
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": [
                    {
                        "type": "doc",
                        "name": "export.json",
                        "uri": asset.as_uri(),
                        "ext": "json",
                    },
                ],
            }
        ],
    }

    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/add",
            json.dumps(payload).encode(),
            attachments_root=attachments_root,
        )
        == "add"
    )


def test_processing_probe_builds_the_adapter_from_child_environment_only(monkeypatch) -> None:
    received: dict[str, object] = {}

    class _Provider:
        def __init__(self, socket_path, **kwargs) -> None:
            received["socket_path"] = socket_path
            received.update(kwargs)

        async def processing_healthy(self) -> bool:
            return True

    monkeypatch.setenv("EVEROS_LLM__BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("EVEROS_LLM__MODEL", "chat-model")
    monkeypatch.setenv("EVEROS_LLM__API_KEY", "llm-secret")
    monkeypatch.setenv("EVEROS_EMBEDDING__BASE_URL", "https://embed.example.test/v1")
    monkeypatch.setenv("EVEROS_EMBEDDING__MODEL", "embed-model")
    monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", "embedding-secret")
    monkeypatch.setattr(everos, "EverOSPort", _Provider)

    assert _processing_healthy_from_child_environment() is True
    assert str(received.pop("socket_path")) == "/nonexistent-memory-sidecar.sock"
    assert received == {
        "llm_base_url": "https://llm.example.test/v1",
        "llm_model": "chat-model",
        "llm_api_key": "llm-secret",
        "embedding_base_url": "https://embed.example.test/v1",
        "embedding_model": "embed-model",
        "embedding_api_key": "embedding-secret",
    }

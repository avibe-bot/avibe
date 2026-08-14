from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory import everos
from core.memory import sidecar
from core.memory.sidecar import (
    _RecorderHealthProjection,
    _processing_healthy_from_child_environment,
    _request_rejection,
)


PROJECT = "default"
LEGACY_PROJECT = "p-22222222222222222222222222222222"
SESSION_ID = f"src--{'1' * 64}--e1"


def test_sidecar_server_bounds_graceful_shutdown(monkeypatch, tmp_path: Path) -> None:
    import uvicorn

    captured: dict[str, object] = {}

    class _App:
        def middleware(self, _kind: str):
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

    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.3")
    monkeypatch.setattr(sidecar, "install_error_scrubbers", lambda: None)
    monkeypatch.setattr(sidecar.importlib, "import_module", lambda _module: _FactoryModule())
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))

    sidecar.serve(tmp_path / "everos.sock")

    assert captured["timeout_graceful_shutdown"] == 1


def test_sidecar_rejects_artifact_before_everos_can_persist_diagnostics(
    monkeypatch, tmp_path: Path
) -> None:
    imported = False

    def scrubbers_fail() -> None:
        raise RuntimeError("incompatible scrubber")

    def import_app(_module: str):
        nonlocal imported
        imported = True
        raise AssertionError("EverOS app must not load after scrubber rejection")

    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.3")
    monkeypatch.setattr(sidecar, "install_error_scrubbers", scrubbers_fail)
    monkeypatch.setattr(sidecar.importlib, "import_module", import_app)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))

    with pytest.raises(RuntimeError, match="incompatible scrubber"):
        sidecar.serve(tmp_path / "everos.sock")

    assert imported is False


def test_sidecar_prepares_recorder_before_import_and_wraps_existing_lifespan(
    monkeypatch, tmp_path: Path
) -> None:
    import uvicorn

    events: list[object] = []
    state = {"everos": "state"}

    @asynccontextmanager
    async def original_lifespan(_app):
        events.append("everos-start")
        yield state
        events.append("everos-stop")

    class _App:
        def __init__(self) -> None:
            self.router = SimpleNamespace(lifespan_context=original_lifespan)
            self.guard = None

        def middleware(self, _kind: str):
            def register(handler):
                self.guard = handler
                return handler

            return register

    app = _App()

    class _FactoryModule:
        @staticmethod
        def create_app() -> _App:
            events.append("create-app")
            return app

    class _Handle:
        def start(self) -> None:
            events.append("recorder-start")

        async def close(self, *, timeout: float) -> None:
            events.append(("recorder-close", timeout))

        @contextmanager
        def boundary_request(self):
            events.append("boundary-enter")
            try:
                yield
            finally:
                events.append("boundary-exit")

    handle = _Handle()

    def prepare(path: Path):
        events.append(("prepare", path))
        return handle

    class _Config:
        def __init__(self, _app, **_kwargs):
            return None

    class _Server:
        def __init__(self, _config):
            return None

        def run(self) -> None:
            async def exercise() -> None:
                async with app.router.lifespan_context(app) as yielded:
                    assert yielded is state
                    events.append("inside")

            asyncio.run(exercise())

    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.3")
    monkeypatch.setattr(
        sidecar,
        "install_error_scrubbers",
        lambda: events.append("install-error-scrubbers"),
    )
    monkeypatch.setattr(sidecar, "prepare_call_recorder", prepare)
    monkeypatch.setattr(
        sidecar.importlib,
        "import_module",
        lambda _module: (events.append("import-app"), _FactoryModule())[1],
    )
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    db_path = tmp_path / "call-log.db"
    monkeypatch.setenv("AVIBE_MEMORY_CALL_LOG_DB", str(db_path))

    sidecar.serve(tmp_path / "everos.sock")

    assert events == [
        "install-error-scrubbers",
        ("prepare", db_path),
        "import-app",
        "create-app",
        "recorder-start",
        "everos-start",
        "inside",
        "everos-stop",
        ("recorder-close", 1.0),
    ]

    class _Request:
        def __init__(self, path: str, payload: dict[str, object]) -> None:
            self.method = "POST"
            self.url = SimpleNamespace(path=path)
            self._body = json.dumps(payload).encode()

        async def body(self) -> bytes:
            return self._body

    search_payload = {
        "user_id": "u-11111111111111111111111111111111",
        "app_id": "avibe",
        "project_id": PROJECT,
        "query": "profile",
        "method": "keyword",
        "top_k": 1,
        "include_profile": False,
        "enable_llm_rerank": False,
        "filters": {"session_id": SESSION_ID},
    }
    get_payload = {
        "user_id": "u-11111111111111111111111111111111",
        "app_id": "avibe",
        "project_id": "default",
        "memory_type": "profile",
        "page": 1,
        "page_size": 1,
    }

    async def exercise_guard() -> None:
        async def call_next(request):
            return request.url.path

        assert (
            await app.guard(
                _Request("/api/v2/memory/search", search_payload), call_next
            )
            == "/api/v2/memory/search"
        )
        assert (
            await app.guard(_Request("/api/v2/memory/get", get_payload), call_next)
            == "/api/v2/memory/get"
        )

    asyncio.run(exercise_guard())
    assert "boundary-enter" not in events


def test_sidecar_projects_recorder_state_through_existing_health_response() -> None:
    async def app(scope, _receive, send) -> None:
        assert scope["path"] == "/health"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"status":"ok","version":"1.2.3"}',
            }
        )

    class _Handle:
        health = {"state": "degraded", "reason": "call_log_corrupt"}

    messages: list[dict] = []

    async def run() -> None:
        async def send(message: dict) -> None:
            messages.append(message)

        projection = _RecorderHealthProjection(app, _Handle())
        await projection(
            {"type": "http", "method": "GET", "path": "/health"},
            None,
            send,
        )

    asyncio.run(run())

    body = json.loads(messages[1]["body"])
    assert body == {
        "status": "ok",
        "version": "1.2.3",
        "recorder": {"state": "degraded", "reason": "call_log_corrupt"},
    }


def test_sidecar_projects_disabled_recorder_when_capture_is_off() -> None:
    async def app(_scope, _receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"status":"ok"}'})

    messages: list[dict] = []

    async def run() -> None:
        async def send(message: dict) -> None:
            messages.append(message)

        await _RecorderHealthProjection(app, None)(
            {"type": "http", "method": "GET", "path": "/health"},
            None,
            send,
        )

    asyncio.run(run())

    assert json.loads(messages[1]["body"])["recorder"] == {
        "state": "disabled",
        "reason": None,
    }


def test_sidecar_guard_allows_derived_principals_and_memory_scope() -> None:
    principal = "u-11111111111111111111111111111111"
    payload = json.dumps(
        {
            "session_id": SESSION_ID,
            "app_id": "avibe",
            "project_id": PROJECT,
            "messages": [
                {
                    "sender_id": principal,
                    "role": "user",
                    "timestamp": 1_725_000_001_234,
                    "content": "text",
                }
            ],
        }
    ).encode()

    search = json.dumps(
        {
            "user_id": principal,
            "app_id": "avibe",
            "project_id": PROJECT,
            "query": "profile",
            "method": "vector",
            "top_k": 1,
            "include_profile": False,
            "enable_llm_rerank": False,
            "filters": {"session_id": SESSION_ID},
        }
    ).encode()
    get = json.dumps(
        {
            "user_id": "u-22222222222222222222222222222222",
            "app_id": "avibe",
            "project_id": "default",
            "memory_type": "profile",
            "page": 1,
            "page_size": 1,
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


def test_sidecar_guard_rejects_untrusted_search_scope_and_unbudgeted_agentic() -> None:
    search = {
        "user_id": "u-11111111111111111111111111111111",
        "app_id": "avibe",
        "project_id": PROJECT,
        "query": "memory",
        "method": "keyword",
        "top_k": 8,
        "include_profile": True,
        "enable_llm_rerank": False,
    }

    for invalid in (
        {**search, "method": "agentic"},
        {**search, "filters": {"session_id": "raw-session"}},
        {**search, "filters": {"project_id": PROJECT}},
    ):
        assert (
            _request_rejection(
                "POST",
                "/api/v2/memory/search",
                json.dumps(invalid).encode(),
            )
            == "search"
        )


def test_sidecar_guard_allows_workbench_attachment_file_uri_only(tmp_path: Path) -> None:
    attachments_root = tmp_path / "attachments" / "avibe"
    asset = attachments_root / "session-1" / "diagram.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    payload = {
        "session_id": SESSION_ID,
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
        "session_id": SESSION_ID,
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
    monkeypatch.setenv("EVEROS_RERANK__BASE_URL", "https://rerank.example.test/v1/inference")
    monkeypatch.setenv("EVEROS_RERANK__MODEL", "rerank-model")
    monkeypatch.setenv("EVEROS_RERANK__API_KEY", "rerank-secret")
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
        "rerank_base_url": "https://rerank.example.test/v1/inference",
        "rerank_model": "rerank-model",
        "rerank_api_key": "rerank-secret",
    }

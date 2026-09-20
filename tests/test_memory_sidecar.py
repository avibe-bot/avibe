from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from avibe_memory import everos
from avibe_memory.everos import MULTIMODAL_EXPLICIT_ENV
from avibe_memory import sidecar
from avibe_memory.sidecar import (
    _processing_healthy_from_child_environment,
    _request_rejection,
)


PROJECT = "default"
LEGACY_PROJECT = "p-22222222222222222222222222222222"
SESSION_ID = f"src--{'1' * 64}--e1"


def _agentic_search_body() -> bytes:
    return json.dumps(
        {
            "user_id": "u-11111111111111111111111111111111",
            "app_id": "avibe",
            "project_id": PROJECT,
            "query": "connect the clues",
            "method": "agentic",
            "top_k": 8,
            "include_profile": True,
            "enable_llm_rerank": False,
        }
    ).encode()


def test_sidecar_server_bounds_graceful_shutdown(monkeypatch, tmp_path: Path) -> None:
    import uvicorn

    captured: dict[str, object] = {}
    round_logger = logging.getLogger("everos.memory.search.agentic")
    original_round_logger_level = round_logger.level
    round_logger.setLevel(logging.ERROR)

    class _App:
        def middleware(self, _kind: str):
            return lambda handler: handler

    class _FactoryModule:
        @staticmethod
        def create_app() -> _App:
            return _App()

    class _Config:
        def __init__(self, configured_app, **kwargs):
            captured["app"] = configured_app
            captured.update(kwargs)

    class _Server:
        def __init__(self, _config):
            return None

        def run(self) -> None:
            captured["round_logger_level"] = round_logger.level
            return None

    monkeypatch.setattr(sidecar, "version", lambda _package: "1.2.3")
    monkeypatch.setattr(sidecar, "install_error_scrubbers", lambda: None)
    monkeypatch.setattr(sidecar.importlib, "import_module", lambda _module: _FactoryModule())
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))

    try:
        sidecar.serve(tmp_path / "everos.sock")
    finally:
        round_logger.setLevel(original_round_logger_level)

    assert captured["timeout_graceful_shutdown"] == 1
    assert captured["round_logger_level"] == logging.INFO
    assert round_logger.level == original_round_logger_level
    assert isinstance(captured["app"], sidecar._AgenticDeadlineProjection)
    assert not hasattr(sidecar, "_RecorderHealthProjection")


def test_agentic_deadline_projection_cancels_downstream_and_preserves_round() -> None:
    downstream_cancelled = asyncio.Event()
    sent: list[dict] = []
    request_messages = [
        {
            "type": "http.request",
            "body": _agentic_search_body(),
            "more_body": False,
        }
    ]

    async def receive():
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def capture(message):
        sent.append(message)

    async def downstream(_scope, downstream_receive, _send):
        assert (await downstream_receive())["body"] == _agentic_search_body()
        round_state = sidecar._AGENTIC_ROUND_STATE.get()
        assert round_state is not None
        round_state["round"] = "round2"
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            downstream_cancelled.set()
            raise

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v2/memory/search",
        "headers": [
            (sidecar._AGENTIC_TIMEOUT_HEADER.lower().encode(), b"0.001"),
        ],
    }

    asyncio.run(
        sidecar._AgenticDeadlineProjection(downstream)(scope, receive, capture)
    )

    assert downstream_cancelled.is_set()
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 504
    headers = dict(sent[0]["headers"])
    assert headers[sidecar._AGENTIC_ROUND_HEADER.lower().encode()] == b"round2"
    assert json.loads(sent[1]["body"]) == {"detail": "memory_request_timed_out"}


def test_agentic_deadline_projection_returns_allowlisted_round_header() -> None:
    sent: list[dict] = []
    request_messages = [
        {
            "type": "http.request",
            "body": _agentic_search_body(),
            "more_body": False,
        }
    ]
    round_logger = logging.getLogger("everos.memory.search.agentic")
    round_handler = sidecar._AgenticRoundHandler()
    original_round_logger_level = round_logger.level
    round_logger.setLevel(logging.INFO)
    round_logger.addHandler(round_handler)

    async def receive():
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def capture(message):
        sent.append(message)

    async def downstream(_scope, downstream_receive, send):
        assert (await downstream_receive())["body"] == _agentic_search_body()
        round_logger.info(
            {
                "event": "agentic_search_decision",
                "round": "round2",
                "query": "private query must not be projected",
            }
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"data":{}}'})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v2/memory/search",
        "headers": [
            (sidecar._AGENTIC_TIMEOUT_HEADER.lower().encode(), b"1"),
        ],
    }

    try:
        asyncio.run(
            sidecar._AgenticDeadlineProjection(downstream)(
                scope,
                receive,
                capture,
            )
        )
    finally:
        round_logger.removeHandler(round_handler)
        round_logger.setLevel(original_round_logger_level)

    headers = dict(sent[0]["headers"])
    assert headers[sidecar._AGENTIC_ROUND_HEADER.lower().encode()] == b"round2"
    assert all(b"private query" not in value for value in headers.values())


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


def test_sidecar_installs_canonical_scrubber_without_provider_call_state(
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

    class _Config:
        def __init__(self, projected_app, **_kwargs):
            assert isinstance(projected_app, sidecar._AgenticDeadlineProjection)

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
    monkeypatch.setattr(
        sidecar.importlib,
        "import_module",
        lambda _module: (events.append("import-app"), _FactoryModule())[1],
    )
    monkeypatch.setattr(sidecar.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("AVIBE_MEMORY_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    sidecar.serve(tmp_path / "everos.sock")

    assert events == [
        "install-error-scrubbers",
        "import-app",
        "create-app",
        "everos-start",
        "inside",
        "everos-stop",
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
            "user_id": principal,
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
    assistant_owner = f"{principal}-agent".encode()
    assert _request_rejection(
        "POST",
        "/api/v2/memory/add",
        payload.replace(principal.encode(), assistant_owner),
    ) is None
    assert _request_rejection(
        "POST",
        "/api/v2/memory/search",
        search.replace(principal.encode(), assistant_owner),
    ) is None
    assert _request_rejection(
        "POST",
        "/api/v2/memory/get",
        get.replace(principal.encode(), assistant_owner),
    ) is None
    assert _request_rejection("POST", "/api/v2/memory/add", payload.replace(principal.encode(), b"owner-1")) == "add"
    assert _request_rejection("POST", "/api/v2/memory/add", payload.replace(PROJECT.encode(), b"personal")) == "add"
    assert _request_rejection("POST", "/api/v1/memory/add", payload) == "route"
    assert _request_rejection("GET", "/api/v2/memory/search", b"") == "route"
    assert _request_rejection("POST", "/unrelated", b"{}") == "route"


@pytest.mark.parametrize("sender_name", ["用户", "User", "小王 Élodie 🌱", "名" * 128])
@pytest.mark.parametrize("owner_suffix", ["", "-agent"])
@pytest.mark.parametrize("structured_content", [False, True])
def test_sidecar_guard_accepts_sender_name_capture_contract(
    sender_name: str, owner_suffix: str, structured_content: bool
) -> None:
    payload = {
        "session_id": SESSION_ID,
        "app_id": "avibe",
        "project_id": PROJECT,
        "messages": [
            {
                "sender_id": f"u-{'1' * 32}{owner_suffix}",
                "sender_name": sender_name,
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": [{"type": "text", "text": "测试"}] if structured_content else "测试",
            }
        ],
    }
    assert _request_rejection(
        "POST", "/api/v2/memory/add", json.dumps(payload).encode()
    ) is None


@pytest.mark.parametrize("sender_name", [None, False, 12, [], {}, "", "  ", "名" * 129])
def test_sidecar_guard_rejects_invalid_sender_name(sender_name: object) -> None:
    payload = {
        "session_id": SESSION_ID,
        "app_id": "avibe",
        "project_id": PROJECT,
        "messages": [
            {
                "sender_id": f"u-{'1' * 32}",
                "sender_name": sender_name,
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": "text",
            }
        ],
    }
    assert _request_rejection(
        "POST", "/api/v2/memory/add", json.dumps(payload).encode()
    ) == "add"


@pytest.mark.parametrize("change", [
    {"unexpected": "field"},
    {"sender_id": "owner-1"},
    {"role": "assistant"},
    {"timestamp": True},
])
def test_sidecar_sender_name_does_not_relax_capture_scope(change: dict) -> None:
    payload = {
        "session_id": SESSION_ID,
        "app_id": "avibe",
        "project_id": PROJECT,
        "messages": [
            {
                "sender_id": f"u-{'1' * 32}",
                "sender_name": "用户",
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": "text",
                **change,
            }
        ],
    }
    assert _request_rejection(
        "POST", "/api/v2/memory/add", json.dumps(payload).encode()
    ) == "add"


def test_sidecar_guard_accepts_large_everos_request_body() -> None:
    payload = {
        "session_id": SESSION_ID,
        "app_id": "avibe",
        "project_id": PROJECT,
        "messages": [
            {
                "sender_id": "u-11111111111111111111111111111111",
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": "remember " * 10_000,
            }
        ],
    }
    body = json.dumps(payload).encode()

    assert len(body) > 64 * 1024
    assert _request_rejection("POST", "/api/v2/memory/add", body) is None


def test_sidecar_guard_accepts_agentic_but_rejects_untrusted_search_scope() -> None:
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

    agentic_body = json.dumps({**search, "method": "agentic"}).encode()
    assert _request_rejection("POST", "/api/v2/memory/search", agentic_body) is None
    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/search",
            json.dumps({**search, "top_k": 100}).encode(),
        )
        is None
    )
    assert (
        _request_rejection(
            "POST",
            "/api/v2/memory/search",
            json.dumps({**search, "top_k": 101}).encode(),
        )
        == "search"
    )
    assert (
        sidecar._agentic_request_timeout(
            "/api/v2/memory/search",
            agentic_body,
            {sidecar._AGENTIC_TIMEOUT_HEADER: "30"},
        )
        == 30.0
    )
    assert sidecar._agentic_request_timeout(
        "/api/v2/memory/search", agentic_body, {}
    ) is False
    assert sidecar._agentic_request_timeout(
        "/api/v2/memory/search",
        agentic_body,
        {sidecar._AGENTIC_TIMEOUT_HEADER: "30.1"},
    ) is False

    for invalid in (
        {**search, "filters": {"session_id": "raw-session"}},
        {**search, "filters": {"project_id": PROJECT}},
        {**search, "enable_llm_rerank": True},
    ):
        assert (
            _request_rejection(
                "POST",
                "/api/v2/memory/search",
                json.dumps(invalid).encode(),
            )
            == "search"
        )


def test_sidecar_guard_keeps_profile_exact_and_uses_everos_episode_page_limit() -> None:
    principal = "u-11111111111111111111111111111111"
    profile = {
        "user_id": principal,
        "app_id": "avibe",
        "project_id": "default",
        "memory_type": "profile",
        "page": 1,
        "page_size": 1,
    }
    episode = {
        "user_id": principal,
        "app_id": "avibe",
        "project_id": "notes",
        "memory_type": "episode",
        "page": 2,
        "page_size": 100,
        "sort_by": "timestamp",
        "sort_order": "desc",
    }

    assert _request_rejection("POST", "/api/v2/memory/get", json.dumps(profile).encode()) is None
    assert _request_rejection("POST", "/api/v2/memory/get", json.dumps(episode).encode()) is None

    invalid_profiles = (
        {**profile, "project_id": "notes"},
        {**profile, "page_size": 2},
        {**profile, "sort_by": "timestamp"},
    )
    invalid_episodes = (
        {**episode, "agent_id": principal},
        {**episode, "project_id": "all"},
        {**episode, "project_id": "p-" + "a" * 32},
        {**episode, "page": 0},
        {**episode, "page_size": 101},
        {**episode, "sort_by": "updated_at"},
        {**episode, "sort_order": "asc"},
        {**episode, "filters": {}},
    )
    for invalid in (*invalid_profiles, *invalid_episodes):
        assert (
            _request_rejection(
                "POST",
                "/api/v2/memory/get",
                json.dumps(invalid).encode(),
            )
            == "get"
        )


def test_sidecar_guard_allows_pinned_attachment_file_uri_only(tmp_path: Path) -> None:
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
    monkeypatch.setenv("EVEROS_MULTIMODAL__BASE_URL", "https://vision.example.test/v1")
    monkeypatch.setenv("EVEROS_MULTIMODAL__MODEL", "vision-model")
    monkeypatch.setenv("EVEROS_MULTIMODAL__API_KEY", "vision-secret")
    monkeypatch.setenv(MULTIMODAL_EXPLICIT_ENV, "1")
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
        "rerank_provider": None,
        "multimodal_base_url": "https://vision.example.test/v1",
        "multimodal_model": "vision-model",
        "multimodal_api_key": "vision-secret",
    }


def test_processing_probe_omits_legacy_multimodal_fallback_without_explicit_marker(
    monkeypatch,
) -> None:
    received: dict[str, object] = {}

    class _Provider:
        def __init__(self, _socket_path, **kwargs) -> None:
            received.update(kwargs)

        async def processing_healthy(self) -> bool:
            return True

    monkeypatch.setenv("EVEROS_LLM__BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("EVEROS_LLM__MODEL", "text-model")
    monkeypatch.setenv("EVEROS_LLM__API_KEY", "llm-secret")
    monkeypatch.setenv("EVEROS_EMBEDDING__BASE_URL", "https://embed.example.test/v1")
    monkeypatch.setenv("EVEROS_EMBEDDING__MODEL", "embed-model")
    monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", "embedding-secret")
    monkeypatch.setenv("EVEROS_MULTIMODAL__BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("EVEROS_MULTIMODAL__MODEL", "text-model")
    monkeypatch.setenv("EVEROS_MULTIMODAL__API_KEY", "llm-secret")
    monkeypatch.setattr(everos, "EverOSPort", _Provider)

    assert _processing_healthy_from_child_environment() is True
    assert not any(key.startswith("multimodal_") for key in received)

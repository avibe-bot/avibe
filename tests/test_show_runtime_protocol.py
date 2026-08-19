import asyncio
from types import SimpleNamespace

import httpx
import pytest

import core.show_runtime as show_runtime
from core.show_runtime import (
    SHOW_RUNTIME_BASE_HEADER,
    SHOW_RUNTIME_CONTEXT_HEADER,
    SHOW_RUNTIME_PROTOCOL_HEADER,
    ShowRuntimeContext,
    ShowRuntimeContextCapability,
    ShowRuntimeManager,
    ShowRuntimeProtocolEnvelope,
    ShowRuntimeResponseTooLarge,
    ShowRuntimeResult,
)


def _manager(tmp_path) -> ShowRuntimeManager:
    return ShowRuntimeManager(
        command="/bin/echo",
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
    )


class _CapabilityClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404), ShowRuntimeContextCapability.UNSUPPORTED),
        (
            httpx.Response(200, json={"protocol": 1, "features": []}),
            ShowRuntimeContextCapability.UNSUPPORTED,
        ),
        (
            httpx.Response(200, json={"protocol": 1, "features": ["show-context-key-v1"]}),
            ShowRuntimeContextCapability.SUPPORTED,
        ),
        (httpx.Response(503), ShowRuntimeContextCapability.TRANSIENT_UNKNOWN),
        (httpx.Response(200, content=b'{"protocol":1'), ShowRuntimeContextCapability.TRANSIENT_UNKNOWN),
    ],
)
def test_show_live_014_capability_evidence_is_classified_without_guessing(
    monkeypatch,
    tmp_path,
    response,
    expected,
):
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        show_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: _CapabilityClient(response=response),
    )

    outcome = asyncio.run(manager._probe_context_key_capability("http://127.0.0.1:4173"))

    assert outcome is expected


def test_show_live_015_transport_failure_is_transient(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    request = httpx.Request("GET", "http://127.0.0.1:4173/capabilities")
    monkeypatch.setattr(
        show_runtime.httpx,
        "AsyncClient",
        lambda **_kwargs: _CapabilityClient(error=httpx.ConnectTimeout("timed out", request=request)),
    )

    outcome = asyncio.run(manager._probe_context_key_capability("http://127.0.0.1:4173"))

    assert outcome is ShowRuntimeContextCapability.TRANSIENT_UNKNOWN


def test_show_live_015_transient_probe_keeps_shared_request_live_and_retries(
    monkeypatch,
    tmp_path,
):
    manager = _manager(tmp_path)
    manager._base_url = "http://127.0.0.1:4173"
    manager._process = SimpleNamespace(pid=101, poll=lambda: None)
    clock = {"now": 100.0}
    probes = []
    requests = []
    outcomes = iter(
        [
            ShowRuntimeContextCapability.TRANSIENT_UNKNOWN,
            ShowRuntimeContextCapability.SUPPORTED,
        ]
    )

    async def ensure():
        return ShowRuntimeResult(True, manager._base_url)

    async def probe(base_url):
        probes.append(base_url)
        return next(outcomes)

    class _AppClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, *, headers, content):
            requests.append((method, url, headers, content))
            return httpx.Response(200, content=b"ready")

    monkeypatch.setattr(manager, "ensure", ensure)
    monkeypatch.setattr(manager, "_probe_context_key_capability", probe)
    monkeypatch.setattr(show_runtime.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(show_runtime, "_show_runtime_capability_retry_delay", lambda _attempt: 1.0)
    monkeypatch.setattr(show_runtime.httpx, "AsyncClient", lambda **_kwargs: _AppClient())
    envelope = ShowRuntimeProtocolEnvelope(ShowRuntimeContext.SHARED)

    async def exercise():
        first = await manager.request(
            "GET",
            "/sessions/ses/app/",
            envelope=envelope,
            headers={"X-Vibe-Show-Base": "/p/untrusted/"},
        )
        second = await manager.request("GET", "/sessions/ses/app/src/main.tsx", envelope=envelope)
        clock["now"] = 101.0
        third = await manager.request("GET", "/sessions/ses/app/src/App.tsx", envelope=envelope)
        return first, second, third

    responses = asyncio.run(exercise())

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert probes == [manager._base_url, manager._base_url]
    assert all(call[2][SHOW_RUNTIME_PROTOCOL_HEADER] == "1" for call in requests)
    assert all(call[2][SHOW_RUNTIME_CONTEXT_HEADER] == "shared" for call in requests)
    assert all(call[2][SHOW_RUNTIME_BASE_HEADER] == "/show/ses/" for call in requests)
    assert all("X-Vibe-Show-Base" not in call[2] for call in requests)


def test_show_runtime_request_aborts_a_response_over_the_caller_budget(monkeypatch, tmp_path):
    manager = _manager(tmp_path)
    manager._base_url = "http://127.0.0.1:4173"

    async def ensure():
        return ShowRuntimeResult(True, manager._base_url)

    async def negotiate(_base_url):
        return ShowRuntimeContextCapability.UNSUPPORTED

    class _StreamingResponse:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/markdown"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self, **_kwargs):
            yield b"1234"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return _StreamingResponse()

    monkeypatch.setattr(manager, "ensure", ensure)
    monkeypatch.setattr(manager, "_negotiate_context_key_capability", negotiate)
    monkeypatch.setattr(show_runtime.httpx, "AsyncClient", lambda **_kwargs: _Client())

    with pytest.raises(ShowRuntimeResponseTooLarge):
        asyncio.run(
            manager.request(
                "GET",
                "/sessions/ses/app/api/agent-markdown",
                envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE),
                max_response_bytes=3,
            )
        )


def test_show_live_014_capability_cache_resets_with_process_base_and_manager_lifetime(
    monkeypatch,
    tmp_path,
):
    manager = _manager(tmp_path)
    manager._base_url = "http://127.0.0.1:4173"
    manager._process = SimpleNamespace(pid=101, poll=lambda: 0)
    probes = []
    outcomes = iter(
        [
            ShowRuntimeContextCapability.SUPPORTED,
            ShowRuntimeContextCapability.UNSUPPORTED,
            ShowRuntimeContextCapability.SUPPORTED,
            ShowRuntimeContextCapability.TRANSIENT_UNKNOWN,
            ShowRuntimeContextCapability.SUPPORTED,
        ]
    )

    async def ensure():
        return ShowRuntimeResult(True, manager._base_url)

    async def probe(base_url):
        probes.append((base_url, manager._process.pid))
        return next(outcomes)

    monkeypatch.setattr(manager, "ensure", ensure)
    monkeypatch.setattr(manager, "_probe_context_key_capability", probe)
    monkeypatch.setattr(show_runtime, "_show_runtime_capability_retry_delay", lambda _attempt: 5.0)

    async def exercise():
        assert await manager.context_key_capability() is ShowRuntimeContextCapability.SUPPORTED
        assert await manager.context_key_capability() is ShowRuntimeContextCapability.SUPPORTED
        manager._process = SimpleNamespace(pid=102, poll=lambda: 0)
        assert await manager.context_key_capability() is ShowRuntimeContextCapability.UNSUPPORTED
        manager._base_url = "http://127.0.0.1:4174"
        assert await manager.context_key_capability() is ShowRuntimeContextCapability.SUPPORTED
        manager._process = SimpleNamespace(pid=103, poll=lambda: 0)
        assert await manager.context_key_capability() is ShowRuntimeContextCapability.TRANSIENT_UNKNOWN
        manager.stop()
        manager._base_url = "http://127.0.0.1:4174"
        manager._process = SimpleNamespace(pid=104, poll=lambda: 0)
        assert await manager.context_key_capability() is ShowRuntimeContextCapability.SUPPORTED

    asyncio.run(exercise())

    assert probes == [
        ("http://127.0.0.1:4173", 101),
        ("http://127.0.0.1:4173", 102),
        ("http://127.0.0.1:4174", 102),
        ("http://127.0.0.1:4174", 103),
        ("http://127.0.0.1:4174", 104),
    ]
    assert manager._capability_retry_deadline == 0.0

    replacement = _manager(tmp_path)
    assert replacement._context_key_capability is None
    assert replacement._capability_retry_deadline == 0.0


def test_show_live_017_protocol_envelope_is_total_and_strips_untrusted_values():
    private = ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE)
    shared = ShowRuntimeProtocolEnvelope(ShowRuntimeContext.SHARED)

    assert private.headers(
        {
            SHOW_RUNTIME_PROTOCOL_HEADER.lower(): "999",
            SHOW_RUNTIME_CONTEXT_HEADER.lower(): "shared",
            "accept": "text/html",
        }
    ) == {
        "accept": "text/html",
        SHOW_RUNTIME_PROTOCOL_HEADER: "1",
        SHOW_RUNTIME_CONTEXT_HEADER: "private",
    }
    assert shared.headers() == {
        SHOW_RUNTIME_PROTOCOL_HEADER: "1",
        SHOW_RUNTIME_CONTEXT_HEADER: "shared",
    }
    with pytest.raises(TypeError):
        ShowRuntimeProtocolEnvelope("private")  # type: ignore[arg-type]


def test_show_live_017_app_graph_request_cannot_omit_protocol_context(tmp_path):
    manager = _manager(tmp_path)

    with pytest.raises(TypeError):
        manager.request("GET", "/sessions/ses/app/")  # type: ignore[call-arg]


def test_show_runtime_capability_backoff_is_jittered_exponential_and_bounded(monkeypatch):
    monkeypatch.setattr(show_runtime.random, "random", lambda: 1.0)

    delays = [show_runtime._show_runtime_capability_retry_delay(attempt) for attempt in range(1, 9)]

    assert delays == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]

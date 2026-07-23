"""Tests for ``vibe.internal_client``.

The UI server uses this module to reach the controller's Unix socket to
start fire-and-forget turns and run the turn-control surface (cancel /
send-now / turn-state). We cover the socket-missing degradation and the
round-trip shape of each call against a fake ASGI app via
``httpx.ASGITransport`` (skips uvicorn).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import internal_client


def _bind_socket_path(target: Path) -> Path:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(target))
    finally:
        listener.close()
    os.chmod(target, 0o600)
    return target


@pytest.fixture
def socket_path():
    # macOS's sockaddr_un length applies to the string passed to bind, so use a
    # short path rather than pytest's deliberately descriptive temp directory.
    with tempfile.TemporaryDirectory(prefix="avibe-uds-", dir="/tmp") as directory:
        yield _bind_socket_path(Path(directory) / "dispatch.sock")


def test_default_socket_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "dispatch.sock"
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(target))

    assert internal_client.default_socket_path() == target


def test_cancel_dispatch_round_trip(tmp_path, socket_path):
    """``cancel_dispatch`` should forward the session id to the
    controller's ``POST /internal/cancel/<session_id>`` endpoint and
    surface the JSON body verbatim so the UI can render it.
    """

    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/cancel/{session_id}")
    async def _cancel(session_id: str):
        captured["session_id"] = session_id
        return {"ok": True, "session_id": session_id, "status": "cancel_requested"}

    sock = socket_path

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.cancel_dispatch("ses_abc", socket_path=sock)

    result = asyncio.run(_go())
    assert captured["session_id"] == "ses_abc"
    assert result["status_code"] == 200
    assert result["body"] == {"ok": True, "session_id": "ses_abc", "status": "cancel_requested"}


def test_cancel_dispatch_missing_socket_raises_unavailable(tmp_path):
    sock = tmp_path / "missing.sock"
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(internal_client.cancel_dispatch("ses_x", socket_path=sock))


def test_dispatch_async_round_trip(tmp_path, socket_path):
    """``dispatch_async`` posts the payload to ``/internal/dispatch_async`` and
    surfaces the controller's status + body so the UI route can tell a started
    turn (202) from a concurrent-turn refusal (409)."""
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/dispatch_async")
    async def _async(payload: dict):
        captured["payload"] = payload
        return JSONResponse(status_code=202, content={"ok": True, "session_id": payload.get("session_id")})

    sock = socket_path

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.dispatch_async(
                {"session_id": "ses_z", "text": "hi"}, socket_path=sock
            )

    result = asyncio.run(_go())
    assert captured["payload"] == {"session_id": "ses_z", "text": "hi"}
    assert result["status_code"] == 202
    assert result["body"] == {"ok": True, "session_id": "ses_z"}


def test_dispatch_async_missing_socket_raises_unavailable(tmp_path):
    sock = tmp_path / "missing.sock"
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(internal_client.dispatch_async({"session_id": "s", "text": "x"}, socket_path=sock))


def test_reconcile_platforms_round_trip(tmp_path, socket_path):
    app = FastAPI()
    calls: list[bool] = []

    @app.post("/internal/reconcile-platforms")
    async def _reconcile():
        calls.append(True)
        return {"ok": True, "rebuilt": ["slack"]}

    sock = socket_path

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.reconcile_platforms(socket_path=sock)

    result = asyncio.run(_go())

    assert calls == [True]
    assert result["status_code"] == 200
    assert result["body"] == {"ok": True, "rebuilt": ["slack"]}


def test_reconcile_platforms_missing_socket_raises_unavailable(tmp_path):
    sock = tmp_path / "missing.sock"
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(internal_client.reconcile_platforms(socket_path=sock))


def test_reconcile_agent_backends_round_trip(tmp_path, socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/reconcile-agent-backends")
    async def _reconcile(payload: dict):
        captured["payload"] = payload
        return {
            "ok": True,
            "backends": payload["backends"],
            "states": {backend: "restarted" for backend in payload["backends"]},
        }

    sock = socket_path

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.reconcile_agent_backends(
                ["codex", "opencode"],
                socket_path=sock,
            )

    result = asyncio.run(_go())

    assert captured["payload"] == {"backends": ["codex", "opencode"]}
    assert result["status_code"] == 200
    assert result["body"]["states"] == {
        "codex": "restarted",
        "opencode": "restarted",
    }


def test_reconcile_agent_backends_missing_socket_raises_unavailable(tmp_path):
    sock = tmp_path / "missing.sock"
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(
            internal_client.reconcile_agent_backends(
                ["codex"],
                socket_path=sock,
            )
        )


def test_memory_runtime_install_sync_round_trip(socket_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"ok": False, "reason": "memory_runtime_unpublished"})

    with patch("vibe.internal_client.httpx.HTTPTransport", return_value=httpx.MockTransport(handler)):
        result = internal_client.memory_install_runtime_sync(socket_path=socket_path)

    assert captured == {"method": "POST", "path": "/internal/memory/install-runtime"}
    assert result == {
        "status_code": 200,
        "body": {"ok": False, "reason": "memory_runtime_unpublished"},
    }


def test_memory_capture_round_trip(socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/memory/capture")
    async def _capture(payload: dict):
        captured["payload"] = payload
        return {"status": "accepted"}

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.memory_capture(
                "workbench:message-1",
                "session-1",
                "ordinary text",
                123,
                socket_path=socket_path,
            )

    result = asyncio.run(_go())

    assert captured["payload"] == {
        "source_message_id": "workbench:message-1",
        "session_id": "session-1",
        "text": "ordinary text",
        "occurred_at_ms": 123,
    }
    assert result == {"status_code": 200, "body": {"status": "accepted"}}


def test_memory_sync_read_helpers_use_verified_uds(socket_path):
    captured: list[tuple[str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else None
        captured.append((request.url.path, payload))
        if request.url.path == "/internal/memory/status":
            return httpx.Response(200, json={"state": "ready"})
        if request.url.path == "/internal/memory/profile":
            return httpx.Response(200, json={"status": "ok", "items": []})
        return httpx.Response(200, json={"status": "ok", "items": []})

    with patch("vibe.internal_client.httpx.HTTPTransport", return_value=httpx.MockTransport(handler)):
        assert internal_client.memory_status_sync(socket_path=socket_path)["body"] == {"state": "ready"}
        assert internal_client.memory_profile_sync(socket_path=socket_path)["body"] == {"status": "ok", "items": []}
        assert internal_client.memory_search_sync("find this", 4, socket_path=socket_path)["body"] == {
            "status": "ok",
            "items": [],
        }

    assert captured == [
        ("/internal/memory/status", None),
        ("/internal/memory/profile", None),
        ("/internal/memory/search", {"query": "find this", "limit": 4}),
    ]


def test_notify_vault_request_created_round_trip(tmp_path, socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/vault/request-created")
    async def _notify(payload: dict):
        captured["payload"] = payload
        return {"ok": True, "queued": True}

    sock = socket_path

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.notify_vault_request_created(
                {"id": "vrq_1", "status": "pending"}, socket_path=sock
            )

    result = asyncio.run(_go())
    assert captured["payload"] == {"request": {"id": "vrq_1", "status": "pending"}}
    assert result["status_code"] == 200
    assert result["body"] == {"ok": True, "queued": True}


def test_notify_vault_request_created_sync_round_trip(tmp_path, socket_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "queued": True})

    sock = socket_path
    fake_transport = httpx.MockTransport(handler)
    with patch("vibe.internal_client.httpx.HTTPTransport", return_value=fake_transport):
        result = internal_client.notify_vault_request_created_sync(
            {"id": "vrq_1", "status": "pending"}, socket_path=sock
        )

    assert captured["path"] == "/internal/vault/request-created"
    assert captured["payload"] == {"request": {"id": "vrq_1", "status": "pending"}}
    assert result["status_code"] == 200
    assert result["body"] == {"ok": True, "queued": True}


def test_turn_state_os_error_raises_unavailable(tmp_path, socket_path):
    """Socket files can exist on Docker Desktop bind mounts while connection
    operations raise platform ``OSError`` values (for example errno 95). The UI
    route must see the same unavailable signal as a missing socket and degrade
    instead of returning 500."""
    sock = socket_path

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _path):
            raise OSError(95, "Operation not supported")

    with patch("vibe.internal_client.httpx.AsyncClient", return_value=FailingClient()):
        with pytest.raises(internal_client.InternalServerUnavailable) as exc:
            asyncio.run(internal_client.turn_state("ses_x", socket_path=sock))

    assert "Operation not supported" in str(exc.value)


def test_turn_state_uses_short_timeout(tmp_path, socket_path):
    sock = socket_path
    captured: dict = {}

    class CapturingClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _path):
            raise httpx.ReadTimeout("slow internal turn-state")

    with patch("vibe.internal_client.httpx.AsyncClient", CapturingClient):
        with pytest.raises(internal_client.InternalServerTimeout):
            asyncio.run(internal_client.turn_state("ses_x", socket_path=sock))

    assert captured["timeout"].connect == 0.2
    assert captured["timeout"].read == 1.0


def test_memory_client_rejects_non_socket_symlink_and_wrong_mode_before_transport(socket_path) -> None:

    def transport_must_not_run(*_args, **_kwargs):
        raise AssertionError("unverified socket reached transport")

    os.chmod(socket_path, 0o644)
    with patch("vibe.internal_client.httpx.AsyncHTTPTransport", transport_must_not_run):
        with pytest.raises(internal_client.InternalServerUnavailable):
            asyncio.run(internal_client.memory_status(socket_path=socket_path))

    socket_path.unlink()
    owned_socket = _bind_socket_path(socket_path)
    symlink = socket_path.parent / "linked.sock"
    symlink.symlink_to(owned_socket)
    assert stat.S_ISLNK(symlink.lstat().st_mode)
    with patch("vibe.internal_client.httpx.AsyncHTTPTransport", transport_must_not_run):
        with pytest.raises(internal_client.InternalServerUnavailable):
            asyncio.run(internal_client.memory_status(socket_path=symlink))

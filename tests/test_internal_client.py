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
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import internal_client


def test_default_socket_path_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "dispatch.sock"
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", str(target))

    assert internal_client.default_socket_path() == target


def test_cancel_dispatch_round_trip(tmp_path):
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

    sock = tmp_path / "dispatch.sock"
    sock.touch()

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


def test_dispatch_async_round_trip(tmp_path):
    """``dispatch_async`` posts the payload to ``/internal/dispatch_async`` and
    surfaces the controller's status + body so the UI route can tell a started
    turn (202) from a concurrent-turn refusal (409)."""
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/dispatch_async")
    async def _async(payload: dict):
        captured["payload"] = payload
        return JSONResponse(status_code=202, content={"ok": True, "session_id": payload.get("session_id")})

    sock = tmp_path / "dispatch.sock"
    sock.touch()

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


def test_reconcile_platforms_round_trip(tmp_path):
    app = FastAPI()
    calls: list[bool] = []

    @app.post("/internal/reconcile-platforms")
    async def _reconcile():
        calls.append(True)
        return {"ok": True, "rebuilt": ["slack"]}

    sock = tmp_path / "dispatch.sock"
    sock.touch()

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


def test_notify_vault_request_created_round_trip(tmp_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/vault/request-created")
    async def _notify(payload: dict):
        captured["payload"] = payload
        return {"ok": True, "queued": True}

    sock = tmp_path / "dispatch.sock"
    sock.touch()

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


def test_notify_vault_request_created_sync_round_trip(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "queued": True})

    sock = tmp_path / "dispatch.sock"
    sock.touch()
    fake_transport = httpx.MockTransport(handler)
    with patch("vibe.internal_client.httpx.HTTPTransport", return_value=fake_transport):
        result = internal_client.notify_vault_request_created_sync(
            {"id": "vrq_1", "status": "pending"}, socket_path=sock
        )

    assert captured["path"] == "/internal/vault/request-created"
    assert captured["payload"] == {"request": {"id": "vrq_1", "status": "pending"}}
    assert result["status_code"] == 200
    assert result["body"] == {"ok": True, "queued": True}


def test_turn_state_os_error_raises_unavailable(tmp_path):
    """Socket files can exist on Docker Desktop bind mounts while connection
    operations raise platform ``OSError`` values (for example errno 95). The UI
    route must see the same unavailable signal as a missing socket and degrade
    instead of returning 500."""
    sock = tmp_path / "dispatch.sock"
    sock.touch()

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


def test_turn_state_uses_short_timeout(tmp_path):
    sock = tmp_path / "dispatch.sock"
    sock.touch()
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

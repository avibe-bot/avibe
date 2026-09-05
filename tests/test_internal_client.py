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


def test_cancel_dispatch_forwards_exact_run_guard(socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/cancel/{session_id}")
    async def _cancel(session_id: str, run_id: str | None = None):
        captured.update(session_id=session_id, run_id=run_id)
        return {"ok": True, "session_id": session_id, "status": "run_detached"}

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=fake_transport,
        ):
            return await internal_client.cancel_dispatch(
                "ses_shared",
                run_id="run_exact",
                socket_path=socket_path,
            )

    result = asyncio.run(_go())

    assert captured == {"session_id": "ses_shared", "run_id": "run_exact"}
    assert result["status_code"] == 200
    assert result["body"]["status"] == "run_detached"


def test_cancel_dispatch_preserves_an_explicit_blank_run_guard(socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/cancel/{session_id}")
    async def _cancel(session_id: str, run_id: str | None = None):
        captured.update(session_id=session_id, run_id=run_id)
        return {"ok": False, "code": "invalid_run_id"}

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=fake_transport,
        ):
            return await internal_client.cancel_dispatch(
                "ses_shared",
                run_id="",
                socket_path=socket_path,
            )

    result = asyncio.run(_go())

    assert captured == {"session_id": "ses_shared", "run_id": ""}
    assert result["body"] == {"ok": False, "code": "invalid_run_id"}


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


def test_running_agents_ownership_snapshot_posts_bounded_candidates(socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/running-agents/snapshot")
    async def _snapshot(payload: dict):
        captured.update(payload)
        return {
            "ok": True,
            "agents": [],
            "owned_run_ids": payload["run_ids"][:1],
        }

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=fake_transport,
        ):
            return await internal_client.list_running_agents(
                run_ids=["run-a", "run-b"],
                socket_path=socket_path,
            )

    result = asyncio.run(_go())

    assert captured == {"run_ids": ["run-a", "run-b"]}
    assert result["status_code"] == 200
    assert result["body"]["owned_run_ids"] == ["run-a"]


def test_dispatch_async_read_timeout_reports_acceptance_unknown(socket_path):
    sock = socket_path

    class TimingOutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _path, json):
            raise httpx.ReadTimeout("response deadline elapsed")

    with patch("vibe.internal_client.httpx.AsyncClient", return_value=TimingOutClient()):
        with pytest.raises(internal_client.InternalServerTimeout):
            asyncio.run(
                internal_client.dispatch_async(
                    {"session_id": "s", "text": "x"},
                    socket_path=sock,
                )
            )


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


def test_invalidate_activity_streaming_round_trip(socket_path):
    app = FastAPI()
    calls: list[bool] = []

    @app.post("/internal/invalidate-activity-streaming")
    async def _invalidate():
        calls.append(True)
        return {"ok": True}

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.invalidate_activity_streaming(socket_path=socket_path)

    result = asyncio.run(_go())

    assert calls == [True]
    assert result == {"status_code": 200, "body": {"ok": True}}


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


def test_backend_auth_round_trip(socket_path):
    app = FastAPI()
    captured: dict = {}

    @app.post("/internal/backend-auth/test")
    async def _test(payload: dict):
        captured["payload"] = payload
        return {"ok": True, "excerpt": "hello"}

    sock = socket_path

    async def _go():
        fake_transport = httpx.ASGITransport(app=app)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=fake_transport):
            return await internal_client.test_backend_auth(
                "codex",
                model="gpt-5.4-mini",
                socket_path=sock,
            )

    result = asyncio.run(_go())
    assert captured["payload"] == {"backend": "codex", "model": "gpt-5.4-mini"}
    assert result == {
        "status_code": 200,
        "body": {"ok": True, "excerpt": "hello"},
    }


def test_backend_auth_missing_socket_raises_unavailable(tmp_path):
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(
            internal_client.test_backend_auth(
                "claude",
                socket_path=tmp_path / "missing.sock",
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


def test_memory_remember_round_trip(socket_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        captured["session"] = request.headers["X-Avibe-Caller-Session"]
        return httpx.Response(200, json={"status": "accepted"})

    with patch("vibe.internal_client.httpx.HTTPTransport", return_value=httpx.MockTransport(handler)):
        result = internal_client.memory_remember_sync(
            "ordinary text",
            caller_session_id="session-1",
            socket_path=socket_path,
        )

    assert captured == {
        "path": "/internal/memory/remember",
        "payload": {"text": "ordinary text"},
        "session": "session-1",
    }
    assert result == {"status_code": 200, "body": {"status": "accepted"}}


def test_memory_archive_session_round_trip_uses_only_session_identity(socket_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "session": {"id": "ses-memory", "status": "archived"},
            },
        )

    async def _exercise():
        transport = httpx.MockTransport(handler)
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=transport):
            return await internal_client.memory_archive_session(
                "ses-memory",
                socket_path=socket_path,
            )

    result = asyncio.run(_exercise())

    assert captured == {
        "method": "POST",
        "path": "/internal/memory/archive-session",
        "payload": {"session_id": "ses-memory"},
        "timeout": {
            "connect": 5.0,
            "read": None,
            "write": None,
            "pool": None,
        },
    }
    assert result == {
        "status_code": 200,
        "body": {
            "ok": True,
            "session": {"id": "ses-memory", "status": "archived"},
        },
    }


def test_memory_recovery_reads_round_trip_signed_operator(monkeypatch, socket_path):
    from vibe import memory_ui_access as ui_access

    captured: list[httpx.Request] = []
    monkeypatch.setattr(ui_access, "_process_secret", "test-ui-controller-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            processing_record = await internal_client.memory_processing_record(
                user_key="avibe:remote:user-1",
                socket_path=socket_path,
            )
            failures = await internal_client.memory_failures(
                user_key="avibe:remote:user-1",
                socket_path=socket_path,
            )
            maintenance = await internal_client.memory_maintenance(
                user_key="avibe:remote:user-1",
                socket_path=socket_path,
            )
            return processing_record, failures, maintenance

    processing_record, failures, maintenance = asyncio.run(_go())

    assert processing_record == {"status_code": 200, "body": {"status": "ok"}}
    assert failures == {"status_code": 200, "body": {"status": "ok"}}
    assert maintenance == {"status_code": 200, "body": {"status": "ok"}}
    assert [request.url.path for request in captured] == [
        "/internal/memory/processing-record",
        "/internal/memory/failures",
        "/internal/memory/maintenance",
    ]
    for request in captured:
        assert request.headers["x-avibe-memory-user-key"] == "avibe:remote:user-1"
        assert request.headers["x-avibe-memory-ui-proof"] == ui_access.build_ui_read_proof(
            "test-ui-controller-secret",
            method="GET",
            path=request.url.path,
            user_key="avibe:remote:user-1",
        )


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
        assert internal_client.memory_search_sync(
            "connect the clues",
            6,
            mode="agentic",
            socket_path=socket_path,
        )["body"] == {"status": "ok", "items": []}

    assert captured == [
        ("/internal/memory/status", None),
        ("/internal/memory/profile", None),
        (
            "/internal/memory/search",
            {
                "query": "find this",
                "policy": {
                    "mode": "hybrid",
                    "max_results": 4,
                    "include_profile": True,
                    "include_current_session": False,
                },
            },
        ),
        (
            "/internal/memory/search",
            {
                "query": "connect the clues",
                "policy": {
                    "mode": "agentic",
                    "max_results": 6,
                    "include_profile": True,
                    "include_current_session": False,
                    "timeout_seconds": 30.0,
                    "max_model_calls": 2,
                    "cost_budget_tokens": 32_000,
                },
            },
        ),
    ]


def test_memory_list_sync_forwards_page_project_and_caller_session(socket_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            path=request.url.path,
            payload=json.loads(request.content.decode("utf-8")),
            caller_session=request.headers.get("x-avibe-caller-session"),
        )
        return httpx.Response(200, json={"status": "ok", "items": []})

    with patch(
        "vibe.internal_client.httpx.HTTPTransport",
        return_value=httpx.MockTransport(handler),
    ):
        response = internal_client.memory_list_sync(
            project="notes",
            page=3,
            limit=7,
            caller_session_id="ses-memory-list",
            socket_path=socket_path,
        )

    assert response["status_code"] == 200
    assert captured == {
        "path": "/internal/memory/list",
        "payload": {"page": 3, "limit": 7, "project": "notes"},
        "caller_session": "ses-memory-list",
    }


def test_memory_list_async_signs_ui_aggregate_cursor(monkeypatch, socket_path):
    from vibe import memory_ui_access as ui_access

    monkeypatch.setattr(ui_access, "_process_secret", "test-ui-controller-secret")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            path=request.url.path,
            payload=json.loads(request.content.decode("utf-8")),
            user_key=request.headers.get("x-avibe-memory-user-key"),
            proof=request.headers.get("x-avibe-memory-ui-proof"),
        )
        return httpx.Response(200, json={"status": "ok", "items": []})

    async def _exercise():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            return await internal_client.memory_list(
                user_key="avibe:remote:user-list",
                project="all",
                cursor="cursor-token",
                limit=9,
                origin="agent",
                socket_path=socket_path,
            )

    response = asyncio.run(_exercise())

    assert response["status_code"] == 200
    assert captured["path"] == "/internal/memory/list"
    assert captured["payload"] == {
        "limit": 9,
        "project": "all",
        "cursor": "cursor-token",
        "origin": "agent",
    }
    assert captured["user_key"] == "avibe:remote:user-list"
    assert captured["proof"] == ui_access.build_ui_read_proof(
        "test-ui-controller-secret",
        method="POST",
        path="/internal/memory/list",
        user_key="avibe:remote:user-list",
    )


def test_memory_ui_read_helper_signs_the_fixed_local_owner(monkeypatch, socket_path):
    from vibe import memory_ui_access as ui_access

    captured: dict[str, str] = {}
    monkeypatch.setattr(ui_access, "_process_secret", "test-ui-controller-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"status": "ok", "items": []})

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            return await internal_client.memory_profile(
                user_key="avibe:local",
                socket_path=socket_path,
            )

    result = asyncio.run(_go())

    assert result["status_code"] == 200
    assert captured["x-avibe-memory-user-key"] == "avibe:local"
    assert captured["x-avibe-memory-ui-proof"] == ui_access.build_ui_read_proof(
        "test-ui-controller-secret",
        method="GET",
        path="/internal/memory/profile",
        user_key="avibe:local",
    )


@pytest.mark.parametrize(
    ("operation", "path"),
    [
        ("repair", "/internal/memory/repair"),
        ("delete_data", "/internal/memory/delete-data"),
    ],
)
def test_memory_destructive_clients_sign_owner_and_post_exact_loss_confirmation(
    monkeypatch,
    socket_path,
    operation,
    path,
):
    from vibe import memory_ui_access as ui_access

    captured: dict[str, object] = {}
    monkeypatch.setattr(ui_access, "_process_secret", "test-ui-controller-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True, "operation": operation, "result": "completed"})

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            call = (
                internal_client.memory_repair
                if operation == "repair"
                else internal_client.memory_delete_data
            )
            return await call(
                confirm_loss=True,
                user_key="avibe:remote:user-1",
                socket_path=socket_path,
            )

    result = asyncio.run(_go())
    headers = captured["headers"]
    assert result["status_code"] == 200
    assert captured["path"] == path
    assert captured["payload"] == {"confirm_loss": True}
    assert captured["timeout"] == {"connect": 5.0, "read": None, "write": None, "pool": None}
    assert headers["x-avibe-memory-user-key"] == "avibe:remote:user-1"
    assert headers["x-avibe-memory-ui-proof"] == ui_access.build_ui_read_proof(
        "test-ui-controller-secret",
        method="POST",
        path=path,
        user_key="avibe:remote:user-1",
    )


def test_memory_wake_posts_and_passes_through_the_runtime_result(socket_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True, "state": "running"})

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            return await internal_client.memory_wake(socket_path=socket_path)

    result = asyncio.run(_go())

    assert result == {
        "status_code": 200,
        "body": {"ok": True, "state": "running"},
    }
    assert captured == {
        "method": "POST",
        "path": "/internal/memory/wake",
        "timeout": {
            "connect": 5.0,
            "read": None,
            "write": None,
            "pool": None,
        },
    }


def test_memory_reconfigure_forwards_candidate_and_expected_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    socket_path,
) -> None:
    from vibe import memory_ui_access as ui_access

    captured: dict[str, object] = {}
    monkeypatch.setattr(ui_access, "_process_secret", "test-ui-controller-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "operation": "reconfigure"})

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            return await internal_client.memory_reconfigure(
                confirm_loss=True,
                memory={"enabled": True},
                expected_memory={"enabled": False},
                user_key="avibe:local",
                socket_path=socket_path,
            )

    result = asyncio.run(_go())

    assert result["status_code"] == 200
    assert captured == {
        "path": "/internal/memory/reconfigure",
        "payload": {
            "confirm_loss": True,
            "memory": {"enabled": True},
            "expected_memory": {"enabled": False},
        },
    }


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ReadError("controller closed the response stream"),
        httpx.RemoteProtocolError("controller disconnected without a response"),
    ],
)
def test_memory_wake_maps_read_transport_errors_to_unavailable(
    socket_path,
    transport_error: httpx.TransportError,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise transport_error

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            await internal_client.memory_wake(socket_path=socket_path)

    with pytest.raises(internal_client.InternalServerUnavailable) as raised:
        asyncio.run(_go())

    assert raised.value.__cause__ is transport_error


def test_memory_processing_record_helpers_forward_query_and_sign_owner(monkeypatch, socket_path):
    from vibe import memory_ui_access as ui_access

    captured: list[httpx.Request] = []
    monkeypatch.setattr(ui_access, "_process_secret", "test-ui-controller-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async def _go():
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=httpx.MockTransport(handler),
        ):
            await internal_client.memory_processing_record_entries(
                cursor="opaque_cursor",
                limit=17,
                project="notes",
                user_key="avibe:remote:user-1",
                socket_path=socket_path,
            )
            await internal_client.memory_processing_record_entry(
                "mc_1",
                project="notes",
                user_key="avibe:remote:user-1",
                socket_path=socket_path,
            )

    asyncio.run(_go())

    assert [(request.url.path, dict(request.url.params)) for request in captured] == [
        (
            "/internal/memory/processing-record/entries",
            {"cursor": "opaque_cursor", "limit": "17", "project": "notes"},
        ),
        (
            "/internal/memory/processing-record/entry",
            {"memcell_id": "mc_1", "project": "notes"},
        ),
    ]
    for request in captured:
        assert request.headers["x-avibe-memory-user-key"] == "avibe:remote:user-1"
        assert request.headers["x-avibe-memory-ui-proof"] == ui_access.build_ui_read_proof(
            "test-ui-controller-secret",
            method="GET",
            path=request.url.path,
            user_key="avibe:remote:user-1",
        )


def test_memory_sync_read_helper_sends_agent_session_header(socket_path):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"state": "ready"})

    with patch("vibe.internal_client.httpx.HTTPTransport", return_value=httpx.MockTransport(handler)):
        result = internal_client.memory_status_sync(
            caller_session_id="ses-admin",
            socket_path=socket_path,
        )

    assert result["body"] == {"state": "ready"}
    assert captured["x-avibe-caller-session"] == "ses-admin"


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


def test_socket_verifier_accepts_umask_created_owner_only_mode(socket_path) -> None:
    os.chmod(socket_path, 0o700)

    assert internal_client._verified_socket_path(socket_path) == socket_path


def test_socket_verifier_skips_posix_mode_check_on_windows(monkeypatch, socket_path) -> None:
    os.chmod(socket_path, 0o644)
    monkeypatch.setattr(internal_client, "_CHECK_POSIX_SOCKET_MODE", False)

    assert internal_client._verified_socket_path(socket_path) == socket_path


def test_health_sync_rejects_a_stale_socket_path(socket_path) -> None:
    assert internal_client.health_sync(socket_path, timeout=0.05) is False


def test_health_sync_accepts_a_healthy_controller(socket_path) -> None:
    class HealthyClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, path):
            assert path == "/internal/health"
            return httpx.Response(200, json={"ok": True})

    with patch("vibe.internal_client.httpx.Client", return_value=HealthyClient()):
        assert internal_client.health_sync(socket_path, timeout=0.05) is True


def test_show_access_clients_round_trip(socket_path):
    app = FastAPI()
    captured: list[tuple[str, dict]] = []

    @app.post("/internal/show-access/settings-read")
    async def _read(payload: dict):
        captured.append(("read", payload))
        return {
            "show_access": {
                "page_id": payload["page_id"],
                "access_mode": "private",
                "share_id": "stable-link",
                "revision": 3,
                "normalized_emails": [],
            }
        }

    @app.post("/internal/show-access/apply")
    async def _apply(payload: dict):
        captured.append(("apply", payload))
        return {
            "status": "applied",
            "show_access": {
                "page_id": payload["page_id"],
                "access_mode": payload["target_access_mode"],
                "share_id": payload["target_share_id"],
                "revision": payload["expected_revision"] + 1,
                "normalized_emails": payload["target_emails"],
            },
        }

    read_payload = {"page_id": "ses-show-access"}
    apply_payload = {
        "page_id": "ses-show-access",
        "expected_revision": 3,
        "target_access_mode": "limited",
        "target_share_id": "stable-link",
        "target_emails": ["guest@example.com"],
    }

    async def _exercise():
        fake_transport = httpx.ASGITransport(app=app)
        with patch(
            "vibe.internal_client.httpx.AsyncHTTPTransport",
            return_value=fake_transport,
        ):
            loaded = await internal_client.show_access_settings_read(
                read_payload,
                socket_path=socket_path,
            )
            applied = await internal_client.show_access_apply(
                apply_payload,
                socket_path=socket_path,
            )
            return loaded, applied

    loaded, applied = asyncio.run(_exercise())

    assert loaded["status_code"] == 200
    assert loaded["body"]["show_access"]["page_id"] == "ses-show-access"
    assert applied == {
        "status_code": 200,
        "body": {
            "status": "applied",
            "show_access": {
                "page_id": "ses-show-access",
                "access_mode": "limited",
                "share_id": "stable-link",
                "revision": 4,
                "normalized_emails": ["guest@example.com"],
            },
        },
    }
    assert captured == [("read", read_payload), ("apply", apply_payload)]


@pytest.mark.parametrize(
    "operation",
    [
        lambda socket_path: internal_client.show_access_settings_read(
            {"page_id": "ses-show-access"},
            socket_path=socket_path,
        ),
        lambda socket_path: internal_client.show_access_apply(
            {
                "page_id": "ses-show-access",
                "expected_revision": 0,
                "target_access_mode": "private",
                "target_share_id": None,
                "target_emails": [],
            },
            socket_path=socket_path,
        ),
    ],
)
def test_show_access_clients_report_missing_controller_socket(tmp_path, operation):
    with pytest.raises(internal_client.InternalServerUnavailable):
        asyncio.run(operation(tmp_path / "missing.sock"))


def test_show_access_settings_read_reports_read_timeout(socket_path):
    class _TimingOutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _path, json):
            raise httpx.ReadTimeout(f"timed out applying {json['page_id']}")

    with patch(
        "vibe.internal_client.httpx.AsyncClient",
        return_value=_TimingOutClient(),
    ):
        with pytest.raises(internal_client.InternalServerTimeout):
            asyncio.run(
                internal_client.show_access_settings_read(
                    {"page_id": "ses-show-access"},
                    socket_path=socket_path,
                )
            )


def test_show_access_apply_waits_for_a_definitive_controller_result(socket_path):
    captured: dict[str, httpx.Timeout] = {}

    class _Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _path, json):
            return httpx.Response(
                200,
                json={
                    "status": "no_change",
                    "show_access": {
                        "page_id": json["page_id"],
                        "access_mode": "private",
                        "share_id": "stable-link",
                        "revision": json["expected_revision"],
                        "normalized_emails": [],
                    },
                },
            )

    with patch("vibe.internal_client.httpx.AsyncClient", _Client):
        result = asyncio.run(
            internal_client.show_access_apply(
                {
                    "page_id": "ses-show-access",
                    "expected_revision": 0,
                    "target_access_mode": "private",
                    "target_share_id": "stable-link",
                    "target_emails": [],
                },
                socket_path=socket_path,
            )
        )

    assert result["body"]["status"] == "no_change"
    assert captured["timeout"].connect == 1.0
    assert captured["timeout"].read is None

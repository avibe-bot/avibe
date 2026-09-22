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


def test_backend_application_uses_verified_socket_and_real_projection(socket_path):
    from core.backend_restart import BackendRestartCoordinator
    from core.internal_server import create_app
    from tests.test_backend_restart import _AgentService, _controller
    from unittest.mock import AsyncMock

    service = _AgentService()
    service.agents = {"claude": object()}
    controller = _controller(service)
    controller.backend_restart_coordinator = BackendRestartCoordinator(controller, AsyncMock())
    app = create_app(controller)

    async def run():
        with patch("vibe.internal_client.httpx.AsyncHTTPTransport", return_value=httpx.ASGITransport(app=app)):
            result = await internal_client.backend_application("claude", socket_path=socket_path)
            assert result["status_code"] == 200
            assert result["body"]["state"] == "applied"
            assert result["body"]["controller_pid"] == os.getpid()
            missing = await internal_client.backend_application("codex", socket_path=socket_path)
            assert missing["body"]["state"] == "unavailable"
            from config.v2_config import V2Config
            from config.v2_compat import to_app_config

            config = V2Config.default()
            config.agents.codex.enabled = False
            controller.config = to_app_config(config)
            disabled = await internal_client.backend_application("codex", socket_path=socket_path)
            assert disabled["body"]["state"] == "applied"
            assert disabled["body"]["disabled"] is True
            with pytest.raises(ValueError, match="unsupported_backend"):
                await internal_client.backend_application("other", socket_path=socket_path)

    asyncio.run(run())

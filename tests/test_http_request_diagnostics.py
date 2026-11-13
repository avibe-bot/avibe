"""HTTP admission diagnostics: fixed aggregates, not connection/effect counts.

Existing transport diagnostics count streams, and existing authorization tests
exercise only the old connectivity operation. These cases protect the new
observer's coverage/privacy and the snapshot's existing authorization boundary.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from vibe.direct_ingress import DIRECT_INGRESS_SCOPE_KEY, DirectIngressGuard
from vibe.http_request_diagnostics import MAX_COUNT, RequestCounterMiddleware, RequestCounters


def _scope(method="GET", **changes):
    return {
        "type": "http", "method": method, "scheme": "https",
        "path": "/private-account/session-secret", "raw_path": b"/private-account/session-secret",
        "headers": [(b"host", b"test-app.avibe.bot"), (b"cf-ray", b"private-ray")],
        "client": ("private-peer", 123), **changes,
    }


async def _noop(scope, receive, send):
    pass


def _total(counters):
    return sum(sum(row.values()) for row in counters.snapshot(enabled=True)["counts"].values())


@pytest.mark.asyncio
async def test_fixed_route_method_counts_use_guard_not_headers_and_keep_no_private_data():
    counters = RequestCounters()
    observer = RequestCounterMiddleware(_noop, counters=counters, enabled=lambda: True)
    direct = DirectIngressGuard(observer, hostname="test-app.avibe.bot", peer_label=lambda _: "private-peer")
    for _ in range(2):  # Same connection metadata, distinct HTTP requests.
        for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "SECRET-method"):
            scope = _scope(method)
            await observer(scope, None, None)
            await direct(scope, None, None)
    snapshot = counters.snapshot(enabled=True)
    assert snapshot["counts"] == {
        "direct_ingress": {"get_head_options": 6, "post_put_patch_delete": 8, "other": 2},
        "main_listener": {"get_head_options": 6, "post_put_patch_delete": 8, "other": 2},
    }
    assert "private" not in json.dumps(snapshot) and "SECRET" not in json.dumps(snapshot)
    snapshot["counts"]["main_listener"]["other"] = 999
    assert _total(counters) == 32  # Snapshot cannot mutate the live table.
    refused = []
    async def send(message):
        refused.append(message)
    await direct(_scope(headers=[(b"host", b"wrong.test")]), None, send)
    assert refused[0]["status"] == 421 and _total(counters) == 32
    # Arbitrary method/path/header/peer values never allocate new labels.
    for i in range(100):
        await observer(_scope(str(i), path=str(i)), None, None)
    assert counters.snapshot(enabled=True)["counts"]["main_listener"]["other"] == 102
    assert len(counters._counts) == 2 and all(len(row) == 3 for row in counters._counts)
    counters._counts[1][2] = MAX_COUNT
    await observer(_scope("another-private-method"), None, None)
    assert counters._counts[1][2] == MAX_COUNT
    assert counters.snapshot(enabled=True)["saturated"] is True


@pytest.mark.asyncio
async def test_disabled_and_non_http_skip_all_counter_and_classification_work():
    class NoLabels(dict):
        def get(self, *args):
            pytest.fail("disabled/non-HTTP must not inspect request labels")
    counters = RequestCounters()
    def forbidden(*args):
        pytest.fail("counter touched while disabled/non-HTTP")
    counters.observe = forbidden
    seen = []
    async def inner(scope, receive, send):
        seen.append(scope["type"])
    observer = RequestCounterMiddleware(inner, counters=counters, enabled=lambda: False)
    await observer(NoLabels(type="http"), None, None)
    observer.enabled = lambda: True
    for kind in ("websocket", "lifespan"):
        await observer(NoLabels(type=kind), None, None)
    assert seen == ["http", "websocket", "lifespan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sse", "exception", "cancel", "disconnect"])
async def test_admission_once_without_wrapping_response_or_interfering_with_streams(outcome):
    counters = RequestCounters()
    messages = []
    release = asyncio.Event()
    started = asyncio.Event()
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        messages.append(message)
    async def inner(scope, recv, output):
        assert recv is receive and output is send
        if outcome == "exception":
            raise ValueError("private exception")
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "disconnect":
            assert await recv() == {"type": "http.disconnect"}
            return
        await output({"type": "http.response.start", "status": 200, "headers": []})
        await output({"type": "http.response.body", "body": b"first", "more_body": True})
        started.set()
        await release.wait()
        await output({"type": "http.response.body", "body": b"last", "more_body": False})
    observer = RequestCounterMiddleware(inner, counters=counters, enabled=lambda: True)
    if outcome in {"exception", "cancel"}:
        with pytest.raises(ValueError if outcome == "exception" else asyncio.CancelledError):
            await observer(_scope(), receive, send)
    elif outcome == "sse":
        task = asyncio.create_task(observer(_scope(), receive, send))
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert messages[-1]["body"] == b"first" and not task.done()
            assert _total(counters) == 1
        finally:
            release.set()
            await task
    else:
        await observer(_scope(), receive, send)
    assert _total(counters) == 1  # Even no-response requests are admissions.


@pytest.fixture
def ui(monkeypatch, tmp_path):
    from tests.ui_server_test_helpers import save_config
    from vibe import remote_access, ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = save_config(tmp_path)
    ui_server._request_counters.reset()
    lifecycle = SimpleNamespace(direct_paths_enabled=True)
    monkeypatch.setattr(ui_server, "_direct_lifecycle", lifecycle)
    def no_probes(*args, **kwargs):
        pytest.fail("snapshot must not run connectivity probes")
    monkeypatch.setattr(remote_access, "connectivity_diagnostics", no_probes)
    yield ui_server, config, lifecycle
    ui_server._request_counters.reset()


def _remote_client(ui_server, config, role):
    from tests.ui_server_test_helpers import csrf_headers, remote_session_cookie
    from vibe import remote_access
    client = ui_server.app.test_client()
    if role is not None:
        client.set_cookie(remote_access.SESSION_COOKIE_NAME, remote_session_cookie(
            config, "private@example.test", "private-subject", role=role,
            access_source="owner" if role == "owner" else "email",
        ), domain="alex.avibe.bot")
    return client, csrf_headers(client, base_url="https://alex.avibe.bot")


def _snapshot(client, headers):
    from tests.ui_server_test_helpers import remote_peer
    return client.post("/api/remote-access/diagnostics", json={"operation": "snapshot"},
                       headers=headers, base_url="https://alex.avibe.bot", environ_base=remote_peer())


@pytest.mark.parametrize("role", [None, "viewer", "editor", "member", "owner"])
def test_snapshot_inherits_existing_auth_roles_and_csrf(ui, role):
    ui_server, config, _ = ui
    client, headers = _remote_client(ui_server, config, role)
    response = _snapshot(client, headers)
    if role in {"member", "owner"}:
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True and data["request_counts"]["enabled"] is True
        # Snapshot itself is an admitted main-listener mutation-class request.
        assert data["request_counts"]["counts"]["main_listener"]["post_put_patch_delete"] == 1
        assert _snapshot(client, {}).status_code == 403
    else:
        assert response.status_code in {401, 403}
        assert "request_counts" not in response.get_json()
    # Denied requests are counted too; the counter runs outside authorization.
    assert ui_server._request_counters.snapshot(enabled=True)["counts"]["main_listener"]["post_put_patch_delete"] >= 1


def test_empty_connectivity_operation_unchanged_and_snapshot_does_not_reset(ui, monkeypatch):
    from vibe import remote_access
    ui_server, config, lifecycle = ui
    client, headers = _remote_client(ui_server, config, "owner")
    called = []
    result = {"ok": True, "dns": {"status": "available"}, "http2": {"status": "unknown"}}
    def connectivity():
        called.append(True)
        return result
    monkeypatch.setattr(remote_access, "connectivity_diagnostics", connectivity)
    response = client.post("/api/remote-access/diagnostics", json={}, headers=headers,
                           base_url="https://alex.avibe.bot")
    assert response.status_code == 200 and response.get_json() == result and called == [True]
    first = _snapshot(client, headers).get_json()["request_counts"]
    lifecycle.direct_paths_enabled = False
    disabled = _snapshot(client, headers).get_json()["request_counts"]
    assert disabled["enabled"] is False and disabled["counts"] == first["counts"]
    lifecycle.direct_paths_enabled = True
    resumed = _snapshot(client, headers).get_json()["request_counts"]
    assert resumed["counts"]["main_listener"]["post_put_patch_delete"] == first["counts"]["main_listener"]["post_put_patch_delete"] + 1
    assert called == [True]
    for payload in ({"operation": "start"}, {"operation": "clear"}, {"operation": "snapshot", "path": "secret"}):
        response = client.post("/api/remote-access/diagnostics", json=payload, headers=headers,
                               base_url="https://alex.avibe.bot")
        assert response.status_code == 400
    assert called == [True]


@pytest.mark.asyncio
async def test_ui_wiring_counts_direct_admission_before_auth_and_live_host_rejection(ui):
    import httpx

    ui_server, config, _ = ui
    config.remote_access.vibe_cloud.public_url = "https://test-app.avibe.bot"
    config.save()
    guard = DirectIngressGuard(ui_server.app, hostname="test-app.avibe.bot")
    transport = httpx.ASGITransport(app=guard)
    async with httpx.AsyncClient(transport=transport, base_url="https://test-app.avibe.bot") as client:
        assert (await client.get("/api/config")).status_code == 401
        # Unknown API is denied before routing for this unauthenticated peer.
        assert (await client.get("/api/nonexistent-diagnostic-test-route")).status_code == 403
        # This refusal occurs before the observer and is deliberately excluded.
        assert (await client.get("/api/config", headers={"Host": "wrong.test"})).status_code == 421
        config.remote_access.vibe_cloud.public_url = "https://other-app.avibe.bot"
        config.save()
        # Live-host middleware is inside the observer: its refusal still counts.
        assert (await client.get("/health")).status_code == 421
    assert ui_server._request_counters.snapshot(enabled=True)["counts"] == {
        "direct_ingress": {"get_head_options": 3, "post_put_patch_delete": 0, "other": 0},
        "main_listener": {"get_head_options": 0, "post_put_patch_delete": 0, "other": 0},
    }


@pytest.mark.asyncio
async def test_http_error_responses_are_admissions_not_success_counts():
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    async def handler(request):
        return Response(status_code=500)
    app = Starlette(routes=[Route("/endpoint", handler, methods=["GET"])])
    counters = RequestCounters()
    observer = RequestCounterMiddleware(app, counters=counters, enabled=lambda: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observer), base_url="http://test") as client:
        assert (await client.get("/absent")).status_code == 404
        assert (await client.post("/endpoint")).status_code == 405
        assert (await client.get("/endpoint")).status_code == 500
    assert counters.snapshot(enabled=True)["counts"]["main_listener"] == {
        "get_head_options": 2, "post_put_patch_delete": 1, "other": 0,
    }


@pytest.mark.asyncio
async def test_setting_gate_reuses_lifecycle_observation_and_service_restart_resets(monkeypatch):
    from config.v2_config import V2Config
    from vibe import direct_lifecycle, remote_access, ui_server
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *a, **k: None)
    owner = direct_lifecycle.DirectAccessLifecycle(object())
    config = V2Config.default()
    assert owner.direct_paths_enabled is False
    owner._observe_config(config)
    config.remote_access.vibe_cloud.direct = True
    owner._observe_config(config)
    assert owner.direct_paths_enabled is True  # Setting, NOT transport readiness.
    config.remote_access.vibe_cloud.direct = False
    owner._observe_config(config)
    assert owner.direct_paths_enabled is False
    config.remote_access.vibe_cloud.direct = True
    owner._observe_config(config)
    owner._shutdown = True
    assert owner.direct_paths_enabled is False

    async def no_start(self):
        pass
    async def no_stop(self):
        pass
    monkeypatch.setattr(direct_lifecycle.DirectAccessLifecycle, "start", no_start)
    monkeypatch.setattr(direct_lifecycle.DirectAccessLifecycle, "stop", no_stop)
    monkeypatch.setattr(direct_lifecycle, "register_active_lifecycle", lambda _: None)
    monkeypatch.setattr(ui_server, "_direct_lifecycle", None)
    ui_server._request_counters.observe(_scope())
    await ui_server._start_direct_lifecycle()
    assert _total(ui_server._request_counters) == 0
    ui_server._request_counters.observe(_scope())
    await ui_server._start_direct_lifecycle()  # Idempotent startup does not reset.
    assert _total(ui_server._request_counters) == 1
    await ui_server._stop_direct_lifecycle()
    await ui_server._start_direct_lifecycle()
    assert _total(ui_server._request_counters) == 0

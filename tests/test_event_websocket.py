import asyncio

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from vibe.event_websocket import stream_event_response
from vibe.ui_compat import TEST_REMOTE_ADDR_HEADER


@pytest.mark.asyncio
async def test_stream_end_and_client_message_have_one_close_owner():
    close_started = asyncio.Event()
    connected = False
    sent = []
    released = []

    async def receive():
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await close_started.wait()
        return {"type": "websocket.receive", "text": "unexpected publish"}

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.close":
            close_started.set()
            # Let a pending receive finish while the close is in flight.
            await asyncio.sleep(0)

    async def frames():
        try:
            yield ": connected\n\n"
        finally:
            released.append(True)

    async def response():
        return StreamingResponse(frames(), media_type="text/event-stream")

    socket = WebSocket({"type": "websocket", "path": "/events"}, receive, send)
    await stream_event_response(socket, response)
    assert [message for message in sent if message["type"] == "websocket.close"] == [
        {"type": "websocket.close", "code": 1000, "reason": ""},
    ]
    assert released == [True]


@pytest.mark.asyncio
async def test_send_disconnect_does_not_close_an_already_disconnected_socket():
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    sent = []
    released = []

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.send":
            raise OSError("peer disconnected")

    async def frames():
        try:
            yield ": connected\n\n"
        finally:
            released.append(True)

    async def response():
        return StreamingResponse(frames(), media_type="text/event-stream")

    socket = WebSocket({"type": "websocket", "path": "/events"}, incoming.get, send)
    await stream_event_response(socket, response)
    assert [message["type"] for message in sent] == ["websocket.accept", "websocket.send"]
    assert released == [True]


def test_socket_delivers_same_generator_frames_and_releases_it_on_close():
    app = FastAPI()
    released = []

    async def frames():
        try:
            yield ": connected\n\n"
            yield 'id: e1\nevent: show.event\ndata: {"text":"中文"}\n\n'
            await asyncio.Event().wait()
        finally:
            released.append(True)

    @app.websocket("/events")
    async def events(socket: WebSocket):
        async def response():
            return StreamingResponse(frames(), media_type="text/event-stream")

        await stream_event_response(socket, response)

    with TestClient(app).websocket_connect("/events") as socket:
        assert socket.receive_text() == ": connected\n\n"
        assert '"text":"中文"' in socket.receive_text()
    assert released == [True]


@pytest.mark.parametrize("status", [200, 302, 401, 403, 404, 503])
def test_non_stream_response_never_becomes_a_socket_subscription(status):
    app = FastAPI()

    @app.websocket("/events")
    async def events(socket: WebSocket):
        async def response():
            return JSONResponse({"error": "not a subscription"}, status_code=status)

        await stream_event_response(socket, response)

    with pytest.raises(WebSocketDisconnect) as error:
        with TestClient(app).websocket_connect("/events"):
            pytest.fail("Non-stream response was accepted")
    assert error.value.code == (1011 if status >= 500 else 1008)


@pytest.mark.parametrize("path", ["/api/events", "/show/page/__show/events?stream=1", "/p/share/__show/events?stream=1"])
def test_real_event_endpoints_require_exact_origin(monkeypatch, tmp_path, path):
    from vibe import ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    for origin in (None, "http://evil.test", "http://127.0.0.1:7777", "https://127.0.0.1"):
        headers = {"Origin": origin} if origin else {}
        with pytest.raises(WebSocketDisconnect) as error:
            with ui_server.app.test_client().websocket_connect(f"ws://127.0.0.1{path}", headers=headers):
                pytest.fail("Cross-origin socket was accepted")
        assert error.value.code == 1008


def test_workbench_socket_runs_http_hooks_and_unsubscribes(monkeypatch, tmp_path):
    from vibe import ui_server
    from vibe.sse_broker import broker
    from vibe.ui_compat import request

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    observed = []

    def observe():
        observed.append((request.method, request.path, request.args.get("after_id")))

    monkeypatch.setattr(ui_server.app, "_before_request_handlers", [
        *ui_server.app._before_request_handlers, observe,
    ])
    before = broker.subscriber_count()
    with ui_server.app.test_client().websocket_connect(
        "ws://127.0.0.1/api/events?after_id=cursor", headers={"Origin": "http://127.0.0.1", TEST_REMOTE_ADDR_HEADER: "127.0.0.1"},
    ) as socket:
        assert socket.receive_text() == ": stream connected\n\n"
        assert "event: connected" in socket.receive_text()
        assert broker.subscriber_count() == before + 1
    assert observed == [("GET", "/api/events", "cursor")]
    assert broker.subscriber_count() == before


def test_workbench_socket_preserves_http_denial(monkeypatch, tmp_path):
    from vibe import ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(ui_server.app, "_before_request_handlers", [
        *ui_server.app._before_request_handlers,
        lambda: JSONResponse({"error": "instance_access_forbidden"}, status_code=403),
    ])
    with pytest.raises(WebSocketDisconnect) as error:
        with ui_server.app.test_client().websocket_connect("ws://127.0.0.1/api/events", headers={"Origin": "http://127.0.0.1", TEST_REMOTE_ADDR_HEADER: "127.0.0.1"}):
            pytest.fail("Denied HTTP context became a WS subscription")
    assert error.value.code == 1008


def test_subscription_rejects_client_messages_and_releases_broker(monkeypatch, tmp_path):
    from vibe import ui_server
    from vibe.sse_broker import broker

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    before = broker.subscriber_count()
    with ui_server.app.test_client().websocket_connect(
        "ws://127.0.0.1/api/events",
        headers={"Origin": "http://127.0.0.1", TEST_REMOTE_ADDR_HEADER: "127.0.0.1"},
    ) as socket:
        for _ in range(3):
            socket.receive_text()
        socket.send_text('{"type":"publish"}')
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_text()
        assert error.value.code == 1008
    assert broker.subscriber_count() == before


def test_remote_viewer_filtering_and_live_session_revocation(monkeypatch, tmp_path):
    from tests.test_remote_authorization_revision import _paired_config, _organization_claims
    from vibe import remote_access, ui_server
    from vibe.sse_broker import broker

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    cookie = remote_access.make_session_cookie(
        config, "viewer@example.com", "viewer-1",
        session_claims=_organization_claims(config, role="viewer"),
    )
    identity = remote_access.parse_session_identity(config, cookie)
    assert identity is not None
    headers = {
        "Origin": "https://alex.avibe.bot",
        "Cookie": f"{remote_access.SESSION_COOKIE_NAME}={cookie}",
        TEST_REMOTE_ADDR_HEADER: "203.0.113.10",
    }
    before = broker.subscriber_count()
    with ui_server.app.test_client().websocket_connect("wss://alex.avibe.bot/api/events", headers=headers) as socket:
        for _ in range(3):
            socket.receive_text()
        broker.publish("vaults.updated", {"secret_name": "hidden-secret"})
        broker.publish("authorization.changed", {"project_ids": ["hidden-project"]})
        frame = socket.receive_text()
        assert "event: authorization.changed" in frame
        assert "hidden-project" not in frame
        assert "hidden-secret" not in frame
        assert remote_access.revoke_browser_session(identity)
        broker.publish("authorization.changed", {"project_ids": []})
        frame = socket.receive_text()
        assert "event: remote.authorization" in frame
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    assert broker.subscriber_count() == before
    # The same signed cookie cannot re-enter by either transport.
    with pytest.raises(WebSocketDisconnect):
        with ui_server.app.test_client().websocket_connect("wss://alex.avibe.bot/api/events", headers=headers):
            pytest.fail("Revoked session reconnected")


@pytest.mark.parametrize("public", [False, True])
def test_show_socket_replays_after_cursor_and_preserves_session_boundary(monkeypatch, tmp_path, public):
    from core.show_session_events import ShowSessionEventStore
    from tests.test_ui_show_pages import _create_agent_session, _create_show_page
    from tests.ui_server_test_helpers import _save_config
    from vibe import ui_server
    from vibe.sse_broker import broker

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    _create_agent_session("ses_socket")
    share_id = _create_show_page("ses_socket", "public" if public else "private")
    store = ShowSessionEventStore()
    try:
        for index in range(3):
            store.append("ses_socket", {
                "id": f"show_evt_{index}", "type": "assistant.mark.created",
                "mark": {"target": "title", "body": f"note {index}", "createdAt": "2026-11-27T01:00:00Z"},
            })
    finally:
        store.close()
    path = f"/p/{share_id}" if public else "/show/ses_socket"
    before = broker.subscriber_count()
    with ui_server.app.test_client().websocket_connect(
        f"ws://127.0.0.1{path}/__show/events?stream=1&after_id=show_evt_0",
        headers={"Origin": "http://127.0.0.1", TEST_REMOTE_ADDR_HEADER: "127.0.0.1"},
    ) as socket:
        assert socket.receive_text() == ": show events connected\n\n"
        assert "id: show_evt_1" in socket.receive_text()
        assert "id: show_evt_2" in socket.receive_text()
        broker.publish("show.dispatch", {"session_id": "another_session", "event": "turn.chunk", "data": {"text": "hidden"}})
        broker.publish("show.dispatch", {"session_id": "ses_socket", "event": "turn.chunk", "data": {"text": "visible"}})
        frame = socket.receive_text()
        assert "visible" in frame
        assert "hidden" not in frame
    assert broker.subscriber_count() == before


def test_session_capability_is_only_advertised_after_admission(monkeypatch, tmp_path):
    from tests.test_remote_authorization_revision import _paired_config, _organization_cookie
    from vibe import remote_access, ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _paired_config(tmp_path)
    client = ui_server.app.test_client()
    local = client.get("/api/session", base_url="http://127.0.0.1").get_json()
    assert local["event_stream_transport"] == "websocket-sse-v1"
    denied = client.get("/api/session", base_url="https://alex.avibe.bot").get_json()
    assert denied["authenticated"] is False
    assert "event_stream_transport" not in denied
    client.set_cookie(remote_access.SESSION_COOKIE_NAME, _organization_cookie(config), domain="alex.avibe.bot")
    admitted = client.get("/api/session", base_url="https://alex.avibe.bot").get_json()
    assert admitted["authenticated"] is True
    assert admitted["event_stream_transport"] == "websocket-sse-v1"


async def test_event_socket_over_real_direct_ingress_keeps_remote_admission(monkeypatch, tmp_path):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect
    from tests.test_direct_ingress import HOSTNAME, _client_context, _connect, _running
    from tests.ui_server_test_helpers import _save_config, remote_session_cookie
    from vibe import remote_access, ui_server

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    cookie = remote_session_cookie(config, "owner@example.com", "owner-1")
    async with _running(ui_server.app, tmp_path) as harness:
        def subscribe(cookie_value):
            status, sock = _connect(harness.transport_port)
            assert status == 200
            try:
                with connect(
                    f"wss://{HOSTNAME}/api/events", sock=sock,
                    ssl=_client_context(harness.certificate.cafile), server_hostname=HOSTNAME,
                    origin=f"https://{HOSTNAME}", proxy=None,
                    additional_headers={"Cookie": f"{remote_access.SESSION_COOKIE_NAME}={cookie_value}"},
                ) as socket:
                    assert socket.recv(timeout=2) == ": stream connected\n\n"
                    return socket.recv(timeout=2)
            finally:
                sock.close()

        with pytest.raises(InvalidStatus) as error:
            await asyncio.to_thread(subscribe, "")
        assert error.value.response.status_code == 403
        frame = await asyncio.to_thread(subscribe, cookie)
        assert '"via":"direct"' in frame

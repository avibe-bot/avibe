"""Published state (not connector health) retires network-position grants."""

import asyncio
import subprocess
import sys

import pytest

from config import paths
from config.v2_config import V2Config
from storage import web_push_cutover as cutover, web_push_service as push
from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.network_position import BindingState, binding_state

LAN = "http://192.168.2.3:15131"
LAN_PEER = {"REMOTE_ADDR": "192.168.2.4"}
HOST = "http://127.0.0.1:15131"


def _unbound():
    config = V2Config.default()
    config.ui.setup_host = "192.168.2.3"
    config.save()
    return config


def _bind(config):
    cloud = config.remote_access.vibe_cloud
    cloud.instance_id = "fixture-instance"
    cloud.instance_secret = "fixture-secret"
    cloud.session_secret = "fixture-cookie-key"
    cloud.public_url = "https://fixture.example.test"
    # A released, saved binding persists when the connector is Off.
    assert not cloud.enabled
    return config


def _payload(device):
    return {
        "endpoint": f"https://push.example.test/{device}",
        "keys": {"p256dh": f"key-{device}", "auth": f"auth-{device}"},
    }


@pytest.mark.parametrize("first_start", [True, False], ids=["persisted-upgrade", "publish"])
def test_retirement_and_opt_out(monkeypatch, tmp_path, first_start):
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        rows = {
            device: push.upsert_subscription(
                conn,
                user_key="remote:member" if device == "signed" else "local",
                payload=_payload(device),
                device_id=device,
            )
            for device in ["host", "old-proxy", "off", "signed"]
        }
        push.disable_subscription(conn, endpoint=rows["off"]["endpoint"], user_key="local")
    client = ui_server.app.test_client()
    assert client.get("/api/session", base_url=LAN, environ_base=LAN_PEER).status_code == 200
    if first_start:
        _bind(config).save()  # no modern publication path/receipt on the released install
        assert not cutover.may_dispatch(engine, rows["host"])
        cutover.reconcile_startup(V2Config.load())
    else:
        config = api.save_config({"remote_access": {"vibe_cloud": vars(_bind(config).remote_access.vibe_cloud)}})
    assert binding_state(V2Config.load()) is BindingState.BOUND
    assert V2Config.load().ui.setup_host == "192.168.2.3"
    for device in ["host", "old-proxy", "off"]:
        assert not cutover.may_dispatch(engine, rows[device])
    with engine.connect() as conn:
        assert [r["endpoint"] for r in push.list_enabled(conn)] == [rows["signed"]["endpoint"]]
    headers = csrf_headers(client, HOST)

    def status(device):
        return client.post(
            "/api/web-push/status",
            base_url=HOST,
            headers=headers,
            json={
                "endpoint": rows[device]["endpoint"],
                "subscription": _payload(device),
                "device_id": device,
            },
        ).get_json()

    def repair(device, **extra):
        return client.post(
            "/api/web-push/subscriptions",
            base_url=HOST,
            headers=headers,
            json={
                "binding_repair": True,
                "subscription": _payload(device),
                "device_id": device,
                **extra,
            },
        ).get_json()

    assert status("host")["current_subscription_binding_retired"]
    assert not status("host")["current_subscription_enabled"]
    assert not repair("host", background_rotation=True)["accepted"]
    assert repair("host")["accepted"]
    assert status("host")["current_subscription_enabled"]
    assert not status("off")["current_subscription_binding_retired"]
    assert not repair("off")["accepted"]
    assert not repair("off", binding_repair=False)["accepted"]  # legacy foreground retry
    assert not repair("signed", binding_repair=False, explicit_enable=True)["accepted"]
    assert not repair("unseen", binding_repair=False)["accepted"]  # no unattended fresh opt-in
    cutover.reconcile_startup(V2Config.load())
    with engine.connect() as conn:
        assert {r["endpoint"] for r in push.list_enabled(conn)} == {
            rows["host"]["endpoint"],
            rows["signed"]["endpoint"],
        }
        assert push.is_binding_retired(conn, rows["old-proxy"]["endpoint"])
        current_host = push.get_enabled_by_endpoint(conn, endpoint=rows["host"]["endpoint"], user_key="local")
    assert cutover.may_dispatch(engine, current_host)
    assert not cutover.may_dispatch(engine, rows["host"])  # A queued pre-binding grant stays retired.
    denied = client.post(
        "/api/web-push/status", base_url=LAN, environ_base=LAN_PEER, json={"subscription": _payload("old-proxy")}
    )
    assert denied.status_code == 503
    assert repair("off", binding_repair=False, explicit_enable=True)["accepted"]


@pytest.mark.parametrize("route", ["/api/web-push/subscriptions", "/api/web-push/status"])
def test_registration_admitted_before_publish_is_fenced_at_write(monkeypatch, tmp_path, route):
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    ensure_sqlite_state()
    client = ui_server.app.test_client()
    headers = csrf_headers(client, LAN)
    # The ASGI admission and the browser identity lookup already succeeded.
    # Interleave publication immediately before this request reserves its writer.
    check = ui_server._local_push_registration_state
    entered = []

    def publish_then_check(conn):
        entered.append(True)
        api.save_config({"remote_access": {"vibe_cloud": vars(_bind(config).remote_access.vibe_cloud)}})
        return check(conn)

    monkeypatch.setattr(ui_server, "_local_push_registration_state", publish_then_check)
    response = client.post(
        route,
        base_url=LAN,
        environ_base=LAN_PEER,
        headers=headers,
        json={"subscription": _payload("late"), "device_id": "late"},
    )
    assert entered == [True]
    assert response.status_code == 503 and response.get_json()["error"] == "remote_access_host_mismatch"
    with get_cached_sqlite_engine().connect() as conn:
        assert push.get_by_endpoint(conn, endpoint=_payload("late")["endpoint"]) is None


def test_host_push_grant_survives_remote_off_and_cookie_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _bind(_unbound())
    config.remote_access.vibe_cloud.enabled = True
    config.save()
    cutover.reconcile_startup(config)
    client = ui_server.app.test_client()
    headers = csrf_headers(client, HOST)

    def register(device):
        result = client.post(
            "/api/web-push/subscriptions",
            base_url=HOST,
            headers=headers,
            json={"subscription": _payload(device), "explicit_enable": True},
        )
        assert result.status_code == 200 and result.get_json()["accepted"]
        return result.get_json()["subscription"]

    engine = get_cached_sqlite_engine()
    host = register("host")
    assert cutover.may_dispatch(engine, host)
    # Use the actual settings writer, without starting or stopping a connector.
    before = config.remote_access.vibe_cloud.session_secret
    ui_server._save_config_and_runtime_decisions({"remote_access": {"vibe_cloud": {"enabled": False}}})
    off = V2Config.load()
    assert not off.remote_access.vibe_cloud.enabled and off.remote_access.vibe_cloud.session_secret != before
    assert cutover.may_dispatch(engine, host)
    second = register("second")
    cutover.reconcile_startup(off)
    assert cutover.may_dispatch(engine, host) and cutover.may_dispatch(engine, second)

    # A real replacement pairing still requires its own committed receipt.
    off.remote_access.vibe_cloud.instance_secret = "replacement-pairing"
    off.save()
    assert not cutover.may_dispatch(engine, host)


def test_published_file_with_rolled_back_receipt_blocks_delivery_until_reconciled(monkeypatch, tmp_path):
    from config.v2_config import config_file_lock

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    before = _unbound()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        stale = push.upsert_subscription(conn, user_key="local", payload=_payload("browser"))
    after = _bind(V2Config.load())
    with pytest.raises(RuntimeError, match="interrupted"), config_file_lock(), cutover.publishing(before, after):
        after.save()
        raise RuntimeError("interrupted after atomic replace")
    # Rollback really left an enabled row. A queued preview must not send it.
    with engine.connect() as conn:
        assert push.get_deliverable_by_endpoint(conn, endpoint=stale["endpoint"], user_key="local")
    assert not cutover.may_dispatch(engine, stale)
    client = ui_server.app.test_client()
    headers = csrf_headers(client, HOST)
    assert (
        client.post(
            "/api/web-push/subscriptions", base_url=HOST, headers=headers, json={"subscription": _payload("new")}
        ).status_code
        == 503
    )
    cutover.reconcile_startup(after)
    with engine.connect() as conn:
        assert push.is_binding_retired(conn, stale["endpoint"])
    assert not cutover.may_dispatch(engine, stale)


def test_rotated_foreground_grant_cannot_consume_an_opt_out_or_signed_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    ensure_sqlite_state()
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        for device in ("a", "b", "c", "native", "member"):
            push.upsert_subscription(
                conn,
                user_key="remote:member" if device == "member" else "local",
                payload=_payload(device),
                device_id=None if device == "native" else device,
            )
    _bind(config).save()
    cutover.reconcile_startup(config)
    with engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

        def repair(endpoint, previous, device_id):
            return push.resume_retired_host_subscription(
                conn,
                payload=_payload(endpoint),
                device_id=device_id,
                device_label=None,
                user_agent=None,
                previous_endpoints=[_payload(previous)["endpoint"]],
            )

        assert repair("member", "a", "a") is None
        assert repair("new", "a", "other") is None
        assert push.disable_device_subscription(conn, endpoint=_payload("b")["endpoint"], user_key="local")
        assert repair("new", "b", "b") is None
        assert repair("new", "a", "a")["enabled"]
        assert repair("second", "a", "a") is None
        assert repair("new-native", "native", None)["enabled"]
        assert push.disable_device_subscription(conn, endpoint=_payload("native")["endpoint"], user_key="local")
        assert not push.get_enabled_by_endpoint(conn, endpoint=_payload("new-native")["endpoint"], user_key="local")
        assert push.is_binding_retired(conn, _payload("c")["endpoint"])
        assert push.get_enabled_by_endpoint(conn, endpoint=_payload("member")["endpoint"], user_key="remote:member")


def test_persisted_bound_lan_liveness_docker_and_canonical(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _bind(_unbound())
    config.save()
    client = ui_server.app.test_client()

    def get(path, origin=LAN, environ=LAN_PEER, **kwargs):
        return client.get(path, base_url=origin, environ_base=environ, **kwargs)

    notice = get("/settings/devices?private=no", headers={"Accept": "text/html"})
    assert notice.status_code == 200 and "Location" not in notice.headers
    assert notice.headers["Cache-Control"] == "no-store" and notice.headers["Referrer-Policy"] == "no-referrer"
    assert (
        b"https://avibe.bot/app" in notice.content
        and b"fixture" not in notice.content
        and b"?private" not in notice.content
    )
    assert get("/health").get_json() == {"status": "ok"}
    assert get("/health", environ={"REMOTE_ADDR": "8.8.8.8"}).status_code == 503
    assert get("/", origin="http://192.168.2.99", headers={"Accept": "text/html"}).status_code == 503
    assert get("/", headers={"Accept": "application/json"}).status_code == 503
    for path in ["/status", "/api/session", "/api/config/", "/api/remote-access/claims/synthetic", "/auth/login"]:
        assert get(path).status_code == 503
    assert get("/api/session", HOST, {}).status_code == 200
    bridge = {"REMOTE_ADDR": "172.19.0.1"}
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_PEER_IPS", "172.19.0.1")
    monkeypatch.setenv("VIBE_REMOTE_ALLOW_DOCKER_LOOPBACK_PEERS", "1")
    assert get("/api/session", HOST, bridge).status_code == 503
    monkeypatch.setenv("VIBE_REMOTE_DOCKER_LOOPBACK_BIND_HOST", "127.0.0.1")
    assert get("/api/session", HOST, bridge).status_code == 200
    assert get("/api/desktop/local-instance", HOST, bridge).status_code == 403
    assert get("/api/desktop/local-instance", HOST, bridge).get_json()["error"] == "direct_loopback_required"
    assert get("/api/session", HOST, bridge, headers={"X-Forwarded-Proto": "https"}).status_code == 503
    assert get("/api/session", HOST, {"REMOTE_ADDR": "172.19.0.2"}).status_code == 503
    config.remote_access.vibe_cloud.enabled = True
    config.save()
    assert get("/settings/devices", headers={"Accept": "text/html"}).content == notice.content
    public = get(
        "/api/session",
        "http://fixture.example.test",
        {"REMOTE_ADDR": "127.0.0.1"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert public.status_code == 200
    assert public.get_json()["remote"] and public.get_json()["authenticated"] is False
    assert "Secure" in public.headers.get("Set-Cookie", "")


@pytest.mark.parametrize(("address", "expected"), [("192.168.2.3", "127.0.0.1"), ("fd00::4", "[::1]")])
def test_cli_bound_ui_target_uses_same_family_loopback(monkeypatch, tmp_path, address, expected):
    from vibe.cli import _ui_show_events_host

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _unbound()
    config.ui.setup_host = address
    assert _ui_show_events_host(config) == (f"[{address}]" if ":" in address else address)
    _bind(config)
    assert _ui_show_events_host(config) == expected


def test_missing_partial_reset_block_position(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    client = ui_server.app.test_client()

    def denied():
        assert client.get("/api/session", base_url=LAN, environ_base=LAN_PEER).status_code == 503
        assert client.get("/api/session", base_url=HOST).status_code == 200

    denied()
    config = _unbound()
    config.remote_access.vibe_cloud.session_secret = "partial"
    config.save()
    denied()
    _unbound()
    (paths.get_state_dir() / "remote-binding-reset.json").write_text("not valid json")
    denied()


@pytest.mark.asyncio
async def test_second_process_retires_live_setup_channels(monkeypatch, tmp_path):
    # Real persisted config and actual ASGI middleware. Only the consumers are
    # stand-ins: no PTY, Vite server, network sockets or user's processes.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    # Separate per-message admission from the idle timer: even if no tick
    # can occur during this test, the very first input and output must fail.
    from vibe import setup_network_stream

    monkeypatch.setattr(setup_network_stream, "CHECK_INTERVAL_SECONDS", 60.0)
    _unbound()
    inbox = asyncio.Queue()
    release_output = asyncio.Event()
    events = {k: [] for k in ("terminal", "hmr", "sse", "host")}
    ready = {k: asyncio.Event() for k in events}
    received = []

    async def consumer(scope, receive, send):
        name = scope["path"][1:]
        sse = name == "sse"
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]}
            if sse
            else {"type": "websocket.accept"}
        )
        ready[name].set()
        if name in ("terminal", "host"):
            msg = await receive()
            received.append((name, msg["text"]))
        else:
            await release_output.wait()
            await send(
                {"type": "http.response.body", "body": b"private", "more_body": True}
                if sse
                else {"type": "websocket.send", "text": "private"}
            )

    gate = ui_server._SetupNetworkMiddleware(consumer)

    async def run(name):
        host = name == "host"

        async def send(message):
            events[name].append(message)

        async def receive():
            await inbox.get()
            return {"type": "websocket.receive", "text": "first"}

        await gate(
            {
                "type": "http" if name == "sse" else "websocket",
                "path": "/" + name,
                "method": "GET",
                "client": ("127.0.0.1" if host else "192.168.2.4", 42100),
                "headers": [(b"host", b"localhost" if host else b"192.168.2.3")],
            },
            receive,
            send,
        )

    tasks = [asyncio.create_task(run(name)) for name in events]
    try:
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in ready.values())), 5)
        # Complete this publication before delivering a key or making output ready.
        # The parent does not share any Python cache, Event, or lock with the writer.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from vibe.api import save_config; "
                'save_config({"remote_access":{"vibe_cloud":{"instance_id":"fixture",'
                '"instance_secret":"bound","session_secret":"fixture-cookie"}}})',
            ],
            check=True,
            timeout=15,
        )
        release_output.set()
        for _ in range(2):
            inbox.put_nowait(True)
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert received == [("host", "first")]
        for name in ("terminal", "hmr"):
            assert events[name] == [{"type": "websocket.accept"}, {"type": "websocket.close", "code": 1008}]
        assert events["sse"][-1] == {"type": "http.response.body", "body": b""}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True], ids=["bound", "snapshot-error"])
async def test_idle_terminal_closes_on_first_tick_and_host_terminal_stays_open(monkeypatch, tmp_path, unavailable):
    from starlette.websockets import WebSocketDisconnect
    from vibe import setup_network_stream

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")
    monkeypatch.setattr(ui_server, "TERMINAL_SUPPORTED", True)
    _unbound()
    writes = []

    class Terminal:
        def start_reaper(self):
            pass

        async def handle_websocket(self, websocket, session_id, *, initial_cwd):
            await websocket.send_text("ready")
            try:
                while True:
                    value = await websocket.receive_text()
                    writes.append((session_id, value))
                    await websocket.send_text("received")
            except WebSocketDisconnect:
                pass

    monkeypatch.setattr(ui_server, "get_terminal_service", Terminal)
    assert 0 < setup_network_stream.CHECK_INTERVAL_SECONDS <= 1

    def open_terminal(name, host, peer):
        sent, incoming = asyncio.Queue(), asyncio.Queue()
        incoming.put_nowait({"type": "websocket.connect"})
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "http_version": "1.1",
            "path": "/api/terminal/" + name,
            "query_string": b"",
            "client": (peer, 1234),
            "server": (host, 15131),
            "headers": [(b"host", host.encode() + b":15131"), (b"origin", ("http://" + host + ":15131").encode())],
        }
        return asyncio.create_task(ui_server.app(scope, incoming.get, sent.put)), incoming, sent

    both = [open_terminal("idle", "192.168.2.3", "192.168.2.4"), open_terminal("host", "127.0.0.1", "127.0.0.1")]
    try:
        for _, _, sent in both:
            assert (await asyncio.wait_for(sent.get(), 3))["type"] == "websocket.accept"
            assert (await asyncio.wait_for(sent.get(), 3))["text"] == "ready"
        if unavailable:
            from vibe import network_position

            def failed_read(*args):
                raise RuntimeError("snapshot reader unavailable")

            monkeypatch.setattr(network_position, "_config_revision", failed_read)
            response = ui_server.app.test_client().get("/api/session", base_url=LAN, environ_base=LAN_PEER)
            assert response.status_code == 503 and response.get_json()["error"] == "remote_access_config_unavailable"
        else:
            _bind(V2Config.load()).save()
        _, _, idle_out = both[0]
        _, host_in, host_out = both[1]
        # No receive or protected send on the old origin can wake the guard.
        assert (await asyncio.wait_for(idle_out.get(), 2))["code"] == 1008
        await host_in.put({"type": "websocket.receive", "text": "still-local"})
        assert (await asyncio.wait_for(host_out.get(), 2))["text"] == "received"
        assert writes == [("host", "still-local")]
        await host_in.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(asyncio.gather(*(task for task, _, _ in both)), 3)
    finally:
        for task, _, _ in both:
            task.cancel()
        await asyncio.gather(*(task for task, _, _ in both), return_exceptions=True)

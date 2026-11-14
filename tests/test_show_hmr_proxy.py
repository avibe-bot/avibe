"""Show HMR admission and lifetime against an actual WebSocket upstream.

The old proxy accepted before dialing, selected vite-hmr for Vite's vite-ping
recovery probes, and left the opposite pump pending on an upstream close.
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web
from starlette.websockets import WebSocket

from config import paths
from core.show_runtime import (
    ShowRuntimeContext,
    ShowRuntimeManager,
    ShowRuntimeProtocolEnvelope,
    ShowRuntimeUnavailableError,
    ShowRuntimeWebSocketTarget,
    set_show_runtime_manager_for_tests,
)
from tests.test_ui_show_pages import _create_show_page
from tests.ui_server_test_helpers import _save_config
from vibe import ui_server


class Browser:
    def __init__(self, path, protocol="vite-hmr"):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.messages = []
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.socket = WebSocket(
            {
                "type": "websocket", "scheme": "ws", "path": path,
                "query_string": b"token=test-token" if protocol == "vite-hmr" else b"",
                "headers": [(b"host", b"127.0.0.1:5123"), (b"origin", b"http://127.0.0.1:5123"),
                            (b"sec-websocket-protocol", protocol.encode())],
                "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 5123),
                "subprotocols": [protocol],
            }, self.incoming.get, self.send,
        )

    async def send(self, message):
        self.messages.append(message)
        self.outgoing.put_nowait(message)

    async def receive(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)


@asynccontextmanager
async def upstream_server(handler):
    app = web.Application()
    app.router.add_get("/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class Runtime:
    def __init__(self, url):
        self.url = url
        self.targets = []
        self.invalidations = []

    async def websocket_target(self, path, *, envelope):
        self.targets.append((path, envelope.headers()))
        return ShowRuntimeWebSocketTarget(url=self.url + path, headers=envelope.headers())

    async def invalidate_websocket_target(self, target, **_kwargs):
        self.invalidations.append(target)

    def stop(self):
        pass


@pytest.fixture
def page(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)
    return _create_show_page("ses123", "public")


def route(browser, share_id, public):
    if public:
        return ui_server.public_show_runtime_hmr_websocket(browser.socket, share_id)
    return ui_server.show_runtime_hmr_websocket(browser.socket, "ses123")


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("status", [404, 503])
def test_hmr_rejects_upstream_failure_before_browser_upgrade(page, public, status):
    async def run():
        async def reject(_request):
            return web.Response(status=status)
        async with upstream_server(reject) as url:
            manager = Runtime(url)
            set_show_runtime_manager_for_tests(manager)
            browser = Browser(f"/p/{page}/__vite_hmr" if public else "/show/ses123/__vite_hmr")
            await asyncio.wait_for(route(browser, page, public), 2)
            assert [m["type"] for m in browser.messages] == ["websocket.close"]
            # An application rejection must not retire a healthy shared process.
            assert manager.invalidations == []
    asyncio.run(run())


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("protocol", ["vite-hmr", "vite-ping"])
def test_hmr_preserves_protocol_and_closes_both_pumps(page, public, protocol):
    async def run():
        seen = []
        upstream_closed = asyncio.Event()
        async def serve(request):
            seen.append((request.path_qs, request.headers.get("X-Avibe-Show-Context")))
            ws = web.WebSocketResponse(protocols=[protocol])
            await ws.prepare(request)
            if protocol == "vite-hmr":
                await ws.send_str('{"type":"connected"}')
                assert (await ws.receive()).data == "client-message"
                await ws.send_bytes(b"update")
            await ws.close(code=1001)
            upstream_closed.set()
            return ws
        async with upstream_server(serve) as url:
            set_show_runtime_manager_for_tests(Runtime(url))
            path = f"/p/{page}/__vite_hmr" if public else "/show/ses123/__vite_hmr"
            browser = Browser(path, protocol)
            task = asyncio.create_task(route(browser, page, public))
            try:
                accepted = await browser.receive()
                assert accepted == {"type": "websocket.accept", "subprotocol": protocol, "headers": []}
                if protocol == "vite-hmr":
                    assert (await browser.receive())["text"] == '{"type":"connected"}'
                    browser.incoming.put_nowait({"type": "websocket.receive", "text": "client-message"})
                    assert (await browser.receive())["bytes"] == b"update"
                assert (await browser.receive())["type"] == "websocket.close"
                await asyncio.wait_for(task, 2)
                await asyncio.wait_for(upstream_closed.wait(), 2)
                assert seen == [(path + ("?token=test-token" if protocol == "vite-hmr" else ""),
                                 "shared" if public else "private")]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("end", ["disconnect", "cancel"])
def test_hmr_disposal_closes_upstream(page, public, end):
    async def run():
        closed = asyncio.Event()
        async def serve(request):
            ws = web.WebSocketResponse(protocols=["vite-hmr"])
            await ws.prepare(request)
            async for _message in ws:
                pass
            closed.set()
            return ws
        async with upstream_server(serve) as url:
            set_show_runtime_manager_for_tests(Runtime(url))
            browser = Browser(f"/p/{page}/__vite_hmr" if public else "/show/ses123/__vite_hmr")
            task = asyncio.create_task(route(browser, page, public))
            try:
                assert (await browser.receive())["type"] == "websocket.accept"
                if end == "cancel":
                    task.cancel()
                else:
                    browser.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
                await asyncio.wait_for(closed.wait(), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("public", [False, True])
def test_hmr_missing_workspace_never_contacts_runtime(page, public):
    import shutil
    shutil.rmtree(paths.get_show_page_dir("ses123"))
    manager = Runtime("ws://127.0.0.1:1")
    set_show_runtime_manager_for_tests(manager)
    browser = Browser(f"/p/{page}/__vite_hmr" if public else "/show/ses123/__vite_hmr")
    asyncio.run(route(browser, page, public))
    assert manager.targets == []
    assert [m["type"] for m in browser.messages] == ["websocket.close"]
    assert not paths.get_show_page_dir("ses123").exists()


def test_hmr_target_never_starts_runtime(monkeypatch, tmp_path):
    manager = ShowRuntimeManager(workspace_root=tmp_path / "show", runtime_dir=tmp_path / "runtime")
    calls = []
    async def ensure(**_kwargs):
        calls.append("start")
        return SimpleNamespace(available=True, base_url="http://127.0.0.1:1234")
    monkeypatch.setattr(manager, "ensure", ensure)
    async def negotiate(_url):
        pass
    monkeypatch.setattr(manager, "_negotiate_context_key_capability", negotiate)
    with pytest.raises(ShowRuntimeUnavailableError):
        asyncio.run(manager.websocket_target("/show/ses123/__vite_hmr", envelope=ShowRuntimeProtocolEnvelope(ShowRuntimeContext.PRIVATE)))
    assert calls == []
